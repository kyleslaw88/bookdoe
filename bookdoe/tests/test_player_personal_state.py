"""Phase 2: per-account state behind the Player (progress sync rule, bookmarks,
favorites, preferences, continue/recent/home) and that none of it leaks between
accounts or past the kid visibility boundary.
"""
import dataclasses

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
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)",
                         (db.uid(), "Root", str(tmp_path), kind, t, t))


def _book(tmp_path, title, kind="ebook", ext="epub"):
    _root(tmp_path, kind)
    work_id = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = helpers.own(work_id, kind, ext)
    p = tmp_path / f"{title}.{ext}"
    p.write_bytes(b"PK\x03\x04 bytes " * 50)
    fid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,?,'{}',1,0,0,?)",
                     (fid, aid, str(p), p.stat().st_size, 3600 if kind == "audiobook" else None, t))
    return work_id, aid, fid


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


@pytest.fixture
def family(local_mode, tmp_path):
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=h)
        kids = _book(tmp_path, "Kids Book")
        tagging.add_tag(kids[0], tagging.kids_tag_id())
        adult = _book(tmp_path, "Adult Book")
        audio = _book(tmp_path, "Spoken Book", "audiobook", "mp3")
        sessions = {}
        for name in ("jane", "owen"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            sessions[name] = (c, {"X-CSRF-Token": r.json()["csrf"]})
        yield {"admin": (admin, h), "jane": sessions["jane"], "owen": sessions["owen"], "kids": kids, "adult": adult, "audio": audio}


# ---------- schema ----------
def test_migration_17_adds_sync_columns_and_tables():
    cols = {r["name"] for r in db.all("PRAGMA table_info(media_progress)")}
    assert {"device_id", "client_updated_at", "duration"} <= cols
    for t in ("bookmarks", "favorites", "user_preferences"):
        assert db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)), t
    assert db.one("SELECT row_counts_json FROM migration_steps WHERE version=17")


# ---------- progress conflict rule ----------
def test_newer_client_timestamp_wins_and_stale_update_is_reported_not_applied(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    url = f"/api/v1/library/{w}/progress/{a}"
    r = c.put(url, json={"location": "epubcfi(/6/2!/4/1:0)", "percent": 0.1, "device_id": "phone", "client_updated_at": 1000.0}, headers=h)
    assert r.status_code == 200 and r.json()["applied"] is True
    r = c.put(url, json={"location": "epubcfi(/6/8!/4/1:0)", "percent": 0.4, "device_id": "tablet", "client_updated_at": 2000.0}, headers=h)
    assert r.json()["applied"] is True
    # The phone was offline and now sends an older position: refused, current state returned.
    r = c.put(url, json={"location": "epubcfi(/6/4!/4/1:0)", "percent": 0.2, "device_id": "phone", "client_updated_at": 1500.0}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is False and body["percent"] == 0.4 and body["device_id"] == "tablet"
    # Same timestamp twice (a retry) is a no-op, not an error.
    r = c.put(url, json={"location": "epubcfi(/6/8!/4/1:0)", "percent": 0.4, "device_id": "tablet", "client_updated_at": 2000.0}, headers=h)
    assert r.json()["applied"] is False
    assert reader.get_progress(a, c.get("/api/v1/auth/me").json()["user"]["id"])["percent"] == 0.4


def test_updates_without_a_client_clock_still_apply(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    url = f"/api/v1/library/{w}/progress/{a}"
    c.put(url, json={"location": "a", "percent": 0.5, "client_updated_at": 5000.0}, headers=h)
    r = c.put(url, json={"location": "b", "percent": 0.6}, headers=h)   # legacy client
    assert r.json()["applied"] is True and c.get(url).json()["progress"]["percent"] == 0.6


def test_future_client_clocks_are_clamped(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    url = f"/api/v1/library/{w}/progress/{a}"
    r = c.put(url, json={"location": "a", "percent": 0.5, "client_updated_at": db.now() + 10 ** 7}, headers=h)
    assert r.json()["client_updated_at"] <= db.now() + reader.CLIENT_CLOCK_SLACK + 1
    # An honest update a minute later therefore still wins.
    r = c.put(url, json={"location": "b", "percent": 0.6, "client_updated_at": db.now() + reader.CLIENT_CLOCK_SLACK + 60}, headers=h)
    assert r.json()["applied"] is True


def test_audio_progress_keeps_duration_and_device(family):
    c, h = family["jane"]; w, a, f = family["audio"]
    r = c.put(f"/api/v1/library/{w}/progress/{a}", json={"file_id": f, "position": 120.5, "percent": 0.03, "duration": 3600, "device_id": "esp32-1", "client_updated_at": 10.0}, headers=h)
    assert r.status_code == 200, r.text
    g = c.get(f"/api/v1/library/{w}/progress/{a}").json()["progress"]
    assert g["duration"] == 3600 and g["device_id"] == "esp32-1" and g["position"] == 120.5
    # A later update without duration keeps the known one.
    c.put(f"/api/v1/library/{w}/progress/{a}", json={"file_id": f, "position": 200, "client_updated_at": 20.0}, headers=h)
    assert c.get(f"/api/v1/library/{w}/progress/{a}").json()["progress"]["duration"] == 3600


def test_one_account_cannot_read_or_move_another_accounts_progress(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]; w, a, _ = family["kids"]
    jane.put(f"/api/v1/library/{w}/progress/{a}", json={"location": "jane-here", "percent": 0.7}, headers=jh)
    owen.put(f"/api/v1/library/{w}/progress/{a}", json={"location": "owen-here", "percent": 0.2}, headers=oh)
    assert jane.get(f"/api/v1/library/{w}/progress/{a}").json()["progress"]["location"] == "jane-here"
    assert owen.get(f"/api/v1/library/{w}/progress/{a}").json()["progress"]["location"] == "owen-here"
    assert owen.get("/api/player/v1/continue").json()["items"][0]["percent"] == 0.2
    assert all(i["percent"] == 0.7 for i in jane.get("/api/player/v1/continue").json()["items"])


def test_player_delete_progress_marks_a_book_unread(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    c.put(f"/api/player/v1/progress/{a}", json={"asset_id": a, "location": "epubcfi(/6/4!/4/1:0)", "percent": 0.3}, headers=h)
    assert any(i["asset_id"] == a for i in c.get("/api/player/v1/continue").json()["items"])
    r = c.delete(f"/api/player/v1/progress/{a}", headers=h)
    assert r.status_code == 200 and r.json()["progress"] is None and r.json()["work_id"] == w
    assert c.get(f"/api/player/v1/progress/{a}").json()["progress"] is None
    assert not any(i["asset_id"] == a for i in c.get("/api/player/v1/continue").json()["items"])
    # Clearing again (a retry, or nothing there) is not an error.
    assert c.delete(f"/api/player/v1/progress/{a}", headers=h).status_code == 200


def test_player_delete_progress_only_touches_the_callers_row_and_shelf(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    kw, ka, _ = family["kids"]; aw, aa, _ = family["adult"]
    jane.put(f"/api/player/v1/progress/{ka}", json={"asset_id": ka, "location": "jane", "percent": 0.7}, headers=jh)
    owen.put(f"/api/player/v1/progress/{ka}", json={"asset_id": ka, "location": "owen", "percent": 0.2}, headers=oh)
    assert owen.delete(f"/api/player/v1/progress/{ka}", headers=oh).status_code == 200
    assert jane.get(f"/api/player/v1/progress/{ka}").json()["progress"]["location"] == "jane"
    # A kid account cannot learn about, or touch, a book that is off its shelf.
    assert owen.delete(f"/api/player/v1/progress/{aa}", headers=oh).status_code == 404
    # Writes need the CSRF token, like every other progress write.
    assert jane.delete(f"/api/player/v1/progress/{ka}").status_code == 403


# ---------- highlights (bookmarks of kind 'highlight') ----------
def test_migration_42_adds_kind_color_and_updated_at():
    cols = {r["name"] for r in db.all("PRAGMA table_info(bookmarks)")}
    assert {"kind", "color", "updated_at"} <= cols
    assert db.one("SELECT name FROM migration_steps WHERE version=42")["name"] == "bookmark_kinds"


def test_highlights_are_bookmarks_with_a_kind_color_and_note(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    r = c.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "epubcfi(/6/4!/4/2,/1:10,/1:42)", "percent": 0.2,
                                                 "title": "the selected words", "kind": "highlight", "color": "yellow", "note": "check this"}, headers=h)
    assert r.status_code == 201, r.text
    hl = r.json()
    assert (hl["kind"], hl["color"], hl["note"], hl["title"]) == ("highlight", "yellow", "check this", "the selected words")
    plain = c.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "epubcfi(/6/2!/4/1:0)"}, headers=h).json()
    assert plain["kind"] == "bookmark" and plain["color"] is None
    both = c.get(f"/api/player/v1/bookmarks?asset_id={a}").json()
    assert both["total"] == 2
    assert [i["id"] for i in c.get(f"/api/player/v1/bookmarks?asset_id={a}&kind=highlight").json()["items"]] == [hl["id"]]
    assert [i["id"] for i in c.get(f"/api/player/v1/bookmarks?asset_id={a}&kind=bookmark").json()["items"]] == [plain["id"]]
    assert c.get(f"/api/player/v1/bookmarks?kind=nonsense").status_code == 422


def test_a_highlight_needs_a_passage_and_a_sane_color(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    assert c.post("/api/player/v1/bookmarks", json={"asset_id": a, "position": 5.0, "kind": "highlight"}, headers=h).status_code == 400
    assert c.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "x", "kind": "highlight", "color": "red; drop table"}, headers=h).status_code == 422
    assert c.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "x", "kind": "scribble"}, headers=h).status_code == 422


def test_patch_changes_only_the_fields_sent_and_only_for_the_owner(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]; w, a, _ = family["adult"]
    b = jane.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "epubcfi(/6/4)", "title": "t", "note": "first", "kind": "highlight", "color": "yellow"}, headers=jh).json()
    url = f"/api/player/v1/bookmarks/{b['id']}"
    r = jane.patch(url, json={"note": "second"}, headers=jh)
    assert r.status_code == 200 and r.json()["note"] == "second" and r.json()["color"] == "yellow" and r.json()["title"] == "t"
    assert r.json()["updated_at"] >= b["updated_at"]
    r = jane.patch(url, json={"color": "green", "note": None}, headers=jh)
    assert r.json()["color"] == "green" and r.json()["note"] is None
    assert jane.patch(url, json={}, headers=jh).status_code == 400
    assert jane.patch(url, json={"kind": "bookmark"}, headers=jh).status_code == 422      # the kind never changes
    assert jane.patch(url, json={"note": "x"}).status_code == 403                          # CSRF
    assert owen.patch(url, json={"note": "mine now"}, headers=oh).status_code == 404       # not his
    assert jane.patch("/api/player/v1/bookmarks/nope", json={"note": "x"}, headers=jh).status_code == 404
    assert jane.get(f"/api/player/v1/bookmarks?asset_id={a}").json()["items"][0]["note"] is None


def test_sync_reports_an_edited_bookmark_again(family):
    c, h = family["jane"]; w, a, _ = family["adult"]
    b = c.post("/api/player/v1/bookmarks", json={"asset_id": a, "location": "epubcfi(/6/4)", "kind": "highlight"}, headers=h).json()
    first = c.get("/api/player/v1/sync").json()
    assert [x["id"] for x in first["bookmarks"]] == [b["id"]]
    after_create = c.get(f"/api/player/v1/sync?since={first['server_time']}").json()
    assert after_create["bookmarks"] == []
    c.patch(f"/api/player/v1/bookmarks/{b['id']}", json={"note": "edited"}, headers=h)
    again = c.get(f"/api/player/v1/sync?since={first['server_time']}").json()
    assert [x["note"] for x in again["bookmarks"]] == ["edited"]


def test_capabilities_advertise_highlights():
    with TestClient(app) as anon:
        assert "highlights" in anon.get("/api/player/v1/capabilities").json()["features"]


# ---------- continue / recent / home ----------
def test_continue_lists_only_in_progress_visible_books_split_by_kind(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    (wk, ak, _), (wa, aa, _), (ws, as_, fs) = family["kids"], family["adult"], family["audio"]
    jane.put(f"/api/v1/library/{wa}/progress/{aa}", json={"location": "x", "percent": 0.3}, headers=jh)
    jane.put(f"/api/v1/library/{ws}/progress/{as_}", json={"file_id": fs, "position": 10, "percent": 0.01}, headers=jh)
    jane.put(f"/api/v1/library/{wk}/progress/{ak}", json={"location": "end", "percent": 1.0}, headers=jh)  # finished: not "continue"
    home = jane.get("/api/player/v1/home").json()
    assert [i["title"] for i in home["continue_reading"]] == ["Adult Book"]
    assert [i["title"] for i in home["continue_listening"]] == ["Spoken Book"]
    assert {i["title"] for i in home["recently_added"]} == {"Kids Book", "Adult Book", "Spoken Book"}
    assert home["user"]["account_type"] == "adult"
    # Kid: only the kids shelf, even for books the kid somehow has progress on.
    reader.set_progress(wa, aa, owen.get("/api/v1/auth/me").json()["user"]["id"], file_id=None, position=None, location="x", percent=0.5)
    oh_home = owen.get("/api/player/v1/home").json()
    assert oh_home["continue_reading"] == [] and [i["title"] for i in oh_home["recently_added"]] == ["Kids Book"]
    assert owen.get("/api/player/v1/continue?kind=ebook").json()["items"] == []


def test_recent_opened_follows_the_viewer_and_visibility(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    wa, aa, _ = family["adult"]
    jane.put(f"/api/v1/library/{wa}/progress/{aa}", json={"location": "x", "percent": 0.3}, headers=jh)
    assert [w["title"] for w in jane.get("/api/player/v1/recent").json()["recently_opened"]] == ["Adult Book"]
    assert owen.get("/api/player/v1/recent").json()["recently_opened"] == []


def test_etag_round_trip_gives_304(family):
    jane, _ = family["jane"]
    r = jane.get("/api/player/v1/home")
    tag = r.headers["ETag"]
    assert r.headers["Cache-Control"] == "private, no-cache"
    r2 = jane.get("/api/player/v1/home", headers={"If-None-Match": tag})
    assert r2.status_code == 304 and r2.headers["ETag"] == tag


# ---------- bookmarks ----------
def test_bookmarks_are_owned_and_bounded_by_visibility(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    wa, aa, _ = family["adult"]; wk, ak, _ = family["kids"]
    r = jane.post("/api/player/v1/bookmarks", json={"asset_id": aa, "location": "epubcfi(/6/2!/4/1:0)", "percent": 0.1, "title": "Chapter 1"}, headers=jh)
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert jane.get(f"/api/player/v1/bookmarks?asset_id={aa}").json()["total"] == 1
    # Owen cannot see, create on, or delete bookmarks of a book off the kids shelf.
    assert owen.get(f"/api/player/v1/bookmarks?asset_id={aa}").json()["total"] == 0
    assert owen.post("/api/player/v1/bookmarks", json={"asset_id": aa, "location": "x"}, headers=oh).status_code == 404
    assert owen.delete(f"/api/player/v1/bookmarks/{bid}", headers=oh).status_code == 404
    assert jane.get(f"/api/player/v1/bookmarks?asset_id={aa}").json()["total"] == 1
    # Owen bookmarks his own book; Jane does not see it.
    assert owen.post("/api/player/v1/bookmarks", json={"asset_id": ak, "location": "y"}, headers=oh).status_code == 201
    assert jane.get("/api/player/v1/bookmarks").json()["total"] == 1
    assert jane.post("/api/player/v1/bookmarks", json={"asset_id": aa}, headers=jh).status_code == 400
    assert jane.delete(f"/api/player/v1/bookmarks/{bid}", headers=jh).status_code == 200
    assert jane.get("/api/player/v1/bookmarks").json()["total"] == 0


# ---------- favorites ----------
def test_favorites_are_per_account_and_visibility_bound(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    wa, _, _ = family["adult"]; wk, _, _ = family["kids"]
    assert jane.put(f"/api/player/v1/favorites/{wa}", headers=jh).json()["favorite"] is True
    assert owen.put(f"/api/player/v1/favorites/{wa}", headers=oh).status_code == 404
    assert owen.put(f"/api/player/v1/favorites/{wk}", headers=oh).status_code == 200
    assert [w["title"] for w in jane.get("/api/player/v1/favorites").json()["items"]] == ["Adult Book"]
    assert [w["title"] for w in owen.get("/api/player/v1/favorites").json()["items"]] == ["Kids Book"]
    assert jane.get("/api/player/v1/home").json()["favorites"] == 1
    assert jane.delete(f"/api/player/v1/favorites/{wa}", headers=jh).json()["favorite"] is False
    assert jane.get("/api/player/v1/favorites").json()["total"] == 0


# ---------- preferences ----------
def test_preferences_merge_per_section_and_stay_private(family):
    jane, jh = family["jane"]; owen, oh = family["owen"]
    assert jane.get("/api/player/v1/preferences").json()["reader"] == {}
    jane.put("/api/player/v1/preferences", json={"reader": {"theme": "sepia", "size": 4}}, headers=jh)
    jane.put("/api/player/v1/preferences", json={"audio": {"speed": 1.5}}, headers=jh)
    p = jane.get("/api/player/v1/preferences").json()
    assert p["reader"] == {"theme": "sepia", "size": 4} and p["audio"] == {"speed": 1.5}
    assert owen.get("/api/player/v1/preferences").json()["reader"] == {}
    assert jane.put("/api/player/v1/preferences", json={"unknown": {}}, headers=jh).status_code == 422
    assert jane.put("/api/player/v1/preferences", json={"ui": {"blob": "x" * 9000}}, headers=jh).status_code == 413


def test_player_api_requires_a_signed_in_placed_account(local_mode):
    with TestClient(app) as c:
        c.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        anon = TestClient(app)
        assert anon.get("/api/player/v1/home").status_code == 401
        auth_module.create_local_user("newbie", "a-very-long-newbie-password", is_admin=False, access_scope="pending")
        p = TestClient(app)
        p.post("/api/v1/auth/login", json={"username": "newbie", "password": "a-very-long-newbie-password"})
        assert p.get("/api/player/v1/home").status_code == 403


def test_player_api_mutations_need_csrf_on_cookie_sessions(family):
    jane, _ = family["jane"]
    assert jane.put("/api/player/v1/preferences", json={"ui": {}}).status_code == 403
