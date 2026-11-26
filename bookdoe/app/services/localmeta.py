from __future__ import annotations

import logging
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from defusedxml.ElementTree import fromstring as safe_fromstring

from .. import db
from . import observations

log = logging.getLogger(__name__)

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac"}

# "Dead World: Convergence, Book 6" -> title, series, position. Deliberately strict:
# it must end with an explicit Book/Part/Volume number, because a loose pattern turns
# every subtitled book into a fake series entry.
_SERIES_IN_TITLE = re.compile(
    r"^(?P<title>.+?)\s*[:\-–]\s*(?P<series>.+?),?\s*(?:Book|Bk\.?|Part|Volume|Vol\.?)\s*(?P<position>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


# Audible ASINs are B0 + 8 alphanumerics; older titles reuse the ISBN-10.
_ASIN = re.compile(r"^(?:B0[0-9A-Z]{8}|\d{9}[\dX])$")
_ASIN_TOKEN = re.compile(r"\bB0[0-9A-Z]{8}\b")


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def read_epub(path: Path) -> dict[str, Any]:
    """Bibliographic metadata from an epub's OPF, including Calibre's series fields."""
    out: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            container = safe_fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return out
            opf = safe_fromstring(zf.read(rootfile.attrib["full-path"]))
            meta = opf.find(".//{*}metadata")
            if meta is None:
                return out
            for child in meta:
                tag = child.tag.split("}")[-1]
                text = _clean(child.text)
                if tag == "description" and text:
                    out["description"] = re.sub(r"<[^>]+>", "", text).strip()
                elif tag == "language" and text:
                    out["language"] = text
                elif tag == "date" and text:
                    out["date"] = text
                elif tag == "title" and text and "title" not in out:
                    out["title"] = text
                elif tag == "meta":
                    name = (child.get("name") or "").lower()
                    content = _clean(child.get("content"))
                    if name == "calibre:series" and content:
                        out["series"] = content
                    elif name == "calibre:series_index" and content:
                        out["series_position"] = content.rstrip("0").rstrip(".") if "." in content else content
    except Exception as exc:
        log.debug("epub metadata read failed for %s: %s", path, exc)
    return out


def read_audio(path: Path) -> dict[str, Any]:
    """Bibliographic metadata from an audiobook's container tags."""
    out: dict[str, Any] = {}
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if probe.returncode != 0:
            return out
        import json as _json
        tags = {str(k).lower(): v for k, v in ((_json.loads(probe.stdout or "{}").get("format") or {}).get("tags") or {}).items()}
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as exc:
        log.debug("audio metadata read failed for %s: %s", path, exc)
        return out

    if _clean(tags.get("title")):
        out["title"] = _clean(tags.get("title"))
    # Audiobook convention: the narrator is credited in composer, occasionally in an
    # explicit narrator tag. Artist is the author, not the narrator.
    narrator = _clean(tags.get("narrator")) or _clean(tags.get("composer"))
    if narrator:
        out["narrators"] = [x.strip() for x in re.split(r"[;&,]| and ", narrator) if x.strip()]
    for key in ("description", "comment", "synopsis"):
        if _clean(tags.get(key)):
            out["description"] = _clean(tags.get(key))
            break
    if _clean(tags.get("date")):
        out["date"] = _clean(tags.get("date"))
    if _clean(tags.get("language")):
        out["language"] = _clean(tags.get("language"))
    asin = _find_asin(tags)
    if asin:
        out["asin"] = asin
    abridged = _find_abridged(tags)
    if abridged is not None:
        out["abridged"] = abridged
    for key in ("publisher", "label", "organization"):
        if _clean(tags.get(key)):
            out["publisher"] = _clean(tags.get(key))
            break
    for key in ("releasedate", "date", "originalyear"):
        if _clean(tags.get(key)):
            out["release_date"] = _clean(tags.get(key))
            break
    # Some taggers put "Series, Book N" in album; others repeat the title there.
    for source in (out.get("title"), _clean(tags.get("album"))):
        match = _SERIES_IN_TITLE.match(source or "")
        if match:
            out.setdefault("series", match.group("series").strip())
            out.setdefault("series_position", match.group("position"))
            out["title"] = match.group("title").strip()
            break
    return out


def _find_asin(tags: dict[str, Any]) -> str | None:
    """A valid ASIN from the dedicated tags, else a B0XXXXXXXX token in the free text."""
    for key in ("asin", "cdek", "audible_asin"):
        value = (_clean(tags.get(key)) or "").upper()
        if _ASIN.match(value):
            return value
    for key in ("comment", "description", "synopsis"):
        match = _ASIN_TOKEN.search(str(tags.get(key) or ""))
        if match:
            return match.group()
    return None


def _find_abridged(tags: dict[str, Any]) -> int | None:
    """1 for abridged, 0 for unabridged, None when the tags do not say."""
    def flag(value: Any) -> bool | None:
        text = (_clean(value) or "").lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
        return None

    explicit = flag(tags.get("abridged"))
    if explicit is not None:
        return int(explicit)
    inverse = flag(tags.get("unabridged"))
    if inverse is not None:
        return int(not inverse)
    for key in ("comment", "description", "synopsis"):
        text = str(tags.get(key) or "").lower()
        if re.search(r"\bunabridged\b", text):
            return 0
        if re.search(r"\babridged\b", text):
            return 1
    return None


def read_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return read_epub(path)
    if suffix in AUDIO_EXTS:
        return read_audio(path)
    return {}


def _year(value: str | None) -> str | None:
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(value or ""))
    return match.group() if match else None


_AUDIO_ONLY = {"asin", "abridged", "publisher", "release_date"}


def harvest(work_id: str) -> dict[str, Any]:
    """Fill in a work's blanks from the files the user already owns.

    For anything but the best-known titles, the user's own file is the better source:
    a 2026 Audible-exclusive has a series, a position, a year and a narrator sitting
    in its tags while no public provider has heard of it. This only ever *fills gaps* -
    it never overwrites a value that metadata resolution has already supplied, so a
    resolved work is untouched.
    """
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not work:
        raise ValueError("Work not found")

    rows = db.all(
        """SELECT f.path, a.id asset_id, a.kind FROM media_files f
           JOIN media_assets a ON a.id=f.asset_id
           WHERE a.work_id=? AND f.missing=0 ORDER BY COALESCE(f.part_number,1),f.path""",
        (work_id,),
    )
    applied: dict[str, Any] = {}
    combined: dict[str, Any] = {}
    for row in rows:
        path = Path(row["path"])
        if not path.exists():
            continue
        data = read_file(path)
        if not data:
            continue
        if row["kind"] == "audiobook":
            _fill_audiobook_fields(work_id, row["asset_id"], data, applied)
        # Ebook tags win on bibliographic fields (Calibre keeps them properly);
        # narrators can only come from the audiobook.
        for key, value in data.items():
            if key == "narrators":
                if row["kind"] == "audiobook" and value:
                    _set_narrators(row["asset_id"], value, work_id)
                    applied.setdefault("narrators", value)
                continue
            if key in _AUDIO_ONLY:
                continue
            if key not in combined or path.suffix.lower() == ".epub":
                combined[key] = value

    updates: list[str] = []
    params: list[Any] = []
    seen = {
        "description": combined.get("description"),
        "first_publish_date": _year(combined.get("date")),
        "primary_language": combined.get("language"),
    }
    with db.tx() as conn:
        t = db.now()
        for field, value in seen.items():
            if not value:
                continue
            # Evidence is always kept; the column is only filled when it is blank
            # and no manual lock covers it.
            fill = not work.get(field) and not observations.is_locked(conn, "work", work_id, field)
            observations.record(conn, "work", work_id, field, value, "local", applied=fill, t=t)
            if fill:
                updates.append(f"{field}=?")
                params.append(value)
                applied[field] = value[:60] if field == "description" else value
        if updates:
            conn.execute(f"UPDATE works SET {','.join(updates)},updated_at=? WHERE id=?", tuple(params + [t, work_id]))  # nosec B608

    if combined.get("series") and not db.one("SELECT 1 FROM series_memberships WHERE work_id=?", (work_id,)):
        attached = _attach_series(work_id, combined["series"], combined.get("series_position"))
        if attached:
            applied["series"] = "; ".join(f"{n} #{p or '?'}" for n, p in attached)

    if applied and not work.get("metadata_id") and work.get("metadata_source") != "manual":
        # 'local' sits between 'provider' (a metadata service vouched for this) and
        # nothing at all - it is real data, just sourced from the file rather than a
        # provider, and must not render with the same warning color as "unresolved".
        # A prior manual correction is never demoted back to an automatic source.
        with db.tx() as conn:
            conn.execute("UPDATE works SET metadata_source='local' WHERE id=? AND metadata_source IS NOT 'manual'", (work_id,))

    # Remembered against the set of files read, so a work is not re-probed every pass
    # but IS re-read when its files change.
    fingerprint = "|".join(sorted(r["path"] for r in rows))
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO local_meta(work_id,files_fingerprint,applied_json,harvested_at) VALUES (?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET files_fingerprint=excluded.files_fingerprint,
                 applied_json=excluded.applied_json,harvested_at=excluded.harvested_at""",
            (work_id, fingerprint, db.json_dump(applied), db.now()),
        )
    if applied:
        db.add_history("metadata.local", f"Filled metadata for {work['title']} from its own files", work_id=work_id, data=applied)
    return {"work_id": work_id, "files_read": len(rows), "applied": applied}


def _fill_audiobook_fields(work_id: str, asset_id: str, data: dict[str, Any], applied: dict[str, Any]) -> None:
    """Gap-fill ASIN, abridged, publisher and release date from audiobook tags.

    Nothing non-empty is overwritten, and a tag-derived ASIN never takes an
    identifier another entity already owns.
    """
    asset = db.one("SELECT edition_id, abridged FROM media_assets WHERE id=?", (asset_id,))
    if not asset:
        return
    edition_id = asset["edition_id"]
    t = db.now()
    with db.tx() as conn:
        asin = data.get("asin")
        if asin and "asin" not in applied:
            entity_type, entity_id = ("edition", edition_id) if edition_id else ("work", work_id)
            has_one = conn.execute(
                "SELECT 1 FROM identifiers WHERE entity_type=? AND entity_id=? AND namespace='asin'", (entity_type, entity_id)
            ).fetchone()
            owner = conn.execute(
                "SELECT entity_type, entity_id FROM identifiers WHERE namespace='asin' AND value=?", (asin,)
            ).fetchone()
            if owner and (owner["entity_type"], owner["entity_id"]) != (entity_type, entity_id):
                log.info("ASIN %s from tags of work %s already belongs to %s %s; skipped", asin, work_id, owner["entity_type"], owner["entity_id"])
            elif not has_one:
                conn.execute(
                    "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES (?,?,?,?,?,0,0.7)",
                    (entity_type, entity_id, "asin", asin, "file-tags"),
                )
                applied["asin"] = asin
        if data.get("abridged") is not None and asset["abridged"] is None and "abridged" not in applied:
            conn.execute("UPDATE media_assets SET abridged=?,updated_at=? WHERE id=?", (data["abridged"], t, asset_id))
            applied["abridged"] = data["abridged"]
        if not edition_id:
            return
        edition = conn.execute("SELECT publisher, release_date FROM editions WHERE id=?", (edition_id,)).fetchone()
        for field in ("publisher", "release_date"):
            value = data.get(field)
            if not value or field in applied or not edition:
                continue
            fill = not (edition[field] or "").strip() and not observations.is_locked(conn, "edition", edition_id, field)
            observations.record(conn, "edition", edition_id, field, value, "file-tags", applied=fill, t=t)
            if fill:
                conn.execute(f"UPDATE editions SET {field}=?,updated_at=? WHERE id=?", (value, t, edition_id))  # nosec B608
                applied[field] = value


def _set_narrators(asset_id: str, narrators: list[str], work_id: str | None = None) -> None:
    existing = db.one("SELECT narrator_json FROM media_assets WHERE id=?", (asset_id,))
    if existing and db.json_load(existing["narrator_json"], []):
        return
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET narrator_json=?,updated_at=? WHERE id=?", (db.json_dump(narrators), db.now(), asset_id))
        # Also write work_contributors rows so narrators are first-class, queryable contributors.
        if work_id:
            for i, name in enumerate(narrators):
                cid = db.contributor_id_for_name(conn, name)
                if cid:
                    conn.execute(
                        "INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                        (work_id, cid, "narrator", i),
                    )


def _attach_series(work_id: str, name: str, position: str | None) -> list[tuple[str, str | None]]:
    """Attach a work to the series named by a file's tags, through the same parser as
    OpenReads-provided series (splits compound strings, drops imprints and catalog
    numbers). Returns the (name, position) pairs actually attached."""
    from . import metadata, seriesnorm  # lazy: metadata imports this package's siblings

    attached: list[tuple[str, str | None]] = []
    with db.tx() as conn:
        opts = seriesnorm.load_options(conn)
        cands = seriesnorm.parse_series(name, ignored=opts.ignored, max_position=opts.max_position)
        for cand in cands:
            if cand.ignored:
                if conn.execute("SELECT 1 FROM series_assertions WHERE work_id=? AND series_id IS NULL AND raw_value=? AND normalized_name=?", (work_id, name, cand.name)).fetchone():
                    continue
                metadata._insert_series_assertion(conn, work_id, None, name, cand.name, None, "free_text", 0.0, "rejected", db.now(), source="local")
                continue
            pos = cand.position
            if len(cands) == 1 and position and str(position).strip() and not seriesnorm.position_problem(position, opts.max_position):
                pos = seriesnorm.normalize_position(str(position))
            key = seriesnorm.norm_key(cand.name)
            row = conn.execute("SELECT id FROM series WHERE norm_key=? AND metadata_id IS NULL", (key,)).fetchone()
            if not row:
                row = conn.execute("SELECT id FROM series WHERE name=? COLLATE NOCASE AND metadata_id IS NULL", (cand.name,)).fetchone()
            series_id = row["id"] if row else db.uid()
            if not row:
                conn.execute(
                    "INSERT INTO series(id,metadata_id,name,sort_name,norm_key,created_at,updated_at) VALUES (?,NULL,?,?,?,?,?)",
                    (series_id, cand.name, cand.name, key, db.now(), db.now()),
                )
            suppressed = conn.execute(
                "SELECT 1 FROM series_assertions WHERE work_id=? AND series_id=? AND status='manually_suppressed'",
                (work_id, series_id),
            ).fetchone()
            if suppressed:
                # A manual suppression outranks file tags: keep the evidence, not the membership.
                metadata._insert_series_assertion(conn, work_id, series_id, name, cand.name, pos, "embedded_tag", 0.0, "rejected", db.now(), source="file-tags")
                continue
            others = conn.execute("SELECT COUNT(*) c FROM series_memberships WHERE series_id=? AND work_id!=?", (series_id, work_id)).fetchone()["c"]
            _, confidence, status = metadata._score_series_link(cand.name, pos, others)
            metadata._insert_series_assertion(conn, work_id, series_id, name, cand.name, pos, "embedded_tag", confidence, status, db.now(), source="file-tags")
            joined = not conn.execute("SELECT 1 FROM series_memberships WHERE work_id=? AND series_id=?", (work_id, series_id)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)",
                (work_id, series_id, pos),
            )
            if joined:
                from . import series

                series.apply_future_policy(conn, work_id, series_id)
            attached.append((cand.name, pos))
    return attached


def harvest_missing(limit: int = 200) -> dict[str, Any]:
    """Backfill works that own files and have not been read yet.

    Selection is by "not yet harvested", not by "still missing something": most books
    are missing a description because their file has none, and selecting on that would
    re-probe the same audiobooks forever without ever reaching the rest of the library.
    Works whose files change are re-read by import_download(), which calls harvest()
    directly; the stored fingerprint records which files a result came from.
    """
    rows = db.all(
        """SELECT DISTINCT w.id FROM works w
           JOIN media_assets a ON a.work_id=w.id
           JOIN media_files f ON f.asset_id=a.id AND f.missing=0
           LEFT JOIN local_meta lm ON lm.work_id=w.id
           WHERE lm.work_id IS NULL LIMIT ?""",
        (limit,),
    )
    filled = 0
    for row in rows:
        try:
            if harvest(row["id"])["applied"]:
                filled += 1
        except Exception as exc:
            log.warning("local metadata harvest failed for %s: %s", row["id"], exc)
    return {"considered": len(rows), "filled": filled}
