"""The scheduler must never be starved by a long job (live incident 2026-10-11: a
tag-classify batch of 20 x up-to-300 s LLM calls ran inside the scheduler loop,
/readyz went 503 with the heartbeat 1197 s old, download polls stalled for 20 min).

Reproduction: the OLD loop ran every due job inline in one thread and the heartbeat
was the newest run time, so a job that takes 1200 s (fake clock, no sleeping) leaves
the heartbeat 1200 s stale and nothing else runs. With the fix the loop only ticks
(heartbeat + dispatch to a pool), one run per kind at a time (skip, not queue), a
stuck run's lease expires, /readyz reads the loop's heartbeat and reports the busy job
instead of failing, batches honour a wall-clock budget, and the Management scan
cannot overlap the scheduled batch.
"""
import dataclasses
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import llmclient, migration, readiness, scheduler, tagging

import helpers

app = main_module.app
_GUARD = 120   # seconds: a hang detector, never a timing the tests depend on


def _job(kind):
    return db.one("SELECT * FROM jobs WHERE kind=?", (kind,))


def _settle(items):
    """Wait for the runs a tick dispatched to finish (their futures), so the next fake-clock
    step happens after they are recorded, not while a pool thread is still scheduled out."""
    for item in items:
        fut = item.get("future")
        if fut is not None:
            fut.result(_GUARD)


def _only(kind, schedule=30):
    db.execute("UPDATE jobs SET enabled=0")
    db.execute("UPDATE jobs SET enabled=1,schedule_seconds=?,next_run_at=NULL WHERE kind=?", (schedule, kind))
    return _job(kind)


@pytest.fixture
def clock(monkeypatch):
    """A fake wall clock for db.now (what runs, leases and readiness are timed with)."""
    state = {"t": 1_700_000_000.0}
    monkeypatch.setattr(db, "now", lambda: state["t"])
    monkeypatch.setattr(readiness, "settings", dataclasses.replace(readiness.settings, scheduler_enabled=True))
    monkeypatch.setattr(readiness, "_check_openreads", lambda: {"ok": True})
    return state


@pytest.fixture
def gate(monkeypatch):
    """tag-classify blocks on an Event until the test releases it (no real sleeping). The
    waits below are only deadlock guards: they are far longer than any loaded CI runner
    needs, and teardown always sets the event, so wall-clock speed never decides a result."""
    ev = threading.Event()
    entered = threading.Event()
    real = scheduler._execute

    def slow(job):
        if job["kind"] == "tag-classify":
            entered.set()
            ev.wait(_GUARD)
            return {"considered": 1, "proposed": 0, "auto_approved": 0, "abstained": 0, "errors": 0, "swept_pending": 0, "remaining": 0}
        return real(job)
    monkeypatch.setattr(scheduler, "_execute", slow)
    yield {"release": ev, "entered": entered}
    ev.set()
    scheduler.shutdown_pool(wait=True)


# ---------- the incident, reproduced ----------
def test_old_inline_execution_starves_the_heartbeat(clock):
    """What production did: the batch ran inline in the loop's own thread, so while it
    was busy (20 x up-to-300 s LLM calls) nothing else ticked or ran. Mid-run, with
    no loop heartbeat (the old loop never wrote one), readiness fell back to the
    newest run time and failed once that was 300 s old; 1197 s was observed live."""
    _only("tag-classify", schedule=900)
    db.execute("UPDATE jobs SET enabled=1,schedule_seconds=30,next_run_at=NULL WHERE kind='download-poll'")
    job = _job("tag-classify")
    scheduler._begin_run(job, "schedule", clock["t"])   # the inline run starts ...
    clock["t"] += 1197                                   # ... and is still inside its LLM calls
    assert scheduler.heartbeat() is None
    ready, body = readiness.readiness()
    hb = body["checks"]["scheduler_heartbeat"]
    assert ready is False and hb["ok"] is False and hb["source"] == "runs"
    assert hb["last_run_age_seconds"] == 1197 and hb["allowed_seconds"] == 300
    # Meanwhile the poll that was due got no chance: the old loop only moved on
    # after run_due_jobs() returned, i.e. after the batch.
    assert db.one("SELECT COUNT(*) n FROM job_runs WHERE kind='download-poll'")["n"] == 0


# ---------- the fix ----------
def test_loop_keeps_ticking_and_other_jobs_run_while_classify_is_busy(clock, gate):
    _only("tag-classify", schedule=900)
    db.execute("UPDATE jobs SET enabled=1,schedule_seconds=30,next_run_at=NULL WHERE kind='housekeeping'")
    first = scheduler.tick()                       # dispatches both; returns at once
    assert gate["entered"].wait(_GUARD), "classify did not start in the pool"
    assert {x["kind"]: x["status"] for x in first}["tag-classify"] == "running"
    # tick() never waits for a job (that is the property); the test, not the loop, waits for
    # the short housekeeping run before moving the fake clock, so whether a pool thread got
    # CPU between two ticks cannot change which runs happen. Classify stays blocked on the gate.
    _settle([x for x in first if x["kind"] == "housekeeping"])
    # Twenty minutes pass with classify still blocked; the loop ticks every 15 s.
    for _ in range(80):
        clock["t"] += 15
        _settle([x for x in scheduler.tick() if x["kind"] == "housekeeping"])
    ready, body = readiness.readiness()
    hb = body["checks"]["scheduler_heartbeat"]
    assert ready is True and hb["ok"] is True and hb["source"] == "loop" and hb["last_tick_age_seconds"] == 0
    busy = {r["kind"]: r for r in hb["running"]}
    assert "tag-classify" in busy and busy["tag-classify"]["age_seconds"] >= 1200
    # housekeeping became due again during those ticks and ran, i.e. was not starved.
    hk = [r["status"] for r in db.all("SELECT status FROM job_runs WHERE kind='housekeeping'")]
    assert hk.count("success") >= 2 and set(hk) <= {"success", "running"} and hk.count("running") <= 1, hk
    assert db.one("SELECT status FROM job_runs WHERE kind='tag-classify'")["status"] == "running"
    gate["release"].set()
    scheduler.shutdown_pool(wait=True)
    done = db.one("SELECT status,result_json FROM job_runs WHERE kind='tag-classify'")
    assert done["status"] == "success" and '"considered":1' in done["result_json"]
    assert scheduler.running_jobs() == []


def test_one_run_per_kind_at_a_time_skips_not_queues(clock, gate):
    job = _only("tag-classify", schedule=30)
    scheduler.tick(); assert gate["entered"].wait(_GUARD)
    clock["t"] += 60                               # due again, but still running
    second = scheduler.tick()
    assert second and second[0]["status"] == "skipped" and "still running" in second[0]["error"]
    assert db.one("SELECT COUNT(*) n FROM job_runs WHERE kind='tag-classify'")["n"] == 1
    with pytest.raises(scheduler.JobBusy):
        scheduler.run_job(job["id"])              # a manual run is refused too
    with pytest.raises(scheduler.JobBusy):
        scheduler.run_exclusive("tag-classify", lambda: {})
    gate["release"].set(); scheduler.shutdown_pool(wait=True)
    assert scheduler.run_job(job["id"])["status"] == "success", "free again once finished"


def test_manual_run_route_answers_409_while_busy(clock, gate, monkeypatch):
    job = _only("tag-classify", schedule=30)
    scheduler.tick(); assert gate["entered"].wait(_GUARD)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/jobs/{job['id']}/run", json={})
        assert r.status_code == 409 and "still running" in r.json()["detail"]
        health = c.get("/api/v1/system/health").json()["scheduler"]
        assert health["running"][0]["kind"] == "tag-classify" and health["last_tick_age_seconds"] == 0
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["checks"]["scheduler_heartbeat"]["running"][0]["kind"] == "tag-classify"
        # App shutdown (leaving this block) waits for running jobs, so free classify first;
        # it used to sit out the gate's wall-clock timeout here instead.
        gate["release"].set()


def test_stuck_run_lease_expires_and_the_kind_runs_again(clock, gate, monkeypatch):
    monkeypatch.setattr(scheduler, "settings", dataclasses.replace(scheduler.settings, job_lease_seconds=600))
    job = _only("tag-classify", schedule=30)
    scheduler.tick(); assert gate["entered"].wait(_GUARD)
    stuck = db.one("SELECT id FROM job_runs WHERE kind='tag-classify'")["id"]
    clock["t"] += 601
    second = scheduler.tick()
    assert second[0]["status"] == "running", "a fresh run started after the lease expired"
    old = db.one("SELECT status,error FROM job_runs WHERE id=?", (stuck,))
    assert old["status"] == "failed" and "Lease expired" in old["error"]
    assert db.one("SELECT last_status FROM jobs WHERE id=?", (job["id"],))["last_status"] == "failed"
    # The stuck thread finishing late must not resurrect its row or clobber the new run.
    gate["release"].set(); scheduler.shutdown_pool(wait=True)
    rows = {r["id"]: r["status"] for r in db.all("SELECT id,status FROM job_runs WHERE kind='tag-classify'")}
    assert rows[stuck] == "failed" and list(rows.values()).count("success") == 1


def test_crashing_job_releases_its_kind(clock, monkeypatch):
    job = _only("housekeeping", schedule=30)

    def boom(_job):
        raise RuntimeError("kaput")
    monkeypatch.setattr(scheduler, "_execute", boom)
    out = scheduler.run_job(job["id"])
    assert out["status"] == "failed" and scheduler.running_jobs() == []
    monkeypatch.undo()
    assert scheduler.run_job(job["id"])["status"] == "success"


def test_readyz_reports_a_dead_loop_even_with_fresh_runs(clock):
    """A loop that stopped ticking is the failure the probe exists for; a run that
    happened to finish recently must not mask it once a heartbeat exists."""
    _only("download-poll", schedule=30)
    scheduler.beat()
    db.execute("UPDATE jobs SET last_run_at=? WHERE kind='download-poll'", (clock["t"],))
    clock["t"] += 1197
    db.execute("UPDATE jobs SET last_run_at=? WHERE kind='download-poll'", (clock["t"],))
    ready, body = readiness.readiness()
    hb = body["checks"]["scheduler_heartbeat"]
    assert ready is False and hb["source"] == "loop" and hb["last_tick_age_seconds"] == 1197
    scheduler.beat()
    assert readiness.readiness()[0] is True


# ---------- classifier batch: budget, timeout, no double processing ----------
@pytest.fixture
def llm(monkeypatch):
    calls = []

    def fake(system, user, **kw):
        calls.append(kw.get("timeout"))
        return {"verdict": "not_kids", "confidence": 0.9, "reason": "r"}
    monkeypatch.setattr(llmclient, "chat_json", fake)
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "m")
    return calls


def _works(n):
    out = []
    for i in range(n):
        wid = migration._find_or_create_stub({"title": f"Book {i:02d}", "author": f"Author {i:02d}", "identifiers": {}})
        helpers.own(wid); out.append(wid)
    return out


def test_batch_defaults_are_small_and_use_the_classifier_timeout(llm):
    _works(8)
    out = tagging.classify_kids_missing()
    assert out["considered"] == base_settings.classify_batch == 5 and out["remaining"] == 3
    assert llm == [base_settings.classify_llm_timeout] * 5 and base_settings.classify_llm_timeout == 120.0


def test_batch_stops_starting_books_after_the_wall_clock_budget(llm, monkeypatch):
    _works(6)
    fake = {"t": 0.0}
    monkeypatch.setattr(tagging, "_monotonic", lambda: fake["t"])
    real = llmclient.chat_json

    def slow(system, user, **kw):
        fake["t"] += 50.0                     # each call "takes" 50 s
        return real(system, user, **kw)
    monkeypatch.setattr(llmclient, "chat_json", slow)
    out = tagging.classify_kids_missing(limit=6, budget_seconds=120)
    # Calls at t=0, 50, 100 start (budget not yet spent); the 4th would start at 150: stopped.
    assert out["considered"] == 3 and out["budget_stopped"] == 3 and out["remaining"] == 3
    out = tagging.classify_kids_missing(limit=6, budget_seconds=120)
    assert out["considered"] == 3 and out["remaining"] == 0, "the rest ran on the next batch"


def test_management_scan_cannot_overlap_the_scheduled_batch(clock, gate, monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(tagging.llmclient, "enabled", lambda: True)
    _only("tag-classify", schedule=30)
    scheduler.tick(); assert gate["entered"].wait(_GUARD)
    with TestClient(app) as c:
        r = c.post("/api/v1/tags/classify-library", json={"limit": 5})
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        gate["release"].set()   # app shutdown waits for running jobs, so free classify before leaving
    scheduler.shutdown_pool(wait=True)
    # And the other way round: a scan in progress makes the scheduled batch skip.
    holder = threading.Event(); inside = threading.Event()

    def scan():
        return scheduler.run_exclusive("tag-classify", lambda: (inside.set(), holder.wait(_GUARD), {"considered": 0})[-1])
    t = threading.Thread(target=scan); t.start(); assert inside.wait(_GUARD)
    clock["t"] += 60
    assert scheduler.tick()[0]["status"] == "skipped"
    holder.set(); t.join(_GUARD)
    assert scheduler.running_jobs() == []


def test_no_work_is_classified_twice_across_batches(llm):
    ids = _works(4)
    tagging.classify_kids_missing(limit=4)
    tagging.classify_kids_missing(limit=4)
    assert len(llm) == 4
    assert db.one("SELECT COUNT(*) n FROM kids_classification")["n"] == 4
