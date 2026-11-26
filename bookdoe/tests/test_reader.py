"""In-browser reader and audiobook player: the manifest the page opens from, the
Range-capable file route it streams from, per-account positions, and the kids boundary.

The browser side (epub.js rendering, the <audio> element) is not under test here; what
is under test is every byte and every position the server hands out or stores.
"""
import dataclasses
import os

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import migration, reader, tagging

import helpers

app = main_module.app


def _root(tmp_path, kind="ebook"):
    t = db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)",
                (db.uid(), "Root", str(tmp_path), kind, t, t),
            )


def _asset(work_id, kind, fmt):
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'owned',0,?,?)",
            (aid, work_id, kind, fmt, t, t),
        )
    return aid


def _file(asset_id, path, *, part=None, duration=None, missing=0):
    fid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at)"
            " VALUES (?,?,?,?,?,?,'{}',1,0,?,?)",
            (fid, asset_id, str(path), part, os.path.getsize(path) if os.path.exists(path) else 0, duration, missing, t),
        )
    return fid


def _epub_book(tmp_path, content=b"PK\x03\x04 fake epub bytes " * 100, title="Readable Book"):
    _root(tmp_path)
    work_id = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = _asset(work_id, "ebook", "epub")
    p = tmp_path / f"{title}.epub"
    p.write_bytes(content)
    fid = _file(aid, p)
    return work_id, aid, fid, p


def _audiobook(tmp_path, parts=3):
    _root(tmp_path, "audiobook")
    work_id = migration._find_or_create_stub({"title": "Spoken Book", "author": "Author", "identifiers": {}})
    aid = _asset(work_id, "audiobook", "mp3")
    fids = []
    # Inserted out of order on purpose: the manifest must order by part_number.
    for n in reversed(range(1, parts + 1)):
        p = tmp_path / f"part{n}.mp3"
        p.write_bytes(b"ID3" + bytes([n]) * 50)
        fids.append((n, _file(aid, p, part=n, duration=600)))
    return work_id, aid, dict(fids)


# ---------- schema ----------
def test_migration_15_creates_media_progress_table():
    cols = {r["name"] for r in db.all("PRAGMA table_info(media_progress)")}
    assert {"asset_id", "user_id", "work_id", "kind", "file_id", "position", "location", "percent", "updated_at"} <= cols
    assert db.one("SELECT 1 FROM migration_steps WHERE version=15") is not None


# ---------- manifest ----------
def test_epub_manifest_lists_the_file_and_no_position_yet(tmp_path):
    work_id, aid, fid, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/read/{aid}")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["kind"] == "ebook" and m["format"] == "epub"
    assert [f["id"] for f in m["files"]] == [fid]
    assert m["files"][0]["url"] == f"/api/v1/library/{work_id}/files/{fid}"
    assert m["progress"] is None
    assert m["chapters"] == []


def test_manifest_refuses_an_ebook_format_the_browser_cannot_render(tmp_path):
    _root(tmp_path)
    work_id = migration._find_or_create_stub({"title": "Mobi Only", "author": "A", "identifiers": {}})
    aid = _asset(work_id, "ebook", "mobi")
    p = tmp_path / "book.mobi"; p.write_bytes(b"BOOKMOBI")
    _file(aid, p)
    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/read/{aid}")
    assert r.status_code == 400
    assert "EPUB or PDF" in r.json()["detail"]


def test_manifest_404s_for_an_unknown_asset(tmp_path):
    work_id, _, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_id}/read/nope").status_code == 400
        assert c.get("/api/v1/library/nope/read/nope").status_code == 400


def test_audiobook_manifest_orders_tracks_by_part_number(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "_ffprobe_chapters", lambda path: [])
    work_id, aid, fids = _audiobook(tmp_path)
    with TestClient(app) as c:
        m = c.get(f"/api/v1/library/{work_id}/read/{aid}").json()
    assert m["kind"] == "audiobook"
    assert [f["part_number"] for f in m["files"]] == [1, 2, 3]
    assert [f["id"] for f in m["files"]] == [fids[1], fids[2], fids[3]]
    assert [f["title"] for f in m["files"]] == ["part1", "part2", "part3"]
    # Chapter markers are only probed for a single-file book; a multi-file one
    # navigates by track.
    assert m["chapters"] == []


def test_single_file_audiobook_exposes_embedded_chapters(tmp_path, monkeypatch):
    probed = []
    def fake(path):
        probed.append(path)
        return [{"index": 0, "title": "Opening", "start": 0.0, "end": 120.5}, {"index": 1, "title": "Chapter 2", "start": 120.5, "end": 600.0}]
    monkeypatch.setattr(reader, "_ffprobe_chapters", fake)
    reader._CHAPTER_CACHE.clear()
    work_id, aid, fids = _audiobook(tmp_path, parts=1)
    with TestClient(app) as c:
        m = c.get(f"/api/v1/library/{work_id}/read/{aid}").json()
        again = c.get(f"/api/v1/library/{work_id}/read/{aid}").json()
    assert [ch["title"] for ch in m["chapters"]] == ["Opening", "Chapter 2"]
    assert m["files"][0]["title"] == "Full book"
    assert again["chapters"] == m["chapters"]
    assert len(probed) == 1, "an unchanged file is probed once, then served from the cache"


def test_chapters_are_empty_when_ffprobe_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(reader.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ffprobe")))
    reader._CHAPTER_CACHE.clear()
    p = tmp_path / "x.m4b"; p.write_bytes(b"x")
    assert reader.chapters_for_file(p) == []


# ---------- file streaming ----------
def test_file_route_serves_the_bytes_inline_with_the_right_type(tmp_path):
    work_id, aid, fid, p = _epub_book(tmp_path)
    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/files/{fid}")
    assert r.status_code == 200
    assert r.content == p.read_bytes()
    assert r.headers["content-type"].startswith("application/epub+zip")
    assert r.headers["content-disposition"].startswith("inline;")
    assert "no-store" in r.headers["cache-control"]


def test_file_route_honours_range_requests_so_audio_can_seek(tmp_path):
    work_id, aid, fid, p = _epub_book(tmp_path, content=bytes(range(256)) * 4)
    with TestClient(app) as c:
        r = c.get(f"/api/v1/library/{work_id}/files/{fid}", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206, r.text
    assert r.content == bytes(range(10, 20))
    assert r.headers["content-range"] == f"bytes 10-19/{p.stat().st_size}"
    assert r.headers.get("accept-ranges") == "bytes"


def test_file_route_404s_for_a_file_that_belongs_to_another_work(tmp_path):
    work_a, _, fid_a, _ = _epub_book(tmp_path, title="Book A")
    work_b, _, _, _ = _epub_book(tmp_path, title="Book B")
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_b}/files/{fid_a}").status_code == 404
        assert c.get(f"/api/v1/library/{work_a}/files/{fid_a}").status_code == 200


def test_file_route_404s_for_a_file_marked_missing(tmp_path):
    work_id, aid, fid, p = _epub_book(tmp_path)
    with db.tx() as conn:
        conn.execute("UPDATE media_files SET missing=1 WHERE id=?", (fid,))
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_id}/files/{fid}").status_code == 404


def test_file_route_refuses_a_symlink_that_escapes_the_root(tmp_path):
    """Same guard as /opds/files: the row's path is inside the root, the file it
    resolves to is not. Neither the stream nor the manifest may hand it out."""
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside.epub"; outside.write_bytes(b"secret")
    _root(root)
    work_id = migration._find_or_create_stub({"title": "Escapee", "author": "A", "identifiers": {}})
    aid = _asset(work_id, "ebook", "epub")
    link = root / "book.epub"; link.symlink_to(outside)
    fid = _file(aid, link)
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_id}/files/{fid}").status_code == 403
        r = c.get(f"/api/v1/library/{work_id}/read/{aid}")
    assert r.status_code == 400
    assert "outside" in r.json()["detail"]


def test_file_route_404s_when_no_root_contains_the_file(tmp_path):
    work_id, aid, fid, p = _epub_book(tmp_path)
    with db.tx() as conn:
        conn.execute("UPDATE root_folders SET enabled=0")
    with TestClient(app) as c:
        assert c.get(f"/api/v1/library/{work_id}/files/{fid}").status_code == 403


# ---------- progress ----------
def test_progress_round_trips_and_shows_up_on_the_work(tmp_path):
    work_id, aid, fid, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"location": "epubcfi(/6/14[chap05]!/4/2/14/1:0)", "percent": 0.4215})
        assert r.status_code == 200, r.text
        assert r.json()["location"].startswith("epubcfi(") and r.json()["percent"] == 0.4215

        g = c.get(f"/api/v1/library/{work_id}/progress/{aid}").json()
        assert g["progress"]["location"] == "epubcfi(/6/14[chap05]!/4/2/14/1:0)"
        assert g["progress"]["file_id"] is None

        m = c.get(f"/api/v1/library/{work_id}/read/{aid}").json()
        assert m["progress"]["percent"] == 0.4215

        w = c.get(f"/api/v1/library/{work_id}").json()
        assert w["progress"] == [dict(w["progress"][0])]
        assert w["progress"][0]["asset_id"] == aid and w["progress"][0]["kind"] == "ebook"

        # A later save replaces, never duplicates (one row per account per asset).
        c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"location": "epubcfi(/6/20!/4/2/1:0)", "percent": 0.9})
        assert db.one("SELECT COUNT(*) AS n FROM media_progress")["n"] == 1
        assert c.get(f"/api/v1/library/{work_id}/progress/{aid}").json()["progress"]["percent"] == 0.9

        d = c.delete(f"/api/v1/library/{work_id}/progress/{aid}")
        assert d.status_code == 200 and d.json()["progress"] is None
        assert c.get(f"/api/v1/library/{work_id}/progress/{aid}").json()["progress"] is None


def test_audio_progress_names_the_track(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "_ffprobe_chapters", lambda path: [])
    work_id, aid, fids = _audiobook(tmp_path)
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"file_id": fids[2], "position": 311.4, "percent": 0.5})
        assert r.status_code == 200, r.text
        m = c.get(f"/api/v1/library/{work_id}/read/{aid}").json()
    assert m["progress"]["file_id"] == fids[2]
    assert m["progress"]["position"] == 311.4


def test_progress_rejects_a_track_from_a_different_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "_ffprobe_chapters", lambda path: [])
    work_id, aid, fids = _audiobook(tmp_path)
    other_work, other_aid, other_fid, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"file_id": other_fid, "position": 1})
        assert r.status_code == 400
        assert "belong" in r.json()["detail"]
        assert c.put(f"/api/v1/library/{work_id}/progress/{other_aid}", json={"position": 1}).status_code == 404  # same answer as GET: an asset that is not under this work is just not found
        assert c.get(f"/api/v1/library/{work_id}/progress/{other_aid}").status_code == 404


def test_progress_validates_its_body(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        assert c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"percent": 1.5}).status_code == 422
        assert c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"position": -3}).status_code == 422
        assert c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"bookmark": "x"}).status_code == 422
        assert c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"location": "x" * 2001}).status_code == 422


def test_progress_is_per_account(tmp_path):
    work_id, aid, fid, _ = _epub_book(tmp_path)
    a = auth_module.create_local_user("alice", "a-very-long-password-1", is_admin=False)
    b = auth_module.create_local_user("bob", "a-very-long-password-2", is_admin=False)
    reader.set_progress(work_id, aid, a, file_id=None, position=None, location="epubcfi(/6/2!/4/1:0)", percent=0.1)
    reader.set_progress(work_id, aid, b, file_id=None, position=None, location="epubcfi(/6/40!/4/1:0)", percent=0.8)
    assert reader.get_progress(aid, a)["percent"] == 0.1
    assert reader.get_progress(aid, b)["percent"] == 0.8
    assert reader.progress_for_work(work_id, a)[0]["location"] == "epubcfi(/6/2!/4/1:0)"
    reader.clear_progress(aid, a)
    assert reader.get_progress(aid, a) is None
    assert reader.get_progress(aid, b)["percent"] == 0.8


def test_progress_rows_go_with_their_asset(tmp_path):
    auth_module.ensure_anonymous_user()  # normally the app lifespan's job; no TestClient here
    work_id, aid, fid, _ = _epub_book(tmp_path)
    reader.set_progress(work_id, aid, "anonymous", file_id=fid, position=0, location=None, percent=0.2)
    with db.tx() as conn:
        conn.execute("DELETE FROM media_files WHERE id=?", (fid,))
    assert reader.get_progress(aid, "anonymous")["file_id"] is None, "a deleted track leaves the position, not a dangling id"
    with db.tx() as conn:
        conn.execute("DELETE FROM media_assets WHERE id=?", (aid,))
    assert reader.get_progress(aid, "anonymous") is None


# ---------- kids boundary ----------
@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


@pytest.fixture
def household(local_mode, tmp_path):
    """A kids session plus one kid-visible and one kid-invisible readable book, each
    with a file on disk inside an enabled root. Same shape as test_kids_access_control."""
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert r.status_code == 200, r.text
        r = admin.post("/api/v1/users", json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"},
                       headers={"X-CSRF-Token": r.json()["csrf"]})
        assert r.status_code == 200, r.text
        kids = _epub_book(tmp_path, title="Kids Book")
        tagging.add_tag(kids[0], tagging.kids_tag_id())
        adult = _epub_book(tmp_path, title="Adult Book")
        kid = TestClient(app)
        r = kid.post("/api/v1/auth/login", json={"username": "junior", "password": "a-very-long-kid-password"})
        assert r.status_code == 200, r.text
        yield {"kid": kid, "kid_csrf": r.json()["csrf"], "kids": kids, "adult": adult}


def test_kid_can_read_and_bookmark_a_book_on_the_kids_shelf(household):
    kid, csrf = household["kid"], household["kid_csrf"]
    work_id, aid, fid, p = household["kids"]
    assert kid.get(f"/api/v1/library/{work_id}/read/{aid}").status_code == 200
    r = kid.get(f"/api/v1/library/{work_id}/files/{fid}")
    assert r.status_code == 200 and r.content == p.read_bytes()
    r = kid.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"location": "epubcfi(/6/4!/4/1:0)", "percent": 0.05}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert kid.get(f"/api/v1/library/{work_id}/progress/{aid}").json()["progress"]["percent"] == 0.05


def test_kid_gets_404_for_every_reader_surface_of_an_untagged_book(household):
    kid, csrf = household["kid"], household["kid_csrf"]
    work_id, aid, fid, _ = household["adult"]
    assert kid.get(f"/api/v1/library/{work_id}/read/{aid}").status_code == 404
    assert kid.get(f"/api/v1/library/{work_id}/files/{fid}").status_code == 404
    assert kid.get(f"/api/v1/library/{work_id}/progress/{aid}").status_code == 404
    assert kid.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"percent": 0.5}, headers={"X-CSRF-Token": csrf}).status_code == 404
    assert kid.delete(f"/api/v1/library/{work_id}/progress/{aid}", headers={"X-CSRF-Token": csrf}).status_code == 404
    assert db.one("SELECT COUNT(*) AS n FROM media_progress")["n"] == 0


def test_progress_write_needs_a_csrf_token_on_a_cookie_session(household):
    kid = household["kid"]
    work_id, aid, _, _ = household["kids"]
    assert kid.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"percent": 0.5}).status_code == 403


def test_reader_routes_require_a_signed_in_user(local_mode, tmp_path):
    work_id, aid, fid, _ = _epub_book(tmp_path)
    with TestClient(app) as anon:
        assert anon.get(f"/api/v1/library/{work_id}/read/{aid}").status_code == 401
        assert anon.get(f"/api/v1/library/{work_id}/files/{fid}").status_code == 401
        assert anon.get(f"/api/v1/library/{work_id}/progress/{aid}").status_code == 401
