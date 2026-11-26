from __future__ import annotations

import logging
import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import settings
from .migrations import WORK_MERGE_TABLES_DDL

_lock = threading.RLock()


def uid() -> str:
    return str(uuid.uuid4())


def now() -> float:
    return time.time()


def connect(path: Path | str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection; the caller must close it (prefer session()/tx(), or
    contextlib.closing). Dropping the reference is not enough: CPython keeps the
    connection alive in a cycle with its statement cache until the cyclic GC runs,
    and the eventual late close checkpoints and unlinks -wal/-shm by path (#36)."""
    target = str(path or settings.db_path)
    if readonly:
        uri = f"file:{Path(target).resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(target, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error and is always closed.

    ``with connect() as conn`` only ends the transaction; it leaves the (WAL)
    connection open, holding file handles on the database."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _is_locked(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in str(exc).lower() or "busy" in str(exc).lower())


def tx_retry(fn: Callable[[sqlite3.Connection], Any], *, attempts: int = 6, delay: float = 1.0) -> Any:
    """Run ``fn(conn)`` in a tx(), retrying the whole transaction while the database is
    locked. For finalising writes (closing a job run, marking an import done): losing one
    leaves a record open forever, so a lock that outlasts busy_timeout is waited out."""
    for n in range(1, attempts + 1):
        try:
            with tx() as conn:
                return fn(conn)
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc) or n == attempts:
                raise
            time.sleep(delay * n)


def one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with session() as conn:
        return [dict(x) for x in conn.execute(sql, params).fetchall()]


def like_pattern(q: str) -> str:
    """'%q%' with LIKE wildcards in user text escaped; pair with ESCAPE '\\'."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with tx() as conn:
        conn.execute(sql, params)


def json_load(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def json_dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  email TEXT,
  display_name TEXT,
  password_hash TEXT,
  auth_source TEXT NOT NULL DEFAULT 'local',
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_login_at REAL,
  auth_subject TEXT,
  session_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  scopes_json TEXT NOT NULL DEFAULT '["*"]',
  created_at REAL NOT NULL,
  last_used_at REAL,
  revoked_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT,
  secret_value TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contributors (
  id TEXT PRIMARY KEY,
  metadata_id TEXT,
  name TEXT NOT NULL,
  sort_name TEXT,
  image_url TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contributors_metadata ON contributors(metadata_id) WHERE metadata_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS works (
  id TEXT PRIMARY KEY,
  metadata_id TEXT,
  title TEXT NOT NULL,
  sort_title TEXT,
  subtitle TEXT,
  description TEXT,
  cover_url TEXT,
  first_publish_date TEXT,
  primary_language TEXT,
  monitored INTEGER NOT NULL DEFAULT 1,
  monitor_policy TEXT NOT NULL DEFAULT 'all',
  metadata_status TEXT NOT NULL DEFAULT 'resolved',
  added_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  preferred_ebook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL,
  preferred_audiobook_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_works_metadata ON works(metadata_id) WHERE metadata_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_works_title ON works(title);

CREATE TABLE IF NOT EXISTS work_contributors (
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  contributor_id TEXT NOT NULL REFERENCES contributors(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'author',
  ord INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(work_id, contributor_id, role)
);

CREATE TABLE IF NOT EXISTS editions (
  id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  metadata_id TEXT,
  title TEXT,
  publisher TEXT,
  release_date TEXT,
  language TEXT,
  pages INTEGER,
  cover_url TEXT,
  duration_seconds INTEGER,
  abridged INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_editions_work ON editions(work_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_editions_metadata ON editions(metadata_id) WHERE metadata_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS identifiers (
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY(entity_type, namespace, value)
);
CREATE INDEX IF NOT EXISTS idx_identifiers_entity ON identifiers(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS series (
  id TEXT PRIMARY KEY,
  metadata_id TEXT,
  name TEXT NOT NULL,
  sort_name TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_series_metadata ON series(metadata_id) WHERE metadata_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS series_memberships (
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  position TEXT,
  PRIMARY KEY(work_id, series_id)
);

CREATE TABLE IF NOT EXISTS root_folders (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,
  media_kind TEXT NOT NULL CHECK(media_kind IN ('ebook','audiobook','mixed')),
  management_mode TEXT NOT NULL CHECK(management_mode IN ('observe','adopt','managed')) DEFAULT 'adopt',
  monitor_default INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_scan_at REAL
);

CREATE TABLE IF NOT EXISTS profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  media_kind TEXT NOT NULL CHECK(media_kind IN ('ebook','audiobook')),
  config_json TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media_assets (
  id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK(kind IN ('ebook','audiobook')),
  format TEXT,
  -- Defaults must describe a slot nobody has asked for. 'missing' + monitored=1 as
  -- the default is what let catalog discovery silently fill a download queue.
  status TEXT NOT NULL DEFAULT 'none',
  monitored INTEGER NOT NULL DEFAULT 0,
  has_acquisition_intent INTEGER NOT NULL DEFAULT 0,
  profile_id TEXT REFERENCES profiles(id) ON DELETE SET NULL,
  root_folder_id TEXT REFERENCES root_folders(id) ON DELETE SET NULL,
  narrator_json TEXT NOT NULL DEFAULT '[]',
  duration_seconds INTEGER,
  abridged INTEGER,
  quality_json TEXT NOT NULL DEFAULT '{}',
  upgrade_policy TEXT NOT NULL DEFAULT 'none',
  added_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(work_id,kind,format)
);
CREATE INDEX IF NOT EXISTS idx_assets_work ON media_assets(work_id);
CREATE INDEX IF NOT EXISTS idx_assets_status ON media_assets(kind,status,monitored);

CREATE TABLE IF NOT EXISTS media_files (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  path TEXT NOT NULL UNIQUE,
  relative_path TEXT,
  part_number INTEGER,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  mtime REAL,
  sha256 TEXT,
  fingerprint TEXT,
  container TEXT,
  codec TEXT,
  sample_rate INTEGER,
  channels INTEGER,
  chapter_count INTEGER,
  has_embedded_cover INTEGER,
  bitrate INTEGER,
  duration_seconds INTEGER,
  embedded_json TEXT NOT NULL DEFAULT '{}',
  adopted INTEGER NOT NULL DEFAULT 0,
  managed INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0,
  discovered_at REAL NOT NULL,
  verified_at REAL,
  integrity_state TEXT CHECK(integrity_state IN ('ok','changed','missing','damaged','unverified')),
  integrity_detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_media_files_asset ON media_files(asset_id);

CREATE TABLE IF NOT EXISTS scan_runs (
  id TEXT PRIMARY KEY,
  root_folder_id TEXT REFERENCES root_folders(id) ON DELETE SET NULL,
  status TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  files_seen INTEGER NOT NULL DEFAULT 0,
  matched INTEGER NOT NULL DEFAULT 0,
  ambiguous INTEGER NOT NULL DEFAULT 0,
  unmatched INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS scan_candidates (
  id TEXT PRIMARY KEY,
  scan_run_id TEXT NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  media_kind TEXT NOT NULL,
  detected_json TEXT NOT NULL,
  match_work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
  match_edition_id TEXT REFERENCES editions(id) ON DELETE SET NULL,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('matched','probable','ambiguous','unmatched','ignored')),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_candidates_run ON scan_candidates(scan_run_id,status);

CREATE TABLE IF NOT EXISTS indexers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('prowlarr','newznab','torznab')),
  base_url TEXT NOT NULL,
  api_key_enc TEXT,
  categories_json TEXT NOT NULL DEFAULT '[]',
  priority INTEGER NOT NULL DEFAULT 50,
  enabled INTEGER NOT NULL DEFAULT 1,
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  last_test_at REAL,
  last_ok_at REAL,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_clients (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('sabnzbd','qbittorrent','nzbget','transmission')),
  base_url TEXT NOT NULL,
  username_enc TEXT,
  password_enc TEXT,
  api_key_enc TEXT,
  category TEXT,
  priority INTEGER NOT NULL DEFAULT 50,
  enabled INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL DEFAULT '{}',
  last_test_at REAL,
  last_ok_at REAL,
  last_error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS release_searches (
  id TEXT PRIMARY KEY,
  work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
  asset_kind TEXT NOT NULL,
  query_json TEXT NOT NULL,
  automatic INTEGER NOT NULL DEFAULT 0,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY,
  search_id TEXT REFERENCES release_searches(id) ON DELETE CASCADE,
  work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
  asset_kind TEXT NOT NULL,
  indexer_id TEXT REFERENCES indexers(id) ON DELETE SET NULL,
  indexer_guid TEXT,
  protocol TEXT NOT NULL,
  raw_title TEXT NOT NULL,
  download_url TEXT,
  info_url TEXT,
  size_bytes INTEGER,
  publish_date TEXT,
  seeders INTEGER,
  leechers INTEGER,
  grabs INTEGER,
  parsed_json TEXT NOT NULL DEFAULT '{}',
  score INTEGER NOT NULL DEFAULT 0,
  score_json TEXT NOT NULL DEFAULT '[]',
  rejected INTEGER NOT NULL DEFAULT 0,
  rejection_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_releases_search ON releases(search_id,score DESC);

CREATE TABLE IF NOT EXISTS downloads (
  id TEXT PRIMARY KEY,
  work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
  asset_id TEXT REFERENCES media_assets(id) ON DELETE SET NULL,
  release_id TEXT REFERENCES releases(id) ON DELETE SET NULL,
  client_id TEXT REFERENCES download_clients(id) ON DELETE SET NULL,
  client_download_id TEXT,
  state TEXT NOT NULL,
  protocol TEXT,
  source_title TEXT,
  download_path TEXT,
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  added_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  completed_at REAL,
  imported_at REAL
);
CREATE INDEX IF NOT EXISTS idx_downloads_state ON downloads(state,updated_at);

CREATE TABLE IF NOT EXISTS blocklist (
  id TEXT PRIMARY KEY,
  work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
  indexer_guid TEXT,
  release_title TEXT,
  reason TEXT NOT NULL,
  expires_at REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
  id TEXT PRIMARY KEY,
  work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
  asset_id TEXT REFERENCES media_assets(id) ON DELETE SET NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC);

CREATE TABLE IF NOT EXISTS review_tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  title TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  priority INTEGER NOT NULL DEFAULT 50,
  created_at REAL NOT NULL,
  resolved_at REAL,
  resolution_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_open ON review_tasks(status,priority DESC,created_at);

CREATE TABLE IF NOT EXISTS migration_runs (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_path TEXT NOT NULL,
  media_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 1,
  analysis_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_mappings (
  run_id TEXT NOT NULL REFERENCES migration_runs(id) ON DELETE CASCADE,
  source_entity TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_entity TEXT,
  target_id TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL DEFAULT 'review',
  PRIMARY KEY(run_id,source_entity,source_id)
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  schedule_seconds INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at REAL,
  last_run_at REAL,
  last_status TEXT,
  last_duration REAL,
  last_error TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(kind,name)
);

CREATE TABLE IF NOT EXISTS job_runs (
  id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  error TEXT,
  result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('webhook', 'generic')),
  url TEXT NOT NULL,
  secret_enc TEXT,
  events_json TEXT NOT NULL DEFAULT '["*"]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""

EXTRA_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS path_mappings (
  id TEXT PRIMARY KEY,
  client_id TEXT REFERENCES download_clients(id) ON DELETE CASCADE,
  remote_path TEXT NOT NULL,
  local_path TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(client_id,remote_path)
);
CREATE TABLE IF NOT EXISTS naming_templates (
  id TEXT PRIMARY KEY,
  media_kind TEXT NOT NULL UNIQUE,
  folder_template TEXT NOT NULL,
  file_template TEXT NOT NULL,
  multipart_template TEXT,
  replace_illegal INTEGER NOT NULL DEFAULT 1,
  colon_replacement TEXT NOT NULL DEFAULT 'smart',
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS author_monitors (
  contributor_id TEXT PRIMARY KEY REFERENCES contributors(id) ON DELETE CASCADE,
  policy TEXT NOT NULL CHECK(policy IN ('all','future','latest','none')) DEFAULT 'future',
  ebook INTEGER NOT NULL DEFAULT 0,
  audiobook INTEGER NOT NULL DEFAULT 1,
  started_at REAL NOT NULL,
  last_refresh_at REAL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS series_monitors (
  series_id TEXT PRIMARY KEY REFERENCES series(id) ON DELETE CASCADE,
  policy TEXT NOT NULL CHECK(policy IN ('all','future','none')) DEFAULT 'future',
  ebook INTEGER NOT NULL DEFAULT 0,
  audiobook INTEGER NOT NULL DEFAULT 1,
  started_at REAL NOT NULL,
  last_refresh_at REAL,
  audiobook_policy TEXT CHECK(audiobook_policy IN ('none','future','all')),
  ebook_policy TEXT CHECK(ebook_policy IN ('none','future','all'))
);
CREATE TABLE IF NOT EXISTS cover_cache (
  work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('cached','none')),
  source TEXT,
  content_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cover_cache_status ON cover_cache(status,updated_at);
-- Which works have already been read for metadata embedded in their own files.
-- Without this the backfill re-runs ffprobe over the same audiobooks on every pass,
-- because "still missing a description" stays true for a file that simply has none.
CREATE TABLE IF NOT EXISTS local_meta (
  work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  files_fingerprint TEXT,
  applied_json TEXT NOT NULL DEFAULT '{}',
  harvested_at REAL NOT NULL
);
-- Contributors are looked up by name on every migrated/adopted book; without this
-- index that lookup is a full scan of the contributor table per imported work.
-- The index must carry NOCASE, or a "name = ? COLLATE NOCASE" lookup cannot use it.
CREATE INDEX IF NOT EXISTS idx_contributors_name ON contributors(name COLLATE NOCASE);

-- Tags: freeform labels plus one reserved system tag ('kids') that gates visibility
-- for kids-scope users. 'kind' distinguishes it so the UI can block deleting/renaming
-- the one tag an access-control decision depends on.
CREATE TABLE IF NOT EXISTS tags (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL DEFAULT 'manual' CHECK(kind IN ('manual','system')),
  color TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_tags (
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','llm')),
  confidence REAL,
  created_at REAL NOT NULL,
  PRIMARY KEY(work_id,tag_id)
);
CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON work_tags(tag_id);

-- Per-user annotation, not per-install: two people reading the same library disagree
-- about ratings and are at different points in the same book.
CREATE TABLE IF NOT EXISTS ratings (
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
  updated_at REAL NOT NULL,
  PRIMARY KEY(work_id,user_id)
);
CREATE TABLE IF NOT EXISTS read_states (
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('want_to_read','reading','finished','dnf')),
  started_at REAL,
  finished_at REAL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(work_id,user_id)
);
CREATE INDEX IF NOT EXISTS idx_read_states_user ON read_states(user_id,status);

-- Cached so opening a work does not cost an LLM/heuristic pass every time; recomputed
-- when stale (see recommendations.py). algorithm is recorded so a later upgrade
-- (heuristic -> LLM) is visibly distinguishable rather than silently mixed together.
CREATE TABLE IF NOT EXISTS recommendation_cache (
  work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  algorithm TEXT NOT NULL,
  items_json TEXT NOT NULL,
  generated_at REAL NOT NULL
);

-- Remembers that a work was already put in front of the LLM for kids-classification,
-- distinct from the tag itself: a 'not_kids' verdict never becomes a tag, so without
-- this a rejected work would be re-submitted to the LLM on every batch pass forever.
-- No row is ever written when the LLM was never actually consulted (disabled/unset) -
-- that must not be conflated with a real "uncertain" verdict, or enabling the LLM
-- later would silently skip everything a disabled LLM had "seen".
CREATE TABLE IF NOT EXISTS kids_classification (
  work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  verdict TEXT NOT NULL CHECK(verdict IN ('kids','not_kids','uncertain')),
  confidence REAL,
  reason TEXT,
  checked_at REAL NOT NULL,
  detail_json TEXT
);

-- Evidence behind a series link, independent of whether it was accepted:
-- series/series_memberships only ever hold the post-filter result, so a rejected
-- or uncertain candidate leaves no trace today. series_id is nullable because an
-- assertion can be scored before a series row is chosen or created.
CREATE TABLE IF NOT EXISTS series_assertions (
  id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  series_id TEXT REFERENCES series(id) ON DELETE CASCADE,
  source TEXT,
  raw_value TEXT NOT NULL,
  normalized_name TEXT,
  proposed_position TEXT,
  evidence_type TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('auto_accepted','accepted','uncertain','rejected','manually_confirmed','manually_suppressed')),
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_assertions_work ON series_assertions(work_id);
CREATE INDEX IF NOT EXISTS idx_series_assertions_series_status ON series_assertions(series_id,status);

-- Ephemeral federated-search results (vNext Part XVII.D). A row here is NOT a
-- canonical Work -- search_federated() results must never create one on their
-- own. It only becomes a Work via search_candidates.promote_candidate(), which
-- is reached by explicit user select/add, resolver adoption, intentional
-- author/series discovery import, or an automation that explicitly requires it.
CREATE TABLE IF NOT EXISTS search_candidates (
  id TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  title TEXT,
  subtitle TEXT,
  authors_json TEXT NOT NULL DEFAULT '[]',
  contributors_json TEXT NOT NULL DEFAULT '[]',
  series_json TEXT NOT NULL DEFAULT '[]',
  series_position TEXT,
  identifiers_json TEXT NOT NULL DEFAULT '[]',
  publication_date TEXT,
  publisher TEXT,
  language TEXT,
  cover_refs_json TEXT NOT NULL DEFAULT '[]',
  providers_json TEXT NOT NULL DEFAULT '[]',
  provider_evidence_json TEXT NOT NULL DEFAULT '{}',
  match_score REAL NOT NULL DEFAULT 0,
  match_explanation TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','promoted','discarded','dismissed')),
  promoted_work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_candidates_query ON search_candidates(query);
CREATE INDEX IF NOT EXISTS idx_search_candidates_status ON search_candidates(status);
"""
# The work_merges / work_merge_ledger tables (vNext Part XXVI) are part of a fresh install
# too, but their DDL is owned by migrations.py (step 29 rebuilds older installs from the
# same text), so it is appended from there rather than written out twice.
# maintenance_issues / maintenance_events (step 25) and file_operation_plans /
# file_operation_steps (step 26) are not mirrored here at all: each step is the only copy
# of its DDL (tables and indexes together), and init_db() runs every pending step right
# after this script, so a fresh install gets them from the same text an upgrade does.
EXTRA_SCHEMA += WORK_MERGE_TABLES_DDL


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for installs created before this column existed."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    settings.ensure_dirs()
    with _lock, session() as conn:
        conn.executescript(SCHEMA + EXTRA_SCHEMA)
        # Legacy baseline marker. Production already carries version 1 (and repair.py
        # records 2); the F-04 framework tracks its own steps in migration_steps.
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (?,?)",
            (1, now()),
        )
        # access_scope gates a 'kids' account to only the works tagged 'kids' and
        # strips acquisition/settings surfaces entirely - see auth.require_full_access.
        _add_column(conn, "users", "access_scope", "access_scope TEXT NOT NULL DEFAULT 'full'")
        _add_column(conn, "users", "kindle_email", "kindle_email TEXT")
        # Display-only prefix (never enough to authenticate) so a key can be recognised
        # in the list, and an optional hard expiry. NULL expiry = does not expire.
        _add_column(conn, "api_keys", "key_prefix", "key_prefix TEXT")
        _add_column(conn, "api_keys", "expires_at", "expires_at REAL")
        # NULL = never resolved (a genuine warning). 'provider'/'local'/'manual' are all
        # legitimate resolved states that must not render as a warning - see
        # metadata_badge() in app.js. Existing rows get backfilled once below.
        _add_column(conn, "works", "metadata_source", "metadata_source TEXT")
        # bookdoe's own series-name dedup key (see services/seriesnorm.py) -
        # independent of whatever id OpenReads attaches, since OpenReads' own
        # normalizer only re-converges duplicates on its next restart.
        _add_column(conn, "series", "norm_key", "norm_key TEXT")
        # Distinguishes an asset the user actually wants (Wanted list, acquisition
        # automation) from a placeholder slot created by catalog discovery - see
        # Part XI of the vNext spec: knowing about a book is not wanting it.
        _add_column(conn, "media_assets", "has_acquisition_intent", "has_acquisition_intent INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # Versioned migrations own every data backfill and every post-baseline schema
    # change (see migrations.py); steps detect state already applied by older code.
    from . import migrations as _migrations

    _migrations.run_pending_migrations()

    _migrations.enforce_invariants()
    _ensure_system_tags()
    ensure_default_profiles()
    ensure_default_jobs()
    ensure_default_naming()


def _ensure_system_tags() -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tags(id,name,kind,color,created_at) VALUES (?,?,?,?,?)",
            (uid(), "kids", "system", "#45c98a", now()),
        )


def ensure_default_profiles() -> None:
    defaults = [
        (
            "ebook-default",
            "Ebook — EPUB preferred",
            "ebook",
            {
                "allowed_formats": ["epub", "azw3", "mobi", "pdf"],
                "preferred_formats": ["epub", "azw3", "mobi", "pdf"],
                "cutoff": "epub",
                "pdf_undesirable_if_epub": True,
                "languages": ["eng"],
                "language_policy": "prefer",
                "min_size_mb": 0.05,
                "max_size_mb": 500,
            },
        ),
        (
            "audio-default",
            "Audiobook — M4B preferred",
            "audiobook",
            {
                "allowed_formats": ["m4b", "mp3", "m4a", "flac", "opus", "ogg"],
                "preferred_formats": ["m4b", "m4a", "mp3", "flac", "opus", "ogg"],
                "abridged": "reject",
                "languages": ["eng"],
                "language_policy": "prefer",
                "min_size_mb": 10,
                "max_size_mb": 50000,
            },
        ),
    ]
    with tx() as conn:
        for pid, name, kind, cfg in defaults:
            conn.execute(
                """INSERT OR IGNORE INTO profiles(id,name,media_kind,config_json,is_default,created_at,updated_at)
                   VALUES (?,?,?,?,1,?,?)""",
                (pid, name, kind, json_dump(cfg), now(), now()),
            )


# Jobs that acquire media or call external metadata providers stay OFF until an
# operator turns them on. A fresh install must never start searching, grabbing or
# hammering providers on its own; see docs/OPERATIONS.md for the recommended
# enable order. Purely local/internal jobs are safe to run immediately.
UNSAFE_BY_DEFAULT_JOBS = frozenset({"wanted-search", "upgrade-search", "author-discovery", "metadata-refresh"})


def ensure_default_jobs() -> None:
    jobs = [
        ("download-poll", "Download queue poll", 30),
        ("wanted-search", "Wanted automatic search", 1800),
        # Searches only for owned assets a user opted in (upgrade_policy='search'); it
        # records candidates and never grabs or replaces anything. See services/upgrades.py.
        ("upgrade-search", "Upgrade candidate search", 21600),
        ("metadata-refresh", "Metadata refresh", 86400),
        ("author-discovery", "Author bibliography discovery", 21600),
        ("database-backup", "Database backup", 86400),
        ("library-health", "Library file health", 21600),
        ("housekeeping", "Housekeeping", 3600),
        # Cover art is part of rendering the library rather than acquiring media: it
        # reads bytes already on disk first and only asks a cover CDN for works it
        # cannot satisfy locally, in small batches. Set BOOKDOE_COVER_REMOTE_FETCH=0
        # to keep it entirely local.
        ("cover-fetch", "Cover art backfill", 600),
        # Enabled by default even though it is a no-op until BOOKDOE_LLM_ENABLED is
        # set - the LLM setting is the actual safety gate (see llmclient.py), not this
        # flag. Small batches, and it never writes a tag: a confident verdict becomes
        # a kids_tag review task an admin approves in the Inbox - see tagging.py.
        ("tag-classify", "Kids-tag classification", 900),
        # Fills categories / subjects / maturity rating for works that lack them, a few per
        # run (services/audience.py). Provider calls go through OpenReads, which caches them.
        ("audience-backfill", "Audience metadata backfill", 600),
        # Read-only: detectors only write maintenance_issues rows, never files. Each
        # detector has a finding and time budget, so the daily run is bounded.
        ("maintenance-scan", "Maintenance scan", 86400),
        # Manual-only job (never scheduled): the body of a managed library build
        # (services/managedbuild.py). It runs only after a dry run was confirmed and
        # started from Settings > Library roots; it copies into managed roots only.
        ("managed-library-build", "Managed library build", None),
        # Drain the conversion queue (services/conversion.py). Safe to leave on: it only works
        # on rows an admin queued (or the opt-in auto-on-import policy queued), one run per
        # kind, and returns at once when the queue is empty. Settings > Conversion switches a
        # kind off.
        # Deletes trashed redundant files (services/filetrash.py) once they are older than the retention
        # (Settings > System, default 30 days). Only ever touches files this app moved into a managed
        # root's .bookdoe-trash folder.
        ("trash-purge", "Trash purge", 86400),
        # Duplicate finder (services/dupefinder.py, bulkmerge.py). All four are manual-only: the scan is read-only but long
        # (resumable, incremental), the harvest and the post-merge refresh call the metadata provider (rate limited, a bounded
        # number per run), and the batch job only finishes a bulk merge an admin started and closed the page on.
        ("duplicate-scan", "Duplicate scan (whole library)", None),
        ("duplicate-harvest", "Refresh-conflict harvest (file-only works)", None),
        ("duplicate-batch", "Bulk merge: finish started batches", None),
        ("duplicate-refresh", "Metadata refresh of merged records", None),
        # Read-only walk of BOOKDOE_PODCASTS_DIR into podcast shows, seasons and episodes (services/podcastscan.py);
        # a no-op while that folder is not configured.
        ("podcast-scan", "Podcast folder scan", 3600),
        ("convert-audio", "Audiobook conversion (M4B)", 60),
        ("convert-ebook", "Ebook conversion (EPUB)", 60),
    ]
    with tx() as conn:
        for kind, name, seconds in jobs:
            enabled = 0 if kind in UNSAFE_BY_DEFAULT_JOBS else 1
            next_run = now() + seconds if seconds is not None else None
            conn.execute(
                """INSERT OR IGNORE INTO jobs(id,kind,name,schedule_seconds,enabled,next_run_at,config_json)
                   VALUES (?,?,?,?,?,?,'{}')""",
                (uid(), kind, name, seconds, enabled, next_run),
            )


def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value_json FROM settings WHERE key=?", (key,))
    if not row or row["value_json"] is None:
        return default
    return json_load(row["value_json"], default)


def set_setting(key: str, value: Any) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (key, json_dump(value), now()),
        )


def contributor_key(name: str) -> str:
    """The identity an author name is deduplicated on.

    Deliberately conservative: case and surrounding/repeated whitespace only. Any
    cleverer normalisation (stripping punctuation, initials) would merge genuinely
    different people, and an author page is the one place where a wrong merge is
    immediately visible and tedious to undo.
    """
    return " ".join(str(name or "").split()).casefold()


def contributor_id_for_name(conn, name: str, *, create: bool = True) -> str | None:
    """Resolve an author name to a single contributor row, creating it once.

    Every caller that records an author from a non-metadata source (Readarr import,
    file adoption) must go through this. Inserting a fresh contributor per book is
    what turned the authors list into one row per book.
    """
    key = contributor_key(name)
    if not key:
        return None
    clean = " ".join(str(name).split())
    # The indexed COLLATE NOCASE lookup is a prefilter on the whitespace-collapsed
    # name; contributor_key then decides. Matching only the raw name would miss
    # "craig  alanson" against a stored "Craig Alanson".
    #
    # Two statements rather than one "a=? OR b=?": SQLite's OR optimisation rewrites
    # that into index lookups that use the *index's* collation, silently dropping the
    # NOCASE and matching nothing. Verified against this schema.
    candidates: dict[str, Any] = {}
    for probe in {clean, str(name)}:
        for row in conn.execute(
            "SELECT id,name,metadata_id,created_at FROM contributors WHERE name=? COLLATE NOCASE", (probe,)
        ).fetchall():
            candidates[row["id"]] = row
    matches = [r for r in candidates.values() if contributor_key(r["name"]) == key]
    if matches:
        # Prefer a row already linked to OpenReads; otherwise the oldest, so the
        # chosen id is stable across repeated imports.
        matches.sort(key=lambda r: (r["metadata_id"] is None, r["created_at"]))
        return matches[0]["id"]
    if not create:
        return None
    cid, t = uid(), now()
    clean = " ".join(str(name).split())
    conn.execute(
        "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
        (cid, clean, clean, t, t),
    )
    return cid


def adopt_unlinked_contributor(conn, name: str) -> str | None:
    """Find a same-named contributor that is not yet tied to a metadata record.

    When a work is linked to OpenReads, its authors arrive with provider ids. Those
    authors usually already exist locally, created nameless-of-provider by the Readarr
    import. Matching on provider id alone would add a second row for the same person
    and put the authors page back to one entry per book, one link at a time. A row
    already bound to a *different* provider id is a different person with the same
    name and is never adopted.
    """
    key = contributor_key(name)
    if not key:
        return None
    rows = conn.execute(
        "SELECT id,name,created_at FROM contributors WHERE metadata_id IS NULL AND name=? COLLATE NOCASE",
        (" ".join(str(name).split()),),
    ).fetchall()
    matches = sorted((r for r in rows if contributor_key(r["name"]) == key), key=lambda r: r["created_at"])
    return matches[0]["id"] if matches else None


def add_history(event_type: str, message: str, *, work_id: str | None = None, asset_id: str | None = None, data: Any = None) -> str:
    hid = uid()
    with tx() as conn:
        conn.execute(
            "INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (hid, work_id, asset_id, event_type, message, json_dump(data or {}), now()),
        )
    return hid


def ensure_default_naming() -> None:
    rows = [
        ("ebook", "{Author}/{Title}", "{Title} - {Year}.{Ext}", None),
        ("audiobook", "{Author}/{Title}", "{SeriesPrefix}{Title} - {Year}.{Ext}", "{SeriesPrefix}{Title} - {Part:02} - {Year}.{Ext}"),
    ]
    with tx() as conn:
        for kind, folder, filename, multipart in rows:
            conn.execute(
                "INSERT OR IGNORE INTO naming_templates(id,media_kind,folder_template,file_template,multipart_template,updated_at) VALUES (?,?,?,?,?,?)",
                (uid(), kind, folder, filename, multipart, now()),
            )
