"""Local search: owned library, wanted catalog, local catalog, authors, series, identifiers."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import search

import helpers


def _make_work(title, author=None, series_name=None, identifier=None):
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'resolved',?,?)",
            (wid, title, title, t, t),
        )
        if author:
            cid = db.contributor_id_for_name(conn, author)
            conn.execute("INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',0)", (wid, cid))
        if series_name:
            row = conn.execute("SELECT id FROM series WHERE name=?", (series_name,)).fetchone()
            sid = row["id"] if row else db.uid()
            if not row:
                conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, series_name, series_name, t, t))
            conn.execute("INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,NULL)", (wid, sid))
        if identifier:
            ns, value = identifier
            conn.execute(
                "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,?,?,'test',1,1.0)",
                (wid, ns, value),
            )
    return wid


def test_empty_query_returns_empty_results():
    result = search.search_local("")
    assert result == {"results": [], "total": 0, "limit": 50, "offset": 0}
    assert search.search_local("   ")["results"] == []


def test_owned_work_found_by_title():
    wid = _make_work("The Owned Book")
    helpers.own(wid)
    result = search.search_local("Owned Book")
    assert [r["id"] for r in result["results"]] == [wid]
    assert result["results"][0]["owned_count"] == 1
    assert result["results"][0]["type"] == "work"


def test_wanted_work_found_by_title():
    wid = _make_work("The Wanted Book")
    helpers.want(wid)
    result = search.search_local("Wanted Book")
    assert [r["id"] for r in result["results"]] == [wid]
    assert result["results"][0]["wanted_count"] == 1
    assert result["results"][0]["owned_count"] == 0


def test_local_catalog_work_found_without_intent():
    wid = _make_work("Just Cataloged Book")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','missing',0,?,?)",
            (db.uid(), wid, db.now(), db.now()),
        )
    result = search.search_local("Cataloged Book")
    assert [r["id"] for r in result["results"]] == [wid]
    assert result["results"][0]["owned_count"] == 0
    assert result["results"][0]["wanted_count"] == 0
    assert result["results"][0]["media_count"] == 1


def test_series_match_returns_series_entity_and_member_work():
    wid = _make_work("Volume One", series_name="The Great Saga")
    result = search.search_local("Great Saga")
    types_by_id = {r["id"]: r["type"] for r in result["results"]}
    assert wid in types_by_id and types_by_id[wid] == "work"
    assert "series" in types_by_id.values()


def test_author_match_returns_author_entity_and_their_work():
    wid = _make_work("Some Novel", author="Jane Novelist")
    result = search.search_local("Jane Novelist")
    types_by_id = {r["id"]: r["type"] for r in result["results"]}
    assert wid in types_by_id and types_by_id[wid] == "work"
    assert "author" in types_by_id.values()


def test_identifier_exact_match():
    wid = _make_work("An Identified Book", identifier=("isbn", "9780000000001"))
    result = search.search_local("9780000000001")
    assert [r["id"] for r in result["results"]] == [wid]
    assert result["results"][0]["identifiers"] == [{"namespace": "isbn", "value": "9780000000001"}]


def test_identifier_match_ranks_above_partial_title_match():
    # Neither title contains the other's search term as a substring by accident,
    # so the only way "Common Word" surfaces both is via the two distinct match types.
    title_hit = _make_work("A Book About Common Word Origins")
    identifier_hit = _make_work("Totally Unrelated Title")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,?,?,'test',1,1.0)",
            (identifier_hit, "isbn", "Common Word"),
        )
    result = search.search_local("Common Word")
    ids = [r["id"] for r in result["results"]]
    assert ids.index(identifier_hit) < ids.index(title_hit)


def test_owned_work_ranks_above_non_owned_for_same_title_match():
    owned = _make_work("Ranked Title Match Owned")
    helpers.own(owned)
    unowned = _make_work("Ranked Title Match Plain")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','none',0,?,?)",
            (db.uid(), unowned, db.now(), db.now()),
        )
    result = search.search_local("Ranked Title Match")
    ids = [r["id"] for r in result["results"]]
    assert ids.index(owned) < ids.index(unowned)


def test_pagination_limit_and_offset():
    ids = [_make_work(f"Paginated Widget {i:02d}") for i in range(5)]
    page1 = search.search_local("Paginated Widget", limit=2, offset=0)
    page2 = search.search_local("Paginated Widget", limit=2, offset=2)
    assert page1["total"] == 5
    assert page2["total"] == 5
    assert len(page1["results"]) == 2
    assert len(page2["results"]) == 2
    assert {r["id"] for r in page1["results"]}.isdisjoint({r["id"] for r in page2["results"]})
    assert set(ids) >= {r["id"] for r in page1["results"]} | {r["id"] for r in page2["results"]}


def test_search_endpoint_returns_results():
    wid = _make_work("Endpoint Search Target")
    helpers.own(wid)
    with TestClient(app) as c:
        r = c.get("/api/v1/search", params={"q": "Endpoint Search"})
        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert any(item["id"] == wid for item in body["results"])


def test_search_endpoint_requires_query_param():
    with TestClient(app) as c:
        r = c.get("/api/v1/search")
        assert r.status_code == 422
