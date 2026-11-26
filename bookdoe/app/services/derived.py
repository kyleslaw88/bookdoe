"""Derived assets (issue #265): files bookdoe made from another asset's files.

A converted audiobook or ebook is its own ``media_assets`` row with ``derived_from_asset_id``
set. It exists so that Management and the Player can offer the preferred format, but it is
NOT a second owned copy of the book: it never counts toward ownership, wanted/missing logic,
acquisition intent, duplicate detection or upgrade searches, and an asset lookup that picks
"the" asset of a work and kind for acquisition or monitoring must skip it (``NOT_DERIVED``).

The slot ``UNIQUE(work_id, kind, format)`` is shared with real assets, so a real asset of the
same format that arrives later (an import, a scan) supersedes the derivative: ``supersede``.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .. import db

log = logging.getLogger(__name__)

# SQL fragments. Unaliased and `a.`-aliased spellings, for queries that choose one asset.
NOT_DERIVED = "derived_from_asset_id IS NULL"
NOT_DERIVED_A = "a.derived_from_asset_id IS NULL"

PREFERRED_FORMAT = {"audiobook": "m4b", "ebook": "epub"}


def label(asset: dict[str, Any], source: dict[str, Any] | None = None) -> str | None:
    """'Converted from MP3' for a derived asset, None for an ordinary one."""
    if not asset.get("derived_from_asset_id"):
        return None
    src = (source or {}).get("format")
    return f"Converted from {str(src).upper()}" if src else "Converted"


def annotate(assets: list[dict[str, Any]]) -> None:
    """Add ``derived_label`` ('Converted from MP3') and ``derived_from_format`` to the derived
    assets of one work's asset list (the source is in the same list: it is the same work)."""
    by_id = {a["id"]: a for a in assets}
    for a in assets:
        if a.get("derived_from_asset_id"):
            src = by_id.get(a["derived_from_asset_id"])
            a["derived_from_format"] = (src or {}).get("format")
            a["derived_label"] = label(a, src)


def is_derived(asset: dict[str, Any] | None) -> bool:
    return bool(asset and asset.get("derived_from_asset_id"))


def is_generated_path(path: str) -> bool:
    """True for a file bookdoe wrote as a conversion output (even if its asset is gone).
    Scans and the reconcile pass never treat it as a find: it would become a second file of
    some real asset."""
    return db.one("SELECT 1 FROM conversions WHERE output_path=? AND status IN ('done','superseded')", (path,)) is not None


def supersede(conn: sqlite3.Connection, work_id: str, kind: str, fmt: str | None) -> list[str]:
    """A real asset of (work, kind, fmt) is about to exist: retire a derivative that holds the slot.

    Inside the caller's transaction the derivative's rows go (its files cascade) and the
    conversion row records why. Returns the derived file paths; the caller passes them to
    ``remove_generated_files`` AFTER its commit, so a rollback never leaves rows without files.
    The file is a redundant copy we made; left on disk it would be re-adopted as a second
    file of the real asset."""
    if not fmt:
        return []
    rows = conn.execute(
        "SELECT id FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id IS NOT NULL",
        (work_id, kind, fmt)).fetchall()
    paths: list[str] = []
    for r in rows:
        paths += [f["path"] for f in conn.execute("SELECT path FROM media_files WHERE asset_id=?", (r["id"],)).fetchall()]
        conn.execute("UPDATE conversions SET status='superseded',output_asset_id=NULL,finished_at=? WHERE output_asset_id=?",
                     (db.now(), r["id"]))
        conn.execute("DELETE FROM media_assets WHERE id=?", (r["id"],))
        log.info("derived asset %s superseded by a real %s %s", r["id"], kind, fmt)
    return paths


def remove_generated_files(paths: list[str]) -> None:
    """Delete files we generated, only where the path guard says a managed root owns them."""
    from . import files as files_svc

    for path in paths:
        mutable, _reason = files_svc.is_mutable({"path": path})
        if not mutable:
            continue
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - permissions
            log.warning("could not remove superseded derived file %s: %s", path, exc)
