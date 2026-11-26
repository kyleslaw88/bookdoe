"""A client that keeps writing a book's position must not be able to flood the server.

Live incident: a kid's audiobook client sent ~140 PUT /progress per minute for 30
minutes after an admin took the book off the kids shelf. The 404s were right; the
volume was not. Server side: a hidden or missing book is one consistent, cheap 404
(no existence leak, never cached) and writes past a per-account, per-book budget get
429 + Retry-After before any database work. The client side lives in
scripts/ui-player-e2e.mjs and tests/test_audio_client.py.
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
PW = "a-very-long-{}-password"


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


def _audiobook(tmp_path, title):
    t = db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)",
                         (db.uid(), "Root", str(tmp_path), "audiobook", t, t))
    work = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = helpers.own(work, "audiobook", "mp3")
    p = tmp_path / f"{title}.mp3"
    p.write_bytes(b"ID3" * 100)
    fid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,3600,'{}',1,0,0,?)",
                     (fid, aid, str(p), p.stat().st_size, t))
    return work, aid, fid


@pytest.fixture
def home(local_mode, tmp_path):
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": PW.format("admin")})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        for name, scope in (("owen", "kids"), ("jane", "full")):
            assert admin.post("/api/v1/users", json={"username": name, "password": PW.format(name), "access_scope": scope}, headers=h).status_code == 200
        shelf = _audiobook(tmp_path, "On The Shelf")
        tagging.add_tag(shelf[0], tagging.kids_tag_id())
        hidden = _audiobook(tmp_path, "Not For Kids")
        sessions = {}
        for name in ("owen", "jane"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": name, "password": PW.format(name)})
            assert r.status_code == 200, r.text
            sessions[name] = (c, {"X-CSRF-Token": r.json()["csrf"]})
        yield {"admin": (admin, h), **sessions, "shelf": shelf, "hidden": hidden}


def _put(c, h, work, aid, **body):
    return c.put(f"/api/v1/library/{work}/progress/{aid}", json={"position": 5, **body}, headers=h)


def test_hidden_and_missing_books_get_one_identical_cheap_404(home):
    owen, oh = home["owen"]; admin, ah = home["admin"]
    hw, ha, _ = home["hidden"]
    hidden = _put(owen, oh, hw, ha)
    missing_for_kid = _put(owen, oh, "no-such-work", "no-such-asset")
    # An admin naming an asset that is not under the work used to get a 400 that told
    # an existing asset from a missing one; it is the same 404 now.
    mismatch = _put(admin, ah, hw, "no-such-asset")
    for r in (hidden, missing_for_kid, mismatch):
        assert r.status_code == 404, r.text
        assert r.json() == {"detail": "Not found"}
        assert r.headers["cache-control"] == "no-store"
    assert db.one("SELECT COUNT(*) AS n FROM media_progress")["n"] == 0


def test_writes_past_the_budget_get_429_with_retry_after_and_no_database_work(home, monkeypatch):
    owen, oh = home["owen"]
    sw, sa, _ = home["shelf"]
    for i in range(reader.PROGRESS_WRITE_BUDGET):
        assert _put(owen, oh, sw, sa, position=i).status_code == 200
    calls = []
    monkeypatch.setattr(main_module.tagging, "is_visible_to", lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(reader, "set_progress", lambda *a, **k: calls.append(a))
    r = _put(owen, oh, sw, sa)
    assert r.status_code == 429, r.text
    assert 1 <= int(r.headers["retry-after"]) <= int(reader.PROGRESS_WRITE_WINDOW)
    assert not calls, "a throttled write must not touch visibility or the progress row"


def test_a_hidden_book_flood_is_throttled_too(home):
    """The incident shape: every request 404s. After the budget the answer becomes 429
    (so the client is told when to come back) instead of an unbounded stream of 404s."""
    owen, oh = home["owen"]
    hw, ha, _ = home["hidden"]
    codes = [_put(owen, oh, hw, ha, position=i).status_code for i in range(reader.PROGRESS_WRITE_BUDGET + 20)]
    assert codes[:reader.PROGRESS_WRITE_BUDGET] == [404] * reader.PROGRESS_WRITE_BUDGET
    assert set(codes[reader.PROGRESS_WRITE_BUDGET:]) == {429}


def test_budget_is_per_account_and_per_book_and_recovers(home, monkeypatch):
    owen, oh = home["owen"]; jane, jh = home["jane"]
    sw, sa, _ = home["shelf"]; hw, ha, _ = home["hidden"]
    now = [1000.0]
    monkeypatch.setattr(reader, "_clock", lambda: now[0])
    for _i in range(reader.PROGRESS_WRITE_BUDGET):
        _put(owen, oh, sw, sa)
    assert _put(owen, oh, sw, sa).status_code == 429
    assert _put(jane, jh, sw, sa).status_code == 200, "another account is not throttled by Owen's flood"
    assert _put(jane, jh, hw, ha).status_code == 200, "nor is another book"
    now[0] += reader.PROGRESS_WRITE_WINDOW + 1
    assert _put(owen, oh, sw, sa).status_code == 200, "the budget is a window, not a ban"


def test_player_api_single_put_matches_the_legacy_route(home):
    owen, oh = home["owen"]
    sw, sa, sf = home["shelf"]; hw, ha, _ = home["hidden"]
    url = "/api/player/v1/progress/{}"
    assert owen.put(url.format(ha), json={"asset_id": ha, "position": 1}, headers=oh).status_code == 404
    ok = owen.put(url.format(sa), json={"asset_id": sa, "file_id": sf, "position": 1}, headers=oh)
    assert ok.status_code == 200 and ok.json()["applied"] is True
    for _i in range(reader.PROGRESS_WRITE_BUDGET):
        owen.put(url.format(sa), json={"asset_id": sa, "position": 2}, headers=oh)
    r = owen.put(url.format(sa), json={"asset_id": sa, "position": 3}, headers=oh)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_batch_replay_reports_hidden_and_throttled_items_instead_of_failing(home):
    """The offline replay keeps what the server did not apply, so each item says why."""
    owen, oh = home["owen"]
    sw, sa, sf = home["shelf"]; hw, ha, _ = home["hidden"]
    r = owen.put("/api/player/v1/progress", json={"items": [
        {"asset_id": sa, "file_id": sf, "position": 9}, {"asset_id": ha, "position": 9}]}, headers=oh)
    assert r.status_code == 200
    first, second = r.json()["items"]
    assert first["applied"] is True
    assert second == {"asset_id": ha, "applied": False, "error": "not_found"}
    for _i in range(reader.PROGRESS_WRITE_BUDGET):
        reader.progress_write_retry_after(db.one("SELECT id FROM users WHERE username='owen'")["id"], sa)
    r = owen.put("/api/player/v1/progress", json={"items": [{"asset_id": sa, "position": 10}]}, headers=oh)
    item = r.json()["items"][0]
    assert item["error"] == "rate_limited" and item["applied"] is False and item["retry_after"] >= 1
