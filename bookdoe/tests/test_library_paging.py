"""The library page could only see its first page.

The UI loaded a fixed first 300 works ordered by sort title and then filtered those
300 in the browser. On a library of 12,026 works that meant everything after the
alphabetical cut was unreachable and unsearchable - a freshly imported "Dead World"
was in the database, owned, with its file on disk, and still reported "not found".
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration

import helpers


def _stub(title: str, author: str = "Craig Alanson") -> str:
    """A work that is actually *in* the library.

    The Library listing is owned-only since vNext Phase 1, so a bare catalog stub is
    no longer library-visible - these tests are about paging and search, not about
    the owned/wanted split, so they own what they seed.
    """
    work_id = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    helpers.own(work_id)
    return work_id


def _seed(n: int) -> None:
    for i in range(n):
        _stub(f"Book {i:03d}")


def test_library_reports_the_real_total_not_the_page_size():
    _seed(25)
    with TestClient(app) as c:
        body = c.get("/api/v1/library?limit=10").json()
    assert len(body["items"]) == 10
    assert body["total"] == 25
    assert body["limit"] == 10 and body["offset"] == 0


def test_paging_walks_the_whole_library_without_gaps_or_repeats():
    _seed(25)
    seen = []
    with TestClient(app) as c:
        for offset in range(0, 25, 10):
            seen += [x["title"] for x in c.get(f"/api/v1/library?limit=10&offset={offset}").json()["items"]]
    assert len(seen) == 25
    assert len(set(seen)) == 25


def test_search_finds_a_work_past_the_first_page():
    """The exact shape of the reported bug."""
    _seed(400)
    _stub("Dead World")
    with TestClient(app) as c:
        first_page = [x["title"] for x in c.get("/api/v1/library?limit=300").json()["items"]]
        assert "Dead World" not in first_page, "precondition: the work is past the first page"

        found = c.get("/api/v1/library?q=Dead World").json()
    assert found["total"] == 1
    assert [x["title"] for x in found["items"]] == ["Dead World"]


def test_search_matches_author_as_well_as_title():
    _stub("Dead World")
    _stub("All Systems Red", "Martha Wells")
    with TestClient(app) as c:
        body = c.get("/api/v1/library?q=alanson").json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Dead World"


def test_total_reflects_the_filter_not_the_library():
    _seed(30)
    _stub("Dead World")
    with TestClient(app) as c:
        assert c.get("/api/v1/library?q=Dead").json()["total"] == 1
        assert c.get("/api/v1/library").json()["total"] == 31


def test_wanted_and_review_are_bounded_and_report_totals():
    _seed(5)
    with TestClient(app) as c:
        wanted = c.get("/api/v1/wanted?kind=ebook&limit=2").json()
        assert "total" in wanted and len(wanted["items"]) <= 2
        review = c.get("/api/v1/review?limit=2").json()
        assert "total" in review and len(review["items"]) <= 2
