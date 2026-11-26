"""F-09: performance baseline on a seeded 100,000-work library (spec Part XLIII).

Marked ``slow``: the default suite skips these (see conftest); run them with
``PYTHONPATH=. pytest -o addopts= -m slow tests/test_large_library_perf.py``.

Library shape (tests/large_library.py): 100,000 works, 20,000 authors, 8,000 series,
50,000 owned ebooks with one file each.  Every request goes through the real
route, so the budgets cover SQL, batching and JSON rendering together.

Budgets (wall clock, one request, generous against the measured costs so a loaded CI
box does not flake, yet far below what a scan or an N+1 would cost; the search budgets
are mirrored in docs/OPERATIONS.md "Performance budgets", checked by
test_perf_docs_budgets.py):

    library page (first / deep offset / filtered)   1.0s,  <= 15 SQL statements
    library text search                              1.25s
    global search                                    1.5s
    author list                                      3.0s   (returns every author)
    OPDS v1 / v2 page (first / deep offset)          1.0s,  <= 15 SQL statements

Query-count budgets are the constant per-page cost (count + page + the batched
summary queries), so they hold at any library size.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

import large_library as ll

pytestmark = pytest.mark.slow

PAGE_BUDGET_S = 1.0
LIBRARY_SEARCH_BUDGET_S = 1.25
GLOBAL_SEARCH_BUDGET_S = 1.5
AUTHORS_BUDGET_S = 3.0
SERIES_PAGE_BUDGET_S = 2.0
AUTHORS_PAGE_BUDGET_S = 3.0
MAX_PAGE_STATEMENTS = 15


@pytest.fixture
def big():
    return ll.seed_large_library()


@pytest.fixture
def statements(monkeypatch):
    """Record every SQL statement run through db.connect (real queries, only observed)."""
    seen: list[str] = []
    real = db.connect

    def counting(*a, **kw):
        conn = real(*a, **kw)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(db, "connect", counting)
    return seen


def _timed(client, url):
    t0 = time.perf_counter()
    r = client.get(url)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:200]
    return r, elapsed


def _squash(sql):
    """Collapse whitespace and case so a harmless reformat of a query keeps matching."""
    return " ".join(sql.split()).upper()


def _selects(seen):
    return [s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))]


def test_seed_matches_documented_shape(big):
    assert big == {"works": 100_000, "authors": 20_000, "series": 8_000, "owned": 50_000}
    assert db.one("SELECT COUNT(*) n FROM works")["n"] == 100_000


@pytest.mark.parametrize("url,total", [
    ("/api/v1/library?limit=50&offset=0", 50_000),
    ("/api/v1/library?limit=50&offset=40000", 50_000),
    (f"/api/v1/library?limit=50&author_id=a{2:06d}", 5),
    ("/api/v1/library?limit=50&q=Zanzibar", 1),
])
def test_library_page_budget(big, statements, url, total):
    with TestClient(app) as c:
        statements.clear()
        r, elapsed = _timed(c, url)
        n = len(_selects(statements))
    body = r.json()
    assert body["total"] == total
    assert len(body["items"]) == min(50, total)
    assert elapsed < (LIBRARY_SEARCH_BUDGET_S if "q=" in url else PAGE_BUDGET_S), f"{url}: {elapsed:.2f}s"
    assert n <= MAX_PAGE_STATEMENTS, f"{url}: {n} statements"


@pytest.mark.parametrize("q,title", [("Zanzibar", ll.NEEDLE_TITLE), ("Quillon", ll.NEEDLE_AUTHOR)])
def test_global_search_budget(big, q, title):
    with TestClient(app) as c:
        r, elapsed = _timed(c, f"/api/v1/search?q={q}")
    assert any(title in x["title"] for x in r.json()["results"])
    assert elapsed < GLOBAL_SEARCH_BUDGET_S, f"search {q}: {elapsed:.2f}s"


def test_author_list_budget_and_statement_count(big, statements):
    with TestClient(app) as c:
        statements.clear()
        r, elapsed = _timed(c, "/api/v1/authors")
        n = len(_selects(statements))
    items = r.json()["items"]
    assert len(items) == 20_000
    assert sum(a["work_count"] for a in items) == 100_000
    assert elapsed < AUTHORS_BUDGET_S, f"authors: {elapsed:.2f}s"
    assert n <= 5, f"authors: {n} statements (aggregation must be one query)"


def test_series_page_budget_and_statement_count(big, statements):
    """D9: limit/offset/q are honoured and gap_count is computed for the page only,
    in one pass (it used to be one query per series)."""
    with TestClient(app) as c:
        statements.clear()
        r, elapsed = _timed(c, "/api/v1/series?limit=50&offset=100&include_uncertain=true")
        n = len(_selects(statements))
        body = r.json()
        q_r, q_elapsed = _timed(c, "/api/v1/series?limit=50&q=a&include_uncertain=true")
    assert len(body["items"]) == 50
    assert body["total"] == 1_600  # series with at least one work (of the 8,000 seeded)
    assert all("gap_count" in s for s in body["items"])
    assert elapsed < SERIES_PAGE_BUDGET_S, f"series page: {elapsed:.2f}s"
    assert n <= 6, f"series page: {n} statements (gap_count must not be per series)"
    assert 0 < len(q_r.json()["items"]) <= 50 and q_r.json()["total"] <= 1_600
    assert q_elapsed < SERIES_PAGE_BUDGET_S, f"series q: {q_elapsed:.2f}s"


def test_authors_page_budget(big, statements):
    with TestClient(app) as c:
        statements.clear()
        r, elapsed = _timed(c, "/api/v1/authors?limit=50&offset=100")
        n = len(_selects(statements))
        q_r, q_elapsed = _timed(c, "/api/v1/authors?limit=50&q=Quillon")
    body = r.json()
    assert len(body["items"]) == 50 and body["total"] == 20_000
    assert elapsed < AUTHORS_PAGE_BUDGET_S, f"authors page: {elapsed:.2f}s"
    assert n <= 5, f"authors page: {n} statements"
    assert q_r.json()["total"] >= 1
    assert q_elapsed < AUTHORS_PAGE_BUDGET_S, f"authors q: {q_elapsed:.2f}s"


@pytest.mark.parametrize("url,key", [
    ("/opds/v2/catalog.json?limit=50&offset=0", "publications"),
    ("/opds/v2/catalog.json?limit=50&offset=40000", "publications"),
    ("/opds/v1/catalog.xml?limit=50&offset=0", None),
    ("/opds/v1/catalog.xml?limit=50&offset=40000", None),
])
def test_opds_page_budget(big, statements, url, key):
    with TestClient(app) as c:
        statements.clear()
        r, elapsed = _timed(c, url)
        n = len(_selects(statements))
    if key:
        assert len(r.json()[key]) == 50
    else:
        assert r.text.count("<entry>") == 50
    assert elapsed < PAGE_BUDGET_S, f"{url}: {elapsed:.2f}s"
    assert n <= MAX_PAGE_STATEMENTS, f"{url}: {n} statements"


@pytest.mark.parametrize("url,marker,index,ordered", [
    ("/api/v1/library?limit=50&offset=40000", "ORDER BY COALESCE(w.sort_title,w.title)", "idx_works_library_order", True),
    ("/opds/v2/catalog.json?limit=50&offset=40000", "ORDER BY w.sort_title,w.title,w.id", "idx_works_opds_order", True),
    ("/opds/v1/catalog.xml?limit=50&offset=40000", "ORDER BY w.sort_title,w.title,w.id", "idx_works_opds_order", True),
    ("/api/v1/authors", "COUNT(DISTINCT wc.work_id)", "idx_work_contributors_contributor", False),
    ("/api/v1/series", "COUNT(DISTINCT smem.work_id)", "idx_series_memberships_series", False),
])
def test_hot_queries_use_the_f09_indexes(big, statements, url, marker, index, ordered):
    """EXPLAIN the statements the route really ran (captured by the trace callback).

    ``ordered`` cases are the paged listings, whose ORDER BY must be served by the
    index without a sort.  The author and series aggregates sort their grouped
    result, so for them only the index lookup is asserted.  Search is not covered
    here: it remains an unindexed ``LIKE '%q%'`` scan (out of scope for F-09), held
    only by the LIBRARY_SEARCH_BUDGET_S and GLOBAL_SEARCH_BUDGET_S timing tests.
    """
    with TestClient(app) as c:
        statements.clear()
        _timed(c, url)
        ran = [s for s in _selects(statements) if _squash(marker) in _squash(s)]
    assert ran, f"{url} ran no statement containing {marker!r}"
    with db.session() as conn:  # closed on exit: fresh_db teardown fails a test that leaks a handle (#36)
        for sql in ran:
            plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
            assert index in plan, plan
            if ordered:
                assert "TEMP B-TREE FOR ORDER BY" not in plan, plan
