import os

import db
import v2db


def setup_store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "v2.db"))
    db.init_db()
    v2db.init_db()


def test_canonical_sync_preserves_provider_independent_identity(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    aid = db.get_or_create_author("OL1A", "Jane Author")
    wid = db.get_or_create_work("OL2W", "Test Work")
    db.link_work_author(wid, aid)
    db.update_work(wid, title="Test Work", description="A description")
    eid = db.get_or_create_edition("OL3M", wid, "Test Edition")
    db.update_edition(eid, isbn13="9781234567897", publisher="Example House")

    cid = v2db.sync_work(wid)
    v2db.sync_edition(eid)
    work = v2db.canonical_work(cid)
    editions = v2db.editions_for_work(cid)

    assert cid and cid != "OL2W"
    assert work["title"] == "Test Work"
    assert work["contributors"][0]["name"] == "Jane Author"
    assert editions[0]["identifiers"][0]["namespace"] in {"openlibrary_edition", "isbn13"}
    assert any(x["namespace"] == "isbn13" and x["value"] == "9781234567897" for x in editions[0]["identifiers"])


def test_manual_metadata_lock_survives_provider_resync(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    wid = db.get_or_create_work("OL2W", "Provider Title")
    cid = v2db.sync_work(wid)
    v2db.set_decision("work", cid, "title", "My Preferred Title", lock=True)
    db.update_work(wid, title="Provider Changed Title")
    v2db.sync_work(wid)
    meta = v2db.metadata("work", cid)
    title = next(x for x in meta["decisions"] if x["field"] == "title")
    assert title["value"] == "My Preferred Title"
    assert title["locked"] is True


def test_search_goes_upstream_when_the_cache_cannot_fill_the_requested_limit(tmp_path, monkeypatch):
    """A "More results" control is only honest if raising the limit can return more.

    The old rule short-circuited once the local cache held 8 rows, so a caller asking
    for 30 got the same 11 cached results however high they raised the limit.
    """
    setup_store(tmp_path, monkeypatch)
    import openlibrary as ol
    import resources as res
    import v2

    for i in range(11):
        wid = db.get_or_create_work(f"OL{i}W", f"Dune Study {i}")
        v2db.sync_work(wid)

    upstream_calls = []
    monkeypatch.setattr(ol, "search", lambda q, limit=20: upstream_calls.append((q, limit)) or [])
    monkeypatch.setattr(res, "ensure_work", lambda *a, **k: None)

    # Asking for fewer than the cache holds stays local.
    v2.search(q="Dune", limit=5, _=None)
    assert upstream_calls == []

    # Asking for more than it holds must reach upstream.
    v2.search(q="Dune", limit=30, _=None)
    assert len(upstream_calls) == 1, "raising the limit must consult Open Library"


def test_search_does_not_fetch_each_result_from_openlibrary(tmp_path, monkeypatch):
    """A round trip per result cost twenty seconds on an uncached search for a
    prolific author, to fetch a description and series the result list never shows.
    Running them concurrently measured *slower* still - 5.0s against 1.9s for eight
    works - because Open Library throttles parallel reads. get_work() hydrates the one
    the user opens."""
    setup_store(tmp_path, monkeypatch)
    import openlibrary as ol
    import resources as res
    import v2

    docs = [{"key": f"/works/OL{i}W", "title": f"Book {i}", "author_key": [], "author_name": []} for i in range(8)]
    monkeypatch.setattr(ol, "search", lambda q, limit=20: docs)
    hydrated = []
    monkeypatch.setattr(res, "ensure_work", lambda *a, **k: hydrated.append(a))

    result = v2.search(q="Book", limit=8, _=None)

    assert len(result["items"]) == 8, "results still come back"
    assert hydrated == [], "search must not hydrate each result"


def test_resolve_consults_upstream_when_the_cache_cannot_answer(tmp_path, monkeypatch):
    """Resolution used to read the local cache and nothing else.

    A book that had never been searched for therefore resolved to "no candidates" -
    which is why an entire imported library sat unresolved with nothing to choose
    from in Review.
    """
    setup_store(tmp_path, monkeypatch)
    import openlibrary as ol
    import v2

    calls = []

    def fake_search(q, limit=20):
        calls.append(q)
        return [{"key": "/works/OL77W", "title": "Dead World",
                 "author_key": ["OL7A"], "author_name": ["Craig Alanson"]}]

    monkeypatch.setattr(ol, "search", fake_search)
    req = v2.ResolveRequest(title="Dead World", authors=["Craig Alanson"])

    result = v2.resolve(req, None)

    assert calls, "upstream was never consulted"
    assert "Dead World" in calls[0] and "Craig Alanson" in calls[0]
    assert result["match"] is not None
    assert result["confidence"] >= 0.95
    assert result["match"]["title"] == "Dead World"


def test_resolve_stays_local_when_the_cache_already_answers(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    import openlibrary as ol
    import v2

    aid = db.get_or_create_author("OL9A", "Craig Alanson")
    wid = db.get_or_create_work("OL9W", "Dead World")
    db.link_work_author(wid, aid)
    v2db.sync_work(wid)

    calls = []
    monkeypatch.setattr(ol, "search", lambda q, limit=20: calls.append(q) or [])

    result = v2.resolve(v2.ResolveRequest(title="Dead World", authors=["Craig Alanson"]), None)

    assert result["confidence"] >= 0.95
    assert calls == [], "a confident local answer must not cost an upstream call"


def test_upstream_can_be_turned_off_for_batch_callers(tmp_path, monkeypatch):
    setup_store(tmp_path, monkeypatch)
    import openlibrary as ol
    import v2

    calls = []
    monkeypatch.setattr(ol, "search", lambda q, limit=20: calls.append(q) or [])
    v2.resolve(v2.ResolveRequest(title="Nothing Cached", authors=["Nobody"], upstream=False), None)
    assert calls == []
