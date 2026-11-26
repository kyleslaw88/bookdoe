"""Plan storage, retrieval and the confirmation gate for the managed library build (#229)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app import main as main_module
from app.config import settings

from test_managed_library_build_dry_run import (  # noqa: F401  (fixtures + helpers)
    URL, _add_file, _seed_root, _seed_work, client, templates,
)

BASE = "/api/v1/jobs/managed-library-build"


@pytest.fixture
def developer_mode():
    """Settings is a frozen dataclass; flip the gate the way conftest swaps paths."""
    object.__setattr__(settings, "developer_mode", True)
    yield
    object.__setattr__(settings, "developer_mode", False)


def _setup(tmp_path):
    db.set_setting("managed_build.run_enabled", True)       # the admin switch (default OFF) is tested in test_managed_build_safety.py
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    source = _add_file(_seed_work("Dune", "Frank Herbert"), src / "dune.epub", 128)
    return source, managed / "Frank Herbert" / "Dune" / "Dune.epub"


def test_dry_run_stores_plan_and_ui_endpoint_returns_it(client, tmp_path):
    source, dest = _setup(tmp_path)
    assert client.get(f"{BASE}/plan").status_code == 404

    posted = client.post(URL).json()
    stored = db.one("SELECT * FROM build_plans WHERE id=?", (posted["plan_id"],))
    assert stored is not None and stored["confirmed_at"] is None

    got = client.get(f"{BASE}/plan").json()
    assert got["plan_id"] == posted["plan_id"]
    assert got["confirmed"] is False
    assert got["entries"] == posted["entries"]
    assert got["entries"][0]["source_path"] == str(source)
    assert got["entries"][0]["destination_path"] == str(dest)
    assert got["totals"] == posted["totals"]
    assert not dest.exists()


def test_run_is_blocked_without_confirmation(client, tmp_path):
    _, dest = _setup(tmp_path)
    plan_id = client.post(URL).json()["plan_id"]

    r = client.post(f"{BASE}/run", json={"plan_id": plan_id})

    assert r.status_code == 409
    assert "confirm" in r.json()["detail"].lower()
    assert not dest.exists()
    assert db.one("SELECT executed_at FROM build_plans WHERE id=?", (plan_id,))["executed_at"] is None


def _finish(client, run_id):
    import time
    end = time.time() + 30
    while client.get(f"/api/v1/managed-build/runs/{run_id}").json()["status"] == "running" and time.time() < end:
        time.sleep(0.05)
    return client.get(f"/api/v1/managed-build/runs/{run_id}").json()


def test_confirmation_needs_the_typed_book_count_and_the_run_needs_no_developer_mode(client, tmp_path):
    """The old developer-mode-only inline copy loop is gone (#222/#223): a confirmed plan runs through the verified
    engine for everyone, but only after the admin typed the plan's number of books."""
    source, dest = _setup(tmp_path)
    plan_id = client.post(URL).json()["plan_id"]
    r = client.post(f"{BASE}/confirm", json={"plan_id": plan_id})
    assert r.status_code == 409 and "Type the number of books" in r.json()["detail"]
    assert client.post(f"{BASE}/confirm", json={"plan_id": plan_id, "book_count": 2}).status_code == 409
    assert client.post(f"{BASE}/confirm", json={"plan_id": plan_id, "book_count": 1}).json()["confirmed"] is True
    assert settings.developer_mode is False
    r = client.post(f"{BASE}/run", json={"plan_id": plan_id})
    assert r.status_code == 200, r.text
    assert _finish(client, r.json()["run_id"])["status"] == "completed"
    assert dest.read_bytes() == source.read_bytes()


def test_run_after_confirmation_copies_once(client, tmp_path, developer_mode):
    source, dest = _setup(tmp_path)
    plan_id = client.post(URL).json()["plan_id"]

    assert client.post(f"{BASE}/confirm", json={"plan_id": plan_id, "book_count": 1}).json()["confirmed"] is True
    r = client.post(f"{BASE}/run", json={"plan_id": plan_id})

    assert r.status_code == 200
    assert _finish(client, r.json()["run_id"])["counts"] == {"done": 1}
    assert dest.read_bytes() == source.read_bytes()
    assert source.exists()  # copy, never move
    assert client.post(f"{BASE}/run", json={"plan_id": plan_id}).status_code == 409


def test_new_dry_run_invalidates_earlier_confirmation(client, tmp_path, developer_mode):
    _, dest = _setup(tmp_path)
    old = client.post(URL).json()["plan_id"]
    client.post(f"{BASE}/confirm", json={"plan_id": old})
    new = client.post(URL).json()["plan_id"]

    assert client.post(f"{BASE}/run", json={"plan_id": old}).status_code == 409
    assert client.post(f"{BASE}/run", json={"plan_id": new}).status_code == 409
    assert not dest.exists()


def test_build_panel_gates_start_behind_the_typed_confirmation():
    js = (Path(main_module.__file__).parent / "static" / "managedbuild.js").read_text()
    assert 'id="mb-confirm-count"' in js and "/confirm`" in js and "book_count" in js
    assert 'id="mb-start"' in js and "/start`" in js
