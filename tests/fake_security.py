#!/usr/bin/env python3
"""Stand-in for /usr/bin/security: enough of find/add-generic-password to test."""
import json, os, shlex, sys

STORE = os.environ["FAKE_KEYCHAIN_STORE"]
MODE = os.environ.get("FAKE_KEYCHAIN_MODE", "ok")


def read():
    try:
        return json.load(open(STORE))
    except FileNotFoundError:
        return {}


def find(argv):
    svc = argv[argv.index("-s") + 1]
    acct = argv[argv.index("-a") + 1]
    db = read()
    hit = db.get(f"{svc}\x00{acct}")
    if hit is None:
        sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item "
                         "could not be found in the keychain.\n")
        return 44
    sys.stdout.write(hit + "\n")          # security appends a newline
    return 0


def add(argv):
    svc = argv[argv.index("-s") + 1]
    acct = argv[argv.index("-a") + 1]
    val = argv[argv.index("-w") + 1]
    assert "-T" in argv, "ACL flag missing"
    db = read()
    key = f"{svc}\x00{acct}"
    if key in db and "-U" not in argv:
        sys.stderr.write("security: SecKeychainAddGenericPassword: The specified "
                         "item already exists in the keychain.\n")
        sys.stderr.write("add-generic-password: returned 45\n")   # interactive mode
        return 45
    db[key] = val
    json.dump(db, open(STORE, "w"))
    return 0


def delete(argv):
    svc = argv[argv.index("-s") + 1]
    acct = argv[argv.index("-a") + 1]
    db = read()
    if db.pop(f"{svc}\x00{acct}", None) is None:
        sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item "
                         "could not be found in the keychain.\n")
        return 44
    json.dump(db, open(STORE, "w"))
    sys.stderr.write("password has been deleted.\n")   # success chatter on stderr
    return 0


def main():
    if MODE == "noisy":                   # rc 0 but chatty on stderr
        sys.stderr.write("security: warning: something happened\n")
    if MODE == "denied":
        sys.stderr.write("security: SecKeychainFindGenericPassword: User interaction "
                         "is not allowed.\n")
        return 36
    if MODE == "hang":
        import time; time.sleep(60)
    if MODE == "add_blocked" and sys.argv[1:2] == ["-i"]:
        import time; time.sleep(60)          # a pending dialog blocks creates only
    argv = sys.argv[1:]
    if argv and argv[0] == "-i":
        line = sys.stdin.readline()
        assert line, "interactive mode got no command on stdin"
        return add(shlex.split(line))
    if argv and argv[0] == "find-generic-password":
        return find(argv)
    if argv and argv[0] == "delete-generic-password":
        return delete(argv)
    sys.stderr.write(f"security: unknown command {argv!r}\n")
    return 2


sys.exit(main())
