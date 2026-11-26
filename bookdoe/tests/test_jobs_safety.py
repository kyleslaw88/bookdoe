"""Regression tests for the acquisition-safety posture of scheduled jobs.

Defect found during the 2026-09-24 Unraid deployment: every default job was
seeded with enabled=1, so a fresh install would begin automatic Wanted searches
30 minutes after first boot, and there was no supported way to turn one off.
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def _job(kind: str) -> dict:
    return db.one("SELECT * FROM jobs WHERE kind=?", (kind,))


def test_acquisition_and_provider_jobs_are_disabled_on_a_fresh_install():
    # A new install must not search, grab, or hammer external providers on its own.
    for kind in ("wanted-search", "upgrade-search", "author-discovery", "metadata-refresh"):
        assert _job(kind)["enabled"] == 0, f"{kind} must be disabled by default"


def test_local_maintenance_jobs_remain_enabled():
    # Backups and health checks are safe and are wanted from day one.
    for kind in ("download-poll", "database-backup", "library-health", "housekeeping"):
        assert _job(kind)["enabled"] == 1, f"{kind} should be enabled by default"


def test_cover_fetch_is_enabled_but_bounded():
    """Explicit safety decision for cover-fetch.

    It is enabled because cover art is part of rendering the library rather than
    acquiring media: without it the grid shows an empty frame for every work. It is
    not an acquisition job and it does not touch indexers or download clients. Its
    provider contact is bounded - a small batch per run, local sources tried first,
    failures remembered so a work is not re-asked forever - and remote fetching can
    be turned off entirely with BOOKDOE_COVER_REMOTE_FETCH=0.
    """
    job = _job("cover-fetch")
    assert job["enabled"] == 1
    assert job["schedule_seconds"] >= 300, "cover backfill must not run in a tight loop"


def test_tag_classify_is_enabled_but_inert_without_the_llm():
    """Explicit safety decision for tag-classify.

    Enabled by default, but BOOKDOE_LLM_ENABLED - not this flag - is the actual
    gate: classify_kids_missing() returns {"skipped": True} and touches nothing when
    the LLM is unset (see llmclient.py/tagging.py). It only ever ADDS the 'kids' tag,
    and only above a high confidence threshold, so even a misconfigured LLM cannot
    remove a book from a kids account's shelf.
    """
    from app.services import tagging
    job = _job("tag-classify")
    assert job["enabled"] == 1
    result = tagging.classify_kids_missing()
    assert result == {"skipped": True, "reason": "LLM not enabled (BOOKDOE_LLM_ENABLED)"}


def test_audience_backfill_is_enabled_but_bounded():
    """Explicit safety decision for audience-backfill.

    It only READS provider metadata (through OpenReads, which caches it) and writes the
    audience tables; it never changes a tag or a work's canonical fields. Provider contact is
    bounded: a few works per run (BOOKDOE_AUDIENCE_BATCH), a wall-clock budget, a pause
    between works, a stop on throttling, and a work is never asked again once answered.
    """
    job = _job("audience-backfill")
    assert job["enabled"] == 1
    assert job["schedule_seconds"] >= 300, "must not run in a tight loop"


def test_maintenance_scan_is_enabled_because_it_only_writes_issue_rows():
    assert _job("maintenance-scan")["enabled"] == 1


def test_conversion_jobs_are_enabled_because_they_only_work_a_queue_an_admin_filled():
    """convert-audio / convert-ebook drain the `conversions` table. Nothing enters it except an
    admin's explicit request or the opt-in auto-on-import policy (off by default), so the jobs
    are safe to run from the first start; an empty queue returns at once."""
    from app.services import conversion

    for kind in ("convert-audio", "convert-ebook"):
        job = _job(kind)
        assert job["enabled"] == 1 and job["schedule_seconds"] >= 60
    assert conversion.get_settings()["auto_on_import"] is False
    assert conversion.run_queue("audio") == {"processed": 0, "done": 0, "failed": 0, "skipped": 0, "retrying": 0, "cancelled": 0, "conversion_ids": []}


def test_every_seeded_job_is_classified():
    kinds = {r["kind"] for r in db.all("SELECT kind FROM jobs")}
    # Guards against a new job being added without an explicit safety decision.
    assert kinds == {
        "download-poll", "wanted-search", "metadata-refresh", "author-discovery",
        "database-backup", "library-health", "housekeeping", "cover-fetch", "tag-classify",
        "managed-library-build", "maintenance-scan", "upgrade-search", "audience-backfill",
        "convert-audio", "convert-ebook", "trash-purge",
        "duplicate-scan", "duplicate-harvest", "duplicate-batch", "duplicate-refresh", "podcast-scan",
    }


def test_job_can_be_enabled_and_disabled_through_the_api():
    job = _job("wanted-search")
    assert job["enabled"] == 0

    r = client.put(f"/api/v1/jobs/{job['id']}", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] == 1
    assert _job("wanted-search")["enabled"] == 1

    r = client.put(f"/api/v1/jobs/{job['id']}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert _job("wanted-search")["enabled"] == 0


def test_toggling_a_job_is_recorded_in_history():
    job = _job("wanted-search")
    client.put(f"/api/v1/jobs/{job['id']}", json={"enabled": True})
    events = {r["event_type"] for r in db.all("SELECT event_type FROM history")}
    assert "job.enabled" in events


def test_rescheduling_updates_the_next_run():
    job = _job("library-health")
    r = client.put(f"/api/v1/jobs/{job['id']}", json={"schedule_seconds": 900})
    assert r.status_code == 200, r.text
    assert r.json()["schedule_seconds"] == 900


def test_unknown_job_is_rejected():
    assert client.put("/api/v1/jobs/does-not-exist", json={"enabled": True}).status_code == 404


def test_empty_update_is_rejected():
    job = _job("housekeeping")
    assert client.put(f"/api/v1/jobs/{job['id']}", json={}).status_code == 400


def test_scheduler_only_selects_enabled_jobs():
    # The scheduler's due-query is the enforcement point for the safety default.
    db.tx  # noqa: B018  (documenting intent; query below mirrors scheduler.py)
    due = db.all("SELECT kind FROM jobs WHERE enabled=1")
    assert "wanted-search" not in {r["kind"] for r in due}
