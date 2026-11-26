"""P5-07: Series assertion write paths and confirm/reject/suppress actions.

Tests for:
- File-tag series creating assertions with source 'file-tags' (AC1)
- Manual edit creating manually_confirmed assertions (AC2)
- Suppress preventing re-creation by _link_series (AC3)
- Confirm of uncertain single-book series makes it visible (AC4)
- Reject removes membership only when no other support (AC5)
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import metadata, series as series_service, migration, localmeta

client = TestClient(app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _stub_work(title="Test Work", author="Test Author"):
    """Create a minimal work via the migration helper."""
    return migration._find_or_create_stub({
        "title": title, "author": author, "identifiers": {}
    })


def _assertion_rows(work_id):
    """Return all series_assertions rows for a work."""
    return db.all("SELECT * FROM series_assertions WHERE work_id=?", (work_id,))


def _series_names(work_id):
    """Return series names for a work."""
    rows = db.all(
        "SELECT s.name FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?",
        (work_id,),
    )
    return [r["name"] for r in rows]


# ── AC1: Harvested file-tag series creates assertion with source 'file-tags' ─

def test_file_tag_series_creates_assertion_with_file_tags_source():
    """AC1: A harvested file-tag series now creates an assertion row with source 'file-tags'."""
    work_id = _stub_work("File Tag Book")
    localmeta._attach_series(work_id, "The Test Series", "1")

    rows = _assertion_rows(work_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "file-tags"
    assert row["evidence_type"] == "embedded_tag"
    assert row["status"] == "auto_accepted"
    assert row["confidence"] == 0.8  # scored by metadata._score_series_link
    assert row["raw_value"] == "The Test Series"
    assert row["normalized_name"] == "The Test Series"
    assert row["proposed_position"] == "1"


def test_file_tag_series_without_position_is_uncertain():
    """File-tag series without a position should be uncertain, not auto_accepted."""
    work_id = _stub_work("File Tag No Pos")
    localmeta._attach_series(work_id, "Bare Series", None)

    rows = _assertion_rows(work_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "file-tags"
    assert rows[0]["status"] == "uncertain"
    assert rows[0]["evidence_type"] == "embedded_tag"


# ── AC2: Manual edit creates manually_confirmed assertions ─

def test_manual_edit_creates_manually_confirmed_assertions():
    """AC2: Editing a work's series manually creates manually_confirmed assertions."""
    work_id = metadata.upsert_work({
        "id": "or-manual-1",
        "title": "Manual Edit Book",
        "contributors": [{"id": "or-a", "name": "Author A", "role": "author"}],
        "series": [{"id": "or-s1", "name": "OpenReads Series", "position": "1"}],
        "identifiers": [], "editions": [],
    })
    # Now edit manually to replace the series.
    r = client.patch(f"/api/v1/library/{work_id}", json={
        "series": [{"name": "Manual Series", "position": "2"}]
    })
    assert r.status_code == 200

    rows = _assertion_rows(work_id)
    # Should have: 1 openreads assertion + 1 manually_confirmed assertion
    assert len(rows) >= 2
    statuses = {row["status"] for row in rows}
    assert "manually_confirmed" in statuses
    # The manually_confirmed assertion should have source 'manual' and status 'manually_confirmed'.
    manual_rows = [r for r in rows if r["source"] == "manual" and r["status"] == "manually_confirmed"]
    assert len(manual_rows) == 1
    assert manual_rows[0]["confidence"] == 1.0


def test_manual_edit_leaves_earlier_assertion_rows_intact():
    """AC2: Manual edit leaves earlier assertion rows intact (no deletion)."""
    work_id = metadata.upsert_work({
        "id": "or-pres-1",
        "title": "Preserve History",
        "contributors": [{"id": "or-a", "name": "Author A", "role": "author"}],
        "series": [
            {"id": "or-s1", "name": "Old Series", "position": "1"},
            {"id": "or-s2", "name": "Also Old", "position": "2"},
        ],
        "identifiers": [], "editions": [],
    })
    old_count = len(_assertion_rows(work_id))
    # Edit to a single new series.
    client.patch(f"/api/v1/library/{work_id}", json={
        "series": [{"name": "New Series", "position": "1"}]
    })
    new_count = len(_assertion_rows(work_id))
    # Should have more rows now (new manually_confirmed + manually_suppressed for removed ones).
    assert new_count >= old_count


# ── AC3: Suppress prevents re-creation by _link_series ─

def test_suppress_prevents_relink_series_recreation():
    """AC3: After suppress, re-running _link_series does not recreate membership."""
    work_id = metadata.upsert_work({
        "id": "or-sup-1",
        "title": "Suppress Book",
        "contributors": [{"id": "or-a", "name": "Author A", "role": "author"}],
        "series": [{"id": "or-s-sup", "name": "Suppress Me", "position": "1"}],
        "identifiers": [], "editions": [],
    })
    assert _series_names(work_id) == ["Suppress Me"]

    # Get the series_id.
    sid = db.one("SELECT id FROM series WHERE name='Suppress Me'")["id"]

    # Suppress it.
    result = series_service.suppress_series_for_work(work_id, sid)
    assert result["status"] == "manually_suppressed"
    assert result["changed"] is True
    assert _series_names(work_id) == []

    # Now re-run _link_series for the same series.
    conn = db.connect()
    metadata._link_series(
        conn, work_id,
        {"id": "or-s-sup", "name": "Suppress Me", "position": "1"},
        db.now(),
    )
    conn.commit()
    conn.close()

    # Membership should NOT be recreated.
    assert _series_names(work_id) == []
    # But a rejected assertion should exist.
    rows = _assertion_rows(work_id)
    rejected = [r for r in rows if r["status"] == "rejected"]
    assert len(rejected) >= 1


def test_suppress_is_idempotent():
    """Suppressing twice is a no-op."""
    work_id = metadata.upsert_work({
        "id": "or-idem-1",
        "title": "Idempotent Suppress",
        "contributors": [{"id": "or-a", "name": "Author A", "role": "author"}],
        "series": [{"id": "or-s-idem", "name": "Idempotent Series", "position": "1"}],
        "identifiers": [], "editions": [],
    })
    sid = db.one("SELECT id FROM series WHERE name='Idempotent Series'")["id"]

    r1 = series_service.suppress_series_for_work(work_id, sid)
    r2 = series_service.suppress_series_for_work(work_id, sid)
    assert r1["changed"] is True
    assert r2["changed"] is False


# ── AC4: Confirm of uncertain single-book series makes it visible ─

def test_confirm_uncertain_single_book_makes_it_visible():
    """AC4: Confirm of an 'uncertain' single-book series makes list_series return it."""
    work_id = _stub_work("Confirm Book")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "Uncertain Series", "uncertain series", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "Uncertain Series", "Uncertain Series",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    # Before confirm, single-book uncertain series is hidden.
    names = {s["name"] for s in series_service.list_series()}
    assert "Uncertain Series" not in names

    # Confirm it.
    result = series_service.confirm_assertion(aid)
    assert result["status"] == "manually_confirmed"
    assert result["changed"] is True

    # Now it should be visible.
    names = {s["name"] for s in series_service.list_series()}
    assert "Uncertain Series" in names


def test_confirm_is_idempotent():
    """Confirming twice is a no-op."""
    work_id = _stub_work("Idempotent Confirm")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "Confirm Idem", "confirm idem", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "Confirm Idem", "Confirm Idem",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    r1 = series_service.confirm_assertion(aid)
    r2 = series_service.confirm_assertion(aid)
    assert r1["changed"] is True
    assert r2["changed"] is False


# ── AC5: Reject removes membership only when no other support ─

def test_reject_removes_membership_when_only_support():
    """AC5: Reject on the only support removes the membership."""
    work_id = _stub_work("Reject Only")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "Reject Me", "reject me", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "Reject Me", "Reject Me",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    assert _series_names(work_id) == ["Reject Me"]
    result = series_service.reject_assertion(aid)
    assert result["status"] == "rejected"
    assert result["changed"] is True
    assert _series_names(work_id) == []


def test_reject_keeps_membership_with_other_support():
    """AC5: With other accepted/confirmed support, reject keeps the membership."""
    work_id = _stub_work("Reject With Support")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "Keep Me", "keep me", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        # Two assertions: one uncertain, one auto_accepted.
        aid1 = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid1, work_id, sid, "openreads", "Keep Me", "Keep Me",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )
        aid2 = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid2, work_id, sid, "openreads", "Keep Me", "Keep Me",
             "1", "explicit_position", 1.0, "auto_accepted", db.now(), db.now()),
        )

    assert _series_names(work_id) == ["Keep Me"]
    result = series_service.reject_assertion(aid1)
    assert result["status"] == "rejected"
    assert result["changed"] is True
    # Membership should still exist because aid2 is auto_accepted.
    assert _series_names(work_id) == ["Keep Me"]


def test_reject_is_idempotent():
    """Rejecting twice is a no-op."""
    work_id = _stub_work("Idempotent Reject")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "Reject Idem", "reject idem", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "Reject Idem", "Reject Idem",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    r1 = series_service.reject_assertion(aid)
    r2 = series_service.reject_assertion(aid)
    assert r1["changed"] is True
    assert r2["changed"] is False


# ── API route tests ──────────────────────────────────────────────────────────

def test_api_confirm_assertion():
    """POST /series-assertions/{id}/confirm returns 200."""
    work_id = _stub_work("API Confirm")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "API Confirm Series", "api confirm series", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "API Confirm Series", "API Confirm Series",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    r = client.post(f"/api/v1/series-assertions/{aid}/confirm")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "manually_confirmed"
    assert body["changed"] is True


def test_api_reject_assertion():
    """POST /series-assertions/{id}/reject returns 200."""
    work_id = _stub_work("API Reject")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "API Reject Series", "api reject series", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )
        aid = db.uid()
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, work_id, sid, "openreads", "API Reject Series", "API Reject Series",
             "1", "free_text", 0.5, "uncertain", db.now(), db.now()),
        )

    r = client.post(f"/api/v1/series-assertions/{aid}/reject")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["changed"] is True


def test_api_suppress_series():
    """POST /library/{work_id}/series/{series_id}/suppress returns 200."""
    work_id = _stub_work("API Suppress")
    sid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (sid, None, "API Suppress Series", "api suppress series", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, "1"),
        )

    r = client.post(f"/api/v1/library/{work_id}/series/{sid}/suppress")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "manually_suppressed"
    assert body["changed"] is True
    assert _series_names("API Suppress") == []


def test_api_list_assertions():
    """GET /library/{work_id}/series-assertions returns assertions."""
    work_id = metadata.upsert_work({
        "id": "or-list-1",
        "title": "API List Assertions",
        "contributors": [{"id": "or-a", "name": "Author A", "role": "author"}],
        "series": [{"id": "or-s-list", "name": "List Series", "position": "1"}],
        "identifiers": [], "editions": [],
    })

    r = client.get(f"/api/v1/library/{work_id}/series-assertions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["assertions"]) >= 1
    assert body["assertions"][0]["source"] == "openreads"
    assert body["assertions"][0]["status"] in ("auto_accepted", "uncertain")


def test_api_confirm_nonexistent_assertion():
    """Confirming a nonexistent assertion returns 404."""
    r = client.post("/api/v1/series-assertions/nonexistent/confirm")
    assert r.status_code == 404


def test_api_reject_nonexistent_assertion():
    """Rejecting a nonexistent assertion returns 404."""
    r = client.post("/api/v1/series-assertions/nonexistent/reject")
    assert r.status_code == 404
