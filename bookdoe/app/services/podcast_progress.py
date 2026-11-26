"""Podcast played state (issue #515): the played rule and the manual mark played / unplayed actions.

Built on the progress table from #510 (keyed by episode id, never by path). Every function takes an open connection.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import podcasts

PLAYED_PERCENT = 95
PLAYED_REMAINING_SECONDS = 30


class EpisodeNotFound(LookupError):
    pass


def is_played(position: float, duration: float | None) -> bool:
    """True at or above 95% of the duration, or with 30 seconds or less left.

    An episode of 30 seconds or less would satisfy the remaining-time rule at position 0, so it counts as played only at
    its end. An unknown or non-positive duration is never played.
    """
    if duration is None or duration <= 0:
        return False
    if duration <= PLAYED_REMAINING_SECONDS:
        return position >= duration
    return position * 100 >= PLAYED_PERCENT * duration or duration - position <= PLAYED_REMAINING_SECONDS


def _episode(conn: sqlite3.Connection, episode_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT id,duration_seconds FROM podcast_episode WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise EpisodeNotFound(episode_id)
    return dict(row)


def write_progress(conn: sqlite3.Connection, episode_id: str, user_id: str, position: float,
                   client_updated_at: float | None = None) -> tuple[bool, dict[str, Any]]:
    """Store a resume position; played state comes from :func:`is_played` and the episode's duration.

    The single write path for positions. Returns (applied, stored row): with ``client_updated_at`` a write not newer
    than the stored one is not applied and the stored row comes back.
    """
    ep = _episode(conn, episode_id)
    position = max(0.0, float(position))
    applied = podcasts.set_progress(conn, episode_id, played=is_played(position, ep["duration_seconds"]),
                                    position_seconds=position, user_id=user_id, client_updated_at=client_updated_at)
    return applied, podcasts.get_progress(conn, episode_id, user_id)  # type: ignore[return-value]


def mark_played(conn: sqlite3.Connection, episode_id: str, user_id: str) -> dict[str, Any]:
    """Played regardless of position; the stored position is kept."""
    _episode(conn, episode_id)
    prior = podcasts.get_progress(conn, episode_id, user_id)
    podcasts.set_progress(conn, episode_id, played=True,
                          position_seconds=prior["position_seconds"] if prior else 0.0, user_id=user_id)
    return podcasts.get_progress(conn, episode_id, user_id)


def mark_unplayed(conn: sqlite3.Connection, episode_id: str, user_id: str) -> dict[str, Any]:
    """Not played, position back to 0; the progress row and the episode are kept."""
    _episode(conn, episode_id)
    podcasts.set_progress(conn, episode_id, played=False, position_seconds=0.0, user_id=user_id)
    return podcasts.get_progress(conn, episode_id, user_id)
