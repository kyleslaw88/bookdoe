"""Send-to-Kindle (SMTP) and the Kobo kepub download path."""
import pytest

from app import db
from app.services import devices, migration


def _ebook_asset(tmp_path, ext="epub", content=b"fake epub bytes"):
    work_id = migration._find_or_create_stub({"title": "Some Book", "author": "Some Author", "identifiers": {}})
    path = tmp_path / f"book.{ext}"
    path.write_bytes(content)
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,'ebook','adopt',?,?)", (db.uid(), "Books", str(tmp_path), t, t))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook',?, 'owned',1,?,?)",
            (asset_id, work_id, ext, t, t),
        )
        conn.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)",
            (db.uid(), asset_id, str(path), path.stat().st_size, t),
        )
    return work_id, asset_id


def test_smtp_config_round_trips_without_ever_returning_the_password():
    devices.set_smtp_config(host="smtp.example.com", port=587, username="me", password="hunter2", from_email="me@example.com")
    cfg = devices.get_smtp_config()
    assert cfg["host"] == "smtp.example.com"
    assert cfg["configured"] is True
    assert "password" not in cfg and "password_enc" not in cfg


def test_omitting_the_password_on_update_keeps_the_existing_one(monkeypatch):
    devices.set_smtp_config(host="smtp.example.com", port=587, username="me", password="hunter2", from_email="me@example.com")
    devices.set_smtp_config(host="smtp.example.com", port=465, username="me", password=None, from_email="me@example.com")
    assert devices._smtp_password() == "hunter2"


def test_send_to_kindle_requires_smtp_configuration(tmp_path):
    work_id, asset_id = _ebook_asset(tmp_path)
    with pytest.raises(ValueError, match="not configured"):
        devices.send_to_kindle(work_id, asset_id, "kid@kindle.com")


def test_send_to_kindle_rejects_unsupported_formats(tmp_path):
    devices.set_smtp_config(host="smtp.example.com", port=587, username="", password=None, from_email="me@example.com")
    work_id, asset_id = _ebook_asset(tmp_path, ext="cbz")
    with pytest.raises(ValueError, match="does not accept"):
        devices.send_to_kindle(work_id, asset_id, "kid@kindle.com")


def test_send_to_kindle_rejects_an_audiobook_asset(tmp_path):
    devices.set_smtp_config(host="smtp.example.com", port=587, username="", password=None, from_email="me@example.com")
    work_id = migration._find_or_create_stub({"title": "An Audiobook", "author": "X", "identifiers": {}})
    path = tmp_path / "book.m4b"; path.write_bytes(b"audio")
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',1,?,?)", (asset_id, work_id, t, t))
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)", (db.uid(), asset_id, str(path), path.stat().st_size, t))
    with pytest.raises(ValueError, match="ebook"):
        devices.send_to_kindle(work_id, asset_id, "kid@kindle.com")


def test_send_to_kindle_sends_the_file_as_an_attachment(tmp_path, monkeypatch):
    devices.set_smtp_config(host="smtp.example.com", port=587, username="me", password="hunter2", from_email="me@example.com")
    work_id, asset_id = _ebook_asset(tmp_path, content=b"the actual epub bytes")

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["tls"] = True
        def login(self, user, password): sent["login"] = (user, password)
        def send_message(self, msg):
            sent["to"] = msg["To"]
            sent["attachment"] = msg.get_payload()[1].get_payload(decode=True)

    monkeypatch.setattr(devices.smtplib, "SMTP", FakeSMTP)
    result = devices.send_to_kindle(work_id, asset_id, "kid@kindle.com")

    assert result["sent"] is True
    assert sent["host"] == "smtp.example.com" and sent["port"] == 587
    assert sent["tls"] is True
    assert sent["login"] == ("me", "hunter2")
    assert sent["to"] == "kid@kindle.com"
    assert sent["attachment"] == b"the actual epub bytes"
    history = db.one("SELECT event_type FROM history WHERE work_id=? ORDER BY created_at DESC LIMIT 1", (work_id,))
    assert history["event_type"] == "device.sent"


def test_send_to_kindle_requires_a_file_on_disk(tmp_path):
    devices.set_smtp_config(host="smtp.example.com", port=587, username="", password=None, from_email="me@example.com")
    work_id, asset_id = _ebook_asset(tmp_path)
    db.one("SELECT path FROM media_files WHERE asset_id=?", (asset_id,))
    import os
    for row in db.all("SELECT path FROM media_files WHERE asset_id=?", (asset_id,)):
        os.remove(row["path"])
    with pytest.raises(ValueError, match="missing"):
        devices.send_to_kindle(work_id, asset_id, "kid@kindle.com")


def test_kepub_for_asset_converts_the_epub_file(tmp_path):
    import zipfile
    epub_bytes = _minimal_epub()
    work_id, asset_id = _ebook_asset(tmp_path, content=epub_bytes)
    data, filename = devices.kepub_for_asset(work_id, asset_id)
    assert filename.endswith(".kepub.epub")
    zf = zipfile.ZipFile.__new__(zipfile.ZipFile)
    import io
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert "koboSpan" in zf.read("OEBPS/c1.xhtml").decode()


def test_kepub_rejects_non_epub_formats(tmp_path):
    work_id, asset_id = _ebook_asset(tmp_path, ext="pdf")
    with pytest.raises(ValueError, match="epub"):
        devices.kepub_for_asset(work_id, asset_id)


def _minimal_epub() -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>'
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine/></package>',
        )
        z.writestr("OEBPS/c1.xhtml", '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hi.</p></body></html>')
    return buf.getvalue()
