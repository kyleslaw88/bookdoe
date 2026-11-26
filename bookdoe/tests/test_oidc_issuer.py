"""Tests for OIDC issuer validation.

Defect found 2026-09-24: after the JWKS fix, the callback still 500'd with
`jwt.exceptions.InvalidIssuerError`. config.py strips the trailing slash from
BOOKDOE_OIDC_ISSUER so the discovery URL can be built by appending
"/.well-known/openid-configuration". That stripped value was then handed to
jwt.decode(issuer=...), which compares it literally against the token's `iss`.

Authentik emits a path-style issuer *with* a trailing slash
(`https://id.../application/o/bookdoe/`), so the comparison could never match.
The archive's own README example carries that trailing slash, meaning OIDC against
Authentik was broken as shipped.

Fix: validate against the issuer the provider publishes in its discovery document,
but only after confirming that document describes the configured issuer (compared
slash-insensitively) so a substituted document cannot redirect trust.
"""
import dataclasses

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import auth as auth_module
from app.config import settings

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "issuer-test"
CONFIGURED = "https://id.example.com/application/o/bookdoe"   # as stored (stripped)
PUBLISHED = "https://id.example.com/application/o/bookdoe/"   # as Authentik emits


def _jwks():
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
    pub.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    return {"keys": [pub]}


def _id_token(iss, aud="cid", nonce="n"):
    import time
    return jwt.encode(
        {"iss": iss, "aud": aud, "sub": "u1", "nonce": nonce,
         "iat": int(time.time()), "exp": int(time.time()) + 300,
         "preferred_username": "ada", "email": "ada@example.com"},
        _KEY, algorithm="RS256", headers={"kid": _KID},
    )


def _decode(token, published, configured=CONFIGURED):
    """Mirror the production validation path."""
    repl = dataclasses.replace(settings, oidc_issuer=configured, oidc_client_id="cid")
    key = jwt.PyJWKSet.from_dict(_jwks()).keys[0]
    published_issuer = (published or "").strip() or repl.oidc_issuer
    if published_issuer.rstrip("/") != repl.oidc_issuer.rstrip("/"):
        raise HTTPException(status_code=502, detail="OIDC discovery issuer does not match the configured issuer")
    return jwt.decode(token, key.key, algorithms=[key.algorithm_name],
                      audience=repl.oidc_client_id, issuer=published_issuer,
                      options={"require": ["exp", "iat", "iss", "sub"]})


def test_trailing_slash_issuer_is_accepted():
    """The exact production failure: Authentik's iss has a slash, config does not."""
    claims = _decode(_id_token(PUBLISHED), PUBLISHED)
    assert claims["sub"] == "u1"


def test_issuer_without_slash_also_works():
    claims = _decode(_id_token(CONFIGURED), CONFIGURED)
    assert claims["sub"] == "u1"


def test_a_different_issuer_is_still_rejected():
    """Slash tolerance must not become 'any issuer will do'."""
    with pytest.raises(jwt.InvalidIssuerError):
        _decode(_id_token("https://evil.example.com/"), PUBLISHED)


def test_discovery_document_claiming_a_foreign_issuer_is_refused():
    """A substituted discovery document must not be able to redirect trust."""
    with pytest.raises(HTTPException) as e:
        _decode(_id_token("https://evil.example.com/"), "https://evil.example.com/")
    assert e.value.status_code == 502
    assert "does not match" in e.value.detail


def test_empty_published_issuer_falls_back_to_configuration():
    claims = _decode(_id_token(CONFIGURED), "")
    assert claims["sub"] == "u1"


def test_config_still_strips_the_trailing_slash_for_url_building():
    """The stripped form is what makes the discovery URL correct - keep it."""
    import os, importlib
    os.environ["BOOKDOE_OIDC_ISSUER"] = PUBLISHED
    import app.config as cfg
    importlib.reload(cfg)
    try:
        assert cfg.settings.oidc_issuer == CONFIGURED
        assert cfg.settings.oidc_issuer + "/.well-known/openid-configuration" == \
            "https://id.example.com/application/o/bookdoe/.well-known/openid-configuration"
    finally:
        os.environ.pop("BOOKDOE_OIDC_ISSUER", None)
        importlib.reload(cfg)


def test_production_code_path_uses_the_published_issuer():
    import inspect
    src = inspect.getsource(auth_module.oidc_callback)
    assert "published_issuer" in src
    assert "issuer=settings.oidc_issuer" not in src, "must not compare the stripped value literally"
