"""Podcast play order (issue #518): next unplayed, play-next across seasons, and the in-progress feed.

Order inside a show: numbered seasons ascending, Extras after them, then episode position, release date and title.
An episode counts as unplayed when it has no progress row for the user or its row says played=0.
Episodes flagged missing (file gone, issue #512) are never offered as next or play-next.
"""
from __future__ import annotations

import sqlite3
from typing import Any

_ORDER = "s.is_extras, s.number, (e.position IS NULL), e.position, e.release_date, e.title, e.id"
_SELECT = ("SELECT e.id,e.show_id,e.title,e.release_date,e.duration_seconds,e.position,e.missing,s.number season_number,"
           "s.is_extras,COALESCE(p.played,0) played,COALESCE(p.position_seconds,0) position_seconds "
           "FROM podcast_episode e JOIN podcast_season s ON s.id=e.season_id "
           "LEFT JOIN podcast_progress p ON p.episode_id=e.id AND p.user_id=? ")


def _ordered(conn: sqlite3.Connection, show_id: str, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"{_SELECT}WHERE e.show_id=? ORDER BY {_ORDER}", (user_id, show_id)).fetchall()
    return [dict(r) for r in rows]


def next_unplayed(conn: sqlite3.Connection, show_id: str, user_id: str = "") -> dict[str, Any] | None:
    """First unplayed episode of the show in season order, or None when all are played."""
    return next((e for e in _ordered(conn, show_id, user_id) if not e["played"] and not e["missing"]), None)


def play_next(conn: sqlite3.Connection, episode_id: str, user_id: str = "") -> dict[str, Any] | None:
    """The unplayed episode that follows ``episode_id`` in its show, crossing season boundaries.

    None when it is the last one. Raises LookupError for an unknown episode id.
    """
    row = conn.execute("SELECT show_id FROM podcast_episode WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise LookupError(episode_id)
    eps = _ordered(conn, row["show_id"], user_id)
    idx = next((i for i, e in enumerate(eps) if e["id"] == episode_id), None)
    if idx is None:  # episode exists but is not in the ordered list (e.g. its season row is missing)
        raise LookupError(episode_id)
    return next((e for e in eps[idx + 1:] if not e["played"] and not e["missing"]), None)


def continue_items(conn: sqlite3.Connection, user_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """In-progress episodes (started, not played), newest activity first, shaped for the Continue Listening feed."""
    rows = conn.execute(
        "SELECT e.id episode_id,e.show_id,sh.title show_title,e.title,e.duration_seconds,s.number season_number,"
        "s.is_extras,p.position_seconds,p.updated_at "
        "FROM podcast_progress p JOIN podcast_episode e ON e.id=p.episode_id "
        "JOIN podcast_season s ON s.id=e.season_id JOIN podcast_show sh ON sh.id=e.show_id "
        "WHERE p.user_id=? AND p.played=0 AND p.position_seconds>0 ORDER BY p.updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [{**dict(r), "kind": "podcast", "is_extras": bool(r["is_extras"])} for r in rows]
