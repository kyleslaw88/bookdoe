"""In-browser reader and audiobook player: the per-asset manifest the UI plays from,
the per-user position it resumes from, and the path-guarded file lookup it streams.

Spec Part XXXVII deferred reading/listening but required the architecture to carry
"reading progress, listening progress, playback position, chapters, range streaming".
This module is that layer. It deliberately owns no rendering: EPUB rendering happens in
the browser (epub.js), PDF in the browser's own viewer, audio in an HTML5 <audio>
element fed by a Range-capable file route. Everything here is about *which* bytes a
user may read and *where* they were.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import db
from . import assetformats
from . import devices

log = logging.getLogger(__name__)

# What the browser can actually render. .mobi/.azw3 download fine (OPDS, Kindle) but no
# in-browser renderer we are willing to ship handles them, so they get no Read button.
READABLE_EBOOK_FORMATS = {"epub", "pdf"}
PLAYABLE_AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wav"}

# Chapter markers are read from the container with ffprobe on demand (the scanner stores
# only a chapter *count*). Keyed on (path, mtime) so an unchanged file costs one probe
# per process lifetime, and a re-encoded file is probed again.
_CHAPTER_CACHE: dict[tuple[str, float], list[dict[str, Any]]] = {}
_CHAPTER_CACHE_MAX = 512


def file_for_work(work_id: str, file_id: str) -> dict[str, Any] | None:
    """The media_files row joined to its asset, or None when it is not this work's,
    is marked missing, or is not inside an enabled root. ``resolved`` is the path to
    serve (see devices.resolve_library_file for why the raw path is never served)."""
    row = db.one(
        """SELECT f.*, a.kind AS asset_kind, a.format AS asset_format, a.work_id
           FROM media_files f JOIN media_assets a ON a.id=f.asset_id
           WHERE f.id=? AND a.work_id=? AND f.missing=0""",
        (file_id, work_id),
    )
    if not row:
        return None
    safe = devices.resolve_library_file(row["path"])
    if safe is None:
        log.warning("Refusing to serve %s: outside every enabled root", row["path"])
        row["resolved"] = None
        return row
    if not safe.is_file():
        return None
    row["resolved"] = safe
    return row


def _ffprobe_chapters(path: Path) -> list[dict[str, Any]]:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if p.returncode != 0:
            return []
        data = json.loads(p.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.info("ffprobe chapters unavailable for %s: %s", path, exc)
        return []
    out = []
    for i, ch in enumerate(data.get("chapters") or []):
        try:
            start, end = float(ch.get("start_time") or 0), float(ch.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        title = ((ch.get("tags") or {}).get("title") or "").strip() or f"Chapter {i + 1}"
        out.append({"index": i, "title": title, "start": start, "end": end})
    return out


def chapters_for_file(path: Path) -> list[dict[str, Any]]:
    """Embedded chapter markers of an audio file, [] when there are none or ffprobe
    is unavailable. Cached per (path, mtime)."""
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return []
    hit = _CHAPTER_CACHE.get(key)
    if hit is not None:
        return hit
    chapters = _ffprobe_chapters(path)
    if len(_CHAPTER_CACHE) >= _CHAPTER_CACHE_MAX:
        _CHAPTER_CACHE.clear()
    _CHAPTER_CACHE[key] = chapters
    return chapters


def _track_title(f: dict[str, Any], index: int, total: int, raw_names: bool = True) -> str:
    embedded = db.json_load(f.get("embedded_json"), {}) or {}
    if embedded.get("title") and total > 1:
        return str(embedded["title"])
    if total > 1 and not raw_names:
        return f"Track {f.get('part_number') or index + 1}"   # a raw file name can carry folder and root hints
    return Path(f["path"]).stem if total > 1 else "Full book"


def manifest(work_id: str, asset_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """Everything the reader/player page needs in one call: the asset, its playable or
    readable files (with the URL to fetch each from), chapter markers for a single-file
    audiobook, and this user's saved position. Raises ValueError with a message that is
    safe to show when the asset cannot be consumed in a browser."""
    asset = db.one("SELECT * FROM media_assets WHERE id=? AND work_id=?", (asset_id, work_id))
    if not asset:
        raise ValueError("Asset not found")
    files = db.all(
        "SELECT id,path,part_number,size_bytes,duration_seconds,embedded_json FROM media_files "
        "WHERE asset_id=? AND missing=0 ORDER BY part_number,path",
        (asset_id,),
    )
    if asset["kind"] == "ebook":
        fmt = (asset.get("format") or "").lower()
        files = [f for f in files if Path(f["path"]).suffix.lower().lstrip(".") in READABLE_EBOOK_FORMATS]
        if not files:
            raise ValueError("No EPUB or PDF file to read for this book" if fmt not in READABLE_EBOOK_FORMATS
                             else "No file available for this book")
        files = files[:1]
        fmt = Path(files[0]["path"]).suffix.lower().lstrip(".")
    else:
        files = [f for f in files if Path(f["path"]).suffix.lower() in PLAYABLE_AUDIO_EXTS]
        if not files:
            raise ValueError("No playable audio file for this audiobook")
        fmt = (asset.get("format") or Path(files[0]["path"]).suffix.lstrip(".")).lower()

    # Serve only what is inside an enabled root; a file that escapes is dropped here
    # rather than 403ing later, so the page never offers a track it cannot play.
    tracks: list[dict[str, Any]] = []
    for f in files:
        if devices.resolve_library_file(f["path"]) is None:
            log.warning("Manifest drops %s: outside every enabled root", f["path"])
            continue
        tracks.append(f)
    if not tracks:
        raise ValueError("File is outside the library roots")

    work = db.one("SELECT id,title FROM works WHERE id=?", (work_id,)) or {"id": work_id, "title": ""}
    items = []
    for i, f in enumerate(tracks):
        items.append({
            "id": f["id"],
            "title": _track_title(f, i, len(tracks), assetformats.sees_paths(user)),
            "part_number": f.get("part_number"),
            "size_bytes": f.get("size_bytes"),
            "duration_seconds": f.get("duration_seconds"),
            "url": f"/api/v1/library/{work_id}/files/{f['id']}",
        })
    chapters: list[dict[str, Any]] = []
    if asset["kind"] == "audiobook" and len(tracks) == 1:
        chapters = chapters_for_file(devices.resolve_library_file(tracks[0]["path"]))
    return {
        "work_id": work_id,
        "title": work["title"],
        "asset_id": asset_id,
        "kind": asset["kind"],
        "format": fmt,
        "files": items,
        "chapters": chapters,
        "progress": get_progress(asset_id, user["id"]),
    }


PROGRESS_COLUMNS = "file_id,position,location,percent,updated_at,device_id,client_updated_at,duration"
# A client clock this far ahead of the server is wrong, not early; clamping keeps one
# bad device from parking a timestamp in 2099 that every honest update then loses to.
class AssetNotFound(ValueError):
    """The asset does not exist under this work (a ValueError so older callers still catch it)."""


CLIENT_CLOCK_SLACK = 300.0

# Per-account write budget for one book's position. A player legitimately saves every
# ~10 s plus pause/seek/track changes, so 30 a minute is generous; anything past it is a
# runaway client (a retry loop, two tabs fighting) and is answered 429 + Retry-After
# before any database work, which is also what keeps a hidden book's 404s cheap.
PROGRESS_WRITE_BUDGET = 30
PROGRESS_WRITE_WINDOW = 60.0
_PROGRESS_MAX_KEYS = 20000
_progress_writes: dict[tuple[str, str], list[float]] = {}
_clock = time.monotonic


def progress_write_retry_after(user_id: str, asset_id: str) -> int:
    """Count one position write; 0 when within budget, else the seconds to wait. In
    memory and per process: it bounds abuse, it is not an accounting record."""
    now = _clock()
    key = (user_id, asset_id)
    hits = [t for t in _progress_writes.get(key, ()) if now - t < PROGRESS_WRITE_WINDOW]
    if len(hits) >= PROGRESS_WRITE_BUDGET:
        _progress_writes[key] = hits
        return int(PROGRESS_WRITE_WINDOW - (now - hits[0])) + 1
    hits.append(now)
    if key not in _progress_writes and len(_progress_writes) >= _PROGRESS_MAX_KEYS:
        for k in [k for k, v in _progress_writes.items() if not v or now - v[-1] >= PROGRESS_WRITE_WINDOW]:
            del _progress_writes[k]
        if len(_progress_writes) >= _PROGRESS_MAX_KEYS:
            _progress_writes.clear()
    _progress_writes[key] = hits
    return 0


def reset_progress_limits() -> None:
    """Forget every budget (tests)."""
    _progress_writes.clear()


def get_progress(asset_id: str, user_id: str) -> dict[str, Any] | None:
    return db.one(f"SELECT {PROGRESS_COLUMNS} FROM media_progress WHERE asset_id=? AND user_id=?", (asset_id, user_id))  # nosec B608


def set_progress(work_id: str, asset_id: str, user_id: str, *, file_id: str | None, position: float | None,
                 location: str | None, percent: float | None, device_id: str | None = None,
                 client_updated_at: float | None = None, duration: float | None = None) -> dict[str, Any]:
    """Upsert this user's position in the asset. ``file_id`` must belong to the asset
    (the player names the track it is on); ebooks pass the renderer's location string
    (an EPUB CFI, or a PDF page) and no file.

    Conflict rule (docs/PLAYER_API.md, "Sync"): an update that carries
    ``client_updated_at`` is applied only when it is newer than the stored client
    timestamp; an older one is stale (the device was offline while another device
    moved on) and the stored state is returned with ``applied: false`` so the device
    can adopt it. Updates without a client timestamp (the web shells before this
    field existed, older API clients) are applied as before. Re-sending the same
    update is harmless. Timestamps ahead of the server clock are clamped.
    """
    asset = db.one("SELECT id,kind FROM media_assets WHERE id=? AND work_id=?", (asset_id, work_id))
    if not asset:
        raise AssetNotFound("Asset not found")
    if file_id is not None and not db.one("SELECT 1 FROM media_files WHERE id=? AND asset_id=?", (file_id, asset_id)):
        raise ValueError("File does not belong to this asset")
    t = db.now()
    if client_updated_at is not None:
        client_updated_at = min(float(client_updated_at), t + CLIENT_CLOCK_SLACK)
    pct = None if percent is None else max(0.0, min(1.0, float(percent)))
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT client_updated_at,file_id,position,location,percent,updated_at,device_id,duration FROM media_progress WHERE asset_id=? AND user_id=?",
            (asset_id, user_id),
        ).fetchone()
        if existing is not None and client_updated_at is not None and existing["client_updated_at"] is not None \
                and client_updated_at <= existing["client_updated_at"]:
            stale = dict(existing)
            stale.update({"asset_id": asset_id, "applied": False})
            return stale
        # The first position saved on a work is the moment the person started it; the
        # read state follows (see _auto_start_reading). Checked before the upsert so
        # the periodic saves the reader makes every few seconds cost nothing extra.
        first_on_work = conn.execute(
            "SELECT 1 FROM media_progress WHERE work_id=? AND user_id=?", (work_id, user_id)
        ).fetchone() is None
        conn.execute(
            """INSERT INTO media_progress(asset_id,user_id,work_id,kind,file_id,position,location,percent,updated_at,device_id,client_updated_at,duration)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id,user_id) DO UPDATE SET file_id=excluded.file_id,position=excluded.position,
                 location=excluded.location,percent=excluded.percent,updated_at=excluded.updated_at,
                 device_id=excluded.device_id,client_updated_at=excluded.client_updated_at,
                 duration=COALESCE(excluded.duration,media_progress.duration)""",
            (asset_id, user_id, work_id, asset["kind"], file_id, float(position or 0.0), location, pct, t,
             device_id, client_updated_at, duration),
        )
        if first_on_work:
            _auto_start_reading(conn, work_id, user_id, t)
    return {"asset_id": asset_id, "file_id": file_id, "position": float(position or 0.0), "location": location,
            "percent": pct, "updated_at": t, "device_id": device_id, "client_updated_at": client_updated_at,
            "duration": duration, "applied": True}


# Precedence between the reader's automatic read state and the one a person picks in
# the Reading-status dropdown (read_states.source: 'auto' vs 'manual'):
#
#   * Starting a book (first progress row on the work for this account) marks it
#     'reading' only when there is no read state yet, or the existing one is
#     'want_to_read' - opening a book you wanted to read IS starting to read it.
#     'reading', 'finished' and 'dnf' are left exactly as they are: re-opening a
#     finished book to look something up must not un-finish it, and a hand-set
#     'reading' keeps its manual provenance.
#   * Clearing progress removes a 'reading' state only if the reader set it ('auto')
#     and no position remains on any other asset of the work. A state the person
#     chose by hand - any status, including 'reading' - is never touched. (A
#     'want_to_read' the reader upgraded is gone too rather than restored: the
#     previous status is not kept, and "not tracked" is the honest answer once the
#     person has wiped every trace of having started it.)
#   * The dropdown always writes source='manual', so a person can override the
#     automatic state at any time and the reader will not flip it back.
def _auto_start_reading(conn, work_id: str, user_id: str, t: float) -> None:
    existing = conn.execute("SELECT status FROM read_states WHERE work_id=? AND user_id=?", (work_id, user_id)).fetchone()
    if existing and existing["status"] != "want_to_read":
        return
    conn.execute(
        """INSERT INTO read_states(work_id,user_id,status,started_at,finished_at,updated_at,source)
           VALUES (?,?,'reading',?,NULL,?,'auto')
           ON CONFLICT(work_id,user_id) DO UPDATE SET status='reading',
             started_at=COALESCE(read_states.started_at,excluded.started_at),
             finished_at=NULL,updated_at=excluded.updated_at,source='auto'""",
        (work_id, user_id, t, t),
    )


def clear_progress(asset_id: str, user_id: str) -> None:
    with db.tx() as conn:
        row = conn.execute("SELECT work_id FROM media_progress WHERE asset_id=? AND user_id=?", (asset_id, user_id)).fetchone()
        conn.execute("DELETE FROM media_progress WHERE asset_id=? AND user_id=?", (asset_id, user_id))
        if not row:
            return
        still_in_progress = conn.execute(
            "SELECT 1 FROM media_progress WHERE work_id=? AND user_id=?", (row["work_id"], user_id)
        ).fetchone()
        if not still_in_progress:
            conn.execute(
                "DELETE FROM read_states WHERE work_id=? AND user_id=? AND status='reading' AND source='auto'",
                (row["work_id"], user_id),
            )


def progress_for_work(work_id: str, user_id: str) -> list[dict[str, Any]]:
    """Positions this user holds in any of the work's assets (for the detail page's
    Resume buttons)."""
    return db.all(
        f"SELECT asset_id,kind,{PROGRESS_COLUMNS} FROM media_progress WHERE work_id=? AND user_id=?",  # nosec B608
        (work_id, user_id),
    )


def continue_items(user: dict[str, Any], *, kind: str | None = None, limit: int = 12) -> list[dict[str, Any]]:
    """This account's in-progress books, newest activity first, restricted to works the
    account may see and to assets that still have a file. One row per asset: the same
    person can be in both the ebook and the audiobook of one title."""
    from . import tagging  # lazy: tagging imports llmclient, which this module does not need

    visible, vparams = tagging.kids_visibility(user)
    where = [visible, "p.user_id=?", "(p.percent IS NULL OR p.percent<0.995)",
             "EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=p.asset_id AND f.missing=0)"]
    params: list[Any] = [*vparams, user["id"]]
    if kind:
        where.append("p.kind=?")
        params.append(kind)
    rows = db.all(
        f"""SELECT p.asset_id,p.kind,{",".join("p." + c for c in PROGRESS_COLUMNS.split(","))},
                   w.id work_id,w.title,w.sort_title,
                   (SELECT 1 FROM cover_cache cc WHERE cc.work_id=w.id AND cc.status='cached') has_cover,
                   (SELECT GROUP_CONCAT(c.name, ', ') FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id
                     WHERE wc.work_id=w.id AND wc.role='author') authors
            FROM media_progress p JOIN works w ON w.id=p.work_id
            WHERE {" AND ".join(where)} ORDER BY p.updated_at DESC LIMIT ?""",  # nosec B608
        tuple(params + [limit]),
    )
    for r in rows:
        r["has_cover"] = bool(r["has_cover"])
    return rows
