"""Contract tests for the tag assignment endpoint.

Proves that POST /api/v1/library/{id}/tags accepts a JSON body with
`tag_id` (a UUID string) and rejects a body with `tag` (a name string).

These are an external client contract tests: they pin the request shape the
orchestrator depends on (spec Part III, area A05a).
"""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration, tagging

import helpers


def _make_work(title: str = "Contract Test Book") -> str:
    """Insert a work row into the test database."""
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'resolved',?,?)",
            (wid, title, title, t, t),
        )
    return wid


def test_assign_tag_accepts_tag_id():
    """POST /library/{id}/tags with {tag_id} succeeds and returns updated tags."""
    work_id = _make_work()
    helpers.own(work_id)

    # Create a tag and get its ID
    tag_id = tagging.create_tag("contract-test-tag")

    with TestClient(app) as c:
        r = c.post(f"/api/v1/library/{work_id}/tags", json={"tag_id": tag_id})
        assert r.status_code == 200
        body = r.json()
        assert any(t["id"] == tag_id for t in body["items"])

        # Clean up
        tagging.remove_tag(work_id, tag_id)
        tagging.delete_tag(tag_id)


def test_assign_tag_rejects_tag_name():
    """POST /library/{id}/tags with {tag: "name"} must be rejected (422)."""
    work_id = _make_work()
    helpers.own(work_id)

    with TestClient(app) as c:
        r = c.post(f"/api/v1/library/{work_id}/tags", json={"tag": "some-name"})
        assert r.status_code == 422


def test_assign_tag_rejects_missing_body():
    """POST /library/{id}/tags with no JSON body must be rejected (422)."""
    work_id = _make_work()
    helpers.own(work_id)

    with TestClient(app) as c:
        r = c.post(f"/api/v1/library/{work_id}/tags")
        assert r.status_code == 422


def test_assign_tag_rejects_invalid_uuid():
    """POST /library/{id}/tags with a non-UUID tag_id must be rejected (400)."""
    work_id = _make_work()
    helpers.own(work_id)

    with TestClient(app) as c:
        r = c.post(f"/api/v1/library/{work_id}/tags", json={"tag_id": "not-a-uuid"})
        # The tag_id is validated as a UUID string; invalid UUIDs raise ValueError -> 400
        assert r.status_code == 400


def test_assign_tag_twice_is_idempotent():
    """Assigning the same tag twice does not create duplicates."""
    work_id = _make_work()
    helpers.own(work_id)
    tag_id = tagging.create_tag("idempotent-tag")

    with TestClient(app) as c:
        r1 = c.post(f"/api/v1/library/{work_id}/tags", json={"tag_id": tag_id})
        assert r1.status_code == 200
        count1 = len(r1.json()["items"])

        r2 = c.post(f"/api/v1/library/{work_id}/tags", json={"tag_id": tag_id})
        assert r2.status_code == 200
        count2 = len(r2.json()["items"])
        assert count1 == count2, "Duplicate tag assignment should not increase count"

        # Clean up
        tagging.remove_tag(work_id, tag_id)
        tagging.delete_tag(tag_id)
