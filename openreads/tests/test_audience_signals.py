"""Audience signals for the kids classifier: Google Books categories / maturityRating and Open
Library subjects, captured through the existing provider layer (googlebooks.py, openlibrary.py,
resources.ensure_work_audience) and exposed on the canonical work.

The JSON in tests/fixtures/audience is the shape each provider returns (volumeInfo with
categories + maturityRating; a work record with subjects), one juvenile, one young-adult, one
mature and one ambiguous book each. No test touches the network: the providers' HTTP clients
are replaced by httpx mock transports.
"""
import json
import os
import pathlib
import tempfile

import httpx
import pytest

# db reads DB_PATH at import; set it first like the other test modules do, because this module
# sorts (and so is collected) before them and would otherwise pin db.DB_PATH to /data.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "audience-import.db"))

import db  # noqa: E402
import googlebooks as gb  # noqa: E402
import openlibrary as ol  # noqa: E402
import resources  # noqa: E402
import v2  # noqa: E402
import v2db  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures" / "audience"


def fixture(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "openreads.db"))
    db.init_db()
    v2db.init_db()
    monkeypatch.setattr(gb, "API_KEY", "test-key")
    monkeypatch.setattr(gb, "_backoff_until", 0.0)


class Providers:
    """Counts and serves provider calls from fixtures."""

    def __init__(self, monkeypatch, google=None, open_library=None, google_status=200, ol_status=200):
        self.google_calls, self.ol_calls = [], []

        def google_handler(request):
            self.google_calls.append(str(request.url))
            return httpx.Response(google_status, json=fixture(google) if google else {"totalItems": 0})

        def ol_handler(request):
            self.ol_calls.append(str(request.url))
            return httpx.Response(ol_status, json=fixture(open_library) if open_library else {})

        monkeypatch.setattr(gb, "_client", httpx.Client(base_url=gb.BASE, transport=httpx.MockTransport(google_handler)))
        monkeypatch.setattr(ol, "_client", httpx.Client(base_url=ol.BASE, transport=httpx.MockTransport(ol_handler)))


def make_work(title="The Lighthouse Map", author="Ann Writer", ol_key="OL1W"):
    wid = db.get_or_create_work(ol_key, title)
    aid = db.get_or_create_author("OL1A", author)
    db.link_work_author(wid, aid)
    return wid


@pytest.mark.parametrize("name,categories,maturity", [
    ("google_juvenile.json", ["Juvenile Fiction / Action & Adventure / General"], "NOT_MATURE"),
    ("google_ya.json", ["Young Adult Fiction / Fantasy / General"], "NOT_MATURE"),
    ("google_mature.json", ["Fiction / Romance / Erotica"], "MATURE"),
    ("google_ambiguous.json", ["Fiction / General"], "NOT_MATURE"),
    ("google_nocategories.json", [], "NOT_MATURE"),
])
def test_google_volume_info_keeps_categories_and_maturity(name, categories, maturity):
    info = gb._volume_info(fixture(name)["items"][0])
    assert info["categories"] == categories
    assert info["maturity_rating"] == maturity
    assert info["language"] == "en"


@pytest.mark.parametrize("name,expected", [
    ("openlibrary_juvenile.json", ["Juvenile fiction", "Lighthouses", "Adventure and adventurers", "Open Library Staff Picks"]),
    ("openlibrary_ya.json", ["Young adult fiction", "Fantasy fiction", "Queens"]),
    ("openlibrary_mature.json", ["Erotica", "Romance", "Billionaires"]),
    ("openlibrary_ambiguous.json", ["Fiction", "Road trips", "Accessible book", "nyt:paperback-fiction=2011-01-01"]),
])
def test_openlibrary_work_subjects_are_read_in_one_call(monkeypatch, name, expected):
    p = Providers(monkeypatch, open_library=name)
    got = ol.get_work_subjects("OL1W")
    assert got["subjects"] == expected
    assert p.ol_calls == ["https://openlibrary.org/works/OL1W.json"]


def test_openlibrary_subject_places_and_garbage_values(monkeypatch):
    Providers(monkeypatch, open_library="openlibrary_juvenile.json")
    assert ol.get_work_subjects("OL1W")["subject_places"] == ["Maine"]
    assert ol._string_list(["a", "", None, 5, "a", " b "]) == ["a", "b"]
    assert ol._string_list("not a list") == []


def audience(cid):
    return {(a["provider"], a["field"]): a["value"] for a in v2db.audience_for_work(cid)}


def test_ensure_work_audience_stores_both_providers_and_exposes_them_on_the_canonical_work(monkeypatch):
    p = Providers(monkeypatch, google="google_mature.json", open_library="openlibrary_mature.json")
    wid = make_work("Midnight Contract", "R. Vale")
    out = resources.ensure_work_audience(wid)
    assert out["complete"] is True
    assert out["status"] == {"openlibrary": "ok", "google_books": "ok"}
    cid = v2db.sync_work(wid)
    got = audience(cid)
    assert got[("google_books", "categories")] == ["Fiction / Romance / Erotica"]
    assert got[("google_books", "maturity_rating")] == "MATURE"
    assert got[("openlibrary", "subjects")] == ["Erotica", "Romance", "Billionaires"]
    assert got[("google_books", "publisher")] == "Ardent Press"
    assert {a["provider"] for a in v2db.canonical_work(cid)["audience"]} == {"google_books", "openlibrary"}


def test_a_second_call_asks_no_provider(monkeypatch):
    p = Providers(monkeypatch, google="google_juvenile.json", open_library="openlibrary_juvenile.json")
    wid = make_work()
    resources.ensure_work_audience(wid)
    calls = (len(p.google_calls), len(p.ol_calls))
    again = resources.ensure_work_audience(wid)
    assert (len(p.google_calls), len(p.ol_calls)) == calls == (1, 1)
    assert again["status"] == {"openlibrary": "stored", "google_books": "stored"} and again["complete"] is True


def test_google_is_asked_by_isbn_when_an_edition_has_one(monkeypatch):
    p = Providers(monkeypatch, google="google_juvenile.json", open_library="openlibrary_juvenile.json")
    wid = make_work()
    eid = db.get_or_create_edition("OL1M", wid, "Edition")
    db.update_edition(eid, isbn13="9780000000000")
    resources.ensure_work_audience(wid)
    assert len(p.google_calls) == 1 and "isbn%3A9780000000000" in p.google_calls[0].replace(":", "%3A")


def test_no_api_key_skips_google_but_still_reads_open_library(monkeypatch):
    monkeypatch.setattr(gb, "API_KEY", "")
    p = Providers(monkeypatch, google="google_juvenile.json", open_library="openlibrary_juvenile.json")
    out = resources.ensure_work_audience(make_work())
    assert p.google_calls == [] and out["status"]["google_books"] == "disabled" and out["complete"] is True
    assert out["status"]["openlibrary"] == "ok"


def test_a_rate_limited_google_answer_is_deferred_not_recorded_as_nothing(monkeypatch):
    p = Providers(monkeypatch, google="google_juvenile.json", open_library="openlibrary_juvenile.json", google_status=429)
    wid = make_work()
    out = resources.ensure_work_audience(wid)
    assert out["status"]["google_books"] == "deferred" and out["complete"] is False
    assert db.work_by_id(wid)["audience_attempted_at"] is None  # must be retried, not marked answered
    # while the breaker is open Google is not even called again
    calls = len(p.google_calls)
    resources.ensure_work_audience(wid)
    assert len(p.google_calls) == calls
    # once the breaker closes and Google answers, it completes
    monkeypatch.setattr(gb, "_backoff_until", 0.0)
    Providers(monkeypatch, google="google_juvenile.json", open_library="openlibrary_juvenile.json")
    done = resources.ensure_work_audience(wid)
    assert done["complete"] is True and done["status"]["google_books"] == "ok"


def test_an_open_library_failure_is_incomplete_and_does_not_hide_google(monkeypatch):
    p = Providers(monkeypatch, google="google_ya.json", open_library="openlibrary_ya.json", ol_status=503)
    out = resources.ensure_work_audience(make_work("Crown of Ash", "Sam Teller"))
    assert out["status"] == {"openlibrary": "error", "google_books": "ok"} and out["complete"] is False
    assert any(a["provider"] == "google_books" for a in out["audience"])


def test_a_work_google_has_nothing_for_is_remembered(monkeypatch):
    p = Providers(monkeypatch, google=None, open_library="openlibrary_ambiguous.json")
    wid = make_work("Obscure", "Nobody")
    first = resources.ensure_work_audience(wid)
    assert first["status"]["google_books"] == "none" and first["complete"] is True
    resources.ensure_work_audience(wid)
    assert len(p.google_calls) == 1


def test_the_audience_endpoint_returns_the_signals(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    Providers(monkeypatch, google="google_ya.json", open_library="openlibrary_ya.json")
    wid = make_work("Crown of Ash", "Sam Teller")
    cid = v2db.sync_work(wid)
    app = FastAPI()
    app.include_router(v2.router)
    client = TestClient(app)
    r = client.post(f"/api/v2/works/{cid}/audience")
    assert r.status_code == 200
    body = r.json()
    assert body["complete"] is True
    assert {(a["provider"], a["field"]) for a in body["audience"]} >= {("google_books", "categories"), ("openlibrary", "subjects")}
    assert client.post("/api/v2/works/does-not-exist/audience").status_code == 404


def test_ordinary_work_refresh_records_subjects_for_free(monkeypatch):
    """ensure_work already fetches the work record; the subjects in it are stored with no extra call."""
    wid = make_work()
    monkeypatch.setattr(ol, "get_work", lambda key: {"title": "The Lighthouse Map", "description": "d", "author_keys": [], "series": [],
                                                      "subjects": ["Juvenile fiction"], "subject_places": []})
    monkeypatch.setattr(resources, "_enrich_work_with_google", lambda *a, **k: None)
    resources.ensure_work(wid, force=True)
    cid = v2db.sync_work(wid)
    assert audience(cid)[("openlibrary", "subjects")] == ["Juvenile fiction"]
