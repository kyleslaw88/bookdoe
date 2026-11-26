"""F-06: Library summary uses a constant number of queries per page.

Before the fix, _library_summary ran ~7 queries per work (authors, series,
assets, files, tags, cover cache, ratings/read states).  With 100 works on
a page that was ~700 queries.  The fix batch-loads everything by work_id
in a constant number of queries (7 regardless of page size).
"""
from __future__ import annotations

import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db
from app.main import app, _batch_library_summary, _library_summary
from app.services import migration

import helpers


def _stub(title: str, author: str = "Craig Alanson") -> str:
    work_id = migration._find_or_create_stub(
        {"title": title, "author": author, "identifiers": {}}
    )
    helpers.own(work_id)
    return work_id


def _seed(n: int) -> None:
    for i in range(n):
        _stub(f"Book {i:03d}")


def _seed_with_files(n: int) -> list[str]:
    """Seed works with owned assets and media_files so OPDS generates entries."""
    work_ids = []
    t = db.now()
    for i in range(n):
        wid = migration._find_or_create_stub(
            {"title": f"Book {i:03d}", "author": "Craig Alanson", "identifiers": {}}
        )
        work_ids.append(wid)
        # Create an owned asset
        aid = db.uid()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, wid, "ebook", "epub", "owned", 0, 0, t, t),
            )
            # Create a media_file for the asset
            fid = db.uid()
            conn.execute(
                "INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,container,missing,discovered_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fid, aid, f"/books/book-{i:03d}.epub", 1, 1024, "epub", 0, t),
            )
    return work_ids


# ---------------------------------------------------------------------------
# Query-count tests for _batch_library_summary
# ---------------------------------------------------------------------------

def _count_all_calls(func):
    """Wrap db.all to count invocations."""
    call_count = 0
    original_all = db.all

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    return counted_all, lambda: call_count


def test_batch_summary_uses_constant_queries_small_page():
    """_batch_library_summary should issue a constant number of DB calls.

    The _seed function creates stubs with owned assets (via helpers.own), so
    the files query runs.  We get 6 queries: authors, series, assets, files,
    tags, cover_cache.
    """
    _seed(20)
    works = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 20")
    assert len(works) == 20

    call_count = 0
    original_all = db.all

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    with patch.object(db, "all", side_effect=counted_all):
        result = _batch_library_summary(works)

    # 6 queries: authors, series, assets, files, tags, cover_cache
    assert call_count == 6, f"Expected 6 queries, got {call_count}"
    assert len(result) == 20


def test_batch_summary_no_assets_constant_queries():
    """When works have no assets, the files query is skipped (5 queries)."""
    # Create works without calling helpers.own
    work_ids = []
    for i in range(10):
        wid = migration._find_or_create_stub(
            {"title": f"Book {i:03d}", "author": "Craig Alanson", "identifiers": {}}
        )
        work_ids.append(wid)
    works = db.all("SELECT * FROM works WHERE id IN (%s)" % ",".join("?" for _ in work_ids), tuple(work_ids))  # nosec B608

    call_count = 0
    original_all = db.all

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    with patch.object(db, "all", side_effect=counted_all):
        result = _batch_library_summary(works)

    # 5 queries: authors, series, assets, tags, cover_cache (files skipped)
    assert call_count == 5, f"Expected 5 queries (no assets), got {call_count}"
    assert len(result) == 10


def test_batch_summary_uses_constant_queries_large_page():
    """The query count should be the same for 50 works as for 5 works."""
    _seed(50)
    works_small = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 5")
    works_large = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 50")

    def count_for(works_list):
        counted, get_count = _count_all_calls(None)
        with patch.object(db, "all", side_effect=counted):
            _batch_library_summary(works_list)
        return get_count()

    count_small = count_for(works_small)
    count_large = count_for(works_large)

    assert count_small == count_large, (
        f"Query count should be constant: 5 works={count_small}, "
        f"50 works={count_large}"
    )


def test_batch_summary_with_files_constant_queries():
    """Even with assets and files present, the count should be constant."""
    _seed_with_files(20)
    works = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 20")
    assert len(works) == 20

    call_count = 0
    original_all = db.all

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    with patch.object(db, "all", side_effect=counted_all):
        result = _batch_library_summary(works)

    # 6 queries: authors, series, assets, files, tags, cover_cache
    assert call_count == 6, f"Expected 6 queries with files, got {call_count}"
    assert len(result) == 20


def test_batch_summary_with_user_has_extra_queries():
    """When a user is provided, ratings + read_states add 2 more queries."""
    _seed(20)
    works = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 20")
    assert len(works) == 20

    call_count = 0
    original_all = db.all

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    user = {"id": "test-user", "username": "test"}

    with patch.object(db, "all", side_effect=counted_all):
        result = _batch_library_summary(works, user=user)

    # 6 base + 2 (ratings + read_states) = 8
    assert call_count == 8, f"Expected 8 queries, got {call_count}"
    assert len(result) == 20


def test_batch_summary_with_files_and_user():
    """With files and a user: 6 base + 2 = 8 queries."""
    _seed_with_files(20)
    works = db.all("SELECT * FROM works ORDER BY sort_title,title LIMIT 20")

    call_count = 0
    original_all = db.all
    user = {"id": "test-user", "username": "test"}

    def counted_all(sql: str, params=()):
        nonlocal call_count
        call_count += 1
        return original_all(sql, params)

    with patch.object(db, "all", side_effect=counted_all):
        result = _batch_library_summary(works, user=user)

    assert call_count == 8, f"Expected 8 queries (files + user), got {call_count}"


def test_batch_summary_empty_list():
    """Empty input returns empty output with zero queries."""
    counted, get_count = _count_all_calls(None)

    with patch.object(db, "all", side_effect=counted):
        result = _batch_library_summary([])

    assert result == []
    assert get_count() == 0


# ---------------------------------------------------------------------------
# API-level: library endpoint uses batch summary
# ---------------------------------------------------------------------------

def test_library_api_uses_batch_summary():
    """The /api/v1/library endpoint should return correct data via batch summary."""
    _seed(15)
    with TestClient(app) as c:
        body = c.get("/api/v1/library?limit=10").json()
    assert len(body["items"]) == 10
    assert body["total"] == 15
    # Each item should have the same structure as before
    item = body["items"][0]
    assert "id" in item
    assert "title" in item
    assert "authors" in item
    assert "series" in item
    assert "assets" in item
    assert "tags" in item
    assert "has_cover" in item


def test_library_api_data_unchanged_after_batch_refactor():
    """The output shape and values should be identical to the old per-work approach."""
    _seed(5)
    with TestClient(app) as c:
        body = c.get("/api/v1/library").json()
    assert body["total"] == 5
    for item in body["items"]:
        assert isinstance(item["authors"], list)
        assert isinstance(item["series"], list)
        assert isinstance(item["assets"], list)
        assert isinstance(item["tags"], list)
        assert isinstance(item["has_cover"], bool)
        assert "rating" in item
        assert "read_status" in item


def test_library_api_query_count_constant_at_endpoint():
    """The library endpoint should use a constant number of queries regardless of page size.

    Counts both db.all and db.one so the total is accurate.  The endpoint calls
    db.one for the total-count row, db.all for the works page, and then
    _batch_library_summary which calls db.all 6 times (no user) or 8 times
    (with user).  The total must be the same for limit=5 and limit=50.
    """
    _seed(50)

    def count_endpoint_queries(limit):
        """Count db.all and db.one calls during a library endpoint request."""
        all_count = 0
        one_count = 0
        original_all = db.all
        original_one = db.one

        def counted_all(sql: str, params=()):
            nonlocal all_count
            all_count += 1
            return original_all(sql, params)

        def counted_one(sql: str, params=()):
            nonlocal one_count
            one_count += 1
            return original_one(sql, params)

        with patch.object(db, "all", side_effect=counted_all):
            with patch.object(db, "one", side_effect=counted_one):
                with TestClient(app) as c:
                    c.get(f"/api/v1/library?limit={limit}")
        return all_count + one_count

    count_5 = count_endpoint_queries(5)
    count_50 = count_endpoint_queries(50)

    assert count_5 == count_50, (
        f"Endpoint query count should be constant: limit=5 => {count_5}, "
        f"limit=50 => {count_50}"
    )


def test_library_api_query_count_with_files():
    """Endpoint query count should be constant even when works have files."""
    _seed_with_files(50)

    def count_endpoint_queries(limit):
        all_count = 0
        one_count = 0
        original_all = db.all
        original_one = db.one

        def counted_all(sql: str, params=()):
            nonlocal all_count
            all_count += 1
            return original_all(sql, params)

        def counted_one(sql: str, params=()):
            nonlocal one_count
            one_count += 1
            return original_one(sql, params)

        with patch.object(db, "all", side_effect=counted_all):
            with patch.object(db, "one", side_effect=counted_one):
                with TestClient(app) as c:
                    c.get(f"/api/v1/library?limit={limit}")
        return all_count + one_count

    count_5 = count_endpoint_queries(5)
    count_50 = count_endpoint_queries(50)

    assert count_5 == count_50, (
        f"Endpoint query count with files should be constant: limit=5 => {count_5}, "
        f"limit=50 => {count_50}"
    )


# ---------------------------------------------------------------------------
# Output equivalence: _batch_library_summary vs _library_summary
# ---------------------------------------------------------------------------

def test_batch_vs_single_output_equivalence():
    """_batch_library_summary output should match _library_summary for each work.

    Seeds works with assets, files, series, tags, ratings and read states so
    every code path is exercised.  Compares every field including nested
    assets/files and tag dicts.
    """
    work_ids = _seed_with_files(5)
    t = db.now()
    # Add series memberships
    sid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, "Test Series", t, t))
        for wid in work_ids:
            conn.execute(
                "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,1)",
                (wid, sid),
            )
    # Add tags
    tid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO tags(id,name,kind,color,created_at) VALUES (?,?,?,?,?)", (tid, "test-tag", "manual", "#ff0000", t))
        for wid in work_ids:
            conn.execute(
                "INSERT INTO work_tags(work_id,tag_id,source,confidence,created_at) VALUES (?,?,?,?,?)",
                (wid, tid, "manual", 1.0, t),
            )
    # Add ratings and read states for the test user
    uid = "test-user"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id,username,auth_source,is_admin,access_scope,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (uid, uid, "local", 0, "full", t, t),
        )
        conn.execute(
            "INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,?,?)",
            (work_ids[0], uid, 5, t),
        )
        conn.execute(
            "INSERT INTO read_states(work_id,user_id,status,updated_at) VALUES (?,?,?,?)",
            (work_ids[1], uid, "reading", t),
        )

    works = db.all("SELECT * FROM works WHERE id IN (%s)" % ",".join("?" for _ in work_ids), tuple(work_ids))  # nosec B608
    works = sorted(works, key=lambda w: w["id"])

    user = {"id": uid, "username": "test"}

    # Build summaries via the batch path
    batch_results = _batch_library_summary(works, user=user)

    # Build summaries via the single-work path
    single_results = [_library_summary(w, user=user) for w in works]

    assert len(batch_results) == len(single_results)

    assert batch_results and any(r["tags"] and r["series"] for r in batch_results)
    assert any(r["assets"][0]["files"] for r in batch_results)
    # Full structural equality: every key, nested asset, file and tag dict.
    for batch, single in zip(batch_results, single_results):
        assert batch == single, f"Mismatch for work {batch['id']}: batch={batch!r} vs single={single!r}"


# ---------------------------------------------------------------------------
# OPDS pagination tests
# ---------------------------------------------------------------------------

def _walk_v2(c, limit):
    """Follow next links from the first page; return the list of pages."""
    pages = []
    href = f"/opds/v2/catalog.json?limit={limit}"
    while href:
        page = c.get(href).json()
        pages.append(page)
        assert len(page["publications"]) <= limit
        nxt = [l["href"] for l in page["links"] if l["rel"] == "next"]
        href = nxt[0] if nxt else None
        assert len(pages) < 100, "next links never terminate"
    return pages


def _walk_v1(c, limit):
    import re
    from xml.sax.saxutils import unescape
    pages = []
    href = f"/opds/v1/catalog.xml?limit={limit}"
    while href:
        resp = c.get(href)
        assert resp.status_code == 200
        pages.append(resp.text)
        m = re.search(r'<link rel="next" href="([^"]+)"', resp.text)
        href = unescape(m.group(1)) if m else None
        assert len(pages) < 100, "next links never terminate"
    return pages


def _seed_mixed(n_with: int, n_without: int) -> set[str]:
    """Interleave works with files, works with no assets and works whose only
    file is missing, so skipped works fall inside every LIMIT/OFFSET window."""
    t = db.now()
    with_files: set[str] = set()
    for i in range(n_with + n_without):
        wid = migration._find_or_create_stub(
            {"title": f"Book {i:03d}", "author": "Craig Alanson", "identifiers": {}}
        )
        if i % 3 == 1 and n_without:
            n_without -= 1
            if n_without % 2:  # asset whose only file is missing
                aid = db.uid()
                with db.tx() as conn:
                    conn.execute(
                        "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (aid, wid, "ebook", "epub", "owned", 0, 0, t, t))
                    conn.execute(
                        "INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,container,missing,discovered_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (db.uid(), aid, f"/books/gone-{i}.epub", 1, 1, "epub", 1, t))
            continue
        aid = db.uid()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (aid, wid, "ebook", "epub", "owned", 0, 0, t, t))
            conn.execute(
                "INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,container,missing,discovered_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (db.uid(), aid, f"/books/book-{i:03d}.epub", 1, 1024, "epub", 0, t))
        with_files.add(wid)
    return with_files


def test_opds_v2_pagination():
    """OPDS v2 returns bounded pages with a working next link, ending cleanly."""
    _seed_with_files(30)
    with TestClient(app) as c:
        pages = _walk_v2(c, 10)
    assert [len(p["publications"]) for p in pages] == [10, 10, 10]
    assert all("next" in [l["rel"] for l in p["links"]] for p in pages[:-1])
    assert "next" not in [l["rel"] for l in pages[-1]["links"]]
    assert "offset=10" in [l["href"] for l in pages[0]["links"] if l["rel"] == "next"][0]


def test_opds_v2_walk_lists_each_work_once_when_some_have_no_files():
    """Works without downloadable files must not make pages repeat or drop items."""
    expected = _seed_mixed(n_with=14, n_without=7)
    with TestClient(app) as c:
        for limit in (3, 5, 50):
            pages = _walk_v2(c, limit)
            ids = [p["metadata"]["identifier"] for page in pages for p in page["publications"]]
            assert len(ids) == len(set(ids)), f"duplicates at limit={limit}"
            assert set(ids) == expected, f"wrong works at limit={limit}"
            assert all(pg["metadata"]["numberOfItems"] == len(expected) for pg in pages)


def test_opds_v1_walk_lists_each_work_once_when_some_have_no_files():
    import re
    expected = _seed_mixed(n_with=14, n_without=7)
    with TestClient(app) as c:
        pages = _walk_v1(c, 4)
    ids = [i for body in pages for i in re.findall(r"<id>urn:uuid:([^<]+)</id>", body)]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected
    assert len(pages) == 4  # 14 works at 4 per page


def test_opds_v1_pagination():
    """OPDS v1 accepts limit/offset, is bounded, and its next link is followable."""
    _seed_with_files(30)
    with TestClient(app) as c:
        pages = _walk_v1(c, 10)
    assert [b.count("<entry>") for b in pages] == [10, 10, 10]
    assert all('rel="next"' in b for b in pages[:-1])
    assert 'rel="next"' not in pages[-1]
    assert all("2026-01-01T00:00:00Z" not in b for b in pages)


def test_opds_v1_updated_timestamp_is_real():
    """The <updated> element should reflect actual work timestamps."""
    _seed_with_files(1)
    with TestClient(app) as c:
        resp = c.get("/opds/v1/catalog.xml")
    assert resp.status_code == 200
    body = resp.text
    # Should have a valid ISO 8601 timestamp
    import re
    match = re.search(r"<updated>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)</updated>", body)
    assert match is not None, f"Expected ISO timestamp in <updated>, got: {body[:500]}"
    import datetime
    newest = db.one("SELECT MAX(updated_at) m FROM works")["m"]
    expected = datetime.datetime.fromtimestamp(newest, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert match.group(1) == expected


def test_works_updated_at_column_is_numeric():
    """The OPDS <updated> conversion assumes works.updated_at is a numeric epoch.

    Schema source: ``updated_at REAL NOT NULL`` in the ``works`` table of
    bookdoe/app/db.py (SCHEMA); this test fails if that definition drifts.
    """
    _seed_with_files(1)
    cols = {r["name"]: r for r in db.all("PRAGMA table_info(works)")}
    assert cols["updated_at"]["type"].upper() == "REAL"
    assert cols["updated_at"]["notnull"] == 1
    assert db.one("SELECT typeof(updated_at) t FROM works")["t"] == "real"


def test_opds_v1_has_next_link():
    """OPDS v1 should emit a <link rel="next"> when there are more pages."""
    _seed_with_files(30)
    with TestClient(app) as c:
        resp = c.get("/opds/v1/catalog.xml?limit=10")
    assert resp.status_code == 200
    body = resp.text
    assert 'rel="next"' in body, "OPDS v1 should include a next link"
    # The & in the URL is XML-escaped by saxutils.escape
    assert "offset=10" in body or "&amp;offset=10" in body, (
        f"Next link should have the correct offset. Body: {body}"
    )


def test_opds_v2_numberOfItems_reports_total():
    """numberOfItems reports total listable works, not just page length."""
    _seed_with_files(30)
    with TestClient(app) as c:
        page1 = c.get("/opds/v2/catalog.json?limit=10").json()
    # numberOfItems should be the total count, not len(publications)
    assert page1["metadata"]["numberOfItems"] == 30, (
        f"numberOfItems should be total (30), got {page1['metadata']['numberOfItems']}"
    )
    assert len(page1["publications"]) == 10, "Page should have 10 items"


def test_opds_v1_next_link_empty_library():
    """OPDS v1 should not emit a next link when there are no works."""
    with TestClient(app) as c:
        resp = c.get("/opds/v1/catalog.xml")
    assert resp.status_code == 200
    body = resp.text
    assert 'rel="next"' not in body, "Empty library should not have a next link"


def test_works_updated_at_float_epoch_round_trips_into_opds_updated():
    """A fractional epoch written to works.updated_at reads back unchanged and
    is what the OPDS <updated> element is derived from."""
    wid = _seed_with_files(1)[0]
    epoch = 1700000000.75
    with db.tx() as conn:
        conn.execute("UPDATE works SET updated_at=? WHERE id=?", (epoch, wid))
    row = db.one("SELECT updated_at, typeof(updated_at) t FROM works WHERE id=?", (wid,))
    assert row["updated_at"] == epoch
    assert row["t"] == "real"
    with TestClient(app) as c:
        resp = c.get("/opds/v1/catalog.xml")
    assert resp.status_code == 200
    body = resp.text
    # Per opds1() in app/main.py, entries carry no <updated>; the only one is
    # the feed-level element, which is max(updated_at) over the works on the
    # page. Pin the page to exactly the seeded work so that max is this work's
    # timestamp, not another's or now(). The regex tolerates attributes on <entry>.
    assert len(re.findall(r"<entry[\s>]", body)) == 1
    assert f"<id>urn:uuid:{wid}</id>" in body
    # 1700000000.75 is formatted with %S, so the .75 fraction is truncated.
    assert body.count("<updated>") == 1
    assert "<updated>2023-11-14T22:13:20Z</updated>" in body
