"""Player API password login for native apps: mints a device token, throttled, mode-gated."""
import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app


@pytest.fixture
def local_mode():
    object.__setattr__(settings, "auth_mode", "local")
    yield
    object.__setattr__(settings, "auth_mode", "none")


def _admin():
    return auth.create_local_user("owner", "owner-password-123456", is_admin=True)


def test_capabilities_advertise_login_methods_and_setup_state(local_mode):
    with TestClient(app) as c:
        caps = c.get("/api/player/v1/capabilities").json()
        assert caps["setup_required"] is True
        assert caps["auth"]["password_login"] is True and caps["auth"]["device_pairing"] is True
        assert caps["auth"]["auth_mode"] == "local" and "password_login" in caps["features"]
        _admin()
        assert c.get("/api/player/v1/capabilities").json()["setup_required"] is False


def test_password_login_returns_a_working_device_token(local_mode):
    _admin()
    with TestClient(app) as c:
        r = c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "owner-password-123456",
                                                      "device_name": "Ada's iPhone", "platform": "ios", "client_version": "1.0"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"].startswith(auth.DEVICE_TOKEN_PREFIX) and body["token_type"] == "Bearer"
        assert body["user"]["username"] == "owner" and body["user"]["account_type"] == "admin"
        assert r.headers["cache-control"] == "no-store"
        dev = TestClient(app, headers={"Authorization": f"Bearer {body['token']}"})
        me = dev.get("/api/player/v1/me").json()
        assert me["device"]["name"] == "Ada's iPhone" and me["management"] is False
        assert dev.get("/api/v1/users").status_code == 403  # device lane only
        row = db.one("SELECT platform,client_version FROM devices WHERE id=?", (body["device_id"],))
        assert row == {"platform": "ios", "client_version": "1.0"}
        assert db.one("SELECT 1 FROM history WHERE event_type='device.registered'")


def test_default_device_name_and_bad_password(local_mode):
    _admin()
    with TestClient(app) as c:
        assert c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "wrong-password-xx"}).status_code == 401
        r = c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "owner-password-123456", "platform": "ios"})
        assert r.json()["name"] == "ios device"


def test_password_login_is_throttled(local_mode, monkeypatch):
    _admin()
    monkeypatch.setattr(auth, "_throttle", {}, raising=False)
    with TestClient(app) as c:
        for _ in range(auth.THROTTLE_FAILURES):
            c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "wrong-password-xx"})
        r = c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "owner-password-123456"})
        assert r.status_code == 429


def test_password_login_refused_in_proxy_and_none_modes():
    _admin()
    with TestClient(app) as c:  # auth_mode none
        r = c.post("/api/player/v1/auth/login", json={"username": "owner", "password": "owner-password-123456"})
        assert r.status_code == 400 and "pair" in r.json()["detail"]
        assert c.get("/api/player/v1/capabilities").json()["auth"]["password_login"] is False


def test_pending_account_gets_no_token(local_mode):
    _admin()
    uid = auth.create_local_user("newbie", "newbie-password-1234", is_admin=False, access_scope="pending")
    with TestClient(app) as c:
        r = c.post("/api/player/v1/auth/login", json={"username": "newbie", "password": "newbie-password-1234"})
        assert r.status_code == 403
        assert db.one("SELECT COUNT(*) n FROM devices WHERE user_id=?", (uid,))["n"] == 0
