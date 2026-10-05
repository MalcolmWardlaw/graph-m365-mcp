# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""The daemon and the stdio shim (ROADMAP.md Phase 3).

Runs a real `server.py --daemon` under a throwaway KEYCHAIN_SERVICE, so the
socket and lock live in ~/.config/graph-m365-daemon-test/ and the real cache is
never touched, then drives the shim as the hosts do: JSON-RPC on stdin, replies
on stdout. No Graph, no auth; initialize and tools/list need neither.

The acceptance test for the whole roadmap runs the shim under a sandbox-exec
profile that denies every network capability except the one Unix socket. In
that sandbox getaddrinfo fails exactly as it did in the 2026-10-05 incident, and
the shim still completes a session, with the daemon's diagnose reporting a
healthy resolver from its own (GUI) context.

Run: uv run --script tests/test_daemon.py
"""
import os, sys, json, time, select, shutil, signal, socket, struct, fcntl, subprocess

SVC = "graph-m365-daemon-test"
assert SVC != "graph-m365"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")
CFG = os.path.expanduser(f"~/.config/{SVC}")
SOCK, LOCK = os.path.join(CFG, "mcp.sock"), os.path.join(CFG, "mcp.sock.lock")
PY = sys.executable                       # the uv interpreter that already has the deps

ENV = {k: v for k, v in os.environ.items() if k not in ("ATTACH_SAVE_DIR", "EXCLUDE_FOLDERS", "KEYCHAIN_BACKEND")}
ENV.update(GRAPH_CLIENT_ID="test", GRAPH_TENANT_ID="test", KEYCHAIN_SERVICE=SVC)

fails = 0
def check(name, cond, note=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + (f"  [{note}]" if note and not cond else "")); fails += 0 if cond else 1


def rpc(method, id=None, params=None):
    m = {"jsonrpc": "2.0", "method": method}
    if id is not None: m["id"] = id
    if params is not None: m["params"] = params
    return (json.dumps(m) + "\n").encode()

INIT = rpc("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "test_daemon", "version": "0"}})
INITIALIZED = rpc("notifications/initialized")
TOOLS = rpc("tools/list", 2, {})


class Session:
    """A host: one shim process, stdin kept open until every reply is in."""
    def __init__(self, argv_prefix=(), env=ENV):
        self.p = subprocess.Popen([*argv_prefix, PY, SERVER], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def send(self, *msgs):
        for m in msgs:
            self.p.stdin.write(m)
        self.p.stdin.flush()

    def reply(self, want_id, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.p.stdout], [], [], max(0.0, deadline - time.monotonic()))
            if not r:
                break
            line = self.p.stdout.readline()
            if not line:
                break
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("id") == want_id:
                return d
        return None

    def close(self, timeout=10.0):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill(); self.p.wait()
        return self.p.returncode, self.p.stderr.read().decode(errors="replace")


def wait_for(pred, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


# --- unit: peer credentials and the lock probe --------------------------------
sys.path.insert(0, ROOT)
os.environ.update(ENV)
import server  # noqa: E402

xucred = struct.pack("@II", 0, 501) + b"\0" * 68
check("xucred parser reads the uid", server._parse_xucred(xucred) == 501)
try:
    server._parse_xucred(struct.pack("@II", 7, 501) + b"\0" * 68); check("xucred parser rejects a foreign version", False)
except ValueError:
    check("xucred parser rejects a foreign version", True)
check("own uid allowed", server._peer_allowed(os.getuid()))
check("other uid refused", not server._peer_allowed(os.getuid() + 1))
check("unknown uid refused", not server._peer_allowed(None))
check("live peer uid on a socketpair is ours",
      (lambda a, b: server._peer_uid(a) == os.getuid())(*socket.socketpair(socket.AF_UNIX)))
check("socket path is inside the credential directory", server.SOCK_PATH == SOCK and server.SOCK_LOCK == LOCK)

shutil.rmtree(CFG, ignore_errors=True)
check("lock probe: no file means no daemon", not server._daemon_lock_held(LOCK))

# --- daemon refuses a Keychain backend ----------------------------------------
r = subprocess.run([PY, SERVER, "--daemon"], env={**ENV, "KEYCHAIN_BACKEND": "cli"},
                   capture_output=True, text=True, timeout=60)
check("daemon refuses KEYCHAIN_BACKEND=cli", r.returncode == 2 and "requires KEYCHAIN_BACKEND=file" in r.stderr, r.stderr[-200:])

# --- fallback with no daemon ---------------------------------------------------
s = Session()
s.send(INIT)
d = s.reply(1)
check("no daemon: shim falls back to an in-process server",
      bool(d) and d["result"]["serverInfo"]["name"] == "graph-m365")
s.send(INITIALIZED, TOOLS)
d = s.reply(2)
check("no daemon: in-process server lists tools", bool(d) and any(t["name"] == "diagnose" for t in d["result"]["tools"]))
rc, err = s.close()
check("no daemon: shim exits 0 on stdin EOF", rc == 0, f"rc={rc} {err[-200:]}")

# --- the daemon ----------------------------------------------------------------
daemon_log = open(os.path.join(ROOT, "tests", ".daemon-test.log"), "w+")
daemon = subprocess.Popen([PY, SERVER, "--daemon"], env=ENV, stdin=subprocess.DEVNULL,
                          stdout=daemon_log, stderr=subprocess.STDOUT)
try:
    check("daemon creates the socket", wait_for(lambda: os.path.exists(SOCK)))
    check("socket mode is 0600", os.path.exists(SOCK) and (os.stat(SOCK).st_mode & 0o777) == 0o600)
    check("lock probe: daemon holds the lock", server._daemon_lock_held(LOCK))

    # Second daemon refuses.
    r = subprocess.run([PY, SERVER, "--daemon"], env=ENV, capture_output=True, text=True, timeout=60)
    check("second daemon exits on the held lock", r.returncode == 1 and "another daemon holds" in r.stderr, r.stderr[-200:])
    check("first daemon survived the attempt", daemon.poll() is None and os.path.exists(SOCK))

    # Shim round trip.
    s = Session()
    s.send(INIT)
    d = s.reply(1)
    check("shim: initialize answered by the daemon", bool(d) and d["result"]["serverInfo"]["name"] == "graph-m365")
    s.send(INITIALIZED, TOOLS)
    d = s.reply(2)
    names = sorted(t["name"] for t in d["result"]["tools"]) if d else []
    check("shim: tools/list carries the full surface", "diagnose" in names and "list_messages" in names and "create_draft" in names)
    rc, err = s.close()
    check("shim: exits 0 when the host closes stdin", rc == 0, f"rc={rc} {err[-200:]}")
    daemon_log.flush()
    check("daemon logged the session", "session open" in open(daemon_log.name).read())

    # The shim imports nothing heavy. -X importtime records every import.
    p = subprocess.Popen([PY, "-X", "importtime", SERVER], env=ENV, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(INIT); p.stdin.flush()
    sel = select.select([p.stdout], [], [], 20)
    first = p.stdout.readline() if sel[0] else b""
    p.stdin.close(); p.wait(timeout=10)
    imported = {ln.rsplit("|", 1)[-1].strip() for ln in p.stderr.read().decode(errors="replace").splitlines() if ln.startswith("import time:")}
    heavy = {m for m in imported if m.split(".")[0] in ("msal", "httpx", "requests", "mcp", "html2text", "pypdf", "msal_extensions", "anyio")}
    check("shim imports no MSAL, httpx or MCP module", bool(first) and not heavy, ", ".join(sorted(heavy))[:200])

    # Two hosts at once.
    a, b = Session(), Session()
    a.send(INIT); b.send(INIT)
    da, db = a.reply(1), b.reply(1)
    check("two concurrent sessions both initialize", bool(da) and bool(db))
    a.send(INITIALIZED, TOOLS); b.send(INITIALIZED, TOOLS)
    check("two concurrent sessions both list tools", bool(a.reply(2)) and bool(b.reply(2)))
    a.close(); b.close()

    # Acceptance: a host context with no network and no resolver.
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        profile = ('(version 1)(allow default)(deny network*)'
                   f'(allow network-outbound (remote unix-socket (path-literal "{os.path.realpath(SOCK)}")))')
        probe = subprocess.run(["sandbox-exec", "-p", profile, PY, "-c",
                                "import socket; socket.getaddrinfo('login.microsoftonline.com', 443)"],
                               capture_output=True, text=True)
        check("sandbox: resolver API is dead inside it", probe.returncode != 0 and "gaierror" in probe.stderr)
        s = Session(argv_prefix=("sandbox-exec", "-p", profile))
        s.send(INIT)
        d = s.reply(1)
        check("sandbox: shim completes initialize with no DNS", bool(d) and d["result"]["serverInfo"]["name"] == "graph-m365")
        s.send(INITIALIZED, rpc("tools/call", 3, {"name": "diagnose", "arguments": {}}))
        d = s.reply(3, timeout=40)
        rows = []
        if d and "result" in d:
            try:
                content = d["result"]["content"]
                rows = json.loads(content[0]["text"]) if len(content) == 1 else [json.loads(c["text"]) for c in content]
                if isinstance(rows, dict): rows = [rows]
            except Exception:
                rows = []
        by = {r["check"]: r for r in rows if isinstance(r, dict) and "check" in r}
        check("sandbox: diagnose ran in the daemon's context (resolver API healthy there)",
              by.get("resolver API", {}).get("ok") is True, json.dumps(by.get("resolver API"))[:200])
        check("sandbox: host context row reports the daemon's parent chain",
              "host context" in by and "python" in str(by["host context"].get("detail", "")).lower())
        rc, err = s.close()
        check("sandbox: shim exits 0", rc == 0, f"rc={rc} {err[-200:]}")
    else:
        print("skip sandbox acceptance (needs macOS sandbox-exec)")

    # Shutdown: SIGTERM removes the socket and releases the lock.
    daemon.send_signal(signal.SIGTERM)
    try:
        daemon.wait(timeout=15)
        check("daemon exits on SIGTERM", daemon.returncode == 0, f"rc={daemon.returncode}")
    except subprocess.TimeoutExpired:
        daemon.kill(); check("daemon exits on SIGTERM", False, "timeout")
    check("socket removed on shutdown", not os.path.exists(SOCK))
    check("lock released on shutdown", not server._daemon_lock_held(LOCK))
    daemon_log.flush()
    log = open(daemon_log.name).read()
    check("log records the shutdown", "received SIGTERM" in log and "stopped" in log)
    check("log holds no request bodies or tokens", "Bearer" not in log and '"jsonrpc"' not in log)

    # Lock held with no socket: the shim must refuse to fall back.
    holder = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        s = Session()
        s.send(INIT)
        rc, err = s.close(timeout=20)
        check("held lock, no socket: shim refuses a second in-process server",
              rc == 1 and "refusing to start a second server" in err, f"rc={rc} {err[-200:]}")
    finally:
        os.close(holder)
finally:
    if daemon.poll() is None:
        daemon.kill(); daemon.wait()
    daemon_log.close()
    os.remove(daemon_log.name)
    shutil.rmtree(CFG, ignore_errors=True)

print(f"FAILURES: {fails}"); sys.exit(1 if fails else 0)
