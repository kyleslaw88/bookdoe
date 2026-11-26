"""A small per-admin budget for the destructive merge/cleanup actions (merge, undo, remove, restore, purge): 30 per minute.
In-memory and per process, like the other limiters here; it exists to stop a runaway script or a stuck double-click loop, not
to meter a person."""
from __future__ import annotations

import threading
import time

LIMIT = 30
WINDOW_SECONDS = 60.0
_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"Too many merge/cleanup actions; try again in {retry_after}s")
        self.retry_after = retry_after


def check(user: object, now: float | None = None) -> None:
    """Count one write action for this admin; RateLimited once the window's budget is spent."""
    key = str(user.get("id") if isinstance(user, dict) else user or "anonymous")
    t = time.monotonic() if now is None else now
    with _lock:
        hits = [h for h in _hits.get(key, []) if t - h < WINDOW_SECONDS]
        if len(hits) >= LIMIT:
            _hits[key] = hits
            raise RateLimited(int(WINDOW_SECONDS - (t - hits[0])) + 1)
        hits.append(t)
        _hits[key] = hits


def reset() -> None:
    with _lock:
        _hits.clear()
