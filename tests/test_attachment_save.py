# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""Path helpers behind save_attachment. No Graph, no auth; writes only inside
a tempdir that is removed at exit.

Run: uv run --script tests/test_attachment_save.py
"""
import os, sys, tempfile

os.environ.setdefault("GRAPH_CLIENT_ID", "test"); os.environ.setdefault("GRAPH_TENANT_ID", "test")
os.environ["KEYCHAIN_SERVICE"] = "graph-m365-attach-test"   # keep the real cache untouched
os.environ["ATTACH_SAVE_DIR"] = ""                            # tool must stay dark here
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name); fails += 0 if cond else 1

def raises(fn, exc=ValueError):
    try:
        fn(); return False
    except exc:
        return True

# Registration is tied to the env var, same dial as the other conditionals.
tools = server.mcp._tool_manager._tools
check("dark when ATTACH_SAVE_DIR is empty", "save_attachment" not in tools)
check("get_attachment still registered", "get_attachment" in tools)

# Root resolution: unset defaults to ~/Downloads; empty or "off" turns it off.
asd = server._attach_save_dir
check("unset defaults to ~/Downloads", asd(None) == os.path.expanduser("~/Downloads"))
check("empty turns it off", asd("") == "" and asd("   ") == "")
check("'off' turns it off", asd("off") == "" and asd("OFF") == "")
check("explicit path used, ~ expanded", asd("~/x/y") == os.path.expanduser("~/x/y"))
check("explicit absolute path used", asd("/tmp/z") == "/tmp/z")

# Filename sanitizer.
sf = server._safe_filename
check("plain name passes", sf("Report.xlsx") == "Report.xlsx")
check("path stripped to basename", sf("../../etc/passwd") == "passwd")
check("backslash path stripped", sf("C:\\Users\\x\\evil.exe") == "evil.exe")
check("separators and control chars replaced", sf("b:c*d?e\"f<g>h|i\x00j.txt") == "b_c_d_e_f_g_h_i_j.txt")
check("leading dot removed", sf(".hidden") == "hidden")
check("empty falls back", sf("") == "attachment" and sf(None) == "attachment")
check("dotdot falls back", sf("..") == "attachment")
check("long name capped", len(sf("x" * 500 + ".pdf")) == 200)

# Target resolution.
with tempfile.TemporaryDirectory() as root:
    st = server._save_target
    p = st(root, None, "a.bin")
    check("root target under root", os.path.dirname(p) == os.path.realpath(root) and p.endswith("a.bin"))
    p2 = st(root, "sub/dir", "a.bin")
    check("subdir created under root", os.path.isdir(os.path.join(root, "sub", "dir")) and p2.startswith(os.path.realpath(root) + os.sep))
    check("subdir with .. refused", raises(lambda: st(root, "../x", "a.bin")))
    check("subdir with embedded .. refused", raises(lambda: st(root, "ok/../../x", "a.bin")))
    check("absolute subdir refused", raises(lambda: st(root, "/etc", "a.bin")))
    # symlink escape: a subdir that is a symlink out of the root must be refused
    outside = tempfile.mkdtemp()
    try:
        os.symlink(outside, os.path.join(root, "link"))
        check("symlinked subdir escaping root refused", raises(lambda: st(root, "link", "a.bin")))
    finally:
        os.rmdir(outside)
    # no overwrite: existing file gets a numbered sibling
    open(os.path.join(root, "a.bin"), "wb").close()
    check("existing file not reused", os.path.basename(st(root, None, "a.bin")) == "a-1.bin")
    open(os.path.join(root, "a-1.bin"), "wb").close()
    check("second collision increments", os.path.basename(st(root, None, "a.bin")) == "a-2.bin")
    # directory permissions
    check("created leaf dir is 0700", (os.stat(os.path.join(root, "sub", "dir")).st_mode & 0o777) == 0o700)

check("save ceiling is 50 MB", server._ATTACH_SAVE_MAX_BYTES == 50 * 1024 * 1024)

# Registration in a fresh process: on with ATTACH_SAVE_DIR unset (the default),
# off with "off". This process imported server with "", so it cannot test this.
import subprocess
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
probe = ("import sys; sys.path.insert(0, %r); import server; "
         "print('save_attachment' in server.mcp._tool_manager._tools, server.ATTACH_SAVE_DIR)") % here
def registered(value):
    env = {k: v for k, v in os.environ.items() if k != "ATTACH_SAVE_DIR"}
    if value is not None:
        env["ATTACH_SAVE_DIR"] = value
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True)
    return out.stdout.strip()
check("registered by default, root is ~/Downloads",
      registered(None) == f"True {os.path.expanduser('~/Downloads')}")
check("not registered with ATTACH_SAVE_DIR=off", registered("off") == "False")

print(f"FAILURES: {fails}"); sys.exit(1 if fails else 0)
