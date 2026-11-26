"""Working down a large Inbox of open `metadata_match` review tasks.

The Readarr migration left thousands of identical tasks.  This groups them into four
categories (each task falls in exactly one, first match wins) and offers two bulk actions:

* identifier-exact: a stored provider candidate shares an exact identifier with the work
  (or the candidate's own id is one of the work's identifiers).  Safe to link automatically.
* has-owned-files: the work has an owned media asset; a human should look, never bulk-dismissed.
* hopeless: no identifiers, no owned asset and no stored candidate; nothing a matcher or a
  person could act on.
* other.

Everything is bounded by `limit`; the category predicates are SQL so counting 6k+ tasks is
a handful of indexed queries rather than a Python loop (only the tasks that carry stored
candidates are compared in Python, because that comparison is not expressible in SQL).
"""
from __future__ import annotations

import re
from typing import Any

from .. import db
from . import metadata

CATEGORIES = ("identifier-exact", "has-owned-files", "hopeless", "other")
EXACT_SCAN_LIMIT = 5000

_OPEN = "t.kind='metadata_match' AND t.status='open'"
_HAS_CANDIDATES = "json_extract(t.payload_json,'$.candidates[0]') IS NOT NULL"
_OWNED = "EXISTS (SELECT 1 FROM media_assets a WHERE a.work_id=t.entity_id AND a.status='owned')"
_HAS_IDS = "EXISTS (SELECT 1 FROM identifiers i WHERE i.entity_type='work' AND i.entity_id=t.entity_id)"
_WORK_EXISTS = "t.entity_type='work' AND EXISTS (SELECT 1 FROM works w WHERE w.id=t.entity_id)"
_HOPELESS = f"{_WORK_EXISTS} AND NOT {_HAS_IDS} AND NOT {_OWNED} AND NOT {_HAS_CANDIDATES}"


def _key(value: Any) -> str:
    """Compare identifiers ignoring hyphens, spaces and case (978-0-... == 9780...)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _chunks(items: list[str], n: int = 500):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _work_keys(work_ids: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for chunk in _chunks(work_ids):
        rows = db.all(
            "SELECT entity_id,value FROM identifiers WHERE entity_type='work' AND entity_id IN (SELECT value FROM json_each(?))",
            (db.json_dump(chunk),),
        )
        for r in rows:
            k = _key(r["value"])
            if len(k) >= 8:  # a short value is not evidence of sameness
                out.setdefault(r["entity_id"], set()).add(k)
    return out


def _exact_candidate(payload: dict[str, Any], keys: set[str]) -> str | None:
    """The one candidate id that shares an identifier with the work, or None.

    Two different candidates matching is ambiguous, so it is left for a person."""
    if not keys:
        return None
    hits = []
    for c in payload.get("candidates") or []:
        cid = c.get("id")
        if not cid:
            continue
        cand_keys = {_key(c.get("id"))} | {_key(i.get("value")) for i in (c.get("identifiers") or []) if isinstance(i, dict)}
        if keys & {k for k in cand_keys if len(k) >= 8}:
            hits.append(cid)
    return hits[0] if len(set(hits)) == 1 else None


def _candidate_matches(limit: int) -> list[tuple[dict[str, Any], str]]:
    """Open tasks (at most `limit` examined) paired with their exact-identifier candidate id."""
    tasks = db.all(
        f"""SELECT t.id,t.entity_id,t.payload_json FROM review_tasks t
            WHERE {_OPEN} AND t.entity_type='work' AND t.entity_id IS NOT NULL AND {_HAS_CANDIDATES}
            ORDER BY t.created_at LIMIT ?""",  # nosec B608
        (limit,),
    )
    keys = _work_keys(sorted({t["entity_id"] for t in tasks}))
    found = []
    for t in tasks:
        cid = _exact_candidate(db.json_load(t["payload_json"], {}) or {}, keys.get(t["entity_id"], set()))
        if cid:
            found.append((t, cid))
    return found


def _category_sql(category: str, exact_ids: list[str]) -> tuple[str, tuple]:
    ids = db.json_dump(exact_ids)
    in_exact = "t.id IN (SELECT value FROM json_each(?))"
    if category == "identifier-exact":
        return in_exact, (ids,)
    if category == "has-owned-files":
        return f"{_OWNED} AND NOT {in_exact}", (ids,)
    if category == "hopeless":
        return f"({_HOPELESS})", ()
    if category == "other":
        return f"NOT {in_exact} AND NOT {_OWNED} AND NOT ({_HOPELESS})", (ids,)
    raise ValueError(f"Unknown category: {category}")


def category_filter(category: str) -> tuple[str, tuple]:
    """WHERE fragment (alias `t`) selecting open metadata_match tasks in a category."""
    exact_ids = [t["id"] for t, _ in _candidate_matches(EXACT_SCAN_LIMIT)]
    sql, params = _category_sql(category, exact_ids)
    return f"{_OPEN} AND {sql}", params


def summary(limit: int = EXACT_SCAN_LIMIT) -> dict[str, Any]:
    """Counts per category for open metadata_match tasks; `limit` bounds the Python scan."""
    exact_ids = [t["id"] for t, _ in _candidate_matches(limit)]
    counts = {}
    for cat in CATEGORIES:
        sql, params = _category_sql(cat, exact_ids)
        counts[cat] = db.one(f"SELECT COUNT(*) n FROM review_tasks t WHERE {_OPEN} AND {sql}", params)["n"]  # nosec B608
    return {"categories": counts, "total": sum(counts.values())}


def auto_resolve_identifier_matches(limit: int = 100) -> dict[str, Any]:
    """Link works to the stored candidate that shares an exact identifier, and resolve the task.

    Goes through metadata.refresh_work(work_id, metadata_id=...), the same call an operator's
    "pick this match" makes, so linking, history and task resolution behave identically."""
    limit = max(1, min(int(limit), 1000))
    linked, errors = 0, []
    for task, cid in _candidate_matches(limit):
        try:
            result = metadata.refresh_work(task["entity_id"], metadata_id=cid)
        except Exception as exc:  # one provider hiccup or duplicate link must not stop the batch
            errors.append({"task_id": task["id"], "work_id": task["entity_id"], "error": str(exc)})
            continue
        if result.get("status") == "linked":
            linked += 1
            with db.tx() as conn:
                conn.execute(
                    "UPDATE review_tasks SET resolution_json=? WHERE id=? AND status='resolved'",
                    (db.json_dump({"metadata_id": cid, "auto": "identifier-exact"}), task["id"]),
                )
        else:
            errors.append({"task_id": task["id"], "work_id": task["entity_id"], "error": f"unexpected status {result.get('status')}"})
    if linked:
        db.add_history("review.auto_resolve", f"Auto-resolved {linked} review task(s) on exact identifier match", data={"linked": linked})
    return {"linked": linked, "errors": errors, "remaining": summary()["categories"]["identifier-exact"]}


def bulk_ignore_hopeless(limit: int = 500) -> dict[str, Any]:
    """Ignore up to `limit` tasks whose work has no identifiers, no owned file and no candidates."""
    limit = max(1, min(int(limit), 5000))
    reason = {"auto": "hopeless", "reason": "no identifiers, no owned files and no candidates"}
    with db.tx() as conn:
        cur = conn.execute(
            f"""UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=?
                WHERE id IN (SELECT t.id FROM review_tasks t WHERE {_OPEN} AND {_HOPELESS}
                             ORDER BY t.created_at LIMIT ?)""",  # nosec B608
            (db.now(), db.json_dump(reason), limit),
        )
        ignored = cur.rowcount
    if ignored:
        db.add_history("review.ignore_hopeless", f"Ignored {ignored} hopeless review task(s)", data={"ignored": ignored})
    return {"ignored": ignored}
