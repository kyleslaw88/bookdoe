"""Automatic wanted search: one grab per asset, blocklisting of failed releases, tunable threshold.

Only the remote services are faked (release search, download client); the scheduler,
grab, poll and import code, and the database, are the real ones.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.integrations import downloaders
from app.services import downloads, migration, releases, scheduler

import helpers
from test_download_cancel import _client_row

client = TestClient(main_module.app)


def _release(work_id, guid="guid-1", title="Good Audiobook", score=90):
    rid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO releases(id,work_id,asset_kind,indexer_guid,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
            (rid, work_id, "audiobook", guid, "usenet", title, "http://idx.test/nzb/1", score, db.now()),
        )
    return rid


@pytest.fixture
def wanted(monkeypatch):
    """A wanted audiobook, a usenet client, and fakes for search and the client add."""
    work_id = migration._find_or_create_stub({"title": "Hunted Book", "author": "A Writer", "identifiers": {}})
    asset_id = helpers.want(work_id, "audiobook")
    _client_row("sabnzbd")
    state = {"rid": _release(work_id), "score": 90, "searches": 0, "adds": []}

    def fake_search(wid, kind, automatic=False):
        state["searches"] += 1
        return {"items": [{"id": state["rid"], "rejected": 0, "score": state["score"], "title": "Hunted Book by A Writer [ENG / M4B]"}]}

    monkeypatch.setattr(scheduler.releases, "search_releases", fake_search)
    monkeypatch.setattr(downloaders, "add", lambda c, rel: state["adds"].append(rel["id"]) or {"ok": True, "id": f"job-{len(state['adds'])}"})
    state.update(work_id=work_id, asset_id=asset_id)
    return state


def _asset_status(asset_id):
    return db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"]


def test_two_consecutive_searches_grab_exactly_once(wanted):
    first = scheduler._wanted_search()
    second = scheduler._wanted_search()
    assert (first["grabbed"], second["grabbed"]) == (1, 0)
    assert len(wanted["adds"]) == 1
    assert wanted["searches"] == 1  # the second run did not even search: the asset is in flight
    assert db.one("SELECT COUNT(*) n FROM downloads WHERE asset_id=?", (wanted["asset_id"],))["n"] == 1
    assert _asset_status(wanted["asset_id"]) == "downloading"


def test_a_downloading_asset_leaves_the_wanted_list(wanted):
    assert wanted["work_id"] in json.dumps(client.get("/api/v1/wanted").json())
    scheduler._wanted_search()
    assert wanted["work_id"] not in json.dumps(client.get("/api/v1/wanted").json())
    assert client.get("/api/v1/dashboard").json()["counts"]["wanted_audiobooks"] == 0


def test_search_skips_an_asset_with_an_active_download_even_if_its_status_is_missing(wanted):
    """Belt and braces: an older build, or an intent toggle, can leave status='missing'."""
    scheduler._wanted_search()
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='missing' WHERE id=?", (wanted["asset_id"],))
    assert scheduler._wanted_search()["searched"] == 0
    assert len(wanted["adds"]) == 1


def test_cancelling_the_download_makes_the_asset_wanted_again(wanted, monkeypatch):
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    monkeypatch.setattr(downloaders, "remove", lambda *a, **k: None)
    downloads.cancel_download(did)
    assert _asset_status(wanted["asset_id"]) == "missing"


def test_failed_import_blocklists_the_release_and_frees_the_asset(wanted):
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state='completed',download_path='/nowhere' WHERE id=?", (did,))
    with pytest.raises(ValueError):
        downloads.import_completed(did)
    row = db.one("SELECT * FROM blocklist")
    assert (row["indexer_guid"], row["release_title"], row["work_id"]) == ("guid-1", "Good Audiobook", wanted["work_id"])
    assert 29 * 86400 < row["expires_at"] - db.now() <= 30 * 86400
    assert db.one("SELECT 1 FROM history WHERE event_type='release.blocklisted'")
    assert _asset_status(wanted["asset_id"]) == "missing"
    # The real scorer now refuses the same release (by guid, and by title from another indexer).
    for rel in ({"guid": "guid-1", "title": "other"}, {"guid": "other", "title": "Good Audiobook"}):
        _, _, rejected, _ = releases.score_release(wanted["work_id"], "audiobook", {**rel, "download_url": "x", "size": 1}, None)
        assert "Release is blocklisted" in rejected


def test_a_blocklisted_release_is_not_regrabbed_but_a_different_one_is(wanted):
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state='completed',download_path='/nowhere' WHERE id=?", (did,))
    with pytest.raises(ValueError):
        downloads.import_completed(did)
    # Search results now mark the failed release rejected (the real scorer does this) ...
    with db.tx() as conn:
        conn.execute("UPDATE releases SET rejected=1 WHERE id=?", (wanted["rid"],))
    wanted["rid"] = _release(wanted["work_id"], guid="guid-2", title="Better Audiobook")
    result = scheduler._wanted_search()
    assert result["grabbed"] == 1 and len(wanted["adds"]) == 2
    assert wanted["adds"][1] == wanted["rid"]


def test_a_client_reported_failure_blocklists_and_frees_the_asset(wanted, monkeypatch):
    scheduler._wanted_search()
    monkeypatch.setattr(downloaders, "poll", lambda c: [{"id": "job-1", "name": "Good Audiobook", "progress": 0.3, "state": "failed", "path": None}])
    downloads.poll_all()
    assert db.one("SELECT state FROM downloads")["state"] == "failed"
    assert db.one("SELECT indexer_guid FROM blocklist")["indexer_guid"] == "guid-1"
    assert _asset_status(wanted["asset_id"]) == "missing"


def test_successful_import_makes_the_asset_owned(wanted, tmp_path):
    from test_import_idempotent import prepare_import
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    prepare_import(tmp_path, did, ["a.m4b"])
    downloads.import_completed(did)
    assert _asset_status(wanted["asset_id"]) == "owned"
    assert db.one("SELECT COUNT(*) n FROM blocklist")["n"] == 0


# ---------- threshold setting ----------
def test_threshold_defaults_to_80_and_is_respected(wanted):
    assert scheduler.auto_grab_min_score() == 80
    wanted["score"] = 70
    assert scheduler._wanted_search()["grabbed"] == 0
    # Lowering the floor (through the settings API) clears the "not found" cooldown so the asset is retried.
    assert client.put("/api/v1/settings/acquisition", json={"auto_grab_min_score": 60}).status_code == 200
    assert scheduler._wanted_search()["grabbed"] == 1


def test_a_higher_threshold_blocks_a_grab(wanted):
    db.set_setting("acquisition.auto_grab_min_score", 95)
    assert scheduler._wanted_search()["grabbed"] == 0 and wanted["adds"] == []


def test_threshold_is_readable_and_writable_over_http():
    assert client.get("/api/v1/settings/acquisition").json()["auto_grab_min_score"] == 80
    r = client.put("/api/v1/settings/acquisition", json={"auto_grab_min_score": 65})
    assert r.status_code == 200 and r.json()["auto_grab_min_score"] == 65
    assert scheduler.auto_grab_min_score() == 65
    assert client.put("/api/v1/settings/acquisition", json={"auto_grab_min_score": -1}).status_code == 422
    assert client.put("/api/v1/settings/acquisition", json={"auto_grab_min_score": 1, "x": 1}).status_code == 422


# ---------- B4: concurrent imports ----------
def test_a_second_import_of_the_same_download_is_refused(wanted, tmp_path, monkeypatch):
    from test_import_idempotent import prepare_import
    from app.services import importer
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    prepare_import(tmp_path, did, ["a.m4b"])
    outcomes = {}
    real_import = importer.import_download

    def import_while_another_arrives(download_id, *, mode=None):
        # The first import is mid-flight (state 'importing') when the second one arrives.
        try:
            downloads.import_completed(download_id)
        except downloads.ImportBusy as exc:
            outcomes["second"] = str(exc)
        return real_import(download_id, mode=mode)

    monkeypatch.setattr(importer, "import_download", import_while_another_arrives)
    downloads.import_completed(did)
    assert "already being imported" in outcomes["second"]
    d = db.one("SELECT state FROM downloads WHERE id=?", (did,))
    assert d["state"] == "imported"
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 1
    # The refused attempt must not have recorded a failure or a blocklist entry for the winner.
    assert db.one("SELECT COUNT(*) n FROM blocklist")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='download.import_failed'")["n"] == 0


def test_auto_import_treats_a_lost_claim_as_not_a_failure(wanted, monkeypatch):
    def busy(download_id, *a, **k):
        raise downloads.ImportBusy("busy")
    monkeypatch.setattr(downloads, "import_completed", busy)
    assert downloads._auto_import("x", "label") == 0
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='download.import_failed'")["n"] == 0


@pytest.mark.parametrize("state", ["importing", "imported"])
def test_poll_does_not_overwrite_an_importing_or_imported_download(wanted, state, monkeypatch):
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state=? WHERE id=?", (state, did))
    # The state filter on the poll's SELECT is not the only guard: the UPDATE itself must hold
    # when the row changes between the SELECT and the UPDATE.
    real_all = db.all

    def all_then_import_starts(sql, params=()):
        rows = real_all(sql, params)
        if "FROM downloads WHERE client_id" in sql:
            for r in rows:
                r["state"] = "downloading"  # the snapshot the poll took before the import began
            rows = rows or [dict(real_all("SELECT * FROM downloads")[0], state="downloading")]
        return rows

    monkeypatch.setattr(downloads.db, "all", all_then_import_starts)
    monkeypatch.setattr(downloaders, "poll", lambda c: [{"id": "job-1", "name": "Good Audiobook", "progress": 1.0, "state": "completed", "path": "/x"}])
    result = downloads.poll_all()
    assert result["updated"] == 0
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == state


def test_claim_is_atomic_when_the_state_changes_after_the_read(wanted, monkeypatch):
    scheduler._wanted_search()
    did = db.one("SELECT id FROM downloads")["id"]
    stale = db.one("SELECT * FROM downloads WHERE id=?", (did,))
    stale["state"] = "completed"  # what this caller saw before the other import claimed it
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state='importing' WHERE id=?", (did,))
    monkeypatch.setattr(downloads.db, "one", lambda sql, params=(): stale)
    with pytest.raises(downloads.ImportBusy):
        downloads.import_completed(did)
    monkeypatch.undo()
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "importing"
