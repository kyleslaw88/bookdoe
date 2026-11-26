"""Tests for OIDC signing-key retrieval.

Defect found 2026-09-24: the OIDC callback returned 500 with
`PyJWKClientConnectionError: HTTP Error 403: Forbidden`. PyJWKClient fetches the JWKS
with urllib, bypassing the application's HTTP stack. The provider sits behind
Cloudflare, whose bot protection answers urllib's default User-Agent with 403 while
allowing httpx — verified directly against the live endpoint:

    User-Agent: Python-urllib/3.12  -> 403
    User-Agent: python-httpx/0.28.1 -> 200

So the token exchange (httpx) succeeded and only the key fetch failed, making login
impossible. Key retrieval now uses httpx like every other OIDC call.
"""
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "test-kid-1"


def _jwks_dict(kid=_KID):
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
    pub.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [pub]}


def _token(kid=_KID):
    return jwt.encode({"sub": "u1"}, _KEY, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _clear_cache():
    auth._JWKS_CACHE.clear()
    yield
    auth._JWKS_CACHE.clear()


def _stub(monkeypatch, payload, counter=None, status=200):
    req = httpx.Request("GET", "https://idp.example/jwks/")

    def fake_get(url, **kw):
        if counter is not None:
            counter.append(kw.get("headers"))
        return httpx.Response(status, json=payload, request=req)

    monkeypatch.setattr(auth.httpx, "get", fake_get)


def test_jwks_is_fetched_with_httpx_not_urllib(monkeypatch):
    calls = []
    _stub(monkeypatch, _jwks_dict(), calls)
    ks = auth._fetch_jwk_set("https://idp.example/jwks/")
    assert len(ks.keys) == 1
    assert calls, "must go through httpx.get"


def test_signing_key_is_selected_by_kid(monkeypatch):
    _stub(monkeypatch, _jwks_dict())
    key = auth._oidc_signing_key(_token(), "https://idp.example/jwks/")
    assert key.key_id == _KID
    assert key.algorithm_name == "RS256"


def test_fetched_key_actually_verifies_the_token(monkeypatch):
    _stub(monkeypatch, _jwks_dict())
    tok = _token()
    key = auth._oidc_signing_key(tok, "https://idp.example/jwks/")
    claims = jwt.decode(tok, key.key, algorithms=[key.algorithm_name], options={"verify_aud": False})
    assert claims["sub"] == "u1"


def test_key_set_is_cached(monkeypatch):
    calls = []
    _stub(monkeypatch, _jwks_dict(), calls)
    url = "https://idp.example/jwks/"
    auth._fetch_jwk_set(url)
    auth._fetch_jwk_set(url)
    assert len(calls) == 1, "second call should be served from cache"


def test_cache_expires(monkeypatch):
    calls = []
    _stub(monkeypatch, _jwks_dict(), calls)
    url = "https://idp.example/jwks/"
    auth._fetch_jwk_set(url)
    auth._JWKS_CACHE[url] = (time.time() - auth._JWKS_TTL_SECONDS - 1, auth._JWKS_CACHE[url][1])
    auth._fetch_jwk_set(url)
    assert len(calls) == 2


def test_rotated_key_triggers_a_forced_refetch(monkeypatch):
    """A token signed with a kid absent from the cached set must refetch, not fail."""
    url = "https://idp.example/jwks/"
    auth._JWKS_CACHE[url] = (time.time(), jwt.PyJWKSet.from_dict(_jwks_dict("old-kid")))
    calls = []
    _stub(monkeypatch, _jwks_dict("new-kid"), calls)
    key = auth._oidc_signing_key(_token("new-kid"), url)
    assert key.key_id == "new-kid"
    assert len(calls) == 1, "should refetch once when the kid is unknown"


def test_unmatched_kid_raises_a_clear_error(monkeypatch):
    from fastapi import HTTPException
    _stub(monkeypatch, _jwks_dict("something-else"))
    with pytest.raises(HTTPException) as e:
        auth._oidc_signing_key(_token("absent-kid"), "https://idp.example/jwks/")
    assert e.value.status_code == 400
    assert "signing key" in e.value.detail.lower()


def test_http_error_from_the_provider_propagates(monkeypatch):
    _stub(monkeypatch, {"keys": []}, status=503)
    with pytest.raises(httpx.HTTPStatusError):
        auth._fetch_jwk_set("https://idp.example/jwks/")


def test_token_without_kid_uses_the_only_key(monkeypatch):
    _stub(monkeypatch, _jwks_dict())
    tok = jwt.encode({"sub": "u1"}, _KEY, algorithm="RS256")
    key = auth._oidc_signing_key(tok, "https://idp.example/jwks/")
    assert key.key_id == _KID
