"""Which build is running: git sha, build time, process start and database step.

The Docker image carries the stamp twice (env vars and app/build_info.json, written by
the Dockerfile from the GIT_SHA / BUILD_TIME build args); local dev has neither and
reports sha "dev". Env wins over the file so an operator can override it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import __version__

API_VERSION = "1"
INFO_FILE = Path(__file__).resolve().parent.parent / "build_info.json"
_STARTED_AT = time.time()


def _stamp() -> dict[str, str]:
    """sha and built_at from the environment, else build_info.json, else dev/empty."""
    sha = (os.environ.get("BOOKDOE_BUILD_SHA") or "").strip()
    built = (os.environ.get("BOOKDOE_BUILD_TIME") or "").strip()
    if not sha:
        try:
            data = json.loads(INFO_FILE.read_text())
            sha = str(data.get("sha") or "").strip()
            built = built or str(data.get("built_at") or "").strip()
        except (OSError, ValueError, AttributeError):
            pass
    return {"sha": sha or "dev", "built_at": built}


def migration_step() -> int | None:
    """Highest applied migration step; None when the table cannot be read."""
    try:
        from .. import db
        row = db.one("SELECT MAX(version) AS v FROM migration_steps")
        return int(row["v"]) if row and row["v"] is not None else 0
    except Exception:
        return None


def get() -> dict:
    """Full build description (admin detail)."""
    s = _stamp()
    return {"version": __version__, "sha": s["sha"], "built_at": s["built_at"] or None,
            "started_at": _STARTED_AT, "api_version": API_VERSION, "migration_step": migration_step()}


def short() -> dict:
    """The only form anonymous callers, kids and devices see: no process or schema internals."""
    s = _stamp()
    return {"sha": s["sha"], "built_at": s["built_at"] or None}
