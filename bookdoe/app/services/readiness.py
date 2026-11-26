"""Readiness probe behind the unauthenticated GET /readyz.

/health only proves the process answers. This checks the things that make the app
useless while the process is still up: a database that cannot be written and a
scheduler that has stopped running jobs. Nothing here returns a secret, a path or
an upstream error string: the body is safe to expose without authentication.

Failure policy:
* db not writable            -> not ready (503)
* scheduler heartbeat stale  -> not ready (503)
* OpenReads unreachable      -> "degraded" but still 200. bookdoe serves and
  manages the existing library without it; failing the probe would make Docker mark
  the container unhealthy and make deploy.sh roll back over a sibling's outage.
* failed imports / failed job runs -> informational counts only.
"""
from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import httpx

from .. import db
from . import buildinfo
from ..config import settings

# The loop ticks every 15 s and jobs run off it; a bare 2x of a 30 s poll would still
# flap on a busy host, so never demand fresher than this.
HEARTBEAT_FLOOR_SECONDS = 300.0


def _check_db_writable() -> dict[str, Any]:
    try:
        conn = db.connect()
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("BEGIN IMMEDIATE")
            # BEGIN IMMEDIATE alone can succeed on a read-only WAL database; a header
            # write (same value, then rolled back) is what fails on a read-only file.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute(f"PRAGMA user_version={int(version)}")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        return {"ok": True}
    except sqlite3.OperationalError as exc:
        # Another writer holds the lock (a backup, a big import): busy is not broken.
        if "locked" in str(exc) or "busy" in str(exc):
            return {"ok": True, "busy": True}
        return {"ok": False, "error": "database is not writable"}
    except Exception:
        return {"ok": False, "error": "database is not writable"}


def _check_heartbeat() -> dict[str, Any]:
    """Is the scheduler LOOP alive? The loop writes a heartbeat every tick whether or not
    a job is busy (scheduler.tick), so a tag-classify batch talking to a slow model for
    minutes no longer reads as a dead scheduler. Databases from before the heartbeat
    (or a process whose loop never ticked yet) fall back to the newest run time. The
    running jobs and their ages are reported for operators, never counted as failure."""
    from . import scheduler

    if not settings.scheduler_enabled:
        return {"ok": True, "skipped": "scheduler disabled"}
    running = scheduler.running_jobs()
    beat = scheduler.heartbeat()
    shortest = db.one("SELECT MIN(schedule_seconds) AS s FROM jobs WHERE enabled=1 AND schedule_seconds IS NOT NULL")
    shortest_s = shortest["s"] if shortest else None
    if beat is not None:
        allowed = max(2.0 * float(shortest_s or scheduler.TICK_SECONDS), HEARTBEAT_FLOOR_SECONDS)
        age = max(0.0, db.now() - beat)
        return {"ok": age <= allowed, "source": "loop", "last_tick_age_seconds": round(age), "allowed_seconds": round(allowed), "running": running}
    # Both sources: no-op download polls are not kept as job_runs rows, but they still
    # advance jobs.last_run_at.
    row = db.one(
        "SELECT MAX(t) AS newest FROM (SELECT MAX(started_at) AS t FROM job_runs UNION ALL SELECT MAX(last_run_at) FROM jobs)"
    )
    newest = row["newest"] if row else None
    if newest is None:
        return {"ok": True, "skipped": "no job has run yet", "running": running}
    if not shortest_s:
        return {"ok": True, "skipped": "no enabled scheduled job", "running": running}
    allowed = max(2.0 * float(shortest_s), HEARTBEAT_FLOOR_SECONDS)
    age = max(0.0, db.now() - float(newest))
    return {"ok": age <= allowed, "source": "runs", "last_run_age_seconds": round(age), "allowed_seconds": round(allowed), "running": running}


def _check_openreads() -> dict[str, Any]:
    try:
        # Short timeout: this runs on every Docker health interval.
        from . import metadata
        with contextlib.closing(metadata._client(2.0)) as c:
            r = c.get("/api/v2/health")
            r.raise_for_status()
        return {"ok": True}
    except (httpx.HTTPError, OSError):
        return {"ok": False, "error": "openreads unreachable"}


def _counts() -> dict[str, int]:
    since = db.now() - 86400
    return {
        "import_failed_downloads": db.one("SELECT COUNT(*) n FROM downloads WHERE state='import_failed'")["n"],
        "failed_job_runs_24h": db.one("SELECT COUNT(*) n FROM job_runs WHERE status='failed' AND started_at>=?", (since,))["n"],
    }


def readiness() -> tuple[bool, dict[str, Any]]:
    checks = {"db_writable": _check_db_writable(), "scheduler_heartbeat": _check_heartbeat(), "openreads": _check_openreads()}
    ready = checks["db_writable"]["ok"] and checks["scheduler_heartbeat"]["ok"]
    body: dict[str, Any] = {"status": "ok" if ready else "unavailable", "checks": checks}
    if ready and not checks["openreads"]["ok"]:
        body["status"] = "degraded"
    body["build"] = buildinfo.short()
    try:
        body["counts"] = _counts()
    except Exception:
        body["counts"] = None
    return ready, body
