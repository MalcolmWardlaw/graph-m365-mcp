# graph-m365-mcp

> **Part of [email-cleaner-for-llms](https://github.com/MalcolmWardlaw/email-cleaner-for-llms)** — a small family of sanitizing MCP proxies that sit between a mail API and the LLM, flattening HTML and stripping cruft so a model can batch-classify and act on hundreds of messages without blowing its context window.
>
> **Backends:** **graph-m365-mcp** (Microsoft 365 / Graph) · [fastmail-clean-mcp](https://github.com/MalcolmWardlaw/fastmail-clean-mcp) (Fastmail / JMAP) · IMAP (planned)

Local stdio MCP server exposing Microsoft 365 mail and personal-calendar tools to a Claude Desktop or Claude Code host, via the Microsoft Graph v1.0 API. Read-and-stage by design (drafts, not send; soft-delete, not hard-delete) with a configurable folder-exclusion gate on the read paths.

> Unofficial and independent. Not affiliated with, endorsed by, or sponsored by Microsoft. "Microsoft 365", "Microsoft Graph", and "Outlook" are trademarks of Microsoft Corporation, used here only to describe what the server talks to.

## Prerequisites

- macOS (uses the login Keychain for token persistence)
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
cd ~/Documents/projects/graph-m365-mcp
GRAPH_CLIENT_ID=<your-app-id> GRAPH_TENANT_ID=<your-tenant-id> uv run server.py --auth
```

A browser opens for the OAuth flow. On success, MSAL writes a refresh token to the macOS login Keychain at `(service=$KEYCHAIN_SERVICE, account=default-mailbox)` — `service=graph-m365` by default. The non-secret signal file `~/.config/$KEYCHAIN_SERVICE/cache.signal` is also created.

After this, normal runs use silent token acquisition. Re-run `--auth` only if silent acquisition fails or you enable a capability whose scope MSAL has not seen before.

## Configuration

All configuration is via environment variables. The server itself ships with no host-specific defaults; the host supplies them.

| Variable           | Default     | Effect                                                                  |
|--------------------|-------------|-------------------------------------------------------------------------|
| `GRAPH_CLIENT_ID`  | (required)  | AAD application id                                                      |
| `GRAPH_TENANT_ID`  | (required)  | AAD tenant id (single-tenant authority)                                 |
| `MAIL_READ`        | `1`         | Register `list_messages`, `search_messages`, `get_message`, `triage_messages`, `list_mail_folders`; adds `Mail.Read` |
| `MAIL_WRITE`       | `1`         | Register `archive_message`, `move_messages`, `delete_messages`, `mark_messages`; adds `Mail.ReadWrite` |
| `MAIL_DRAFT`       | `1`         | Register `create_draft`; adds `Mail.ReadWrite`                          |
| `MAIL_SEND`        | `0`         | Register `send_message`; adds `Mail.Send` (off by design)               |
| `CAL_READ`         | `1`         | Register `list_events`, `get_event`; adds `Calendars.Read`              |
| `CAL_WRITE`        | `1`         | Register `create/update/delete_event`; adds `Calendars.ReadWrite`       |
| `EXCLUDE_FOLDERS`  | (empty)     | Comma-separated top-level folder display names hard-blocked from list/search/get. When non-empty, registers `excluded_recent_count` for metadata-only counts. |
| `KEYCHAIN_SERVICE` | `graph-m365`| macOS Keychain service name for the refresh token (and the `~/.config/<name>/` signal path). Override only to reuse a token entry created under a different name. |
| `CAL_TIMEZONE`     | `Eastern Standard Time`| Windows/Outlook timezone name applied to calendar reads and writes (e.g. `Pacific Standard Time`, `UTC`). An explicit ISO offset in a passed datetime still wins. |

When a capability is off, neither its tools nor its scope are requested. Requested scopes are the minimum union over enabled capabilities; the AAD-consented grant is only the ceiling.

## Claude Desktop registration

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "graph-m365": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "/Users/<you>/Documents/projects/graph-m365-mcp/server.py"],
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
claude mcp add graph-m365 -- uv run /Users/<you>/Documents/projects/graph-m365-mcp/server.py
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
- **No on-disk persistence of content or credentials.** Message bodies are
  returned to the client — that is the point — but never written to disk. The
  refresh token lives only in the macOS login Keychain; nothing else but a
  non-secret cache-reload signal touches disk.
- **Stdio only.** No network transport, no multi-user assumption.

### Portability (non-macOS)

The server is macOS-only in exactly one respect: where it stores the refresh
token. `_build_cache()` in `server.py` hardcodes msal-extensions'
`KeychainPersistence`. Everything else — the MSAL auth flow, the Graph v1.0
HTTP calls, the dependency set (`msal`, `msal-extensions`, `httpx`, `mcp`,
`html2text`) — is platform-neutral.

Porting to Windows or Linux is a small, well-isolated change with one hard
constraint: the token must stay **encrypted at rest** (this server never writes
a plaintext token to disk — see Security model). The intended path is to replace
the hardcoded `KeychainPersistence` with msal-extensions' OS-dispatching
`build_encrypted_persistence()`, which selects:

- **Windows** — DPAPI-encrypted file persistence
- **Linux** — `LibsecretPersistence` (requires libsecret / a Secret Service provider)
- **macOS** — Keychain (current behavior)

The `FilePersistence` fallback already in `_build_cache()` is **dev-only**: it
writes the token unencrypted and must not be the cross-platform answer. PRs that
preserve the encrypted-at-rest invariant are welcome; only macOS is tested today.

## Design notes

`CLAUDE.md` documents the operational guardrails: read-and-stage tool surface, folder-exclusion semantics, no on-disk persistence of bodies or tokens, calendar scoping to the default personal calendar, and the conditional-registration dial that ties tool surface to requested scopes.

## License

MIT. See [`LICENSE`](LICENSE).
