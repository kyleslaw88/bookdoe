"""Podcast play order and Continue Listening feed (issue #518)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.main import app
from app.services import migration, podcast_queue as pq, podcasts, reader

import helpers

USER = "anonymous"


def _show():
    """Show with season 1 (a, b), season 2 (c) and Extras (x). Returns (show_id, {title: episode_id})."""
    ids = {}
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        # Extras is created first on purpose: ordering must not depend on insertion order.
        for number, title, pos, date in ((None, "x", 1, "2024-01-01"), (2, "c", 1, "2024-03-01"),
                                         (1, "b", 2, "2024-02-02"), (1, "a", 1, "2024-02-01")):
            sid = podcasts.get_or_create_season(conn, show, number)
            ids[title] = podcasts.insert_episode(conn, show_id=show, season_id=sid, title=title, release_date=date,
                                                 path=f"/p/{title}.mp3", position=pos, duration_seconds=1000)
    return show, ids


def _play(ep, played=True, pos=0.0, at=None):
    with db.tx() as conn:
        podcasts.set_progress(conn, ep, played=played, position_seconds=pos, user_id=USER)
        if at is not None:
            conn.execute("UPDATE podcast_progress SET updated_at=? WHERE episode_id=?", (at, ep))


def _title(ep):
    return ep["title"] if ep else None


def test_next_unplayed_is_first_in_season_order_with_extras_last():
    show, ids = _show()
    with db.session() as conn:
        assert _title(pq.next_unplayed(conn, show, USER)) == "a"
    _play(ids["a"])
    with db.session() as conn:
        assert _title(pq.next_unplayed(conn, show, USER)) == "b"
    _play(ids["b"])
    _play(ids["c"])
    with db.session() as conn:
        assert _title(pq.next_unplayed(conn, show, USER)) == "x"
    _play(ids["x"])
    with db.session() as conn:
        assert pq.next_unplayed(conn, show, USER) is None


def test_play_next_crosses_the_season_boundary():
    _, ids = _show()
    with db.session() as conn:
        assert _title(pq.play_next(conn, ids["a"], USER)) == "b"
        # b is the last of season 1: the next is the first of season 2.
        assert _title(pq.play_next(conn, ids["b"], USER)) == "c"


def test_play_next_puts_extras_after_all_numbered_seasons_and_ends():
    _, ids = _show()
    with db.session() as conn:
        assert _title(pq.play_next(conn, ids["c"], USER)) == "x"
        assert pq.play_next(conn, ids["x"], USER) is None
        with pytest.raises(LookupError):
            pq.play_next(conn, "nope", USER)


def test_play_next_skips_played_episodes():
    _, ids = _show()
    _play(ids["b"])
    with db.session() as conn:
        assert _title(pq.play_next(conn, ids["a"], USER)) == "c"


def test_next_endpoints():
    show, ids = _show()
    _play(ids["a"])
    _play(ids["b"])
    with TestClient(app) as c:
        r = c.get(f"/api/player/v1/podcasts/shows/{show}/next")
        assert r.status_code == 200 and r.json()["episode"]["title"] == "c"
        r = c.get(f"/api/player/v1/podcasts/episodes/{ids['b']}/next")
        assert r.json()["episode"]["title"] == "c"
        assert c.get("/api/player/v1/podcasts/shows/nope/next").status_code == 404
        assert c.get("/api/player/v1/podcasts/episodes/nope/next").status_code == 404


def _book(tmp_path, kind="audiobook", fmt="m4b"):
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) "
                     "VALUES (?,?,?,?,'adopt',?,?)", (db.uid(), "Root", str(tmp_path), kind, t, t))
    work_id = migration._find_or_create_stub({"title": "Book", "author": "Author", "identifiers": {}})
    aid = helpers.own(work_id, kind, fmt)
    p = tmp_path / f"Book.{fmt}"
    p.write_bytes(b"PK\x03\x04 bytes " * 50)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) "
                     "VALUES (?,?,?,?,'{}',1,0,0,?)", (db.uid(), aid, str(p), p.stat().st_size, t))
    return work_id, aid


def _stamp_book(aid, at):
    with db.tx() as conn:
        conn.execute("UPDATE media_progress SET updated_at=? WHERE asset_id=?", (at, aid))


def test_continue_listening_interleaves_book_and_episode_by_last_played(tmp_path):
    _, ids = _show()
    auth.ensure_anonymous_user()
    work_id, aid = _book(tmp_path)
    reader.set_progress(work_id, aid, USER, file_id=None, position=None, location="x", percent=0.3)
    _play(ids["a"], played=False, pos=50.0, at=2000.0)
    _play(ids["b"], played=True, pos=0.0, at=3000.0)   # played: never in the feed
    _stamp_book(aid, 1000.0)
    with TestClient(app) as c:
        items = c.get("/api/player/v1/continue?kind=audiobook").json()["items"]
        assert [(i.get("kind"), i["title"]) for i in items] == [("podcast", "a"), ("audiobook", "Book")]
        # Newer audiobook activity moves the book to the front.
        _stamp_book(aid, 5000.0)
        items = c.get("/api/player/v1/continue?kind=audiobook").json()["items"]
        assert [i["title"] for i in items] == ["Book", "a"]
        assert [i["title"] for i in c.get("/api/player/v1/continue?kind=podcast").json()["items"]] == ["a"]
        # Ebook shelf never contains episodes (and has no ebooks here).
        assert c.get("/api/player/v1/continue?kind=ebook").json()["items"] == []


def test_home_listening_row_interleaves_book_and_episode_and_is_empty_without_progress(tmp_path):
    _, ids = _show()
    auth.ensure_anonymous_user()
    with TestClient(app) as c:
        assert c.get("/api/player/v1/home").json()["continue_listening"] == []
        work_id, aid = _book(tmp_path)
        reader.set_progress(work_id, aid, USER, file_id=None, position=None, location="x", percent=0.3)
        _play(ids["a"], played=False, pos=50.0, at=2000.0)
        _stamp_book(aid, 1000.0)
        row = c.get("/api/player/v1/home").json()["continue_listening"]
        assert [(i["kind"], i["title"]) for i in row] == [("podcast", "a"), ("audiobook", "Book")]
        assert row[0]["show_title"] == "Show" and row[0]["episode_id"] == ids["a"]


def test_continue_timestamps_from_service_calls_share_a_type_and_sort(tmp_path):
    _, ids = _show()
    auth.ensure_anonymous_user()
    work_id, aid = _book(tmp_path)
    reader.set_progress(work_id, aid, USER, file_id=None, position=None, location="x", percent=0.3)
    _play(ids["a"], played=False, pos=50.0)   # no manual updated_at: both columns come from db.now()
    with TestClient(app) as c:
        items = c.get("/api/player/v1/continue?kind=audiobook").json()["items"]
    assert {i["title"] for i in items} == {"Book", "a"}
    assert all(isinstance(i["updated_at"], float) for i in items)


def test_play_next_episode_missing_from_ordered_list_is_lookup_error(monkeypatch):
    # An episode row that the ordered join drops (e.g. no season row) must give LookupError (404), not StopIteration.
    show, ids = _show()
    monkeypatch.setattr(pq, "_ordered", lambda conn, show_id, user_id: [])
    with db.session() as conn:
        with pytest.raises(LookupError):
            pq.play_next(conn, ids["a"], USER)
