"""Podcast folder scanner: walks the podcasts root into shows, seasons and episodes (issue #511, parent #508).

Layout: <root>/<Show>/Season NN/<file>.mp3, <root>/<Show>/Extras/<file>.mp3 and <root>/<Show>/cover.jpg. Each file goes
through the #509 parser (tags first, path as fallback) and is upserted through the #510 storage by identity key
(show, title, release date), so a re-scan never duplicates rows and a moved or renamed file keeps its episode id and
progress (only path, season and position change). An episode whose file is gone is flagged missing, never deleted
(issue #512). Duration is measured with mutagen on every scan (issue #564). Not here: routes, UI.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..config import settings
from . import podcasts
from .podcastmeta import EXTRAS, EpisodeMeta, PodcastTagError, measure_duration, parse_episode

log = logging.getLogger(__name__)

COVER_NAME = "cover.jpg"


@dataclass
class ScanResult:
    shows: int = 0
    episodes_added: int = 0
    episodes_updated: int = 0  # includes episodes found at a new path
    episodes_moved: int = 0
    episodes_missing: int = 0  # flagged missing by this scan
    skipped: list[str] = field(default_factory=list)  # files not stored: parse failure or no usable identity


def _visible_dirs(parent: Path) -> list[Path]:
    return sorted((d for d in parent.iterdir() if d.is_dir() and not d.name.startswith(".")), key=lambda d: d.name)


def _mp3_files(folder: Path) -> list[Path]:
    """Direct mp3 children of a folder, no dotfiles (macOS ._ files and the like)."""
    return sorted(
        (f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".") and f.suffix.lower() == ".mp3"),
        key=lambda f: f.name,
    )


def _store(conn: sqlite3.Connection, path: Path, meta: EpisodeMeta, folder_show: str,
           shows_seen: dict[str, str], result: ScanResult, seen_ids: set[str]) -> None:
    show_title = meta.show or folder_show
    if path.parent.name == folder_show and meta.show == path.parent.parent.name:
        show_title = folder_show  # flat file: the parser's path fallback took the library root as the show
    if not meta.title or meta.date is None:
        log.warning("podcast scan: %s has no title or release date, skipped", path)
        result.skipped.append(str(path))
        return
    if show_title not in shows_seen:
        shows_seen[show_title] = podcasts.get_or_create_show(conn, show_title)
    show_id = shows_seen[show_title]
    season_id = podcasts.get_or_create_season(conn, show_id, None if meta.season is EXTRAS else meta.season)
    release = meta.date.isoformat()
    duration = measure_duration(path)
    existing = podcasts.find_episode(conn, show_id, meta.title, release)
    if existing is None:
        new_id = podcasts.insert_episode(conn, show_id=show_id, season_id=season_id, title=meta.title,
                                         release_date=release, path=str(path), position=meta.position,
                                         description=meta.description, has_cover=meta.has_cover,
                                         duration_seconds=duration)
        result.episodes_added += 1
        seen_ids.add(new_id)
        return
    seen_ids.add(existing["id"])
    if existing["path"] != str(path):
        result.episodes_moved += 1
    podcasts.update_episode_fields(conn, existing["id"], season_id=season_id, position=meta.position,
                                   description=meta.description, has_cover=meta.has_cover, path=str(path),
                                   duration_seconds=duration)
    result.episodes_updated += 1


def _flag_missing(conn: sqlite3.Connection, root: Path, seen_ids: set[str], result: ScanResult) -> None:
    """Flag episodes under ``root`` that this scan did not find. A file still on disk (e.g. unreadable tags) is not
    missing. Never called for an absent root, so an unmounted share cannot flag the whole library."""
    for ep in podcasts.episodes_under(conn, str(root)):
        if ep["id"] in seen_ids or Path(ep["path"]).exists():
            continue
        if podcasts.set_missing(conn, ep["id"], True):
            result.episodes_missing += 1


def scan_podcasts(root: str | Path, conn: sqlite3.Connection | None = None) -> ScanResult:
    """Scan ``root`` and persist what it finds. Opens its own committed session unless a connection is given."""
    if conn is None:
        with db.session() as own:
            return scan_podcasts(root, own)
    result = ScanResult()
    seen_ids: set[str] = set()
    root = Path(root)
    if not root.is_dir():
        log.warning("podcast scan: %s is not a directory", root)
        return result
    for show_dir in _visible_dirs(root):
        has_cover = (show_dir / COVER_NAME).is_file()
        shows_seen: dict[str, str] = {}
        # Flat files directly in the show folder are read too (the pre-organize layout); the season comes from tags.
        # Without tags a flat file has no season and the parser's fallback would name the show after the root folder.
        folders = [show_dir, *_visible_dirs(show_dir)]
        for folder in folders:
            for mp3 in _mp3_files(folder):
                try:
                    meta = parse_episode(mp3)
                    if isinstance(meta, PodcastTagError):
                        log.warning("podcast scan: %s unreadable (%s), skipped", mp3, meta.reason)
                        result.skipped.append(str(mp3))
                        continue
                    _store(conn, mp3, meta, show_dir.name, shows_seen, result, seen_ids)
                except Exception:  # one bad file must not stop the scan
                    log.exception("podcast scan: %s failed, skipped", mp3)
                    result.skipped.append(str(mp3))
        if not shows_seen:  # a show folder with no readable episode still gets its row
            shows_seen[show_dir.name] = podcasts.get_or_create_show(conn, show_dir.name)
        for show_id in shows_seen.values():
            podcasts.set_show_cover(conn, show_id, has_cover)
        result.shows += len(shows_seen)
    _flag_missing(conn, root, seen_ids, result)
    return result


def scan_job() -> dict[str, int | bool | str]:
    """The podcast-scan job body: scan BOOKDOE_PODCASTS_DIR, or skip when it is not configured."""
    if settings.podcasts_dir is None:
        return {"skipped": True, "reason": "BOOKDOE_PODCASTS_DIR is not set"}
    r = scan_podcasts(settings.podcasts_dir)
    return {"shows": r.shows, "episodes_added": r.episodes_added, "episodes_updated": r.episodes_updated,
            "skipped_files": len(r.skipped)}
