"""Work merge preview (P5-23, Part XXVI): the evidence for merging Works and exactly what would
move, conflict or be lost. The preview is strictly read-only; `execute` (P5-24, end of file) honours its contract.

Nothing here writes to the database or touches a file. The preview runs on a read-only
connection inside one transaction, so the numbers are a single consistent snapshot, and
paths on disk are never looked at (re-laying out files afterwards is a P5-10/P5-12 plan).

Table inventory: the dependent tables are derived from ``PRAGMA foreign_key_list`` over every
table, never from a hand-kept list. Each (table, column) pair that references works,
media_assets or editions must appear in HANDLED, otherwise it is reported as ``unhandled``
and the preview carries an ``unhandled_tables`` blocker, so a table added later can never be
silently skipped by a merge. Tables without a foreign key (identifiers and the P4-03
metadata tables key on entity_type/entity_id) are listed in POLYMORPHIC.

Merge model the plan describes (execute must do the same):
  * the canonical Work wins every collision; the losing source row is kept in the merge ledger
  * assets with the same (kind, format) fold into the canonical asset: their files, progress
    and bookmarks are re-parented into it, flags are OR-ed (ownership, monitoring and
    acquisition intent are never lost)
  * tags are unioned; the access-control 'kids' tag is never dropped
  * a source that was merged leaves a history event ``work_merged`` whose data carries
    ``merged_work_id``; that is how a later preview recognises an already-merged source
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable

from .. import db
from . import catalogdupes, matchevidence, observations, tagging

MAX_SOURCES = 20
FIELDS = ("title", "description", "first_publish_date")
KIDS_TAG = "kids"
STATE_EXACT, STATE_SIMILAR = matchevidence.STATE_EXACT, matchevidence.STATE_SIMILAR
STATE_CONFLICT, STATE_UNAVAILABLE = matchevidence.STATE_CONFLICT, matchevidence.STATE_UNAVAILABLE

# (table, column) pairs that reference works / media_assets / editions and that a merge handles.
# The value is how the preview treats the rows: 'repoint' (work_id changes to the canonical),
# 'asset' (follows its asset, re-parented when the asset folds into a canonical one),
# 'follow' (references an edition, whose id never changes), 'special' (own logic below).
HANDLED: dict[tuple[str, str], str] = {
    ("work_contributors", "work_id"): "repoint",
    ("editions", "work_id"): "repoint",
    ("series_memberships", "work_id"): "repoint",
    ("series_assertions", "work_id"): "repoint",
    ("work_tags", "work_id"): "repoint",
    ("history", "work_id"): "repoint",
    ("local_meta", "work_id"): "repoint",
    ("cover_cache", "work_id"): "repoint",
    ("ratings", "work_id"): "repoint",
    ("read_states", "work_id"): "repoint",
    ("kids_decisions", "work_id"): "repoint",
    ("kids_classification", "work_id"): "repoint",
    ("work_audience_signals", "work_id"): "repoint",
    ("work_audience_status", "work_id"): "repoint",
    ("recommendation_cache", "work_id"): "repoint",
    ("media_progress", "work_id"): "repoint",
    ("bookmarks", "work_id"): "repoint",
    ("favorites", "work_id"): "repoint",
    ("content_grants", "work_id"): "repoint",
    ("requests", "work_id"): "repoint",
    ("release_searches", "work_id"): "repoint",
    ("releases", "work_id"): "repoint",
    ("downloads", "work_id"): "repoint",
    ("blocklist", "work_id"): "repoint",
    ("scan_candidates", "match_work_id"): "repoint",
    ("search_candidates", "promoted_work_id"): "repoint",
    ("work_merges", "canonical_work_id"): "repoint",
    ("media_assets", "work_id"): "special",
    ("media_files", "asset_id"): "asset",
    ("media_progress", "asset_id"): "asset",
    ("bookmarks", "asset_id"): "asset",
    ("downloads", "asset_id"): "asset",
    ("history", "asset_id"): "asset",
    ("wanted_search_state", "asset_id"): "asset",
    # Converted copies (services/derived.py): the source link follows its asset when that asset
    # folds into a canonical one; the conversion queue/history rows follow their work and assets.
    ("media_assets", "derived_from_asset_id"): "asset",
    ("conversions", "work_id"): "repoint",
    ("conversions", "source_asset_id"): "asset",
    ("conversions", "output_asset_id"): "asset",
    ("media_assets", "edition_id"): "follow",
    ("scan_candidates", "match_edition_id"): "follow",
    ("works", "preferred_ebook_edition_id"): "special",
    ("works", "preferred_audiobook_edition_id"): "special",
}
# No foreign key: keyed by entity_type/entity_id.
POLYMORPHIC = ("identifiers", "metadata_observations", "metadata_field_locks")
PARENTS = ("works", "media_assets", "editions")


class WorkNotFound(LookupError):
    """The canonical Work does not exist."""


class BadRequest(ValueError):
    """The ids given cannot be previewed (none, or too many sources)."""


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _marks(n: int) -> str:
    return ",".join("?" * n)


def dependent_tables(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Every (table, column, parent) foreign key that points at works, media_assets or editions."""
    out = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({_q(t)})").fetchall():
            if fk["table"] in PARENTS:
                out.append({"table": t, "column": fk["from"], "parent": fk["table"]})
    return out


def table_inventory(conn: sqlite3.Connection) -> dict[str, list[dict[str, str]]]:
    """Dependent tables split into ``handled`` and ``unhandled`` (known to the schema, not to the merge)."""
    handled, unhandled = [], []
    for d in dependent_tables(conn):
        action = HANDLED.get((d["table"], d["column"]))
        (handled if action else unhandled).append({**d, "action": action or "unhandled"})
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in POLYMORPHIC:
        if t in existing:
            handled.append({"table": t, "column": "entity_id", "parent": "polymorphic", "action": "polymorphic"})
    return {"handled": handled, "unhandled": unhandled}


def _pk(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = [r for r in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall() if r["pk"]]
    return [r["name"] for r in sorted(rows, key=lambda r: r["pk"])]


def _norm(v: Any) -> str:
    return " ".join(str(v or "").split()).casefold()


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _work_facts(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    w = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    authors = {db.contributor_key(r["name"]): r["name"] for r in conn.execute(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "
        "WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,))}
    idents = {(r["namespace"], str(r["value"]).casefold()) for r in conn.execute(
        "SELECT i.namespace,i.value FROM identifiers i WHERE (i.entity_type='work' AND i.entity_id=?) "
        "OR (i.entity_type='edition' AND i.entity_id IN (SELECT id FROM editions WHERE work_id=?))", (work_id, work_id))}
    series = {r["series_id"]: r["position"] for r in conn.execute("SELECT series_id,position FROM series_memberships WHERE work_id=?", (work_id,))}
    return {"row": dict(w), "authors": authors, "identifiers": idents, "series": series}


def _agree_title(a: dict, b: dict) -> dict[str, Any]:
    ta, tb = a["row"]["title"], b["row"]["title"]
    if _norm(ta) == _norm(tb):
        state = STATE_EXACT
    elif catalogdupes.title_key(ta) and catalogdupes.title_key(ta) == catalogdupes.title_key(tb):
        state = STATE_SIMILAR
    else:
        state = STATE_CONFLICT
    return {"state": state, "canonical": ta, "source": tb}


def _agree_author(a: dict, b: dict) -> dict[str, Any]:
    ka, kb = set(a["authors"]), set(b["authors"])
    if not ka or not kb:
        state = STATE_UNAVAILABLE
    elif ka == kb:
        state = STATE_EXACT
    elif ka & kb:
        state = STATE_SIMILAR
    else:
        state = STATE_CONFLICT
    return {"state": state, "canonical": list(a["authors"].values()), "source": list(b["authors"].values())}


def _agree_identifier(a: dict, b: dict) -> dict[str, Any]:
    shared = sorted(a["identifiers"] & b["identifiers"])
    ns_a = {ns: v for ns, v in a["identifiers"]}
    differing = sorted(ns for ns, v in b["identifiers"] if ns in ns_a and (ns, v) not in a["identifiers"])
    if shared:
        state = STATE_EXACT
    elif differing:
        state = STATE_CONFLICT
    else:
        state = STATE_UNAVAILABLE
    return {"state": state, "shared": [{"namespace": ns, "value": v} for ns, v in shared], "differing_namespaces": differing}


def _agree_series(a: dict, b: dict) -> dict[str, Any]:
    same = sorted(set(a["series"]) & set(b["series"]))
    if not a["series"] or not b["series"]:
        state = STATE_UNAVAILABLE
    elif same:
        state = STATE_EXACT if all(_norm(a["series"][s]) == _norm(b["series"][s]) for s in same) else STATE_SIMILAR
    else:
        state = STATE_CONFLICT
    return {"state": state, "shared_series": same}


def _maintenance_signals(conn: sqlite3.Connection, canonical_id: str, source_id: str) -> dict[str, Any] | None:
    """What the P5-21a duplicate detectors stored for this pair, if they ran."""
    subject = catalogdupes._pair_subject(canonical_id, source_id)
    for kind in ("duplicate_work", "split_duplicate_work"):
        r = conn.execute("SELECT id,kind,status,detail_json FROM maintenance_issues WHERE fingerprint=?",
                         (f"{kind}|work_pair|{subject}|",)).fetchone()
        if r:
            d = db.json_load(r["detail_json"], {})
            return {"issue_id": r["id"], "kind": r["kind"], "status": r["status"], "signals": d.get("signals", []),
                    "confidence": d.get("confidence"), "canonical_work_id": d.get("canonical_work_id")}
    return None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _entry(table: str, column: str, parent: str, action: str, rows: int, absorbed: int = 0, **extra: Any) -> dict[str, Any]:
    return {"table": table, "column": column, "parent": parent, "action": action,
            "rows": rows, "move": rows - absorbed, "absorbed": absorbed, **extra}


def _asset_targets(conn: sqlite3.Connection, canonical_id: str, source_ids: list[str]) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Map each source asset to the asset it ends in, describe each (kind, format) collision, and
    list how converted copies (derived assets) are handled.

    A format-less asset (a slot nobody has filled) is its own format value, so two of them
    collide too, which is what the unique slot per kind means to the rest of the app.

    A derived asset is never a conflict to resolve and never contributes ownership. Real assets
    claim their slots first. A derived source asset whose slot is taken is DROPPED (its file rows
    go, the file stays on disk; the asset folds into the holder so progress and bookmarks follow).
    A canonical derived asset whose slot a real source asset needs is RETIRED the same way, into
    that real asset. A derived asset in a free slot just moves and keeps its source link."""
    target: dict[str, str] = {}
    claimed: dict[tuple[str, str | None], str] = {}
    derived_canonical: set[str] = set()
    for r in conn.execute("SELECT id,kind,format,derived_from_asset_id FROM media_assets WHERE work_id=? ORDER BY added_at,id", (canonical_id,)):
        claimed[(r["kind"], r["format"])] = r["id"]
        if r["derived_from_asset_id"]:
            derived_canonical.add(r["id"])
    src_assets = [(sid, conn.execute("SELECT * FROM media_assets WHERE work_id=? ORDER BY added_at,id", (sid,)).fetchall()) for sid in source_ids]
    notes: list[dict[str, Any]] = []
    for sid, assets in src_assets:
        for a in assets:
            held = claimed.get((a["kind"], a["format"]))
            if a["format"] and not a["derived_from_asset_id"] and held in derived_canonical:
                notes.append({"action": "retired", "asset_id": held, "replaced_by": a["id"], "work_id": canonical_id,
                              "reason": "a real asset of this format arrives from a source Work"})
                del claimed[(a["kind"], a["format"])]
                derived_canonical.discard(held)
    collisions = []
    for derived_pass in (False, True):
        for sid, assets in src_assets:
            for a in assets:
                if bool(a["derived_from_asset_id"]) != derived_pass:
                    continue
                key = (a["kind"], a["format"])
                if key in claimed:
                    keep = claimed[key]
                    target[a["id"]] = keep
                    if derived_pass:
                        notes.append({"action": "dropped", "asset_id": a["id"], "folded_into": keep, "work_id": sid,
                                      "files": conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (a["id"],)).fetchone()[0],
                                      "reason": "the Work already has an asset of this format"})
                        continue
                    k = conn.execute("SELECT * FROM media_assets WHERE id=?", (keep,)).fetchone()
                    files = conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (a["id"],)).fetchone()[0]
                    collisions.append({
                        "type": "asset_collision", "kind": a["kind"], "format": a["format"],
                        "source_work_id": sid, "source_asset_id": a["id"], "canonical_asset_id": keep,
                        "files_to_reparent": files,
                        "flags_after": {"owned": "owned" in (a["status"], k["status"]), "monitored": bool(a["monitored"] or k["monitored"]),
                                        "acquisition_intent": bool(a["has_acquisition_intent"] or k["has_acquisition_intent"])},
                        "proposed_resolution": "fold into the canonical asset: re-parent its files, OR the ownership, monitoring and acquisition-intent flags",
                    })
                else:
                    claimed[key] = a["id"]
                    target[a["id"]] = a["id"]
                    if derived_pass:
                        notes.append({"action": "moved", "asset_id": a["id"], "work_id": sid,
                                      "derived_from_asset_id": a["derived_from_asset_id"], "reason": "keeps its source link"})
    collisions += _fold_placeholders(conn, canonical_id, source_ids, target, notes)
    return target, collisions, notes


def _fold_placeholders(conn: sqlite3.Connection, canonical_id: str, source_ids: list[str], target: dict[str, str], notes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """An empty format-less slot (not owned, no files, not a converted copy) never survives beside an OWNED asset of its
    kind (services/ownership.py): it folds into that asset, so its monitoring and intent are OR-ed in by the normal fold and
    its children follow. Covers both a source's placeholder and one the canonical already had. ``target`` is updated in place.
    Honors the cleanup rule (services/ownership.py): a placeholder anything else references (an in-flight download, a release
    grab, progress, a bookmark, a conversion...) is NOT folded or repointed; it is skipped and reported in ``notes``."""
    from . import ownership
    blocking = ownership._blocking_columns(conn)
    rows = []
    for wid in [canonical_id, *source_ids]:
        rows += conn.execute("SELECT * FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL ORDER BY added_at,id", (wid,)).fetchall()
    surviving = [r for r in rows if target.get(r["id"], r["id"]) == r["id"]]
    owned_by_kind: dict[str, str] = {}
    for r in surviving:
        if r["status"] == "owned":
            owned_by_kind.setdefault(r["kind"], r["id"])
    fold: dict[str, str] = {}
    out = []
    for r in surviving:
        keep = owned_by_kind.get(r["kind"])
        if r["status"] == "owned" or r["format"] is not None or not keep:
            continue
        held_by = next((f"{t}.{c}" for t, c in blocking if conn.execute(f'SELECT 1 FROM "{t}" WHERE "{c}"=? LIMIT 1', (r["id"],)).fetchone()), None)  # nosec B608
        if held_by:
            if notes is not None:
                notes.append({"action": "kept", "asset_id": r["id"], "work_id": r["work_id"], "kind": r["kind"],
                              "reason": f"empty placeholder is still referenced ({held_by}); not folded, its references are not repointed"})
            continue
        fold[r["id"]] = keep
        out.append({"type": "asset_collision", "kind": r["kind"], "format": None, "source_work_id": r["work_id"], "source_asset_id": r["id"],
                    "canonical_asset_id": keep, "files_to_reparent": 0, "placeholder": True,
                    "flags_after": {"owned": True, "monitored": bool(r["monitored"]), "acquisition_intent": bool(r["has_acquisition_intent"])},
                    "proposed_resolution": "an empty placeholder beside an owned asset of the same kind folds into it (its monitoring is kept)"})
    for aid, keep in list(target.items()):
        if keep in fold:
            target[aid] = fold[keep]
    for aid, keep in fold.items():
        target[aid] = keep
    return out


def _repoint_counts(conn: sqlite3.Connection, table: str, col: str, canonical_id: str, source_ids: list[str]) -> tuple[int, int]:
    """(rows on the sources, rows whose key is already on the canonical or an earlier source)."""
    pk = _pk(conn, table)
    key_cols = [c for c in pk if c != col] if col in pk else None
    seen: set[tuple] = set()
    if key_cols is not None:
        sel = ",".join(_q(c) for c in key_cols) or "1"
        seen = {tuple(r) for r in conn.execute(f"SELECT {sel} FROM {_q(table)} WHERE {_q(col)}=?", (canonical_id,))}  # nosec B608
        if not key_cols and seen:
            seen = {()}
    rows = absorbed = 0
    for sid in source_ids:
        sel = ",".join(_q(c) for c in key_cols) if key_cols else "1"
        for r in conn.execute(f"SELECT {sel} FROM {_q(table)} WHERE {_q(col)}=?", (sid,)).fetchall():  # nosec B608
            rows += 1
            if key_cols is None:
                continue
            key = tuple(r) if key_cols else ()
            if key in seen:
                absorbed += 1
            else:
                seen.add(key)
    return rows, absorbed


def _asset_rows(conn: sqlite3.Connection, table: str, col: str, target: dict[str, str]) -> tuple[int, int, int]:
    """(rows on source assets, rows re-parented into another asset, of those the ones that collide on the key)."""
    pk = _pk(conn, table)
    key_cols = [c for c in pk if c != col] if col in pk else None
    rows = reparented = absorbed = 0
    for sid, tid in target.items():
        n = conn.execute(f"SELECT COUNT(*) FROM {_q(table)} WHERE {_q(col)}=?", (sid,)).fetchone()[0]  # nosec B608
        rows += n
        if tid == sid:
            continue
        reparented += n
        if key_cols:
            cols = ",".join(_q(c) for c in key_cols)
            existing = {tuple(r) for r in conn.execute(f"SELECT {cols} FROM {_q(table)} WHERE {_q(col)}=?", (tid,))}  # nosec B608
            absorbed += sum(1 for r in conn.execute(f"SELECT {cols} FROM {_q(table)} WHERE {_q(col)}=?", (sid,)) if tuple(r) in existing)  # nosec B608
    return rows, reparented, absorbed


def _plan_tables(conn, canonical_id: str, source_ids: list[str], target: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    src_editions = [r[0] for r in conn.execute(f"SELECT id FROM editions WHERE work_id IN ({_marks(len(source_ids))})", source_ids)]  # nosec B608
    for d in dependent_tables(conn):
        table, col, parent = d["table"], d["column"], d["parent"]
        action = HANDLED.get((table, col))
        if action == "repoint":
            rows, absorbed = _repoint_counts(conn, table, col, canonical_id, source_ids)
            out.append(_entry(table, col, parent, "repoint", rows, absorbed))
        elif action == "special" and table == "media_assets":
            folded = sum(1 for a, t in target.items() if a != t)
            out.append(_entry(table, col, parent, "move_or_fold", len(target), folded, folded_into_canonical=folded))
        elif action == "asset":
            rows, reparented, absorbed = _asset_rows(conn, table, col, target)
            out.append(_entry(table, col, parent, "follow_asset", rows, absorbed, reparented=reparented))
        elif action == "follow":
            n = 0
            if src_editions and table != "works":
                n = conn.execute(f"SELECT COUNT(*) FROM {_q(table)} WHERE {_q(col)} IN ({_marks(len(src_editions))})", src_editions).fetchone()[0]  # nosec B608
            out.append(_entry(table, col, parent, "follow_edition", n, 0))
        elif action is None:
            out.append(_entry(table, col, parent, "unhandled", 0, 0))
    out.extend(_polymorphic_entries(conn, canonical_id, source_ids))
    return sorted(out, key=lambda e: (e["table"], e["column"]))


def _polymorphic_entries(conn, canonical_id: str, source_ids: list[str]) -> list[dict[str, Any]]:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ed = [r[0] for r in conn.execute(f"SELECT id FROM editions WHERE work_id IN ({_marks(len(source_ids))})", source_ids)]  # nosec B608
    files = [r[0] for r in conn.execute(
        f"SELECT f.id FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE a.work_id IN ({_marks(len(source_ids))})", source_ids)]  # nosec B608
    out = []
    if "identifiers" in names:
        n = conn.execute(f"SELECT COUNT(*) FROM identifiers WHERE entity_type='work' AND entity_id IN ({_marks(len(source_ids))})", source_ids).fetchone()[0]  # nosec B608
        if ed:
            n += conn.execute(f"SELECT COUNT(*) FROM identifiers WHERE entity_type='edition' AND entity_id IN ({_marks(len(ed))})", ed).fetchone()[0]  # nosec B608
        # The primary key is global per (entity_type, namespace, value), so two Works can never hold the same value.
        out.append(_entry("identifiers", "entity_id", "polymorphic", "repoint", n, 0))
    if "metadata_observations" in names:
        n = _count_entities(conn, "metadata_observations", source_ids, ed, files)
        demoted = conn.execute(
            f"SELECT COUNT(*) FROM metadata_observations s WHERE s.entity_type='work' AND s.is_current=1 AND s.entity_id IN ({_marks(len(source_ids))}) "  # nosec B608
            "AND EXISTS (SELECT 1 FROM metadata_observations c WHERE c.entity_type='work' AND c.entity_id=? AND c.field_name=s.field_name AND c.is_current=1)",
            [*source_ids, canonical_id]).fetchone()[0]
        out.append(_entry("metadata_observations", "entity_id", "polymorphic", "repoint", n, 0, demoted_from_current=demoted))
    if "metadata_field_locks" in names:
        n = _count_entities(conn, "metadata_field_locks", source_ids, ed, files)
        absorbed = conn.execute(
            f"SELECT COUNT(*) FROM metadata_field_locks s WHERE s.entity_type='work' AND s.entity_id IN ({_marks(len(source_ids))}) "  # nosec B608
            "AND EXISTS (SELECT 1 FROM metadata_field_locks c WHERE c.entity_type='work' AND c.entity_id=? AND c.field_name=s.field_name)",
            [*source_ids, canonical_id]).fetchone()[0]
        out.append(_entry("metadata_field_locks", "entity_id", "polymorphic", "repoint", n, absorbed))
    return out


def _count_entities(conn, table: str, work_ids: list[str], edition_ids: list[str], file_ids: list[str]) -> int:
    """Rows keyed to the source Works, their editions and their files (the P4-03 entity types).

    Edition and file ids never change in a merge, so only the work rows are re-pointed; the
    rest are counted so the plan shows nothing is dropped."""
    n = 0
    for etype, ids in (("work", work_ids), ("edition", edition_ids), ("media_file", file_ids)):
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            n += conn.execute(f"SELECT COUNT(*) FROM {_q(table)} WHERE entity_type=? AND entity_id IN ({_marks(len(chunk))})", [etype, *chunk]).fetchone()[0]  # nosec B608
    return n


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def _per_user_conflicts(conn, table: str, label: str, value_col: str, canonical_id: str, source_ids: list[str]) -> list[dict[str, Any]]:
    out, seen = [], {r["user_id"] for r in conn.execute(f"SELECT user_id FROM {table} WHERE work_id=?", (canonical_id,))}  # nosec B608
    canon = {r["user_id"]: r[value_col] for r in conn.execute(f"SELECT user_id,{value_col} FROM {table} WHERE work_id=?", (canonical_id,))}  # nosec B608
    for sid in source_ids:
        for r in conn.execute(f"SELECT user_id,{value_col} FROM {table} WHERE work_id=? ORDER BY user_id", (sid,)).fetchall():  # nosec B608
            if r["user_id"] in seen and canon.get(r["user_id"]) != r[value_col]:
                out.append({"type": label, "user_id": r["user_id"], "source_work_id": sid,
                            "canonical_value": canon.get(r["user_id"]), "source_value": r[value_col],
                            "proposed_resolution": "canonical wins; the source's value is preserved in the merge ledger"})
            seen.add(r["user_id"])
    return out


def _conflicts(conn, canonical_id: str, source_ids: list[str], collisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = list(collisions)
    out += _per_user_conflicts(conn, "ratings", "rating_conflict", "rating", canonical_id, source_ids)
    out += _per_user_conflicts(conn, "read_states", "read_state_conflict", "status", canonical_id, source_ids)

    canon_series = {r["series_id"]: r["position"] for r in conn.execute("SELECT series_id,position FROM series_memberships WHERE work_id=?", (canonical_id,))}
    for sid in source_ids:
        for r in conn.execute("SELECT m.series_id,m.position,s.name FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (sid,)).fetchall():
            if r["series_id"] in canon_series and _norm(canon_series[r["series_id"]]) != _norm(r["position"]):
                empty = not _norm(canon_series[r["series_id"]])
                out.append({"type": "series_position_conflict", "series_id": r["series_id"], "series": r["name"], "source_work_id": sid,
                            "canonical_position": canon_series[r["series_id"]], "source_position": r["position"],
                            "proposed_resolution": "take the source position (the canonical has none)" if empty
                            else "keep the canonical position; the source's stays as a series assertion"})

    canon = conn.execute("SELECT * FROM works WHERE id=?", (canonical_id,)).fetchone()
    for kind in ("ebook", "audiobook"):
        col = f"preferred_{kind}_edition_id"
        for sid in source_ids:
            s = conn.execute(f"SELECT {col} p FROM works WHERE id=?", (sid,)).fetchone()  # nosec B608
            if s["p"] and canon[col]:
                out.append({"type": "preferred_edition_conflict", "kind": kind, "source_work_id": sid,
                            "canonical_edition_id": canon[col], "source_edition_id": s["p"],
                            "proposed_resolution": "keep the canonical preferred edition; the source's edition moves and stays selectable"})

    for sid in source_ids:
        s = conn.execute("SELECT * FROM works WHERE id=?", (sid,)).fetchone()
        for f in FIELDS:
            if _norm(s[f]) and _norm(canon[f]) and _norm(s[f]) != _norm(canon[f]):
                out.append({"type": "field_conflict", "field": f, "source_work_id": sid,
                            "canonical_value": canon[f], "source_value": s[f], "choices": ["canonical", "source"],
                            "proposed_resolution": "keep the canonical value; the source value is kept as an observation"})
            elif _norm(s[f]) and not _norm(canon[f]):
                out.append({"type": "field_fill", "field": f, "source_work_id": sid, "source_value": s[f],
                            "proposed_resolution": "fill the empty canonical field from the source"})
    if _table_exists(conn, "metadata_field_locks"):
        locked = {r["field_name"] for r in conn.execute("SELECT field_name FROM metadata_field_locks WHERE entity_type='work' AND entity_id=?", (canonical_id,))}
        for sid in source_ids:
            for r in conn.execute("SELECT field_name FROM metadata_field_locks WHERE entity_type='work' AND entity_id=? ORDER BY field_name", (sid,)).fetchall():
                if r["field_name"] in locked:
                    out.append({"type": "field_lock_conflict", "field": r["field_name"], "source_work_id": sid,
                                "proposed_resolution": "keep the lock on the canonical value; the source lock is not carried"})
    return out


def _progress_conflicts(conn, target: dict[str, str]) -> list[dict[str, Any]]:
    """A user with reading/listening progress on both a folded asset and the asset it folds into: the canonical's
    stays unless ``resolutions.per_user == 'newest'`` and the source's was updated later."""
    out = []
    for aid, keep in target.items():
        if aid == keep:
            continue
        for r in conn.execute("SELECT * FROM media_progress WHERE asset_id=? ORDER BY user_id", (aid,)).fetchall():
            mine = conn.execute("SELECT * FROM media_progress WHERE asset_id=? AND user_id=?", (keep, r["user_id"])).fetchone()
            if mine:
                out.append({"type": "progress_conflict", "user_id": r["user_id"], "kind": r["kind"], "source_asset_id": aid,
                            "canonical_percent": mine["percent"], "source_percent": r["percent"],
                            "canonical_updated_at": mine["updated_at"], "source_updated_at": r["updated_at"],
                            "newest": "source" if (r["updated_at"] or 0) > (mine["updated_at"] or 0) else "canonical",
                            "choices": ["canonical", "newest"],
                            "proposed_resolution": "keep the canonical's progress; choose 'newest' to keep whichever was updated last"})
    return out


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ---------------------------------------------------------------------------
# Tags, acquisition, blockers
# ---------------------------------------------------------------------------

def _tags(conn, canonical_id: str, source_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(tags the canonical gains, blockers). A source's 'kids' tag onto a canonical without it
    is an access-control change, so it is a blocker that carries the union proposal."""
    have = {r["tag_id"] for r in conn.execute("SELECT tag_id FROM work_tags WHERE work_id=?", (canonical_id,))}
    union, blockers = [], []
    for sid in source_ids:
        for r in conn.execute("SELECT t.id,t.name FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? ORDER BY t.name", (sid,)).fetchall():
            if r["id"] in have:
                continue
            union.append({"tag_id": r["id"], "name": r["name"], "from_work_id": sid})
            have.add(r["id"])
            if r["name"] == KIDS_TAG:
                blockers.append({
                    "code": "kids_tag_mismatch", "work_id": sid,
                    "message": "The source has the 'kids' tag and the canonical does not; the tag must not be dropped.",
                    "proposed_resolution": {"action": "tag_union", "add_tags": [KIDS_TAG], "to_work_id": canonical_id},
                })
    if blockers:
        # Every record that ends up on the Kids shelf because of the union is named, and an adult SOURCE among them (a third
        # work in a many-way merge) needs its own consent: resolutions.kids_tag_union_works must list it.
        def has_kids(wid):
            return bool(conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name=?", (wid, KIDS_TAG)).fetchone())
        gaining = [w for w in [canonical_id, *source_ids] if not has_kids(w)]
        adult_sources = [w for w in source_ids if w in gaining]
        titles = {r["id"]: r["title"] for r in conn.execute(f"SELECT id,title FROM works WHERE id IN ({_marks(len(gaining))})", gaining)} if gaining else {}  # nosec B608
        for b in blockers:
            b["gaining"] = [{"id": w, "title": titles.get(w)} for w in gaining]
            b["adult_sources"] = adult_sources
    return union, blockers


def _acquisition(conn, canonical_id: str, source_ids: list[str]) -> dict[str, Any]:
    """Part XI: ownership, monitoring and acquisition intent are OR-ed across the Works."""
    ids = [canonical_id, *source_ids]
    m = _marks(len(ids))
    works = conn.execute(f"SELECT id,monitored FROM works WHERE id IN ({m})", ids).fetchall()  # nosec B608
    assets = conn.execute(f"SELECT work_id,status,monitored,has_acquisition_intent FROM media_assets WHERE work_id IN ({m}) AND derived_from_asset_id IS NULL", ids).fetchall()  # nosec B608
    cano = [a for a in assets if a["work_id"] == canonical_id]

    def count(rows, pred):
        return sum(1 for r in rows if pred(r))
    return {
        "rule": "or",
        "work_monitored": {"canonical": bool(next(w["monitored"] for w in works if w["id"] == canonical_id)),
                           "after": any(w["monitored"] for w in works)},
        "owned_assets": {"canonical": count(cano, lambda a: a["status"] == "owned"), "sources": count(assets, lambda a: a["status"] == "owned" and a["work_id"] != canonical_id)},
        "monitored_assets": {"canonical": count(cano, lambda a: a["monitored"]), "sources": count(assets, lambda a: a["monitored"] and a["work_id"] != canonical_id)},
        "acquisition_intent_assets": {"canonical": count(cano, lambda a: a["has_acquisition_intent"]),
                                      "sources": count(assets, lambda a: a["has_acquisition_intent"] and a["work_id"] != canonical_id)},
    }


def _merged_before(conn, work_id: str) -> bool:
    """Legacy ``work_merged`` events carry merged_work_id; execute writes ``work.merged`` with merged_work_ids."""
    return bool(conn.execute(
        "SELECT 1 FROM history WHERE (event_type='work_merged' AND json_extract(data_json,'$.merged_work_id')=?) "
        "OR (event_type='work.merged' AND EXISTS (SELECT 1 FROM json_each(history.data_json,'$.merged_work_ids') WHERE value=?)) LIMIT 1",
        (work_id, work_id)).fetchone())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def preview(canonical_id: str, source_ids: Iterable[str]) -> dict[str, Any]:
    """Read-only merge preview. Raises WorkNotFound for a missing canonical and BadRequest for bad input."""
    sources = list(dict.fromkeys(s for s in source_ids if s))
    if not sources:
        raise BadRequest("at least one source Work is required")
    if len(sources) > MAX_SOURCES:
        raise BadRequest(f"at most {MAX_SOURCES} source Works per preview")
    conn = db.connect(readonly=True)
    try:
        conn.execute("BEGIN")  # one snapshot for every count below; nothing is ever written
        if not conn.execute("SELECT 1 FROM works WHERE id=?", (canonical_id,)).fetchone():
            raise WorkNotFound(canonical_id)
        return _preview(conn, canonical_id, sources)
    finally:
        conn.rollback()
        conn.close()


def _preview(conn: sqlite3.Connection, canonical_id: str, sources: list[str]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    usable: list[str] = []
    for sid in sources:
        if sid == canonical_id:
            blockers.append({"code": "source_equals_canonical", "work_id": sid, "message": "A Work cannot be merged into itself."})
        elif not conn.execute("SELECT 1 FROM works WHERE id=?", (sid,)).fetchone():
            merged = _merged_before(conn, sid)
            blockers.append({"code": "source_already_merged" if merged else "source_not_found", "work_id": sid,
                             "message": "This Work was already merged into another." if merged else "No Work with this id exists."})
        else:
            usable.append(sid)

    inventory = table_inventory(conn)
    if inventory["unhandled"]:
        blockers.append({"code": "unhandled_tables", "message": "Tables reference Works, assets or editions but the merge does not handle them yet.",
                         "tables": [f"{d['table']}.{d['column']}" for d in inventory["unhandled"]]})

    canon = _work_facts(conn, canonical_id)
    result: dict[str, Any] = {
        "canonical": {"id": canonical_id, "title": canon["row"]["title"]},
        "sources": [{"id": s, "title": conn.execute("SELECT title FROM works WHERE id=?", (s,)).fetchone()["title"]} for s in usable],
        "evidence": [], "plan": {"tables": [], "unhandled": inventory["unhandled"], "tag_union": [], "acquisition": None},
        "conflicts": [], "lost": [],
    }
    result["plan"]["derived_assets"] = []
    if usable:
        for sid in usable:
            f = _work_facts(conn, sid)
            result["evidence"].append({
                "work_id": sid, "title": _agree_title(canon, f), "author": _agree_author(canon, f),
                "identifier": _agree_identifier(canon, f), "series": _agree_series(canon, f),
                "maintenance": _maintenance_signals(conn, canonical_id, sid),
            })
        target, collisions, derived_notes = _asset_targets(conn, canonical_id, usable)
        tables = _plan_tables(conn, canonical_id, usable, target)
        union, tag_blockers = _tags(conn, canonical_id, usable)
        blockers += tag_blockers
        blockers += _kid_widening(conn, canonical_id, usable)
        result["plan"].update(tables=tables, tag_union=union, acquisition=_acquisition(conn, canonical_id, usable), derived_assets=derived_notes)
        result["conflicts"] = _conflicts(conn, canonical_id, usable, collisions) + _progress_conflicts(conn, target)
        # Rows that exist on a source but have no row of their own after the merge. Nothing is
        # destroyed: execute writes each one to the ledger first.
        result["lost"] = [{"table": t["table"], "rows": t["absorbed"], "preserved_in": "merge ledger"} for t in tables if t["absorbed"]]
    result["blockers"] = blockers
    result["ok"] = not blockers
    return result


# ---------------------------------------------------------------------------
# Execute (P5-24)
# ---------------------------------------------------------------------------

EVENT_MERGED = "work.merged"


KID_WIDEN_CODES = ("kid_grant_widening", "tag_grant_widening", "adult_into_kids_canonical", "kid_visibility_widening")
# One consent key per blocker (``kids_widen`` consents to all of them); each lists the kid accounts it covers.
KID_WIDEN_KEYS = {"kid_grant_widening": "kids_widen_grant", "tag_grant_widening": "kids_widen_tag_grant",
                  "adult_into_kids_canonical": "kids_widen_adult", "kid_visibility_widening": "kids_widen_visibility"}


def _kids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id,username FROM users WHERE access_scope='kids' ORDER BY username").fetchall()


def _kid_sees(conn: sqlite3.Connection, kid_id: str, work_id: str) -> bool:
    """tagging.kids_visibility, evaluated for one kid and one Work."""
    clause, params = tagging.kids_visibility({"access_scope": "kids", "id": kid_id})
    return bool(conn.execute(f"SELECT 1 FROM works w WHERE w.id=? AND {clause}", (work_id, *params)).fetchone())  # nosec B608


def _kid_widening(conn: sqlite3.Connection, canonical_id: str, sources: list[str]) -> list[dict[str, Any]]:
    """Merging must not let a child see something they could not see before. Blockers (cleared only by the explicit
    ``kids_widen`` resolution, default off): a kid's grant on a source (``kid_grant_widening``) or a tag the kid is granted
    that only a source carries (``tag_grant_widening``) would make the merged book visible to a kid who cannot see the
    canonical record; and an adult source merged into a Kids-shelf canonical (``adult_into_kids_canonical``) would put adult
    assets in front of every kid. (The kids tag on a source only is ``kids_tag_mismatch``.)"""
    out: list[dict[str, Any]] = []
    kids = _kids(conn)
    canon_kids_tag = bool(conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name=?", (canonical_id, KIDS_TAG)).fetchone())
    canon_tags = {r[0] for r in conn.execute("SELECT tag_id FROM work_tags WHERE work_id=?", (canonical_id,))}
    for sid in sources:
        if not conn.execute("SELECT 1 FROM works WHERE id=?", (sid,)).fetchone():
            continue
        grant_kids, tag_kids = [], []
        for k in kids:
            if _kid_sees(conn, k["id"], canonical_id):
                continue
            if conn.execute("SELECT 1 FROM content_grants WHERE work_id=? AND user_id=?", (sid, k["id"])).fetchone():
                grant_kids.append(k["username"])
            elif conn.execute("SELECT 1 FROM work_tags wt JOIN tag_grants tg ON tg.tag_id=wt.tag_id WHERE wt.work_id=? AND tg.user_id=?"  # nosec B608
                              + (f" AND wt.tag_id NOT IN ({_marks(len(canon_tags))})" if canon_tags else ""), (sid, k["id"], *canon_tags)).fetchone():
                tag_kids.append(k["username"])
        if grant_kids:
            out.append({"code": "kid_grant_widening", "work_id": sid, "kids": grant_kids,
                        "message": f"{', '.join(grant_kids)} can open the other record through a grant but not the kept one; merging would let them see the merged book.",
                        "proposed_resolution": {"action": "kids_widen"}})
        if tag_kids:
            out.append({"code": "tag_grant_widening", "work_id": sid, "kids": tag_kids,
                        "message": f"{', '.join(tag_kids)} are granted a tag only the other record has; merging would add it to the kept record and let them see it.",
                        "proposed_resolution": {"action": "kids_widen"}})
        src_kids_tag = bool(conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name=?", (sid, KIDS_TAG)).fetchone())
        if canon_kids_tag and not src_kids_tag:
            affected = [k["username"] for k in kids if not _kid_sees(conn, k["id"], sid)]
            out.append({"code": "adult_into_kids_canonical", "work_id": sid, "kids": affected,
                        "message": "The kept record is on the Kids shelf and the other one is not; merging would put its assets in front of kids"
                                   + (f" ({', '.join(affected)})" if affected else "") + ".",
                        "proposed_resolution": {"action": "kids_widen"}})
    # Anything else the REAL visibility predicate says would widen (e.g. the kid sees the canonical through a grant or a
    # collection grant and the other record is adult): predicted from today's visibility, enforced for real inside execute.
    named = {n for b in out for n in b["kids"]}
    leftover = []
    for k in kids:
        if k["username"] in named:
            continue
        sees_c = _kid_sees(conn, k["id"], canonical_id)
        sees_s = [_kid_sees(conn, k["id"], sid) for sid in sources if conn.execute("SELECT 1 FROM works WHERE id=?", (sid,)).fetchone()]
        if (sees_c or any(sees_s)) and (not sees_c or not all(sees_s)):
            if not (not sees_c and any(sees_s) and conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE t.name=? AND wt.work_id IN (%s)" % _marks(len(sources)), (KIDS_TAG, *sources)).fetchone()):  # nosec B608
                leftover.append(k["username"])      # (a kid who sees a source only through the kids tag is kids_tag_mismatch's business)
    if leftover:
        out.append({"code": "kid_visibility_widening", "work_id": sources[0] if sources else canonical_id, "kids": leftover,
                    "message": f"{', '.join(leftover)} would be able to see content of the merged book that they cannot see today.",
                    "proposed_resolution": {"action": "kids_widen"}})
    return out


class MergeBlocked(ValueError):
    """The preview reports blockers that the resolutions do not clear; nothing was written."""

    def __init__(self, blockers: list[dict[str, Any]]):
        super().__init__("merge blocked: " + ", ".join(b["code"] for b in blockers))
        self.blockers = blockers


class _Ledger:
    """Row-level reversal ledger. Every change goes through here, so a row's full before-image
    is written first and a reversal is: in reverse ``id`` order, delete the row at ``pk_json``,
    then re-insert ``before_json`` when there is one. ``action`` is 'moved' for a row re-pointed
    to the canonical Work or asset, else 'updated', 'deleted' or 'inserted'."""

    def __init__(self, conn: sqlite3.Connection, merge_id: str):
        self.conn, self.merge_id, self.rows = conn, merge_id, 0
        self._keys: dict[str, list[str]] = {}

    def keys(self, table: str) -> list[str]:
        if table not in self._keys:
            self._keys[table] = _pk(self.conn, table) or ["rowid"]
        return self._keys[table]

    def select(self, table: str, where: str, params: Iterable[Any]) -> list[dict[str, Any]]:
        head = "rowid AS rowid, *" if self.keys(table) == ["rowid"] else "*"
        return [dict(r) for r in self.conn.execute(f"SELECT {head} FROM {_q(table)} WHERE {where}", list(params)).fetchall()]  # nosec B608

    def _log(self, table: str, action: str, pk: dict[str, Any], before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
        self.rows += 1
        self.conn.execute(
            "INSERT INTO work_merge_ledger(merge_id,table_name,pk_json,action,before_json,after_json) VALUES (?,?,?,?,?,?)",
            (self.merge_id, table, db.json_dump(pk), action, db.json_dump(before) if before is not None else None,
             db.json_dump(after) if after is not None else None))

    def _where(self, table: str, row: dict[str, Any]) -> tuple[str, list[Any]]:
        ks = self.keys(table)
        return " AND ".join(f"{_q(k)}=?" for k in ks), [row[k] for k in ks]

    def update(self, table: str, row: dict[str, Any], action: str = "updated", **changes: Any) -> dict[str, Any]:
        after = {**row, **changes}
        self._log(table, action, {k: after[k] for k in self.keys(table)}, row, after)
        where, params = self._where(table, row)
        self.conn.execute(f"UPDATE {_q(table)} SET {','.join(f'{_q(c)}=?' for c in changes)} WHERE {where}", [*changes.values(), *params])  # nosec B608
        return after

    def delete(self, table: str, row: dict[str, Any]) -> None:
        self._log(table, "deleted", {k: row[k] for k in self.keys(table)}, row, None)
        where, params = self._where(table, row)
        self.conn.execute(f"DELETE FROM {_q(table)} WHERE {where}", params)  # nosec B608

    def insert(self, table: str, values: dict[str, Any]) -> None:
        self.conn.execute(f"INSERT INTO {_q(table)}({','.join(_q(c) for c in values)}) VALUES ({_marks(len(values))})", list(values.values()))  # nosec B608
        self._log(table, "inserted", {k: values[k] for k in self.keys(table)}, None, values)


def _unique_keys(conn: sqlite3.Connection, table: str, col: str) -> list[list[str]]:
    """The other columns of every full unique key (primary key or unique index) that contains ``col``."""
    keys = []
    pk = _pk(conn, table)
    if col in pk:
        keys.append([c for c in pk if c != col])
    for ix in conn.execute(f"PRAGMA index_list({_q(table)})").fetchall():
        if ix["unique"] and ix["origin"] != "pk" and not ix["partial"]:
            cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({_q(ix['name'])})").fetchall()]
            if col in cols:
                keys.append([c for c in cols if c != col])
    return keys


def _repoint(led: _Ledger, table: str, col: str, mapping: dict[str, str], extra: str = "1") -> int:
    """Move rows from ``mapping`` keys to values in ``table.col``. A row whose key already exists on the
    target loses (the canonical wins); it is deleted only after the ledger holds its full row."""
    conn, moved = led.conn, 0
    uniques = _unique_keys(conn, table, col)
    for old, new in mapping.items():
        if old == new:
            continue
        for row in led.select(table, f"{_q(col)}=? AND {extra} ORDER BY rowid", (old,)):
            clash = any(conn.execute(
                f"SELECT 1 FROM {_q(table)} WHERE {_q(col)}=?{''.join(f' AND {_q(c)} IS ?' for c in key)} AND {extra}",  # nosec B608
                [new, *(row[c] for c in key)]).fetchone() for key in uniques)
            if clash:
                led.delete(table, row)
            else:
                led.update(table, row, "moved", **{col: new})
            moved += 1
    return moved


def _empty(v: Any) -> bool:
    return not _norm(v)


def execute(canonical_id: str, source_ids: Iterable[str], resolutions: dict[str, Any] | None = None,
            user: Any = None, confirm_dissimilar: bool = False) -> dict[str, Any]:
    """Merge the source Works into the canonical one in ONE transaction (Part XXVI, Part LIII).

    Raises WorkNotFound, BadRequest, or MergeBlocked (the preview's blockers that ``resolutions`` does
    not clear: ``{"kids_tag_union": True}`` accepts the access-control tag union; ``{"fields":
    {"title": "source"}}`` takes the source's value for a conflicting field). A source that does not look like the
    same book (different title once series notes are removed, different author, language or position, and no shared
    identifier) blocks with ``dissimilar_works`` unless ``confirm_dissimilar=True`` is passed explicitly. Any error rolls back
    every row and every ledger entry. Files on disk are never looked at."""
    sources = list(dict.fromkeys(s for s in source_ids if s))
    if not sources:
        raise BadRequest("at least one source Work is required")
    if len(sources) > MAX_SOURCES:
        raise BadRequest(f"at most {MAX_SOURCES} source Works per merge")
    resolutions = resolutions or {}
    merge_id, t = db.uid(), db.now()
    by = user.get("id") if isinstance(user, dict) else user
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM works WHERE id=?", (canonical_id,)).fetchone():
            raise WorkNotFound(canonical_id)
        plan = _preview(conn, canonical_id, sources)
        consented = set(resolutions.get("kids_tag_union_works") or [])
        blockers = [b for b in plan["blockers"] if not (b["code"] == "kids_tag_mismatch" and resolutions.get("kids_tag_union") is True
                                                        and set(b.get("adult_sources") or []) <= consented)
                    and not (b["code"] in KID_WIDEN_CODES and (resolutions.get("kids_widen") is True or resolutions.get(KID_WIDEN_KEYS[b["code"]]) is True))]
        if not confirm_dissimilar:
            blockers += _dissimilar(conn, canonical_id, plan)
        if blockers:
            raise MergeBlocked(blockers)
        conn.execute("INSERT INTO work_merges(id,canonical_work_id,source_work_ids_json,status,resolutions_json,created_by,created_at) "
                     "VALUES (?,?,?,'executed',?,?,?)",
                     (merge_id, canonical_id, db.json_dump(sources), db.json_dump(resolutions), by, t))
        led = _Ledger(conn, merge_id)
        kids = _kids(conn)
        vis0 = {k["id"]: (_kid_sees(conn, k["id"], canonical_id), [_kid_sees(conn, k["id"], sid) for sid in sources]) for k in kids}
        canon_kids_tag0 = bool(conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name=?", (canonical_id, KIDS_TAG)).fetchone())
        works = {s: canonical_id for s in sources}
        titles = {s: conn.execute("SELECT title FROM works WHERE id=?", (s,)).fetchone()["title"] for s in sources}
        target, _, derived_notes = _asset_targets(conn, canonical_id, sources)

        # A series position the canonical lacks is taken from the source; the source row then loses below.
        for sid in sources:
            for r in led.select("series_memberships", "work_id=?", (sid,)):
                mine = led.select("series_memberships", "work_id=? AND series_id=?", (canonical_id, r["series_id"]))
                if mine and _empty(mine[0]["position"]) and not _empty(r["position"]):
                    led.update("series_memberships", mine[0], position=r["position"])

        if resolutions.get("per_user") == "newest":
            _prefer_newest(led, canonical_id, sources, target)
        deps = dependent_tables(conn)
        _settle_derived(led, derived_notes, deps)
        for d in deps:
            if HANDLED.get((d["table"], d["column"])) == "repoint":
                _repoint(led, d["table"], d["column"], works)

        _merge_assets(led, canonical_id, sources, target)
        for d in deps:
            if HANDLED.get((d["table"], d["column"])) == "asset":
                _repoint(led, d["table"], d["column"], {a: k for a, k in target.items() if a != k})
        _drop_folded_assets(led, target)

        _repoint(led, "identifiers", "entity_id", works, "entity_type='work'")
        _repoint(led, "metadata_field_locks", "entity_id", works, "entity_type='work'")
        _move_observations(led, canonical_id, sources)
        _update_canonical(led, canonical_id, sources, resolutions, t)

        for sid in sources:
            led.delete("works", led.select("works", "id=?", (sid,))[0])
        for sid in sources:
            _observe(led, canonical_id, "merged_work", f"{sid}: {titles[sid]}", "manual", False, t)
        led.insert("history", {
            "id": db.uid(), "work_id": canonical_id, "asset_id": None, "event_type": EVENT_MERGED,
            "message": f"Merged {len(sources)} Work(s) into this one",
            "data_json": db.json_dump({"merge_id": merge_id, "merged_work_ids": sources,
                                       "sources": [{"id": s, "title": titles[s]} for s in sources]}),
            "created_at": t})
        # The authoritative check, with the real predicate (kids tag, content grants, tag/collection grants) on the merged
        # state inside this transaction: a kid who now sees the merged book but could not see the canonical or one of the
        # sources before is "widened". Consent is the explicit kids_widen / per-blocker key, or the kids-shelf union itself.
        canon_kids_tag1 = bool(conn.execute("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name=?", (canonical_id, KIDS_TAG)).fetchone())
        if resolutions.get("kids_widen") is not True:
            consent = {n for b in plan["blockers"] if b["code"] in KID_WIDEN_KEYS and resolutions.get(KID_WIDEN_KEYS[b["code"]]) is True for n in b.get("kids", [])}
            union_ok = resolutions.get("kids_tag_union") is True and not canon_kids_tag0 and canon_kids_tag1
            bad = [k["username"] for k in kids
                   if _kid_sees(conn, k["id"], canonical_id) and (not vis0[k["id"]][0] or not all(vis0[k["id"]][1]))
                   and k["username"] not in consent and not union_ok]
            if bad:
                raise MergeBlocked([{"code": "kid_visibility_widening", "kids": bad, "work_id": canonical_id,
                                     "message": f"After this merge {', '.join(bad)} would be able to see content they cannot see now. Tick the matching Kids access box to allow it.",
                                     "proposed_resolution": {"action": "kids_widen"}}])
    return {"merge_id": merge_id, "canonical_work_id": canonical_id, "source_work_ids": sources, "ledger_rows": led.rows}


def _dissimilar(conn: sqlite3.Connection, canonical_id: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Default-deny: a source is a blocker unless it shares an identifier with the canonical (ISBN, ASIN, provider id) AND
    has the same title, or is plausibly the same book: the same title once series decoration is removed (catalogdupes.title_key), no different
    position (series membership or a number in the title: 'Part 1' is not 'Part 2', 'Book 1' is not 'Book 10'), the same
    author set and a compatible language (workdupes.compatible). Merging anything else folds two books into one."""
    from . import workdupes
    out = []
    ids = [canonical_id] + [ev["work_id"] for ev in plan["evidence"]]
    facts = workdupes.load_facts(ids)
    canon = facts.get(canonical_id)
    for ev in plan["evidence"]:
        src = facts.get(ev["work_id"])
        if canon is None or src is None:
            continue
        # A shared ISBN/ASIN/provider id vouches for the book only when the titles also agree (once series notes are
        # removed): one wrongly shared identifier must not fold two different books together.
        if ev["identifier"]["state"] == STATE_EXACT and canon["full_key"] == src["full_key"]:
            continue
        ok, _ = workdupes.compatible(canon, src)
        if ok:
            continue
        out.append({"code": "dissimilar_works", "work_id": ev["work_id"],
                    "message": f"\"{src['title']}\" does not look like the same book as \"{canon['title']}\" (different title, author, language or position; a shared identifier only counts when the titles agree). Confirm to merge anyway.",
                    "proposed_resolution": {"action": "confirm_dissimilar"}})
    return out


def _prefer_newest(led: _Ledger, canonical_id: str, sources: list[str], target: dict[str, str]) -> None:
    """Resolution ``per_user: newest``: where a user has a rating, read state or progress on both sides, the more
    recently updated one wins (default: the canonical's). The loser's row is ledgered like every other change."""
    for table in ("ratings", "read_states"):
        for sid in sources:
            for r in led.select(table, "work_id=?", (sid,)):
                mine = led.select(table, "work_id=? AND user_id=?", (canonical_id, r["user_id"]))
                if mine and (r["updated_at"] or 0) > (mine[0]["updated_at"] or 0):
                    led.update(table, mine[0], **{k: v for k, v in r.items() if k not in ("work_id", "user_id", "rowid")})
    for aid, keep in target.items():
        if aid == keep:
            continue
        for r in led.select("media_progress", "asset_id=?", (aid,)):
            mine = led.select("media_progress", "asset_id=? AND user_id=?", (keep, r["user_id"]))
            if mine and (r["updated_at"] or 0) > (mine[0]["updated_at"] or 0):
                led.update("media_progress", mine[0], position=r["position"], location=r["location"], percent=r["percent"],
                           updated_at=r["updated_at"], file_id=r["file_id"])


def _settle_derived(led: _Ledger, notes: list[dict[str, Any]], deps: list[dict[str, str]]) -> None:
    """Converted copies that lose their slot (see _asset_targets): their file rows are deleted
    (the files stay on disk and stay known as generated), their conversion rows record why, and a
    retired canonical copy hands its progress and bookmarks to the real asset that replaces it."""
    for n in notes:
        if n["action"] not in ("dropped", "retired"):
            continue
        aid = n["asset_id"]
        for f in led.select("media_files", "asset_id=?", (aid,)):
            led.delete("media_files", f)
        for c in led.select("conversions", "output_asset_id=?", (aid,)):
            led.update("conversions", c, status="superseded", output_asset_id=None)
        if n["action"] == "retired":
            for d in deps:
                if HANDLED.get((d["table"], d["column"])) == "asset" and (d["table"], d["column"]) != ("media_files", "asset_id"):
                    _repoint(led, d["table"], d["column"], {aid: n["replaced_by"]})
            led.delete("media_assets", led.select("media_assets", "id=?", (aid,))[0])


def _merge_assets(led: _Ledger, canonical_id: str, sources: list[str], target: dict[str, str]) -> None:
    """Fold colliding assets into the canonical asset (flags OR-ed, Part XI) and re-point the rest.
    The folded asset's children move afterwards (HANDLED 'asset' tables), then the empty shell is deleted."""
    for aid, keep in target.items():
        if aid == keep:
            continue
        a, k = led.select("media_assets", "id=?", (aid,))[0], led.select("media_assets", "id=?", (keep,))[0]
        if a["derived_from_asset_id"]:
            continue    # a converted copy adds no ownership, monitoring or intent
        changes = {"monitored": int(bool(a["monitored"] or k["monitored"])),
                   "has_acquisition_intent": int(bool(a["has_acquisition_intent"] or k["has_acquisition_intent"]))}
        if "owned" in (a["status"], k["status"]):
            changes["status"] = "owned"
        if not k["edition_id"] and a["edition_id"]:
            changes["edition_id"] = a["edition_id"]
        led.update("media_assets", k, **changes)
    for aid, keep in target.items():
        if aid == keep:
            led.update("media_assets", led.select("media_assets", "id=?", (aid,))[0], "moved", work_id=canonical_id)


def _drop_folded_assets(led: _Ledger, target: dict[str, str]) -> None:
    """Delete the emptied source assets, each only after its full row is in the ledger."""
    for aid, keep in target.items():
        if aid != keep:
            led.delete("media_assets", led.select("media_assets", "id=?", (aid,))[0])


def _move_observations(led: _Ledger, canonical_id: str, sources: list[str]) -> None:
    """Re-point the sources' work observations; a current one yields to the canonical's current value."""
    for sid in sources:
        for r in led.select("metadata_observations", "entity_type='work' AND entity_id=?", (sid,)):
            changes: dict[str, Any] = {"entity_id": canonical_id}
            if r["is_current"] and led.conn.execute(
                    "SELECT 1 FROM metadata_observations WHERE entity_type='work' AND entity_id=? AND field_name=? AND is_current=1",
                    (canonical_id, r["field_name"])).fetchone():
                changes["is_current"] = 0
            led.update("metadata_observations", r, "moved", **changes)


def _observe(led: _Ledger, work_id: str, field: str, value: str, source: str, applied: bool, t: float) -> None:
    """The P4-03 observation trail (services/observations.record), written through the ledger so a
    reversal removes it; an applied value demotes the field's current observation."""
    if applied:
        for r in led.select("metadata_observations", "entity_type='work' AND entity_id=? AND field_name=? AND is_current=1", (work_id, field)):
            led.update("metadata_observations", r, is_current=0)
    led.insert("metadata_observations", {
        "id": db.uid(), "entity_type": "work", "entity_id": work_id, "field_name": field, "value": str(value), "source": source,
        "confidence": observations.CONFIDENCE.get(source, 0.0), "is_current": int(applied), "observed_at": t})


def _update_canonical(led: _Ledger, canonical_id: str, sources: list[str], resolutions: dict[str, Any], t: float) -> None:
    """Canonical-else-source for preferred editions and empty fields, OR for monitoring, then one ledgered update."""
    conn = led.conn
    canon = led.select("works", "id=?", (canonical_id,))[0]
    changes: dict[str, Any] = {}
    chosen = resolutions.get("fields") or {}
    for sid in sources:
        s = led.select("works", "id=?", (sid,))[0]
        for kind in ("ebook", "audiobook"):
            col = f"preferred_{kind}_edition_id"
            if not (changes.get(col) or canon[col]) and s[col]:
                changes[col] = s[col]
        if s["monitored"] and not (changes.get("monitored") or canon["monitored"]):
            changes["monitored"] = 1
        for f in FIELDS:
            now_value = changes.get(f, canon[f])
            if _empty(s[f]) or _norm(s[f]) == _norm(now_value):
                continue
            take = _empty(now_value) or chosen.get(f) == "source"
            locked = observations.is_locked(conn, "work", canonical_id, f)
            _observe(led, canonical_id, f, s[f], "manual" if take and not locked else "local", take and not locked, t)
            if take and not locked:
                changes[f] = s[f]
    if changes:
        led.update("works", canon, **changes, updated_at=t)


# ---------------------------------------------------------------------------
# History and reversal (P5-25a)
# ---------------------------------------------------------------------------

EVENT_REVERSED = "work.merge_reversed"


class MergeNotFound(LookupError):
    """No merge with this id."""


class ReversalBlocked(ValueError):
    """Rows the merge wrote were changed or removed since; nothing was written. ``conflicts`` lists them."""

    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__(f"reversal blocked: {len(conflicts)} row(s) changed since the merge")
        self.conflicts = conflicts


class ReversalRefused(ReversalBlocked):
    """A reversal that ``force`` cannot override: files of the merged books are in the trash (``files_in_trash``), or a later
    merge built on this one (``later_merge``). ``detail`` says what to do first."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__([])
        self.args = (message,)
        self.code, self.message, self.detail = code, message, detail


class ReversalFailed(RuntimeError):
    """The database refused a restored row (a forced reversal over later changes); nothing was written."""


def list_merges(limit: int = 50) -> list[dict[str, Any]]:
    """Recent merges, newest first, with the Works' titles (the sources no longer exist as Works)."""
    out = []
    for r in db.all("SELECT m.*,w.title canonical_title,(SELECT COUNT(*) FROM work_merge_ledger l WHERE l.merge_id=m.id) ledger_rows "
                    "FROM work_merges m LEFT JOIN works w ON w.id=m.canonical_work_id ORDER BY m.created_at DESC,m.id LIMIT ?", (max(1, min(limit, 200)),)):
        titles = {}
        for h in db.all("SELECT data_json FROM history WHERE event_type=? AND work_id=?", (EVENT_MERGED, r["canonical_work_id"])) if r["canonical_work_id"] else []:
            d = db.json_load(h["data_json"], {})
            if d.get("merge_id") == r["id"]:
                titles = {s["id"]: s["title"] for s in d.get("sources", [])}
        if not titles:   # reversed: the history event went with the reversal; the ledger still holds the deleted rows
            for e in db.all("SELECT before_json FROM work_merge_ledger WHERE merge_id=? AND table_name='works' AND action='deleted'", (r["id"],)):
                b = db.json_load(e["before_json"], {})
                titles[b.get("id")] = b.get("title")
        sources = db.json_load(r["source_work_ids_json"], [])
        out.append({"id": r["id"], "canonical_work_id": r["canonical_work_id"], "canonical_title": r["canonical_title"],
                    "sources": [{"id": s, "title": titles.get(s)} for s in sources], "status": r["status"],
                    "created_by": r["created_by"], "created_at": r["created_at"], "ledger_rows": r["ledger_rows"],
                    "resolutions": db.json_load(r["resolutions_json"], {})})
    return out


def _touched_works(conn: sqlite3.Connection, merge_id: str, canonical: str | None, sources: list[str]) -> set[str]:
    """Every Work id a merge's ledger mentions (a later merge repoints work_merges.canonical_work_id, so the stored canonical
    can no longer be trusted to name the Work this merge wrote to)."""
    ids = {w for w in [canonical, *sources] if w}
    for e in conn.execute("SELECT table_name,pk_json,before_json,after_json FROM work_merge_ledger WHERE merge_id=?", (merge_id,)):
        for blob in (e["before_json"], e["after_json"]):
            row = db.json_load(blob, {}) if blob else {}
            if isinstance(row, dict):
                if row.get("work_id"):
                    ids.add(row["work_id"])
                if e["table_name"] == "works" and row.get("id"):
                    ids.add(row["id"])
    return ids


# Columns written by read-only verification (redundantfiles) that must not count as "the row changed since the merge".
_IGNORED_COLS = {"media_files": {"checksum_verified", "checksum_verified_at"}}


def _canon(row: dict[str, Any] | None, table: str = "") -> str | None:
    if row is None:
        return None
    skip = _IGNORED_COLS.get(table, ())
    return db.json_dump({k: v for k, v in row.items() if k not in skip})


def reverse(merge_id: str, force: bool = False, user: Any = None, allow_kid_widening: bool = False) -> dict[str, Any]:
    """Undo a merge from its ledger, in ONE transaction: walk the entries newest first, take every row back to
    its before-image, delete the rows the merge inserted and re-create the source Works it deleted.

    Each step first compares the row as it is now with the after-image the merge left. Any difference (a
    progress update, a tag edit, a later merge that moved the row) is a conflict: without ``force`` nothing is
    written and ReversalBlocked lists them; with ``force`` the before-image wins and the conflicts are returned
    as warnings. Reversing an already reversed merge changes nothing (``already_reversed``). A database refusal
    (e.g. a forced restore into a row that no longer has its parent) rolls everything back, marks the merge
    ``reversal_failed`` and raises ReversalFailed; it can be retried."""
    by = user.get("id") if isinstance(user, dict) else user
    conflicts: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    t = db.now()
    try:
        with db.tx() as conn:
            m = conn.execute("SELECT * FROM work_merges WHERE id=?", (merge_id,)).fetchone()
            if not m:
                raise MergeNotFound(merge_id)
            if m["status"] == "reversed":
                return {"merge_id": merge_id, "status": "reversed", "already_reversed": True, "restored_rows": 0, "warnings": []}
            involved = list(_touched_works(conn, merge_id, m["canonical_work_id"], db.json_load(m["source_work_ids_json"], [])))
            for later in conn.execute("SELECT id,canonical_work_id,source_work_ids_json FROM work_merges WHERE status='executed' AND id<>? AND created_at>=? ORDER BY created_at",
                                      (merge_id, m["created_at"])).fetchall():
                if set(db.json_load(later["source_work_ids_json"], [])) & {w for w in involved if w}:
                    title = conn.execute("SELECT title FROM works WHERE id=?", (later["canonical_work_id"],)).fetchone()
                    raise ReversalRefused("later_merge", "A later merge built on this one"
                                          + (f" (into \"{title['title']}\")" if title else "") + ". Undo that merge first, then this one.", later_merge_id=later["id"])
            file_ids = [db.json_load(e["pk_json"], {}).get("id") for e in conn.execute("SELECT pk_json FROM work_merge_ledger WHERE merge_id=? AND table_name='media_files'", (merge_id,))]
            from . import filetrash
            waiting = filetrash.files_in_trash_for([w for w in involved if w], file_ids)
            if waiting:
                raise ReversalRefused("files_in_trash", f"{len(waiting)} file(s) of these books are in the trash. Restore them first (Settings > System > Trash), then undo the merge.", files=waiting)
            entries = conn.execute("SELECT * FROM work_merge_ledger WHERE merge_id=? ORDER BY id DESC", (merge_id,)).fetchall()
            kids = _kids(conn)
            existing_before = {w for w in involved if w and conn.execute("SELECT 1 FROM works WHERE id=?", (w,)).fetchone()}
            vis_before = {(k["id"], w): _kid_sees(conn, k["id"], w) for k in kids for w in existing_before}
            led = _Ledger(conn, merge_id)
            restored = 0
            for e in entries:
                table, pk = e["table_name"], db.json_load(e["pk_json"], {})
                before = db.json_load(e["before_json"], None) if e["before_json"] is not None else None
                after = db.json_load(e["after_json"], None) if e["after_json"] is not None else None
                where, params = " AND ".join(f"{_q(k)}=?" for k in pk), list(pk.values())
                if list(pk) == ["rowid"]:
                    sel = f"SELECT rowid AS rowid, * FROM {_q(table)} WHERE {where}"  # nosec B608
                else:
                    sel = f"SELECT * FROM {_q(table)} WHERE {where}"  # nosec B608
                cur = conn.execute(sel, params).fetchone()
                cur = dict(cur) if cur else None
                note = {"table": table, "key": pk, "action": e["action"]}
                if e["action"] == "deleted":
                    if cur is not None:
                        if _canon(cur, table) != _canon(before, table):
                            conflicts.append({**note, "problem": "a row with this key exists again and differs from the one the merge deleted"})
                        continue          # already restored (or recreated identically)
                    _insert_row(conn, table, before)
                else:
                    if after is not None and cur is None:
                        conflicts.append({**note, "problem": "the row the merge wrote was removed since"})
                        if before is not None and force and table == "media_files" and not os.path.lexists(before.get("path") or ""):
                            skipped_files.append({"file_id": before.get("id"), "path": before.get("path"), "why": "the file is gone from disk (purged or deleted); its record is not recreated"})
                            continue
                        if before is not None and force:
                            _insert_row(conn, table, before)
                        else:
                            continue
                    elif cur is not None and after is not None and _canon(cur, table) != _canon(after, table):
                        conflicts.append({**note, "problem": "the row changed since the merge"})
                        if not force:
                            continue
                    if cur is not None:
                        if before is None:
                            conn.execute(f"DELETE FROM {_q(table)} WHERE {where}", params)  # nosec B608
                        else:
                            cols = [c for c in before if c != "rowid" and c not in _IGNORED_COLS.get(table, ())]
                            conn.execute(f"UPDATE {_q(table)} SET {','.join(f'{_q(c)}=?' for c in cols)} WHERE {where}", [before[c] for c in cols] + params)  # nosec B608
                restored += 1
            if conflicts and not force:
                raise ReversalBlocked(conflicts)
            if not allow_kid_widening:
                canonical_ok = {k["id"]: any(vis_before.get((k["id"], w)) for w in existing_before) for k in kids}
                widened = []
                for k in kids:
                    for w in involved:
                        if not w or not conn.execute("SELECT 1 FROM works WHERE id=?", (w,)).fetchone() or not _kid_sees(conn, k["id"], w):
                            continue
                        if w in existing_before:
                            if not vis_before[(k["id"], w)]:
                                widened.append(k["username"])
                        elif not canonical_ok[k["id"]]:      # a work brought back for a kid who could not see the merged book
                            widened.append(k["username"])
                if widened:
                    raise ReversalRefused("kid_widening", "Undoing this merge over later changes would let " + ", ".join(sorted(set(widened))) + " see a book they cannot see now. "
                                          "Confirm that explicitly if that is what you want.", kids=sorted(set(widened)))
            conn.execute("UPDATE work_merges SET status='reversed' WHERE id=?", (merge_id,))
            canonical = m["canonical_work_id"]
            if canonical and conn.execute("SELECT 1 FROM works WHERE id=?", (canonical,)).fetchone():
                conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                             (db.uid(), canonical, None, EVENT_REVERSED, f"Undid the merge of {len(db.json_load(m['source_work_ids_json'], []))} Work(s)",
                              db.json_dump({"merge_id": merge_id, "by": by, "forced": bool(force), "restored_rows": restored,
                                            "source_work_ids": db.json_load(m["source_work_ids_json"], []), "conflicts": conflicts}), t))
            else:   # the audit trail must exist even if the canonical is gone
                conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                             (db.uid(), None, None, EVENT_REVERSED, "Undid a Work merge",
                              db.json_dump({"merge_id": merge_id, "by": by, "forced": bool(force), "restored_rows": restored}), t))
        return {"merge_id": merge_id, "status": "reversed", "already_reversed": False, "restored_rows": restored,
                "source_work_ids": db.json_load(m["source_work_ids_json"], []), "warnings": conflicts, "skipped_files": skipped_files}
    except sqlite3.IntegrityError as exc:
        try:
            with db.tx() as conn:
                conn.execute("UPDATE work_merges SET status='reversal_failed' WHERE id=? AND status='executed'", (merge_id,))
        except Exception:   # the status note is best effort; the original error is what matters
            pass
        raise ReversalFailed(str(exc)) from exc


def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = [c for c in row if c != "rowid"]
    if "rowid" in row and not _pk(conn, table):
        cols = ["rowid", *cols]
    conn.execute(f"INSERT INTO {_q(table)}({','.join(_q(c) for c in cols)}) VALUES ({_marks(len(cols))})", [row[c] for c in cols])  # nosec B608


LEDGER_RETENTION_KEY = "merge.ledger_retention_days"
DEFAULT_LEDGER_RETENTION_DAYS = 365


def ledger_retention_days() -> int:
    try:
        return max(1, min(int(db.get_setting(LEDGER_RETENTION_KEY, DEFAULT_LEDGER_RETENTION_DAYS)), 36500))
    except (TypeError, ValueError):
        return DEFAULT_LEDGER_RETENTION_DAYS


def prune_ledger(now: float | None = None) -> dict[str, int]:
    """Housekeeping: forget merges (and their ledger rows) older than the retention (default 365 days). A merge inside the
    window keeps its whole ledger, so Undo keeps working for it; an older one can no longer be undone. A merge that a newer,
    kept merge built on (it merged the old canonical away) is kept too, so a chain is never cut in the middle."""
    cutoff = (db.now() if now is None else now) - ledger_retention_days() * 86400
    with db.tx() as conn:
        keep_sources: set[str] = set()
        for r in conn.execute("SELECT canonical_work_id,source_work_ids_json FROM work_merges WHERE created_at>=? AND status='executed'", (cutoff,)):
            keep_sources.update(db.json_load(r["source_work_ids_json"], []))
        old = [r for r in conn.execute("SELECT id,canonical_work_id FROM work_merges WHERE created_at<?", (cutoff,)).fetchall()]
        doomed = [r["id"] for r in old if not (_touched_works(conn, r["id"], r["canonical_work_id"], []) & keep_sources)]
        for mid in doomed:
            conn.execute("DELETE FROM work_merge_ledger WHERE merge_id=?", (mid,))
            conn.execute("DELETE FROM work_merges WHERE id=?", (mid,))
    return {"merges_pruned": len(doomed)}


def dissimilar_blockers(canonical_id: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``dissimilar_works`` blockers ``execute`` would raise for this preview unless ``confirm_dissimilar`` is passed
    (read-only; the bulk merge's dry run uses it to keep titles-differ pairs out of an unattended run)."""
    return _dissimilar(None, canonical_id, plan)
