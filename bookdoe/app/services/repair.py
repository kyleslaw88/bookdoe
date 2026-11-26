from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import db
from . import derived, managedbuild, scanner
from . import paths as path_guard

log = logging.getLogger(__name__)

# Repairs that rewrite existing rows run once and record themselves here, so a
# restart does not repeat work that is already done.
DEDUPE_CONTRIBUTORS_VERSION = 2


def _applied(version: int) -> bool:
    return db.one("SELECT version FROM schema_migrations WHERE version=?", (version,)) is not None


def _mark_applied(version: int) -> None:
    with db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, db.now()))


def run_startup_repairs() -> dict[str, Any]:
    results: dict[str, Any] = {}
    try:
        # Run every start rather than once: it is a no-op on a clean database, and a
        # one-shot guarded by a version marker can never clean up a duplicate that
        # appears afterwards.
        result = dedupe_contributors()
        if result["rows_removed"]:
            results["dedupe_contributors"] = result
            _mark_applied(DEDUPE_CONTRIBUTORS_VERSION)
    except Exception:
        # A failed repair must not stop the application from serving.
        log.exception("contributor de-duplication repair failed")
    return results


def dedupe_contributors() -> dict[str, Any]:
    """Collapse contributor rows that describe the same author into one.

    Readarr import created a contributor row per book, so the authors list had one
    entry per title and every author owned exactly one work. Rows are merged only
    when the name matches case/whitespace-insensitively; a group holding two
    different OpenReads ids is two different people with one name and is left alone.
    """
    rows = db.all("SELECT id,name,metadata_id,created_at FROM contributors")
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = db.contributor_key(row["name"])
        if key:
            groups.setdefault(key, []).append(row)

    merged = removed = skipped = 0
    duplicated = {k: v for k, v in groups.items() if len(v) > 1}
    if not duplicated:
        # Nothing to merge: do not open a write transaction on a healthy database.
        return {"groups_merged": 0, "rows_removed": 0, "groups_skipped": 0, "authors_now": len(groups)}
    with db.tx() as conn:
        for key, members in duplicated.items():
            linked = {m["metadata_id"] for m in members if m["metadata_id"]}
            if len(linked) > 1:
                log.warning("contributor name %r maps to %d OpenReads ids; not merging", key, len(linked))
                skipped += 1
                continue
            members.sort(key=lambda m: (m["metadata_id"] is None, m["created_at"]))
            canonical = members[0]
            duplicates = [m["id"] for m in members[1:]]
            for dup in duplicates:
                # OR IGNORE skips rows the canonical contributor already has; the
                # DELETE then clears those leftovers so nothing points at the dup.
                conn.execute(
                    "UPDATE OR IGNORE work_contributors SET contributor_id=? WHERE contributor_id=?",
                    (canonical["id"], dup),
                )
                conn.execute("DELETE FROM work_contributors WHERE contributor_id=?", (dup,))
                conn.execute(
                    "UPDATE OR IGNORE author_monitors SET contributor_id=? WHERE contributor_id=?",
                    (canonical["id"], dup),
                )
                conn.execute("DELETE FROM contributors WHERE id=?", (dup,))
                removed += 1
            merged += 1
    result = {"groups_merged": merged, "rows_removed": removed, "groups_skipped": skipped, "authors_now": len(groups)}
    if removed:
        db.add_history("repair.contributors", f"Merged {removed} duplicate contributor rows into {merged} authors", data=result)
        log.info("contributor de-duplication: %s", result)
    return result


def sync_asset_status() -> dict[str, int]:
    """Make an asset's status agree with whether it has a present file.

    `owned` with no present (missing=0) file goes back to what a file-less asset reads
    (`missing` when it has acquisition intent, else `none`); `none`/`missing` with a
    present file reads `owned` again.  Called after every pass that flips media_files.missing,
    so it also corrects rows left stale by earlier scans.
    """
    present = "EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=media_assets.id AND f.missing=0)"
    now = db.now()
    with db.tx() as conn:
        cleared = conn.execute(
            "UPDATE media_assets SET status=CASE WHEN has_acquisition_intent=1 THEN 'missing' ELSE 'none' END,updated_at=? "  # nosec B608
            f"WHERE status='owned' AND NOT {present}", (now,)).rowcount
        restored = conn.execute(
            f"UPDATE media_assets SET status='owned',updated_at=? WHERE status IN ('none','missing') AND {present}",  # nosec B608
            (now,)).rowcount
    return {"cleared": cleared, "restored": restored}


def reconcile_managed_files(limit: int = 5000) -> dict[str, Any]:
    """Register files sitting in a managed root that the database has lost track of.

    A managed root only ever contains files bookdoe itself placed there, laid out
    as <Author>/<Title>/file. That makes recovery deterministic: the owning work is
    the one whose title and author match those two directory names exactly. Anything
    ambiguous is reported rather than guessed, so a mislabelled file is never
    attached to the wrong book.
    """
    roots = db.all("SELECT * FROM root_folders WHERE enabled=1 AND management_mode='managed'")
    registered, ambiguous, unmatched, seen = 0, 0, 0, 0
    for root in roots:
        base = Path(root["path"])
        if not base.exists() or not base.is_dir():
            continue
        for path in path_guard.scan_safe(base):
            if seen >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in scanner.MEDIA_EXTS:
                continue
            kind = scanner.kind_for_path(path)
            if root["media_kind"] != "mixed" and root["media_kind"] != kind:
                continue
            seen += 1
            if db.one("SELECT id FROM media_files WHERE path=?", (str(path),)) or derived.is_generated_path(str(path)) or managedbuild.is_build_path(str(path)):
                continue
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if len(relative.parts) < 3:
                unmatched += 1
                continue
            author, title = relative.parts[0], relative.parts[1]
            works = db.all(
                """SELECT DISTINCT w.id FROM works w
                   JOIN work_contributors wc ON wc.work_id=w.id AND wc.role='author'
                   JOIN contributors c ON c.id=wc.contributor_id
                   WHERE w.title=? COLLATE NOCASE AND c.name=? COLLATE NOCASE""",
                (title, author),
            )
            if len(works) > 1:
                ambiguous += 1
                continue
            if not works:
                unmatched += 1
                continue
            work_id = works[0]["id"]
            asset = db.one(
                "SELECT * FROM media_assets WHERE work_id=? AND kind=? AND derived_from_asset_id IS NULL ORDER BY format IS NULL DESC LIMIT 1",
                (work_id, kind),
            )
            if not asset:
                unmatched += 1
                continue
            detected = scanner.inspect_file(path, root=base)
            stat = path.stat()
            fmt = path.suffix.lower().lstrip(".")
            pc = scanner.probe_columns(detected)
            with db.tx() as conn:
                conn.execute(
                    """INSERT INTO media_files(id,asset_id,path,relative_path,size_bytes,mtime,fingerprint,container,codec,sample_rate,channels,
                                               chapter_count,has_embedded_cover,bitrate,
                                               duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,0,?,?)
                       ON CONFLICT(path) DO NOTHING""",
                    (
                        db.uid(), asset["id"], str(path), str(relative), stat.st_size, stat.st_mtime,
                        detected.get("fingerprint"), fmt,
                        pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"], detected.get("bitrate"), detected.get("duration_seconds"),
                        db.json_dump(detected), db.now(), db.now(),
                    ),
                )
                superseded = derived.supersede(conn, work_id, asset["kind"], fmt)
                conn.execute(
                    "UPDATE media_assets SET status='owned',format=COALESCE(format,?),updated_at=? WHERE id=?",
                    (fmt, db.now(), asset["id"]),
                )
            derived.remove_generated_files(superseded)
            registered += 1
            db.add_history(
                "library.reconciled",
                f"Re-registered managed file {path.name}",
                work_id=work_id, asset_id=asset["id"], data={"path": str(path)},
            )
    return {"files_seen": seen, "registered": registered, "ambiguous": ambiguous, "unmatched": unmatched}
