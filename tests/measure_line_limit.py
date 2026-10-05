"""Exact command-line length at which `security -i` splits the line.

Backs CliKeychainPersistence._LINE_LIMIT. Run it if that constant is ever
doubted: it binary-searches the real security(1) and prints the boundary.
Writes only to a throwaway service name.
"""
import subprocess

SVC, SEC = "graph-m365-boundary", "/usr/bin/security"


def ok(total):
    """Build a command of exactly `total` chars incl. newline; did it store whole?"""
    prefix = 'add-generic-password -s "%s" -a "x" -T %s -w ' % (SVC, SEC)
    n = total - len(prefix) - 1
    tok = "A" * n
    subprocess.run([SEC, "delete-generic-password", "-s", SVC, "-a", "x"],
                   capture_output=True, text=True)
    r = subprocess.run([SEC, "-i"], input=prefix + tok + "\n",
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return False
    g = subprocess.run([SEC, "find-generic-password", "-s", SVC, "-a", "x", "-w"],
                       capture_output=True, text=True)
    good = g.stdout.rstrip("\n") == tok
    subprocess.run([SEC, "delete-generic-password", "-s", SVC, "-a", "x"],
                   capture_output=True, text=True)
    return good


lo, hi = 1000, 5000          # lo known good, hi known bad
assert ok(lo) and not ok(hi)
while hi - lo > 1:
    mid = (lo + hi) // 2
    if ok(mid):
        lo = mid
    else:
        hi = mid
print("largest whole command line: %d bytes (first failing: %d)" % (lo, hi))
