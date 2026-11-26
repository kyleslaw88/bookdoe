"""Ebook conversion: MOBI / AZW / AZW3 to EPUB with a pure-Python unpacker (issue #265).

The fixtures are generated (conv_fixtures.make_mobi hand-assembles a MOBI 6 book), so nothing
binary is checked in. A real KF8 / MOBI 6 pair can be tried by pointing BOOKDOE_TEST_MOBI_DIR at
a folder holding any ``*.mobi`` and ``*.azw3`` files.
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services import convert_audio, convert_ebook, conversion, localmeta, scanner

import conv_fixtures as cf

client = TestClient(main_module.app)
TITLE, AUTHOR = "Tiny Book", "Anne Author"


@pytest.fixture(autouse=True)
def no_background_kick(monkeypatch):
    monkeypatch.setattr(conversion, "kick", lambda kind: None)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def mobi_book(tmp_path, *, drm=False, name="Tiny Book.mobi", mode="managed", title=TITLE):
    rt = cf.root(tmp_path, mode, "ebook")
    f = cf.make_mobi(rt["path"] / AUTHOR / title / name, title=title, author=AUTHOR, drm=drm)
    work_id, asset_id = cf.add_book(rt, title, AUTHOR, "ebook", [f])
    return rt, work_id, asset_id, f


# ---------------------------------------------------------------------------
# Header inspection and DRM
# ---------------------------------------------------------------------------

def test_header_inspection_tells_drm_from_plain_and_rejects_other_formats(tmp_path):
    plain = cf.make_mobi(tmp_path / "a.mobi")
    drm = cf.make_mobi(tmp_path / "b.mobi", drm=True)
    assert convert_ebook.inspect_header(plain) == {"ok": True, "drm": False, "kf8": False, "version": 6, "reason": None}
    got = convert_ebook.inspect_header(drm)
    assert got["ok"] is False and got["drm"] is True and "never removes DRM" in got["reason"]
    junk = tmp_path / "c.azw"
    junk.write_bytes(b"not a kindle book " * 20)
    assert "Not a Mobipocket" in convert_ebook.inspect_header(junk)["reason"]
    tiny = tmp_path / "d.mobi"
    tiny.write_bytes(b"x")
    assert convert_ebook.inspect_header(tiny)["ok"] is False
    kfx = tmp_path / "e.azw"
    kfx.write_bytes(b"CONT" + b"\0" * 200)
    assert "KFX" in convert_ebook.inspect_header(kfx)["reason"]
    assert convert_ebook.convertible(tmp_path / "missing.mobi")[0] is False
    ok, why = convert_ebook.convertible(tmp_path / "book.pdf")
    assert not ok and "PDF" in why and "MOBI, AZW, AZW3" in why


# ---------------------------------------------------------------------------
# The converter
# ---------------------------------------------------------------------------

def test_mobi_becomes_a_valid_epub_with_metadata_and_cover(tmp_path):
    src = cf.make_mobi(tmp_path / "in.mobi", title=TITLE, author=AUTHOR)
    before = sha(src)
    dest = tmp_path / "out.epub"
    report = convert_ebook.to_epub(src, dest)
    assert sha(src) == before, "the source is read, never changed"
    assert report["mode"] == "mobi7-assembled" and report["has_cover"] and report["images"] == 1 and report["title"] == TITLE

    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype" and zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names and "OEBPS/content.opf" in names
        opf = zf.read("OEBPS/content.opf").decode()
        assert "<package" in opf and "opf:package" not in opf          # default namespace, readable by simple parsers
        assert TITLE in opf and AUTHOR in opf and 'name="cover"' in opf
        ncx = zf.read("OEBPS/toc.ncx").decode()
        assert ".html" not in ncx and "book.xhtml" in ncx or "<navPoint" not in ncx
        text = zf.read("OEBPS/book.xhtml").decode()
        assert "Paragraph 29" in text and "Chapter Two" in text and "mbp:" not in text and "The end &amp; more." in text
        assert any(n.startswith("OEBPS/Images/") for n in names)
    convert_ebook.validate_epub(dest)


def test_the_project_reads_the_converted_epub_with_its_own_epub_handling(tmp_path):
    src = cf.make_mobi(tmp_path / "in.mobi", title=TITLE, author=AUTHOR)
    dest = tmp_path / "Tiny Book.epub"
    convert_ebook.to_epub(src, dest)
    detected = scanner.inspect_file(dest, root=tmp_path)
    assert detected["title"] == TITLE and detected["extension"] == "epub" and detected["kind"] == "ebook"
    assert AUTHOR in (detected.get("authors") or [])
    assert detected.get("drm") in (False, None)
    meta = localmeta.read_epub(dest)
    assert meta.get("language")


def test_validation_names_what_is_wrong_with_a_bad_epub(tmp_path):
    def make(path, entries, *, mimetype_compressed=False):
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED if (name != "mimetype" or mimetype_compressed) else zipfile.ZIP_STORED)
        return path

    container = '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/c.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0" unique-identifier="u"><metadata>'
           '<dc:title>T</dc:title></metadata><manifest><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="a"/></spine></package>')
    good_doc = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>' + "word " * 30 + "</p></body></html>"
    base = [("mimetype", "application/epub+zip"), ("META-INF/container.xml", container), ("OEBPS/c.opf", opf), ("OEBPS/a.xhtml", good_doc)]
    assert convert_ebook.validate_epub(make(tmp_path / "ok.epub", base))["spine_items"] == 1

    cases = {
        "first entry must be": make(tmp_path / "1.epub", base[1:] + base[:1]),
        "stored uncompressed": make(tmp_path / "2.epub", base, mimetype_compressed=True),
        "missing from the archive": make(tmp_path / "3.epub", base[:3]),
        "not well-formed": make(tmp_path / "4.epub", base[:3] + [("OEBPS/a.xhtml", "<html><p>unclosed")]),
        "no title": make(tmp_path / "5.epub", base[:2] + [("OEBPS/c.opf", opf.replace("<dc:title>T</dc:title>", "")), base[3]]),
        "unreadable container.xml / OPF": make(tmp_path / "6.epub", [base[0], ("META-INF/container.xml", "nope")]),
    }
    for fragment, path in cases.items():
        with pytest.raises(convert_audio.ConversionError, match=fragment):
            convert_ebook.validate_epub(path)
    bad = tmp_path / "7.epub"
    bad.write_bytes(b"not a zip")
    with pytest.raises(convert_audio.ConversionError, match="not a zip"):
        convert_ebook.validate_epub(bad)


def test_html_is_repaired_into_well_formed_xhtml():
    raw = (b"<html><head><title>x</title><guide><reference href='a'/></guide></head><body>"
           b"<P ALIGN=left onclick='bad()'>One<br>two<IMG src=a.jpg><a name=top>anchor</a></P>"
           b"<mbp:pagebreak/><div>unclosed <b>bold</div><script>alert(1)</script>&nbsp;&copy;</body></html>")
    xhtml = convert_ebook.html_to_xhtml(raw, "T")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xhtml.split("?>", 1)[1].split("\n", 2)[2] if xhtml.startswith("<?xml") and "<!DOCTYPE" in xhtml else xhtml)
    assert root.tag.endswith("html")
    assert "onclick" not in xhtml and "alert" not in xhtml and "<guide" not in xhtml and "<reference" not in xhtml
    assert '<br/>' in xhtml and '<img src="a.jpg"/>' in xhtml and 'id="top"' in xhtml and 'class="mbp_pagebreak"' in xhtml


def test_a_drm_file_is_skipped_by_the_converter_before_it_is_read(tmp_path, monkeypatch):
    drm = cf.make_mobi(tmp_path / "locked.mobi", drm=True)
    called = []
    monkeypatch.setattr(convert_ebook.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(convert_audio.ConversionError, match="DRM") as exc:
        convert_ebook.to_epub(drm, tmp_path / "o.epub")
    assert exc.value.skip and not exc.value.retryable
    assert called == [] and not (tmp_path / "o.epub").exists()


# ---------------------------------------------------------------------------
# Through the queue
# ---------------------------------------------------------------------------

def test_a_mobi_is_converted_to_a_derived_epub_and_the_original_is_kept(tmp_path):
    rt, work_id, asset_id, src = mobi_book(tmp_path)
    before = sha(src)
    opt = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))])[asset_id]
    assert opt["state"] == "available" and opt["target"] == "epub" and opt["conversion"] is None
    conversion.enqueue(asset_id, user={"username": "ada"})
    out = conversion.run_queue("ebook")
    assert out["done"] == 1
    conv = db.one("SELECT * FROM conversions")
    final = Path(conv["output_path"])
    assert conv["status"] == "done" and final == rt["path"] / AUTHOR / TITLE / "Tiny Book.epub" and final.is_file()
    assert sha(src) == before, "the MOBI is untouched"
    assert not list(rt["path"].rglob(".*partial"))
    d = db.one("SELECT * FROM media_assets WHERE id=?", (conv["output_asset_id"],))
    assert (d["kind"], d["format"], d["status"], d["monitored"], d["has_acquisition_intent"], d["derived_from_asset_id"]) == \
           ("ebook", "epub", "owned", 0, 0, asset_id)
    assert "epub" in d["derived_by"].lower() or "bookdoe-convert" in d["derived_by"]
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=?", (asset_id,))["n"] == 1

    # Management labels the copy; the Player offers it only to a client that asks for EPUB
    assets = {a["id"]: a for a in client.get(f"/api/v1/library/{work_id}").json()["assets"]}
    assert assets[conv["output_asset_id"]]["derived_label"] == "Converted from MOBI"
    assert assets[asset_id]["convert"]["state"] == "converted"
    # the converted copy is now a fully readable ebook for the reader
    from app.services import reader
    manifest = reader.manifest(work_id, conv["output_asset_id"], {"id": "u"})
    assert manifest["format"] == "epub" and manifest["files"][0]["size_bytes"] == final.stat().st_size


def test_drm_protected_books_are_skipped_with_a_clear_status(tmp_path):
    rt, work_id, asset_id, src = mobi_book(tmp_path, drm=True)
    opt = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))])[asset_id]
    assert opt["state"] == "blocked" and opt["code"] == "drm" and "never removes DRM" in opt["reason"]
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "drm"
    # if it is queued anyway (the file became locked later), the run records a skip, not a failure
    with db.tx() as conn:
        conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,created_at) VALUES (?,?,?,?,?,'queued',?)",
                     (db.uid(), "ebook", work_id, asset_id, "epub", db.now()))
    out = conversion.run_queue("ebook")
    row = db.one("SELECT status,error FROM conversions")
    assert out["skipped"] == 1 and row["status"] == "skipped" and "DRM" in row["error"]
    assert not list(rt["path"].rglob("*.epub"))
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE derived_from_asset_id IS NOT NULL")["n"] == 0


def test_unsupported_formats_are_reported_cleanly(tmp_path):
    rt = cf.root(tmp_path, "managed", "ebook")
    for ext in ("pdf", "cbz", "fb2"):
        f = rt["path"] / ext / f"Book.{ext}"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4 x" * 20)
        _, asset_id = cf.add_book(rt, f"Book {ext}", AUTHOR, "ebook", [f])
        opt = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))])[asset_id]
        assert opt["state"] == "blocked" and opt["code"] == "unsupported" and ext.upper() in opt["reason"]
        r = client.post(f"/api/v1/assets/{asset_id}/convert", json={})
        assert r.status_code == 409 and ext.upper() in r.json()["detail"]
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0


def test_a_book_in_an_adopted_root_with_no_managed_root_is_not_converted(tmp_path):
    rt, work_id, asset_id, src = mobi_book(tmp_path, mode="adopt")
    r = client.post(f"/api/v1/assets/{asset_id}/convert", json={})
    assert r.status_code == 409 and "read-only" in r.json()["detail"] and "managed ebook" in r.json()["detail"]
    assert [p.name for p in src.parent.iterdir()] == [src.name]


def test_ebook_conversions_run_in_the_ebook_queue_only(tmp_path):
    rt, work_id, asset_id, src = mobi_book(tmp_path)
    conversion.enqueue(asset_id)
    assert conversion.run_queue("audio")["processed"] == 0
    assert db.one("SELECT status FROM conversions")["status"] == "queued"
    assert conversion.run_queue("ebook")["done"] == 1


@pytest.mark.skipif(not os.environ.get("BOOKDOE_TEST_MOBI_DIR"), reason="set BOOKDOE_TEST_MOBI_DIR to a folder with real *.mobi / *.azw3 files")
def test_real_world_files_convert_to_a_valid_epub(tmp_path):
    folder = Path(os.environ["BOOKDOE_TEST_MOBI_DIR"])
    found = [p for p in sorted(folder.glob("*.mobi")) + sorted(folder.glob("*.azw3")) if "drm" not in p.name.lower()]
    assert found
    for i, src in enumerate(found):
        report = convert_ebook.to_epub(src, tmp_path / f"{i}.epub")
        assert report["spine_items"] >= 1 and report["text_bytes"] > 1000
