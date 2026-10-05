# /// script
# requires-python = ">=3.11"
# dependencies = ["msal-extensions"]
# ///
"""End-to-end round trip of CliKeychainPersistence against the real login
Keychain, under a throwaway service name that is deleted at the end."""
import base64, io, json, os, re, subprocess, sys, tempfile, time

SERVICE = "graph-m365-probe"          # NOT the real service; deleted below
assert SERVICE != "graph-m365"

from msal_extensions.persistence import (
    BasePersistence, FilePersistence, PersistenceNotFound, PersistenceDecryptionError)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
ns = dict(os=os, re=re, sys=sys, base64=base64, subprocess=subprocess,
          BasePersistence=BasePersistence, FilePersistence=FilePersistence,
          PersistenceNotFound=PersistenceNotFound,
          PersistenceDecryptionError=PersistenceDecryptionError,
          KEYCHAIN_ACCOUNT="default-mailbox")
exec(SRC[SRC.index("def _sec_name("):SRC.index("def _build_cache(")], ns)
P = ns["CliKeychainPersistence"]

tmp = tempfile.mkdtemp(prefix="cli-keychain-real-")
p = P(os.path.join(tmp, "cache.signal"), service_name=SERVICE)
# Cache-shaped and cache-sized: incompressible token blobs, hostile characters,
# and past one item's budget, which is what the first cut of this test missed.
PAYLOAD = json.dumps({
    "AccessToken": {"a b": 'quoted " and \n newline'}, "u": "café ✓",
    "RefreshToken": {f"k{i}": {"secret": base64.b64encode(
        bytes((i * 7 + j) % 256 for j in range(900))).decode()} for i in range(4)},
})
rc = 0
try:
    try:
        p.load()
        print("FAIL: expected PersistenceNotFound"); rc = 1
    except PersistenceNotFound:
        print("ok   load before save -> PersistenceNotFound")

    p.save(PAYLOAD)
    print("ok   save (created the item)")

    back = p.load()
    print(f"{'ok  ' if back == PAYLOAD else 'FAIL'} load returns the exact payload")
    rc |= (back != PAYLOAD)

    def item(acct):
        return subprocess.run(["/usr/bin/security", "find-generic-password",
                               "-s", SERVICE, "-a", acct, "-w"],
                              capture_output=True, text=True).stdout.strip()

    head = item("default-mailbox")
    m = re.match(r"^v2:(\d+):", head)
    n = int(m.group(1)) if m else 0
    parts = [head[m.end():]] + [item(f"default-mailbox.{i}") for i in range(1, n)]
    enc = n > 1 and base64.b64decode("".join(parts), validate=True).decode() == PAYLOAD
    print(f"{'ok  ' if enc else 'FAIL'} stored at rest as base64 across {n} items "
          f"(head {head[:16]}...)")
    rc |= (not enc)

    longest = max(len(p_) for p_ in parts)
    fits = longest + 128 < 4096
    print(f"{'ok  ' if fits else 'FAIL'} largest item {longest} chars, clear of "
          "security's 4096-byte command line")
    rc |= (not fits)

    t0 = time.time()
    p.save(PAYLOAD + " v2")
    back2 = p.load()
    print(f"{'ok  ' if back2 == PAYLOAD + ' v2' else 'FAIL'} second save swaps the live item "
          f"({time.time() - t0:.2f}s, no prompt)")
    rc |= (back2 != PAYLOAD + " v2")

    t0 = time.time()
    p.save(PAYLOAD + " v3")
    print(f"{'ok  ' if p.load() == PAYLOAD + ' v3' else 'FAIL'} third save "
          f"({time.time() - t0:.2f}s)")
    rc |= (p.load() != PAYLOAD + " v3")

    staged = subprocess.run(["/usr/bin/security", "find-generic-password",
                             "-s", SERVICE, "-a", "default-mailbox.staged"],
                            capture_output=True, text=True)
    print(f"{'ok  ' if staged.returncode == 44 else 'FAIL'} staging item cleaned up "
          f"(rc {staged.returncode})")
    rc |= (staged.returncode != 44)

    print(f"ok   signal mtime {p.time_last_modified():.0f}, location {p.get_location()}")
finally:
    left = []
    for base in ("default-mailbox", "default-mailbox.staged"):
        for acct in [base] + [f"{base}.{i}" for i in range(1, 65)]:
            d = subprocess.run(["/usr/bin/security", "delete-generic-password",
                                "-s", SERVICE, "-a", acct],
                               capture_output=True, text=True)
            if d.returncode == 0:
                left.append(acct)
    print(f"cleanup: removed {len(left)} item(s): {', '.join(left) or 'none'}")
    for base in ("default-mailbox", "default-mailbox.staged"):
        g = subprocess.run(["/usr/bin/security", "find-generic-password",
                            "-s", SERVICE, "-a", base], capture_output=True, text=True)
        if g.returncode != 44:
            print(f"FAIL: {base} still present after cleanup"); rc = 1
sys.exit(1 if rc else 0)
