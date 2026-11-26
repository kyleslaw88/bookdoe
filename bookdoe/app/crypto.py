from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import settings

_ph = PasswordHasher()
log = logging.getLogger(__name__)


def ensure_secret_configured() -> None:
    """The secret signs sessions and keys the Fernet store for indexer/client/SMTP
    secrets. The hostname fallback (a 12-hex Docker id) is guessable, so outside
    auth_mode='none' an empty key stops startup instead of silently weakening it."""
    if settings.secret_key:
        return
    if settings.auth_mode != "none":
        raise RuntimeError(
            "BOOKDOE_SECRET_KEY is empty. Set it to a long random value; sessions and "
            "stored secrets would otherwise be signed with a guessable fallback."
        )
    log.warning("BOOKDOE_SECRET_KEY is empty (auth_mode=none): falling back to the hostname for stored secrets")


def _base_secret() -> bytes:
    raw = settings.secret_key or os.getenv("HOSTNAME", "bookdoe-development-secret")
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_base_secret()))


def encrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # Allows an operator to recover from pre-encryption/plaintext settings.
        return value


# Query-string credential parameters as the indexer and download-client APIs name them
# (SABnzbd/newznab `apikey`, generic `api_key`/`token`, Basic-style `password`).
_SECRET_PARAMS = re.compile(r"(?i)\b(apikey|api_key|api-key|token|password|passwd|pwd|secret)=([^&\s'\"<>]+)")


def scrub_secrets(message: Any, *secrets: str | None) -> str:
    """Redact credentials from a message before it is stored or returned.

    httpx error messages contain the full request URL, and several integrations carry
    their key in the query string, so a failed test() or search() would otherwise write
    the live key into `last_error`, which every full-access account can read. Both the
    well-known parameter names and the configured secret values themselves are masked.
    """
    text = str(message if message is not None else "")
    text = _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _ph.verify(stored, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def sign_session(payload: dict[str, Any], ttl_seconds: int = 86400) -> str:
    data = dict(payload)
    data["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    sig = hmac.new(_base_secret(), body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def verify_session(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body_s, sig_s = token.split(".", 1)
    try:
        body = body_s.encode()
        sig = base64.urlsafe_b64decode(sig_s + "=" * (-len(sig_s) % 4))
        expected = hmac.new(_base_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        raw = base64.urlsafe_b64decode(body_s + "=" * (-len(body_s) % 4))
        data = json.loads(raw)
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return data
    except Exception:
        return None
