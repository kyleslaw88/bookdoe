"""Maintenance issue store, detector lifecycle, API and scan job (P5-18, Part XXV)."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import maintenance, scheduler
from app.services.maintenance import Finding

app = main_module.app


@pytest.fixture
def stub(monkeypatch):
    """A test-only detector whose findings the test controls (restored on teardown)."""
    state = {"findings": [Finding("stub_kind", "warning", "work", "w1", detail={"n": 1})], "truncate": False}

    def fn(ctx):
        yield from state["findings"]
        if state["truncate"]:
            ctx.truncated = True

    monkeypatch.setitem(maintenance.DETECTORS, "stub", maintenance.Detector("stub", ("stub_kind",), fn))
    return state


def _issues(**where):
    clause = " AND ".join(f"{k}=?" for k in where) or "1"
    return db.all(f"SELECT * FROM maintenance_issues WHERE {clause} ORDER BY fingerprint", tuple(where.values()))  # nosec B608


def _events(issue_id):
    return [e["event"] for e in db.all("SELECT event FROM maintenance_events WHERE issue_id=? ORDER BY created_at, rowid", (issue_id,))]


def test_finding_creates_one_open_issue_and_rerun_refreshes_it(stub, monkeypatch):
    clock = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr(db, "now", lambda: next(clock))
    r = maintenance.run_detector("stub")
    assert r["complete"] and r["opened"] == 1
    (issue,) = _issues(kind="stub_kind")
    assert issue["status"] == "open" and issue["fingerprint"] == "stub_kind|work|w1|"
    assert issue["first_seen"] == issue["last_seen"] == 100.0

    stub["findings"] = [Finding("stub_kind", "error", "work", "w1", detail={"n": 2})]  # detail/severity change, same identity
    r = maintenance.run_detector("stub")
    assert r["opened"] == 0 and r["refreshed"] == 1
    (again,) = _issues(kind="stub_kind")
    assert again["id"] == issue["id"] and again["first_seen"] == 100.0 and again["last_seen"] == 200.0
    assert again["severity"] == "error" and db.json_load(again["detail_json"], {}) == {"n": 2}
    assert _events(issue["id"]) == ["detected"]


def test_complete_run_auto_resolves_but_truncated_run_does_not(stub):
    maintenance.run_detector("stub")
    (issue,) = _issues(kind="stub_kind")
    stub["findings"] = []
    stub["truncate"] = True
    r = maintenance.run_detector("stub")
    assert r["complete"] is False and r["auto_resolved"] == 0
    assert _issues(kind="stub_kind")[0]["status"] == "open"

    stub["truncate"] = False
    r = maintenance.run_detector("stub")
    assert r["complete"] and r["auto_resolved"] == 1
    resolved = _issues(kind="stub_kind")[0]
    assert resolved["status"] == "resolved" and resolved["resolved_at"] is not None
    assert _events(issue["id"]) == ["detected", "auto_resolved"]


def test_limit_budget_truncates_and_never_resolves(stub):
    stub["findings"] = [Finding("stub_kind", "info", "work", f"w{i}") for i in range(3)]
    r = maintenance.run_detector("stub", limit=2)
    assert r["complete"] is False and r["found"] == 2 and r["opened"] == 2
    stub["findings"] = []
    assert maintenance.run_detector("stub", limit=2)["auto_resolved"] == 2  # complete run: both gone
    # Exactly at the limit is still complete.
    stub["findings"] = [Finding("stub_kind", "info", "work", f"w{i}") for i in range(2)]
    assert maintenance.run_detector("stub", limit=2)["complete"] is True


def test_zero_limit_is_an_empty_budget_not_the_default(stub):
    maintenance.run_detector("stub")  # one open issue from the default stub finding
    r = maintenance.run_detector("stub", limit=0)
    assert r["complete"] is False and r["found"] == 0 and r["auto_resolved"] == 0
    assert [i["status"] for i in _issues(kind="stub_kind")] == ["open"]  # a truncated run resolves nothing
    assert maintenance.run_detector("stub", limit=None)["complete"] is True  # None falls back to the detector's budget


def test_registering_a_kind_another_detector_owns_is_rejected(monkeypatch):
    monkeypatch.setattr(maintenance, "DETECTORS", dict(maintenance.DETECTORS))
    with pytest.raises(ValueError, match="job_failing.*failing-jobs"):
        maintenance.register_detector("other-jobs", ("job_failing",), lambda ctx: [])
    assert "other-jobs" not in maintenance.DETECTORS
    # Re-registering the same name (a module reload) keeps its kinds.
    again = maintenance.register_detector("failing-jobs", ("job_failing",), maintenance._failing_jobs, limit=200)
    assert maintenance.DETECTORS["failing-jobs"] is again


def test_time_budget_truncates(stub):
    r = maintenance.run_detector("stub", seconds=-1)
    assert r["complete"] is False and r["found"] == 0 and _issues() == []


def test_crashing_detector_is_incomplete_and_resolves_nothing(stub, monkeypatch):
    maintenance.run_detector("stub")

    def boom(ctx):
        raise RuntimeError("disk gone")
        yield  # pragma: no cover

    monkeypatch.setitem(maintenance.DETECTORS, "stub", maintenance.Detector("stub", ("stub_kind",), boom))
    r = maintenance.run_detector("stub")
    assert r["complete"] is False and "disk gone" in r["error"]
    assert _issues(kind="stub_kind")[0]["status"] == "open"


def test_undeclared_kind_is_rejected(stub):
    stub["findings"] = [Finding("other_kind", "info", "work", "w1")]
    r = maintenance.run_detector("stub")
    assert r["complete"] is False and "undeclared kind" in r["error"] and _issues() == []


def test_dismissed_stays_dismissed_and_resolved_reopens(stub):
    maintenance.run_detector("stub")
    (issue,) = _issues(kind="stub_kind")
    maintenance.dismiss(issue["id"], "u1")
    maintenance.run_detector("stub")
    assert _issues(kind="stub_kind")[0]["status"] == "dismissed"
    stub["findings"] = []
    maintenance.run_detector("stub")
    assert _issues(kind="stub_kind")[0]["status"] == "dismissed"  # not auto-resolved either

    # A different issue: resolved, then recurring -> reopened.
    stub["findings"] = [Finding("stub_kind", "info", "work", "w2")]
    maintenance.run_detector("stub")
    stub["findings"] = []
    maintenance.run_detector("stub")
    (w2,) = _issues(fingerprint="stub_kind|work|w2|")
    assert w2["status"] == "resolved"
    stub["findings"] = [Finding("stub_kind", "info", "work", "w2")]
    r = maintenance.run_detector("stub")
    w2 = _issues(fingerprint="stub_kind|work|w2|")[0]
    assert r["reopened"] == 1 and w2["status"] == "open" and w2["resolved_at"] is None
    assert _events(w2["id"]) == ["detected", "auto_resolved", "reopened"]


def test_acknowledge_and_dismiss_write_events_and_guard_transitions(stub):
    maintenance.run_detector("stub")
    (issue,) = _issues(kind="stub_kind")
    got = maintenance.acknowledge(issue["id"], "u1")
    assert got["status"] == "acknowledged"
    maintenance.acknowledge(issue["id"], "u1")  # repeat: no second event
    got = maintenance.dismiss(issue["id"], "u2")
    assert got["status"] == "dismissed"
    assert [(e["event"], e["user_id"]) for e in got["events"]] == [("detected", None), ("acknowledged", "u1"), ("dismissed", "u2")]
    with pytest.raises(maintenance.IssueConflict):
        maintenance.acknowledge(issue["id"], "u1")
    assert maintenance.dismiss("nope") is None


def test_acknowledged_issue_is_auto_resolved_when_gone(stub):
    maintenance.run_detector("stub")
    (issue,) = _issues(kind="stub_kind")
    maintenance.acknowledge(issue["id"])
    stub["findings"] = []
    maintenance.run_detector("stub")
    assert _issues(kind="stub_kind")[0]["status"] == "resolved"


def test_run_is_refused_while_another_run_is_active(stub):
    assert maintenance._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(maintenance.MaintenanceBusy):
            maintenance.run_all()
        assert maintenance.scan_job()["skipped"] is True
    finally:
        maintenance._run_lock.release()


def test_builtin_failing_jobs_detector_follows_the_latest_run():
    job = db.one("SELECT * FROM jobs WHERE kind='housekeeping'")
    with db.tx() as c:
        c.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at,error) VALUES ('r1',?,?,'failed',?, 'boom')", (job["id"], job["kind"], db.now()))
    assert maintenance.run_detector("failing-jobs")["opened"] == 1
    (issue,) = _issues(kind="job_failing")
    assert issue["subject_id"] == job["id"] and db.json_load(issue["detail_json"], {})["error"] == "boom"
    with db.tx() as c:
        c.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('r2',?,?,'success',?)", (job["id"], job["kind"], db.now() + 1))
    assert maintenance.run_detector("failing-jobs")["auto_resolved"] == 1


# ---------- job ----------

def test_maintenance_scan_job_is_seeded_and_listed_by_the_api():
    job = db.one("SELECT * FROM jobs WHERE kind='maintenance-scan'")
    assert job["enabled"] == 1 and job["schedule_seconds"] == 86400
    items = TestClient(app).get("/api/v1/jobs").json()["items"]
    assert any(j["kind"] == "maintenance-scan" for j in items)


def test_scan_job_records_per_detector_counts_in_the_run(stub):
    job = db.one("SELECT * FROM jobs WHERE kind='maintenance-scan'")
    out = scheduler.run_job(job["id"])
    assert out["status"] == "success"
    run = db.one("SELECT * FROM job_runs WHERE id=?", (out["run_id"],))
    result = db.json_load(run["result_json"], {})
    assert result["complete"] is True
    assert result["detectors"]["stub"]["opened"] == 1 and "failing-jobs" in result["detectors"]
    assert _issues(kind="stub_kind")[0]["status"] == "open"


# ---------- API ----------

@pytest.fixture
def household(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert r.status_code == 200, r.text
        csrf = r.json()["csrf"]
        r = admin.post("/api/v1/users", json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"},
                       headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
        with TestClient(app) as kid:
            r = kid.post("/api/v1/auth/login", json={"username": "junior", "password": "a-very-long-kid-password"})
            assert r.status_code == 200, r.text
            yield {"admin": admin, "kid": kid, "h": {"X-CSRF-Token": csrf}, "kid_h": {"X-CSRF-Token": r.json()["csrf"]}}


def test_issue_filters_pagination_detail_and_summary(stub, household):
    admin = household["admin"]
    stub["findings"] = [Finding("stub_kind", "warning" if i % 2 else "error", "work", f"w{i}") for i in range(5)]
    r = admin.post("/api/v1/maintenance/run", json={"detectors": ["stub"]}, headers=household["h"])
    assert r.status_code == 200 and r.json()["complete"] is True and r.json()["detectors"]["stub"]["opened"] == 5

    page = admin.get("/api/v1/maintenance/issues", params={"kind": "stub_kind", "limit": 2, "offset": 0}).json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["items"][0]["detail"] == {}
    rest = admin.get("/api/v1/maintenance/issues", params={"kind": "stub_kind", "limit": 2, "offset": 4}).json()
    assert len(rest["items"]) == 1
    ids = {i["id"] for o in (0, 2, 4) for i in admin.get("/api/v1/maintenance/issues", params={"kind": "stub_kind", "limit": 2, "offset": o}).json()["items"]}
    assert len(ids) == 5
    assert admin.get("/api/v1/maintenance/issues", params={"severity": "error"}).json()["total"] == 3
    assert admin.get("/api/v1/maintenance/issues", params={"kind": "nope"}).json()["total"] == 0
    assert admin.get("/api/v1/maintenance/issues", params={"status": "bogus"}).status_code == 422

    iid = page["items"][0]["id"]
    assert admin.post(f"/api/v1/maintenance/issues/{iid}/acknowledge", headers=household["h"]).json()["status"] == "acknowledged"
    detail = admin.post(f"/api/v1/maintenance/issues/{iid}/dismiss", headers=household["h"]).json()
    assert detail["status"] == "dismissed" and [e["event"] for e in detail["events"]] == ["detected", "acknowledged", "dismissed"]
    assert detail["events"][-1]["user_id"]
    assert admin.get(f"/api/v1/maintenance/issues/{iid}").json()["events"] == detail["events"]
    assert admin.post(f"/api/v1/maintenance/issues/{iid}/acknowledge", headers=household["h"]).status_code == 409
    assert admin.get("/api/v1/maintenance/issues/nope").status_code == 404
    assert admin.post("/api/v1/maintenance/issues/nope/dismiss", headers=household["h"]).status_code == 404

    assert admin.get("/api/v1/maintenance/issues", params={"status": "dismissed"}).json()["total"] == 1
    s = admin.get("/api/v1/maintenance/summary").json()
    assert s["by_kind"]["stub_kind"] == {"open": 4, "dismissed": 1} and s["by_status"]["open"] == 4 and s["total"] == 5


def test_writes_need_csrf_and_unknown_detector_is_400(stub, household):
    admin = household["admin"]
    assert admin.post("/api/v1/maintenance/run", json={}).status_code == 403
    assert admin.post("/api/v1/maintenance/run", json={"detectors": ["nope"]}, headers=household["h"]).status_code == 400
    assert admin.post("/api/v1/maintenance/run", headers=household["h"]).json()["detectors"].keys() >= {"stub", "failing-jobs"}


def test_kids_and_anonymous_callers_get_403(stub, household):
    maintenance.run_detector("stub")
    (issue,) = _issues(kind="stub_kind")
    kid, h = household["kid"], household["kid_h"]
    for path in ("/api/v1/maintenance/issues", f"/api/v1/maintenance/issues/{issue['id']}", "/api/v1/maintenance/summary"):
        assert kid.get(path).status_code == 403, path
    for path, body in ((f"/api/v1/maintenance/issues/{issue['id']}/acknowledge", None), (f"/api/v1/maintenance/issues/{issue['id']}/dismiss", None),
                       ("/api/v1/maintenance/run", {})):
        assert kid.post(path, json=body, headers=h).status_code == 403, path
    assert _issues(kind="stub_kind")[0]["status"] == "open"
    assert TestClient(app).get("/api/v1/maintenance/issues").status_code in (401, 403)
