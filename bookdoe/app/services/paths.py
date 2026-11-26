"""Path guard service -- every filesystem read, serve and write goes through here.

Provides:
- ``validate_root(path)`` -- validate a candidate root path before creating it.
- ``resolve_safe(base, target)`` -- resolve a target path against a base root,
  rejecting ``..`` traversal and symlink escapes.
- ``scan_safe(base)`` -- an ``rglob``-like iterator that skips files outside the
  root (including via symlinks).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import legacy

log = logging.getLogger(__name__)

# Per-root recycle folder written by services/filetrash.py. Scans never walk into it, so a file waiting there
# is not re-discovered as a new book.
TRASH_DIR_NAME = ".bookdoe-trash"
TRASH_DIR_NAMES = (TRASH_DIR_NAME, legacy.OLD_TRASH_DIR_NAME)   # scans skip both; only the first is written


def trash_root_for(root_real: Path, trash_path: str) -> Path:
    """The trash folder of a managed root that holds ``trash_path``: the old-named one for a file trashed before the rename."""
    parts = Path(os.path.abspath(trash_path or "")).parts
    return root_real / next((n for n in TRASH_DIR_NAMES if n in parts), TRASH_DIR_NAME)


# ---------------------------------------------------------------------------
# Root validation (called when a root is created)
# ---------------------------------------------------------------------------

def validate_root(path: str, *, data_dir: Path | None = None,
                  existing_roots: list[dict[str, Any]] | None = None) -> list[str]:
    """Validate a candidate root path.

    Returns a list of error messages (empty == valid).  Checks:
      1. Path is absolute.
      2. Path exists and is a directory.
      3. Path is not ``/`` (the filesystem root).
      4. Path is not inside the application data directory.
      5. Path does not overlap with any existing root.
    """
    errors: list[str] = []
    raw = Path(path)

    # 1. Must be absolute
    if not raw.is_absolute():
        errors.append("Root path must be absolute")
        return errors
    # Normalise ".." and resolve symlinks before any other check, so "/books/../config"
    # or a symlink into the data directory cannot slip past checks 3-5.
    p = Path(os.path.normpath(str(raw))).resolve()

    # 2. Must exist and be a directory
    if not p.exists():
        errors.append(f"Root path does not exist: {path}")
        return errors
    if not p.is_dir():
        errors.append(f"Root path is not a directory: {path}")
        return errors

    # 3. Must not be the filesystem root
    if p == Path("/"):
        errors.append("Root path cannot be the filesystem root (/)")
        return errors

    # 4. Must not be inside the data directory
    if data_dir is not None:
        try:
            p.relative_to(Path(data_dir).resolve())
            errors.append(
                f"Root path must not be inside the data directory ({data_dir})"
            )
        except ValueError:
            pass  # good -- not inside data_dir

    # 5. Must not overlap with existing roots
    if existing_roots is not None:
        for root in existing_roots:
            existing = Path(root["path"]).resolve()
            if existing == p:
                errors.append(
                    f"Root path overlaps with existing root '{root['name']}'"
                )
                continue
            # Check if one contains the other
            try:
                p.relative_to(existing)
                errors.append(
                    f"Root path is inside existing root '{root['name']}' ({existing})"
                )
            except ValueError:
                try:
                    existing.relative_to(p)
                    errors.append(
                        f"Existing root '{root['name']}' ({existing}) is inside this path"
                    )
                except ValueError:
                    pass  # no overlap

    return errors


# ---------------------------------------------------------------------------
# Path resolution and containment
# ---------------------------------------------------------------------------

def resolve_safe(base: Path, target: Path) -> Path | None:
    """Resolve *target* relative to *base* and verify containment.

    Returns the resolved absolute path if it is safely inside *base*,
    or ``None`` if the path escapes via ``..`` or a symlink.
    """
    # Resolve the target to an absolute path
    resolved = target.resolve()
    # Resolve the base too (in case base itself is a symlink)
    base_resolved = base.resolve()

    # Check containment using is_relative_to (Python 3.9+)
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        return None

    return resolved


def is_inside_root(path: Path, root: Path) -> bool:
    """Return True if *path* is safely inside *root* (after resolving symlinks)."""
    return resolve_safe(root, path) is not None


def exists_safe(base: Path, target: Path) -> bool:
    """Return True if *target* exists and is safely inside *base*; a stat, never a write."""
    resolved = resolve_safe(base, target)
    return resolved is not None and resolved.exists()


# ---------------------------------------------------------------------------
# Safe rglob -- skips files that escape the root via symlinks
# ---------------------------------------------------------------------------

def scan_safe(base: Path):
    """Iterate over files under *base*, skipping anything that escapes.

    Yields every regular file that is safely inside the resolved base
    directory.  Symlinks pointing outside the root are silently skipped
    (with a log warning).
    """
    base_resolved = base.resolve()
    yield from scan_safe_dir(base, base_resolved)


def scan_safe_dir(base: Path, base_resolved: Path):
    """Internal helper that walks *base* recursively, yielding only files.

    Directory symlinks are never followed (same as the previous ``rglob("*")``), which
    also rules out symlink cycles. File symlinks are yielded only if they resolve inside
    the root. Unreadable directories are logged and skipped instead of aborting a scan.
    """
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        log.warning("Skipping unreadable directory %s: %s", base, exc)
        return
    for entry in entries:
        if entry.is_symlink() and entry.is_dir():
            log.info("Not following directory symlink during scan: %s", entry)
            continue
        if not entry.exists():
            continue

        if entry.is_file():
            try:
                if not set(TRASH_DIR_NAMES).isdisjoint(entry.resolve().relative_to(base_resolved).parts):
                    continue    # a file in (or linked into) the trash is never library content
            except ValueError:
                log.warning(
                    "Skipping file outside root via symlink: %s (root: %s)",
                    entry, base_resolved,
                )
                continue
            yield entry
            continue

        if entry.is_dir():
            try:
                in_trash = entry.name in TRASH_DIR_NAMES or not set(TRASH_DIR_NAMES).isdisjoint(entry.resolve().relative_to(base_resolved).parts)
            except ValueError:
                in_trash = entry.name in TRASH_DIR_NAMES
            if in_trash:
                continue
            yield from scan_safe_dir(entry, base_resolved)
