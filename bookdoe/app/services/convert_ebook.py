"""Ebook conversion: MOBI / AZW / AZW3 (KF8) to EPUB, pure Python, DRM-free input only (issue #265).

The unpacking is KindleUnpack, shipped on PyPI as ``mobi`` (pinned in requirements.txt). It is
GPL-3.0-only while bookdoe is MIT, so it is never imported here: it runs as a separate
process (``python -m mobi.kindleunpack``), which also contains a crash, a hang or a hostile
file to a killable child with a timeout. Everything else (DRM detection, assembling a valid
EPUB from KindleUnpack's MOBI 6 output, validation) is this module's own code.

What comes out
- KF8 / AZW3 / dual files: KindleUnpack writes a complete EPUB (``mobi8/<name>.epub``); it is
  validated and used as is.
- Plain MOBI 6 (most ``.mobi`` / ``.azw``): KindleUnpack writes one HTML document plus an OPF,
  an NCX and the images. The HTML is rewritten as well-formed XHTML (one chapter file, the
  book's own table of contents in the NCX), the images are added to the manifest and the
  cover and metadata of the OPF are kept.

Limits: no DRM removal (an encrypted file is skipped with a clear reason); no Topaz, KFX,
AZW4 or PalmDOC; a MOBI 6 book becomes a single XHTML file (its pagebreaks are kept as
markers, not split into files).
"""
from __future__ import annotations

import html
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring, parse as safe_parse

from .convert_audio import ConversionError, trim

log = logging.getLogger(__name__)

EBOOK_SOURCE_EXTS = {".mobi", ".azw", ".azw3", ".prc"}
UNPACK_TIMEOUT = 300
MAX_SOURCE_BYTES = 300 * 1024 * 1024
TOOL_VERSION = "bookdoe-convert/1"

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


def inspect_header(path: Path) -> dict[str, Any]:
    """Read the Palm database and MOBI headers: ``{ok, drm, kf8, reason}`` without unpacking.

    DRM is the PalmDOC header's encryption field (0 = none, 1 = old Mobipocket, 2 = Mobipocket);
    anything else readable as Mobipocket is treated as convertible."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if len(head) < 90:
                return {"ok": False, "drm": False, "reason": "Not a Kindle/Mobipocket file (too small)"}
            kind = head[60:68]
            if kind[:3] == b"TPZ" or head[:3] == b"TPZ":
                return {"ok": False, "drm": False, "reason": "Topaz (.tpz) books are not supported"}
            if head[:4] in (b"CONT", b"PK\x03\x04") or head[:4] == b"%PDF":
                return {"ok": False, "drm": False, "reason": "KFX / AZW4 / PDF-based Kindle files are not supported"}
            if kind != b"BOOKMOBI":
                return {"ok": False, "drm": False, "reason": "Not a Mobipocket (MOBI/AZW/AZW3) file"}
            rec0 = int.from_bytes(head[78:82], "big")
            if rec0 <= 0 or rec0 + 32 > size:
                return {"ok": False, "drm": False, "reason": "Corrupt Mobipocket header"}
            fh.seek(rec0)
            r0 = fh.read(256)
    except OSError as exc:
        return {"ok": False, "drm": False, "reason": f"Cannot read the file: {exc.strerror or exc}"}
    encryption = int.from_bytes(r0[12:14], "big")
    version = int.from_bytes(r0[36:40], "big") if r0[16:20] == b"MOBI" else 0
    if encryption != 0:
        return {"ok": False, "drm": True, "reason": "DRM-protected: skipped (bookdoe never removes DRM)", "version": version}
    if r0[16:20] != b"MOBI":
        return {"ok": False, "drm": False, "reason": "Plain PalmDOC text, not a Mobipocket book"}
    return {"ok": True, "drm": False, "kf8": version >= 8, "version": version, "reason": None}


def convertible(path: Path) -> tuple[bool, str | None]:
    """(True, None) when the file can be converted, else (False, the reason to show)."""
    if path.suffix.lower() not in EBOOK_SOURCE_EXTS:
        return False, f"{path.suffix.lstrip('.').upper() or 'This'} files cannot be converted to EPUB (supported: MOBI, AZW, AZW3)"
    info = inspect_header(path)
    return bool(info["ok"]), info["reason"]


def to_epub(src: Path, dest: Path | None, *, title: str | None = None, authors: list[str] | None = None,
            dest_fd: int | None = None) -> dict[str, Any]:
    """Convert ``src`` into the EPUB file ``dest`` (which must not exist: it is created with O_CREAT|O_EXCL|O_NOFOLLOW,
    so an existing file or a planted symlink is never written through) or, with ``dest_fd``, into that already-open
    file (the caller created it safely). All the work happens in a private 0700 temp folder. Returns a result dict.

    Raises ConversionError: ``skip=True`` for DRM / unsupported input, else retryable only
    when the failure could plausibly pass on a second try."""
    info = inspect_header(src)
    if not info["ok"]:
        raise ConversionError(info["reason"] or "Unsupported file", retryable=False, skip=True)
    if src.stat().st_size > MAX_SOURCE_BYTES:
        raise ConversionError("Refusing: the source file is larger than 300 MB", retryable=False)
    work = Path(tempfile.mkdtemp(prefix="alx-mobi-"))
    try:
        out = work / "out"
        # A copy under a plain name: KindleUnpack derives file names from it, and the original
        # path (arbitrary characters, possibly a read-only adopted file) never reaches it.
        local = work / ("book" + src.suffix.lower())
        sfd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))   # never read through a link
        try:
            with os.fdopen(os.dup(sfd), "rb") as fin, open(local, "xb") as fout:
                shutil.copyfileobj(fin, fout)
        finally:
            os.close(sfd)
        try:
            p = subprocess.run([sys.executable, "-m", "mobi.kindleunpack", str(local), str(out)],
                               capture_output=True, text=True, timeout=UNPACK_TIMEOUT, check=False, cwd=str(work))
        except subprocess.TimeoutExpired:
            raise ConversionError(f"Unpacking timed out after {UNPACK_TIMEOUT} s", retryable=False)
        if p.returncode != 0 or not out.is_dir():
            tail = trim((p.stderr or "") + "\n" + (p.stdout or ""), 600)
            if "encrypted" in tail.lower():
                raise ConversionError("DRM-protected: skipped (bookdoe never removes DRM)", retryable=False, skip=True)
            raise ConversionError(f"KindleUnpack failed: {tail}", retryable=False)
        kf8 = next(iter(sorted((out / "mobi8").glob("*.epub"))), None) if (out / "mobi8").is_dir() else None
        if kf8 is not None:
            shutil.copyfile(kf8, work / "result.epub")
            mode = "kf8-epub"
        elif (out / "mobi7" / "content.opf").is_file():
            _assemble_mobi7(out / "mobi7", work / "result.epub", title=title, authors=authors)
            mode = "mobi7-assembled"
        else:
            raise ConversionError("KindleUnpack produced no book (print-replica or unsupported layout)", retryable=False)
        report = validate_epub(work / "result.epub")
        _write_out(work / "result.epub", dest, dest_fd)
        return {"mode": mode, "tool": "KindleUnpack (mobi 0.4.1)", **report}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# MOBI 6 assembly
# ---------------------------------------------------------------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_DROP_WITH_CONTENT = {"script", "head", "guide", "title"}
_ATTR_NAME = re.compile(r"^[A-Za-z_][\w.-]*$")


class _Xhtml(HTMLParser):
    """Tolerant HTML -> well-formed XHTML body: lower-case names, quoted attributes, void
    elements closed, unclosed or stray end tags repaired, Kindle-only tags (mbp:*) mapped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0
        self.in_body = False
        self.saw_body = False

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        seen, parts = set(), []
        for k, v in attrs:
            k = k.lower()
            if not _ATTR_NAME.match(k) or k in seen or k.startswith("on"):
                continue
            seen.add(k)
            parts.append(f' {k}="{html.escape(v if v is not None else k, quote=True)}"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "body":
            self.in_body = self.saw_body = True
            return
        if tag == "html":
            return
        if tag in _DROP_WITH_CONTENT and not self.in_body:
            self.skip += 1
            self.stack.append("#drop:" + tag)
            return
        if tag == "script":
            self.skip += 1
            self.stack.append("#drop:script")
            return
        if self.skip:
            if tag not in _VOID:
                self.stack.append("#drop:" + tag)
                self.skip += 1
            return
        if tag == "mbp:pagebreak":
            self.out.append('<div class="mbp_pagebreak" style="page-break-after: always;"></div>')
            return
        if ":" in tag:     # other mbp:* wrappers: keep the content, drop the tag
            self.stack.append("#unwrap")
            return
        if tag in _VOID:
            self.out.append(f"<{tag}{self._attrs(attrs)}/>")
            return
        if tag == "a" and any(k.lower() == "name" for k, _ in attrs) and not any(k.lower() == "id" for k, _ in attrs):
            attrs = [*attrs, ("id", next(v for k, v in attrs if k.lower() == "name"))]
        self.out.append(f"<{tag}{self._attrs(attrs)}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        t = tag.lower()
        if t not in _VOID and t not in ("body", "html", "mbp:pagebreak") and self.stack:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("body", "html"):
            return
        if ":" in tag:
            if "#unwrap" in self.stack:
                self._pop_to("#unwrap")
            return
        if tag in _VOID:
            return
        if self.skip:
            if "#drop:" + tag in self.stack:
                self._pop_to("#drop:" + tag)
            return
        if tag in self.stack:
            self._pop_to(tag)

    def _pop_to(self, name: str) -> None:
        while self.stack:
            top = self.stack.pop()
            if top.startswith("#drop:"):
                self.skip = max(0, self.skip - 1)
            elif top != "#unwrap":
                self.out.append(f"</{top}>")
            if top == name:
                break

    def handle_data(self, data: str) -> None:
        if self.skip or (not self.in_body and not data.strip()):
            return
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            top = self.stack.pop()
            if not top.startswith("#"):
                self.out.append(f"</{top}>")
        return "".join(self.out)


def html_to_xhtml(raw: bytes, title: str) -> str:
    """A complete XHTML 1.1 document for the MOBI 6 HTML in ``raw`` (utf-8 from KindleUnpack)."""
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", "", text, flags=re.I)
    p = _Xhtml()
    p.feed(text)
    p.close()
    body = p.result()
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
            f'<html xmlns="{XHTML_NS}" xml:lang="en"><head><title>{html.escape(title)}</title>'
            '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/></head>'
            f"<body>{body}</body></html>")


def _media_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".svg": "image/svg+xml",
            ".css": "text/css", ".ncx": "application/x-dtbncx+xml", ".xhtml": "application/xhtml+xml"}.get(ext) \
        or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _retarget_ncx(data: bytes, renamed: dict[str, str]) -> bytes:
    """Point the NCX's content links at the renamed .xhtml documents."""
    text = data.decode("utf-8", errors="replace")
    for old, new in renamed.items():
        text = text.replace(f'src="{old}', f'src="{new}')
    return text.encode("utf-8")


_NS_LOCK = threading.Lock()


def _serialize_opf(opf: ET.Element) -> bytes:
    """Serialize with the OPF as the default namespace, then put the process-wide ElementTree
    prefix table back: services/kepub.py relies on "" meaning XHTML, and a global
    register_namespace("", OPF) would silently change its output."""
    with _NS_LOCK:
        saved = dict(ET._namespace_map)
        try:
            ET.register_namespace("", OPF_NS)
            ET.register_namespace("dc", DC_NS)
            return ET.tostring(opf, encoding="utf-8", xml_declaration=True)
        finally:
            ET._namespace_map.clear()
            ET._namespace_map.update(saved)


def _write_out(result: Path, dest: Path | None, dest_fd: int | None) -> None:
    """Put the finished EPUB in place without ever following a link or replacing a file."""
    with open(result, "rb") as fin:
        data_fd = fin.fileno()
        if dest_fd is not None:
            os.ftruncate(dest_fd, 0)
            off = 0
            while True:
                b = os.read(data_fd, 1024 * 1024)
                if not b:
                    break
                view = memoryview(b)
                while view:
                    n = os.pwrite(dest_fd, view, off)
                    off += n
                    view = view[n:]
            return
        if dest is None:
            raise ValueError("to_epub needs dest or dest_fd")
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o644)
        with os.fdopen(out, "wb") as fout:
            shutil.copyfileobj(fin, fout)


def _assemble_mobi7(src: Path, dest: Path, *, title: str | None, authors: list[str] | None) -> None:
    """Zip KindleUnpack's MOBI 6 output (content.opf, toc.ncx, book.html, Images/) as an EPUB 2."""
    # The OPF is the default namespace (some readers match the unprefixed 'package'); the few
    # opf:-qualified attributes (opf:event, opf:role) are written with a literal prefix.
    opf = safe_parse(src / "content.opf").getroot()
    meta = opf.find(f"{{{OPF_NS}}}metadata")
    manifest = opf.find(f"{{{OPF_NS}}}manifest")
    spine = opf.find(f"{{{OPF_NS}}}spine")
    if meta is None or manifest is None or spine is None:
        raise ConversionError("KindleUnpack's package document is incomplete", retryable=False)
    book_title = (meta.findtext(f"{{{DC_NS}}}title") or title or "Untitled").strip()
    if meta.find(f"{{{DC_NS}}}title") is None:
        ET.SubElement(meta, f"{{{DC_NS}}}title").text = book_title
    if meta.find(f"{{{DC_NS}}}creator") is None:
        for a in authors or []:
            ET.SubElement(meta, f"{{{DC_NS}}}creator").text = a
    if meta.find(f"{{{DC_NS}}}language") is None:
        ET.SubElement(meta, f"{{{DC_NS}}}language").text = "en"
    files: dict[str, bytes] = {}
    listed: set[str] = set()
    renamed: dict[str, str] = {}
    for item in list(manifest):
        href = unquote(item.get("href", ""))
        target = src / href
        if not href or not target.is_file():
            manifest.remove(item)
            continue
        listed.add(href)
        if href.lower().endswith((".html", ".htm")):
            new = re.sub(r"\.html?$", ".xhtml", href, flags=re.I)
            files["OEBPS/" + new] = html_to_xhtml(target.read_bytes(), book_title).encode("utf-8")
            item.set("href", new)
            item.set("media-type", "application/xhtml+xml")
            renamed[href] = new
        else:
            files["OEBPS/" + href] = target.read_bytes()
    # Images the HTML uses that KindleUnpack did not list: an EPUB must manifest every resource.
    n = 0
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        if f.is_file() and rel not in listed and rel.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".css")):
            n += 1
            ET.SubElement(manifest, f"{{{OPF_NS}}}item", {"id": f"asset{n}", "href": rel, "media-type": _media_type(rel)})
            files["OEBPS/" + rel] = f.read_bytes()
    ids = {i.get("id") for i in manifest}
    for ref in list(spine):
        if ref.get("idref") not in ids:
            spine.remove(ref)
    if len(spine) == 0:
        raise ConversionError("The book has no readable text", retryable=False)
    # After the loop: the NCX can precede the HTML in the manifest.
    for name, data in list(files.items()):
        if name.lower().endswith(".ncx"):
            files[name] = _retarget_ncx(data, renamed)
    for ref in opf.iter(f"{{{OPF_NS}}}reference"):
        href = ref.get("href", "")
        base_href, sep, frag = href.partition("#")
        if base_href in renamed:
            ref.set("href", renamed[base_href] + sep + frag)
    for el in opf.iter():
        for k in [k for k in el.attrib if k.startswith(f"{{{OPF_NS}}}")]:
            el.set("opf:" + k.split("}", 1)[1], el.attrib.pop(k))
    opf.set("xmlns:opf", OPF_NS)
    opf_bytes = _serialize_opf(opf)
    container = (f'<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" xmlns="{CONTAINER_NS}">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf_bytes, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_epub(path: Path) -> dict[str, Any]:
    """Structural check of an EPUB: a good zip whose first entry is the stored ``mimetype``,
    a container.xml naming an OPF that parses, has a title, whose manifest files exist, whose
    spine references manifest items, and whose spine documents are well-formed XML.
    Raises ConversionError (not retryable) with the first problem."""
    def bad(msg: str) -> ConversionError:
        return ConversionError(f"The converted EPUB is invalid: {msg}", retryable=False)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise bad("not a zip file")
    with zf:
        if zf.testzip() is not None:
            raise bad("corrupt zip member")
        names = zf.namelist()
        if not names or names[0] != "mimetype" or zf.read("mimetype").strip() != b"application/epub+zip":
            raise bad("the first entry must be the 'mimetype' file")
        if zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            raise bad("'mimetype' must be stored uncompressed")
        try:
            container = safe_fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
            opf_path = rootfile.attrib["full-path"]
            opf = safe_fromstring(zf.read(opf_path))
        except (KeyError, AttributeError, ET.ParseError, ValueError):
            raise bad("missing or unreadable container.xml / OPF")
        base = posixpath.dirname(opf_path)
        title = (opf.findtext(f".//{{{DC_NS}}}title") or "").strip()
        if not title:
            raise bad("the OPF has no title")
        manifest = {i.get("id"): i for i in opf.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")}
        have = set(names)
        for i in manifest.values():
            href = posixpath.normpath(posixpath.join(base, unquote(i.get("href", "").split("#")[0])))
            if href not in have:
                raise bad(f"manifest item {i.get('href')} is missing from the archive")
        spine = [r.get("idref") for r in opf.findall(f".//{{{OPF_NS}}}spine/{{{OPF_NS}}}itemref")]
        if not spine:
            raise bad("the spine is empty")
        warnings: list[str] = []
        chars = 0
        for ref in spine:
            item = manifest.get(ref)
            if item is None:
                raise bad(f"spine item {ref} is not in the manifest")
            if item.get("media-type") == "application/xhtml+xml":
                data = zf.read(posixpath.normpath(posixpath.join(base, unquote(item.get("href", "")))))
                try:
                    safe_fromstring(data)
                except (ET.ParseError, ValueError) as exc:
                    raise bad(f"{item.get('href')} is not well-formed XML ({exc})")
                chars += len(data)
        if chars < 50:
            raise bad("the book text is empty")
        cover_id = next((m.get("content") for m in opf.findall(f".//{{{OPF_NS}}}meta") if m.get("name") == "cover"), None)
        has_cover = bool(cover_id and cover_id in manifest) or any("cover-image" in (i.get("properties") or "") for i in manifest.values())
        return {"title": title, "spine_items": len(spine), "has_cover": has_cover, "text_bytes": chars,
                "images": sum(1 for i in manifest.values() if (i.get("media-type") or "").startswith("image/")), "warnings": warnings}
