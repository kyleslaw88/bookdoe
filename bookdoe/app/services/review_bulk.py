"""Inbox filters and bulk actions over review_tasks (every kind).

A selection is either explicit ids or "everything matching this filter, minus these
ids" (select-all across pages). The bulk runner walks it in bounded batches and calls
the SAME per-item code the Inbox buttons use (tagging.approve_kids_proposal /
reject_kids_proposal, the ignore update), so kids_decisions, history and resolution
records are identical whether one item or a thousand were acted on. Per-item outcomes
are returned (applied / skipped / failed with a reason) and the run is audited with
the count and the filter.

Which bulk actions a kind supports (documented in docs/PLAYER_MANAGEMENT.md):
  kids_tag        approve, reject            (both record kids_decisions)
  kids_recheck    keep, remove               (manual_tag / manual_untag: final; nothing is untagged until an admin acts)
  metadata_match  ignore only                (linking needs a per-item pick: use the
                                              triage auto-resolve for exact matches)
  anything else   ignore only
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from . import kidsrecheck, review_triage, tagging

log = logging.getLogger(__name__)

STATUSES = ("open", "resolved", "ignored")
BATCH = 200
MAX_SELECTION = 20000
KIND_ACTIONS: dict[str, tuple[str, ...]] = {"kids_tag": ("approve", "reject", "ignore"), "kids_recheck": ("keep", "remove")}
DEFAULT_ACTIONS: tuple[str, ...] = ("ignore",)


def actions_for(kind: str) -> tuple[str, ...]:
    return KIND_ACTIONS.get(kind, DEFAULT_ACTIONS)


def build_filter(*, kind: str | None = None, status: str | None = "open", q: str | None = None,
                 category: str | None = None) -> tuple[str, tuple[Any, ...]]:
    """WHERE fragment (alias `t`) and params. `category` only means something for
    metadata_match and reuses review_triage's category predicates."""
    where: list[str] = []
    params: list[Any] = []
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        where.append("t.status=?"); params.append(status)
    if kind:
        where.append("t.kind=?"); params.append(kind)
    if q and q.strip():
        like = db.like_pattern(q.strip())
        where.append("(t.title LIKE ? ESCAPE '\\' OR t.payload_json LIKE ? ESCAPE '\\')"); params += [like, like]
    if category:
        cat_sql, cat_params = review_triage.category_filter(category)
        where.append(f"({cat_sql})"); params += list(cat_params)
    return (" AND ".join(where) or "1=1"), tuple(params)


def kind_counts(status: str = "open") -> dict[str, int]:
    return {r["kind"]: r["n"] for r in db.all("SELECT kind, COUNT(*) n FROM review_tasks WHERE status=? GROUP BY kind ORDER BY kind", (status,))}


def list_tasks(*, limit: int, offset: int, **filters) -> dict[str, Any]:
    where, params = build_filter(**filters)
    total = db.one(f"SELECT COUNT(*) n FROM review_tasks t WHERE {where}", params)["n"]  # nosec B608
    rows = db.all(f"SELECT t.* FROM review_tasks t WHERE {where} ORDER BY t.priority DESC,t.created_at LIMIT ? OFFSET ?", (*params, limit, offset))  # nosec B608
    for r in rows:
        r["payload"] = db.json_load(r.pop("payload_json"), {})
        r["bulk_actions"] = list(actions_for(r["kind"]))
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def _selection_ids(ids: list[str] | None, filters: dict[str, Any] | None, exclude: list[str] | None) -> list[str]:
    exclude_set = set(exclude or [])
    if ids is not None:
        # Keep the caller's order, drop duplicates and excluded ids.
        seen: set[str] = set()
        out = []
        for i in ids:
            if i in exclude_set or i in seen:
                continue
            seen.add(i); out.append(i)
        return out[:MAX_SELECTION]
    where, params = build_filter(**(filters or {}))
    rows = db.all(f"SELECT t.id FROM review_tasks t WHERE {where} ORDER BY t.priority DESC,t.created_at LIMIT ?", (*params, MAX_SELECTION))  # nosec B608
    return [r["id"] for r in rows if r["id"] not in exclude_set]


def _apply_one(task: dict[str, Any], action: str, actor: dict[str, Any]) -> dict[str, Any]:
    tid, kind = task["id"], task["kind"]
    if action not in actions_for(kind):
        return {"id": tid, "kind": kind, "result": "skipped", "reason": f"'{action}' is not a bulk action for {kind}"}
    if task["status"] != "open":
        return {"id": tid, "kind": kind, "result": "skipped", "reason": f"already {task['status']}"}
    try:
        if kind == "kids_tag" and action == "approve":
            tagging.approve_kids_proposal(tid, decided_by=actor.get("username") or actor.get("id"), decided_by_id=actor.get("id"))
        elif kind == "kids_tag" and action == "reject":
            tagging.reject_kids_proposal(tid, decided_by=actor.get("username") or actor.get("id"), decided_by_id=actor.get("id"))
        elif kind == "kids_recheck":
            kidsrecheck.resolve_task(tid, action, by_user_id=actor.get("id"), by_name=actor.get("username") or actor.get("id"))
        else:  # ignore: the same statement the per-item route runs
            with db.tx() as conn:
                n = conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=?,resolution_json=? WHERE id=? AND status='open'",
                                 (db.now(), db.json_dump({"action": "ignored", "by": actor.get("id"), "bulk": True}), tid)).rowcount
            if not n:
                return {"id": tid, "kind": kind, "result": "skipped", "reason": "already decided"}
        return {"id": tid, "kind": kind, "result": "applied"}
    except ValueError as exc:
        return {"id": tid, "kind": kind, "result": "skipped", "reason": str(exc)}
    except Exception as exc:  # one bad row must not stop the batch
        log.warning("bulk %s on %s failed: %s", action, tid, exc)
        return {"id": tid, "kind": kind, "result": "failed", "reason": str(exc)[:200]}


def run_bulk(*, action: str, actor: dict[str, Any], ids: list[str] | None = None, filters: dict[str, Any] | None = None,
             exclude: list[str] | None = None, kind: str | None = None) -> dict[str, Any]:
    """Apply `action` to the selection. `kind` (required for a filter selection, and
    checked against explicit ids) keeps one bulk from leaking across kinds: a
    "select all kids suggestions, approve" can never touch a metadata task."""
    if action not in ("approve", "reject", "ignore", "keep", "remove"):
        raise ValueError("action must be approve, reject, ignore, keep or remove")
    if ids is None and not kind:
        raise ValueError("A filter selection needs a kind")
    if ids is None:
        filters = {**(filters or {}), "kind": kind, "status": "open"}
    selected = _selection_ids(ids, filters, exclude)
    results: list[dict[str, Any]] = []
    for start in range(0, len(selected), BATCH):
        chunk = selected[start:start + BATCH]
        marks = ",".join("?" * len(chunk))
        rows = {r["id"]: r for r in db.all(f"SELECT id,kind,status FROM review_tasks WHERE id IN ({marks})", tuple(chunk))}  # nosec B608
        for tid in chunk:
            task = rows.get(tid)
            if not task:
                results.append({"id": tid, "kind": None, "result": "skipped", "reason": "not found"}); continue
            if kind and task["kind"] != kind:
                results.append({"id": tid, "kind": task["kind"], "result": "skipped", "reason": f"not a {kind} task"}); continue
            results.append(_apply_one(task, action, actor))
    counts = {k: sum(1 for r in results if r["result"] == k) for k in ("applied", "skipped", "failed")}
    db.add_history("review.bulk", f"Bulk {action} on {len(selected)} review task(s): {counts['applied']} applied, {counts['skipped']} skipped, {counts['failed']} failed (by {actor.get('username') or actor.get('id')})",
                   data={"action": action, "kind": kind, "selected": len(selected), "filter": filters if ids is None else None,
                         "explicit_ids": ids is not None, "excluded": len(exclude or []), "by": actor.get("id"), **counts})
    log.info("review bulk %s: %s", action, counts)
    return {"action": action, "kind": kind, "selected": len(selected), **counts, "items": results}
