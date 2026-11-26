"""Bulk merge of duplicate groups (admin): dry run, batches, per-merge ledger entries, batch undo, post-merge refresh.

Every group is merged by ``workmerge.execute`` in its OWN transaction, so each one has its own ledger entry and its own
Undo; a batch only ties them together (``merge_batches`` / ``merge_batch_items``) so one click can undo the lot, newest first,
stopping at the first conflict. Nothing here deletes a file: a merge moves database records and redundant copies are handled
by the separate, confirmed Duplicate files step.

What an unattended run will NOT do (the group is listed as "needs review" and left to the normal merge dialog):
  * anything ``workmerge.preview`` calls a blocker: kids-visibility widening (a bulk run never carries a consent key, so it can
    never widen what a child sees), kids-tag mismatches, unhandled tables, vanished or already-merged sources;
  * titles that differ (``dissimilar_works``; the same default-deny as ``confirm_dissimilar``);
  * choices only a person can make: per-user progress, rating or reading-status conflicts, a differing series position;
  * groups that overlap another selected group, or that changed since the scan (a member is gone).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Callable

from .. import db
from . import dupefinder, workmerge, writelimit

MAX_PER_REQUEST = 10      # merges done by one HTTP request; the page asks again until the batch is finished
MAX_SELECTION = 500
REFRESH_BATCH = 10
REFRESH_ATTEMPTS = 3
STALE_RUNNING_SECONDS = 900      # a claim ('running') whose worker has not touched its heartbeat (updated_at) for this long is given back
STALE_UNDOING_SECONDS = 900      # an 'undoing' batch whose undo has made no heartbeat for this long may be resumed
GRACE_DAYS_KEY = "duplicates.refresh_grace_days"
GRACE_DAYS_DEFAULT = 7.0
EVENT_BULK = "work.bulk_merged"
EVENT_FINALIZED = "work.bulk_finalized"
UNDOABLE = ("done", "stopped", "partly_undone")      # (an already undone batch may be "undone" again: a no-op)
# Conflicts that need a human choice. Everything else the preview lists is either informational or resolved by the
# canonical-wins rule that the ordinary merge dialog also applies by default.
CHOICE_CONFLICTS = {
    "progress_conflict": "Reading or listening progress exists on both records for the same person",
    "rating_conflict": "Both records carry a different rating from the same person",
    "read_state_conflict": "Both records carry a different reading status from the same person",
    "series_position_conflict": "The records name different positions in the same series",
}


_LIVE: set[int] = set()      # item ids this process is merging right now: recover_stale never gives these back
_LIVE_LOCK = threading.Lock()


class BatchBusy(Exception):
    """The batch cannot be undone right now (still merging, or another undo is running)."""


def _reason(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def evaluate(group: dict[str, Any], canonical_override: str | None = None) -> dict[str, Any]:
    """Dry run for one group: what moves and why it may not be bulk merged. Strictly read-only."""
    ids = list(group["work_ids"])
    out: dict[str, Any] = {"group_id": group["id"], "title": group["title"], "confidence": group["confidence"], "kind": group["kind"],
                           "canonical_work_id": None, "source_work_ids": [], "status": "ready", "reasons": [], "moves": {}}
    existing = {r["id"] for r in db.all(f"SELECT id FROM works WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else set()  # nosec B608
    if len(existing) < 2:
        out["status"], out["reasons"] = "stale", [_reason("stale_group", "A record in this group no longer exists; scan again.")]
        return out
    canonical = canonical_override or group["canonical_work_id"]
    if canonical not in existing:
        out["status"], out["reasons"] = "stale", [_reason("stale_group", "The recommended record no longer exists; scan again.")]
        return out
    sources = [i for i in ids if i in existing and i != canonical]
    out["canonical_work_id"], out["source_work_ids"] = canonical, sources
    if len(sources) > workmerge.MAX_SOURCES:
        out["status"], out["reasons"] = "review", [_reason("too_many", f"More than {workmerge.MAX_SOURCES} records; merge them in the dialog.")]
        return out
    try:
        plan = workmerge.preview(canonical, sources)
    except (workmerge.WorkNotFound, workmerge.BadRequest) as exc:
        out["status"], out["reasons"] = "stale", [_reason("stale_group", str(exc) or "Not found")]
        return out
    reasons: list[dict[str, Any]] = []
    for b in plan["blockers"]:
        reasons.append(_reason(b["code"], b.get("message") or b["code"], kids=b.get("kids"), work_id=b.get("work_id"), kind="blocker"))
    for b in workmerge.dissimilar_blockers(canonical, plan):
        reasons.append(_reason("dissimilar_works", b["message"], work_id=b.get("work_id"), kind="blocker"))
    for c in plan["conflicts"]:
        label = CHOICE_CONFLICTS.get(c["type"])
        if label and not (c["type"] == "series_position_conflict" and not str(c.get("canonical_position") or "").strip()):
            reasons.append(_reason(c["type"], label, kind="choice"))
    out["reasons"] = reasons
    out["status"] = "review" if reasons else "ready"
    tables = {t["table"]: t for t in plan["plan"]["tables"]}
    moved = lambda name: (tables.get(name) or {}).get("move", 0)   # noqa: E731
    out["moves"] = {"files": moved("media_files"), "assets": moved("media_assets"), "tags": len(plan["plan"]["tag_union"]),
                    "history": moved("history"), "identifiers": moved("identifiers"),
                    "folded_assets": sum(1 for c in plan["conflicts"] if c["type"] == "asset_collision"),
                    "field_fills": [c["field"] for c in plan["conflicts"] if c["type"] == "field_fill"],
                    "field_conflicts": [c["field"] for c in plan["conflicts"] if c["type"] == "field_conflict"],
                    "sources": [{"id": s["id"], "title": s["title"]} for s in plan["sources"]]}
    out["canonical_title"] = plan["canonical"]["title"]
    return out


def _groups(group_ids: list[str]) -> list[dict[str, Any]]:
    from .dupefinder import _row
    rows = []
    for gid in group_ids:
        r = db.one("SELECT * FROM duplicate_groups WHERE id=?", (gid,))
        if r:
            rows.append(_row(r))
    return rows


def dry_run(group_ids: list[str], overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Evaluate the selection. Groups that overlap an earlier selected group are held back (a Work can only be merged once)."""
    group_ids = list(dict.fromkeys(group_ids))[:MAX_SELECTION]
    overrides = overrides or {}
    seen_works: set[str] = set()
    results = []
    by_id = {g["id"]: g for g in _groups(group_ids)}
    for gid in group_ids:
        g = by_id.get(gid)
        if not g:
            results.append({"group_id": gid, "status": "stale", "title": None, "reasons": [_reason("not_found", "No such group.")], "moves": {}})
            continue
        if g["dismissed"]:
            results.append({"group_id": gid, "title": g["title"], "status": "review", "reasons": [_reason("dismissed", "Marked as not the same book.")], "moves": {}})
            continue
        if seen_works & set(g["work_ids"]):
            results.append({"group_id": gid, "title": g["title"], "status": "review", "reasons": [_reason("overlap", "Shares a record with another selected group; merge one first.")], "moves": {}})
            continue
        ev = evaluate(g, overrides.get(gid))
        if ev["status"] == "ready":
            seen_works |= set(g["work_ids"])
        results.append(ev)
    return {"groups": results, "ready": sum(1 for r in results if r["status"] == "ready"), "review": sum(1 for r in results if r["status"] == "review"),
            "stale": sum(1 for r in results if r["status"] == "stale"), "total": len(results)}


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

def start(group_ids: list[str], user: Any, overrides: dict[str, str] | None = None, stop_on_error: bool = False, refresh: bool = True) -> dict[str, Any]:
    """Create a batch from a dry run: ready groups become pending items, everything else is recorded as skipped (with the
    reason) so the results table is complete. Nothing is merged yet; ``process`` does that in chunks."""
    plan = dry_run(group_ids, overrides)
    by = user.get("id") if isinstance(user, dict) else user
    batch_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO merge_batches(id,status,options_json,created_by,created_at,total) VALUES (?, 'running', ?, ?, ?, ?)",
                     (batch_id, db.json_dump({"stop_on_error": bool(stop_on_error), "refresh": bool(refresh)}), by, t, plan["total"]))
        for i, r in enumerate(plan["groups"]):
            ready = r["status"] == "ready"
            conn.execute("INSERT INTO merge_batch_items(batch_id,ord,group_id,canonical_work_id,source_work_ids_json,title,status,reason,detail_json,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (batch_id, i, r["group_id"], r.get("canonical_work_id") or "", db.json_dump(r.get("source_work_ids") or []), r.get("title"),
                          "pending" if ready else "skipped", None if ready else "; ".join(x["code"] for x in r["reasons"]) or r["status"],
                          db.json_dump({"reasons": r["reasons"], "moves": r.get("moves")}), None if ready else t))
    if not any(r["status"] == "ready" for r in plan["groups"]):
        _close(batch_id)
    return batch(batch_id)


def _close(batch_id: str) -> None:
    """A batch with nothing left to do is finished (a stopped one keeps its 'stopped' status)."""
    recover_stale(batch_id)
    left = db.one("SELECT COUNT(*) n FROM merge_batch_items WHERE batch_id=? AND status IN ('pending','running')", (batch_id,))["n"]
    if left == 0:
        with db.tx() as conn:
            conn.execute("UPDATE merge_batches SET status=CASE WHEN status='running' THEN 'done' ELSE status END,finished_at=COALESCE(finished_at,?) WHERE id=?", (db.now(), batch_id))


def _touch(item_id: int) -> None:
    """Worker heartbeat: proves the claim is alive. Never raises (a missed beat only makes the claim look older)."""
    try:
        with db.tx() as conn:
            conn.execute("UPDATE merge_batch_items SET updated_at=? WHERE id=? AND status='running'", (db.now(), item_id))
    except sqlite3.Error:
        pass


def _stale_items_sql() -> str:
    return "status='running' AND COALESCE(updated_at,finished_at,0)<?"


def stale_claims(batch_id: str) -> int:
    """Claims of this batch whose worker has gone quiet (read-only; what ``recover_stale`` would act on)."""
    cutoff = db.now() - STALE_RUNNING_SECONDS
    with _LIVE_LOCK:
        live = set(_LIVE)
    return sum(1 for r in db.all(f"SELECT id FROM merge_batch_items WHERE batch_id=? AND {_stale_items_sql()}", (batch_id, cutoff)) if r["id"] not in live)  # nosec B608


def _claim_sources(item: dict[str, Any]) -> list[str]:
    """The sources this item's worker actually merged: recorded on the item (``detail.claim_sources``) just before the merge
    ran, because the worker recomputes them at claim time (only works that still exist). Falls back to the stored plan."""
    used = db.json_load(item.get("detail_json"), {}).get("claim_sources")
    return list(used) if isinstance(used, list) else db.json_load(item["source_work_ids_json"], [])


def _record_claim_sources(item_id: int, sources: list[str]) -> None:
    """Persist the source ids about to be merged (same write as a heartbeat) so recovery can match the committed merge exactly."""
    with db.tx() as conn:
        row = conn.execute("SELECT detail_json FROM merge_batch_items WHERE id=? AND status='running'", (item_id,)).fetchone()
        if row:
            detail = db.json_load(row["detail_json"], {})
            detail["claim_sources"] = list(sources)
            conn.execute("UPDATE merge_batch_items SET detail_json=?,updated_at=? WHERE id=?", (db.json_dump(detail), db.now(), item_id))


def _find_own_merge(item: dict[str, Any]) -> dict[str, Any] | None:
    """The executed merge this item made: INTO its canonical since its claim, with EXACTLY the sources it merged (a manual
    merge of other sources must never be pinned to the batch), oldest first, and not already attached to any batch item."""
    want = sorted(_claim_sources(item))
    if not want:
        return None
    for m in db.all("SELECT id,source_work_ids_json FROM work_merges WHERE canonical_work_id=? AND created_at>=? AND status='executed' ORDER BY created_at ASC,id",
                    (item["canonical_work_id"], (item["finished_at"] or 0) - 1)):
        if sorted(db.json_load(m["source_work_ids_json"], [])) != want:
            continue
        if db.one("SELECT 1 x FROM merge_batch_items WHERE merge_id=?", (m["id"],)):
            continue
        return m
    return None


def recover_stale(batch_id: str | None = None) -> int:
    """Give back items a worker claimed ('running') and then died on (no heartbeat for STALE_RUNNING_SECONDS; a claim this
    process is working on is never touched, however long its merge takes). If its merge did commit (an executed ledger entry
    into the canonical with exactly the item's sources since the claim) the item is recorded as merged, like a normal merge
    (group status, history event, held refresh) so batch undo still sees it; otherwise it goes back to pending.
    Without ``batch_id`` every running batch is checked."""
    cutoff = db.now() - STALE_RUNNING_SECONDS
    if batch_id:
        rows = db.all(f"SELECT * FROM merge_batch_items WHERE batch_id=? AND {_stale_items_sql()}", (batch_id, cutoff))  # nosec B608
    else:
        rows = db.all(f"SELECT * FROM merge_batch_items WHERE {_stale_items_sql()}", (cutoff,))  # nosec B608
    n = 0
    for it in rows:
        with _LIVE_LOCK:
            if it["id"] in _LIVE:
                continue
        m = _find_own_merge(it)
        recovered = back = False
        with db.tx() as conn:
            if m:
                recovered = conn.execute("UPDATE merge_batch_items SET status='merged',merge_id=?,finished_at=?,updated_at=? WHERE id=? AND status='running' AND COALESCE(updated_at,finished_at,0)<?",
                                         (m["id"], db.now(), db.now(), it["id"], cutoff)).rowcount == 1
            else:
                back = conn.execute("UPDATE merge_batch_items SET status='pending',finished_at=NULL,updated_at=NULL WHERE id=? AND status='running' AND COALESCE(updated_at,finished_at,0)<?",
                                    (it["id"], cutoff)).rowcount == 1
        if recovered:
            b = db.one("SELECT created_by FROM merge_batches WHERE id=?", (it["batch_id"],))
            _after_merge(it["batch_id"], it, m["id"], _claim_sources(it), b["created_by"] if b else None, db.now())
        n += int(recovered or back)
    return n


def housekeeping() -> dict[str, int]:
    """Scheduled housekeeping: give back dead claims of every batch and finish batches that have nothing left to do."""
    n = recover_stale()
    closed = 0
    for r in db.all("SELECT id FROM merge_batches WHERE status='running'"):
        before = _status(r["id"])
        _close(r["id"])
        closed += int(before != _status(r["id"]))
    return {"claims_recovered": n, "batches_closed": closed}


def _status(batch_id: str) -> str | None:
    r = db.one("SELECT status FROM merge_batches WHERE id=?", (batch_id,))
    return r["status"] if r else None


def process(batch_id: str, user: Any, limit: int | None = None) -> dict[str, Any]:
    """Merge up to ``limit`` pending items, one transaction each, then report. Safe to call again and again (the page does,
    and so does the resume job), also concurrently: every item is CLAIMED atomically (pending -> running) before it is merged,
    so two callers never merge the same item, and the batch status is re-read before each item so a batch that was stopped or
    is being undone is left alone. Stops at once on a rate limit (the rest stay pending)."""
    b = db.one("SELECT * FROM merge_batches WHERE id=?", (batch_id,))
    if not b:
        raise LookupError(batch_id)
    opts = db.json_load(b["options_json"], {})
    limit = limit or MAX_PER_REQUEST
    done = 0
    retry_after = None
    recover_stale(batch_id)
    while done < limit and _status(batch_id) == "running":
        item = db.one("SELECT * FROM merge_batch_items WHERE batch_id=? AND status='pending' ORDER BY ord LIMIT 1", (batch_id,))
        if not item:
            break
        try:
            writelimit.check(user)
        except writelimit.RateLimited as exc:
            retry_after = exc.retry_after
            break
        with db.tx() as conn:
            claimed = conn.execute("UPDATE merge_batch_items SET status='running',finished_at=?,updated_at=? WHERE id=? AND status='pending' "
                                   "AND EXISTS (SELECT 1 FROM merge_batches WHERE id=? AND status='running')", (db.now(), db.now(), item["id"], batch_id)).rowcount
        if not claimed:      # another worker has it (or the batch left 'running'): look again
            continue
        with _LIVE_LOCK:
            _LIVE.add(item["id"])
        try:
            outcome = _merge_item(batch_id, item, user)
        finally:
            with _LIVE_LOCK:
                _LIVE.discard(item["id"])
        done += 1
        if outcome == "failed" and opts.get("stop_on_error"):
            with db.tx() as conn:
                conn.execute("UPDATE merge_batch_items SET status='skipped',reason='stopped after an error',finished_at=? WHERE batch_id=? AND status='pending'", (db.now(), batch_id))
                conn.execute("UPDATE merge_batches SET status='stopped',finished_at=? WHERE id=? AND status='running'", (db.now(), batch_id))
            break
    _close(batch_id)
    out = batch(batch_id)
    out["processed"] = done      # merges attempted by THIS call (0 = no progress: callers must back off)
    if retry_after:
        out["rate_limited"] = {"retry_after": retry_after}
    return out


def _merge_item(batch_id: str, item: dict[str, Any], user: Any) -> str:
    """Re-check and merge one CLAIMED item (status 'running'); returns merged / skipped / failed. A result other than 'merged'
    is written only from the claimed state; 'merged' is also accepted over a claim that was wrongly given back meanwhile
    (pending / skipped without a merge), so a committed merge is never left unrecorded and a recorded one never overwritten.
    The worker touches the item's heartbeat before and after each phase."""
    g = _groups([item["group_id"]]) if item["group_id"] else []

    def finish(status: str, reason: str | None = None, merge_id: str | None = None, extra: dict[str, Any] | None = None) -> bool:
        detail = db.json_load(item["detail_json"], {})
        detail.update(extra or {})
        t = db.now()
        where = "status='running'" if status != "merged" else "(status='running' OR (status IN ('pending','skipped') AND merge_id IS NULL))"
        for attempt in range(4):      # a busy database must not lose a result
            try:
                with db.tx() as conn:
                    return conn.execute(f"UPDATE merge_batch_items SET status=?,reason=?,merge_id=?,detail_json=?,finished_at=?,updated_at=? WHERE id=? AND {where}",  # nosec B608
                                        (status, reason, merge_id, db.json_dump(detail), t, t, item["id"])).rowcount == 1
            except sqlite3.OperationalError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * (attempt + 1))
        return False
    _touch(item["id"])
    if not g:
        finish("skipped", "stale_group")
        return "skipped"
    ev = evaluate(g[0], item["canonical_work_id"] or None)
    if ev["status"] != "ready":     # the library moved on since the dry run
        finish("skipped", "; ".join(x["code"] for x in ev["reasons"]) or ev["status"], extra={"reasons": ev["reasons"]})
        return "skipped"
    _record_claim_sources(item["id"], ev["source_work_ids"])
    try:
        # No resolutions at all: a bulk run never carries a kids consent key, so it can never widen what a child sees.
        res = workmerge.execute(ev["canonical_work_id"], ev["source_work_ids"], {}, user, confirm_dissimilar=False)
    except workmerge.MergeBlocked as exc:
        finish("skipped", "; ".join(b["code"] for b in exc.blockers), extra={"reasons": [_reason(b["code"], b.get("message") or b["code"]) for b in exc.blockers]})
        return "skipped"
    except (workmerge.WorkNotFound, workmerge.BadRequest) as exc:
        finish("skipped", "stale_group", extra={"error": str(exc)})
        return "skipped"
    except Exception as exc:
        finish("failed", str(exc)[:300])
        return "failed"
    _touch(item["id"])
    by = user.get("id") if isinstance(user, dict) else user
    # Record the merge on the item FIRST: from here on batch undo can see it.
    won = finish("merged", None, res["merge_id"], {"ledger_rows": res["ledger_rows"], "moves": ev["moves"], "claim_sources": ev["source_work_ids"]})
    if won:      # (if recover_stale already recorded this very merge it also did the steps below)
        _after_merge(batch_id, item, res["merge_id"], ev["source_work_ids"], by, db.now(), canonical=ev["canonical_work_id"])
    return "merged"


def _after_merge(batch_id: str, item: dict[str, Any], merge_id: str, sources: list[str], by: Any, t: float, canonical: str | None = None) -> None:
    """What every recorded merge gets, whichever path recorded it: a history event, the group marked merged and the kept
    record's metadata refresh queued as 'held' (see ``enqueue_refresh``)."""
    canonical = canonical or item["canonical_work_id"]
    db.add_history(EVENT_BULK, f"Merged {len(sources)} duplicate record(s) in bulk batch", work_id=canonical,
                   data={"batch_id": batch_id, "merge_id": merge_id, "group_id": item["group_id"], "by": by, "source_work_ids": sources})
    if item["group_id"]:
        with db.tx() as conn:
            conn.execute("UPDATE duplicate_groups SET status='merged',updated_at=? WHERE id=?", (t, item["group_id"]))
    if db.json_load(db.one("SELECT options_json o FROM merge_batches WHERE id=?", (batch_id,))["o"], {}).get("refresh", True):
        enqueue_refresh(canonical, batch_id, hold=True)


def batch(batch_id: str, with_items: bool = True) -> dict[str, Any]:
    b = db.one("SELECT * FROM merge_batches WHERE id=?", (batch_id,))
    if not b:
        raise LookupError(batch_id)
    counts = {r["status"]: r["n"] for r in db.all("SELECT status,COUNT(*) n FROM merge_batch_items WHERE batch_id=? GROUP BY status", (batch_id,))}
    opts = db.json_load(b["options_json"], {})
    held = db.one("SELECT COUNT(*) n, MAX(queued_at) last FROM duplicate_refresh_queue WHERE batch_id=? AND status='held'", (batch_id,))
    out = {"id": b["id"], "status": b["status"], "created_by": b["created_by"], "created_at": b["created_at"], "finished_at": b["finished_at"], "total": b["total"],
           "options": opts, "counts": {k: counts.get(k, 0) for k in ("pending", "running", "merged", "skipped", "failed", "undone")},
           "progress": {"done": b["total"] - counts.get("pending", 0) - counts.get("running", 0), "total": b["total"]},
           "finalized_at": opts.get("finalized_at"), "finalized_by": opts.get("finalized_by"),
           "held_refreshes": held["n"], "grace_days": grace_days(),
           "refresh_held_until": (held["last"] + grace_days() * 86400) if held["n"] and held["last"] else None,
           "stale_claims": stale_claims(batch_id) if counts.get("running") else 0,
           "undo_stuck": b["status"] == "undoing" and _undo_is_stale(b),
           "can_undo": (b["status"] in UNDOABLE or (b["status"] == "undoing" and _undo_is_stale(b))) and counts.get("merged", 0) > 0 and not counts.get("running")}
    if with_items:
        out["items"] = [{"order": r["ord"], "group_id": r["group_id"], "title": r["title"], "status": r["status"], "reason": r["reason"], "merge_id": r["merge_id"],
                         "canonical_work_id": r["canonical_work_id"] or None, "source_work_ids": db.json_load(r["source_work_ids_json"], []),
                         "detail": db.json_load(r["detail_json"], {})}
                        for r in db.all("SELECT * FROM merge_batch_items WHERE batch_id=? ORDER BY ord", (batch_id,))]
    return out


def list_batches(limit: int = 20) -> list[dict[str, Any]]:
    """Newest first. Also gives back dead claims and closes finished batches (cheap and bounded: only 'running' batches),
    so a batch stuck on a worker that died shows its true state without anyone running a job."""
    recover_running()
    return [batch(r["id"], with_items=False) for r in db.all("SELECT id FROM merge_batches ORDER BY created_at DESC,id LIMIT ?", (max(1, min(limit, 100)),))]


def recover_running(limit: int = 100) -> int:
    """Recover dead claims of up to ``limit`` running batches (newest first) and close the ones with nothing left."""
    n = 0
    for r in db.all("SELECT id FROM merge_batches WHERE status='running' ORDER BY created_at DESC LIMIT ?", (limit,)):
        n += recover_stale(r["id"])
        _close(r["id"])
    return n


def _undo_is_stale(b: dict[str, Any]) -> bool:
    return (b.get("updated_at") or b.get("finished_at") or b["created_at"] or 0) < db.now() - STALE_UNDOING_SECONDS


def undo(batch_id: str, user: Any, force: bool = False) -> dict[str, Any]:
    """Reverse the batch's merges newest first, each through the ordinary ledger reversal. Stops at the first one that cannot be
    undone cleanly (rows changed since, a later merge built on it, files in the trash, a kid would see more) and says why;
    what was undone stays undone, and a second call carries on from there."""
    b = db.one("SELECT * FROM merge_batches WHERE id=?", (batch_id,))
    if not b:
        raise LookupError(batch_id)
    _close(batch_id)    # a 'running' batch with nothing left to merge is finished
    t = db.now()
    with db.tx() as conn:       # one atomic step: only a finished batch with no merge in flight can start undoing, one undo at a time;
        # an 'undoing' batch whose undo went quiet (heartbeat older than STALE_UNDOING_SECONDS) may be resumed
        took = conn.execute("UPDATE merge_batches SET status='undoing',updated_at=? WHERE id=? AND "
                            "(status IN ('done','stopped','partly_undone','undone') OR (status='undoing' AND COALESCE(updated_at,finished_at,created_at)<?)) "
                            "AND NOT EXISTS (SELECT 1 FROM merge_batch_items WHERE batch_id=? AND status='running')",
                            (t, batch_id, t - STALE_UNDOING_SECONDS, batch_id)).rowcount
    if not took:
        cur = _status(batch_id)
        if cur == "running":
            raise BatchBusy("This batch is still merging. Wait for it to finish before undoing it.")
        if cur == "undoing":
            raise BatchBusy("This batch is already being undone.")
        if db.one("SELECT 1 x FROM merge_batch_items WHERE batch_id=? AND status='running'", (batch_id,)):
            raise BatchBusy("Another merge of this batch is still in progress. Try again in a minute.")
        raise BatchBusy(f"This batch cannot be undone from its current state ({cur}).")
    undone, stopped = [], None
    items = db.all("SELECT * FROM merge_batch_items WHERE batch_id=? AND status='merged' ORDER BY ord DESC", (batch_id,))
    try:
        stopped, undone = _undo_items(items, user, force, batch_id)
    finally:
        left = db.one("SELECT COUNT(*) n FROM merge_batch_items WHERE batch_id=? AND status='merged'", (batch_id,))["n"]
        with db.tx() as conn:
            conn.execute("UPDATE merge_batches SET status=?,finished_at=COALESCE(finished_at,?) WHERE id=?", ("undone" if not left else "partly_undone", db.now(), batch_id))
    out = batch(batch_id)
    out.update(undone=len(undone), stopped=stopped)
    return out


def _undo_items(items: list[dict[str, Any]], user: Any, force: bool, batch_id: str | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    undone: list[str] = []
    stopped = None
    for it in items:
        if batch_id:
            with db.tx() as conn:
                conn.execute("UPDATE merge_batches SET updated_at=? WHERE id=?", (db.now(), batch_id))
        try:
            writelimit.check(user)
            workmerge.reverse(it["merge_id"], force=force, user=user)
        except writelimit.RateLimited as exc:
            stopped = {"group_id": it["group_id"], "merge_id": it["merge_id"], "code": "rate_limited", "message": str(exc), "retry_after": exc.retry_after}
            break
        except workmerge.ReversalRefused as exc:
            stopped = {"group_id": it["group_id"], "merge_id": it["merge_id"], "code": exc.code, "message": exc.message}
            break
        except workmerge.ReversalBlocked as exc:
            stopped = {"group_id": it["group_id"], "merge_id": it["merge_id"], "code": "conflicts", "message": str(exc), "conflicts": exc.conflicts[:20]}
            break
        except (workmerge.MergeNotFound, workmerge.ReversalFailed) as exc:
            stopped = {"group_id": it["group_id"], "merge_id": it["merge_id"], "code": "failed", "message": str(exc)}
            break
        with db.tx() as conn:
            conn.execute("UPDATE merge_batch_items SET status='undone',finished_at=? WHERE id=?", (db.now(), it["id"]))
            if batch_id:
                conn.execute("UPDATE merge_batches SET updated_at=? WHERE id=?", (db.now(), batch_id))      # undo heartbeat
            if it["group_id"]:
                conn.execute("UPDATE duplicate_groups SET status='open',updated_at=? WHERE id=?", (db.now(), it["group_id"]))
            if it["canonical_work_id"]:
                conn.execute("DELETE FROM duplicate_refresh_queue WHERE work_id=? AND status IN ('pending','held')", (it["canonical_work_id"],))
        undone.append(it["merge_id"])
    return stopped, undone


def resume_running(user_lookup: Callable[[str], Any] | None = None, limit: int = 50) -> dict[str, Any]:
    """Job entry: finish batches whose page was closed mid-way. The work is attributed to the admin who started the batch."""
    merged = 0
    dupefinder.prune_missing()      # housekeeping: reads (GET) never delete
    recover_running()
    batches = db.all("SELECT id,created_by FROM merge_batches WHERE status='running' AND EXISTS (SELECT 1 FROM merge_batch_items i WHERE i.batch_id=merge_batches.id AND i.status IN ('pending','running')) ORDER BY created_at")
    for b in batches:
        while merged < limit:
            had = db.one("SELECT COUNT(*) n FROM merge_batch_items WHERE batch_id=? AND status='merged'", (b["id"],))["n"]
            res = process(b["id"], {"id": b["created_by"]}, limit=max(1, min(MAX_PER_REQUEST, limit - merged)))
            merged += res["counts"]["merged"] - had
            if res.get("rate_limited") or res["status"] != "running" or not res["counts"]["pending"]:
                break
        if merged >= limit:
            break
    return {"batches": len(batches), "merged": merged}


# ---------------------------------------------------------------------------
# Metadata refresh of the surviving records (queued, rate limited)
# ---------------------------------------------------------------------------

def enqueue_refresh(work_id: str, batch_id: str | None = None, hold: bool = False) -> None:
    """Queue the surviving record for a metadata refresh so it gets the provider's data. A hand-edited record is left alone.
    ``hold=True`` (every bulk merge) parks the row as 'held': a refresh rewrites the very rows the merge ledger recorded
    (refresh_work -> link_work -> upsert_work), which would make the batch's one-click Undo refuse. A held row becomes
    pending when the admin finalizes the batch (``finalize``) or after the grace period (``release_held``)."""
    w = db.one("SELECT metadata_source FROM works WHERE id=?", (work_id,))
    if not w or w["metadata_source"] == "manual":
        return
    with db.tx() as conn:
        conn.execute("INSERT INTO duplicate_refresh_queue(work_id,batch_id,queued_at,status,attempts) VALUES (?,?,?,?,0) "
                     "ON CONFLICT(work_id) DO UPDATE SET batch_id=excluded.batch_id,queued_at=excluded.queued_at,status=excluded.status,attempts=0,last_error=NULL,not_before=NULL",
                     (work_id, batch_id, db.now(), "held" if hold else "pending"))


def grace_days() -> float:
    try:
        return max(0.0, float(db.get_setting(GRACE_DAYS_KEY, GRACE_DAYS_DEFAULT)))
    except (TypeError, ValueError):
        return GRACE_DAYS_DEFAULT


def release_held(batch_id: str | None = None, now: float | None = None) -> int:
    """Make held refreshes pending. With ``batch_id`` ONLY that batch's rows are released (the admin finalizing it). Without,
    only batches whose grace period (setting ``duplicates.refresh_grace_days``, default 7) is over, counted from the batch's
    newest held row: each such batch is released as a whole, marked finalized (``finalized_at``, ``finalized_by: grace``)
    and gets a history event, so nothing is released silently. After that the batch's one-click Undo may stop at the first
    refreshed item. This runs when the duplicate-refresh queue runs; the job is manual unless the admin schedules it."""
    if batch_id:
        with db.tx() as conn:
            return conn.execute("UPDATE duplicate_refresh_queue SET status='pending' WHERE batch_id=? AND status='held'", (batch_id,)).rowcount
    cutoff = (now or db.now()) - grace_days() * 86400
    total = 0
    for r in db.all("SELECT batch_id,MAX(queued_at) last FROM duplicate_refresh_queue WHERE status='held' GROUP BY batch_id"):
        if r["last"] > cutoff:
            continue
        with db.tx() as conn:
            if r["batch_id"]:       # a batch that is still merging or being undone is never released behind its back
                st = conn.execute("SELECT status FROM merge_batches WHERE id=?", (r["batch_id"],)).fetchone()
                if st and st["status"] in ("running", "undoing"):
                    continue
            n = conn.execute("UPDATE duplicate_refresh_queue SET status='pending' WHERE status='held' AND batch_id IS ? AND queued_at<=?", (r["batch_id"], cutoff)).rowcount
            if n and r["batch_id"]:
                row = conn.execute("SELECT options_json FROM merge_batches WHERE id=?", (r["batch_id"],)).fetchone()
                if row:
                    opts = db.json_load(row["options_json"], {})
                    if not opts.get("finalized_at"):
                        opts.update(finalized_at=now or db.now(), finalized_by="grace")
                        conn.execute("UPDATE merge_batches SET options_json=? WHERE id=?", (db.json_dump(opts), r["batch_id"]))
                        conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                                     (db.uid(), None, None, EVENT_FINALIZED, f"Duplicate batch finished automatically after {grace_days():g} day(s); metadata refresh queued",
                                      db.json_dump({"batch_id": r["batch_id"], "released": n, "by": "grace"}), db.now()))
        total += n
    return total


def finalize(batch_id: str) -> dict[str, Any]:
    """"Finish batch / refresh metadata": the admin accepts the batch. Its kept books are queued for the metadata refresh
    and the batch is marked finalized. From here on the refresh may rewrite rows the merge ledger recorded, so undoing the
    batch is no longer guaranteed to be one click (an undo that hits a changed row stops and says which). Only THIS batch's
    held rows are released. The state check and the change happen in one transaction (a conditional update), so a batch that
    starts merging or undoing meanwhile is never finalized."""
    if not db.one("SELECT 1 x FROM merge_batches WHERE id=?", (batch_id,)):
        raise LookupError(batch_id)
    with db.tx() as conn:
        row = conn.execute("SELECT options_json FROM merge_batches WHERE id=?", (batch_id,)).fetchone()
        opts = db.json_load(row["options_json"], {})
        opts.setdefault("finalized_at", db.now())
        opts.setdefault("finalized_by", "admin")
        took = conn.execute("UPDATE merge_batches SET options_json=? WHERE id=? AND status NOT IN ('running','undoing') "
                            "AND NOT EXISTS (SELECT 1 FROM merge_batch_items WHERE batch_id=? AND status='running')",
                            (db.json_dump(opts), batch_id, batch_id)).rowcount
        if not took:
            raise BatchBusy("This batch is still merging or being undone; finish it first.")
        released = conn.execute("UPDATE duplicate_refresh_queue SET status='pending' WHERE batch_id=? AND status='held'", (batch_id,)).rowcount
    out = batch(batch_id)
    out["queued_refreshes"] = released
    return out


def refresh_queue(limit: int = REFRESH_BATCH, delay: float = 1.0, sleep: Callable[[float], None] = time.sleep,
                  refresh: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Drain the queue: at most ``limit`` provider calls per run, ``delay`` seconds apart, and a rate-limited or transient
    answer puts the item back with the provider's retry time and ends the run (providers throttle; we respect it).
    Held rows (merged batches still undoable) are not touched until finalized or past the grace period."""
    from . import metadata
    refresh = refresh or metadata.refresh_work
    release_held()      # batches past their grace period are finished here (marked finalized, with an event)
    rows = db.all("SELECT * FROM duplicate_refresh_queue WHERE status='pending' AND (not_before IS NULL OR not_before<=?) ORDER BY queued_at LIMIT ?", (db.now(), max(1, min(int(limit), 100))))
    out = {"refreshed": 0, "retry_later": 0, "failed": 0, "stopped": None}
    for i, r in enumerate(rows):
        if i:
            sleep(delay)
        try:
            res = refresh(r["work_id"])
        except Exception as exc:
            attempts = r["attempts"] + 1
            with db.tx() as conn:
                conn.execute("UPDATE duplicate_refresh_queue SET attempts=?,last_error=?,status=?,done_at=? WHERE work_id=?",
                             (attempts, str(exc)[:300], "failed" if attempts >= REFRESH_ATTEMPTS else "pending", db.now() if attempts >= REFRESH_ATTEMPTS else None, r["work_id"]))
            out["failed"] += 1
            continue
        status = res.get("status")
        if status in ("rate_limited", "transient", "deferred"):
            with db.tx() as conn:
                conn.execute("UPDATE duplicate_refresh_queue SET not_before=? WHERE work_id=?", (res.get("retry_at") or db.now() + 300, r["work_id"]))
            out["retry_later"] += 1
            out["stopped"] = status
            break
        with db.tx() as conn:
            conn.execute("UPDATE duplicate_refresh_queue SET status='done',done_at=?,last_error=NULL WHERE work_id=?", (db.now(), r["work_id"]))
        out["refreshed"] += 1
    out["pending"] = db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='pending'")["n"]
    out["held"] = db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='held'")["n"]
    return out


def refresh_job(job: dict[str, Any] | None = None) -> dict[str, Any]:
    config = db.json_load((job or {}).get("config_json"), {}) or {}
    return refresh_queue(limit=int(config.get("batch") or REFRESH_BATCH), delay=float(config.get("delay") or 1.0))
