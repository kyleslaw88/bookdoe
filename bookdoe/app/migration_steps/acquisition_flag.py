"""Step 48: store the acquisition_enabled setting.

An existing install that already has an indexer or a download client keeps acquisition on; anything
else (a fresh install) starts with it off. A value already stored is never overwritten. Idempotent.
"""
from __future__ import annotations

import sqlite3

from .. import db
from ..migrations import register


@register(48)
def _migrate_acquisition_flag(conn: sqlite3.Connection) -> dict[str, int]:
    if conn.execute("SELECT 1 FROM settings WHERE key='acquisition_enabled'").fetchone():
        return {"already_applied": 1, "enabled": 0}
    used = bool(conn.execute("SELECT 1 FROM indexers LIMIT 1").fetchone()
                or conn.execute("SELECT 1 FROM download_clients LIMIT 1").fetchone())
    conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
                 ("acquisition_enabled", db.json_dump(used), db.now()))
    return {"already_applied": 0, "enabled": int(used)}
