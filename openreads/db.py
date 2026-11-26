"""SQLite-backed ID authority + cache.

Readarr expects stable integer IDs for authors/works/editions/series that
persist across restarts (it stores them in its own database long-term).
Open Library uses string keys (OL123456A, OL123456W, OL123456M). This module
is the single source of truth mapping OL keys <-> our integer IDs, plus a
cache of the fetched metadata so we don't hammer Open Library/Google Books
on every request.
"""
import sqlite3
import time
import os
import threading

import aliases
import seriesnorm

DB_PATH = os.environ.get("DB_PATH", "/data/openreads.db")
_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with _lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS authors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ol_key TEXT UNIQUE NOT NULL,
                name TEXT,
                description TEXT,
                image_url TEXT,
                fetched_at REAL
            );

            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ol_key TEXT UNIQUE NOT NULL,
                title TEXT,
                description TEXT,
                first_publish_year TEXT,
                fetched_at REAL,
                editions_fetched_at REAL
            );

            CREATE TABLE IF NOT EXISTS work_authors (
                work_id INTEGER NOT NULL REFERENCES works(id),
                author_id INTEGER NOT NULL REFERENCES authors(id),
                PRIMARY KEY (work_id, author_id)
            );

            CREATE TABLE IF NOT EXISTS editions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ol_key TEXT UNIQUE,
                work_id INTEGER REFERENCES works(id),
                title TEXT,
                isbn13 TEXT,
                isbn10 TEXT,
                asin TEXT,
                publisher TEXT,
                release_date TEXT,
                num_pages INTEGER,
                image_url TEXT,
                description TEXT,
                fetched_at REAL,
                enrich_attempted_at REAL
            );

            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_series (
                work_id INTEGER NOT NULL REFERENCES works(id),
                series_id INTEGER NOT NULL REFERENCES series(id),
                position TEXT,
                PRIMARY KEY (work_id, series_id)
            );

            CREATE TABLE IF NOT EXISTS enrichment_cache (
                source TEXT NOT NULL,
                key TEXT NOT NULL,
                found INTEGER NOT NULL,
                payload TEXT,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (source, key)
            );

            CREATE TABLE IF NOT EXISTS source_usage (
                source TEXT NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, day)
            );

            CREATE INDEX IF NOT EXISTS idx_editions_isbn13 ON editions(isbn13);
            CREATE INDEX IF NOT EXISTS idx_editions_isbn10 ON editions(isbn10);
            CREATE INDEX IF NOT EXISTS idx_editions_asin ON editions(asin);
            CREATE INDEX IF NOT EXISTS idx_editions_work ON editions(work_id);
            """
        )
        try:
            conn.execute("ALTER TABLE editions ADD COLUMN enrich_attempted_at REAL")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            conn.execute("ALTER TABLE works ADD COLUMN series_enrich_attempted_at REAL")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            # When Google Books was last asked for this work's description. Without it,
            # every search result lacking a description re-asks on every single search.
            conn.execute("ALTER TABLE works ADD COLUMN description_enrich_attempted_at REAL")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            # When audience signals (subjects, categories, maturity) were last looked up
            # for this work, so the backfill never re-asks a work it has just answered.
            conn.execute("ALTER TABLE works ADD COLUMN audience_attempted_at REAL")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            # which source last set the position ('openlibrary' | 'audiobookdb')
            conn.execute("ALTER TABLE work_series ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            conn.execute("ALTER TABLE series ADD COLUMN norm_key TEXT")
        except sqlite3.OperationalError:
            pass  # already migrated
        try:
            # NULL = never checked; '' = checked, no language data;
            # otherwise comma-separated ISO 639-2 codes.
            conn.execute("ALTER TABLE works ADD COLUMN languages TEXT")
        except sqlite3.OperationalError:
            pass  # already migrated
        # v1: series used to be read from the work record only and never
        # stored a position. Mark cached works stale once so they re-fetch
        # (lazily, on next request) with the edition-level/position logic.
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute("UPDATE works SET fetched_at=0")
            conn.execute("PRAGMA user_version=1")
    apply_author_aliases()
    clean_series()


def apply_author_aliases():
    """Re-point data stored under duplicate OL author keys at the canonical
    author: work links move over (the duplicate's row is kept so ids
    Readarr already holds still resolve, via author_by_id). Idempotent."""
    with _lock, get_conn() as conn:
        for dup, canon in aliases._all().items():
            canon = aliases.canonical_key(dup)
            if canon == dup:
                continue
            row = conn.execute("SELECT id FROM authors WHERE ol_key=?", (dup,)).fetchone()
            if not row:
                continue
            canon_row = conn.execute("SELECT id FROM authors WHERE ol_key=?", (canon,)).fetchone()
            if canon_row:
                canon_id = canon_row["id"]
            else:
                canon_id = conn.execute(
                    "INSERT INTO authors (ol_key, fetched_at) VALUES (?, 0)", (canon,)
                ).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO work_authors (work_id, author_id) "
                "SELECT work_id, ? FROM work_authors WHERE author_id=?",
                (canon_id, row["id"]),
            )
            conn.execute("DELETE FROM work_authors WHERE author_id=?", (row["id"],))
            # force a re-list so the canonical author picks up both records' works
            conn.execute("UPDATE works SET languages=NULL WHERE id IN "
                         "(SELECT work_id FROM work_authors WHERE author_id=?)", (canon_id,))


def now():
    return time.time()


def get_or_create_author(ol_key: str, name: str = None) -> int:
    ol_key = aliases.canonical_key(ol_key)
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT id FROM authors WHERE ol_key=?", (ol_key,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO authors (ol_key, name, fetched_at) VALUES (?, ?, 0)",
            (ol_key, name),
        )
        return cur.lastrowid


def get_or_create_work(ol_key: str, title: str = None) -> int:
    with _lock, get_conn() as conn:
        row = conn.execute("SELECT id FROM works WHERE ol_key=?", (ol_key,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO works (ol_key, title, fetched_at) VALUES (?, ?, 0)",
            (ol_key, title),
        )
        return cur.lastrowid


def get_or_create_edition(ol_key: str, work_id: int = None, title: str = None) -> int:
    with _lock, get_conn() as conn:
        if ol_key:
            row = conn.execute("SELECT id FROM editions WHERE ol_key=?", (ol_key,)).fetchone()
            if row:
                return row["id"]
        cur = conn.execute(
            "INSERT INTO editions (ol_key, work_id, title, fetched_at) VALUES (?, ?, ?, 0)",
            (ol_key, work_id, title),
        )
        return cur.lastrowid


def get_or_create_placeholder_edition(work_id: int, title: str = None) -> int:
    """One stable, key-less placeholder edition per work. Used when a work
    has to be listed with a Book before its real editions were fetched."""
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM editions WHERE work_id=? AND ol_key IS NULL ORDER BY id LIMIT 1",
            (work_id,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO editions (ol_key, work_id, title, fetched_at) VALUES (NULL, ?, ?, 0)",
            (work_id, title),
        )
        return cur.lastrowid


def adopt_placeholder_edition(work_id: int, ol_key: str) -> bool:
    """Give a work's placeholder edition the real OL key of its first real
    edition, so the id already handed to Readarr stays valid instead of
    lingering as a duplicate. False if there is no placeholder or the key
    already has a row."""
    with _lock, get_conn() as conn:
        if conn.execute("SELECT 1 FROM editions WHERE ol_key=?", (ol_key,)).fetchone():
            return False
        row = conn.execute(
            "SELECT id FROM editions WHERE work_id=? AND ol_key IS NULL ORDER BY id LIMIT 1",
            (work_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE editions SET ol_key=? WHERE id=?", (ol_key, row["id"]))
        return True


def _find_series(conn, key: str):
    rows = conn.execute("SELECT id, name, norm_key FROM series ORDER BY id").fetchall()
    for r in rows:
        if r["norm_key"] == key:
            return r
    for r in rows:
        if r["norm_key"] and seriesnorm.similar(key, r["norm_key"]):
            return r
    return None


def get_or_create_series(name: str) -> int:
    """Series are matched by seriesnorm.norm_key, so spelling variants of
    one series ("A Molly Murphy mystery", "Molly Murphy mysteries") share a
    row. The row keeps a bare-form name if any variant provides one."""
    key = seriesnorm.norm_key(name)
    with _lock, get_conn() as conn:
        row = _find_series(conn, key)
        if row:
            new_name = None
            if not seriesnorm.is_bare(row["name"]):
                if seriesnorm.is_bare(name):
                    new_name = name
                elif name != row["name"]:
                    # two spellings merged, neither bare: show the bare form
                    new_name = seriesnorm.bare_form(row["name"])
            if new_name and new_name != row["name"]:
                try:
                    conn.execute("UPDATE series SET name=? WHERE id=?", (new_name, row["id"]))
                except sqlite3.IntegrityError:
                    pass  # another row already has that exact name
            return row["id"]
        cur = conn.execute("INSERT INTO series (name, norm_key) VALUES (?, ?)", (name, key))
        return cur.lastrowid


def clean_series():
    """Bring series rows stored before normalization existed in line with
    it: drop publisher-imprint 'series', and merge variant spellings into
    one row (links re-pointed, positions kept). Idempotent. Note merged-away
    series ids stop resolving."""
    with _lock, get_conn() as conn:
        for r in conn.execute("SELECT id, name FROM series").fetchall():
            if seriesnorm.is_imprint(r["name"]):
                conn.execute("DELETE FROM work_series WHERE series_id=?", (r["id"],))
                conn.execute("DELETE FROM series WHERE id=?", (r["id"],))
        for r in conn.execute("SELECT id, name FROM series WHERE norm_key IS NULL").fetchall():
            conn.execute("UPDATE series SET norm_key=? WHERE id=?", (seriesnorm.norm_key(r["name"]), r["id"]))
        # Names stored with a position still attached ("Molly Murphy
        # mysteries -- [16]"): move the position onto the links and key the
        # row by the real name so it merges with its siblings below.
        for r in conn.execute("SELECT id, name FROM series").fetchall():
            base, pos = seriesnorm.split_position(r["name"])
            if pos and base != r["name"]:
                conn.execute("UPDATE series SET norm_key=? WHERE id=?", (seriesnorm.norm_key(base), r["id"]))
                conn.execute("UPDATE work_series SET position=? WHERE series_id=? AND position IS NULL", (pos, r["id"]))
                try:
                    conn.execute("UPDATE series SET name=? WHERE id=?", (base, r["id"]))
                except sqlite3.IntegrityError:
                    pass  # merged into the existing same-named row below
        rows = conn.execute(
            """SELECT s.id, s.name, s.norm_key, COUNT(ws.work_id) n FROM series s
               LEFT JOIN work_series ws ON ws.series_id=s.id GROUP BY s.id ORDER BY s.id"""
        ).fetchall()
        groups = []
        for r in rows:
            for g in groups:
                if seriesnorm.similar(r["norm_key"], g[0]["norm_key"]):
                    g.append(r)
                    break
            else:
                groups.append([r])
        for g in groups:
            if len(g) < 2:
                continue
            best = seriesnorm.pick_canonical([r["name"] for r in g for _ in range(max(r["n"], 1))])
            keep = next(r for r in g if r["name"] == best)
            for r in g:
                if r["id"] == keep["id"]:
                    continue
                conn.execute(
                    """INSERT INTO work_series (work_id, series_id, position)
                       SELECT work_id, ?, position FROM work_series WHERE series_id=?
                       ON CONFLICT(work_id, series_id) DO UPDATE
                       SET position=COALESCE(position, excluded.position)""",
                    (keep["id"], r["id"]),
                )
                conn.execute("DELETE FROM work_series WHERE series_id=?", (r["id"],))
                conn.execute("DELETE FROM series WHERE id=?", (r["id"],))
            if not seriesnorm.is_bare(keep["name"]):
                try:
                    conn.execute("UPDATE series SET name=? WHERE id=?",
                                 (seriesnorm.bare_form(keep["name"]), keep["id"]))
                except sqlite3.IntegrityError:
                    pass


def link_work_author(work_id: int, author_id: int):
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO work_authors (work_id, author_id) VALUES (?, ?)",
            (work_id, author_id),
        )


def link_work_series(work_id: int, series_id: int, position: str = None, source: str = "openlibrary"):
    """Link a work to a series. Position priority: an 'audiobookdb' position
    replaces whatever is there; an 'openlibrary'/other position only fills a
    gap and never overwrites an audiobookdb one (see audiobookdb.py)."""
    with _lock, get_conn() as conn:
        if source == "audiobookdb":
            conn.execute(
                """INSERT INTO work_series (work_id, series_id, position, source) VALUES (?, ?, ?, ?)
                   ON CONFLICT(work_id, series_id) DO UPDATE
                   SET position=COALESCE(excluded.position, position),
                       source=CASE WHEN excluded.position IS NOT NULL THEN excluded.source ELSE source END""",
                (work_id, series_id, position, source),
            )
        else:
            conn.execute(
                """INSERT INTO work_series (work_id, series_id, position, source) VALUES (?, ?, ?, ?)
                   ON CONFLICT(work_id, series_id) DO UPDATE
                   SET position=CASE WHEN source='audiobookdb' AND position IS NOT NULL
                                     THEN position ELSE COALESCE(excluded.position, position) END""",
                (work_id, series_id, position, source),
            )


def author_by_id(author_id: int):
    """Returns the canonical author row: an id that was stored for a
    duplicate OL key (see aliases.py) resolves to the canonical row."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
        if row is not None:
            canon = aliases.canonical_key(row["ol_key"])
            if canon != row["ol_key"]:
                row = conn.execute("SELECT * FROM authors WHERE ol_key=?", (canon,)).fetchone() or row
        return row


def work_by_id(work_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()


def edition_by_id(edition_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM editions WHERE id=?", (edition_id,)).fetchone()


def edition_by_isbn(isbn: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM editions WHERE isbn13=? OR isbn10=?", (isbn, isbn)
        ).fetchone()


def edition_by_asin(asin: str):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM editions WHERE asin=?", (asin,)).fetchone()


def series_by_id(series_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()


def works_for_author(author_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT w.* FROM works w JOIN work_authors wa ON wa.work_id=w.id WHERE wa.author_id=?",
            (author_id,),
        ).fetchall()


def authors_for_work(work_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT a.* FROM authors a JOIN work_authors wa ON wa.author_id=a.id WHERE wa.work_id=?",
            (work_id,),
        ).fetchall()


def editions_for_work(work_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM editions WHERE work_id=?", (work_id,)).fetchall()


def works_sharing_author(work_id: int):
    """(id, title) of every other work linked to any author of this work."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT DISTINCT w.id, w.title FROM works w
               JOIN work_authors wa ON wa.work_id=w.id
               WHERE w.id != ? AND wa.author_id IN
                     (SELECT author_id FROM work_authors WHERE work_id=?)""",
            (work_id, work_id),
        ).fetchall()


def series_for_work(work_id: int):
    with get_conn() as conn:
        return conn.execute(
            """SELECT s.*, ws.position FROM series s
               JOIN work_series ws ON ws.series_id=s.id WHERE ws.work_id=?""",
            (work_id,),
        ).fetchall()


_UPDATABLE = {
    "authors": frozenset({"ol_key", "name", "description", "image_url", "fetched_at"}),
    "works": frozenset({"ol_key", "title", "description", "first_publish_year", "fetched_at",
                        "editions_fetched_at", "series_enrich_attempted_at",
                        "description_enrich_attempted_at", "audience_attempted_at", "languages"}),
    "editions": frozenset({"ol_key", "work_id", "title", "isbn13", "isbn10", "asin", "publisher",
                           "release_date", "num_pages", "image_url", "description", "fetched_at",
                           "enrich_attempted_at"}),
}


def _set_clause(table: str, fields: dict) -> str:
    """Build "a=?, b=?" for an UPDATE, rejecting any key not in the table's column allow-list."""
    bad = sorted(set(fields) - _UPDATABLE[table])
    if bad:
        raise ValueError(f"unknown {table} column(s): {', '.join(bad)}")
    return ", ".join(f"{k}=?" for k in fields)


def update_author(author_id: int, **fields):
    if not fields:
        return
    fields["fetched_at"] = now()
    # cols is checked against the per-table column allow-list in _set_clause
    cols = _set_clause("authors", fields)
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE authors SET {cols} WHERE id=?", (*fields.values(), author_id))  # nosec B608


def mark_work_description_enriched(work_id: int):
    """Record the Google Books attempt without touching fetched_at.

    update_work() stamps fetched_at, which is what _stale() uses to decide whether to
    re-read Open Library. Using it merely to note an enrichment attempt would keep
    pushing that clock forward and quietly stop the work from ever being refreshed.
    """
    with _lock, get_conn() as conn:
        conn.execute("UPDATE works SET description_enrich_attempted_at=? WHERE id=?", (now(), work_id))


def mark_work_audience_attempted(work_id: int):
    """Stamp the audience lookup without touching fetched_at (see mark_work_description_enriched)."""
    with _lock, get_conn() as conn:
        conn.execute("UPDATE works SET audience_attempted_at=? WHERE id=?", (now(), work_id))


def update_work(work_id: int, **fields):
    if not fields:
        return
    fields["fetched_at"] = now()
    # cols is checked against the per-table column allow-list in _set_clause
    cols = _set_clause("works", fields)
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE works SET {cols} WHERE id=?", (*fields.values(), work_id))  # nosec B608


def set_work_languages(work_id: int, languages):
    """Store language codes without touching fetched_at (update_work would
    mark the work fresh and skip its real metadata fetch)."""
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE works SET languages=? WHERE id=?", (",".join(languages), work_id)
        )


def mark_work_editions_fetched(work_id: int):
    with _lock, get_conn() as conn:
        conn.execute("UPDATE works SET editions_fetched_at=? WHERE id=?", (now(), work_id))


def update_edition(edition_id: int, **fields):
    if not fields:
        return
    fields["fetched_at"] = now()
    # cols is checked against the per-table column allow-list in _set_clause
    cols = _set_clause("editions", fields)
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE editions SET {cols} WHERE id=?", (*fields.values(), edition_id))  # nosec B608


def clear_author_cache(author_id: int):
    """Force a refresh: blank fetched_at on the author and all their works."""
    with _lock, get_conn() as conn:
        conn.execute("UPDATE authors SET fetched_at=0 WHERE id=?", (author_id,))
        conn.execute(
            "UPDATE works SET fetched_at=0, editions_fetched_at=0 WHERE id IN "
            "(SELECT work_id FROM work_authors WHERE author_id=?)",
            (author_id,),
        )


# ---- external-source enrichment cache and request budget -----------------

def enrichment_get(source: str, key: str):
    import json
    with get_conn() as conn:
        row = conn.execute(
            "SELECT found, payload, fetched_at FROM enrichment_cache WHERE source=? AND key=?",
            (source, key),
        ).fetchone()
    if row is None:
        return None
    return {"found": bool(row["found"]), "payload": json.loads(row["payload"] or "null"),
            "fetched_at": row["fetched_at"]}


def enrichment_put(source: str, key: str, found: bool, payload):
    import json
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO enrichment_cache (source, key, found, payload, fetched_at) VALUES (?,?,?,?,?)",
            (source, key, int(found), json.dumps(payload), time.time()),
        )


def usage_count(source: str, day: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT count FROM source_usage WHERE source=? AND day=?", (source, day)).fetchone()
    return row["count"] if row else 0


def usage_incr(source: str, day: str):
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO source_usage (source, day, count) VALUES (?, ?, 1)
               ON CONFLICT(source, day) DO UPDATE SET count=count+1""",
            (source, day),
        )
