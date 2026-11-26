"""F-04 versioned migration framework (spec Part XL).

Three starting points must converge on the same schema and keep their data:
  fresh       - empty file
  pre-d3d036d - old search_candidates CHECK, no preferred-edition columns,
                narrator_json without narrator contributor rows
  production  - exactly what the pre-framework init_db left at d3d036d: all three
                changes applied, schema_migrations = {1}, no migration_steps table
"""
from __future__ import annotations

import inspect
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app import db, migrations
from app.config import settings
from app.migrations import MigrationError


def _wipe() -> None:
    p = Path(settings.db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)
    for f in Path(settings.backup_dir).glob("*"):
        f.unlink()


def _premigrate_backups() -> list[Path]:
    return sorted(Path(settings.backup_dir).glob("premigrate-*.db"))


def _baseline_schema(conn) -> None:
    """What init_db() created before F-04's steps 4-6 existed (older columns only)."""
    # Fresh installs get the preferred-edition columns from CREATE TABLE; databases
    # created before P4-09 did not have them, so strip them from the DDL here.
    schema = db.SCHEMA.replace(
        "  updated_at REAL NOT NULL,\n  preferred_ebook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL,\n"
        "  preferred_audiobook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL\n);",
        "  updated_at REAL NOT NULL\n);",
    )
    assert schema != db.SCHEMA
    conn.executescript(schema + db.EXTRA_SCHEMA)
    for table, col, ddl in [
        ("users", "access_scope", "access_scope TEXT NOT NULL DEFAULT 'full'"),
        ("users", "kindle_email", "kindle_email TEXT"),
        ("api_keys", "key_prefix", "key_prefix TEXT"),
        ("api_keys", "expires_at", "expires_at REAL"),
        ("works", "metadata_source", "metadata_source TEXT"),
        ("series", "norm_key", "norm_key TEXT"),
        ("media_assets", "has_acquisition_intent", "has_acquisition_intent INTEGER NOT NULL DEFAULT 0"),
    ]:
        db._add_column(conn, table, col, ddl)
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (1,?)", (db.now(),))


def _old_search_candidates(conn) -> None:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search_candidates'").fetchone()["sql"]
    old = sql.replace("'pending','promoted','discarded','dismissed'", "'pending','promoted','discarded'")
    assert old != sql
    conn.execute("DROP TABLE search_candidates")
    conn.execute(old)
    conn.execute("CREATE INDEX idx_search_candidates_query ON search_candidates(query)")
    conn.execute("CREATE INDEX idx_search_candidates_status ON search_candidates(status)")


def _seed(conn) -> None:
    t = db.now()
    for wid, mid in (("w-provider", "ol:1"), ("w-plain", None), ("w-aud", None)):
        conn.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at,metadata_id) VALUES (?,?,?,?,?,?,?)",
            (wid, wid, wid, "resolved", t, t, mid),
        )
    conn.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES ('s1','The Expanse',?,?)", (t, t))
    conn.execute(
        "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,narrator_json,added_at,updated_at) "
        "VALUES ('a-aud','w-aud','audiobook','owned',0,0,?,?,?)",
        (json.dumps(["Wil Wheaton", "Jane Doe"]), t, t),
    )
    conn.execute(
        "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at) "
        "VALUES ('a-miss','w-plain','ebook','missing',1,0,?,?)",
        (t, t),
    )
    conn.execute(
        "INSERT INTO search_candidates(id,query,status,created_at,updated_at) VALUES ('c1','leviathan','pending',?,?)",
        (t, t),
    )
    conn.execute(
        "INSERT INTO search_candidates(id,query,status,created_at,updated_at) VALUES ('c2','x','discarded',?,?)",
        (t, t),
    )


def build_pre_d3d036d() -> None:
    _wipe()
    with db.session() as conn:
        _baseline_schema(conn)
        _old_search_candidates(conn)
        _seed(conn)
        conn.commit()


def build_production_after_d3d036d() -> None:
    """Pre-framework end state, written with explicit SQL (the old init_db logic)."""
    build_pre_d3d036d()
    t = db.now()
    with db.session() as conn:
        # P4-01
        conn.execute("ALTER TABLE search_candidates RENAME TO search_candidates_pre_dismissed")
        conn.executescript(db.EXTRA_SCHEMA)
        conn.execute("INSERT INTO search_candidates SELECT * FROM search_candidates_pre_dismissed")
        conn.execute("DROP TABLE search_candidates_pre_dismissed")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_query ON search_candidates(query)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_status ON search_candidates(status)")
        # P4-09
        conn.execute("ALTER TABLE works ADD COLUMN preferred_ebook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE works ADD COLUMN preferred_audiobook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL")
        # P4-07 + the older backfills
        conn.execute("UPDATE works SET metadata_source='provider' WHERE metadata_source IS NULL AND metadata_id IS NOT NULL")
        conn.execute("UPDATE series SET norm_key='the expanse' WHERE norm_key IS NULL")
        conn.execute("UPDATE media_assets SET status='none',monitored=0,updated_at=? WHERE status='missing' AND has_acquisition_intent=0", (t,))
        for i, name in enumerate(["Wil Wheaton", "Jane Doe"]):
            conn.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (f"c-{i}", name, name, t, t))
            conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES ('w-aud',?,'narrator',?)", (f"c-{i}", i))
        conn.commit()


# Seeded by init_db's ensure_default_*() on every start (INSERT OR IGNORE); unrelated to
# migrations, and absent from the hand-built databases until the first start.
SEEDED = {"tags", "jobs", "profiles", "naming_templates"}


def snapshot() -> dict:
    """Every row of every table except bookkeeping and default-seed ones, plus DDL."""
    out: dict = {}
    with db.session() as conn:
        for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') AND sql IS NOT NULL ORDER BY name"):
            if r["name"] in ("migration_steps", "schema_migrations"):
                continue
            out[f"ddl:{r['name']}"] = r["sql"]
            if r["name"] not in SEEDED and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (r["name"],)).fetchone():
                out[f"rows:{r['name']}"] = [tuple(x) for x in conn.execute(f"SELECT * FROM {r['name']} ORDER BY 1,2")]  # nosec B608
    return out


def steps() -> list[int]:
    with db.session() as conn:
        return [r[0] for r in conn.execute("SELECT version FROM migration_steps ORDER BY version")]


ALL = [v for v, _, _ in migrations.MIGRATIONS]


# Steps 1-38 are on main. Branches that add a step each take the next free number and are renumbered when they merge
# (managed_build, discover_chat, duplicate_finder, ...), so this test checks the invariants rather than one exact list:
# merging another branch's step must not be a logical conflict here.
FIXED_NAMES = [
    "narrator_contributors", "preferred_edition_columns", "search_candidates_dismissed",
    "provider_retry_state", "job_run_observability", "metadata_observations",
    "large_library_indexes", "build_plans", "series_kind_policies",
    "series_junk_and_assertion_backfill",
    "user_sessions_and_subjects",
    "media_progress",
    "read_state_source",
    "player_personal_state",
    "family_controls",
    "devices",
    "kids_decisions",
    "scan_candidates_evidence_json",
    "kids_decisions_auto",
    "media_file_stream_columns",
    "media_file_integrity",
    "maintenance_issues",
    "file_operation_plans",
    "work_merge_ledger",
    "asset_upgrade_policy",
    "work_merge_status_ledger",
    "kids_classification_detail",
    "work_audience_signals",
    "wanted_search_state",
    "downloads_client_stats",
    "media_conversions",
    "trusted_devices",
    "ebook_profile_pdf_rule",
    "series_cleanup_journal",
    "file_trash",
]
BRANCH_STEPS = {"managed_build"}          # added after 38 by this branch (any number, any order among later steps)


def test_steps_are_ordered_and_named():
    named = {v: n for v, n, _ in migrations.MIGRATIONS}
    # Order/gap tolerant on purpose: step 39 belongs to feat/discover-chat and the duplicate finder is 40, so merging the two
    # branches must not need an edit here beyond trivially listing both. Steps 1-38 are pinned exactly.
    assert [named[v] for v in sorted(named) if v <= 38][3:] == [
        "narrator_contributors", "preferred_edition_columns", "search_candidates_dismissed",
        "provider_retry_state", "job_run_observability", "metadata_observations",
        "large_library_indexes", "build_plans", "series_kind_policies",
        "series_junk_and_assertion_backfill",
        "user_sessions_and_subjects",
        "media_progress",
        "read_state_source",
        "player_personal_state",
        "family_controls",
        "devices",
        "kids_decisions",
        "scan_candidates_evidence_json",
        "kids_decisions_auto",
        "media_file_stream_columns",
        "media_file_integrity",
        "maintenance_issues",
        "file_operation_plans",
        "work_merge_ledger",
        "asset_upgrade_policy",
        "work_merge_status_ledger",
        "kids_classification_detail",
        "work_audience_signals",
        "wanted_search_state",
        "downloads_client_stats",
        "media_conversions",
        "trusted_devices",
        "ebook_profile_pdf_rule",
        "series_cleanup_journal",
        "file_trash",
    ]
    assert ALL == sorted(ALL) and ALL[:38] == list(range(1, 39))
    assert named[39] == "discover_chat" and named[40] == "duplicate_finder"
    assert set(ALL) - set(range(1, 39)) <= {39, 40, 41, 42, 43, 44, 45, 46, 47, 48}




def test_fresh_database_records_all_steps_without_backup():
    _wipe()
    db.init_db()
    assert steps() == ALL
    assert _premigrate_backups() == []  # empty database: nothing worth copying
    with db.session() as conn:
        row = conn.execute("SELECT * FROM migration_steps WHERE version=6").fetchone()
        assert row["applied_at"] > 0 and row["applied_at_iso"] and json.loads(row["row_counts_json"]) == {"rows_copied": 0, "already_applied": 1}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='search_candidates'").fetchone()["sql"]
        assert "'dismissed'" in sql
        assert {"preferred_ebook_edition_id", "preferred_audiobook_edition_id"} <= migrations._columns(conn, "works")
    assert migrations.run_pending_migrations() == []


def test_pre_d3d036d_database_is_upgraded_with_data_kept():
    build_pre_d3d036d()
    db.init_db()
    assert steps() == ALL
    with db.session() as conn:
        assert "'dismissed'" in conn.execute("SELECT sql FROM sqlite_master WHERE name='search_candidates'").fetchone()["sql"]
        assert {r[0] for r in conn.execute("SELECT id FROM search_candidates")} == {"c1", "c2"}
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(search_candidates)")}
        assert {"idx_search_candidates_query", "idx_search_candidates_status"} <= idx
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_candidates_pre_dismissed'").fetchone() is None
        conn.execute("UPDATE search_candidates SET status='dismissed' WHERE id='c1'")  # new CHECK accepts it
        names = [r[0] for r in conn.execute(
            "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "
            "WHERE wc.role='narrator' AND wc.work_id='w-aud' ORDER BY wc.ord")]
        assert names == ["Wil Wheaton", "Jane Doe"]
        assert conn.execute("SELECT metadata_source FROM works WHERE id='w-provider'").fetchone()[0] == "provider"
        assert conn.execute("SELECT norm_key FROM series WHERE id='s1'").fetchone()[0]
        assert conn.execute("SELECT status,monitored FROM media_assets WHERE id='a-miss'").fetchone()[:] == ("none", 0)
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=4").fetchone()[0])
        assert counts["rows_added"] == 2
    assert len(_premigrate_backups()) == 1  # data present and steps pending
    before = snapshot()
    db.init_db()  # second start is a no-op
    assert snapshot() == before
    assert len(_premigrate_backups()) == 1


def test_production_after_d3d036d_is_recorded_not_redone():
    build_production_after_d3d036d()
    before = snapshot()
    db.init_db()
    # Steps 7 (F-08), 8 (F-07), 9 (P4-03) and 10 (F-09) post-date production, so they are the ones that change something.
    after = snapshot()
    created = {k for k in after if "provider_retry" in k or "metadata_" in k}
    assert created == {"ddl:provider_retry_state", "ddl:idx_provider_retry_next", "rows:provider_retry_state"} | {
        k for k in after if "metadata_" in k}
    assert any("metadata_observations" in k for k in created)
    f09 = {"ddl:idx_work_contributors_contributor", "ddl:idx_series_memberships_series",
           "ddl:idx_works_library_order", "ddl:idx_works_opds_order"}
    assert f09 <= set(after) and not f09 & set(before)
    created |= f09  # step 10 only adds indexes
    assert "trigger_source" in after["ddl:job_runs"] and "trigger_source" not in before["ddl:job_runs"]
    created.add("ddl:job_runs")  # altered in place by step 8
    created |= {"ddl:build_plans", "rows:build_plans"}  # step 11 only adds an empty table
    # step 15 only adds an empty table and its two indexes
    created |= {"ddl:media_progress", "rows:media_progress", "ddl:idx_media_progress_user", "ddl:idx_media_progress_work"}
    assert "source" in after["ddl:read_states"] and "source" not in before["ddl:read_states"]
    created.add("ddl:read_states")  # altered in place by step 16
    # step 17: sync columns on media_progress, three new empty tables and one index
    assert "client_updated_at" in after["ddl:media_progress"] and "client_updated_at" not in before.get("ddl:media_progress", "")
    created |= {"ddl:bookmarks", "rows:bookmarks", "ddl:idx_bookmarks_user_asset", "ddl:favorites", "rows:favorites",
                "ddl:user_preferences", "rows:user_preferences"}
    # step 18: four new empty tables and their indexes
    created |= {"ddl:content_grants", "rows:content_grants", "ddl:idx_content_grants_work", "ddl:tag_grants", "rows:tag_grants",
                "ddl:requests", "rows:requests", "ddl:idx_requests_user", "ddl:idx_requests_status",
                "ddl:approver_assignments", "rows:approver_assignments", "ddl:idx_approver_assignments_kid"}
    # step 19: two new empty tables and their indexes
    created |= {"ddl:devices", "rows:devices", "ddl:idx_devices_user", "ddl:device_codes", "rows:device_codes", "ddl:idx_device_codes_expires"}
    # step 25: the two maintenance tables (empty) and their indexes
    created |= {"ddl:maintenance_issues", "rows:maintenance_issues", "ddl:maintenance_events", "rows:maintenance_events",
                "ddl:idx_maintenance_issues_kind_status", "ddl:idx_maintenance_issues_status", "ddl:idx_maintenance_events_issue"}
    # step 26: the two file-operation plan tables (empty) and their indexes
    created |= {"ddl:file_operation_plans", "rows:file_operation_plans", "ddl:idx_file_operation_plans_status",
                "ddl:file_operation_steps", "rows:file_operation_steps", "ddl:idx_file_operation_steps_plan"}
    created |= {"ddl:kids_decisions", "rows:kids_decisions"}  # step 20 (no kids tags in the fixture: nothing backfilled)
    created |= {k for k in after if "work_audience" in k}  # step 31: audience signal + backfill-cursor tables, empty
    created |= {k for k in after if "wanted_search_state" in k or "idx_release_searches_auto" in k}  # step 32: search-state table (empty) and index
    assert "speed_bps" in after["ddl:downloads"] and "speed_bps" not in before["ddl:downloads"]
    created.add("ddl:downloads")  # step 33: client stats columns added in place
    # step 34: derived-asset columns on media_assets (altered in place, no row changes), the empty
    # conversions queue table and its indexes
    assert "derived_from_asset_id" in after["ddl:media_assets"] and "derived_from_asset_id" not in before["ddl:media_assets"]
    # (each existing asset row gains three NULL columns: not derived)
    assert all(row[-3:] == (None, None, None) for row in after["rows:media_assets"])
    created |= {"ddl:media_assets", "rows:media_assets"} | {k for k in after if "conversions" in k or "idx_media_assets_derived" in k}
    assert "kind" in after["ddl:devices"]
    created |= {"ddl:idx_devices_prev_token", "ddl:idx_devices_kind_seen"}  # step 35: trusted-browser columns and indexes
    created |= {k for k in after if "work_merge" in k}  # steps 27/29: merge header and ledger tables, empty
    created |= {k for k in after if "series_cleanup" in k}  # step 37: cleanup run + change journal tables, empty
    created |= {k for k in after if "file_trash" in k}  # step 38: the redundant-file trash log, empty
    created |= {k for k in after if any(t in k for t in ("duplicate_groups", "duplicate_scan_state", "work_refresh_conflicts", "merge_batch", "duplicate_refresh_queue"))}  # step 40: duplicate finder tables, empty
    created |= {k for k in after if "discover_" in k}  # step 39: Discover chat conversations, messages and profiles, empty
    assert "checksum_verified" in after["ddl:media_files"] and "checksum_verified" not in before["ddl:media_files"]
    created.add("ddl:media_files")  # step 38 also adds two verification columns in place (rows unchanged apart from NULLs)
    # step 41 (managed_build): managed library build plan items, runs and the managed-copy journal (empty); build_plans gains v2 columns
    created |= {k for k in after if "build_plan_items" in k or "build_items" in k or "build_runs" in k or "managed_copies" in k}
    assert "summary_json" in after["ddl:build_plans"]
    created.add("ddl:build_plans")
    # step 42 (bookmark_kinds): bookmarks gain kind, color and updated_at (this fixture predates the table, so step 17 creates it
    # and step 42 only adds the kind index here; the column additions are covered by test_player_personal_state)
    assert "kind" in after["ddl:bookmarks"]
    created.add("ddl:idx_bookmarks_user_asset_kind")
    # step 46 (root_library_type): root_folders gains library_type in place (existing rows default to 'books')
    assert "library_type" in after["ddl:root_folders"] and "library_type" not in before["ddl:root_folders"]
    created.add("ddl:root_folders")
    # step 47 (file_operation_audit): the plan-execution audit log and its index, empty
    created |= {k for k in after if "file_operation_audit" in k}
    # step 48 (acquisition_flag): stores the acquisition_enabled setting (OFF here: the fixture has no indexer or download client)
    assert [r[0] for r in after["rows:settings"]] == ["acquisition_enabled"]
    created.add("rows:settings")
    # step 43 (podcast_schema): podcast shows, seasons, episodes and progress tables, empty
    created |= {k for k in after if "podcast_" in k}
    # Everything the steps above account for is new; nothing else in the production fixture changed.
    assert {k: v for k, v in after.items() if k not in created} == {k: v for k, v in before.items() if k not in created}
    db.init_db()  # second start: nothing pending, nothing changes
    assert snapshot() == after


def test_step_36_adds_the_pdf_rule_to_an_existing_ebook_default_profile():
    old = {"cutoff": "epub", "preferred_formats": ["epub", "pdf"]}
    with db.tx() as conn:
        conn.execute("UPDATE profiles SET config_json=? WHERE id='ebook-default'", (db.json_dump(old),))
        assert migrations._migrate_ebook_profile_pdf_rule(conn) == {"profiles_updated": 1}
    cfg = db.json_load(db.one("SELECT config_json FROM profiles WHERE id='ebook-default'")["config_json"], {})
    assert cfg == {**old, "pdf_undesirable_if_epub": True}
    cfg["pdf_undesirable_if_epub"] = False  # an operator's explicit choice survives a re-run
    with db.tx() as conn:
        conn.execute("UPDATE profiles SET config_json=? WHERE id='ebook-default'", (db.json_dump(cfg),))
        assert migrations._migrate_ebook_profile_pdf_rule(conn) == {"profiles_updated": 0}
    assert db.json_load(db.one("SELECT config_json FROM profiles WHERE id='ebook-default'")["config_json"], {})["pdf_undesirable_if_epub"] is False


def test_step_39_discover_chat_applies_to_a_step_38_database_and_is_rerunnable():
    discover = ("discover_messages", "discover_conversations", "discover_profiles", "discover_usage")
    _wipe()
    db.init_db()
    with db.session() as conn:   # a database that has applied everything up to step 38
        for t in discover:
            conn.execute(f"DROP TABLE {t}")
        conn.execute("DELETE FROM migration_steps WHERE version=39")
        conn.commit()
    assert steps() == [v for v in ALL if v != 39]
    results = migrations.run_pending_migrations()
    assert [r["version"] for r in results] == [39]
    assert results[0]["row_counts"] == {"tables_created": 4, "already_applied": 0}
    with db.session() as conn:
        for t in ("discover_conversations", "discover_profiles"):
            assert "account_type" in migrations._columns(conn, t)
        migrations._validate_discover_chat(conn)
        assert migrations._migrate_discover_chat(conn) == {"tables_created": 0, "already_applied": 1}
    assert migrations.run_pending_migrations() == []
    assert "ALTER TABLE" not in inspect.getsource(migrations._migrate_discover_chat)


def _audit_columns(conn) -> dict:
    return {r["name"]: (r["type"], r["notnull"], r["pk"]) for r in conn.execute("PRAGMA table_info(file_operation_audit)")}


def test_step_47_file_operation_audit_fresh_pre_change_and_already_applied():
    # fresh: init_db() creates the table with the documented columns and types
    _wipe()
    db.init_db()
    assert 47 in steps()
    with db.session() as conn:
        cols = _audit_columns(conn)
        assert list(cols) == ["id", "plan_id", "step_id", "event", "detail_json", "created_at"]
        assert cols["id"] == ("INTEGER", 0, 1)
        assert cols["plan_id"] == ("TEXT", 1, 0) and cols["step_id"] == ("TEXT", 0, 0)
        assert cols["event"] == ("TEXT", 1, 0) and cols["detail_json"] == ("TEXT", 1, 0) and cols["created_at"] == ("REAL", 1, 0)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_file_operation_audit_plan'").fetchone()
        migrations._validate_file_operation_audit(conn)
        fresh_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='file_operation_audit'").fetchone()["sql"]
        # already applied: re-running the step reports it and changes nothing
        assert migrations._migrate_file_operation_audit(conn) == {"tables_created": 0, "already_applied": 1}
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='file_operation_audit'").fetchone()["sql"] == fresh_sql
    assert migrations.run_pending_migrations() == []

    # pre-change: an existing production database has no audit table until the upgrade runs
    build_production_after_d3d036d()
    with db.session() as conn:
        assert not migrations._table_exists(conn, "file_operation_audit")
    db.init_db()
    assert 47 in steps()
    with db.session() as conn:
        assert _audit_columns(conn) == cols
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name='file_operation_audit'").fetchone()["sql"] == fresh_sql
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=47").fetchone()[0])
        assert counts == {"tables_created": 1, "already_applied": 0}


def test_step_47_validation_rejects_a_table_missing_columns():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(MigrationError):
            migrations._validate_file_operation_audit(conn)
        conn.execute("CREATE TABLE file_operation_audit (id INTEGER PRIMARY KEY, plan_id TEXT)")
        with pytest.raises(MigrationError):
            migrations._validate_file_operation_audit(conn)
