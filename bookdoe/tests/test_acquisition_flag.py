"""The acquisition feature flag (#494, part A): off for a fresh install, on for an upgrade that uses it."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import acquisition, scheduler

client = TestClient(app)

ROUTES = [
    ("get", "/api/v1/indexers"),
    ("get", "/api/v1/download-clients"),
    ("get", "/api/v1/queue"),
    ("get", "/api/v1/wanted"),
    ("post", "/api/v1/queue/poll"),
    ("post", "/api/v1/wanted/search-now"),
]


def _call(method, path):
    return getattr(client, method)(path)


def test_fresh_install_acquisition_404(monkeypatch):
    monkeypatch.delenv(acquisition.ENV_NAME, raising=False)
    assert acquisition.acquisition_enabled() is False
    for method, path in ROUTES:
        assert _call(method, path).status_code == 404, path
    assert client.get("/api/v1/auth/me").json()["acquisition_enabled"] is False


def test_fresh_install_no_acquisition_jobs(monkeypatch):
    monkeypatch.delenv(acquisition.ENV_NAME, raising=False)
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET next_run_at=0 WHERE schedule_seconds IS NOT NULL")
    dispatched = []

    def fake_submit(job_id, trigger="schedule"):
        dispatched.append(db.one("SELECT kind FROM jobs WHERE id=?", (job_id,))["kind"])
        return {"kind": "x"}

    monkeypatch.setattr(scheduler, "submit_job", fake_submit)
    scheduler.run_due_jobs(block=False)
    assert dispatched, "non-acquisition jobs still run"
    assert not set(dispatched) & set(acquisition.ACQUISITION_JOB_KINDS)
    # Turning it on dispatches them again.
    dispatched.clear()
    monkeypatch.setenv(acquisition.ENV_NAME, "1")
    with db.tx() as conn:
        conn.execute("UPDATE jobs SET enabled=1 WHERE kind='download-poll'")
    scheduler.run_due_jobs(block=False)
    assert "download-poll" in dispatched


def test_upgrade_with_indexer_flag_on(monkeypatch):
    """A database that already holds an indexer gets the flag ON when step 48 runs; an empty one gets OFF."""
    monkeypatch.delenv(acquisition.ENV_NAME, raising=False)
    with db.tx() as conn:
        conn.execute("INSERT INTO indexers(id,name,kind,base_url,api_key_enc,categories_json,priority,enabled,created_at,updated_at) VALUES ('i1','Idx','torznab','http://x','','[]',1,1,0,0)")
        conn.execute("DELETE FROM settings WHERE key='acquisition_enabled'")
        conn.execute("DELETE FROM migration_steps WHERE version=48")
    db.init_db()
    assert db.get_setting("acquisition_enabled") is True
    assert acquisition.acquisition_enabled() is True
    assert client.get("/api/v1/indexers").status_code == 200

    with db.tx() as conn:
        conn.execute("DELETE FROM indexers")
        conn.execute("DELETE FROM settings WHERE key='acquisition_enabled'")
        conn.execute("DELETE FROM migration_steps WHERE version=48")
    db.init_db()
    assert db.get_setting("acquisition_enabled") is False


def test_env_override(monkeypatch):
    monkeypatch.setenv(acquisition.ENV_NAME, "1")
    assert db.get_setting("acquisition_enabled") is False
    assert acquisition.acquisition_enabled() is True
    assert client.get("/api/v1/indexers").status_code == 200
    db.set_setting("acquisition_enabled", True)
    monkeypatch.setenv(acquisition.ENV_NAME, "0")
    assert acquisition.acquisition_enabled() is False
    assert client.get("/api/v1/indexers").status_code == 404
    monkeypatch.setenv(acquisition.ENV_NAME, "garbage")  # unrecognised: falls back to the stored setting
    assert acquisition.acquisition_enabled() is True
