"""F-07: job run observability and the startup reaper for runs left 'running'."""
import json

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import scheduler

client = TestClient(app)


def _job(kind: str) -> dict:
    return db.one("SELECT * FROM jobs WHERE kind=?", (kind,))


def _runs(job_id: str) -> list[dict]:
    return db.all("SELECT * FROM job_runs WHERE job_id=? ORDER BY started_at, rowid", (job_id,))


def test_run_records_execution_id_trigger_and_entities():
    job = _job("housekeeping")
    out = scheduler.run_job(job["id"], trigger="manual")
    row = db.one("SELECT * FROM job_runs WHERE id=?", (out["run_id"],))
    assert row["status"] == "success"
    assert row["trigger_source"] == "manual"
    assert row["execution_id"] and row["execution_id"] == out["execution_id"] != row["id"]
    assert row["attempt"] == 1 and row["retry_of"] is None
    assert json.loads(row["entities_json"]) == {}


def test_scheduled_runs_are_marked_schedule_and_get_distinct_execution_ids():
    job = _job("housekeeping")
    db.execute("UPDATE jobs SET enabled=0")
    db.execute("UPDATE jobs SET enabled=1,next_run_at=NULL WHERE id=?", (job["id"],))
    scheduler.run_due_jobs()
    scheduler.run_job(job["id"])  # default trigger: manual
    first, second = _runs(job["id"])
    assert (first["trigger_source"], second["trigger_source"]) == ("schedule", "manual")
    assert first["execution_id"] != second["execution_id"]


def test_failed_run_then_rerun_records_retry_state(monkeypatch):
    job = _job("housekeeping")

    def boom(_job):
        raise RuntimeError("kaput")

    monkeypatch.setattr(scheduler, "_execute", boom)
    failed = scheduler.run_job(job["id"])
    assert failed["status"] == "failed"
    monkeypatch.undo()
    retry = scheduler.run_job(job["id"])
    row = db.one("SELECT * FROM job_runs WHERE id=?", (retry["run_id"],))
    assert row["status"] == "success" and row["attempt"] == 2 and row["retry_of"] == failed["run_id"]
    # A success resets the chain.
    assert db.one("SELECT attempt FROM job_runs WHERE id=?", (scheduler.run_job(job["id"])["run_id"],))["attempt"] == 1


def test_affected_entities_collects_ids_and_failures():
    got = scheduler._affected_entities({"asset_ids": ["a1"], "errors": [{"work_id": "w1", "error": "x"}], "n": 3})
    assert got == {"asset_ids": ["a1"], "failed_work_ids": ["w1"]}


def test_reaper_fails_running_rows_and_leaves_finished_ones():
    job = _job("housekeeping")
    done = scheduler.run_job(job["id"])
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('stale1',?,?,'running',?)", (job["id"], job["kind"], db.now() - 600))
    assert scheduler.reap_stale_runs() == 1
    row = db.one("SELECT * FROM job_runs WHERE id='stale1'")
    assert row["status"] == "failed" and row["finished_at"] and "Interrupted" in row["error"]
    assert db.one("SELECT status FROM job_runs WHERE id=?", (done["run_id"],))["status"] == "success"
    assert db.one("SELECT last_status FROM jobs WHERE id=?", (job["id"],))["last_status"] == "failed"
    assert not db.all("SELECT 1 FROM job_runs WHERE status='running'")
    assert scheduler.reap_stale_runs() == 0


def test_app_startup_reaps_runs_left_running_by_a_killed_process():
    job = _job("housekeeping")
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('stale2',?,?,'running',?)", (job["id"], job["kind"], db.now()))
    with TestClient(app):  # runs the real lifespan startup
        assert not db.all("SELECT 1 FROM job_runs WHERE status='running'")
    assert db.one("SELECT status FROM job_runs WHERE id='stale2'")["status"] == "failed"


def test_jobs_api_exposes_run_history_with_trigger_and_outcome():
    job = _job("housekeeping")
    assert client.post(f"/api/v1/jobs/{job['id']}/run", json={}).status_code == 200
    run = client.get("/api/v1/jobs").json()["runs"][0]
    assert run["trigger_source"] == "manual" and run["status"] == "success" and run["execution_id"]
