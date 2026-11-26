"""Permanently dismissing a review item that will never get a confident match."""
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def _review_task(title="Some Book"):
    tid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,'metadata_match','work',NULL,?,'{}', 'open',50,?)",
            (tid, title, db.now()),
        )
    return tid


def test_ignoring_a_task_removes_it_from_the_open_list():
    tid = _review_task()
    before = client.get("/api/v1/review").json()["total"]
    r = client.post(f"/api/v1/review/{tid}/ignore")
    assert r.status_code == 200
    after = client.get("/api/v1/review").json()["total"]
    assert after == before - 1
    assert db.one("SELECT status,resolved_at FROM review_tasks WHERE id=?", (tid,))["status"] == "ignored"


def test_ignoring_an_already_resolved_task_is_404():
    tid = _review_task()
    client.post(f"/api/v1/review/{tid}/ignore")
    assert client.post(f"/api/v1/review/{tid}/ignore").status_code == 404


def test_ignoring_a_missing_task_is_404():
    assert client.post("/api/v1/review/does-not-exist/ignore").status_code == 404
