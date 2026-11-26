"""Whole-flow test of the OIDC callback.

Written after four consecutive production failures, each only visible once the
previous one was fixed:

  1. login screen rendered a password form   (api() dropped the 401 body)
  2. callback 500  PyJWKClientConnectionError (urllib blocked by Cloudflare)
  3. callback 500  InvalidIssuerError         (trailing-slash mismatch)
  4. callback 500  IntegrityError             (username UNIQUE vs (username,source) lookup)

Each was fixed in isolation and re-deployed, only for the next to surface. This test
drives oidc_callback() from end to end -- state cookie, token exchange, JWKS, issuer,
nonce, user upsert and session cookie -- so the whole path is exercised at once.
"""
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from app import auth, db
from app.config import settings
from app.crypto import sign_session

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "e2e-kid"
ISSUER_PUBLISHED = "https://idp.example.com/application/o/bookdoe/"
ISSUER_CONFIGURED = "https://idp.example.com/application/o/bookdoe"
CLIENT_ID = "test-client"


def _jwks():
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
    pub.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return {"keys": [pub]}


def _id_token(nonce, username="ada", email="ada@example.com", **extra):
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER_PUBLISHED, "aud": CLIENT_ID, "sub": "abc123", "nonce": nonce,
         "iat": now, "exp": now + 300,
         "preferred_username": username, "email": email, "name": "Ada S", **extra},
        _KEY, algorithm="RS256", headers={"kid": _KID},
    )


@pytest.fixture
def oidc(monkeypatch):
    import dataclasses
    repl = dataclasses.replace(settings, auth_mode="oidc", oidc_issuer=ISSUER_CONFIGURED,
                               oidc_client_id=CLIENT_ID, oidc_client_secret="shh",
                               public_url="https://bookdoe.example.com")
    monkeypatch.setattr(auth, "settings", repl)
    auth._JWKS_CACHE.clear()

    discovery = {
        "issuer": ISSUER_PUBLISHED,
        "authorization_endpoint": ISSUER_PUBLISHED + "authorize/",
        "token_endpoint": ISSUER_PUBLISHED + "token/",
        "jwks_uri": ISSUER_PUBLISHED + "jwks/",
    }
    state = {"nonce": "nonce-123"}

    def fake_get(url, **kw):
        req = httpx.Request("GET", url)
        if "openid-configuration" in url:
            return httpx.Response(200, json=discovery, request=req)
        if "jwks" in url:
            return httpx.Response(200, json=_jwks(), request=req)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kw):
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"id_token": _id_token(state["nonce"], **state.get("claims", {})), "access_token": "at"}, request=req)

    monkeypatch.setattr(auth.httpx, "get", fake_get)
    monkeypatch.setattr(auth.httpx, "post", fake_post)
    return state


def _request_with_state(st, nonce):
    cookie = sign_session({"state": st, "nonce": nonce, "verifier": "v" * 40}, ttl_seconds=600)
    scope = {"type": "http", "headers": [(b"cookie", f"{auth.OIDC_COOKIE}={cookie}".encode())]}
    return Request(scope)


def test_full_callback_succeeds_on_a_clean_install(oidc):
    resp = auth.oidc_callback(_request_with_state("st1", "nonce-123"), code="c", state="st1")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    assert auth.SESSION_COOKIE in resp.headers.get("set-cookie", "") or any(
        auth.SESSION_COOKIE in v for k, v in resp.raw_headers if k == b"set-cookie"
    )
    u = db.one("SELECT * FROM users WHERE username='ada'")
    assert u and u["email"] == "ada@example.com"


def test_full_callback_succeeds_when_a_local_admin_already_exists(oidc):
    """The production scenario: local admin created first, then OIDC enabled."""
    auth.create_local_user("ada", "a-long-enough-password", email="old@example.com", is_admin=True)
    resp = auth.oidc_callback(_request_with_state("st2", "nonce-123"), code="c", state="st2")
    assert resp.status_code == 302
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1
    u = db.one("SELECT * FROM users WHERE username='ada'")
    assert u["is_admin"] == 1
    assert u["auth_source"] == "local", "break-glass must survive"


def test_state_mismatch_is_rejected(oidc):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth.oidc_callback(_request_with_state("st3", "nonce-123"), code="c", state="different")
    assert e.value.status_code == 400


def test_nonce_mismatch_is_rejected(oidc):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        auth.oidc_callback(_request_with_state("st4", "wrong-nonce"), code="c", state="st4")
    assert e.value.status_code == 400
    assert "nonce" in e.value.detail.lower()


def test_missing_state_cookie_is_rejected(oidc):
    from fastapi import HTTPException
    empty = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as e:
        auth.oidc_callback(empty, code="c", state="st5")
    assert e.value.status_code == 400


def test_repeat_logins_are_idempotent(oidc):
    for i in range(3):
        r = auth.oidc_callback(_request_with_state(f"s{i}", "nonce-123"), code="c", state=f"s{i}")
        assert r.status_code == 302
    assert db.one("SELECT COUNT(*) n FROM users WHERE username='ada'")["n"] == 1


def test_second_login_does_not_log_account_linked_again(oidc):
    """C4: the link is recorded once; later sign-ins match on the stored sub."""
    auth.create_local_user("ada", "a-long-enough-password", is_admin=True)
    for i in range(3):
        auth.oidc_callback(_request_with_state(f"l{i}", "nonce-123"), code="c", state=f"l{i}")
    n = db.one("SELECT COUNT(*) n FROM history WHERE event_type='auth.account_linked'")["n"]
    assert n == 1
    assert db.one("SELECT auth_subject FROM users WHERE username='ada'")["auth_subject"] == "abc123"


def test_group_allow_list_refuses_a_stranger_through_the_callback(oidc, monkeypatch):
    import dataclasses
    from fastapi import HTTPException
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, oidc_allowed_groups="bookdoe-users, admins"))
    oidc["claims"] = {"groups": ["somebody-else"]}
    with pytest.raises(HTTPException) as e:
        auth.oidc_callback(_request_with_state("g1", "nonce-123"), code="c", state="g1")
    assert e.value.status_code == 403
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    oidc["claims"] = {"groups": ["admins"]}
    assert auth.oidc_callback(_request_with_state("g2", "nonce-123"), code="c", state="g2").status_code == 302
