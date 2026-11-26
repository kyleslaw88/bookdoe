"""Shared provider result states and persisted retry state (F-08, spec Part XXXIX).

Covers already told a throttled response apart from a missing one (services/covers.py).
Metadata and search providers now share that vocabulary: a 429 is ``rate_limited`` and is
rescheduled, never "not found" and never "no data".  Only ``success`` and ``not_found`` are
answers; everything else says nothing about whether the provider has the data, so callers must
leave existing data alone and must not record a miss.

Retry state lives in ``provider_retry_state`` (migration step 7), keyed by provider and entity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .. import db

log = logging.getLogger(__name__)

SUCCESS = "success"
NOT_FOUND = "not_found"
TRANSIENT = "transient"
RATE_LIMITED = "rate_limited"
DEFERRED = "deferred"
PERMANENT = "permanent"

STATUSES = (SUCCESS, NOT_FOUND, TRANSIENT, RATE_LIMITED, DEFERRED, PERMANENT)
# Statuses that are scheduled for another attempt.
RETRYABLE = (TRANSIENT, RATE_LIMITED, DEFERRED)

BACKOFF_BASE_SECONDS = {TRANSIENT: 60.0, RATE_LIMITED: 300.0, DEFERRED: 60.0}
BACKOFF_MAX_SECONDS = 6 * 3600.0


@dataclass
class ProviderResult:
    status: str
    provider: str = ""
    data: Any = None
    error: str | None = None
    retry_after: float | None = None  # seconds, as the provider asked (Retry-After)
    retry_at: float | None = None  # filled in once the retry has been scheduled

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE

    @property
    def is_answer(self) -> bool:
        return self.status in (SUCCESS, NOT_FOUND)


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        value = float(headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None
    return value if value >= 0 else None


def classify_status(code: int, headers: Any = None) -> tuple[str, float | None]:
    """Map an HTTP status to a result state.  403 counts as throttling, as in covers.py
    (CDNs and Google Books answer quota exhaustion with it)."""
    if code in (403, 429):
        return RATE_LIMITED, _retry_after_seconds(headers)
    if code == 404:
        return NOT_FOUND, None
    if code == 408 or code >= 500:
        return TRANSIENT, _retry_after_seconds(headers)
    return PERMANENT, None


def classify_exception(exc: BaseException, provider: str = "") -> ProviderResult:
    if isinstance(exc, httpx.HTTPStatusError):
        status, retry_after = classify_status(exc.response.status_code, exc.response.headers)
        return ProviderResult(status, provider, error=f"{exc.response.status_code} from {exc.request.url.host}", retry_after=retry_after)
    # Timeouts, resets, malformed bodies and anything unexpected: the same class of
    # problem as a 5xx - try again later rather than conclude anything about the data.
    return ProviderResult(TRANSIENT, provider, error=f"{type(exc).__name__}: {exc}"[:300])


def backoff_seconds(status: str, attempts: int, retry_after: float | None = None) -> float:
    delay = min(BACKOFF_BASE_SECONDS.get(status, 60.0) * (2 ** max(0, attempts - 1)), BACKOFF_MAX_SECONDS)
    return max(delay, retry_after or 0.0)


def retry_at(provider: str, entity_type: str, entity_id: str) -> float | None:
    """When the provider may next be asked about this entity, or None if it may be asked now."""
    row = db.one(
        "SELECT next_retry_at FROM provider_retry_state WHERE provider=? AND entity_type=? AND entity_id=?",
        (provider, entity_type, entity_id),
    )
    if row and row["next_retry_at"] and row["next_retry_at"] > db.now():
        return row["next_retry_at"]
    return None


def record(provider: str, entity_type: str, entity_id: str, result: ProviderResult) -> ProviderResult:
    """Persist the outcome.  An answer (success / not found) clears the retry state; a
    retryable failure schedules the next attempt; a permanent one is kept without one.
    Nothing here touches the entity's own data."""
    with db.tx() as conn:
        if result.is_answer:
            conn.execute("DELETE FROM provider_retry_state WHERE provider=? AND entity_type=? AND entity_id=?",
                         (provider, entity_type, entity_id))
            return result
        t = db.now()
        prev = conn.execute(
            "SELECT attempts, first_failed_at FROM provider_retry_state WHERE provider=? AND entity_type=? AND entity_id=?",
            (provider, entity_type, entity_id),
        ).fetchone()
        attempts = (prev["attempts"] if prev else 0) + 1
        result.retry_at = t + backoff_seconds(result.status, attempts, result.retry_after) if result.retryable else None
        conn.execute(
            """INSERT INTO provider_retry_state(provider,entity_type,entity_id,status,attempts,next_retry_at,last_error,first_failed_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,entity_type,entity_id) DO UPDATE SET status=excluded.status,
                 attempts=excluded.attempts,next_retry_at=excluded.next_retry_at,last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (provider, entity_type, entity_id, result.status, attempts, result.retry_at, result.error,
             prev["first_failed_at"] if prev else t, t),
        )
    log.info("provider %s %s for %s %s; retry at %s", provider, result.status, entity_type, entity_id, result.retry_at)
    return result


def deferred(provider: str, entity_type: str, entity_id: str) -> ProviderResult | None:
    """A ``deferred`` result if a retry is still pending for this provider and entity,
    so the provider is not asked again before it is due."""
    due = retry_at(provider, entity_type, entity_id)
    if due is None:
        return None
    return ProviderResult(DEFERRED, provider, error="retry not yet due", retry_at=due)
