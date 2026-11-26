"""P2-01: series_assertions is a new evidence table, created empty, no behavior change.

series/series_memberships only ever hold the post-filter result of a series link;
there was previously nowhere to record the evidence (source, raw value, confidence,
status) behind that decision. This only adds the table and its migration - no
code writes to it yet.
"""
import contextlib
import dataclasses
import sqlite3

import pytest

from app import db


def _columns(table: str) -> dict:
    with db.session() as conn:
        return {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(table: str) -> set:
    with db.session() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def test_table_exists_after_fresh_init_db():
    cols = _columns("series_assertions")
    expected = {
        "id", "work_id", "series_id", "source", "raw_value", "normalized_name",
        "proposed_position", "evidence_type", "confidence", "status",
        "created_at", "updated_at",
    }
    assert expected <= set(cols)


def test_table_is_empty_after_fresh_migration():
    assert db.one("SELECT COUNT(*) n FROM series_assertions")["n"] == 0


def test_expected_indexes_present():
    idx = _indexes("series_assertions")
    assert "idx_series_assertions_work" in idx
    assert "idx_series_assertions_series_status" in idx


def test_repeated_init_db_is_a_no_op():
    """Calling init_db() again against an already-migrated DB must not error,
    must not duplicate schema_migrations rows, and must leave the table empty."""
    db.init_db()
    db.init_db()
    # The legacy baseline marker only; the F-04 framework records in migration_steps
    assert db.one("SELECT COUNT(*) n FROM schema_migrations")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM series_assertions")["n"] == 0


def test_migrating_a_db_that_predates_the_table_adds_it_without_error(tmp_path, monkeypatch):
    """Simulates a production DB created before P2-01: schema_migrations already
    at version 1, but no series_assertions table. init_db() must add it cleanly,
    with no rows populated (this task does not backfill)."""
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
        INSERT INTO schema_migrations(version, applied_at) VALUES (1, 0);
        CREATE TABLE works (id TEXT PRIMARY KEY, metadata_id TEXT, title TEXT NOT NULL, added_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE series (id TEXT PRIMARY KEY, metadata_id TEXT, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        """
    )
    conn.commit()
    conn.close()

    legacy_settings = dataclasses.replace(db.settings, db_path=legacy_path)
    monkeypatch.setattr(db, "settings", legacy_settings)
    db.init_db()

    with contextlib.closing(db.connect(legacy_path)) as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "series_assertions" in tables
        assert c.execute("SELECT COUNT(*) n FROM series_assertions").fetchone()["n"] == 0


def test_insert_requires_work_id_and_raw_value():
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO series_assertions(id,work_id,raw_value,confidence,status,created_at,updated_at)"
                " VALUES (?,NULL,?,0,'rejected',?,?)",
                (db.uid(), "Some Series #1", db.now(), db.now()),
            )


def test_status_check_constraint_rejects_unknown_values():
    t = db.now()
    work_id = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,added_at,updated_at) VALUES (?,?,?,?)",
            (work_id, "Test Work", t, t),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO series_assertions(id,work_id,raw_value,confidence,status,created_at,updated_at)"
                " VALUES (?,?,?,0,'not_a_real_status',?,?)",
                (db.uid(), work_id, "Some Series #1", t, t),
            )


def test_series_id_is_nullable_and_cascades_on_series_delete():
    t = db.now()
    work_id = db.uid()
    series_id = db.uid()
    assertion_id = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,added_at,updated_at) VALUES (?,?,?,?)", (work_id, "Test Work", t, t))
        conn.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (series_id, "Test Series", t, t))
        # An assertion may exist with no series chosen yet.
        conn.execute(
            "INSERT INTO series_assertions(id,work_id,series_id,raw_value,confidence,status,created_at,updated_at)"
            " VALUES (?,?,NULL,?,0.2,'uncertain',?,?)",
            (db.uid(), work_id, "Some Series #1", t, t),
        )
        conn.execute(
            "INSERT INTO series_assertions(id,work_id,series_id,raw_value,confidence,status,created_at,updated_at)"
            " VALUES (?,?,?,?,0.9,'auto_accepted',?,?)",
            (assertion_id, work_id, series_id, "Test Series #1", t, t),
        )
    assert db.one("SELECT COUNT(*) n FROM series_assertions WHERE series_id IS NULL")["n"] == 1

    with db.tx() as conn:
        conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    remaining = db.one("SELECT series_id FROM series_assertions WHERE id=?", (assertion_id,))
    assert remaining is None  # cascaded, not orphaned


def test_work_delete_cascades_to_assertions():
    t = db.now()
    work_id = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,added_at,updated_at) VALUES (?,?,?,?)", (work_id, "Test Work", t, t))
        conn.execute(
            "INSERT INTO series_assertions(id,work_id,raw_value,confidence,status,created_at,updated_at)"
            " VALUES (?,?,?,0,'rejected',?,?)",
            (db.uid(), work_id, "junk", t, t),
        )
    assert db.one("SELECT COUNT(*) n FROM series_assertions WHERE work_id=?", (work_id,))["n"] == 1
    with db.tx() as conn:
        conn.execute("DELETE FROM works WHERE id=?", (work_id,))
    assert db.one("SELECT COUNT(*) n FROM series_assertions WHERE work_id=?", (work_id,))["n"] == 0
