from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .. import db
from ..config import settings
from . import acquisition, downloads, releases, metadata, monitoring, backup, covers, repair, localmeta, tagging, scanner, maintenance, quality, upgrades, audience, wanted_search, conversion, filetrash, workmerge, dupefinder, bulkmerge, discover_chat, podcastscan

log = logging.getLogger(__name__)
_stop = asyncio.Event()

# How jobs run (fixed 2026-10-11 after /readyz went 503 live). The loop below only
# TICKS: it writes a heartbeat and hands due jobs to a thread pool. It never waits
# for a job, so a tag-classify batch talking to a slow local model for minutes cannot
# stall download polls, imports or the heartbeat. One run per job kind at a time: a
# kind whose previous run is still active is skipped this tick (not queued). A run
# that outlives its lease is treated as dead so the kind can run again.
HEARTBEAT_KEY = "scheduler.heartbeat"
TICK_SECONDS = 15
_POOL_WORKERS = 4
_executor: ThreadPoolExecutor | None = None
_active_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}   # kind -> {run_id, job_id, started_at, future, trigger}


class JobBusy(RuntimeError):
    """A run of this job kind is still active; the new one was skipped, not queued."""


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_POOL_WORKERS, thread_name_prefix="alx-job")
    return _executor


async def run_scheduler() -> None:
    _stop.clear()
    log.info("scheduler started")
    while not _stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("scheduler iteration failed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("scheduler stopped")


def stop_scheduler() -> None:
    _stop.set()


def shutdown_pool(wait: bool = True) -> None:
    """Stop the worker threads (app shutdown; test teardown). Running jobs finish when
    wait=True; the next job submission creates a fresh pool."""
    global _executor
    pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=wait)
    with _active_lock:
        _active.clear()


def tick() -> list[dict[str, Any]]:
    """One loop iteration: heartbeat first (so a busy pool never looks like a dead
    loop), then dispatch what is due. Returns at once."""
    beat()
    try:
        reap_orphan_runs()
    except Exception:
        log.exception("reaping orphaned job runs failed")
    return run_due_jobs(block=False)


def beat() -> float:
    t = db.now()
    db.set_setting(HEARTBEAT_KEY, t)
    return t


def heartbeat() -> float | None:
    """When the loop last ticked (None before the first tick / in older databases)."""
    value = db.get_setting(HEARTBEAT_KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def running_jobs() -> list[dict[str, Any]]:
    """Runs active in this process, oldest first, with their age in seconds."""
    now = db.now()
    with _active_lock:
        items = [{"kind": k, "run_id": v["run_id"], "job_id": v["job_id"], "trigger": v["trigger"],
                  "started_at": v["started_at"], "age_seconds": round(max(0.0, now - v["started_at"]))} for k, v in _active.items()]
    return sorted(items, key=lambda x: x["started_at"])


def touch_lease(kind: str) -> None:
    """Renew the lease of a run that is making progress (a long conversion): the lease exists to
    free a kind whose run is stuck, not one that is busy. A no-op when the kind is not active."""
    with _active_lock:
        entry = _active.get(kind)
        if entry is not None:
            entry["started_at"] = db.now()


def _release_expired(kind: str, now: float) -> None:
    """A run past its lease is presumed stuck (a hung socket, a crashed worker whose
    cleanup never ran): drop it from the active set and fail its row so the kind runs
    again. If the old thread ever finishes it finds its row already closed."""
    entry = _active.get(kind)
    if entry and now - entry["started_at"] > settings.job_lease_seconds:
        del _active[kind]
        reason = f"Lease expired after {int(now - entry['started_at'])} s; released so the job can run again"
        with db.tx() as conn:
            conn.execute("UPDATE job_runs SET status='failed',finished_at=?,error=? WHERE id=? AND status='running'", (now, reason, entry["run_id"]))
            conn.execute("UPDATE jobs SET last_run_at=?,last_status='failed',last_duration=?,last_error=? WHERE id=?",
                         (now, now - entry["started_at"], reason, entry["job_id"]))
        log.error("job %s: %s", kind, reason)


def run_due_jobs(block: bool = True) -> list[dict[str, Any]]:
    """Dispatch every due job to the pool (one per kind). With block=True (tests,
    manual callers) wait for them and return their results; the loop passes False."""
    # Manual-only jobs (no schedule_seconds) are never due: their next_run_at stays NULL.
    due = db.all("SELECT * FROM jobs WHERE enabled=1 AND schedule_seconds IS NOT NULL AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY next_run_at LIMIT 10", (db.now(),))
    if not acquisition.acquisition_enabled():
        # Acquisition off: the wanted-search, download-poll, upgrade and author-discovery jobs are never dispatched.
        due = [j for j in due if j["kind"] not in acquisition.ACQUISITION_JOB_KINDS]
    out = []
    for j in due:
        try:
            out.append(submit_job(j["id"], trigger="schedule"))
        except JobBusy as exc:
            log.info("scheduler: %s", exc)
            out.append({"job_id": j["id"], "kind": j["kind"], "status": "skipped", "error": str(exc)})
    if block:
        for item in out:
            fut = item.pop("future", None)
            if fut is not None:
                item.update(fut.result())
    return out


def submit_job(job_id: str, trigger: str = "schedule") -> dict[str, Any]:
    """Claim the kind, record the run as 'running', hand the work to the pool.
    Raises JobBusy when a run of that kind is still active (within its lease)."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise ValueError("Job not found")
    if job["kind"] in acquisition.ACQUISITION_JOB_KINDS:
        acquisition.ensure_enabled()   # a manual or direct run is refused too, not only the schedule
    started = db.now()
    with _active_lock:
        _release_expired(job["kind"], started)
        if job["kind"] in _active:
            entry = _active[job["kind"]]
            raise JobBusy(f"{job['kind']} is still running (started {int(started - entry['started_at'])} s ago); skipped")
        run_id, execution_id, attempt, retry_of = _begin_run(job, trigger, started)
        entry = {"run_id": run_id, "job_id": job_id, "started_at": started, "trigger": trigger, "future": None}
        _active[job["kind"]] = entry
    fut = _pool().submit(_finish_run, job, run_id, execution_id, trigger, attempt, started)
    entry["future"] = fut
    return {"run_id": run_id, "execution_id": execution_id, "trigger": trigger, "attempt": attempt, "job_id": job_id, "kind": job["kind"], "status": "running", "future": fut}


def _begin_run(job: dict[str, Any], trigger: str, started: float) -> tuple[str, str, int, str | None]:
    run_id, execution_id = db.uid(), db.uid()
    with db.tx() as conn:
        # Retry state: a run that follows a failed run of the same job is its retry.
        prev = conn.execute("SELECT id,status,attempt FROM job_runs WHERE job_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (job["id"],)).fetchone()
        retry_of, attempt = (prev["id"], prev["attempt"] + 1) if prev and prev["status"] == "failed" else (None, 1)
        conn.execute(
            "INSERT INTO job_runs(id,job_id,kind,status,started_at,execution_id,trigger_source,attempt,retry_of) VALUES (?,?,?,'running',?,?,?,?,?)",
            (run_id, job["id"], job["kind"], started, execution_id, trigger, attempt, retry_of),
        )
        # Push next run now to avoid overlapping duplicate execution.
        if job.get("schedule_seconds"):
            conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?", (started + int(job["schedule_seconds"]), job["id"]))
    return run_id, execution_id, attempt, retry_of


def _finish_run(job: dict[str, Any], run_id: str, execution_id: str, trigger: str, attempt: int, started: float) -> dict[str, Any]:
    """Execute and record. Always releases the kind, even when the handler raises."""
    status, result, error = "success", {}, None
    try:
        try:
            result = _execute(job)
        except Exception as exc:
            status, error = "failed", str(exc)
            log.exception("job %s failed", job["name"])
        finished = db.now()
        noop = status == "success" and _is_noop_poll(job["kind"], result)
        def close_out(conn):
            # The row may already be closed by the lease reaper: never resurrect it.
            still_running = conn.execute("SELECT 1 FROM job_runs WHERE id=? AND status='running'", (run_id,)).fetchone()
            if noop and still_running:
                # download-poll fires every 30 s and is idle almost always (20k rows a week).
                # The running row above was the overlap guard; an all-zero poll leaves no
                # trace. Errors, any activity or a failure still record a full run.
                conn.execute("DELETE FROM job_runs WHERE id=?", (run_id,))
            elif still_running:
                conn.execute(
                    "UPDATE job_runs SET status=?,finished_at=?,error=?,result_json=?,entities_json=? WHERE id=?",
                    (status, finished, error, db.json_dump(result), db.json_dump(_affected_entities(result)), run_id),
                )
            conn.execute("UPDATE jobs SET last_run_at=?,last_status=?,last_duration=?,last_error=? WHERE id=?", (finished, status, finished - started, error, job["id"]))
        try:
            db.tx_retry(close_out)
        except Exception:
            # Left 'running' here, reap_orphan_runs() closes it on a later tick.
            log.exception("job %s: could not record the end of run %s", job["name"], run_id)
    finally:
        with _active_lock:
            if _active.get(job["kind"], {}).get("run_id") == run_id:
                del _active[job["kind"]]
    return {"run_id": run_id, "execution_id": execution_id, "trigger": trigger, "attempt": attempt, "job_id": job["id"], "status": status, "result": result, "error": error}


def run_job(job_id: str, trigger: str = "manual") -> dict[str, Any]:
    """Run one job now and wait for it (manual runs from Management, tests). Shares
    the per-kind single flight with the scheduler: raises JobBusy while that kind is
    already running."""
    item = submit_job(job_id, trigger=trigger)
    return item["future"].result()


def run_exclusive(kind: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run `fn` under the single-flight lease of `kind` without a jobs row (used by the
    Management classify scan so it cannot overlap the scheduled tag-classify batch).
    Raises JobBusy immediately when that kind is active."""
    started = db.now()
    with _active_lock:
        _release_expired(kind, started)
        if kind in _active:
            raise JobBusy(f"{kind} is already running; try again when it finishes")
        token = db.uid()
        _active[kind] = {"run_id": token, "job_id": None, "started_at": started, "trigger": "manual", "future": None}
    try:
        return fn()
    finally:
        with _active_lock:
            if _active.get(kind, {}).get("run_id") == token:
                del _active[kind]


STALE_RUN_REASON = "Interrupted: the app stopped while this run was in progress (reaped at startup)"


def reap_stale_runs() -> int:
    """Mark every run still 'running' as failed. Called once at startup, before the
    scheduler starts: this process is the only runner, so nothing running can be live."""
    finished = db.now()
    with db.tx() as conn:
        stale = conn.execute("SELECT id,job_id,started_at FROM job_runs WHERE status='running'").fetchall()
        for r in stale:
            conn.execute("UPDATE job_runs SET status='failed',finished_at=?,error=? WHERE id=?", (finished, STALE_RUN_REASON, r["id"]))
            conn.execute(
                "UPDATE jobs SET last_run_at=?,last_status='failed',last_duration=?,last_error=? WHERE id=?",
                (finished, finished - r["started_at"], STALE_RUN_REASON, r["job_id"]),
            )
    if stale:
        log.warning("reaped %d stale job run(s) left running by a previous process", len(stale))
    return len(stale)


ORPHAN_RUN_REASON = "Interrupted: the run ended without recording its result (reaped by the scheduler)"


def reap_orphan_runs() -> int:
    """Fail every 'running' row that no live worker in this process owns, e.g. when the
    write that closes a run could not get the database lock. Holding _active_lock keeps
    a run that is just being submitted (row inserted under the same lock) out of reach."""
    with _active_lock:
        live = {v["run_id"] for v in _active.values()}
        finished = db.now()
        with db.tx() as conn:
            stale = [r for r in conn.execute("SELECT id,job_id,started_at FROM job_runs WHERE status='running'").fetchall() if r["id"] not in live]
            for r in stale:
                conn.execute("UPDATE job_runs SET status='failed',finished_at=?,error=? WHERE id=?", (finished, ORPHAN_RUN_REASON, r["id"]))
                conn.execute("UPDATE jobs SET last_run_at=?,last_status='failed',last_duration=?,last_error=? WHERE id=?",
                             (finished, finished - r["started_at"], ORPHAN_RUN_REASON, r["job_id"]))
    if stale:
        log.warning("reaped %d orphaned job run(s)", len(stale))
    return len(stale)


def _affected_entities(result: dict[str, Any]) -> dict[str, list[str]]:
    """Entity ids a run touched: any '<entity>_ids' list in the result, plus the
    work/asset ids of per-entity errors."""
    out: dict[str, list[str]] = {}
    for key, value in result.items():
        if key.endswith("_ids") and isinstance(value, list):
            out[key] = [str(v) for v in value]
    for err in result.get("errors") or []:
        if isinstance(err, dict):
            for field in ("work_id", "asset_id"):
                if err.get(field):
                    out.setdefault(f"failed_{field}s", []).append(str(err[field]))
    return out



def _is_noop_poll(kind: str, result: dict[str, Any]) -> bool:
    # The conversion jobs wake every minute and find an empty queue almost always.
    if kind not in ("download-poll", "convert-audio", "convert-ebook"):
        return False
    return not result.get("errors") and all(v in (0, None, [], False) for k, v in result.items() if k != "errors")


def _execute(job: dict[str, Any]) -> dict[str, Any]:
    kind = job["kind"]
    if kind == "download-poll":
        return downloads.poll_all()
    if kind == "wanted-search":
        return _wanted_search(job)
    if kind == "upgrade-search":
        return upgrades.search_job()
    if kind == "metadata-refresh":
        return _metadata_refresh(job)
    if kind == "author-discovery":
        return monitoring.refresh_all_authors()
    if kind == "database-backup":
        result = backup.create_backup()
        # OpenReads holds field locks and manual mappings that cannot be rebuilt. Its
        # failure is reported in the result but must not fail the main backup.
        try:
            opened = backup.create_openreads_backup()
        except Exception as exc:
            log.exception("openreads backup failed")
            opened = {"error": str(exc)}
        if opened is not None:
            result["openreads"] = opened
        return result
    if kind == "library-health":
        return _library_health()
    if kind == "housekeeping":
        return _housekeeping()
    if kind == "cover-fetch":
        return _cover_fetch(job)
    if kind == "tag-classify":
        return tagging.classify_kids_missing()
    if kind == "audience-backfill":
        return audience.backfill_job(job)
    if kind == "maintenance-scan":
        return maintenance.scan_job()
    if kind == "trash-purge":
        return filetrash.purge_job()
    if kind == "duplicate-scan":
        return dupefinder.scan_job(job)
    if kind == "duplicate-harvest":
        return dupefinder.harvest_job(job)
    if kind == "duplicate-batch":
        return bulkmerge.resume_running()
    if kind == "duplicate-refresh":
        return bulkmerge.refresh_job(job)
    if kind == "podcast-scan":
        return podcastscan.scan_job()
    if kind == "convert-audio":
        return conversion.run_queue("audio", job)
    if kind == "convert-ebook":
        return conversion.run_queue("ebook", job)
    if kind == "managed-library-build":
        # Manual-only: never due on a schedule; started by POST /api/v1/managed-build/plans/<id>/start (or Resume).
        from . import managedbuild
        return managedbuild.job_body()
    return {"skipped": True, "reason": f"No handler for {kind}"}


DEFAULT_AUTO_GRAB_MIN_SCORE = wanted_search.DEFAULTS["auto_grab_min_score"]
auto_grab_min_score = wanted_search.auto_grab_min_score


def _wanted_search(job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drain the wanted backlog in batches within a time budget (see services/wanted_search).
    Progress is written onto the running job_runs row so Management shows it live."""
    with _active_lock:
        run_id = _active.get("wanted-search", {}).get("run_id")

    def progress(partial: dict[str, Any]) -> None:
        if run_id:
            with db.tx() as conn:
                conn.execute("UPDATE job_runs SET result_json=? WHERE id=? AND status='running'", (db.json_dump(partial), run_id))

    return wanted_search.run(job, on_progress=progress)


def _metadata_refresh(job: dict[str, Any] | None = None) -> dict[str, Any]:
    # metadata_source='manual' means an operator corrected this by hand; an automatic
    # background resync must never silently overwrite that (upsert_work replaces
    # title/subtitle unconditionally on every sync, not just the still-empty fields).
    rows = db.all("SELECT id,metadata_id FROM works WHERE metadata_id IS NOT NULL AND metadata_source IS NOT 'manual' ORDER BY updated_at ASC LIMIT 50")
    ok, errors = 0, []
    for w in rows:
        try:
            metadata.sync_work(w["metadata_id"])
            ok += 1
        except Exception as exc:
            errors.append({"work_id": w["id"], "error": str(exc)})

    # Works imported from a Readarr database carry no metadata_id at all, so a job
    # that only re-synced already-linked works was a no-op on the entire migrated
    # library and left every one of those works permanently "unresolved".
    # `batch` (job config_json, like cover-fetch) is how many of those a run tries; each is
    # one OpenReads resolve call, so it is the knob that sets provider traffic.  Bounded so a
    # typo cannot turn one run into a library-wide crawl.
    config = db.json_load((job or {}).get("config_json"), {}) or {}
    try:
        batch = max(1, min(int(config.get("batch") or 25), 1000))
    except (TypeError, ValueError):
        batch = 25
    unlinked = db.all(
        """SELECT w.id FROM works w
           LEFT JOIN review_tasks r ON r.entity_type='work' AND r.entity_id=w.id AND r.kind='metadata_match' AND r.status='open'
           WHERE w.metadata_id IS NULL AND r.id IS NULL AND w.metadata_source IS NOT 'manual'
           ORDER BY w.updated_at ASC LIMIT ?""",
        (batch,),
    )
    linked = review = unmatched = retrying = 0
    for w in unlinked:
        try:
            status = metadata.refresh_work(w["id"]).get("status")
        except Exception as exc:
            errors.append({"work_id": w["id"], "error": str(exc)})
            continue
        if status == "linked":
            linked += 1
        elif status == "review":
            review += 1
        elif status in ("rate_limited", "transient", "deferred"):
            # Not "unmatched": the provider did not answer, and a retry is scheduled.
            retrying += 1
        else:
            unmatched += 1
    # Cheap, purely local, and the only source of truth for anything a provider has
    # never heard of.
    local = localmeta.harvest_missing()
    return {"refreshed": ok, "newly_linked": linked, "needs_review": review,
            "unmatched": unmatched, "retrying": retrying, "local": local, "errors": errors}


def _cover_fetch(job: dict[str, Any]) -> dict[str, Any]:
    from ..config import settings

    config = db.json_load(job.get("config_json"), {}) or {}
    batch = int(config.get("batch") or covers.DEFAULT_BATCH)
    allow_remote = bool(config.get("allow_remote", settings.cover_remote_fetch))
    return covers.backfill(limit=batch, allow_remote=allow_remote)


def _library_health() -> dict[str, Any]:
    rows = db.all("SELECT id,path,missing FROM media_files")
    missing, restored = 0, 0
    from pathlib import Path
    with db.tx() as conn:
        for f in rows:
            exists = Path(f["path"]).exists()
            if not exists and not f["missing"]:
                conn.execute("UPDATE media_files SET missing=1 WHERE id=?", (f["id"],))
                missing += 1
            elif exists and f["missing"]:
                conn.execute("UPDATE media_files SET missing=0,verified_at=? WHERE id=?", (db.now(), f["id"]))
                restored += 1
    # The reverse direction matters too: a file can exist in a managed root with no
    # row pointing at it, which shows the work as owned but file-less.
    reconciled = repair.reconcile_managed_files()
    asset_status = repair.sync_asset_status()
    # Bounded per run so production's pre-existing audio files fill in gradually.
    try:
        stream_data = scanner.backfill_stream_data()
    except Exception:
        log.warning("stream-data backfill failed", exc_info=True)
        stream_data = {"error": True}
    try:
        evaluation = quality.evaluate_all()
    except Exception:
        log.warning("quality evaluation failed", exc_info=True)
        evaluation = {"error": True}
    return {"new_missing": missing, "restored": restored, "reconciled": reconciled, "asset_status": asset_status, "stream_data": stream_data, "quality": evaluation}


JOB_RUN_RETENTION_DAYS = 30
JOB_RUN_KEEP_PER_JOB = 50
HISTORY_RETENTION_DAYS = 90
_PRUNE_BATCH = 5000


def _prune_in_batches(sql: str, params: tuple) -> int:
    """Run a `DELETE ... WHERE id IN (SELECT ... LIMIT ?)` until it deletes nothing.
    One transaction per batch so the process-wide write lock is never held for the
    whole of a first prune of tens of thousands of rows."""
    total = 0
    while True:
        with db.tx() as conn:
            n = conn.execute(sql, params + (_PRUNE_BATCH,)).rowcount
        total += n
        if n < _PRUNE_BATCH:
            return total


def prune_job_runs(now: float | None = None) -> int:
    cutoff = (now or db.now()) - JOB_RUN_RETENTION_DAYS * 86400
    return _prune_in_batches(
        """DELETE FROM job_runs WHERE id IN (
             SELECT id FROM (
               SELECT id, status, started_at,
                      ROW_NUMBER() OVER (PARTITION BY COALESCE(job_id, kind) ORDER BY started_at DESC, rowid DESC) AS rn
               FROM job_runs)
             WHERE started_at<? AND rn>? AND status!='running' LIMIT ?)""",
        (cutoff, JOB_RUN_KEEP_PER_JOB),
    )


def prune_history(now: float | None = None) -> int:
    cutoff = (now or db.now()) - HISTORY_RETENTION_DAYS * 86400
    # auth.* rows (key created/revoked, account linked) are the security audit trail.
    return _prune_in_batches(
        "DELETE FROM history WHERE id IN (SELECT id FROM history WHERE created_at<? AND event_type NOT LIKE 'auth.%' LIMIT ?)",
        (cutoff,),
    )


def _housekeeping() -> dict[str, Any]:
    cutoff = db.now() - 30 * 86400
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM releases WHERE created_at<? AND search_id IN (SELECT id FROM release_searches WHERE automatic=1)", (cutoff,))
        removed = cur.rowcount
        conn.execute("DELETE FROM blocklist WHERE expires_at IS NOT NULL AND expires_at<?", (db.now(),))
    from .. import auth
    return {"old_releases_removed": removed, "job_runs_pruned": prune_job_runs(), "history_pruned": prune_history(),
            "idle_trusted_devices_revoked": auth.revoke_idle_trusted_devices(),
            "work_merges_pruned": workmerge.prune_ledger()["merges_pruned"],
            "bulk_merge": bulkmerge.housekeeping(), **discover_chat.prune()}
