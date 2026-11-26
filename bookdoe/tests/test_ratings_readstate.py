"""Per-user ratings and read state.

Two accounts reading the same shared library legitimately disagree about a rating
or where they are in a book - these are annotations on the (work, user) pair, not on
the work. HTTP-layer tests use the default anonymous full-access user (auth
mode=none); the isolation tests call the route functions directly with distinct
user dicts, which is equivalent to a real multi-session HTTP test for this logic
since Depends(...) markers are simply not evaluated when the argument is supplied.
"""
from fastapi.testclient import TestClient

from app import db, main
from app.main import app, RatingRequest, ReadStateRequest
from app.services import migration

client = TestClient(app)


def _work():
    return migration._find_or_create_stub({"title": "Some Book", "author": "Some Author", "identifiers": {}})


def _user(access_scope="full"):
    uid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id,username,auth_source,is_admin,access_scope,created_at,updated_at) VALUES (?,?, 'local',0,?,?,?)",
            (uid, uid, access_scope, db.now(), db.now()),
        )
    return {"id": uid, "access_scope": access_scope}


def test_set_and_clear_rating_via_http():
    # These three tests write to a table with a NOT NULL FK to users(id) under
    # auth_mode=none's fixed synthetic identity, which only gets a real row when the
    # app's lifespan runs - `with` is what makes TestClient actually run it.
    with TestClient(app) as c:
        work_id = _work()
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 4})
        assert r.status_code == 200 and r.json()["rating"] == 4
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["rating"] == 4

        r2 = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": None})
        assert r2.json()["rating"] is None
        assert c.get(f"/api/v1/library/{work_id}").json()["rating"] is None


def test_rating_out_of_range_is_rejected():
    work_id = _work()
    assert client.put(f"/api/v1/library/{work_id}/rating", json={"rating": 6}).status_code == 422
    assert client.put(f"/api/v1/library/{work_id}/rating", json={"rating": 0}).status_code == 422


def test_read_state_transitions_and_timestamps():
    with TestClient(app) as c:
        work_id = _work()
        r = c.put(f"/api/v1/library/{work_id}/read-state", json={"status": "want_to_read"})
        assert r.json()["status"] == "want_to_read"
        row = db.one("SELECT * FROM read_states WHERE work_id=?", (work_id,))
        assert row["started_at"] is None and row["finished_at"] is None

        c.put(f"/api/v1/library/{work_id}/read-state", json={"status": "reading"})
        row = db.one("SELECT * FROM read_states WHERE work_id=?", (work_id,))
        started_at = row["started_at"]
        assert started_at is not None and row["finished_at"] is None

        c.put(f"/api/v1/library/{work_id}/read-state", json={"status": "finished"})
        row = db.one("SELECT * FROM read_states WHERE work_id=?", (work_id,))
        assert row["started_at"] == started_at, "starting a re-read of the same status must not reset when it began"
        assert row["finished_at"] is not None


def test_clearing_read_state_removes_the_row():
    with TestClient(app) as c:
        work_id = _work()
        c.put(f"/api/v1/library/{work_id}/read-state", json={"status": "reading"})
        c.put(f"/api/v1/library/{work_id}/read-state", json={"status": None})
        assert db.one("SELECT * FROM read_states WHERE work_id=?", (work_id,)) is None


def test_invalid_read_state_value_is_rejected():
    work_id = _work()
    r = client.put(f"/api/v1/library/{work_id}/read-state", json={"status": "on-fire"})
    assert r.status_code == 422


def test_ratings_are_isolated_per_user():
    work_id = _work()
    alice, bob = _user(), _user()
    main.set_rating(work_id, RatingRequest(rating=5), request=None, _=None, user=alice)
    main.set_rating(work_id, RatingRequest(rating=2), request=None, _=None, user=bob)

    alice_rating = db.one("SELECT rating FROM ratings WHERE work_id=? AND user_id=?", (work_id, alice["id"]))["rating"]
    bob_rating = db.one("SELECT rating FROM ratings WHERE work_id=? AND user_id=?", (work_id, bob["id"]))["rating"]
    assert (alice_rating, bob_rating) == (5, 2)

    summary_for_alice = main._library_summary(db.one("SELECT * FROM works WHERE id=?", (work_id,)), alice)
    summary_for_bob = main._library_summary(db.one("SELECT * FROM works WHERE id=?", (work_id,)), bob)
    assert summary_for_alice["rating"] == 5
    assert summary_for_bob["rating"] == 2


def test_read_states_are_isolated_per_user():
    work_id = _work()
    alice, bob = _user(), _user()
    main.set_read_state(work_id, ReadStateRequest(status="finished"), request=None, _=None, user=alice)
    main.set_read_state(work_id, ReadStateRequest(status="want_to_read"), request=None, _=None, user=bob)

    assert db.one("SELECT status FROM read_states WHERE work_id=? AND user_id=?", (work_id, alice["id"]))["status"] == "finished"
    assert db.one("SELECT status FROM read_states WHERE work_id=? AND user_id=?", (work_id, bob["id"]))["status"] == "want_to_read"


def test_rating_a_work_not_visible_to_a_kids_user_is_404():
    import pytest
    from fastapi import HTTPException
    work_id = _work()  # not tagged 'kids'
    kid = _user(access_scope="kids")
    with pytest.raises(HTTPException) as exc:
        main.set_rating(work_id, RatingRequest(rating=3), request=None, _=None, user=kid)
    assert exc.value.status_code == 404


def test_rating_a_kid_visible_work_is_allowed_for_a_kids_user():
    from app.services import tagging
    work_id = _work()
    tagging.add_tag(work_id, tagging.kids_tag_id())
    kid = _user(access_scope="kids")
    result = main.set_rating(work_id, RatingRequest(rating=5), request=None, _=None, user=kid)
    assert result["rating"] == 5
