from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from . import db, legacy
from .config import settings
from .crypto import hash_password, verify_password, sign_session, verify_session
from .services import signin

log = logging.getLogger(__name__)

SESSION_COOKIE = "bookdoe_session"
OIDC_COOKIE = "bookdoe_oidc"
DEVICE_COOKIE = "bookdoe_device"
HOST_PREFIX = "__Host-"   # over https the cookies carry this prefix (Secure, Path=/, no Domain: enforced by the browser)
SESSION_TTL = 86400                       # a normal sign-in: 24 h, no renewal
DEVICE_COOKIE_MAX_AGE = 10 * 365 * 86400  # "perpetual": the server, not the cookie, decides when it ends
TRUST_ROTATE_AFTER = 30 * 86400           # a trusted browser's token is replaced after this much use
TRUST_ROTATE_GRACE = 300.0                # the replaced token still works this long (a request race, a lost response)


# --- Trusted proxies (C2) -----------------------------------------------------------
def _trusted_networks() -> list[Any]:
    nets = []
    for part in (settings.trusted_proxies or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.error("BOOKDOE_TRUSTED_PROXIES: ignoring invalid entry %r", part)
    return nets


def _in_trusted(host: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks())


def is_trusted_proxy(request: Request) -> bool:
    """True only when the TCP peer is inside BOOKDOE_TRUSTED_PROXIES. An empty list
    trusts nobody, so proxy auth fails closed."""
    return _in_trusted(request.client.host if request.client else None)


def client_ip(request: Request) -> str:
    """The caller's address for throttling. X-Forwarded-For is believed only when the
    peer is a trusted proxy (rightmost entry that is not itself a trusted proxy)."""
    peer = request.client.host if request.client else "unknown"
    if _in_trusted(peer):
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            if not _in_trusted(hop):
                return hop
    return peer


def warn_insecure_config() -> None:
    if settings.auth_mode == "proxy" and not _trusted_networks():
        log.warning(
            "SECURITY: BOOKDOE_AUTH_MODE=proxy but BOOKDOE_TRUSTED_PROXIES is empty. "
            "The %s header is being REFUSED for every request (fail closed); nobody can sign in "
            "until you list your reverse proxy's address(es).", settings.proxy_user_header,
        )


# --- Login throttling (C5) ----------------------------------------------------------
# In-memory and per-process: enough to blunt online guessing on a single-container app.
# `_clock` is module-level so tests can drive time without sleeping.
_clock = time.monotonic
THROTTLE_FAILURES = 5
THROTTLE_LOCKOUT_SECONDS = 60.0
_THROTTLE_STALE_SECONDS = 300.0
_THROTTLE_MAX_KEYS = 10000
_throttle: dict[str, list[float]] = {}  # key -> [failures, locked_until, last_failure]


def _throttle_keys(request: Request, username: str) -> list[str]:
    return ["ip:" + client_ip(request), "user:" + (username or "").strip().lower()]


def throttle_check(request: Request, username: str) -> None:
    now = _clock()
    for key in _throttle_keys(request, username):
        entry = _throttle.get(key)
        if entry and entry[1] > now:
            raise HTTPException(
                status_code=429, detail="Too many failed sign-in attempts; try again shortly",
                headers={"Retry-After": str(int(entry[1] - now) + 1)},
            )


def throttle_failure(request: Request, username: str) -> None:
    now = _clock()
    if len(_throttle) > _THROTTLE_MAX_KEYS:
        for k in [k for k, e in _throttle.items() if e[1] <= now and now - e[2] > _THROTTLE_STALE_SECONDS]:
            del _throttle[k]
    for key in _throttle_keys(request, username):
        entry = _throttle.setdefault(key, [0, 0.0, now])
        if now - entry[2] > _THROTTLE_STALE_SECONDS:
            entry[0] = 0
        entry[0] += 1
        entry[2] = now
        if entry[0] >= THROTTLE_FAILURES:
            entry[0], entry[1] = 0, now + THROTTLE_LOCKOUT_SECONDS


def throttle_success(request: Request, username: str) -> None:
    # Only the account's own counter is cleared; the IP counter must not be resettable
    # by logging in to an account the attacker legitimately holds.
    _throttle.pop("user:" + (username or "").strip().lower(), None)


# Successful OPDS Basic results are cached so a reader polling the catalogue does not
# run argon2 on every request. Keyed by a hash of the credentials, never the password.
_OPDS_CACHE_TTL = 300.0
_opds_cache: dict[str, tuple[float, str, int]] = {}  # key -> (expires, user_id, session_version)


def opds_basic_login(request: Request, username: str, password: str) -> dict[str, Any] | None:
    key = hashlib.sha256(f"{username}:{password}".encode()).hexdigest()
    now = _clock()
    hit = _opds_cache.get(key)
    if hit and hit[0] > now:
        row = db.one("SELECT * FROM users WHERE id=?", (hit[1],))
        # Re-read per hit: a deleted user or a password change/demotion (session_version
        # bump) must take effect immediately, not after the TTL.
        if row and row.get("session_version", 1) == hit[2]:
            return row
    _opds_cache.pop(key, None)
    throttle_check(request, username)
    user = login_local(username, password)
    if not user:
        throttle_failure(request, username)
        return None
    throttle_success(request, username)
    for k in [k for k, v in _opds_cache.items() if v[0] <= now]:
        del _opds_cache[k]
    _opds_cache[key] = (now + _OPDS_CACHE_TTL, user["id"], user.get("session_version", 1))
    return user


def bump_session_version(conn, user_id: str) -> None:
    """Invalidate every outstanding session cookie for this user."""
    conn.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (user_id,))


def ensure_bootstrap_user() -> None:
    if settings.auth_mode != "local":
        return
    if db.one("SELECT id FROM users LIMIT 1"):
        return
    if not settings.bootstrap_password:
        # First-run setup remains available through a guarded endpoint/UI.
        return
    create_local_user(settings.bootstrap_user, settings.bootstrap_password, is_admin=True)


def ensure_anonymous_user() -> None:
    """auth_mode='none' answers every request with the same fixed synthetic identity
    (see current_user() below) rather than a real session. Per-user tables - ratings,
    read_states - carry a NOT NULL foreign key to users(id), so that identity needs
    one real row or the very first rating/read-state write in this mode fails with an
    integrity error. Idempotent; called once at startup, not per-request.
    """
    if settings.auth_mode != "none":
        return
    t = db.now()
    with db.tx() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO users(id,username,auth_source,is_admin,access_scope,created_at,updated_at)
               VALUES ('anonymous','anonymous','none',1,'full',?,?)""",
            (t, t),
        )


# --- Account types ----------------------------------------------------------------
# One derivation, used everywhere: the stored columns stay `is_admin` + `access_scope`
# (so every existing row keeps its meaning) and the three product-facing types come
# from them. `pending` is the least-privilege landing state for an identity Authentik
# authenticated but nobody has placed yet: it may sign in and sign out, nothing else.
ACCOUNT_TYPES = ("admin", "adult", "kid", "pending")
ACCESS_SCOPES = ("full", "kids", "pending")


def account_type(user: dict[str, Any] | None) -> str:
    if not user:
        return "pending"
    if user.get("is_admin"):
        return "admin"
    scope = user.get("access_scope") or "full"
    if scope == "kids":
        return "kid"
    if scope == "pending":
        return "pending"
    return "adult"


def columns_for_account_type(kind: str) -> tuple[int, str]:
    """(is_admin, access_scope) that stores ``kind``; the inverse of account_type()."""
    if kind not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of {ACCOUNT_TYPES}")
    return {"admin": (1, "full"), "adult": (0, "full"), "kid": (0, "kids"), "pending": (0, "pending")}[kind]


def is_pending(user: dict[str, Any]) -> bool:
    return account_type(user) == "pending"


def create_local_user(
    username: str, password: str, *, email: str | None = None, is_admin: bool = True,
    access_scope: str = "full",
) -> str:
    if len(password) < 12:
        raise ValueError("Local passwords must be at least 12 characters")
    if access_scope not in ACCESS_SCOPES:
        raise ValueError("access_scope must be 'full', 'kids' or 'pending'")
    if access_scope == "kids" and is_admin:
        # A restricted account must never also hold the keys to remove its own
        # restriction - that is the whole point of the boundary.
        raise ValueError("A kids-scope account cannot be an administrator")
    uid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id,username,email,password_hash,auth_source,is_admin,access_scope,created_at,updated_at) VALUES (?,?,?,?, 'local', ?,?,?,?)",
            (uid, username, email, hash_password(password), int(is_admin), access_scope, t, t),
        )
    return uid


_DUMMY_HASH: str | None = None


def login_local(username: str, password: str) -> dict[str, Any] | None:
    global _DUMMY_HASH
    row = db.one("SELECT * FROM users WHERE username=? AND auth_source='local'", (username,))
    stored = row.get("password_hash") if row else None
    if not stored:
        # Unknown user (or no local password): burn the same argon2 work so response
        # time does not reveal which usernames exist.
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("bookdoe-timing-equaliser")
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(stored, password):
        return None
    with db.tx() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), row["id"]))
    return row


def csrf_for_session(session_id: str) -> str:
    return hashlib.sha256((session_id + settings.secret_key + "csrf").encode()).hexdigest()


def csrf_for_device(device_id: str) -> str:
    """CSRF token of a trusted-browser cookie lane. Derived from the device id and the
    server secret (like csrf_for_session), so it needs no storage and survives the
    cookie's rotation."""
    return hashlib.sha256((device_id + settings.secret_key + "device-csrf").encode()).hexdigest()


def is_cookie_device(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("_auth_method") == "device" and user.get("_device_via") == "cookie"


def csrf_for_user(user: dict[str, Any]) -> str | None:
    if user.get("_auth_method") == "session":
        return csrf_for_session(user.get("_session_id", ""))
    if is_cookie_device(user):
        return csrf_for_device(user["_device_id"])
    return None


def _secure_cookies() -> bool:
    return settings.public_url.startswith("https://")


def cookie_name(base: str) -> str:
    """The name a cookie is written under: `__Host-` prefixed over https, plain over http."""
    return HOST_PREFIX + base if _secure_cookies() else base


LEGACY_UNTIL_KEY = "auth.legacy_cookie_until"
LEGACY_GRACE_DAYS = 1   # pre-prefix sessions lived at most 24 h, so a day loses nothing and keeps the planting window short
OLD_FORMAT_CLAIMS = {"uid", "username", "admin", "sid", "sv", "exp"}


def legacy_cookie_until() -> float:
    """When the pre-`__Host-` session cookie stops being honoured over https. Fixed the
    first time it is asked for (the server calls it at start-up), 14 days later."""
    until = db.get_setting(LEGACY_UNTIL_KEY)
    if not isinstance(until, (int, float)):
        until = db.now() + LEGACY_GRACE_DAYS * 86400
        db.set_setting(LEGACY_UNTIL_KEY, until)
    return float(until)


def _cookie_values(request: Request, base: str) -> list[str]:
    """The value presented under the name this deployment writes (`__Host-` prefixed over
    https). Legacy unprefixed names are never read here: a sibling subdomain can plant
    them, which a `__Host-` cookie exists to prevent. The one transition read (a session
    cookie from before the prefix) is in current_user."""
    v = request.cookies.get(cookie_name(base))
    return [v] if v else []


def _set_session_cookie(response: Response, user: dict[str, Any], *, sid: str, ttl: int, abs_exp: int | None) -> None:
    # "v": 2 marks every token this code mints, so none can be replayed as a pre-prefix (legacy) cookie.
    claims: dict[str, Any] = {"v": 2, "uid": user["id"], "username": user["username"], "admin": bool(user["is_admin"]), "sid": sid, "sv": user.get("session_version", 1)}
    if abs_exp:
        # A remembered session: `life` is what each renewal grants (renew when less than
        # half is left) and `abs` the absolute end, fixed at sign-in, which no renewal passes.
        claims.update({"rem": 1, "abs": abs_exp, "life": ttl})
    response.set_cookie(cookie_name(SESSION_COOKIE), sign_session(claims, ttl_seconds=ttl), httponly=True, secure=_secure_cookies(),
                        samesite="lax", max_age=ttl, path="/")
    if _secure_cookies():
        response.delete_cookie(SESSION_COOKIE, path="/")   # the pre-prefix cookie (a different name), so it cannot outlive this one


def set_session(response: Response, user: dict[str, Any], *, remember: bool = False) -> str:
    """Start a session. Normally 24 h. With ``remember`` (a sign-in that asked for it, and
    only when the administrator allows it for this account type) the cookie lives
    ``auth.remember_days`` and carries the claims sliding renewal needs."""
    sid = secrets.token_urlsafe(24)
    ttl, abs_exp = SESSION_TTL, None
    if remember and signin.remember_allowed(account_type(user)):
        now = int(time.time())
        abs_exp = now + signin.remember_absolute_days() * 86400
        ttl = min(signin.remember_days() * 86400, abs_exp - now)
        db.add_history("session.remembered", f"'{user['username']}' signed in with 'keep me signed in' ({ttl // 86400} days)",
                       data={"user_id": user["id"], "days": ttl // 86400})
    _set_session_cookie(response, user, sid=sid, ttl=ttl, abs_exp=abs_exp)
    return csrf_for_session(sid)


def _maybe_renew_session(request: Request, payload: dict[str, Any], row: dict[str, Any]) -> None:
    """Sliding renewal of a remembered session: once less than half of its life is left,
    queue a re-issue of the cookie (same session id, so the page's CSRF token stays valid)
    for ``remember_days`` from now, never past the absolute end fixed at sign-in. Applied
    to the response by apply_pending_cookies, so it happens at most once per half-life."""
    now = time.time()
    remaining = float(payload.get("exp", 0)) - now
    if remaining >= float(payload.get("life") or 0) / 2:
        return
    if not signin.remember_allowed(account_type(row)):
        return
    ttl = int(min(signin.remember_days() * 86400, float(payload.get("abs", 0)) - now))
    if ttl <= remaining + 60:
        return   # at the absolute cap: nothing more to grant
    request.scope.setdefault("alx_cookies", {})["session"] = (row, payload.get("sid"), ttl, int(payload["abs"]))


def clear_session(response: Response) -> None:
    if _secure_cookies():
        # __Host- needs Secure and Path=/ (and no Domain) to be accepted, so a deletion carries them too.
        response.delete_cookie(cookie_name(SESSION_COOKIE), path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request) -> dict[str, Any] | None:
    if settings.auth_mode == "none":
        return {"id": "anonymous", "username": "anonymous", "email": None, "is_admin": True, "access_scope": "full", "auth_source": "none", "_auth_method": "none"}
    # API keys and device tokens work in every auth mode.
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz.split(" ", 1)[1].strip()
        if token.startswith(API_KEY_PREFIX):
            row = user_for_api_key(token)
            if row:
                _enforce_key_scope(row, request.method)
                # Read by the security_headers middleware to warn about a key close to expiry.
                request.scope["alx_key_expires_at"] = row.get("key_expires_at")
                request.scope["alx_key_id"] = row.get("key_id")
                return row
        if token.startswith(DEVICE_TOKEN_PREFIX):
            row = user_for_device_token(token)
            if row:
                return row
    if settings.auth_mode == "proxy":
        username = request.headers.get(settings.proxy_user_header)
        # Any client can send this header; only the configured reverse proxy may assert it.
        if username and is_trusted_proxy(request):
            email = request.headers.get(settings.proxy_email_header)
            row = _upsert_external_user(username, email, source="proxy")
            row["_auth_method"] = "proxy"
            return row
        return None
    payload, legacy = None, False
    presented = _cookie_values(request, SESSION_COOKIE)
    if presented:
        payload = verify_session(presented[0])      # the prefixed cookie always wins, valid or not
    elif _secure_cookies() and request.cookies.get(SESSION_COOKIE) and db.now() < legacy_cookie_until():
        # Transition only: a session cookie issued before the prefix, until the removal date.
        payload = verify_session(request.cookies.get(SESSION_COOKIE))
        legacy = bool(payload)
        if legacy and (set(payload) != OLD_FORMAT_CLAIMS or request.cookies.get(cookie_name(DEVICE_COOKIE))):
            payload, legacy = None, False   # not an old-format token, or a prefixed device cookie exists: prefixed always wins
    row = db.one("SELECT * FROM users WHERE id=?", (payload.get("uid"),)) if payload else None
    if row and legacy and is_pending(row):
        row = None
    # A token minted before a password change / demotion carries an older version.
    if row and int(payload.get("sv", 1)) != int(row.get("session_version") or 1):
        row = None
    if row:
        row["_auth_method"] = "session"
        row["_session_id"] = payload.get("sid")
        if legacy:
            # Migrate on first use into a FRESH normal 24 h __Host- session (new id: nothing of the
            # presented token is carried over, so a planted cookie cannot become a long-lived one).
            fresh_sid = secrets.token_urlsafe(24)
            request.scope.setdefault("alx_cookies", {})["session"] = (row, fresh_sid, SESSION_TTL, None)
            row["_session_id"] = fresh_sid   # this very response already reports the CSRF token of the new session
            db.add_history("session.legacy_migrated", f"'{row['username']}' used a pre-prefix session cookie; replaced by a fresh __Host- session",
                           data={"user_id": row["id"]})
        if payload.get("rem"):
            _maybe_renew_session(request, payload, row)
        return row
    # No usable session (none, expired, or invalidated): a trusted-browser cookie may
    # stand in. It never upgrades to a session; it is its own Player-only lane. A valid
    # session always wins, which is why this is only reached when there is none.
    return user_for_device_cookie(request)


PENDING_DETAIL = "Your account is awaiting approval by an administrator"


def require_signed_in(request: Request) -> dict[str, Any]:
    """Any authenticated identity, a pending one included. Only sign-out and the
    identity probe use this; everything with content behind it uses require_user."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_user(request: Request) -> dict[str, Any]:
    """A placed account (admin, adult or kid). A pending identity is authenticated but
    gets 403 here: default-deny until an administrator assigns a type."""
    user = require_signed_in(request)
    if is_pending(user):
        raise HTTPException(status_code=403, detail=PENDING_DETAIL)
    if user.get("_auth_method") == "device":
        _enforce_device_route(request, cookie=is_cookie_device(user))
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required")
    if user.get("_auth_method") == "api_key" and user["_key_level"] < KEY_LEVELS["admin"]:
        raise HTTPException(status_code=403, detail="This API key is not admin-level")
    if user.get("_auth_method") == "device":
        raise HTTPException(status_code=403, detail="Device credentials never carry administrator access")
    return user


def is_kids_scope(user: dict[str, Any]) -> bool:
    return user.get("access_scope") == "kids"


def require_full_access(request: Request) -> dict[str, Any]:
    """Management access: administrators only.

    Acquisition (grab/import/scan), metadata editing, monitoring, the Inbox, history
    and every settings surface sit behind this. Before the Management/Player split any
    non-kids account passed; now an adult is a Player user (read, listen, request) and
    reaches none of it, which is what makes "a kid or adult must not reach Management
    by entering its URL" true at the API, not just in navigation. Kept under its old
    name because ~100 routes depend on it; require_management is the readable alias.
    """
    user = require_user(request)
    if not user.get("is_admin"):
        if is_kids_scope(user):
            raise HTTPException(status_code=403, detail="Not available on a kids account")
        raise HTTPException(status_code=403, detail="Management is limited to administrators")
    if user.get("_auth_method") == "device":
        raise HTTPException(status_code=403, detail="Device credentials never reach Management")
    return user


require_management = require_full_access


def require_write_scope(user: dict[str, Any]) -> None:
    """Gate for a GET that nonetheless causes work (e.g. recommendations?force=true)."""
    if is_kids_scope(user):
        raise HTTPException(status_code=403, detail="Not available on a kids account")
    if user.get("_auth_method") == "api_key" and user["_key_level"] < KEY_LEVELS["write"]:
        raise HTTPException(status_code=403, detail="This API key is read-only")


# Device tokens (Player API, phase 4) are confined to these prefixes. A device is a
# reading client: it may use the Player API and fetch the bytes that API points it at,
# and nothing else, whatever account owns it.
DEVICE_ROUTE_PREFIXES = ("/api/player/",)
DEVICE_ROUTE_PATTERNS = (re.compile(r"^/api/v1/library/[^/]+/(files|cover)/?"),)
# A trusted browser runs the whole Player web app, which also calls these personal
# /api/v1 routes (library browsing, book detail, rating, read state, reader manifest and
# legacy progress, authors, series, tags). They are listed exactly - method and shape -
# and each already applies the owner's visibility rules. A Bearer device (a reader) keeps
# the narrower lane above. Everything not listed here, Management and admin routes
# included, stays refused.
COOKIE_DEVICE_ROUTES = (
    ("GET", re.compile(r"^/api/v1/library/?$")),
    ("GET", re.compile(r"^/api/v1/library/[^/]+/?$")),
    ("PUT", re.compile(r"^/api/v1/library/[^/]+/(rating|read-state)/?$")),
    ("GET", re.compile(r"^/api/v1/library/[^/]+/read/[^/]+/?$")),
    ("GET", re.compile(r"^/api/v1/library/[^/]+/progress/[^/]+/?$")),
    ("PUT", re.compile(r"^/api/v1/library/[^/]+/progress/[^/]+/?$")),
    ("DELETE", re.compile(r"^/api/v1/library/[^/]+/progress/[^/]+/?$")),
    ("GET", re.compile(r"^/api/v1/(authors|series)/?$")),
    ("GET", re.compile(r"^/api/v1/(authors|series)/[^/]+/?$")),
    ("GET", re.compile(r"^/api/v1/tags/?$")),
)


def _enforce_device_route(request: Request, *, cookie: bool = False) -> None:
    path = request.url.path
    if path.startswith(DEVICE_ROUTE_PREFIXES) or any(p.match(path) for p in DEVICE_ROUTE_PATTERNS):
        return
    if cookie and any(m == request.method and p.match(path) for m, p in COOKIE_DEVICE_ROUTES):
        return
    raise HTTPException(status_code=403, detail="Device credentials are limited to the Player API")


def require_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    # require_signed_in, not require_user: a pending account must still be able to sign
    # out, and the real gate of the route runs in its own dependency.
    user = require_signed_in(request)
    method = user.get("_auth_method")
    if method in {"api_key", "proxy", "none"} or (method == "device" and not is_cookie_device(user)):
        return
    # A session, or a trusted-browser cookie. The cookie rides along on cross-site
    # requests like any other, so SameSite=Lax alone is not a defence: both need the header.
    expected = csrf_for_user(user) or ""
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


# --- API keys --------------------------------------------------------------------
# A key authenticates as its owning user and is additionally capped to one access
# level: read (GET/HEAD only) < write (any non-admin mutation) < admin (user and
# system management, and only ever effective for an account that is itself an admin).
# The legacy "*" scope predates levels and means "no extra cap".
API_KEY_PREFIX = "alex_"
KEY_LEVELS = {"read": 1, "write": 2, "admin": 3}
LEVEL_NAMES = {v: k for k, v in KEY_LEVELS.items()}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LAST_USED_GRANULARITY = 60.0  # seconds; avoids a DB write on every agent request


def key_level(scopes: list[str] | None) -> int:
    if not scopes or "*" in scopes:
        return KEY_LEVELS["admin"]
    return max((KEY_LEVELS.get(s, 0) for s in scopes), default=0)


def user_for_api_key(token: str) -> dict[str, Any] | None:
    """Resolve a raw key to its owner, or None if unknown, revoked or expired."""
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    t = db.now()
    row = db.one(
        """SELECT u.*,k.id key_id,k.scopes_json,k.expires_at key_expires_at,k.last_used_at key_last_used FROM api_keys k
           JOIN users u ON u.id=k.user_id
           WHERE k.key_hash=? AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>?)""",
        (key_hash, t),
    )
    if not row:
        return None
    if not row.get("key_last_used") or t - row["key_last_used"] > _LAST_USED_GRANULARITY:
        with db.tx() as conn:
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (t, row["key_id"]))
    row["_auth_method"] = "api_key"
    row["_key_level"] = key_level(db.json_load(row.get("scopes_json"), ["*"]))
    return row


def _enforce_key_scope(user: dict[str, Any], method: str) -> None:
    needed = KEY_LEVELS["read"] if method in _SAFE_METHODS else KEY_LEVELS["write"]
    if user["_key_level"] < needed:
        raise HTTPException(status_code=403, detail="This API key is read-only")


def require_session_user(request: Request) -> dict[str, Any]:
    """A signed-in human (session/proxy), never an API key: a key must not be able to
    mint, list or revoke keys, or a leaked read-only key could escalate itself."""
    user = require_user(request)
    if user.get("_auth_method") in ("api_key", "device"):
        raise HTTPException(status_code=403, detail="API keys and device tokens cannot manage credentials")
    if is_kids_scope(user):
        raise HTTPException(status_code=403, detail="Not available on a kids account")
    return user


def create_api_key(
    user_id: str, name: str, scopes: list[str] | None = None, *, expires_days: float | None = None,
    creator_is_admin: bool = True,
) -> tuple[str, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("A key needs a name")
    requested = scopes or ["read"]
    if any(s != "*" and s not in KEY_LEVELS for s in requested):
        raise ValueError(f"scopes must be drawn from {sorted(KEY_LEVELS)}")
    # Store one canonical level, not whatever list the caller sent.
    level = LEVEL_NAMES[key_level(requested)]
    if level == "admin" and not creator_is_admin:
        raise ValueError("Only an administrator can create an admin-level key")
    if expires_days is not None and expires_days <= 0:
        raise ValueError("expires_days must be positive")
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    kid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO api_keys(id,user_id,name,key_hash,scopes_json,created_at,key_prefix,expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (kid, user_id, name, hashlib.sha256(raw.encode()).hexdigest(), db.json_dump([level]), t,
             raw[: len(API_KEY_PREFIX) + 6], t + expires_days * 86400 if expires_days else None),
        )
    db.add_history("auth.api_key.created", f"API key '{name}' created ({level})", data={"key_id": kid, "user_id": user_id})
    return kid, raw


def list_api_keys(user_id: str | None) -> list[dict[str, Any]]:
    """Key metadata only - the secret is never retrievable. user_id=None lists all."""
    rows = db.all(
        """SELECT k.id,k.name,k.key_prefix,k.scopes_json,k.created_at,k.last_used_at,k.expires_at,k.revoked_at,
                  u.username owner FROM api_keys k JOIN users u ON u.id=k.user_id
           WHERE (?1 IS NULL OR k.user_id=?1) AND k.revoked_at IS NULL ORDER BY k.created_at DESC""",
        (user_id,),
    )
    for r in rows:
        r["scope"] = LEVEL_NAMES[key_level(db.json_load(r.pop("scopes_json"), ["*"]))]
        r["expired"] = bool(r["expires_at"] and r["expires_at"] <= db.now())
    return rows


def revoke_api_key(key_id: str, *, user_id: str | None) -> bool:
    """Revoke (soft-delete, so last_used/created history survives). user_id scopes it
    to one owner; None lets an administrator revoke anyone's."""
    sql = "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL"
    params: tuple[Any, ...] = (db.now(), key_id)
    if user_id:
        sql, params = sql + " AND user_id=?", params + (user_id,)
    with db.tx() as conn:
        cur = conn.execute(sql, params)
    if cur.rowcount:
        db.add_history("auth.api_key.revoked", "API key revoked", data={"key_id": key_id})
    return bool(cur.rowcount)


# --- Device tokens (Player API) ---------------------------------------------------
# A device token is a credential for one reading device of one account: never admin,
# never Management (see _enforce_device_route), revocable from the Player and from
# Management, rotatable by the device itself. Only its SHA-256 is stored.
DEVICE_TOKEN_PREFIX = "alxd_"
DEVICE_CODE_TTL = 900.0          # pairing code lifetime (seconds)
DEVICE_CODE_INTERVAL = 5         # recommended poll interval (seconds)
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"  # no vowels, no 0/1/O/I


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_for_device_token(token: str) -> dict[str, Any] | None:
    """The owning account of a live PAIRED device token, or None if unknown, revoked or
    expired. A trusted-browser token (kind 'browser') is a cookie credential and is never
    accepted as a Bearer token: that would skip CSRF and the trust settings.
    A device that belongs to an account later made pending is refused by require_user."""
    t = db.now()
    row = db.one(
        """SELECT u.*, d.id device_id, d.name device_name, d.last_seen_at device_seen FROM devices d
           JOIN users u ON u.id=d.user_id
           WHERE d.token_hash=? AND d.kind='paired' AND d.revoked_at IS NULL AND (d.expires_at IS NULL OR d.expires_at>?)""",
        (_hash(token), t),
    )
    if not row:
        return None
    if not row.get("device_seen") or t - row["device_seen"] > _LAST_USED_GRANULARITY:
        with db.tx() as conn:
            conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (t, row["device_id"]))
    row["_auth_method"] = "device"
    row["_device_via"] = "bearer"
    row["_device_id"] = row.pop("device_id")
    row["_device_name"] = row.pop("device_name")
    row.pop("device_seen", None)
    return row


def new_user_code() -> str:
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def normalize_user_code(value: str) -> str:
    cleaned = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    return cleaned[:4] + "-" + cleaned[4:] if len(cleaned) == 8 else cleaned


def start_device_code(*, name: str | None, platform: str | None, client_version: str | None) -> dict[str, Any]:
    """Step 1 of pairing: the device asks for a code pair. Unauthenticated (rate limited
    by the route). The device keeps device_code secret; the person types user_code."""
    device_code = secrets.token_urlsafe(32)
    t = db.now()
    with db.tx() as conn:
        for _ in range(10):
            user_code = new_user_code()
            if not conn.execute("SELECT 1 FROM device_codes WHERE user_code=?", (user_code,)).fetchone():
                break
        conn.execute("DELETE FROM device_codes WHERE expires_at<?", (t,))  # housekeeping, cheap
        conn.execute(
            "INSERT INTO device_codes(device_code_hash,user_code,name,platform,client_version,created_at,expires_at) VALUES (?,?,?,?,?,?,?)",
            (_hash(device_code), user_code, (name or "").strip()[:80] or None, (platform or "").strip()[:60] or None,
             (client_version or "").strip()[:40] or None, t, t + DEVICE_CODE_TTL),
        )
    return {"device_code": device_code, "user_code": user_code, "expires_in": int(DEVICE_CODE_TTL), "interval": DEVICE_CODE_INTERVAL}


def approve_device_code(user: dict[str, Any], user_code: str, *, name: str | None = None) -> dict[str, Any]:
    """Step 2: a signed-in person (session, never a device or key) approves the code
    they see on the device. Binds the pending code to that account."""
    if user.get("_auth_method") not in ("session", "proxy", "none"):
        raise HTTPException(status_code=403, detail="Pair a device from a signed-in browser, not from another credential")
    code = normalize_user_code(user_code)
    t = db.now()
    row = db.one("SELECT * FROM device_codes WHERE user_code=? AND expires_at>? AND consumed_at IS NULL AND denied_at IS NULL", (code, t))
    if not row:
        raise HTTPException(status_code=404, detail="That code is not waiting to be paired (expired, mistyped or already used)")
    if row.get("approved_by"):
        raise HTTPException(status_code=409, detail="That code was already approved")
    with db.tx() as conn:
        conn.execute("UPDATE device_codes SET approved_by=?,approved_at=?,name=COALESCE(?,name) WHERE user_code=?", (user["id"], t, (name or "").strip()[:80] or None, code))
    return {"user_code": code, "approved": True, "name": (name or row.get("name") or "New device")}


def deny_device_code(user: dict[str, Any], user_code: str) -> bool:
    code = normalize_user_code(user_code)
    with db.tx() as conn:
        n = conn.execute("UPDATE device_codes SET denied_at=? WHERE user_code=? AND consumed_at IS NULL AND denied_at IS NULL", (db.now(), code)).rowcount
    return bool(n)


def redeem_device_code(device_code: str) -> tuple[str, dict[str, Any] | None]:
    """Step 3: the device polls. Returns (state, payload): 'authorization_pending',
    'slow_down', 'expired_token', 'access_denied' or 'ok' with the minted token. The
    code is consumed on success, so a stolen device_code cannot be replayed."""
    t = db.now()
    h = _hash(device_code)
    row = db.one("SELECT * FROM device_codes WHERE device_code_hash=?", (h,))
    if not row or row.get("consumed_at"):
        return "expired_token", None
    if row["expires_at"] <= t:
        return "expired_token", None
    if row.get("denied_at"):
        return "access_denied", None
    if row.get("last_poll_at") and t - row["last_poll_at"] < DEVICE_CODE_INTERVAL - 1:
        with db.tx() as conn:
            conn.execute("UPDATE device_codes SET last_poll_at=? WHERE device_code_hash=?", (t, h))
        return "slow_down", None
    if not row.get("approved_by"):
        with db.tx() as conn:
            conn.execute("UPDATE device_codes SET last_poll_at=? WHERE device_code_hash=?", (t, h))
        return "authorization_pending", None
    user = db.one("SELECT * FROM users WHERE id=?", (row["approved_by"],))
    if not user or is_pending(user):
        return "access_denied", None
    with db.tx() as conn:
        claimed = conn.execute("UPDATE device_codes SET consumed_at=? WHERE device_code_hash=? AND consumed_at IS NULL", (t, h)).rowcount
    if not claimed:
        return "expired_token", None
    return "ok", create_device(user["id"], name=row.get("name") or "New device", platform=row.get("platform"), client_version=row.get("client_version"))


class DeviceLimitError(Exception):
    def __init__(self, active: int):
        super().__init__(active)
        self.active = active


def create_device(user_id: str, *, name: str, platform: str | None = None, client_version: str | None = None,
                  expires_days: float | None = None, rotated_from: str | None = None, kind: str = "paired",
                  ua_summary: str | None = None, ip_prefix: str | None = None, max_browser: int | None = None) -> dict[str, Any]:
    raw = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    did, t = db.uid(), db.now()
    with db.tx() as conn:
        if max_browser is not None:
            # Counted and inserted in one IMMEDIATE transaction, so parallel requests cannot pass the limit together.
            n = conn.execute("SELECT COUNT(*) FROM devices WHERE user_id=? AND kind='browser' AND revoked_at IS NULL", (user_id,)).fetchone()[0]
            if n >= max_browser:
                raise DeviceLimitError(n)
        conn.execute(
            "INSERT INTO devices(id,user_id,name,platform,client_version,token_hash,token_prefix,created_at,expires_at,rotated_from,kind,ua_summary,first_seen_at,last_seen_at,last_ip_prefix,rotated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, user_id, name[:80], platform, client_version, _hash(raw), raw[: len(DEVICE_TOKEN_PREFIX) + 6], t,
             t + expires_days * 86400 if expires_days else None, rotated_from, kind, ua_summary,
             t if kind == "browser" else None, t if kind == "browser" else None, ip_prefix, t if kind == "browser" else None),
        )
    if kind == "browser":
        db.add_history("device.trusted", f"Browser '{name}' trusted", data={"device_id": did, "user_id": user_id, "ua": ua_summary})
    else:
        db.add_history("device.registered", f"Device '{name}' registered", data={"device_id": did, "user_id": user_id, "rotated_from": rotated_from})
    return {"device_id": did, "token": raw, "token_type": "Bearer", "name": name[:80], "user_id": user_id}


DEVICE_PUBLIC_FIELDS = ("id", "user_id", "name", "platform", "client_version", "token_prefix", "created_at", "last_seen_at", "expires_at", "revoked_at",
                        "kind", "ua_summary", "first_seen_at", "last_ip_prefix", "rotated_at", "reuse_detected_at")


def device_public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in DEVICE_PUBLIC_FIELDS}


def list_devices(user_id: str | None, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    """Metadata only; the token is never retrievable. user_id=None lists everyone's."""
    where = ["(?1 IS NULL OR d.user_id=?1)"]
    if not include_revoked:
        where.append("d.revoked_at IS NULL")
    rows = db.all(f"SELECT d.*,u.username owner FROM devices d JOIN users u ON u.id=d.user_id WHERE {' AND '.join(where)} ORDER BY d.created_at DESC", (user_id,))  # nosec B608
    out = []
    for r in rows:
        item = device_public(r)
        item["owner"] = r["owner"]
        out.append(item)
    return out


def revoke_device(device_id: str, *, user_id: str | None, actor: str | None) -> bool:
    """Soft revoke; user_id scopes it to one owner, None lets an administrator revoke anyone's."""
    sql, params = "UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", [db.now(), device_id]
    if user_id:
        sql += " AND user_id=?"; params.append(user_id)
    with db.tx() as conn:
        n = conn.execute(sql, tuple(params)).rowcount
    if n:
        db.add_history("device.revoked", "Device credential revoked", data={"device_id": device_id, "by": actor})
    return bool(n)


def revoke_user_devices(user_id: str, *, kinds: tuple[str, ...] = ("browser",), actor: str | None, reason: str) -> int:
    """Revoke every live device of this kind for an account (one audit event each).
    Trusted browsers are 'browser'; pass ('browser', 'paired') for everything."""
    marks = ",".join("?" * len(kinds))
    ids = [r["id"] for r in db.all(f"SELECT id FROM devices WHERE user_id=? AND revoked_at IS NULL AND kind IN ({marks})", (user_id, *kinds))]  # nosec B608
    t = db.now()
    with db.tx() as conn:
        for did in ids:
            conn.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (t, did))
    for did in ids:
        db.add_history("device.revoked", "Device credential revoked", data={"device_id": did, "by": actor, "reason": reason})
    return len(ids)


def revoke_idle_trusted_devices() -> int:
    """Housekeeping: with auth.trusted_device_idle_days set, a trusted browser unused that
    long is revoked. 0 (the default) means a trusted device never expires from disuse."""
    days = signin.trusted_device_idle_days()
    if days <= 0:
        return 0
    t = db.now()
    ids = [r["id"] for r in db.all(
        "SELECT id FROM devices WHERE kind='browser' AND revoked_at IS NULL AND COALESCE(last_seen_at,created_at)<?", (t - days * 86400,))]
    with db.tx() as conn:
        for did in ids:
            conn.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (t, did))
    for did in ids:
        db.add_history("device.revoked", f"Trusted browser unused for {days} days", data={"device_id": did, "by": "housekeeping", "reason": "idle"})
    return len(ids)


# --- Trusted browsers ("Trust this device") ----------------------------------------
# The token of a trusted browser lives only in an HttpOnly cookie: the page, the service
# worker and any script never see it, and it is never returned in JSON. The server stores
# its SHA-256, rotates it after TRUST_ROTATE_AFTER of use (re-setting the cookie and keeping
# the old hash valid for TRUST_ROTATE_GRACE), and honours it only in the Player-only
# device lane (_enforce_device_route), with CSRF on writes.
_UA_BROWSERS = (("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser/", "Samsung Internet"),
                ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_UA_SYSTEMS = (("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"), ("CrOS", "ChromeOS"), ("Windows", "Windows"),
               ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"))


def ua_summary(user_agent: str | None) -> str:
    """"Safari on iOS": all that is kept of a User-Agent (never the raw string)."""
    ua = user_agent or ""
    browser = next((name for token, name in _UA_BROWSERS if token in ua), "Browser")
    system = next((name for token, name in _UA_SYSTEMS if token in ua), None)
    return f"{browser} on {system}" if system else browser


def ip_prefix(ip: str | None) -> str | None:
    """The network, not the person: /24 for IPv4, /48 for IPv6."""
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 48}", strict=False))


_DEV_SELECT = """SELECT u.*, d.id dev_id, d.name dev_name, d.last_seen_at dev_seen, d.rotated_at dev_rotated, d.created_at dev_created,
                  d.token_hash dev_hash, d.prev_valid_until dev_prev_until, d.prev_rerotated dev_rerot
           FROM devices d JOIN users u ON u.id=d.user_id"""
_DEV_LIVE = "d.kind='browser' AND d.revoked_at IS NULL AND (d.expires_at IS NULL OR d.expires_at>?)"
GRACE_RETRY = 10.0   # a replaced token seen again later than this after a rotation is re-rotated (lost response); sooner is a race


def _device_cookie_row(raw: str) -> tuple[dict[str, Any] | None, str]:
    """(row, state) for a cookie value. state: 'current' (the live token), 'grace' (the token
    a rotation replaced, still inside its window), 'reused' (that replaced token, presented
    after the window: it can only be a copy) or 'unknown' (no live browser device)."""
    h, t = _hash(raw), db.now()
    row = db.one(f"{_DEV_SELECT} WHERE {_DEV_LIVE} AND (d.token_hash=? OR d.prev_token_hash=?)", (t, h, h))
    if not row:
        return None, "unknown"
    if row["dev_hash"] == h:
        return row, "current"
    return row, "grace" if (row["dev_prev_until"] or 0) > t else "reused"


def _device_cookie_value(request: Request) -> str | None:
    return next((v for v in _cookie_values(request, DEVICE_COOKIE) if v.startswith(DEVICE_TOKEN_PREFIX) and len(v) < 200), None)


def device_cookie_present(request: Request) -> bool:
    return bool(_cookie_values(request, DEVICE_COOKIE))


def _rotate_browser_token(row: dict[str, Any], cache: dict[str, Any], t: float, *, rerotation: bool = False) -> None:
    new = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    with db.tx() as conn:
        # Compare-and-set: of two racing requests only one rotates; the other
        # authenticates on the grace window and the browser takes the winner's cookie.
        n = conn.execute(
            "UPDATE devices SET token_hash=?,token_prefix=?,prev_token_hash=?,prev_valid_until=?,rotated_at=?,prev_rerotated=? WHERE id=? AND token_hash=?",
            (_hash(new), new[: len(DEVICE_TOKEN_PREFIX) + 6], row["dev_hash"], t + TRUST_ROTATE_GRACE, t, int(rerotation), row["dev_id"], row["dev_hash"]),
        ).rowcount
    if n:
        cache["device_set"] = new
        db.add_history("device.rotated", f"Trusted browser '{row['dev_name']}' token rotated", data={"device_id": row["dev_id"], "user_id": row["id"]})


def _revoke_for_reuse(row: dict[str, Any]) -> None:
    t = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE devices SET revoked_at=?,reuse_detected_at=? WHERE id=? AND revoked_at IS NULL", (t, t, row["dev_id"]))
    db.add_history("device.reuse_detected", f"A replaced token of trusted browser '{row['dev_name']}' was presented after its grace window; the device was revoked",
                   data={"device_id": row["dev_id"], "user_id": row["id"]})


def user_for_device_cookie(request: Request) -> dict[str, Any] | None:
    """The account a valid device cookie stands for, as a device-lane identity, or None.
    Resolved once per request (rotation must not run twice)."""
    if settings.auth_mode not in ("local", "oidc"):
        return None
    cache = request.scope.setdefault("alx_cookies", {})
    if "device_user" in cache:
        found = cache["device_user"]
        return dict(found) if found else None
    raw = _device_cookie_value(request)
    result = None
    if raw:
        row, state = _device_cookie_row(raw)
        if state == "unknown":
            cache["clear_device"] = True    # revoked, expired or unknown: stop sending it
        elif state == "reused":
            # The browser holds the current token; someone else holds an old one. Kill the
            # registration (both tokens) and let the owner see why.
            _revoke_for_reuse(row)
            cache["clear_device"] = True
        elif signin.trust_allowed(account_type(row)):
            t = db.now()
            did = row["dev_id"]
            age = t - (row["dev_rotated"] or row["dev_created"])
            if state == "grace" and age > GRACE_RETRY and row["dev_rerot"]:
                # A replaced token may re-rotate once (the browser never got the response). The
                # rotation it caused was already that one: a second use means two parties hold
                # tokens of this device taking turns, so it is revoked.
                _revoke_for_reuse(row)
                cache["clear_device"] = True
                cache["device_user"] = None
                return None
            if (state == "current" and age > TRUST_ROTATE_AFTER) or (state == "grace" and age > GRACE_RETRY):
                _rotate_browser_token(row, cache, t, rerotation=state == "grace")
            if not row["dev_seen"] or t - row["dev_seen"] > _LAST_USED_GRANULARITY:
                with db.tx() as conn:
                    conn.execute("UPDATE devices SET last_seen_at=?,last_ip_prefix=COALESCE(?,last_ip_prefix),first_seen_at=COALESCE(first_seen_at,?) WHERE id=?",
                                 (t, ip_prefix(client_ip(request)), t, did))
            row["_auth_method"] = "device"
            row["_device_via"] = "cookie"
            row["_device_id"] = row.pop("dev_id")
            row["_device_name"] = row.pop("dev_name")
            for k in ("dev_seen", "dev_rotated", "dev_created", "dev_hash", "dev_prev_until", "dev_rerot"):
                row.pop(k, None)
            result = row
    cache["device_user"] = result
    return dict(result) if result else None


def device_for_cookie(request: Request) -> dict[str, Any] | None:
    """The live trusted-browser registration (id, user_id) this request's cookie names, whoever owns it."""
    raw = _device_cookie_value(request)
    if not raw:
        return None
    row, state = _device_cookie_row(raw)
    return {"id": row["dev_id"], "user_id": row["id"]} if row and state in ("current", "grace") else None


def device_id_for_cookie(request: Request, user_id: str) -> str | None:
    """The trusted-browser device this request's cookie names, if it is this account's and live."""
    d = device_for_cookie(request)
    return d["id"] if d and d["user_id"] == user_id else None


def set_device_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(cookie_name(DEVICE_COOKIE), raw_token, httponly=True, secure=_secure_cookies(), samesite="lax", max_age=DEVICE_COOKIE_MAX_AGE, path="/")


def clear_device_cookie(response: Response) -> None:
    for name in {cookie_name(DEVICE_COOKIE), DEVICE_COOKIE}:
        response.delete_cookie(name, path="/", httponly=True, secure=_secure_cookies(), samesite="lax")


def forget_trusted_device(request: Request, response: Response, user_id: str) -> bool:
    """Sign-out on a browser: revoke the trusted-browser registration this request's cookie
    names and clear the cookie. Whoever owns it: it is this browser being signed out, and a
    family member's registration must not be left behind as an orphan counting toward their limit."""
    d = device_for_cookie(request)
    revoked = bool(d) and revoke_device(d["id"], user_id=None, actor=user_id)
    if device_cookie_present(request):
        clear_device_cookie(response)
    return revoked


def apply_pending_cookies(request: Request, response: Response) -> None:
    """Cookies decided while authenticating (the middleware calls this on every response):
    a rotated device token, a renewed remembered session, or a dead device cookie to drop."""
    pend = request.scope.get("alx_cookies")
    if not pend:
        return

    def already_set(name: str) -> bool:
        return any(c.startswith(name + "=") for c in response.headers.getlist("set-cookie"))

    if pend.get("device_set"):
        set_device_cookie(response, pend["device_set"])
    elif pend.get("clear_device") and not already_set(cookie_name(DEVICE_COOKIE)):
        clear_device_cookie(response)
    if pend.get("session") and not already_set(cookie_name(SESSION_COOKIE)):
        row, sid, ttl, abs_exp = pend["session"]
        _set_session_cookie(response, row, sid=sid, ttl=ttl, abs_exp=abs_exp)


def rename_device(device_id: str, user_id: str, name: str) -> bool:
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("A device needs a name")
    with db.tx() as conn:
        n = conn.execute("UPDATE devices SET name=? WHERE id=? AND user_id=? AND revoked_at IS NULL", (name, device_id, user_id)).rowcount
    return bool(n)


def rotate_device(user: dict[str, Any]) -> dict[str, Any]:
    """Called by the device with its current token: mint a replacement and revoke the
    old one in the same step, so a leaked old token dies the moment the device rotates."""
    if user.get("_auth_method") != "device":
        raise HTTPException(status_code=403, detail="Only a device can rotate its own token")
    old = db.one("SELECT * FROM devices WHERE id=?", (user["_device_id"],))
    fresh = create_device(user["id"], name=old["name"], platform=old.get("platform"), client_version=old.get("client_version"), rotated_from=old["id"])
    revoke_device(old["id"], user_id=user["id"], actor="rotation")
    return fresh


# --- JWKS retrieval -------------------------------------------------------------
# PyJWKClient fetches the key set with urllib, bypassing this application's HTTP
# stack entirely. Behind a CDN with bot protection that is fatal: Cloudflare answers
# urllib's default User-Agent with 403 while allowing httpx through, so the token
# exchange (httpx) succeeded and only the key fetch failed. Every other OIDC call
# here uses httpx; this makes key retrieval consistent with them.
_JWKS_CACHE: dict[str, tuple[float, Any]] = {}
_JWKS_TTL_SECONDS = 300.0


def _fetch_jwk_set(url: str, *, force: bool = False) -> Any:
    now = time.time()
    cached = _JWKS_CACHE.get(url)
    if cached and not force and now - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    resp = httpx.get(url, timeout=15, headers={"Accept": "application/json"})
    resp.raise_for_status()
    jwk_set = jwt.PyJWKSet.from_dict(resp.json())
    _JWKS_CACHE[url] = (now, jwk_set)
    return jwk_set


def _oidc_signing_key(id_token: str, jwks_uri: str):
    kid = jwt.get_unverified_header(id_token).get("kid")

    def pick(jwk_set):
        keys = [k for k in jwk_set.keys if kid is None or k.key_id == kid]
        return keys[0] if keys else None

    key = pick(_fetch_jwk_set(jwks_uri))
    if key is None:
        # The provider may have rotated keys since we cached the set.
        key = pick(_fetch_jwk_set(jwks_uri, force=True))
    if key is None:
        raise HTTPException(status_code=400, detail="OIDC provider has no signing key matching the ID token")
    return key


def safe_next_path(value: str | None) -> str:
    """A same-origin path to return to after sign-in; anything else becomes '/'."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value and len(value) < 512:
        return value
    return "/"


def oidc_login_url(next_path: str | None = None, remember: bool = False) -> RedirectResponse:
    if settings.auth_mode != "oidc" or not settings.oidc_issuer or not settings.oidc_client_id:
        raise HTTPException(status_code=400, detail="OIDC is not configured")
    config = _oidc_config()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    cookie = sign_session({"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next_path(next_path),
                           # Bound to this state/nonce by the signature: the callback reads it from here,
                           # never from its own query string, so it cannot be forged by a crafted link.
                           "remember": bool(remember)}, ttl_seconds=600)
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "redirect_uri": settings.public_url + "/auth/callback",
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(config["authorization_endpoint"] + "?" + urlencode(params), status_code=302)
    response.set_cookie(OIDC_COOKIE, cookie, httponly=True, secure=settings.public_url.startswith("https://"), samesite="lax", max_age=600, path="/")
    return response


def oidc_callback(request: Request, code: str, state: str) -> RedirectResponse:
    saved = verify_session(request.cookies.get(OIDC_COOKIE))
    if not saved or not secrets.compare_digest(saved.get("state", ""), state):
        raise HTTPException(status_code=400, detail="OIDC state validation failed")
    config = _oidc_config()
    token_resp = httpx.post(
        config["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.public_url + "/auth/callback",
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
            "code_verifier": saved["verifier"],
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    tokens = token_resp.json()
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="OIDC provider did not return an ID token")
    signing_key = _oidc_signing_key(id_token, config["jwks_uri"])
    # The issuer must be compared against the value the provider actually publishes.
    # settings.oidc_issuer has its trailing slash stripped (so the discovery URL can be
    # built by appending "/.well-known/..."), but providers that use a path-style issuer
    # -- Authentik among them -- emit `iss` *with* the trailing slash. Comparing the
    # stripped form literally fails with InvalidIssuerError. Trust the discovery
    # document's issuer, but only after checking it describes the issuer we configured,
    # so a substituted discovery document cannot redirect trust elsewhere.
    published_issuer = (config.get("issuer") or "").strip() or settings.oidc_issuer
    if published_issuer.rstrip("/") != settings.oidc_issuer.rstrip("/"):
        raise HTTPException(
            status_code=502,
            detail="OIDC discovery issuer does not match the configured issuer",
        )
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=[signing_key.algorithm_name],
        audience=settings.oidc_client_id,
        issuer=published_issuer,
        options={"require": ["exp", "iat", "iss", "sub"]},
    )
    if saved.get("nonce") and claims.get("nonce") != saved["nonce"]:
        raise HTTPException(status_code=400, detail="OIDC nonce validation failed")
    username = claims.get("preferred_username") or claims.get("email") or claims.get("name") or claims["sub"]
    user = _upsert_external_user(
        username, claims.get("email"), source="oidc", display_name=claims.get("name"),
        subject=str(claims["sub"]), email_verified=claims.get("email_verified") in (True, "true"),
        groups=claims.get("groups"),
    )
    response = RedirectResponse(safe_next_path(saved.get("next")), status_code=302)
    set_session(response, user, remember=bool(saved.get("remember")))
    response.delete_cookie(OIDC_COOKIE, path="/")
    return response


def _oidc_config() -> dict[str, Any]:
    r = httpx.get(settings.oidc_issuer + "/.well-known/openid-configuration", timeout=10, follow_redirects=False)
    r.raise_for_status()
    data = r.json()
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not data.get(key):
            raise HTTPException(status_code=502, detail=f"OIDC discovery missing {key}")
    return data


def _group_set(groups: Any) -> set[str] | None:
    """The claim as a set of names, or None when the token carried no groups claim at
    all (which is different from an empty list: absent data must never decide)."""
    if groups is None:
        return None
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple, set)):
        return None
    return {str(g) for g in groups}


def _groups_allowed(groups: Any) -> bool:
    allowed = {g.strip() for g in (settings.oidc_allowed_groups or "").split(",") if g.strip()}
    if not allowed:
        return True
    return bool(allowed & (_group_set(groups) or set()))


def _require_group(source: str, groups: Any) -> None:
    if source == "oidc" and not _groups_allowed(groups):
        raise HTTPException(status_code=403, detail="Your account is not in a group allowed to use bookdoe")


def group_mapping_configured() -> bool:
    return any((settings.oidc_group_admin, settings.oidc_group_adult, settings.oidc_group_kid))


def role_from_groups(groups: Any) -> tuple[str | None, bool]:
    """(account type the claim asks for, inconsistent?).

    None means the claim carried no mapped group (or no claim at all). admin + adult is
    fine (admin). The kid group next to admin or adult is contradictory: a child must
    never be widened by a stray membership, so that resolves to the most restrictive
    reading - `pending` - and is flagged so the sign-in writes an audit event.
    """
    names = _group_set(groups)
    if names is None or not group_mapping_configured():
        return None, False
    has = {
        "admin": bool(settings.oidc_group_admin) and not legacy.group_names(settings.oidc_group_admin).isdisjoint(names),
        "adult": bool(settings.oidc_group_adult) and not legacy.group_names(settings.oidc_group_adult).isdisjoint(names),
        "kid": bool(settings.oidc_group_kid) and not legacy.group_names(settings.oidc_group_kid).isdisjoint(names),
    }
    if has["kid"] and (has["admin"] or has["adult"]):
        return "pending", True
    if has["admin"]:
        return "admin", False
    if has["adult"]:
        return "adult", False
    if has["kid"]:
        return "kid", False
    return None, False


def _bootstrap_admin(username: str, subject: str | None) -> bool:
    wanted = {x.strip() for x in (settings.oidc_bootstrap_admins or "").split(",") if x.strip()}
    return bool(wanted) and (username in wanted or (subject or "") in wanted)


def _apply_claimed_role(row: dict[str, Any], claimed: str | None, inconsistent: bool, groups: Any) -> None:
    """Role source 'groups': make the stored type follow the validated claim on every
    sign-in. Guards: an absent or unmapped claim changes nothing (logged once per
    sign-in so a misconfigured Authentik mapping is visible, never silently
    destructive); the last administrator is never demoted by a claim."""
    current = account_type(row)
    if inconsistent:
        db.add_history("auth.role_claim_inconsistent",
                       f"'{row['username']}' is in contradictory bookdoe groups; type left as {current}",
                       data={"user_id": row["id"], "groups": sorted(_group_set(groups) or [])})
        return
    if claimed is None:
        db.add_history("auth.role_claim_ignored",
                       f"'{row['username']}' signed in without a mapped bookdoe group; type left as {current}",
                       data={"user_id": row["id"]})
        return
    if claimed == current:
        return
    if current == "admin" and db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1 AND id<>?", (row["id"],))["n"] == 0:
        db.add_history("auth.role_claim_refused",
                       f"Refused to demote '{row['username']}': last administrator", data={"user_id": row["id"], "claimed": claimed})
        return
    set_account_type(row["id"], claimed, actor="oidc-groups", reason="groups claim")


def set_account_type(user_id: str, kind: str, *, actor: str | None, reason: str | None = None) -> dict[str, Any]:
    """The one writer of is_admin/access_scope after creation. Any loss of privilege
    (admin -> anything, anything -> kid or pending) invalidates outstanding sessions,
    and every change is a history event naming who did it."""
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise ValueError("User not found")
    before = account_type(row)
    is_admin, scope = columns_for_account_type(kind)
    if before == kind:
        return row
    rank = {"admin": 3, "adult": 2, "kid": 1, "pending": 0}
    demotion = rank[kind] < rank[before]
    with db.tx() as conn:
        conn.execute("UPDATE users SET is_admin=?,access_scope=?,updated_at=? WHERE id=?", (is_admin, scope, db.now(), user_id))
        if demotion:
            bump_session_version(conn, user_id)
    db.add_history("auth.account_type_changed", f"'{row['username']}' {before} -> {kind}" + (f" ({reason})" if reason else ""),
                   data={"user_id": user_id, "from": before, "to": kind, "by": actor})
    if kind == "pending":
        revoke_user_devices(user_id, actor=actor, reason="demoted to pending")
    return db.one("SELECT * FROM users WHERE id=?", (user_id,))


def _upsert_external_user(
    username: str, email: str | None, *, source: str, display_name: str | None = None,
    subject: str | None = None, email_verified: bool = False, groups: Any = None,
) -> dict[str, Any]:
    """Resolve the account for an externally-authenticated user.

    Order: (1) the stored IdP subject, which is stable and unforgeable; (2) a VERIFIED
    email; (3) the username, which only lets an account that has never been linked adopt
    its first external identity (the documented rollout: create a local administrator,
    prove the deployment, switch to OIDC - the administrator keeps the same name, and
    `users.username` is UNIQUE, so an INSERT would collide). Steps 2 and 3 record the
    subject on the row, so every later sign-in matches at step 1 and a different IdP
    identity can no longer take the name over.

    `auth_source` is deliberately left untouched. login_local() filters on
    auth_source='local', so rewriting it here would silently disable the documented
    local break-glass login. `auth_subject` is what marks an account as linked.
    Proxy sign-ins have no subject; they use "proxy:<username>" as a stable stand-in.
    """
    subj = subject or f"{source}:{username}"
    row = db.one("SELECT * FROM users WHERE auth_subject=?", (subj,))
    linking = False
    if not row and email and email_verified:
        matches = db.all("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,))
        if len(matches) == 1:
            row, linking = matches[0], True
    if not row:
        row = db.one("SELECT * FROM users WHERE username=?", (username,))
        linking = row is not None
    # Never let one IdP identity take over an account already bound to another.
    if row and linking and row.get("auth_subject") and row["auth_subject"] != subj and source == "oidc":
        raise HTTPException(status_code=403, detail="That account is linked to a different identity")
    claimed, inconsistent = role_from_groups(groups) if source == "oidc" else (None, False)
    if row:
        if linking:
            _require_group(source, groups)
        with db.tx() as conn:
            conn.execute(
                "UPDATE users SET email=COALESCE(?,email),display_name=COALESCE(?,display_name),auth_subject=?,last_login_at=?,updated_at=? WHERE id=?",
                (email, display_name, subj, db.now(), db.now(), row["id"]),
            )
        if linking and row["auth_source"] != source:
            db.add_history(
                "auth.account_linked",
                f"{source} sign-in linked to existing account '{row['username']}' (auth_source={row['auth_source']})",
            )
        if source == "oidc" and settings.oidc_role_source == "groups":
            _apply_claimed_role(row, claimed, inconsistent, groups)
        return db.one("SELECT * FROM users WHERE id=?", (row["id"],))
    _require_group(source, groups)
    # Provisioning (least privilege). In order: an explicitly listed bootstrap admin; the
    # mapped group; the "first account on an empty install is the admin" rule, which an
    # install that names its bootstrap admins turns off; else pending. Nobody becomes an
    # adult, let alone an admin, just by being able to sign in.
    is_first = db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    if source == "oidc" and _bootstrap_admin(username, subject):
        kind, why = "admin", "BOOKDOE_OIDC_BOOTSTRAP_ADMINS"
    elif claimed is not None:
        kind, why = claimed, "groups claim" + (" (inconsistent, most restrictive)" if inconsistent else "")
    elif is_first and not (source == "oidc" and settings.oidc_bootstrap_admins):
        kind, why = "admin", "first account on an empty install"
    else:
        kind, why = "pending", "no mapped group; awaiting an administrator"
    is_admin, scope = columns_for_account_type(kind)
    uid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO users(id,username,email,display_name,auth_source,auth_subject,is_admin,access_scope,created_at,updated_at,last_login_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username, email, display_name, source, subj, is_admin, scope, t, t, t),
        )
    db.add_history("auth.account_provisioned", f"{source} sign-in created '{username}' as {kind} ({why})",
                   data={"user_id": uid, "account_type": kind, "groups": sorted(_group_set(groups) or [])})
    return db.one("SELECT * FROM users WHERE id=?", (uid,))
