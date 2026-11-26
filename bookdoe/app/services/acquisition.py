"""The acquisition feature flag: indexers, download clients, wanted search, queue and their jobs.

New installs start with it off; an upgrade that already has an indexer or download client keeps it on
(migration step 48). ``BOOKDOE_ACQUISITION_ENABLED`` overrides the stored setting either way.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

from .. import db

SETTING_KEY = "acquisition_enabled"
ENV_NAME = "BOOKDOE_ACQUISITION_ENABLED"
# Scheduler kinds that only make sense with acquisition: they search, poll download clients or watch authors.
ACQUISITION_JOB_KINDS = ("download-poll", "wanted-search", "upgrade-search", "author-discovery")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def acquisition_enabled() -> bool:
    """True when acquisition is on: the env var wins when set to a recognised value, else the stored setting."""
    raw = (os.getenv(ENV_NAME) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return bool(db.get_setting(SETTING_KEY, False))


class AcquisitionOff(ValueError):
    """An acquisition entry point was called while the flag is off. A ValueError so routes that map
    ValueError to 400 stay well-formed; the route dependency already answers 404 first."""


def ensure_enabled() -> None:
    """Service-level gate for entry points reached other than through a gated route (jobs, background threads)."""
    if not acquisition_enabled():
        raise AcquisitionOff("Acquisition is turned off")


def require_acquisition() -> None:
    """Route dependency: acquisition routes answer 404 (as if absent) while the flag is off."""
    if not acquisition_enabled():
        raise HTTPException(404, "Not found")
