"""File-quality maintenance detectors (P5-20, Part XXV): invalid EPUBs, damaged audio, missing
chapters, missing covers and missing embedded metadata.

Read-only on disk: the only file reads are bounded EPUB structure checks and the P5-14
``verify_file`` of never-verified audio, both through the path guard. Most checks are DB reads
of the P5-02 stream columns and the P5-14 integrity columns. A NULL stream column or a NULL
integrity_state means "never probed": that is unknown, never a finding, and is only counted
(``unknown_counts``, shown in the maintenance summary).

Probing is bounded per run: ``maintenance.quality_batch`` files, taken from a rotating cursor
(``maintenance.quality_cursor.<name>`` in settings, the last file id examined) so successive runs
walk the whole library. Every run still yields the findings already known (damaged rows, cached EPUB
results), so a run is complete and auto-resolve stays correct although it probed only a batch.

Fingerprint rules (one finding per file; the discriminator separates the file-level and the
work-level missing_cover):

  invalid_epub, damaged_audio, missing_chapters, missing_embedded_metadata  subject file/<id>, ''
  missing_cover   subject file/<id>, 'embedded'  or  work/<id>, 'no_cover'
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterable

from .. import db
from . import covers, fileverify, scanner
from .maintenance import DetectorContext, Finding, register_detector

BATCH_SETTING = "maintenance.quality_batch"
CURSOR_SETTING = "maintenance.quality_cursor."
DEFAULT_BATCH = 25
MAX_XML_BYTES = 5 * 1024 * 1024  # a container.xml / OPF larger than this is not read
LONG_AUDIOBOOK_SECONDS = 3600

_FILE_SQL = (
    "SELECT f.id,f.path,f.fingerprint,f.missing,f.duration_seconds,f.chapter_count,f.has_embedded_cover,"
    "f.embedded_json,f.integrity_state,f.integrity_detail_json,a.id asset_id,w.title,"
    "(SELECT COUNT(*) FROM media_files x WHERE x.asset_id=f.asset_id) asset_files "
    "FROM media_files f JOIN media_assets a ON a.id=f.asset_id JOIN works w ON w.id=a.work_id"
)


def _is_epub(path: str) -> bool:
    return path.lower().endswith(".epub")


def _is_audio(path: str) -> bool:
    return Path(path).suffix.lower() in scanner.AUDIO_EXTS


def _files(where: str = "", params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return db.all(f"{_FILE_SQL}{' WHERE ' + where if where else ''} ORDER BY f.id", list(params))


def _batch_size() -> int:
    try:
        return max(1, int(db.get_setting(BATCH_SETTING, DEFAULT_BATCH)))
    except (TypeError, ValueError):
        return DEFAULT_BATCH


def _rotate(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """*rows* (ordered by id) starting after the stored cursor, wrapping around."""
    cursor = db.get_setting(CURSOR_SETTING + name, "")
    return [r for r in rows if r["id"] > cursor] + [r for r in rows if r["id"] <= cursor]


def _advance(name: str, file_id: str) -> None:
    db.set_setting(CURSOR_SETTING + name, file_id)


# ---------------------------------------------------------------------------
# EPUB structure
# ---------------------------------------------------------------------------

def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"{name} is larger than {MAX_XML_BYTES} bytes")
    with zf.open(info) as fh:
        return fh.read(MAX_XML_BYTES + 1)


def _text(el: ET.Element | None) -> str | None:
    return el.text.strip() if el is not None and el.text and el.text.strip() else None


def inspect_epub(path: Path) -> dict[str, Any]:
    """Structure check of one EPUB: ``{"ok", "problems", "title", "authors"}``. Never raises."""
    problems: list[str] = []
    title, authors = None, []
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos or infos[0].filename != "mimetype":
                problems.append("mimetype is not the first zip entry")
            try:
                container = safe_fromstring(_read_member(zf, "META-INF/container.xml"))
            except KeyError:
                problems.append("META-INF/container.xml is missing")
            except (ET.ParseError, ValueError) as exc:
                problems.append(f"container.xml is unreadable: {exc}")
            else:
                rootfile = container.find(".//{*}rootfile")
                opf_path = rootfile.attrib.get("full-path") if rootfile is not None else None
                if not opf_path:
                    problems.append("container.xml names no OPF rootfile")
                else:
                    try:
                        opf = safe_fromstring(_read_member(zf, opf_path))
                    except KeyError:
                        problems.append(f"OPF {opf_path} is missing")
                    except (ET.ParseError, ValueError) as exc:
                        problems.append(f"OPF {opf_path} does not parse: {exc}")
                    else:
                        meta = opf.find(".//{*}metadata")
                        if meta is not None:
                            title = _text(meta.find("{*}title"))
                            authors = [t for t in (_text(c) for c in meta.findall("{*}creator")) if t]
    except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError, zlib.error) as exc:
        problems.append(f"not a readable zip: {exc}")
    return {"ok": not problems, "problems": problems, "title": title, "authors": authors}


def _epub_cache(row: dict[str, Any]) -> dict[str, Any] | None:
    """The cached structure result, only while the file still has the fingerprint it was checked at."""
    cache = db.json_load(row["integrity_detail_json"], {}).get("epub")
    return cache if isinstance(cache, dict) and cache.get("fingerprint") == row["fingerprint"] else None


def _check_epub_batch(ctx: DetectorContext) -> None:
    """Check up to a batch of EPUBs that have no valid cached result and cache what was found."""
    rows = [r for r in _files("f.missing=0") if _is_epub(r["path"]) and _epub_cache(r) is None]
    done = 0
    for row in _rotate(rows, "epub"):
        if done >= _batch_size() or ctx.expired():
            break
        found = fileverify.resolve_in_root(Path(row["path"]))
        if found is None or not fileverify.is_file(found[1]):
            continue  # gone or outside every root: verify/missing-file report that, not this check
        result = inspect_epub(found[1])
        detail = db.json_load(row["integrity_detail_json"], {})
        detail["epub"] = {**result, "fingerprint": row["fingerprint"], "checked_at": db.now()}
        with db.tx() as conn:
            conn.execute("UPDATE media_files SET integrity_detail_json=? WHERE id=?", (db.json_dump(detail), row["id"]))
        _advance("epub", row["id"])
        done += 1


def _invalid_epub(ctx: DetectorContext) -> Iterable[Finding]:
    _check_epub_batch(ctx)
    for row in _files("f.missing=0"):
        cache = _epub_cache(row) if _is_epub(row["path"]) else None
        if cache and not cache.get("ok"):
            yield Finding("invalid_epub", "error", "file", row["id"],
                          detail={"path": row["path"], "work_title": row["title"], "problems": cache.get("problems", []),
                                  "checked_at": cache.get("checked_at")})


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def _damaged_audio(ctx: DetectorContext) -> Iterable[Finding]:
    unverified = [r for r in _files("f.missing=0 AND f.integrity_state IS NULL") if _is_audio(r["path"])]
    done = 0
    for row in _rotate(unverified, "audio"):
        if done >= _batch_size() or ctx.expired():
            break
        fileverify.verify_file(row["id"])  # records integrity_state, so the file leaves the unverified set
        _advance("audio", row["id"])
        done += 1
    for row in _files("f.missing=0 AND f.integrity_state='damaged'"):
        if not _is_audio(row["path"]):
            continue
        checks = db.json_load(row["integrity_detail_json"], {}).get("checks", [])
        failed = [c for c in checks if c.get("ok") is False]
        yield Finding("damaged_audio", "error", "file", row["id"],
                      detail={"path": row["path"], "work_title": row["title"], "failed_checks": failed})


def _missing_chapters(ctx: DetectorContext) -> Iterable[Finding]:
    """Single-file m4b longer than an hour with at most one chapter; multipart assets are excluded."""
    for row in _files("f.missing=0 AND f.duration_seconds>? AND f.chapter_count<=1", (LONG_AUDIOBOOK_SECONDS,)):
        if row["path"].lower().endswith(".m4b") and row["asset_files"] == 1:
            yield Finding("missing_chapters", "warning", "file", row["id"],
                          detail={"path": row["path"], "work_title": row["title"],
                                  "duration_seconds": row["duration_seconds"], "chapter_count": row["chapter_count"]})


# ---------------------------------------------------------------------------
# Covers and embedded metadata
# ---------------------------------------------------------------------------

def _missing_cover(ctx: DetectorContext) -> Iterable[Finding]:
    for row in _files("f.missing=0 AND f.has_embedded_cover=0"):
        if _is_audio(row["path"]) or _is_epub(row["path"]):
            yield Finding("missing_cover", "info", "file", row["id"], "embedded",
                          {"path": row["path"], "work_title": row["title"]}, repair_action="embed_cover")
    for w in db.all(
        "SELECT w.id,w.title FROM works w WHERE COALESCE(w.cover_url,'')='' "
        "AND EXISTS (SELECT 1 FROM media_assets a WHERE a.work_id=w.id AND a.status='owned') ORDER BY w.id"
    ):
        if not covers.has_cached(w["id"]):
            yield Finding("missing_cover", "warning", "work", w["id"], "no_cover", {"work_title": w["title"]})


def _tag_gaps(row: dict[str, Any]) -> list[str] | None:
    """Which of title/author the file's embedded metadata lacks; None when it was never read."""
    if _is_audio(row["path"]):
        if row["has_embedded_cover"] is None:
            return None  # never probed
        tags = db.json_load(row["embedded_json"], {}).get("embedded_tags") or {}
        has_title = bool(str(tags.get("title") or "").strip())
        has_author = any(str(tags.get(k) or "").strip() for k in ("artist", "album_artist", "author"))
    elif _is_epub(row["path"]):
        cache = _epub_cache(row)
        if not cache or cache.get("problems"):
            return None  # not checked yet, or too broken to say what it carries
        has_title, has_author = bool(cache.get("title")), bool(cache.get("authors"))
    else:
        return None
    return [n for n, ok in (("title", has_title), ("author", has_author)) if not ok]


def _missing_embedded_metadata(ctx: DetectorContext) -> Iterable[Finding]:
    for row in _files("f.missing=0"):
        gaps = _tag_gaps(row)
        if gaps:
            yield Finding("missing_embedded_metadata", "warning", "file", row["id"],
                          detail={"path": row["path"], "work_title": row["title"], "missing": gaps}, repair_action="retag")


def unknown_counts() -> dict[str, int]:
    """Files whose quality is unknown because they were never probed; reported, never findings."""
    rows = _files("f.missing=0")
    audio = [r for r in rows if _is_audio(r["path"])]
    return {
        "audio_not_probed": sum(1 for r in audio if r["has_embedded_cover"] is None),
        "audio_not_verified": sum(1 for r in audio if r["integrity_state"] is None),
        "epub_not_checked": sum(1 for r in rows if _is_epub(r["path"]) and _epub_cache(r) is None),
    }


# invalid-epub registers first: missing-embedded-metadata reads the EPUB results it caches.
register_detector("invalid-epub", ("invalid_epub",), _invalid_epub, limit=5000, seconds=60.0)
register_detector("damaged-audio", ("damaged_audio",), _damaged_audio, limit=5000, seconds=60.0)
register_detector("missing-chapters", ("missing_chapters",), _missing_chapters, limit=5000)
register_detector("missing-cover", ("missing_cover",), _missing_cover, limit=10000)
register_detector("missing-embedded-metadata", ("missing_embedded_metadata",), _missing_embedded_metadata, limit=10000)
