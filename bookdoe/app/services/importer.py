from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .. import db
from ..config import settings
from . import conversion, derived, scanner
from . import paths as path_guard

log = logging.getLogger(__name__)


def map_remote_path(client_id: str, path: str) -> str:
    mappings = db.all("SELECT remote_path,local_path FROM path_mappings WHERE client_id=? ORDER BY length(remote_path) DESC", (client_id,))
    for m in mappings:
        remote = m["remote_path"].rstrip("/\\")
        if path == remote or path.startswith(remote + "/") or path.startswith(remote + "\\"):
            suffix = path[len(remote):].lstrip("/\\")
            return str(Path(m["local_path"]) / Path(suffix))
    return path


def discover_download_files(path: str, kind: str) -> list[Path]:
    p = Path(path)
    allowed = scanner.AUDIO_EXTS if kind == "audiobook" else scanner.EBOOK_EXTS
    if p.is_file():
        return [p] if p.suffix.lower() in allowed else []
    if not p.exists():
        return []
    return sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in allowed)


# A completed download should be roughly the size the indexer advertised. A payload
# far larger than that means the source path is not this download's own payload --
# typically the client's shared completed-downloads directory -- and importing it
# would copy unrelated media into the library. Refuse rather than proceed.
OVERSIZE_TOLERANCE = 3.0
OVERSIZE_FLOOR_BYTES = 512 * 1024 * 1024


def _guard_against_oversized_import(download: dict[str, Any], source: str, files: list[Path]) -> None:
    release = db.one("SELECT size_bytes FROM releases WHERE id=?", (download.get("release_id"),)) if download.get("release_id") else None
    expected = int(release["size_bytes"]) if release and release["size_bytes"] else 0
    if expected <= 0:
        return
    try:
        total = sum(f.stat().st_size for f in files)
    except OSError:
        return
    limit = max(expected * OVERSIZE_TOLERANCE, expected + OVERSIZE_FLOOR_BYTES)
    if total > limit:
        raise ValueError(
            f"Refusing to import {len(files)} file(s) totalling {total / 1e9:.1f} GB from {source}: "
            f"the release advertised {expected / 1e9:.1f} GB. The source path is probably a shared "
            "download directory rather than this download's own payload."
        )


DEFAULT_TRANSFER_MODE = "copy"
DEFAULT_AUTOMATIC_IMPORT = True


def automatic_import_enabled() -> bool:
    """Whether a completed download is imported without being asked.

    On by default. Import was manual because an early defect copied 826 files / 71GB
    from a wrong source path; that is fixed, and the download path is now taken
    per-client and checked against the advertised release size. The acquisition
    decision was already made when the release was grabbed, so stopping to ask again
    at the end is friction rather than safety - and anything that does fail still lands
    in the queue as import_failed for a manual retry.
    """
    return bool(db.get_setting("import.automatic", DEFAULT_AUTOMATIC_IMPORT))


def configured_transfer_mode() -> str:
    """The transfer mode used when a caller does not name one.

    Copy is the default rather than hardlink: hardlinking requires the download and
    the library destination to sit inside the *same mount*, which is not the case
    under the read-only download mount this deployment uses, and silently degrading
    a requested hardlink into a copy hides that from the operator.
    """
    mode = db.get_setting("import.transfer_mode", DEFAULT_TRANSFER_MODE)
    return mode if mode in {"hardlink", "copy", "move"} else DEFAULT_TRANSFER_MODE


def import_download(download_id: str, *, mode: str | None = None) -> dict[str, Any]:
    mode = mode or configured_transfer_mode()
    if mode not in {"hardlink", "copy", "move"}:
        raise ValueError("mode must be hardlink, copy, or move")
    d = db.one("SELECT * FROM downloads WHERE id=?", (download_id,))
    if not d:
        raise ValueError("Download not found")
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (d["asset_id"],)) if d.get("asset_id") else None
    if not asset:
        raise ValueError("Download is not linked to a media asset")
    root = db.one("SELECT * FROM root_folders WHERE id=?", (asset.get("root_folder_id"),)) if asset.get("root_folder_id") else None
    if not root:
        # Select first managed root of correct kind.
        root = db.one("SELECT * FROM root_folders WHERE enabled=1 AND management_mode='managed' AND (media_kind=? OR media_kind='mixed') ORDER BY created_at LIMIT 1", (asset["kind"],))
    if not root:
        raise ValueError(f"No managed root folder configured for {asset['kind']}")
    if root["management_mode"] != "managed":
        raise ValueError("Destination root is not in managed mode")
    source = map_remote_path(d["client_id"], d.get("download_path") or "")
    files = discover_download_files(source, asset["kind"])
    if not files:
        raise ValueError(f"No supported media files found at {source}")
    _guard_against_oversized_import(d, source, files)
    if mode == "move":
        _refuse_move_from_library_roots(files)
    work = _work_context(asset["work_id"])
    template = db.one("SELECT * FROM naming_templates WHERE media_kind=?", (asset["kind"],))
    opts = _template_opts(template)
    destination_dir = Path(root["path"]) / _render(template["folder_template"], work, ext="", part=None, **opts)
    # Guard: verify the destination is safely inside the root
    resolved_dest = path_guard.resolve_safe(Path(root["path"]), destination_dir)
    if resolved_dest is None:
        raise ValueError(f"Destination path escapes root: {destination_dir}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    imported = []
    claimed: set[Path] = set()
    for i, src in enumerate(files, 1):
        part = i if len(files) > 1 else None
        file_tpl = template["multipart_template"] if part and template.get("multipart_template") else template["file_template"]
        filename = _render(file_tpl, work, ext=src.suffix.lower().lstrip("."), part=part, **opts)
        dest = _earlier_copy(src, asset["id"], destination_dir, claimed) or _unique_destination(destination_dir / filename, src)
        claimed.add(dest)
        _transfer(src, dest, mode)
        # Verify before recording. For copy/move, source may no longer exist after move.
        if not dest.exists() or dest.stat().st_size == 0:
            raise IOError(f"Destination verification failed: {dest}")
        detected = scanner.inspect_file(dest, root=Path(root["path"]))
        size = dest.stat().st_size
        # A full-content hash is only recorded when the file is small enough that
        # fingerprint() hashed all of it; larger files keep just the sampled fingerprint.
        sha = scanner.full_sha256(dest) if size <= settings.scan_hash_full_max_mb * 1024 * 1024 else None
        pc = scanner.probe_columns(detected)
        with db.tx() as conn:
            # Idempotent: a retry after a partial failure meets rows its earlier run wrote.
            conn.execute(
                """INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,sha256,fingerprint,container,codec,sample_rate,channels,chapter_count,has_embedded_cover,bitrate,duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,0,?,?)
                   ON CONFLICT(path) DO UPDATE SET asset_id=excluded.asset_id,relative_path=excluded.relative_path,part_number=excluded.part_number,
                     size_bytes=excluded.size_bytes,mtime=excluded.mtime,sha256=excluded.sha256,fingerprint=excluded.fingerprint,container=excluded.container,
                     codec=COALESCE(excluded.codec,codec),sample_rate=COALESCE(excluded.sample_rate,sample_rate),channels=COALESCE(excluded.channels,channels),
                     chapter_count=COALESCE(excluded.chapter_count,chapter_count),has_embedded_cover=COALESCE(excluded.has_embedded_cover,has_embedded_cover),
                     bitrate=excluded.bitrate,duration_seconds=excluded.duration_seconds,embedded_json=excluded.embedded_json,
                     adopted=0,managed=1,missing=0,verified_at=excluded.verified_at""",
                (db.uid(), asset["id"], str(dest), str(dest.relative_to(root["path"])), part, size, dest.stat().st_mtime, sha, detected.get("fingerprint"), dest.suffix.lower().lstrip("."), pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"], detected.get("bitrate"), detected.get("duration_seconds"), db.json_dump(detected), db.now(), db.now()),
            )
        last_ext = dest.suffix.lower().lstrip(".")
        imported.append(str(dest))
    _remove_leftovers(asset["id"], destination_dir, Path(root["path"]), files, claimed)

    def finalise(conn):
        # Owned only once every file is in place, so a failure part-way leaves the asset
        # un-owned rather than half-applied.
        sup = derived.supersede(conn, asset["work_id"], asset["kind"], last_ext)
        conn.execute("UPDATE media_assets SET status='owned',format=COALESCE(format,?),updated_at=? WHERE id=?", (last_ext, db.now(), asset["id"]))
        conn.execute("UPDATE downloads SET state='imported',imported_at=?,updated_at=? WHERE id=?", (db.now(), db.now(), download_id))
        return sup
    superseded = db.tx_retry(finalise)
    derived.remove_generated_files(superseded)
    try:
        from . import localmeta
        localmeta.harvest(asset["work_id"])
    except Exception:
        log.warning("local metadata harvest failed after import", exc_info=True)
    db.add_history("download.imported", f"Imported {len(imported)} file(s) for {work['Title']}", work_id=asset["work_id"], asset_id=asset["id"], data={"files": imported, "mode": mode})
    conversion.maybe_auto_enqueue(asset["id"])    # policy off by default; never raises
    return {"download_id": download_id, "files": imported, "mode": mode}


# A worked example used when previewing templates with no particular Work in hand,
# chosen to exercise every token including an absent one.
SAMPLE_WORK = {
    "Author": "Craig Alanson",
    "Title": "Dead World",
    "Year": "2024",
    "Series": "Expeditionary Force",
    "SeriesPosition": "15",
    "SeriesPrefix": "Expeditionary Force 15 - ",
}
SAMPLE_WORK_NO_OPTIONALS = {**SAMPLE_WORK, "Year": "", "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}


def preview_name(work_id: str | None, kind: str, ext: str = "m4b", parts: int = 1,
                 overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render a template set without touching any file.

    `work_id` is optional so the settings UI can preview a template before any work
    is selected; `overrides` lets it preview unsaved edits.
    """
    template = dict(db.one("SELECT * FROM naming_templates WHERE media_kind=?", (kind,)) or {})
    if overrides:
        template.update({k: v for k, v in overrides.items() if v is not None})
    opts = _template_opts(template)

    def render_set(ctx: dict[str, Any]) -> dict[str, Any]:
        folder = _render(template["folder_template"], ctx, ext="", part=None, **opts)
        names = []
        for i in range(1, max(1, parts) + 1):
            part = i if parts > 1 else None
            f = template["multipart_template"] if part and template.get("multipart_template") else template["file_template"]
            names.append(_render(f, ctx, ext=ext, part=part, **opts))
        return {"folder": folder, "files": names}

    if work_id:
        work = _work_context(work_id)
        out = render_set(work)
        out["sample"] = False
        return out
    out = render_set(SAMPLE_WORK)
    # Show what happens when the optional tokens resolve to nothing, which is the
    # common case for migrated works and the source of the old "Unknown" filenames.
    out["without_optional_fields"] = render_set(SAMPLE_WORK_NO_OPTIONALS)
    out["sample"] = True
    return out


def _template_opts(template: dict[str, Any] | None) -> dict[str, Any]:
    if not template:
        return {}
    return {
        "replace_illegal": bool(template.get("replace_illegal", 1)),
        "colon": template.get("colon_replacement") or "smart",
    }


def _work_context(work_id: str) -> dict[str, Any]:
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w:
        raise ValueError("Work not found")
    authors = db.all("SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,))
    series = db.one("SELECT s.name,sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=? ORDER BY s.name LIMIT 1", (work_id,))
    year = ""
    if w.get("first_publish_date"):
        m = re.search(r"\d{4}", str(w["first_publish_date"]))
        year = m.group() if m else ""
    return {
        "Author": authors[0]["name"] if authors else "Unknown Author",
        "Title": w["title"],
        "Year": year,
        "Series": series["name"] if series else "",
        "SeriesPosition": series["position"] if series else "",
        "SeriesPrefix": (f"{series['name']} {series['position']} - " if series and series.get("position") else f"{series['name']} - " if series else ""),
    }


def _render(template: str, ctx: dict[str, Any], *, ext: str, part: int | None,
            replace_illegal: bool = True, colon: str = "smart") -> str:
    values = {**ctx, "Ext": ext, "Part": part or ""}
    def repl(m: re.Match):
        key, spec = m.group(1), m.group(2)
        value = values.get(key, "")
        if spec and isinstance(value, int):
            try:
                value = f"{value:0{int(spec)}d}"
            except Exception:
                pass
        return _safe_component(str(value), fallback="", trim=False, replace_illegal=replace_illegal, colon=colon)
    out = re.sub(r"\{([A-Za-z]+)(?::(\d+))?\}", repl, template)
    # Clean up each path component independently. Doing this on the whole string
    # would miss a dangling separator that sits mid-string because it is at the
    # start of a *later* component, e.g. "{Author}/{SeriesPosition} - {Title}" with
    # no series position leaving "Author/- Title".
    parts: list[str] = []
    for raw in re.split(r"[/\\]+", out):
        piece = re.sub(r"\s+", " ", raw)
        piece = re.sub(r"\s*-\s*(?=\.[^.]*$)", "", piece)   # " - .ext" -> ".ext"
        piece = re.sub(r"(?:\s*-\s*){2,}", " - ", piece)     # collapse repeats
        piece = piece.strip(" .-")                           # dangling edges
        if not piece or piece in {".", ".."}:
            continue
        parts.append(_safe_component(piece, replace_illegal=replace_illegal, colon=colon))
    return str(Path(*parts)) if parts else "Unknown"


# A colon is almost always written ": ", so the replacement consumes the whitespace
# after it - otherwise "dash" yields "Dune- Messiah" and "remove" yields "Dune Messiah".
COLON_REPLACEMENTS = {"smart": " - ", "dash": "-", "space": " ", "remove": ""}


def _safe_component(value: str, *, fallback: str = "Unknown", trim: bool = True,
                    replace_illegal: bool = True, colon: str = "smart") -> str:
    """Sanitise one path component.

    `fallback` applies when the whole component would be empty. Individual template
    fields pass fallback="" - an absent optional field (no series, no publication
    year) must vanish, not become the literal word "Unknown".
    """
    value = re.sub(r":\s*", COLON_REPLACEMENTS.get(colon, " - "), value)
    if replace_illegal:
        value = re.sub(r"[<>\"|?*\x00-\x1f]", "", value)
    else:
        # Path separators and NUL can never survive, whatever the policy.
        value = re.sub(r"[\x00-\x1f]", "", value)
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"\s+", " ", value)
    # Field substitutions keep their edge spacing: a series prefix legitimately ends
    # in " - " and trimming it would fuse it onto the next field. Whole components
    # are trimmed by the caller.
    value = value.strip(" .") if trim else value.strip(".")
    return value[:220] or fallback


def _same_content(a: Path, b: Path) -> bool:
    """Size first (cheap), then the scanner's fingerprint: a full hash for files up to
    the configured limit and a head/middle/tail sample above it."""
    try:
        return a.stat().st_size == b.stat().st_size and scanner.fingerprint(a) == scanner.fingerprint(b)
    except OSError:
        return False


def _earlier_folder_files(asset_id: str, destination_dir: Path) -> list[Path]:
    """Files in the destination folder and in the folders the asset's earlier files sit in (the folder
    name can change with the work's metadata too); this finds a copy that was never recorded."""
    dirs = {destination_dir} | {Path(r["path"]).parent for r in db.all("SELECT path FROM media_files WHERE asset_id=? AND managed=1", (asset_id,))}
    found: list[Path] = []
    for d in sorted(dirs):
        try:
            found += sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
        except OSError:
            pass
    return found


def _earlier_copy(src: Path, asset_id: str, destination_dir: Path, claimed: set[Path]) -> Path | None:
    """A file an earlier attempt for this asset already wrote with the same content as `src`, whatever
    name the naming template gives it now (issue #719): found among the asset's files and the
    destination folder, by size and fingerprint."""
    candidates = [Path(r["path"]) for r in db.all("SELECT path FROM media_files WHERE asset_id=? AND managed=1 AND missing=0 ORDER BY part_number, path", (asset_id,))]
    candidates += [p for p in _earlier_folder_files(asset_id, destination_dir) if p.suffix == src.suffix]
    for c in candidates:
        if c not in claimed and c.is_file() and _same_content(c, src):
            return c
    return None


def _remove_leftovers(asset_id: str, destination_dir: Path, root: Path, sources: list[Path], kept: set[Path]) -> None:
    """Delete copies an earlier attempt left beside this import's files: same content as a source, not
    one of the files just written. Other files of the asset (a converted M4B, say) are never touched."""
    candidates = {Path(r["path"]) for r in db.all("SELECT path FROM media_files WHERE asset_id=? AND managed=1", (asset_id,))}
    candidates |= set(_earlier_folder_files(asset_id, destination_dir))
    for c in sorted(candidates - kept):
        if path_guard.resolve_safe(root, c) is None or not c.is_file() or any(c.samefile(k) for k in kept if k.exists()):
            continue
        if not any(_same_content(c, s) for s in sources if s.exists() and s.suffix == c.suffix):
            continue
        c.unlink(missing_ok=True)
        with db.tx() as conn:
            conn.execute("DELETE FROM media_files WHERE path=? AND asset_id=?", (str(c), asset_id))
        log.info("import: removed leftover copy %s from an earlier attempt", c)


def _unique_destination(dest: Path, src: Path) -> Path:
    # Same name AND same content reuses the path (an import retry); a same-size file
    # with different content is a different book and must never be reused or overwritten.
    candidate, i = dest, 2
    while candidate.exists():
        if _same_content(candidate, src):
            return candidate
        candidate = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        i += 1
    return candidate


def _refuse_move_from_library_roots(files: list[Path]) -> None:
    """`move` deletes the source. Never allow that inside a library the user owns."""
    roots = db.all("SELECT path,management_mode FROM root_folders WHERE management_mode IN ('observe','adopt')")
    for f in files:
        for r in roots:
            if path_guard.resolve_safe(Path(r["path"]), f) is not None:
                raise ValueError(f"Refusing to move {f}: it is inside the {r['management_mode']} root {r['path']}; use copy or hardlink")


def _transfer(src: Path, dest: Path, mode: str):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    if mode == "hardlink":
        try:
            os.link(src, dest)
            return
        except OSError as exc:
            # EROFS: linking bumps the source inode link count, so a read-only
            # downloads mount rejects it even though the destination is writable.
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EROFS}:
                raise
            # Safe fallback for cross-filesystem downloads.
            mode = "copy"
    if mode == "copy":
        tmp = dest.with_suffix(dest.suffix + ".bookdoe-part")
        shutil.copy2(src, tmp)
        if not _same_content(tmp, src):
            tmp.unlink(missing_ok=True)
            raise IOError("Copy verification failed")
        os.replace(tmp, dest)
    elif mode == "move":
        try:
            os.replace(src, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            tmp = dest.with_suffix(dest.suffix + ".bookdoe-part")
            shutil.copy2(src, tmp)
            if not _same_content(tmp, src):
                tmp.unlink(missing_ok=True)
                raise IOError("Cross-filesystem move verification failed")
            os.replace(tmp, dest)
            src.unlink()
