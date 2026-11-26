"""Verify and rescan one MediaFile (Part XXIII).

Read-only on disk: nothing here writes, moves or deletes a file, so it works for
adopted and observe roots too.  ``verify_file`` only touches ``verified_at``,
``missing`` and the integrity columns; ``rescan_file`` is the one place that
refreshes the stored size/mtime/fingerprint/stream columns.  Every read goes
through ``paths.resolve_safe`` against a library root.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any

from .. import db
from . import paths as path_guard
from . import scanner

log = logging.getLogger(__name__)

MTIME_TOLERANCE = 1e-3


def resolve_in_root(path: Path) -> tuple[Path, Path] | None:
    """``(root, resolved_path)`` for the longest library root that safely contains *path*."""
    for r in db.all("SELECT path FROM root_folders ORDER BY LENGTH(path) DESC"):
        resolved = path_guard.resolve_safe(Path(r["path"]), path)
        if resolved is not None:
            return Path(r["path"]), resolved
    return None


def _check(name: str, ok: bool | None, **detail: Any) -> dict[str, Any]:
    """One check result; ``ok`` None means skipped (nothing stored to compare with)."""
    return {"name": name, "ok": ok, **detail}


def is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _container_check(path: Path) -> dict[str, Any] | None:
    """Container probe: EPUB opens as a zip with container.xml; audio probes with duration > 0."""
    ext = path.suffix.lower()
    if ext == ".epub":
        try:
            with zipfile.ZipFile(path) as zf:
                ok = "META-INF/container.xml" in zf.namelist()
            return _check("container", ok, **({} if ok else {"error": "container.xml not found"}))
        except (zipfile.BadZipFile, OSError, EOFError, NotImplementedError, RuntimeError) as exc:
            return _check("container", False, error=f"not a readable zip: {exc}")
    if ext in scanner.AUDIO_EXTS:
        probe = scanner._ffprobe(path)
        duration = probe.get("duration_seconds") or 0
        ok = duration > 0
        return _check("container", ok, **({} if ok else {"error": "ffprobe failed or duration is 0"}))
    return None


def _compare(checks: list[dict[str, Any]], name: str, stored: Any, read) -> None:
    """Append a check comparing *stored* with ``read()``; skipped when nothing is stored."""
    if stored is None:
        checks.append(_check(name, None, skipped="nothing stored"))
        return
    try:
        actual = read()
    except OSError as exc:
        checks.append(_check(name, False, error=str(exc)))
        return
    checks.append(_check(name, actual == stored, expected=stored, actual=actual))


def _save(file_id: str, state: str, checks: list[dict[str, Any]], deep: bool, missing: int | None) -> None:
    now = db.now()
    detail = db.json_dump({"deep": deep, "checks": checks})
    with db.tx() as conn:
        if missing is None:
            conn.execute(
                "UPDATE media_files SET verified_at=?,integrity_state=?,integrity_detail_json=? WHERE id=?",
                (now, state, detail, file_id),
            )
        else:
            conn.execute(
                "UPDATE media_files SET verified_at=?,missing=?,integrity_state=?,integrity_detail_json=? WHERE id=?",
                (now, missing, state, detail, file_id),
            )
    if missing is not None:
        from . import repair  # late: repair imports scanner/managedbuild
        repair.sync_asset_status()


def verify_file(file_id: str, deep: bool = False) -> dict[str, Any] | None:
    """Verify one file against its row; ``None`` if the id is unknown. Never raises on a bad file.

    State: missing (gone), changed (outside every root, or size/mtime/fingerprint/sha256
    differ from the row), damaged (container probe fails; wins over changed), else ok.
    The stored size and fingerprint are never overwritten here (rescan does that).
    """
    row = db.one("SELECT * FROM media_files WHERE id=?", (file_id,))
    if not row:
        return None

    found = resolve_in_root(Path(row["path"]))
    if found is None:
        checks = [_check("outside_root", False)]
        _save(file_id, "changed", checks, deep, None)
        return {"state": "changed", "checks": checks}
    _, path = found

    if not is_file(path):
        checks = [_check("exists", False)]
        _save(file_id, "missing", checks, deep, 1)
        return {"state": "missing", "checks": checks}

    checks = [_check("exists", True)]
    _compare(checks, "size", row["size_bytes"], lambda: path.stat().st_size)
    stored_mtime = row["mtime"]
    if stored_mtime is None:
        checks.append(_check("mtime", None, skipped="nothing stored"))
    else:
        try:
            actual_mtime = path.stat().st_mtime
            checks.append(_check("mtime", abs(actual_mtime - stored_mtime) <= MTIME_TOLERANCE,
                                 expected=stored_mtime, actual=actual_mtime))
        except OSError as exc:
            checks.append(_check("mtime", False, error=str(exc)))
    _compare(checks, "fingerprint", row["fingerprint"], lambda: scanner.fingerprint(path))
    if deep:
        _compare(checks, "sha256", row["sha256"], lambda: scanner.full_sha256(path))

    changed = any(c["ok"] is False for c in checks)
    container = _container_check(path)
    if container is not None:
        checks.append(container)
    damaged = container is not None and container["ok"] is False

    state = "damaged" if damaged else "changed" if changed else "ok"
    _save(file_id, state, checks, deep, 0)
    return {"state": state, "checks": checks}


def rescan_file(file_id: str) -> dict[str, Any] | None:
    """Re-read one file and refresh its row; ``None`` if the id is unknown.

    Updates size, mtime, fingerprint, container, codec, bitrate, duration, the stream
    columns and embedded_json, then re-verifies so integrity_state reflects the new
    values.  A file that is gone gets missing=1 via the verify; one outside every root is not read.
    """
    row = db.one("SELECT * FROM media_files WHERE id=?", (file_id,))
    if not row:
        return None

    found = resolve_in_root(Path(row["path"]))
    if found is None:
        return {"rescanned": False, "reason": "outside any root", "verify": verify_file(file_id)}
    root, path = found
    if not is_file(path):
        return {"rescanned": False, "reason": "file is missing", "verify": verify_file(file_id)}

    try:
        detected = scanner.inspect_file(path, root)
    except OSError as exc:
        return {"rescanned": False, "reason": f"unreadable: {exc}", "verify": verify_file(file_id)}

    pc = scanner.probe_columns(detected)
    with db.tx() as conn:
        # COALESCE keeps the P5-02 stream data when the probe fails this time (same rule as adopt_candidate).
        conn.execute(
            """UPDATE media_files SET size_bytes=?,mtime=?,fingerprint=?,container=?,bitrate=?,duration_seconds=?,
               codec=COALESCE(?,codec),sample_rate=COALESCE(?,sample_rate),channels=COALESCE(?,channels),
               chapter_count=COALESCE(?,chapter_count),has_embedded_cover=COALESCE(?,has_embedded_cover),
               embedded_json=?,missing=0 WHERE id=?""",
            (
                int(detected.get("size_bytes") or 0), detected.get("mtime"), detected.get("fingerprint"),
                detected.get("extension") or row["container"], detected.get("bitrate"), detected.get("duration_seconds"),
                pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"],
                db.json_dump(detected), file_id,
            ),
        )
    return {"rescanned": True, "verify": verify_file(file_id)}
