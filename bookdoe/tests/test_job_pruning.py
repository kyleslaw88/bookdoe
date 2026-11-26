"""A4: job_runs/history pruning and no-op download-poll runs."""
from app import db
from app.services import downloads, scheduler

DAY = 86400


def _poll_job():
    return db.one("SELECT * FROM jobs WHERE kind='download-poll'")


def _runs(job_id):
    return db.all("SELECT * FROM job_runs WHERE job_id=?", (job_id,))


def test_noop_poll_leaves_no_run_row_but_updates_job(monkeypatch):
    job = _poll_job()
    monkeypatch.setattr(downloads, "poll_all", lambda: {"updated": 0, "completed": 0, "imported": 0, "errors": []})
    out = scheduler.run_job(job["id"], trigger="schedule")
    assert out["status"] == "success"
    assert _runs(job["id"]) == []
    row = db.one("SELECT last_run_at,last_status FROM jobs WHERE id=?", (job["id"],))
    assert row["last_status"] == "success" and row["last_run_at"] is not None


def test_active_or_erroring_poll_is_recorded(monkeypatch):
    job = _poll_job()
    monkeypatch.setattr(downloads, "poll_all", lambda: {"updated": 1, "completed": 0, "imported": 0, "errors": []})
    scheduler.run_job(job["id"])
    monkeypatch.setattr(downloads, "poll_all", lambda: {"updated": 0, "completed": 0, "imported": 0, "errors": [{"client": "x", "error": "down"}]})
    scheduler.run_job(job["id"])
    assert len(_runs(job["id"])) == 2


def test_other_job_kinds_are_never_dropped():
    job = db.one("SELECT * FROM jobs WHERE kind='housekeeping'")
    scheduler.run_job(job["id"])
    assert len(_runs(job["id"])) == 1


def _seed_runs(job_id, n, age_days, kind="x"):
    now = db.now()
    with db.tx() as conn:
        for i in range(n):
            conn.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES (?,?,?,'success',?)",
                         (f"{job_id}-{age_days}-{i}", job_id, kind, now - age_days * DAY - i))


def test_prune_keeps_last_50_per_job_and_all_recent():
    a, b = _poll_job()["id"], db.one("SELECT id FROM jobs WHERE kind='housekeeping'")["id"]
    _seed_runs(a, 120, 40)  # old, plenty: only the newest 50 survive
    _seed_runs(a, 5, 1)     # recent: all survive, and they count toward the 50
    _seed_runs(b, 10, 60)   # old but fewer than 50: all survive
    db.execute("INSERT INTO job_runs(id,job_id,kind,status,started_at) VALUES ('live',?,'x','running',?)", (a, db.now() - 90 * DAY))
    removed = scheduler.prune_job_runs()
    assert removed == 120 - 45 and len(_runs(a)) == 5 + 45 + 1
    assert len(_runs(b)) == 10
    assert db.one("SELECT 1 x FROM job_runs WHERE id='live'")


def test_prune_history_spares_recent_and_auth_events():
    now = db.now()
    for i, (etype, age) in enumerate([("download.completed", 100), ("download.completed", 5), ("auth.api_key.created", 400), ("auth.account_linked", 100)]):
        db.execute("INSERT INTO history(id,event_type,message,created_at) VALUES (?,?,?,?)", (f"h{i}", etype, "m", now - age * DAY))
    assert scheduler.prune_history() == 1
    assert {r["id"] for r in db.all("SELECT id FROM history")} == {"h1", "h2", "h3"}


def test_housekeeping_reports_pruning():
    a = _poll_job()["id"]
    _seed_runs(a, 60, 45)
    out = scheduler._housekeeping()
    assert out["job_runs_pruned"] == 10 and out["history_pruned"] == 0
