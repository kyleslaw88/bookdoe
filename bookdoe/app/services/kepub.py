"""Best-effort EPUB -> KEPUB conversion, with no dependency beyond the stdlib.

Kobo devices and the Kobo desktop app read plain EPUB natively, but only track
per-paragraph reading position/highlighting on files whose reading-system content
documents carry Kobo's own <span class="koboSpan"> markers and whose filename ends
in .kepub.epub - that suffix, not file content, is what tells Kobo's software to turn
those features on. Real kepubify wraps individual sentences; this wraps whole leaf
block elements (the "no nested block" case below) - coarser granularity, but a
sentence splitter is real NLP surface area for a difference that mostly affects
highlight precision, not whether the book opens or where "resume reading" lands.

If a content document fails to parse, it is copied through unmodified rather than
failing the whole conversion - a partially-annotated EPUB that opens beats an error.
"""
from __future__ import annotations

import io
import logging
import zipfile
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring

log = logging.getLogger(__name__)

XHTML_NS = "http://www.w3.org/1999/xhtml"
ET.register_namespace("", XHTML_NS)

BLOCK_TAGS = {
    f"{{{XHTML_NS}}}{t}" for t in
    ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "dd", "dt", "figcaption")
} | {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "td", "dd", "dt", "figcaption"}


def _has_block_descendant(el: ET.Element) -> bool:
    for child in el:
        if child.tag in BLOCK_TAGS or _has_block_descendant(child):
            return True
    return False


def _wrap_leaf_blocks(el: ET.Element, counter: list[int]) -> None:
    for child in list(el):
        if child.tag in BLOCK_TAGS and not _has_block_descendant(child):
            if (child.text and child.text.strip()) or len(child):
                counter[0] += 1
                span = ET.Element("span", {"class": "koboSpan", "id": f"kobo.{counter[0]}.1"})
                span.text = child.text
                child.text = None
                for grandchild in list(child):
                    child.remove(grandchild)
                    span.append(grandchild)
                child.append(span)
        else:
            _wrap_leaf_blocks(child, counter)


def _convert_document(data: bytes) -> bytes:
    root = safe_fromstring(data)
    _wrap_leaf_blocks(root, [0])
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _content_documents(zf: zipfile.ZipFile) -> set[str]:
    try:
        container = safe_fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").attrib["full-path"]
        opf = safe_fromstring(zf.read(opf_path))
    except Exception:
        return set()
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    names = set(zf.namelist())
    docs = set()
    for item in opf.findall(".//{*}manifest/{*}item"):
        if (item.get("media-type") or "") in ("application/xhtml+xml", "text/html"):
            href = (base + (item.get("href") or "")).replace("//", "/")
            if href in names:
                docs.add(href)
    return docs


def convert(epub_bytes: bytes) -> bytes:
    """Convert one EPUB's bytes to KEPUB bytes. Never raises for a single bad document."""
    src = zipfile.ZipFile(io.BytesIO(epub_bytes))
    content_docs = _content_documents(src)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as dest:
        # mimetype must be the first entry, uncompressed, with no extra field - the
        # part of the zip spec every epub reader actually enforces.
        dest.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in src.infolist():
            if info.filename == "mimetype":
                continue
            data = src.read(info.filename)
            if info.filename in content_docs:
                try:
                    data = _convert_document(data)
                except Exception as exc:
                    log.warning("kepub: leaving %s unconverted (%s)", info.filename, exc)
            dest.writestr(info.filename, data)
    return out_buf.getvalue()
