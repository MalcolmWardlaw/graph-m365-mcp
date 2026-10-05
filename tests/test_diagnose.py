# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""The diagnose tool and the network-failure classifier behind _token/_req.

No Graph, no auth. The resolver API is monkeypatched to fail the way it did in
the 2026-10-05 incident, the UDP resolver is stubbed, and the wire parser is
fed a hand-built response. One live block queries real DNS and is skipped when
the network is down.

Run: uv run --script tests/test_diagnose.py
"""
import os, sys, socket, struct

os.environ.setdefault("GRAPH_CLIENT_ID", "test"); os.environ.setdefault("GRAPH_TENANT_ID", "test")
os.environ["KEYCHAIN_SERVICE"] = "graph-m365-diag-test"    # keep the real cache untouched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name); fails += 0 if cond else 1

# Registered unconditionally, independent of every capability dial.
check("diagnose registered", "diagnose" in server.mcp._tool_manager._tools)

# --- wire format -------------------------------------------------------------
q = server._dns_build_query("login.microsoftonline.com", 0xBEEF)
check("query header: id, RD, one question", q[:12] == struct.pack("!HHHHHH", 0xBEEF, 0x0100, 1, 0, 0, 0))
check("query ends with QTYPE A / QCLASS IN", q.endswith(b"\x00\x00\x01\x00\x01"))

def name_bytes(n):
    return b"".join(bytes([len(l)]) + l.encode() for l in n.split(".")) + b"\x00"

# A recursive answer: CNAME then two A records, the second with a compressed
# owner name (pointer to offset 12, the question name).
resp = struct.pack("!HHHHHH", 0xBEEF, 0x8180, 1, 3, 0, 0)
resp += name_bytes("login.microsoftonline.com") + struct.pack("!HH", 1, 1)
resp += b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, len(name_bytes("login.mso.msidentity.com"))) + name_bytes("login.mso.msidentity.com")
resp += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([40, 126, 29, 13])
resp += name_bytes("login.mso.msidentity.com") + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([20, 190, 160, 2])
check("parser collects A records across CNAME and pointers",
      server._dns_parse_a(resp, 0xBEEF) == ["40.126.29.13", "20.190.160.2"])

def raises(fn, exc=Exception):
    try:
        fn(); return False
    except exc:
        return True

check("parser rejects id mismatch", raises(lambda: server._dns_parse_a(resp, 0x0001), ValueError))
nx = struct.pack("!HHHHHH", 0xBEEF, 0x8183, 1, 0, 0, 0) + name_bytes("x.invalid") + struct.pack("!HH", 1, 1)
check("parser rejects NXDOMAIN", raises(lambda: server._dns_parse_a(nx, 0xBEEF), ValueError))
check("parser rejects short datagram", raises(lambda: server._dns_parse_a(b"\x00" * 5, 0), ValueError))

# --- classifier ----------------------------------------------------------------
real_gai = socket.getaddrinfo
real_direct = server._direct_dns

def dead_resolver(*a, **k):
    raise socket.gaierror(8, "nodename nor servname provided, or not known")

try:
    # The incident: resolver API dead, direct DNS fine.
    socket.getaddrinfo = dead_resolver
    server._direct_dns = lambda name: (["40.126.29.13"], "1.1.1.1")
    msg = server._classify_network("login.microsoftonline.com")
    check("incident shape names the resolver API", "resolver API failed" in msg)
    check("incident shape says direct DNS works", "direct DNS works" in msg and "40.126.29.13" in msg)
    check("incident shape points at diagnose", "diagnose" in msg)

    # Everything dead.
    def no_dns(name):
        raise RuntimeError("192.168.1.1: timeout; 1.1.1.1: timeout")
    server._direct_dns = no_dns
    msg = server._classify_network("login.microsoftonline.com")
    check("total failure says the network is down", "network is down" in msg)

    # Resolver fine, port closed: point at a known-unroutable address.
    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
    real_connect = socket.create_connection
    def refuse(addr, timeout=None, **k):
        raise OSError(61, "Connection refused")
    socket.create_connection = refuse
    try:
        msg = server._classify_network("login.microsoftonline.com")
        check("resolves-but-unreachable names port 443", "port 443 is unreachable" in msg)
    finally:
        socket.create_connection = real_connect
finally:
    socket.getaddrinfo = real_gai
    server._direct_dns = real_direct

# --- the full table, offline-safe ---------------------------------------------
rows = server._diagnose()
check("six checks in order",
      [r["check"] for r in rows] == ["resolver API", "direct DNS", "TCP reach", "token cache", "clock", "host context"])
check("every row carries ok/detail/ms", all({"ok", "detail", "ms"} <= set(r) for r in rows))
cache_row = next(r for r in rows if r["check"] == "token cache")
check("token cache for the test service reports absence, not contents",
      not cache_row["ok"] and "FileNotFoundError" in cache_row["detail"])
host_row = next(r for r in rows if r["check"] == "host context")
check("host context names this interpreter", host_row["ok"] and "python" in host_row["detail"].lower())
check("host context reports scutil state", "scutil --dns" in host_row["detail"])

# --- live DNS, skipped offline ------------------------------------------------
try:
    real_gai("login.microsoftonline.com", 443)
    online = True
except OSError:
    online = False
if online:
    addrs, srv = server._direct_dns("login.microsoftonline.com")
    check(f"live direct DNS answers (via {srv})", bool(addrs) and all(a.count(".") == 3 for a in addrs))
else:
    print("skip live direct DNS (offline)")

print(f"FAILURES: {fails}"); sys.exit(1 if fails else 0)
