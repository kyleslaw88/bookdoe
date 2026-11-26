"""Audience metadata for the kids classifier: store, count and backfill.

Providers (Open Library subjects, Google Books categories and maturityRating) are asked by
OpenReads, never by the classifier. This module keeps what they said in two forms:
the evidence trail in `metadata_observations` (provider as source, raw value, fetched time)
and a normalized, queryable row per value in `work_audience_signals` (migration step 31),
which `services/kidsgate.gather_evidence` reads. What counts as juvenile / adult / mature
is decided in kidsgate (one table); `norm` here is that decision at store time.

`backfill` is the background job (`audience-backfill`): resumable (a work with a
`work_audience_status` row is never asked again unless a provider was unavailable),
idempotent, small batches, a wall-clock budget, owned books first and newest first,
and it stops early on throttling. It runs on the scheduler pool like every job kind.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .. import db
from ..config import settings
from . import kidsgate, observations, provider_results

log = logging.getLogger(__name__)

PROVIDER = "openreads-audience"      # provider_retry_state key for the backfill's calls
MAX_ATTEMPTS = 4                     # after this many incomplete answers a work is closed, not retried forever
RETRY_AFTER_SECONDS = 6 * 3600
MAX_BATCH = 200
# Signal kinds that count as "has audience metadata" (the rest is provenance: publisher, language, ...).
AUDIENCE_KINDS = tuple(kidsgate.SIGNAL_KIND_TO_EVIDENCE)

# OpenReads observation field -> (signal kind, is a list)
_FIELD_KIND: dict[str, tuple[str, bool]] = {
    "subjects": ("subject", True), "subject_places": ("subject_place", True),
    "categories": ("category", True), "maturity_rating": ("maturity", False),
    "age_range": ("age_range", False), "audience": ("audience", False),
    "language": ("language", False), "publisher": ("publisher", False), "num_pages": ("page_count", False),
}


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _values(raw: Any, is_list: bool) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for item in items:
        text = str(item).strip()[:300] if item is not None else ""
        if text and text not in out:
            out.append(text)
    return out if is_list or not out else out[:1]


def store(work_id: str, audience: list[dict[str, Any]] | None, *, status: dict[str, Any] | None = None,
          complete: bool = True, t: float | None = None) -> int:
    """Record OpenReads' audience payload ([{provider, field, value, observed_at}]) for a work.
    Per provider and kind the stored values are REPLACED by what the provider now says, so a
    corrected category does not linger; kinds a provider did not report are left alone.
    Returns the number of signal rows written. `status` (the backfill only) also updates the
    work's cursor row. Idempotent."""
    t = t or db.now()
    if not audience and status is None:
        return 0
    if not db.one("SELECT 1 FROM works WHERE id=?", (work_id,)):
        return 0
    written = 0
    with db.tx() as conn:
        for item in audience or []:
            provider, field = str(item.get("provider") or ""), item.get("field")
            if not provider or field not in _FIELD_KIND:
                continue
            kind, is_list = _FIELD_KIND[field]
            values = _values(item.get("value"), is_list)
            if not values:
                continue
            fetched = float(item.get("observed_at") or t)
            observations.record(conn, "work", work_id, field, json.dumps(values) if is_list else values[0], provider, applied=False, t=fetched)
            conn.execute("DELETE FROM work_audience_signals WHERE work_id=? AND provider=? AND kind=?", (work_id, provider, kind))
            for v in values:
                conn.execute("INSERT OR REPLACE INTO work_audience_signals(work_id,provider,kind,value,norm,fetched_at) VALUES (?,?,?,?,?,?)",
                             (work_id, provider, kind, v, kidsgate.normalize_term(kind, v), fetched))
                written += 1
        if status is not None:
            count = conn.execute(f"SELECT COUNT(*) n FROM work_audience_signals WHERE work_id=? AND kind IN ({','.join('?' * len(AUDIENCE_KINDS))})",  # nosec B608
                                 (work_id, *AUDIENCE_KINDS)).fetchone()["n"]
            prev = conn.execute("SELECT attempts FROM work_audience_status WHERE work_id=?", (work_id,)).fetchone()
            attempts = (prev["attempts"] if prev else 0) + 1
            closed = complete or attempts >= MAX_ATTEMPTS
            conn.execute(
                """INSERT INTO work_audience_status(work_id,checked_at,complete,attempts,next_try_at,providers_json,signal_count)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(work_id) DO UPDATE SET checked_at=excluded.checked_at,complete=excluded.complete,attempts=excluded.attempts,
                     next_try_at=excluded.next_try_at,providers_json=excluded.providers_json,signal_count=excluded.signal_count""",
                (work_id, t, int(closed), attempts, None if closed else t + RETRY_AFTER_SECONDS, db.json_dump(status), count))
    return written


def signals_for_work(work_id: str) -> list[dict[str, Any]]:
    """What the book page shows: every stored provider value with its class and fetch time."""
    return [dict(r) for r in db.all(
        "SELECT provider,kind,value,norm,fetched_at FROM work_audience_signals WHERE work_id=? ORDER BY kind,provider,value", (work_id,))]


def coverage() -> dict[str, Any]:
    """'N of M works have audience metadata', plus how the backfill is doing."""
    marks = ",".join("?" * len(AUDIENCE_KINDS))
    total = db.one("SELECT COUNT(*) n FROM works")["n"]
    with_meta = db.one(f"SELECT COUNT(DISTINCT work_id) n FROM work_audience_signals WHERE kind IN ({marks})", AUDIENCE_KINDS)["n"]  # nosec B608
    unlinked = db.one("SELECT COUNT(*) n FROM works WHERE metadata_id IS NULL")["n"]
    checked = db.one("SELECT COUNT(*) n FROM work_audience_status WHERE complete=1")["n"]
    pending = db.one("""SELECT COUNT(*) n FROM works w LEFT JOIN work_audience_status s ON s.work_id=w.id
                        WHERE w.metadata_id IS NOT NULL AND (s.work_id IS NULL OR s.complete=0)""")["n"]
    job = db.one("SELECT last_run_at,last_status,last_error FROM jobs WHERE kind='audience-backfill'") or {}
    running = any(r["kind"] == "audience-backfill" for r in _running())
    return {"total_works": total, "with_audience": with_meta, "checked": checked, "pending": pending, "unlinked": unlinked,
            "running": running, "last_run_at": job.get("last_run_at"), "last_status": job.get("last_status"), "last_error": job.get("last_error")}


def _running() -> list[dict[str, Any]]:
    from . import scheduler
    return scheduler.running_jobs()


def candidates(limit: int, now: float | None = None) -> list[dict[str, Any]]:
    """Linked works the backfill has not finished, owned first then newest first."""
    return db.all(
        """SELECT w.id,w.metadata_id FROM works w LEFT JOIN work_audience_status s ON s.work_id=w.id
           WHERE w.metadata_id IS NOT NULL AND (s.work_id IS NULL OR (s.complete=0 AND COALESCE(s.next_try_at,0)<=?))
           ORDER BY EXISTS(SELECT 1 FROM media_assets a WHERE a.work_id=w.id AND a.status='owned') DESC, w.added_at DESC, w.id
           LIMIT ?""",
        (now if now is not None else db.now(), limit),
    )


def backfill(limit: int | None = None, *, budget_seconds: float | None = None, pause: float | None = None) -> dict[str, Any]:
    """One bounded batch. Never raises for a provider problem; stops starting works once the
    wall-clock budget is spent or OpenReads says it is being throttled."""
    from . import metadata

    limit = max(1, min(int(limit if limit is not None else settings.audience_batch), MAX_BATCH))
    budget = settings.audience_budget_seconds if budget_seconds is None else budget_seconds
    pause = settings.audience_pause_seconds if pause is None else pause
    rows = candidates(limit)
    started = _monotonic()
    stored = with_signals = none_found = incomplete = deferred = errors = budget_stopped = 0
    stop_reason, consecutive = None, 0
    for i, row in enumerate(rows):
        if budget and i and _monotonic() - started >= budget:
            budget_stopped, stop_reason = len(rows) - i, "budget"
            break
        if provider_results.deferred(PROVIDER, "work", row["id"]):
            deferred += 1
            continue
        if i:
            _sleep(pause)
        try:
            payload = metadata.fetch_audience(row["metadata_id"])
        except httpx.HTTPError as exc:
            failure = provider_results.classify_exception(exc, PROVIDER)
            provider_results.record(PROVIDER, "work", row["id"], failure)
            if failure.status == provider_results.NOT_FOUND or not failure.retryable:
                # OpenReads has no such work (or refuses it): close it so it is not asked every run.
                store(row["id"], None, status={"openreads": failure.status}, complete=True)
                none_found += 1
                continue
            errors += 1
            consecutive += 1
            if failure.status == provider_results.RATE_LIMITED or consecutive >= 2:
                stop_reason = failure.status if failure.status == provider_results.RATE_LIMITED else "provider_unavailable"
                break
            continue
        consecutive = 0
        provider_results.record(PROVIDER, "work", row["id"], provider_results.ProviderResult(provider_results.SUCCESS, PROVIDER))
        audience = payload.get("audience") or []
        complete = bool(payload.get("complete", True))
        store(row["id"], audience, status=payload.get("status") or {}, complete=complete)
        stored += 1
        if not complete:
            incomplete += 1
        if any(a.get("field") in ("subjects", "categories", "maturity_rating", "age_range", "audience") for a in audience):
            with_signals += 1
        else:
            none_found += 1
    cov = coverage()
    out = {"considered": len(rows) - budget_stopped, "stored": stored, "with_signals": with_signals, "no_signals": none_found,
           "incomplete": incomplete, "deferred": deferred, "errors": errors, "budget_stopped": budget_stopped,
           "stopped": stop_reason, "elapsed_seconds": round(_monotonic() - started, 1), "remaining": cov["pending"],
           "with_audience": cov["with_audience"], "total_works": cov["total_works"]}
    log.info("audience backfill: %s", out)
    return out


def backfill_job(job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scheduler entry: `batch` / `budget_seconds` in the job's config_json override the settings."""
    config = db.json_load((job or {}).get("config_json"), {}) or {}
    try:
        batch = int(config.get("batch")) if config.get("batch") else None
        budget = float(config["budget_seconds"]) if config.get("budget_seconds") else None
    except (TypeError, ValueError):
        batch = budget = None
    return backfill(batch, budget_seconds=budget)
