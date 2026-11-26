from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from html import escape as html_escape
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any, Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from . import __version__, auth, crypto, db
from . import setup as setup_svc
from . import player_api as _player_api  # Player API module; also hosts the Kindle helpers
from . import discover_api as _discover_api  # Discover chat routes (Player lane)
from . import podcast_api as _podcast_api  # Podcast played-state routes (Player lane)
from . import podcast_library_api as _podcast_library_api  # Podcast show/season/episode read routes
from .config import settings
from .crypto import encrypt_secret, scrub_secrets
from .integrations import indexers as indexer_api, downloaders as downloader_api
from .services import buildinfo, family, readiness, metadata, scanner, migration, releases, downloads, importer, scheduler, monitoring, backup, covers, repair, localmeta, tagging, series as series_service, seriesnorm, recommendations, devices, failures, search as search_service, search_candidates as candidate_service, paths as path_guard, editions as editions_service, review_triage, review_bulk, kidsrecheck, signin, audience as audience_service, discover_chat, request_search, llmclient, reader, files as files_service, fileverify, fileops, maintenance, workmerge, workdupes, redundantfiles, filetrash, writelimit, quality, upgrades, wanted_search, acquisition as acquisition_svc, conversion, derived, assetformats, ownership, dupefinder, bulkmerge, managedbuild, mediatools

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx/httpcore log every request URL at INFO. Download-client and indexer APIs
# commonly carry credentials as query parameters (SABnzbd's ?apikey=, Newznab's
# &apikey=), so leaving these at INFO writes live secrets into container logs.
# bookdoe's own logging stays at the configured level.
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("bookdoe")


def worker_count_warning(env: dict[str, str] | None = None) -> str | None:
    """A warning when more than one worker process is configured (WEB_CONCURRENCY / UVICORN_WORKERS / GUNICORN_WORKERS).
    Merge, undo and the redundant-file cleanup serialise on a per-PROCESS lock (plus a database stamp that only holds off
    reconcile), so they are only safe under a single worker."""
    env = os.environ if env is None else env
    for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "WORKERS"):
        try:
            n = int(env.get(key, "") or 0)
        except ValueError:
            continue
        if n > 1:
            return (f"{key}={n}: bookdoe must run a single worker process; the merge/undo and file-cleanup locks are per process, "
                    "so concurrent workers could race on the same files")
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    warning = worker_count_warning()
    if warning:
        log.warning(warning)
    # A first start with no BOOKDOE_SECRET_KEY anywhere generates one into the config
    # volume (the container entrypoint does the same earlier); the image holds none.
    try:
        setup_svc.ensure_secret_key()
    except OSError as exc:  # read-only config volume: fall through to the clear error below
        log.warning("Could not persist the generated signing material to the config volume (%s)", exc.__class__.__name__)
    crypto.ensure_secret_configured()  # refuse to start on a guessable secret (C3)
    auth.warn_insecure_config()
    db.init_db()
    if auth._secure_cookies():
        auth.legacy_cookie_until()   # starts the clock on honouring pre-prefix session cookies (https only)
    repair.run_startup_repairs()
    scheduler.reap_stale_runs()
    discover_chat.reap_interrupted()
    managedbuild.on_startup()       # a build cut short by a restart waits for Resume; its journal is settled now
    auth.ensure_bootstrap_user()
    auth.ensure_anonymous_user()
    # Pre-warm capabilities so warnings are logged at startup, not on first request.
    mediatools.capabilities()
    task = asyncio.create_task(scheduler.run_scheduler()) if settings.scheduler_enabled else None
    log.info("%s %s started", settings.app_name, __version__)
    yield
    if task:
        scheduler.stop_scheduler()
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
    # Let an in-flight job finish writing its run row rather than killing the thread.
    await asyncio.to_thread(scheduler.shutdown_pool, True)
    # Let an in-flight mail delivery finish (bounded by its own timeout) before exit.
    await asyncio.to_thread(devices.shutdown_mail_pool, True)
    # A chat turn waiting on the model is not worth blocking exit for: its message is reaped on the next start.
    await asyncio.to_thread(discover_chat.shutdown_pool, False)
    # Same for a request search waiting on the indexers: queued ones are dropped, a running one is not awaited.
    await asyncio.to_thread(request_search.shutdown_pool, False)


def api_docs_enabled() -> bool:
    """Serve /docs, /redoc and /openapi.json? env BOOKDOE_API_DOCS (1/0) decides; unset, they
    are on for a plain-http (local) instance and off once BOOKDOE_PUBLIC_URL is https."""
    raw = os.environ.get("BOOKDOE_API_DOCS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return not settings.public_url.lower().startswith("https://")


_DOCS_ON = api_docs_enabled()
app = FastAPI(title=settings.app_name, version=__version__, description="Unified ebook and audiobook acquisition, library adoption, and automation platform", lifespan=lifespan,
              docs_url="/docs" if _DOCS_ON else None, redoc_url="/redoc" if _DOCS_ON else None,
              openapi_url="/openapi.json" if _DOCS_ON else None)

_CSP = ("default-src 'self'; script-src 'self' https://cdn.jsdelivr.net{inline}; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; media-src 'self' blob:; font-src 'self' data:; "
        "connect-src 'self' https://cdn.jsdelivr.net; frame-src 'self' blob: data:; worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
_HSTS = "max-age=31536000; includeSubDomains"


def _expiry_warn_days() -> float:
    """Warn window for API key expiry, in days: env API_KEY_EXPIRY_WARN_DAYS, default 14."""
    try:
        n = float(os.environ.get("API_KEY_EXPIRY_WARN_DAYS", "") or 14)
    except ValueError:
        return 14.0
    return n if n > 0 else 14.0


_expiry_warned_keys: set[str] = set()  # one log warning per key per process


def _custom_openapi():
    """Document the status codes every endpoint can return (401/403/429) so a schema-driven
    fuzzer does not flag them as 'Undocumented status code'."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    common = {
        "401": "Not authenticated",
        "403": "Forbidden (insufficient scope, read-only key, or CSRF)",
        "404": "Not found",
        "422": "Validation error",
        "429": "Too many requests",
    }
    for path_item in schema.get("paths", {}).values():
        for method, op in path_item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            for code, desc in common.items():
                op.setdefault("responses", {}).setdefault(code, {"description": desc})
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    path = request.url.path
    if path not in ("/docs", "/redoc"):  # the Swagger/ReDoc pages need inline script; they are opt-in anyway
        # /setup carries one inline script.
        response.headers.setdefault("Content-Security-Policy", _CSP.format(inline=" 'unsafe-inline'" if path == "/setup" else ""))
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip().lower()
    if proto == "https" or settings.public_url.lower().startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", _HSTS)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    # Pentest hardening: tell an API-key client its key is about to expire.
    key_exp = request.scope.get("alx_key_expires_at")
    if key_exp:
        days_left = (float(key_exp) - db.now()) / 86400
        if 0 < days_left <= _expiry_warn_days():
            response.headers.setdefault("X-API-Key-Expires-In-Days", str(max(1, int(days_left + 0.999))))
            kid = str(request.scope.get("alx_key_id") or key_exp)
            if kid not in _expiry_warned_keys:
                _expiry_warned_keys.add(kid)
                log.warning("Client id %s expires in %.1f days", kid, days_left)
    # Cookies decided during authentication: a rotated trusted-browser token, a renewed
    # remembered session, a dead device cookie to drop.
    auth.apply_pending_cookies(request, response)
    return response


# ---------- Models ----------
class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = False   # "Keep me signed in": a long-lived session cookie (see auth.set_session)


class SetupRequest(BaseModel):
    username: str = "admin"
    password: str = Field(min_length=12)
    email: str | None = None


class RootRequest(BaseModel):
    name: str
    path: str
    media_kind: Literal["ebook", "audiobook", "mixed"] = "ebook"
    management_mode: Literal["observe", "adopt", "managed"] = "adopt"
    monitor_default: bool = True
    enabled: bool = True
    library_type: Literal["books", "podcasts"] = "books"


class ScanRequest(BaseModel):
    auto_adopt_exact: bool = True


class AdoptRequest(BaseModel):
    work_id: str | None = None
    edition_id: str | None = None


class RefreshWorkRequest(BaseModel):
    # Supplied when the operator picks one of the review candidates by hand; omitted
    # to let the resolver decide.
    metadata_id: str | None = None


class AddWorkRequest(BaseModel):
    metadata_id: str
    monitored: bool = True


class PromoteCandidateRequest(BaseModel):
    monitored: bool = True
    work_id: str | None = None


class ScanCandidateEditRequest(BaseModel):
    """A constrained partial edit of a scan candidate's own detected fields.

    None means "leave alone" -- only fields actually present in the request
    body are applied (see edit_scan_candidate), matching scanner.py's
    SCAN_CANDIDATE_EDITABLE_FIELDS allow-list exactly (authors/identifiers,
    not author/identifier, to match the real detected_json key shape).
    """
    title: str | None = None
    authors: list[str] | None = None
    series: str | None = None
    series_position: str | None = None
    identifiers: dict[str, str] | None = None


class MonitorRequest(BaseModel):
    monitored: bool


class AuthorMonitorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy: Literal["all", "future", "latest", "none"] = "future"
    ebook: bool = False
    audiobook: bool = True


class PreferredEditionRequest(BaseModel):
    """Set the preferred edition for a specific media kind on a Work (Part XXVII)."""
    kind: Literal["ebook", "audiobook"]
    edition_id: str | None = None


# Declaring acquisition intent is what promotes a discovered slot (status 'none')
# to actively wanted ('missing'), and withdrawing it demotes the slot again. Owned
# assets keep their status: not wanting more copies is not the same as not owning it.
STATUS_ON_INTENT = "status=CASE WHEN status='owned' THEN status WHEN ?=1 THEN 'missing' ELSE 'none' END"


class AssetRequest(BaseModel):
    monitored: bool | None = None
    profile_id: str | None = None
    root_folder_id: str | None = None
    format: str | None = None
    # The explicit opt-in to upgrade searches (P5-27); 'none' withdraws it.
    upgrade_policy: Literal["none", "flag_only", "search"] | None = None


class IndexerRequest(BaseModel):
    name: str
    kind: Literal["prowlarr", "newznab", "torznab"]
    base_url: str
    api_key: str | None = None
    categories: list[int] = Field(default_factory=list)
    priority: int = 50
    enabled: bool = True


class DownloadClientRequest(BaseModel):
    name: str
    kind: Literal["sabnzbd", "qbittorrent", "nzbget", "transmission"]
    base_url: str
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    category: str | None = None
    priority: int = 50
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class GrabRequest(BaseModel):
    client_id: str | None = None
    force: bool = False


class ImportRequest(BaseModel):
    # None means "use the configured default" (see Settings -> Library).
    mode: Literal["hardlink", "copy", "move"] | None = None


class MigrationAnalyzeRequest(BaseModel):
    path: str
    media_kind: Literal["ebook", "audiobook"]
    source_name: str | None = None


class MigrationReconcileRequest(BaseModel):
    use_network: bool = True


class MigrationExecuteRequest(BaseModel):
    import_unresolved_stubs: bool = True


class ImportSettingsRequest(BaseModel):
    transfer_mode: Literal["hardlink", "copy", "move"]
    automatic: bool | None = None


class MatchingSettingsRequest(BaseModel):
    auto_accept_threshold: float
    probable_threshold: float


class NamingRequest(BaseModel):
    folder_template: str
    file_template: str
    multipart_template: str | None = None
    replace_illegal: bool = True
    colon_replacement: str = "smart"


class PathMappingRequest(BaseModel):
    client_id: str
    remote_path: str
    local_path: str


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    languages: list[str] | None = None
    language_policy: Literal["prefer", "reject", "ignore"] | None = None
    allowed_formats: list[str] | None = None
    preferred_formats: list[str] | None = None
    abridged: Literal["reject", "allow"] | None = None
    min_size_mb: float | None = Field(default=None, ge=0)
    max_size_mb: float | None = Field(default=None, ge=0)
    # Owned-media quality keys (P5-26); all optional, see services/quality.py.
    chapters_required: bool | None = None
    embedded_cover_preferred: bool | None = None
    min_bitrate_kbps: float | None = Field(default=None, ge=0)
    preferred_codecs: list[str] | None = None
    require_drm_free: bool | None = None
    kepub_derivative_ok: bool | None = None
    pdf_undesirable_if_epub: bool | None = None


class JobUpdateRequest(BaseModel):
    enabled: bool | None = None
    schedule_seconds: int | None = Field(default=None, ge=10)


class ApiKeyRequest(BaseModel):
    name: str
    # One of read / write / admin. Defaults to the least privilege; "scopes" is the
    # original list-shaped field, still accepted.
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_days: float | None = None


class WorkEditRequest(BaseModel):
    """A full replacement of the manually-editable fields on a work.

    None means "leave alone"; an empty string is a deliberate clear (for the optional
    text fields - title stays required). authors/series, when provided, replace the
    entire list rather than merge, matching how the edit form always submits the
    complete current state.
    """
    title: str | None = Field(default=None, min_length=1)
    subtitle: str | None = None
    description: str | None = None
    first_publish_date: str | None = None
    primary_language: str | None = None
    authors: list[str] | None = None
    series: list[dict[str, Any]] | None = None


class RatingRequest(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)


class CoverUrlRequest(BaseModel):
    url: str = Field(min_length=1)


class ReadStateRequest(BaseModel):
    status: Literal["want_to_read", "reading", "finished", "dnf"] | None = None


class TagCreateRequest(BaseModel):
    name: str
    color: str | None = None


class TagAssignRequest(BaseModel):
    tag_id: str


class TagRemoveWorksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ids: list[str] = Field(min_length=1, max_length=tagging.BULK_TAG_MAX)
    # Where the batch came from (the series page): recorded in the audit event, never
    # used to pick the works - the caller names them.
    series_id: str | None = Field(default=None, max_length=64)
    scope: Literal["all", "selected"] | None = None


class SeriesMonitorRequest(BaseModel):
    """Per-kind policies (Part XV), or the legacy {policy, ebook, audiobook} shape, not both."""
    model_config = ConfigDict(extra='forbid')
    audiobook_policy: Literal["none", "future", "all"] | None = None
    ebook_policy: Literal["none", "future", "all"] | None = None
    policy: Literal["all", "future", "none"] | None = None
    ebook: bool | None = None
    audiobook: bool | None = None

    @model_validator(mode="after")
    def _not_mixed(self):
        per_kind = self.audiobook_policy is not None or self.ebook_policy is not None
        legacy = self.policy is not None or self.ebook is not None or self.audiobook is not None
        if per_kind and legacy:
            raise ValueError("send audiobook_policy/ebook_policy or the legacy policy/ebook/audiobook, not both")
        return self


class UserCreateRequest(BaseModel):
    username: str
    password: str = Field(min_length=12)
    display_name: str | None = None
    email: str | None = None
    is_admin: bool = False
    access_scope: Literal["full", "kids", "pending"] = "full"
    # Preferred over is_admin/access_scope; when given it wins.
    account_type: Literal["admin", "adult", "kid", "pending"] | None = None


class UserUpdateRequest(BaseModel):
    display_name: str | None = None
    access_scope: Literal["full", "kids", "pending"] | None = None
    is_admin: bool | None = None
    account_type: Literal["admin", "adult", "kid", "pending"] | None = None
    kindle_email: str | None = None
    password: str | None = Field(default=None, min_length=12)
    # Trusted browsers stay in place across a password or role change unless the
    # administrator ticks this. Demotion to pending revokes them regardless.
    revoke_devices: bool = False


class SelfUpdateRequest(BaseModel):
    kindle_email: str | None = None


class SmtpConfigRequest(BaseModel):
    host: str
    port: int = 587
    username: str = ""
    password: str | None = None
    from_email: str
    use_tls: bool = True
    # starttls (587) | ssl (implicit TLS, 465) | none. Wins over use_tls when given.
    tls_mode: Literal["starttls", "ssl", "none"] | None = None


class AcquisitionSettingsRequest(BaseModel):
    """Partial update: only the fields sent change. Ranges are checked in wanted_search.validate_changes."""
    model_config = ConfigDict(extra="forbid")
    auto_grab_min_score: int | None = Field(default=None, ge=0, le=1000)
    wanted_search_daily_cap: int | None = None
    wanted_search_batch: int | None = None
    wanted_search_budget_minutes: int | None = None
    wanted_search_cooldown_hours: int | None = None
    wanted_search_delay_seconds: float | None = None


class ConversionSettingsRequest(BaseModel):
    """Partial update: only the fields sent change. Ranges are checked in conversion.validate_changes."""
    model_config = ConfigDict(extra="forbid")
    enabled_audio: StrictBool | None = None
    enabled_ebook: StrictBool | None = None
    audio_bitrate_mono_kbps: StrictInt | None = None
    audio_bitrate_stereo_kbps: StrictInt | None = None
    audio_concurrency: StrictInt | None = None
    ebook_concurrency: StrictInt | None = None
    auto_on_import: StrictBool | None = None
    only_if_no_preferred: StrictBool | None = None
    max_attempts: StrictInt | None = None
    retry_backoff_minutes: StrictInt | None = None
    disk_margin_mb: StrictInt | None = None
    queue_limit: StrictInt | None = None
    audiobook_output_root: StrictStr | None = None
    ebook_output_root: StrictStr | None = None


class ConversionBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ids: list[str] = Field(min_length=1, max_length=conversion.BULK_MAX_WORKS)
    dry_run: bool = True
    confirm: bool = False


class KindleSendRequest(BaseModel):
    asset_id: str


class ProgressRequest(BaseModel):
    """Where the reader/player is. Audio sends file_id + position (seconds); an ebook
    sends location (EPUB CFI or PDF page). percent is the comparable 0..1 for both."""
    model_config = ConfigDict(extra="forbid")
    file_id: str | None = None
    position: float | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, max_length=2000)
    percent: float | None = Field(default=None, ge=0, le=1)
    # Sync metadata (optional): which device, by that device's clock, and the audio
    # duration it observed. See reader.set_progress for the conflict rule.
    device_id: str | None = Field(default=None, max_length=120)
    client_updated_at: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0)


class ClassifyRequest(BaseModel):
    force: bool = False
    # classify-library only: how many unseen works one scan puts to the LLM.
    limit: int = Field(20, ge=1, le=200)


# ---------- Public/system ----------
@app.get("/health")
def health():
    """Unauthenticated liveness probe. The version is deliberately omitted (pentest LOW)."""
    return {"status": "ok", "name": settings.app_name}


@app.get("/api/v1/version")
def version_short():
    """Which build answers: sha and build time only. Unauthenticated so a signed-out or paired
    device can compare it with its own; internals are on /version/details (admin)."""
    return JSONResponse(buildinfo.short(), headers={"Cache-Control": "no-store"})


@app.get("/api/v1/version/details")
def version_details(user=Depends(auth.require_admin)):
    """Admin detail: version, sha, build and start time, API version and database migration step."""
    return JSONResponse(buildinfo.get(), headers={"Cache-Control": "no-store"})


@app.get("/readyz")
def readyz():
    ready, body = readiness.readiness()
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/api/v1/system/health")
def system_health(user=Depends(auth.require_full_access)):
    openreads = metadata.health()
    indexers = db.all("SELECT id,name,kind,last_ok_at,last_error,enabled FROM indexers ORDER BY name")
    clients = db.all("SELECT id,name,kind,last_ok_at,last_error,enabled FROM download_clients ORDER BY name")
    roots = db.all("SELECT id,name,path,media_kind,management_mode,last_scan_at,enabled FROM root_folders ORDER BY name")
    beat = scheduler.heartbeat()
    return {"application": {"status": "ok", "version": __version__}, "openreads": openreads, "indexers": indexers, "download_clients": clients, "roots": roots,
            "scheduler": {"enabled": settings.scheduler_enabled, "last_tick_age_seconds": None if beat is None else round(max(0.0, db.now() - beat)),
                          "running": scheduler.running_jobs()},
            "audience_backfill": audience_service.coverage()}


@app.get("/api/v1/system/tools")
def system_tools(user=Depends(auth.require_admin)):
    """Return availability and version info for external media tools.

    Admin-only endpoint.  Returns ffmpeg, ffprobe and mutagen status plus
    a ``supports`` dict that downstream plan-builders gate on.
    """
    return mediatools.capabilities()


# ---------- Authentication ----------
@app.get("/api/v1/auth/me")
def me(request: Request):
    user = auth.current_user(request)
    setup_required = settings.auth_mode == "local" and db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    if not user:
        return JSONResponse({"authenticated": False, "auth_mode": settings.auth_mode, "setup_required": setup_required,
                             "remember_days": signin.remember_days(), "acquisition_enabled": acquisition_svc.acquisition_enabled()}, status_code=401 if not setup_required else 200)
    kind = auth.account_type(user)
    return {"authenticated": True, "auth_mode": settings.auth_mode, "user": _me_public(user), "csrf": auth.csrf_for_user(user),
            "acquisition_enabled": acquisition_svc.acquisition_enabled(),
            "signin": {"remember_days": signin.remember_days(), "trust_allowed": signin.trust_allowed(kind) and settings.auth_mode in ("local", "oidc"),
                       "trusted_here": bool(auth.is_cookie_device(user) or auth.device_id_for_cookie(request, user["id"]))}}


def _me_public(user: dict[str, Any]) -> dict[str, Any]:
    """What the shells need to decide what to show. account_type is the product-facing
    role; is_admin/access_scope stay for older clients. Nothing here is trusted by the
    server - every route re-derives the type from the database row."""
    kind = auth.account_type(user)
    return {
        "id": user["id"], "username": user["username"], "email": user.get("email"), "display_name": user.get("display_name"),
        "is_admin": bool(user.get("is_admin")), "access_scope": user.get("access_scope", "full"),
        "account_type": kind, "auth_method": user.get("_auth_method"),
        "management": kind == "admin" and user.get("_auth_method") != "device",
        # Approvals surface in the Player: admins always; adults when an admin assigned
        # them kids. Count only - the kids themselves come from the approvals API.
        "approver_for": len(family.approver_kid_ids(user)) if kind in ("admin", "adult") else 0,
        "device": {"id": user.get("_device_id"), "name": user.get("_device_name"), "via": user.get("_device_via")} if user.get("_auth_method") == "device" else None,
    }


@app.post("/api/v1/auth/setup")
def setup(req: SetupRequest, response: Response):
    if settings.auth_mode != "local" or db.one("SELECT COUNT(*) n FROM users")["n"] != 0:
        raise HTTPException(403, "Initial setup is not available")
    uid = auth.create_local_user(req.username, req.password, email=req.email, is_admin=True)
    user = db.one("SELECT * FROM users WHERE id=?", (uid,))
    csrf = auth.set_session(response, user)
    return {"ok": True, "csrf": csrf}


# ---------- first-run setup wizard (docs/INSTALL.md) ----------
def _require_setup_open() -> None:
    """The wizard's write routes exist only while the install is empty; afterwards
    they answer 403 so nobody can re-run first-run setup against a live library."""
    if not setup_svc.setup_required():
        raise HTTPException(403, "Setup is complete; use Management to change settings")


@app.get("/api/v1/setup/status")
def setup_status(request: Request):
    """Open while setup is required; administrators only afterwards."""
    if not setup_svc.setup_required():
        auth.require_admin(request)
    return setup_svc.status()


@app.post("/api/v1/setup/check")
def setup_check(req: setup_svc.SetupCheckRequest, _=Depends(_require_setup_open)):
    return setup_svc.run_checks(req)


@app.post("/api/v1/setup/complete")
def setup_complete(req: setup_svc.SetupRequest, response: Response, _=Depends(_require_setup_open)):
    try:
        result = setup_svc.apply(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(500, f"Could not write the configuration file ({exc.__class__.__name__}); check that /config is writable")
    user = db.one("SELECT * FROM users WHERE id=?", (result["admin_user_id"],))
    result["csrf"] = auth.set_session(response, user)
    return result


@app.post("/api/v1/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    # 'oidc' deliberately allows this too: OIDC is the admin's SSO path, but a kids
    # account has no Authentik identity and needs a plain username/password the
    # household can actually use. Only 'proxy' (identity fully delegated to the
    # reverse proxy, no login form exists) and 'none' block it.
    if settings.auth_mode not in ("local", "oidc"):
        raise HTTPException(400, f"Local login is disabled (auth mode: {settings.auth_mode})")
    auth.throttle_check(request, req.username)
    user = auth.login_local(req.username, req.password)
    if not user:
        auth.throttle_failure(request, req.username)
        raise HTTPException(401, "Invalid username or password")
    auth.throttle_success(request, req.username)
    return {"ok": True, "csrf": auth.set_session(response, user, remember=req.remember)}


@app.post("/api/v1/auth/logout")
def logout(response: Response, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_signed_in)):
    # require_signed_in, not require_user: a pending account must be able to leave.
    if user.get("_auth_method") == "device" and not auth.is_cookie_device(user):
        raise HTTPException(403, "A device signs out by revoking itself (DELETE /api/player/v1/devices/{id})")
    # Signing out of a browser also forgets it as a trusted device; otherwise its cookie
    # would sign the person straight back in.
    auth.forget_trusted_device(request, response, user["id"])
    auth.clear_session(response)
    return {"ok": True}


class SignOutEverywhereRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revoke_trusted_devices: bool = False


@app.post("/api/v1/auth/logout-all")
def logout_everywhere(req: SignOutEverywhereRequest, response: Response, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """End every browser session of this account (session_version bump). Trusted browsers
    keep working unless revoke_trusted_devices is true: that is the person's choice, not a
    side effect. Session sign-in only."""
    if user.get("_auth_method") != "session":
        raise HTTPException(403, "Sign out everywhere needs a normal signed-in session")
    with db.tx() as conn:
        auth.bump_session_version(conn, user["id"])
    revoked = auth.revoke_user_devices(user["id"], actor=user["id"], reason="sign out everywhere") if req.revoke_trusted_devices else 0
    if req.revoke_trusted_devices and auth.device_cookie_present(request):
        auth.clear_device_cookie(response)
    auth.clear_session(response)
    return {"ok": True, "trusted_devices_revoked": revoked}


@app.get("/auth/login")
def oidc_login(next: str | None = None, remember: bool = False):
    # `next` lets the Management shell come back to /manage; anything off-origin is dropped.
    # `remember` only records the person's wish inside the signed, state-bound OIDC cookie.
    return auth.oidc_login_url(next, remember)


@app.get("/auth/callback")
def oidc_callback(request: Request, code: str, state: str):
    return auth.oidc_callback(request, code, state)


@app.post("/api/v1/auth/api-keys")
def create_api_key(req: ApiKeyRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_session_user)):
    try:
        kid, raw = auth.create_api_key(user["id"], req.name, req.scopes, expires_days=req.expires_days, creator_is_admin=bool(user.get("is_admin")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": kid, "key": raw, "warning": "This key is shown only once."}


@app.get("/api/v1/auth/api-keys")
def list_api_keys(all: bool = False, user=Depends(auth.require_session_user)):
    """Your own keys; an administrator may pass ?all=true to see everyone's."""
    return {"keys": auth.list_api_keys(None if (all and user.get("is_admin")) else user["id"])}


@app.delete("/api/v1/auth/api-keys/{key_id}")
def revoke_api_key(key_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_session_user)):
    if not auth.revoke_api_key(key_id, user_id=None if user.get("is_admin") else user["id"]):
        raise HTTPException(404, "API key not found")
    return {"ok": True}


@app.patch("/api/v1/auth/me")
def update_self(req: SelfUpdateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """The one account field every signed-in user - including a kids account - manages
    for themselves: where their own "Send to Kindle" goes."""
    if req.kindle_email is not None:
        _player_api.set_own_kindle_email(user, req.kindle_email)
    return {"ok": True}


# ---------- Users (household accounts) ----------
def _user_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "username": row["username"], "email": row.get("email"), "display_name": row.get("display_name"),
        "auth_source": row["auth_source"], "is_admin": bool(row["is_admin"]), "access_scope": row.get("access_scope", "full"),
        "account_type": auth.account_type(row), "linked_identity": bool(row.get("auth_subject")),
        "kindle_email": row.get("kindle_email"), "created_at": row["created_at"], "last_login_at": row.get("last_login_at"),
    }


@app.get("/api/v1/users")
def list_users(user=Depends(auth.require_admin)):
    return {"items": [_user_public(u) for u in db.all("SELECT * FROM users ORDER BY created_at")]}


@app.post("/api/v1/users")
def create_user(req: UserCreateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if db.one("SELECT id FROM users WHERE username=? COLLATE NOCASE", (req.username,)):
        raise HTTPException(409, "That username is already taken")
    is_admin, access_scope = (auth.columns_for_account_type(req.account_type) if req.account_type
                              else (req.is_admin, req.access_scope))
    try:
        uid = auth.create_local_user(
            req.username, req.password, email=req.email, is_admin=bool(is_admin), access_scope=access_scope,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if req.display_name:
        with db.tx() as conn:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (req.display_name, uid))
    return _user_public(db.one("SELECT * FROM users WHERE id=?", (uid,)))


@app.patch("/api/v1/users/{user_id}")
def update_user(user_id: str, req: UserUpdateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    target = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise HTTPException(404, "User not found")
    if req.account_type is not None:
        is_admin, access_scope = auth.columns_for_account_type(req.account_type)
        is_admin = bool(is_admin)
    else:
        access_scope = req.access_scope if req.access_scope is not None else target["access_scope"]
        is_admin = req.is_admin if req.is_admin is not None else bool(target["is_admin"])
    if access_scope in ("kids", "pending") and is_admin:
        raise HTTPException(400, "A kids-scope or pending account cannot be an administrator")
    if bool(target["is_admin"]) and not is_admin:
        remaining_admins = db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1 AND id<>?", (user_id,))["n"]
        if remaining_admins == 0:
            raise HTTPException(400, "Cannot remove the last administrator")
    fields, values = ["access_scope=?", "is_admin=?"], [access_scope, int(is_admin)]
    if req.display_name is not None:
        fields.append("display_name=?"); values.append(req.display_name)
    if req.kindle_email is not None:
        fields.append("kindle_email=?"); values.append(req.kindle_email or None)
    if req.password:
        from .crypto import hash_password
        fields.append("password_hash=?"); values.append(hash_password(req.password))
    # A password change or a loss of privilege must end sessions already issued (C6).
    revoke = bool(req.password) or (bool(target["is_admin"]) and not is_admin) or (
        access_scope in ("kids", "pending") and target["access_scope"] not in ("kids", "pending"))
    before = auth.account_type(target)
    with db.tx() as conn:
        conn.execute(f"UPDATE users SET {','.join(fields)},updated_at=? WHERE id=?", tuple(values + [db.now(), user_id]))  # nosec B608
        if revoke:
            auth.bump_session_version(conn, user_id)
    updated = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    after = auth.account_type(updated)
    if req.revoke_devices or after == "pending":
        auth.revoke_user_devices(user_id, actor=user["id"], reason="administrator request" if req.revoke_devices else "demoted to pending")
    if after != before:
        # Admin actions on accounts are auditable (same event set_account_type writes).
        db.add_history("auth.account_type_changed", f"'{target['username']}' {before} -> {after} (by {user.get('username') or user['id']})",
                       data={"user_id": user_id, "from": before, "to": after, "by": user["id"]})
    return _user_public(updated)


@app.delete("/api/v1/users/{user_id}")
def delete_user(user_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    target = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise HTTPException(404, "User not found")
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account while signed in as it")
    if target["is_admin"] and db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1 AND id<>?", (user_id,))["n"] == 0:
        raise HTTPException(400, "Cannot remove the last administrator")
    with db.tx() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


# ---------- Dashboard/library ----------
@app.get("/api/v1/dashboard")
def dashboard(user=Depends(auth.require_full_access)):
    counts = {
        "works": db.one("SELECT COUNT(*) n FROM works")["n"],
        "owned_ebooks": db.one("SELECT COUNT(DISTINCT work_id) n FROM media_assets WHERE kind='ebook' AND status='owned'")["n"],
        "owned_audiobooks": db.one("SELECT COUNT(DISTINCT work_id) n FROM media_assets WHERE kind='audiobook' AND status='owned'")["n"],
        # Must stay in lockstep with the /api/v1/wanted predicate, or the dashboard
        # advertises a Wanted count the Wanted page cannot show.
        "wanted_ebooks": db.one("SELECT COUNT(*) n FROM media_assets WHERE kind='ebook' AND status='missing' AND monitored=1 AND has_acquisition_intent=1")["n"],
        "wanted_audiobooks": db.one("SELECT COUNT(*) n FROM media_assets WHERE kind='audiobook' AND status='missing' AND monitored=1 AND has_acquisition_intent=1")["n"],
        "active_downloads": db.one("SELECT COUNT(*) n FROM downloads WHERE state IN ('grabbed','downloading','completed','import_pending','importing')")["n"],
        "review": db.one("SELECT COUNT(*) n FROM review_tasks WHERE status='open'")["n"],
    }
    recent = db.all("SELECT * FROM history ORDER BY created_at DESC LIMIT 20")
    for x in recent:
        x["data"] = db.json_load(x.pop("data_json"), {})
    queue = db.all("SELECT d.*,w.title FROM downloads d LEFT JOIN works w ON w.id=d.work_id WHERE d.state NOT IN ('imported','failed','cancelled') ORDER BY d.updated_at DESC LIMIT 10")
    recently_finished = db.all(
        """SELECT rs.finished_at,w.id work_id,w.title FROM read_states rs JOIN works w ON w.id=rs.work_id
           WHERE rs.user_id=? AND rs.status='finished' ORDER BY rs.finished_at DESC LIMIT 8""",
        (user["id"],),
    )
    return {
        "counts": counts, "recent": recent, "queue": queue, "metadata_health": metadata.health(), "covers": covers.stats(),
        "failures": failures.summary(), "recently_finished": recently_finished,
    }


def _library_filters(user: dict, q: str | None, status: str | None, kind: str | None, author_id: str | None, series_id: str | None,
                     read_status: str | None, tag_id: str | None, for_counts: bool = False) -> tuple[list[str], list[Any]]:
    """The library WHERE clauses and parameters. ``for_counts`` leaves out the media kind, the state and the
    owned-something default, so the State dropdown can count every state under the other filters."""
    visible, vparams = tagging.kids_visibility(user)
    params: list[Any] = list(vparams)
    where = [visible]
    state = ownership.normalize_state(status) if not (kind and status == "missing") else None
    # Default: only show owned works (Part X - Library shows owned books by default). "Nothing owned" is the
    # one state that lists works without a file: the ones that are wanted but have none.
    if not for_counts and state != "nothing":
        where.append(ownership.OWNS_ANY)
    if q:
        where.append("(w.title LIKE ? OR EXISTS (SELECT 1 FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=w.id AND c.name LIKE ?))")
        params += [f"%{q}%", f"%{q}%"]
    if not for_counts:
        # `kind` and `status` describe ONE asset, not two independent facts about the work.
        # Filtering to "Audiobooks" means "I own the audiobook": a work whose audiobook is
        # merely wanted (status=missing) while only the ebook is owned must not match. With
        # both given as a raw asset status, the single asset must satisfy both - kind=audiobook&status=missing
        # is "works whose audiobook is still missing". The new state values (complete / partial / nothing,
        # services/ownership.py) judge the whole Work per kind and compose with `kind` as "owns that kind"
        # ("wants that kind" for nothing).
        if state:
            where.append(ownership.state_clause(state))
            if kind and state == "nothing":
                where.append("EXISTS (SELECT 1 FROM media_assets ax WHERE ax.work_id=w.id AND ax.kind=? AND ax.monitored=1 AND ax.derived_from_asset_id IS NULL)")
                params.append(kind)
            elif kind:
                where.append("EXISTS (SELECT 1 FROM media_assets ax WHERE ax.work_id=w.id AND ax.kind=? AND ax.status='owned')")
                params.append(kind)
        elif kind and status:
            where.append("EXISTS (SELECT 1 FROM media_assets ax WHERE ax.work_id=w.id AND ax.kind=? AND ax.status=?)")
            params += [kind, status]
        elif kind:
            where.append("EXISTS (SELECT 1 FROM media_assets ax WHERE ax.work_id=w.id AND ax.kind=? AND ax.status='owned')")
            params.append(kind)
        elif status:
            where.append("EXISTS (SELECT 1 FROM media_assets ax WHERE ax.work_id=w.id AND ax.status=?)")
            params.append(status)
    if author_id:
        where.append("EXISTS (SELECT 1 FROM work_contributors wc WHERE wc.work_id=w.id AND wc.contributor_id=? AND wc.role='author')")
        params.append(author_id)
    if series_id:
        where.append("EXISTS (SELECT 1 FROM series_memberships sm WHERE sm.work_id=w.id AND sm.series_id=?)")
        params.append(series_id)
    if tag_id:
        where.append("EXISTS (SELECT 1 FROM work_tags wt WHERE wt.work_id=w.id AND wt.tag_id=?)")
        params.append(tag_id)
    if read_status:
        where.append("EXISTS (SELECT 1 FROM read_states rs WHERE rs.work_id=w.id AND rs.user_id=? AND rs.status=?)")
        params += [user["id"], read_status]
    return where, params


@app.get("/api/v1/library")
def library(
    q: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    author_id: str | None = None,
    series_id: str | None = None,
    read_status: str | None = None,
    tag_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_user),
):
    where, params = _library_filters(user, q, status, kind, author_id, series_id, read_status, tag_id)
    clause = " AND ".join(where)
    # The total is what makes a paged library honest: without it the UI could only
    # report the size of the page it happened to load, which read as "your library
    # has 300 books" on a library of twelve thousand.
    total = db.one(f"SELECT COUNT(*) n FROM works w WHERE {clause}", tuple(params))["n"]  # nosec B608
    sql = f"SELECT w.* FROM works w WHERE {clause} ORDER BY COALESCE(w.sort_title,w.title) LIMIT ? OFFSET ?"  # nosec B608
    works = db.all(sql, tuple(params + [limit, offset]))
    return {"items": _batch_library_summary(works, user), "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/library-state-counts")
def library_state_counts(
    q: str | None = None,
    author_id: str | None = None,
    series_id: str | None = None,
    read_status: str | None = None,
    tag_id: str | None = None,
    user=Depends(auth.require_user),
):
    """Works per State filter value under the other filters, for the counts in the State dropdown:
    owned (owns something), complete, partial (partly owned) and nothing (wanted, nothing owned)."""
    where, params = _library_filters(user, q, None, None, author_id, series_id, read_status, tag_id, for_counts=True)
    return ownership.state_counts(" AND ".join(where), params)


@app.get("/api/v1/library/{work_id}")
def work_detail(work_id: str, user=Depends(auth.require_user)):
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w or not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    result = _library_summary(w, user)
    result["description"] = w.get("description")
    result["identifiers"] = db.all("SELECT namespace,value,source,verified,confidence FROM identifiers WHERE entity_type='work' AND entity_id=?", (work_id,))
    result["editions"] = editions_service.ordered_editions(w)
    # Preferred editions (Part XXVII): include the full edition objects for the
    # preferred ebook and audiobook editions when set, so the UI can display them
    # without a second round-trip.
    result["preferred_ebook_edition"] = editions_service.preferred_edition(w, "ebook", fallback=False)
    result["preferred_audiobook_edition"] = editions_service.preferred_edition(w, "audiobook", fallback=False)
    # Per-viewer, like rating/read_status: where this account is in each asset, so the
    # detail page can offer Resume instead of a cold Read/Listen.
    result["progress"] = reader.progress_for_work(work_id, user["id"])
    if user.get("is_admin") and not auth.is_kids_scope(user):
        # Per-asset "Convert to M4B/EPUB" state for the Management book page (admin only).
        options = conversion.options_for_assets(result["assets"])
        for a in result["assets"]:
            a["convert"] = options.get(a["id"])
    if not auth.is_kids_scope(user) and user.get("_auth_method") != "device":
        # Management shows whether the kids shelf state is a human decision (and whose)
        # or merely unclassified; the classifier never touches a decided work.
        d = tagging.kids_decision(work_id)
        result["kids_decision"] = {"state": d["state"], "via": d["via"], "by": d.get("decided_by_name"), "at": d["decided_at"]} if d else None
        if d and d["via"] == "auto_approved":
            meta = db.json_load(d.get("note"), {}) or {}
            result["kids_decision"].update({"confidence": meta.get("confidence"), "model": meta.get("model")})
        result["kids_evidence"] = tagging.kids_explanation(work_id)
    if not assetformats.sees_paths(user):
        # Acquisition activity (grabs, imports, metadata pulls) is operator-facing
        # noise on a kids account, not something a child needs to see.
        result["history"] = []
    else:
        result["history"] = db.all("SELECT id,event_type,message,data_json,created_at FROM history WHERE work_id=? ORDER BY created_at DESC LIMIT 100", (work_id,))
        for h in result["history"]:
            h["data"] = db.json_load(h.pop("data_json"), {})
    return result


@app.put("/api/v1/library/{work_id}/preferred-edition")
def set_preferred_edition(work_id: str, req: PreferredEditionRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Set (or clear) the preferred edition for a specific media kind on a Work (Part XXVII).

    The edition_id must belong to the same work.  Passing edition_id=None clears
    the preference for that kind.  No kind-check is enforced on the edition's
    media_assets (an edition can legitimately serve either purpose depending on
    context), because the spec says "an Edition can legitimately serve either
    purpose depending on context" -- the preference is about Work-level display,
    not Edition partitioning.
    """
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    if req.edition_id is not None:
        # Validate the edition belongs to this work.
        ed = db.one("SELECT id FROM editions WHERE id=? AND work_id=?", (req.edition_id, work_id))
        if not ed:
            raise HTTPException(400, "Edition does not belong to this work")
    col = "preferred_ebook_edition_id" if req.kind == "ebook" else "preferred_audiobook_edition_id"
    with db.tx() as conn:
        conn.execute(
            f"UPDATE works SET {col}=?,updated_at=? WHERE id=?",  # nosec B608
            (req.edition_id, db.now(), work_id),
        )
    return work_detail(work_id, user)


@app.patch("/api/v1/library/{work_id}")
def edit_work(work_id: str, req: WorkEditRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Manual metadata correction - the terminal step when no provider has this book.

    Marks metadata_source='manual' so neither the scheduled resync nor a future batch
    pull silently overwrites what an operator typed in by hand (see scheduler.py).
    """
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    fields, values = [], []
    for column, value in (
        ("title", req.title), ("subtitle", req.subtitle), ("description", req.description),
        ("first_publish_date", req.first_publish_date), ("primary_language", req.primary_language),
    ):
        if value is not None:
            fields.append(f"{column}=?")
            values.append(value)
    with db.tx() as conn:
        if fields:
            conn.execute(
                f"UPDATE works SET {','.join(fields)},sort_title=COALESCE(?,sort_title),metadata_source='manual',metadata_status='resolved',updated_at=? WHERE id=?",  # nosec B608
                tuple(values + [metadata._sort_title(req.title) if req.title else None, db.now(), work_id]),
            )
        elif req.authors is not None or req.series is not None:
            conn.execute("UPDATE works SET metadata_source='manual',updated_at=? WHERE id=?", (db.now(), work_id))
        if req.authors is not None:
            conn.execute("DELETE FROM work_contributors WHERE work_id=?", (work_id,))
            for i, name in enumerate(a for a in req.authors if a and a.strip()):
                cid = db.contributor_id_for_name(conn, name)
                if cid:
                    conn.execute("INSERT OR IGNORE INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',?)", (work_id, cid, i))
        if req.series is not None:
            prior_series = {r["series_id"] for r in conn.execute("SELECT series_id FROM series_memberships WHERE work_id=?", (work_id,)).fetchall()}
            # Series dropped by this edit keep their evidence: a manually_suppressed assertion
            # records the decision so later automatic linking cannot bring them back.
            new_names = {(s.get("name") or "").strip().lower() for s in req.series if s.get("name")}
            for sid in prior_series:
                srow = conn.execute("SELECT name FROM series WHERE id=?", (sid,)).fetchone()
                sname = (srow["name"] or "").strip().lower() if srow else ""
                if sname and sname not in new_names:
                    conn.execute(
                        """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
                           proposed_position,evidence_type,confidence,status,created_at,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.uid(), work_id, sid, "manual", "", srow["name"], None, "manual_suppression", 1.0,
                         "manually_suppressed", db.now(), db.now()),
                    )
            conn.execute("DELETE FROM series_memberships WHERE work_id=?", (work_id,))
            for s in req.series:
                name = (s.get("name") or "").strip()
                if not name:
                    continue
                row = conn.execute("SELECT id FROM series WHERE name=? COLLATE NOCASE", (name,)).fetchone()
                sid = row["id"] if row else db.uid()
                if not row:
                    conn.execute(
                        "INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,?,?,?,?)",
                        (sid, name, name, db.now(), db.now()),
                    )
                conn.execute("INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work_id, sid, s.get("position")))
                metadata._insert_series_assertion(
                    conn, work_id, sid, name, name, s.get("position"),
                    "manual_edit", 1.0, "manually_confirmed", db.now(), source="manual",
                )
                if sid not in prior_series:
                    series_service.apply_future_policy(conn, work_id, sid)
    wanted_search.reset_cooldown(work_id=work_id)  # new title/authors change the query: try again at once
    db.add_history("metadata.manual_edit", "Metadata edited manually", work_id=work_id)
    return work_detail(work_id, user)


class MetadataChooseRequest(BaseModel):
    observation_id: str


class MetadataManualRequest(BaseModel):
    value: str


def _field_action(fn, *args):
    try:
        return fn(*args)
    except metadata.FieldError as exc:
        raise HTTPException(exc.status, str(exc))


@app.get("/api/v1/library/{work_id}/metadata/{field}")
def work_metadata_field(work_id: str, field: str, user=Depends(auth.require_full_access)):
    return _field_action(metadata.get_field, work_id, field)


@app.post("/api/v1/library/{work_id}/metadata/{field}/choose")
def work_metadata_choose(work_id: str, field: str, req: MetadataChooseRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return _field_action(metadata.choose_observation, work_id, field, req.observation_id)


@app.post("/api/v1/library/{work_id}/metadata/{field}/manual")
def work_metadata_manual(work_id: str, field: str, req: MetadataManualRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return _field_action(metadata.set_manual, work_id, field, req.value, user.get("id"))


@app.post("/api/v1/library/{work_id}/metadata/{field}/restore-automatic")
def work_metadata_restore(work_id: str, field: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return _field_action(metadata.restore_automatic, work_id, field)


@app.post("/api/v1/library/{work_id}/metadata/{field}/lock")
def work_metadata_lock(work_id: str, field: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return _field_action(metadata.set_lock, work_id, field, True, user.get("id"))


@app.post("/api/v1/library/{work_id}/metadata/{field}/unlock")
def work_metadata_unlock(work_id: str, field: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return _field_action(metadata.set_lock, work_id, field, False)


@app.put("/api/v1/library/{work_id}/rating")
def set_rating(work_id: str, req: RatingRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    with db.tx() as conn:
        if req.rating is None:
            conn.execute("DELETE FROM ratings WHERE work_id=? AND user_id=?", (work_id, user["id"]))
        else:
            conn.execute(
                """INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(work_id,user_id) DO UPDATE SET rating=excluded.rating,updated_at=excluded.updated_at""",
                (work_id, user["id"], req.rating, db.now()),
            )
    return {"work_id": work_id, "rating": req.rating}


@app.put("/api/v1/library/{work_id}/read-state")
def set_read_state(work_id: str, req: ReadStateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    t = db.now()
    with db.tx() as conn:
        if req.status is None:
            conn.execute("DELETE FROM read_states WHERE work_id=? AND user_id=?", (work_id, user["id"]))
        else:
            existing = conn.execute("SELECT started_at FROM read_states WHERE work_id=? AND user_id=?", (work_id, user["id"])).fetchone()
            started_at = existing["started_at"] if existing and existing["started_at"] else (t if req.status in ("reading", "finished") else None)
            finished_at = t if req.status == "finished" else None
            # source='manual': this is the person's own choice, which the reader's
            # automatic 'reading' marking must never override (see reader.py).
            conn.execute(
                """INSERT INTO read_states(work_id,user_id,status,started_at,finished_at,updated_at,source) VALUES (?,?,?,?,?,?,'manual')
                   ON CONFLICT(work_id,user_id) DO UPDATE SET status=excluded.status,
                     started_at=COALESCE(read_states.started_at,excluded.started_at),
                     finished_at=excluded.finished_at,updated_at=excluded.updated_at,source='manual'""",
                (work_id, user["id"], req.status, started_at, finished_at, t),
            )
    return {"work_id": work_id, "status": req.status}


@app.get("/api/v1/library/{work_id}/tags")
def get_work_tags(work_id: str, user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    return {"items": tagging.work_tags(work_id)}


@app.post("/api/v1/library/{work_id}/tags")
def assign_tag(work_id: str, req: TagAssignRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        if req.tag_id == tagging.kids_tag_id():
            # The kids tag is an access decision: record who made it so the classifier
            # never reverts it (tagging.set_kids_tag_manually).
            if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
                raise ValueError("Work not found")
            tagging.set_kids_tag_manually(work_id, True, by_user_id=user.get("id"), by_name=user.get("username"))
        else:
            tagging.add_tag(work_id, req.tag_id, source="manual")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"items": tagging.work_tags(work_id)}


@app.delete("/api/v1/library/{work_id}/tags/{tag_id}")
def unassign_tag(work_id: str, tag_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    if tag_id == tagging.kids_tag_id() and db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        tagging.set_kids_tag_manually(work_id, False, by_user_id=user.get("id"), by_name=user.get("username"))
    else:
        had = db.one("SELECT 1 FROM work_tags WHERE work_id=? AND tag_id=?", (work_id, tag_id)) is not None
        tagging.remove_tag(work_id, tag_id)
        if had:
            db.add_history("tag.removed", f"Tag removed by {user.get('username') or 'admin'}", work_id=work_id, data={"tag_id": tag_id, "by": user.get("id")})
    return {"items": tagging.work_tags(work_id)}


@app.post("/api/v1/library/{work_id}/classify")
def classify_work(work_id: str, req: ClassifyRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return tagging.classify_kids_for_work(work_id, force=req.force)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/v1/library/{work_id}/recommendations")
def get_recommendations(work_id: str, force: bool = False, user=Depends(auth.require_user)):
    if force:
        auth.require_write_scope(user)  # force=true spends LLM work; not for read-only keys or kids
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    try:
        return recommendations.recommend_for_work(work_id, viewer=user, force=force)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/v1/library/{work_id}/cover")
def work_cover(work_id: str, user=Depends(auth.require_user)):
    """Serve cached cover art for a work.

    bookdoe serves the bytes itself rather than putting a provider URL in the
    page: a remote <img src> would announce every browsed title to the cover host
    and break whenever that host is unreachable. A work with no cover answers 404
    and the UI falls back to its title tile.
    """
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "No cover available")
    hit = covers.cached(work_id)
    if not hit:
        raise HTTPException(404, "No cover available")
    # Overrides the API-wide no-store: the file is immutable for a given work until
    # a refresh replaces it, and covers are the heaviest thing the library grid loads.
    return FileResponse(
        hit["path"],
        media_type=hit.get("content_type") or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/v1/library/{work_id}/cover/refresh")
def work_cover_refresh(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return covers.resolve_cover(work_id, force=True, allow_remote=settings.cover_remote_fetch)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.put("/api/v1/library/{work_id}/cover")
def work_cover_set_url(work_id: str, req: CoverUrlRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Set a cover from a URL the user found themselves - for a book none of the
    automatic sources have (or have wrong). Never overwritten by a later
    refresh; see covers.resolve_cover()'s manual-source guard."""
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    try:
        data = covers.fetch_manual(req.url)
        return covers.set_manual(work_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/v1/library/{work_id}/cover/upload")
async def work_cover_upload(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Same as the URL form, for a cover the user has as a local file. The body
    is the raw image bytes (Content-Type: image/*) - no multipart dependency."""
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    data = await request.body()
    try:
        return covers.set_manual(work_id, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/v1/library/{work_id}/cover")
def work_cover_clear(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Drop a manually-set cover and let automatic resolution try again."""
    covers.clear_manual(work_id)
    try:
        return covers.resolve_cover(work_id, force=True, allow_remote=settings.cover_remote_fetch)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/library/{work_id}/refresh")
def work_refresh(work_id: str, req: RefreshWorkRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Refresh a single work's metadata against OpenReads, and its cover with it."""
    try:
        result = metadata.refresh_work(work_id, metadata_id=req.metadata_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Metadata refresh failed: {exc}")
    if result.get("status") == "linked":
        try:
            result["cover"] = covers.resolve_cover(work_id, force=True, allow_remote=settings.cover_remote_fetch)
        except Exception as exc:
            # A missing cover is not a failed refresh; report it without failing.
            result["cover"] = {"status": "error", "error": str(exc)}
    # Whatever the provider said, the user's own files may still know the series, the
    # year or the narrator. This only fills gaps, so it cannot undo a good match.
    try:
        result["local"] = localmeta.harvest(work_id)["applied"]
    except Exception as exc:
        result["local"] = {"error": str(exc)}
    return result


@app.patch("/api/v1/library/{work_id}/monitor")
def monitor_work(work_id: str, req: MonitorRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    if not db.one("SELECT id FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    with db.tx() as conn:
        conn.execute("UPDATE works SET monitored=?,updated_at=? WHERE id=?", (int(req.monitored), db.now(), work_id))
    wanted_search.reset_cooldown(work_id=work_id)
    return {"ok": True}


@app.patch("/api/v1/assets/{asset_id}")
def patch_asset(asset_id: str, req: AssetRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    if not asset:
        raise HTTPException(404, "Asset not found")
    if asset.get("derived_from_asset_id"):
        raise HTTPException(409, "This is a converted copy; monitoring and acquisition settings belong to the original asset")
    fields, values = [], []
    for field in ("monitored", "profile_id", "root_folder_id", "format", "upgrade_policy"):
        value = getattr(req, field)
        if value is not None:
            fields.append(f"{field}=?")
            values.append(int(value) if field == "monitored" else value)
    # Toggling monitored on a single asset is the explicit "I want this medium"
    # action, so it is also what records acquisition intent (Part XI): catalog
    # discovery creates the empty slot, the user switching it on is what wants it.
    if req.monitored is not None:
        fields.append("has_acquisition_intent=?")
        values.append(int(req.monitored))
        fields.append(STATUS_ON_INTENT)
        values.append(int(req.monitored))
    if fields:
        with db.tx() as conn:
            conn.execute(f"UPDATE media_assets SET {','.join(fields)},updated_at=? WHERE id=?", tuple(values + [db.now(), asset_id]))  # nosec B608
    if req.monitored is not None or req.profile_id is not None or req.format is not None:
        wanted_search.reset_cooldown(asset_id=asset_id)
    if req.upgrade_policy is not None and req.upgrade_policy != asset["upgrade_policy"]:
        db.add_history(
            "asset.upgrade_policy",
            f"Upgrade policy for {asset['kind']} set to '{req.upgrade_policy}' (was '{asset['upgrade_policy']}') by {user.get('username') or user['id']}",
            work_id=asset["work_id"], asset_id=asset_id,
            data={"from": asset["upgrade_policy"], "to": req.upgrade_policy},
        )
    return db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))


@app.get("/api/v1/upgrades")
def list_upgrades(user=Depends(auth.require_full_access)):
    """Better-release candidates for owned assets that opted in with upgrade_policy='search'.
    Read-only: grab one with the normal release grab; replacing the file is a manual plan."""
    items = upgrades.list_candidates()
    return {"items": items, "total": len(items)}



@app.get("/api/v1/assets/{asset_id}/quality")
def asset_quality(asset_id: str, user=Depends(auth.require_full_access)):
    """The stored owned-media quality evaluation; state is 'unevaluated' until one has run."""
    asset = db.one("SELECT work_id FROM media_assets WHERE id=?", (asset_id,))
    if not asset or not tagging.is_visible_to(asset["work_id"], user):
        raise HTTPException(404, "Asset not found")
    return quality.stored_evaluation(asset_id) or {"state": quality.UNEVALUATED, "reasons": ["Not evaluated yet"], "profile_id": None, "evaluated_at": None}


@app.post("/api/v1/assets/{asset_id}/quality/evaluate")
def asset_quality_evaluate(asset_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Evaluate one asset against its profile from stored data; changes nothing on disk."""
    result = quality.evaluate_asset(asset_id)
    if result is None:
        raise HTTPException(404, "Asset not found")
    return result


# ---------- MediaFile detail ----------
@app.get("/api/v1/files/{file_id}")
def file_detail(file_id: str, request: Request, user=Depends(auth.require_user)):
    """Return one MediaFile with its asset, work, owning root, mutability and integrity state.

    This is an operator-only surface: kids-scope users get 404 (not 403) so they
    cannot learn the existence of files.
    """
    if auth.is_kids_scope(user):
        raise HTTPException(404, "File not found")
    if not user.get("is_admin"):
        raise HTTPException(403, "Not available on a kids account")
    result = files_service.get_file_detail(file_id)
    if result is None:
        raise HTTPException(404, "File not found")
    if not tagging.is_visible_to(result["work"]["id"], user):
        raise HTTPException(404, "File not found")
    return result


@app.post("/api/v1/files/{file_id}/verify")
def file_verify(file_id: str, deep: bool = False, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Read-only integrity check of one file; stores integrity_state and verified_at."""
    result = fileverify.verify_file(file_id, deep=deep)
    if result is None:
        raise HTTPException(404, "File not found")
    return result


@app.post("/api/v1/files/{file_id}/rescan")
def file_rescan(file_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Re-read one file from disk and refresh its row (never writes to the file)."""
    result = fileverify.rescan_file(file_id)
    if result is None:
        raise HTTPException(404, "File not found")
    return result

# ---------- Maintenance issues (Part XXV) ----------
class MaintenanceRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detectors: list[str] | None = None


@app.get("/api/v1/works/merge-preview")
def work_merge_preview(canonical: str, sources: str, user=Depends(auth.require_admin)):
    """Read-only evidence and plan for merging the source Works into the canonical one (P5-23)."""
    try:
        return workmerge.preview(canonical, [s.strip() for s in sources.split(",")])
    except workmerge.WorkNotFound:
        raise HTTPException(404, "Canonical Work not found")
    except workmerge.BadRequest as exc:
        raise HTTPException(400, str(exc))


class WorkMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical: str
    sources: list[str]
    resolutions: dict = {}


@app.post("/api/v1/works/merge")
def work_merge(req: WorkMergeRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Merge the source Works into the canonical one in one transaction, with a reversal ledger (P5-24)."""
    _write_budget(user)
    try:
        return workmerge.execute(req.canonical, req.sources, req.resolutions, user, confirm_dissimilar=req.resolutions.get("confirm_dissimilar") is True)
    except workmerge.WorkNotFound:
        raise HTTPException(404, "Canonical Work not found")
    except workmerge.BadRequest as exc:
        raise HTTPException(400, str(exc))
    except workmerge.MergeBlocked as exc:
        raise HTTPException(409, {"message": str(exc), "blockers": exc.blockers})


# ---------- Duplicate Works: candidates, history, undo; redundant files and trash (admin only) ----------
def _write_budget(user: dict) -> None:
    try:
        writelimit.check(user)
    except writelimit.RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)})


@app.get("/api/v1/works/merge-candidates")
def work_merge_candidates(series_id: str | None = None, work_id: str | None = None, work_ids: str | None = None, user=Depends(auth.require_admin)):
    """Duplicate candidates for a series (groups), one book (similar Works) or a picked set (cards, suggested canonical)."""
    if series_id:
        if not db.one("SELECT 1 x FROM series WHERE id=?", (series_id,)):
            raise HTTPException(404, "Series not found")
        groups = workdupes.series_groups(series_id)
        for g in groups:
            g["cards"] = workdupes.work_cards(g["work_ids"])
        return {"scope": "series", "series_id": series_id, "groups": groups}
    if work_id:
        if not db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
            raise HTTPException(404, "Work not found")
        cands = workdupes.work_candidates(work_id)
        return {"scope": "work", "work_id": work_id, "candidates": cands, "cards": workdupes.work_cards([work_id, *[c["work_id"] for c in cands]])}
    if work_ids:
        ids = [i.strip() for i in work_ids.split(",") if i.strip()]
        if len(ids) < 2 or len(ids) > workmerge.MAX_SOURCES + 1:
            raise HTTPException(400, f"Pick between 2 and {workmerge.MAX_SOURCES + 1} Works")
        found = workdupes.describe(ids)
        if len(found["work_ids"]) != len(set(ids)):
            raise HTTPException(404, "One or more Works were not found")
        return {"scope": "selection", **found}
    raise HTTPException(400, "Give series_id, work_id or work_ids")


@app.get("/api/v1/works/merges")
def work_merges(limit: int = 50, user=Depends(auth.require_admin)):
    """Recent merges, newest first, each with its status (executed, reversed, reversal_failed)."""
    return {"items": workmerge.list_merges(limit)}


class WorkMergeReverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    allow_kid_widening: bool = False


@app.post("/api/v1/works/merges/{merge_id}/reverse")
def work_merge_reverse(merge_id: str, req: WorkMergeReverseRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Undo a merge from its ledger. 409 lists rows that changed since unless force is set; files in the trash and later merges block it even then."""
    _write_budget(user)
    try:
        return workmerge.reverse(merge_id, bool(req and req.force), user, allow_kid_widening=bool(req and req.allow_kid_widening))
    except workmerge.MergeNotFound:
        raise HTTPException(404, "Merge not found")
    except workmerge.ReversalRefused as exc:
        raise HTTPException(409, {"message": exc.message, "code": exc.code, "conflicts": [], **exc.detail})
    except workmerge.ReversalBlocked as exc:
        raise HTTPException(409, {"message": str(exc), "conflicts": exc.conflicts})
    except workmerge.ReversalFailed as exc:
        raise HTTPException(409, {"message": "The database refused the restore: " + str(exc), "conflicts": []})


# ---------- Library-wide duplicate finder and bulk merge (admin only; services/dupefinder.py, bulkmerge.py) ----------
def _start_job(kind: str, config: dict | None = None) -> dict:
    """Hand a manual job to the pool and return at once (the page polls /admin/duplicates/status)."""
    job = db.one("SELECT * FROM jobs WHERE kind=?", (kind,))
    if not job:
        db.ensure_default_jobs()
        job = db.one("SELECT * FROM jobs WHERE kind=?", (kind,))
    if config:
        merged = {**(db.json_load(job.get("config_json"), {}) or {}), **config}
        with db.tx() as conn:
            conn.execute("UPDATE jobs SET config_json=? WHERE id=?", (db.json_dump(merged), job["id"]))
    try:
        item = scheduler.submit_job(job["id"], trigger="manual")
    except scheduler.JobBusy as exc:
        raise HTTPException(409, str(exc))
    item.pop("future", None)
    return item


class DuplicateScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: bool = False


class DuplicateHarvestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(25, ge=1, le=500)


class DuplicateDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dismissed: bool = True


class DuplicateSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_ids: list[str] = Field(..., min_length=1, max_length=bulkmerge.MAX_SELECTION)
    canonicals: dict[str, str] = {}


class BulkMergeRequest(DuplicateSelectionRequest):
    stop_on_error: bool = False
    refresh: bool = True


class BatchUndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class PlaceholderCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apply: bool = False
    work_id: str | None = None


@app.get("/api/v1/admin/duplicates/status")
def duplicates_status(user=Depends(auth.require_admin)):
    """Last scan, progress of a running one, group counts by confidence, open refresh conflicts and queued refreshes."""
    bulkmerge.recover_running()      # a batch stuck on a dead worker shows its true state (cheap, bounded)
    out = dupefinder.status()
    out["refresh_queue"] = db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='pending'")["n"]
    out["refresh_held"] = db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='held'")["n"]
    out["refresh_grace_days"] = bulkmerge.grace_days()
    out["running"] = [r for r in scheduler.running_jobs() if r["kind"].startswith("duplicate-")]
    return out


@app.get("/api/v1/admin/duplicates")
def duplicates_list(confidence: str | None = None, kind: str | None = None, has_owned: bool | None = None, series_id: str | None = None,
                    q: str | None = None, dismissed: bool = False, light: bool = False, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                    user=Depends(auth.require_admin)):
    """Groups with side-by-side member cards (``light`` leaves the cards out: ids and ranking only, for select-all)."""
    return dupefinder.list_groups(confidence=confidence, kind=kind, has_owned=has_owned, series_id=series_id, q=q, dismissed=dismissed,
                                  limit=limit, offset=offset, with_cards=not light)


@app.post("/api/v1/admin/duplicates/scan", status_code=202)
def duplicates_scan(req: DuplicateScanRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Start (or resume) the scan as a job; incremental unless ``full``."""
    return _start_job("duplicate-scan", {"full": bool(req and req.full)})


@app.post("/api/v1/admin/duplicates/harvest", status_code=202)
def duplicates_harvest(req: DuplicateHarvestRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """One-off: retry the metadata refresh of up to ``limit`` file-only Works to record 'another work holds this record' conflicts."""
    return _start_job("duplicate-harvest", {"batch": req.limit if req else 25})


@app.post("/api/v1/admin/duplicates/refresh-queue/run", status_code=202)
def duplicates_refresh_run(_=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _start_job("duplicate-refresh")


@app.post("/api/v1/admin/duplicates/dry-run")
def duplicates_dry_run(req: DuplicateSelectionRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Per group: what would move, and the blockers that keep it out of a bulk run. Writes nothing."""
    return bulkmerge.dry_run(req.group_ids, req.canonicals)


@app.post("/api/v1/admin/duplicates/bulk-merge")
def duplicates_bulk_merge(req: BulkMergeRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Create a batch from the selection and merge its first chunk (one transaction per group, each with its own ledger entry).
    Call /batches/{id}/continue until the batch is no longer 'running'."""
    _write_budget(user)
    b = bulkmerge.start(req.group_ids, user, req.canonicals, req.stop_on_error, req.refresh)
    return _after_chunk(bulkmerge.process(b["id"], user), user)


def _after_chunk(b: dict, user: dict) -> dict:
    """Deliberately does NOT start the metadata refresh: a refresh rewrites the rows the merge ledger recorded and would make
    the batch's one-click Undo refuse. The kept books wait ('held') until the admin presses "Finish batch / refresh metadata"
    (POST /batches/{id}/finalize) or, when the duplicate-refresh queue next runs (a manual job unless scheduled), the grace period
    (setting duplicates.refresh_grace_days, default 7) has passed; that run marks the batch finalized."""
    return b


@app.post("/api/v1/admin/duplicates/batches/{batch_id}/finalize")
def duplicates_batch_finalize(batch_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Accept the batch and queue the metadata refresh of its kept books. After this the batch's Undo is no longer guaranteed
    to be one click (the refresh may change rows the merge recorded)."""
    try:
        out = bulkmerge.finalize(batch_id)
    except LookupError:
        raise HTTPException(404, "Batch not found")
    except bulkmerge.BatchBusy as exc:
        raise HTTPException(409, str(exc))
    if out["queued_refreshes"]:
        try:
            _start_job("duplicate-refresh")      # the queue drains in the background, rate limited
        except HTTPException:
            pass                                  # already running: it will pick the new items up
    return out


@app.post("/api/v1/admin/duplicates/batches/{batch_id}/continue")
def duplicates_batch_continue(batch_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return _after_chunk(bulkmerge.process(batch_id, user), user)
    except LookupError:
        raise HTTPException(404, "Batch not found")


@app.get("/api/v1/admin/duplicates/batches")
def duplicates_batches(limit: int = Query(20, ge=1, le=100), user=Depends(auth.require_admin)):
    return {"items": bulkmerge.list_batches(limit)}


@app.get("/api/v1/admin/duplicates/batches/{batch_id}")
def duplicates_batch(batch_id: str, user=Depends(auth.require_admin)):
    try:
        bulkmerge._close(batch_id)      # recovers dead claims first
        return bulkmerge.batch(batch_id)
    except LookupError:
        raise HTTPException(404, "Batch not found")


@app.post("/api/v1/admin/duplicates/batches/{batch_id}/undo")
def duplicates_batch_undo(batch_id: str, req: BatchUndoRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Undo every merge of the batch, newest first; stops at the first one that cannot be undone cleanly and says why."""
    try:
        return bulkmerge.undo(batch_id, user, force=bool(req and req.force))
    except LookupError:
        raise HTTPException(404, "Batch not found")
    except bulkmerge.BatchBusy as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/admin/duplicates/{group_id}/dismiss")
def duplicates_dismiss(group_id: str, req: DuplicateDismissRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """'Not the same book': remembered until a member changes."""
    out = dupefinder.dismiss(group_id, req.dismissed if req else True, user)
    if not out:
        raise HTTPException(404, "Group not found")
    return out


@app.post("/api/v1/admin/placeholder-assets/cleanup")
def placeholder_cleanup(req: PlaceholderCleanupRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Dry run (default) or removal of empty placeholder assets that sit beside an owned asset of the same kind."""
    apply = bool(req and req.apply)
    if apply:
        _write_budget(user)
    return ownership.cleanup_placeholders(apply=apply, work_id=req.work_id if req else None, user=user)


@app.get("/api/v1/works/{work_id}/redundant-files")
def work_redundant_files(work_id: str, user=Depends(auth.require_admin)):
    if not db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    return redundantfiles.analyse(work_id)


class RedundantRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str]
    confirm_similar: bool = False
    confirm_count: int | None = None


def _trash_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except filetrash.TrashError as exc:
        status = 404 if exc.code in ("not_found", "work_not_found") else 409 if exc.code in ("last_copy", "path_in_use", "destination_exists", "not_redundant", "purged", "trash_file_missing", "root_not_managed", "work_gone", "file_missing", "unsafe_file", "unsafe_trash_folder", "whole_set_required", "confirmation_required", "not_trashed", "outside_managed_roots", "corrupt_entry", "content_changed", "unverified", "move_failed", "links_unsupported") else 400
        raise HTTPException(status, {"message": str(exc), "code": exc.code, **exc.detail})


@app.post("/api/v1/works/{work_id}/redundant-files/remove")
def work_redundant_remove(work_id: str, req: RedundantRemoveRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Move the listed redundant files to the root's trash folder (record only for adopted roots). Never the last copy."""
    _write_budget(user)
    return _trash_call(filetrash.remove_redundant, work_id, req.file_ids, user, confirm_similar=req.confirm_similar, confirm_count=req.confirm_count)


@app.get("/api/v1/trash")
def trash_list(status: str = "trashed", limit: int = 200, user=Depends(auth.require_admin)):
    return _trash_call(filetrash.list_trash, status, limit)


class TrashSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: int
    ledger_retention_days: int | None = None


@app.put("/api/v1/trash/settings")
def trash_settings(req: TrashSettingsRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    _write_budget(user)
    out = {"retention_days": _trash_call(filetrash.set_retention_days, req.retention_days)}
    if req.ledger_retention_days is not None:
        if not 1 <= req.ledger_retention_days <= 36500:
            raise HTTPException(400, "ledger_retention_days must be between 1 and 36500")
        db.set_setting(workmerge.LEDGER_RETENTION_KEY, req.ledger_retention_days)
    out["ledger_retention_days"] = workmerge.ledger_retention_days()
    return out


class TrashPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] | None = None
    expired: bool = False


@app.post("/api/v1/trash/purge")
def trash_purge(req: TrashPurgeRequest, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Delete trashed files for good: the listed entries, or every expired one."""
    _write_budget(user)
    if not req.ids and not req.expired:
        raise HTTPException(400, "Give ids, or expired=true")
    return _trash_call(filetrash.purge, req.ids, expired_only=True, user=user)


@app.post("/api/v1/trash/{trash_id}/restore")
def trash_restore(trash_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    _write_budget(user)
    return _trash_call(filetrash.restore, trash_id, user)


@app.get("/api/v1/maintenance/issues")
def maintenance_issues(
    kind: str | None = None,
    status: str | None = Query(None, pattern="^(open|acknowledged|dismissed|resolved|repaired)$"),
    severity: str | None = Query(None, pattern="^(info|warning|error)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_admin),
):
    return maintenance.list_issues(kind=kind, status=status, severity=severity, limit=limit, offset=offset)


@app.get("/api/v1/maintenance/summary")
def maintenance_summary(user=Depends(auth.require_admin)):
    return maintenance.summary()


@app.get("/api/v1/maintenance/issues/{issue_id}")
def maintenance_issue(issue_id: str, user=Depends(auth.require_admin)):
    issue = maintenance.get_issue(issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    return issue


def _maintenance_decision(fn, issue_id: str, user: dict):
    try:
        issue = fn(issue_id, user.get("id"))
    except maintenance.IssueConflict as exc:
        raise HTTPException(409, str(exc))
    if issue is None:
        raise HTTPException(404, "Issue not found")
    return issue


@app.post("/api/v1/maintenance/issues/{issue_id}/acknowledge")
def maintenance_acknowledge(issue_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _maintenance_decision(maintenance.acknowledge, issue_id, user)


@app.post("/api/v1/maintenance/issues/{issue_id}/dismiss")
def maintenance_dismiss(issue_id: str, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _maintenance_decision(maintenance.dismiss, issue_id, user)


@app.post("/api/v1/maintenance/run")
def maintenance_run(req: MaintenanceRunRequest | None = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Run detectors now (default: all). Detectors are read-only; only issue rows change."""
    try:
        return maintenance.run_all(req.detectors if req else None)
    except KeyError as exc:
        raise HTTPException(400, f"Unknown detector: {exc.args[0]}")
    except maintenance.MaintenanceBusy as exc:
        raise HTTPException(409, str(exc))


# ---------- Authors / discovery monitoring ----------
@app.get("/api/v1/authors")
def authors(
    role: str = "author",
    q: str | None = None,
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_user),
):
    if role not in monitoring.BROWSE_ROLES:
        raise HTTPException(400, "Unknown contributor role")
    items = monitoring.list_authors(role, q)
    if auth.is_kids_scope(user):
        # An author with zero kid-visible works has no business appearing on a kids
        # account at all - not even as a name.
        visible_clause, vparams = tagging.kids_visibility(user)
        visible_ids = {
            r["contributor_id"] for r in db.all(
                f"""SELECT DISTINCT wc.contributor_id FROM work_contributors wc
                    JOIN works w ON w.id=wc.work_id AND {visible_clause}
                    WHERE wc.role=?""",  # nosec B608
                (*vparams, role),
            )
        }
        items = [a for a in items if a["id"] in visible_ids]
    # No limit keeps the historical "everything" response; total is pre-paging either way.
    total = len(items)
    if limit is not None or offset:
        items = items[offset:offset + limit] if limit is not None else items[offset:]
    return {"items": items, "total": total}


@app.get("/api/v1/authors/{contributor_id}")
def author_detail(contributor_id: str, role: str = "author", user=Depends(auth.require_user)):
    if role not in monitoring.BROWSE_ROLES:
        raise HTTPException(400, "Unknown contributor role")
    item = monitoring.author_detail(contributor_id, role)
    if not item:
        raise HTTPException(404, "Author not found")
    if auth.is_kids_scope(user):
        item["works"] = [w for w in item["works"] if tagging.is_visible_to(w["id"], user)]
        if not item["works"]:
            raise HTTPException(404, "Author not found")
    # A person who holds no (visible) credit in a non-author role is not a narrator/
    # editor/...; the author default keeps its pre-existing "any contributor" answer.
    if role != "author" and not item["works"]:
        raise HTTPException(404, "Author not found")
    return item


@app.put("/api/v1/authors/{contributor_id}/monitor")
def author_monitor(contributor_id: str, req: AuthorMonitorRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        monitoring.set_author_monitor(contributor_id, policy=req.policy, ebook=req.ebook, audiobook=req.audiobook)
        return monitoring.author_detail(contributor_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/authors/{contributor_id}/refresh")
def author_refresh(contributor_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return monitoring.refresh_author(contributor_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/calendar")
def calendar_events(start: str | None = None, end: str | None = None, user=Depends(auth.require_full_access)):
    # Dates are text because providers may only know a year/month. The UI keeps
    # approximate dates visibly approximate rather than manufacturing precision.
    rows = db.all("""SELECT DISTINCT w.id work_id,w.title,w.cover_url,w.first_publish_date,e.release_date,
                             GROUP_CONCAT(DISTINCT a.kind) media_kinds
                      FROM works w LEFT JOIN editions e ON e.work_id=w.id
                      LEFT JOIN media_assets a ON a.work_id=w.id AND a.monitored=1
                      WHERE w.monitored=1 AND (w.first_publish_date IS NOT NULL OR e.release_date IS NOT NULL)
                      GROUP BY w.id,e.release_date ORDER BY COALESCE(e.release_date,w.first_publish_date)""")
    # A work with a valid preferred edition is shown on that edition's release
    # date(s) only; works with no preference keep every edition date (unchanged).
    pref_dates: dict[str, set] = {}
    for w in db.all("SELECT * FROM works WHERE preferred_ebook_edition_id IS NOT NULL OR preferred_audiobook_edition_id IS NOT NULL"):
        wid = w["id"]
        dates = {e["release_date"] for k in ("ebook", "audiobook")
                 if (e := editions_service.preferred_edition(w, k, fallback=False)) and e.get("release_date")}
        if dates:
            pref_dates[wid] = dates
    items = []
    for r in rows:
        if r["work_id"] in pref_dates and r.get("release_date") not in pref_dates[r["work_id"]]:
            continue
        date_value = r.get("release_date") or r.get("first_publish_date")
        if start and str(date_value) < start:
            continue
        if end and str(date_value) > end:
            continue
        items.append({**r, "date": date_value, "approximate": len(str(date_value or "")) < 10})
    return {"items": items}


@app.get("/api/v1/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), user=Depends(auth.require_full_access)):
    return search_service.search_local(q, limit=limit, offset=offset)


@app.get("/api/v1/search/federated")
def federated_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
    providers: str | None = Query(None, description="Comma-separated provider names"),
    user=Depends(auth.require_full_access),
):
    provider_list = [p.strip() for p in providers.split(",") if p.strip()] if providers else None
    return {"items": candidate_service.build_candidates(q, limit=limit, providers=provider_list)}


# ---------- Metadata discovery ----------
@app.get("/api/v1/discover")
def discover(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=50), user=Depends(auth.require_full_access)):
    try:
        return {"items": metadata.search(q, limit)}
    except Exception as exc:
        raise HTTPException(502, f"Metadata search failed: {exc}")


@app.post("/api/v1/library")
def add_work(req: AddWorkRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        mid = metadata.get_work(req.metadata_id)
        wid = metadata.upsert_work(mid, monitored=req.monitored)
        db.add_history("library.added", f"Added {mid.get('title','work')} to library", work_id=wid)
        return work_detail(wid, user)
    except Exception as exc:
        raise HTTPException(502, f"Could not add metadata work: {exc}")


@app.get("/api/v1/search-candidates")
def search_candidates_list(
    status: str | None = None,
    query: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_full_access),
):
    if status and status not in ("pending", "promoted", "discarded", "dismissed"):
        raise HTTPException(400, f"Invalid status: {status}")
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if query:
        where.append("query=?")
        params.append(query)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = db.one(f"SELECT COUNT(*) n FROM search_candidates{clause}", tuple(params))["n"]  # nosec B608
    sql = f"SELECT * FROM search_candidates{clause} ORDER BY match_score DESC,title LIMIT ? OFFSET ?"  # nosec B608
    items = [candidate_service.decode_candidate(x) for x in db.all(sql, tuple(params + [limit, offset]))]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/search-candidates/{candidate_id}")
def search_candidate_detail(candidate_id: str, user=Depends(auth.require_full_access)):
    x = db.one("SELECT * FROM search_candidates WHERE id=?", (candidate_id,))
    if not x:
        raise HTTPException(404, "Search candidate not found")
    return candidate_service.decode_candidate(x)


@app.post("/api/v1/search-candidates/{candidate_id}/promote")
def promote_search_candidate(candidate_id: str, req: PromoteCandidateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        wid = candidate_service.promote_candidate(candidate_id, monitored=req.monitored, work_id=req.work_id)
        return work_detail(wid, user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/search-candidates/{candidate_id}/dismiss")
def dismiss_search_candidate(candidate_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        candidate_service.dismiss_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ---------- Roots/scanning ----------
@app.get("/api/v1/roots")
def roots(user=Depends(auth.require_full_access)):
    return {"items": db.all("SELECT * FROM root_folders ORDER BY name")}


@app.post("/api/v1/roots")
def add_root(req: RootRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if not Path(req.path).is_absolute():
        raise HTTPException(400, "Root path must be absolute")
    # Same validation and insert as the setup wizard / CLI (setup.create_root).
    try:
        return setup_svc.create_root(req.name, req.path, req.media_kind, req.management_mode,
                                     monitor_default=req.monitor_default, enabled=req.enabled,
                                     library_type=req.library_type)
    except FileExistsError as exc:
        raise HTTPException(409, f"A root already uses the path {exc}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/v1/roots/{root_id}/scan")
def scan(root_id: str, req: ScanRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return scanner.scan_root(root_id, auto_adopt_exact=req.auto_adopt_exact)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/scans/{run_id}")
def scan_result(run_id: str, status: str | None = None, user=Depends(auth.require_full_access)):
    if status and status not in SCAN_CANDIDATE_STATUSES:
        raise HTTPException(400, f"Invalid status: {status}")
    run = db.one("SELECT * FROM scan_runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "Scan not found")
    params: list[Any] = [run_id]
    sql = "SELECT * FROM scan_candidates WHERE scan_run_id=?"
    if status:
        sql += " AND status=?"
        params.append(status)
    else:
        # An explicitly ignored candidate (Part XX) stays out of the default
        # view; ask for status=ignored to see it.
        sql += " AND status != 'ignored'"
    sql += " ORDER BY confidence DESC,path"
    items = db.all(sql, tuple(params))
    for x in items:
        x["detected"] = db.json_load(x.pop("detected_json"), {})
        x["reasons"] = db.json_load(x.pop("reasons_json"), [])
    run["summary"] = db.json_load(run.pop("summary_json"), {})
    return {"run": run, "items": items}


SCAN_CANDIDATE_STATUSES = ("matched", "probable", "ambiguous", "unmatched", "ignored")


@app.get("/api/v1/scan-candidates")
def scan_candidates(
    status: str | None = None,
    scan_run_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_full_access),
):
    if status and status not in SCAN_CANDIDATE_STATUSES:
        raise HTTPException(400, f"Invalid status: {status}")
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    else:
        # An explicitly ignored candidate (Part XX) stays out of the default
        # view; ask for status=ignored to see it.
        where.append("status != 'ignored'")
    if scan_run_id:
        where.append("scan_run_id=?")
        params.append(scan_run_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = db.one(f"SELECT COUNT(*) n FROM scan_candidates{clause}", tuple(params))["n"]  # nosec B608
    sql = f"SELECT * FROM scan_candidates{clause} ORDER BY confidence DESC,path LIMIT ? OFFSET ?"  # nosec B608
    items = db.all(sql, tuple(params + [limit, offset]))
    for x in items:
        x["detected"] = db.json_load(x.pop("detected_json"), {})
        x["reasons"] = db.json_load(x.pop("reasons_json"), [])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/v1/scan-candidates/{candidate_id}")
def scan_candidate_detail(candidate_id: str, user=Depends(auth.require_full_access)):
    x = db.one("SELECT * FROM scan_candidates WHERE id=?", (candidate_id,))
    if not x:
        raise HTTPException(404, "Scan candidate not found")
    x["detected"] = db.json_load(x.pop("detected_json"), {})
    x["reasons"] = db.json_load(x.pop("reasons_json"), [])
    return x


@app.post("/api/v1/scan-candidates/{candidate_id}/adopt")
def adopt(candidate_id: str, req: AdoptRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return {"asset_id": scanner.adopt_candidate(candidate_id, work_id=req.work_id, edition_id=req.edition_id)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/v1/scan-candidates/{candidate_id}")
def edit_scan_candidate(candidate_id: str, req: ScanCandidateEditRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    try:
        return scanner.edit_candidate(candidate_id, fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/scan-candidates/{candidate_id}/ignore")
def ignore_scan_candidate(candidate_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        scanner.ignore_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ---------- Migration ----------
@app.post("/api/v1/migrations/readarr/analyze")
def migration_analyze(req: MigrationAnalyzeRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return migration.analyze_readarr(req.path, req.media_kind, req.source_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/migrations/{run_id}/reconcile")
def migration_reconcile(run_id: str, req: MigrationReconcileRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return migration.reconcile_run(run_id, use_network=req.use_network)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/migrations/{run_id}/plan")
def migration_plan(run_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Dry-run plan: compute total planned size and verify free space before returning."""
    try:
        return migration.plan_migration(run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/migrations/{run_id}/execute")
def migration_execute(run_id: str, req: MigrationExecuteRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return migration.execute_run(run_id, import_unresolved_stubs=req.import_unresolved_stubs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/migrations")
def migrations(user=Depends(auth.require_admin)):
    rows = db.all("SELECT * FROM migration_runs ORDER BY created_at DESC")
    for r in rows:
        r["analysis"] = db.json_load(r.pop("analysis_json"), {})
        r["result"] = db.json_load(r.pop("result_json"), {})
    return {"items": rows}


# ---------- Indexers/download clients ----------
@app.get("/api/v1/indexers", dependencies=[Depends(acquisition_svc.require_acquisition)])
def indexers(user=Depends(auth.require_full_access)):
    rows = db.all("SELECT id,name,kind,base_url,categories_json,priority,enabled,last_test_at,last_ok_at,last_error,created_at,updated_at FROM indexers ORDER BY priority DESC,name")
    for r in rows:
        r["categories"] = db.json_load(r.pop("categories_json"), [])
    return {"items": rows}


@app.post("/api/v1/indexers", dependencies=[Depends(acquisition_svc.require_acquisition)])
def add_indexer(req: IndexerRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    iid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO indexers(id,name,kind,base_url,api_key_enc,categories_json,priority,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (iid, req.name, req.kind, req.base_url.rstrip("/"), encrypt_secret(req.api_key), db.json_dump(req.categories), req.priority, int(req.enabled), t, t))
    return {"id": iid}


@app.post("/api/v1/indexers/{indexer_id}/test", dependencies=[Depends(acquisition_svc.require_acquisition)])
def test_indexer(indexer_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    item = db.one("SELECT * FROM indexers WHERE id=?", (indexer_id,))
    if not item:
        raise HTTPException(404, "Indexer not found")
    result = indexer_api.test(item)
    with db.tx() as conn:
        conn.execute("UPDATE indexers SET last_test_at=?,last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END,last_error=? WHERE id=?", (db.now(), int(result["ok"]), db.now(), None if result["ok"] else scrub_secrets(result["message"]), indexer_id))
    return result


@app.get("/api/v1/download-clients", dependencies=[Depends(acquisition_svc.require_acquisition)])
def download_clients(user=Depends(auth.require_full_access)):
    return {"items": db.all("SELECT id,name,kind,base_url,category,priority,enabled,last_test_at,last_ok_at,last_error,created_at,updated_at FROM download_clients ORDER BY priority DESC,name")}


@app.post("/api/v1/download-clients", dependencies=[Depends(acquisition_svc.require_acquisition)])
def add_download_client(req: DownloadClientRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    cid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("""INSERT INTO download_clients(id,name,kind,base_url,username_enc,password_enc,api_key_enc,category,priority,enabled,config_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (cid, req.name, req.kind, req.base_url.rstrip("/"), encrypt_secret(req.username), encrypt_secret(req.password), encrypt_secret(req.api_key), req.category, req.priority, int(req.enabled), db.json_dump(req.config), t, t))
    return {"id": cid}


@app.post("/api/v1/download-clients/{client_id}/test", dependencies=[Depends(acquisition_svc.require_acquisition)])
def test_client(client_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    item = db.one("SELECT * FROM download_clients WHERE id=?", (client_id,))
    if not item:
        raise HTTPException(404, "Download client not found")
    result = downloader_api.test(item)
    with db.tx() as conn:
        conn.execute("UPDATE download_clients SET last_test_at=?,last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END,last_error=? WHERE id=?", (db.now(), int(result["ok"]), db.now(), None if result["ok"] else scrub_secrets(result["message"]), client_id))
    return result


@app.get("/api/v1/path-mappings", dependencies=[Depends(acquisition_svc.require_acquisition)])
def path_mappings(user=Depends(auth.require_full_access)):
    return {"items": db.all("SELECT * FROM path_mappings ORDER BY remote_path")}


@app.post("/api/v1/path-mappings", dependencies=[Depends(acquisition_svc.require_acquisition)])
def add_path_mapping(req: PathMappingRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    mid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO path_mappings(id,client_id,remote_path,local_path,created_at) VALUES (?,?,?,?,?)", (mid, req.client_id, req.remote_path.rstrip("/\\"), req.local_path.rstrip("/\\"), db.now()))
    return {"id": mid}


# ---------- Release search/downloads ----------
@app.post("/api/v1/library/{work_id}/search/{kind}", dependencies=[Depends(acquisition_svc.require_acquisition)])
def release_search(work_id: str, kind: Literal["ebook", "audiobook"], request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return releases.search_releases(work_id, kind, automatic=False)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/releases/{release_id}/grab", dependencies=[Depends(acquisition_svc.require_acquisition)])
def grab_release(release_id: str, req: GrabRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return downloads.force_grab_release(release_id, req.client_id, user.get("username")) if req.force else downloads.grab_release(release_id, req.client_id, user.get("username"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/queue", dependencies=[Depends(acquisition_svc.require_acquisition)])
def queue(scope: Literal["open", "history", "all"] = "open", history_limit: int = 25, user=Depends(auth.require_full_access)):
    """Management queue. Default scope keeps the old contents (still-open downloads); the
    rows now carry the joined fields the Queue page shows. Admin only: paths and indexer
    names are in the payload and must never reach the Player API."""
    return downloads.queue_items(scope, history_limit)


@app.post("/api/v1/queue/poll", dependencies=[Depends(acquisition_svc.require_acquisition)])
def poll_queue(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    return downloads.poll_all()


@app.post("/api/v1/downloads/{download_id}/import", dependencies=[Depends(acquisition_svc.require_acquisition)])
def import_download(download_id: str, req: ImportRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return downloads.import_completed(download_id, req.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/v1/downloads/{download_id}", dependencies=[Depends(acquisition_svc.require_acquisition)])
def cancel_download(download_id: str, request: Request, remove_from_client: bool = False, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return downloads.cancel_download(download_id, remove_from_client)
    except downloads.DownloadNotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------- Wanted/review/history ----------
@app.get("/api/v1/wanted", dependencies=[Depends(acquisition_svc.require_acquisition)])
def wanted(
    kind: str | None = None,
    not_found: bool = False,
    limit: int = Query(300, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_full_access),
):
    """The wanted list with the automatic search's per-asset state: when it was last
    searched automatically, how many misses in a row, and (while cooling down) when it
    becomes eligible again. `not_found` keeps only assets the automatic search tried and
    found nothing acceptable for."""
    where, params = "a.monitored=1 AND a.has_acquisition_intent=1 AND a.status='missing' AND w.monitored=1", []
    if kind:
        where += " AND a.kind=?"
        params.append(kind)
    if not_found:
        where += " AND s.misses>0"
    join = "FROM media_assets a JOIN works w ON w.id=a.work_id LEFT JOIN wanted_search_state s ON s.asset_id=a.id"
    total = db.one(f"SELECT COUNT(*) n {join} WHERE {where}", tuple(params))["n"]
    rows = db.all(
        f"""SELECT a.*,w.title,w.cover_url,s.last_auto_search_at,s.misses AS auto_misses,s.next_eligible_at,s.last_outcome AS auto_outcome
            {join} WHERE {where} ORDER BY w.sort_title,w.title LIMIT ? OFFSET ?""",
        tuple(params + [limit, offset]),
    )
    now = db.now()
    # Grouping by author on the frontend needs a name; a table this large without
    # any grouping was 8,000 individual rows to scroll through with a Search button
    # on every one.
    for r in rows:
        r["cooling_down"] = bool(r["next_eligible_at"] and r["next_eligible_at"] > now)
        authors = db.all(
            "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord",
            (r["work_id"],),
        )
        r["author"] = ", ".join(a["name"] for a in authors) or "Unknown author"
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@app.post("/api/v1/wanted/search-now", status_code=202, dependencies=[Depends(acquisition_svc.require_acquisition)])
def wanted_search_now(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Start the wanted-search job now, in the background (a drain can take 20 minutes, far
    past any HTTP timeout). Same one-run-per-kind guard as the schedule: 409 while it runs.
    Works while the scheduled job is disabled."""
    job = db.one("SELECT id FROM jobs WHERE kind='wanted-search'")
    if not job:
        raise HTTPException(404, "The wanted-search job does not exist")
    try:
        item = scheduler.submit_job(job["id"], trigger="manual")
    except scheduler.JobBusy as exc:
        raise HTTPException(409, str(exc))
    db.add_history("wanted_search.manual_run", f"Wanted search started by {user.get('username') or user.get('id')}")
    return {"run_id": item["run_id"], "status": "running"}


@app.get("/api/v1/review")
def review(
    limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    category: Literal["identifier-exact", "has-owned-files", "hopeless", "other"] | None = None,
    kind: str | None = Query(None, max_length=60),
    status: Literal["open", "resolved", "ignored"] = "open",
    q: str | None = Query(None, max_length=200),
    user=Depends(auth.require_full_access),
):
    """Review tasks of every kind with the Inbox's filters (kind, state, text search,
    metadata category), plus per-kind open counts and the metadata categories so the
    Inbox can show numbers next to every filter. Defaults list every open task."""
    try:
        page = review_bulk.list_tasks(limit=limit, offset=offset, kind=kind, status=status, q=q, category=category)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**page, "kinds": review_bulk.kind_counts(status), "categories": review_triage.summary()["categories"],
            "bulk_actions": {k: list(v) for k, v in review_bulk.KIND_ACTIONS.items()}, "default_bulk_actions": list(review_bulk.DEFAULT_ACTIONS)}


class ReviewBulkFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = Field(default=None, max_length=200)
    category: Literal["identifier-exact", "has-owned-files", "hopeless", "other"] | None = None


class ReviewBulkRequest(BaseModel):
    """Either `ids` (explicit) or `filter` + `exclude` (select-all across pages). `kind`
    is required with a filter and must match every explicit id."""
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "ignore", "keep", "remove"]
    kind: str | None = Field(default=None, max_length=60)
    ids: list[str] | None = Field(default=None, max_length=review_bulk.MAX_SELECTION)
    filter: ReviewBulkFilter | None = None
    exclude: list[str] = Field(default_factory=list, max_length=review_bulk.MAX_SELECTION)


@app.post("/api/v1/review/bulk")
def review_bulk_action(req: ReviewBulkRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Admin-only bulk action through the same per-item code the Inbox buttons use.
    Returns per-item results; audited with count and filter (history 'review.bulk')."""
    try:
        return review_bulk.run_bulk(action=req.action, actor=user, ids=req.ids, kind=req.kind,
                                    filters=req.filter.model_dump() if req.filter else None, exclude=req.exclude)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/review/{task_id}/kids-tag/approve")
def approve_kids_suggestion(task_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Put the suggested book on the kids shelf. Admin only: this widens what a kids
    account can see, and it is the ONLY route by which a classifier verdict becomes a tag."""
    try:
        return tagging.approve_kids_proposal(task_id, decided_by=user.get("username") or user.get("id"), decided_by_id=user.get("id"))
    except ValueError as exc:
        raise HTTPException(409 if "by hand" in str(exc) else 404, str(exc))


@app.post("/api/v1/review/{task_id}/kids-tag/reject")
def reject_kids_suggestion(task_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Decline a suggestion. Nothing about the book changes and it is not asked again."""
    try:
        return tagging.reject_kids_proposal(task_id, decided_by=user.get("username") or user.get("id"), decided_by_id=user.get("id"))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/review/{task_id}/kids-recheck/{action}")
def resolve_kids_recheck(task_id: str, action: Literal["keep", "remove"], request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Keep (manual_tag) or remove (manual_untag) one auto-approved book flagged by the re-check.
    Both are final human decisions; admin only."""
    try:
        return kidsrecheck.resolve_task(task_id, action, by_user_id=user.get("id"), by_name=user.get("username"))
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 409, str(exc))


class ReviewTriageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["auto_resolve", "ignore_hopeless"]
    limit: int = Field(100, ge=1, le=1000)


@app.get("/api/v1/review/triage")
def review_triage_summary(user=Depends(auth.require_full_access)):
    return review_triage.summary()


@app.post("/api/v1/review/triage")
def review_triage_run(req: ReviewTriageRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Bulk-work the Inbox: link exact identifier matches, or dismiss tasks nobody can act on."""
    if req.action == "auto_resolve":
        return review_triage.auto_resolve_identifier_matches(req.limit)
    return review_triage.bulk_ignore_hopeless(req.limit)


@app.post("/api/v1/review/{task_id}/ignore")
def ignore_review_task(task_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    """Permanently dismiss an item that will never get a confident match.

    Distinct from resolving it: nothing about the work changes, it simply stops
    being asked about. Without this a stub with no usable identifiers sat in Review
    forever with no way to say "stop offering me this one".
    """
    if not db.one("SELECT id FROM review_tasks WHERE id=? AND status='open'", (task_id,)):
        raise HTTPException(404, "Review item not found or already resolved")
    with db.tx() as conn:
        conn.execute("UPDATE review_tasks SET status='ignored',resolved_at=? WHERE id=?", (db.now(), task_id))
    return {"ok": True}


@app.get("/api/v1/history")
def history(limit: int = Query(200, ge=1, le=1000), user=Depends(auth.require_full_access)):
    rows = db.all("SELECT h.*,w.title FROM history h LEFT JOIN works w ON w.id=h.work_id ORDER BY h.created_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["data"] = db.json_load(r.pop("data_json"), {})
    return {"items": rows}


# ---------- Naming ----------
@app.get("/api/v1/naming")
def naming(user=Depends(auth.require_full_access)):
    return {"items": db.all("SELECT * FROM naming_templates ORDER BY media_kind")}


@app.put("/api/v1/naming/{kind}")
def update_naming(kind: Literal["ebook", "audiobook"], req: NamingRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    with db.tx() as conn:
        conn.execute("""UPDATE naming_templates SET folder_template=?,file_template=?,multipart_template=?,replace_illegal=?,colon_replacement=?,updated_at=? WHERE media_kind=?""", (req.folder_template, req.file_template, req.multipart_template, int(req.replace_illegal), req.colon_replacement, db.now(), kind))
    return db.one("SELECT * FROM naming_templates WHERE media_kind=?", (kind,))


@app.get("/api/v1/naming/{kind}/preview")
def naming_preview(
    kind: Literal["ebook", "audiobook"],
    work_id: str | None = None,
    ext: str = "m4b",
    parts: int = 1,
    folder_template: str | None = None,
    file_template: str | None = None,
    multipart_template: str | None = None,
    replace_illegal: bool | None = None,
    colon_replacement: str | None = None,
    user=Depends(auth.require_full_access),
):
    """Preview naming. Omit work_id for a sample; pass templates to preview unsaved edits."""
    overrides = {
        "folder_template": folder_template,
        "file_template": file_template,
        "multipart_template": multipart_template,
        "replace_illegal": replace_illegal,
        "colon_replacement": colon_replacement,
    }
    try:
        return importer.preview_name(work_id, kind, ext, parts, overrides=overrides)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------- File-operation plans (Part XXIV: plan + preview only; no execute route) ----------
class FilePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


def _plan_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, fileops.PlanNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, fileops.PlanConflict):
        return HTTPException(409, str(exc))
    return HTTPException(400, str(exc))


@app.post("/api/v1/file-plans")
def create_file_plan(req: FilePlanRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Always HTTP 200 for a well-formed request: refusals are data on the returned plan."""
    try:
        return fileops.create_plan(req.kind, req.params, user)
    except (fileops.PlanError, fileops.PlanNotFound) as exc:
        raise _plan_http_error(exc)


@app.get("/api/v1/file-plans")
def list_file_plans(status: str | None = None, limit: int = 100, user=Depends(auth.require_admin)):
    return {"items": fileops.list_plans(status, limit)}


@app.get("/api/v1/file-plans/{plan_id}")
def get_file_plan(plan_id: str, user=Depends(auth.require_admin)):
    try:
        return fileops.get_plan(plan_id)
    except fileops.PlanNotFound as exc:
        raise _plan_http_error(exc)


@app.post("/api/v1/file-plans/{plan_id}/cancel")
def cancel_file_plan(plan_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return fileops.cancel_plan(plan_id)
    except (fileops.PlanNotFound, fileops.PlanConflict) as exc:
        raise _plan_http_error(exc)


@app.get("/api/v1/settings/import")
def get_import_settings(user=Depends(auth.require_full_access)):
    from .services import scanner as _scanner
    return {
        "transfer_mode": importer.configured_transfer_mode(),
        "default": importer.DEFAULT_TRANSFER_MODE,
        "modes": ["copy", "hardlink", "move"],
        "automatic": importer.automatic_import_enabled(),
        "matching": {
            "auto_accept_threshold": _scanner._auto_accept_threshold(),
            "probable_threshold": _scanner._probable_threshold(),
        },
    }


@app.put("/api/v1/settings/import")
def put_import_settings(req: ImportSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    db.set_setting("import.transfer_mode", req.transfer_mode)
    if req.automatic is not None:
        db.set_setting("import.automatic", bool(req.automatic))
    db.add_history(
        "settings.import",
        f"Import transfer mode set to {req.transfer_mode}; automatic import {'on' if importer.automatic_import_enabled() else 'off'}",
    )
    return {"transfer_mode": importer.configured_transfer_mode(), "automatic": importer.automatic_import_enabled()}


# ---------- Matching thresholds ----------
@app.get("/api/v1/settings/matching")
def get_matching_settings(user=Depends(auth.require_full_access)):
    from .services import scanner as _scanner
    return {
        "auto_accept_threshold": _scanner._auto_accept_threshold(),
        "probable_threshold": _scanner._probable_threshold(),
    }


@app.put("/api/v1/settings/matching")
def put_matching_settings(req: MatchingSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    auto_accept = float(req.auto_accept_threshold)
    probable = float(req.probable_threshold)
    # Validate: 0.90 <= auto_accept <= 1.0
    if not (0.90 <= auto_accept <= 1.0):
        raise HTTPException(400, f"auto_accept_threshold must be between 0.90 and 1.0, got {auto_accept}")
    # Validate: 0.50 <= probable < auto_accept
    if not (0.50 <= probable < auto_accept):
        raise HTTPException(400, f"probable_threshold must be between 0.50 and {auto_accept} (exclusive), got {probable}")
    old_auto = db.get_setting("matching.auto_accept_threshold")
    old_prob = db.get_setting("matching.probable_threshold")
    db.set_setting("matching.auto_accept_threshold", auto_accept)
    db.set_setting("matching.probable_threshold", probable)
    db.add_history(
        "settings.matching",
        f"Matching thresholds: auto_accept {old_auto} -> {auto_accept}, probable {old_prob} -> {probable}",
        data={"old": {"auto_accept_threshold": old_auto, "probable_threshold": old_prob},
              "new": {"auto_accept_threshold": auto_accept, "probable_threshold": probable}},
    )
    return {"auto_accept_threshold": auto_accept, "probable_threshold": probable}


# ---------- Jobs ----------
@app.get("/api/v1/jobs")
def jobs(user=Depends(auth.require_admin)):
    return {"items": db.all("SELECT * FROM jobs ORDER BY name"), "runs": db.all("SELECT * FROM job_runs ORDER BY started_at DESC LIMIT 100")}


@app.get("/api/v1/profiles")
def list_profiles(user=Depends(auth.require_full_access)):
    items = []
    for r in db.all("SELECT * FROM profiles ORDER BY media_kind,name"):
        items.append({
            "id": r["id"], "name": r["name"], "media_kind": r["media_kind"],
            "is_default": bool(r["is_default"]), "config": db.json_load(r["config_json"], {}),
        })
    return {"items": items, "known_languages": sorted(releases.LANGUAGE_TOKENS.keys())}


@app.put("/api/v1/profiles/{profile_id}")
def update_profile(profile_id: str, req: ProfileUpdateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    row = db.one("SELECT * FROM profiles WHERE id=?", (profile_id,))
    if not row:
        raise HTTPException(404, "Profile not found")
    cfg = db.json_load(row["config_json"], {})
    if req.languages is not None:
        normalised = []
        for raw in req.languages:
            code = releases.normalize_language(raw)
            if not code:
                raise HTTPException(400, f"Unrecognised language: {raw}")
            if code not in normalised:
                normalised.append(code)
        cfg["languages"] = normalised
    if req.preferred_codecs is not None:
        codecs = [c.strip().lower() for c in req.preferred_codecs]
        if not all(codecs):
            raise HTTPException(400, "preferred_codecs entries must be non-empty")
        req.preferred_codecs = codecs
    for field in ("language_policy", "allowed_formats", "preferred_formats", "abridged", "min_size_mb", "max_size_mb",
                  *quality.PROFILE_QUALITY_DEFAULTS):
        value = getattr(req, field)
        if value is not None:
            cfg[field] = value
    name = req.name or row["name"]
    with db.tx() as conn:
        conn.execute("UPDATE profiles SET name=?,config_json=?,updated_at=? WHERE id=?", (name, db.json_dump(cfg), db.now(), profile_id))
    db.add_history("profile.updated", f"Updated quality profile: {name}", data={"profile_id": profile_id, "config": cfg})
    updated = db.one("SELECT * FROM profiles WHERE id=?", (profile_id,))
    return {"id": updated["id"], "name": updated["name"], "media_kind": updated["media_kind"], "config": db.json_load(updated["config_json"], {})}


@app.put("/api/v1/jobs/{job_id}")
def update_job(job_id: str, req: JobUpdateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")
    if req.schedule_seconds is not None and job["kind"] == "managed-library-build":
        # The scheduler has no handler for this kind; it is manual-only (dry run).
        raise HTTPException(400, "managed-library-build is manual-only and cannot be scheduled")
    fields, params = [], []
    if req.enabled is not None:
        fields.append("enabled=?")
        params.append(int(req.enabled))
    if req.schedule_seconds is not None:
        fields.append("schedule_seconds=?")
        params.append(int(req.schedule_seconds))
        fields.append("next_run_at=?")
        params.append(db.now() + int(req.schedule_seconds))
    if not fields:
        raise HTTPException(400, "Nothing to update")
    with db.tx() as conn:
        conn.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", (*params, job_id))  # nosec B608
    updated = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if req.enabled is not None and bool(req.enabled) != bool(job["enabled"]):
        db.add_history(
            "job.enabled" if req.enabled else "job.disabled",
            f"{'Enabled' if req.enabled else 'Disabled'} scheduled job: {job['name']}",
        )
    return updated


# ---------- Managed library build (services/managedbuild.py; #219, #222-#224) ----------
# Admin only, CSRF on every write, audited in the history. Nothing starts on its own: a dry run makes a plan, the admin
# confirms it by typing its number of books, and Start runs that plan once. One build at a time.
class BuildIncludeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kinds: list[Literal["ebook", "audiobook"]] | None = None
    root_ids: list[StrictStr] | None = None
    work_ids: list[StrictStr] | None = None
    author_ids: list[StrictStr] | None = None
    series_ids: list[StrictStr] | None = None
    tag_ids: list[StrictStr] | None = None
    limit_books: StrictInt | None = None


class BuildOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["preferred", "as_is"] | None = None
    non_convertible: Literal["copy", "skip"] | None = None
    throttle_mb_s: StrictInt | float | None = None
    verify: Literal["full", "size", "none"] | None = None
    concurrency: StrictInt | None = None
    stop_on_error: StrictBool | None = None
    include: BuildIncludeRequest | None = None


class BuildConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    book_count: StrictInt | None = None


class BuildUndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_count: StrictInt | None = None


_BUILD_STATUS = {"not_found": 404, "bad_options": 400, "bad_request": 400}
_plan_lock = threading.Lock()


def _build_refusal(exc: managedbuild.BuildRefused) -> HTTPException:
    return HTTPException(_BUILD_STATUS.get(exc.code, 409), str(exc))


def _build_options(req: BuildOptionsRequest | None) -> dict[str, Any]:
    if req is None:
        return {}
    data = req.model_dump(exclude_none=True)
    if "include" in data:
        data["include"] = {k: v for k, v in data["include"].items() if v is not None}
    return data


def _make_plan(options: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not _plan_lock.acquire(blocking=False):
        raise HTTPException(409, "A dry run is already being made; wait for it to finish")
    try:
        return managedbuild.create_plan(options, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)
    finally:
        _plan_lock.release()


@app.post("/api/v1/managed-build/plans")
def managed_build_create_plan(req: BuildOptionsRequest | None = None, request: Request = None, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Dry run: decide every book (copy, convert, copy as-is or skip) and store the plan. Writes nothing to any library."""
    return _make_plan(_build_options(req), user)


@app.get("/api/v1/managed-build/plans/latest")
def managed_build_latest_plan(user=Depends(auth.require_admin)):
    try:
        return managedbuild.latest_plan()
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.get("/api/v1/managed-build/plans/{plan_id}")
def managed_build_plan(plan_id: str, user=Depends(auth.require_admin)):
    try:
        return managedbuild.plan_payload(plan_id)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.get("/api/v1/managed-build/plans/{plan_id}/items")
def managed_build_plan_items(plan_id: str, action: str | None = None, state: str | None = None, kind: str | None = None, root_id: str | None = None,
                             q: str | None = None, collision: bool = False, work_id: str | None = None,
                             limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), user=Depends(auth.require_admin)):
    """One page of the plan (filters: action, state, kind, source root, text, collisions only, book)."""
    if not db.one("SELECT 1 x FROM build_plans WHERE id=?", (plan_id,)):
        raise HTTPException(404, "No such plan")
    return managedbuild.list_items(plan_id, limit=limit, offset=offset, action=action, state=state, kind=kind, root_id=root_id, q=q,
                                   collision=collision, work_id=work_id)


@app.post("/api/v1/managed-build/plans/{plan_id}/confirm")
def managed_build_confirm(plan_id: str, req: BuildConfirmRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return managedbuild.confirm_plan(plan_id, req.book_count, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.post("/api/v1/managed-build/plans/{plan_id}/start")
def managed_build_start(plan_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Run a confirmed, unexpired plan once (preflight: space, publish capability of every destination root)."""
    _write_budget(user)
    try:
        return managedbuild.start(plan_id, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


class BuildSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_enabled: StrictBool


@app.get("/api/v1/managed-build/settings")
def managed_build_settings(user=Depends(auth.require_admin)):
    """``run_enabled`` (default off): while off, Start and Resume answer 409; dry runs, plans and Undo keep working."""
    return {"run_enabled": managedbuild.run_enabled()}


@app.put("/api/v1/managed-build/settings")
def managed_build_settings_update(req: BuildSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return managedbuild.set_run_enabled(req.run_enabled, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.get("/api/v1/managed-build/status")
def managed_build_status(user=Depends(auth.require_admin)):
    """Active/last run, latest plan and library coverage (Overview card, roots banner, build panel)."""
    return managedbuild.status()


@app.get("/api/v1/managed-build/runs")
def managed_build_runs(limit: int = Query(20, ge=1, le=100), user=Depends(auth.require_admin)):
    return {"items": managedbuild.list_runs(limit)}


@app.get("/api/v1/managed-build/runs/{run_id}")
def managed_build_run(run_id: str, user=Depends(auth.require_admin)):
    try:
        return managedbuild.run_status(run_id)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.get("/api/v1/managed-build/runs/{run_id}/journal")
def managed_build_journal(run_id: str, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), user=Depends(auth.require_admin)):
    try:
        return managedbuild.journal(run_id, limit=limit, offset=offset)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.post("/api/v1/managed-build/runs/{run_id}/undo")
def managed_build_undo_run(run_id: str, req: BuildUndoRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Undo build: the run's managed copies go to the managed trash, the records point at the originals again."""
    _write_budget(user)
    try:
        managedbuild._run(run_id)
        return managedbuild.undo(run_id=run_id, confirm_count=req.confirm_count, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.post("/api/v1/managed-build/runs/{run_id}/{action}")
def managed_build_control(run_id: str, action: Literal["pause", "resume", "cancel"], request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    _write_budget(user)
    fn = {"pause": managedbuild.pause, "resume": managedbuild.resume, "cancel": managedbuild.cancel}[action]
    try:
        return fn(run_id, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.post("/api/v1/managed-build/runs/{run_id}/items/{item_id}/{action}")
def managed_build_item(run_id: str, item_id: int, action: Literal["retry", "skip"], request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    _write_budget(user)
    try:
        return (managedbuild.retry_item if action == "retry" else managedbuild.skip_item)(run_id, item_id, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.get("/api/v1/works/{work_id}/managed-copy")
def work_managed_copy(work_id: str, user=Depends(auth.require_admin)):
    """Per-asset managed-copy state of one book and its preserved source copies (admin only)."""
    if not db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    return managedbuild.work_state(work_id)


@app.post("/api/v1/works/{work_id}/managed-copy/undo")
def work_managed_copy_undo(work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    _write_budget(user)
    if not db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
        raise HTTPException(404, "Work not found")
    try:
        return managedbuild.undo(work_id=work_id, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


# Compatibility (v1 paths, Settings > Jobs of older clients): the same v2 plan, confirm and start, plus the v1 `entries`
# and `totals` fields. The unsafe developer-mode copy loop that used to live behind /run is gone.
def _legacy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    s = plan.get("summary") or {}
    skips = sum(v["count"] for v in (s.get("skips") or {}).values())
    return {**plan, "entries": managedbuild.legacy_entries(plan["plan_id"]),
            "totals": {"file_count": s.get("files", 0), "book_count": s.get("books", 0), "skip_count": skips,
                       "collision_count": sum((s.get("collisions") or {}).values()), "total_bytes": s.get("bytes", 0),
                       "total_gb": round((s.get("bytes", 0) or 0) / (1024 ** 3), 2)}}


@app.post("/api/v1/jobs/managed-library-build/dry-run")
def managed_library_build_dry_run(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """v1 path: a dry run with the default options (see POST /api/v1/managed-build/plans)."""
    return _legacy_plan(_make_plan({}, user))


@app.get("/api/v1/jobs/managed-library-build/plan")
def managed_library_build_plan(user=Depends(auth.require_admin)):
    try:
        return _legacy_plan(managedbuild.latest_plan())
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


class BuildPlanRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    book_count: StrictInt | None = None


@app.post("/api/v1/jobs/managed-library-build/confirm")
def managed_library_build_confirm(req: BuildPlanRef, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return _legacy_plan(managedbuild.confirm_plan(req.plan_id, req.book_count, user=user))
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


@app.post("/api/v1/jobs/managed-library-build/run")
def managed_library_build_run(req: BuildPlanRef, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """v1 path: start the confirmed plan through the verified copy engine (runs in the background; poll the status)."""
    _write_budget(user)
    try:
        return managedbuild.start(req.plan_id, user=user)
    except managedbuild.BuildRefused as exc:
        raise _build_refusal(exc)


# Declared after the managed-library-build routes so '/jobs/managed-library-build/run' is not read as a job id.
@app.post("/api/v1/jobs/{job_id}/run")
def run_job(job_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    job = db.one("SELECT kind FROM jobs WHERE id=?", (job_id,))
    if job and job["kind"] == "managed-library-build":
        raise HTTPException(400, "managed-library-build has no generic run; use its dry-run endpoint")
    try:
        return scheduler.run_job(job_id)
    except scheduler.JobBusy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/v1/system/backups")
def backups(user=Depends(auth.require_admin)):
    return {"items": backup.list_backups(), "retention": settings.backup_retention}


@app.post("/api/v1/system/backups")
def make_backup(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return backup.create_backup()


# ---------- Series ----------
@app.get("/api/v1/series")
def list_series(
    include_uncertain: bool = False,
    q: str | None = None,
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user=Depends(auth.require_user),
):
    return series_service.list_series_page(user, include_uncertain=include_uncertain, q=q, limit=limit, offset=offset)


@app.get("/api/v1/series/{series_id}")
def series_detail(series_id: str, user=Depends(auth.require_user)):
    item = series_service.series_detail(series_id, user)
    if not item or (auth.is_kids_scope(user) and not item["works"]):
        raise HTTPException(404, "Series not found")
    return item


@app.put("/api/v1/series/{series_id}/monitor")
def set_series_monitor(series_id: str, req: SeriesMonitorRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        series_service.set_series_monitor(
            series_id,
            audiobook_policy=req.audiobook_policy, ebook_policy=req.ebook_policy,
            policy=req.policy or "future", ebook=req.ebook, audiobook=req.audiobook,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return series_service.series_detail(series_id, user)


@app.post("/api/v1/series/{series_id}/discover-more")
def series_discover_more(series_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return series_service.discover_more(series_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class SeriesCleanupApplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: bool = False


class SeriesSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ignored_names: list[str] | None = Field(default=None, max_length=500)
    max_position: int | None = Field(default=None, ge=1, le=999)


def _series_settings() -> dict[str, Any]:
    opts = seriesnorm.load_options()
    return {"ignored_names": list(opts.ignored), "max_position": opts.max_position}


@app.get("/api/v1/admin/series/cleanup")
def series_cleanup_plan(user=Depends(auth.require_admin)):
    """Dry run of the series reparse: counts and the first 50 before/after samples per
    category. Writes nothing."""
    return {"plan": series_service.reparse_all(dry_run=True), "last": series_service.last_cleanup(), "settings": _series_settings()}


@app.post("/api/v1/admin/series/cleanup")
def series_cleanup_apply(req: SeriesCleanupApplyRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Apply the reparse in one transaction with an undo journal (admin only, audited)."""
    if not req.confirm:
        raise HTTPException(400, "Confirmation required")
    result = series_service.reparse_all(dry_run=False, actor=user.get("username") or user.get("id"))
    db.add_history("series.cleanup", f"Series cleanup applied (by {user.get('username')})",
                   data={"run_id": result["run_id"], "totals": result["totals"],
                         "counts": {k: v["count"] for k, v in result["categories"].items()}, "by": user["id"]})
    return {"plan": result, "last": series_service.last_cleanup()}


@app.post("/api/v1/admin/series/cleanup/undo")
def series_cleanup_undo(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Reverse the most recent applied series cleanup (admin only, audited)."""
    try:
        result = series_service.undo_last_cleanup(actor=user.get("username") or user.get("id"))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    db.add_history("series.cleanup_undo", f"Series cleanup undone (by {user.get('username')})", data={**result, "by": user["id"]})
    return {"undone": result, "last": series_service.last_cleanup()}


@app.get("/api/v1/admin/series/settings")
def series_get_settings(user=Depends(auth.require_admin)):
    return _series_settings()


@app.put("/api/v1/admin/series/settings")
def series_put_settings(req: SeriesSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Ignored series names (`series.ignored_names`) and the position cap
    (`series.max_position`). Audited with the old and new values."""
    before = _series_settings()
    if req.ignored_names is not None:
        names = []
        for n in req.ignored_names:
            n = " ".join(n.split())[:120]
            if n and n.lower() not in {x.lower() for x in names}:
                names.append(n)
        db.set_setting("series.ignored_names", names)
    if req.max_position is not None:
        db.set_setting("series.max_position", req.max_position)
    after = _series_settings()
    db.add_history("settings.series", f"Series settings changed (by {user.get('username')})",
                   data={"before": before, "after": after, "by": user["id"]})
    return after


@app.post("/api/v1/admin/series/dedupe")
def dedupe_series(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Merge duplicate series rows and drop publisher-imprint/classification
    strings that predate bookdoe's own series normalizer. Idempotent."""
    return series_service.dedupe_series()


# ---------- Series assertion actions ----------
@app.post("/api/v1/series-assertions/{assertion_id}/confirm")
def confirm_assertion(assertion_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return series_service.confirm_assertion(assertion_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/series-assertions/{assertion_id}/reject")
def reject_assertion(assertion_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return series_service.reject_assertion(assertion_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/library/{work_id}/series/{series_id}/suppress")
def suppress_series(work_id: str, series_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        return series_service.suppress_series_for_work(work_id, series_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/v1/library/{work_id}/series-assertions")
def list_work_assertions(work_id: str, user=Depends(auth.require_user)):
    """List all series assertions for a work, with status and evidence."""
    return {"assertions": series_service.list_assertions_for_work(work_id)}


# ---------- Tags ----------
@app.get("/api/v1/tags")
def list_tags(user=Depends(auth.require_user)):
    return {"items": tagging.list_tags(viewer=user)}


@app.post("/api/v1/tags")
def create_tag(req: TagCreateRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_full_access)):
    try:
        tid = tagging.create_tag(req.name, req.color)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": tid}


@app.delete("/api/v1/tags/{tag_id}")
def delete_tag(tag_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        tagging.delete_tag(tag_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


def _bulk_context(req: TagRemoveWorksRequest) -> dict[str, Any] | None:
    """Audit context for a bulk tag call made from a series page: series id and whether the
    batch was the whole series or a selection."""
    if not req.series_id:
        return None
    if not db.one("SELECT 1 FROM series WHERE id=?", (req.series_id,)):
        raise HTTPException(404, "Series not found")
    return {"series_id": req.series_id, "scope": req.scope or "selected", "filter": "series"}


@app.post("/api/v1/tags/{tag_id}/remove-works")
def remove_tag_from_works(tag_id: str, req: TagRemoveWorksRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Bulk 'Remove tag' for a tag view: per-work results, admin only, audited. For the
    kids tag every removal is a recorded manual_untag (see tagging.remove_tag_from_works)."""
    try:
        return tagging.remove_tag_from_works(tag_id, req.work_ids, by_user_id=user.get("id"), by_name=user.get("username"), context=_bulk_context(req))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/tags/{tag_id}/add-works")
def add_tag_to_works(tag_id: str, req: TagRemoveWorksRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Bulk 'Add tag' (and 'Add to kids shelf'): per-work results, admin only, audited. For
    the kids tag every add is a recorded manual_tag (see tagging.add_tag_to_works)."""
    try:
        return tagging.add_tag_to_works(tag_id, req.work_ids, by_user_id=user.get("id"), by_name=user.get("username"), context=_bulk_context(req))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class KidsAutoSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kids_auto_approve: bool | None = None
    kids_auto_threshold: float | None = Field(default=None, ge=0, le=1)
    kids_require_strong_evidence: bool | None = None


class KidsRecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True   # the count first; nothing is written until the caller says dry_run=false


class KidsAutoRevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ids: list[str] | None = None   # None = every auto-approved work
    mode: Literal["lock_off", "reevaluate"] = "lock_off"


@app.get("/api/v1/settings/kids")
def get_kids_settings(user=Depends(auth.require_admin)):
    return {**tagging.set_auto_approve(), "auto_approved_count": tagging.auto_approved_works(limit=1)["total"],
            "audience_coverage": audience_service.coverage(),
            "pending_proposals": db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag' AND status='open'")["n"]}


@app.put("/api/v1/settings/kids")
def put_kids_settings(req: KidsAutoSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Auto-approval switch and threshold. Turning it on also sweeps the proposals already
    waiting in the Inbox (idempotent); turning it off stops further auto-approvals and
    leaves existing tags alone."""
    was = tagging.auto_approve_enabled()
    try:
        out = tagging.set_auto_approve(req.kids_auto_approve, req.kids_auto_threshold, req.kids_require_strong_evidence)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_history("settings.kids", f"Kids auto-approval {'on' if out['kids_auto_approve'] else 'off'}, threshold {out['kids_auto_threshold']}, strong audience evidence {'required' if out['kids_require_strong_evidence'] else 'not required'} (by {user.get('username')})",
                   data={"settings": {k: out[k] for k in ("kids_auto_approve", "kids_auto_threshold", "kids_require_strong_evidence")}, "by": user.get("id")})
    sweep = tagging.auto_approve_pending() if (out["kids_auto_approve"] and (not was or req.kids_auto_threshold is not None or req.kids_require_strong_evidence is not None)) else None
    return {**get_kids_settings(user), "sweep": sweep}


@app.get("/api/v1/tags/kids-auto")
def list_kids_auto(limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0), user=Depends(auth.require_admin)):
    return tagging.auto_approved_works(limit=limit, offset=offset)


@app.get("/api/v1/tags/kids-auto/recheck")
def kids_recheck_preview(user=Depends(auth.require_admin)):
    """Dry-run count of the re-check: how many auto-approved books pass today's gate and how
    many would be queued for review. Reads only; writes nothing."""
    return kidsrecheck.recheck_auto_approved(dry_run=True)


@app.post("/api/v1/tags/kids-auto/recheck")
def kids_recheck_run(req: KidsRecheckRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Re-check every auto-approved book against the deterministic gate (no LLM call). Books
    that fail are queued in the Inbox group 'Auto-tagged without audience evidence'; no tag
    is removed here. Idempotent."""
    return kidsrecheck.recheck_auto_approved(dry_run=req.dry_run, by=user.get("username"))


@app.post("/api/v1/tags/audience-backfill")
def run_audience_backfill(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Start one audience-metadata backfill batch now. Answers at once: the batch runs on the
    scheduler pool under the job's single flight (409 while one is already running), so it
    can neither block a request nor overlap the scheduled run."""
    job = db.one("SELECT id FROM jobs WHERE kind='audience-backfill'")
    if not job:
        db.ensure_default_jobs()
        job = db.one("SELECT id FROM jobs WHERE kind='audience-backfill'")
    try:
        started = scheduler.submit_job(job["id"], trigger="manual")
    except scheduler.JobBusy as exc:
        raise HTTPException(409, str(exc))
    db.add_history("audience.backfill_started", f"Audience metadata backfill started by {user.get('username')}",
                   data={"by": user.get("id"), "run_id": started["run_id"]})
    return {"started": True, "run_id": started["run_id"], "coverage": audience_service.coverage()}


@app.post("/api/v1/tags/kids-auto/revert")
def revert_kids_auto(req: KidsAutoRevertRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return tagging.revert_auto_approved(req.work_ids, mode=req.mode, by_user_id=user.get("id"), by_name=user.get("username"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/tags/classify-library")
def classify_library(req: ClassifyRequest, background: BackgroundTasks, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Scan owned works the classifier has not seen and queue kids-shelf SUGGESTIONS
    (review tasks) for an admin to approve or reject in the Inbox. Nothing is tagged here.

    Answers immediately and runs the batch after the response: a local reasoning model
    takes tens of seconds per book, which no browser request should wait on. The
    scheduled 'tag-classify' job works through the rest in small batches on its own.
    Refuses (409) rather than silently doing nothing when BOOKDOE_LLM_ENABLED is
    unset - see llmclient.py.
    """
    if not tagging.llmclient.enabled():
        raise HTTPException(409, "LLM not enabled (BOOKDOE_LLM_ENABLED / _BASE_URL / _MODEL)")
    if any(r["kind"] == "tag-classify" for r in scheduler.running_jobs()):
        raise HTTPException(409, "The kids classifier is already running; its results appear in the Inbox as they arrive")
    # Under the tag-classify single flight, so it never overlaps the scheduled batch and
    # a work is never put to the model twice at once. A batch that finds the kind busy
    # (the job started between the check above and now) simply does nothing.
    def scan():
        try:
            scheduler.run_exclusive("tag-classify", lambda: tagging.classify_kids_missing(req.limit))
        except scheduler.JobBusy as exc:
            log.info("classify-library: %s", exc)
    background.add_task(scan)
    pending = tagging.kids_scan_pending()
    return {"started": True, "limit": req.limit, "pending": pending}


# ---------- Requests, approvers, grants (Management) ----------
class AdultPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adult_policy: Literal["auto", "approve"]
    kid_external_search: bool | None = None
    search_on_approval: bool | None = None


def _family_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except family.Forbidden as exc:
        raise HTTPException(403, str(exc))
    except family.NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/requests")
def list_requests(status: Literal["pending", "approved", "denied", "cancelled"] | None = None, limit: int = Query(200, ge=1, le=1000),
                  offset: int = Query(0, ge=0), user=Depends(auth.require_admin)):
    """Every account's requests, for the Management Requests page."""
    return family.list_for_approver(user, status=status, limit=limit, offset=offset)


@app.post("/api/v1/requests/{request_id}/approve")
def approve_request(request_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _family_http(family.decide, user, request_id, "approved")


@app.post("/api/v1/requests/{request_id}/deny")
def deny_request(request_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _family_http(family.decide, user, request_id, "denied")


@app.get("/api/v1/settings/requests")
def get_request_settings(user=Depends(auth.require_admin)):
    return {"adult_policy": family.adult_policy(), "kid_external_search": family.kid_external_search(),
            "search_on_approval": family.search_on_approval()}


@app.put("/api/v1/settings/requests")
def put_request_settings(req: AdultPolicyRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    family.set_adult_policy(req.adult_policy)
    if req.kid_external_search is not None:
        db.set_setting(family.KID_EXTERNAL_SEARCH_KEY, bool(req.kid_external_search))
    if req.search_on_approval is not None:
        db.set_setting(family.SEARCH_ON_APPROVAL_KEY, bool(req.search_on_approval))
    db.add_history("settings.requests", f"Adult requests: {req.adult_policy}; kid provider search: {'on' if family.kid_external_search() else 'off'}; search on approval: {'on' if family.search_on_approval() else 'off'}")
    return get_request_settings(user)


class DiscoverSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    llm_timeout_seconds: int | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    daily_cap: int | None = None
    retention_days: int | None = None
    kid_chats_visible_to_approvers: bool | None = None
    kid_unverified: Literal["drop", "ask_parent"] | None = None
    send_library_titles: bool | None = None


def _discover_settings_payload() -> dict[str, Any]:
    return {**discover_chat.cfg(), "model": llmclient.model_name(), "llm_configured": llmclient.enabled(), "available": discover_chat.available()}


@app.get("/api/v1/settings/discover")
def get_discover_settings(user=Depends(auth.require_admin)):
    """Discover chat: switches and limits. The model name is shown read-only (it is set by environment)."""
    return _discover_settings_payload()


@app.put("/api/v1/settings/discover")
def put_discover_settings(req: DiscoverSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        discover_chat.set_cfg(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_history("settings.discover", "Discover chat settings changed: " + ", ".join(sorted(req.model_dump(exclude_none=True))))
    return _discover_settings_payload()


@app.get("/api/v1/users/{user_id}/family")
def user_family(user_id: str, user=Depends(auth.require_admin)):
    """Approvers of a kid (or kids of an approver), book grants and collection grants."""
    target = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise HTTPException(404, "User not found")
    return {
        "account_type": auth.account_type(target),
        "approvers": family.approvers_of(user_id),
        "approves_for": family.kids_for(target) if auth.account_type(target) == "adult" else [],
        "grants": family.grants_for(user, user_id),
        "tag_grants": family.tag_grants_for(user_id),
    }


@app.put("/api/v1/users/{kid_id}/approvers/{approver_id}")
def put_approver(kid_id: str, approver_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _family_http(family.assign_approver, user, approver_id, kid_id)


@app.delete("/api/v1/users/{kid_id}/approvers/{approver_id}")
def delete_approver(kid_id: str, approver_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if not _family_http(family.remove_approver, user, approver_id, kid_id):
        raise HTTPException(404, "No such assignment")
    return {"ok": True}


@app.put("/api/v1/users/{kid_id}/grants/{work_id}")
def put_grant(kid_id: str, work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _family_http(family.grant_work, user, kid_id, work_id)


@app.delete("/api/v1/users/{kid_id}/grants/{work_id}")
def delete_grant(kid_id: str, work_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if not _family_http(family.revoke_work, user, kid_id, work_id):
        raise HTTPException(404, "No such grant")
    return {"ok": True}


@app.put("/api/v1/users/{kid_id}/tag-grants/{tag_id}")
def put_tag_grant(kid_id: str, tag_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    return _family_http(family.grant_tag, user, kid_id, tag_id)


@app.delete("/api/v1/users/{kid_id}/tag-grants/{tag_id}")
def delete_tag_grant(kid_id: str, tag_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if not _family_http(family.revoke_tag, user, kid_id, tag_id):
        raise HTTPException(404, "No such grant")
    return {"ok": True}


# ---------- Registered reading devices (Management) ----------
@app.get("/api/v1/devices")
def all_devices(include_revoked: bool = False, user=Depends(auth.require_admin)):
    """Every account's paired devices, for Users and Permissions. Tokens are never shown."""
    return {"items": auth.list_devices(None, include_revoked=include_revoked)}


@app.delete("/api/v1/devices/{device_id}")
def admin_revoke_device(device_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    if not auth.revoke_device(device_id, user_id=None, actor=user["id"]):
        raise HTTPException(404, "Device not found or already revoked")
    return {"ok": True}


# ---------- Devices (Kindle / Kobo) ----------
@app.get("/api/v1/settings/devices")
def get_device_settings(user=Depends(auth.require_admin)):
    return devices.get_smtp_config()


@app.put("/api/v1/settings/devices")
def put_device_settings(req: SmtpConfigRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    devices.set_smtp_config(
        host=req.host, port=req.port, username=req.username, password=req.password,
        from_email=req.from_email, use_tls=req.use_tls, tls_mode=req.tls_mode,
    )
    return devices.get_smtp_config()


@app.post("/api/v1/settings/devices/test")
def test_device_settings(request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Send a small test message to the administrator's own email address and report
    exactly what the relay said. A relay failure is a 200 with ok:false, so the text
    reaches the browser untouched by any proxy's error page."""
    to = user.get("email")
    if not to:
        raise HTTPException(400, "Your account has no email address to send the test to (set one in Accounts)")
    return devices.send_test_email(to, by_user=user.get("username"))


class SigninSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remember_days: int | None = None
    remember_allowed_types: list[str] | None = None
    remember_absolute_days: int | None = None
    trust_device_allowed_types: list[str] | None = None
    trusted_devices_max: int | None = None
    trusted_device_idle_days: int | None = None


@app.get("/api/v1/settings/signin")
def get_signin_settings(user=Depends(auth.require_admin)):
    return signin.get_all()


@app.put("/api/v1/settings/signin")
def put_signin_settings(req: SigninSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Remember-me and trusted-device policy (admin only, audited with the old and new values)."""
    changes = {f"auth.{k}": v for k, v in req.model_dump(exclude_none=True).items()}
    before = signin.get_all()
    try:
        after = signin.update(changes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_history("settings.signin", f"Sign-in persistence settings changed (by {user.get('username')})",
                   data={"before": {k: before[k] for k in changes}, "after": {k: after[k] for k in changes}, "by": user["id"]})
    return after


@app.get("/api/v1/settings/acquisition", dependencies=[Depends(acquisition_svc.require_acquisition)])
def get_acquisition_settings(user=Depends(auth.require_admin)):
    return wanted_search.get_settings()


@app.put("/api/v1/settings/acquisition", dependencies=[Depends(acquisition_svc.require_acquisition)])
def put_acquisition_settings(req: AcquisitionSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    changes = req.model_dump(exclude_none=True)
    try:
        return wanted_search.save_settings(changes, actor=user.get("username") or user.get("id"))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# Format conversion to the preferred formats (issue #265). Admin only; originals are kept.
# ---------------------------------------------------------------------------

def _conversion_refusal(exc: conversion.ConversionRefused) -> HTTPException:
    status = {"not_found": 404, "too_many": 422}.get(exc.code, 409)
    return HTTPException(status, str(exc))


@app.get("/api/v1/settings/conversion")
def get_conversion_settings(user=Depends(auth.require_admin)):
    return {**conversion.get_settings(), "ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "targets": {k: v["target"] for k, v in conversion.KINDS.items()},
            "output_roots": conversion.output_root_choices()}


@app.put("/api/v1/settings/conversion")
def put_conversion_settings(req: ConversionSettingsRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return conversion.save_settings(req.model_dump(exclude_none=True), actor=user.get("username") or user.get("id"))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/v1/assets/{asset_id}/convert", status_code=202)
def convert_asset(asset_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Queue a conversion of this asset to the preferred format (M4B / EPUB). The original stays."""
    try:
        return conversion.enqueue(asset_id, trigger="manual", user=user)
    except conversion.ConversionRefused as exc:
        raise _conversion_refusal(exc)


@app.post("/api/v1/conversions/bulk")
def convert_bulk(req: ConversionBulkRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    """Dry run (default): how many files would be converted and the disk they would use.
    With dry_run=false and confirm=true: queue them. Originals are always kept."""
    try:
        if req.dry_run:
            return {"dry_run": True, **conversion.plan_bulk(req.work_ids)}
        if not req.confirm:
            raise HTTPException(422, "Confirm that originals are kept and the disk use is acceptable (confirm=true)")
        return {"dry_run": False, **conversion.enqueue_bulk(req.work_ids, user=user)}
    except conversion.ConversionRefused as exc:
        raise _conversion_refusal(exc)


@app.get("/api/v1/conversions")
def list_conversions(group: Literal["active", "history"] | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     user=Depends(auth.require_admin)):
    return conversion.list_conversions(group=group, limit=limit, offset=offset)


@app.post("/api/v1/conversions/{conversion_id}/cancel")
def cancel_conversion(conversion_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return conversion.cancel(conversion_id, user)
    except conversion.ConversionRefused as exc:
        raise _conversion_refusal(exc)


@app.post("/api/v1/conversions/{conversion_id}/retry")
def retry_conversion(conversion_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_admin)):
    try:
        return conversion.retry(conversion_id, user)
    except conversion.ConversionRefused as exc:
        raise _conversion_refusal(exc)


@app.post("/api/v1/library/{work_id}/send/kindle")
def send_to_kindle(work_id: str, req: KindleSendRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    """Any signed-in account may send a book it can see - including a kids account,
    to its own kindle_email. It is a delivery of something already on their shelf,
    not an acquisition or a settings change."""
    if user.get("_auth_method") == "device" and not auth.is_cookie_device(user):
        raise HTTPException(403, "Sending to a Kindle is not available to a device credential")
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    to_email = user.get("kindle_email")
    if not to_email:
        raise HTTPException(400, "No Kindle email address on file - set one in your account settings")
    _player_api.kindle_rate_limit(user)
    try:
        return devices.send_to_kindle(work_id, req.asset_id, to_email, by_user_id=user["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except devices.MailError as exc:
        # 424, not 5xx: Cloudflare replaces origin 5xx bodies with its own page and the
        # reason would never reach the person. The detail is shown verbatim.
        raise HTTPException(424, exc.message)


@app.get("/api/v1/library/{work_id}/kobo/{asset_id}")
def download_kepub(work_id: str, asset_id: str, user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    try:
        data, filename = devices.kepub_for_asset(work_id, asset_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Response(data, media_type="application/epub+zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------- Reader / player ----------
# Like Send-to-Kindle and the Kobo download, reading or listening to a book already on
# the shelf is open to any account that can see the work, a kids account included.

@app.get("/api/v1/library/{work_id}/read/{asset_id}")
def reader_manifest(work_id: str, asset_id: str, user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    try:
        return reader.manifest(work_id, asset_id, user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


_NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/v1/library/{work_id}/progress/{asset_id}")
def get_progress(work_id: str, asset_id: str, user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Not found", headers=_NO_STORE)
    if not db.one("SELECT 1 FROM media_assets WHERE id=? AND work_id=?", (asset_id, work_id)):
        raise HTTPException(404, "Not found", headers=_NO_STORE)
    return {"asset_id": asset_id, "progress": reader.get_progress(asset_id, user["id"])}


@app.put("/api/v1/library/{work_id}/progress/{asset_id}")
def put_progress(work_id: str, asset_id: str, req: ProgressRequest, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    # Budget first (memory only), then visibility: a client hammering a book that has left
    # its shelf gets the same cheap 404 every time and, past the budget, a 429 that tells
    # it when to come back, without either costing a database query.
    wait = reader.progress_write_retry_after(user["id"], asset_id)
    if wait:
        raise HTTPException(429, "Saving too often; try again shortly", headers={"Retry-After": str(wait)})
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Not found", headers=_NO_STORE)
    try:
        return reader.set_progress(work_id, asset_id, user["id"], file_id=req.file_id, position=req.position,
                                   location=req.location, percent=req.percent, device_id=req.device_id,
                                   client_updated_at=req.client_updated_at, duration=req.duration)
    except reader.AssetNotFound:
        raise HTTPException(404, "Not found", headers=_NO_STORE)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/v1/library/{work_id}/progress/{asset_id}")
def delete_progress(work_id: str, asset_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(auth.require_user)):
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Not found", headers=_NO_STORE)
    reader.clear_progress(asset_id, user["id"])
    return {"asset_id": asset_id, "progress": None}


@app.get("/api/v1/library/{work_id}/files/{file_id}")
def stream_file(work_id: str, file_id: str, user=Depends(auth.require_user)):
    """The bytes behind the reader and player. FileResponse answers Range requests with
    206, which is what lets <audio> seek without downloading the whole book. Served
    inline (not as an attachment) and from the RESOLVED path, same as /opds/files."""
    if not tagging.is_visible_to(work_id, user):
        raise HTTPException(404, "Work not found")
    row = reader.file_for_work(work_id, file_id)
    if not row:
        raise HTTPException(404, "File not found")
    if row["resolved"] is None:
        raise HTTPException(403, "File path is not inside any known root")
    safe: Path = row["resolved"]
    return FileResponse(safe, media_type=_mime(safe.suffix.lower()),
                        headers={"Content-Disposition": f'inline; filename="{quote(safe.name)}"', "Cache-Control": "private, no-store"})


# ---------- Failures ----------
@app.get("/api/v1/failures")
def get_failures(user=Depends(auth.require_full_access)):
    return {"imports": failures.failed_imports(), "jobs": failures.failed_jobs()}


# ---------- OPDS ----------
def _opds_user(request: Request):
    user = auth.current_user(request)
    if user:
        return user
    # OPDS readers frequently support HTTP Basic. Use local account credentials.
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        import base64
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode()
            username, password = raw.split(":", 1)
        except Exception:
            username = password = ""
        # An API key works as the password in every auth mode (username is ignored),
        # which is how OPDS readers authenticate when sign-in is OIDC-only.
        if password.startswith(auth.API_KEY_PREFIX):
            user = auth.user_for_api_key(password)
            if user:
                return user
        if settings.auth_mode == "local":
            user = auth.opds_basic_login(request, username, password)
            if user:
                return user
    raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": f'Basic realm="{settings.app_name} OPDS"'})


@app.get("/opds/v2/catalog.json")
def opds2(
    request: Request,
    user=Depends(_opds_user),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items, next_offset, total, _works = _opds_publications(request, user, limit=limit, offset=offset)
    links = [{"rel": "self", "href": str(request.base_url).rstrip("/") + "/opds/v2/catalog.json", "type": "application/opds+json"}]
    if next_offset is not None:
        links.append({"rel": "next", "href": str(request.base_url).rstrip("/") + f"/opds/v2/catalog.json?limit={limit}&offset={next_offset}", "type": "application/opds+json"})
    return {
        "metadata": {"title": settings.app_name, "numberOfItems": total},
        "links": links,
        "publications": items,
    }


@app.get("/opds/v1/catalog.xml", response_class=Response)
def opds1(
    request: Request,
    user=Depends(_opds_user),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    import datetime
    from xml.etree import ElementTree as ET
    atom = "http://www.w3.org/2005/Atom"
    pubs, next_offset, _total, works = _opds_publications(request, user, limit=limit, offset=offset)
    feed = ET.Element("feed", {"xmlns": atom})  # no register_namespace: it is process-global
    # Elements are serialized by ElementTree, which escapes all text and attributes.
    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el
    entries = []
    for p in pubs:
        entry = ET.Element("entry")
        sub(entry, "id", "urn:uuid:" + str(p["metadata"]["identifier"]))
        sub(entry, "title", str(p["metadata"]["title"]))
        for a in p["metadata"].get("author", []):
            sub(sub(entry, "author"), "name", str(a["name"]))
        for a in p.get("links", []):
            sub(entry, "link", rel="http://opds-spec.org/acquisition", href=str(a["href"]), type=str(a["type"]))
        entries.append(entry)
    # Use the latest updated_at from the works on this page (respecting visibility).
    # Avoids a full-table MAX() scan; falls back to current time on empty page.
    page_updated: float | None = None
    for w in works:
        ua = w.get("updated_at")
        if ua is not None and (page_updated is None or ua > page_updated):
            page_updated = ua
    if page_updated is not None:
        updated = datetime.datetime.fromtimestamp(page_updated, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        updated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Build next link for v1 pagination
    base = str(request.base_url).rstrip("/")
    cat_href = base + "/opds/v1/catalog.xml"
    nav_type = "application/atom+xml;profile=opds-catalog;kind=acquisition"
    sub(feed, "id", f"{settings.public_url}/opds/v1/catalog.xml")
    sub(feed, "title", str(settings.app_name))
    sub(feed, "updated", updated)
    sub(feed, "link", rel="self", href=cat_href, type=nav_type)
    if next_offset is not None:
        sub(feed, "link", rel="next", href=f"{cat_href}?limit={limit}&offset={next_offset}", type=nav_type)
    feed.extend(entries)
    xml = ET.tostring(feed, encoding="UTF-8", xml_declaration=True)
    return Response(xml, media_type="application/atom+xml;profile=opds-catalog;kind=acquisition")


@app.get("/opds/files/{file_id}")
def opds_file(file_id: str, request: Request, user=Depends(_opds_user)):
    row = db.one(
        """SELECT f.*,a.work_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id
           WHERE f.id=? AND f.missing=0""",
        (file_id,),
    )
    if not row or not Path(row["path"]).is_file() or not tagging.is_visible_to(row["work_id"], user):
        raise HTTPException(404, "File not found")
    # Verify the file is inside a known root (reject escaped symlinks / .. traversal)
    # and serve the RESOLVED path, so a symlink swapped in after the check is not followed.
    safe = devices.resolve_library_file(row["path"])
    if safe is None:
        raise HTTPException(403, "File path is not inside any known root")
    return FileResponse(safe, filename=safe.name, media_type=_mime(safe.suffix.lower()))


# ---------- Helpers ----------

# SQLite has a hard limit on the number of variables in a single statement
# (default 999).  When a page has many works the IN-lists for authors, series,
# assets, tags, cover_cache and ratings can exceed that, especially because the
# files query is keyed on *asset* ids (which can be many per work).  We chunk
# IN lists into batches of 250 placeholders to stay well under the limit.
_IN_CHUNK_SIZE = 250


def _in_clause(column: str, ids: list[str]) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(placeholder_string, params)`` for an IN list, chunked.

    Returns ``(column IN (?,?,...))`` suitable for direct insertion into a WHERE
    clause.  When the list is empty returns ``(None, ())`` so callers can skip
    the query entirely.

    Chunked into groups of 250 to stay under SQLite's 999-variable limit.
    """
    if not ids:
        return (None, ())
    parts: list[str] = []
    params: list[str] = []
    for i in range(0, len(ids), _IN_CHUNK_SIZE):
        chunk = ids[i : i + _IN_CHUNK_SIZE]
        parts.append(f"{column} IN ({','.join('?' for _ in chunk)})")
        params.extend(chunk)
    return (" OR ".join(parts), tuple(params))


def _batch_library_summary(
    works: list[dict[str, Any]],
    user: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build summaries for a page of works using a constant number of queries.

    Instead of N queries per work (authors, series, assets, files, tags, cover
    cache, ratings, read states) we batch-load everything by work_id and then
    assemble per-work dicts from the in-memory lookups.
    """
    work_ids = [w["id"] for w in works]
    if not work_ids:
        return []

    # 1. Authors (one query)
    authors_where, authors_params = _in_clause("wc.work_id", work_ids)
    authors_rows = db.all(
        f"SELECT wc.work_id, wc.role, c.id AS cid, c.name FROM work_contributors wc "  # nosec B608
        f"JOIN contributors c ON c.id=wc.contributor_id "
        f"WHERE {authors_where} AND wc.role IN ('author','narrator') "
        "ORDER BY wc.ord",
        authors_params,
    )
    authors_by_work: dict[str, list[dict[str, Any]]] = {}
    narrators_by_work: dict[str, list[dict[str, Any]]] = {}
    for r in authors_rows:
        target = narrators_by_work if r["role"] == "narrator" else authors_by_work
        target.setdefault(r["work_id"], []).append({"id": r["cid"], "name": r["name"]})

    # 2. Series (one query)
    series_where, series_params = _in_clause("sm.work_id", work_ids)
    series_rows = db.all(
        f"SELECT sm.work_id, s.id AS sid, s.name, sm.position "  # nosec B608
        f"FROM series_memberships sm JOIN series s ON s.id=sm.series_id "
        f"WHERE {series_where} ORDER BY s.name",
        series_params,
    )
    series_by_work: dict[str, list[dict[str, Any]]] = {}
    for r in series_rows:
        series_by_work.setdefault(r["work_id"], []).append(
            {"id": r["sid"], "name": r["name"], "position": r["position"]}
        )

    # 3. Assets (one query)
    assets_where, assets_params = _in_clause("work_id", work_ids)
    assets_rows = db.all(
        f"SELECT * FROM media_assets WHERE {assets_where} ORDER BY kind,(derived_from_asset_id IS NOT NULL),format",   # originals first: assetStatus() takes the first of a kind  # nosec B608
        assets_params,
    )
    assets_by_work: dict[str, list[dict[str, Any]]] = {}
    asset_ids: list[str] = []
    for a in assets_rows:
        a["narrators"] = db.json_load(a.pop("narrator_json", None), [])
        by_name = {n["name"]: n["id"] for n in narrators_by_work.get(a["work_id"], [])}
        a["narrator_ids"] = [by_name.get(n) for n in a["narrators"]]
        a["quality"] = db.json_load(a.pop("quality_json", None), {})
        a["quality_state"] = (a["quality"].get("evaluation") or {}).get("state", "unevaluated")
        assets_by_work.setdefault(a["work_id"], []).append(a)
        asset_ids.append(a["id"])
    # What each Work owns vs still wants, judged per kind from the raw rows (before any viewer scrubbing).
    monitored_by_work = {w["id"]: bool(w.get("monitored", 1)) for w in works}
    ownership_by_work = {wid: ownership.summarize(rows, monitored_by_work.get(wid, True)) for wid, rows in assets_by_work.items()}

    # 4. Files (one query, chunked on asset ids)
    files_rows: list[dict[str, Any]] = []
    if asset_ids:
        files_where, files_params = _in_clause("asset_id", asset_ids)
        files_rows = db.all(
            f"SELECT id, asset_id, path, part_number, size_bytes, container, codec, "  # nosec B608
            f"bitrate, duration_seconds, adopted, managed, missing "
            f"FROM media_files WHERE {files_where} ORDER BY part_number, path",
            files_params,
        )
    files_by_asset: dict[str, list[dict[str, Any]]] = {}
    for f in files_rows:
        files_by_asset.setdefault(f["asset_id"], []).append(
            {k: v for k, v in f.items() if k != "asset_id"}
        )
    fmt_order: dict[str, list[str]] = {}
    for a_list in assets_by_work.values():
        for a in a_list:
            a["files"] = files_by_asset.get(a["id"], [])
        derived.annotate(a_list)
        assetformats.annotate(a_list, fmt_order)
        assetformats.scrub_for_viewer(a_list, user)

    # 5. Tags (one query)
    tags_where, tags_params = _in_clause("wt.work_id", work_ids)
    tags_rows = db.all(
        f"SELECT wt.work_id, t.id, t.name, t.kind, t.color, wt.source, wt.confidence "  # nosec B608
        f"FROM work_tags wt JOIN tags t ON t.id=wt.tag_id "
        f"WHERE {tags_where} ORDER BY t.kind DESC, t.name COLLATE NOCASE",
        tags_params,
    )
    tags_by_work: dict[str, list[dict[str, Any]]] = {}
    for r in tags_rows:
        tags_by_work.setdefault(r["work_id"], []).append(
            {**{k: v for k, v in r.items() if k != "work_id"}, "label": tagging.tag_label(r["name"])}
        )

    # 6. Cover cache (one query)
    cover_where, cover_params = _in_clause("work_id", work_ids)
    cover_rows = db.all(
        f"SELECT work_id FROM cover_cache WHERE {cover_where} AND status='cached'",  # nosec B608
        cover_params,
    )
    covered_works = {r["work_id"] for r in cover_rows}

    # 7. Ratings (one query, only if user provided)
    ratings_by_work: dict[str, int | None] = {}
    read_status_by_work: dict[str, str | None] = {}
    if user:
        rating_where, rating_params = _in_clause("work_id", work_ids)
        rating_rows = db.all(
            f"SELECT work_id, rating FROM ratings WHERE {rating_where} AND user_id=?",  # nosec B608
            tuple(rating_params) + (user["id"],),
        )
        for r in rating_rows:
            ratings_by_work[r["work_id"]] = r["rating"]
        rs_where, rs_params = _in_clause("work_id", work_ids)
        rs_rows = db.all(
            f"SELECT work_id, status FROM read_states WHERE {rs_where} AND user_id=?",  # nosec B608
            tuple(rs_params) + (user["id"],),
        )
        for r in rs_rows:
            read_status_by_work[r["work_id"]] = r["status"]

    # Assemble per-work summaries
    result = []
    for w in works:
        wid = w["id"]
        result.append({
            "id": wid,
            "metadata_id": w.get("metadata_id"),
            "title": w["title"],
            "sort_title": w.get("sort_title"),
            "subtitle": w.get("subtitle"),
            "cover_url": w.get("cover_url"),
            "has_cover": wid in covered_works,
            "first_publish_date": w.get("first_publish_date"),
            "primary_language": w.get("primary_language"),
            "monitored": bool(w["monitored"]),
            "metadata_status": w["metadata_status"],
            "metadata_source": w.get("metadata_source"),
            "authors": authors_by_work.get(wid, []),
            "narrators": narrators_by_work.get(wid, []),
            "series": series_by_work.get(wid, []),
            "assets": assets_by_work.get(wid, []),
            **ownership_by_work.get(wid, ownership.summarize([])),
            "tags": tags_by_work.get(wid, []),
            "rating": ratings_by_work.get(wid),
            "read_status": read_status_by_work.get(wid),
        })
    return result


def _library_summary(w: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Single-work summary (used by work_detail).

    Kept for backward compatibility with the work-detail endpoint which
    calls this for a single work.  In practice it still does per-work
    queries, but that is only one work so the N+1 cost is negligible.
    """
    authors = db.all(
        "SELECT c.id,c.name FROM work_contributors wc "
        "JOIN contributors c ON c.id=wc.contributor_id "
        "WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord",
        (w["id"],),
    )
    narrators = db.all(
        "SELECT c.id,c.name FROM work_contributors wc "
        "JOIN contributors c ON c.id=wc.contributor_id "
        "WHERE wc.work_id=? AND wc.role='narrator' ORDER BY wc.ord",
        (w["id"],),
    )
    series_rows = db.all(
        "SELECT s.id,s.name,sm.position FROM series_memberships sm "
        "JOIN series s ON s.id=sm.series_id WHERE sm.work_id=? ORDER BY s.name",
        (w["id"],),
    )
    assets = db.all("SELECT * FROM media_assets WHERE work_id=? ORDER BY kind,(derived_from_asset_id IS NOT NULL),format", (w["id"],))
    for a in assets:
        a["narrators"] = db.json_load(a.pop("narrator_json"), [])
        by_name = {n["name"]: n["id"] for n in narrators}
        a["narrator_ids"] = [by_name.get(n) for n in a["narrators"]]
        a["quality"] = db.json_load(a.pop("quality_json"), {})
        a["quality_state"] = (a["quality"].get("evaluation") or {}).get("state", "unevaluated")
        a["files"] = db.all(
            "SELECT id,path,part_number,size_bytes,container,codec,bitrate,"
            "duration_seconds,adopted,managed,missing FROM media_files "
            "WHERE asset_id=? ORDER BY part_number,path",
            (a["id"],),
        )
    owned_summary = ownership.summarize(assets, bool(w.get("monitored", 1)))
    derived.annotate(assets)
    assetformats.annotate(assets)
    assetformats.scrub_for_viewer(assets, user)
    # rating/read_status are per-viewer, not per-book - two accounts reading the same
    # shared library legitimately disagree. Omitted (None) entirely when there is no
    # viewer context (OPDS builds its catalog without one).
    rating = read_status = None
    if user:
        r = db.one("SELECT rating FROM ratings WHERE work_id=? AND user_id=?", (w["id"], user["id"]))
        rating = r["rating"] if r else None
        rs = db.one("SELECT status FROM read_states WHERE work_id=? AND user_id=?", (w["id"], user["id"]))
        read_status = rs["status"] if rs else None
    return {
        "id": w["id"], "metadata_id": w.get("metadata_id"), "title": w["title"],
        "sort_title": w.get("sort_title"), "subtitle": w.get("subtitle"),
        "cover_url": w.get("cover_url"), "has_cover": covers.has_cached(w["id"]),
        "first_publish_date": w.get("first_publish_date"),
        "primary_language": w.get("primary_language"),
        "monitored": bool(w["monitored"]), "metadata_status": w["metadata_status"],
        "metadata_source": w.get("metadata_source"),
        "authors": authors, "narrators": narrators, "series": series_rows, "assets": assets,
        **owned_summary,
        "tags": tagging.work_tags(w["id"]),
        "rating": rating, "read_status": read_status,
    }


def _opds_publications(
    request: Request,
    user: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int | None, int, list[dict[str, Any]]]:
    """Return OPDS publications for a page.

    Returns (items, next_offset, total, works).  ``next_offset`` is None when
    there are no more pages.  ``total`` is the total number of works matching
    the visibility filter and have at least one downloadable file (not just the
    page length).  ``works`` is the list of
    work dicts for this page (including ``updated_at``) so callers can derive
    page-level metadata without extra queries.
    """
    # Only works with at least one present file can appear in the feed, so the
    # filter lives in SQL: total, LIMIT/OFFSET and the next link then all agree.
    visible_clause, vparams = tagging.kids_visibility(user)
    visible = (
        f"{visible_clause} AND EXISTS ("  # nosec B608
        "SELECT 1 FROM media_assets ma JOIN media_files mf ON mf.asset_id=ma.id "
        "WHERE ma.work_id=w.id AND mf.missing=0)"
    )
    total = db.one(
        f"SELECT COUNT(*) n FROM works w WHERE {visible}", vparams,  # nosec B608
    )["n"]
    works = db.all(
        f"SELECT w.* FROM works w WHERE {visible} "  # nosec B608
        "ORDER BY w.sort_title,w.title,w.id LIMIT ? OFFSET ?",
        (*vparams, limit, offset),
    )

    # Batch-load summaries for this page (no user — OPDS output doesn't use
    # rating or read_status, so skip those queries).
    summaries = _batch_library_summary(works, user=None)

    out = []
    for w, summary in zip(works, summaries):
        links = []
        for a in summary["assets"]:
            for f in a["files"]:
                if not f["missing"]:
                    ext = "." + (f.get("format") or "")
                    links.append({
                        "rel": "http://opds-spec.org/acquisition",
                        "href": str(request.base_url).rstrip("/") + f"/opds/files/{f['id']}",
                        "type": _mime(ext),
                        "title": f"{a['kind']} {ext.lstrip('.').upper()}",
                    })
        out.append({
            "metadata": {
                "title": w["title"],
                "identifier": w["id"],
                "author": [{"name": x["name"]} for x in summary["authors"]],
                "language": w.get("primary_language"),
                "published": w.get("first_publish_date"),
            },
            "links": links,
        })

    next_offset = (offset + len(works)) if (offset + len(works)) < total else None
    return out, next_offset, total, works


def _mime(ext: str) -> str:
    return {".epub": "application/epub+zip", ".pdf": "application/pdf", ".mobi": "application/x-mobipocket-ebook", ".azw3": "application/vnd.amazon.ebook", ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac"}.get(ext, "application/octet-stream")


# The versioned Player API (player_api.py) is mounted before the static shells so its
# routes win over the SPA fallback. Imported here, late, because it reuses this module's
# work summary.
app.include_router(_player_api.router)
app.include_router(_discover_api.router)
app.include_router(_podcast_api.router)
app.include_router(_podcast_library_api.router)

# Static UI mount comes last so /api and /opds routes win.
STATIC_DIR = Path(__file__).parent / "static"


class RevalidatedStaticFiles(StaticFiles):
    """The UI files are unversioned (/assets/app.js), so without a directive browsers
    and CDNs such as Cloudflare reuse an old copy on a heuristic timer and a deploy
    does not show up. no-cache means "store it, but revalidate by ETag first": an
    unchanged file costs a 304, a changed one is picked up immediately."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/assets", RevalidatedStaticFiles(directory=STATIC_DIR), name="assets")


# PWA files live at the origin root on purpose. A service worker may only control
# paths at or below its own URL, and the UI's routes are hash fragments of "/", so
# /assets/sw.js could never serve the app shell offline; browsers likewise expect the
# manifest next to the page. Both are no-cache like the rest of the UI so a deploy
# with a new worker is picked up on the next load (the browser also byte-compares
# the worker script every 24 h regardless).
@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    text = (STATIC_DIR / "manifest.webmanifest").read_text().replace(_NAME_TOKEN, json.dumps(settings.app_name)[1:-1])
    return Response(text, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


# The UI files carry this token wherever the product name shows; the server fills it in from
# settings.app_name (the one place the name is set) when it serves the file.
_NAME_TOKEN = "{{app_name}}"
_SW_BUILD_RE = re.compile(r"const BUILD = '[^']*';")
_ASSET_REF_RE = re.compile(r'((?:src|href)="/assets/[^"?]+\.(?:js|css))"')


def _safe_token(value: str) -> str:
    """Build values end up in URLs, HTML attributes and a JS literal: keep a conservative alphabet."""
    return re.sub(r"[^A-Za-z0-9._:-]", "", value or "")[:64]


@app.get("/sw.js", include_in_schema=False)
def pwa_service_worker():
    """The worker with the build baked in: every deploy changes its bytes, so browsers install
    it, and its shell cache name follows the build (media caches keep theirs)."""
    text = (STATIC_DIR / "sw.js").read_text()
    text = _SW_BUILD_RE.sub(lambda m: f"const BUILD = '{_safe_token(buildinfo.short()['sha'])}';", text, count=1)
    # The token sits in single-quoted JS strings that build HTML: escape for HTML text (quotes stay
    # literal there), then for the JS string.
    js_name = json.dumps(html_escape(settings.app_name, quote=False))[1:-1].replace("'", "\\'")
    text = text.replace(_NAME_TOKEN, js_name)
    return Response(text, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


MANAGEMENT_FORBIDDEN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{app_name}}</title><link rel="stylesheet" href="/assets/player.css"></head>
<body class="player-body"><main class="pl-gate"><div class="pl-gate-card"><img class="pl-logo" src="/assets/brand/bookdoe-mark.svg" alt="" width="44" height="44"><h1>Management is for administrators</h1>
<p>This account can read, listen and request books in the Player, but cannot open the library's administration.</p>
<a class="pl-button" href="/app">Open the Player</a></div></main></body></html>"""


def _shell(name: str) -> HTMLResponse:
    """An HTML shell stamped with the build: the sha is a <meta name="alx-build"> (read by pwa.js
    as window.__ALX_BUILD__) and goes on every script/stylesheet URL, so a deploy changes
    the asset URLs and a browser or the worker never pairs old JS with a new API."""
    b = buildinfo.short()
    sha = _safe_token(b["sha"])
    html = (STATIC_DIR / name).read_text()
    if name == "setup.html" and not acquisition_svc.acquisition_enabled():
        # No indexer or download-client step in the wizard while acquisition is off.
        html = re.sub(r"<!--ACQ-->.*?<!--/ACQ-->\s*|/\*ACQ\*/.*?/\*/ACQ\*/\s*", "", html, flags=re.S)
    html = _ASSET_REF_RE.sub(lambda m: f'{m.group(1)}?v={sha}"', html)
    name = html_escape(settings.app_name)
    html = html.replace(_NAME_TOKEN, name)
    # The scripts read the name from here; data-brand lets the CSS show the vendored logo
    # only while the instance still carries the default name.
    html = html.replace("<html ", f'<html data-brand="{"default" if settings.default_brand else "custom"}" ', 1)
    meta = (f'<meta name="alx-build" content="{sha}" data-built="{_safe_token(b["built_at"] or "")}" />\n'
            f'  <meta name="alx-product" content="{name}" data-tagline="{html_escape(settings.app_tagline)}" />')
    html = html.replace("<head>", "<head>\n  " + meta, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
def setup_page():
    """The first-run wizard. Served only while the install is empty; afterwards the
    URL goes to Management, where every setting it covers lives."""
    if not setup_svc.setup_required():
        return RedirectResponse("/manage", status_code=303)
    return _shell("setup.html")


@app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
@app.get("/manage/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def management_shell(request: Request, path: str = ""):
    """The Management SPA. The real boundary is every /api/v1 management route being
    admin-only; this keeps a kid or adult who types the URL from even loading the
    shell. Nobody signed in gets the shell so its sign-in screen can run."""
    user = auth.current_user(request)
    if user and auth.is_cookie_device(user) and user.get("is_admin"):
        # A trusted browser of an administrator is Player-only: Management shows its own
        # sign-in (the shell treats this identity as signed out) and needs the normal login.
        return _shell("index.html")
    if user and not (user.get("is_admin") and user.get("_auth_method") != "device"):
        return HTMLResponse(MANAGEMENT_FORBIDDEN_HTML.replace(_NAME_TOKEN, html_escape(settings.app_name)), status_code=403, headers={"Cache-Control": "no-store"})
    return _shell("index.html")


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa(path: str):
    """The Player SPA at /, /app and /app/... (and as the fallback for any other path, so
    the installed PWA and old links keep resolving). Mistyped API URLs stay 404."""
    if path.startswith("api/") or path.startswith("opds/") or path.startswith("auth/"):
        raise HTTPException(404)
    if path in ("", "app") and setup_svc.setup_required():
        return RedirectResponse("/setup", status_code=303)
    return _shell("player.html")
