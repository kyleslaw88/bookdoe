from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from . import legacy
from .secrets_provider import load_env_file, secret as _secret

# The setup wizard writes <data dir>/setup.env; merge it before any setting is read.
# Real environment variables always win over the file (see secrets_provider.py).
legacy.apply_legacy_env()
load_env_file()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


DEFAULT_APP_NAME = "bookdoe"
APP_TAGLINE = "Every Story. One Library."


@dataclass(frozen=True)
class Settings:
    # The public product name every page, the manifest and the service worker show; an
    # instance re-labels itself by setting it. Internal names (env vars, DB, paths) stay put.
    app_name: str = os.getenv("BOOKDOE_APP_NAME", "").strip() or DEFAULT_APP_NAME
    app_tagline: str = APP_TAGLINE
    data_dir: Path = Path(os.getenv("BOOKDOE_DATA_DIR", "/config"))
    db_path: Path = Path(os.getenv("BOOKDOE_DB_PATH") or legacy.default_db_path())
    log_level: str = os.getenv("BOOKDOE_LOG_LEVEL", "INFO")
    # Credentials go through the secrets provider: environment, then the env file,
    # then the optional backend (Infisical); any of them may hold a reference such as
    # infisical://NAME or file:///run/secrets/name.
    secret_key: str = _secret("BOOKDOE_SECRET_KEY", "")
    public_url: str = os.getenv("BOOKDOE_PUBLIC_URL", "http://localhost:8787").rstrip("/")
    auth_mode: str = os.getenv("BOOKDOE_AUTH_MODE", "local").lower()
    bootstrap_user: str = os.getenv("BOOKDOE_BOOTSTRAP_USER", "admin")
    bootstrap_password: str = _secret("BOOKDOE_BOOTSTRAP_PASSWORD", "")
    proxy_user_header: str = os.getenv("BOOKDOE_PROXY_USER_HEADER", "X-authentik-username")
    proxy_email_header: str = os.getenv("BOOKDOE_PROXY_EMAIL_HEADER", "X-authentik-email")
    oidc_issuer: str = os.getenv("BOOKDOE_OIDC_ISSUER", "").rstrip("/")
    oidc_client_id: str = os.getenv("BOOKDOE_OIDC_CLIENT_ID", "")
    oidc_client_secret: str = _secret("BOOKDOE_OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.getenv("BOOKDOE_OIDC_SCOPES", "openid profile email")
    # Comma list of group names; when set, an OIDC sign-in that would CREATE or newly
    # LINK an account must carry at least one of them in its `groups` claim.
    oidc_allowed_groups: str = os.getenv("BOOKDOE_OIDC_ALLOWED_GROUPS", "")
    # Account-type mapping from the ID token's `groups` claim (docs/PLAYER_MANAGEMENT.md).
    # Group names are suggestions, not requirements; empty disables that mapping.
    oidc_group_admin: str = os.getenv("BOOKDOE_OIDC_GROUP_ADMIN", "bookdoe-admin")
    oidc_group_adult: str = os.getenv("BOOKDOE_OIDC_GROUP_ADULT", "bookdoe-adult")
    oidc_group_kid: str = os.getenv("BOOKDOE_OIDC_GROUP_KID", "bookdoe-kid")
    # 'provision': groups decide the type of a NEW account only; an administrator owns the
    # type afterwards. 'groups': the claim is authoritative on every sign-in (demotions
    # included, last-admin guarded). Anything else is read as 'provision'.
    oidc_role_source: str = os.getenv("BOOKDOE_OIDC_ROLE_SOURCE", "provision").lower()
    # Comma list of preferred_username or sub values that become administrators on their
    # first OIDC sign-in: the no-self-registration way to seed the first admin.
    oidc_bootstrap_admins: str = os.getenv("BOOKDOE_OIDC_BOOTSTRAP_ADMINS", "")
    # Comma list of CIDRs/IPs of reverse proxies allowed to assert the proxy-auth
    # identity header (and X-Forwarded-For). Empty means the header is never honoured.
    trusted_proxies: str = os.getenv("BOOKDOE_TRUSTED_PROXIES", "")
    openreads_url: str = os.getenv("OPENREADS_URL", "http://openreads:8790").rstrip("/")
    openreads_api_key: str = _secret("OPENREADS_API_KEY", "")
    scan_hash_full_max_mb: int = int(os.getenv("BOOKDOE_FULL_HASH_MAX_MB", "64"))
    scheduler_enabled: bool = _bool("BOOKDOE_SCHEDULER_ENABLED", True)
    backup_dir: Path = Path(os.getenv("BOOKDOE_BACKUP_DIR", "/config/backups"))
    backup_retention: int = int(os.getenv("BOOKDOE_BACKUP_RETENTION", "14"))
    # Optional read-only mount of OpenReads' own database (see docs/OPERATIONS.md). When
    # set and present, the database-backup job also backs it up. Unset = no backup of it.
    openreads_db_path: Path | None = Path(os.environ["BOOKDOE_OPENREADS_DB_PATH"]) if os.getenv("BOOKDOE_OPENREADS_DB_PATH") else None
    # Pre-migration safety copies (migrations.py) are kept apart from the scheduled
    # backups, so neither prunes the other. Newest N are kept.
    migration_backup_retention: int = int(os.getenv("BOOKDOE_MIGRATION_BACKUP_RETENTION", "5"))
    cover_dir: Path = Path(os.getenv("BOOKDOE_COVER_DIR", "/config/covers"))
    # Folder the podcast-scan job walks (<Show>/Season NN/*.mp3, services/podcastscan.py). Unset = the job is a no-op.
    podcasts_dir: Path | None = Path(os.environ["BOOKDOE_PODCASTS_DIR"]) if os.getenv("BOOKDOE_PODCASTS_DIR") else None
    cover_remote_fetch: bool = _bool("BOOKDOE_COVER_REMOTE_FETCH", True)
    # Optional, independent of OpenReads' own Google Books enrichment: a large batch
    # import can exhaust OpenReads' daily quota on that same key long before every
    # work has an edition-level image_url. When set, covers.py asks Google Books
    # directly (by ISBN only) as a last-resort source, off entirely when unset.
    google_books_api_key: str = _secret("BOOKDOE_GOOGLE_BOOKS_API_KEY", "")
    # Federated search providers (Part XVII.C) -- optional, off by way of an empty
    # key rather than a hard failure; both providers work anonymously at a lower
    # rate limit, same posture as google_books_api_key above.
    hardcover_api_key: str = _secret("BOOKDOE_HARDCOVER_API_KEY", "")
    audiobookdb_api_key: str = _secret("BOOKDOE_AUDIOBOOKDB_API_KEY", "") or _secret("AUDIOBOOKDB_API_KEY", "")
    developer_mode: bool = _bool("BOOKDOE_DEVELOPER_MODE", False)
    # Pre-migration free-space safety margin (fraction, e.g. 0.10 = 10%).
    migration_safety_margin: float = float(os.getenv("BOOKDOE_MIGRATION_SAFETY_MARGIN", "0.10"))

    # Off by default. Every LLM-backed feature (kids-tag classification, "if you liked
    # this" recommendations) has a non-LLM fallback and must keep working with this
    # unset - see llmclient.py. Kids classification in particular treats "the LLM is
    # unreachable" as "abstain", never as "assume safe".
    llm_enabled: bool = _bool("BOOKDOE_LLM_ENABLED", False)
    llm_base_url: str = os.getenv("BOOKDOE_LLM_BASE_URL", "").rstrip("/")
    llm_api_key: str = _secret("BOOKDOE_LLM_API_KEY", "")
    llm_model: str = os.getenv("BOOKDOE_LLM_MODEL", "")
    llm_timeout: float = float(os.getenv("BOOKDOE_LLM_TIMEOUT", "30"))
    # Kids classifier batches (scheduled job and the Management scan). Each book is one
    # LLM call on a slow local reasoning model, so a batch is small, gives up starting
    # new books after a wall-clock budget, and uses its own per-call timeout; other LLM
    # callers keep llm_timeout. Observed live 2026-10-11: 20 x 300 s calls inside the
    # scheduler loop stalled every job for 20 minutes and failed /readyz.
    classify_batch: int = int(os.getenv("BOOKDOE_CLASSIFY_BATCH", "5"))
    classify_budget_seconds: float = float(os.getenv("BOOKDOE_CLASSIFY_BUDGET_SECONDS", "120"))
    classify_llm_timeout: float = float(os.getenv("BOOKDOE_CLASSIFY_LLM_TIMEOUT", "120"))
    # Audience-metadata backfill (kids classifier): works per run, wall-clock budget per run, and
    # the pause between works. Each work costs at most ~2 provider calls (Open Library subjects +
    # Google Books categories), so the defaults (6 works / 10 min = 864 works/day) stay under
    # Google Books' default 1000 requests/day quota.
    audience_batch: int = int(os.getenv("BOOKDOE_AUDIENCE_BATCH", "6"))
    audience_budget_seconds: float = float(os.getenv("BOOKDOE_AUDIENCE_BUDGET_SECONDS", "90"))
    audience_pause_seconds: float = float(os.getenv("BOOKDOE_AUDIENCE_PAUSE_SECONDS", "1.0"))
    # A job run older than this with no completion is treated as dead: its lease is
    # released so the kind can run again and the row is marked failed.
    job_lease_seconds: float = float(os.getenv("BOOKDOE_JOB_LEASE_SECONDS", "3600"))

    @property
    def default_brand(self) -> bool:
        """True while the instance keeps the default name, so the UI shows the vendored logo."""
        return self.app_name == DEFAULT_APP_NAME

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.cover_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
