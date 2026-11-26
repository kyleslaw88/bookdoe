"""Tests for the managed-library-build dry-run endpoint (POST /api/v1/jobs/managed-library-build/dry-run).

Each test gets its own database from the autouse ``fresh_db`` fixture in conftest.py and its
own TestClient; the naming templates are set explicitly rather than assumed.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings

URL = "/api/v1/jobs/managed-library-build/dry-run"


@pytest.fixture
def client():
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def templates():
    """Pin the naming templates so expected destinations are explicit."""
    with db.tx() as conn:
        conn.execute(
            "UPDATE naming_templates SET folder_template='{Author}/{Title}', file_template='{Title}.{Ext}' "
            "WHERE media_kind='ebook'"
        )


def _seed_root(path: Path, kind: str = "ebook") -> None:
    """The managed root, plus the adopted (read-only) root next to it that the build copies FROM (plan v2 decides by
    the root a file lives in, not by the media_files.adopted flag)."""
    path.mkdir(parents=True, exist_ok=True)
    adopted = path.parent / "adopted"
    adopted.mkdir(parents=True, exist_ok=True)
    t = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
            " VALUES (?,?,?,?, 'managed',1,1,?,?)",
            (db.uid(), f"managed-{kind}", str(path), kind, t, t),
        )
        conn.execute(
            "INSERT OR IGNORE INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
            " VALUES (?,?,?,?, 'adopt',1,1,?,?)",
            (db.uid(), f"adopted-{kind}", str(adopted), kind, t, t),
        )


def _seed_work(title: str, author: str, year: str = "2024") -> str:
    """Create a work with an author and an owned ebook asset; returns the asset id."""
    wid, cid, aid, t = db.uid(), db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,first_publish_date,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (wid, title, title, year, "resolved", t, t),
        )
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, author, author, t, t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',0)", (wid, cid))
        c.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (aid, wid, "ebook", "owned", 0, t, t),
        )
    return aid


def _add_file(asset_id: str, path: Path, size: int, *, adopted: int = 1, managed: int = 0) -> Path:
    """Write a real file and a media_files row pointing at it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    with db.tx() as c:
        c.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at,verified_at)"
            " VALUES (?,?,?,?, '{}',?,?,0,?,?)",
            (db.uid(), asset_id, str(path), size, adopted, managed, db.now(), db.now()),
        )
    return path


def _snapshot(*roots: Path) -> dict[str, int]:
    return {str(p): p.stat().st_size for r in roots for p in r.rglob("*") if p.is_file()}


def test_plan_lists_correct_source_paths_destinations_and_sizes(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    a1 = _seed_work("Dune", "Frank Herbert")
    a2 = _seed_work("Emma", "Jane Austen")
    s1 = _add_file(a1, src / "inbox" / "dune.epub", 2048)
    s2 = _add_file(a2, src / "inbox" / "emma.epub", 4096)

    data = client.post(URL).json()

    assert data["transfer_mode"] == "copy"
    by_src = {e["source_path"]: e for e in data["entries"]}
    assert set(by_src) == {str(s1), str(s2)}
    assert by_src[str(s1)]["destination_path"] == str(managed / "Frank Herbert" / "Dune" / "Dune.epub")
    assert by_src[str(s1)]["size_bytes"] == 2048
    assert by_src[str(s2)]["destination_path"] == str(managed / "Jane Austen" / "Emma" / "Emma.epub")
    assert by_src[str(s2)]["size_bytes"] == 4096
    assert all(e["skip_reason"] is None and e["collision"] is False for e in data["entries"])
    assert data["totals"]["file_count"] == 2
    assert data["totals"]["total_bytes"] == 6144
    assert data["totals"]["total_gb"] == round(6144 / 1024**3, 2)


def test_dry_run_writes_no_files(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    _add_file(_seed_work("Dune", "Frank Herbert"), src / "dune.epub", 512)
    before = _snapshot(src, managed)
    src.chmod(0o555)  # read-only source: any write attempt there would fail the request
    try:
        r = client.post(URL)
    finally:
        src.chmod(0o755)
    assert r.status_code == 200, r.text
    assert _snapshot(src, managed) == before
    assert not list(managed.iterdir()), "dry run must not create destination folders"


def test_file_already_in_a_managed_root_is_not_planned(client, tmp_path):
    managed = tmp_path / "managed"
    _seed_root(managed)
    aid = _seed_work("Dune", "Frank Herbert")
    _add_file(aid, managed / "Frank Herbert" / "Dune" / "Dune.epub", 100)

    data = client.post(URL).json()

    assert data["entries"] == []
    assert data["totals"]["file_count"] == 0
    assert data["totals"]["total_bytes"] == 0


def test_two_files_with_same_destination_collide(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    # Same author, title and extension: the template yields one destination for both.
    a1 = _seed_work("Dune", "Frank Herbert", "1965")
    a2 = _seed_work("Dune", "Frank Herbert", "1984")
    _add_file(a1, src / "a" / "dune.epub", 10)
    _add_file(a2, src / "b" / "dune.epub", 20)

    data = client.post(URL).json()

    # v2: never a skip, never an overwrite: the second file gets the next free name.
    assert len(data["entries"]) == 2
    folder = managed / "Frank Herbert" / "Dune"
    assert {e["destination_path"] for e in data["entries"]} == {str(folder / "Dune.epub"), str(folder / "Dune (2).epub")}
    assert sorted(e["collision"] for e in data["entries"]) == [False, True]
    assert all(e["skip_reason"] is None for e in data["entries"])
    assert data["totals"]["collision_count"] == 1
    assert data["totals"]["file_count"] == 2


def test_only_files_in_adopted_roots_are_planned(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    aid = _seed_work("Dune", "Frank Herbert")
    kept = _add_file(aid, src / "dune.epub", 10)
    _add_file(aid, tmp_path / "outside-every-root" / "stray.epub", 10)

    data = client.post(URL).json()

    assert [e["source_path"] for e in data["entries"]] == [str(kept)]


def test_asset_without_files_is_not_part_of_the_build(client, tmp_path):
    _seed_root(tmp_path / "managed")
    _seed_work("Dune", "Frank Herbert")
    data = client.post(URL).json()
    assert data["entries"] == [] and data["book_count"] == 0


def test_empty_library_returns_zero_totals(client, tmp_path):
    _seed_root(tmp_path / "managed")
    data = client.post(URL).json()
    assert data["entries"] == []
    assert data["totals"]["file_count"] == 0
    assert data["totals"]["total_bytes"] == 0


def test_no_managed_roots_returns_409(client):
    assert client.post(URL).status_code == 409


def test_job_is_seeded_manual_only():
    job = db.one("SELECT * FROM jobs WHERE kind='managed-library-build'")
    assert job["schedule_seconds"] is None
    assert job["next_run_at"] is None


def test_scheduler_never_runs_the_manual_only_job():
    from app.services import scheduler

    for _ in range(3):
        ran = scheduler.run_due_jobs()
        assert all(r["job_id"] != db.one("SELECT id FROM jobs WHERE kind='managed-library-build'")["id"] for r in ran)
    assert db.one("SELECT COUNT(*) AS n FROM job_runs WHERE kind='managed-library-build'")["n"] == 0


def test_generic_run_endpoint_rejects_the_job(client):
    job = db.one("SELECT id FROM jobs WHERE kind='managed-library-build'")
    assert client.post(f"/api/v1/jobs/{job['id']}/run", json={}).status_code == 400
    assert db.one("SELECT COUNT(*) AS n FROM job_runs WHERE kind='managed-library-build'")["n"] == 0


def test_dry_run_requires_authentication(client, monkeypatch):
    replaced = dataclasses.replace(settings, auth_mode="oidc")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    assert client.post(URL).status_code in (401, 403)


def test_existing_file_at_destination_is_a_collision(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    _add_file(_seed_work("Dune", "Frank Herbert"), src / "dune.epub", 10)
    existing = managed / "Frank Herbert" / "Dune" / "Dune.epub"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"other")

    data = client.post(URL).json()

    # v2: reported, and settled at run time without overwriting (identical content is reused, anything else gets
    # the next free name); see test_managed_build_engine.py.
    (entry,) = data["entries"]
    assert entry["collision"] is True
    assert entry["skip_reason"] is None
    assert data["totals"]["collision_count"] == 1
    assert data["totals"]["file_count"] == 1
    assert existing.read_bytes() == b"other"


def test_zero_size_file_keeps_its_extension(client, tmp_path):
    src, managed = tmp_path / "adopted", tmp_path / "managed"
    _seed_root(managed)
    _add_file(_seed_work("Dune", "Frank Herbert"), src / "dune.epub", 0)
    (entry,) = client.post(URL).json()["entries"]
    assert entry["destination_path"] == str(managed / "Frank Herbert" / "Dune" / "Dune.epub")


def test_job_cannot_be_given_a_schedule(client):
    job = db.one("SELECT id FROM jobs WHERE kind='managed-library-build'")
    r = client.put(f"/api/v1/jobs/{job['id']}", json={"schedule_seconds": 60})
    assert r.status_code == 400
    assert db.one("SELECT schedule_seconds FROM jobs WHERE id=?", (job["id"],))["schedule_seconds"] is None


def test_settings_jobs_links_to_the_one_build_panel():
    static = Path(main_module.__file__).parent / "static"
    js = static.joinpath("app.js").read_text()
    body = js[js.index("async function settingsJobs"):]
    body = body[: body.index("\nasync function ") if "\nasync function " in body else len(body)]
    assert 'id="mb-open-from-jobs"' in body and "renderDryRunResult" not in js
    panel = static.joinpath("managedbuild.js").read_text()
    assert "/api/v1/managed-build/plans" in panel and "export async function buildPanel" in panel
