# graph-m365-mcp

> **Part of [email-cleaner-for-llms](https://github.com/MalcolmWardlaw/email-cleaner-for-llms)** — a small family of sanitizing MCP proxies that sit between a mail API and the LLM, flattening HTML and stripping cruft so a model can batch-classify and act on hundreds of messages without blowing its context window.
>
> **Backends:** **graph-m365-mcp** (Microsoft 365 / Graph) · [fastmail-clean-mcp](https://github.com/MalcolmWardlaw/fastmail-clean-mcp) (Fastmail / JMAP) · IMAP (planned)

Local stdio MCP server exposing Microsoft 365 mail and personal-calendar tools to a Claude Desktop or Claude Code host, via the Microsoft Graph v1.0 API. Read-and-stage by design (drafts, not send; soft-delete, not hard-delete) with a configurable folder-exclusion gate on the read paths.

> Unofficial and independent. Not affiliated with, endorsed by, or sponsored by Microsoft. "Microsoft 365", "Microsoft Graph", and "Outlook" are trademarks of Microsoft Corporation, used here only to describe what the server talks to.

## Prerequisites

- macOS. The token cache is a 0600 file by default; the login Keychain is used only by the optional `cli` backend and for a one-time seed from an older install, and the optional daemon runs under launchd.
- Python ≥3.11
- [`uv`](https://docs.astral.sh/uv/) on `PATH`
- An Azure AD app registration in the target tenant with delegated Microsoft Graph permissions consented

## Setup

### 1. Register a public-client AAD app

Here's the workflow, in the [Microsoft Entra admin center](https://entra.microsoft.com)
(formerly Azure Active Directory) for the target tenant:

1. **Identity → Applications → App registrations → New registration.**
   - Name: anything (e.g. `graph-m365-mcp`).
   - Supported account types: **Accounts in this organizational directory only
     (single tenant)**.
   - Leave Redirect URI blank for now. **Register.**
2. From the app's **Overview**, record the **Application (client) ID** and
   **Directory (tenant) ID** — these become `GRAPH_CLIENT_ID` and
   `GRAPH_TENANT_ID`.
3. **Authentication → Add a platform → Mobile and desktop applications.** Check
   `http://localhost` (the OAuth loopback; MSAL picks the port at auth time),
   then **Configure**.
4. Still under **Authentication → Advanced settings**, set **Allow public client
   flows** to **Yes**. This toggle is the one most often missed; without it the
   interactive public-client flow fails. **Save.**
5. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions.** Add only the scopes for capabilities you'll enable; the
   maximum the server ever requests is `Mail.Read`, `Mail.ReadWrite`,
   `Mail.Send`, `Calendars.Read`, `Calendars.ReadWrite`. The server never
   requests `*.Shared`. Do **not** create a client secret.
6. If your tenant requires it, click **Grant admin consent for <tenant>**. If
   you can't, a tenant admin must — see the consent note in `CLAUDE.md`.

The structure here is correct as of early 2026; portal menu labels drift over
time (Microsoft renames blades roughly yearly). A general summary, in terms that
should survive the renames:

- A **single-tenant, public-client** app registration (no client secret).
- A **loopback redirect URI** (`http://localhost`) registered under the
  desktop/native platform, with **public client flows allowed**.
- **Delegated** Microsoft Graph permissions for the capabilities you enable —
  some subset of `Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.Read`,
  `Calendars.ReadWrite` — **admin-consented** if the tenant requires it.
- The app's **client id** and **tenant id**, which become `GRAPH_CLIENT_ID` and
  `GRAPH_TENANT_ID`.

### 2. One-time interactive auth

In a terminal:

```fish
cd ~/code/mcp/graph-m365-mcp
GRAPH_CLIENT_ID=<your-app-id> GRAPH_TENANT_ID=<your-tenant-id> uv run server.py --auth
```

A browser opens for the OAuth flow. On success, MSAL writes its token cache (which holds the refresh token) to `~/.config/$KEYCHAIN_SERVICE/cache.json` (`graph-m365` by default), a file created with mode 0600 in a 0700 directory. The non-secret signal file `cache.signal` sits next to it.

That file is the default because it is the only store that works from a session with no GUI: see [Where the token lives](#where-the-token-lives). With `KEYCHAIN_BACKEND=cli` the cache goes to the macOS login Keychain instead, under `service=$KEYCHAIN_SERVICE`, `account=default-mailbox` plus `default-mailbox.1` and so on, written by a `/usr/bin/security` subprocess and created with `security` on the item's ACL so later reads never prompt (see [Keychain access](#keychain-access)).

Migrating an install whose cache is in the Keychain: nothing to do. The first launch on the file backend from a GUI session finds no cache file, reads the Keychain once, and seeds the file from it. The copy is one-way; after it the Keychain item is stale and can be deleted (`security delete-generic-password -s graph-m365 -a default-mailbox`, repeat for `.1`, `.2` and the `.staged` twins).

After this, normal runs use silent token acquisition. Re-run `--auth` only if silent acquisition fails or you enable a capability whose scope MSAL has not seen before.

## Configuration

All configuration is via environment variables. The server itself ships with no host-specific defaults; the host supplies them.

| Variable           | Default     | Effect                                                                  |
|--------------------|-------------|-------------------------------------------------------------------------|
| `GRAPH_CLIENT_ID`  | (required)  | AAD application id                                                      |
| `GRAPH_TENANT_ID`  | (required)  | AAD tenant id (single-tenant authority)                                 |
| `MAIL_READ`        | `1`         | Register `list_messages`, `search_messages`, `get_message`, `triage_messages`, `list_mail_folders`, `unsubscribe_scan`, `unsubscribe_targets`, `list_attachments`, `get_attachment`; adds `Mail.Read` |
| `MAIL_WRITE`       | `1`         | Register `archive_message`, `move_messages`, `delete_messages`, `mark_messages`; adds `Mail.ReadWrite` |
| `MAIL_DRAFT`       | `1`         | Register `create_draft` (with local-file `attachments`); adds `Mail.ReadWrite` |
| `MAIL_SEND`        | `0`         | Register `send_message`; adds `Mail.Send` (off by design)               |
| `ATTACH_SAVE_DIR`  | `~/Downloads` | Root for `save_attachment`, which writes file attachments under this directory only; no scope added. Unset means `~/Downloads`; empty or `off` leaves the tool unregistered |
| `CAL_READ`         | `1`         | Register `list_events`, `get_event`; adds `Calendars.Read`              |
| `CAL_WRITE`        | `1`         | Register `create/update/delete_event`; adds `Calendars.ReadWrite`       |
| `EXCLUDE_FOLDERS`  | (empty)     | Comma-separated top-level folder display names hard-blocked from list/search/get. When non-empty, registers `excluded_recent_count` for metadata-only counts. |
| (always)           |             | `diagnose` registers regardless of every dial: environment health checks, no scope, never touches the mailbox. Also `server.py --diagnose` from a shell. |
| `KEYCHAIN_SERVICE` | `graph-m365`| Name that keys the `~/.config/<name>/` cache directory and, with the `cli` backend, the macOS Keychain service. Override only to reuse a cache created under a different name. |
| `CAL_TIMEZONE`     | `Eastern Standard Time`| Windows/Outlook timezone name applied to calendar reads and writes (e.g. `Pacific Standard Time`, `UTC`). An explicit ISO offset in a passed datetime still wins. |
| `KEYCHAIN_BACKEND` | `file`      | Where the token cache lives: `file` (a 0600 file at `~/.config/<name>/cache.json`; works from GUI and non-GUI sessions alike, and the only choice off macOS), `cli` (macOS login Keychain through a fresh `/usr/bin/security` process per access; GUI login session only), `msal` (msal-extensions' in-process Keychain client, kept for A/B only). |

When a capability is off, neither its tools nor its scope are requested. Requested scopes are the minimum union over enabled capabilities; the AAD-consented grant is only the ceiling.

## Daemon (optional)

The server inherits the context of whatever spawns it. A Claude Code
background host once spawned it into a process tree with no system resolver,
and every call failed for an hour while the network was fine. The fix is to
run one instance in the GUI login session and have each host talk to it:

```fish
fish launchd/install.fish            # render the plist from the Desktop config and bootstrap it
fish launchd/install.fish --remove   # boot it out again
```

After that, `uv run server.py` (what both hosts invoke) is a thin stdio shim
that bridges to `~/.config/graph-m365/mcp.sock` and needs no DNS, Keychain or
credential of its own. Restart Claude Desktop and reconnect live Claude Code
sessions (`/mcp`) once so they pick it up. If the daemon is down, the shim
falls back to running the server in-process exactly as before, so nothing
depends on the daemon being installed. Logs (metadata only) go to
`~/Library/Logs/graph-m365.log`; `diagnose` from a host shows the daemon's
parent chain under `host context`, which is how you can tell which one
answered.

Three rules. The daemon runs on the file backend only and refuses to start
otherwise, because a long-lived process on a Keychain backend is the failure
described under [Keychain access](#keychain-access). Capability flags belong
to the daemon, since it decides which tools exist: the installer copies the
Desktop `env` block into the LaunchAgent, so re-run it after changing a flag.
And the socket is 0600 inside the 0700 credential directory, with every
connection checked against the daemon's uid; there is no TCP listener and
there will not be one.

## Troubleshooting

If any tool fails with a connection or name-resolution error, run `diagnose` (or
`uv run server.py --diagnose` in a terminal) before anything else. It prints one
line per check: resolver API, direct DNS, TCP reach to the login host, token
cache state (mode, accounts, expiry; never contents), clock skew against Graph,
and the parent process chain with whether `scutil --dns` has output.

The pattern to recognise is `resolver API` failing while `direct DNS` and `TCP
reach` pass. That means the network is fine and only the system resolver is
unreachable from the process tree the server was spawned into, which is what a
Claude Code background host looks like. Retrying does nothing; restart the MCP
server from a GUI session. The connection errors raised by the tools carry the
same classification inline.

## Claude Desktop registration

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "graph-m365": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "/Users/<you>/code/mcp/graph-m365-mcp/server.py"],
      "env": {
        "GRAPH_CLIENT_ID": "<your-app-id>",
        "GRAPH_TENANT_ID": "<your-tenant-id>",
        "MAIL_SEND": "0",
        "CAL_READ": "1",
        "CAL_WRITE": "1",
        "EXCLUDE_FOLDERS": ""
      }
    }
  }
}
```

Absolute `uv` path is required; Claude Desktop does not inherit shell `PATH`. Restart Desktop after edits.

## Claude Code registration

```fish
claude mcp add graph-m365 -- uv run /Users/<you>/code/mcp/graph-m365-mcp/server.py
```

Set `GRAPH_CLIENT_ID`, `GRAPH_TENANT_ID`, and any capability toggles in the same shell before invoking.

## Security model

Single-user, own-mailbox and own-default-calendar only. The server is
deliberately scoped down rather than full-featured:

- **Mail is read-and-stage.** It reads and drafts, never sends — `Mail.Send`
  is dark unless `MAIL_SEND=1`, and requesting it is the only action that may
  trigger a separate admin-consent round. Deletion is *soft*-delete
  (`DELETE /me/messages/{id}`, recoverable from Deleted Items); there is no
  permanent delete or folder-empty.
- **No shared scopes, ever.** The server never requests `*.Shared`; it cannot
  reach delegated or shared mailboxes.
- **Calendar is boxed to the default personal calendar** (`/me/events`,
  `/me/calendar`, `/me/calendarView`). It never enumerates `/me/calendars` and
  exposes no shared-calendar tools. Note: creating or updating an event with
  attendees causes Outlook to send invitations, and deleting an
  organizer-owned meeting sends cancellations.
- **Tool surface = requested scopes.** A capability that is off registers
  neither its tools nor its scope. Requested scopes are the minimal union over
  enabled capabilities; the AAD-consented grant is only a ceiling.
- **No on-disk persistence of message content.** Message bodies are
  returned to the client, which is the point, but never written to disk, and
  neither are access tokens. Attachments follow the same rule: `get_attachment`
  extracts text in memory (PDF, .docx, HTML, plain text) and never returns
  bytes or base64, never writes a file, and refuses binary types outright. The
  one exception is `save_attachment`, registered by default with its root at
  `~/Downloads` (override with `ATTACH_SAVE_DIR`, or set it to `off` to
  unregister the tool): it writes a file attachment under that root and nowhere
  else (relative `subdir`, bare basename, no overwrite, 50 MB cap, 0600) and
  returns the path and a sha256, not the bytes. In the other direction,
  `create_draft(attachments=[...])` is the one path that reads a local file and
  uploads it: each path must resolve (after `realpath`, so symlinks cannot
  escape) to a regular file under your home directory or under
  `ATTACH_SAVE_DIR`, with a 150 MB ceiling; all paths are checked before the
  draft exists, a failed upload removes the draft, and the return carries name,
  size and sha256, never the bytes. The one credential on disk is the MSAL cache with
  the refresh token, at `~/.config/<KEYCHAIN_SERVICE>/cache.json`, mode 0600,
  encrypted at rest only by FileVault. With `KEYCHAIN_BACKEND=cli` it lives in
  the login Keychain instead and nothing but a non-secret reload signal touches
  disk. On either backend the token is never passed on a command line:
  `security` takes it on stdin, so it does not appear in `ps` output.
- **Stdio at the host boundary.** Hosts speak stdio to `server.py`. The
  optional daemon listens only on a 0600 Unix socket inside the credential
  directory, accepts connections only from its own uid, and never opens a TCP
  port. No HTTP transport, no multi-user assumption.

### Where the token lives

macOS tracks login-keychain unlock state per **audit session**, not per user.
A GUI login session is unlocked once you log in. Anything that is not that
session, meaning an SSH or Remote Control login, a launchd job, or a Claude Code
background-job daemon, runs in its own audit session, sees the keychain as
locked, and has no way to prompt: every read returns `errSecInteractionNotAllowed`
(-25308; `security` exits 36). Unlocking the keychain in a terminal on the
desktop does nothing for those sessions, because that unlocks a different
session. This is what killed the server in every headless context through
August and September 2026, and it is not fixable from inside the process.

So the default backend is a file: `~/.config/<KEYCHAIN_SERVICE>/cache.json`,
0600 in a 0700 directory, re-tightened on every save
(`SecureFilePersistence`). It trades Keychain encryption for FileVault at rest
plus file mode, which is the same posture as the `gh`, `az` and `gcloud`
credential files. Consequences:

- **Seeding is automatic and one-way.** When the file backend starts and no
  cache file exists, it reads the Keychain once (this only succeeds from a GUI
  session) and writes the file. A refresh token rotates on use, so after the
  seed only the file is live; the Keychain copy goes stale and should not be
  reintroduced.
- **Keep the directory out of backups and sync.** `~/.config` is not synced by
  default, but Time Machine includes it: `tmutil addexclusion
  ~/.config/<KEYCHAIN_SERVICE>`. Never point `CACHE_FILE` inside a repo or a
  cloud-synced folder.
- **Revocation is at the identity provider.** If the machine is compromised,
  revoke the refresh token in Entra (sign the user out of all sessions);
  deleting the file alone does not invalidate the token.
- **`cli` remains available** for a desktop-only install that wants the
  Keychain. Its failure mode is exactly the one above: correct on the desktop,
  dead everywhere else.

### Keychain access

This section applies to `KEYCHAIN_BACKEND=cli`.

The token cache is read and written by a `/usr/bin/security` subprocess
(`CliKeychainPersistence`), not by msal-extensions' in-process
`KeychainPersistence`. Security.framework's legacy keychain client caches
records per process and refreshes them only from change notifications delivered
over a CFRunLoop, which a stdio server never runs. After a device lock the
cached handle can go stale (`CSSMERR_DL_INVALID_RECORD_UID`) and stay stale for
the life of the process: every later read fails, and no in-process retry helps.
A fresh `security` process has a fresh cache, so the failure cannot persist.
Cost is one process per item per load and roughly four per item per save (about
30 ms each; a real cache spans two items, so about 60 ms to read and 240 ms to
write), and saves happen only on token refresh.

Three consequences worth knowing:

- **Writes go through a staging account.** Updating an item in place raises a
  Keychain ACL prompt that a headless server cannot answer, so a write has to be
  delete-then-add. And while any Keychain dialog is pending, `security` reads and
  deletes still succeed while creates block, which would leave a naive
  delete-then-add unable to rewrite what it just destroyed. `save()` therefore
  writes to the `<account>.staged` set first, then swaps the live set; `load()`
  falls back to the staging copy, so an interrupted write costs nothing.
  `PersistedTokenCache` holds its cross-process lock across the whole sequence.
- **The cache is split across several items.** `security`'s interactive mode
  reads a command into a 4096-byte line buffer, and past that it does not fail:
  it stores a truncated value and parses the tail as a second command. A real
  MSAL cache base64-encodes past that ceiling, so it cannot go in one item.
  Chunk 0 keeps the bare account name and carries a `v2:<n>:` header giving the
  item count; chunk *i* lives at `<account>.<i>`. So
  `security find-generic-password -s graph-m365 -a default-mailbox` shows the
  head of the set, not the whole value.
- **The item's ACL names `/usr/bin/security`.** Reads through the CLI backend
  never prompt; a switch to `KEYCHAIN_BACKEND=msal` afterwards will, because the
  Python interpreter is not on that ACL.

`_token()` also translates a `KeychainError` into a message carrying the
OSStatus and its Security.framework text. msal-extensions raises that exception
with no arguments, so an MCP host would otherwise show an empty error string.

### Portability (non-macOS)

Nothing in the server is macOS-specific except the optional Keychain
backends. The default `file` backend is what runs on every platform, and the
rest (the MSAL auth flow, the Graph v1.0 HTTP calls, the dependency set `msal`,
`msal-extensions`, `httpx`, `mcp`, `html2text`, `pypdf`) is platform-neutral, so the
server should run unchanged on Windows or Linux with a 0600 cache file in the
user's config directory. Only macOS is tested today.

If a port wants OS-level encryption of the cache file rather than relying on
full-disk encryption, msal-extensions' `build_encrypted_persistence()` selects
DPAPI on Windows and libsecret on Linux; wiring it in as a fourth backend is a
small change to `_build_cache()`. Do not make it the default without the
audit-session analysis above: a store that needs a logged-in desktop session to
unlock recreates the headless failure this server was rebuilt to avoid.

## Design notes

`CLAUDE.md` documents the operational guardrails: read-and-stage tool surface, folder-exclusion semantics, no on-disk persistence of bodies or tokens, calendar scoping to the default personal calendar, and the conditional-registration dial that ties tool surface to requested scopes.

## License

MIT. See [`LICENSE`](LICENSE).
