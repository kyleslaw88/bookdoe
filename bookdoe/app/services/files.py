"""MediaFile detail and mutability service (Part XXIII).

Every file-level read goes through ``paths.py``; this module is the single place
that decides whether a file is mutable and what integrity state it is in.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import db
from . import paths as path_guard

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unified root resolution
# ---------------------------------------------------------------------------

class _RootResult:
    """Single result from resolving a file path against all roots.

    Attributes:
        root: The matched root dict (enabled, or None if no match).
        enabled: Whether the matched root is enabled.
        mutable: Whether the file is mutable (enabled + managed).
        reason: Human-readable reason for the mutability decision.
        mode: The management_mode of the matched root, or None.
    """
    __slots__ = ("root", "enabled", "mutable", "reason", "mode")

    def __init__(self, root: dict[str, Any] | None, enabled: bool,
                 mutable: bool, reason: str, mode: str | None) -> None:
        self.root = root
        self.enabled = enabled
        self.mutable = mutable
        self.reason = reason
        self.mode = mode


def _is_symlink_escape(file_path: Path, root_path: Path) -> bool:
    """Check if *file_path* is lexically inside *root_path* but resolves outside.

    This catches the case where a parent directory in the path is a symlink
    pointing outside the root, so the lexical path is contained but the
    resolved path is not.
    """
    try:
        resolved_file = file_path.resolve()
    except (OSError, ValueError):
        return False
    try:
        resolved_root = root_path.resolve()
    except (OSError, ValueError):
        return False

    try:
        resolved_file.relative_to(resolved_root)
        return False  # safely inside
    except ValueError:
        return True  # escaped


def _resolve_root(path: str) -> _RootResult:
    """Resolve a file path against all roots in a single pass.

    Returns a _RootResult that captures:
    - The owning root (longest match among enabled roots, or None).
    - Whether the matched root is enabled.
    - Whether the file is mutable.
    - A reason string for the mutability decision.
    - The management_mode of the matched root.

    The longest matching root wins, regardless of enabled status. If the
    longest match is disabled, we report "root disabled". If the longest
    match is enabled, we apply its management_mode.
    """
    file_path = Path(path)

    # Fetch all roots ordered by path length (longest first).
    all_roots = db.all(
        "SELECT id, name, path, management_mode, enabled FROM root_folders "
        "ORDER BY LENGTH(path) DESC"
    )

    # Find the longest root that contains the path (lexically).
    longest_match: dict[str, Any] | None = None
    for root in all_roots:
        root_path = Path(root["path"])
        if path_guard.is_inside_root(file_path, root_path):
            longest_match = root
            break  # first match is longest

    if longest_match is not None:
        if longest_match["enabled"]:
            mode = longest_match["management_mode"]
            if mode == "managed":
                return _RootResult(longest_match, True, True, "", mode)
            if mode == "adopt":
                return _RootResult(longest_match, True, False, "adopted root", mode)
            if mode == "observe":
                return _RootResult(longest_match, True, False, "observe root", mode)
            return _RootResult(longest_match, True, False,
                               f"unknown management mode '{mode}'", mode)
        else:
            # Disabled root matched. Check for symlink escape.
            if _is_symlink_escape(file_path, Path(longest_match["path"])):
                return _RootResult(longest_match, False, False,
                                   "symlink escape", longest_match["management_mode"])
            return _RootResult(longest_match, False, False,
                               "root disabled", longest_match["management_mode"])

    # No root matched via resolved containment. Check if the file is lexically
    # inside any root but resolved containment failed (e.g. a symlinked parent
    # directory in the path escapes the root).
    for root in all_roots:
        root_path = Path(root["path"])
        try:
            file_path.relative_to(root_path)
            return _RootResult(root, False, False, "symlink escape", root["management_mode"])
        except ValueError:
            pass

    # Truly outside every root.
    return _RootResult(None, False, False, "outside any root", None)


def is_mutable(file_row: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(mutable, reason)`` for a ``media_files`` row.

    Delegates to ``_resolve_root`` for consistent root resolution.
    """
    path = file_row.get("path")
    if not path:
        return False, "outside any root"
    result = _resolve_root(path)
    return result.mutable, result.reason


def _file_exists(file_row: dict[str, Any], root: dict[str, Any] | None) -> bool:
    """Path-guarded existence check.

    When *root* is known, verifies the file through the path guard so symlinks
    that escape the root are rejected.  When *root* is ``None`` (file is outside
    every known root) we fall back to a direct stat so we don't misreport an
    existing file as ``missing``.
    """
    path = file_row.get("path")
    if not path:
        return False

    if root is not None:
        file_path = Path(path)
        safe_path = path_guard.resolve_safe(Path(root["path"]), file_path)
        if safe_path is None:
            return False
        try:
            return safe_path.is_file()
        except OSError:
            return False

    # No root matched -- check directly.
    try:
        return Path(path).is_file()
    except OSError:
        return False


def get_file_detail(file_id: str) -> dict[str, Any] | None:
    """Return the full MediaFile detail dict, or ``None`` if the id is unknown."""
    row = db.one(
        """SELECT f.*,
                  a.id AS asset_id, a.kind AS asset_kind, a.format AS asset_format,
                  a.status AS asset_status,
                  w.id AS work_id, w.title AS work_title
           FROM media_files f
           JOIN media_assets a ON a.id=f.asset_id
           JOIN works w ON w.id=a.work_id
           WHERE f.id=?""",
        (file_id,),
    )
    if not row:
        return None

    # Use the unified resolver (is_mutable delegates to _resolve_root).
    mutable, mutable_reason = is_mutable(row)
    # Re-resolve to get the root dict (is_mutable only returns bool+reason).
    root = _resolve_root(row["path"]).root

    # Integrity: only report 'missing' when the file is actually missing or
    # unverified, not just because no enabled root matched.
    exists = _file_exists(row, root)

    if row["missing"] or not exists:
        integrity_state = "missing"
    elif row["integrity_state"]:
        integrity_state = row["integrity_state"]
    elif row["verified_at"] is None:
        integrity_state = "unverified"
    else:
        integrity_state = "ok"

    return {
        "id": row["id"],
        "path": row["path"],
        "relative_path": row["relative_path"],
        "part_number": row["part_number"],
        "size_bytes": row["size_bytes"],
        "mtime": row["mtime"],
        "sha256": row["sha256"],
        "fingerprint": row["fingerprint"],
        "container": row["container"],
        "codec": row["codec"],
        "sample_rate": row["sample_rate"],
        "channels": row["channels"],
        "chapter_count": row["chapter_count"],
        "has_embedded_cover": None if row["has_embedded_cover"] is None else bool(row["has_embedded_cover"]),
        "bitrate": row["bitrate"],
        "duration_seconds": row["duration_seconds"],
        "adopted": bool(row["adopted"]),
        "managed": bool(row["managed"]),
        "missing": bool(row["missing"]),
        "discovered_at": row["discovered_at"],
        "verified_at": row["verified_at"],
        "embedded": db.json_load(row["embedded_json"], {}),
        "asset": {
            "id": row["asset_id"],
            "kind": row["asset_kind"],
            "format": row["asset_format"],
            "status": row["asset_status"],
        },
        "work": {
            "id": row["work_id"],
            "title": row["work_title"],
        },
        "root": {
            "id": root["id"],
            "name": root["name"],
            "management_mode": root["management_mode"],
        } if root else None,
        "mutable": mutable,
        "mutable_reason": mutable_reason,
        "integrity": {
            "state": integrity_state,
            "verified_at": row["verified_at"],
            "detail": db.json_load(row["integrity_detail_json"], {}),
        },
    }
