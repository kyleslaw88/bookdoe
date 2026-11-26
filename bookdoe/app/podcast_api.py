"""Podcast played-state and resume-position routes (`/api/player/v1/podcasts`, issues #515, #516). Player lane; writes
need CSRF. Positions are keyed by episode id, never by path."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import auth, db
from .services import devices
from .services import podcast_progress as progress
from .services import podcast_queue as queue
from .services import podcasts
from . import podcast_library_api as library

router = APIRouter(prefix="/api/player/v1/podcasts", tags=["player"])


class ProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_seconds: float = Field(ge=0)
    client_updated_at: float | None = Field(default=None, ge=0)


def _run(fn, episode_id: str, user: dict[str, Any], *args) -> dict[str, Any]:
    try:
        with db.session() as conn:
            return fn(conn, episode_id, user["id"], *args)
    except progress.EpisodeNotFound:
        raise HTTPException(404, "Episode not found")


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"position_seconds": row["position_seconds"], "client_updated_at": row["updated_at"], "played": bool(row["played"])}


@router.post("/episodes/{episode_id}/progress")
def write_progress(episode_id: str, body: ProgressBody, request: Request, _=Depends(auth.require_csrf),
                   user=Depends(auth.require_user)):
    applied, row = _run(progress.write_progress, episode_id, user, body.position_seconds, body.client_updated_at)
    return {**row, "applied": applied}


@router.post("/episodes/{episode_id}/played")
def mark_played(episode_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    return _run(progress.mark_played, episode_id, user)


@router.post("/episodes/{episode_id}/unplayed")
def mark_unplayed(episode_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    return _run(progress.mark_unplayed, episode_id, user)


@router.get("/episodes/{episode_id}/progress")
def get_position(episode_id: str, user=Depends(auth.require_user)):
    """This account's saved position in the episode (null when none)."""
    with db.session() as conn:
        if not podcasts.episode_exists(conn, episode_id):
            raise HTTPException(404, "Episode not found")
        return {"episode_id": episode_id, "progress": _view(podcasts.get_progress(conn, episode_id, user["id"]))}


@router.put("/episodes/{episode_id}/progress")
def put_position(episode_id: str, body: ProgressBody, request: Request, _=Depends(auth.require_csrf),
                 user=Depends(auth.require_user)):
    """Save a position stamped with the device's clock. A write not newer than the stored one is not applied; the
    stored position comes back. Same write path as the POST (played rule included)."""
    if body.client_updated_at is None:
        raise HTTPException(422, "client_updated_at is required")
    applied, row = _run(progress.write_progress, episode_id, user, body.position_seconds, body.client_updated_at)
    return {"episode_id": episode_id, "applied": applied, "progress": _view(row)}


@router.get("/shows/{show_id}/next")
def next_unplayed(show_id: str, user=Depends(auth.require_user)):
    """First unplayed episode of a show in season order (Extras last); `episode` is null when all are played."""
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM podcast_show WHERE id=?", (show_id,)).fetchone():
            raise HTTPException(404, "Show not found")
        return {"episode": queue.next_unplayed(conn, show_id, user["id"])}


@router.get("/episodes/{episode_id}/next")
def play_next(episode_id: str, user=Depends(auth.require_user)):
    """The unplayed episode after this one, across season boundaries."""
    try:
        with db.session() as conn:
            return {"episode": queue.play_next(conn, episode_id, user["id"])}
    except LookupError:
        raise HTTPException(404, "Episode not found")


# Read routes for the Player shell: the same handlers as /api/v1/podcasts, reachable with a device credential.
@router.get("")
def list_shows(user=Depends(auth.require_user)):
    return library.list_shows(user)


@router.get("/shows/{show_id}")
def get_show(show_id: str, user=Depends(auth.require_user)):
    return library.get_show(show_id, user)


@router.get("/shows/{show_id}/cover")
def get_cover(show_id: str, user=Depends(auth.require_user)):
    return library.get_cover(show_id, user)


@router.get("/episodes/{episode_id}/audio")
def get_episode_audio(episode_id: str, user=Depends(auth.require_user)):
    return library.get_episode_audio(episode_id, user)


_EPISODE = ("SELECT e.id,e.show_id,sh.title show_title,e.title,e.duration_seconds,e.path FROM podcast_episode e "
            "JOIN podcast_show sh ON sh.id=e.show_id WHERE e.id=?")


def _playable(episode_id: str):
    """The episode row and its file resolved inside an enabled root; 404 when either is missing."""
    with db.session() as conn:
        row = conn.execute(_EPISODE, (episode_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Episode not found")
    safe = devices.resolve_library_file(row["path"])
    if safe is None or not safe.is_file():
        raise HTTPException(404, "Episode file not available")
    return dict(row), safe


@router.get("/episodes/{episode_id}")
def episode_manifest(episode_id: str, user=Depends(auth.require_user)):
    """What a player needs to start an episode: title, show, audio URL and this account's resume position."""
    row, _ = _playable(episode_id)
    with db.session() as conn:
        prog = conn.execute("SELECT position_seconds,played FROM podcast_progress WHERE episode_id=? AND user_id=?",
                            (episode_id, user["id"])).fetchone()
    return {"episode_id": row["id"], "show_id": row["show_id"], "show_title": row["show_title"], "title": row["title"],
            "duration_seconds": row["duration_seconds"], "url": f"/api/player/v1/podcasts/episodes/{episode_id}/audio",
            "position_seconds": prog["position_seconds"] if prog and not prog["played"] else 0.0}
