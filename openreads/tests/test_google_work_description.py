"""Work-level descriptions from Google Books.

Open Library carries no description at all for whole authors - Craig Alanson's entire
catalogue comes back with zero-length descriptions - while Google Books has over a
thousand characters for the same titles. Google Books was already consulted for
*editions*, but the work's own description, which is what a client displays, came from
Open Library alone. So a book could show a correct cover and nothing else.
"""
import time

import pytest

import db
import resources


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "openreads.db"))
    db.init_db()
    yield


def _work(title="Dead World", author="Craig Alanson", description=None):
    work_id = db.get_or_create_work(f"OL{int(time.time()*1e6)%10**9}W", title)
    if description:
        db.update_work(work_id, description=description)
    author_id = db.get_or_create_author(f"OL{int(time.time()*1e6)%10**9}A", author)
    db.link_work_author(work_id, author_id)
    return work_id


def test_description_is_filled_from_google_when_openlibrary_has_none(monkeypatch):
    work_id = _work()
    monkeypatch.setattr(resources.gb, "find_by_title_author",
                        lambda t, a=None: {"title": t, "description": "A ship falls silent."})

    resources._enrich_work_with_google(db.work_by_id(work_id))

    assert db.work_by_id(work_id)["description"] == "A ship falls silent."


def test_an_openlibrary_description_is_never_overwritten(monkeypatch):
    work_id = _work(description="Open Library's own blurb")
    called = []
    monkeypatch.setattr(resources.gb, "find_by_title_author",
                        lambda t, a=None: called.append(t) or {"description": "Google's blurb"})

    resources._enrich_work_with_google(db.work_by_id(work_id))

    assert db.work_by_id(work_id)["description"] == "Open Library's own blurb"
    assert called == [], "Google Books must not even be asked when we already have one"


def test_google_is_asked_with_the_author(monkeypatch):
    work_id = _work("Dead World", "Craig Alanson")
    seen = {}
    monkeypatch.setattr(resources.gb, "find_by_title_author",
                        lambda t, a=None: seen.update(title=t, author=a) or None)

    resources._enrich_work_with_google(db.work_by_id(work_id))

    assert seen == {"title": "Dead World", "author": "Craig Alanson"}


def test_a_work_google_has_nothing_for_is_not_re_asked(monkeypatch):
    """Otherwise every search that returns it costs another 3-second provider call."""
    work_id = _work()
    calls = []
    monkeypatch.setattr(resources.gb, "find_by_title_author", lambda t, a=None: calls.append(t) or None)

    resources._enrich_work_with_google(db.work_by_id(work_id))
    resources._enrich_work_with_google(db.work_by_id(work_id))

    assert len(calls) == 1


def test_the_attempt_marker_does_not_disturb_the_openlibrary_refresh_clock(monkeypatch):
    """update_work() stamps fetched_at, which _stale() uses to decide whether to
    re-read Open Library. Noting an enrichment attempt through it would keep pushing
    that clock forward and stop the work ever being refreshed."""
    work_id = _work()
    db.update_work(work_id, title="Dead World")
    before = db.work_by_id(work_id)["fetched_at"]
    time.sleep(0.01)

    monkeypatch.setattr(resources.gb, "find_by_title_author", lambda t, a=None: None)
    resources._enrich_work_with_google(db.work_by_id(work_id))

    assert db.work_by_id(work_id)["fetched_at"] == before
    assert db.work_by_id(work_id)["description_enrich_attempted_at"] is not None


def test_enrichment_can_be_disabled_for_bulk_paths(monkeypatch):
    work_id = _work()
    calls = []
    monkeypatch.setattr(resources.gb, "find_by_title_author", lambda t, a=None: calls.append(t) or None)
    resources._enrich_work_with_google(db.work_by_id(work_id), enable=False)
    assert calls == []


def test_get_work_mirrors_an_enriched_description_into_the_canonical_record(monkeypatch, tmp_path):
    """ensure_work() can fill a description, but get_work() only mirrored *editions*.
    The enrichment landed in the legacy row and stopped there, so a client asking for
    the work still saw nothing."""
    import v2db
    import v2

    v2db.init_db()
    work_id = _work()
    author_id = db.get_or_create_author("OL99A", "Craig Alanson")
    db.link_work_author(work_id, author_id)
    canonical = v2db.sync_work(work_id)
    assert not (v2db.canonical_work(canonical).get("description") or "")

    monkeypatch.setattr(resources, "_stale", lambda *_a, **_k: False)
    monkeypatch.setattr(resources.gb, "find_by_title_author",
                        lambda t, a=None: {"description": "Kaz is not my real name."})
    monkeypatch.setattr(resources, "ensure_work_editions", lambda *a, **k: None)

    item = v2.get_work(canonical, None)

    assert item["description"] == "Kaz is not my real name."


def test_search_does_not_pay_for_description_enrichment(monkeypatch, tmp_path):
    """A search creates a work per upstream hit. A Google Books call each - up to
    three seconds - turned an uncached search into a ten-second wait. Descriptions are
    not shown in a result list; get_work() fetches one when it is actually displayed."""
    work_id = _work()
    calls = []
    monkeypatch.setattr(resources.gb, "find_by_title_author", lambda t, a=None: calls.append(t) or None)
    monkeypatch.setattr(resources, "_stale", lambda *_a, **_k: False)

    resources.ensure_work(work_id, enrich=False)
    assert calls == []

    resources.ensure_work(work_id, enrich=True)
    assert len(calls) == 1
