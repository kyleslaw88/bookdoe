from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import zipfile
from xml.etree import ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring
from typing import Any

from .. import db
from ..config import settings
from . import derived, managedbuild, metadata
from . import paths as path_guard
from . import pathevidence

# Default thresholds – used when the settings table has no value yet.
DEFAULT_AUTO_ACCEPT_THRESHOLD = 0.95
DEFAULT_PROBABLE_THRESHOLD = 0.85


def _auto_accept_threshold() -> float:
    val = db.get_setting("matching.auto_accept_threshold", DEFAULT_AUTO_ACCEPT_THRESHOLD)
    try:
        v = float(val)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_ACCEPT_THRESHOLD
    return max(0.90, min(1.0, v))


def _probable_threshold() -> float:
    val = db.get_setting("matching.probable_threshold", DEFAULT_PROBABLE_THRESHOLD)
    try:
        v = float(val)
    except (TypeError, ValueError):
        return DEFAULT_PROBABLE_THRESHOLD
    return max(0.50, min(1.0, v))

log = logging.getLogger(__name__)

EBOOK_EXTS = {".epub", ".azw3", ".mobi", ".pdf", ".azw", ".fb2", ".cbz", ".cbr"}
AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac"}
MEDIA_EXTS = EBOOK_EXTS | AUDIO_EXTS


def kind_for_path(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in EBOOK_EXTS:
        return "ebook"
    if ext in AUDIO_EXTS:
        return "audiobook"
    return None


def inspect_file(path: Path, root: Path | None = None) -> dict[str, Any]:
    stat = path.stat()
    # Structured path evidence (Part XVIII) — computed first so embedded
    # metadata can override it below.
    evidence = pathevidence.parse_path_evidence(str(path), root)

    detected: dict[str, Any] = {
        "path": str(path),
        "filename": path.name,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "extension": path.suffix.lower().lstrip("."),
        "kind": kind_for_path(path),
        "title": evidence.get("title") or _guess_title(path),
        "authors": [evidence["author"]] if evidence.get("author") else _guess_authors(path),
        "identifiers": dict(evidence.get("identifiers", {})),
        "evidence": evidence,
    }

    # Embedded metadata (EPUB OPF, ffprobe) overrides path evidence
    if path.suffix.lower() == ".epub":
        ep = _read_epub(path)
        if ep.get("title"):
            detected["title"] = ep["title"]
        if ep.get("authors"):
            detected["authors"] = ep["authors"]
        if ep.get("identifiers"):
            detected["identifiers"].update(ep["identifiers"])
        if "drm" in ep:
            detected["drm"] = ep["drm"]
    elif path.suffix.lower() in AUDIO_EXTS:
        # Containment before any subprocess touches the file (Part III); an
        # unrooted caller (root=None) has nothing to contain against.
        fp = _ffprobe(path) if root is None or path_guard.resolve_safe(root, path) is not None else {}
        if fp.get("title"):
            detected["title"] = fp["title"]
        if fp.get("authors"):
            detected["authors"] = fp["authors"]
        if fp.get("narrators"):
            detected["narrators"] = fp["narrators"]
        if fp.get("bitrate"):
            detected["bitrate"] = fp["bitrate"]
        if fp.get("duration_seconds"):
            detected["duration_seconds"] = fp["duration_seconds"]
        for key in STREAM_KEYS:
            if fp.get(key) is not None:
                detected[key] = fp[key]
        if fp.get("embedded_tags"):
            detected["embedded_tags"] = fp["embedded_tags"]
    detected["fingerprint"] = fingerprint(path)
    return detected


def fingerprint(path: Path) -> str:
    """Stable cheap fingerprint; full SHA-256 for small files, sampled hash for huge audio."""
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    limit = settings.scan_hash_full_max_mb * 1024 * 1024
    with path.open("rb") as f:
        if size <= limit:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        else:
            h.update(f.read(4 * 1024 * 1024))
            mid = max(0, size // 2 - 2 * 1024 * 1024)
            f.seek(mid)
            h.update(f.read(4 * 1024 * 1024))
            f.seek(max(0, size - 4 * 1024 * 1024))
            h.update(f.read(4 * 1024 * 1024))
    return h.hexdigest()


def full_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# Algorithms in META-INF/encryption.xml that only obfuscate embedded fonts (not DRM).
FONT_OBFUSCATION_ALGORITHMS = frozenset({
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
})


def _epub_drm(zf: zipfile.ZipFile) -> bool | None:
    """True when encryption.xml names a non-font cipher, False when absent/font-only, None if unreadable."""
    try:
        raw = zf.read("META-INF/encryption.xml")
    except KeyError:
        return False
    try:
        root = safe_fromstring(raw)
    except (ET.ParseError, ValueError):
        return None
    algorithms = {m.attrib.get("Algorithm", "") for m in root.findall(".//{*}EncryptionMethod")}
    return bool(algorithms - FONT_OBFUSCATION_ALGORITHMS)


def _read_epub(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            drm = _epub_drm(zf)
            if drm is not None:
                result["drm"] = drm
            container = safe_fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return result
            opf = safe_fromstring(zf.read(rootfile.attrib["full-path"]))
            metadata_el = opf.find(".//{*}metadata")
            if metadata_el is None:
                return result
            titles = [x.text.strip() for x in metadata_el.findall("{*}title") if x.text and x.text.strip()]
            creators = [x.text.strip() for x in metadata_el.findall("{*}creator") if x.text and x.text.strip()]
            identifiers: dict[str, str] = {}
            for x in metadata_el.findall("{*}identifier"):
                if not x.text:
                    continue
                value = x.text.strip()
                scheme = ""
                for k, v in x.attrib.items():
                    if k.lower().endswith("scheme"):
                        scheme = v.lower()
                isbn = re.sub(r"[^0-9Xx]", "", value)
                if scheme == "isbn" or len(isbn) in (10, 13):
                    identifiers["isbn13" if len(isbn) == 13 else "isbn10"] = isbn.upper()
            if titles:
                result["title"] = titles[0]
            if creators:
                result["authors"] = creators
            result["identifiers"] = identifiers
            language = metadata_el.find("{*}language")
            if language is not None and language.text:
                result["language"] = language.text.strip()
    except Exception as exc:
        result["epub_error"] = str(exc)
    return result


MAX_CHAPTERS = 500
# Keys _ffprobe() adds to detected_json beyond the tag-derived ones; chapters is the
# capped detail list, chapter_count the true count.
STREAM_KEYS = ("codec", "sample_rate", "channels", "bit_depth", "chapters", "chapter_count", "has_embedded_cover")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if p.returncode != 0:
            return {}
        data = json.loads(p.stdout or "{}")
        fmt = data.get("format") or {}
        tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
        streams = data.get("streams") or []
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        raw_chapters = data.get("chapters") or []
        chapters = [
            {
                "start": _float_or_none(c.get("start_time")),
                "end": _float_or_none(c.get("end_time")),
                "title": {str(k).lower(): v for k, v in (c.get("tags") or {}).items()}.get("title"),
            }
            for c in raw_chapters[:MAX_CHAPTERS]
        ]
        result: dict[str, Any] = {
            "duration_seconds": int(float(fmt.get("duration") or 0)) or None,
            "bitrate": int(fmt.get("bit_rate") or 0) or None,
            "chapters": chapters,
            "chapter_count": len(raw_chapters),
            "codec": audio.get("codec_name"),
            "sample_rate": _int_or_none(audio.get("sample_rate")),
            "channels": _int_or_none(audio.get("channels")),
            "bit_depth": _int_or_none(audio.get("bits_per_raw_sample")) or _int_or_none(audio.get("bits_per_sample")),
            "has_embedded_cover": int(any(
                s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1 for s in streams
            )),
            "embedded_tags": tags,
        }
        if tags.get("title"):
            result["title"] = tags["title"]
        author = tags.get("artist") or tags.get("album_artist") or tags.get("author")
        if author:
            result["authors"] = [x.strip() for x in re.split(r"[;&]", str(author)) if x.strip()]
        narrator = tags.get("narrator") or tags.get("composer")
        if narrator:
            result["narrators"] = [x.strip() for x in re.split(r"[;&]", str(narrator)) if x.strip()]
        return result
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def probe_columns(detected: dict[str, Any]) -> dict[str, Any]:
    """media_files stream columns for a detected dict; all None when the file was never probed.

    has_embedded_cover is only set by a successful ffprobe, so it doubles as the
    "probed" marker. A probed file with no audio stream stores 0 (not NULL) for
    sample_rate/channels so the backfill does not retry it forever.
    """
    if detected.get("has_embedded_cover") is None:
        return {"codec": None, "sample_rate": None, "channels": None, "chapter_count": None, "has_embedded_cover": None}
    return {
        "codec": detected.get("codec"),
        "sample_rate": detected.get("sample_rate") or 0,
        "channels": detected.get("channels") or 0,
        "chapter_count": detected.get("chapter_count") or 0,
        "has_embedded_cover": int(bool(detected["has_embedded_cover"])),
    }


def _root_for(path: Path) -> Path | None:
    """The enabled library root that safely contains *path*, longest match first."""
    for r in db.all("SELECT path FROM root_folders WHERE enabled=1 ORDER BY LENGTH(path) DESC"):
        if path_guard.resolve_safe(Path(r["path"]), path) is not None:
            return Path(r["path"])
    return None


def backfill_stream_data(limit: int = 200) -> dict[str, int]:
    """Probe up to *limit* present audio files that predate stream-data persistence.

    Read-only on disk, and only the stream columns plus embedded_json change: size,
    mtime, fingerprint and missing are never touched, so a file that cannot be
    probed (or is gone) simply stays NULL.
    """
    rows = db.all(
        """SELECT f.id,f.path,f.embedded_json FROM media_files f JOIN media_assets a ON a.id=f.asset_id
           WHERE f.sample_rate IS NULL AND f.missing=0 AND a.kind='audiobook'
           ORDER BY f.discovered_at, f.id LIMIT ?""",
        (max(0, int(limit)),),
    )
    filled = 0
    for f in rows:
        path = Path(f["path"])
        if _root_for(path) is None:
            continue
        fp = _ffprobe(path)
        cols = probe_columns(fp)
        if cols["has_embedded_cover"] is None:
            continue
        embedded = db.json_load(f["embedded_json"], {})
        embedded.update({k: fp[k] for k in STREAM_KEYS if fp.get(k) is not None})
        with db.tx() as conn:
            conn.execute(
                "UPDATE media_files SET codec=?,sample_rate=?,channels=?,chapter_count=?,has_embedded_cover=?,embedded_json=? WHERE id=?",
                (cols["codec"], cols["sample_rate"], cols["channels"], cols["chapter_count"], cols["has_embedded_cover"], db.json_dump(embedded), f["id"]),
            )
        filled += 1
    return {"candidates": len(rows), "filled": filled}


def _guess_title(path: Path) -> str:
    # Prefer parent folder for part files; otherwise filename without release cruft.
    stem = path.stem
    if re.search(r"(?i)(part|disc|cd)[ ._-]*\d+$", stem) and path.parent.name:
        stem = path.parent.name
    stem = re.sub(r"\[[^\]]+\]", " ", stem)
    stem = re.sub(r"(?i)\b(19|20)\d{2}\b", " ", stem)
    stem = re.sub(r"(?i)\b(epub|mobi|azw3|m4b|mp3|audiobook|unabridged|webrip|webdl)\b", " ", stem)
    stem = re.sub(r"[._]+", " ", stem)
    stem = re.sub(r"\s+-\s+\d{1,3}$", "", stem)
    return re.sub(r"\s+", " ", stem).strip(" -_") or path.stem


def _guess_authors(path: Path) -> list[str]:
    # Common libraries are Author/Book/file or Book/file. Take grandparent only
    # when there is enough depth; do not pretend it is certain.
    parent = path.parent
    if parent.parent != parent and parent.parent.name and parent.parent.name not in {"books", "audiobooks", "library", "media"}:
        candidate = parent.parent.name
        if not re.search(r"(?i)season|series|book \d", candidate):
            return [candidate]
    return []


def _scan_podcast_root(root: dict[str, Any]) -> dict[str, Any]:
    """A podcasts library goes to the podcast scanner (#511); it has no book scan run or candidates."""
    from . import podcastscan
    res = podcastscan.scan_podcasts(root["path"])
    with db.tx() as conn:
        conn.execute("UPDATE root_folders SET last_scan_at=? WHERE id=?", (db.now(), root["id"]))
    return {"library_type": "podcasts", "shows": res.shows, "episodes_added": res.episodes_added,
            "episodes_updated": res.episodes_updated, "skipped": len(res.skipped)}


def scan_root(root_id: str, *, auto_adopt_exact: bool = True) -> dict[str, Any]:
    root = db.one("SELECT * FROM root_folders WHERE id=?", (root_id,))
    if not root:
        raise ValueError("Root folder not found")
    base = Path(root["path"])
    if not base.exists() or not base.is_dir():
        raise ValueError(f"Root path is not a directory: {base}")
    if root.get("library_type") == "podcasts":
        return _scan_podcast_root(root)
    run_id = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO scan_runs(id,root_folder_id,status,started_at) VALUES (?,?,'running',?)",
            (run_id, root_id, db.now()),
        )
    counts = {"files_seen": 0, "matched": 0, "ambiguous": 0, "unmatched": 0, "errors": 0, "probable": 0}
    for path in path_guard.scan_safe(base):
        if path.suffix.lower() not in MEDIA_EXTS:
            continue
        kind = kind_for_path(path)
        if root["media_kind"] != "mixed" and root["media_kind"] != kind:
            continue
        counts["files_seen"] += 1
        # A converted copy bookdoe made is already registered to its derived asset; it is not a find.
        if derived.is_generated_path(str(path)):
            continue
        # A preserved original of a managed copy, or a copy the managed library build owns: already in the library
        # (as the managed copy), so never a find, never a duplicate (services/managedbuild.py).
        if managedbuild.is_build_path(str(path)):
            continue
        try:
            detected = inspect_file(path, root=base)
            match = match_detected(detected)
            status = match["status"]
            counts[status] = counts.get(status, 0) + 1
            candidate_id = db.uid()
            with db.tx() as conn:
                conn.execute(
                    """INSERT INTO scan_candidates(id,scan_run_id,path,media_kind,detected_json,match_work_id,match_edition_id,confidence,status,reasons_json,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id, run_id, str(path), kind, db.json_dump(detected), match.get("work_id"), match.get("edition_id"),
                        match.get("confidence", 0), status, db.json_dump(match.get("reasons") or []), db.now(),
                    ),
                )
            if auto_adopt_exact and root["management_mode"] in ("adopt", "managed") and status == "matched" and match.get("work_id"):
                adopt_candidate(candidate_id)
        except Exception as exc:
            log.exception("scan failed for %s", path)
            counts["errors"] += 1
    with db.tx() as conn:
        conn.execute(
            """UPDATE scan_runs SET status='complete',finished_at=?,files_seen=?,matched=?,ambiguous=?,unmatched=?,errors=?,summary_json=? WHERE id=?""",
            (db.now(), counts["files_seen"], counts["matched"], counts["ambiguous"], counts["unmatched"], counts["errors"], db.json_dump(counts), run_id),
        )
        conn.execute("UPDATE root_folders SET last_scan_at=? WHERE id=?", (db.now(), root_id))
    return {"run_id": run_id, **counts}


def match_detected(detected: dict[str, Any]) -> dict[str, Any]:
    # 1) Local exact identifiers.
    for ns in ("isbn13", "isbn10", "asin", "audible"):
        val = (detected.get("identifiers") or {}).get(ns)
        if not val:
            continue
        row = db.one(
            "SELECT entity_type,entity_id FROM identifiers WHERE namespace=? AND value=? ORDER BY verified DESC,confidence DESC LIMIT 1",
            (ns, val),
        )
        if row:
            work_id, edition_id = None, None
            if row["entity_type"] == "work":
                work_id = row["entity_id"]
            elif row["entity_type"] == "edition":
                edition_id = row["entity_id"]
                ed = db.one("SELECT work_id FROM editions WHERE id=?", (edition_id,))
                work_id = ed["work_id"] if ed else None
            if work_id:
                return {"status": "matched", "work_id": work_id, "edition_id": edition_id, "confidence": 1.0, "reasons": [f"exact {ns}"]}
    # 2) Ask OpenReads resolver; import an exact/strong match into local catalog.
    try:
        result = metadata.resolve(
            identifiers=detected.get("identifiers") or {},
            title=detected.get("title"),
            authors=detected.get("authors") or [],
        )
        if result.get("match") and float(result.get("confidence", 0)) >= _auto_accept_threshold():
            work_id = metadata.upsert_work(result["match"])
            edition_id = None
            if result.get("edition_id"):
                e = db.one("SELECT id FROM editions WHERE metadata_id=?", (result["edition_id"],))
                edition_id = e["id"] if e else None
            return {
                "status": "matched",
                "work_id": work_id,
                "edition_id": edition_id,
                "confidence": float(result.get("confidence", 1.0)),
                "reasons": [result.get("method", "metadata resolver")],
            }
        if result.get("match"):
            return {"status": "ambiguous", "confidence": float(result.get("confidence", 0)), "reasons": ["resolver match below auto_accept threshold"]}
        if result.get("candidates"):
            return {"status": "ambiguous", "confidence": float(result.get("confidence", 0)), "reasons": ["metadata candidates require review"]}
    except Exception as exc:
        log.warning("metadata resolution failed for %s: %s", detected.get("path"), exc)
    # 3) Local title/author matching.
    title = (detected.get("title") or "").strip()
    if title:
        rows = db.all("SELECT id,title FROM works WHERE lower(title)=lower(?)", (title,))
        if len(rows) == 1:
            # Title-only probable keeps a fixed confidence (0.85) regardless of the
            # probable_threshold setting.  The setting is a display/gating hint only;
            # the confidence score itself is not the setting value.
            return {"status": "probable", "work_id": rows[0]["id"], "confidence": 0.85, "reasons": ["exact normalized title only"]}
        if len(rows) > 1:
            return {"status": "ambiguous", "confidence": 0.5, "reasons": ["multiple local title matches"]}
    return {"status": "unmatched", "confidence": 0, "reasons": ["insufficient matching evidence"]}


def adopt_candidate(candidate_id: str, *, work_id: str | None = None, edition_id: str | None = None) -> str:
    c = db.one("SELECT * FROM scan_candidates WHERE id=?", (candidate_id,))
    if not c:
        raise ValueError("Candidate not found")
    work_id = work_id or c["match_work_id"]
    edition_id = edition_id or c["match_edition_id"]
    if not work_id:
        raise ValueError("Candidate has no selected work")
    if managedbuild.is_build_path(c["path"]):
        raise ValueError("This file is the preserved original of a managed copy (or a managed copy); it is already in the library")
    detected = db.json_load(c["detected_json"], {})
    kind = c["media_kind"]
    fmt = Path(c["path"]).suffix.lower().lstrip(".")
    # Prefer an existing generic or same-format asset.
    asset = db.one(
        "SELECT * FROM media_assets WHERE work_id=? AND kind=? AND (format=? OR format IS NULL) AND derived_from_asset_id IS NULL "
        "ORDER BY format IS NULL DESC LIMIT 1",
        (work_id, kind, fmt),
    )
    asset_id = asset["id"] if asset else db.uid()
    with db.tx() as conn:
        # A real file of this format takes the slot a converted copy of the same format held.
        superseded = derived.supersede(conn, work_id, kind, fmt)
        if not asset:
            conn.execute(
                """INSERT INTO media_assets(id,work_id,edition_id,kind,format,status,monitored,profile_id,added_at,updated_at)
                   VALUES (?,?,?,?,?,'owned',1,?,?,?)""",
                (asset_id, work_id, edition_id, kind, fmt, "audio-default" if kind == "audiobook" else "ebook-default", db.now(), db.now()),
            )
        else:
            conn.execute(
                "UPDATE media_assets SET edition_id=COALESCE(?,edition_id),format=COALESCE(format,?),status='owned',updated_at=? WHERE id=?",
                (edition_id, fmt, db.now(), asset_id),
            )
        existing = conn.execute("SELECT id FROM media_files WHERE path=?", (c["path"],)).fetchone()
        file_id = existing["id"] if existing else db.uid()
        pc = probe_columns(detected)
        conn.execute(
            """INSERT INTO media_files(id,asset_id,path,size_bytes,mtime,fingerprint,container,codec,sample_rate,channels,chapter_count,has_embedded_cover,bitrate,duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,0,?,?)
               ON CONFLICT(path) DO UPDATE SET asset_id=excluded.asset_id,size_bytes=excluded.size_bytes,mtime=excluded.mtime,
                 fingerprint=excluded.fingerprint,container=excluded.container,bitrate=excluded.bitrate,duration_seconds=excluded.duration_seconds,
                 codec=COALESCE(excluded.codec,codec),sample_rate=COALESCE(excluded.sample_rate,sample_rate),channels=COALESCE(excluded.channels,channels),
                 chapter_count=COALESCE(excluded.chapter_count,chapter_count),has_embedded_cover=COALESCE(excluded.has_embedded_cover,has_embedded_cover),
                 embedded_json=excluded.embedded_json,missing=0,verified_at=excluded.verified_at""",
            (
                file_id, asset_id, c["path"], int(detected.get("size_bytes") or 0), detected.get("mtime"), detected.get("fingerprint"), fmt,
                pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"],
                detected.get("bitrate"), detected.get("duration_seconds"), db.json_dump(detected), db.now(), db.now(),
            ),
        )
        conn.execute("UPDATE scan_candidates SET status='matched',match_work_id=?,match_edition_id=?,confidence=MAX(confidence,0.99) WHERE id=?", (work_id, edition_id, candidate_id))
    derived.remove_generated_files(superseded)
    db.add_history("library.adopted", f"Adopted {Path(c['path']).name} in place", work_id=work_id, asset_id=asset_id, data={"path": c["path"]})
    return asset_id


# Matches the actual keys inspect_file()/_read_epub()/_ffprobe() write into
# detected_json above (authors/identifiers, not author/identifier) -- not the
# vNext ledger's loose prose description of them.
SCAN_CANDIDATE_EDITABLE_FIELDS = ("title", "authors", "series", "series_position", "identifiers")


def edit_candidate(candidate_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Persist an edited subset of a scan candidate's own detected fields (Part XX).

    This only changes *this candidate's* proposal (its own detected_json), never
    a Work -- and it never re-runs match_detected(), so an edit cannot silently
    bypass the identifier-first matching Part XIX locks down. Adopting the
    candidate afterward reads whatever is in detected_json at that time, so the
    edited values -- not the originally-detected ones -- are what a caller sees.
    """
    c = db.one("SELECT * FROM scan_candidates WHERE id=?", (candidate_id,))
    if not c:
        raise ValueError("Candidate not found")
    unknown = set(fields) - set(SCAN_CANDIDATE_EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown editable field(s): {', '.join(sorted(unknown))}")
    detected = db.json_load(c["detected_json"], {})
    detected.update(fields)
    with db.tx() as conn:
        conn.execute("UPDATE scan_candidates SET detected_json=? WHERE id=?", (db.json_dump(detected), candidate_id))
    x = db.one("SELECT * FROM scan_candidates WHERE id=?", (candidate_id,))
    x["detected"] = db.json_load(x.pop("detected_json"), {})
    x["reasons"] = db.json_load(x.pop("reasons_json"), [])
    return x


def ignore_candidate(candidate_id: str) -> None:
    """Mark a scan candidate as explicitly ignored (Part XX).

    Distinct from the system's own 'unmatched'/'ambiguous' classification --
    'ignored' is an explicit user "stop offering me this file" choice, the
    scan_candidates counterpart to review_tasks' existing /ignore action.
    'ignored' is already a legal status in the CHECK constraint (confirmed by
    reading db.py and main.py's SCAN_CANDIDATE_STATUSES directly, not assumed)
    but no action previously set it.
    """
    if not db.one("SELECT id FROM scan_candidates WHERE id=?", (candidate_id,)):
        raise ValueError("Candidate not found")
    with db.tx() as conn:
        conn.execute("UPDATE scan_candidates SET status='ignored' WHERE id=?", (candidate_id,))
