"""First-run setup: the web wizard at /setup, the `bookdoe setup` CLI and the
secret-key bootstrap share this module.

Everything the wizard decides is written to the config volume, never into the image:
``<data dir>/setup.env`` (variables, 0600, merged into the environment at the next
start by secrets_provider.load_env_file) and ``<data dir>/setup.json`` (a summary
with no secret values). The wizard is available only while the install is empty
(local auth mode, zero users); afterwards its status is admin-only and the rest
answers 403. Re-running the CLI is safe: it merges variables, skips roots that
exist and never creates a second administrator.
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, auth, db, secrets_provider
from .config import settings
from .services import paths as path_guard

log = logging.getLogger("bookdoe.setup")

SECRET_KEY_VAR = "BOOKDOE_SECRET_KEY"
SETUP_SUMMARY = "setup.json"
METADATA_KEYS = {
    "google_books_api_key": "BOOKDOE_GOOGLE_BOOKS_API_KEY",
    "hardcover_api_key": "BOOKDOE_HARDCOVER_API_KEY",
    "audiobookdb_api_key": "BOOKDOE_AUDIOBOOKDB_API_KEY",
}
INFISICAL_KEYS = {
    "site_url": "INFISICAL_SITE_URL",
    "client_id": "INFISICAL_CLIENT_ID",
    "client_secret": "INFISICAL_CLIENT_SECRET",
    "project_id": "INFISICAL_PROJECT_ID",
    "environment": "INFISICAL_ENVIRONMENT",
    "secret_path": "INFISICAL_SECRET_PATH",
}


# ---------- env file ----------
def env_file_path() -> Path:
    configured = os.environ.get(secrets_provider.ENV_FILE_VAR)
    return Path(configured) if configured else settings.data_dir / "setup.env"


def _quote(value: str) -> str:
    if value == "" or any(ch in value for ch in " #\"'\\\n\t$"):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return value


def write_env_file(path: Path, updates: dict[str, str | None]) -> list[str]:
    """Merge ``updates`` into the env file (None deletes a key), keeping other keys.
    Written atomically with mode 0600. Returns the keys now present."""
    current = secrets_provider.parse_env_file(path.read_text(encoding="utf-8")) if path.exists() else {}
    for k, v in updates.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by the bookdoe setup wizard / CLI. Values here are loaded at start;",
             "# a variable set in the real environment always wins over this file."]
    lines += [f"{k}={_quote(v)}" for k, v in current.items()]
    fd, tmp = tempfile.mkstemp(prefix=".setup.env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sorted(current)


def ensure_secret_key(path: Path | None = None) -> bool:
    """Generate BOOKDOE_SECRET_KEY into the env file when neither the environment
    nor the file holds one. Returns True when a key was generated. Called by the
    container entrypoint (``bookdoe ensure-secret``) so a first start needs no
    manual step and the image still contains no secret."""
    if os.environ.get(SECRET_KEY_VAR):
        return False
    path = path or env_file_path()
    existing = secrets_provider.parse_env_file(path.read_text(encoding="utf-8")) if path.exists() else {}
    if existing.get(SECRET_KEY_VAR):
        os.environ[SECRET_KEY_VAR] = existing[SECRET_KEY_VAR]
        object.__setattr__(settings, "secret_key", existing[SECRET_KEY_VAR])
        return False
    value = _secrets.token_hex(32)
    write_env_file(path, {SECRET_KEY_VAR: value})
    os.environ[SECRET_KEY_VAR] = value
    object.__setattr__(settings, "secret_key", value)
    log.info("Generated %s into %s", SECRET_KEY_VAR, path)
    return True


# ---------- models ----------
class AdminSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(default="admin", min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=1024)
    email: str | None = Field(default=None, max_length=200)


class RootSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1024)
    media_kind: Literal["ebook", "audiobook", "mixed"] = "ebook"
    management_mode: Literal["observe", "adopt", "managed"] = "adopt"
    monitor_default: bool = True
    library_type: Literal["books", "podcasts"] = "books"


class MetadataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openreads_url: str | None = Field(default=None, max_length=300)
    google_books_api_key: str | None = Field(default=None, max_length=300)
    hardcover_api_key: str | None = Field(default=None, max_length=300)
    audiobookdb_api_key: str | None = Field(default=None, max_length=300)


class InfisicalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_url: str = Field(default="https://app.infisical.com", max_length=300)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str = Field(min_length=1, max_length=1024)
    project_id: str = Field(min_length=1, max_length=300)
    environment: str = Field(default="prod", max_length=80)
    secret_path: str = Field(default="/", max_length=300)


class SecretsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Literal["env", "infisical"] = "env"
    infisical: InfisicalSpec | None = None


class SetupCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roots: list[RootSpec] = Field(default_factory=list, max_length=50)
    metadata: MetadataSpec = Field(default_factory=MetadataSpec)
    secrets: SecretsSpec = Field(default_factory=SecretsSpec)


class SetupRequest(SetupCheckRequest):
    admin: AdminSpec
    public_url: str | None = Field(default=None, max_length=300)


# ---------- status ----------
def setup_required() -> bool:
    return settings.auth_mode == "local" and db.one("SELECT COUNT(*) n FROM users")["n"] == 0


def status() -> dict[str, Any]:
    path = env_file_path()
    summary = None
    try:
        summary = json.loads((settings.data_dir / SETUP_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {
        "version": __version__,
        "setup_required": setup_required(),
        "auth_mode": settings.auth_mode,
        "users": db.one("SELECT COUNT(*) n FROM users")["n"],
        "roots": db.one("SELECT COUNT(*) n FROM root_folders")["n"],
        "secret_key_configured": bool(settings.secret_key),
        "env_file": str(path),
        "env_file_exists": path.exists(),
        "env_file_writable": os.access(path.parent, os.W_OK) if path.parent.exists() else False,
        "secrets_backend": secrets_provider.resolver().backend_name,
        "openreads_url": settings.openreads_url,
        "public_url": settings.public_url,
        "completed": summary,
    }


# ---------- checks ----------
def check_path(spec: RootSpec) -> dict[str, Any]:
    p = Path(spec.path)
    out: dict[str, Any] = {"name": spec.name, "path": spec.path, "ok": False}
    if not p.is_absolute():
        out["error"] = "Path must be absolute (as seen inside the container, e.g. /books)"
        return out
    if not p.exists():
        out["error"] = "Path does not exist inside the container; check the volume mount"
        return out
    if not p.is_dir():
        out["error"] = "Path is not a directory"
        return out
    if not os.access(p, os.R_OK):
        out["error"] = "Not readable by the application user (PUID/PGID)"
        return out
    if spec.management_mode == "managed" and not os.access(p, os.W_OK):
        out["error"] = "Managed roots must be writable by the application user"
        return out
    errors = path_guard.validate_root(spec.path, data_dir=settings.data_dir, existing_roots=db.all("SELECT id,name,path FROM root_folders"))
    if errors:
        existing = db.one("SELECT 1 FROM root_folders WHERE path=?", (str(p),))
        if existing and len(errors) == 1:
            out.update(ok=True, note="Already configured")
            return out
        out["error"] = "; ".join(errors)
        return out
    try:
        entries = sum(1 for _ in p.iterdir())
    except OSError:
        entries = None
    out.update(ok=True, entries=entries, writable=os.access(p, os.W_OK))
    return out


def check_openreads(url: str | None) -> dict[str, Any]:
    url = (url or settings.openreads_url).rstrip("/")
    try:
        with httpx.Client(base_url=url, timeout=5) as c:
            r = c.get("/api/v2/health")
            r.raise_for_status()
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"ok": True, "url": url, "version": body.get("version")}
    except Exception as exc:
        return {"ok": False, "url": url, "error": f"OpenReads not reachable ({exc.__class__.__name__})"}


def _secrets_env(spec: SecretsSpec) -> dict[str, str | None]:
    out: dict[str, str | None] = {secrets_provider.BACKEND_VAR: spec.backend}
    if spec.backend == "infisical":
        if spec.infisical is None:
            raise ValueError("Infisical backend selected but no Infisical settings given")
        for field, var in INFISICAL_KEYS.items():
            out[var] = getattr(spec.infisical, field)
    else:
        for var in INFISICAL_KEYS.values():
            out[var] = None
    return out


def check_secrets(spec: SecretsSpec) -> dict[str, Any]:
    try:
        env = {k: v for k, v in os.environ.items()}
        for k, v in _secrets_env(spec).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        result = secrets_provider.SecretResolver(env).check()
    except ValueError as exc:
        return {"ok": False, "backend": spec.backend, "error": str(exc)}
    return result


def run_checks(req: SetupCheckRequest) -> dict[str, Any]:
    paths = [check_path(r) for r in req.roots]
    openreads = check_openreads(req.metadata.openreads_url)
    secrets_result = check_secrets(req.secrets)
    return {"paths": paths, "openreads": openreads, "secrets": secrets_result,
            "ok": all(p["ok"] for p in paths) and bool(secrets_result.get("ok"))}


# ---------- apply ----------
def create_root(name: str, path: str, media_kind: str, management_mode: str = "adopt", *, monitor_default: bool = True,
                enabled: bool = True, library_type: str = "books") -> dict[str, Any]:
    """Insert a library root after the same validation the management API applies.
    Raises ValueError (bad path or library type) or FileExistsError (already present).
    A ``podcasts`` library is read-only (observe) and is scanned by the podcast scanner right after it is created."""
    if library_type not in ("books", "podcasts"):
        raise ValueError(f"Unknown library type: {library_type}")
    if library_type == "podcasts":
        media_kind, management_mode = "audiobook", "observe"
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("Root path must be absolute")
    if db.one("SELECT 1 FROM root_folders WHERE path=?", (str(p),)):
        raise FileExistsError(str(p))
    errors = path_guard.validate_root(path, data_dir=settings.data_dir, existing_roots=db.all("SELECT id,name,path FROM root_folders"))
    if errors:
        raise ValueError("; ".join(errors))
    rid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at,library_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, name, str(p), media_kind, management_mode, int(monitor_default), int(enabled), t, t, library_type),
        )
    if library_type == "podcasts":
        from .services import scanner
        scanner.scan_root(rid)
    return db.one("SELECT * FROM root_folders WHERE id=?", (rid,))


def env_updates(req: SetupCheckRequest, public_url: str | None = None) -> dict[str, str | None]:
    """The variables the wizard persists. Empty strings are left untouched (None in
    the mapping means 'remove'); only an explicit value is written."""
    updates: dict[str, str | None] = {}
    if public_url:
        updates["BOOKDOE_PUBLIC_URL"] = public_url.rstrip("/")
    if req.metadata.openreads_url:
        updates["OPENREADS_URL"] = req.metadata.openreads_url.rstrip("/")
    for field, var in METADATA_KEYS.items():
        value = getattr(req.metadata, field)
        if value:
            updates[var] = value
    updates.update(_secrets_env(req.secrets))
    return updates


def apply(req: SetupRequest, *, env_path: Path | None = None) -> dict[str, Any]:
    """Create the administrator (only when none exists), add the roots and persist
    the variables. Idempotent for roots and variables."""
    env_path = env_path or env_file_path()
    created_admin = None
    if db.one("SELECT COUNT(*) n FROM users")["n"] == 0:
        created_admin = auth.create_local_user(req.admin.username, req.admin.password, email=req.admin.email, is_admin=True)
    roots_out = []
    for r in req.roots:
        try:
            create_root(r.name, r.path, r.media_kind, r.management_mode, monitor_default=r.monitor_default,
                        library_type=r.library_type)
            roots_out.append({"name": r.name, "path": r.path, "status": "created"})
        except FileExistsError:
            roots_out.append({"name": r.name, "path": r.path, "status": "exists"})
        except ValueError as exc:
            roots_out.append({"name": r.name, "path": r.path, "status": "rejected", "error": str(exc)})
    updates = env_updates(req, req.public_url)
    keys = write_env_file(env_path, updates)
    restart_required = any(k in updates and updates[k] and os.environ.get(k) != updates[k]
                           for k in ("BOOKDOE_PUBLIC_URL", "OPENREADS_URL", secrets_provider.BACKEND_VAR, *METADATA_KEYS.values()))
    summary = {
        "completed_at": db.now(), "version": __version__, "admin": req.admin.username,
        "roots": [{"name": r["name"], "path": r["path"], "status": r["status"]} for r in roots_out],
        "secrets_backend": req.secrets.backend,
        "metadata_sources": sorted(var for field, var in METADATA_KEYS.items() if getattr(req.metadata, field)),
        "env_keys": keys,
    }
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / SETUP_SUMMARY).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    db.add_history("setup.completed", "First-run setup completed", data={"admin": req.admin.username, "roots": len(roots_out), "secrets_backend": req.secrets.backend})
    return {"ok": True, "admin_user_id": created_admin, "roots": roots_out, "env_file": str(env_path), "restart_required": restart_required}
