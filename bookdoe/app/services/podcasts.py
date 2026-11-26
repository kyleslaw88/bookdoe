"""Podcast storage: shows, seasons, episodes and played state (issue #510).

Storage only. An episode is identified by (show, title, release date); its path can change without losing its id or its
progress row. Every function takes an open connection so callers choose the transaction.

The #509 parser is app/services/podcastmeta.py, in this same package, beside this module (as #525 asked).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import db
from . import reader


def get_or_create_show(conn: sqlite3.Connection, title: str) -> str:
    """Id of the show called ``title``, creating it when new."""
    row = conn.execute("SELECT id FROM podcast_show WHERE title=?", (title,)).fetchone()
    if row:
        return row["id"]
    show_id, t = db.uid(), db.now()
    conn.execute("INSERT INTO podcast_show(id,title,created_at,updated_at) VALUES (?,?,?,?)", (show_id, title, t, t))
    return show_id


def get_or_create_season(conn: sqlite3.Connection, show_id: str, number: int | None) -> str:
    """Id of a numbered season, or of the Extras season when ``number`` is None."""
    extras = int(number is None)
    row = conn.execute(
        "SELECT id FROM podcast_season WHERE show_id=? AND is_extras=? AND COALESCE(number,0)=COALESCE(?,0)",
        (show_id, extras, number),
    ).fetchone()
    if row:
        return row["id"]
    season_id = db.uid()
    conn.execute(
        "INSERT INTO podcast_season(id,show_id,number,is_extras,created_at) VALUES (?,?,?,?,?)",
        (season_id, show_id, number, extras, db.now()),
    )
    return season_id


def insert_episode(conn: sqlite3.Connection, *, show_id: str, season_id: str, title: str, release_date: str, path: str,
                   position: int | None = None, description: str | None = None,
                   duration_seconds: float | None = None, has_cover: bool = False) -> str:
    """Insert an episode and return its id.

    Raises sqlite3.IntegrityError on a duplicate (show, title, release date) or when ``season_id`` is not a season of
    ``show_id`` (the foreign key alone would accept another show's season).
    """
    season = season_of(conn, season_id)
    if not season or season["show_id"] != show_id:
        raise sqlite3.IntegrityError(f"season {season_id} does not belong to show {show_id}")
    episode_id, t = db.uid(), db.now()
    conn.execute(
        "INSERT INTO podcast_episode(id,show_id,season_id,position,title,release_date,description,duration_seconds,"
        "has_cover,path,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, show_id, season_id, position, title, release_date, description, duration_seconds,
         int(has_cover), path, t, t),
    )
    return episode_id


def update_episode_path(conn: sqlite3.Connection, episode_id: str, path: str) -> bool:
    """Point an episode at a new file; id and progress are untouched. False when the id is unknown."""
    cur = conn.execute("UPDATE podcast_episode SET path=?,updated_at=? WHERE id=?", (path, db.now(), episode_id))
    return cur.rowcount == 1


def find_episode(conn: sqlite3.Connection, show_id: str, title: str, release_date: str) -> dict[str, Any] | None:
    """Look an episode up by its identity key."""
    row = conn.execute(
        "SELECT * FROM podcast_episode WHERE show_id=? AND title=? AND release_date=?", (show_id, title, release_date)
    ).fetchone()
    return dict(row) if row else None


def set_progress(conn: sqlite3.Connection, episode_id: str, *, played: bool, position_seconds: float,
                 user_id: str = "", client_updated_at: float | None = None) -> bool:
    """Record played state and resume position for an episode; True when stored.

    ``updated_at`` is the one timeline of the row: the device's own timestamp for a position write, server time for an
    action without one (mark played / unplayed). A write carrying ``client_updated_at`` is applied only when newer than
    the stored ``updated_at`` (same rule as the books' reader.set_progress), so a device that was offline cannot
    overwrite a newer position. Timestamps ahead of the server clock are clamped like the books do.
    """
    now = db.now()
    ts = now if client_updated_at is None else min(float(client_updated_at), now + reader.CLIENT_CLOCK_SLACK)
    if client_updated_at is not None:
        existing = get_progress(conn, episode_id, user_id)
        if existing is not None and ts <= existing["updated_at"]:
            return False
    conn.execute(
        "INSERT INTO podcast_progress(episode_id,user_id,played,position_seconds,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(episode_id,user_id) DO UPDATE SET played=excluded.played,"
        "position_seconds=excluded.position_seconds,updated_at=excluded.updated_at",
        (episode_id, user_id, int(played), position_seconds, ts),
    )
    return True


def get_progress(conn: sqlite3.Connection, episode_id: str, user_id: str = "") -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM podcast_progress WHERE episode_id=? AND user_id=?", (episode_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def season_of(conn: sqlite3.Connection, season_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM podcast_season WHERE id=?", (season_id,)).fetchone()
    return dict(row) if row else None


def episode_exists(conn: sqlite3.Connection, episode_id: str) -> bool:
    return conn.execute("SELECT 1 FROM podcast_episode WHERE id=?", (episode_id,)).fetchone() is not None


def update_episode_fields(conn: sqlite3.Connection, episode_id: str, *, season_id: str, position: int | None,
                          description: str | None, has_cover: bool, path: str,
                          duration_seconds: float | None = None) -> bool:
    """Refresh the scanned fields of an existing episode (identity key and id never change); its file is present again.
    A duration that could not be measured (None) keeps the stored one."""
    cur = conn.execute(
        "UPDATE podcast_episode SET season_id=?,position=?,description=?,has_cover=?,path=?,"
        "duration_seconds=COALESCE(?,duration_seconds),missing=0,updated_at=? WHERE id=?",
        (season_id, position, description, int(has_cover), path, duration_seconds, db.now(), episode_id),
    )
    return cur.rowcount == 1


def set_show_cover(conn: sqlite3.Connection, show_id: str, has_cover: bool) -> None:
    """Record whether the show folder holds a cover.jpg."""
    conn.execute("UPDATE podcast_show SET has_cover=?,updated_at=? WHERE id=?", (int(has_cover), db.now(), show_id))


def set_missing(conn: sqlite3.Connection, episode_id: str, missing: bool) -> bool:
    """Flag an episode whose file is gone (or clear the flag). The row, its id and its progress are kept."""
    cur = conn.execute("UPDATE podcast_episode SET missing=?,updated_at=? WHERE id=? AND missing<>?",
                       (int(missing), db.now(), episode_id, int(missing)))
    return cur.rowcount == 1


def episodes_under(conn: sqlite3.Connection, root: str) -> list[dict[str, Any]]:
    """Episodes whose stored path lies under ``root`` (id, path, missing)."""
    prefix = root.rstrip("/") + "/"
    rows = conn.execute("SELECT id,path,missing FROM podcast_episode").fetchall()
    return [dict(r) for r in rows if r["path"].startswith(prefix)]
