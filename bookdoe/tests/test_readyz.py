"""E2: /readyz reflects real readiness, unlike the constant /health."""
import dataclasses

import httpx
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import readiness

client = TestClient(app)


def _no_openreads(monkeypatch, ok=False):
    def fake_client(timeout=20.0):
        def handler(request):
            return httpx.Response(200 if ok else 500, json={"ok": True})
        return httpx.Client(base_url="http://openreads.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.metadata._client", fake_client)


def _scheduler_on(monkeypatch):
    monkeypatch.setattr(readiness, "settings", dataclasses.replace(readiness.settings, scheduler_enabled=True))


def test_readyz_ok_and_unauthenticated(monkeypatch):
    _no_openreads(monkeypatch, ok=True)
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["db_writable"]["ok"] is True
    assert body["counts"] == {"import_failed_downloads": 0, "failed_job_runs_24h": 0}
    assert client.get("/health").json()["status"] == "ok"
    assert "version" not in client.get("/health").json()


def test_openreads_down_is_degraded_not_503(monkeypatch):
    _no_openreads(monkeypatch, ok=False)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["openreads"] == {"ok": False, "error": "openreads unreachable"}


def test_read_only_database_is_503(monkeypatch):
    _no_openreads(monkeypatch, ok=True)
    real = db.connect
    monkeypatch.setattr(readiness.db, "connect", lambda: real(readonly=True))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["db_writable"]["ok"] is False
    assert r.json()["status"] == "unavailable"


def test_stale_heartbeat_is_503_and_fresh_is_ok(monkeypatch):
    _no_openreads(monkeypatch, ok=True)
    _scheduler_on(monkeypatch)
    db.execute("UPDATE jobs SET enabled=0")
    job = db.one("SELECT id FROM jobs WHERE kind='download-poll'")
    db.execute("UPDATE jobs SET enabled=1, schedule_seconds=30, last_run_at=? WHERE id=?", (db.now() - 7200, job["id"]))
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('old',?, 'download-poll','success',?)", (job["id"], db.now() - 7200))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["scheduler_heartbeat"]["ok"] is False
    db.execute("UPDATE jobs SET last_run_at=? WHERE id=?", (db.now() - 10, job["id"]))
    assert client.get("/readyz").status_code == 200


def test_heartbeat_skipped_when_scheduler_disabled_or_never_ran(monkeypatch):
    _no_openreads(monkeypatch, ok=True)
    db.execute("UPDATE jobs SET last_run_at=NULL")
    _scheduler_on(monkeypatch)
    assert client.get("/readyz").json()["checks"]["scheduler_heartbeat"]["skipped"] == "no job has run yet"
    monkeypatch.undo()
    _no_openreads(monkeypatch, ok=True)
    assert client.get("/readyz").json()["checks"]["scheduler_heartbeat"]["skipped"] == "scheduler disabled"


def test_counts_report_failed_imports_and_recent_failed_runs(monkeypatch):
    _no_openreads(monkeypatch, ok=True)
    job = db.one("SELECT id FROM jobs LIMIT 1")
    now = db.now()
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('f1',?, 'x','failed',?)", (job["id"], now - 60))
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('f2',?, 'x','failed',?)", (job["id"], now - 3 * 86400))
    assert client.get("/readyz").json()["counts"]["failed_job_runs_24h"] == 1
