"""Upgrade path for P4-01's search_candidates CHECK widening (overseer review, 2026-10-03).

A database created before 'dismissed' existed must keep its rows, accept the new
status, and keep both indexes after init_db() rebuilds the table.
"""
from app import db



def test_old_search_candidates_table_is_upgraded_in_place():
    db.init_db()
    with db.session() as conn:
        new_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search_candidates'").fetchone()["sql"]
        old_sql = new_sql.replace("'pending','promoted','discarded','dismissed'", "'pending','promoted','discarded'")
        assert old_sql != new_sql
        conn.execute("DROP TABLE search_candidates")
        conn.execute(old_sql)
        conn.execute("CREATE INDEX idx_search_candidates_query ON search_candidates(query)")
        conn.execute("CREATE INDEX idx_search_candidates_status ON search_candidates(status)")
        # Simulate a database from before the step existed (F-04 records each step once).
        conn.execute("DELETE FROM migration_steps WHERE version=6")
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(search_candidates)").fetchall()}
        required = [n for n, r in cols.items() if r["notnull"] and r["dflt_value"] is None and not r["pk"]]
        values = {n: ("x" if n not in ("created_at", "updated_at") else 1.0) for n in required}
        values.update({"id": "c1", "status": "pending"})
        if "title" in cols:
            values["title"] = "Leviathan Wakes"
        names = ",".join(values)
        conn.execute(f"INSERT INTO search_candidates({names}) VALUES ({','.join('?' * len(values))})", tuple(values.values()))  # nosec B608
        conn.commit()

    db.init_db()  # performs the rename / recreate / copy

    with db.session() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search_candidates'").fetchone()["sql"]
        assert "'dismissed'" in sql
        assert conn.execute("SELECT id FROM search_candidates WHERE id='c1'").fetchone() is not None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_candidates_pre_dismissed'").fetchone() is None
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(search_candidates)").fetchall()}
        assert {"idx_search_candidates_query", "idx_search_candidates_status"} <= idx
        conn.execute("UPDATE search_candidates SET status='dismissed' WHERE id='c1'")
        conn.commit()

    db.init_db()  # second start is a no-op
    with db.session() as conn:
        assert conn.execute("SELECT status FROM search_candidates WHERE id='c1'").fetchone()["status"] == "dismissed"
