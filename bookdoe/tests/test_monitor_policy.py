"""Tests for AuthorMonitorRequest and SeriesMonitorRequest unknown-field rejection.

Per issue #2 (F-01): both request models must reject unknown fields so a
malformed body cannot silently change monitoring behaviour.
"""
from fastapi.testclient import TestClient
from app.main import app


# ---------------------------------------------------------------------------
# Author monitor
# ---------------------------------------------------------------------------

def test_author_monitor_unknown_field_returns_422():
    """A body with an unknown field must be rejected with 422."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/authors/00000000-0000-0000-0000-000000000001/monitor",
            json={"monitored": False},
        )
        assert r.status_code == 422
        body = r.json()
        assert "detail" in body


def test_author_monitor_valid_body_succeeds():
    """A valid body with only known fields must not return 422 (model parsed)."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/authors/00000000-0000-0000-0000-000000000001/monitor",
            json={"policy": "all", "ebook": True, "audiobook": False},
        )
        # 200 = author exists and was updated; 400 = author not found (service error).
        # Either is fine -- the important thing is we did NOT get 422 (model rejected it).
        assert r.status_code in (200, 400)
        assert r.status_code != 422


def test_author_monitor_minimal_valid_body_succeeds():
    """Minimal valid body (all fields have defaults) must not return 422."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/authors/00000000-0000-0000-0000-000000000001/monitor",
            json={},
        )
        assert r.status_code in (200, 400)
        assert r.status_code != 422


def test_author_monitor_extra_field_rejected():
    """An extra field like 'foo' must be rejected."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/authors/00000000-0000-0000-0000-000000000001/monitor",
            json={"policy": "all", "foo": "bar"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Series monitor
# ---------------------------------------------------------------------------

def test_series_monitor_unknown_field_returns_422():
    """A body with an unknown field must be rejected with 422."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/series/00000000-0000-0000-0000-000000000001/monitor",
            json={"monitored": False},
        )
        assert r.status_code == 422
        body = r.json()
        assert "detail" in body


def test_series_monitor_valid_body_succeeds():
    """A valid body with only known fields must not return 422 (model parsed)."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/series/00000000-0000-0000-0000-000000000001/monitor",
            json={"policy": "all", "ebook": True, "audiobook": False},
        )
        # 200 = series exists and was updated; 400 = series not found.
        # Either is fine -- the important thing is we did NOT get 422.
        assert r.status_code in (200, 400)
        assert r.status_code != 422


def test_series_monitor_minimal_valid_body_succeeds():
    """Minimal valid body (all fields have defaults) must not return 422."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/series/00000000-0000-0000-0000-000000000001/monitor",
            json={},
        )
        assert r.status_code in (200, 400)
        assert r.status_code != 422


def test_series_monitor_extra_field_rejected():
    """An extra field like 'foo' must be rejected."""
    with TestClient(app) as c:
        r = c.put(
            "/api/v1/series/00000000-0000-0000-0000-000000000001/monitor",
            json={"policy": "all", "foo": "bar"},
        )
        assert r.status_code == 422
