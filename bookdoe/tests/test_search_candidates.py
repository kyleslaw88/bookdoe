"""Ephemeral search candidates (vNext Part XVII.D).

Federated search results must never create a canonical Work on their own --
they land in search_candidates only, and only promote_candidate() (reached by
an explicit user select/add, resolver adoption, author/series discovery
import, or an automation that explicitly requires it) turns one into a Work.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import search_candidates as sc
from app.services import search_providers as sp


def _stub_provider(name, results, *, enabled=True):
    class Stub(sp.SearchProvider):
        def __init__(self):
            self.name = name

        def enabled(self):
            return enabled

        def search(self, query, limit):
            return results

    return Stub()


def _set_providers(monkeypatch, **by_name):
    for name, results in by_name.items():
        monkeypatch.setitem(sp._PROVIDERS, name, _stub_provider(name, results))


def _count_works() -> int:
    return db.one("SELECT COUNT(*) n FROM works")["n"]


# -- build_candidates: never creates a canonical Work -------------------------

def test_build_candidates_never_creates_a_work(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Some Book", authors=["Some Author"])])

    sc.build_candidates("some book", providers=["p1"])

    assert _count_works() == 0
    rows = db.all("SELECT * FROM search_candidates")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["promoted_work_id"] is None


def test_build_candidates_returns_full_part_xvii_d_field_shape(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result(
        "p1", title="Full Shape", authors=["A. Uthor"],
        identifiers=[{"namespace": "isbn", "value": "9780000000009"}],
        publication_date="2020", cover_url="https://example.invalid/c.jpg",
    )])

    [candidate] = sc.build_candidates("full shape", providers=["p1"])

    for field in (
        "title", "subtitle", "authors", "contributors", "series", "series_position",
        "identifiers", "publication_date", "publisher", "language", "cover_refs",
        "providers", "provider_evidence", "match_score", "match_explanation",
    ):
        assert field in candidate, f"missing {field}"
    assert candidate["title"] == "Full Shape"
    assert candidate["authors"] == ["A. Uthor"]
    assert candidate["cover_refs"] == ["https://example.invalid/c.jpg"]
    assert candidate["providers"] == ["p1"]
    assert "p1" in candidate["provider_evidence"]


# -- build_candidates: cross-provider merge -----------------------------------

def test_build_candidates_merges_results_sharing_an_identifier(monkeypatch):
    ident = [{"namespace": "isbn", "value": "9781234567897"}]
    _set_providers(
        monkeypatch,
        p1=[sp._result("p1", title="Shared Book", authors=["Author One"], identifiers=ident, match_score=0.5)],
        p2=[sp._result("p2", title="Shared Book Alt Title", authors=["Author Two"], identifiers=ident, match_score=0.9)],
    )

    candidates = sc.build_candidates("shared book", providers=["p1", "p2"])

    assert len(candidates) == 1
    c = candidates[0]
    assert set(c["providers"]) == {"p1", "p2"}
    assert set(c["provider_evidence"].keys()) == {"p1", "p2"}
    assert c["match_score"] == 0.9
    assert set(c["authors"]) == {"Author One", "Author Two"}
    assert "2 providers" in c["match_explanation"]


def test_build_candidates_merges_results_sharing_title_and_author_without_identifiers(monkeypatch):
    _set_providers(
        monkeypatch,
        p1=[sp._result("p1", title="No Identifiers Here", authors=["Same Author"])],
        p2=[sp._result("p2", title="No Identifiers Here", authors=["Same Author"])],
    )

    candidates = sc.build_candidates("no identifiers here", providers=["p1", "p2"])

    assert len(candidates) == 1
    assert set(candidates[0]["providers"]) == {"p1", "p2"}


def test_build_candidates_keeps_distinct_books_as_separate_candidates(monkeypatch):
    _set_providers(
        monkeypatch,
        p1=[sp._result("p1", title="Book One", authors=["Author One"])],
        p2=[sp._result("p2", title="Book Two", authors=["Author Two"])],
    )

    candidates = sc.build_candidates("book", providers=["p1", "p2"])

    assert {c["title"] for c in candidates} == {"Book One", "Book Two"}
    assert len(db.all("SELECT * FROM search_candidates")) == 2


# -- build_candidates: normalization / enrichment ------------------------------

def test_build_candidates_splits_subtitle_when_provider_does_not(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Main Title: The Subtitle")])

    [c] = sc.build_candidates("main title", providers=["p1"])

    assert c["title"] == "Main Title"
    assert c["subtitle"] == "The Subtitle"


def test_build_candidates_extracts_series_position_from_series_string(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Some Book", series=["Foundation #2"])])

    [c] = sc.build_candidates("some book", providers=["p1"])

    assert c["series"] == ["Foundation"]
    assert c["series_position"] == "2"


# -- promote_candidate: the only path into canonical works ---------------------

def _seed_candidate(**overrides) -> str:
    cid, t = db.uid(), db.now()
    fields = {
        "id": cid, "query": "q", "title": "Promotable Book", "subtitle": None,
        "authors_json": db.json_dump(["Jane Author"]), "contributors_json": "[]",
        "series_json": db.json_dump(["Some Series"]), "series_position": "3",
        "identifiers_json": db.json_dump([{"namespace": "isbn", "value": "9789999999999"}]),
        "publication_date": "2022", "publisher": "Acme Press", "language": "en",
        "cover_refs_json": db.json_dump(["https://example.invalid/cover.jpg"]),
        "providers_json": db.json_dump(["openlibrary"]), "provider_evidence_json": "{}",
        "match_score": 0.8, "match_explanation": "from openlibrary", "status": "pending",
        "created_at": t, "updated_at": t,
    }
    fields.update(overrides)
    cols = ",".join(fields)
    placeholders = ",".join("?" for _ in fields)
    with db.tx() as conn:
        conn.execute(f"INSERT INTO search_candidates({cols}) VALUES ({placeholders})", tuple(fields.values()))  # nosec B608
    return cid


def test_promote_candidate_creates_canonical_work_with_metadata():
    cid = _seed_candidate()

    work_id = sc.promote_candidate(cid)

    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    assert work["title"] == "Promotable Book"
    assert work["primary_language"] == "en"
    assert work["metadata_source"] == "candidate"
    assert work["cover_url"] == "https://example.invalid/cover.jpg"

    authors = db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=?",
        (work_id,),
    )
    assert [a["name"] for a in authors] == ["Jane Author"]

    membership = db.one(
        "SELECT sm.position, s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?",
        (work_id,),
    )
    assert membership["name"] == "Some Series"
    assert membership["position"] == "3"

    ident = db.one("SELECT * FROM identifiers WHERE entity_type='work' AND entity_id=?", (work_id,))
    assert ident["namespace"] == "isbn" and ident["value"] == "9789999999999"

    candidate = db.one("SELECT * FROM search_candidates WHERE id=?", (cid,))
    assert candidate["status"] == "promoted"
    assert candidate["promoted_work_id"] == work_id


def test_promote_candidate_does_not_imply_acquisition_intent():
    """Promoting into the catalog must not silently create Wanted state (Part XI)."""
    cid = _seed_candidate()

    work_id = sc.promote_candidate(cid)

    assets = db.all("SELECT * FROM media_assets WHERE work_id=?", (work_id,))
    assert {a["kind"] for a in assets} == {"ebook", "audiobook"}
    for a in assets:
        assert a["status"] == "none"
        assert a["monitored"] == 0


def test_promote_candidate_is_idempotent():
    cid = _seed_candidate()

    first = sc.promote_candidate(cid)
    second = sc.promote_candidate(cid)

    assert first == second
    assert db.one("SELECT COUNT(*) n FROM works")["n"] == 1


def test_promote_candidate_reuses_existing_unlinked_contributor():
    t = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("existing-author", "Jane Author", "Author, Jane", t, t),
        )
    cid = _seed_candidate()

    work_id = sc.promote_candidate(cid)

    authors = db.all("SELECT id FROM contributors WHERE name='Jane Author'")
    assert len(authors) == 1
    assert authors[0]["id"] == "existing-author"


def test_promote_candidate_raises_for_unknown_id():
    with pytest.raises(ValueError):
        sc.promote_candidate("does-not-exist")


def test_promote_candidate_raises_when_candidate_has_no_title():
    cid = _seed_candidate(title=None)
    with pytest.raises(ValueError):
        sc.promote_candidate(cid)


# ---------- P4-01: promote_candidate(work_id=...) link-to-existing-Work ----------

def _seed_existing_work(title="Existing Work", author="Existing Author"):
    wid, existing_author_id, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)",
            (wid, title, title, t, t),
        )
        c.execute(
            "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
            (existing_author_id, author, author, t, t),
        )
        c.execute(
            "INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)",
            (wid, existing_author_id),
        )
    return wid


def test_promote_candidate_with_work_id_links_instead_of_creating_a_work():
    wid = _seed_existing_work()
    cid = _seed_candidate()  # title="Promotable Book", authors=["Jane Author"], series=["Some Series"]

    result = sc.promote_candidate(cid, work_id=wid)

    assert result == wid
    assert _count_works() == 1  # no second Work minted

    candidate = db.one("SELECT * FROM search_candidates WHERE id=?", (cid,))
    assert candidate["status"] == "promoted"
    assert candidate["promoted_work_id"] == wid

    authors = {
        r["name"] for r in db.all(
            "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=?",
            (wid,),
        )
    }
    assert authors == {"Existing Author", "Jane Author"}  # merged, not replaced

    ident = db.one("SELECT * FROM identifiers WHERE entity_type='work' AND entity_id=?", (wid,))
    assert ident["namespace"] == "isbn" and ident["value"] == "9789999999999"

    membership = db.one(
        "SELECT s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?",
        (wid,),
    )
    assert membership["name"] == "Some Series"


def test_promote_candidate_with_work_id_reuses_an_already_linked_contributor():
    wid = _seed_existing_work(author="Jane Author")  # same name the candidate's own author list carries
    cid = _seed_candidate()

    sc.promote_candidate(cid, work_id=wid)

    authors = db.all("SELECT id FROM contributors WHERE name='Jane Author'")
    assert len(authors) == 1  # no duplicate contributor row minted
    linked = db.all("SELECT contributor_id FROM work_contributors WHERE work_id=?", (wid,))
    assert len(linked) == 1  # INSERT OR IGNORE deduped the (work_id,contributor_id,role) pair


def test_promote_candidate_raises_for_unknown_work_id():
    cid = _seed_candidate()
    with pytest.raises(ValueError):
        sc.promote_candidate(cid, work_id="does-not-exist")
    # Nothing partially applied on failure.
    assert db.one("SELECT status FROM search_candidates WHERE id=?", (cid,))["status"] == "pending"


def test_promote_candidate_with_work_id_is_still_idempotent():
    wid = _seed_existing_work()
    cid = _seed_candidate()

    first = sc.promote_candidate(cid, work_id=wid)
    second = sc.promote_candidate(cid, work_id=wid)

    assert first == second == wid
    assert _count_works() == 1


def test_promote_candidate_without_work_id_still_creates_a_new_work_unchanged():
    """Regression: the work_id parameter must not disturb the existing no-override path."""
    cid = _seed_candidate()
    work_id = sc.promote_candidate(cid)
    assert _count_works() == 1
    assert db.one("SELECT id FROM works WHERE id=?", (work_id,)) is not None


# ---------- P4-01: dismiss_candidate ----------

def test_dismiss_candidate_sets_status_without_creating_a_work():
    cid = _seed_candidate()
    sc.dismiss_candidate(cid)
    candidate = db.one("SELECT * FROM search_candidates WHERE id=?", (cid,))
    assert candidate["status"] == "dismissed"
    assert candidate["promoted_work_id"] is None
    assert _count_works() == 0


def test_dismiss_candidate_raises_for_unknown_id():
    with pytest.raises(ValueError):
        sc.dismiss_candidate("does-not-exist")


# -- API endpoints --------------------------------------------------------------

def test_federated_search_endpoint_returns_candidates_without_creating_a_work(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Endpoint Book", authors=["Endpoint Author"])])

    with TestClient(app) as client:
        resp = client.get("/api/v1/search/federated?q=endpoint+book&providers=p1")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Endpoint Book"
    assert _count_works() == 0


def test_search_candidates_list_and_detail_endpoints(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Listed Book")])
    with TestClient(app) as client:
        client.get("/api/v1/search/federated?q=listed+book&providers=p1")
        body = client.get("/api/v1/search-candidates").json()
        assert body["total"] == 1
        cid = body["items"][0]["id"]

        detail = client.get(f"/api/v1/search-candidates/{cid}").json()
        assert detail["title"] == "Listed Book"

        missing = client.get("/api/v1/search-candidates/does-not-exist")
    assert missing.status_code == 404


def test_search_candidates_list_rejects_unknown_status():
    with TestClient(app) as client:
        resp = client.get("/api/v1/search-candidates?status=bogus")
    assert resp.status_code == 400


def test_promote_endpoint_creates_work_and_returns_work_detail(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Promote Me", authors=["Promote Author"])])
    with TestClient(app) as client:
        search_body = client.get("/api/v1/search/federated?q=promote+me&providers=p1").json()
        cid = search_body["items"][0]["id"]

        resp = client.post(f"/api/v1/search-candidates/{cid}/promote", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Promote Me"
    assert _count_works() == 1


def test_promote_endpoint_returns_400_for_unknown_candidate():
    with TestClient(app) as client:
        resp = client.post("/api/v1/search-candidates/does-not-exist/promote", json={})
    assert resp.status_code == 400


def test_promote_endpoint_with_work_id_links_into_existing_work(monkeypatch):
    wid = _seed_existing_work()
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Link Me", authors=["Link Author"])])
    with TestClient(app) as client:
        search_body = client.get("/api/v1/search/federated?q=link+me&providers=p1").json()
        cid = search_body["items"][0]["id"]

        resp = client.post(f"/api/v1/search-candidates/{cid}/promote", json={"work_id": wid})

    assert resp.status_code == 200
    assert resp.json()["id"] == wid
    assert _count_works() == 1


def test_promote_endpoint_returns_400_for_unknown_work_id(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Bad Link", authors=["Author"])])
    with TestClient(app) as client:
        search_body = client.get("/api/v1/search/federated?q=bad+link&providers=p1").json()
        cid = search_body["items"][0]["id"]

        resp = client.post(f"/api/v1/search-candidates/{cid}/promote", json={"work_id": "does-not-exist"})
    assert resp.status_code == 400


def test_dismiss_endpoint_sets_status(monkeypatch):
    _set_providers(monkeypatch, p1=[sp._result("p1", title="Dismiss Me")])
    with TestClient(app) as client:
        search_body = client.get("/api/v1/search/federated?q=dismiss+me&providers=p1").json()
        cid = search_body["items"][0]["id"]

        resp = client.post(f"/api/v1/search-candidates/{cid}/dismiss")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        detail = client.get(f"/api/v1/search-candidates/{cid}").json()
    assert detail["status"] == "dismissed"
    assert _count_works() == 0


def test_dismiss_endpoint_returns_400_for_unknown_candidate():
    with TestClient(app) as client:
        resp = client.post("/api/v1/search-candidates/does-not-exist/dismiss")
    assert resp.status_code == 400


def test_list_endpoint_accepts_dismissed_status_filter():
    cid = _seed_candidate(status="dismissed")
    with TestClient(app) as client:
        body = client.get("/api/v1/search-candidates?status=dismissed").json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == cid


def test_explain_reads_state_constants(monkeypatch):
    """_explain takes its states from STATE_*: collapsing UNAVAILABLE onto EXACT surfaces every signal."""
    merged = {"providers": ["openlibrary"], "identifiers": [], "title": "Dune", "series": "Dune"}
    assert sc._explain(merged) == "from openlibrary; title match; series match"
    monkeypatch.setattr(sc, "STATE_UNAVAILABLE", sc.STATE_EXACT)
    assert sc._explain(merged) == "from openlibrary; identifier match; title match; series match"
