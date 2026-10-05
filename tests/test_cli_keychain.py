# /// script
# requires-python = ">=3.11"
# dependencies = ["msal-extensions"]
# ///
"""Unit tests for CliKeychainPersistence against a stand-in `security` binary.

Extracts the credential-layer classes out of server.py so the test needs none
of the server's runtime deps (msal, httpx, mcp, html2text).
"""
import base64, io, json, os, re, subprocess, sys, tempfile

from msal_extensions.persistence import (
    BasePersistence, FilePersistence, PersistenceNotFound, PersistenceDecryptionError)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
start = SRC.index("def _sec_name(")
end = SRC.index("def _build_cache(")
ns = dict(os=os, re=re, sys=sys, base64=base64, subprocess=subprocess,
          BasePersistence=BasePersistence, FilePersistence=FilePersistence,
          PersistenceNotFound=PersistenceNotFound,
          PersistenceDecryptionError=PersistenceDecryptionError,
          KEYCHAIN_ACCOUNT="default-mailbox")
exec(SRC[start:end], ns)
CliKeychainPersistence = ns["CliKeychainPersistence"]
_sec_name = ns["_sec_name"]

tmp = tempfile.mkdtemp(prefix="cli-keychain-test-")
STORE = os.path.join(tmp, "store.json")
SIGNAL = os.path.join(tmp, "cache.signal")
os.environ["FAKE_KEYCHAIN_STORE"] = STORE

FAKE = [sys.executable, os.path.join(HERE, "fake_security.py")]


class P(CliKeychainPersistence):
    _SECURITY = FAKE[0]

    def _run(self, args, stdin=""):
        return super()._run([FAKE[1], *args], stdin=stdin)


def make(service="graph-m365-test"):
    return P(SIGNAL, service_name=service)


def check(name, fn):
    try:
        fn()
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}")
        return 1
    print(f"ok   {name}")
    return 0


fails = 0
p = make()

def t_not_found():
    try:
        p.load()
    except PersistenceNotFound:
        return
    raise AssertionError("expected PersistenceNotFound")
fails += check("load before save -> PersistenceNotFound", t_not_found)

PAYLOAD = json.dumps({"RefreshToken": {"a b": "tok \"quoted\"\nnewline\ttab"},
                      "unicode": "café ✓"})

def t_round_trip():
    p.save(PAYLOAD)
    assert p.load() == PAYLOAD, "round trip mismatch"
fails += check("save then load round trips exactly", t_round_trip)

def t_encoded_at_rest():
    db = json.load(open(STORE))
    head = db["graph-m365-test\x00default-mailbox"]
    assert not head.startswith("{"), "stored value is not encoded"
    m = re.match(r"^v2:(\d+):", head)
    assert m, f"chunk 0 lacks the v2 count header: {head[:16]!r}"
    n = int(m.group(1))
    parts = [head[m.end():]] + [db[f"graph-m365-test\x00default-mailbox.{i}"]
                                for i in range(1, n)]
    assert base64.b64decode("".join(parts), validate=True).decode() == PAYLOAD
fails += check("value is stored base64, not plaintext", t_encoded_at_rest)

def t_chunked_at_rest():
    # A payload past one item's budget must land in several, and read back whole.
    big = json.dumps({"RefreshToken": {f"k{i}": base64.b64encode(
        bytes((i * 7 + j) % 256 for j in range(900))).decode() for i in range(10)}})
    p.save(big)
    db = json.load(open(STORE))
    n = int(re.match(r"^v2:(\d+):", db["graph-m365-test\x00default-mailbox"]).group(1))
    assert n > 1, f"expected several items for a {len(big)}-byte payload, got {n}"
    longest = max(len(v) for k, v in db.items() if "default-mailbox" in k)
    assert longest < p._LINE_LIMIT, f"an item holds {longest} chars"
    assert p.load() == big, "chunked round trip mismatch"
    p.save(PAYLOAD)                       # shrink back; must clear the extra items
    assert p.load() == PAYLOAD, "shrink round trip mismatch"
    left = [k for k in json.load(open(STORE)) if ".staged" not in k and k.endswith(
        tuple(f".{i}" for i in range(1, n)))]
    assert not left, f"stale chunks left behind: {left}"
fails += check("a large payload chunks, reads back, and shrinks cleanly",
               t_chunked_at_rest)

def t_update():  # delete-then-add
    p.save(PAYLOAD + " v2")
    assert p.load() == PAYLOAD + " v2"
fails += check("second save replaces the item", t_update)

def t_signal_touched():
    before = p.time_last_modified()
    p.save(PAYLOAD)
    assert p.time_last_modified() >= before
    assert p.get_location() == SIGNAL
fails += check("save touches the signal file", t_signal_touched)

def t_legacy_json():
    legacy = make("graph-m365-legacy")
    db = json.load(open(STORE))
    db["graph-m365-legacy\x00default-mailbox"] = '{"RefreshToken": {"x": 1}}'
    json.dump(db, open(STORE, "w"))
    assert legacy.load() == '{"RefreshToken": {"x": 1}}'
fails += check("legacy raw-JSON value reads back verbatim", t_legacy_json)

def t_garbage():
    junk = make("graph-m365-junk")
    db = json.load(open(STORE))
    db["graph-m365-junk\x00default-mailbox"] = "not base64 and not json !!"
    json.dump(db, open(STORE, "w"))
    try:
        junk.load()
    except PersistenceDecryptionError:
        return
    raise AssertionError("expected PersistenceDecryptionError")
fails += check("undecodable value -> PersistenceDecryptionError", t_garbage)

def t_denied():
    os.environ["FAKE_KEYCHAIN_MODE"] = "denied"
    try:
        p.load()
    except RuntimeError as e:
        assert "User interaction is not allowed" in str(e), str(e)
        assert str(e).strip(), "error message must not be empty"
        return
    finally:
        os.environ["FAKE_KEYCHAIN_MODE"] = "ok"
    raise AssertionError("expected RuntimeError")
fails += check("denied read raises with a non-empty message", t_denied)

def t_timeout():
    os.environ["FAKE_KEYCHAIN_MODE"] = "hang"
    slow = make()
    slow._TIMEOUT = 1
    try:
        slow.load()
    except RuntimeError as e:
        assert "timed out" in str(e), str(e)
        return
    finally:
        os.environ["FAKE_KEYCHAIN_MODE"] = "ok"
    raise AssertionError("expected RuntimeError on timeout")
fails += check("hung security call raises a timeout error", t_timeout)

def t_bad_name():
    for bad in ('has"quote', "back\\slash", "new\nline", ""):
        try:
            make(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
fails += check("service names hostile to the tokenizer are rejected", t_bad_name)

def t_stdin_is_a_pipe():
    # A child must never inherit this process' stdin (the MCP channel).
    r, w = os.pipe()
    os.write(w, b"MCP-BYTES\n"); os.close(w)
    saved = os.dup(0); os.dup2(r, 0)
    try:
        p.save(PAYLOAD)
        assert p.load() == PAYLOAD
        assert os.read(r, 32) == b"MCP-BYTES\n", "child consumed our stdin"
    finally:
        os.dup2(saved, 0); os.close(saved); os.close(r)
def t_acl_flag():
    assert CliKeychainPersistence._SECURITY == "/usr/bin/security"
    seen = {}
    class Rec(CliKeychainPersistence):
        def _run(self, args, stdin=""):
            seen["args"], seen["stdin"] = args, stdin
            class R: returncode, stdout, stderr = 0, "", ""
            return R()
    secret = base64.b64encode(b"tok").decode()
    Rec(SIGNAL, service_name="graph-m365-test")._add(secret, "default-mailbox")
    assert seen["args"] == ["-i"], seen["args"]
    assert "-T /usr/bin/security" in seen["stdin"], seen["stdin"]
    assert "-U" not in seen["stdin"], "must not take the prompting update path"
    assert seen["stdin"].endswith("\n")
    assert not any(secret in a for a in seen["args"]), "secret must not reach argv"
fails += check("save passes the secret on stdin, never -U", t_acl_flag)

def t_write_sequence():
    calls = []
    class Rec(CliKeychainPersistence):
        def _run(self, args, stdin=""):
            acct = (args[args.index("-a") + 1] if "-a" in args
                    else stdin.split('-a "')[1].split('"')[0])
            calls.append((args[0], acct))
            class R:
                # Only chunk 0 of either set exists, so the sweep for further
                # items stops immediately, as it does against a one-item cache.
                returncode = 44 if acct.rsplit(".", 1)[-1].isdigit() else 0
                stderr, stdout = "", ""
            return R()
    Rec(SIGNAL, service_name="graph-m365-test").save("y")
    # The invariant: the staging set is complete before anything touches the
    # live set, so a blocked create cannot leave the cache unrecoverable.
    assert calls == [
        ("delete-generic-password", "default-mailbox.staged"),
        ("delete-generic-password", "default-mailbox.staged.1"),
        ("-i", "default-mailbox.staged"),
        ("delete-generic-password", "default-mailbox"),
        ("delete-generic-password", "default-mailbox.1"),
        ("-i", "default-mailbox"),
        ("delete-generic-password", "default-mailbox.staged"),
        ("delete-generic-password", "default-mailbox.staged.1"),
    ], calls
    live = [i for i, c in enumerate(calls) if c[1].startswith("default-mailbox")
            and ".staged" not in c[1]]
    staged_write = next(i for i, c in enumerate(calls)
                        if c == ("-i", "default-mailbox.staged"))
    assert staged_write < min(live), "live set touched before staging was complete"
fails += check("save stages, then swaps the live set", t_write_sequence)

def t_torn_live_set():
    # A crash between clearing and rewriting the live set leaves a head that
    # promises items which are absent. That must read as "no live value" so the
    # load falls through to staging, not as a short value.
    big = json.dumps({"k": base64.b64encode(bytes(range(256)) * 20).decode()})
    p.save(big)
    db = json.load(open(STORE))
    n = int(re.match(r"^v2:(\d+):", db["graph-m365-test\x00default-mailbox"]).group(1))
    assert n > 1, "payload did not chunk; test proves nothing"
    # Re-stage the whole value, then tear the live set by dropping its last item.
    for i in range(n):
        src_key = "graph-m365-test\x00default-mailbox" + (f".{i}" if i else "")
        dst_key = "graph-m365-test\x00default-mailbox.staged" + (f".{i}" if i else "")
        db[dst_key] = db[src_key]
    del db["graph-m365-test\x00default-mailbox.%d" % (n - 1)]
    json.dump(db, open(STORE, "w"))
    assert p.load() == big, "torn live set did not fall back to staging"
fails += check("a torn live set falls back to the staging copy", t_torn_live_set)

def t_unreadable_staging():
    # Live gone, staging junk: the honest report is an empty cache. Raising a
    # decryption error instead would escape PersistedTokenCache, which catches
    # only PersistenceNotFound, and block the interactive re-auth that recovers.
    db = {"graph-m365-test\x00default-mailbox.staged": "A" * 4006}
    json.dump(db, open(STORE, "w"))
    try:
        p.load()
        assert False, "expected PersistenceNotFound"
    except PersistenceNotFound:
        pass
    except PersistenceDecryptionError:
        assert False, "staging junk raised PersistenceDecryptionError"
fails += check("unreadable staging reads as an empty cache", t_unreadable_staging)

def t_overlong_command_refused():
    # The guard behind the whole chunking design: security truncates silently
    # past 4096 bytes, so a command that long must never be issued.
    try:
        p._add("A" * p._LINE_LIMIT, "default-mailbox")
        assert False, "expected an over-length command to be refused"
    except RuntimeError as e:
        assert "line buffer" in str(e), str(e)
fails += check("an over-length write is refused, not truncated",
               t_overlong_command_refused)

def t_blocked_create_leaves_live_item(store=STORE):
    # While a Keychain dialog is pending, creates block but deletes succeed.
    p.save(PAYLOAD)                                  # a good value is in place
    os.environ["FAKE_KEYCHAIN_MODE"] = "add_blocked"
    try:
        p.save(PAYLOAD + " never lands")
        raise AssertionError("expected the blocked create to raise")
    except RuntimeError as e:
        assert "timed out" in str(e), str(e)
    finally:
        os.environ["FAKE_KEYCHAIN_MODE"] = "ok"
    assert p.load() == PAYLOAD, "the live item must survive a blocked create"
fails += check("a blocked create leaves the live item intact",
               t_blocked_create_leaves_live_item)

def t_stranded_staging_is_read_back():
    q = make("graph-m365-stranded")
    db = json.load(open(STORE))
    db["graph-m365-stranded\x00default-mailbox.staged"] = base64.b64encode(b"rescued").decode()
    json.dump(db, open(STORE, "w"))
    assert q.load() == "rescued"
fails += check("a value stranded in staging is read back", t_stranded_staging_is_read_back)

def t_delete_chatter_is_not_a_failure():
    p.save(PAYLOAD)          # goes through delete, whose stderr says "deleted."
    assert p.load() == PAYLOAD
fails += check("success chatter on stderr is not treated as failure",
               t_delete_chatter_is_not_a_failure)

def t_interactive_failure_line():
    class Rec(CliKeychainPersistence):
        def _run(self, args, stdin=""):
            class R:
                returncode = 0                      # rc lies
                stderr = "add-generic-password: returned 45"
                stdout = ""
            return R()
    try:
        Rec(SIGNAL, service_name="graph-m365-test")._add("eA==", "default-mailbox")
    except RuntimeError as e:
        assert "returned 45" in str(e)
        return
    raise AssertionError("expected a failure despite rc 0")
fails += check("interactive 'returned N' counts as failure even at rc 0",
               t_interactive_failure_line)

fails += check("child never reads this process' stdin", t_stdin_is_a_pipe)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
