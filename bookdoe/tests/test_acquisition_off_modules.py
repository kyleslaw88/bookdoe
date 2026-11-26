"""#494 stage A2: with the acquisition flag off, every acquisition module's entry point is unreachable.

Each test drives the real entry point (route, scheduler or service function) and asserts that
nothing downstream ran: no outbound call, no import, no job run.
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.integrations import downloaders, indexers
from app.main import app
from app.services import acquisition, downloads, releases, request_search, scheduler, upgrades, wanted_search
from app.services.acquisition import AcquisitionOff

client = TestClient(app)


@pytest.fixture(autouse=True)
def flag_off(monkeypatch):
    monkeypatch.setenv(acquisition.ENV_NAME, "0")


def _boom(*a, **k):
    raise AssertionError("reached past the acquisition gate")


def test_acquisition_off_blocks_release_search(monkeypatch):
    monkeypatch.setattr(indexers, "_search_prowlarr", _boom)
    monkeypatch.setattr(indexers, "_search_newznab", _boom)
    assert client.post("/api/v1/library/w1/search/ebook").status_code == 404
    with pytest.raises(AcquisitionOff):
        releases.search_releases("w1", "ebook")
    with pytest.raises(AcquisitionOff):
        indexers.search({"kind": "prowlarr"}, {})
    with pytest.raises(AcquisitionOff):
        indexers.test({"kind": "prowlarr"})
    with pytest.raises(AcquisitionOff):
        upgrades.search_job()


def test_acquisition_off_blocks_downloads_and_clients(monkeypatch):
    for name in ("_sab_add", "_sab_poll", "_sab_remove", "_sab_test"):
        monkeypatch.setattr(downloaders, name, _boom)
    client_row = {"kind": "sabnzbd", "name": "c"}
    with pytest.raises(AcquisitionOff):
        downloaders.add(client_row, {})
    with pytest.raises(AcquisitionOff):
        downloaders.poll(client_row)
    with pytest.raises(AcquisitionOff):
        downloaders.remove(client_row, "j")
    with pytest.raises(AcquisitionOff):
        downloaders.test(client_row)
    with pytest.raises(AcquisitionOff):
        downloads.grab_release("r1")
    with pytest.raises(AcquisitionOff):
        downloads.force_grab_release("r1")
    with pytest.raises(AcquisitionOff):
        downloads.poll_all()
    with pytest.raises(AcquisitionOff):
        downloads.cancel_download("d1")
    assert client.post("/api/v1/releases/r1/grab", json={}).status_code == 404


def test_acquisition_off_no_managed_import(monkeypatch):
    monkeypatch.setattr(downloads.importer, "import_download", _boom)
    with db.tx() as conn:
        conn.execute("DELETE FROM downloads WHERE id='dl-off'")
        conn.execute("INSERT INTO downloads(id,state,added_at,updated_at) VALUES ('dl-off','completed',0,0)")
    with pytest.raises(AcquisitionOff):
        downloads.import_completed("dl-off")
    assert client.post("/api/v1/downloads/dl-off/import", json={}).status_code == 404
    assert db.one("SELECT state FROM downloads WHERE id='dl-off'")["state"] == "completed"


def test_acquisition_off_blocks_wanted_and_request_search(monkeypatch):
    monkeypatch.setattr(wanted_search.releases, "search_releases", _boom)
    with pytest.raises(AcquisitionOff):
        wanted_search.run()
    assert request_search.run("nope") == []
    enqueued = []
    monkeypatch.setattr(request_search, "_enqueue", lambda *a: enqueued.append(a))
    request_search.trigger("req1")
    assert enqueued == []
    for method, path in (("get", "/api/v1/settings/acquisition"), ("put", "/api/v1/settings/acquisition"),
                         ("post", "/api/v1/wanted/search-now")):
        assert getattr(client, method)(path).status_code in (401, 404), path


def test_acquisition_off_scheduler_refuses_manual_runs(monkeypatch):
    monkeypatch.setattr(wanted_search, "run", _boom)
    db.ensure_default_jobs()
    for kind in acquisition.ACQUISITION_JOB_KINDS:
        job = db.one("SELECT id FROM jobs WHERE kind=?", (kind,))
        if not job:
            continue
        with pytest.raises(AcquisitionOff):
            scheduler.submit_job(job["id"], trigger="manual")
        with pytest.raises(AcquisitionOff):
            scheduler.run_job(job["id"])
    monkeypatch.setenv(acquisition.ENV_NAME, "1")
    # Flag on: the gate lets a run through (the body is stubbed, so nothing real executes).
    monkeypatch.setattr(scheduler, "_execute", lambda job: {})
    job = db.one("SELECT id FROM jobs WHERE kind='download-poll'")
    assert scheduler.run_job(job["id"])["status"] == "success"
