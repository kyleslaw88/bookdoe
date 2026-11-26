"""Where configuration secrets come from.

Precedence, highest first:

1. the process environment (what Docker, systemd or the shell set);
2. the env file (``BOOKDOE_ENV_FILE``, default ``<data dir>/setup.env``, which the
   setup wizard writes); a variable already present in the environment is never
   overridden by the file;
3. the optional secrets backend (``BOOKDOE_SECRETS_BACKEND=infisical``), asked only
   for a name neither of the above provides.

Any value may be a *reference* instead of a literal:

- ``infisical://NAME`` or ``infisical:///folder/NAME`` fetches NAME from Infisical;
- ``file:///run/secrets/name`` reads a file (Docker or Kubernetes secrets);
- ``env://OTHER_NAME`` aliases another variable.

Values are never logged, never placed in exception messages and never echoed by
``describe()``; errors name the variable and the backend, nothing more. Infisical is
reached through its REST API with a machine identity (universal auth): set
``INFISICAL_CLIENT_ID``, ``INFISICAL_CLIENT_SECRET``, ``INFISICAL_PROJECT_ID`` and
optionally ``INFISICAL_SITE_URL`` (default https://app.infisical.com),
``INFISICAL_ENVIRONMENT`` (default ``prod``) and ``INFISICAL_SECRET_PATH`` (default ``/``).
The API shapes come from Infisical's public documentation and are exercised only
against a fake server in the tests (see docs/SECRETS.md).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from . import legacy

log = logging.getLogger("bookdoe.secrets")

ENV_FILE_VAR = "BOOKDOE_ENV_FILE"
BACKEND_VAR = "BOOKDOE_SECRETS_BACKEND"
BACKENDS = ("env", "infisical")
REFERENCE_SCHEMES = ("infisical", "file", "env")
_REF_RE = re.compile(r"^(infisical|file|env)://")


class SecretError(RuntimeError):
    """A secret could not be resolved. The message is safe to print: it never carries
    a secret value, only variable names, backend names and HTTP status codes."""


class SecretsProvider(Protocol):
    name: str

    def get(self, key: str) -> str | None: ...

    def check(self) -> dict[str, Any]: ...


# ---------- .env parsing ----------
def parse_env_file(text: str) -> dict[str, str]:
    """A small dotenv parser: ``KEY=value``, optional ``export``, ``#`` comments,
    single or double quotes (double quotes honour \\n and \\"). Later keys win."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            while quote == '"' and end > 0 and value[end - 1] == "\\":
                end = value.find(quote, end + 1)
            if end > 0:
                inner = value[1:end]
                value = inner.replace('\\n', "\n").replace('\\"', '"').replace("\\\\", "\\") if quote == '"' else inner
            # No closing quote: keep the raw text as written.
        else:
            # Unquoted: an inline comment starts at " #".
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def load_env_file(path: Path | None = None, *, environ: dict[str, str] | None = None) -> list[str]:
    """Merge the env file into ``environ`` (os.environ by default) without overriding
    existing variables. Returns the names that were set. Missing file: nothing."""
    env = os.environ if environ is None else environ
    legacy.apply_legacy_env(env)
    if path is None:
        configured = env.get(ENV_FILE_VAR)
        if configured:
            path = Path(configured)
        else:
            path = Path(env.get("BOOKDOE_DATA_DIR", "/config")) / "setup.env"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise SecretError(f"Cannot read env file {path}: {exc.__class__.__name__}") from exc
    added = []
    for k, v in parse_env_file(text).items():
        if k not in env:
            env[k] = v
            added.append(k)
    legacy.apply_legacy_env(env)   # an env file written before the rename still holds the old names
    if added:
        log.info("Loaded %d variable(s) from %s", len(added), path)
    return added


# ---------- providers ----------
class EnvProvider:
    """Process environment (after the env file was merged in)."""

    name = "env"

    def __init__(self, environ: dict[str, str] | None = None):
        self._env = os.environ if environ is None else environ

    def get(self, key: str) -> str | None:
        value = self._env.get(key)
        return value if value not in (None, "") else None

    def check(self) -> dict[str, Any]:
        return {"backend": "env", "ok": True}


class InfisicalProvider:
    """Infisical REST client using a machine identity (universal auth).

    Endpoints (from the public API reference):
      POST {site}/api/v1/auth/universal-auth/login  {clientId, clientSecret}
           -> {accessToken, expiresIn, accessTokenMaxTTL, tokenType}
      GET  {site}/api/v3/secrets/raw/{name}?workspaceId=&environment=&secretPath=
           Authorization: Bearer <accessToken> -> {secret: {secretKey, secretValue, ...}}
    """

    name = "infisical"
    REQUIRED = ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_PROJECT_ID")

    def __init__(self, environ: dict[str, str] | None = None, *, client_factory: Callable[[], Any] | None = None,
                 cache_ttl: float = 60.0, clock: Callable[[], float] = time.monotonic):
        env = os.environ if environ is None else environ
        missing = [k for k in self.REQUIRED if not env.get(k)]
        if missing:
            raise SecretError(f"{BACKEND_VAR}=infisical needs {', '.join(missing)} to be set")
        self.site = (env.get("INFISICAL_SITE_URL") or "https://app.infisical.com").rstrip("/")
        self.client_id = env["INFISICAL_CLIENT_ID"]
        self._client_secret = env["INFISICAL_CLIENT_SECRET"]
        self.project_id = env["INFISICAL_PROJECT_ID"]
        self.environment = env.get("INFISICAL_ENVIRONMENT") or "prod"
        self.secret_path = env.get("INFISICAL_SECRET_PATH") or "/"
        self.timeout = float(env.get("INFISICAL_TIMEOUT") or "10")
        self._client_factory = client_factory or self._default_client
        self._cache_ttl = cache_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires = 0.0
        self._cache: dict[tuple[str, str], tuple[float, str | None]] = {}

    def _default_client(self):
        import httpx
        return httpx.Client(base_url=self.site, timeout=self.timeout, headers={"User-Agent": "bookdoe-secrets"})

    def describe(self) -> dict[str, Any]:
        return {"backend": "infisical", "site": self.site, "project_id": self.project_id, "environment": self.environment,
                "secret_path": self.secret_path, "client_id_prefix": self.client_id[:8]}

    # -- auth --
    def _login(self) -> str:
        with self._client_factory() as client:
            try:
                resp = client.post("/api/v1/auth/universal-auth/login", json={"clientId": self.client_id, "clientSecret": self._client_secret})
            except Exception as exc:  # network errors carry URLs, not secrets, but keep it short
                raise SecretError(f"Infisical login failed: could not reach {self.site} ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise SecretError(f"Infisical login failed (HTTP {resp.status_code}) for client id {self.client_id[:8]}... at {self.site}")
        try:
            body = resp.json()
            token = body["accessToken"]
            ttl = float(body.get("expiresIn") or 300)
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretError("Infisical login answered 200 without an accessToken") from exc
        self._token = token
        self._token_expires = self._clock() + max(30.0, ttl - 30.0)
        return token

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires:
            return self._token
        return self._login()

    # -- secrets --
    def _fetch(self, key: str, path: str) -> str | None:
        token = self._access_token()
        with self._client_factory() as client:
            try:
                resp = client.get(f"/api/v3/secrets/raw/{key}", params={"workspaceId": self.project_id, "environment": self.environment,
                                                                        "secretPath": path},
                                  headers={"Authorization": f"Bearer {token}"})
            except Exception as exc:
                raise SecretError(f"Infisical request for {key} failed: could not reach {self.site} ({exc.__class__.__name__})") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            # Token expired early or was revoked: one fresh login, then give up.
            self._token = None
            token = self._login()
            with self._client_factory() as client:
                resp = client.get(f"/api/v3/secrets/raw/{key}", params={"workspaceId": self.project_id, "environment": self.environment,
                                                                        "secretPath": path},
                                  headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 404:
                return None
        if resp.status_code != 200:
            raise SecretError(f"Infisical returned HTTP {resp.status_code} for {key} (project {self.project_id}, env {self.environment}, path {path})")
        try:
            value = resp.json()["secret"]["secretValue"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretError(f"Infisical answer for {key} had no secret.secretValue") from exc
        return value if isinstance(value, str) else str(value)

    def get(self, key: str, path: str | None = None) -> str | None:
        path = path or self.secret_path
        cache_key = (path, key)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit[0] > now:
                return hit[1]
            value = self._fetch(key, path)
            self._cache[cache_key] = (now + self._cache_ttl, value)
            return value

    def check(self) -> dict[str, Any]:
        info = self.describe()
        try:
            self._login()
            info["ok"] = True
        except SecretError as exc:
            info["ok"] = False
            info["error"] = str(exc)
        return info


# ---------- resolver ----------
class SecretResolver:
    """Applies the precedence rules and expands references."""

    def __init__(self, environ: dict[str, str] | None = None, *, backend: SecretsProvider | None = None, backend_factory=None):
        self._env = os.environ if environ is None else environ
        self.env_provider = EnvProvider(self._env)
        self._backend = backend
        self._backend_factory = backend_factory
        self._backend_checked = False
        self._backend_error: SecretError | None = None

    @property
    def backend_name(self) -> str:
        return (self._env.get(BACKEND_VAR) or "env").strip().lower()

    def backend(self) -> SecretsProvider | None:
        """The configured backend, built lazily so an env-only install never imports
        or contacts anything. Raises SecretError when misconfigured."""
        if self._backend_error is not None:
            raise self._backend_error
        if self._backend is not None or self._backend_checked:
            return self._backend
        name = self.backend_name
        try:
            if name == "env":
                self._backend_checked = True
                return None
            if name != "infisical":
                raise SecretError(f"{BACKEND_VAR}={name!r} is not a known backend; use one of {', '.join(BACKENDS)}")
            self._backend = (self._backend_factory or (lambda: InfisicalProvider(self._env)))()
        except SecretError as exc:
            self._backend_error = exc
            raise
        self._backend_checked = True
        return self._backend

    def resolve_reference(self, value: str, *, for_key: str) -> str:
        scheme = _REF_RE.match(value).group(1)
        rest = value[len(scheme) + 3:]
        if scheme == "env":
            other = self._env.get(rest)
            if other in (None, ""):
                raise SecretError(f"{for_key} references env://{rest} but that variable is empty")
            return self.resolve_reference(other, for_key=rest) if _REF_RE.match(other) else other
        if scheme == "file":
            path = urlparse(value).path or rest
            try:
                return Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SecretError(f"{for_key} references {value} but the file could not be read ({exc.__class__.__name__})") from exc
        # infisical://NAME or infisical:///folder/sub/NAME
        backend = self.backend()
        if backend is None or backend.name != "infisical":
            raise SecretError(f"{for_key} references {value} but {BACKEND_VAR} is not 'infisical'")
        folder, _, name = rest.rpartition("/")
        if not name:
            raise SecretError(f"{for_key} references {value} which names no secret")
        folder = folder or None
        if folder is not None and not folder.startswith("/"):
            folder = "/" + folder
        found = backend.get(name, folder) if isinstance(backend, InfisicalProvider) else backend.get(name)
        if found is None:
            raise SecretError(f"{for_key} references {value} but Infisical has no secret named {name} in {folder or backend.secret_path if isinstance(backend, InfisicalProvider) else '/'}")
        return found

    def get(self, key: str, default: str = "") -> str:
        """The value for ``key`` by precedence, with references expanded."""
        value = self.env_provider.get(key)
        if value is not None:
            return self.resolve_reference(value, for_key=key) if _REF_RE.match(value) else value
        backend = self.backend()
        if backend is not None:
            found = backend.get(key)
            if found is not None:
                return found
        return default

    def check(self) -> dict[str, Any]:
        """Connectivity summary for the wizard and CLI. No values."""
        out: dict[str, Any] = {"backend": self.backend_name, "env_file": self._env.get(ENV_FILE_VAR) or str(Path(self._env.get("BOOKDOE_DATA_DIR", "/config")) / "setup.env")}
        try:
            backend = self.backend()
        except SecretError as exc:
            out.update(ok=False, error=str(exc))
            return out
        if backend is None:
            out["ok"] = True
        else:
            out.update(backend.check())
        return out


_resolver: SecretResolver | None = None


def resolver() -> SecretResolver:
    global _resolver
    if _resolver is None:
        load_env_file()
        _resolver = SecretResolver()
    return _resolver


def reset(environ: dict[str, str] | None = None, **kwargs) -> SecretResolver:
    """Rebuild the process resolver (tests, and the CLI after writing a new env file)."""
    global _resolver
    _resolver = SecretResolver(environ, **kwargs)
    return _resolver


def secret(key: str, default: str = "") -> str:
    """Config entry point: ``settings`` fields that hold credentials call this instead
    of os.getenv so every precedence rule and reference scheme applies to them."""
    return resolver().get(key, default)


def is_reference(value: str | None) -> bool:
    return bool(value) and bool(_REF_RE.match(value))
