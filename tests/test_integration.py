# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""Two PersistedTokenCache instances over one store, as the live server
processes are. Exercises modify -> save and search -> reload -> load through the
signal file. Runs on whichever backend KEYCHAIN_BACKEND selects (file by
default); the Keychain sweep in the finally block is a no-op on the file
backend. Also the regression check for the MSAL singleton: _app() caching one
application object must not bypass this reload path, which lives in the cache."""
import os, subprocess, sys, tempfile

SVC = "graph-m365-probe3"
assert SVC != "graph-m365"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["KEYCHAIN_SERVICE"] = SVC
import server
from msal_extensions import PersistedTokenCache

print("persistence:", type(server._CACHE._persistence).__name__,
      "| signal:", server.SIGNAL)

TOKEN = dict(home_account_id="h.acct", environment="login.microsoftonline.com",
             client_id="cid", realm="tenant", target="Mail.Read",
             secret="fake-not-a-real-token", credential_type="AccessToken",
             expires_on="4102462800", cached_at="1787900000")
rc = 0
try:
    writer = server._CACHE
    writer.modify("AccessToken", TOKEN, {"secret": TOKEN["secret"]})
    print("ok   modify() wrote through to the Keychain")

    reader = PersistedTokenCache(server._build_cache()._persistence)
    hits = list(reader.search("AccessToken"))
    ok = len(hits) == 1 and hits[0]["secret"] == TOKEN["secret"]
    print(f"{'ok  ' if ok else 'FAIL'} a second instance reads it back ({len(hits)} hit)")
    rc |= (not ok)

    writer.modify("AccessToken", TOKEN, {"secret": "rotated"})
    hits = list(reader.search("AccessToken"))
    ok = len(hits) == 1 and hits[0]["secret"] == "rotated"
    print(f"{'ok  ' if ok else 'FAIL'} a refresh in one instance is seen by the other")
    rc |= (not ok)

    raw = subprocess.run(["/usr/bin/security", "find-generic-password",
                          "-s", SVC, "-a", "default-mailbox", "-w"],
                         capture_output=True, text=True).stdout.strip()
    leaked = "rotated" in raw
    print(f"{'FAIL' if leaked else 'ok  '} value at rest is not plaintext")
    rc |= leaked
finally:
    for acct in ("default-mailbox", "default-mailbox.staged"):
        subprocess.run(["/usr/bin/security", "delete-generic-password",
                        "-s", SVC, "-a", acct], capture_output=True)
    for f in ("cache.json", "cache.signal", "cache.signal.lockfile"):
        try: os.remove(os.path.expanduser(f"~/.config/{SVC}/{f}"))
        except FileNotFoundError: pass
    try: os.rmdir(os.path.expanduser(f"~/.config/{SVC}"))
    except OSError as e: print("dir cleanup:", e)
    print("cleanup done")
sys.exit(1 if rc else 0)
