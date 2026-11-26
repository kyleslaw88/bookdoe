"""Contract tests for the search and library query endpoints.

Proves that the `q` query parameter on both GET /api/v1/search and
GET /api/v1/library actually filters results by title/author, and that
omitting `q` returns the full (unfiltered) result set.

These are an external client contract tests: they pin the request shapes the
orchestrator depends on (spec Part III, areas A01b/A05a).
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration

import helpers


def _make_work(title: str, author: str | None = None) -> str:
    """Insert a work row (not yet owned) into the test database."""
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'resolved',?,?)",
            (wid, title, title, t, t),
        )
        if author:
            cid = db.contributor_id_for_name(conn, author)
            conn.execute(
                "INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',0)",
                (wid, cid),
            )
    return wid


def test_search_endpoint_filters_by_q():
    """GET /api/v1/search?q=<term> must return only matching works."""
    a_id = _make_work("Alpha Book")
    b_id = _make_work("Beta Book")
    helpers.own(a_id)
    helpers.own(b_id)

    with TestClient(app) as c:
        r = c.get("/api/v1/search", params={"q": "Alpha"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["results"][0]["id"] == a_id

        r2 = c.get("/api/v1/search", params={"q": "Beta"})
        assert r2.status_code == 200
        assert r2.json()["total"] == 1
        assert r2.json()["results"][0]["id"] == b_id

        # A term matching neither should return empty
        r3 = c.get("/api/v1/search", params={"q": "Gamma"})
        assert r3.status_code == 200
        assert r3.json()["total"] == 0


def test_search_endpoint_returns_all_when_no_q():
    """The /api/v1/search endpoint requires q (min_length=1); omitting it is a 422."""
    _make_work("Some Book")
    helpers.own(_make_work("Another Book"))
    with TestClient(app) as c:
        r = c.get("/api/v1/search")
        assert r.status_code == 422


def test_library_endpoint_filters_by_q():
    """GET /api/v1/library?q=<term> must filter by title and author."""
    a_id = _make_work("Library Alpha")
    b_id = _make_work("Library Beta")
    helpers.own(a_id)
    helpers.own(b_id)

    with TestClient(app) as c:
        r = c.get("/api/v1/library", params={"q": "Alpha"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == a_id

        r2 = c.get("/api/v1/library", params={"q": "Beta"})
        assert r2.status_code == 200
        assert r2.json()["total"] == 1
        assert r2.json()["items"][0]["id"] == b_id


def test_library_endpoint_returns_all_when_no_q():
    """GET /api/v1/library without q returns all owned works (unfiltered)."""
    a_id = _make_work("Full List Alpha")
    b_id = _make_work("Full List Beta")
    helpers.own(a_id)
    helpers.own(b_id)

    with TestClient(app) as c:
        r = c.get("/api/v1/library")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        ids = {item["id"] for item in body["items"]}
        assert ids == {a_id, b_id}


def test_search_filters_by_author():
    """The `q` param also matches author names; returns the work and the author entity."""
    wid = _make_work("Author Book", author="Jane Smith")
    helpers.own(wid)

    with TestClient(app) as c:
        r = c.get("/api/v1/search", params={"q": "Jane Smith"})
        assert r.status_code == 200
        body = r.json()
        # Search returns both the work (author match) and the author entity
        assert body["total"] >= 1
        work_ids = {item["id"] for item in body["results"] if item["type"] == "work"}
        assert wid in work_ids
