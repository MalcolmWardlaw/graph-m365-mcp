# /// script
# requires-python = ">=3.11"
# dependencies = ["msal", "msal-extensions", "httpx", "mcp", "html2text"]
# ///
"""Local stdio MCP: Microsoft 365 mail + personal calendar via Microsoft Graph.

Capability flags (env) drive both the OAuth scopes requested at token time and
the tools registered on the FastMCP server. Requested scopes are the union over
enabled capabilities; the AAD-consented grant is only the ceiling.
"""
import os
import re
import sys
import time
import hashlib
import unicodedata
from datetime import datetime, timezone, timedelta

import msal
import httpx
import html2text
from mcp.server.fastmcp import FastMCP

from msal_extensions import PersistedTokenCache, FilePersistence
from msal_extensions.persistence import KeychainPersistence

CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else ""
GRAPH     = "https://graph.microsoft.com/v1.0"

# Keychain service name (and the non-secret cache-signal path) both key off
# this. Defaults to the server name; override KEYCHAIN_SERVICE to reuse a
# refresh token minted under a different name (e.g. migrating an older install).
KEYCHAIN_SERVICE = os.environ.get("KEYCHAIN_SERVICE", "graph-m365")

# Non-secret marker file. The refresh token lives in the macOS login Keychain;
# this file only signals cache state to msal-extensions.
SIGNAL = os.path.expanduser(f"~/.config/{KEYCHAIN_SERVICE}/cache.signal")


# ---------------------------------------------------------------------------
# Capability config — single source of truth for scopes AND tool registration.

CAPS = {
    "mail_read":  os.environ.get("MAIL_READ",  "1") == "1",
    "mail_write": os.environ.get("MAIL_WRITE", "1") == "1",  # move/archive/delete/mark
    "mail_draft": os.environ.get("MAIL_DRAFT", "1") == "1",
    "mail_send":  os.environ.get("MAIL_SEND",  "0") == "1",  # DARK by default
    "cal_read":   os.environ.get("CAL_READ",   "1") == "1",  # phase 2
    "cal_write":  os.environ.get("CAL_WRITE",  "1") == "1",  # phase 2
}


def required_scopes() -> list[str]:
    s: set[str] = set()
    if CAPS["mail_read"]:
        s.add("Mail.Read")
    if CAPS["mail_write"] or CAPS["mail_draft"]:
        s.add("Mail.ReadWrite")        # superset of Mail.Read
    if CAPS["mail_send"]:
        s.add("Mail.Send")
    if CAPS["cal_read"]:
        s.add("Calendars.Read")
    if CAPS["cal_write"]:
        s.add("Calendars.ReadWrite")
    return sorted(s)


SCOPES = required_scopes()


# ---------------------------------------------------------------------------
# Folder exclusion — fail-closed gate on configured folders.
# Names listed in EXCLUDE_FOLDERS (set by the host) are hard-blocked from the
# three read tools. Default in code is empty; the value lives in the per-host
# config (e.g. the Claude Desktop env block).

EXCLUDE_FOLDER_NAMES = [
    n.strip() for n in os.environ.get("EXCLUDE_FOLDERS", "").split(",") if n.strip()
]

_EXCLUDED_MAP: dict[str, str] | None = None    # display-name → folder id


def _excluded_folder_map() -> dict[str, str]:
    """Resolve EXCLUDE_FOLDER_NAMES to {displayName: id} on first call; cache for the process.

    Lazy so server.py can import without a token (e.g. for `--auth` bootstrap).
    Fail-closed: any unmatched name raises and prevents the read.
    Keys use Graph's casing of the displayName, not the env's spelling.
    """
    global _EXCLUDED_MAP
    if _EXCLUDED_MAP is not None:
        return _EXCLUDED_MAP
    if not EXCLUDE_FOLDER_NAMES:
        _EXCLUDED_MAP = {}
        return _EXCLUDED_MAP
    d = _req("GET", "/me/mailFolders",
             params={"$select": "id,displayName", "$top": 200})
    by_name = {f["displayName"].casefold(): (f["displayName"], f["id"])
               for f in d.get("value", [])}
    pairs: dict[str, str] = {}
    for name in EXCLUDE_FOLDER_NAMES:
        hit = by_name.get(name.casefold())
        if hit is None:
            raise RuntimeError(
                f"EXCLUDE_FOLDERS: no top-level folder named {name!r}; refusing to read"
            )
        canonical, fid = hit
        pairs[canonical] = fid
    _EXCLUDED_MAP = pairs
    return _EXCLUDED_MAP


def _excluded_folder_ids() -> frozenset[str]:
    return frozenset(_excluded_folder_map().values())


# ---------------------------------------------------------------------------
# Credential layer — Keychain via msal-extensions.

def _build_cache() -> PersistedTokenCache:
    os.makedirs(os.path.dirname(SIGNAL), exist_ok=True)
    try:
        # account_name is generic ("default-mailbox") so the code carries no
        # institution-specific signal. service_name comes from KEYCHAIN_SERVICE
        # (default "graph-m365"); set it to an older name to reuse a refresh
        # token minted under that name without re-auth.
        persistence = KeychainPersistence(
            SIGNAL,
            service_name=KEYCHAIN_SERVICE,
            account_name="default-mailbox",
        )
    except Exception:
        # Dev-only fallback. Do not point SIGNAL inside the repo or any synced dir.
        persistence = FilePersistence(SIGNAL)
    return PersistedTokenCache(persistence)


_CACHE = _build_cache()


def _app() -> msal.PublicClientApplication:
    if not CLIENT_ID or not TENANT_ID:
        raise RuntimeError(
            "GRAPH_CLIENT_ID and GRAPH_TENANT_ID must be set (point at your registered AAD public-client app)"
        )
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=_CACHE)


def _token() -> str:
    app = _app()
    accts = app.get_accounts()
    if not accts:
        raise RuntimeError("No cached account. Run: uv run server.py --auth")
    res = app.acquire_token_silent(SCOPES, account=accts[0])
    if not res or "access_token" not in res:
        raise RuntimeError(f"token acquisition failed: {res}")
    return res["access_token"]


# ---------------------------------------------------------------------------
# HTTP helper.

# Request immutable ids globally so a message id is stable across folder moves
# AND process restarts. This must be global — mixing immutable and default ids
# in one process makes Graph reject the foreign id as "malformed". Transition
# note: ids minted before this was enabled won't match afterward; the first scan
# after deploy reissues handles, so just re-scan. Nothing persistent stores raw ids.
_BASE_PREFER = 'IdType="ImmutableId"'

_MAX_TOP = 200      # post-diet, 200 metadata rows is a few k tokens; hard ceiling on one call
_MAX_RETRIES = 4


def _req(method: str, path: str, *, params=None, json=None, extra_headers=None):
    headers = {"Authorization": f"Bearer {_token()}", "Prefer": _BASE_PREFER}
    if params and "$search" in params:
        headers["ConsistencyLevel"] = "eventual"
    if extra_headers:                       # merge, don't clobber, Prefer (calendar sets one)
        eh = dict(extra_headers)
        extra_prefer = eh.pop("Prefer", None)
        headers.update(eh)
        if extra_prefer:
            headers["Prefer"] = f"{_BASE_PREFER}, {extra_prefer}"

    url = f"{GRAPH}{path}"
    r = None
    for attempt in range(_MAX_RETRIES + 1):
        r = httpx.request(method, url, headers=headers,
                          params=params, json=json, timeout=30)
        # Bulk writes burst into Graph's throttle; honor Retry-After with bounded
        # backoff so a single 429/503 degrades gracefully instead of aborting.
        if r.status_code in (429, 503) and attempt < _MAX_RETRIES:
            delay = float(r.headers.get("Retry-After", 2 ** attempt))
            time.sleep(min(delay, 30))
            continue
        break

    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            raise RuntimeError(
                f"Graph {r.status_code}: {err.get('code')} — {err.get('message')}"
            )
        except ValueError:
            r.raise_for_status()
    return r.json() if r.content else {}


def _get(path, params=None):
    return _req("GET", path, params=params)


# ---------------------------------------------------------------------------
# Batch helper — Graph's /$batch runs up to 20 sub-requests per round-trip.

def _batch(requests: list[dict]) -> list[dict]:
    """Run Graph requests in chunks of 20; return responses in request order.
    Each request: {"method","url","body"(opt),"headers"(opt)}.
    A per-sub-request 429 surfaces in the response with status 429 — the caller
    can re-issue just those rather than the whole batch."""
    results: list[dict] = []
    for i in range(0, len(requests), 20):
        chunk = [dict(r) for r in requests[i:i + 20]]
        for n, rq in enumerate(chunk):
            rq["id"] = str(n)
            rq.setdefault("headers", {})["Prefer"] = _BASE_PREFER   # id-format consistency
        resp = _req("POST", "/$batch", json={"requests": chunk})
        by_id = {r["id"]: r for r in resp.get("responses", [])}
        results.extend(by_id[str(n)] for n in range(len(chunk)))
    return results


# handle <-> real Graph id. Deterministic (same id always maps to same handle),
# repopulated on every scan; lives for the process lifetime. The model reasons
# over short handles (~3-4 tokens) instead of ~152-char base64 ids (~89 tokens).
# The map is in-process and rebuilt each scan, so a handle is valid for the
# session. After a restart, a stale handle isn't in the map; _resolve passes the
# 8-char string through to Graph, yielding a clean 404 rather than a wrong target
# — acceptable because the model always re-scans before acting. 32-bit digest:
# collisions are negligible at inbox scale (birthday bound ~77k live ids).
_HANDLES: dict[str, str] = {}


def _handle(real_id: str) -> str:
    h = hashlib.blake2s(real_id.encode(), digest_size=4).hexdigest()  # 8 hex chars
    _HANDLES[h] = real_id
    return h


def _resolve(ref: str) -> str:
    # Accept either a handle we issued or a raw id (fallthrough keeps it robust).
    return _HANDLES.get(ref, ref)


# HTML→Markdown converter for message bodies. Built once; html2text's defaults
# wrap at 78 cols and emit images, both of which hurt agent consumption.
_h2t = html2text.HTML2Text()
_h2t.body_width = 0          # no hard wrap — keeps links intact, friendlier to agents
_h2t.ignore_images = True    # tracking pixels and inline images are pure noise
_h2t.single_line_break = True
_h2t.escape_snob = False     # don't escape every punctuation char in prose


# --- vendored from fastmail-clean-mcp/fastmail_clean.py (source of truth) -----
# Post-pass that runs AFTER html2text: strips invisible preview padding and
# tracking URLs, then optionally truncates to a meaningful head. Pure stdlib;
# keep in sync with fastmail_clean.py if the tracker heuristics there change.
_ZERO_WIDTH = {
    0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,   # ZWSP, ZWNJ, ZWJ, word-joiner, BOM
    0x00AD,                                    # soft hyphen
    0x034F,                                    # combining grapheme joiner (the big one)
    0x115F, 0x1160, 0x3164, 0xFFA0,            # Hangul filler family
}
_TRACKER_PARAMS = re.compile(
    r"(?:[?&])(?:utm_[a-z]+|euid|tuid|pid|configId|mc_eid|mc_cid|s_id|ct|"
    r"ss_source|ss_campaign\w*|ss_email\w*|src_section)=",
    re.I,
)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+", re.I)
_ANGLE_URL = re.compile(r"<https?://[^>]+>")
_OPENTRACK = re.compile(r"%opentrack%")
# Graph-specific: html2text renders <a href> as [text](url). Collapse tracker
# links to just their anchor text instead of leaving a degenerate [text]( ).
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")


def _strip_invisibles(s: str) -> str:
    out = []
    for ch in s:
        if ord(ch) in _ZERO_WIDTH:
            continue
        if unicodedata.category(ch) in ("Cf", "Mn"):
            continue
        out.append(ch)
    return "".join(out)


def _kill_tracking_urls(s: str, max_url_len: int = 80) -> str:
    def md_repl(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if len(url) > max_url_len or _TRACKER_PARAMS.search(url):
            return text          # drop the tracker URL, keep the link text
        return m.group(0)        # keep short, clean markdown links intact

    s = _MD_LINK.sub(md_repl, s)
    s = _ANGLE_URL.sub(" ", s)
    s = _OPENTRACK.sub(" ", s)

    def repl(m: re.Match) -> str:
        url = m.group(0)
        if len(url) > max_url_len or _TRACKER_PARAMS.search(url):
            return " "
        return url

    return _URL.sub(repl, s)


def _postclean(text: str | None, head_chars: int | None = None) -> str | None:
    """Strip invisibles + tracking URLs from html2text output, collapse whitespace,
    and optionally keep only the meaningful head. None/empty passes through."""
    if not text:
        return text
    text = _strip_invisibles(text)
    text = _kill_tracking_urls(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if head_chars is not None:
        text = text[:head_chars].rstrip()
    return text


# Defense against a text/plain body that is actually HTML (mislabeled sender).
# Graph normally normalizes contentType, so this rarely bites — free insurance.
_HTML_HINT = re.compile(
    r"<\s*(table|div|span|td|tr|tbody|html|body|a\s|img\s|style|p\s|br\b)", re.I
)


def _looks_like_html(s: str) -> bool:
    return bool(_HTML_HINT.search(s)) or s.count("<") > 15


def _body_to_markdown(field: dict | None) -> str | None:
    if not field:
        return None
    content = field.get("content")
    if not content:
        return content
    if field.get("contentType") == "html" or _looks_like_html(content):
        return _h2t.handle(content).strip()
    return content


def _summ(m: dict) -> dict:
    return {
        "id": _handle(m["id"]),          # short handle, not the ~152-char raw id
        "subject": m.get("subject"),
        "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
        "received": m.get("receivedDateTime"),
        "isRead": m.get("isRead"),
        # bodyPreview intentionally omitted: scan is metadata-only
    }


def _recipients(addrs):
    return [{"emailAddress": {"address": a}} for a in (addrs or [])]


# ---------------------------------------------------------------------------
# Mail tool implementations (registration is conditional, see bottom).

INSTRUCTIONS = (
    "This server is intentionally read-and-stage only on mail and scoped to the "
    "user's default personal calendar on events. It exposes draft creation (not "
    "send) and soft-delete (not hard-delete) for mail by design. Mail send and "
    "permanent deletion are deliberately omitted; shared/delegated mailboxes and "
    "non-default calendars are deliberately out of scope. Folders listed in "
    "EXCLUDE_FOLDERS (set by the host) are hard-excluded from list/search/get by "
    "design; reads against them return an error and there is no override. Do not "
    "describe these absences as limitations or missing features."
)

mcp = FastMCP("graph-m365", instructions=INSTRUCTIONS)


@mcp.tool()
def list_messages(top: int = 25, folder: str = "inbox") -> list[dict]:
    """Recent messages, newest first. folder: inbox, sentitems, drafts, archive, ..."""
    ids = _excluded_folder_ids()
    cf = folder.casefold()
    if folder in ids or any(cf == n.casefold() for n in EXCLUDE_FOLDER_NAMES):
        raise RuntimeError(f"folder {folder!r} is excluded by EXCLUDE_FOLDERS")
    d = _get(
        f"/me/mailFolders/{folder}/messages",
        {
            "$top": min(top, _MAX_TOP),
            "$select": "id,subject,from,receivedDateTime,isRead",
            "$orderby": "receivedDateTime desc",
        },
    )
    return [_summ(m) for m in d.get("value", [])]


@mcp.tool()
def search_messages(query: str, top: int = 25) -> list[dict]:
    """Full-text mailbox search. No $orderby allowed alongside $search."""
    ids = _excluded_folder_ids()
    d = _get(
        "/me/messages",
        {
            "$search": f'"{query}"',
            "$top": min(top, _MAX_TOP),
            "$select": "id,subject,from,receivedDateTime,isRead,parentFolderId",
        },
    )
    return [_summ(m) for m in d.get("value", []) if m.get("parentFolderId") not in ids]


@mcp.tool()
def get_message(message_id: str, mode: str = "unique", head_chars: int | None = None) -> dict:
    """One message with Markdown body. mode='unique' (default) returns just the new content; mode='full' returns the whole message including quoted thread history. Set head_chars to cap the (sanitized) body length."""
    if mode not in ("unique", "full"):
        raise ValueError(f"mode must be 'unique' or 'full', got {mode!r}")
    field = "uniqueBody" if mode == "unique" else "body"
    m = _get(
        f"/me/messages/{_resolve(message_id)}",
        {"$select": f"id,subject,from,toRecipients,receivedDateTime,parentFolderId,{field}"},
    )
    if m.get("parentFolderId") in _excluded_folder_ids():
        raise RuntimeError(f"message {message_id} is in an excluded folder")
    return {
        "id": _handle(m["id"]),
        "subject": m.get("subject"),
        "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
        "to": [
            (r.get("emailAddress") or {}).get("address")
            for r in (m.get("toRecipients") or [])
        ],
        "received": m.get("receivedDateTime"),
        "bodyMode": mode,
        "body": _postclean(_body_to_markdown(m.get(field)), head_chars),
    }


@mcp.tool()
def triage_messages(top: int = 25, folder: str = "inbox", head_chars: int = 600) -> list[dict]:
    """Bulk triage: recent messages with sanitized, head-truncated bodies in ONE call —
    stream these into context to categorize, instead of N get_message round-trips. Bodies
    are cleaned (invisible padding + tracking URLs stripped) and capped at head_chars."""
    ids = _excluded_folder_ids()
    cf = folder.casefold()
    if folder in ids or any(cf == n.casefold() for n in EXCLUDE_FOLDER_NAMES):
        raise RuntimeError(f"folder {folder!r} is excluded by EXCLUDE_FOLDERS")
    # Full `body` (not uniqueBody, which is unreliable in a list $select); head_chars
    # trims quoted history for top-posted mail, which is sufficient for triage.
    d = _get(
        f"/me/mailFolders/{folder}/messages",
        {
            "$top": min(top, 50),
            "$select": "id,subject,from,receivedDateTime,isRead,body",
            "$orderby": "receivedDateTime desc",
        },
    )
    return [
        {
            "id": _handle(m["id"]),
            "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
            "subject": m.get("subject"),
            "received": m.get("receivedDateTime"),
            "isRead": m.get("isRead"),
            "body": _postclean(_body_to_markdown(m.get("body")), head_chars),
        }
        for m in d.get("value", [])
    ]


def _walk_folders(path="/me/mailFolders", depth=0, max_depth=4) -> list[dict]:
    excluded = _excluded_folder_ids()
    d = _get(path, {"$select": "id,displayName,parentFolderId,childFolderCount,"
                              "totalItemCount,unreadItemCount", "$top": 200})
    out: list[dict] = []
    for f in d.get("value", []):
        if f["id"] in excluded:                 # fail-closed: skip excluded subtree
            continue
        out.append(f)
        if depth < max_depth and (f.get("childFolderCount") or 0) > 0:
            out.extend(_walk_folders(f"/me/mailFolders/{f['id']}/childFolders",
                                     depth + 1, max_depth))
    return out


@mcp.tool()
def list_mail_folders() -> list[dict]:
    """Mail folders as {id(handle), name, parent, total, unread}. Excluded folders omitted.
    Use the returned id as a `move` destination to file into a custom folder."""
    # EXCLUDE_FOLDERS matching is top-level by design (see _excluded_folder_map);
    # nested protected folders should be named at the top level.
    return [
        {"id": _handle(f["id"]),
         "name": f.get("displayName"),
         "parent": _handle(f["parentFolderId"]) if f.get("parentFolderId") else None,
         "total": f.get("totalItemCount"),
         "unread": f.get("unreadItemCount")}
        for f in _walk_folders()
    ]


# Internal helper, NOT a registered tool. The single-message move path is kept
# only for archive_message; the model-facing surface is the batched move_messages
# (a one-element list is valid), so there is no singular move tool to choose.
def move_message(message_id: str, destination: str) -> dict:
    dest = _resolve(destination)        # folder handle -> id; well-known names pass through
    m = _req("POST", f"/me/messages/{_resolve(message_id)}/move",
             json={"destinationId": dest})
    return {"id": _handle(m.get("id")), "parentFolderId": m.get("parentFolderId")}


def archive_message(message_id: str) -> dict:
    """Move a message to the Archive folder."""
    return move_message(message_id, "archive")


# Write surface is batched-only: move_messages/delete_messages/mark_messages each
# take a list (pass one id for a one-off). One batched call per 20 items instead
# of N round-trips, the difference between a graceful triage and a throttling
# magnet. A per-sub-request 429 surfaces in failed[] with status 429, so the
# caller can re-issue just those ids. Soft-delete only — no hard-delete by design.

def _batch_summary(inputs: list[str], resp: list[dict]) -> dict:
    """Compact result: counts + per-failure detail keyed by the input handle the model passed."""
    failed = [{"id": inputs[i], "status": r.get("status"),
               "error": (r.get("body") or {}).get("error", {}).get("code")}
              for i, r in enumerate(resp) if r.get("status", 500) >= 300]
    return {"requested": len(inputs), "ok": len(inputs) - len(failed), "failed": failed}


def move_messages(message_ids: list[str], destination: str) -> dict:
    """Move many messages in one batched call. ids: handles or raw ids.
    destination: well-known name (archive/junkemail/deleteditems) or a folder handle/id."""
    dest = _resolve(destination)                       # folder handle -> id; well-known passes through
    reqs = [{"method": "POST", "url": f"/me/messages/{_resolve(m)}/move",
             "body": {"destinationId": dest},
             "headers": {"Content-Type": "application/json"}} for m in message_ids]
    return _batch_summary(message_ids, _batch(reqs))


def delete_messages(message_ids: list[str]) -> dict:
    """Soft-delete messages to Deleted Items in one batched call. Recoverable;
    there is no hard-delete tool by design. Pass one id for a single message."""
    reqs = [{"method": "DELETE", "url": f"/me/messages/{_resolve(m)}"} for m in message_ids]
    return _batch_summary(message_ids, _batch(reqs))


def mark_messages(message_ids: list[str], is_read: bool = True) -> dict:
    """Mark many messages read/unread in one batched call."""
    reqs = [{"method": "PATCH", "url": f"/me/messages/{_resolve(m)}",
             "body": {"isRead": is_read},
             "headers": {"Content-Type": "application/json"}} for m in message_ids]
    return _batch_summary(message_ids, _batch(reqs))


def create_draft(to: list[str], subject: str, body: str,
                 cc: list[str] | None = None) -> dict:
    """Create a draft in Drafts (send is by design out of scope — user sends from Outlook). Returns id and webLink."""
    payload = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _recipients(to),
    }
    if cc:
        payload["ccRecipients"] = _recipients(cc)
    m = _req("POST", "/me/messages", json=payload)
    return {"id": m.get("id"), "webLink": m.get("webLink")}


def send_message(to: list[str], subject: str, body: str,
                 cc: list[str] | None = None) -> dict:
    """Create a draft, then send it. Leaves a reviewable artifact in Sent Items."""
    draft = create_draft(to, subject, body, cc=cc)
    _req("POST", f"/me/messages/{draft['id']}/send")
    return {"id": draft["id"], "sent": True}


def excluded_recent_count(hours: int = 24) -> dict:
    """Counts of messages received in excluded folders over the last `hours` hours. Metadata only — never returns message ids, subjects, previews, or bodies."""
    fmap = _excluded_folder_map()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    counts: dict[str, int] = {}
    for name, fid in fmap.items():
        d = _req(
            "GET", f"/me/mailFolders/{fid}/messages",
            params={
                "$filter": f"receivedDateTime ge {since}",
                "$select": "id",
                "$top": 1,
                "$count": "true",
            },
            extra_headers={"ConsistencyLevel": "eventual"},
        )
        counts[name] = int(d.get("@odata.count") or 0)
    return {"windowHours": hours, "since": since, "counts": counts}


# ---------------------------------------------------------------------------
# Calendar (default personal calendar only; never enumerate /me/calendars).

# Windows/Outlook timezone name applied to calendar reads and writes. Defaults
# to Eastern; override CAL_TIMEZONE (e.g. "Pacific Standard Time", "UTC").
CAL_TZ = os.environ.get("CAL_TIMEZONE", "Eastern Standard Time")
CAL_TZ_HEADER = {"Prefer": f'outlook.timezone="{CAL_TZ}"'}


def _event_summ(e: dict) -> dict:
    start = (e.get("start") or {})
    end   = (e.get("end") or {})
    loc   = (e.get("location") or {})
    return {
        "id": e["id"],
        "subject": e.get("subject"),
        "start": start.get("dateTime"),
        "end":   end.get("dateTime"),
        "tz":    start.get("timeZone") or end.get("timeZone"),
        "isAllDay": e.get("isAllDay"),
        "location": loc.get("displayName"),
        "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("address"),
        "webLink": e.get("webLink"),
    }


def _attendees(addrs):
    # Adding attendees to a created/updated event causes Outlook to send invites.
    return [{"emailAddress": {"address": a}, "type": "required"} for a in (addrs or [])]


def _dt(dt: str, tz: str = CAL_TZ) -> dict:
    return {"dateTime": dt, "timeZone": tz}


def list_events(start: str, end: str, top: int = 50) -> list[dict]:
    """Events between start and end (ISO 8601). Uses calendarView so recurrences expand. Times returned in the configured timezone (CAL_TIMEZONE, default Eastern)."""
    d = _req(
        "GET", "/me/calendarView",
        params={
            "startDateTime": start,
            "endDateTime": end,
            "$top": top,
            "$select": "id,subject,start,end,isAllDay,location,organizer,webLink",
            "$orderby": "start/dateTime",
        },
        extra_headers=CAL_TZ_HEADER,
    )
    return [_event_summ(e) for e in d.get("value", [])]


def get_event(event_id: str, head_chars: int | None = None) -> dict:
    """One event including a sanitized Markdown body. Times in the configured timezone (CAL_TIMEZONE, default Eastern).
    Event bodies are often KB of HTML join-link boilerplate (Teams/Zoom); set
    head_chars to cap the cleaned body."""
    e = _req(
        "GET", f"/me/events/{_resolve(event_id)}",
        params={"$select": "id,subject,start,end,isAllDay,location,organizer,attendees,body,webLink"},
        extra_headers=CAL_TZ_HEADER,
    )
    s = _event_summ(e)
    s["body"] = _postclean(_body_to_markdown(e.get("body")), head_chars)
    s["attendees"] = [
        ((a.get("emailAddress") or {}).get("address"))
        for a in (e.get("attendees") or [])
    ]
    return s


def create_event(subject: str, start: str, end: str,
                 body: str | None = None, location: str | None = None,
                 attendees: list[str] | None = None) -> dict:
    """Create an event on the default calendar. Times are in the configured timezone (CAL_TIMEZONE, default Eastern) unless an ISO offset is present. Attendees, if any, trigger Outlook invites."""
    payload: dict = {
        "subject": subject,
        "start": _dt(start),
        "end":   _dt(end),
    }
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = _attendees(attendees)
    e = _req("POST", "/me/events", json=payload)
    return _event_summ(e)


def update_event(event_id: str, fields: dict) -> dict:
    """Patch an event with the given fields (e.g. {'subject': ...}, {'start': {'dateTime': ..., 'timeZone': ...}}). Adding attendees here also triggers invites."""
    e = _req("PATCH", f"/me/events/{event_id}", json=fields)
    return _event_summ(e)


def delete_event(event_id: str) -> dict:
    """Delete an event from the default calendar. If you are the organizer, attendees receive cancellations."""
    _req("DELETE", f"/me/events/{event_id}")
    return {"id": event_id, "deleted": True}


# ---------------------------------------------------------------------------
# Conditional registration — the dial. With a capability off, its tool is NOT
# registered AND its scope is NOT in SCOPES.

if CAPS["mail_write"]:
    for fn in (archive_message, move_messages, delete_messages, mark_messages):
        mcp.tool()(fn)

if CAPS["mail_draft"]:
    mcp.tool()(create_draft)

if CAPS["mail_send"]:
    mcp.tool()(send_message)

if EXCLUDE_FOLDER_NAMES:
    mcp.tool()(excluded_recent_count)

if CAPS["cal_read"]:
    for fn in (list_events, get_event):
        mcp.tool()(fn)

if CAPS["cal_write"]:
    for fn in (create_event, update_event, delete_event):
        mcp.tool()(fn)


# ---------------------------------------------------------------------------
# CLI.

def _auth() -> None:
    res = _app().acquire_token_interactive(scopes=SCOPES)
    if res and "access_token" in res:
        print("OK", file=sys.stderr)
    else:
        err = (res or {}).get("error_description") or (res or {}).get("error") or "unknown"
        print(f"FAILED: {err}", file=sys.stderr)


if __name__ == "__main__":
    if "--auth" in sys.argv:
        _auth()
    else:
        mcp.run()
