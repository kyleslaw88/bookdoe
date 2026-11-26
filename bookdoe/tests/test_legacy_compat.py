"""Rename compatibility (#598): an install configured with the old ALEXANDRIA_* names, or the
old default database / trash folder / OIDC group names, keeps working next to the new BOOKDOE_* names.

The start-up tests run a fresh interpreter with ONLY the old (or ONLY the new) variables, the way a
container started from the new image sees an old or a new env file.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import legacy
from app.services import paths as path_guard

APP_DIR = Path(__file__).resolve().parents[1]

PROBE = """
import json
from fastapi.testclient import TestClient
from app.config import settings
from app.main import app
with TestClient(app) as c:
    ready = c.get("/readyz").status_code
print(json.dumps({"ready": ready, "public_url": settings.public_url, "auth_mode": settings.auth_mode,
                  "app_name": settings.app_name, "db": str(settings.db_path), "secret": bool(settings.secret_key)}))
"""


def _run_with(tmp_path: Path, prefix: str) -> tuple[dict, str]:
    """Start the app in a fresh interpreter whose environment holds only ``<prefix>*`` settings."""
    # Keep the runner's loader settings (LD_LIBRARY_PATH etc.) so sys.executable starts on any Python
    # layout; drop only the app's own variables of either name so the probe sees just ``prefix``.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BOOKDOE_", legacy.OLD_ENV_PREFIX))}
    env.update({"PYTHONPATH": str(APP_DIR), "HOME": str(tmp_path)})
    values = {
        "DATA_DIR": str(tmp_path / "data"),
        "DB_PATH": str(tmp_path / "x.db"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "COVER_DIR": str(tmp_path / "covers"),
        "AUTH_MODE": "none",
        "SCHEDULER_ENABLED": "0",
        "SECRET_KEY": "pytest-secret-key-that-is-long-and-stable",
        "PUBLIC_URL": "https://books.example.com",
        "APP_NAME": "Acme Reads",
    }
    for k, v in values.items():
        env[prefix + k] = v
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr


def test_container_starts_with_only_new_bookdoe_env(tmp_path):
    out, err = _run_with(tmp_path, "BOOKDOE_")
    assert out["ready"] == 200 and out["public_url"] == "https://books.example.com"
    assert out["auth_mode"] == "none" and out["app_name"] == "Acme Reads" and out["secret"]
    assert "DEPRECATED" not in err


def test_container_starts_with_only_old_env_and_warns_once_per_variable(tmp_path):
    out, err = _run_with(tmp_path, legacy.OLD_ENV_PREFIX)
    assert out["ready"] == 200 and out["public_url"] == "https://books.example.com"
    assert out["auth_mode"] == "none" and out["app_name"] == "Acme Reads" and out["secret"]
    assert out["db"].endswith("x.db")
    warned = [line for line in err.splitlines() if "DEPRECATED: environment variable" in line]
    assert len(warned) == 9 and len(set(warned)) == 9, warned
    assert all("is read as BOOKDOE_" in line for line in warned)


def test_new_name_wins_over_old_when_both_are_set():
    env = {"ALEXANDRIA_PORT": "1", "BOOKDOE_PORT": "2", "ALEXANDRIA_ONLY_OLD": "x"}
    assert legacy.apply_legacy_env(env) == ["BOOKDOE_ONLY_OLD"]
    assert env["BOOKDOE_PORT"] == "2" and env["BOOKDOE_ONLY_OLD"] == "x"


def test_env_file_written_before_the_rename_is_still_read(tmp_path):
    from app import secrets_provider
    f = tmp_path / "setup.env"
    f.write_text("ALEXANDRIA_PUBLIC_URL=https://old.example.com\nALEXANDRIA_HARDCOVER_API_KEY=abc\n")
    env: dict[str, str] = {"BOOKDOE_HARDCOVER_API_KEY": "new"}
    secrets_provider.load_env_file(f, environ=env)
    assert env["BOOKDOE_PUBLIC_URL"] == "https://old.example.com"
    assert env["BOOKDOE_HARDCOVER_API_KEY"] == "new"


def test_deprecation_warning_is_a_single_line(caplog):
    legacy._warned.discard("ALEXANDRIA_WARN_PROBE")
    with caplog.at_level(logging.WARNING, logger="bookdoe.legacy"):
        legacy.apply_legacy_env({"ALEXANDRIA_WARN_PROBE": "1"})
    assert len(caplog.records) == 1 and "\n" not in caplog.records[0].getMessage()


def test_default_db_name_is_new_for_a_fresh_install_and_old_for_an_existing_one(tmp_path):
    assert legacy.default_db_path(str(tmp_path)) == tmp_path / "bookdoe.db"
    (tmp_path / legacy.OLD_DB_NAME).write_bytes(b"")
    assert legacy.default_db_path(str(tmp_path)) == tmp_path / legacy.OLD_DB_NAME
    (tmp_path / "bookdoe.db").write_bytes(b"")
    assert legacy.default_db_path(str(tmp_path)) == tmp_path / "bookdoe.db"


def test_old_default_oidc_group_names_still_map_while_the_new_ones_are_the_default():
    from app import auth
    assert legacy.group_names("bookdoe-admin") == {"bookdoe-admin", "alexandria-admin"}
    assert legacy.group_names("custom-admins") == {"custom-admins"}
    assert auth.settings.oidc_group_admin == "bookdoe-admin"


def test_old_trash_folder_is_skipped_by_scans_and_found_for_restore(tmp_path):
    root = tmp_path / "lib"
    for d in (".bookdoe-trash/2026-11-18", legacy.OLD_TRASH_DIR_NAME + "/2026-11-01", "Author"):
        (root / d).mkdir(parents=True)
    (root / "Author/book.epub").write_bytes(b"x")
    (root / ".bookdoe-trash/2026-11-18/a.epub").write_bytes(b"x")
    (root / legacy.OLD_TRASH_DIR_NAME / "2026-11-01/b.epub").write_bytes(b"x")
    seen = sorted(p.name for p in path_guard.scan_safe_dir(root, root.resolve()))
    assert seen == ["book.epub"]
    old = root / legacy.OLD_TRASH_DIR_NAME / "2026-11-01/b.epub"
    new = root / ".bookdoe-trash/2026-11-18/a.epub"
    assert path_guard.trash_root_for(root, str(old)) == root / legacy.OLD_TRASH_DIR_NAME
    assert path_guard.trash_root_for(root, str(new)) == root / ".bookdoe-trash"
    assert path_guard.trash_root_for(root, "") == root / ".bookdoe-trash"
