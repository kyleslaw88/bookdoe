"""DB-driven maintenance detectors (P5-19a, Part XXV): missing files, files outside roots, DB/path mismatches.

Read-only: they query, stat through the path guard and yield Findings. Adopted and observe
roots are inspected like managed ones. Each detector issues one files query and one roots
query regardless of library size, and every file is located against a root list loaded once.
Repair hints (``repair_action``) are for P5-22a; nothing here repairs.

``_missing_file`` reads ``media_files.missing`` and stats on its own instead of reusing the
scheduler's ``_library_health``: that job is a repair (it flips the missing flag, reconciles
managed files and backfills stream data), while a detector must only report, including the
case where the flag and the disk disagree. Sharing its loop would mean either writing from a
detector or splitting the job in two for a dozen lines of lookup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .. import db
from . import paths
from .maintenance import DetectorContext, Finding, register_detector

_FILES_SQL = (
    "SELECT f.id,f.asset_id,f.path,f.relative_path,f.missing,f.verified_at,"
    "a.status asset_status,a.kind asset_kind,a.root_folder_id,a.work_id,w.title,"
    "(SELECT COUNT(*) FROM media_files x WHERE x.asset_id=f.asset_id) asset_files "
    "FROM media_files f JOIN media_assets a ON a.id=f.asset_id JOIN works w ON w.id=a.work_id ORDER BY f.id"
)


@dataclass(frozen=True)
class _Root:
    id: str
    name: str
    path: str
    resolved: Path
    enabled: bool


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _load_roots() -> list[_Root]:
    rows = db.all("SELECT id,name,path,enabled FROM root_folders")
    roots = [_Root(r["id"], r["name"], _norm(r["path"]), Path(r["path"]).resolve(), bool(r["enabled"])) for r in rows]
    return sorted(roots, key=lambda r: -len(r.path))  # deepest first, so a nested root wins


def _locate(path: str, roots: list[_Root], enabled_only: bool) -> tuple[_Root, str] | None:
    """The root containing *path* (via the guard) and the path relative to it.

    ``paths.is_inside_root`` resolves the path on disk, once per file and once more per root
    for a file outside every root. That work is not charged to the detector's stat budget,
    which only caps the existence checks in ``_missing_file``: resolving is bounded by the
    file count times the root count, which stays small at the ~4k files a library holds.
    """
    p = _norm(path)
    cands = [r for r in roots if r.enabled or not enabled_only]
    for r in cands:  # cheap lexical pass first
        if p.startswith(r.path.rstrip("/") + "/") and paths.is_inside_root(Path(p), Path(r.path)):
            return r, PurePosixPath(os.path.relpath(p, r.path)).as_posix()
    for r in cands:  # then symlinked paths, which only match once resolved
        if paths.is_inside_root(Path(p), Path(r.path)):
            return r, PurePosixPath(os.path.relpath(Path(p).resolve(), r.resolved)).as_posix()
    return None


def _nearest_root(path: str, roots: list[_Root]) -> _Root | None:
    """The root sharing the longest directory prefix with *path* (None if only '/' is shared)."""
    best, depth = None, 0
    for r in roots:
        try:
            common = os.path.commonpath([_norm(path), r.path])
        except ValueError:
            continue
        d = len([c for c in common.split("/") if c])
        if d > depth:
            best, depth = r, d
    return best


def _missing_file(ctx: DetectorContext) -> Iterable[Finding]:
    roots, stats = _load_roots(), 0
    for f in db.all(_FILES_SQL):
        loc = _locate(f["path"], roots, enabled_only=False)
        on_disk: bool | None = None  # unknown when outside every root or out of stat budget
        if loc is not None:
            if stats >= ctx.limit:
                ctx.truncated = True
            else:
                stats += 1
                on_disk = paths.exists_safe(loc[0].resolved, Path(f["path"]))
        flagged = bool(f["missing"])
        if not flagged and on_disk is not False:
            continue
        detail: dict[str, Any] = {
            "path": f["path"], "work_id": f["work_id"], "work_title": f["title"], "asset_id": f["asset_id"],
            "asset_kind": f["asset_kind"], "asset_status": f["asset_status"], "verified_at": f["verified_at"],
            "flagged_missing": flagged, "on_disk": on_disk,
        }
        if on_disk is not None and on_disk == flagged:
            detail["flag_disagrees_with_disk"] = True  # the missing flag is stale
        only = f["asset_status"] == "owned" and f["asset_files"] == 1
        yield Finding("missing_file", "error" if only else "warning", "file", f["id"], detail=detail,
                      repair_action="refresh_state" if on_disk else "unlink_missing")


def _file_outside_roots(ctx: DetectorContext) -> Iterable[Finding]:
    roots = _load_roots()
    for f in db.all(_FILES_SQL):
        if _locate(f["path"], roots, enabled_only=True) is not None:
            continue
        near = _nearest_root(f["path"], roots)
        detail = {"path": f["path"], "work_title": f["title"], "asset_id": f["asset_id"],
                  "nearest_root": near and {"id": near.id, "name": near.name, "path": near.path, "enabled": near.enabled}}
        yield Finding("file_outside_roots", "warning", "file", f["id"], detail=detail, repair_action="relink_root")


def _db_path_mismatch(ctx: DetectorContext) -> Iterable[Finding]:
    roots = _load_roots()
    seen: dict[str, list[dict[str, Any]]] = {}
    for f in db.all(_FILES_SQL):
        base = {"path": f["path"], "work_title": f["title"], "asset_id": f["asset_id"]}
        loc = _locate(f["path"], roots, enabled_only=False)
        if loc is not None:
            root, rel = loc
            if f["relative_path"] and PurePosixPath(f["relative_path"]).as_posix() != rel:
                yield Finding("db_path_mismatch", "warning", "file", f["id"], "relative_path",
                              {**base, "case": "a", "relative_path": f["relative_path"], "expected": rel, "root": root.name},
                              repair_action="refresh_state")
            if f["root_folder_id"] and f["root_folder_id"] != root.id:
                yield Finding("db_path_mismatch", "warning", "file", f["id"], "root_folder",
                              {**base, "case": "b", "asset_root_folder_id": f["root_folder_id"], "containing_root_id": root.id},
                              repair_action="relink_root")
        seen.setdefault(_norm(f["path"]).lower(), []).append(f)
    for rows in seen.values():
        if len(rows) > 1:  # rows come ordered by id, so rows[0] is a stable subject
            yield Finding("db_path_mismatch", "warning", "file", rows[0]["id"], "duplicate_path",
                          {"case": "e", "file_ids": [r["id"] for r in rows], "paths": [r["path"] for r in rows],
                           "work_title": rows[0]["title"]}, repair_action="dedupe_rows")
    for a in db.all(
        "SELECT a.id,a.status,a.kind,w.title,COUNT(f.id) files,COALESCE(SUM(f.missing=0),0) live "
        "FROM media_assets a JOIN works w ON w.id=a.work_id LEFT JOIN media_files f ON f.asset_id=a.id "
        "WHERE a.status IN ('owned','none','missing') GROUP BY a.id ORDER BY a.id"
    ):
        base = {"work_title": a["title"], "asset_kind": a["kind"], "asset_status": a["status"], "files": a["files"], "live_files": a["live"]}
        if a["status"] == "owned" and a["live"] == 0:
            yield Finding("db_path_mismatch", "error", "asset", a["id"], "owned_no_live_file", {**base, "case": "c"},
                          repair_action="refresh_state")
        elif a["status"] in ("none", "missing") and a["live"] > 0:
            yield Finding("db_path_mismatch", "warning", "asset", a["id"], "live_file_status", {**base, "case": "d"},
                          repair_action="refresh_state")


# The limit doubles as the per-run stat cap for missing-file (a whole library fits in one run).
register_detector("missing-files", ("missing_file",), _missing_file, limit=10000, seconds=60.0)
register_detector("files-outside-roots", ("file_outside_roots",), _file_outside_roots, limit=5000)
register_detector("db-path-mismatch", ("db_path_mismatch",), _db_path_mismatch, limit=5000)
