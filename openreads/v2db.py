"""Canonical v2 metadata store for OpenReads.

The original OpenReads tables remain the compatibility cache/ID authority for
Readarr.  These tables add provider-independent UUID identities, provenance,
field decisions, and identifier mappings without breaking the v1 contract.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Iterable

import db as legacy


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def init_db() -> None:
    with legacy._lock, legacy.get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2_contributors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sort_name TEXT,
                description TEXT,
                image_url TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_works (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                subtitle TEXT,
                description TEXT,
                first_publish_date TEXT,
                primary_language TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_editions (
                id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL REFERENCES v2_works(id) ON DELETE CASCADE,
                title TEXT,
                publisher TEXT,
                release_date TEXT,
                language TEXT,
                num_pages INTEGER,
                image_url TEXT,
                description TEXT,
                duration_seconds INTEGER,
                abridged INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_series (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sort_name TEXT,
                description TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v2_work_contributors (
                work_id TEXT NOT NULL REFERENCES v2_works(id) ON DELETE CASCADE,
                contributor_id TEXT NOT NULL REFERENCES v2_contributors(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'author',
                ord INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(work_id, contributor_id, role)
            );
            CREATE TABLE IF NOT EXISTS v2_series_memberships (
                work_id TEXT NOT NULL REFERENCES v2_works(id) ON DELETE CASCADE,
                series_id TEXT NOT NULL REFERENCES v2_series(id) ON DELETE CASCADE,
                position TEXT,
                source TEXT,
                PRIMARY KEY(work_id, series_id)
            );
            CREATE TABLE IF NOT EXISTS v2_identifiers (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(entity_type, namespace, value)
            );
            CREATE INDEX IF NOT EXISTS idx_v2_ident_entity
              ON v2_identifiers(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_v2_ident_value
              ON v2_identifiers(namespace, value);

            CREATE TABLE IF NOT EXISTS v2_metadata_observations (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                field TEXT NOT NULL,
                source TEXT NOT NULL,
                value_json TEXT,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_identifier TEXT,
                observed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_v2_obs_entity
              ON v2_metadata_observations(entity_type, entity_id, field);

            CREATE TABLE IF NOT EXISTS v2_metadata_decisions (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                field TEXT NOT NULL,
                value_json TEXT,
                selected_source TEXT,
                method TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                locked INTEGER NOT NULL DEFAULT 0,
                decided_at REAL NOT NULL,
                PRIMARY KEY(entity_type, entity_id, field)
            );

            CREATE TABLE IF NOT EXISTS v2_legacy_map (
                entity_type TEXT NOT NULL,
                legacy_id INTEGER NOT NULL,
                canonical_id TEXT NOT NULL,
                PRIMARY KEY(entity_type, legacy_id),
                UNIQUE(entity_type, canonical_id)
            );
            CREATE TABLE IF NOT EXISTS v2_llm_audit (
                id TEXT PRIMARY KEY,
                entity_type TEXT,
                entity_id TEXT,
                model TEXT,
                purpose TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT,
                accepted INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            """
        )


def _map_get(conn: sqlite3.Connection, entity_type: str, legacy_id: int) -> str | None:
    row = conn.execute(
        "SELECT canonical_id FROM v2_legacy_map WHERE entity_type=? AND legacy_id=?",
        (entity_type, legacy_id),
    ).fetchone()
    return row["canonical_id"] if row else None


def _map_put(conn: sqlite3.Connection, entity_type: str, legacy_id: int, canonical_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO v2_legacy_map(entity_type,legacy_id,canonical_id) VALUES (?,?,?)",
        (entity_type, legacy_id, canonical_id),
    )


def add_identifier(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    namespace: str,
    value: str | None,
    source: str,
    *,
    verified: bool = True,
    confidence: float = 1.0,
) -> None:
    if not value:
        return
    value = str(value).strip()
    if not value:
        return
    now = _now()
    conn.execute(
        """
        INSERT INTO v2_identifiers(entity_type,entity_id,namespace,value,source,verified,confidence,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(entity_type,namespace,value) DO UPDATE SET
          entity_id=excluded.entity_id, source=excluded.source, verified=MAX(v2_identifiers.verified,excluded.verified),
          confidence=MAX(v2_identifiers.confidence,excluded.confidence), updated_at=excluded.updated_at
        """,
        (entity_type, entity_id, namespace, value, source, int(verified), confidence, now, now),
    )


def observe(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    field: str,
    source: str,
    value: Any,
    *,
    confidence: float = 1.0,
    source_identifier: str | None = None,
) -> None:
    if value is None or value == "":
        return
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    # De-duplicate exact current observations so repeated syncs stay compact.
    row = conn.execute(
        """SELECT 1 FROM v2_metadata_observations
           WHERE entity_type=? AND entity_id=? AND field=? AND source=? AND value_json=?
           ORDER BY observed_at DESC LIMIT 1""",
        (entity_type, entity_id, field, source, payload),
    ).fetchone()
    if row:
        return
    conn.execute(
        """INSERT INTO v2_metadata_observations
           (id,entity_type,entity_id,field,source,value_json,confidence,source_identifier,observed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (_id(), entity_type, entity_id, field, source, payload, confidence, source_identifier, _now()),
    )


def decide(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    field: str,
    value: Any,
    source: str | None,
    method: str,
    *,
    confidence: float = 1.0,
    force: bool = False,
) -> None:
    existing = conn.execute(
        "SELECT locked FROM v2_metadata_decisions WHERE entity_type=? AND entity_id=? AND field=?",
        (entity_type, entity_id, field),
    ).fetchone()
    if existing and existing["locked"] and not force:
        return
    conn.execute(
        """
        INSERT INTO v2_metadata_decisions(entity_type,entity_id,field,value_json,selected_source,method,confidence,locked,decided_at)
        VALUES (?,?,?,?,?,?,?,COALESCE((SELECT locked FROM v2_metadata_decisions WHERE entity_type=? AND entity_id=? AND field=?),0),?)
        ON CONFLICT(entity_type,entity_id,field) DO UPDATE SET
          value_json=excluded.value_json, selected_source=excluded.selected_source,
          method=excluded.method, confidence=excluded.confidence, decided_at=excluded.decided_at
        """,
        (
            entity_type, entity_id, field, json.dumps(value, ensure_ascii=False), source, method, confidence,
            entity_type, entity_id, field, _now(),
        ),
    )


def sync_author(legacy_id: int) -> str | None:
    row = legacy.author_by_id(legacy_id)
    if row is None:
        return None
    with legacy._lock, legacy.get_conn() as conn:
        cid = _map_get(conn, "contributor", legacy_id) or _id()
        now = _now()
        conn.execute(
            """INSERT INTO v2_contributors(id,name,description,image_url,created_at,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                 description=COALESCE(excluded.description,v2_contributors.description),
                 image_url=COALESCE(excluded.image_url,v2_contributors.image_url),updated_at=excluded.updated_at""",
            (cid, row["name"] or "Unknown", row["description"], row["image_url"], now, now),
        )
        _map_put(conn, "contributor", legacy_id, cid)
        add_identifier(conn, "contributor", cid, "openlibrary_author", row["ol_key"], "openlibrary")
        for field in ("name", "description", "image_url"):
            observe(conn, "contributor", cid, field, "openlibrary", row[field], source_identifier=row["ol_key"])
            if row[field]:
                decide(conn, "contributor", cid, field, row[field], "openlibrary", "provider-precedence")
        return cid


def sync_series(legacy_id: int) -> str | None:
    with legacy.get_conn() as conn:
        row = conn.execute("SELECT * FROM series WHERE id=?", (legacy_id,)).fetchone()
    if row is None:
        return None
    with legacy._lock, legacy.get_conn() as conn:
        cid = _map_get(conn, "series", legacy_id) or _id()
        now = _now()
        conn.execute(
            """INSERT INTO v2_series(id,name,created_at,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at""",
            (cid, row["name"], now, now),
        )
        _map_put(conn, "series", legacy_id, cid)
        observe(conn, "series", cid, "name", "openreads-series-normalizer", row["name"])
        decide(conn, "series", cid, "name", row["name"], "openreads-series-normalizer", "normalized")
        return cid


def sync_edition(legacy_id: int) -> str | None:
    row = legacy.edition_by_id(legacy_id)
    if row is None:
        return None
    work_id = sync_work(row["work_id"]) if row["work_id"] else None
    if not work_id:
        return None
    with legacy._lock, legacy.get_conn() as conn:
        cid = _map_get(conn, "edition", legacy_id) or _id()
        now = _now()
        conn.execute(
            """INSERT INTO v2_editions
               (id,work_id,title,publisher,release_date,num_pages,image_url,description,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET work_id=excluded.work_id,title=COALESCE(excluded.title,v2_editions.title),
                 publisher=COALESCE(excluded.publisher,v2_editions.publisher),release_date=COALESCE(excluded.release_date,v2_editions.release_date),
                 num_pages=COALESCE(excluded.num_pages,v2_editions.num_pages),image_url=COALESCE(excluded.image_url,v2_editions.image_url),
                 description=COALESCE(excluded.description,v2_editions.description),updated_at=excluded.updated_at""",
            (
                cid, work_id, row["title"], row["publisher"], row["release_date"], row["num_pages"],
                row["image_url"], row["description"], now, now,
            ),
        )
        _map_put(conn, "edition", legacy_id, cid)
        add_identifier(conn, "edition", cid, "openlibrary_edition", row["ol_key"], "openlibrary")
        add_identifier(conn, "edition", cid, "isbn13", row["isbn13"], "openlibrary")
        add_identifier(conn, "edition", cid, "isbn10", row["isbn10"], "openlibrary")
        add_identifier(conn, "edition", cid, "asin", row["asin"], "openlibrary", verified=False, confidence=0.9)
        for field in ("title", "publisher", "release_date", "num_pages", "image_url", "description"):
            if row[field] is not None:
                # Legacy table may hold Google Books enrichment; mark provenance conservatively as aggregated.
                source = "openreads-v1-aggregate"
                observe(conn, "edition", cid, field, source, row[field], source_identifier=row["ol_key"])
                decide(conn, "edition", cid, field, row[field], source, "legacy-import")
        return cid


def sync_work(legacy_id: int) -> str | None:
    row = legacy.work_by_id(legacy_id)
    if row is None:
        return None
    with legacy._lock, legacy.get_conn() as conn:
        cid = _map_get(conn, "work", legacy_id) or _id()
        now = _now()
        language = None
        langs = (row["languages"] or "").split(",") if "languages" in row.keys() else []
        langs = [x for x in langs if x]
        if len(langs) == 1:
            language = langs[0]
        conn.execute(
            """INSERT INTO v2_works(id,title,description,first_publish_date,primary_language,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                 description=COALESCE(excluded.description,v2_works.description),
                 first_publish_date=COALESCE(excluded.first_publish_date,v2_works.first_publish_date),
                 primary_language=COALESCE(excluded.primary_language,v2_works.primary_language),updated_at=excluded.updated_at""",
            (cid, row["title"] or "Unknown", row["description"], row["first_publish_year"], language, now, now),
        )
        _map_put(conn, "work", legacy_id, cid)
        add_identifier(conn, "work", cid, "openlibrary_work", row["ol_key"], "openlibrary")
        for field in ("title", "description", "first_publish_year"):
            if row[field] is not None:
                canonical_field = "first_publish_date" if field == "first_publish_year" else field
                observe(conn, "work", cid, canonical_field, "openlibrary", row[field], source_identifier=row["ol_key"])
                decide(conn, "work", cid, canonical_field, row[field], "openlibrary", "provider-precedence")

    # Child syncs use their own connections intentionally to keep lock windows small.
    for index, a in enumerate(legacy.authors_for_work(legacy_id)):
        aid = sync_author(a["id"])
        if aid:
            with legacy._lock, legacy.get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO v2_work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                    (cid, aid, "author", index),
                )
    for s in legacy.series_for_work(legacy_id):
        sid = sync_series(s["id"])
        if sid:
            with legacy._lock, legacy.get_conn() as conn:
                conn.execute(
                    """INSERT INTO v2_series_memberships(work_id,series_id,position,source) VALUES (?,?,?,?)
                       ON CONFLICT(work_id,series_id) DO UPDATE SET position=COALESCE(excluded.position,v2_series_memberships.position),
                         source=COALESCE(excluded.source,v2_series_memberships.source)""",
                    (cid, sid, s["position"], s["source"] if "source" in s.keys() else None),
                )
    return cid


# Providers and fields that carry audience evidence (kids classifier). Only the latest
# observation per provider and field is exposed, so a corrected provider answer replaces
# the old one instead of piling up beside it.
AUDIENCE_SOURCES = ("openlibrary", "google_books")
AUDIENCE_FIELDS = ("subjects", "subject_places", "categories", "maturity_rating", "age_range",
                   "audience", "language", "publisher", "num_pages", "series")


def audience_for_work(work_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """[{provider, field, value, observed_at}] of the latest audience observations on the
    work and its editions."""
    def run(c):
        marks = ",".join("?" * len(AUDIENCE_FIELDS))
        smarks = ",".join("?" * len(AUDIENCE_SOURCES))
        # marks/smarks are only "?" placeholders, never user input
        rows = c.execute(
            f"""SELECT source,field,value_json,observed_at FROM v2_metadata_observations
                WHERE field IN ({marks}) AND source IN ({smarks}) AND (
                  (entity_type='work' AND entity_id=?) OR
                  (entity_type='edition' AND entity_id IN (SELECT id FROM v2_editions WHERE work_id=?)))
                ORDER BY observed_at DESC, rowid DESC""",  # nosec B608
            (*AUDIENCE_FIELDS, *AUDIENCE_SOURCES, work_id, work_id),
        ).fetchall()
        seen, out = set(), []
        for r in rows:
            key = (r["source"], r["field"])
            if key in seen:
                continue
            seen.add(key)
            try:
                value = json.loads(r["value_json"])
            except Exception:
                value = r["value_json"]
            out.append({"provider": r["source"], "field": r["field"], "value": value, "observed_at": r["observed_at"]})
        return out
    if conn is not None:
        return run(conn)
    with legacy.get_conn() as c:
        return run(c)


def canonical_work(work_id: str) -> dict[str, Any] | None:
    with legacy.get_conn() as conn:
        w = conn.execute("SELECT * FROM v2_works WHERE id=?", (work_id,)).fetchone()
        if not w:
            return None
        contributors = conn.execute(
            """SELECT c.*, wc.role, wc.ord FROM v2_work_contributors wc
               JOIN v2_contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? ORDER BY wc.ord,c.name""",
            (work_id,),
        ).fetchall()
        series = conn.execute(
            """SELECT s.*, sm.position, sm.source FROM v2_series_memberships sm
               JOIN v2_series s ON s.id=sm.series_id WHERE sm.work_id=? ORDER BY s.name""",
            (work_id,),
        ).fetchall()
        identifiers = conn.execute(
            "SELECT namespace,value,source,verified,confidence FROM v2_identifiers WHERE entity_type='work' AND entity_id=?",
            (work_id,),
        ).fetchall()
        return {
            **dict(w),
            "contributors": [dict(x) for x in contributors],
            "series": [dict(x) for x in series],
            "identifiers": [dict(x) for x in identifiers],
            "audience": audience_for_work(work_id, conn),
        }


def editions_for_work(work_id: str) -> list[dict[str, Any]]:
    with legacy.get_conn() as conn:
        rows = conn.execute("SELECT * FROM v2_editions WHERE work_id=? ORDER BY release_date,title", (work_id,)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["identifiers"] = [dict(x) for x in conn.execute(
                "SELECT namespace,value,source,verified,confidence FROM v2_identifiers WHERE entity_type='edition' AND entity_id=?",
                (row["id"],),
            ).fetchall()]
            out.append(d)
        return out


def metadata(entity_type: str, entity_id: str) -> dict[str, Any]:
    with legacy.get_conn() as conn:
        observations = [dict(x) for x in conn.execute(
            "SELECT * FROM v2_metadata_observations WHERE entity_type=? AND entity_id=? ORDER BY field,observed_at DESC",
            (entity_type, entity_id),
        ).fetchall()]
        decisions = [dict(x) for x in conn.execute(
            "SELECT * FROM v2_metadata_decisions WHERE entity_type=? AND entity_id=? ORDER BY field",
            (entity_type, entity_id),
        ).fetchall()]
    for row in observations:
        try:
            row["value"] = json.loads(row.pop("value_json"))
        except Exception:
            row["value"] = row.pop("value_json")
    for row in decisions:
        try:
            row["value"] = json.loads(row.pop("value_json"))
        except Exception:
            row["value"] = row.pop("value_json")
        row["locked"] = bool(row["locked"])
    return {"observations": observations, "decisions": decisions}


def set_decision(entity_type: str, entity_id: str, field: str, value: Any, *, lock: bool, source: str = "user", method: str = "manual", confidence: float = 1.0) -> None:
    with legacy._lock, legacy.get_conn() as conn:
        decide(conn, entity_type, entity_id, field, value, source, method, confidence=confidence, force=True)
        conn.execute(
            "UPDATE v2_metadata_decisions SET locked=? WHERE entity_type=? AND entity_id=? AND field=?",
            (int(lock), entity_type, entity_id, field),
        )


def find_identifier(namespace: str, value: str) -> dict[str, Any] | None:
    with legacy.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM v2_identifiers WHERE namespace=? AND value=? ORDER BY verified DESC,confidence DESC LIMIT 1",
            (namespace, value),
        ).fetchone()
        return dict(row) if row else None


def search_local(q: str, limit: int = 20) -> list[dict[str, Any]]:
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with legacy.get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT w.* FROM v2_works w
               LEFT JOIN v2_work_contributors wc ON wc.work_id=w.id
               LEFT JOIN v2_contributors c ON c.id=wc.contributor_id
               WHERE w.title LIKE ? COLLATE NOCASE OR c.name LIKE ? COLLATE NOCASE
               ORDER BY CASE WHEN w.title LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,w.title LIMIT ?""",
            (like, like, q + "%", limit),
        ).fetchall()
        return [canonical_work(r["id"]) for r in rows]


def observe_legacy(entity_type: str, legacy_id: int, source: str, fields: dict[str, Any], *, confidence: float = 1.0) -> None:
    """Record a provider's raw field observations against the canonical entity.

    Called opportunistically by the legacy enrichment pipeline. It deliberately
    degrades to a no-op when v2 tables have not been initialized (e.g. isolated
    unit tests of v1 compatibility behavior).
    """
    try:
        if entity_type == "work":
            cid = sync_work(legacy_id)
        elif entity_type == "edition":
            cid = sync_edition(legacy_id)
        elif entity_type == "contributor":
            cid = sync_author(legacy_id)
        elif entity_type == "series":
            cid = sync_series(legacy_id)
        else:
            return
        if not cid:
            return
        with legacy._lock, legacy.get_conn() as conn:
            for field, value in fields.items():
                if value not in (None, "", [], {}):
                    observe(conn, entity_type, cid, field, source, value, confidence=confidence)
    except sqlite3.OperationalError:
        return


def record_llm_audit(entity_type: str, entity_id: str, model: str, purpose: str, input_data: Any, output_data: Any) -> str:
    aid = _id()
    with legacy._lock, legacy.get_conn() as conn:
        conn.execute(
            "INSERT INTO v2_llm_audit(id,entity_type,entity_id,model,purpose,input_json,output_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, entity_type, entity_id, model, purpose, json.dumps(input_data, ensure_ascii=False), json.dumps(output_data, ensure_ascii=False), _now()),
        )
    return aid

def mark_llm_audit_accepted(audit_id: str) -> None:
    with legacy._lock, legacy.get_conn() as conn:
        conn.execute("UPDATE v2_llm_audit SET accepted=1 WHERE id=?", (audit_id,))


def canonical_contributor(contributor_id: str) -> dict[str, Any] | None:
    with legacy.get_conn() as conn:
        c = conn.execute("SELECT * FROM v2_contributors WHERE id=?", (contributor_id,)).fetchone()
        if not c:
            return None
        identifiers = [dict(x) for x in conn.execute(
            "SELECT namespace,value,source,verified,confidence FROM v2_identifiers WHERE entity_type='contributor' AND entity_id=?",
            (contributor_id,),
        ).fetchall()]
        works = [dict(x) for x in conn.execute(
            """SELECT w.*,wc.role,wc.ord FROM v2_work_contributors wc
               JOIN v2_works w ON w.id=wc.work_id WHERE wc.contributor_id=? ORDER BY w.first_publish_date DESC,w.title""",
            (contributor_id,),
        ).fetchall()]
        return {**dict(c), "identifiers": identifiers, "works": works}


def works_for_contributor(contributor_id: str) -> list[dict[str, Any]]:
    with legacy.get_conn() as conn:
        rows = conn.execute(
            """SELECT w.id FROM v2_work_contributors wc JOIN v2_works w ON w.id=wc.work_id
               WHERE wc.contributor_id=? ORDER BY w.first_publish_date DESC,w.title""",
            (contributor_id,),
        ).fetchall()
    return [canonical_work(x["id"]) for x in rows]
