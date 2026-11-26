"""Shared setup for the owned / wanted / discovered distinction.

vNext Phase 1 split three things the old schema conflated: the Library lists only
*owned* works, the Wanted list needs *explicit acquisition intent*, and catalog
discovery implies neither. A test that needs a work to show up in one of those
places now has to say which, and these are the two ways to say it.
"""
from app import db


def _slot(work_id: str, kind: str) -> str | None:
    """The one asset a work owns for this kind, preferring a format-less slot.

    Mirrors migration._asset_for_migration: a Work owns at most one asset per kind,
    so tests must reuse the slot catalog discovery already made rather than insert a
    second one and trip UNIQUE(work_id, kind, format).
    """
    row = db.one(
        "SELECT id FROM media_assets WHERE work_id=? AND kind=? ORDER BY (format IS NOT NULL), added_at LIMIT 1",
        (work_id, kind),
    )
    return row["id"] if row else None


def own(work_id: str, kind: str = "ebook", fmt: str = "epub") -> str:
    """Give the work an owned asset, so it is visible in the Library."""
    aid, t = _slot(work_id, kind), db.now()
    with db.tx() as conn:
        if aid:
            conn.execute(
                "UPDATE media_assets SET status='owned',format=COALESCE(format,?),updated_at=? WHERE id=?",
                (fmt, t, aid),
            )
        else:
            aid = db.uid()
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at)"
                " VALUES (?,?,?,?,'owned',0,0,?,?)",
                (aid, work_id, kind, fmt, t, t),
            )
    return aid


def want(work_id: str, kind: str = "ebook") -> str:
    """Record explicit acquisition intent, as a user toggling the asset would."""
    aid, t = _slot(work_id, kind), db.now()
    with db.tx() as conn:
        if aid:
            conn.execute(
                "UPDATE media_assets SET status='missing',monitored=1,has_acquisition_intent=1,updated_at=? WHERE id=?",
                (t, aid),
            )
        else:
            aid = db.uid()
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at)"
                " VALUES (?,?,?,'missing',1,1,?,?)",
                (aid, work_id, kind, t, t),
            )
    return aid
