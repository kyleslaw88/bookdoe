"""Compatibility with installs made while the product was still called alexandria.

Everything that still needs the old name lives here and nowhere else; docs/UPGRADING.md
describes the migration. Nothing in this module moves user data.

* ``BOOKDOE_X`` falls back to ``ALEXANDRIA_X`` (with a one-line deprecation warning).
* A default database file that already exists under the old name keeps being used.
* A per-root trash folder written under the old name is still read, restored and purged.
* The default OIDC group names ``alexandria-admin|adult|kid`` still map accounts when the
  group variables are left at their defaults.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import MutableMapping

log = logging.getLogger("bookdoe.legacy")

OLD_ENV_PREFIX = "ALEXANDRIA_"
NEW_ENV_PREFIX = "BOOKDOE_"
OLD_DB_NAME = "alexandria.db"
NEW_DB_NAME = "bookdoe.db"
OLD_TRASH_DIR_NAME = ".alexandria-trash"
OLD_GROUP_DEFAULTS = {"bookdoe-admin": "alexandria-admin", "bookdoe-adult": "alexandria-adult", "bookdoe-kid": "alexandria-kid"}

_warned: set[str] = set()


def apply_legacy_env(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy every ``ALEXANDRIA_X`` into ``BOOKDOE_X`` unless ``BOOKDOE_X`` is already set.
    Returns the new names that were filled in. Safe to call repeatedly."""
    if env is None:
        env = os.environ
    filled = []
    for key in [k for k in env if k.startswith(OLD_ENV_PREFIX)]:
        new = NEW_ENV_PREFIX + key[len(OLD_ENV_PREFIX):]
        if new in env:
            continue
        env[new] = env[key]
        filled.append(new)
        if key not in _warned:
            _warned.add(key)
            log.warning("DEPRECATED: environment variable %s is read as %s; rename it (see docs/UPGRADING.md)", key, new)
    return filled


def default_db_path(config_dir: str = "/config") -> Path:
    """The new default name, unless an install already has a database under the old one."""
    base = Path(config_dir)
    old = base / OLD_DB_NAME
    if old.exists() and not (base / NEW_DB_NAME).exists():
        log.warning("DEPRECATED: using the existing database %s; see docs/UPGRADING.md to rename it", old)
        return old
    return base / NEW_DB_NAME


def group_names(configured: str) -> set[str]:
    """The configured OIDC group name plus its old default when it is still the new default."""
    old = OLD_GROUP_DEFAULTS.get(configured)
    return {configured, old} if old else {configured}
