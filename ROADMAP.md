# ROADMAP: robustness work for graph-m365

Written 2026-10-05 after a session in which the server could not create a
draft for about an hour. Nothing here has been executed. Execute in order; each
phase stands alone and can ship on its own. `CLAUDE.md` remains the design doc
and its Guardrails section remains binding except where a phase below says,
in so many words, which line it amends.

## Status (2026-10-05)

All four phases executed the same day, one commit each, in order. Where the
implementation departed from the text below, this block is the record; the
phase text is left as written.

- **Phase 0** shipped as planned: `diagnose` tool, `--diagnose` flag, six
  checks, inline classification in `_token()` and `_req()`. Measured at 0.56 s
  for the full table from a healthy shell. `sandbox-exec` denying the
  `DNSConfiguration` mach service empties `scutil --dns` but does *not* kill
  `getaddrinfo` (libinfo falls back to `resolv.conf`), so that reproduction
  does not work; the classifier is tested by monkeypatching the resolver.
- **Phase 1**: MSAL's `Authority.__init__` fetches the OpenID document eagerly
  at construction, even with `instance_discovery=False`, so the login host
  sees one request per *process*, not zero. Measured over ten warm
  `list_messages` calls: 11 requests and 6.6 s before, 1 request and 0.9 s
  after. A disk-persisted `http_cache` would remove the last one and was
  rejected as a second file in the credential directory.
- **Phase 2**: allowed roots are `$HOME` plus `ATTACH_SAVE_DIR` when set
  (decided: the save dir may sit on a volume outside `$HOME`,
  and files the server saved must be attachable back out). Verified live:
  Graph's attachment `size` runs a few hundred bytes over the file (MIME
  headers), and `DELETE` on an unsent draft lands in Recoverable
  Items/Deletions, not Deleted Items. Still recoverable, still soft.
- **Phase 3**: login start (decided). Sessions are served concurrently rather
  than strictly one at a time; tool bodies are synchronous, so they queue at
  the Graph call anyway. The daemon refuses to start on a non-file backend
  rather than merely documenting the rule. The acceptance harness is a
  `sandbox-exec` profile that denies `network*` except one Unix socket, which
  kills `getaddrinfo` exactly as in the incident; the shim completes a session
  under it. The plist is a template with placeholders, rendered by
  `launchd/install.fish` from the Desktop config, since the ids belong in host
  config and not in the repo. Installation was deliberately left to the user.

## What happened, as data

The session was a Claude Code session on this Mac (macOS 27.0). The first
mailbox search of the day went through this server and worked. Later the
harness moved the session to its background host; the process chain afterward
was `launchd -> claude daemon run --origin transient -> claude bg-pty-host ->
claude bg-spare -> /bin/zsh`. From then on, in every process spawned in that
tree:

- `scutil --dns` printed `No DNS configuration available`.
- `dscacheutil -q host -a name login.microsoftonline.com` returned nothing, and
  Python's `socket.gethostbyname` raised `[Errno 8] nodename nor servname
  provided, or not known`.
- `/etc/resolv.conf` listed valid resolvers, `ping 1.1.1.1` answered in 19 ms,
  and `dig login.microsoftonline.com` (which reads `resolv.conf` itself and
  speaks UDP directly) resolved the name at once.
- `open` failed with `kLSExecutableIncorrectFormat` and AppleScript could not
  get application "Mail" or "Microsoft Outlook", so Launch Services and Apple
  events were also unavailable in that context.

So the machine and the network were fine. Only the system resolver API was
unreachable from that process tree, and this server, spawned fresh into it,
failed every call with:

    HTTPSConnectionPool(host='login.microsoftonline.com', port=443): Max
    retries exceeded with url: /<tenant>/v2.0/.well-known/openid-configuration
    (Caused by NameResolutionError(...))

Three things about that error line matter for the design:

1. It is a `requests`/`urllib3` message, not an `httpx` one, so it came from
   inside MSAL, before any Graph call was attempted.
2. It names the OpenID discovery document. The server was contacting the login
   host on every call, even with a warm token cache. `_app()` builds a new
   `PublicClientApplication` on each `_token()` call, and MSAL does instance
   and tenant discovery per application object.
3. It took six probing shell calls to establish that the network was healthy
   and only the resolver API was dead. The server reported a transport
   exception and nothing else.

The Playwright and OmniFocus servers timed out at the same moment, so this is
a property of the host context, not of this code. A bug report was filed with
Claude Code. The phases below assume the host will do this again.

## Phase 0: make failures diagnose themselves

Smallest change, largest payoff per line. Do this first.

**Change.** Add one tool, `diagnose`, and one CLI flag, `--diagnose`, that run
the same checks and return one line per check:

| check | how | healthy output |
|---|---|---|
| resolver API | `socket.getaddrinfo("login.microsoftonline.com", 443)` | address list |
| direct DNS | UDP query to the first `resolv.conf` server, then to 1.1.1.1, via a 40-line stdlib resolver (no new dependency) | address list, which server answered |
| TCP reach | `socket.create_connection((ip, 443), timeout=3)` to the address from the previous check | connect time |
| token cache | file present, mode 0600, parses, has an account, access-token expiry relative to now | expiry in minutes, or `no account` |
| clock | compare local time to the `Date` header of a HEAD to graph.microsoft.com by IP, if TCP reach passed | skew in seconds |
| host context | parent chain from `ps -o ppid=`, up to five levels, plus whether `scutil --dns` has output | one line |

The tool takes no arguments, needs no scope, and never touches the mailbox.
Register it unconditionally. This adds a tool beyond the capability table in
`CLAUDE.md`; amend the "Tool surface freeze" line to list `diagnose` as the
second always-on tool next to `excluded_recent_count`, and add it to the table
with scope "none".

**Also.** Wrap the two network boundaries so the error a caller sees says
which layer failed:

- In `_token()`, catch `requests.exceptions.ConnectionError` (MSAL uses
  `requests`) and re-raise `RuntimeError` with a one-line classification. The
  classification comes from running the first three checks above inline:
  "resolver API failed, direct DNS works: the host process cannot use the
  system resolver; run diagnose" versus "no route to login.microsoftonline.com".
- In `_req()`, catch `httpx.ConnectError` the same way.

**Tests.** A unit test that monkeypatches `socket.getaddrinfo` to raise
`gaierror` and asserts the classification text; another that stubs the UDP
resolver. Keep them under `tests/` with PEP 723 headers like the rest.

**Acceptance.** From a shell where `scutil --dns` is empty (reproduce by
running under the Claude Code background host, or by a `sandbox-exec` profile
that denies `mach-lookup` to `com.apple.SystemConfiguration.DNSConfiguration`),
one `diagnose` call explains the failure in under a second.

**Effort.** One short session.

## Phase 1: stop talking to the login host on every call

**Change.** Three edits in `server.py`:

1. Make the MSAL application a module-level singleton built on first use, not
   per call. `PersistedTokenCache` already handles reload across processes
   through the signal file, so sharing one app object inside a process loses
   nothing.
2. Construct it with `instance_discovery=False`. The authority is a known
   single-tenant `login.microsoftonline.com` URL, so instance discovery adds
   nothing. MSAL still fetches the tenant's OpenID document once per app
   object, lazily, on the first token request; with a singleton that is once
   per process instead of once per call.
3. Replace `httpx.request(...)` in `_req()` with a module-level `httpx.Client`
   so connections to graph.microsoft.com are reused. Keep the 30 s timeout and
   the retry loop as they are.

With these, a warm cache (access token unexpired, roughly an hour) makes no
request to the login host at all; `acquire_token_silent` returns from the
cache. Only Graph itself is contacted. This would not have saved the session
above, since graph.microsoft.com also needs resolving, but it removes one
dependency per call, cuts latency, and means a login-host outage no longer
takes down reads.

**Check before and after.** Count requests to `login.microsoftonline.com`
during ten consecutive `list_messages` calls with a warm cache. Before: ten or
more. After: zero. Measure by pointing `HTTPS_PROXY` at a local mitmproxy for
the test, or by temporarily logging the MSAL `http_client` calls; remove the
logging before committing (no token material in logs, per Guardrails).

**Risk.** `msal` versions before 1.21 do not accept `instance_discovery`; the
PEP 723 header pins nothing, so pin `msal>=1.21`. The `PersistedTokenCache`
signal-file reload must still be exercised by the integration test after the
singleton change.

**Effort.** Under an hour, plus the measurement.

## Phase 2: attachments on `create_draft`

This is the feature gap that would have changed the outcome: even with a
working resolver, the draft could not have carried the two files.

**Change.** Add `attachments: list[str] | None = None` to `create_draft`
(and so to `send_message`, which calls it). Each entry is a local file path.
Implementation:

1. Create the draft as now and capture its id.
2. For each path, under 3 MB: `POST /me/messages/{id}/attachments` with
   `{"@odata.type": "#microsoft.graph.fileAttachment", "name": basename,
   "contentType": guessed, "contentBytes": base64}`. Over 3 MB and up to
   150 MB: `POST /me/messages/{id}/attachments/createUploadSession`, then PUT
   the bytes in 4 MB ranges. Beyond 150 MB, refuse.
3. If any attachment fails, delete the draft (`DELETE /me/messages/{id}`,
   soft delete, so it is recoverable) and raise, so a half-built draft never
   sits in Drafts looking finished.
4. Return the draft id, webLink, and a list of `{name, bytes, sha256}` per
   attachment, never the bytes.

**Path rules**, mirroring the `save_attachment` discipline in the other
direction: the path must resolve (after `realpath`) to a regular file under
`$HOME`, symlinks that escape `$HOME` are refused, and there is a per-file
ceiling. No new environment gate is needed, since the user names each file
explicitly in the call and the content goes only to the user's own Drafts.

**Guardrails.** This does not add a scope (`Mail.ReadWrite` already covers
attachments on the user's own messages) and does not add a tool, so the
capability table is unchanged. Add one line under "Attachments are extracted
in memory" noting that `create_draft` is the one path that reads a local file
and uploads it, under the path rules above. Update the README security model
in the same commit.

**Tests.** A test that builds the JSON for a small file and asserts the shape;
a test that the escape-from-`$HOME` symlink is refused; a live test against a
throwaway draft that attaches a 10 KB and a 5 MB file, then deletes the draft.

**Effort.** One session including the live test.

## Phase 3: a persistent daemon, with stdio kept at the host boundary

This is the change that addresses the root cause: the server inherits the
context of whatever spawns it. Running it once, in the login session, and
having each host talk to that one process removes the dependency on the
host's context.

**Why not HTTP.** The Guardrails say "Stdio only. No HTTP/network transport,
no multi-user assumption," and that line is doing real work: a TCP listener,
even on localhost, is reachable by any process on the machine and invites the
multi-user question. Keep it. The design below never opens a port.

**Design.**

- `server.py --daemon` runs the FastMCP server as today but listens on a Unix
  domain socket at `~/.config/<KEYCHAIN_SERVICE>/mcp.sock`, created with mode
  0600 inside the existing 0700 directory. Connections are accepted only from
  the same uid (check `SO_PEERCRED` / `LOCAL_PEERCRED` on macOS). One
  connection at a time is enough; two Claude sessions can queue.
- `server.py` with no flags, which is what both hosts invoke, becomes a thin
  stdio shim: if the socket exists and answers, it bridges stdin/stdout to the
  socket byte for byte and does nothing else. The shim imports nothing from
  MSAL or httpx, touches no credential, and needs no DNS, no Launch Services
  and no Keychain, which is exactly the set of things the background host
  could not provide. If the socket is absent or refuses, the shim falls back
  to running the server in-process as today, so nothing regresses when the
  daemon is not installed.
- A launchd user agent (`org.wardlaw.graph-m365.plist`, `RunAtLoad`,
  `KeepAlive`) starts the daemon at login in the GUI session. Follow the
  pattern of the finprof snapshot agent, including `ProgramArguments` with the
  absolute `uv` path and the environment block carrying `GRAPH_CLIENT_ID`,
  `GRAPH_TENANT_ID` and the capability toggles. Log to
  `~/Library/Logs/graph-m365.log`, metadata only.
- Capability flags belong to the daemon, not to the shim, since the daemon
  decides which tools exist. The host `env` blocks in `CLAUDE.md` Hosts then
  carry only `GRAPH_CLIENT_ID`/`GRAPH_TENANT_ID` for the fallback case.

**Transport detail.** The MCP Python SDK's stdio transport reads newline
delimited JSON-RPC from a stream pair; the daemon side wraps the accepted
socket in the same stream types. If the SDK version in use (`mcp<2`) makes
that awkward, the fallback is to run `mcp.run()` in the daemon against a
`socketpair` and have the accept loop copy bytes; measure before choosing.

**What the long-lived process changes.** `CLAUDE.md` records that long-lived
server processes died in August 2026 on the Keychain backend after a device
lock, which is why Keychain access went out of process. The daemon is
long-lived by design, so: it must run on the file backend (the default) and
the plist must not set `KEYCHAIN_BACKEND=cli`. Document this next to the
plist. The `PersistedTokenCache` lock already serialises refresh-token
rotation, and with the daemon the fallback path should refuse to start if the
daemon's lock file is held, so two processes never rotate the same refresh
token.

**Guardrails to amend, knowingly.** Replace "Stdio only. No HTTP/network
transport, no multi-user assumption." with: "Stdio at the host boundary. The
daemon listens only on a 0600 Unix socket inside the credential directory,
accepts connections only from its own uid, and never opens a TCP port. No
multi-user assumption." Add the launchd plist and the shim to the Files
section.

**Tests.** Shim-to-daemon round trip over the socket with a fake FastMCP
handler; peer-uid rejection; fallback when the socket is absent; and the
acceptance test for the whole roadmap: start the daemon, then run the shim
from a process tree where `scutil --dns` is empty, and create a draft.

**Effort.** Two sessions: one for the daemon and shim, one for launchd,
documentation, and the acceptance test.

## Not doing

- **A DNS-over-HTTPS fallback inside the server.** It would have worked in
  the session above, since connections to literal IPs succeeded, and it is
  about thirty lines around `httpx`'s transport plus a resolver for MSAL's
  `requests` session. Rejected because it hides a broken host instead of
  reporting it, it adds a second resolver to reason about in every future
  incident, and Phase 3 removes the need.
- **Moving the token cache or changing the credential layer.** Nothing in the
  incident touched credentials. The file backend and the `security` discipline
  stay as they are.
- **Mail.Send.** Still dark. Phase 2 makes `send_message` attachment-capable
  only because it calls `create_draft`; it stays unregistered unless
  `MAIL_SEND=1`.

## Order and dependencies

Phase 0, then 1, then 2, then 3. None depends on another, but 0 first means
every later phase can be verified with `diagnose`, and 3 last means the daemon
ships with attachments and the singleton already in it.

## Open questions

1. Should the daemon start at login for everyone, or only when a Claude host
   first needs it (socket activation via launchd `Sockets`)? Login start is
   simpler and matches the snapshot agent; socket activation avoids a resident
   process. Default here: login start.
2. Phase 2 reads files under `$HOME` only. If attachments will routinely come
   from the Desktop or a local scratch directory, that is enough;
   if from OneDrive under `~/Library/CloudStorage`, confirm `realpath` keeps
   those under `$HOME` (it should).
3. The cloud Microsoft 365 connector is read-only for mail because the
   tenant has not granted it `Mail.ReadWrite`. That is outside this repo. It
   is the reason this server exists, and nothing here changes it.
