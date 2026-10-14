# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""Local stdio MCP: Microsoft 365 mail + personal calendar via Microsoft Graph.

Capability flags (env) drive both the OAuth scopes requested at token time and
the tools registered on the FastMCP server. Requested scopes are the union over
enabled capabilities; the AAD-consented grant is only the ceiling.
"""
import io
import os
import re
import sys
import time
import json
import socket
import struct
import zipfile
import xml.etree.ElementTree as ET
import ctypes
import base64
import hashlib
import subprocess
import unicodedata
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Stdio shim. Runs before any third-party import, on purpose.
#
# Both hosts invoke `server.py` with no flags. When a daemon (`--daemon`) is
# listening on the Unix socket in the credential directory, this block bridges
# stdin/stdout to it byte for byte and exits; the daemon, started by launchd in
# the GUI login session, is where MSAL, httpx, DNS and the token cache live.
# The shim itself needs none of them: it is stdlib only, touches no credential
# and resolves no name, which is exactly the set of things the Claude Code
# background host could not provide on 2026-10-05 (ROADMAP.md). With no daemon
# it falls through to the in-process server below, so nothing regresses when
# the daemon is not installed. Any flag (e.g. `--direct`) skips the shim.

def _shim_paths() -> tuple[str, str]:
    base = os.path.expanduser(f"~/.config/{os.environ.get('KEYCHAIN_SERVICE', 'graph-m365')}")
    return os.path.join(base, "mcp.sock"), os.path.join(base, "mcp.sock.lock")


def _daemon_lock_held(lock_path: str) -> bool:
    """True if a daemon holds the exclusive lock, i.e. one is alive right now."""
    import fcntl
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _bridge(conn: socket.socket) -> None:
    """Copy stdin to the socket and the socket to stdout until the daemon hangs up."""
    import threading
    in_fd, out_fd = sys.stdin.fileno(), sys.stdout.fileno()

    def to_daemon() -> None:
        try:
            while True:
                data = os.read(in_fd, 65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)     # EOF to the daemon: session over
            except OSError:
                pass

    def to_host() -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                view = memoryview(data)
                while view:
                    view = view[os.write(out_fd, view):]
        except OSError:
            pass

    threading.Thread(target=to_daemon, daemon=True).start()
    to_host()                                     # returns when the daemon closes its side


if __name__ == "__main__" and len(sys.argv) == 1:
    _sock_path, _lock_path = _shim_paths()
    _conn: socket.socket | None = None
    if os.path.exists(_sock_path):
        _conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _conn.settimeout(5)
            _conn.connect(_sock_path)
            _conn.settimeout(None)
        except OSError as _e:
            _conn.close()
            _conn = None
            _why = f"{_sock_path} exists but refused ({_e})"
        else:
            _why = ""
    else:
        _why = f"no socket at {_sock_path}"
    if _conn is not None:
        _bridge(_conn)
        sys.exit(0)
    if _daemon_lock_held(_lock_path):
        # A live daemon that cannot be reached is not a reason to start a
        # second server on the same token cache; two processes rotating one
        # refresh token is how a store goes stale. Say so and stop.
        sys.stderr.write(
            f"graph-m365: a daemon holds {_lock_path} but {_why}; refusing to start "
            "a second server against the same token cache. Restart the daemon: "
            "launchctl kickstart -k gui/$(id -u)/org.wardlaw.graph-m365\n"
        )
        sys.exit(1)
    # No daemon: fall through to the in-process server, as before Phase 3.


import msal
import httpx
import requests                      # MSAL's HTTP stack; imported only to name its exceptions
import html2text
from mcp.server.fastmcp import FastMCP

from msal_extensions import PersistedTokenCache, FilePersistence
from msal_extensions.persistence import (
    BasePersistence, KeychainPersistence,
    PersistenceNotFound, PersistenceDecryptionError,
)

CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else ""
GRAPH     = "https://graph.microsoft.com/v1.0"

# Keychain service name (and the non-secret cache-signal path) both key off
# this. Defaults to the server name; override KEYCHAIN_SERVICE to reuse a
# refresh token minted under a different name (e.g. migrating an older install).
KEYCHAIN_SERVICE = os.environ.get("KEYCHAIN_SERVICE", "graph-m365")

# Generic on purpose, so the code carries no institution-specific signal.
KEYCHAIN_ACCOUNT = "default-mailbox"

# Where the MSAL cache lives: "file" (a 0600 file at CACHE_FILE, the default),
# "cli" (the login Keychain through a fresh /usr/bin/security process per
# access), or "msal" (msal-extensions' in-process legacy client, kept for A/B).
#
# The default is the file backend because the Keychain backends only work in a
# GUI login session. Remote Control reaches this machine over SSH, which lands
# in a background audit session with its own Keychain lock state and no way to
# prompt, so every Keychain read there fails. Set KEYCHAIN_BACKEND=cli to go
# back to Keychain storage on a desktop-only install.
KEYCHAIN_BACKEND = os.environ.get("KEYCHAIN_BACKEND", "file").strip().lower()

# Non-secret marker file. The refresh token lives in the macOS login Keychain;
# this file only signals cache state to msal-extensions.
SIGNAL = os.path.expanduser(f"~/.config/{KEYCHAIN_SERVICE}/cache.signal")

# MSAL cache used when KEYCHAIN_BACKEND=file. The login Keychain is unreachable
# from a Remote Control / SSH session: unlock state is tracked per audit session,
# so a background session sees it as locked and every read returns
# errSecInteractionNotAllowed. This path trades Keychain encryption for FileVault
# at rest plus mode 0600, which is what makes the server usable headless. Keep it
# out of any synced or backed-up directory.
CACHE_FILE = os.path.expanduser(f"~/.config/{KEYCHAIN_SERVICE}/cache.json")


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

# msal-extensions builds KeychainError with no args, so str(e) is "" and the MCP
# host relays an empty error string ("Error executing tool X: "). Catch it by
# type and re-raise with the OSStatus attached. The import is darwin-only:
# msal_extensions.osx binds Security.framework at import time.
try:
    from msal_extensions.osx import KeychainError
    _KEYCHAIN_ERRORS: tuple[type[BaseException], ...] = (KeychainError,)
except Exception:                       # not macOS, or Security.framework absent
    _KEYCHAIN_ERRORS = ()               # `except ()` never fires; keeps _token() portable


def _osstatus_text(status: int) -> str:
    """Human-readable text for a Security.framework OSStatus, "" if unavailable.

    msal_extensions.osx binds SecCopyErrorMessageString with restype c_char_p,
    which misreads the returned CFStringRef; bind it correctly here instead.
    Error path only, so the CDLL loads stay lazy.
    """
    try:
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec.SecCopyErrorMessageString.argtypes = (ctypes.c_int32, ctypes.c_void_p)
        sec.SecCopyErrorMessageString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = (
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32)
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = (ctypes.c_void_p,)

        ref = sec.SecCopyErrorMessageString(status, None)
        if not ref:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            if not cf.CFStringGetCString(ref, buf, len(buf), 0x08000100):  # UTF-8
                return ""
            return buf.value.decode("utf-8", "replace")
        finally:
            cf.CFRelease(ref)
    except Exception:
        return ""


def _keychain_failure(exc: BaseException) -> str:
    """Message for a Keychain read that failed inside this process.

    The known cause is a stale record handle: Security.framework caches keychain
    records per process and refreshes them only from change notifications
    delivered over a CFRunLoop, which this server never runs. After a device
    lock the handle can go stale (CSSMERR_DL_INVALID_RECORD_UID) and stays stale
    for the life of the process, so the only in-band remedy is a restart.
    """
    status = getattr(exc, "exit_status", None)
    detail = _osstatus_text(status) if isinstance(status, int) else ""
    return (
        f"Keychain token cache unreadable in this server process "
        f"(OSStatus {status}{': ' + detail if detail else ''}). "
        "This does not recover on its own: restart the server "
        "(/mcp -> reconnect graph-m365) or re-run: uv run server.py --auth"
    )


def _sec_name(value: str, label: str) -> str:
    """Validate a name that has to survive security's interactive tokenizer."""
    if not value or any(c in value for c in '"\\') or any(ord(c) < 0x20 for c in value):
        raise ValueError(
            f"{label} must be non-empty and free of quotes, backslashes "
            f"and control characters (got {value!r})"
        )
    return value


class CliKeychainPersistence(BasePersistence):
    """MSAL cache in the login Keychain, reached through /usr/bin/security.

    Why a subprocess instead of msal-extensions' KeychainPersistence:
    Security.framework's legacy keychain client caches records per process and
    refreshes them only from change notifications delivered over a CFRunLoop,
    which this stdio server never runs. A device lock can leave the cached
    record handle stale (CSSMERR_DL_INVALID_RECORD_UID) for the life of the
    process; every later read then fails and no in-process retry helps, because
    a fresh Keychain(), a fresh PersistedTokenCache and SecItemCopyMatching all
    route back to the same client. Each `security` invocation is a new process
    with a new cache, so no stale handle survives a call. Cost is one process
    per item per load and roughly four per item per save (measured at about
    30 ms each: 136 ms to read a 16 KB value, 322 ms to write it); loads happen
    only when the signal file's mtime has advanced, saves only on token refresh,
    so this is noise next to a Graph round trip.

    Concurrency is unchanged. PersistedTokenCache serializes load and save
    under CrossPlatLock on cache.signal.lockfile, across every server process.

    The value is stored base64-encoded, which keeps quotes and whitespace out
    of the command fed to security on save, split across as many items as it
    takes to stay under that command's 4096-byte ceiling (see _LINE_LIMIT), and
    written through a staging set first (see save()). Chunk 0 keeps the bare
    account name and carries a "v2:<n>:" header giving the item count; chunk i
    lives at "<account>.<i>". load() also accepts the two older layouts, a
    single base64 item and a bare JSON value, so an item written by an earlier
    build or by KeychainPersistence reads back with no re-auth.

    One caveat when switching KEYCHAIN_BACKEND back to "msal": items this class
    creates carry /usr/bin/security on their ACL, not the Python interpreter,
    so the in-process client will prompt for access.
    """

    is_encrypted = True

    _SECURITY = "/usr/bin/security"
    _TIMEOUT = 15               # seconds; only a GUI prompt should ever be slow
    _NOT_FOUND = 44             # errSecItemNotFound (-25300) truncated to a byte
    _LOCKED = 36                # errSecInteractionNotAllowed (-25308), same truncation
    _FAILED_SUBCOMMAND = re.compile(r": returned -?\d+")

    # security(1) exits with the OSStatus truncated to its low byte, so the
    # inverse is only defined within one 256-wide block. Map the two that
    # matter, with their canonical text, rather than doing arithmetic that is
    # ambiguous in general or calling _osstatus_text, which would pull
    # Security.framework into the one class built to stay out of it.
    _RC_OSSTATUS = {
        _NOT_FOUND: (-25300, "The specified item could not be found in the keychain."),
        _LOCKED:    (-25308, "User interaction is not allowed."),
    }

    # security(1) reads an interactive command into a 4096-byte line buffer
    # (measured exactly: 4096 including the newline stores whole, 4097 splits).
    # Past that it does not error, it parses the tail as a *second* command, so
    # the item is created holding a truncated value. A real MSAL cache
    # base64-encodes to more than that, which is why the value is chunked
    # across items rather than written as one. _add() re-checks every command
    # against this so a future caller cannot reintroduce a silent truncation.
    _LINE_LIMIT = 4096
    _LINE_MARGIN = 64           # slack for a longer service name or index
    _MAX_CHUNKS = 64            # ~250 KB of base64; a runaway-payload backstop
    _CHUNK_HEAD = re.compile(r"^v2:(\d+):")

    def __init__(self, signal_location, service_name, account_name=KEYCHAIN_ACCOUNT):
        # Composition mirrors KeychainPersistence: the signal file supplies
        # time_last_modified()/touch(), the cross-process reload coordination
        # PersistedTokenCache depends on. It holds no secret.
        self._file_persistence = FilePersistence(signal_location)
        self._service_name = _sec_name(service_name, "KEYCHAIN_SERVICE")
        self._account_name = _sec_name(account_name, "Keychain account name")
        self._staged_name = self._account_name + ".staged"

    def load(self) -> str:
        raw, account = self._read_set(self._account_name), self._account_name
        if raw is None:
            # A save() that died between the two writes below leaves the value
            # only in the staging set. Reading it back makes that self-healing.
            # _read_set also returns None for a *partial* live set, which is
            # what a crash between the clear and the rewrite leaves behind; the
            # staging copy is still intact at that point, so the fallback is
            # what makes the multi-item write safe.
            raw, account = self._read_set(self._staged_name), self._staged_name
        if raw is None:
            # A load() before any save(). Mapped to the cross-platform error so
            # PersistedTokenCache treats it as an empty cache, as it does today.
            raise self._not_found("Keychain persistence not initialized. "
                                  "You can recover by calling a save() first.")
        if raw.startswith("{"):
            return raw                          # legacy value written by KeychainPersistence
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception as e:
            if account == self._staged_name:
                # Staging is a fallback, not the source of truth, so an
                # unreadable value there is not a decryption failure: it is a
                # dead half-write, or an item left by an older layout. The live
                # item is already gone, so "no cache" is the honest report, and
                # it is the useful one. PersistedTokenCache catches only
                # PersistenceNotFound; raising the decryption error instead
                # would propagate out of every load and break the interactive
                # re-auth that is the actual recovery. The next save() replaces
                # this item regardless.
                raise self._not_found(
                    f"Ignoring an unreadable staging item ({e}); treating the "
                    "cache as empty. Interactive re-auth will replace it."
                ) from e
            raise PersistenceDecryptionError(
                message=f"Keychain value is neither base64 nor JSON: {e}",
                location=f"Service:{self._service_name} Account:{account}",
            ) from e

    def _not_found(self, message: str) -> PersistenceNotFound:
        return PersistenceNotFound(
            location=f"Service:{self._service_name} Account:{self._account_name}",
            message=message,
        )

    def save(self, content: str) -> None:
        # Two measured facts shape this (2026-08-28). First, overwriting an item
        # in place (add-generic-password -U) goes through
        # SecKeychainItemModifyAttributesAndData, whose ACL check /usr/bin/security
        # does not satisfy even as the item's own creator: it raises a GUI prompt
        # no server process can answer, and the call hangs to the timeout. So a
        # write has to be delete-then-add. Second, while any Keychain dialog is
        # pending anywhere on the machine, `security` reads and deletes still
        # succeed but every *create* blocks. Naive delete-then-add would then
        # destroy the cache and be unable to rewrite it, costing a re-auth round.
        #
        # So the value goes to a staging set first. If creates are blocked we
        # fail there, with the live set untouched; if the second write fails, the
        # value is still in staging and load() picks it up. PersistedTokenCache
        # holds CrossPlatLock across all of it, so no other process sees a gap.
        #
        # Chunking makes that fallback do more work than it used to. The live
        # write is no longer a single delete-then-add but a clear of n items
        # followed by n creates, so an interruption can leave a torn live set.
        # _read_set reports a set whose head promises absent items as missing
        # rather than returning a short value, which routes the next load to
        # staging, where the whole value still sits because the final cleanup
        # below has not run yet.
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        size = self._chunk_size()
        parts = [payload[i:i + size] for i in range(0, len(payload), size)] or [""]
        if len(parts) > self._MAX_CHUNKS:
            raise RuntimeError(
                f"Token cache needs {len(parts)} Keychain items, over the "
                f"{self._MAX_CHUNKS} cap; refusing to write."
            )
        self._clear_set(self._staged_name)      # clear a leftover from a crash
        self._write_set(self._staged_name, parts)
        self._clear_set(self._account_name)
        self._write_set(self._account_name, parts)
        try:
            self._clear_set(self._staged_name)
        except Exception:
            pass                                # a stale copy is cleared by the next save
        self._file_persistence.touch()          # for time_last_modified()

    def _chunk_size(self) -> int:
        """Base64 characters per item, sized so no command can reach the limit."""
        # Budget against the worst case: the staging base, a two-digit index and
        # the header, so every item in either set fits under the same number.
        probe = f"{self._staged_name}.{self._MAX_CHUNKS}"
        overhead = len(self._add_command("", probe)) + len(f"v2:{self._MAX_CHUNKS}:")
        size = self._LINE_LIMIT - overhead - self._LINE_MARGIN
        if size < 256:
            raise RuntimeError(
                f"KEYCHAIN_SERVICE {self._service_name!r} leaves only {size} "
                "bytes per item; use a shorter name."
            )
        return size

    def _names(self, base: str, count: int) -> list[str]:
        # Chunk 0 keeps the bare account name, so the documented
        # `security find-generic-password -s graph-m365 -a default-mailbox`
        # still finds the head of the set.
        return [base if i == 0 else f"{base}.{i}" for i in range(count)]

    def _write_set(self, base: str, parts: list[str]) -> None:
        # The count rides in the first item, so a reader needs one lookup to
        # learn how many follow. Written head-first: an interrupted write then
        # leaves a set whose head promises items that are missing, which
        # _read_set reports as incomplete rather than as a short value.
        for name, part, i in zip(self._names(base, len(parts)), parts, range(len(parts))):
            self._add(f"v2:{len(parts)}:{part}" if i == 0 else part, name)

    def _clear_set(self, base: str) -> None:
        # Delete-driven rather than find-then-delete: _delete already reports a
        # missing item, so probing first would double the process count for no
        # information. Walks until the first gap; sets are written contiguously
        # and always cleared before a rewrite, so this sees a whole set even
        # when the cache shrank since the last save.
        self._delete(base)
        for i in range(1, self._MAX_CHUNKS + 1):
            if not self._delete(f"{base}.{i}"):
                break

    def _read_set(self, base: str) -> str | None:
        """The reassembled value, or None if the set is absent or incomplete."""
        head = self._find(base)
        if head is None:
            return None
        if head.startswith("{"):
            return head                         # legacy bare JSON (KeychainPersistence)
        m = self._CHUNK_HEAD.match(head)
        if not m:
            return head                         # single item, written before chunking
        parts = [head[m.end():]]
        for name in self._names(base, int(m.group(1)))[1:]:
            part = self._find(name)
            if part is None:
                return None                     # torn set; caller falls back to staging
            parts.append(part)
        return "".join(parts)

    def time_last_modified(self):
        return self._file_persistence.time_last_modified()

    def get_location(self):
        return self._file_persistence.get_location()

    def _find(self, account: str) -> str | None:
        """The stored value exactly as written, or None if there is no item."""
        proc = self._run([
            "find-generic-password", "-s", self._service_name, "-a", account, "-w",
        ])
        if proc.returncode == self._NOT_FOUND:
            return None
        self._check(proc, "read", account)
        raw = proc.stdout
        return raw[:-1] if raw.endswith("\n") else raw    # security appends exactly one

    def _add(self, raw: str, account: str) -> None:
        # -w on the command line would put the secret in argv, where `ps` reads
        # it; security's interactive mode takes the command on stdin instead.
        # The value is base64, so the tokenizer needs no escaping. -T keeps
        # /usr/bin/security on the new item's ACL, so later reads do not prompt.
        if not raw or any(c.isspace() or c in '"\\' for c in raw):
            raise ValueError("Keychain value must be a single unquoted token")
        cmd = self._add_command(raw, account)
        if len(cmd) > self._LINE_LIMIT:
            # Unreachable via save(), which sizes chunks against this. Kept as a
            # hard stop because the failure it guards is silent: security would
            # store a truncated value and report the leftover as a bad command.
            raise RuntimeError(
                f"Keychain write for {account!r} would be {len(cmd)} bytes, over "
                f"security's {self._LINE_LIMIT}-byte line buffer; it would be "
                "stored truncated. Chunk the value instead."
            )
        self._check(self._run(["-i"], stdin=cmd), "write", account)

    def _add_command(self, raw: str, account: str) -> str:
        return (f'add-generic-password -s "{self._service_name}" '
                f'-a "{account}" -T {self._SECURITY} -w {raw}\n')

    def _delete(self, account: str) -> bool:
        """True if an item was removed, False if there was none to remove."""
        proc = self._run([
            "delete-generic-password", "-s", self._service_name, "-a", account,
        ])
        if proc.returncode == self._NOT_FOUND:
            return False                        # nothing to replace
        self._check(proc, "delete", account)
        return True

    def _run(self, args: list[str], stdin: str = ""):
        # stdin is always a pipe: this process' stdin is the MCP channel, and a
        # child must never be handed it.
        try:
            return subprocess.run(
                [self._SECURITY, *args], input=stdin,
                capture_output=True, text=True, timeout=self._TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            op = "add-generic-password" if args[0] == "-i" else args[0]
            raise RuntimeError(
                f"/usr/bin/security timed out after {self._TIMEOUT}s on "
                f"{op}; it is waiting on a Keychain prompt this process "
                "cannot answer. Unlock the login Keychain, dismiss any pending "
                "Keychain dialog, and retry."
            ) from e

    def _check(self, proc, verb: str, account: str) -> None:
        err = (proc.stderr or "").strip()
        # security writes to stderr on success too ("password has been
        # deleted."), so the exit status is the signal. Interactive mode also
        # reports a failed subcommand as "<cmd>: returned <n>"; match that as
        # well, in case it ever stops folding into the exit status.
        if proc.returncode == 0 and not self._FAILED_SUBCOMMAND.search(err):
            return
        # On the locked-keychain read path security writes nothing to stderr, so
        # the exit code is the only information that exists. Spell it out rather
        # than reporting a bare truncated byte and "no output". The advice on the
        # TimeoutExpired branch of _run() is right but structurally unreachable
        # here: a hung prompt needs a GUI session to hang in, and this failure
        # only happens where there is none.
        if proc.returncode == self._LOCKED:
            status, detail = self._RC_OSSTATUS[self._LOCKED]
            raise RuntimeError(
                f"Keychain {verb} refused for service {self._service_name!r} "
                f"account {account!r}: the keychain is locked in this login "
                f"session and securityd has no way to prompt (OSStatus {status}: "
                f"{detail}). This is a session problem, not a credential problem; "
                "nothing stored was changed or lost. Unlock state is tracked per "
                "audit session, and an SSH or other non-GUI session gets its own, "
                "so unlocking from a different SSH session will not help. Fix it "
                "from a process inside THIS session: "
                "security unlock-keychain ~/Library/Keychains/login.keychain-db"
            )
        known = self._RC_OSSTATUS.get(proc.returncode)
        gloss = f" = OSStatus {known[0]}: {known[1]}" if known else ""
        raise RuntimeError(
            f"Keychain {verb} via /usr/bin/security failed (rc {proc.returncode}"
            f"{gloss}) "
            f"for service {self._service_name!r} account {account!r}: "
            f"{err or 'no output'}"
        )


class SecureFilePersistence(FilePersistence):
    """FilePersistence that keeps the cache at 0600.

    msal-extensions writes with the process umask, which on a default macOS
    account yields 0644. This file holds the refresh token, so tighten it on
    every save rather than trusting the umask of whatever spawned the server.
    """

    def save(self, content):
        super().save(content)
        try:
            os.chmod(self.get_location(), 0o600)
        except OSError:
            pass


def _seed_cache_file(verbose: bool = False) -> bool:
    """One-way copy of the MSAL cache from the login Keychain into CACHE_FILE.

    Only works from a GUI (Aqua) session, since it reads the Keychain. Called
    automatically when the file backend starts with no cache file, so the first
    desktop launch after the switch carries the existing refresh token over and
    no re-auth is needed. A Remote Control session simply fails the read and
    leaves the file absent, which surfaces as the usual no-account error.

    One-way on purpose: a refresh token rotates on use, so keeping both stores
    live would let whichever one did not perform the refresh go stale.
    """
    try:
        src = CliKeychainPersistence(
            SIGNAL, service_name=KEYCHAIN_SERVICE, account_name=KEYCHAIN_ACCOUNT,
        )
        data = src.load()
    except Exception as exc:
        if verbose:
            print(f"Keychain read failed, nothing migrated: {exc}", file=sys.stderr)
        return False
    if not data or not data.strip():
        if verbose:
            print("Keychain cache is empty, nothing to migrate.", file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(CACHE_FILE), mode=0o700, exist_ok=True)
    SecureFilePersistence(CACHE_FILE).save(data)
    print(f"Seeded {CACHE_FILE} from the Keychain ({len(data)} bytes, mode 0600).",
          file=sys.stderr)
    return True


def _build_cache() -> PersistedTokenCache:
    os.makedirs(os.path.dirname(SIGNAL), exist_ok=True)
    if KEYCHAIN_BACKEND not in ("cli", "msal", "file"):
        raise RuntimeError(
            f"KEYCHAIN_BACKEND must be cli, msal or file (got {KEYCHAIN_BACKEND!r})"
        )
    backend = KEYCHAIN_BACKEND if sys.platform == "darwin" else "file"

    # service_name comes from KEYCHAIN_SERVICE (default "graph-m365"); set it to
    # an older name to reuse a refresh token minted under that name, no re-auth.
    if backend == "cli":
        persistence = CliKeychainPersistence(
            SIGNAL, service_name=KEYCHAIN_SERVICE, account_name=KEYCHAIN_ACCOUNT,
        )
    elif backend == "msal":
        # In-process legacy client. Kept only to A/B against the CLI backend;
        # this is the path whose record handle goes stale after a device lock.
        persistence = KeychainPersistence(
            SIGNAL, service_name=KEYCHAIN_SERVICE, account_name=KEYCHAIN_ACCOUNT,
        )
    else:
        # Unencrypted on disk, protected by FileVault and mode 0600 instead of by
        # the Keychain. This is the backend that works from a Remote Control /
        # SSH session. Do not point CACHE_FILE inside the repo or any synced dir.
        os.makedirs(os.path.dirname(CACHE_FILE), mode=0o700, exist_ok=True)
        if not os.path.exists(CACHE_FILE):
            _seed_cache_file()
        persistence = SecureFilePersistence(CACHE_FILE)
    return PersistedTokenCache(persistence)


_CACHE = _build_cache()


_APP: msal.PublicClientApplication | None = None


def _app() -> msal.PublicClientApplication:
    """The one MSAL application for this process, built on first use.

    MSAL fetches the tenant's OpenID configuration from the login host when a
    PublicClientApplication is constructed (Authority.__init__ is eager, even
    with instance discovery off). Building one per _token() call, as this did
    until 2026-10-05, meant a request to login.microsoftonline.com on every
    tool call with a fully warm cache, and a login-host outage took down reads
    that only needed Graph. One object per process makes that once; a warm
    access token then reaches Graph with no login-host traffic at all.
    Sharing the object loses nothing: PersistedTokenCache already reloads
    across processes through the signal file, and the cache is what carries
    state. A failed construction leaves _APP unset, so the next call retries.
    """
    global _APP
    if _APP is not None:
        return _APP
    if not CLIENT_ID or not TENANT_ID:
        raise RuntimeError(
            "GRAPH_CLIENT_ID and GRAPH_TENANT_ID must be set (point at your registered AAD public-client app)"
        )
    # instance_discovery=False: the authority is a known single-tenant
    # login.microsoftonline.com URL, so the instance-metadata round trip adds
    # nothing. Tenant (OpenID) discovery still runs, once, here.
    _APP = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=_CACHE, instance_discovery=False,
    )
    return _APP


def _token() -> str:
    # MSAL talks to the login host with `requests`, both when the application
    # object is built (tenant discovery) and when a refresh is needed. A dead
    # resolver surfaces there as a bare urllib3 NameResolutionError that says
    # nothing about which layer failed; classify it before it reaches the host.
    try:
        app = _app()
        try:
            accts = app.get_accounts()
        except _KEYCHAIN_ERRORS as e:
            raise RuntimeError(_keychain_failure(e)) from e
        if not accts:
            raise RuntimeError(
                "No cached account. Run: uv run server.py --auth (needs a browser on "
                "this machine), or uv run server.py --auth-device (works over SSH)"
            )
        try:
            res = app.acquire_token_silent(SCOPES, account=accts[0])
        except _KEYCHAIN_ERRORS as e:
            raise RuntimeError(_keychain_failure(e)) from e
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"login host unreachable: {_classify_network(_LOGIN_HOST)}"
        ) from e
    if not res or "access_token" not in res:
        raise RuntimeError(f"token acquisition failed: {res}")
    return res["access_token"]


# ---------------------------------------------------------------------------
# Diagnostics. Every check is local or talks to a literal IP; none needs a
# token, a scope, or the mailbox, so `diagnose` registers unconditionally.
#
# Motivation (ROADMAP.md, 2026-10-05): a Claude Code background host spawned
# this server into a process tree with no system resolver. The machine and the
# network were fine, `dig` resolved every name, and the server reported a bare
# urllib3 NameResolutionError for an hour. Six shell probes were needed to
# learn that only the resolver API was dead. These checks are those probes.

_LOGIN_HOST = "login.microsoftonline.com"
_GRAPH_HOST = "graph.microsoft.com"
_DNS_TIMEOUT = 2.0          # seconds, per UDP query
_TCP_TIMEOUT = 3.0          # seconds, per connect
_PUBLIC_RESOLVER = "1.1.1.1"


def _resolv_conf_servers() -> list[str]:
    try:
        with open("/etc/resolv.conf") as fh:
            return [ln.split()[1] for ln in fh
                    if ln.startswith("nameserver") and len(ln.split()) > 1]
    except OSError:
        return []


def _dns_build_query(name: str, qid: int) -> bytes:
    """One RFC 1035 question for the A records of `name`, recursion desired."""
    q = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        if not 0 < len(label) < 64:
            raise ValueError(f"bad DNS label in {name!r}")
        q += bytes([len(label)]) + label.encode("ascii")
    return q + b"\x00" + struct.pack("!HH", 1, 1)


def _dns_parse_a(data: bytes, qid: int) -> list[str]:
    """A records in the answer section of a response to `qid`.

    Walks compressed names (a 0xC0 pointer ends a name) without following them,
    since only the record bodies matter here. CNAME chains come back in the
    same section from a recursive resolver, so collecting type 1 is enough.
    """
    if len(data) < 12:
        raise ValueError("DNS response shorter than a header")
    rid, flags, qd, an, _, _ = struct.unpack("!HHHHHH", data[:12])
    if rid != qid:
        raise ValueError("DNS response id mismatch")
    if flags & 0xF:
        raise ValueError(f"DNS rcode {flags & 0xF}")

    def skip_name(i: int) -> int:
        while True:
            n = data[i]
            if n == 0:
                return i + 1
            if n & 0xC0 == 0xC0:
                return i + 2
            i += n + 1

    i = 12
    for _ in range(qd):
        i = skip_name(i) + 4
    out: list[str] = []
    for _ in range(an):
        i = skip_name(i)
        rtype, _, _, rdlen = struct.unpack("!HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in data[i:i + 4]))
        i += rdlen
    return out


def _dns_query_a(name: str, server: str, timeout: float = _DNS_TIMEOUT) -> list[str]:
    """A records for `name` from `server` over UDP, bypassing the resolver API.

    This exists so the server can tell "the system resolver is unreachable from
    this process" apart from "the network is down". The former was the
    incident; the latter is what the stock error message implies.
    """
    qid = int.from_bytes(os.urandom(2), "big")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(_dns_build_query(name, qid), (server, 53))
        data, _ = s.recvfrom(4096)
    return _dns_parse_a(data, qid)


def _direct_dns(name: str) -> tuple[list[str], str]:
    """(addresses, server that answered): first resolv.conf server, then 1.1.1.1."""
    errors: list[str] = []
    for srv in _resolv_conf_servers()[:1] + [_PUBLIC_RESOLVER]:
        try:
            addrs = _dns_query_a(name, srv)
        except Exception as e:
            errors.append(f"{srv}: {type(e).__name__}: {e}")
            continue
        if addrs:
            return addrs, srv
        errors.append(f"{srv}: no A records")
    raise RuntimeError("; ".join(errors) or "no DNS server to ask")


def _resolver_api(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def _classify_network(host: str) -> str:
    """One line saying which layer failed to reach `host`, for a caller's error.

    Runs the first three diagnose checks inline, in a few seconds at most.
    """
    try:
        _resolver_api(host)
    except socket.gaierror as e:
        try:
            addrs, srv = _direct_dns(host)
        except Exception as e2:
            return (f"cannot resolve {host}: the system resolver failed ({e}) and "
                    f"so did direct DNS ({e2}). The network is down or has no DNS. "
                    "Run diagnose.")
        return (f"resolver API failed for {host} ({e}) but direct DNS works "
                f"({addrs[0]} via {srv}): the host process cannot use the system "
                "resolver, which is what a Claude Code background host looks like "
                "(scutil --dns is empty in that process tree). The network itself "
                "is fine. Run diagnose; restart this MCP server from a GUI session.")
    try:
        with socket.create_connection((host, 443), timeout=_TCP_TIMEOUT):
            pass
    except OSError as e:
        return (f"{host} resolves but port 443 is unreachable ({e}): no route, "
                "or a firewall. Run diagnose.")
    return f"{host} resolves and accepts connections now; the failure was transient. Retry."


def _cache_status() -> str:
    """Token cache health, as text only: counts, mode and expiry, never contents."""
    if KEYCHAIN_BACKEND != "file":
        return f"backend={KEYCHAIN_BACKEND}; file checks not applicable"
    st = os.stat(CACHE_FILE)                    # FileNotFoundError is the report
    mode = st.st_mode & 0o777
    with open(CACHE_FILE) as fh:
        data = json.load(fh)

    def mine(section: str) -> list[dict]:
        rows = (data.get(section) or {}).values()
        return [t for t in rows if not CLIENT_ID or t.get("client_id") == CLIENT_ID]

    accounts = len(data.get("Account") or {})
    refresh = len(mine("RefreshToken"))
    expiries = [int(t.get("expires_on") or 0) for t in mine("AccessToken")]
    parts = [f"mode {mode:04o}" + ("" if mode == 0o600 else " (expected 0600)"),
             f"{accounts} account(s)", f"{refresh} refresh token(s)"]
    if expiries:
        mins = (max(expiries) - time.time()) / 60
        parts.append(f"access token expires in {mins:.0f} min" if mins > 0
                     else f"access token expired {-mins:.0f} min ago (silent refresh will run)")
    else:
        parts.append("no access token cached")
    if accounts == 0:
        raise RuntimeError(", ".join(parts) + "; no account: run server.py --auth")
    return ", ".join(parts)


def _server_date(ip: str, host: str) -> datetime:
    """The Date header from a HEAD to `host` at a literal `ip` (TLS via SNI)."""
    import ssl
    import email.utils
    ctx = ssl.create_default_context()
    with socket.create_connection((ip, 443), timeout=_TCP_TIMEOUT) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            s.sendall(f"HEAD /v1.0/ HTTP/1.1\r\nHost: {host}\r\n"
                      "Connection: close\r\n\r\n".encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
    for line in buf.decode("latin-1").split("\r\n"):
        if line.lower().startswith("date:"):
            return email.utils.parsedate_to_datetime(line.split(":", 1)[1].strip())
    raise RuntimeError("no Date header in response")


def _host_context() -> str:
    """Parent process chain (five levels) plus whether scutil sees a DNS config."""
    chain: list[str] = []
    pid = os.getpid()
    for _ in range(6):
        p = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                           capture_output=True, text=True)
        parts = p.stdout.split(None, 1)
        if len(parts) < 2:
            break
        ppid, comm = int(parts[0]), parts[1].strip()
        chain.append(f"{os.path.basename(comm)}[{pid}]")
        if ppid <= 1:
            chain.append("launchd[1]")
            break
        pid = ppid
    dns = "n/a"
    if sys.platform == "darwin":
        out = subprocess.run(["scutil", "--dns"], capture_output=True, text=True).stdout
        dns = "present" if "nameserver" in out else "EMPTY (system resolver unreachable here)"
    return " <- ".join(chain) + f"; scutil --dns: {dns}"


def _timed(name: str, fn) -> dict:
    t0 = time.monotonic()
    try:
        detail, ok = fn(), True
    except Exception as e:
        detail, ok = f"{type(e).__name__}: {e}", False
    return {"check": name, "ok": ok, "detail": detail,
            "ms": round((time.monotonic() - t0) * 1000)}


def _diagnose() -> list[dict]:
    rows: list[dict] = []
    addrs: dict[str, list[str]] = {}

    def resolver_api():
        addrs["api"] = _resolver_api(_LOGIN_HOST)
        return ", ".join(addrs["api"])
    rows.append(_timed("resolver API", resolver_api))

    def direct_dns():
        a, srv = _direct_dns(_LOGIN_HOST)
        addrs["direct"] = a
        return f"{', '.join(a)} via {srv}"
    rows.append(_timed("direct DNS", direct_dns))

    login_ip = next(iter(addrs.get("api") or addrs.get("direct") or []), None)

    def tcp_reach():
        if not login_ip:
            raise RuntimeError("skipped: no address for the login host")
        t0 = time.monotonic()
        with socket.create_connection((login_ip, 443), timeout=_TCP_TIMEOUT):
            pass
        return f"{login_ip}:443 in {round((time.monotonic() - t0) * 1000)} ms"
    rows.append(_timed("TCP reach", tcp_reach))
    tcp_ok = rows[-1]["ok"]

    rows.append(_timed("token cache", _cache_status))

    def clock():
        if not tcp_ok:
            raise RuntimeError("skipped: TCP reach failed")
        try:
            ip = _resolver_api(_GRAPH_HOST)[0]
        except socket.gaierror:
            ip = _direct_dns(_GRAPH_HOST)[0][0]
        skew = (datetime.now(timezone.utc) - _server_date(ip, _GRAPH_HOST)).total_seconds()
        return f"local clock is {skew:+.1f} s from {_GRAPH_HOST}"
    rows.append(_timed("clock", clock))

    rows.append(_timed("host context", _host_context))
    return rows


def _print_diagnose() -> int:
    rows = _diagnose()
    for r in rows:
        print(f"{'ok  ' if r['ok'] else 'FAIL'}  {r['check']:<13} {r['ms']:>5} ms  {r['detail']}")
    return 0 if all(r["ok"] for r in rows) else 1


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

# One client per process so connections to graph.microsoft.com are reused
# across tool calls instead of a fresh TLS handshake per request.
_HTTP = httpx.Client(timeout=30)


def _req(method: str, path: str, *, params=None, json=None, extra_headers=None,
         raw: bool = False):
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
        try:
            r = _HTTP.request(method, url, headers=headers, params=params, json=json)
        except httpx.ConnectError as e:
            raise RuntimeError(f"Graph unreachable: {_classify_network(_GRAPH_HOST)}") from e
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
    if raw:
        return r.content
    return r.json() if r.content else {}


def _get(path, params=None):
    return _req("GET", path, params=params)


def _get_bytes(path) -> bytes:
    """GET returning the raw body (attachment `$value`), same auth and retry loop."""
    return _req("GET", path, raw=True)


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


_HEX8 = re.compile(r"[0-9a-f]{8}")


def _folder_ref(folder: str) -> str:
    """Folder handle / well-known name / raw id -> the id Graph accepts in a path.

    The single point where a folder string becomes a URL segment. Read tools must
    call this rather than interpolating `folder` directly: Graph resolves only
    well-known names and full ids in `/me/mailFolders/{id}/`, so a handle passed
    through raw comes back as ErrorInvalidIdMalformed. The write path has always
    resolved (move_messages -> _resolve); this keeps the read path honest about
    the identifiers list_mail_folders hands out.

    Also the exclusion gate, which must run on the *resolved* id: a handle
    matches neither an id nor a display name, so checking the caller's string
    alone would let a handle slip the fail-closed guarantee.
    """
    # Distinguish "handle we no longer know" from "id Graph might know". Handles
    # die with the process, and passing a dead one through yields an opaque 400;
    # say so instead. Well-known names and AAMkA... ids can't match this shape.
    if _HEX8.fullmatch(folder) and folder not in _HANDLES:
        raise RuntimeError(
            f"unknown folder handle {folder!r}: handles last only for the server "
            "process; run list_mail_folders to refresh"
        )
    ref = _resolve(folder)
    cf = folder.casefold()
    if ref in _excluded_folder_ids() or any(cf == n.casefold() for n in EXCLUDE_FOLDER_NAMES):
        raise RuntimeError(f"folder {folder!r} is excluded by EXCLUDE_FOLDERS")
    return ref


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


# Opt-in link harvest. The sanitizer above deliberately destroys long and
# tracker-laden URLs (the right default for bulk triage), which also eats
# legitimate long links: Microsoft Forms, SharePoint, DocuSign, calendar RSVP
# pages. Rather than loosen the heuristics or expose a raw body, `links=True`
# on get_message/get_event returns the pre-clean link inventory as a compact
# side list while the body stays sanitized. Harvested from html2text output
# (after invisible-char stripping, before URL killing); deduped by URL in
# document order; mailto:/tel: never match the https? patterns; capped so a
# newsletter with 300 links cannot blow the budget the sanitizer protects.
_LINK_CAP = 40
_TRAIL_PUNCT = ".,;:!?"


def _extract_links(md: str | None, cap: int = _LINK_CAP) -> list[dict]:
    if not md:
        return []
    md = _strip_invisibles(md)
    seen: set[str] = set()
    out: list[dict] = []

    def add(text: str | None, url: str) -> None:
        url = url.rstrip(_TRAIL_PUNCT)
        if url in seen:
            return
        seen.add(url)
        out.append({"text": (text or "").strip() or None, "url": url})

    for m in _MD_LINK.finditer(md):
        add(m.group(1), m.group(2))
    # Bare URLs not already wrapped in a markdown link (plain-text bodies,
    # html2text output for <a> whose text equals its href).
    bare = _MD_LINK.sub(lambda m: m.group(1), md)
    for m in _URL.finditer(bare):
        add(None, m.group(0))
    return out[:cap]


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
        "hasAttachments": m.get("hasAttachments"),
        # bodyPreview intentionally omitted: scan is metadata-only
    }


def _recipients(addrs):
    return [{"emailAddress": {"address": a}} for a in (addrs or [])]


# --- List-Unsubscribe (RFC 2369 / RFC 8058) --------------------------------
# These live in the message HEADERS, so they never pass through _postclean().
# That is the point: the sanitizer's max_url_len / _TRACKER_PARAMS heuristics
# destroy in-body unsubscribe links, and loosening them to save those links
# would give back the token reduction this server exists to deliver.
#
# Unlike JMAP (which offers header:List-Unsubscribe:asURLs and hands back a
# parsed array), Graph returns internetMessageHeaders as raw [{name, value}],
# so the RFC 2369 list has to be parsed here.
_UNSUB_ANGLE = re.compile(r"<([^>]+)>")


def _header(headers: list[dict] | None, name: str) -> str | None:
    """Case-insensitive header lookup over Graph's internetMessageHeaders."""
    want = name.casefold()
    for h in headers or []:
        if (h.get("name") or "").casefold() == want:
            return h.get("value")
    return None


def _unsub_from_headers(headers: list[dict] | None) -> dict | None:
    """Parse List-Unsubscribe / List-Unsubscribe-Post; None when not offered.

    Returns {mailto, http, one_click}. Real senders emit both the compliant
    bracketed form (<mailto:...>, <https://...>) and, out of spec, a bare
    unbracketed URL (Slack does this), so fall back to comma-splitting.
    """
    raw = _header(headers, "List-Unsubscribe")
    if not raw:
        return None
    urls = _UNSUB_ANGLE.findall(raw) or [p.strip() for p in raw.split(",") if p.strip()]
    http = next((u for u in urls if u.lower().startswith("http")), None)
    post = _header(headers, "List-Unsubscribe-Post") or ""
    return {
        "mailto": next((u for u in urls if u.lower().startswith("mailto:")), None),
        "http": http,
        # RFC 8058 one-click. Gate on http: senders exist that emit the Post
        # header with no parseable List-Unsubscribe, and one_click=True with
        # nothing to POST to would be a lie.
        "one_click": "one-click" in post.casefold() and http is not None,
    }


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

# FastMCP configures root logging at INFO, which makes httpx print every
# request URL to stderr. Those URLs carry $search terms and message ids, and
# stderr is a log file under both the Desktop host and the daemon. Keep them
# out; warnings and errors still get through.
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# The SDK also logs "Processing request of type X" at INFO for every request,
# which is per-call chatter in a long-lived daemon log. Warnings and errors
# from the same logger still get through.
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)


class _DropUnknownMethodNoise(logging.Filter):
    """Drop the SDK's validation dump for a request method it does not know.

    Claude Desktop sends `server/discover` once per session. mcp<2 has no
    model for it, answers with a JSON-RPC error (correct), and then logs the
    whole pydantic union failure, about thirty lines, through the root logger
    (`logging.warning` in mcp.shared.session). The response is unaffected by
    this filter; only the dump is. Anything else that fails validation still
    logs, so a genuinely malformed request remains visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not (msg.startswith("Failed to validate request")
                    and "'server/discover'" in msg)


logging.getLogger().addFilter(_DropUnknownMethodNoise())


@mcp.tool()
def diagnose() -> list[dict]:
    """Health checks for this server's own environment, one row per check:
    resolver API, direct DNS, TCP reach to the login host, token cache (mode,
    account and expiry, never contents), clock skew against Graph, and the
    parent process chain with whether the system resolver is visible. Needs no
    scope and never touches the mailbox. Run it first when any other tool
    fails with a connection or name-resolution error: a healthy network with a
    failed resolver API means the host process cannot use the system
    resolver, which is fixed by restarting the server from a GUI session, not
    by retrying."""
    return _diagnose()


@mcp.tool()
def list_messages(top: int = 25, folder: str = "inbox") -> list[dict]:
    """Recent messages, newest first. folder: a well-known name (inbox, sentitems,
    drafts, archive, ...) or a folder handle from list_mail_folders."""
    fid = _folder_ref(folder)
    d = _get(
        f"/me/mailFolders/{fid}/messages",
        {
            "$top": min(top, _MAX_TOP),
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments",
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
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,parentFolderId",
        },
    )
    return [_summ(m) for m in d.get("value", []) if m.get("parentFolderId") not in ids]


@mcp.tool()
def get_message(message_id: str, mode: str = "unique", head_chars: int | None = None,
                links: bool = False) -> dict:
    """One message with Markdown body. mode='unique' (default) returns just the new content; mode='full' returns the whole message including quoted thread history. Set head_chars to cap the (sanitized) body length.

    The sanitizer strips long and tracker-laden URLs from the body, which also
    removes legitimate long links (forms, RSVP pages, SharePoint). Pass
    links=True to additionally get "links": [{text, url}] harvested before
    sanitization (deduped, capped at 40). The body itself is unchanged.
    Use it only when you need to follow or report a specific link."""
    if mode not in ("unique", "full"):
        raise ValueError(f"mode must be 'unique' or 'full', got {mode!r}")
    field = "uniqueBody" if mode == "unique" else "body"
    m = _get(
        f"/me/messages/{_resolve(message_id)}",
        {"$select": "id,subject,from,toRecipients,receivedDateTime,parentFolderId,"
                    f"hasAttachments,internetMessageHeaders,{field}"},
    )
    if m.get("parentFolderId") in _excluded_folder_ids():
        raise RuntimeError(f"message {message_id} is in an excluded folder")
    md = _body_to_markdown(m.get(field))
    out = {
        "id": _handle(m["id"]),
        "subject": m.get("subject"),
        "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
        "to": [
            (r.get("emailAddress") or {}).get("address")
            for r in (m.get("toRecipients") or [])
        ],
        "received": m.get("receivedDateTime"),
        "hasAttachments": m.get("hasAttachments"),
        "bodyMode": mode,
        "body": _postclean(md, head_chars),
    }
    if links:
        out["links"] = _extract_links(md)
    unsub = _unsub_from_headers(m.get("internetMessageHeaders"))
    if unsub:                       # absent on ordinary mail, not False
        out["unsubscribe"] = unsub
    return out



# ---------------------------------------------------------------------------
# Attachments — metadata via $expand, content extracted to text in memory.
# Never written to disk, never returned as base64 (see CLAUDE.md guardrails).

_ATTACH_MAX_BYTES = 10 * 1024 * 1024      # refuse before download above this
_ATTACH_MAX_CHARS = 60_000                # hard ceiling on returned text
_TEXT_SUFFIXES = (".txt", ".md", ".csv", ".tsv", ".json", ".ics", ".log",
                  ".xml", ".yaml", ".yml")
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _attachment_kind(odata_type: str | None) -> str:
    """Graph's @odata.type -> file | item | reference | unknown."""
    t = (odata_type or "").lower()
    if t.endswith("fileattachment"):
        return "file"
    if t.endswith("itemattachment"):
        return "item"
    if t.endswith("referenceattachment"):
        return "reference"
    return "unknown"


def _extract_text(name: str | None, content_type: str | None, data: bytes):
    """(text, extractor) for text-like attachments, (None, None) when refused.

    Everything happens on the bytes in hand: no tempfile, no open(). PDF via
    pypdf, .docx via the document.xml inside the zip, HTML via html2text, other
    text types decoded as UTF-8 with replacement.
    """
    ct = (content_type or "").lower().split(";")[0].strip()
    low = (name or "").lower()
    if ct == "application/pdf" or low.endswith(".pdf"):
        from pypdf import PdfReader          # lazy: only PDF callers pay the import
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(p for p in pages if p), "pypdf"
    if ct == _DOCX_TYPE or low.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
        paras = []
        for p in root.iter(_W_NS + "p"):
            t = "".join(n.text or "" for n in p.iter(_W_NS + "t"))
            if t.strip():
                paras.append(t)
        return "\n\n".join(paras), "docx-xml"
    if ct == "text/html" or low.endswith((".html", ".htm")):
        return _h2t.handle(data.decode("utf-8", errors="replace")).strip(), "html2text"
    if ct.startswith("text/") or ct in ("application/json", "application/xml",
                                        "text/calendar") or low.endswith(_TEXT_SUFFIXES):
        return data.decode("utf-8", errors="replace"), "utf-8"
    return None, None


def _attachment_meta(a: dict) -> dict:
    return {
        "id": _handle(a["id"]),
        "name": a.get("name"),
        "contentType": a.get("contentType"),
        "size": a.get("size"),
        "isInline": a.get("isInline"),
        "kind": _attachment_kind(a.get("@odata.type")),
    }


@mcp.tool()
def list_attachments(message_id: str) -> dict:
    """Attachment metadata for one message: name, type, size, inline flag, kind.

    kind is 'file' (readable with get_attachment), 'item' (an attached
    message, not readable here) or 'reference' (a OneDrive/SharePoint link;
    use get_message(links=True) to see the URL). One request; no content is
    downloaded."""
    mid = _resolve(message_id)
    m = _get(
        f"/me/messages/{mid}",
        {"$select": "id,parentFolderId,hasAttachments",
         "$expand": "attachments($select=id,name,contentType,size,isInline)"},
    )
    if m.get("parentFolderId") in _excluded_folder_ids():
        raise RuntimeError(f"message {message_id} is in an excluded folder")
    return {
        "id": _handle(m["id"]),
        "hasAttachments": m.get("hasAttachments"),
        "attachments": [_attachment_meta(a) for a in (m.get("attachments") or [])],
    }


@mcp.tool()
def get_attachment(message_id: str, attachment_id: str, head_chars: int = 20000) -> dict:
    """Text of one file attachment, extracted in memory: PDF (pypdf), .docx,
    HTML, and plain text types (txt, md, csv, json, ics). Returns metadata plus
    extractor, chars, truncated, text (capped at head_chars, hard ceiling
    60000). Refuses, with metadata and a 'refused' reason: item and reference
    attachments, files over 10 MB, and binary types with no extractor (images,
    xlsx, pptx, zip). Never returns bytes or base64 and never writes to disk.
    A PDF that extracts to zero chars is a scanned image; OCR is out of scope."""
    mid, aid = _resolve(message_id), _resolve(attachment_id)
    m = _get(f"/me/messages/{mid}", {"$select": "id,parentFolderId"})
    if m.get("parentFolderId") in _excluded_folder_ids():
        raise RuntimeError(f"message {message_id} is in an excluded folder")
    a = _get(f"/me/messages/{mid}/attachments/{aid}",
             {"$select": "id,name,contentType,size,isInline"})
    meta = _attachment_meta(a)
    if meta["kind"] != "file":
        meta["refused"] = ("item attachment (an attached message); not readable here"
                           if meta["kind"] == "item" else
                           "reference attachment (a cloud link); use get_message(links=True)")
        return meta
    size = a.get("size") or 0
    if size > _ATTACH_MAX_BYTES:
        meta["refused"] = f"{size} bytes exceeds the {_ATTACH_MAX_BYTES} byte limit"
        return meta
    data = _get_bytes(f"/me/messages/{mid}/attachments/{aid}/$value")
    text, extractor = _extract_text(a.get("name"), a.get("contentType"), data)
    if text is None:
        meta["refused"] = "binary type; no text extractor"
        return meta
    cap = max(1, min(int(head_chars), _ATTACH_MAX_CHARS))
    meta.update({"extractor": extractor, "chars": len(text),
                 "truncated": len(text) > cap, "text": text[:cap]})
    return meta


# Saving attachments to disk is the one deliberate exception to the
# no-persistence rule. It is on by default and writes to ~/Downloads, because
# that is where a Mac user expects a downloaded file to appear and because the
# server now runs as a launchd daemon, whose environment can only be changed by
# editing its plist. ATTACH_SAVE_DIR overrides the root; "off" or an empty
# value turns the tool off. The exception is narrow: bytes land only under that
# one root, the file name is sanitized to a bare basename, nothing is ever
# overwritten, and the tool returns a path and a digest, never the bytes. See
# CLAUDE.md guardrails.

_ATTACH_SAVE_DEFAULT = "~/Downloads"


def _attach_save_dir(raw: str | None) -> str:
    """Save root from the ATTACH_SAVE_DIR value: unset means ~/Downloads;
    empty or 'off' means the tool is off (returns ''); anything else is used
    as given, with ~ expanded."""
    if raw is None:
        raw = _ATTACH_SAVE_DEFAULT
    raw = raw.strip()
    if not raw or raw.lower() == "off":
        return ""
    return os.path.expanduser(raw)


ATTACH_SAVE_DIR = _attach_save_dir(os.environ.get("ATTACH_SAVE_DIR"))
_ATTACH_SAVE_MAX_BYTES = 50 * 1024 * 1024  # Graph's own fileAttachment ceiling is ~150 MB
_FNAME_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def _safe_filename(name: str | None, fallback: str = "attachment") -> str:
    """Bare basename, printable, no path separators, no leading dot."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = _FNAME_BAD.sub("_", base).strip(" .")
    base = unicodedata.normalize("NFC", base)
    if not base or base in (".", ".."):
        base = fallback
    return base[:200]


def _save_target(root: str, subdir: str | None, filename: str) -> str:
    """Resolved path under root; raises on traversal; never an existing file."""
    root_real = os.path.realpath(root)
    raw = (subdir or "").strip().replace("\\", "/")
    if raw.startswith("/"):
        raise ValueError("subdir must be relative to ATTACH_SAVE_DIR")
    rel = raw.strip("/")
    if rel and (rel.startswith("..") or "/.." in rel):
        raise ValueError("subdir must be a relative path with no '..'")
    directory = os.path.realpath(os.path.join(root_real, rel)) if rel else root_real
    if directory != root_real and not directory.startswith(root_real + os.sep):
        raise ValueError("subdir escapes ATTACH_SAVE_DIR")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    n = 1
    while os.path.lexists(candidate):
        candidate = os.path.join(directory, f"{stem}-{n}{ext}")
        n += 1
    return candidate


def save_attachment(message_id: str, attachment_id: str,
                    subdir: str | None = None, filename: str | None = None) -> dict:
    """Save one file attachment to disk under the save root (~/Downloads by
    default, or ATTACH_SAVE_DIR when the host sets it) and return its
    metadata, path, bytes written, and sha256.
    The only tool on this server that writes message content to disk, and it
    writes only there: `subdir` is relative and may not contain '..',
    `filename` is reduced to a bare basename (default: the attachment's own
    name), and an existing file is never overwritten (a -1, -2 suffix is
    appended instead). Refuses item and reference attachments and files over
    50 MB. Never returns the bytes. For text you can read in place, prefer
    get_attachment; use this for spreadsheets, images, archives, or anything
    that has to be opened in another application."""
    mid, aid = _resolve(message_id), _resolve(attachment_id)
    m = _get(f"/me/messages/{mid}", {"$select": "id,parentFolderId"})
    if m.get("parentFolderId") in _excluded_folder_ids():
        raise RuntimeError(f"message {message_id} is in an excluded folder")
    a = _get(f"/me/messages/{mid}/attachments/{aid}",
             {"$select": "id,name,contentType,size,isInline"})
    meta = _attachment_meta(a)
    if meta["kind"] != "file":
        meta["refused"] = ("item attachment (an attached message); not savable here"
                           if meta["kind"] == "item" else
                           "reference attachment (a cloud link); use get_message(links=True)")
        return meta
    size = a.get("size") or 0
    if size > _ATTACH_SAVE_MAX_BYTES:
        meta["refused"] = f"{size} bytes exceeds the {_ATTACH_SAVE_MAX_BYTES} byte limit"
        return meta
    target = _save_target(ATTACH_SAVE_DIR, subdir, _safe_filename(filename or a.get("name")))
    data = _get_bytes(f"/me/messages/{mid}/attachments/{aid}/$value")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    meta.update({"path": target, "bytes": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()})
    return meta


@mcp.tool()
def triage_messages(top: int = 25, folder: str = "inbox", head_chars: int = 600) -> list[dict]:
    """Bulk triage: recent messages with sanitized, head-truncated bodies in ONE call —
    stream these into context to categorize, instead of N get_message round-trips. Bodies
    are cleaned (invisible padding + tracking URLs stripped) and capped at head_chars.
    folder: a well-known name or a folder handle from list_mail_folders."""
    fid = _folder_ref(folder)
    # Full `body` (not uniqueBody, which is unreliable in a list $select); head_chars
    # trims quoted history for top-posted mail, which is sufficient for triage.
    d = _get(
        f"/me/mailFolders/{fid}/messages",
        {
            "$top": min(top, 50),
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,body",
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


@mcp.tool()
def unsubscribe_scan(top: int = 100, folder: str = "inbox") -> list[dict]:
    """Find which recent messages offer an unsubscribe. Metadata only, no bodies.

    Step 1 of unsubscribing: this says WHICH messages can be unsubscribed from;
    unsubscribe_targets then says WHERE to send it. Deliberately kept off
    triage_messages, which would have to pull every message's full header block
    (~2.4x its payload) on every sweep to answer a question most sweeps never ask.

    Because it fetches no bodies, this scans far wider than triage_messages
    can — several hundred messages for comparable cost — so it catches list mail
    that has already scrolled out of a triage window.

    folder: a well-known name or a folder handle from list_mail_folders.

    Returns [{id, from, subject, received}] for matching messages only.
    """
    fid = _folder_ref(folder)
    # internetMessageHeaders is $select-able on a collection and is the only
    # path that works: the singleValueExtendedProperties/PS_INTERNET_HEADERS
    # form, which would return just this one header, resolves to nothing on
    # Exchange Online (verified against known-positive mail — 0 of 14 found).
    d = _get(
        f"/me/mailFolders/{fid}/messages",
        {
            "$top": min(top, _MAX_TOP),
            "$select": "id,subject,from,receivedDateTime,internetMessageHeaders",
            "$orderby": "receivedDateTime desc",
        },
    )
    return [
        {
            "id": _handle(m["id"]),
            "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
            "subject": m.get("subject"),
            "received": m.get("receivedDateTime"),
        }
        for m in d.get("value", [])
        if _unsub_from_headers(m.get("internetMessageHeaders"))
    ]


@mcp.tool()
def unsubscribe_targets(message_ids: list[str]) -> list[dict]:
    """Resolve unsubscribe endpoints for specific messages (RFC 2369 / RFC 8058).

    Step 2 of unsubscribing; discover candidates with unsubscribe_scan first and
    resolve only the ones you intend to act on.

    Returns [{id, from, subject, mailto, http, one_click}]; mailto/http are None
    when not offered. one_click=True means the http URL accepts an RFC 8058 POST
    (body: List-Unsubscribe=One-Click) and completes with no browser; otherwise
    http is a landing page needing a human, and mailto — an empty message to that
    address — is the automatable path.

    This server is read-only: it reports targets, it does not fire them.
    """
    excluded = _excluded_folder_ids()
    ids = message_ids[:50]
    reqs = [
        {
            "method": "GET",
            "url": (f"/me/messages/{_resolve(mid)}"
                    "?$select=id,subject,from,parentFolderId,internetMessageHeaders"),
        }
        for mid in ids
    ]
    out = []
    for r in _batch(reqs):
        # Tolerate per-message failures (stale handle -> 404) rather than
        # failing the whole call, matching _batch_summary's posture.
        if r.get("status") != 200:
            continue
        m = r.get("body") or {}
        if m.get("parentFolderId") in excluded:
            raise RuntimeError("a requested message is in an excluded folder")
        out.append(
            {
                "id": _handle(m["id"]),
                "from": (m.get("from") or {}).get("emailAddress", {}).get("address"),
                "subject": m.get("subject"),
                **(_unsub_from_headers(m.get("internetMessageHeaders"))
                   or {"mailto": None, "http": None, "one_click": False}),
            }
        )
    return out


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
    The returned id addresses a folder on both paths: pass it as a `move_messages`
    destination to file into a custom folder, or as the `folder` argument to
    list_messages, triage_messages, or unsubscribe_scan to read one."""
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


# --- Draft attachments ---------------------------------------------------------
# The one path on this server that reads a local file and uploads it. It is
# save_attachment's discipline run in the other direction: the file must
# resolve, after realpath, to a regular file under an allowed root, so a
# symlink that escapes the root is refused; there is a per-file ceiling; and
# the return carries name, size and digest, never the bytes. No env gate: the
# user names each file in the call and the content lands only in their own
# Drafts. See CLAUDE.md guardrails.

_DRAFT_ATTACH_INLINE_MAX = 3 * 1024 * 1024      # Graph's ceiling for contentBytes in one POST
_DRAFT_ATTACH_MAX_BYTES = 150 * 1024 * 1024     # Graph's ceiling for an Outlook upload session
_UPLOAD_CHUNK = 12 * 320 * 1024                 # 3.75 MiB: under Graph's 4 MB range cap and a
                                                # multiple of the 320 KiB it asks for elsewhere


def _attach_roots() -> list[str]:
    """Directories a draft attachment may come from: $HOME, plus ATTACH_SAVE_DIR
    when set, so a file this server saved can be attached back out."""
    roots = [os.path.realpath(os.path.expanduser("~"))]
    if ATTACH_SAVE_DIR and os.path.realpath(ATTACH_SAVE_DIR) not in roots:
        roots.append(os.path.realpath(ATTACH_SAVE_DIR))
    return roots


def _attach_source(path: str) -> str:
    """Resolved path of a regular file under an allowed root; raises otherwise."""
    if not path or not path.strip():
        raise ValueError("attachment path is empty")
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        raise ValueError(f"attachment path must be absolute: {path!r}")
    real = os.path.realpath(p)
    roots = _attach_roots()
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise ValueError(
            f"attachment {path!r} resolves to {real!r}, outside the allowed roots "
            f"({', '.join(roots)})"
        )
    if not os.path.isfile(real):
        raise ValueError(f"attachment {path!r} is not a regular file")
    size = os.path.getsize(real)
    if size == 0:
        raise ValueError(f"attachment {path!r} is empty")
    if size > _DRAFT_ATTACH_MAX_BYTES:
        raise ValueError(
            f"attachment {path!r} is {size} bytes, over the {_DRAFT_ATTACH_MAX_BYTES} byte ceiling"
        )
    return real


def _attach_content_type(real: str) -> str:
    import mimetypes
    return mimetypes.guess_type(real)[0] or "application/octet-stream"


def _attach_payload(real: str, data: bytes) -> dict:
    """fileAttachment body for a single-POST (under 3 MB) upload."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": _safe_filename(os.path.basename(real)),
        "contentType": _attach_content_type(real),
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }


def _upload_attachment(draft_id: str, real: str) -> dict:
    """Attach one local file to a draft; returns {name, bytes, sha256}."""
    with open(real, "rb") as fh:
        data = fh.read()
    size, name = len(data), _safe_filename(os.path.basename(real))
    if size <= _DRAFT_ATTACH_INLINE_MAX:
        _req("POST", f"/me/messages/{draft_id}/attachments", json=_attach_payload(real, data))
    else:
        sess = _req(
            "POST", f"/me/messages/{draft_id}/attachments/createUploadSession",
            json={"AttachmentItem": {"attachmentType": "file", "name": name, "size": size,
                                     "contentType": _attach_content_type(real)}},
        )
        url = sess.get("uploadUrl")
        if not url:
            raise RuntimeError("Graph returned an upload session with no uploadUrl")
        # The session URL is pre-authenticated; Graph rejects a bearer token on
        # it, so this goes through the client directly rather than _req.
        for start in range(0, size, _UPLOAD_CHUNK):
            chunk = data[start:start + _UPLOAD_CHUNK]
            r = _HTTP.put(url, content=chunk, headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{size}",
            })
            if r.status_code not in (200, 201, 202):
                raise RuntimeError(
                    f"upload range {start}-{start + len(chunk) - 1} of {name!r} "
                    f"failed with HTTP {r.status_code}"
                )
    return {"name": name, "bytes": size, "sha256": hashlib.sha256(data).hexdigest()}


def create_draft(to: list[str], subject: str, body: str,
                 cc: list[str] | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Create a draft in Drafts (send is by design out of scope; the user sends from Outlook). Returns id and webLink.

    attachments: absolute local paths to attach. Each must resolve to a
    regular file under your home directory (or under ATTACH_SAVE_DIR when the
    host sets it); a symlink leading elsewhere is refused, as is anything
    over 150 MB. Every path is validated before the draft is created, and if
    any upload fails the draft is soft-deleted so Drafts never holds a
    half-built message. The return then carries "attachments":
    [{name, bytes, sha256}], never the content."""
    sources = [_attach_source(p) for p in (attachments or [])]
    payload = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": _recipients(to),
    }
    if cc:
        payload["ccRecipients"] = _recipients(cc)
    m = _req("POST", "/me/messages", json=payload)
    out = {"id": m.get("id"), "webLink": m.get("webLink")}
    if sources:
        done: list[dict] = []
        try:
            for real in sources:
                done.append(_upload_attachment(m["id"], real))
        except Exception as e:
            # Soft delete. Verified 2026-10-05: Exchange routes a deleted
            # *draft* past Deleted Items into Recoverable Items/Deletions
            # (Outlook: "Recover deleted items"), still recoverable for the
            # retention window, so Drafts never shows a half-built message.
            try:
                _req("DELETE", f"/me/messages/{m['id']}")
            except Exception:
                pass
            raise RuntimeError(
                f"attachment {len(done) + 1} of {len(sources)} failed ({e}); the "
                "half-built draft was removed (recoverable from Recoverable Items)"
            ) from e
        out["attachments"] = done
    return out


def send_message(to: list[str], subject: str, body: str,
                 cc: list[str] | None = None,
                 attachments: list[str] | None = None) -> dict:
    """Create a draft, then send it. Leaves a reviewable artifact in Sent Items.
    attachments follow create_draft's path rules."""
    draft = create_draft(to, subject, body, cc=cc, attachments=attachments)
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


def get_event(event_id: str, head_chars: int | None = None, links: bool = False) -> dict:
    """One event including a sanitized Markdown body. Times in the configured timezone (CAL_TIMEZONE, default Eastern).
    Event bodies are often KB of HTML join-link boilerplate (Teams/Zoom); set
    head_chars to cap the cleaned body. links=True adds "links": [{text, url}]
    harvested before sanitization (see get_message); the body is unchanged."""
    e = _req(
        "GET", f"/me/events/{_resolve(event_id)}",
        params={"$select": "id,subject,start,end,isAllDay,location,organizer,attendees,body,webLink"},
        extra_headers=CAL_TZ_HEADER,
    )
    s = _event_summ(e)
    md = _body_to_markdown(e.get("body"))
    s["body"] = _postclean(md, head_chars)
    if links:
        s["links"] = _extract_links(md)
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

if ATTACH_SAVE_DIR:
    mcp.tool()(save_attachment)

if CAPS["cal_read"]:
    for fn in (list_events, get_event):
        mcp.tool()(fn)

if CAPS["cal_write"]:
    for fn in (create_event, update_event, delete_event):
        mcp.tool()(fn)


# ---------------------------------------------------------------------------
# Daemon: this same FastMCP server over a Unix socket. ROADMAP.md Phase 3.
#
# The server inherits the context of whatever spawns it, and a Claude Code
# background host once spawned it into a process tree with no system resolver.
# Running one instance in the GUI login session (launchd, RunAtLoad) and
# bridging each host to it through the stdio shim at the top of this file takes
# the host's context out of the picture. The listener is a 0600 socket inside
# the 0700 credential directory, every connection is checked for the daemon's
# own uid, and no TCP port is ever opened: stdio stays the host boundary.
#
# One instance per credential directory, enforced by an exclusive flock on
# SOCK_LOCK held for the daemon's lifetime. The shim refuses to fall back to an
# in-process server while that lock is held, so two processes never rotate the
# same refresh token. Long-lived by design, so it runs on the file backend
# only: the Keychain backends are what killed long-lived processes in August
# 2026 (see Credentials in CLAUDE.md).
#
# Each accepted connection is one MCP session with its own initialize
# handshake. Sessions run concurrently in the event loop, but tool bodies are
# synchronous, so a Graph call in one session stalls the others for its
# duration; two Claude hosts queue, which is the intended capacity.

SOCK_PATH = os.path.expanduser(f"~/.config/{KEYCHAIN_SERVICE}/mcp.sock")
SOCK_LOCK = SOCK_PATH + ".lock"
_MAX_LINE = 16 * 1024 * 1024        # one JSON-RPC message; attachments travel as paths


def _log(msg: str) -> None:
    """Daemon log line to stderr (launchd redirects it). Metadata only."""
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}",
          file=sys.stderr, flush=True)


def _parse_xucred(buf: bytes) -> int:
    """uid from a struct xucred (macOS LOCAL_PEERCRED): u_int version, uid_t uid, ..."""
    version, uid = struct.unpack_from("@II", buf, 0)
    if version != 0:                    # XUCRED_VERSION
        raise ValueError(f"unexpected xucred version {version}")
    return uid


def _peer_uid(sock: socket.socket) -> int:
    """Effective uid of the process on the other end of a Unix socket."""
    if sys.platform == "darwin":
        return _parse_xucred(sock.getsockopt(0, 0x0001, 76))    # SOL_LOCAL, LOCAL_PEERCRED
    if hasattr(socket, "SO_PEERCRED"):
        pid, uid, gid = struct.unpack("@iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    raise RuntimeError("no peer-credential API on this platform")


def _peer_allowed(uid: int | None) -> bool:
    return uid is not None and uid == os.getuid()


async def _daemon_main() -> None:
    import signal
    import anyio
    from anyio.abc import SocketAttribute
    from anyio.streams.buffered import BufferedByteReceiveStream
    from mcp.server.stdio import stdio_server

    class _Lines:
        """The `async for line in stdin` half of what stdio_server expects."""
        def __init__(self, stream):
            self._buf = BufferedByteReceiveStream(stream)

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            try:
                line = await self._buf.receive_until(b"\n", _MAX_LINE)
            except (anyio.IncompleteRead, anyio.EndOfStream, anyio.ClosedResourceError):
                raise StopAsyncIteration            # peer hung up; a torn tail is garbage
            return line.decode("utf-8", errors="replace")

    class _Writer:
        """The `write`/`flush` half."""
        def __init__(self, stream):
            self._stream = stream

        async def write(self, text: str) -> None:
            await self._stream.send(text.encode("utf-8"))

        async def flush(self) -> None:
            pass

    async def handle(stream) -> None:
        try:
            uid = _peer_uid(stream.extra(SocketAttribute.raw_socket))
        except Exception as e:
            uid = None
            _log(f"peer credential check failed: {type(e).__name__}: {e}")
        if not _peer_allowed(uid):
            _log(f"rejected connection from uid {uid}")
            await stream.aclose()
            return
        _log("session open")
        try:
            async with stream:
                async with stdio_server(stdin=_Lines(stream), stdout=_Writer(stream)) as (r, w):
                    await mcp._mcp_server.run(r, w, mcp._mcp_server.create_initialization_options())
        except Exception as e:
            _log(f"session ended: {type(e).__name__}: {e}")
        else:
            _log("session closed")

    listener = await anyio.create_unix_listener(SOCK_PATH, mode=0o600)
    _log(f"listening on {SOCK_PATH} (pid {os.getpid()}, uid {os.getuid()}, "
         f"{len(mcp._tool_manager._tools)} tools, backend {KEYCHAIN_BACKEND})")
    try:
        async with anyio.create_task_group() as tg:
            async def stop_on_signal() -> None:
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                    async for sig in signals:
                        _log(f"received {signal.Signals(sig).name}; shutting down")
                        tg.cancel_scope.cancel()
                        return
            tg.start_soon(stop_on_signal)
            tg.start_soon(listener.serve, handle)
    finally:
        await listener.aclose()
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        _log("stopped")


def _daemon() -> int:
    if KEYCHAIN_BACKEND != "file":
        print(f"--daemon requires KEYCHAIN_BACKEND=file (got {KEYCHAIN_BACKEND!r}): a "
              "long-lived process on a Keychain backend is the failure mode documented "
              "under Credentials in CLAUDE.md", file=sys.stderr)
        return 2
    import fcntl
    import anyio
    os.makedirs(os.path.dirname(SOCK_PATH), mode=0o700, exist_ok=True)
    lock_fd = os.open(SOCK_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another daemon holds {SOCK_LOCK}; exiting", file=sys.stderr)
        os.close(lock_fd)
        return 1
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, f"{os.getpid()}\n".encode())      # informational; the flock is the lock
    try:
        anyio.run(_daemon_main)
    finally:
        os.close(lock_fd)                                # releases the flock
    return 0


# ---------------------------------------------------------------------------
# CLI.

def _report(res) -> None:
    if res and "access_token" in res:
        print("OK", file=sys.stderr)
    else:
        err = (res or {}).get("error_description") or (res or {}).get("error") or "unknown"
        print(f"FAILED: {err}", file=sys.stderr)


def _auth() -> None:
    _report(_app().acquire_token_interactive(scopes=SCOPES))


def _auth_device() -> None:
    """Device-code auth, for when no browser on this machine is reachable.

    acquire_token_interactive opens a browser in the GUI session, so it cannot
    be driven over SSH: a remote session has no graphic access, which is the
    same constraint that makes a locked Keychain unpromptable. This flow prints
    a short code to enter from any other device, so the server stays
    re-authorizable while away from the machine. That makes it the only
    recovery path for a credential failure that happens while travelling.

    Requires "Allow public client flows" on the AAD app registration. Without
    it Entra refuses initiate_device_flow with AADSTS7000218.
    """
    app = _app()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        err = flow.get("error_description") or flow.get("error") or flow
        print(f"FAILED to start device flow: {err}", file=sys.stderr)
        print(
            "If that is AADSTS7000218, enable 'Allow public client flows' on the "
            "app registration in Entra, then retry.",
            file=sys.stderr,
        )
        return
    print(flow["message"], file=sys.stderr)
    sys.stderr.flush()
    _report(app.acquire_token_by_device_flow(flow))   # blocks until entered or expired


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        sys.exit(_daemon())
    elif "--diagnose" in sys.argv:
        sys.exit(_print_diagnose())
    elif "--migrate-cache" in sys.argv:
        sys.exit(0 if _seed_cache_file(verbose=True) else 1)
    elif "--auth-device" in sys.argv:
        _auth_device()
    elif "--auth" in sys.argv:
        _auth()
    else:
        mcp.run()
