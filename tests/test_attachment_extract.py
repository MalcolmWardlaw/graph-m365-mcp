# /// script
# requires-python = ">=3.11"
# dependencies = ["msal>=1.21", "msal-extensions", "httpx", "mcp<2", "html2text", "pypdf"]
# ///
"""In-memory attachment extractors in server.py. No Graph, no auth, no files.

Run: uv run --script tests/test_attachment_extract.py
Fixtures are built here so no binary lands in the repo.
"""
import builtins, io, os, sys, tempfile, zipfile

os.environ.setdefault("GRAPH_CLIENT_ID", "test"); os.environ.setdefault("GRAPH_TENANT_ID", "test")
os.environ["KEYCHAIN_SERVICE"] = "graph-m365-attach-test"   # keep the real cache untouched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name); fails += 0 if cond else 1

def make_pdf(text: str) -> bytes:
    """Minimal one-page PDF with a Helvetica text run, xref computed properly."""
    content = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n"); offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out)); out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets: out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)

def make_docx(paras) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paras)
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>"); z.writestr("word/document.xml", doc)
    return buf.getvalue()

# guard: extractors must never touch the filesystem
real_open, real_mkstemp = builtins.open, tempfile.mkstemp
def no_open(*a, **k): raise AssertionError("extractor called open()")
def no_tmp(*a, **k): raise AssertionError("extractor called tempfile")
builtins.open = no_open; tempfile.mkstemp = no_tmp; tempfile.NamedTemporaryFile = no_tmp; tempfile.TemporaryFile = no_tmp
try:
    text, ex = server._extract_text("Fin Seminar Day Schedule.pdf", "application/pdf", make_pdf("Seminar 11:00am B133"))
    check("pdf extracts the marker", "Seminar 11:00am B133" in text and ex == "pypdf")
    text, ex = server._extract_text("letter.docx", server._DOCX_TYPE, make_docx(["First paragraph.", "Second paragraph."]))
    check("docx yields both paragraphs in order", text == "First paragraph.\n\nSecond paragraph." and ex == "docx-xml")
    text, ex = server._extract_text("data.csv", "text/csv", b"a,b\n1,2\n")
    check("csv round-trips", text == "a,b\n1,2\n" and ex == "utf-8")
    text, ex = server._extract_text("page.html", "text/html", b"<p>Hello <b>there</b></p>")
    check("html goes through html2text", "Hello" in text and "<p>" not in text and ex == "html2text")
    check("xlsx refused", server._extract_text("book.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK") == (None, None))
    check("png refused", server._extract_text("img.png", "image/png", b"\x89PNG") == (None, None))
    check("suffix fallback when contentType is generic", server._extract_text("notes.md", "application/octet-stream", b"# hi")[1] == "utf-8")
finally:
    builtins.open = real_open; tempfile.mkstemp = real_mkstemp

check("kind: file", server._attachment_kind("#microsoft.graph.fileAttachment") == "file")
check("kind: item", server._attachment_kind("#microsoft.graph.itemAttachment") == "item")
check("kind: reference", server._attachment_kind("#microsoft.graph.referenceAttachment") == "reference")
check("kind: unknown", server._attachment_kind(None) == "unknown")
check("_summ carries hasAttachments", server._summ({"id": "x", "hasAttachments": True})["hasAttachments"] is True)
check("size ceiling is 10 MB", server._ATTACH_MAX_BYTES == 10 * 1024 * 1024)
check("char ceiling is 60000", server._ATTACH_MAX_CHARS == 60_000)
print(f"FAILURES: {fails}"); sys.exit(1 if fails else 0)
