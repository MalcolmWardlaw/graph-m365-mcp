# `graph-m365` — local M365 MCP server

Operational summary and design doc for the server.

> The FastMCP server is named `graph-m365` and is registered in Claude Desktop under the same key. The Keychain service name (and the `~/.config/<name>/` signal path) defaults to `graph-m365` as well, but is overridable via `KEYCHAIN_SERVICE` — set it to reuse a refresh token minted under a different name when migrating an existing install, since the entry is keyed by service name and a rename would otherwise force a re-auth round.

---

## State

Single-file stdio MCP (`server.py`, FastMCP) over Microsoft Graph `v1.0`, exposing mail and personal-calendar tools to a Claude Desktop / Claude Code host.

Tools, by capability:

| Cap (env)     | Default | Tools registered                                                                                  | Scope added            |
|---------------|---------|---------------------------------------------------------------------------------------------------|------------------------|
| `MAIL_READ`   | `1`     | `list_messages`, `search_messages`, `get_message`, `triage_messages`, `list_mail_folders`         | `Mail.Read`            |
| `MAIL_WRITE`  | `1`     | `archive_message`, `move_messages`, `delete_messages`, `mark_messages`                            | `Mail.ReadWrite`       |
| `MAIL_DRAFT`  | `1`     | `create_draft`                                                                                    | `Mail.ReadWrite`       |
| `MAIL_SEND`   | `0`     | `send_message` (dark)                                                                             | `Mail.Send`            |
| `CAL_READ`    | `1`     | `list_events`, `get_event`                                                                        | `Calendars.Read`       |
| `CAL_WRITE`   | `1`     | `create_event`, `update_event`, `delete_event`                                                    | `Calendars.ReadWrite`  |

`MAIL_SEND` is the only capability dark by default: requesting `Mail.Send` may trigger a separate admin-consent round, so it stays off unless explicitly enabled (see the re-seed note under Credentials). The mail read tools (`list_messages`, `search_messages`, `get_message`, `triage_messages`, `list_mail_folders`) carry inline `@mcp.tool()` decorators and register unconditionally; they consume `Mail.Read`, which `MAIL_READ=1` requests by default. `excluded_recent_count` registers only when `EXCLUDE_FOLDERS` is non-empty.

**Write surface is batched-only.** `move_messages`, `delete_messages`, `mark_messages` each take a list of ids (pass one for a one-off); there are no singular `move_message`/`delete_message`/`mark_message_read` *tools* — `move_message` survives in code solely as an internal helper for `archive_message`. The plurals subsume the singulars (a one-element batch is valid) while adding `/$batch` chunking and throttle resilience, so exposing both was pure redundancy. Each runs Graph `/$batch` in chunks of 20 and returns `{requested, ok, failed[]}`, where `failed[]` carries per-item status (a per-sub-request `429` surfaces here for selective re-issue) — note a single-item failure is reported in `failed[]`, not raised. `_req` honors `Retry-After` on `429`/`503` with bounded backoff so bursts degrade gracefully. The handle/id echo the singulars used to return is no longer load-bearing: `Prefer: IdType="ImmutableId"` keeps a moved message's id stable, so the original handle stays valid.

**Stable ids.** A global `Prefer: IdType="ImmutableId"` makes message ids stable across folder moves and process restarts (must be global — mixing id formats in one process makes Graph reject the foreign id). Ids leave the server as short deterministic **handles** (`blake2s`, 8 hex chars), not raw ~152-char base64; a process-lifetime `_handle`↔`_resolve` map translates back at the action boundary. Scans drop `bodyPreview` — classification needs only sender + subject; bodies are opt-in via `get_message`/`triage_messages`. Scan `$top` is clamped to `_MAX_TOP=200`.

**Folder discovery.** `list_mail_folders` walks the folder tree (depth ≤ 4) and returns folder handles usable directly as a `move`/`move_messages` destination, enabling routing into custom folders. It respects `EXCLUDE_FOLDERS` (excluded subtrees omitted, fail-closed). `move_messages` `_resolve`s its destination, so a folder handle works; well-known names (`archive`, `junkemail`, `deleteditems`) aren't in the map and pass through.

**One dial, two effects.** With a capability unset, its tool is not registered AND its scope is not requested. This invariant (tool surface = requested scopes) is enforced by `CAPS` → `required_scopes()` in `server.py`; do not bypass it. Requested scopes are the union over enabled caps; the AAD-consented grant is only the ceiling.

**Server-level instructions** (`FastMCP(..., instructions=...)`) declare the read-and-stage design and the personal-calendar scoping. Tool docstrings reinforce design intent at the point of decision for `create_draft` and `delete_messages`. Calendar tool docstrings flag the invite/cancellation semantics that come with `attendees`.

**Body shape.** `get_message` returns the body as Markdown via `html2text` (link targets preserved as `[text](url)`, images/tracking pixels stripped, no hard wrap). It accepts `mode='unique'` (default — Graph's `uniqueBody`, just the new content) or `mode='full'` (whole message including quoted thread history). Plain-text bodies pass through untouched, except a text/plain body that is actually HTML (`_looks_like_html` re-sniff) is still flattened. `list_messages`/`search_messages` no longer emit `bodyPreview` — scan is metadata-only; use `triage_messages` for sanitized, head-capped bodies in one call, or `get_message` for one. `get_event` applies the same sanitizer to event bodies (Teams/Zoom join-link boilerplate) with an opt-in `head_chars` cap.

**Folder exclusion.** `EXCLUDE_FOLDERS` (comma-separated; set in the Desktop `env` block) names top-level folders that are hard-excluded from `list_messages`, `search_messages`, and `get_message`. Default in code is empty so the server.py file carries no mailbox-specific data. Resolution is by display name, done once per process on first read-tool call; any unmatched name raises and refuses to read (fail-closed). The gate is folder-membership only — no content scrubbing — and is read-path only. Write tools (`move_messages`, `delete_messages`) are not source-gated; moving an excluded message into a non-excluded folder is a known, accepted way to surface it. When `EXCLUDE_FOLDERS` is non-empty, a metadata-only `excluded_recent_count(hours=24)` tool is registered alongside the gate; it returns per-folder counts of recent arrivals (via `$count` + `$filter` on `receivedDateTime`) and never returns ids, subjects, previews, or bodies.

---

## AAD app registration

The server expects a **public-client** AAD app registration with **delegated** Microsoft Graph permissions consented for the target tenant. `GRAPH_CLIENT_ID` and `GRAPH_TENANT_ID` (set in the host `env` block) point at it. Single-tenant authority: pass the tenant id, not `common`.

- Required delegated scopes correspond to enabled `CAPS`: `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.Read`, `Calendars.ReadWrite`. The server never requests `*.Shared`.
- No client secret. Public-client redirect URI must be configured (MSAL listens on a localhost port during `--auth`).
- If the tenant requires admin consent, get the grants consented once before first `--auth`. After that, toggling a capability on (changing `CAPS`) works on the next silent acquisition without re-auth, because MSAL can mint a token for any consented scope from the existing refresh token. If silent ever fails after a new toggle, re-run `--auth` once.

---

## Credentials

Refresh token lives in the **macOS login Keychain** via `msal-extensions`:

- Service: value of `KEYCHAIN_SERVICE` (default `graph-m365`; see header note)
- Account: `default-mailbox`
- Inspect: `security find-generic-password -s graph-m365 -a default-mailbox` (adjust `-s` if `KEYCHAIN_SERVICE` is set)

Only `~/.config/<KEYCHAIN_SERVICE>/cache.signal` (+ `.lockfile`) touches disk. The signal is a non-secret marker for `PersistedTokenCache` reload coordination; it must remain outside any repo or synced folder. **Never reintroduce a JSON token file** — earlier versions cached the refresh token in `token_cache.json`; that path is gone and must not return.

Re-seed (must run in a terminal, not via the MCP host):

```fish
cd ~/Documents/projects/graph-m365-mcp
env GRAPH_CLIENT_ID=<your-app-id> GRAPH_TENANT_ID=<your-tenant-id> \
    CAL_READ=1 CAL_WRITE=1 uv run server.py --auth
```

(`GRAPH_CLIENT_ID`/`GRAPH_TENANT_ID` are required here too — they live in the host `env` block, not the shell, so `--auth` from a terminal won't inherit them.)

`--auth` requests exactly the scopes its `CAPS` env resolves to (`required_scopes()`), so the interactive consent round must cover the scopes the *host* will actually use — otherwise a cap that's on in the host but off at auth time only works if its scope happens to be AAD-consented already; if it isn't, silent acquisition fails later inside the host, where no interactive consent is possible. The default re-seed above mirrors the shipped Desktop config (mail on, both calendar caps on) so one consent round covers everything that ships. `MAIL_SEND` is deliberately left off: requesting `Mail.Send` may trigger a *second* admin-consent request, so it stays out of the default seed. If you want send capability now or in the future, decide that up front and add `MAIL_SEND=1` to the seed env (and the host `env` block) so it's consented in the same round; likewise drop the `CAL_*` vars here if you decide to set the flags off in the calendar tools but want them later. The point is to consent, in one interactive pass, the exact union the host will request — no more, no less.

---

## Hosts

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"graph-m365": {
  "command": "/opt/homebrew/bin/uv",
  "args": ["run", "/Users/<you>/Documents/projects/graph-m365-mcp/server.py"],
  "env": {
    "MAIL_SEND": "0",
    "CAL_READ": "1",
    "CAL_WRITE": "1"
  }
}
```

Absolute `uv` path is required — Desktop does not inherit fish `PATH`.

**Claude Code:**

```fish
claude mcp add graph-m365 -- uv run /Users/<you>/Documents/projects/graph-m365-mcp/server.py
```

(Override caps per-invocation via the `env` block / shell env.)

---

## Guardrails (do not violate)

These invariants are load-bearing for *this* design — a single-user, read-and-stage, own-mailbox/own-default-calendar server. If you fork toward a different goal, change them deliberately and knowingly; don't let an agent erode them by accident.

- **Mail soft delete only.** `delete_messages` uses `DELETE /me/messages/{id}` (per item, via `/$batch`). Do not implement `permanentDelete` or folder-empty.
- **Mail send stays dark** unless `MAIL_SEND=1`. Never request `Mail.Send` otherwise.
- **No `*.Shared` scopes**, ever. Single-user, own-mailbox/own-calendar only.
- **Calendar is boxed to the default personal calendar.** Only touch `/me/events`, `/me/calendar`, `/me/calendarView`. Never enumerate `/me/calendars`. Never add tools for shared calendars.
- **Attendee semantics.** `create_event` and `update_event` with `attendees` cause Outlook to send invites; `delete_event` on an organizer-owned meeting sends cancellations. Tool docstrings already say so — preserve those notes.
- **Times default to `CAL_TIMEZONE` (default `Eastern Standard Time`).** `CAL_TZ` reads that env var; `_dt` stamps `timeZone: CAL_TZ` and read paths send `Prefer: outlook.timezone="{CAL_TZ}"`. If you ever take an ISO string with an explicit offset, the offset wins (Graph behavior). Do not strip the Prefer header.
- **Stdio only.** No HTTP/network transport, no multi-user assumption.
- **No persistence of message content or credentials.** Returning bodies to the MCP client (Claude) is the purpose and fine. Never write message bodies, access tokens, or refresh tokens to log files, stdout logs, or any on-disk cache.
- **Tool surface freeze.** Do not add tools beyond the sixteen named in the capability table above (plus `excluded_recent_count`, which registers only under `EXCLUDE_FOLDERS`) without updating the table and routing the tool through an existing `CAPS` dial. New tools must not introduce a new scope unless a matching capability is added. Footprint must stay legible.

---

## Files

- `server.py` — implementation. Edit here for any capability or tool change; mirror the change in `CAPS` and the table above.
- `.gitignore` — excludes the legacy `token_cache.json` filename and the usual cruft.
