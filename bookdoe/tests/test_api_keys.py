"""API keys: how agents and OPDS readers authenticate when sign-in is OIDC-only.

The table and Bearer handling pre-dated this, but `scopes` was stored and never
enforced (every key was the full power of its owner), keys could mint keys, and
there was no way to list or revoke one. These pin the enforced behaviour.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def oidc(monkeypatch):
    replaced = dataclasses.replace(settings, auth_mode="oidc")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def _user(name="ada", admin=True):
    uid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id,username,email,is_admin,auth_source,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (uid, name, f"{name}@example.com", int(admin), "oidc", t, t),
        )
    return uid


def _h(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_default_scope_is_read_only():
    _, raw = auth_module.create_api_key(_user(), "agent")
    assert client.get("/api/v1/dashboard", headers=_h(raw)).status_code == 200
    r = client.post("/api/v1/tags", headers=_h(raw), json={"name": "x"})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]


def test_write_key_mutates_but_is_not_admin():
    _, raw = auth_module.create_api_key(_user(), "agent", ["write"])
    assert client.post("/api/v1/tags", headers=_h(raw), json={"name": "x"}).status_code in (200, 201)
    assert client.get("/api/v1/users", headers=_h(raw)).status_code == 403


def test_admin_key_reaches_admin_routes_and_legacy_star_still_does():
    uid = _user()
    _, admin = auth_module.create_api_key(uid, "a", ["admin"])
    assert client.get("/api/v1/users", headers=_h(admin)).status_code == 200
    # A row written before levels existed carried "*" and must keep working.
    _, raw = auth_module.create_api_key(uid, "legacy", ["read"])
    with db.tx() as conn:
        conn.execute("UPDATE api_keys SET scopes_json='[\"*\"]' WHERE name='legacy'")
    assert client.get("/api/v1/users", headers=_h(raw)).status_code == 200


def test_admin_level_key_never_exceeds_a_non_admin_owner():
    uid = _user("mallory", admin=False)
    with pytest.raises(ValueError):
        auth_module.create_api_key(uid, "x", ["admin"], creator_is_admin=False)
    _, raw = auth_module.create_api_key(uid, "x", ["write"], creator_is_admin=False)
    assert client.get("/api/v1/users", headers=_h(raw)).status_code == 403


def test_unknown_scope_rejected():
    with pytest.raises(ValueError):
        auth_module.create_api_key(_user(), "x", ["root"])


def test_revoked_and_expired_keys_stop_working():
    uid = _user()
    kid, revoked = auth_module.create_api_key(uid, "r")
    assert client.get("/api/v1/dashboard", headers=_h(revoked)).status_code == 200
    assert auth_module.revoke_api_key(kid, user_id=uid)
    assert client.get("/api/v1/dashboard", headers=_h(revoked)).status_code == 401
    _, expiring = auth_module.create_api_key(uid, "e", expires_days=1)
    assert client.get("/api/v1/dashboard", headers=_h(expiring)).status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE api_keys SET expires_at=? WHERE name='e'", (db.now() - 1,))
    assert client.get("/api/v1/dashboard", headers=_h(expiring)).status_code == 401


def test_only_the_hash_is_stored_and_listing_never_returns_the_secret():
    uid = _user()
    kid, raw = auth_module.create_api_key(uid, "agent")
    stored = db.one("SELECT * FROM api_keys WHERE id=?", (kid,))
    assert raw not in str(stored)
    listed = auth_module.list_api_keys(uid)
    assert listed[0]["key_prefix"] == raw[:11] and raw not in str(listed)
    assert listed[0]["scope"] == "read"


def test_a_key_cannot_manage_keys(monkeypatch):
    _, raw = auth_module.create_api_key(_user(), "a", ["admin"])
    assert client.post("/api/v1/auth/api-keys", headers=_h(raw), json={"name": "child"}).status_code == 403
    assert client.get("/api/v1/auth/api-keys", headers=_h(raw)).status_code == 403
    assert client.delete("/api/v1/auth/api-keys/whatever", headers=_h(raw)).status_code == 403


def test_owner_cannot_revoke_someone_elses_key_but_admin_can():
    a, b = _user("a", admin=False), _user("b", admin=False)
    kid, _ = auth_module.create_api_key(b, "b-key", ["read"], creator_is_admin=False)
    assert not auth_module.revoke_api_key(kid, user_id=a)
    assert auth_module.revoke_api_key(kid, user_id=None)


def test_opds_accepts_an_api_key_as_basic_password_under_oidc():
    import base64
    _, raw = auth_module.create_api_key(_user(), "reader")
    ok = base64.b64encode(f"anyone:{raw}".encode()).decode()
    assert client.get("/opds/v1/catalog.xml", headers={"Authorization": f"Basic {ok}"}).status_code == 200
    bad = base64.b64encode(b"anyone:alex_wrong").decode()
    assert client.get("/opds/v1/catalog.xml", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/opds/v1/catalog.xml").status_code == 401


def test_last_used_is_recorded_but_not_written_on_every_request():
    uid = _user()
    kid, raw = auth_module.create_api_key(uid, "a")
    client.get("/api/v1/dashboard", headers=_h(raw))
    first = db.one("SELECT last_used_at FROM api_keys WHERE id=?", (kid,))["last_used_at"]
    assert first
    client.get("/api/v1/dashboard", headers=_h(raw))
    assert db.one("SELECT last_used_at FROM api_keys WHERE id=?", (kid,))["last_used_at"] == first


def test_near_expiry_key_gets_warning_header():
    uid = _user("expiring")
    _, raw = auth_module.create_api_key(uid, "soon", expires_days=3)
    r = client.get("/api/v1/dashboard", headers=_h(raw))
    assert r.status_code == 200
    assert r.headers["X-API-Key-Expires-In-Days"] == "3"


def test_far_expiry_and_no_expiry_keys_get_no_warning_header():
    uid = _user("fresh")
    _, far = auth_module.create_api_key(uid, "far", expires_days=90)
    _, never = auth_module.create_api_key(uid, "never")
    assert "X-API-Key-Expires-In-Days" not in client.get("/api/v1/dashboard", headers=_h(far)).headers
    assert "X-API-Key-Expires-In-Days" not in client.get("/api/v1/dashboard", headers=_h(never)).headers


def test_openapi_documents_common_error_statuses():
    schema = client.get("/openapi.json").json()
    ops = [op for item in schema["paths"].values() for m, op in item.items() if m in ("get", "post", "put", "patch", "delete")]
    assert ops
    for op in ops:
        for code in ("401", "403", "404", "422", "429"):
            assert code in op["responses"]


def test_expiry_warn_window_defaults_to_14_days_and_honours_env(monkeypatch):
    monkeypatch.delenv("API_KEY_EXPIRY_WARN_DAYS", raising=False)
    assert main_module._expiry_warn_days() == 14
    monkeypatch.setenv("API_KEY_EXPIRY_WARN_DAYS", "30")
    assert main_module._expiry_warn_days() == 30
    monkeypatch.setenv("API_KEY_EXPIRY_WARN_DAYS", "junk")
    assert main_module._expiry_warn_days() == 14


def test_ten_day_key_warns_with_default_window(monkeypatch):
    monkeypatch.delenv("API_KEY_EXPIRY_WARN_DAYS", raising=False)
    uid = _user("tenday")
    _, raw = auth_module.create_api_key(uid, "ten", expires_days=10)
    r = client.get("/api/v1/dashboard", headers=_h(raw))
    assert r.headers["X-API-Key-Expires-In-Days"] == "10"


def test_expiry_warning_logged_once_per_key(monkeypatch, caplog):
    monkeypatch.delenv("API_KEY_EXPIRY_WARN_DAYS", raising=False)
    main_module._expiry_warned_keys.clear()
    uid = _user("logonce")
    _, raw = auth_module.create_api_key(uid, "once", expires_days=2)
    with caplog.at_level("WARNING", logger="bookdoe"):
        for _ in range(3):
            assert client.get("/api/v1/dashboard", headers=_h(raw)).status_code == 200
    warnings = [r for r in caplog.records if "expires in" in r.getMessage()]
    assert len(warnings) == 1
