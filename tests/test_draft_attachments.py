# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""Attachments on create_draft: the path rules and the inline payload, offline;
plus a live round trip gated on LIVE=1 that creates one throwaway draft with a
10 KB and a 5 MB file (the second goes through an upload session), verifies the
sizes Graph reports, soft-deletes it, and then checks the rollback by failing a
second upload on purpose. Exchange routes a deleted draft past Deleted Items
into Recoverable Items/Deletions (verified 2026-10-05), so the live block leaves
two empty drafts there, recoverable through Outlook for the retention window.

Run: uv run --script tests/test_draft_attachments.py
Live: env LIVE=1 GRAPH_CLIENT_ID=... GRAPH_TENANT_ID=... uv run --script tests/test_draft_attachments.py
"""
import os, sys, base64, hashlib, tempfile

LIVE = os.environ.get("LIVE") == "1"
if not LIVE:
    os.environ.setdefault("GRAPH_CLIENT_ID", "test"); os.environ.setdefault("GRAPH_TENANT_ID", "test")
    os.environ["KEYCHAIN_SERVICE"] = "graph-m365-attach-test"   # keep the real cache untouched
# A save dir outside $HOME, to prove it extends the allowed roots.
SAVE_DIR = tempfile.mkdtemp(prefix="graph-m365-savedir-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None)
os.environ["ATTACH_SAVE_DIR"] = SAVE_DIR
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

home = os.path.realpath(os.path.expanduser("~"))
check("roots are $HOME and ATTACH_SAVE_DIR", server._attach_roots() == [home, os.path.realpath(SAVE_DIR)])

work = tempfile.mkdtemp(prefix=".graph-m365-attach-test-", dir=home)
try:
    small = os.path.join(work, "note.txt")
    with open(small, "w") as fh:
        fh.write("hello attachment\n")
    check("regular file under $HOME accepted", server._attach_source(small) == os.path.realpath(small))
    check("tilde path accepted", server._attach_source("~/" + os.path.relpath(small, home)) == os.path.realpath(small))
    check("relative path refused", raises(lambda: server._attach_source(os.path.relpath(small))))
    check("empty path refused", raises(lambda: server._attach_source("  ")))
    check("directory refused", raises(lambda: server._attach_source(work)))
    check("missing file refused", raises(lambda: server._attach_source(os.path.join(work, "nope.pdf"))))
    empty = os.path.join(work, "empty.bin"); open(empty, "wb").close()
    check("empty file refused", raises(lambda: server._attach_source(empty)))

    # Symlink inside $HOME to a file outside it: realpath lands outside, refused.
    link = os.path.join(work, "hosts.txt")
    os.symlink("/etc/hosts", link)
    check("symlink escaping $HOME refused", raises(lambda: server._attach_source(link)))
    check("file outside every root refused", raises(lambda: server._attach_source("/etc/hosts")))

    # ATTACH_SAVE_DIR is a second root.
    saved = os.path.join(SAVE_DIR, "report.csv")
    with open(saved, "w") as fh:
        fh.write("a,b\n1,2\n")
    check("file under ATTACH_SAVE_DIR accepted", server._attach_source(saved) == os.path.realpath(saved))

    # Ceiling check without allocating 150 MB: a sparse file.
    big = os.path.join(work, "big.bin")
    with open(big, "wb") as fh:
        fh.truncate(server._DRAFT_ATTACH_MAX_BYTES + 1)
    check("file over 150 MB refused", raises(lambda: server._attach_source(big)))

    # Inline payload shape.
    data = open(small, "rb").read()
    p = server._attach_payload(small, data)
    check("payload is a fileAttachment", p["@odata.type"] == "#microsoft.graph.fileAttachment")
    check("payload name is the bare basename", p["name"] == "note.txt")
    check("payload content type guessed", p["contentType"] == "text/plain")
    check("payload bytes round-trip", base64.b64decode(p["contentBytes"]) == data)
    check("unknown suffix falls back to octet-stream", server._attach_content_type("/x/y.weirdext") == "application/octet-stream")
    check("inline ceiling is 3 MB", server._DRAFT_ATTACH_INLINE_MAX == 3 * 1024 * 1024)
    check("upload chunk under 4 MB and a 320 KiB multiple",
          server._UPLOAD_CHUNK <= 4 * 1024 * 1024 and server._UPLOAD_CHUNK % (320 * 1024) == 0)

    # Validation happens before any Graph call: a bad path must not create a draft.
    calls = []
    real_req = server._req
    server._req = lambda *a, **k: calls.append(a) or {}
    try:
        check("bad path raises before the draft POST",
              raises(lambda: server.create_draft(["a@b.c"], "s", "b", attachments=[link])) and calls == [])
    finally:
        server._req = real_req

    if LIVE:
        print("--- live")
        ten_k = os.path.join(work, "ten-k.bin")
        five_m = os.path.join(work, "five-m.bin")
        with open(ten_k, "wb") as fh:
            fh.write(os.urandom(10 * 1024))
        with open(five_m, "wb") as fh:
            fh.write(os.urandom(5 * 1024 * 1024))
        me = server._get("/me", {"$select": "mail,userPrincipalName"})
        addr = me.get("mail") or me.get("userPrincipalName")
        d = server.create_draft([addr], "graph-m365 attachment test (safe to delete)",
                                "Created by tests/test_draft_attachments.py", attachments=[ten_k, five_m])
        check("live draft returns id and webLink", bool(d.get("id")) and bool(d.get("webLink")))
        atts = d.get("attachments") or []
        check("live return lists both attachments", [a["name"] for a in atts] == ["ten-k.bin", "five-m.bin"])
        check("live return carries sizes and digests, no bytes",
              [a["bytes"] for a in atts] == [10 * 1024, 5 * 1024 * 1024]
              and atts[1]["sha256"] == hashlib.sha256(open(five_m, "rb").read()).hexdigest()
              and not any("contentBytes" in a for a in atts))
        m = server._get(f"/me/messages/{d['id']}",
                        {"$select": "id,hasAttachments", "$expand": "attachments($select=name,size)"})
        # Graph's `size` is the stored attachment including MIME headers, so it
        # runs a few hundred bytes over the file (measured: 10432 for 10240,
        # 5243029 for 5242880). Assert the order of magnitude, not equality.
        sizes = sorted(a["size"] for a in m.get("attachments") or [])
        check("Graph reports two attachments with the right sizes",
              m.get("hasAttachments") and len(sizes) == 2
              and 0 <= sizes[0] - 10 * 1024 < 2048 and 0 <= sizes[1] - 5 * 1024 * 1024 < 2048)
        server._req("DELETE", f"/me/messages/{d['id']}")
        print("     draft soft-deleted")

        # Rollback: second upload fails -> draft leaves Drafts for Recoverable Items.
        real_upload = server._upload_attachment
        n = {"i": 0}; seen_ids = []
        def flaky(draft_id, real):
            n["i"] += 1; seen_ids.append(draft_id)
            if n["i"] == 2:
                raise RuntimeError("induced failure")
            return real_upload(draft_id, real)
        server._upload_attachment = flaky
        try:
            try:
                server.create_draft([addr], "graph-m365 rollback test (safe to delete)", "x", attachments=[ten_k, ten_k])
                check("rollback raises", False)
            except RuntimeError as e:
                check("rollback raises with the failing index", "attachment 2 of 2 failed" in str(e))
        finally:
            server._upload_attachment = real_upload
        # The rolled-back draft id is only inside create_draft; recover it from
        # the first (successful) upload call, which flaky saw.
        rolled = seen_ids[0]
        import time; time.sleep(1)
        m3 = server._get(f"/me/messages/{rolled}", {"$select": "id,parentFolderId"})
        dumpster = server._get("/me/mailFolders/recoverableitemsdeletions", {"$select": "id"})["id"]
        drafts = server._get("/me/mailFolders/drafts", {"$select": "id"})["id"]
        check("rolled-back draft is out of Drafts and in Recoverable Items/Deletions",
              m3["parentFolderId"] == dumpster and m3["parentFolderId"] != drafts)
    else:
        print("skip live round trip (set LIVE=1 with GRAPH_CLIENT_ID/GRAPH_TENANT_ID)")
finally:
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(SAVE_DIR, ignore_errors=True)

print(f"FAILURES: {fails}"); sys.exit(1 if fails else 0)
