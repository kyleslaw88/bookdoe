"""Edition selection for display (Part XXVII).

Today's fallback rule: bookdoe has no single "the edition" for a Work. Editions
are listed in `ORDER BY release_date,title` (work_detail) and the calendar considers
every edition's release date. The head of that ordering is therefore the fallback
"default edition". Work-level cover/metadata (works.cover_url etc.) is not derived
from editions at read time, so it is unaffected by the preference.
"""
from __future__ import annotations

from typing import Any

from .. import db

_COLUMNS = {"ebook": "preferred_ebook_edition_id", "audiobook": "preferred_audiobook_edition_id"}
EDITION_ORDER = "ORDER BY release_date,title"


def preferred_edition(work: dict[str, Any], kind: str, *, fallback: bool = True) -> dict[str, Any] | None:
    """Preferred edition of `kind` for a works row, else today's fallback.

    A stale preference (edition deleted or re-pointed to another work) is ignored.
    With fallback=False returns None when no valid preference is set.
    """
    col = _COLUMNS.get(kind)
    if col is None:
        raise ValueError(f"unknown edition kind: {kind}")
    eid = work.get(col)
    if eid:
        row = db.one("SELECT * FROM editions WHERE id=? AND work_id=?", (eid, work["id"]))
        if row:
            return row
    if not fallback:
        return None
    return db.one(f"SELECT * FROM editions WHERE work_id=? {EDITION_ORDER} LIMIT 1", (work["id"],))  # nosec B608


def ordered_editions(work: dict[str, Any]) -> list[dict[str, Any]]:
    """All editions in today's order, with valid preferred ones (ebook, then audiobook) first."""
    rows = db.all(f"SELECT * FROM editions WHERE work_id=? {EDITION_ORDER}", (work["id"],))  # nosec B608
    front = []
    for kind in ("ebook", "audiobook"):
        p = preferred_edition(work, kind, fallback=False)
        if p and all(p["id"] != f["id"] for f in front):
            front.append(p)
    ids = {f["id"] for f in front}
    return front + [r for r in rows if r["id"] not in ids]
