"""Per-work metadata refresh.

"Refresh metadata" used to be a button that explained the feature lived elsewhere,
and the global job it pointed at could not have helped: it only re-synced works that
already carried an OpenReads id, which no imported work did. Every one of the 12,026
migrated works was therefore permanently 'unresolved'.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import metadata, migration

META = {
    "id": "or-dead-world",
    "title": "Dead World",
    "description": "Expeditionary Force, book 15.",
    "first_publish_date": "2024",
    "primary_language": "eng",
    "contributors": [{"id": "or-alanson", "name": "Craig Alanson", "role": "author"}],
    "series": [{"id": "or-expforce", "name": "Expeditionary Force", "position": "15"}],
    "identifiers": [{"namespace": "isbn13", "value": "9781234567897", "source": "openlibrary", "verified": 1}],
    "editions": [{"id": "or-ed-1", "title": "Dead World", "language": "eng", "image_url": "https://covers.example/dead-world.jpg"}],
}


def _stub(title="Dead World", author="Craig Alanson"):
    return migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})


def test_confident_match_links_the_existing_work_in_place(monkeypatch):
    work_id = _stub()
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',1,?,?)",
            (asset_id, work_id, t, t),
        )
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": META, "confidence": 0.99, "method": "isbn"})
    monkeypatch.setattr(metadata, "get_work", lambda mid: META)

    result = metadata.refresh_work(work_id)

    assert result["status"] == "linked"
    # Crucially the same work row: upsert_work finds works by metadata_id only, so
    # refreshing an unlinked work without writing the id first would create a second
    # work and strand this one's assets and files.
    assert db.one("SELECT COUNT(*) n FROM works")["n"] == 1
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    assert work["metadata_id"] == "or-dead-world"
    assert work["metadata_status"] == "resolved"
    assert work["cover_url"] == "https://covers.example/dead-world.jpg"
    assert db.one("SELECT work_id FROM media_assets WHERE id=?", (asset_id,))["work_id"] == work_id


def test_ambiguous_match_opens_a_review_task_instead_of_guessing(monkeypatch):
    work_id = _stub()
    candidates = [{"id": "or-a", "title": "Deadworld", "contributors": [{"name": "Stuart Kerr", "role": "author"}]}]
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.75, "candidates": candidates})

    result = metadata.refresh_work(work_id)

    assert result["status"] == "review"
    assert result["candidates"][0]["title"] == "Deadworld"
    assert db.one("SELECT * FROM works WHERE id=?", (work_id,))["metadata_id"] is None
    task = db.one("SELECT * FROM review_tasks WHERE entity_id=? AND status='open'", (work_id,))
    assert task is not None and task["kind"] == "metadata_match"


def test_review_is_not_duplicated_on_repeated_refresh(monkeypatch):
    work_id = _stub()
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.7, "candidates": [{"id": "or-a", "title": "Deadworld"}]})
    metadata.refresh_work(work_id)
    metadata.refresh_work(work_id)
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE entity_id=?", (work_id,))["n"] == 1


def test_operator_choice_links_the_chosen_candidate_and_closes_the_review(monkeypatch):
    work_id = _stub()
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.7, "candidates": [{"id": "or-dead-world", "title": "Dead World"}]})
    metadata.refresh_work(work_id)
    monkeypatch.setattr(metadata, "get_work", lambda mid: META)

    result = metadata.refresh_work(work_id, metadata_id="or-dead-world")

    assert result["status"] == "linked"
    assert db.one("SELECT status FROM review_tasks WHERE entity_id=?", (work_id,))["status"] == "resolved"


def test_no_match_at_all_is_reported_honestly(monkeypatch):
    work_id = _stub()
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.0, "candidates": []})
    result = metadata.refresh_work(work_id)
    assert result["status"] == "unmatched"
    assert db.one("SELECT COUNT(*) n FROM review_tasks")["n"] == 0


def test_a_re_refresh_that_now_finds_nothing_clears_the_stale_candidate(monkeypatch):
    """Regression: a scoring fix that disqualifies an old bogus candidate must be
    reflected in an *already-open* review task, not leave it showing what a worse
    version of the matcher used to suggest."""
    work_id = _stub()
    stale_candidates = [{"id": "or-bad", "title": "The History of Detroit and Michigan", "contributors": [{"name": "Silas Farmer", "role": "author"}]}]
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.23, "candidates": stale_candidates})
    metadata.refresh_work(work_id)
    task = db.one("SELECT payload_json FROM review_tasks WHERE entity_id=? AND status='open'", (work_id,))
    assert db.json_load(task["payload_json"], {})["candidates"], "sanity: the stale candidate is there to begin with"

    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.0, "candidates": []})
    result = metadata.refresh_work(work_id)

    assert result["status"] == "unmatched"
    task = db.one("SELECT payload_json FROM review_tasks WHERE entity_id=? AND status='open'", (work_id,))
    assert task is not None, "the task stays open - unmatched is not resolved"
    assert db.json_load(task["payload_json"], {})["candidates"] == []


def test_linking_to_metadata_owned_by_another_work_is_refused(monkeypatch):
    first, second = _stub("Dead World"), _stub("Dead World Again", "Someone Else")
    monkeypatch.setattr(metadata, "get_work", lambda mid: META)
    metadata.refresh_work(first, metadata_id="or-dead-world")
    with pytest.raises(ValueError, match="already linked"):
        metadata.refresh_work(second, metadata_id="or-dead-world")


def test_refresh_endpoint_reports_the_outcome(monkeypatch):
    work_id = _stub()
    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.6, "candidates": [{"id": "or-a", "title": "Deadworld"}]})
    with TestClient(app) as c:
        r = c.post(f"/api/v1/library/{work_id}/refresh", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "review"


def test_best_cover_prefers_the_works_own_language_over_whatever_comes_first():
    """A Dune search returned the Estonian "Duin" jacket because the first edition won."""
    meta = {
        "title": "Dune",
        "primary_language": "eng",
        "editions": [
            {"title": "Duin", "language": "est", "image_url": "https://covers.example/duin.jpg"},
            {"title": "Dune", "language": "eng", "image_url": "https://covers.example/dune.jpg"},
        ],
    }
    assert metadata._best_cover(meta) == "https://covers.example/dune.jpg"


def test_best_cover_does_not_penalise_untagged_editions():
    meta = {
        "title": "Dune",
        "primary_language": "eng",
        "editions": [
            {"title": "Dune", "language": None, "image_url": "https://covers.example/untagged.jpg"},
            {"title": "Duna", "language": "spa", "image_url": "https://covers.example/spanish.jpg"},
        ],
    }
    assert metadata._best_cover(meta) == "https://covers.example/untagged.jpg"


def test_refresh_reports_what_it_knows_so_the_picker_can_seed_a_search(monkeypatch):
    """The picker was a dead end: five wrong suggestions and no way to look further.

    It now seeds its own search box from the work's title, author and identifiers, so
    those have to come back on both the ambiguous and the no-match answers.
    """
    work_id = _stub("Dead World", "Craig Alanson")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'asin','B0GZHFBB17','readarr',0,0.5)",
            (work_id,),
        )

    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.75, "candidates": [{"id": "or-a", "title": "Deadworld"}]})
    review = metadata.refresh_work(work_id)
    assert review["status"] == "review"
    assert review["title"] == "Dead World"
    assert review["authors"] == ["Craig Alanson"]
    assert {"namespace": "asin", "value": "B0GZHFBB17"} in review["identifiers"]

    monkeypatch.setattr(metadata, "resolve", lambda **kw: {"match": None, "confidence": 0.0, "candidates": []})
    unmatched = metadata.refresh_work(work_id)
    assert unmatched["status"] == "unmatched"
    assert unmatched["title"] == "Dead World"
    assert unmatched["authors"] == ["Craig Alanson"]
