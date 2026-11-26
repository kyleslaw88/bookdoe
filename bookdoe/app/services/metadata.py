from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..config import settings
from .. import db
from . import audience, observations, ownership, provider_results, seriesnorm

log = logging.getLogger(__name__)

PROVIDER = "openreads"


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": "bookdoe/0.1"}
    if settings.openreads_api_key:
        h["Authorization"] = f"Bearer {settings.openreads_api_key}"
    return h


def _client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(base_url=settings.openreads_url, headers=_headers(), timeout=timeout, follow_redirects=False)


def health() -> dict[str, Any]:
    try:
        with _client(5) as c:
            r = c.get("/api/v2/health")
            r.raise_for_status()
            return {"ok": True, **r.json()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    with _client(30) as c:
        r = c.get("/api/v2/search", params={"q": query, "limit": limit})
        r.raise_for_status()
        return r.json().get("items", [])


def get_work(metadata_id: str) -> dict[str, Any]:
    with _client(30) as c:
        r = c.get(f"/api/v2/works/{metadata_id}")
        r.raise_for_status()
        return r.json()


def fetch_audience(metadata_id: str) -> dict[str, Any]:
    """Ask OpenReads to look up (once, cached) and return a work's audience signals. This is
    the only provider traffic the audience backfill causes; the classifier never calls it."""
    with _client(40) as c:
        r = c.post(f"/api/v2/works/{metadata_id}/audience")
        r.raise_for_status()
        return r.json()


def resolve(*, identifiers: dict[str, str] | None = None, title: str | None = None, authors: list[str] | None = None) -> dict[str, Any]:
    with _client(30) as c:
        r = c.post(
            "/api/v2/resolve",
            json={"identifiers": identifiers or {}, "title": title, "authors": authors or []},
        )
        r.raise_for_status()
        return r.json()


def upsert_work(meta: dict[str, Any], *, monitored: bool = True) -> str:
    """Mirror the subset of OpenReads canonical metadata needed by bookdoe.

    OpenReads remains authoritative for bibliographic metadata; bookdoe keeps a
    local snapshot so metadata outages never block the library or download state.
    """
    mid = meta["id"]
    existing = db.one("SELECT id FROM works WHERE metadata_id=?", (mid,))
    work_id = existing["id"] if existing else db.uid()
    t = db.now()
    values = {
        "title": meta.get("title") or "Unknown",
        "subtitle": meta.get("subtitle"),
        "description": meta.get("description"),
        "first_publish_date": meta.get("first_publish_date"),
        "primary_language": meta.get("primary_language"),
        "cover_url": _best_cover(meta),
    }
    with db.tx() as conn:
        before = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone() if existing else None
        conn.execute(
            """INSERT INTO works(id,metadata_id,title,sort_title,subtitle,description,cover_url,first_publish_date,primary_language,monitored,metadata_status,metadata_source,added_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'resolved', 'provider', ?,?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title,sort_title=excluded.sort_title,subtitle=excluded.subtitle,
                 description=COALESCE(excluded.description,works.description),cover_url=COALESCE(excluded.cover_url,works.cover_url),
                 first_publish_date=COALESCE(excluded.first_publish_date,works.first_publish_date),
                 primary_language=COALESCE(excluded.primary_language,works.primary_language),
                 metadata_status='resolved',metadata_source='provider',updated_at=excluded.updated_at""",
            (
                work_id,
                mid,
                values["title"],
                _sort_title(values["title"]),
                values["subtitle"],
                values["description"],
                values["cover_url"],
                values["first_publish_date"],
                values["primary_language"],
                int(monitored),
                t,
                t,
            ),
        )
        _apply_observations(conn, "work", work_id, values, before, t, overwrite=_WORK_OVERWRITE, extra={"title": ("sort_title",)})
        conn.execute("DELETE FROM work_contributors WHERE work_id=?", (work_id,))
        for i, c in enumerate(meta.get("contributors") or []):
            cmid = c.get("id")
            row = conn.execute("SELECT id FROM contributors WHERE metadata_id=?", (cmid,)).fetchone() if cmid else None
            # Adopt the local author row this person already has before minting a new
            # one, or every linked work quietly adds a duplicate to the authors page.
            cid = row["id"] if row else (db.adopt_unlinked_contributor(conn, c.get("name") or "") or db.uid())
            conn.execute(
                """INSERT INTO contributors(id,metadata_id,name,sort_name,image_url,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,sort_name=excluded.sort_name,
                   metadata_id=COALESCE(excluded.metadata_id,contributors.metadata_id),
                   image_url=COALESCE(excluded.image_url,contributors.image_url),updated_at=excluded.updated_at""",
                (cid, cmid, c.get("name") or "Unknown", c.get("sort_name") or _person_sort(c.get("name") or ""), c.get("image_url"), t, t),
            )
            conn.execute(
                "INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                (work_id, cid, c.get("role") or "author", i),
            )
        for ident in meta.get("identifiers") or []:
            _upsert_identifier(conn, "work", work_id, ident)
        prior_series = {r["series_id"] for r in conn.execute("SELECT series_id FROM series_memberships WHERE work_id=?", (work_id,)).fetchall()}
        conn.execute("DELETE FROM series_memberships WHERE work_id=?", (work_id,))
        for s in meta.get("series") or []:
            _link_series(conn, work_id, s, t)
        joined_series = {r["series_id"] for r in conn.execute("SELECT series_id FROM series_memberships WHERE work_id=?", (work_id,)).fetchall()} - prior_series
        for e in meta.get("editions") or []:
            _upsert_edition(conn, work_id, e, t)
    try:
        # Subjects / categories / maturity that OpenReads already holds ride along with every sync.
        audience.store(work_id, meta.get("audience"))
    except Exception:
        log.warning("audience metadata for %s not stored", work_id, exc_info=True)
    _ensure_assets(work_id)
    if joined_series:
        # Assets exist now, so a series Follow policy can record intent on the new member.
        from . import series as series_service

        with db.tx() as conn:
            for sid in joined_series:
                series_service.apply_future_policy(conn, work_id, sid)
    return work_id


def sync_work(metadata_id: str) -> str:
    return upsert_work(get_work(metadata_id))


def _upsert_edition(conn, work_id: str, e: dict[str, Any], t: float) -> str:
    emid = e.get("id")
    row = conn.execute("SELECT id FROM editions WHERE metadata_id=?", (emid,)).fetchone() if emid else None
    eid = row["id"] if row else db.uid()
    before = conn.execute("SELECT * FROM editions WHERE id=?", (eid,)).fetchone() if row else None
    pages = e.get("num_pages") or e.get("pages")
    conn.execute(
        """INSERT INTO editions(id,work_id,metadata_id,title,publisher,release_date,language,pages,cover_url,duration_seconds,abridged,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET title=COALESCE(excluded.title,editions.title),publisher=COALESCE(excluded.publisher,editions.publisher),
             release_date=COALESCE(excluded.release_date,editions.release_date),language=COALESCE(excluded.language,editions.language),
             pages=COALESCE(excluded.pages,editions.pages),cover_url=COALESCE(excluded.cover_url,editions.cover_url),
             duration_seconds=COALESCE(excluded.duration_seconds,editions.duration_seconds),abridged=COALESCE(excluded.abridged,editions.abridged),updated_at=excluded.updated_at""",
        (
            eid, work_id, emid, e.get("title"), e.get("publisher"), e.get("release_date"), e.get("language"), pages,
            e.get("image_url") or e.get("cover_url"), e.get("duration_seconds"), e.get("abridged"), t, t,
        ),
    )
    values = {
        "publisher": e.get("publisher"),
        "release_date": e.get("release_date"),
        "language": e.get("language"),
        "pages": pages,
        "duration": e.get("duration_seconds"),
        "abridged": e.get("abridged"),
    }
    _apply_observations(conn, "edition", eid, values, before, t, columns={"duration": "duration_seconds"})
    for ident in e.get("identifiers") or []:
        _upsert_identifier(conn, "edition", eid, ident)
    return eid


# Work fields the provider upsert overwrites even when the new value is empty; every
# other observed field only changes when the provider supplies a value.
_WORK_OVERWRITE = ("title", "subtitle")


def _apply_observations(conn, entity_type: str, entity_id: str, values: dict[str, Any], before, t: float, *,
                        overwrite: tuple[str, ...] = (), columns: dict[str, str] | None = None,
                        extra: dict[str, tuple[str, ...]] | None = None) -> None:
    """Record each provider value as an observation and honour field locks.

    The canonical upsert has already run; a locked field is put back to what it held
    before (``before``), so the manual value survives the refresh, and the provider's
    value is kept only as a non-current observation.
    """
    table = "works" if entity_type == "work" else "editions"
    for field, value in values.items():
        column = (columns or {}).get(field, field)
        locked = before is not None and observations.is_locked(conn, entity_type, entity_id, field)
        if locked:
            for col in (column, *(extra or {}).get(field, ())):
                conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (before[col], entity_id))  # nosec B608
        applied = not locked and (value is not None or field in overwrite)
        observations.record(conn, entity_type, entity_id, field, value, "provider", applied=applied, t=t)


# Work fields with an observation trail that the field API can act on (Part XXI).
FIELD_ACTION_FIELDS = ("title", "subtitle", "description", "first_publish_date", "primary_language", "cover_url")


class FieldError(Exception):
    """A field action that cannot be applied; ``status`` is the HTTP status to report."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _check_field(work_id: str, field: str) -> dict[str, Any]:
    work = db.one("SELECT id,title FROM works WHERE id=?", (work_id,))
    if not work:
        raise FieldError(404, "Work not found")
    if field not in FIELD_ACTION_FIELDS:
        raise FieldError(404, f"Unknown metadata field: {field}")
    return work


def _write_work_value(conn, work_id: str, field: str, value: str | None, t: float) -> None:
    if field == "title":
        conn.execute("UPDATE works SET title=?,sort_title=?,updated_at=? WHERE id=?", (value, _sort_title(value or ""), t, work_id))
    else:
        conn.execute(f"UPDATE works SET {field}=?,updated_at=? WHERE id=?", (value, t, work_id))  # nosec B608


def _field_audit(action: str, work: dict[str, Any], field: str, **data: Any) -> None:
    db.add_history(
        f"metadata.field_{action}",
        f"Metadata field {field} of {work['title']}: {action.replace('_', ' ')}",
        work_id=work["id"],
        data={"entity_type": "work", "field": field, "action": action, **data},
    )


def get_field(work_id: str, field: str) -> dict[str, Any]:
    """Current value, lock state and every observation for one work field."""
    _check_field(work_id, field)
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    obs = db.all(
        """SELECT id,value,source,confidence,is_current,observed_at FROM metadata_observations
           WHERE entity_type='work' AND entity_id=? AND field_name=? ORDER BY observed_at DESC, rowid DESC""",
        (work_id, field),
    )
    current = next((o for o in obs if o["is_current"]), None)
    lock = db.one(
        "SELECT locked_at,locked_by FROM metadata_field_locks WHERE entity_type='work' AND entity_id=? AND field_name=?",
        (work_id, field),
    )
    return {
        "entity_type": "work",
        "entity_id": work_id,
        "field": field,
        "value": work[field],
        "source": current["source"] if current else None,
        "current_observation_id": current["id"] if current else None,
        "locked": lock is not None,
        "lock": lock,
        "observations": [{**o, "is_current": bool(o["is_current"])} for o in obs],
    }


def choose_observation(work_id: str, field: str, observation_id: str) -> dict[str, Any]:
    """Make an existing observation the current one and apply its value to the work."""
    work = _check_field(work_id, field)
    t = db.now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id,value,source FROM metadata_observations WHERE id=? AND entity_type='work' AND entity_id=? AND field_name=?",
            (observation_id, work_id, field),
        ).fetchone()
        if not row:
            raise FieldError(404, "Observation not found for this field")
        conn.execute(
            "UPDATE metadata_observations SET is_current=0 WHERE entity_type='work' AND entity_id=? AND field_name=? AND is_current=1",
            (work_id, field),
        )
        conn.execute("UPDATE metadata_observations SET is_current=1 WHERE id=?", (observation_id,))
        _write_work_value(conn, work_id, field, row["value"], t)
    _field_audit("chosen", work, field, observation_id=observation_id, value=row["value"], source=row["source"])
    return get_field(work_id, field)


def set_manual(work_id: str, field: str, value: str, locked_by: str | None = None) -> dict[str, Any]:
    """Record a user-supplied value as a ``manual`` observation, apply it and lock the field
    so provider refreshes cannot overwrite it."""
    work = _check_field(work_id, field)
    if field == "title" and not value.strip():
        raise FieldError(400, "Title cannot be empty")
    t = db.now()
    with db.tx() as conn:
        observations.record(conn, "work", work_id, field, value, "manual", applied=True, t=t)
        _write_work_value(conn, work_id, field, value, t)
        conn.execute(
            "INSERT OR REPLACE INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at,locked_by) VALUES ('work',?,?,?,?)",
            (work_id, field, t, locked_by or "manual"),
        )
    _field_audit("manual", work, field, value=value, locked=True)
    return get_field(work_id, field)


def restore_automatic(work_id: str, field: str) -> dict[str, Any]:
    """Unlock the field and re-select the highest-confidence non-manual observation
    (newest wins a tie).  Never fetches from a provider; with no automatic
    observation the value is left as it is and only the lock is removed."""
    work = _check_field(work_id, field)
    t = db.now()
    with db.tx() as conn:
        conn.execute("DELETE FROM metadata_field_locks WHERE entity_type='work' AND entity_id=? AND field_name=?", (work_id, field))
        best = conn.execute(
            """SELECT id,value,source FROM metadata_observations
               WHERE entity_type='work' AND entity_id=? AND field_name=? AND COALESCE(source,'')!='manual'
               ORDER BY confidence DESC, observed_at DESC, rowid DESC LIMIT 1""",
            (work_id, field),
        ).fetchone()
        if best:
            conn.execute(
                "UPDATE metadata_observations SET is_current=0 WHERE entity_type='work' AND entity_id=? AND field_name=? AND is_current=1",
                (work_id, field),
            )
            conn.execute("UPDATE metadata_observations SET is_current=1 WHERE id=?", (best["id"],))
            _write_work_value(conn, work_id, field, best["value"], t)
    _field_audit("restored", work, field, observation_id=best["id"] if best else None, value=best["value"] if best else None, source=best["source"] if best else None)
    return get_field(work_id, field)


def set_lock(work_id: str, field: str, locked: bool, locked_by: str | None = None) -> dict[str, Any]:
    """Add or remove the field lock without touching the current value."""
    work = _check_field(work_id, field)
    with db.tx() as conn:
        if locked:
            conn.execute(
                "INSERT OR IGNORE INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at,locked_by) VALUES ('work',?,?,?,?)",
                (work_id, field, db.now(), locked_by or "manual"),
            )
        else:
            conn.execute("DELETE FROM metadata_field_locks WHERE entity_type='work' AND entity_id=? AND field_name=?", (work_id, field))
    _field_audit("locked" if locked else "unlocked", work, field)
    return get_field(work_id, field)


def _link_series(conn, work_id: str, s: dict[str, Any], t: float) -> None:
    """Attach a work to the series OpenReads reported, normalizing and deduping
    on bookdoe's own side.

    OpenReads runs its own series normalizer before minting an id, but that
    normalizer only re-converges existing duplicates when the OpenReads process
    restarts, and it does not recognise every raw catalog string (spelled-out
    position words, non-English imprint statements). Matching by name here too -
    not just by OpenReads' metadata_id - means two books that OpenReads currently
    treats as different series still end up on one row in bookdoe whenever
    their names actually agree, and a pure classification/imprint string never
    creates a row at all.
    """
    raw_name = (s.get("name") or "").strip()
    if not raw_name:
        return
    # One raw string can carry several memberships ("A - 1, B - 45; C") or junk (a
    # publisher line, a catalog number as position): split and vet it first.
    opts = seriesnorm.load_options(conn)
    cands = seriesnorm.parse_series(raw_name, ignored=opts.ignored, max_position=opts.max_position)
    for cand in cands:
        if cand.ignored:
            # Record the decision as a rejected assertion so the evidence trail is
            # complete even for input that never reaches a series row.
            _insert_series_assertion(conn, work_id, None, raw_name, cand.name, None, "free_text", 0.0, "rejected", t)
            continue
        position = cand.position
        if len(cands) == 1:
            given = s.get("position")
            if given is not None and str(given).strip() and not seriesnorm.position_problem(given, opts.max_position):
                position = seriesnorm.normalize_position(str(given))
        _link_one_series(conn, work_id, raw_name, cand.name, position, s.get("id") if len(cands) == 1 else None, t)


def _link_one_series(conn, work_id: str, raw_name: str, name: str, position: str | None, smid: str | None, t: float) -> None:
    """Link `work_id` to one cleaned, vetted series candidate (find or create the row)."""
    key = seriesnorm.norm_key(name)

    smid = smid or None
    row = conn.execute("SELECT id, name FROM series WHERE metadata_id=?", (smid,)).fetchone() if smid else None
    if not row:
        for candidate in conn.execute("SELECT id, name, norm_key FROM series").fetchall():
            if candidate["norm_key"] and seriesnorm.similar(key, candidate["norm_key"]):
                row = candidate
                break

    if row:
        sid = row["id"]
        new_name = row["name"]
        if not seriesnorm.is_bare(row["name"]) and seriesnorm.is_bare(name):
            new_name = name
        conn.execute(
            """UPDATE series SET name=?,sort_name=?,norm_key=?,metadata_id=COALESCE(metadata_id,?),updated_at=? WHERE id=?""",
            (new_name, _sort_title(new_name), key, smid, t, sid),
        )
    else:
        sid = db.uid()
        conn.execute(
            "INSERT INTO series(id,metadata_id,name,sort_name,norm_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (sid, smid, name, _sort_title(name), key, t, t),
        )

    # Count evidence from OTHER works before this work's own membership row
    # lands, so a first-time link never counts itself as its own corroboration.
    other_memberships = conn.execute(
        "SELECT COUNT(*) c FROM series_memberships WHERE series_id=? AND work_id!=?", (sid, work_id)
    ).fetchone()["c"]

    # Check for a manual suppression before inserting membership.
    suppressed = conn.execute(
        "SELECT id FROM series_assertions WHERE work_id=? AND series_id=? AND status='manually_suppressed'",
        (work_id, sid),
    ).fetchone()

    if suppressed:
        # User explicitly suppressed this series for this work; record as rejected
        # and do NOT recreate the membership.
        _insert_series_assertion(
            conn, work_id, sid, raw_name, name, position,
            "free_text", 0.0, "rejected", t, source="openreads",
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
            (work_id, sid, position),
        )
        evidence_type, confidence, status = _score_series_link(name, position, other_memberships)
        _insert_series_assertion(conn, work_id, sid, raw_name, name, position, evidence_type, confidence, status, t, source="openreads")


_ORDINAL = re.compile(r"^\d+(\.\d+)?$")


def _score_series_link(name: str, position: str | None, other_memberships: int) -> tuple[str, float, str]:
    """Confidence scoring for a non-junk series candidate, per Part XV's signal list.

    Weights and status thresholds are this task's placeholder judgment call (Part
    XV explicitly leaves exact numbers to implementation) - see the P2-02 report
    for the reasoning behind the specific values chosen.
    """
    confidence = 0.6
    has_ordinal = bool(position) and bool(_ORDINAL.match(str(position).strip()))
    if has_ordinal:
        confidence += 0.25
    else:
        confidence -= 0.1

    if other_memberships >= 1:
        confidence += 0.2
    else:
        confidence -= 0.05

    if seriesnorm.is_bare(name):
        confidence -= 0.1

    # round() protects the threshold comparisons below from binary float
    # artifacts (e.g. 0.6 + 0.25 - 0.05 landing on 0.7999999999999999).
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    if has_ordinal:
        evidence_type = "explicit_position"
    elif other_memberships >= 1:
        evidence_type = "repeat_membership"
    else:
        evidence_type = "free_text"

    if confidence >= 0.8:
        status = "auto_accepted"
    elif confidence >= 0.4:
        status = "uncertain"
    else:
        status = "rejected"
    return evidence_type, confidence, status


def _insert_series_assertion(
    conn, work_id: str, series_id: str | None, raw_value: str, normalized_name: str,
    proposed_position: str | None, evidence_type: str, confidence: float, status: str, t: float,
    source: str = "openreads",
) -> None:
    conn.execute(
        """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
           proposed_position,evidence_type,confidence,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (db.uid(), work_id, series_id, source, raw_value, normalized_name,
         proposed_position, evidence_type, confidence, status, t, t),
    )


def _upsert_identifier(conn, entity_type: str, entity_id: str, ident: dict[str, Any]):
    ns, value = ident.get("namespace"), ident.get("value")
    if not ns or not value:
        return
    conn.execute(
        """INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence)
           VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity_type,namespace,value) DO UPDATE SET entity_id=excluded.entity_id,
           source=excluded.source,verified=MAX(identifiers.verified,excluded.verified),confidence=MAX(identifiers.confidence,excluded.confidence)""",
        (entity_type, entity_id, ns, str(value), ident.get("source"), int(bool(ident.get("verified"))), float(ident.get("confidence", 1.0))),
    )


def _ensure_assets(work_id: str) -> None:
    """Ensure media_assets exist for a work WITHOUT creating wanted/monitored state.

    Per the core product rule (Part XI of the spec): knowing about a book is NOT
    the same as owning it, wanting it, or following it. A Work entering the Catalog
    must NOT automatically create wanted ebook and audiobook state.

    This function creates placeholder assets so the UI can display format slots
    (e.g., 'Ebook ○' / 'Audio ○') without implying acquisition intent. Existing
    assets are preserved untouched.

    For explicit acquisition intent (the user wants a specific medium), use
    the monitor_work() API endpoint or the Follow policy automation — not this
    catalog-discovery path.
    """
    for kind, profile_id in (("ebook", "ebook-default"), ("audiobook", "audio-default")):
        existing = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL", (work_id, kind))
        if existing:
            continue
        with db.tx() as conn:
            # Creation guard (services/ownership.py): never a second slot of a kind the Work already owns or has.
            if not ownership.may_create_placeholder(conn, work_id, kind):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO media_assets(id,work_id,kind,status,monitored,profile_id,added_at,updated_at) VALUES (?,?,?,'none',0,?,?,?)",
                (db.uid(), work_id, kind, profile_id, db.now(), db.now()),
            )


def _sort_title(title: str) -> str:
    lower = title.casefold()
    for article in ("the ", "a ", "an "):
        if lower.startswith(article):
            return title[len(article):] + ", " + title[: len(article) - 1]
    return title


def _person_sort(name: str) -> str:
    parts = name.strip().split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name


def _best_cover(meta: dict[str, Any]) -> str | None:
    """Pick the cover most likely to be the one a reader recognises.

    OpenReads returns editions in no useful order, so taking the first one with art
    gives whichever translation happens to lead - a Dune search returned the Estonian
    "Duin" jacket. Prefer an edition in the work's own language whose title still
    matches the work's title, and only then fall back to any art at all.
    """
    editions = [e for e in (meta.get("editions") or []) if e.get("image_url") or e.get("cover_url")]

    def art(edition: dict[str, Any]) -> str:
        return edition.get("image_url") or edition.get("cover_url")

    work_title = (meta.get("title") or "").casefold().strip()
    preferred_language = (meta.get("primary_language") or "").lower()

    def rank(edition: dict[str, Any]) -> tuple[int, int]:
        language = (edition.get("language") or "").lower()
        if preferred_language and language:
            language_rank = 0 if language.startswith(preferred_language[:3]) else 2
        elif not language:
            language_rank = 1  # Untagged is the common case and must not be penalised.
        else:
            language_rank = 0 if language.startswith("en") else 2
        title_rank = 0 if (edition.get("title") or "").casefold().strip() == work_title else 1
        return (language_rank, title_rank)

    if editions:
        return art(sorted(editions, key=rank)[0])
    return meta.get("image_url") or meta.get("cover_url")


def get_contributor(metadata_id: str, *, refresh: bool = False) -> dict[str, Any]:
    with _client(90 if refresh else 30) as c:
        r = c.get(f"/api/v2/contributors/{metadata_id}", params={"refresh": str(refresh).lower()})
        r.raise_for_status()
        return r.json()


def contributor_works(metadata_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
    with _client(120 if refresh else 30) as c:
        r = c.get(f"/api/v2/contributors/{metadata_id}/works", params={"refresh": str(refresh).lower()})
        r.raise_for_status()
        return r.json().get("items", [])


def _work_identifiers(work_id: str) -> dict[str, str]:
    rows = db.all("SELECT namespace,value FROM identifiers WHERE entity_type='work' AND entity_id=?", (work_id,))
    return {r["namespace"]: str(r["value"]) for r in rows if r.get("namespace") and r.get("value")}


def _work_authors(work_id: str) -> list[str]:
    rows = db.all(
        """SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
           WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord""",
        (work_id,),
    )
    return [r["name"] for r in rows if r.get("name")]


def link_work(work_id: str, metadata_id: str) -> str:
    """Attach an existing local work to an OpenReads work and pull its metadata in.

    The metadata_id is written onto the local row *before* upsert_work runs, because
    upsert_work finds an existing work only by metadata_id. Calling it on an unlinked
    work would create a second work and strand the original's assets and files.
    """
    existing = db.one("SELECT id FROM works WHERE metadata_id=? AND id<>?", (metadata_id, work_id))
    from . import dupefinder
    if existing:
        # The moment it happens: the Duplicates page treats this pair as a first-class, highest-confidence candidate.
        dupefinder.record_refresh_conflict(work_id, existing["id"], metadata_id)
        raise ValueError(f"Another work is already linked to that metadata record ({existing['id']})")
    with db.tx() as conn:
        conn.execute("UPDATE works SET metadata_id=?,updated_at=? WHERE id=?", (metadata_id, db.now(), work_id))
    dupefinder.clear_refresh_conflict(work_id)
    return upsert_work(get_work(metadata_id))


def refresh_work(work_id: str, *, metadata_id: str | None = None) -> dict[str, Any]:
    """Refresh one work's metadata, classifying the provider's response (F-08).

    A rate-limited or transient provider answer returns that status with the scheduled
    retry time and changes nothing: the work keeps its data and no review task or "no match"
    is recorded.  An automatic refresh (no explicit metadata_id) is deferred while a retry
    is pending; a manual one with a metadata_id always goes through.
    """
    if metadata_id is None:
        waiting = provider_results.deferred(PROVIDER, "work", work_id)
        if waiting:
            return {"work_id": work_id, "status": waiting.status, "retry_at": waiting.retry_at}
    try:
        result = _refresh_work(work_id, metadata_id=metadata_id)
    except httpx.HTTPError as exc:
        failure = provider_results.classify_exception(exc, PROVIDER)
        if not failure.retryable:
            raise
        provider_results.record(PROVIDER, "work", work_id, failure)
        return {"work_id": work_id, "status": failure.status, "error": failure.error, "retry_at": failure.retry_at}
    provider_results.record(PROVIDER, "work", work_id, provider_results.ProviderResult(provider_results.SUCCESS, PROVIDER))
    return result


def _refresh_work(work_id: str, *, metadata_id: str | None = None) -> dict[str, Any]:
    """Refresh one work's metadata on demand.

    Three outcomes, all of them reported honestly to the caller: the work is linked
    and re-synced, the provider offers candidates but none confidently enough to pick
    (a review task), or nothing matches at all.
    """
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")

    target = metadata_id or work.get("metadata_id")
    if target:
        link_work(work_id, target)
        db.add_history("metadata.refreshed", f"Refreshed metadata for {work['title']}", work_id=work_id, data={"metadata_id": target})
        _resolve_review(work_id, resolution={"metadata_id": target})
        return {"work_id": work_id, "status": "linked", "metadata_id": target}

    result = resolve(identifiers=_work_identifiers(work_id), title=work.get("title"), authors=_work_authors(work_id))
    match = result.get("match")
    confidence = float(result.get("confidence") or 0)
    if match and confidence >= 0.95:
        link_work(work_id, match["id"])
        db.add_history(
            "metadata.refreshed", f"Matched {work['title']} to OpenReads",
            work_id=work_id, data={"metadata_id": match["id"], "confidence": confidence, "method": result.get("method")},
        )
        _resolve_review(work_id, resolution={"metadata_id": match["id"], "confidence": confidence})
        return {"work_id": work_id, "status": "linked", "metadata_id": match["id"], "confidence": confidence}

    candidates = [
        {
            "id": c.get("id"),
            "title": c.get("title"),
            "first_publish_date": c.get("first_publish_date"),
            "authors": [x.get("name") for x in (c.get("contributors") or []) if x.get("role") in (None, "author")],
            # Kept so review_triage can tell an exact identifier match from a fuzzy one later.
            "identifiers": [
                {"namespace": i.get("namespace"), "value": i.get("value")}
                for i in (c.get("identifiers") or []) if isinstance(i, dict) and i.get("value")
            ],
        }
        for c in (result.get("candidates") or [])
    ][:10]
    # The caller needs what we know about the work, not just what we failed to match
    # it to: the picker seeds its own search box from these so the operator can look
    # for the book themselves instead of choosing from five wrong answers.
    known = {
        "title": work.get("title"),
        "authors": _work_authors(work_id),
        "identifiers": [{"namespace": k, "value": v} for k, v in _work_identifiers(work_id).items()],
    }
    if candidates:
        _open_review(work_id, work["title"], candidates, confidence)
        return {"work_id": work_id, "status": "review", "candidates": candidates, "confidence": confidence, **known}
    # An *existing* open review task must not be left showing a stale candidate
    # list from before - e.g. a candidate a later fix disqualified. Only touch it
    # if one is already open; a work with no review history yet stays that way
    # rather than gaining one just because a refresh happened to find nothing.
    if db.one("SELECT id FROM review_tasks WHERE kind='metadata_match' AND entity_type='work' AND entity_id=? AND status='open'", (work_id,)):
        _open_review(work_id, work["title"], [], confidence)
    return {"work_id": work_id, "status": "unmatched", "candidates": [], "confidence": confidence, **known}


def _open_review(work_id: str, title: str, candidates: list[dict[str, Any]], confidence: float) -> None:
    existing = db.one("SELECT id FROM review_tasks WHERE kind='metadata_match' AND entity_type='work' AND entity_id=? AND status='open'", (work_id,))
    payload = db.json_dump({"candidates": candidates, "confidence": confidence})
    with db.tx() as conn:
        if existing:
            conn.execute("UPDATE review_tasks SET payload_json=?,title=? WHERE id=?", (payload, title, existing["id"]))
        else:
            conn.execute(
                """INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at)
                   VALUES (?,'metadata_match','work',?,?,?,'open',50,?)""",
                (db.uid(), work_id, title, payload, db.now()),
            )


def _resolve_review(work_id: str, *, resolution: dict[str, Any]) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE review_tasks SET status='resolved',resolved_at=?,resolution_json=? WHERE entity_type='work' AND entity_id=? AND status='open'",
            (db.now(), db.json_dump(resolution), work_id),
        )
