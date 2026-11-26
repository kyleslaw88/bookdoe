"""Per-field metadata evidence trail and lock check (Part XXI).

Ingest paths call ``record`` for every value they see and ``is_locked`` before
writing a canonical column, so a manual decision survives provider refreshes
while the evidence is never lost.  ``source`` reuses the ``metadata_source``
vocabulary (``provider`` / ``local`` / ``manual``).
"""
from __future__ import annotations

from typing import Any

from .. import db

CONFIDENCE = {"provider": 1.0, "local": 0.5, "file-tags": 0.5, "manual": 1.0, "openlibrary": 1.0, "google_books": 0.9}


def is_locked(conn, entity_type: str, entity_id: str, field: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM metadata_field_locks WHERE entity_type=? AND entity_id=? AND field_name=?",
        (entity_type, entity_id, field),
    ).fetchone() is not None


def record(conn, entity_type: str, entity_id: str, field: str, value: Any, source: str, *, applied: bool, t: float) -> None:
    """Insert an observation of ``value``; when ``applied`` it becomes the one
    ``is_current`` row for the field and the previous current row is flipped to 0.

    A ``None`` value is not evidence, so nothing is inserted, but if it was applied
    (the column was overwritten with NULL) the stale current flag is cleared.  A
    value identical to the latest observation from the same source is not
    re-inserted, so repeated refreshes do not bloat the trail.
    """
    if value is None:
        if applied:
            conn.execute(
                "UPDATE metadata_observations SET is_current=0 WHERE entity_type=? AND entity_id=? AND field_name=? AND is_current=1",
                (entity_type, entity_id, field),
            )
        return
    text = str(value)
    latest = conn.execute(
        """SELECT id,value,source,is_current FROM metadata_observations
           WHERE entity_type=? AND entity_id=? AND field_name=? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
        (entity_type, entity_id, field),
    ).fetchone()
    duplicate = latest is not None and latest["value"] == text and latest["source"] == source
    if applied:
        if duplicate and latest["is_current"]:
            return
        conn.execute(
            "UPDATE metadata_observations SET is_current=0 WHERE entity_type=? AND entity_id=? AND field_name=? AND is_current=1",
            (entity_type, entity_id, field),
        )
        if duplicate:
            conn.execute("UPDATE metadata_observations SET is_current=1 WHERE id=?", (latest["id"],))
            return
    elif duplicate:
        return
    conn.execute(
        """INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,source,confidence,is_current,observed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (db.uid(), entity_type, entity_id, field, text, source, CONFIDENCE.get(source, 0.0), int(applied), t),
    )
