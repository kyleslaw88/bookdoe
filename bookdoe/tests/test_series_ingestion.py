"""Series linking during metadata ingestion (app.services.metadata.upsert_work).

Covers the production bug where OpenReads' own series normalizer had not (yet)
converged two spellings of one series into a single id, or handed back a raw
catalog string that was not a series at all - see app/services/seriesnorm.py
and metadata._link_series. bookdoe must not blindly trust OpenReads' id.
"""
from __future__ import annotations

from app import db
from app.services import metadata, series as series_service


def _meta(work_mid, title, series):
    return {
        "id": work_mid,
        "title": title,
        "contributors": [{"id": "or-author", "name": "Author A", "role": "author"}],
        "series": series,
        "identifiers": [],
        "editions": [],
    }


def _series_names_for(title):
    w = db.one("SELECT id FROM works WHERE title=?", (title,))
    rows = db.all(
        "SELECT s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?",
        (w["id"],),
    )
    return [r["name"] for r in rows]


def test_two_different_openreads_series_ids_with_the_same_name_merge():
    metadata.upsert_work(_meta("or-w1", "Book One", [{"id": "or-series-a", "name": "The 39 Clues", "position": "1"}]))
    metadata.upsert_work(_meta("or-w2", "Book Two", [{"id": "or-series-b", "name": "39 Clues", "position": "2"}]))

    rows = db.all("SELECT id FROM series WHERE norm_key=?", ("39 clues",))
    assert len(rows) == 1, "same series under two OpenReads ids should collapse to one row"
    assert db.one("SELECT COUNT(*) n FROM series_memberships WHERE series_id=?", (rows[0]["id"],))["n"] == 2


def test_spelled_out_position_word_still_merges_with_the_digit_form():
    metadata.upsert_work(_meta("or-w3", "Mission Titanic", [{"id": "or-dc-1", "name": "39 Clues: Doublecross -- book one", "position": None}]))
    metadata.upsert_work(_meta("or-w4", "Mission Hindenburg", [{"id": "or-dc-2", "name": "39 Clues: Doublecross", "position": "2"}]))

    from app.services import seriesnorm
    rows = db.all("SELECT id, name FROM series WHERE norm_key=?", (seriesnorm.norm_key("39 Clues: Doublecross"),))
    assert len(rows) == 1
    # the second call's metadata_id ('or-dc-2') should never surface a new row -
    # it must have matched the first by name and been absorbed into it.
    assert db.one("SELECT COUNT(*) n FROM series WHERE metadata_id='or-dc-2'")["n"] == 0
    positions = {r["position"] for r in db.all(
        "SELECT position FROM series_memberships WHERE series_id=?", (rows[0]["id"],)
    )}
    assert positions == {"1", "2"}


def test_library_classification_junk_is_never_linked():
    metadata.upsert_work(_meta("or-w5", "Some German Paperback", [
        {"id": "or-junk", "name": "Heyne-Bucher -- 01. -- Heyne allgemeine Reihe -- Nr. 10624", "position": None}
    ]))
    assert _series_names_for("Some German Paperback") == []
    assert db.one("SELECT COUNT(*) n FROM series WHERE metadata_id='or-junk'")["n"] == 0


def test_dedupe_series_merges_pre_existing_duplicates_and_drops_junk():
    metadata.upsert_work(_meta("or-w6", "Book Six", [{"id": "or-series-c", "name": "A Molly Murphy mystery", "position": "1"}]))
    metadata.upsert_work(_meta("or-w7", "Book Seven", [{"id": "or-series-d", "name": "The Molly Murphy mysteries", "position": "2"}]))
    # Simulate a row imported before bookdoe's normalizer existed: no norm_key,
    # and an unmistakable classification-string name.
    junk_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (junk_id, None, "Reclam Universal-Bibliothek -- Nr. 1234 -- Sonderreihe", "junk", t, t),
        )

    result = series_service.dedupe_series()
    assert result["dropped"] >= 1
    assert db.one("SELECT COUNT(*) n FROM series WHERE id=?", (junk_id,))["n"] == 0

    rows = db.all("SELECT id FROM series WHERE norm_key=?", ("molly murphy",))
    assert len(rows) == 1, "pre-existing duplicate spellings should merge on cleanup"
    assert db.one("SELECT COUNT(*) n FROM series_memberships WHERE series_id=?", (rows[0]["id"],))["n"] == 2


def test_dedupe_series_is_idempotent():
    metadata.upsert_work(_meta("or-w8", "Book Eight", [{"id": "or-series-e", "name": "Standalone Series", "position": "1"}]))
    first = series_service.dedupe_series()
    second = series_service.dedupe_series()
    assert second == {"dropped": 0, "merged": 0}
    assert first  # sanity: it ran without error the first time too


def _assertions_for(title):
    w = db.one("SELECT id FROM works WHERE title=?", (title,))
    return db.all("SELECT * FROM series_assertions WHERE work_id=?", (w["id"],))


def test_structured_well_evidenced_link_is_auto_accepted():
    """P2-02 AC: explicit position + repeat membership in the same series ->
    auto_accepted, with confidence clamped at 1.0."""
    metadata.upsert_work(_meta("or-w9", "Nine One", [{"id": "or-riftwar-1", "name": "The Riftwar Saga", "position": "1"}]))
    metadata.upsert_work(_meta("or-w10", "Nine Two", [{"id": "or-riftwar-2", "name": "The Riftwar Saga", "position": "2"}]))

    rows = _assertions_for("Nine Two")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "auto_accepted"
    assert row["confidence"] == 1.0
    assert row["evidence_type"] == "explicit_position"
    assert row["source"] == "openreads"
    assert row["series_id"] is not None
    assert row["raw_value"] == "The Riftwar Saga"
    assert row["normalized_name"] == "The Riftwar Saga"
    assert row["proposed_position"] == "2"


def test_first_time_link_with_explicit_position_and_no_repeat_is_still_auto_accepted():
    """P2-02 AC / regression guard for the round() fix: explicit ordinal
    position on a first-time (non-repeat, non-bare) series link sums to
    0.6 + 0.25 - 0.05 == 0.7999999999999999 in raw binary float - just
    under the 0.8 auto_accepted threshold. Without _score_series_link's
    round(), this would incorrectly land as uncertain instead of
    auto_accepted. This isolates that exact signal combination: ordinal
    position present, no repeat series membership, name not bare."""
    metadata.upsert_work(_meta("or-w14", "Fourteen", [{"id": "or-expanse-1", "name": "The Solitary Expanse", "position": "1"}]))

    rows = _assertions_for("Fourteen")
    assert len(rows) == 1
    row = rows[0]
    assert row["confidence"] == 0.8
    assert row["status"] == "auto_accepted"
    assert row["evidence_type"] == "explicit_position"


def test_single_free_text_mention_with_no_position_is_uncertain():
    """P2-02 AC: a lone free-text series mention with no position is
    uncertain, not auto-accepted and not auto-rejected."""
    metadata.upsert_work(_meta("or-w11", "Eleven", [{"id": "or-wander-1", "name": "The Wandering Chronicles", "position": None}]))

    rows = _assertions_for("Eleven")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "uncertain"
    assert 0.4 <= row["confidence"] < 0.8
    assert row["evidence_type"] == "free_text"
    assert row["proposed_position"] is None


def test_junk_value_still_produces_a_rejected_assertion_row():
    """P2-02 AC: is_junk's decision is now recorded, not just silently applied -
    the junk filter's early return must still leave a rejected evidence row."""
    metadata.upsert_work(_meta("or-w12", "Twelve", [
        {"id": "or-junk-2", "name": "Heyne-Bucher -- 01. -- Heyne allgemeine Reihe -- Nr. 10624", "position": None}
    ]))

    rows = _assertions_for("Twelve")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "rejected"
    assert row["series_id"] is None
    assert row["raw_value"] == "Heyne-Bucher -- 01. -- Heyne allgemeine Reihe -- Nr. 10624"
    # confirm the junk filter itself still behaves exactly as before: no series row,
    # no membership - this is additive instrumentation, not a behavior change.
    assert _series_names_for("Twelve") == []
    assert db.one("SELECT COUNT(*) n FROM series WHERE metadata_id='or-junk-2'")["n"] == 0


def test_existing_series_and_membership_rows_unchanged_by_assertion_recording():
    """P2-02 AC: series/series_memberships stay byte-for-byte what they were
    before this task - series_assertions is purely additive instrumentation."""
    metadata.upsert_work(_meta("or-w13", "Thirteen", [{"id": "or-series-f", "name": "Some Standalone Saga", "position": "1"}]))

    w = db.one("SELECT id FROM works WHERE title=?", ("Thirteen",))
    series_row = db.one(
        "SELECT s.* FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?", (w["id"],)
    )
    membership_row = db.one("SELECT * FROM series_memberships WHERE work_id=?", (w["id"],))

    assert set(series_row.keys()) == {"id", "metadata_id", "name", "sort_name", "norm_key", "created_at", "updated_at"}
    assert set(membership_row.keys()) == {"work_id", "series_id", "position"}
    assert series_row["name"] == "Some Standalone Saga"
    assert membership_row["position"] == "1"

    # And exactly one evidence row exists alongside it, without altering the above.
    assert len(_assertions_for("Thirteen")) == 1
