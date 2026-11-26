from __future__ import annotations

from typing import Any

from .. import db


def summary() -> dict[str, Any]:
    """Counts for the dashboard's "needs attention" tile.

    Kept cheap and separate from the detail lists below: the dashboard renders on
    every visit and must not pay for the full failed-download join just to show two
    numbers.
    """
    failed_imports = db.one("SELECT COUNT(*) n FROM downloads WHERE state='import_failed'")["n"]
    # Only the most recent run per job counts as "currently failing" - a job that
    # failed once last week and has since succeeded is not something to act on today.
    failed_jobs = db.one(
        """SELECT COUNT(*) n FROM (
             SELECT jr.job_id, jr.status,
                    ROW_NUMBER() OVER (PARTITION BY jr.job_id ORDER BY jr.started_at DESC) rn
             FROM job_runs jr WHERE jr.job_id IS NOT NULL
           ) latest WHERE rn=1 AND status='failed'"""
    )["n"]
    return {"failed_imports": failed_imports, "failed_jobs": failed_jobs, "total": failed_imports + failed_jobs}


def failed_imports() -> list[dict[str, Any]]:
    return db.all(
        """SELECT d.id,d.source_title,d.download_path,d.error,d.updated_at,d.work_id,d.asset_id,w.title work_title
           FROM downloads d LEFT JOIN works w ON w.id=d.work_id
           WHERE d.state='import_failed' ORDER BY d.updated_at DESC"""
    )


def failed_jobs() -> list[dict[str, Any]]:
    return db.all(
        """SELECT j.id job_id,j.name,j.kind,latest.status,latest.error,latest.started_at,latest.finished_at
           FROM jobs j JOIN (
             SELECT jr.*, ROW_NUMBER() OVER (PARTITION BY jr.job_id ORDER BY jr.started_at DESC) rn
             FROM job_runs jr WHERE jr.job_id IS NOT NULL
           ) latest ON latest.job_id=j.id AND latest.rn=1
           WHERE latest.status='failed' ORDER BY latest.started_at DESC"""
    )
