"""Tests for Part XXVII: preferred edition selection.

A Work can have independently-set preferred ebook and audiobook editions.
Setting one does not affect the other.  The work_detail read path includes
the preferred edition objects when set, and the existing fallback behaviour
is preserved when unset.
"""
from fastapi.testclient import TestClient

from app import db, main
from app.main import app, PreferredEditionRequest
from app.services import migration

client = TestClient(app)


def _work():
    return migration._find_or_create_stub(
        {"title": "Test Book", "author": "Test Author", "identifiers": {}}
    )


def _edition(work_id: str, title: str = "Test Edition") -> str:
    eid = db.uid()
    t = db.now()
    db.execute(
        "INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (eid, work_id, title, t, t),
    )
    return eid


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

def test_set_preferred_ebook_edition():
    with TestClient(app) as c:
        work_id = _work()
        eid = _edition(work_id, "ebook-edition")
        r = c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": eid},
        )
        assert r.status_code == 200
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"]["id"] == eid
        assert detail["preferred_audiobook_edition"] is None


def test_set_preferred_audiobook_edition():
    with TestClient(app) as c:
        work_id = _work()
        eid = _edition(work_id, "audiobook-edition")
        r = c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "audiobook", "edition_id": eid},
        )
        assert r.status_code == 200
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_audiobook_edition"]["id"] == eid
        assert detail["preferred_ebook_edition"] is None


def test_set_and_clear_preferred_edition():
    with TestClient(app) as c:
        work_id = _work()
        eid = _edition(work_id, "to-clear")
        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": eid},
        )
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"]["id"] == eid

        # Clear it
        r = c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": None},
        )
        assert r.status_code == 200
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"] is None


def test_set_preferred_edition_wrong_work_is_400():
    with TestClient(app) as c:
        w1 = migration._find_or_create_stub(
            {"title": "Wrong Work 1", "author": "Author A", "identifiers": {}}
        )
        w2 = migration._find_or_create_stub(
            {"title": "Wrong Work 2", "author": "Author B", "identifiers": {}}
        )
        eid = _edition(w1, "wrong-work")
        r = c.put(
            f"/api/v1/library/{w2}/preferred-edition",
            json={"kind": "ebook", "edition_id": eid},
        )
        assert r.status_code == 400
        assert "does not belong to this work" in r.json()["detail"]


def test_set_preferred_edition_nonexistent_edition_is_400():
    with TestClient(app) as c:
        work_id = _work()
        fake_id = db.uid()
        r = c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": fake_id},
        )
        assert r.status_code == 400


def test_set_preferred_edition_work_not_found_is_404():
    with TestClient(app) as c:
        fake_id = db.uid()
        eid = db.uid()
        r = c.put(
            f"/api/v1/library/{fake_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": eid},
        )
        assert r.status_code == 404


def test_invalid_kind_is_rejected():
    with TestClient(app) as c:
        work_id = _work()
        eid = _edition(work_id)
        r = c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "video", "edition_id": eid},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Independence tests
# ---------------------------------------------------------------------------

def test_ebook_and_audiobook_preferences_are_independent():
    with TestClient(app) as c:
        work_id = _work()
        ebook_eid = _edition(work_id, "ebook-only")
        audio_eid = _edition(work_id, "audio-only")

        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": ebook_eid},
        )
        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "audiobook", "edition_id": audio_eid},
        )

        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"]["id"] == ebook_eid
        assert detail["preferred_audiobook_edition"]["id"] == audio_eid

        # Clearing ebook does not affect audiobook
        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": None},
        )
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"] is None
        assert detail["preferred_audiobook_edition"]["id"] == audio_eid


# ---------------------------------------------------------------------------
# Read-path / work_detail tests
# ---------------------------------------------------------------------------

def test_work_detail_includes_preferred_editions_when_set():
    with TestClient(app) as c:
        work_id = _work()
        ebook_eid = _edition(work_id, "pref-ebook")
        audio_eid = _edition(work_id, "pref-audio")

        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "ebook", "edition_id": ebook_eid},
        )
        c.put(
            f"/api/v1/library/{work_id}/preferred-edition",
            json={"kind": "audiobook", "edition_id": audio_eid},
        )

        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"]["title"] == "pref-ebook"
        assert detail["preferred_audiobook_edition"]["title"] == "pref-audio"


def test_work_detail_returns_null_when_no_preference_set():
    with TestClient(app) as c:
        work_id = _work()
        detail = c.get(f"/api/v1/library/{work_id}").json()
        assert detail["preferred_ebook_edition"] is None
        assert detail["preferred_audiobook_edition"] is None


# ---------------------------------------------------------------------------
# Direct function tests (no HTTP)
# ---------------------------------------------------------------------------

def test_set_preferred_edition_direct_function():
    work_id = _work()
    eid = _edition(work_id, "direct-test")
    result = main.set_preferred_edition(
        work_id,
        PreferredEditionRequest(kind="ebook", edition_id=eid),
        request=None,
        _=None,
        user={"id": "test", "access_scope": "full"},
    )
    assert result["preferred_ebook_edition"]["id"] == eid


def test_set_preferred_edition_direct_clear():
    work_id = _work()
    eid = _edition(work_id, "direct-clear")
    main.set_preferred_edition(
        work_id,
        PreferredEditionRequest(kind="ebook", edition_id=eid),
        request=None,
        _=None,
        user={"id": "test", "access_scope": "full"},
    )
    main.set_preferred_edition(
        work_id,
        PreferredEditionRequest(kind="ebook", edition_id=None),
        request=None,
        _=None,
        user={"id": "test", "access_scope": "full"},
    )
    detail = main.work_detail(work_id, user={"id": "test", "access_scope": "full"})
    assert detail["preferred_ebook_edition"] is None
