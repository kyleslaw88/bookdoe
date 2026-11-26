"""Contract tests for the rating endpoint.

Proves that PUT /api/v1/library/{id}/rating accepts integer ratings 1-5
and rejects values outside that range (0, 6, negative, floats).

These are an external client contract tests: they pin the request shape the
orchestrator depends on (spec Part III, area A05a).
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration


def _make_work(title: str = "Rating Contract Book") -> str:
    """Insert a work row into the test database."""
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'resolved',?,?)",
            (wid, title, title, t, t),
        )
    return wid


def test_rating_1_is_accepted():
    """Rating of 1 (minimum) is accepted."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 1})
        assert r.status_code == 200
        assert r.json()["rating"] == 1


def test_rating_5_is_accepted():
    """Rating of 5 (maximum) is accepted."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 5})
        assert r.status_code == 200
        assert r.json()["rating"] == 5


def test_rating_3_is_accepted():
    """A middle rating of 3 is accepted."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 3})
        assert r.status_code == 200
        assert r.json()["rating"] == 3


def test_rating_0_is_rejected():
    """Rating of 0 (below minimum) is rejected with 422."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 0})
        assert r.status_code == 422


def test_rating_6_is_rejected():
    """Rating of 6 (above maximum) is rejected with 422."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 6})
        assert r.status_code == 422


def test_rating_negative_is_rejected():
    """Negative ratings are rejected with 422."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": -1})
        assert r.status_code == 422


def test_rating_float_is_rejected():
    """Float ratings (e.g. 3.5) are rejected with 422."""
    work_id = _make_work()
    with TestClient(app) as c:
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 3.5})
        assert r.status_code == 422


def test_rating_null_clears_rating():
    """Sending null clears the rating."""
    work_id = _make_work()
    with TestClient(app) as c:
        c.put(f"/api/v1/library/{work_id}/rating", json={"rating": 4})
        r = c.put(f"/api/v1/library/{work_id}/rating", json={"rating": None})
        assert r.status_code == 200
        assert r.json()["rating"] is None
