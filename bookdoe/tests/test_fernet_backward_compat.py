"""F-06: Fernet backward-compatibility across cryptography 46→50.

Fernet is a stable, well-defined format (AES-128-CBC + HMAC-SHA256).
Tokens encrypted with cryptography 46.x must decrypt with 50.x so that
stored secrets (indexer keys, SMTP passwords, etc.) remain readable after
the security bump.

This test uses a hardcoded Fernet token string that was produced by
cryptography 46.0.4 under the same key derivation the app uses
(SHA-256 of the secret key).  The test pins the secret key so the
result is deterministic and environment-independent.
"""
from __future__ import annotations

import pytest

from app.crypto import _fernet, decrypt_secret, encrypt_secret


# ---------------------------------------------------------------------------
# Hardcoded token produced by cryptography 46.0.4 with the app's key
# derivation (SHA-256 of "pytest-secret-key-that-is-long-and-stable")
# encrypted with the plaintext "test-secret-value-46".
# ---------------------------------------------------------------------------
_FIXED_TOKEN = (
    "gAAAAABqzA7DNyw2PKou4q6Qw4ST_p_IHhcBD2Iz1Guz2BpT4YqL13GZgoYJoDpGI_"
    "oitED9A6d9JlnPTn8PQL7BHDnHN58h17jl64XkD_UbP40AMzVApJg="
)
_FIXED_PLAINTEXT = b"test-secret-value-46"


@pytest.fixture(autouse=True)
def _pin_fernet_key(monkeypatch):
    """Pin the key material so _fernet() uses a known value.

    The hardcoded ``_FIXED_TOKEN`` was encrypted with the SHA-256 of this
    exact string. ``_base_secret()`` returns that raw 32-byte digest and
    ``_fernet()`` urlsafe-base64-encodes it on every call (nothing is cached),
    so patching ``_base_secret`` is enough. ``raising=True`` (the default) makes
    the fixture fail loudly if ``_base_secret`` is ever renamed.
    """
    import hashlib

    import app.crypto

    known_key = hashlib.sha256(b"pytest-secret-key-that-is-long-and-stable").digest()
    monkeypatch.setattr(app.crypto, "_base_secret", lambda: known_key)


def test_fernet_46_token_decrypts_with_50():
    """A hardcoded 46.x-produced Fernet token decrypts correctly with 50.x.

    The token string in ``_FIXED_TOKEN`` was produced by cryptography 46.0.4
    with the same key derivation the app uses.  If the Fernet API changed in
    a way that breaks backward compatibility, ``decrypt_secret`` would raise
    ``InvalidToken`` and this assertion would fail.
    """
    result = decrypt_secret(_FIXED_TOKEN)
    assert result == "test-secret-value-46"


def test_fernet_46_token_decrypts_with_50_raw():
    """Same as above but calls Fernet.decrypt directly to prove the raw
    token (not the app wrapper) is compatible."""
    import base64
    import hashlib
    from cryptography.fernet import Fernet

    known_key = hashlib.sha256(b"pytest-secret-key-that-is-long-and-stable").digest()
    f = Fernet(base64.urlsafe_b64encode(known_key))
    plaintext = f.decrypt(_FIXED_TOKEN.encode())
    assert plaintext == _FIXED_PLAINTEXT


def test_encrypt_decrypt_roundtrip():
    """encrypt_secret → decrypt_secret round-trips correctly."""
    result = encrypt_secret("my-api-key-12345")
    assert result is not None and result != "my-api-key-12345"
    assert decrypt_secret(result) == "my-api-key-12345"


def test_decrypt_none_and_empty():
    """None and empty string pass through unchanged."""
    assert decrypt_secret(None) is None
    assert decrypt_secret("") == ""


def test_decrypt_invalid_token_returns_original():
    """Invalid tokens fall through to return the original value."""
    from cryptography.fernet import InvalidToken

    # A random base64 string is not a valid Fernet token
    bad_token = "dGhpcyBpcyBub3QgYSB2YWxpZCB0b2tlbg=="
    result = decrypt_secret(bad_token)
    # decrypt_secret catches InvalidToken and returns the original value
    assert result == bad_token
