"""P4-03: generic metadata observations + field locks (Part XXI foundation, schema only)."""
import sqlite3

import pytest

from app import db


def _obs(conn, entity_id="w1", field="title", current=0, entity_type="work", source="manual"):
    conn.execute(
        "INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,source,is_current,observed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (db.uid(), entity_type, entity_id, field, "v", source, current, db.now()),
    )


def _ddl() -> dict:
    with db.session() as conn:
        return {r["name"]: r["sql"] for r in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE '%metadata_%' AND sql IS NOT NULL")}


def test_init_creates_empty_tables_and_second_run_is_noop():
    first = _ddl()
    assert {"metadata_observations", "metadata_field_locks"} <= first.keys()
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM metadata_field_locks").fetchone()[0] == 0
        _obs(conn)
        conn.commit()
    db.init_db()
    assert _ddl() == first
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_observations").fetchone()[0] == 1


def test_both_tables_are_indexed_by_entity_and_field():
    with db.session() as conn:
        for table in ("metadata_observations", "metadata_field_locks"):
            cols = [[c["name"] for c in conn.execute(f"PRAGMA index_info({i['name']})")]
                    for i in conn.execute(f"PRAGMA index_list({table})")]
            assert ["entity_type", "entity_id", "field_name"] in cols, table


def test_second_current_observation_for_same_field_is_rejected():
    with db.session() as conn:
        _obs(conn, current=1)
        with pytest.raises(sqlite3.IntegrityError):
            _obs(conn, current=1, source="openlibrary")


def test_competing_and_other_field_observations_are_allowed():
    with db.session() as conn:
        _obs(conn, current=1)
        _obs(conn, current=0, source="a")
        _obs(conn, current=0, source="b")  # any number of non-current competitors
        _obs(conn, field="subtitle", current=1)  # different field
        _obs(conn, entity_id="w2", current=1)  # different entity
        _obs(conn, entity_type="edition", current=1)  # same id, different entity type
        _obs(conn, field="brand_new_field_name", current=1)  # field_name is free text


def test_entity_type_is_constrained():
    with db.session() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _obs(conn, entity_type="author")


def test_field_lock_is_unique_per_entity_field():
    with db.session() as conn:
        conn.execute("INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at) VALUES ('work','w1','title',?)", (db.now(),))
        conn.execute("INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at,locked_by) VALUES ('work','w1','subtitle',?,'u1')", (db.now(),))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at) VALUES ('work','w1','title',?)", (db.now(),))
