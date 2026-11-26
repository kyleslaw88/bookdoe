"""P4-04: ingest records per-field observations and respects field locks (Part XXI)."""
from __future__ import annotations

from app import db
from app.services import localmeta, metadata
from tests.test_localmeta import _attach, _epub, _work


def _meta(**over):
    m = {"id": "or-obs", "title": "Obs Book", "contributors": [], "series": [], "identifiers": [], "editions": []}
    m.update(over)
    return m


def _obs(entity_type, entity_id, field):
    return db.all(
        "SELECT value,source,is_current FROM metadata_observations WHERE entity_type=? AND entity_id=? AND field_name=? ORDER BY observed_at,rowid",
        (entity_type, entity_id, field),
    )


def _lock(entity_type, entity_id, field):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at,locked_by) VALUES (?,?,?,?,?)",
            (entity_type, entity_id, field, db.now(), "manual"),
        )


def test_two_provider_values_give_two_observations_one_current():
    wid = metadata.upsert_work(_meta(description="first"))
    metadata.upsert_work(_meta(description="second"))
    rows = _obs("work", wid, "description")
    assert [(r["value"], r["source"], r["is_current"]) for r in rows] == [("first", "provider", 0), ("second", "provider", 1)]
    assert db.one("SELECT description FROM works WHERE id=?", (wid,))["description"] == "second"


def test_unchanged_value_is_not_re_recorded():
    wid = metadata.upsert_work(_meta(description="same"))
    metadata.upsert_work(_meta(description="same"))
    assert len(_obs("work", wid, "description")) == 1


def test_all_work_fields_are_observed():
    wid = metadata.upsert_work(_meta(
        subtitle="Sub", description="D", first_publish_date="2001", primary_language="en", cover_url="http://x/c.jpg"))
    fields = {r["field_name"] for r in db.all("SELECT field_name FROM metadata_observations WHERE entity_id=?", (wid,))}
    assert fields == {"title", "subtitle", "description", "first_publish_date", "primary_language", "cover_url"}


def test_locked_work_field_records_but_does_not_overwrite():
    wid = metadata.upsert_work(_meta(title="Original", description="orig"))
    _lock("work", wid, "title")
    _lock("work", wid, "description")
    metadata.upsert_work(_meta(title="Provider Changed", description="new"))
    work = db.one("SELECT title,sort_title,description FROM works WHERE id=?", (wid,))
    assert (work["title"], work["sort_title"], work["description"]) == ("Original", "Original", "orig")
    rows = _obs("work", wid, "title")
    assert [(r["value"], r["is_current"]) for r in rows] == [("Original", 1), ("Provider Changed", 0)]
    assert [r["value"] for r in _obs("work", wid, "description")] == ["orig", "new"]
    # an unlocked field on the same work still updates
    metadata.upsert_work(_meta(title="Provider Changed", description="new", subtitle="S"))
    assert db.one("SELECT subtitle FROM works WHERE id=?", (wid,))["subtitle"] == "S"


def test_unlocked_behaviour_is_unchanged_for_empty_values():
    wid = metadata.upsert_work(_meta(subtitle="Sub", description="keep"))
    metadata.upsert_work(_meta())
    work = db.one("SELECT subtitle,description FROM works WHERE id=?", (wid,))
    assert work["subtitle"] is None  # subtitle is overwritten unconditionally today
    assert work["description"] == "keep"  # COALESCE keeps the old value
    assert [r["value"] for r in _obs("work", wid, "description") if r["is_current"]] == ["keep"]
    assert not [r for r in _obs("work", wid, "subtitle") if r["is_current"]]


def test_edition_fields_observed_and_locked():
    ed = {"id": "or-ed", "publisher": "Pub A", "release_date": "2020", "language": "en", "num_pages": 100,
          "duration_seconds": 3600, "abridged": 0}
    wid = metadata.upsert_work(_meta(editions=[ed]))
    eid = db.one("SELECT id FROM editions WHERE work_id=?", (wid,))["id"]
    fields = {r["field_name"] for r in db.all("SELECT field_name FROM metadata_observations WHERE entity_type='edition'")}
    assert fields == {"publisher", "release_date", "language", "pages", "duration", "abridged"}

    _lock("edition", eid, "duration")
    _lock("edition", eid, "publisher")
    metadata.upsert_work(_meta(editions=[{**ed, "publisher": "Pub B", "duration_seconds": 7200, "num_pages": 200}]))
    row = db.one("SELECT publisher,duration_seconds,pages FROM editions WHERE id=?", (eid,))
    assert (row["publisher"], row["duration_seconds"], row["pages"]) == ("Pub A", 3600, 200)
    assert [(r["value"], r["is_current"]) for r in _obs("edition", eid, "duration")] == [("3600", 1), ("7200", 0)]
    assert [(r["value"], r["is_current"]) for r in _obs("edition", eid, "pages")] == [("100", 0), ("200", 1)]


def test_harvest_records_local_observations_and_fills_blanks(tmp_path):
    wid = _work()
    _attach(wid, _epub(tmp_path / "b.epub", description="File blurb"), "ebook")
    localmeta.harvest(wid)
    assert [(r["value"], r["source"], r["is_current"]) for r in _obs("work", wid, "description")] == [("File blurb", "local", 1)]
    assert _obs("work", wid, "first_publish_date")[0]["value"] == "2026"
    localmeta.harvest(wid)
    assert len(_obs("work", wid, "description")) == 1


def test_harvest_does_not_apply_over_existing_value_but_keeps_evidence(tmp_path):
    wid = _work()
    with db.tx() as conn:
        conn.execute("UPDATE works SET description='Publisher blurb' WHERE id=?", (wid,))
    _attach(wid, _epub(tmp_path / "b.epub", description="File blurb"), "ebook")
    localmeta.harvest(wid)
    assert db.one("SELECT description FROM works WHERE id=?", (wid,))["description"] == "Publisher blurb"
    assert [(r["value"], r["is_current"]) for r in _obs("work", wid, "description")] == [("File blurb", 0)]


def test_harvest_respects_lock_on_blank_field(tmp_path):
    wid = _work()
    _lock("work", wid, "description")
    _attach(wid, _epub(tmp_path / "b.epub", description="File blurb"), "ebook")
    result = localmeta.harvest(wid)
    assert db.one("SELECT description FROM works WHERE id=?", (wid,))["description"] is None
    assert "description" not in result["applied"]
    assert [(r["value"], r["is_current"]) for r in _obs("work", wid, "description")] == [("File blurb", 0)]
    assert db.one("SELECT first_publish_date FROM works WHERE id=?", (wid,))["first_publish_date"] == "2026"
