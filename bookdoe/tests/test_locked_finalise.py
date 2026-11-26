"""#709: a locked finalising write must not leave a job run 'running' or a download 'importing'."""
import sqlite3
import time

from app import db
from app.services import downloads, scheduler, importer, derived

import helpers  # noqa: F401
from test_import_idempotent import prepare_import, _download


def _locked_once():
    state = {"n": 0}

    def hook(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
    return state, hook


def test_locked_close_out_of_a_run_is_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    state, hook = _locked_once()
    real = scheduler._affected_entities
    monkeypatch.setattr(scheduler, "_affected_entities", lambda r: (hook(), real(r))[1])
    job = db.one("SELECT * FROM jobs WHERE kind='housekeeping'")
    out = scheduler.run_job(job["id"])
    assert state["n"] >= 2
    row = db.one("SELECT status,finished_at FROM job_runs WHERE id=?", (out["run_id"],))
    assert row["status"] == "success" and row["finished_at"]
    assert db.one("SELECT last_status FROM jobs WHERE id=?", (job["id"],))["last_status"] == "success"


def test_locked_final_import_write_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    did, _, asset_id = _download()
    prepare_import(tmp_path, did, ["a.m4b", "b.m4b"])
    state, hook = _locked_once()
    real = derived.supersede
    monkeypatch.setattr(derived, "supersede", lambda *a, **k: (hook(), real(*a, **k))[1])
    downloads.import_completed(did)
    d = db.one("SELECT state,imported_at FROM downloads WHERE id=?", (did,))
    assert d["state"] == "imported" and d["imported_at"]
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_orphaned_running_run_is_reaped_and_does_not_block_the_next_run():
    job = db.one("SELECT * FROM jobs WHERE kind='database-backup'")
    rid = db.uid()
    old = db.now() - 1000
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES (?,?,?,'running',?)", (rid, job["id"], job["kind"], old))
    assert scheduler.reap_orphan_runs() == 1
    row = db.one("SELECT status,finished_at,error FROM job_runs WHERE id=?", (rid,))
    assert row["status"] == "failed" and row["finished_at"] and row["error"] == scheduler.ORPHAN_RUN_REASON
    assert db.one("SELECT last_status FROM jobs WHERE id=?", (job["id"],))["last_status"] == "failed"
    # A stale row never gates scheduling: the kind is free and the next due run goes ahead.
    db.execute("UPDATE jobs SET enabled=1,next_run_at=0 WHERE id=?", (job["id"],))
    out = scheduler.submit_job(job["id"], trigger="schedule")
    assert out["status"] == "running"
    out["future"].result()


def test_live_run_is_not_reaped(monkeypatch):
    job = db.one("SELECT * FROM jobs WHERE kind='housekeeping'")
    seen = {}
    def probe(j):
        seen["n"] = scheduler.reap_orphan_runs()
        return {}
    monkeypatch.setattr(scheduler, "_execute", probe)
    out = scheduler.run_job(job["id"])
    assert seen["n"] == 0
    assert db.one("SELECT status FROM job_runs WHERE id=?", (out["run_id"],))["status"] == "success"


def test_stale_importing_download_is_recovered_and_imported(tmp_path):
    did, _, asset_id = _download()
    prepare_import(tmp_path, did, ["a.m4b"])
    db.execute("UPDATE downloads SET state='importing',updated_at=? WHERE id=?", (db.now() - 3600, did))
    assert downloads.recover_stale_imports() == 1
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "imported"
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_fresh_importing_download_is_left_alone():
    did, _, _ = _download()
    db.execute("UPDATE downloads SET state='importing',updated_at=? WHERE id=?", (db.now(), did))
    assert downloads.recover_stale_imports() == 0
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "importing"
