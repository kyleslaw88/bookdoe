"""Failure aggregation for the dashboard "needs attention" tile and its detail view."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import failures, migration

client = TestClient(app)


def _failed_download(title="A Book"):
    work_id = migration._find_or_create_stub({"title": title, "author": "X", "identifiers": {}})
    did, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO downloads(id,work_id,state,source_title,error,added_at,updated_at)
               VALUES (?,?, 'import_failed',?,?,?,?)""",
            (did, work_id, title, "No managed root folder configured", t, t),
        )
    return did


def _job_run(status="failed"):
    jid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO jobs(id,kind,name,schedule_seconds,enabled,next_run_at,config_json) VALUES (?,?,?,?,1,?,'{}')", (jid, "test-job", "Test Job", 3600, t))
        conn.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at,finished_at) VALUES (?,?,?,?,?,?)", (db.uid(), jid, "test-job", status, t, t))
    return jid


def test_summary_counts_failed_imports_and_jobs():
    _failed_download()
    _job_run("failed")
    summary = failures.summary()
    assert summary["failed_imports"] == 1
    assert summary["failed_jobs"] == 1
    assert summary["total"] == 2


def test_only_the_most_recent_run_per_job_counts():
    """A job that failed once last week and has since succeeded is not something to
    act on today."""
    jid = _job_run("failed")
    with db.tx() as conn:
        conn.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at,finished_at) VALUES (?,?,?,?,?,?)", (db.uid(), jid, "test-job", "success", db.now() + 10, db.now() + 11))
    assert failures.summary()["failed_jobs"] == 0


def test_failed_imports_detail_includes_the_reason_and_work_title():
    _failed_download("Broken Book")
    items = failures.failed_imports()
    assert items[0]["work_title"] == "Broken Book"
    assert "No managed root" in items[0]["error"]


def test_dashboard_surfaces_the_failures_summary():
    _failed_download()
    r = client.get("/api/v1/dashboard")
    assert r.status_code == 200
    assert r.json()["failures"]["failed_imports"] == 1


def test_failures_endpoint_returns_both_lists():
    _failed_download()
    _job_run("failed")
    r = client.get("/api/v1/failures")
    assert r.status_code == 200
    body = r.json()
    assert len(body["imports"]) == 1
    assert len(body["jobs"]) == 1
