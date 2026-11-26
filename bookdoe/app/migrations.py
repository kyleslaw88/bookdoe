"""Versioned migration framework for bookdoe (F-04, spec Part XL).

Ordered, recorded, validated steps.  On startup (db.init_db) the framework:

1. Reads the applied steps from ``migration_steps`` (its own table: the legacy
   ``schema_migrations`` table already holds unrelated markers - 1 = baseline,
   2 = contributor de-dupe repair - so the version spaces must not be shared).
2. If any step is pending and the database holds data, writes a pre-migration
   backup (services/backup, prefix ``premigrate``, newest
   ``BOOKDOE_MIGRATION_BACKUP_RETENTION`` kept).  Nothing pending -> no backup.
3. Runs the pending steps in order inside ONE transaction, running each step's
   validation straight after it and recording version, name, timestamp, row counts.
4. A step or validation failure rolls the whole transaction back (database left as
   it was), leaves the backup and raises MigrationError, which stops startup.

Every step detects state already produced by older code (production ran the
pre-framework equivalents at d3d036d) and then changes nothing, recording
``already_applied`` in its row counts.  Downgrades are out of scope.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

# Fresh-install DDL of the Part XXVI merge tables. db.EXTRA_SCHEMA appends it, so a fresh
# install and one rebuilt by step 29 get exactly the same tables. It sits above the db
# import because db imports this module while it is loading, to pick this up.
# -- work_merges: one row per executed merge; the sources are gone from works, so they live
#    here as JSON. canonical_work_id follows a later merge (workmerge repoints it) and goes
#    NULL only if the canonical is deleted. status moves off 'executed' only by a reversal
#    (P5-25a).
# -- work_merge_ledger: every row a merge touched, in id order. before_json is the full row
#    before the change (NULL for an inserted row), after_json the row after it (NULL for a
#    deleted row), pk_json the key the row has now. A reversal walks the merge's rows in
#    reverse id order. The merge_id index is step 29's, not a fresh install's.
WORK_MERGE_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS work_merges (
  id TEXT PRIMARY KEY,
  canonical_work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
  source_work_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'executed' CHECK(status IN ('executed','reversed','reversal_failed')),
  resolutions_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_merge_ledger (
  id INTEGER PRIMARY KEY,
  merge_id TEXT NOT NULL REFERENCES work_merges(id) ON DELETE CASCADE,
  table_name TEXT NOT NULL,
  pk_json TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('moved','updated','deleted','inserted')),
  before_json TEXT,
  after_json TEXT
);
"""

from . import db  # noqa: E402 - after WORK_MERGE_TABLES_DDL on purpose, see above
from .config import settings  # noqa: E402

logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """A migration step or its validation failed; startup must stop."""


MigrationFn = Callable[[sqlite3.Connection], dict[str, int]]

# (version, name, fn) ordered by version.
MIGRATIONS: list[tuple[int, str, MigrationFn]] = []
# (version, validation_fn); validation raises on failure.
VALIDATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = []

STEPS_DDL = """
CREATE TABLE IF NOT EXISTS migration_steps (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at REAL NOT NULL,
  applied_at_iso TEXT NOT NULL,
  row_counts_json TEXT NOT NULL DEFAULT '{}',
  duration_ms INTEGER NOT NULL DEFAULT 0
)
"""


def register(version: int) -> Callable:
    """Register a migration step.  The step gets the open transaction connection."""

    def wrapper(fn: MigrationFn) -> MigrationFn:
        if any(v == version for v, _, _ in MIGRATIONS):
            raise ValueError(f"duplicate migration version {version}")
        MIGRATIONS.append((version, fn.__name__.removeprefix("_migrate_"), fn))
        MIGRATIONS.sort(key=lambda t: t[0])
        return fn

    return wrapper


def register_validation(version: int) -> Callable:
    """Register a post-step validation for ``version``; raise to fail startup."""

    def wrapper(fn: Callable[[sqlite3.Connection], None]) -> Callable:
        VALIDATIONS.append((version, fn))
        return fn

    return wrapper


def _repair_misnumbered_steps(conn: sqlite3.Connection) -> None:
    """MIGRATION HAZARD (duplicate finder). Its first build (commit f5dfcd9) recorded the finder as step 39; it is step 40 now
    because 39 belongs to feat/discover-chat. A database that ran that build has ``migration_steps`` row 39 named
    'duplicate_finder', which would make a real step 39 look applied and never run. The stale row is dropped (the finder's
    step 40 is idempotent and also adds the columns the first build lacked). Only a row with that exact name is touched."""
    conn.execute("DELETE FROM migration_steps WHERE version=39 AND name='duplicate_finder'")


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(STEPS_DDL)
    _repair_misnumbered_steps(conn)
    return {r[0] for r in conn.execute("SELECT version FROM migration_steps")}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _database_has_data(conn: sqlite3.Connection) -> bool:
    # A brand-new database has nothing worth a backup.  Anything with works, users
    # or an old-style search table is real.
    for table in ("works", "users", "media_assets"):
        if _table_exists(conn, table) and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():  # nosec B608
            return True
    return False


def _create_pre_migration_backup() -> dict[str, Any]:
    """Backup via services/backup.  Failure is fatal: a destructive rebuild must not
    run without its safety copy."""
    from .services import backup as backup_svc

    try:
        return backup_svc.create_backup(prefix="premigrate", retention=settings.migration_backup_retention)
    except Exception as exc:
        raise MigrationError(f"Pre-migration backup failed, refusing to migrate: {exc}") from exc


# ---------------------------------------------------------------------------
# Steps.  1-3 are the original idempotent backfills (effect unchanged); 4-6 are
# the P4-07 / P4-09 / P4-01 changes that previously lived inline in db.py.
# ---------------------------------------------------------------------------


@register(1)
def _migrate_backfill_metadata_source(conn: sqlite3.Connection) -> dict[str, int]:
    """Classify metadata_source for rows that predate the column (only NULL rows touched)."""
    p = conn.execute(
        "UPDATE works SET metadata_source='provider' WHERE metadata_source IS NULL AND metadata_id IS NOT NULL"
    ).rowcount
    l = conn.execute(
        "UPDATE works SET metadata_source='local' WHERE metadata_source IS NULL AND id IN "
        "(SELECT work_id FROM local_meta WHERE applied_json<>'{}')"
    ).rowcount
    return {"rows_provider": p, "rows_local": l, "already_applied": int(p + l == 0)}


@register(2)
def _migrate_backfill_series_norm_key(conn: sqlite3.Connection) -> dict[str, int]:
    """Compute norm_key for series rows created before the column existed."""
    from .services import seriesnorm

    rows = conn.execute("SELECT id, name FROM series WHERE norm_key IS NULL").fetchall()
    for r in rows:
        conn.execute("UPDATE series SET norm_key=? WHERE id=?", (seriesnorm.norm_key(r["name"]), r["id"]))
    return {"rows_updated": len(rows), "already_applied": int(not rows)}


@register(3)
def _migrate_backfill_acquisition_intent(conn: sqlite3.Connection) -> dict[str, int]:
    """status='missing' requires acquisition intent; demote the rest to inert slots."""
    nulls = conn.execute("UPDATE media_assets SET has_acquisition_intent=0 WHERE has_acquisition_intent IS NULL").rowcount
    demoted = conn.execute(
        "UPDATE media_assets SET status='none',monitored=0,updated_at=? "
        "WHERE status='missing' AND has_acquisition_intent=0",
        (db.now(),),
    ).rowcount
    return {"rows_null_fixed": nulls, "rows_demoted": demoted, "already_applied": int(nulls + demoted == 0)}


def _narrator_names(raw: Any) -> list[str]:
    return [n.strip() for n in (db.json_load(raw, []) or []) if isinstance(n, str) and n.strip()]


@register(4)
def _migrate_narrator_contributors(conn: sqlite3.Connection) -> dict[str, int]:
    """P4-07: work_contributors(role='narrator') rows for audiobooks carrying narrator_json.

    Only works with no narrator row yet are considered and INSERT OR IGNORE keeps
    it idempotent, so a database where the pre-framework backfill already ran has
    nothing to select and nothing changes."""
    rows = conn.execute(
        "SELECT a.work_id, a.narrator_json FROM media_assets a "
        "WHERE a.kind='audiobook' AND a.narrator_json IS NOT NULL AND a.narrator_json NOT IN ('', '[]') "
        "AND NOT EXISTS (SELECT 1 FROM work_contributors wc WHERE wc.work_id=a.work_id AND wc.role='narrator')"
    ).fetchall()
    added = 0
    for row in rows:
        for i, name in enumerate(db.json_load(row["narrator_json"], []) or []):
            if not isinstance(name, str) or not name.strip():
                continue
            cid = db.contributor_id_for_name(conn, name.strip())
            if cid:
                added += conn.execute(
                    "INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                    (row["work_id"], cid, "narrator", i),
                ).rowcount or 0
    return {"assets_scanned": len(rows), "rows_added": added, "already_applied": int(added == 0)}


PREFERRED_EDITION_COLUMNS = {
    "preferred_ebook_edition_id": "preferred_ebook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL",
    "preferred_audiobook_edition_id": "preferred_audiobook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL",
}


@register(5)
def _migrate_preferred_edition_columns(conn: sqlite3.Connection) -> dict[str, int]:
    """P4-09: works.preferred_{ebook,audiobook}_edition_id (nullable; existing rows stay NULL)."""
    existing = _columns(conn, "works")
    added = 0
    for col, ddl in PREFERRED_EDITION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE works ADD COLUMN {ddl}")
            added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}


def _search_candidates_ddl() -> str:
    m = re.search(r"CREATE TABLE IF NOT EXISTS search_candidates \(.*?\n\);", db.EXTRA_SCHEMA, re.S)
    if not m:  # pragma: no cover - guards a schema refactor
        raise MigrationError("search_candidates DDL not found in db.EXTRA_SCHEMA")
    return m.group(0).replace("IF NOT EXISTS ", "", 1)


@register(6)
def _migrate_search_candidates_dismissed(conn: sqlite3.Connection) -> dict[str, int]:
    """P4-01: widen search_candidates.status CHECK to include 'dismissed'.

    SQLite cannot alter a CHECK, so: rename the old table, create the current one,
    copy every row across, drop the old table, recreate both indexes (the rename
    kept the old index names attached to the old table).  If the stored CHECK
    already contains 'dismissed' (fresh install, or production after d3d036d) and no
    leftover table exists, nothing is touched.  A leftover search_candidates_
    pre_dismissed from an interrupted pre-framework run is merged back (rows
    already present win) and dropped."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='search_candidates'").fetchone()
    leftover = had_leftover = _table_exists(conn, "search_candidates_pre_dismissed")
    if row is not None and "'dismissed'" in row["sql"] and not leftover:
        return {"rows_copied": 0, "already_applied": 1}
    if row is not None and "'dismissed'" not in row["sql"]:
        if leftover:
            raise MigrationError("search_candidates and search_candidates_pre_dismissed both exist with the old CHECK; manual repair needed")
        conn.execute("ALTER TABLE search_candidates RENAME TO search_candidates_pre_dismissed")
        leftover = True
    if row is None or (leftover and not _table_exists(conn, "search_candidates")):
        conn.execute(_search_candidates_ddl())
    copied = 0
    if leftover:
        before = conn.execute("SELECT COUNT(*) FROM search_candidates_pre_dismissed").fetchone()[0]
        cols = ",".join(r["name"] for r in conn.execute("PRAGMA table_info(search_candidates)").fetchall()
                        if r["name"] in _columns(conn, "search_candidates_pre_dismissed"))
        conn.execute(f"INSERT OR IGNORE INTO search_candidates({cols}) SELECT {cols} FROM search_candidates_pre_dismissed")  # nosec B608
        copied = before
        if not had_leftover and conn.execute("SELECT COUNT(*) FROM search_candidates").fetchone()[0] != before:
            raise MigrationError("search_candidates row count changed during CHECK rebuild")
        conn.execute("DROP TABLE search_candidates_pre_dismissed")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_query ON search_candidates(query)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_status ON search_candidates(status)")
    return {"rows_copied": copied, "already_applied": 0}


PROVIDER_RETRY_STATE_DDL = """
CREATE TABLE IF NOT EXISTS provider_retry_state (
  provider TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('transient','rate_limited','deferred','permanent')),
  attempts INTEGER NOT NULL DEFAULT 1,
  next_retry_at REAL,
  last_error TEXT,
  first_failed_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (provider, entity_type, entity_id)
)
"""


@register(7)
def _migrate_provider_retry_state(conn: sqlite3.Connection) -> dict[str, int]:
    """F-08: provider_retry_state, the persisted retry schedule for metadata and search
    providers (services/provider_results).  New table, no existing rows touched."""
    if _table_exists(conn, "provider_retry_state"):
        return {"tables_created": 0, "already_applied": 1}
    conn.execute(PROVIDER_RETRY_STATE_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_retry_next ON provider_retry_state(next_retry_at)")
    return {"tables_created": 1, "already_applied": 0}


JOB_RUN_COLUMNS = {
    "execution_id": "execution_id TEXT",
    "trigger_source": "trigger_source TEXT NOT NULL DEFAULT 'schedule'",
    "entities_json": "entities_json TEXT NOT NULL DEFAULT '{}'",
    "attempt": "attempt INTEGER NOT NULL DEFAULT 1",
    "retry_of": "retry_of TEXT",
}


@register(8)
def _migrate_job_run_observability(conn: sqlite3.Connection) -> dict[str, int]:
    """F-07: job_runs execution id, trigger, affected entities and retry state.
    Existing rows keep the defaults (trigger 'schedule', attempt 1)."""
    existing = _columns(conn, "job_runs")
    added = 0
    for col, ddl in JOB_RUN_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE job_runs ADD COLUMN {ddl}")
            added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}


@register(9)
def _migrate_metadata_observations(conn: sqlite3.Connection) -> dict[str, int]:
    """P4-03: generic per-field observations + field locks (Part XXI foundation).

    Schema only, no behaviour change: nothing writes to these tables yet.
    ``field_name`` is free text so new fields never need a migration.  At most one
    ``is_current=1`` observation per entity+field is enforced by a partial unique
    index.  A lock is the presence of a ``metadata_field_locks`` row.  Every
    statement is IF NOT EXISTS, so a restart or a database that already has the
    tables is left untouched."""
    existed = _table_exists(conn, "metadata_observations") and _table_exists(conn, "metadata_field_locks")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metadata_observations ("
        " id TEXT PRIMARY KEY,"
        " entity_type TEXT NOT NULL CHECK(entity_type IN ('work','edition','media_file')),"
        " entity_id TEXT NOT NULL,"
        " field_name TEXT NOT NULL,"
        " value TEXT,"
        " source TEXT,"
        " confidence REAL NOT NULL DEFAULT 0,"
        " is_current INTEGER NOT NULL DEFAULT 0,"
        " observed_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_observations_field "
        "ON metadata_observations(entity_type,entity_id,field_name)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_metadata_observations_current "
        "ON metadata_observations(entity_type,entity_id,field_name) WHERE is_current=1"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metadata_field_locks ("
        " entity_type TEXT NOT NULL,"
        " entity_id TEXT NOT NULL,"
        " field_name TEXT NOT NULL,"
        " locked_at REAL NOT NULL,"
        " locked_by TEXT,"
        " PRIMARY KEY(entity_type,entity_id,field_name))"
    )
    return {"already_applied": int(existed)}


@register(10)
def _migrate_large_library_indexes(conn: sqlite3.Connection) -> dict[str, int]:
    """F-09: indexes the 100,000-work performance tests showed were needed.

    work_contributors / series_memberships are keyed work-first, so author and series
    lookups by contributor_id / series_id had no index (the author list built a
    throwaway automatic index on every request).  The two works indexes let the
    Library (COALESCE(sort_title,title)) and OPDS (sort_title,title,id) page orders
    walk an index instead of sorting every work.  Every statement is IF NOT EXISTS,
    so a database that already has them is left untouched."""
    wanted = {
        "idx_work_contributors_contributor": "work_contributors(contributor_id,role)",
        "idx_series_memberships_series": "series_memberships(series_id)",
        "idx_works_library_order": "works(COALESCE(sort_title,title))",
        "idx_works_opds_order": "works(sort_title,title,id)",
    }
    if "sort_title" not in _columns(conn, "works"):  # very old databases predate the column
        conn.execute("ALTER TABLE works ADD COLUMN sort_title TEXT")
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for name, target in wanted.items():
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
    return {"indexes_created": len(set(wanted) - have), "already_applied": int(set(wanted) <= have)}


@register(11)
def _migrate_build_plans(conn: sqlite3.Connection) -> dict[str, int]:
    """P4-13a-3: stored managed-library-build dry-run plans plus the explicit
    confirmation and execution stamps that gate the run."""
    existed = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='build_plans'").fetchone())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS build_plans ("
        "id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, created_at REAL NOT NULL, "
        "confirmed_at REAL, executed_at REAL)"
    )
    return {"already_applied": int(existed)}


@register(12)
def _migrate_series_kind_policies(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-08 (Part XV): independent audiobook / ebook follow policy per series.

    Backfill: the old single policy applies to each kind whose boolean is 1, else 'none'.
    The legacy columns stay and are kept in sync by services/series.set_series_monitor."""
    have = _columns(conn, "series_monitors")
    if {"audiobook_policy", "ebook_policy"} <= have:
        return {"rows_backfilled": 0, "already_applied": 1}
    for col in ("audiobook_policy", "ebook_policy"):
        if col not in have:
            conn.execute(f"ALTER TABLE series_monitors ADD COLUMN {col} TEXT CHECK({col} IN ('none','future','all'))")
    conn.execute(
        "UPDATE series_monitors SET "
        "audiobook_policy=CASE WHEN audiobook=1 THEN policy ELSE 'none' END, "
        "ebook_policy=CASE WHEN ebook=1 THEN policy ELSE 'none' END"
    )
    return {"rows_backfilled": conn.execute("SELECT COUNT(*) FROM series_monitors").fetchone()[0], "already_applied": 0}


@register(13)
def _migrate_series_junk_and_assertion_backfill(conn: sqlite3.Connection) -> dict[str, int]:
    """D1 (Part XV): remove series rows whose name is junk under the extended
    seriesnorm.is_junk (identifiers, "N pages", "(Imprint)", "digits|digits",
    "Novels by X"), then give every membership that has no assertion one, scored by
    the same function metadata._link_series uses. Without evidence rows the
    list_series uncertain filter had nothing to act on in production (3 assertions
    for 1,963 memberships). Idempotent: only memberships lacking an assertion get
    one, and a second run finds no junk."""
    from .services import metadata, seriesnorm  # lazy: services import this package's siblings

    junk_ids = [r["id"] for r in conn.execute("SELECT id, name FROM series").fetchall() if seriesnorm.is_junk(r["name"])]
    for i in range(0, len(junk_ids), 500):
        chunk = junk_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for table in ("series_assertions", "series_memberships", "series_monitors", "series"):
            col = "id" if table == "series" else "series_id"
            conn.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", chunk)  # nosec B608

    rows = conn.execute(
        """SELECT sm.work_id, sm.series_id, sm.position, s.name,
                  (SELECT COUNT(*) FROM series_memberships o WHERE o.series_id=sm.series_id) members
           FROM series_memberships sm JOIN series s ON s.id=sm.series_id
           WHERE NOT EXISTS (SELECT 1 FROM series_assertions sa
                             WHERE sa.work_id=sm.work_id AND sa.series_id=sm.series_id)"""
    ).fetchall()
    t = db.now()
    batch = []
    for r in rows:
        evidence, confidence, status = metadata._score_series_link(r["name"], r["position"], r["members"] - 1)
        batch.append((db.uid(), r["work_id"], r["series_id"], "backfill", r["name"], r["name"],
                      r["position"], evidence, confidence, status, t, t))
    conn.executemany(
        """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
           proposed_position,evidence_type,confidence,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        batch,
    )
    return {"junk_series_deleted": len(junk_ids), "assertions_backfilled": len(batch)}


@register(14)
def _migrate_user_sessions_and_subjects(conn: sqlite3.Connection) -> dict[str, int]:
    """Security review C4/C6: the IdP `sub` an account is linked to (so OIDC matches on
    a stable identifier, not a mutable username) and a per-user session version that
    password changes and demotions bump to revoke outstanding session cookies."""
    have = _columns(conn, "users")
    added = 0
    if "auth_subject" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN auth_subject TEXT")
        added += 1
    if "session_version" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1")
        added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}


@register(15)
def _migrate_media_progress(conn: sqlite3.Connection) -> dict[str, int]:
    """In-browser reader/player (spec Part XXXVII groundwork): where each user is in
    each asset. Per asset, not per work, because the same person can be halfway through
    the audiobook and on page 3 of the ebook of one title. ``position`` is seconds into
    ``file_id`` for audio; ``location`` is the renderer's own bookmark (an EPUB CFI or a
    PDF page) for ebooks; ``percent`` is the comparable 0..1 figure for both."""
    existed = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_progress'").fetchone())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media_progress ("
        "asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
        "kind TEXT NOT NULL CHECK(kind IN ('ebook','audiobook')), "
        "file_id TEXT REFERENCES media_files(id) ON DELETE SET NULL, "
        "position REAL NOT NULL DEFAULT 0, "
        "location TEXT, "
        "percent REAL, "
        "updated_at REAL NOT NULL, "
        "PRIMARY KEY(asset_id,user_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_progress_user ON media_progress(user_id,updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_progress_work ON media_progress(work_id,user_id)")
    return {"already_applied": int(existed)}


@register(16)
def _migrate_read_state_source(conn: sqlite3.Connection) -> dict[str, int]:
    """Who set a read state: the person, via the Reading-status picker ('manual'), or
    the reader/player, which marks a book 'reading' the moment a position is first
    saved ('auto'). Without this the two are indistinguishable, and clearing progress
    could not tell a 'reading' the person chose for a paper copy from one the reader
    set on their behalf. Every pre-existing row predates auto-marking, so 'manual'
    is the correct default for all of them."""
    have = _columns(conn, "read_states")
    if "source" in have:
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE read_states ADD COLUMN source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','auto'))")
    return {"columns_added": 1, "already_applied": 0}


PROGRESS_SYNC_COLUMNS = {
    "device_id": "device_id TEXT",
    "client_updated_at": "client_updated_at REAL",
    "duration": "duration REAL",
}


@register(17)
def _migrate_player_personal_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Player phase 2: per-account state the
    Player needs beyond the position itself.

    media_progress gains the sync metadata the conflict rule orders updates by
    (which device wrote it and that device's clock) plus the audio duration a client
    reported; existing rows keep NULLs and are treated as "older than anything".
    bookmarks, favorites and user_preferences are new, user-owned tables. Additive
    only: a code rollback leaves them unused and harmless."""
    have = _columns(conn, "media_progress")
    added = 0
    for col, ddl in PROGRESS_SYNC_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE media_progress ADD COLUMN {ddl}")
            added += 1
    existed = sum(_table_exists(conn, t) for t in ("bookmarks", "favorites", "user_preferences"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bookmarks ("
        "id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE, "
        "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
        "location TEXT, position REAL, percent REAL, title TEXT, note TEXT, "
        "created_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_user_asset ON bookmarks(user_id,asset_id,created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS favorites ("
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
        "created_at REAL NOT NULL, PRIMARY KEY(user_id,work_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_preferences ("
        "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
        "prefs_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL)"
    )
    return {"columns_added": added, "tables_created": 3 - existed, "already_applied": int(added == 0 and existed == 3)}


FAMILY_TABLES = ("content_grants", "tag_grants", "requests", "approver_assignments")


@register(18)
def _migrate_family_controls(conn: sqlite3.Connection) -> dict[str, int]:
    """Phase 3 (plan sections 4 and 5): per-kid content grants (book and tag level),
    user requests that map onto the existing acquisition intent, and the adult ->
    kid approver assignments. All new tables, nothing existing touched; the `kids`
    system tag keeps its meaning as the household-wide shelf."""
    existed = sum(_table_exists(conn, t) for t in FAMILY_TABLES)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS content_grants ("
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
        "granted_by TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "note TEXT, created_at REAL NOT NULL, PRIMARY KEY(user_id,work_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_content_grants_work ON content_grants(work_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tag_grants ("
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE, "
        "granted_by TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "created_at REAL NOT NULL, PRIMARY KEY(user_id,tag_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        "id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "work_id TEXT REFERENCES works(id) ON DELETE SET NULL, "
        "metadata_id TEXT, title TEXT NOT NULL, author TEXT, "
        "kinds_json TEXT NOT NULL DEFAULT '[\"ebook\"]', "
        "status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','cancelled')), "
        "decided_by TEXT REFERENCES users(id) ON DELETE SET NULL, decided_at REAL, decision_note TEXT, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status,created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS approver_assignments ("
        "approver_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "kid_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "created_by TEXT REFERENCES users(id) ON DELETE SET NULL, "
        "created_at REAL NOT NULL, PRIMARY KEY(approver_user_id,kid_user_id))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_approver_assignments_kid ON approver_assignments(kid_user_id)")
    return {"tables_created": len(FAMILY_TABLES) - existed, "already_applied": int(existed == len(FAMILY_TABLES))}


@register(19)
def _migrate_devices(conn: sqlite3.Connection) -> dict[str, int]:
    """Phase 4 (plan section 6): registered reading devices and the pairing codes that
    mint their tokens. A device row is a user-scoped, revocable credential: only the
    SHA-256 of the token is stored (like api_keys), with a display prefix. device_codes
    are short-lived pairing requests (OAuth 2.0 device-grant shaped), consumed once."""
    existed = sum(_table_exists(conn, t) for t in ("devices", "device_codes"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS devices ("
        "id TEXT PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "name TEXT NOT NULL, platform TEXT, client_version TEXT, "
        "token_hash TEXT UNIQUE NOT NULL, token_prefix TEXT, "
        "created_at REAL NOT NULL, last_seen_at REAL, expires_at REAL, revoked_at REAL, "
        "rotated_from TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id,revoked_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS device_codes ("
        "device_code_hash TEXT PRIMARY KEY, "
        "user_code TEXT UNIQUE NOT NULL, "
        "name TEXT, platform TEXT, client_version TEXT, "
        "created_at REAL NOT NULL, expires_at REAL NOT NULL, "
        "approved_by TEXT REFERENCES users(id) ON DELETE CASCADE, approved_at REAL, "
        "denied_at REAL, consumed_at REAL, last_poll_at REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device_codes_expires ON device_codes(expires_at)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}


@register(20)
def _migrate_kids_decisions(conn: sqlite3.Connection) -> dict[str, int]:
    """Kids-shelf manual override marker. One row per work a person decided about:
    `state` on/off, who, when and through which action. The classifier
    (services/tagging) never touches a work that has a row, on any code path, so a
    human decision stands until a human changes it. Existing kids tags are backfilled
    as decisions when they were placed by hand (work_tags.source='manual'); tags that
    came through an approved proposal ('llm') are backfilled too, because an admin
    clicked Approve - the classifier must not re-propose them either."""
    existed = _table_exists(conn, "kids_decisions")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kids_decisions ("
        "work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE, "
        "state TEXT NOT NULL CHECK(state IN ('on','off')), "
        "via TEXT NOT NULL CHECK(via IN ('manual_tag','manual_untag','proposal_approved','proposal_rejected','backfill')), "
        "decided_by TEXT, decided_at REAL NOT NULL, note TEXT)"
    )
    backfilled = 0
    if not existed:
        t = db.now()
        backfilled = conn.execute(
            "INSERT OR IGNORE INTO kids_decisions(work_id,state,via,decided_by,decided_at,note) "
            "SELECT wt.work_id,'on','backfill',NULL,?,'existing kids tag at upgrade' FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE t.name='kids'",
            (t,),
        ).rowcount
    return {"tables_created": int(not existed), "decisions_backfilled": backfilled, "already_applied": int(existed)}


@register(21)
def _migrate_scan_candidates_evidence_json(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-05-1b: Add evidence_json column to scan_candidates for structured match evidence.

    The column stores a JSON array of evidence signals produced by
    ``services/matchevidence.evidence_for()``.  Already-applied databases
    (from older code that created the column inline) are detected and skipped.
    """
    cols = _columns(conn, "scan_candidates")
    if "evidence_json" in cols:
        return {"already_applied": 1}
    conn.execute(
        "ALTER TABLE scan_candidates ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'"
    )
    return {"columns_added": 1, "already_applied": 0}


KIDS_DECISIONS_VIA = "('manual_tag','manual_untag','proposal_approved','proposal_rejected','backfill','auto_approved')"


@register(22)
def _migrate_kids_decisions_auto(conn: sqlite3.Connection) -> dict[str, int]:
    """Kids auto-approval: the via CHECK gains 'auto_approved'. SQLite cannot alter a
    CHECK, so the table is rebuilt (create new, copy, drop, rename) inside the
    migration transaction; row counts are verified. Already-widened tables are left
    alone."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kids_decisions'").fetchone()
    if row and "'auto_approved'" in row["sql"]:
        return {"rows_copied": 0, "already_applied": 1}
    before = conn.execute("SELECT COUNT(*) FROM kids_decisions").fetchone()[0]
    conn.execute("DROP TABLE IF EXISTS kids_decisions_new")
    conn.execute(
        "CREATE TABLE kids_decisions_new ("
        "work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE, "
        "state TEXT NOT NULL CHECK(state IN ('on','off')), "
        f"via TEXT NOT NULL CHECK(via IN {KIDS_DECISIONS_VIA}), "
        "decided_by TEXT, decided_at REAL NOT NULL, note TEXT)"
    )
    conn.execute("INSERT INTO kids_decisions_new(work_id,state,via,decided_by,decided_at,note) SELECT work_id,state,via,decided_by,decided_at,note FROM kids_decisions")
    conn.execute("DROP TABLE kids_decisions")
    conn.execute("ALTER TABLE kids_decisions_new RENAME TO kids_decisions")
    after = conn.execute("SELECT COUNT(*) FROM kids_decisions").fetchone()[0]
    if after != before:
        raise MigrationError("kids_decisions row count changed during CHECK rebuild")
    return {"rows_copied": after, "already_applied": 0}


MEDIA_FILE_STREAM_COLUMNS = {
    "sample_rate": "sample_rate INTEGER",
    "channels": "channels INTEGER",
    "chapter_count": "chapter_count INTEGER",
    "has_embedded_cover": "has_embedded_cover INTEGER",
}


@register(23)
def _migrate_media_file_stream_columns(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-02: media_files stream data (nullable; NULL = never probed, so existing rows stay NULL
    until scheduler's library-health backfill probes them)."""
    existing = _columns(conn, "media_files")
    added = 0
    for col, ddl in MEDIA_FILE_STREAM_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE media_files ADD COLUMN {ddl}")
            added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}


MEDIA_FILE_INTEGRITY_COLUMNS = {
    "integrity_state": "integrity_state TEXT CHECK(integrity_state IN ('ok','changed','missing','damaged','unverified'))",
    "integrity_detail_json": "integrity_detail_json TEXT NOT NULL DEFAULT '{}'",
}


@register(24)
def _migrate_media_file_integrity(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-14: media_files.integrity_state / integrity_detail_json written by services/fileverify.
    NULL state = never verified, so existing rows stay NULL until someone verifies them."""
    existing = _columns(conn, "media_files")
    added = 0
    for col, ddl in MEDIA_FILE_INTEGRITY_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE media_files ADD COLUMN {ddl}")
            added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}


MAINTENANCE_TABLES = ("maintenance_issues", "maintenance_events")


@register(25)
def _migrate_maintenance_issues(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-18: maintenance issue store (one row per stable fingerprint) and its audit trail.
    New tables only, so existing data is untouched; the indexes are created here because
    they live with the step, not with SCHEMA.

    Deliberately not mirrored in db.py SCHEMA/EXTRA_SCHEMA: this step is the only copy of the
    DDL, so a fresh install and an upgrade cannot drift apart. Fresh installs still get the
    tables because init_db() runs every pending step right after the schema script (the same
    note sits next to EXTRA_SCHEMA in db.py)."""
    existed = sum(_table_exists(conn, t) for t in MAINTENANCE_TABLES)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS maintenance_issues ("
        "id TEXT PRIMARY KEY, "
        "kind TEXT NOT NULL, "
        "severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')), "
        "subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, "
        "fingerprint TEXT NOT NULL UNIQUE, "
        "status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','dismissed','resolved','repaired')), "
        "detail_json TEXT NOT NULL DEFAULT '{}', "
        "repair_action TEXT, bulk_safe INTEGER NOT NULL DEFAULT 0, "
        "first_seen REAL NOT NULL, last_seen REAL NOT NULL, resolved_at REAL, repair_plan_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS maintenance_events ("
        "id TEXT PRIMARY KEY, "
        "issue_id TEXT NOT NULL REFERENCES maintenance_issues(id) ON DELETE CASCADE, "
        "event TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}', "
        "user_id TEXT, created_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_issues_kind_status ON maintenance_issues(kind,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_issues_status ON maintenance_issues(status,last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_events_issue ON maintenance_events(issue_id,created_at)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}


@register(26)
def _migrate_file_operation_plans(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-10 (Part XXIV): plan and plan-step tables. Planning data only; no code path
    executes a plan yet. Steps cascade with their plan, and a step's file_id is a plain
    reference (no FK) so deleting metadata can never reach a plan or a file. Like step 25,
    this is the only copy of the DDL (not mirrored in db.py); fresh installs get the tables
    from init_db() running the pending steps."""
    existed = sum(_table_exists(conn, t) for t in ("file_operation_plans", "file_operation_steps"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_operation_plans ("
        "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','previewed','approved','executing','executed','failed','rolled_back','cancelled')), "
        "params_json TEXT NOT NULL DEFAULT '{}', summary TEXT, refusals_json TEXT NOT NULL DEFAULT '[]', "
        "created_by TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_operation_steps ("
        "id TEXT PRIMARY KEY, "
        "plan_id TEXT NOT NULL REFERENCES file_operation_plans(id) ON DELETE CASCADE, "
        "ord INTEGER NOT NULL, op TEXT NOT NULL, file_id TEXT, source_path TEXT, dest_path TEXT, "
        "expected_json TEXT NOT NULL DEFAULT '{}', effects_json TEXT NOT NULL DEFAULT '{}', "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','failed','rolled_back')))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_operation_plans_status ON file_operation_plans(status,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_operation_steps_plan ON file_operation_steps(plan_id,ord)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}


@register(27)
def _migrate_work_merge_ledger(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-24 (Part XXVI): merge header and row-level reversal ledger. Neither table has a
    foreign key to works, so a merged-away source Work can be deleted while its ledger stays.
    ``before_json`` is the full row before the merge touched it (NULL for a row the merge
    created); ``pk_json`` is the row's key after the change, which is what a reversal deletes."""
    existed = sum(_table_exists(conn, t) for t in ("work_merges", "work_merge_ledger"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_merges ("
        "id TEXT PRIMARY KEY, canonical_work_id TEXT NOT NULL, source_work_ids_json TEXT NOT NULL, "
        "resolutions_json TEXT NOT NULL DEFAULT '{}', merged_by TEXT, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_merge_ledger ("
        "id INTEGER PRIMARY KEY, "
        "merge_id TEXT NOT NULL REFERENCES work_merges(id) ON DELETE CASCADE, "
        "seq INTEGER NOT NULL, table_name TEXT NOT NULL, "
        "op TEXT NOT NULL CHECK(op IN ('update','delete','insert')), "
        "pk_json TEXT NOT NULL, before_json TEXT)"
    )
    if "seq" in _columns(conn, "work_merge_ledger"):  # a fresh install already has step 29's shape (no seq); that step owns the index
        conn.execute("CREATE INDEX IF NOT EXISTS idx_work_merge_ledger_merge ON work_merge_ledger(merge_id,seq)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}


@register(28)
def _migrate_asset_upgrade_policy(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-27 (Part XXIX): media_assets.upgrade_policy. Every existing row takes the default
    'none' (no upgrade search, ever, until a user opts in); the allowed values are checked
    in code, not by a CHECK constraint."""
    if "upgrade_policy" in _columns(conn, "media_assets"):
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE media_assets ADD COLUMN upgrade_policy TEXT NOT NULL DEFAULT 'none'")
    return {"columns_added": 1, "already_applied": 0}


WORK_MERGE_COLUMNS = ("id", "canonical_work_id", "source_work_ids_json", "status", "resolutions_json", "created_by", "created_at")
WORK_MERGE_LEDGER_COLUMNS = ("id", "merge_id", "table_name", "pk_json", "action", "before_json", "after_json")
LEGACY_LEDGER_ACTIONS = {"update": "updated", "delete": "deleted", "insert": "inserted"}


def _merge_table_ddl(table: str) -> str:
    """The fresh-install DDL of ``work_merges`` / ``work_merge_ledger`` (WORK_MERGE_TABLES_DDL, which
    db.EXTRA_SCHEMA includes), so an upgraded database gets exactly the table a fresh install gets."""
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", WORK_MERGE_TABLES_DDL, re.S)
    if not m:  # pragma: no cover - guards a schema refactor
        raise MigrationError(f"{table} DDL not found in WORK_MERGE_TABLES_DDL")
    return m.group(0).replace("IF NOT EXISTS ", "", 1)


def _surviving_canonical(conn: sqlite3.Connection, work_id: str | None, created_at: float) -> str | None:
    """A step-27 header kept pointing at a canonical Work that a later merge absorbed; follow the
    later merges to the Work that holds it now (what workmerge's 'repoint' of the new FK does)."""
    seen: set[str] = set()
    while work_id and work_id not in seen and not conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
        seen.add(work_id)
        later = conn.execute(
            "SELECT canonical_work_id, created_at FROM work_merges_pre29 m WHERE created_at>=? "
            "AND EXISTS (SELECT 1 FROM json_each(m.source_work_ids_json) WHERE value=?) ORDER BY created_at LIMIT 1",
            (created_at, work_id)).fetchone()
        work_id, created_at = (later[0], later[1]) if later else (None, created_at)
    return work_id if work_id not in seen else None


@register(29)
def _migrate_work_merge_status_ledger(conn: sqlite3.Connection) -> dict[str, int]:
    """P5-24-1 (Part XXVI): give the step-27 merge tables the shape a reversal (P5-25a) needs:
    ``work_merges`` gains ``status`` (executed/reversed/reversal_failed), ``merged_by`` becomes
    ``created_by`` and ``canonical_work_id`` a foreign key to works; ``work_merge_ledger`` swaps
    ``op``/``seq`` for ``action`` (moved/updated/deleted/inserted, replayed in reverse ``id``
    order) and gains ``after_json``. A fresh install already has both tables in this shape from
    WORK_MERGE_TABLES_DDL via db.EXTRA_SCHEMA (and so reports already_applied); the ledger index
    on merge_id lives with this step, so it is created here in every case. SQLite cannot add a
    foreign key or alter a CHECK, so old tables are renamed, recreated from the same DDL, copied and dropped
    (the child first, so no ON DELETE CASCADE fires). Legacy ledger rows have no after-image."""
    merge_cols = _columns(conn, "work_merges")
    ledger_cols = _columns(conn, "work_merge_ledger")
    if "status" in merge_cols and "action" in ledger_cols:
        index_created = int(not conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_work_merge_ledger_merge'").fetchone())
        conn.execute("CREATE INDEX IF NOT EXISTS idx_work_merge_ledger_merge ON work_merge_ledger(merge_id)")
        return {"tables_created": 0, "rows_copied": 0, "index_created": index_created, "already_applied": 1}
    for table in ("work_merge_ledger", "work_merges"):  # renaming the parent second re-targets the child's FK
        if _table_exists(conn, table):
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_pre29")
    conn.execute(_merge_table_ddl("work_merges"))
    conn.execute(_merge_table_ddl("work_merge_ledger"))
    copied = 0
    if merge_cols:
        for r in conn.execute("SELECT * FROM work_merges_pre29 ORDER BY created_at, id").fetchall():
            conn.execute(
                "INSERT INTO work_merges(id,canonical_work_id,source_work_ids_json,status,resolutions_json,created_by,created_at) "
                "VALUES (?,?,?,'executed',?,?,?)",
                (r["id"], _surviving_canonical(conn, r["canonical_work_id"], r["created_at"]), r["source_work_ids_json"],
                 r["resolutions_json"], r["merged_by"], r["created_at"]))
            copied += 1
    if ledger_cols:
        for r in conn.execute("SELECT * FROM work_merge_ledger_pre29 ORDER BY merge_id, seq, id").fetchall():
            conn.execute(
                "INSERT INTO work_merge_ledger(merge_id,table_name,pk_json,action,before_json,after_json) VALUES (?,?,?,?,?,NULL)",
                (r["merge_id"], r["table_name"], r["pk_json"], LEGACY_LEDGER_ACTIONS[r["op"]], r["before_json"]))
            copied += 1
        conn.execute("DROP TABLE work_merge_ledger_pre29")
    if merge_cols:
        conn.execute("DROP TABLE work_merges_pre29")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_merge_ledger_merge ON work_merge_ledger(merge_id)")
    return {"tables_created": 2, "rows_copied": copied, "index_created": 1, "already_applied": 0}


@register(30)
def _migrate_kids_classification_detail(conn: sqlite3.Connection) -> dict[str, int]:
    """Kids classifier evidence: kids_classification.detail_json holds what the model cited
    (audience_evidence, adult_signals), the model name, a fingerprint of the evidence it
    saw and the gate outcome. Nullable: rows written before this keep NULL, which the
    re-check reads as "verdict only, no cited evidence"."""
    if "detail_json" in _columns(conn, "kids_classification"):
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE kids_classification ADD COLUMN detail_json TEXT")
    return {"columns_added": 1, "already_applied": 0}


@register(31)
def _migrate_work_audience_signals(conn: sqlite3.Connection) -> dict[str, int]:
    """Kids classifier audience metadata: the normalized, queryable form of what providers
    say about a book's audience (categories, subjects, maturity rating, age range, ...).
    ``work_audience_signals`` holds one row per provider value (the raw text, a ``norm``
    class and when it was fetched); the full evidence trail stays in metadata_observations.
    ``work_audience_status`` is the backfill's cursor: one row per work it has asked about,
    so a run resumes where the last stopped and never repeats a finished work. Additive and
    idempotent; no existing table is touched."""
    existed = sum(_table_exists(conn, t) for t in ("work_audience_signals", "work_audience_status"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_audience_signals ("
        "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
        "provider TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, "
        "norm TEXT NOT NULL DEFAULT 'neutral', fetched_at REAL NOT NULL, "
        "PRIMARY KEY(work_id,provider,kind,value))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_audience_status ("
        "work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE, "
        "checked_at REAL NOT NULL, complete INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 1, "
        "next_try_at REAL, providers_json TEXT NOT NULL DEFAULT '{}', signal_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_audience_signals_kind ON work_audience_signals(kind,norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_audience_status_pending ON work_audience_status(complete,next_try_at)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}


@register(32)
def _migrate_wanted_search_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Wanted-search fairness: ``wanted_search_state`` holds one row per asset the automatic
    wanted search has looked at (when, how many consecutive misses, when it may be tried
    again, the last outcome), so the job can order by "least recently searched" and honour a
    cooldown. media_assets is untouched (searching never bumps its updated_at, which is what
    starved the old updated_at ordering). Also indexes release_searches for the rolling daily
    cap count. Additive and idempotent; no existing row changes."""
    existed = int(_table_exists(conn, "wanted_search_state"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wanted_search_state ("
        "asset_id TEXT PRIMARY KEY REFERENCES media_assets(id) ON DELETE CASCADE, "
        "last_auto_search_at REAL NOT NULL, misses INTEGER NOT NULL DEFAULT 0, "
        "next_eligible_at REAL, last_outcome TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wanted_search_state_last ON wanted_search_state(last_auto_search_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_release_searches_auto ON release_searches(automatic,started_at)")
    return {"tables_created": 1 - existed, "already_applied": existed}


DOWNLOAD_STAT_COLUMNS = {"size_bytes": "INTEGER", "downloaded_bytes": "INTEGER", "speed_bps": "INTEGER", "eta_seconds": "INTEGER", "grabbed_by": "TEXT"}


@register(33)
def _migrate_downloads_client_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Queue page: remember what the download client last reported (total and downloaded
    bytes, speed, ETA) and who clicked Grab, so the queue can show them between polls.
    Nullable columns on ``downloads``; existing rows stay NULL until the next poll.
    Additive and idempotent."""
    cols = _columns(conn, "downloads")
    added = 0
    for name, ddl in DOWNLOAD_STAT_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {name} {ddl}")
            added += 1
    return {"columns_added": added, "already_applied": int(added == 0)}



CONVERSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS conversions ("
    "id TEXT PRIMARY KEY, "
    "kind TEXT NOT NULL CHECK(kind IN ('audio','ebook')), "
    "work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE, "
    "source_asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE, "
    "target_format TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'queued', "
    "percent REAL NOT NULL DEFAULT 0, "
    "attempts INTEGER NOT NULL DEFAULT 0, "
    "next_attempt_at REAL, "
    "error TEXT, "
    "source_signature TEXT, "
    "output_asset_id TEXT REFERENCES media_assets(id) ON DELETE SET NULL, "
    "output_path TEXT, "
    "tmp_path TEXT, "
    "params_json TEXT NOT NULL DEFAULT '{}', "
    "result_json TEXT NOT NULL DEFAULT '{}', "
    "estimated_bytes INTEGER, "
    "cancel_requested INTEGER NOT NULL DEFAULT 0, "
    "trigger_source TEXT NOT NULL DEFAULT 'manual', "
    "requested_by TEXT, "
    "created_at REAL NOT NULL, "
    "started_at REAL, "
    "heartbeat_at REAL, "
    "finished_at REAL)"
)


@register(34)
def _migrate_media_conversions(conn: sqlite3.Connection) -> dict[str, int]:
    """Format conversion (issue #265): a derived file is its own ``media_assets`` row pointing at
    the asset it was made from (``derived_from_asset_id``, plus ``derived_by`` = tool and settings
    and ``derived_at``), so the original asset and its files are never touched and the derivative
    is never confused with a second owned copy. ``conversions`` is the queue and history of the
    background conversion jobs. One active (queued/running) row per source asset and target
    format is enforced by a partial unique index, which is what makes enqueueing idempotent.
    Additive and idempotent; no existing row changes (every existing asset is not derived)."""
    cols = _columns(conn, "media_assets")
    added = 0
    # The self-reference cascades: a derivative has no meaning without its source row, and
    # SET NULL would silently turn it into an ordinary owned asset.
    if "derived_from_asset_id" not in cols:
        conn.execute("ALTER TABLE media_assets ADD COLUMN derived_from_asset_id TEXT REFERENCES media_assets(id) ON DELETE CASCADE")
        added += 1
    if "derived_by" not in cols:
        conn.execute("ALTER TABLE media_assets ADD COLUMN derived_by TEXT")
        added += 1
    if "derived_at" not in cols:
        conn.execute("ALTER TABLE media_assets ADD COLUMN derived_at REAL")
        added += 1
    existed = int(_table_exists(conn, "conversions"))
    conn.execute(CONVERSIONS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_derived ON media_assets(derived_from_asset_id) WHERE derived_from_asset_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversions_queue ON conversions(kind,status,next_attempt_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversions_work ON conversions(work_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversions_active ON conversions(source_asset_id,target_format) "
        "WHERE status IN ('queued','running')"
    )
    return {"columns_added": added, "tables_created": 1 - existed, "already_applied": int(added == 0 and existed == 1)}


TRUSTED_DEVICE_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'paired'",   # 'paired' (reader, Bearer) or 'browser' (trusted browser, cookie)
    "ua_summary": "TEXT",                       # "Safari on iOS"; the raw User-Agent is never stored
    "first_seen_at": "REAL",
    "last_ip_prefix": "TEXT",                   # /24 (IPv4) or /48 (IPv6) of the last request, never the full address
    "rotated_at": "REAL",                       # last cookie rotation; the 30 day clock starts here
    "prev_token_hash": "TEXT",                  # the token a rotation replaced, honoured for a short grace window
    "prev_valid_until": "REAL",
    "prev_rerotated": "INTEGER NOT NULL DEFAULT 0",  # the last rotation was itself triggered by a replaced token (allowed once)
    "reuse_detected_at": "REAL",                # a replaced token came back after its grace window: the device was revoked
}


@register(35)
def _migrate_trusted_devices(conn: sqlite3.Connection) -> dict[str, int]:
    """Trusted browsers ("Trust this device"): a device row of kind 'browser' whose token
    lives in an HttpOnly cookie. Reuses the devices table (only the token hash is stored)
    and adds what a browser needs: a user-agent summary, first/last seen, the last IP
    prefix, and the previous token hash so a rotation survives a request race. Additive
    and idempotent; existing rows become kind 'paired'."""
    cols = _columns(conn, "devices")
    added = 0
    for name, ddl in TRUSTED_DEVICE_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")
            added += 1
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_prev_token ON devices(prev_token_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_kind_seen ON devices(kind,revoked_at,last_seen_at)")
    return {"columns_added": added, "already_applied": int(added == 0)}


@register(36)
def _migrate_ebook_profile_pdf_rule(conn: sqlite3.Connection) -> dict[str, int]:
    """Existing ebook-default profiles gain pdf_undesirable_if_epub (ensure_default_profiles only
    sets it on profiles it creates). A profile that already carries the key keeps its value."""
    row = conn.execute("SELECT config_json FROM profiles WHERE id='ebook-default'").fetchone()
    cfg = json.loads(row["config_json"] or "{}") if row else {}
    if row is None or "pdf_undesirable_if_epub" in cfg:
        return {"profiles_updated": 0}
    cfg["pdf_undesirable_if_epub"] = True
    conn.execute("UPDATE profiles SET config_json=? WHERE id='ebook-default'", (json.dumps(cfg),))
    return {"profiles_updated": 1}


@register(37)
def _migrate_series_cleanup_journal(conn: sqlite3.Connection) -> dict[str, int]:
    """Series cleanup (services/series_cleanup.py): a run table plus a row-level change
    journal holding the BEFORE state of every series, membership, assertion and monitor
    row a cleanup touched, so "Undo last cleanup" can put them back. Additive: new
    tables only, no existing row is read or changed."""
    existed = sum(_table_exists(conn, t) for t in ("series_cleanup_runs", "series_cleanup_changes"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS series_cleanup_runs ("
        "id TEXT PRIMARY KEY, created_at REAL NOT NULL, created_by TEXT, "
        "status TEXT NOT NULL DEFAULT 'applied' CHECK(status IN ('applied','undone')), "
        "undone_at REAL, summary_json TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS series_cleanup_changes ("
        "seq INTEGER PRIMARY KEY, "
        "run_id TEXT NOT NULL REFERENCES series_cleanup_runs(id) ON DELETE CASCADE, "
        "table_name TEXT NOT NULL, op TEXT NOT NULL CHECK(op IN ('insert','update','delete')), "
        "row_key TEXT NOT NULL, before_json TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_series_cleanup_changes_run ON series_cleanup_changes(run_id,seq)")
    return {"tables_created": 2 - existed, "already_applied": int(existed == 2)}
FILE_TRASH_DDL = (
    "CREATE TABLE IF NOT EXISTS file_trash ("
    "id TEXT PRIMARY KEY, "
    "batch_id TEXT NOT NULL, "
    "root_id TEXT, "                          # no foreign key: the log must outlive a removed root
    "root_path TEXT NOT NULL, "
    "work_id TEXT, "
    "asset_id TEXT, "
    "asset_kind TEXT, "
    "asset_format TEXT, "
    "original_path TEXT NOT NULL, "
    "relative_path TEXT NOT NULL, "
    "trash_path TEXT, "                       # NULL for a record-only removal (the file stays where it is)
    "size_bytes INTEGER NOT NULL DEFAULT 0, "
    "sha256 TEXT, "
    "record_only INTEGER NOT NULL DEFAULT 0, "
    "reason TEXT, "
    "keeper_file_id TEXT, "
    "file_json TEXT NOT NULL, "                # the whole media_files row, so Restore can put it back
    "status TEXT NOT NULL DEFAULT 'moving' CHECK(status IN ('moving','trashed','restored','purged','reverted','failed','lost')), "
    "trashed_by TEXT, "
    "trashed_at REAL NOT NULL, "
    "expires_at REAL NOT NULL, "
    "restored_at REAL, "
    "purged_at REAL)"
)


@register(38)
def _migrate_file_trash(conn: sqlite3.Connection) -> dict[str, int]:
    """Redundant-file cleanup after a merge: ``file_trash`` is the log of every file record removed through
    services/filetrash.py. A managed file is moved to ``<root>/.bookdoe-trash/<date>/<relative path>``
    (trash_path) and kept until expires_at (retention setting, default 30 days); an adopted/observe file stays
    where it is (record_only). file_json holds the removed media_files row for Restore. Additive, idempotent."""
    existed = int(_table_exists(conn, "file_trash"))
    # Redundant-file analysis hashes files from the disk; the result goes in these columns, never over the scanner's
    # ``sha256``, so a silent change or bit rot stays visible as a disagreement between the two.
    cols = _columns(conn, "media_files")
    added = 0
    for name, ddl in (("checksum_verified", "TEXT"), ("checksum_verified_at", "REAL")):
        if name not in cols:
            conn.execute(f"ALTER TABLE media_files ADD COLUMN {name} {ddl}")
            added += 1
    conn.execute(FILE_TRASH_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_trash_status ON file_trash(status,expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_trash_work ON file_trash(work_id)")
    return {"tables_created": 1 - existed, "columns_added": added, "already_applied": int(existed and not added)}


# Step 40: library-wide duplicate finder + bulk merge (2026-10-14). No foreign keys on purpose: these tables name
# Works by id, and a foreign key to works would make workmerge.table_inventory() report them as unhandled tables.
DUPLICATE_FINDER_DDL = (
    "CREATE TABLE IF NOT EXISTS duplicate_groups ("
    "id TEXT PRIMARY KEY, members_key TEXT NOT NULL UNIQUE, work_ids_json TEXT NOT NULL, kind TEXT NOT NULL, "
    "confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')), score REAL NOT NULL DEFAULT 0, "
    "reasons_json TEXT NOT NULL DEFAULT '[]', canonical_work_id TEXT, canonical_reason TEXT, title TEXT, authors TEXT, "
    "has_owned INTEGER NOT NULL DEFAULT 0, series_json TEXT NOT NULL DEFAULT '[]', block_key TEXT, signature TEXT, "
    "dismissed INTEGER NOT NULL DEFAULT 0, dismissed_at REAL, dismissed_by TEXT, dismissed_signature TEXT, "
    "status TEXT NOT NULL DEFAULT 'open', first_seen_at REAL NOT NULL, updated_at REAL NOT NULL, scan_id TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_groups_list ON duplicate_groups(status,dismissed,confidence)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_groups_block ON duplicate_groups(block_key)",
    "CREATE TABLE IF NOT EXISTS duplicate_scan_state ("
    "id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'idle', scan_id TEXT, mode TEXT, started_at REAL, finished_at REAL, "
    "last_complete_at REAL, since REAL, cursor TEXT, blocks_total INTEGER NOT NULL DEFAULT 0, blocks_done INTEGER NOT NULL DEFAULT 0, "
    "works_total INTEGER NOT NULL DEFAULT 0, groups_found INTEGER NOT NULL DEFAULT 0, error TEXT, counts_json TEXT NOT NULL DEFAULT '{}')",
    # Recorded at the moment metadata.link_work refuses because another Work already holds the provider record.
    "CREATE TABLE IF NOT EXISTS work_refresh_conflicts ("
    "work_id TEXT PRIMARY KEY, conflict_work_id TEXT NOT NULL, metadata_id TEXT, detected_at REAL NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 1, resolved_at REAL)",
    "CREATE TABLE IF NOT EXISTS merge_batches ("
    "id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', options_json TEXT NOT NULL DEFAULT '{}', created_by TEXT, "
    "created_at REAL NOT NULL, finished_at REAL, total INTEGER NOT NULL DEFAULT 0, updated_at REAL)",
    "CREATE TABLE IF NOT EXISTS merge_batch_items ("
    "id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, ord INTEGER NOT NULL, group_id TEXT, canonical_work_id TEXT NOT NULL, "
    "source_work_ids_json TEXT NOT NULL, title TEXT, status TEXT NOT NULL DEFAULT 'pending', reason TEXT, detail_json TEXT NOT NULL DEFAULT '{}', "
    "merge_id TEXT, finished_at REAL, updated_at REAL)",
    "CREATE INDEX IF NOT EXISTS idx_merge_batch_items_batch ON merge_batch_items(batch_id,ord)",
    "CREATE TABLE IF NOT EXISTS duplicate_refresh_queue ("
    "work_id TEXT PRIMARY KEY, batch_id TEXT, queued_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
    "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, done_at REAL, not_before REAL)",
)
DUPLICATE_FINDER_TABLES = ("duplicate_groups", "duplicate_scan_state", "work_refresh_conflicts", "merge_batches", "merge_batch_items", "duplicate_refresh_queue")


@register(40)
def _migrate_duplicate_finder(conn: sqlite3.Connection) -> dict[str, int]:
    """Library-wide duplicate finder and bulk merge: candidate groups (with a remembered ``dismissed`` flag), the scan's
    resumable state, refresh conflicts harvested from metadata.link_work, merge batches (one id undoes a whole bulk run) and
    the post-merge metadata refresh queue. Additive and idempotent."""
    existed = sum(int(_table_exists(conn, t)) for t in DUPLICATE_FINDER_TABLES)
    for ddl in DUPLICATE_FINDER_DDL:
        conn.execute(ddl)
    # Tables created by the first build of this step (commit f5dfcd9, recorded as step 39) lack the worker heartbeat columns.
    added = 0
    for table in ("merge_batches", "merge_batch_items"):
        if "updated_at" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN updated_at REAL")
            added += 1
    return {"tables_created": len(DUPLICATE_FINDER_TABLES) - existed, "columns_added": added,
            "already_applied": int(existed == len(DUPLICATE_FINDER_TABLES) and not added)}
DISCOVER_CHAT_DDL = (
    "CREATE TABLE IF NOT EXISTS discover_conversations ("
    "id TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
    "title TEXT NOT NULL DEFAULT '', "
    "turn_count INTEGER NOT NULL DEFAULT 0, "
    "dropped_count INTEGER NOT NULL DEFAULT 0, "
    "blocked_count INTEGER NOT NULL DEFAULT 0, "
    "owner_deleted_at REAL, "
    "account_type TEXT NOT NULL DEFAULT '', "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_discover_conv_user ON discover_conversations(user_id,updated_at)",
    "CREATE TABLE IF NOT EXISTS discover_messages ("
    "id TEXT PRIMARY KEY, "
    "conversation_id TEXT NOT NULL REFERENCES discover_conversations(id) ON DELETE CASCADE, "
    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
    "role TEXT NOT NULL CHECK(role IN ('user','assistant')), "
    "status TEXT NOT NULL DEFAULT 'done' CHECK(status IN ('pending','done','failed','cancelled')), "
    "content TEXT NOT NULL DEFAULT '', "
    "result_json TEXT, error TEXT, "
    "dropped INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, "
    "deadline REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_discover_msg_conv ON discover_messages(conversation_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_discover_msg_user ON discover_messages(user_id,role,created_at)",
    "CREATE TABLE IF NOT EXISTS discover_profiles ("
    "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
    "profile_json TEXT NOT NULL DEFAULT '{}', account_type TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)",
    # Append-only usage ledger for the burst limit and the daily cap. It is NOT derived from
    # discover_messages, so deleting a chat (or the 100-chat eviction) cannot reset a counter.
    "CREATE TABLE IF NOT EXISTS discover_usage ("
    "id INTEGER PRIMARY KEY, "
    "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
    "at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_discover_usage_user ON discover_usage(user_id,at)",
)


@register(39)
def _migrate_discover_chat(conn: sqlite3.Connection) -> dict[str, int]:
    """Discover chat (docs/PLAYER_MANAGEMENT.md, "Discover chat"): per-account conversations, their
    messages (the assistant message is created `pending` and filled in by a background thread, so the
    request that posts a message returns at once) and the reading-taste profile built from them.
    Deleting a user or a conversation cascades. Additive only; a rollback leaves the tables unused."""
    tables = ("discover_conversations", "discover_messages", "discover_profiles", "discover_usage")
    existed = sum(_table_exists(conn, t) for t in tables)
    for ddl in DISCOVER_CHAT_DDL:
        conn.execute(ddl)
    return {"tables_created": len(tables) - existed, "already_applied": int(existed == len(tables))}
# Managed library build v2 (#219, #222-#224; services/managedbuild.py), migration step 41 (39 and 40 are taken by the
# discover-chat and bulk-duplicates branches; created_by_build is part of this same single step: no deployed database has
# run an earlier version of it). No foreign keys on purpose: the journal must outlive
# a removed asset, file or root, and a work merge must not have to know about these tables (it moves media_files rows, whose
# ids stay stable; the journal follows them by id).
# -- build_plan_items: one row per planned file (copy) or per book (convert), written by the read-only dry run; the run then
#    moves each row through pending -> copying -> verifying -> publishing -> published -> done (or failed/skipped/cancelled).
#    src_* is the source signature taken at planning time; temp_path/temp_ident/final_path are the crash journal.
# -- build_runs: the single execution of a confirmed plan (one per plan), with pause/cancel requests and progress.
# -- managed_copies: the record model. After a verified publish the ACTIVE media_files row (same id: progress, bookmarks and
#    every listing keep working) is re-pointed at the managed copy; the original file stays where it is and is kept here as a
#    preserved source (source_path + the whole original row in source_row_json for Undo). A convert row points at the derived
#    asset bookdoe made in the managed root.
MANAGED_BUILD_DDL = (
    "CREATE TABLE IF NOT EXISTS build_plan_items ("
    "id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, unit_key TEXT NOT NULL, part_index INTEGER, "
    "work_id TEXT, asset_id TEXT, file_id TEXT, file_ids_json TEXT, title TEXT, kind TEXT, "
    "source_root_id TEXT, source_path TEXT, src_size INTEGER, src_mtime_ns INTEGER, src_ino INTEGER, src_dev INTEGER, sources_json TEXT, "
    "action TEXT NOT NULL CHECK(action IN ('copy','convert','copy_as_is','skip')), format TEXT, target_format TEXT, "
    "dest_root_id TEXT, dest_path TEXT, size_bytes INTEGER NOT NULL DEFAULT 0, est_bytes INTEGER NOT NULL DEFAULT 0, "
    "skip_code TEXT, skip_reason TEXT, collision TEXT, "
    "state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','copying','verifying','publishing','published','done','failed','skipped','cancelled')), "
    "attempts INTEGER NOT NULL DEFAULT 0, error TEXT, next_attempt_at REAL, claimed_by TEXT, temp_path TEXT, temp_ident TEXT, "
    "final_path TEXT, sha256 TEXT, reused INTEGER NOT NULL DEFAULT 0, percent REAL, conversion_id TEXT, updated_at REAL)",
    "CREATE INDEX IF NOT EXISTS idx_build_items_plan_state ON build_plan_items(plan_id,state)",
    "CREATE INDEX IF NOT EXISTS idx_build_items_plan_action ON build_plan_items(plan_id,action)",
    "CREATE INDEX IF NOT EXISTS idx_build_items_unit ON build_plan_items(plan_id,unit_key)",
    "CREATE INDEX IF NOT EXISTS idx_build_items_work ON build_plan_items(work_id)",
    "CREATE INDEX IF NOT EXISTS idx_build_items_final ON build_plan_items(final_path)",
    "CREATE TABLE IF NOT EXISTS build_runs ("
    "id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE, "
    "status TEXT NOT NULL CHECK(status IN ('running','paused','interrupted','stopped','cancelled','completed')), "
    "options_json TEXT NOT NULL DEFAULT '{}', started_by TEXT, started_at REAL NOT NULL, finished_at REAL, heartbeat_at REAL, "
    "pause_requested INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0, "
    "progress_json TEXT NOT NULL DEFAULT '{}', stop_reason TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_build_runs_status ON build_runs(status)",
    "CREATE TABLE IF NOT EXISTS managed_copies ("
    "id TEXT PRIMARY KEY, run_id TEXT, item_id INTEGER, kind TEXT NOT NULL CHECK(kind IN ('copy','convert')), "
    "work_id TEXT, asset_id TEXT, file_id TEXT, source_asset_id TEXT, "
    "source_path TEXT NOT NULL, source_root_id TEXT, source_size INTEGER, source_sig TEXT, source_row_json TEXT NOT NULL DEFAULT '{}', "
    "asset_root_before TEXT, managed_path TEXT NOT NULL, managed_root_id TEXT, managed_ident TEXT, sha256 TEXT, "
    "size_bytes INTEGER NOT NULL DEFAULT 0, verify_mode TEXT, conversion_id TEXT, "
    "status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','undone')), "
    "undo_state TEXT CHECK(undo_state IN ('undoing','moving','done','failed')), trash_path TEXT, undo_error TEXT, "
    "created_at REAL NOT NULL, undone_at REAL, undone_by TEXT, created_by_build INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_managed_copies_source ON managed_copies(source_path)",
    "CREATE INDEX IF NOT EXISTS idx_managed_copies_managed ON managed_copies(managed_path)",
    "CREATE INDEX IF NOT EXISTS idx_managed_copies_file ON managed_copies(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_managed_copies_asset ON managed_copies(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_managed_copies_run ON managed_copies(run_id)",
)
BUILD_PLAN_V2_COLUMNS = (
    ("version", "INTEGER NOT NULL DEFAULT 1"), ("status", "TEXT"), ("options_json", "TEXT"), ("summary_json", "TEXT"),
    ("book_count", "INTEGER"), ("item_count", "INTEGER"), ("expires_at", "REAL"), ("created_by", "TEXT"),
    ("confirmed_by", "TEXT"),
)


@register(41)
def _migrate_managed_build(conn: sqlite3.Connection) -> dict[str, int]:
    """Managed library build v2: plan items in a table instead of one JSON blob, the run journal and the managed-copy
    record model (see MANAGED_BUILD_DDL). Additive and idempotent; old v1 plans keep their plan_json (version 1)."""
    existed = int(_table_exists(conn, "managed_copies"))
    have = _columns(conn, "build_plans")
    added = 0
    for name, ddl in BUILD_PLAN_V2_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE build_plans ADD COLUMN {name} {ddl}")
            added += 1
    for stmt in MANAGED_BUILD_DDL:
        conn.execute(stmt)
    # created_by_build: 1 when the build created the (converted) asset, so Undo never deletes an asset that existed before.
    if "created_by_build" not in _columns(conn, "managed_copies"):
        conn.execute("ALTER TABLE managed_copies ADD COLUMN created_by_build INTEGER NOT NULL DEFAULT 0")
        added += 1
    return {"tables_created": 3 * (1 - existed), "columns_added": added, "already_applied": int(existed and not added)}


# ---------------------------------------------------------------------------
# Validations (run inside the transaction right after their step)
# ---------------------------------------------------------------------------


@register_validation(41)
def _validate_managed_build(conn: sqlite3.Connection) -> None:
    for table in ("build_plan_items", "build_runs", "managed_copies"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    missing = {n for n, _ in BUILD_PLAN_V2_COLUMNS} - _columns(conn, "build_plans")
    if missing:
        _fail(f"build_plans is missing columns {sorted(missing)}")
    if not {"source_path", "managed_path", "source_row_json", "file_id", "undo_state", "created_by_build"} <= _columns(conn, "managed_copies"):
        _fail("managed_copies is missing columns")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(managed_copies)").fetchall()}
    if "idx_managed_copies_source" not in idx:
        _fail("index idx_managed_copies_source missing")


@register_validation(38)
def _validate_file_trash(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "file_trash"):
        _fail("file_trash missing")
    missing = {"file_json", "trash_path", "expires_at", "record_only", "status"} - _columns(conn, "file_trash")
    if missing:
        _fail(f"file_trash is missing columns {sorted(missing)}")
    if not {"checksum_verified", "checksum_verified_at"} <= _columns(conn, "media_files"):
        _fail("media_files is missing the checksum_verified columns")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(file_trash)").fetchall()}
    if "idx_file_trash_status" not in idx:
        _fail("index idx_file_trash_status missing")


@register_validation(40)
def _validate_duplicate_finder(conn: sqlite3.Connection) -> None:
    for t in DUPLICATE_FINDER_TABLES:
        if not _table_exists(conn, t):
            _fail(f"{t} missing")
    if "dismissed" not in _columns(conn, "duplicate_groups"):
        _fail("duplicate_groups.dismissed missing")


@register_validation(31)
def _validate_work_audience_signals(conn: sqlite3.Connection) -> None:
    for table in ("work_audience_signals", "work_audience_status"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    missing = {"provider", "kind", "value", "norm", "fetched_at"} - _columns(conn, "work_audience_signals")
    if missing:
        _fail(f"work_audience_signals is missing columns {sorted(missing)}")


@register_validation(32)
def _validate_wanted_search_state(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "wanted_search_state"):
        _fail("wanted_search_state missing")
    missing = {"asset_id", "last_auto_search_at", "misses", "next_eligible_at", "last_outcome"} - _columns(conn, "wanted_search_state")
    if missing:
        _fail(f"wanted_search_state is missing columns {sorted(missing)}")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(release_searches)").fetchall()}
    if "idx_release_searches_auto" not in idx:
        _fail("index idx_release_searches_auto missing")


@register_validation(33)
def _validate_downloads_client_stats(conn: sqlite3.Connection) -> None:
    missing = set(DOWNLOAD_STAT_COLUMNS) - _columns(conn, "downloads")
    if missing:
        _fail(f"downloads is missing columns {sorted(missing)}")


@register_validation(34)
def _validate_media_conversions(conn: sqlite3.Connection) -> None:
    missing = {"derived_from_asset_id", "derived_by", "derived_at"} - _columns(conn, "media_assets")
    if missing:
        _fail(f"media_assets is missing columns {sorted(missing)}")
    if not _table_exists(conn, "conversions"):
        _fail("conversions missing")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(conversions)").fetchall()}
    if "idx_conversions_active" not in idx:
        _fail("index idx_conversions_active missing")
    n = conn.execute("SELECT COUNT(*) FROM media_assets WHERE derived_from_asset_id IS NOT NULL AND derived_from_asset_id NOT IN (SELECT id FROM media_assets)").fetchone()[0]
    if n:
        _fail(f"{n} derived assets point at a missing source asset")


@register_validation(35)
def _validate_trusted_devices(conn: sqlite3.Connection) -> None:
    missing = set(TRUSTED_DEVICE_COLUMNS) - _columns(conn, "devices")
    if missing:
        _fail(f"devices is missing columns {sorted(missing)}")


@register_validation(25)
def _validate_maintenance_issues(conn: sqlite3.Connection) -> None:
    for table in MAINTENANCE_TABLES:
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    missing = {"fingerprint", "status", "bulk_safe", "repair_plan_id"} - _columns(conn, "maintenance_issues")
    if missing:
        _fail(f"maintenance_issues is missing columns {sorted(missing)}")


@register_validation(24)
def _validate_media_file_integrity(conn: sqlite3.Connection) -> None:
    missing = set(MEDIA_FILE_INTEGRITY_COLUMNS) - _columns(conn, "media_files")
    if missing:
        _fail(f"media_files is missing columns {sorted(missing)}")


@register_validation(23)
def _validate_media_file_stream_columns(conn: sqlite3.Connection) -> None:
    missing = set(MEDIA_FILE_STREAM_COLUMNS) - _columns(conn, "media_files")
    if missing:
        _fail(f"media_files is missing columns {sorted(missing)}")


@register_validation(28)
def _validate_asset_upgrade_policy(conn: sqlite3.Connection) -> None:
    if "upgrade_policy" not in _columns(conn, "media_assets"):
        _fail("media_assets.upgrade_policy missing")
    n = conn.execute("SELECT COUNT(*) FROM media_assets WHERE upgrade_policy!='none'").fetchone()[0]
    if n:
        _fail(f"{n} assets have an upgrade policy before anyone opted in")


@register_validation(30)
def _validate_kids_classification_detail(conn: sqlite3.Connection) -> None:
    if "detail_json" not in _columns(conn, "kids_classification"):
        _fail("kids_classification.detail_json missing")


@register_validation(27)
def _validate_work_merge_ledger(conn: sqlite3.Connection) -> None:
    for table in ("work_merges", "work_merge_ledger"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    if "before_json" not in _columns(conn, "work_merge_ledger"):
        _fail("work_merge_ledger.before_json missing")


@register_validation(29)
def _validate_work_merge_status_ledger(conn: sqlite3.Connection) -> None:
    for table, want in (("work_merges", WORK_MERGE_COLUMNS), ("work_merge_ledger", WORK_MERGE_LEDGER_COLUMNS)):
        got = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if got != list(want):
            _fail(f"{table} columns are {got}, expected {list(want)}")
    for leftover in ("work_merges_pre29", "work_merge_ledger_pre29"):
        if _table_exists(conn, leftover):
            _fail(f"{leftover} was not dropped")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_work_merge_ledger_merge'").fetchone():
        _fail("idx_work_merge_ledger_merge missing")


@register_validation(26)
def _validate_file_operation_plans(conn: sqlite3.Connection) -> None:
    for table in ("file_operation_plans", "file_operation_steps"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    if "expected_json" not in _columns(conn, "file_operation_steps"):
        _fail("file_operation_steps.expected_json missing")
    if "refusals_json" not in _columns(conn, "file_operation_plans"):
        _fail("file_operation_plans.refusals_json missing")


@register_validation(22)
def _validate_kids_decisions_auto(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='kids_decisions'").fetchone()
    if row is None or "'auto_approved'" not in row["sql"]:
        _fail("kids_decisions CHECK does not allow 'auto_approved'")
    if _table_exists(conn, "kids_decisions_new"):
        _fail("kids_decisions_new was not dropped")


@register_validation(20)
def _validate_kids_decisions(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "kids_decisions"):
        _fail("kids_decisions missing")
    n = conn.execute(
        "SELECT COUNT(*) FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE t.name='kids' "
        "AND NOT EXISTS (SELECT 1 FROM kids_decisions d WHERE d.work_id=wt.work_id)").fetchone()[0]
    if n:
        _fail(f"{n} kids-tagged works have no decision row")


@register_validation(21)
def _validate_scan_candidates_evidence_json(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "scan_candidates"):
        _fail("scan_candidates missing")
    if "evidence_json" not in _columns(conn, "scan_candidates"):
        _fail("scan_candidates.evidence_json missing")


@register_validation(19)
def _validate_devices(conn: sqlite3.Connection) -> None:
    for table in ("devices", "device_codes"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    if "token_hash" not in _columns(conn, "devices"):
        _fail("devices.token_hash missing")


@register_validation(18)
def _validate_family_controls(conn: sqlite3.Connection) -> None:
    for table in FAMILY_TABLES:
        if not _table_exists(conn, table):
            _fail(f"{table} missing")


@register_validation(17)
def _validate_player_personal_state(conn: sqlite3.Connection) -> None:
    missing = set(PROGRESS_SYNC_COLUMNS) - _columns(conn, "media_progress")
    if missing:
        _fail(f"media_progress is missing columns {sorted(missing)}")
    for table in ("bookmarks", "favorites", "user_preferences"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")


def _fail(msg: str) -> None:
    raise MigrationError(f"validation failed: {msg}")


@register_validation(1)
def _validate_metadata_source(conn: sqlite3.Connection) -> None:
    n = conn.execute(
        "SELECT COUNT(*) FROM works WHERE metadata_source IS NULL AND (metadata_id IS NOT NULL OR id IN "
        "(SELECT work_id FROM local_meta WHERE applied_json<>'{}'))"
    ).fetchone()[0]
    if n:
        _fail(f"{n} works with metadata still have NULL metadata_source")


@register_validation(2)
def _validate_series_norm_key(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM series WHERE norm_key IS NULL").fetchone()[0]
    if n:
        _fail(f"{n} series still have NULL norm_key")


@register_validation(3)
def _validate_acquisition_intent(conn: sqlite3.Connection) -> None:
    n = conn.execute(
        "SELECT COUNT(*) FROM media_assets WHERE has_acquisition_intent IS NULL "
        "OR (status='missing' AND has_acquisition_intent=0)"
    ).fetchone()[0]
    if n:
        _fail(f"{n} assets have NULL intent or are 'missing' without intent")


@register_validation(4)
def _validate_narrators(conn: sqlite3.Connection) -> None:
    for r in conn.execute(
        "SELECT a.work_id, a.narrator_json FROM media_assets a WHERE a.kind='audiobook' "
        "AND a.narrator_json IS NOT NULL AND a.narrator_json NOT IN ('', '[]')"
    ).fetchall():
        if _narrator_names(r["narrator_json"]) and not conn.execute(
            "SELECT 1 FROM work_contributors WHERE work_id=? AND role='narrator'", (r["work_id"],)
        ).fetchone():
            _fail(f"work {r['work_id']} has narrator_json but no narrator contributor row")


@register_validation(5)
def _validate_preferred_columns(conn: sqlite3.Connection) -> None:
    missing = set(PREFERRED_EDITION_COLUMNS) - _columns(conn, "works")
    if missing:
        _fail(f"works is missing columns {sorted(missing)}")


@register_validation(6)
def _validate_search_candidates(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='search_candidates'").fetchone()
    if row is None or "'dismissed'" not in row["sql"]:
        _fail("search_candidates CHECK does not allow 'dismissed'")
    if _table_exists(conn, "search_candidates_pre_dismissed"):
        _fail("search_candidates_pre_dismissed was not dropped")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(search_candidates)").fetchall()}
    for name in ("idx_search_candidates_query", "idx_search_candidates_status"):
        if name not in idx:
            _fail(f"index {name} missing")
    if conn.execute("PRAGMA foreign_key_check(search_candidates)").fetchall():
        _fail("foreign key violations in search_candidates")


@register_validation(7)
def _validate_provider_retry_state(conn: sqlite3.Connection) -> None:
    missing = {"provider", "entity_type", "entity_id", "status", "attempts", "next_retry_at"} - _columns(conn, "provider_retry_state")
    if missing:
        _fail(f"provider_retry_state is missing columns {sorted(missing)}")


@register_validation(8)
def _validate_job_run_columns(conn: sqlite3.Connection) -> None:
    missing = set(JOB_RUN_COLUMNS) - _columns(conn, "job_runs")
    if missing:
        _fail(f"job_runs is missing columns {sorted(missing)}")


@register_validation(9)
def _validate_metadata_observations(conn: sqlite3.Connection) -> None:
    for table in ("metadata_observations", "metadata_field_locks"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(metadata_observations)").fetchall()}
    for name in ("idx_metadata_observations_field", "idx_metadata_observations_current"):
        if name not in idx:
            _fail(f"index {name} missing")


@register_validation(12)
def _validate_series_kind_policies(conn: sqlite3.Connection) -> None:
    missing = {"audiobook_policy", "ebook_policy"} - _columns(conn, "series_monitors")
    if missing:
        _fail(f"series_monitors is missing columns {sorted(missing)}")


@register_validation(13)
def _validate_series_backfill(conn: sqlite3.Connection) -> None:
    n = conn.execute(
        """SELECT COUNT(*) FROM series_memberships sm WHERE NOT EXISTS
           (SELECT 1 FROM series_assertions sa WHERE sa.work_id=sm.work_id AND sa.series_id=sm.series_id)"""
    ).fetchone()[0]
    if n:
        _fail(f"{n} series memberships still have no assertion")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@register_validation(39)
def _validate_discover_chat(conn: sqlite3.Connection) -> None:
    for table in ("discover_conversations", "discover_messages", "discover_profiles", "discover_usage"):
        if not _table_exists(conn, table):
            _fail(f"{table} missing")
    if not {"result_json", "deadline", "status"} <= _columns(conn, "discover_messages"):
        _fail("discover_messages is missing columns")
    for table in ("discover_conversations", "discover_profiles"):
        if "account_type" not in _columns(conn, table):
            _fail(f"{table} is missing account_type")
    if not {"user_id", "at"} <= _columns(conn, "discover_usage"):
        _fail("discover_usage is missing columns")


def run_pending_migrations() -> list[dict[str, Any]]:
    """Apply pending steps in order; returns one result dict per step applied
    (``version``, ``name``, ``applied_at``, ``row_counts``, ``duration_ms``).
    Returns [] without touching the filesystem when nothing is pending."""
    with db.session() as conn:
        done = _applied_versions(conn)
        conn.commit()
        pending = [(v, n, fn) for v, n, fn in MIGRATIONS if v not in done]
        if not pending:
            logger.info("No pending migrations (%d recorded)", len(done))
            return []
        needs_backup = _database_has_data(conn)

    logger.info("Running %d pending migration(s): %s", len(pending), [v for v, _, _ in pending])
    backup: dict[str, Any] | None = _create_pre_migration_backup() if needs_backup else None
    if backup:
        logger.info("Pre-migration backup written: %s", backup.get("database"))

    results: list[dict[str, Any]] = []
    try:
        with db.tx() as conn:
            for version, name, fn in pending:
                t0 = db.now()
                counts = fn(conn)
                for v, val in VALIDATIONS:
                    if v == version:
                        val(conn)
                applied = db.now()
                conn.execute(
                    "INSERT INTO migration_steps(version,name,applied_at,applied_at_iso,row_counts_json,duration_ms) "
                    "VALUES (?,?,?,?,?,?)",
                    (version, name, applied, datetime.fromtimestamp(applied, tz=timezone.utc).isoformat(),
                     json.dumps(counts, sort_keys=True), int((applied - t0) * 1000)),
                )
                results.append({"version": version, "name": name, "row_counts": counts,
                                "applied_at": datetime.fromtimestamp(applied, tz=timezone.utc).isoformat(),
                                "duration_ms": int((applied - t0) * 1000)})
                logger.info("Migration %d (%s) applied: %s", version, name, counts)
    except Exception as exc:
        where = f" The pre-migration backup is kept at {settings.backup_dir / backup['database']}." if backup else ""
        logger.error("Migration failed, transaction rolled back: %s", exc)
        raise MigrationError(f"Migration failed and was rolled back; startup stopped: {exc}.{where}") from exc
    return results


def enforce_invariants() -> dict[str, int]:
    """Run on EVERY start, after the recorded steps (overseer review, 2026-10-03).

    The acquisition-intent rule (status='missing' requires intent) used to be re-applied on
    every start, so any code path that inserts a bad row is corrected on the next restart
    (Phase 1 relied on this). Recording step 3 once would lose that self-heal, so the same
    idempotent statements also run here. Cheap: both updates are no-ops on clean data.
    """
    with db.tx() as conn:
        counts = _migrate_backfill_acquisition_intent(conn)
    if counts.get("rows_null_fixed") or counts.get("rows_demoted"):
        logger.warning("invariants: acquisition intent corrected %s", counts)
    return counts



# ---- Step 43: podcast library storage (issue #510) ----------------------------------------------------------------------
# An episode's identity is (show, title, release date); its file path is an ordinary updatable column, so a reorganize
# changes the path and nothing else. Played state and resume position hang off the episode id, never the path. Extras is a
# season row with is_extras=1 and no number, so it can never collide with a numbered season (including season 0).
PODCAST_TABLES = ("podcast_show", "podcast_season", "podcast_episode", "podcast_progress")

PODCAST_DDL = (
    """CREATE TABLE IF NOT EXISTS podcast_show (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS podcast_season (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES podcast_show(id) ON DELETE CASCADE,
  number INTEGER,
  is_extras INTEGER NOT NULL DEFAULT 0 CHECK (is_extras IN (0,1)),
  created_at REAL NOT NULL,
  CHECK ((is_extras=1 AND number IS NULL) OR (is_extras=0 AND number IS NOT NULL))
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_podcast_season_key ON podcast_season(show_id,is_extras,COALESCE(number,0))",
    """CREATE TABLE IF NOT EXISTS podcast_episode (
  id TEXT PRIMARY KEY,
  show_id TEXT NOT NULL REFERENCES podcast_show(id) ON DELETE CASCADE,
  season_id TEXT NOT NULL REFERENCES podcast_season(id) ON DELETE CASCADE,
  position INTEGER,
  title TEXT NOT NULL,
  release_date TEXT NOT NULL CHECK (release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  description TEXT,
  duration_seconds REAL,
  has_cover INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (show_id,title,release_date)
)""",
    "CREATE INDEX IF NOT EXISTS idx_podcast_episode_season ON podcast_episode(season_id,position)",
    "CREATE INDEX IF NOT EXISTS idx_podcast_episode_path ON podcast_episode(path)",
    """CREATE TABLE IF NOT EXISTS podcast_progress (
  episode_id TEXT NOT NULL REFERENCES podcast_episode(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL DEFAULT '',
  played INTEGER NOT NULL DEFAULT 0 CHECK (played IN (0,1)),
  position_seconds REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (episode_id,user_id)
)""",
)


@register(43)
def _migrate_podcast_schema(conn: sqlite3.Connection) -> dict[str, int]:
    created = sum(1 for t in PODCAST_TABLES if not _table_exists(conn, t))
    for stmt in PODCAST_DDL:
        conn.execute(stmt)
    return {"tables_created": created, "already_applied": int(not created)}


@register_validation(43)
def _validate_podcast_schema(conn: sqlite3.Connection) -> None:
    missing = [t for t in PODCAST_TABLES if not _table_exists(conn, t)]
    if missing:
        _fail(f"podcast tables missing: {missing}")


# ---- Step 44: podcast show cover flag (issue #511) ----------------------------------------------------------------------
@register(44)
def _migrate_podcast_show_has_cover(conn: sqlite3.Connection) -> dict[str, int]:
    """The scanner records whether a show folder holds a cover.jpg. Additive and idempotent."""
    if "has_cover" in _columns(conn, "podcast_show"):
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE podcast_show ADD COLUMN has_cover INTEGER NOT NULL DEFAULT 0")
    return {"columns_added": 1, "already_applied": 0}


@register_validation(44)
def _validate_podcast_show_has_cover(conn: sqlite3.Connection) -> None:
    if "has_cover" not in _columns(conn, "podcast_show"):
        _fail("podcast_show is missing has_cover")


# ---- Step 45: podcast episode missing flag (issue #512) -----------------------------------------------------------------
@register(45)
def _migrate_podcast_episode_missing(conn: sqlite3.Connection) -> dict[str, int]:
    """A rescan flags an episode whose file is gone instead of deleting it, so progress survives a temporary move."""
    if "missing" in _columns(conn, "podcast_episode"):
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE podcast_episode ADD COLUMN missing INTEGER NOT NULL DEFAULT 0")
    return {"columns_added": 1, "already_applied": 0}


@register_validation(45)
def _validate_podcast_episode_missing(conn: sqlite3.Connection) -> None:
    if "missing" not in _columns(conn, "podcast_episode"):
        _fail("podcast_episode is missing the missing column")


# ---- Step 46: library type on root folders (issue #513) -----------------------------------------------------------------
# The media_kind CHECK cannot be widened without rebuilding root_folders (its children use ON DELETE SET NULL), so a separate
# additive column carries the library type. Existing rows are 'books' and keep taking the existing scan path.
@register(46)
def _migrate_root_library_type(conn: sqlite3.Connection) -> dict[str, int]:
    if "library_type" in _columns(conn, "root_folders"):
        return {"columns_added": 0, "already_applied": 1}
    conn.execute("ALTER TABLE root_folders ADD COLUMN library_type TEXT NOT NULL DEFAULT 'books' "
                 "CHECK(library_type IN ('books','podcasts'))")
    return {"columns_added": 1, "already_applied": 0}


@register_validation(46)
def _validate_root_library_type(conn: sqlite3.Connection) -> None:
    if "library_type" not in _columns(conn, "root_folders"):
        _fail("root_folders is missing library_type")


# ---- Step 47: file-operation audit log (P5-11-1, issue #309) -------------------------------------------------------------
# Append-only event log for plan execution. Like steps 25/26 this step is the only copy of the DDL: init_db() runs every
# pending step right after the base schema, so a fresh install gets the table from here too.
@register(47)
def _migrate_file_operation_audit(conn: sqlite3.Connection) -> dict[str, int]:
    existed = _table_exists(conn, "file_operation_audit")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_operation_audit ("
        "id INTEGER PRIMARY KEY, "
        "plan_id TEXT NOT NULL REFERENCES file_operation_plans(id) ON DELETE CASCADE, "
        "step_id TEXT REFERENCES file_operation_steps(id) ON DELETE SET NULL, "
        "event TEXT NOT NULL, "
        "detail_json TEXT NOT NULL DEFAULT '{}', "
        "created_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_operation_audit_plan ON file_operation_audit(plan_id,created_at)")
    return {"tables_created": 1 - int(existed), "already_applied": int(existed)}


@register_validation(47)
def _validate_file_operation_audit(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "file_operation_audit"):
        _fail("file_operation_audit missing")
    missing = {"id", "plan_id", "step_id", "event", "detail_json", "created_at"} - _columns(conn, "file_operation_audit")
    if missing:
        _fail(f"file_operation_audit is missing columns {sorted(missing)}")


def load_steps() -> None:
    """Import every module of the ``migration_steps`` package so its ``@register`` calls run (a step with schema DDL stays inline in this file, per the migration rules; a data-only step may be its own file)."""
    import importlib
    import pkgutil

    from . import migration_steps

    for mod in sorted(m.name for m in pkgutil.iter_modules(migration_steps.__path__)):
        importlib.import_module(f"{migration_steps.__name__}.{mod}")


load_steps()
