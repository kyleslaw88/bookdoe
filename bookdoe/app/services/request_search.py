"""Search on request: when a request is approved, look for just that book once.

Approving a request records acquisition intent on the work's assets (family._apply_approval),
but the scheduled wanted-search job is deliberately a household decision and may be off. This
module gives an approved request its own single search per requested kind, off the request
thread, without turning that job on. It reuses the job's pieces: the eligibility rule
(wanted_search.ELIGIBLE), the per-asset state and cooldown (wanted_search_state / _record),
the daily cap (searches_last_24h) and the shared search_and_grab (which refuses a release that
does not name the wanted book). A failure here never reaches the request: it is logged and
written to history.

Kids: only called after an approval (family._apply_approval); a pending request searches nothing.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .. import db
from . import wanted_search
from . import acquisition

log = logging.getLogger(__name__)

EVENT = "request.search"
STATES = ("grabbed", "no_match", "no_result", "error", "skipped_cooldown", "skipped_cap", "skipped_active", "busy", "throttled")
RECORDED = ("grabbed", "no_match", "no_result", "error")   # outcomes that update the asset's search state

_pool_obj: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_inflight_requests: set[str] = set()
_inflight_assets: set[str] = set()
_pace_lock = threading.Lock()
_last_search_at: float | None = None


def _pool() -> ThreadPoolExecutor:
    global _pool_obj
    with _lock:
        if _pool_obj is None:
            _pool_obj = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alx-reqsearch")
        return _pool_obj


def shutdown_pool(wait: bool = True) -> None:
    global _pool_obj
    with _lock:
        pool, _pool_obj = _pool_obj, None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=True)


def _pace(delay: float) -> None:
    """Space request searches by the job's delay setting (one shared clock for both workers)."""
    global _last_search_at
    with _pace_lock:
        if _last_search_at is not None and delay > 0:
            wait = _last_search_at + delay - wanted_search.monotonic()
            if wait > 0:
                wanted_search.sleep(wait)
        _last_search_at = wanted_search.monotonic()


def _enqueue(fn: Callable[..., None], *args: Any) -> None:
    """Hook the tests replace (inline or recording); production runs it on the pool."""
    _pool().submit(fn, *args)


def trigger(req_id: str) -> None:
    """Start the one background search for an approved request. Never raises."""
    if not acquisition.acquisition_enabled():
        return   # acquisition off: an approved request waits, nothing searches
    try:
        from . import family
        if not family.search_on_approval():
            return
        with _lock:
            if req_id in _inflight_requests:
                return
            _inflight_requests.add(req_id)
        try:
            _enqueue(_run_safely, req_id)
        except Exception:
            with _lock:
                _inflight_requests.discard(req_id)
            raise
    except Exception:
        log.exception("search on request: could not start for %s", req_id)


def _run_safely(req_id: str) -> None:
    try:
        run(req_id)
    except Exception:
        log.exception("search on request failed for %s", req_id)
    finally:
        with _lock:
            _inflight_requests.discard(req_id)


def _assets_for(work_id: str, kinds: list[str]) -> list[tuple[str, dict[str, Any] | None]]:
    out = []
    for kind in kinds:
        a = db.one(
            f"""SELECT a.* FROM media_assets a JOIN works w ON w.id=a.work_id
                WHERE a.work_id=? AND a.kind=? AND a.derived_from_asset_id IS NULL AND {wanted_search.ELIGIBLE}
                ORDER BY (a.format IS NOT NULL), a.added_at LIMIT 1""", (work_id, kind))  # nosec B608
        out.append((kind, a))
    return out


def run(req_id: str) -> list[dict[str, Any]]:
    """Synchronous body: one search per requested kind that is still wanted. Returns the
    outcomes (also written to history, one event each)."""
    if not acquisition.acquisition_enabled():
        return []
    req = db.one("SELECT * FROM requests WHERE id=?", (req_id,))
    if not req or req["status"] != "approved" or not req.get("work_id"):
        return []
    work = db.one("SELECT title FROM works WHERE id=?", (req["work_id"],))
    title = work["title"] if work else req["title"]
    cfg = wanted_search.get_settings()
    outcomes = []
    for kind, asset in _assets_for(req["work_id"], db.json_load(req["kinds_json"], ["ebook"])):
        if not asset:
            continue   # owned, not monitored or already downloading: nothing to search for
        detail: dict[str, Any] = {}
        errs: list[dict[str, Any]] = []
        with _lock:
            busy = asset["id"] in _inflight_assets
            if not busy:
                _inflight_assets.add(asset["id"])
        try:
            state = db.one("SELECT next_eligible_at FROM wanted_search_state WHERE asset_id=?", (asset["id"],))
            if busy:
                outcome = "busy"
            elif wanted_search.throttle_remaining() > 0:
                outcome = "throttled"   # the indexers pushed back a moment ago: no new searches yet
            elif state and state["next_eligible_at"] and state["next_eligible_at"] > db.now():
                outcome = "skipped_cooldown"
            else:
                _pace(cfg["wanted_search_delay_seconds"])
                outcome, errs = wanted_search.search_and_grab(asset, cfg["auto_grab_min_score"], detail)
                if outcome == "error" and wanted_search._is_throttle(errs):
                    wanted_search.note_throttle()
                    outcome = "throttled"
                if outcome in RECORDED:
                    try:
                        wanted_search._record(asset["id"], outcome, cfg["wanted_search_cooldown_hours"])
                    except Exception:
                        log.exception("search on request: could not record state for %s", asset["id"])
        finally:
            if not busy:
                with _lock:
                    _inflight_assets.discard(asset["id"])
        data = {"request_id": req_id, "kind": kind, "outcome": outcome, "near_misses": detail.get("near_misses"),
                "release": detail.get("release"), "errors": [str(e.get("error", ""))[:200] for e in errs[:3]]}
        try:
            db.add_history(EVENT, f"Search on request for '{title}' ({kind}): {outcome}", work_id=req["work_id"], asset_id=asset["id"], data=data)
        except Exception:
            log.exception("search on request: could not write history")
        outcomes.append({"kind": kind, "asset_id": asset["id"], "outcome": outcome})
    return outcomes


def status_for(req: dict[str, Any]) -> dict[str, Any] | None:
    """`{state, at}` for a request payload: 'searching' while a search runs, else the latest
    outcome (a grab on any kind wins). None when no search was made for the request."""
    if req.get("status") != "approved":
        return None
    if req["id"] in _inflight_requests:
        return {"state": "searching", "at": db.now()}
    if not req.get("work_id"):
        return None
    rows = db.all("SELECT data_json,created_at FROM history WHERE work_id=? AND event_type=? ORDER BY created_at DESC LIMIT 20", (req["work_id"], EVENT))
    mine = [(r["created_at"], db.json_load(r["data_json"], {})) for r in rows]
    mine = [(at, d) for at, d in mine if d.get("request_id") == req["id"]]
    if not mine:
        return None
    for at, d in mine:
        if d.get("outcome") == "grabbed":
            return {"state": "grabbed", "at": at}
    return {"state": mine[0][1].get("outcome"), "at": mine[0][0]}
