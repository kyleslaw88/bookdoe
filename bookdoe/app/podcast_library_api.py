"""Read-only podcast library routes (`/api/v1/podcasts`, issue #514): show list, one show with its seasons and
episodes, and the show cover. Same auth as the library routes (`auth.require_user`). Writes and progress live in
podcast_api.py.

The schema has no cover column: a show's cover is the `cover.jpg` in the show folder, which is two levels above any
episode file (`<Show>/Season NN/x.mp3` or `<Show>/Extras/x.mp3`). Without one, the cover is the ID3 APIC image of the
show's newest episode that has one (extracted once into the data dir; the library folder is never written to).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import auth, db
from .config import settings

router = APIRouter(prefix="/api/v1/podcasts", tags=["podcasts"])

COVER_NAME = "cover.jpg"


def _file_cover(conn, show_id: str) -> Path | None:
    """The show's cover.jpg, found next to the show folder of its episodes; None when no such file exists."""
    seen: set[Path] = set()
    for row in conn.execute("SELECT path FROM podcast_episode WHERE show_id=? ORDER BY release_date, title", (show_id,)):
        folder = Path(row["path"]).parent.parent
        if folder in seen:
            continue
        seen.add(folder)
        candidate = folder / COVER_NAME
        if candidate.is_file():
            return candidate
    return None


def _read_apic(path: Path) -> tuple[bytes, str] | None:
    """The first non-empty APIC picture of an mp3 as (bytes, mime); None when unreadable or absent."""
    try:
        from mutagen.id3 import ID3
        tags = ID3(str(path))
        for frame in tags.getall("APIC"):
            if frame.data:
                mime = (frame.mime or "").lower()
                if mime in ("image/jpg", "jpg", ""):
                    mime = "image/jpeg"
                elif mime == "png":
                    mime = "image/png"
                return bytes(frame.data), mime
    except Exception:
        logging.getLogger(__name__).debug("no readable APIC in %s", path, exc_info=True)
    return None


def _embedded_cover(conn, show_id: str) -> tuple[Path, str] | None:
    """The newest episode's embedded artwork, cached under data_dir/podcast_covers keyed by episode id and mtime."""
    rows = conn.execute("SELECT id,path FROM podcast_episode WHERE show_id=? AND has_cover=1 AND missing=0 "
                        "ORDER BY release_date DESC, title DESC, id DESC", (show_id,)).fetchall()
    cache = settings.data_dir / "podcast_covers"
    for row in rows:
        src = Path(row["path"])
        try:
            mtime = int(src.stat().st_mtime_ns)
        except OSError:
            continue
        stem = f"{row['id']}-{mtime}"
        for hit in cache.glob(stem + ".*") if cache.is_dir() else ():
            return hit, "image/png" if hit.suffix == ".png" else "image/jpeg"
        found = _read_apic(src)
        if found is None:
            continue
        data, mime = found
        ext = ".png" if mime == "image/png" else ".jpg"
        cache.mkdir(parents=True, exist_ok=True)
        for old in cache.glob(f"{row['id']}-*"):   # stale entries for an earlier mtime
            old.unlink(missing_ok=True)
        target = cache / (stem + ext)
        tmp = cache / (stem + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target, mime
    return None


def _cover_path(conn, show_id: str) -> tuple[Path, str] | None:
    """(file, media type) of the show's cover: cover.jpg first, else embedded episode artwork; None when neither."""
    path = _file_cover(conn, show_id)
    if path is not None:
        return path, "image/jpeg"
    return _embedded_cover(conn, show_id)


def _cover_url(conn, show_id: str) -> str | None:
    return f"/api/v1/podcasts/{show_id}/cover" if _has_cover(conn, show_id) else None


def _has_cover(conn, show_id: str) -> bool:
    """Cheap check for the lists: cover.jpg exists or some episode carries an APIC frame (no mp3 is read)."""
    return (_file_cover(conn, show_id) is not None
            or conn.execute("SELECT 1 FROM podcast_episode WHERE show_id=? AND has_cover=1 AND missing=0",
                            (show_id,)).fetchone() is not None)


@router.get("")
def list_shows(user=Depends(auth.require_user)):
    """Every show: title, cover URL, number of numbered seasons and whether Extras exist."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT sh.id, sh.title, "
            "COALESCE(SUM(CASE WHEN se.is_extras=0 THEN 1 ELSE 0 END),0) season_count, "
            "COALESCE(MAX(se.is_extras),0) has_extras "
            "FROM podcast_show sh LEFT JOIN podcast_season se ON se.show_id=sh.id "
            "GROUP BY sh.id ORDER BY sh.title COLLATE NOCASE, sh.id"
        ).fetchall()
        covers = {r["id"]: _cover_url(conn, r["id"]) for r in rows}
    return {"shows": [{"id": r["id"], "title": r["title"], "cover_url": covers[r["id"]],
                       "season_count": r["season_count"], "has_extras": bool(r["has_extras"])} for r in rows]}


@router.get("/{show_id}")
def get_show(show_id: str, user=Depends(auth.require_user)):
    """One show: numbered seasons ascending, Extras last, each with its episodes in position order."""
    with db.session() as conn:
        show = conn.execute("SELECT id,title FROM podcast_show WHERE id=?", (show_id,)).fetchone()
        if not show:
            raise HTTPException(404, "Show not found")
        seasons = conn.execute(
            "SELECT id,number,is_extras FROM podcast_season WHERE show_id=? ORDER BY is_extras, number, id",
            (show_id,)).fetchall()
        episodes = conn.execute(
            "SELECT e.id,e.season_id,e.title,e.release_date,e.duration_seconds,e.description,e.position,e.missing,"
            "COALESCE(p.played,0) played,COALESCE(p.position_seconds,0) position_seconds "
            "FROM podcast_episode e LEFT JOIN podcast_progress p ON p.episode_id=e.id AND p.user_id=? "
            "WHERE e.show_id=? ORDER BY (e.position IS NULL), e.position, e.release_date, e.title, e.id",
            (user["id"], show_id)).fetchall()
        cover_url = _cover_url(conn, show_id)
    by_season: dict[str, list[dict[str, Any]]] = {s["id"]: [] for s in seasons}
    for e in episodes:
        by_season.setdefault(e["season_id"], []).append({
            "id": e["id"], "title": e["title"], "release_date": e["release_date"],
            "duration_seconds": e["duration_seconds"], "description": e["description"] or "",
            "missing": bool(e["missing"]), "played": bool(e["played"]), "position_seconds": e["position_seconds"]})
    return {
        "id": show["id"], "title": show["title"], "cover_url": cover_url,
        "seasons": [{"id": s["id"], "number": s["number"], "is_extras": bool(s["is_extras"]),
                     "episodes": by_season[s["id"]]} for s in seasons],
    }


@router.get("/{show_id}/cover")
def get_cover(show_id: str, user=Depends(auth.require_user)):
    """The show's cover.jpg or, failing that, its newest episode's embedded artwork; 404 when unknown or neither exists."""
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM podcast_show WHERE id=?", (show_id,)).fetchone():
            raise HTTPException(404, "Show not found")
        found = _cover_path(conn, show_id)
    if found is None:
        raise HTTPException(404, "No cover available")
    return FileResponse(found[0], media_type=found[1], headers={"Cache-Control": "private, max-age=86400"})


@router.get("/episodes/{episode_id}/audio")
def get_episode_audio(episode_id: str, user=Depends(auth.require_user)):
    """The episode's audio file (Range-capable); 404 when the episode is unknown, flagged missing or gone from disk."""
    with db.session() as conn:
        row = conn.execute("SELECT path,missing FROM podcast_episode WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Episode not found")
    path = Path(row["path"])
    if row["missing"] or not path.is_file():
        raise HTTPException(404, "Episode file is missing")
    return FileResponse(path, headers={"Content-Disposition": "inline"})
