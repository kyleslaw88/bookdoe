"""Tests for the auth-mode contract the login screen depends on.

Defect found 2026-09-24 after Gate 8: with BOOKDOE_AUTH_MODE=oidc the UI still
showed a username/password form. The OIDC branch in the frontend was correct but
unreachable — initAuth() caught the 401 from /api/v1/auth/me and hardcoded
auth_mode:'local', discarding the body that says 'oidc'. The form it rendered could
never work, because the server rejects local logins with 400 in that mode.

The frontend fix keeps the parsed body on the thrown error. These tests pin the
server side of that contract: the 401 must carry the real auth_mode.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import main as main_module
from app.config import settings

# NB: `from app.main import app` would shadow the `app` package, making
# `app.main` resolve against the FastAPI instance instead of the module.
client = TestClient(main_module.app)


@pytest.fixture
def mode(monkeypatch):
    """Swap in a settings copy with a different auth_mode.

    `settings` is a frozen dataclass, so the value cannot be assigned in place;
    each module that reads it holds its own binding, so both are rebound. No
    module reloads - those leak state into unrelated tests.
    """
    def _set(value):
        replaced = dataclasses.replace(settings, auth_mode=value)
        monkeypatch.setattr(main_module, "settings", replaced)
        monkeypatch.setattr(auth_module, "settings", replaced)
        return value
    return _set


def test_unauthenticated_me_reports_auth_mode_in_the_body(mode):
    """The 401 body is the only thing that tells the UI which mode to render."""
    mode("oidc")
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    body = r.json()
    assert body["authenticated"] is False
    assert body["auth_mode"] == "oidc", "without this the login screen cannot offer OIDC"
    assert "setup_required" in body


@pytest.mark.parametrize("value", ["local", "oidc", "proxy"])
def test_every_mode_is_reported_faithfully(mode, value):
    mode(value)
    r = client.get("/api/v1/auth/me")
    assert r.json()["auth_mode"] == value


def test_auth_mode_is_never_silently_local_when_configured_otherwise(mode):
    for value in ("oidc", "proxy"):
        mode(value)
        assert client.get("/api/v1/auth/me").json()["auth_mode"] != "local"


def test_local_login_is_available_in_oidc_mode_as_a_secondary_path(mode):
    """OIDC is the household admin's SSO path, but a kids account has no Authentik
    identity of its own - it needs a plain username/password the whole family can use.
    Local login is therefore a deliberate secondary path in oidc mode, not disabled:
    an unknown user still gets a normal 401, never the "disabled" 400 that proxy/none
    modes still return (those have no login form/concept at all)."""
    mode("oidc")
    r = client.post("/api/v1/auth/login", json={"username": "x", "password": "y" * 12})
    assert r.status_code == 401


def test_local_login_is_refused_in_proxy_and_none_modes(mode):
    for value in ("proxy", "none"):
        mode(value)
        r = client.post("/api/v1/auth/login", json={"username": "x", "password": "y" * 12})
        assert r.status_code == 400
        assert "disabled" in r.json()["detail"].lower()


def test_setup_is_refused_in_oidc_mode(mode):
    mode("oidc")
    r = client.post("/api/v1/auth/setup", json={"username": "x", "password": "y" * 12})
    assert r.status_code == 403


def test_oidc_login_route_never_falls_back_to_local(mode):
    mode("oidc")
    r = client.get("/auth/login", follow_redirects=False)
    # Redirect to the IdP, or an explicit error if OIDC is unconfigured in this
    # test environment - but never a 200 rendering a local form.
    assert r.status_code != 200
    if r.status_code in (302, 307):
        assert "client_id=" in r.headers.get("location", "")
