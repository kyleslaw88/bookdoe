"""Configurable conservative auto-match thresholds (Part XIX).

Tests for the matching settings API and the scanner's use of configurable
thresholds.  Default behaviour must be identical to the hard-coded values
that existed before this task.
"""
import dataclasses
import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module, db
from app import main as main_module
from app.config import settings as base_settings
from app.services import scanner

import helpers

app = main_module.app
client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixture: restore matching settings after every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_matching_settings(monkeypatch):
    """Restore matching.* settings to defaults after each test so no test
    leaks state into the next one.  Also restores auth_mode to 'none'
    (the conftest default) so the synthetic identity is always admin."""
    monkeypatch.setattr(main_module, "settings", base_settings)
    monkeypatch.setattr(auth_module, "settings", base_settings)
    yield
    # Teardown: clear matching.* rows so defaults apply on next read
    with db.tx() as conn:
        conn.execute("DELETE FROM settings WHERE key IN ('matching.auto_accept_threshold','matching.probable_threshold')")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_resolve(confidence, has_match=True, has_candidates=False):
    """Build a resolver response with the given confidence."""
    if has_match:
        return {"match": {"id": "resolver-1", "title": "X"}, "confidence": confidence}
    if has_candidates:
        return {"candidates": [{"title": "X"}], "confidence": confidence}
    return {}


def _set_local_mode(monkeypatch):
    """Swap in auth_mode='local' so require_admin actually checks is_admin."""
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


# ---------------------------------------------------------------------------
# Defaults – regression: unchanged behaviour
# ---------------------------------------------------------------------------

def test_defaults_auto_accept_is_095():
    """With defaults, a resolver result at 0.949 is ambiguous and at 0.95 is matched."""
    assert scanner.DEFAULT_AUTO_ACCEPT_THRESHOLD == 0.95
    assert scanner._auto_accept_threshold() == 0.95


def test_defaults_probable_is_085():
    assert scanner.DEFAULT_PROBABLE_THRESHOLD == 0.85
    assert scanner._probable_threshold() == 0.85


# ---------------------------------------------------------------------------
# F3: Real resolver results – ambiguous at 0.949, matched at 0.95/0.96
# ---------------------------------------------------------------------------

def test_0949_resolver_is_ambiguous_by_default(monkeypatch):
    """Criterion 1: 0.949 → below threshold, no candidates → ambiguous.

    The resolver returns a match below the auto_accept threshold WITHOUT
    candidates.  The code should return 'ambiguous' (not fall through to
    title matching).
    """
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: {
        "match": {"id": "resolver-1", "title": "X"}, "confidence": 0.949,
    })
    match = scanner.match_detected({"identifiers": {}, "title": "X"})
    assert match["status"] == "ambiguous"
    assert match["confidence"] == 0.949
    assert "resolver match below auto_accept threshold" in match["reasons"][0]


def test_095_resolver_is_matched_by_default(monkeypatch):
    """Criterion 1: 0.95 → matched (at default 0.95)."""
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: _stub_resolve(0.95))
    monkeypatch.setattr(scanner.metadata, "upsert_work", lambda *a, **kw: "stub-work-id")
    match = scanner.match_detected({"identifiers": {}, "title": "X"})
    assert match["status"] == "matched"


def test_096_resolver_is_matched_by_default(monkeypatch):
    """Regression: 0.96 still matched with defaults."""
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: _stub_resolve(0.96))
    monkeypatch.setattr(scanner.metadata, "upsert_work", lambda *a, **kw: "stub-work-id")
    match = scanner.match_detected({"identifiers": {}, "title": "X"})
    assert match["status"] == "matched"


# ---------------------------------------------------------------------------
# Changing auto_accept_threshold
# ---------------------------------------------------------------------------

def test_setting_auto_accept_to_098_makes_096_ambiguous(monkeypatch):
    """Criterion 2: auto_accept 0.98 turns a 0.96 resolver result into ambiguous."""
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.98,
        "probable_threshold": 0.85,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"
    # Resolver returns a match below threshold (no candidates) → ambiguous
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kwargs: {
        "match": {"id": "resolver-1", "title": "X"}, "confidence": 0.96,
    })
    match = scanner.match_detected({"identifiers": {}, "title": "X"})
    assert match["status"] == "ambiguous"


def test_get_matching_settings_returns_defaults():
    """Criterion 4: GET returns current values and defaults."""
    # Clear any stored values so defaults apply
    with db.tx() as conn:
        conn.execute("DELETE FROM settings WHERE key IN ('matching.auto_accept_threshold','matching.probable_threshold')")
    r = client.get("/api/v1/settings/matching")
    assert r.status_code == 200
    data = r.json()
    assert data["auto_accept_threshold"] == 0.95
    assert data["probable_threshold"] == 0.85


def test_put_matching_settings_persists_and_returns():
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.99,
        "probable_threshold": 0.80,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"
    data = r.json()
    assert data["auto_accept_threshold"] == 0.99
    assert data["probable_threshold"] == 0.80
    # Verify it persisted
    r2 = client.get("/api/v1/settings/matching")
    assert r2.json()["auto_accept_threshold"] == 0.99


# ---------------------------------------------------------------------------
# Validation – PUT rejects invalid values
# ---------------------------------------------------------------------------

def test_put_auto_accept_below_090_returns_400():
    """Criterion 3: PUT with auto_accept 0.5 returns 400."""
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.5,
        "probable_threshold": 0.50,
    })
    assert r.status_code == 400
    assert "auto_accept_threshold" in r.json()["detail"].lower()


def test_put_probable_ge_auto_accept_returns_400():
    """Criterion 3: PUT with probable >= auto_accept returns 400."""
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.95,
        "probable_threshold": 0.95,
    })
    assert r.status_code == 400
    assert "probable_threshold" in r.json()["detail"].lower()


def test_put_probable_above_auto_accept_returns_400():
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.90,
        "probable_threshold": 0.95,
    })
    assert r.status_code == 400


def test_put_auto_accept_above_10_returns_400():
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 1.1,
        "probable_threshold": 0.85,
    })
    assert r.status_code == 400


def test_put_probable_below_050_returns_400():
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.95,
        "probable_threshold": 0.49,
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# F1: Auth – non-admin PUT returns 403; missing CSRF returns 403
# ---------------------------------------------------------------------------

def test_non_admin_put_matching_returns_403(monkeypatch):
    """Criterion 4: A non-admin PUT returns 403.

    We set auth_mode='local', create a non-admin user, log in as that user,
    and send PUT /api/v1/settings/matching.  The route uses auth.require_admin,
    so the response must be 403.
    """
    _set_local_mode(monkeypatch)

    # Create a non-admin user
    with TestClient(app) as admin_client:
        r = admin_client.post("/api/v1/auth/setup", json={
            "username": "admin", "password": "a-very-long-password"
        })
        assert r.status_code == 200, r.text
        admin_csrf = r.json()["csrf"]

        r = admin_client.post(
            "/api/v1/users",
            json={"username": "reader", "password": "a-very-long-password", "access_scope": "full"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert r.status_code == 200, r.text

    # Log in as non-admin
    with TestClient(app) as non_admin:
        r = non_admin.post("/api/v1/auth/login", json={
            "username": "reader", "password": "a-very-long-password"
        })
        assert r.status_code == 200, r.text
        csrf = r.json()["csrf"]

        # PUT as non-admin → 403
        r = non_admin.put(
            "/api/v1/settings/matching",
            json={"auto_accept_threshold": 0.98, "probable_threshold": 0.85},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


def test_missing_csrf_put_matching_returns_403(monkeypatch):
    """A PUT without a CSRF token returns 403 (auth.require_csrf).

    With auth_mode='none' the CSRF check is bypassed (require_csrf returns early
    for _auth_method='none').  We must use auth_mode='local' to exercise the
    real CSRF gate.
    """
    _set_local_mode(monkeypatch)

    # Set up the admin user first
    with TestClient(app) as admin_client:
        r = admin_client.post("/api/v1/auth/setup", json={
            "username": "admin", "password": "a-very-long-password"
        })
        assert r.status_code == 200, r.text

    # Now log in and try PUT without CSRF
    with TestClient(app) as sess:
        r = sess.post("/api/v1/auth/login", json={
            "username": "admin", "password": "a-very-long-password"
        })
        assert r.status_code == 200, r.text
        # Deliberately NOT sending X-CSRF-Token
        r = sess.put("/api/v1/settings/matching", json={
            "auto_accept_threshold": 0.98,
            "probable_threshold": 0.85,
        })
        assert r.status_code == 403, f"Expected 403 for missing CSRF, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# F2: Title-only match keeps fixed confidence (0.85), not the setting
# ---------------------------------------------------------------------------

def test_title_only_confidence_is_fixed_085(monkeypatch):
    """F2: Title-only probable keeps confidence 0.85 regardless of probable_threshold."""
    # Seed a work so title-only matching has a target
    wid = _seed_work_title_only("Unique Title")

    # With default probable_threshold
    match = scanner.match_detected({"identifiers": {}, "title": "Unique Title"})
    assert match["status"] == "probable"
    assert match["confidence"] == 0.85, f"Expected 0.85, got {match['confidence']}"

    # With a very high probable_threshold setting (must be < auto_accept)
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.99,
        "probable_threshold": 0.98,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"

    match2 = scanner.match_detected({"identifiers": {}, "title": "Unique Title"})
    assert match2["status"] == "probable"
    assert match2["confidence"] == 0.85, f"Expected 0.85 even with high setting, got {match2['confidence']}"


def test_title_only_stays_probable_under_every_valid_setting(monkeypatch):
    """Criterion 5: Title-only match stays 'probable' under every valid setting."""
    wid = _seed_work_title_only("Unique Title")

    # Default settings
    match = scanner.match_detected({"identifiers": {}, "title": "Unique Title"})
    assert match["status"] == "probable", f"Expected 'probable', got '{match['status']}'"

    # Test with lowered auto_accept
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.90,
        "probable_threshold": 0.80,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"
    match2 = scanner.match_detected({"identifiers": {}, "title": "Unique Title"})
    assert match2["status"] == "probable", f"Expected 'probable' with lowered thresholds, got '{match2['status']}'"

    # Test with raised auto_accept
    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 1.0,
        "probable_threshold": 0.90,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"
    match3 = scanner.match_detected({"identifiers": {}, "title": "Unique Title"})
    assert match3["status"] == "probable", f"Expected 'probable' with raised thresholds, got '{match3['status']}'"


# ---------------------------------------------------------------------------
# F5: Settings listing pattern – /settings/import includes matching
# ---------------------------------------------------------------------------

def test_settings_import_includes_matching():
    """The /settings/import listing includes a 'matching' sub-object."""
    r = client.get("/api/v1/settings/import")
    assert r.status_code == 200
    data = r.json()
    assert "matching" in data
    assert "auto_accept_threshold" in data["matching"]
    assert "probable_threshold" in data["matching"]
    assert data["matching"]["auto_accept_threshold"] == 0.95
    assert data["matching"]["probable_threshold"] == 0.85


def test_invalid_stored_value_falls_back_to_default(monkeypatch):
    """Corrupt stored values fall back to defaults instead of crashing."""
    # Store a corrupt value
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("matching.auto_accept_threshold", '"not-a-number"', db.now()),
        )
    # The function should fall back to the default, not raise
    assert scanner._auto_accept_threshold() == 0.95


def test_invalid_probable_stored_value_falls_back_to_default(monkeypatch):
    """Corrupt probable_threshold falls back to default."""
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("matching.probable_threshold", "null", db.now()),
        )
    assert scanner._probable_threshold() == 0.85


def test_out_of_range_auto_accept_clamped(monkeypatch):
    """Values stored outside [0.90, 1.0] are clamped."""
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("matching.auto_accept_threshold", "0.50", db.now()),
        )
    assert scanner._auto_accept_threshold() == 0.90  # clamped to floor


def test_out_of_range_probable_clamped(monkeypatch):
    """Values stored outside [0.50, 1.0] are clamped."""
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("matching.probable_threshold", "0.10", db.now()),
        )
    assert scanner._probable_threshold() == 0.50  # clamped to floor


# ---------------------------------------------------------------------------
# Title-only match stays 'probable' regardless of thresholds
# ---------------------------------------------------------------------------

def _seed_work_title_only(title):
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)",
            (wid, title, title, t, t),
        )
    return wid


# ---------------------------------------------------------------------------
# History logging
# ---------------------------------------------------------------------------

def test_settings_matching_history_event():
    """Settings writes are logged with event 'settings.matching'.

    Identifies its own history row by counting rows before and after the PUT,
    then selecting the new row by id (the id not present before).  Asserts the
    exact old and new values the issue requires.
    """
    # Ensure clean settings state
    with db.tx() as conn:
        conn.execute("DELETE FROM settings WHERE key IN ('matching.auto_accept_threshold','matching.probable_threshold')")

    # Count existing matching-history rows so we can identify our own
    before_ids = {row["id"] for row in db.all("SELECT id FROM history WHERE event_type='settings.matching'")}
    before_count = len(before_ids)

    r = client.put("/api/v1/settings/matching", json={
        "auto_accept_threshold": 0.99,
        "probable_threshold": 0.90,
    })
    assert r.status_code == 200, f"PUT failed: {r.text}"

    # Our PUT should have added exactly one row
    after_ids = {row["id"] for row in db.all("SELECT id FROM history WHERE event_type='settings.matching'")}
    assert len(after_ids) == before_count + 1, (
        f"Expected {before_count + 1} history rows, got {len(after_ids)}"
    )
    new_id = (after_ids - before_ids).pop()

    # Select the new row by id
    entry = db.one(
        "SELECT id, event_type, message, data_json FROM history WHERE id=?",
        (new_id,),
    )
    assert entry["event_type"] == "settings.matching"
    data = db.json_load(entry["data_json"], {})
    assert "old" in data and "new" in data
    assert data["new"]["auto_accept_threshold"] == 0.99
    assert data["new"]["probable_threshold"] == 0.90
    # old values should be None (we cleared settings above)
    assert data["old"]["auto_accept_threshold"] is None
    assert data["old"]["probable_threshold"] is None
