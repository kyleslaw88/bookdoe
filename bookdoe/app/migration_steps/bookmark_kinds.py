"""Step 42: a bookmark can also be a highlight.

Highlights (a selected passage of an ebook, optionally with a note) are stored as bookmarks of kind
'highlight': ``location`` holds the passage's EPUB CFI range, ``title`` the selected text, ``note`` the
reader's own words and ``color`` the marker colour. Existing rows keep kind 'bookmark'. ``updated_at``
lets an edited note or colour reach other devices through ``/sync``. Additive and idempotent.
"""
from __future__ import annotations

import sqlite3

from ..migrations import _columns, _fail, register, register_validation

NEW_COLUMNS = (("kind", "TEXT NOT NULL DEFAULT 'bookmark'"), ("color", "TEXT"), ("updated_at", "REAL"))


@register(42)
def _migrate_bookmark_kinds(conn: sqlite3.Connection) -> dict[str, int]:
    have = _columns(conn, "bookmarks")
    added = 0
    for name, ddl in NEW_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE bookmarks ADD COLUMN {name} {ddl}")
            added += 1
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_user_asset_kind ON bookmarks(user_id,asset_id,kind,created_at)")
    return {"columns_added": added, "already_applied": int(not added)}


@register_validation(42)
def _validate_bookmark_kinds(conn: sqlite3.Connection) -> None:
    missing = {n for n, _ in NEW_COLUMNS} - _columns(conn, "bookmarks")
    if missing:
        _fail(f"bookmarks is missing columns {sorted(missing)}")
    bad = conn.execute("SELECT COUNT(*) FROM bookmarks WHERE kind NOT IN ('bookmark','highlight')").fetchone()[0]
    if bad:
        _fail(f"{bad} bookmarks have an unknown kind")
