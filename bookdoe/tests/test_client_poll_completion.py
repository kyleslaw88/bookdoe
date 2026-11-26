"""NZBGet and qBittorrent: a download must be followed from add to completion.

The remote client is a fake HTTP server (httpx MockTransport); the adapters, poll loop and
import are the real ones.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import db
from app.integrations import downloaders
from app.services import downloads

import helpers
from test_download_cancel import _client_row, fake_server  # noqa: F401  (fixture)
from test_import_idempotent import add_root


class Fake:
    def __init__(self, torrents=None, queue=None, history=None):
        self.torrents, self.queue, self.history = torrents or [], queue or [], history or []
        self.added = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, request.content.decode() if request.content else ""
        if path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/add":
            self.added.append(body)
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=self.torrents)
        if path == "/jsonrpc":
            method = json.loads(body)["method"]
            result = {"listgroups": self.queue, "history": self.history, "appendurl": 7}.get(method)
            return httpx.Response(200, json={"result": result})
        return httpx.Response(404)


def _release(work_id, protocol, title="Fake Book"):
    rid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO releases(id,work_id,asset_kind,indexer_guid,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,?,0,90,?)",
            (rid, work_id, "audiobook", "http://idx.test/guid/1", protocol, title, "http://idx.test/dl/1", db.now()),
        )
    return rid


def _grab(kind, protocol):
    from app.services import migration
    work_id = migration._find_or_create_stub({"title": "Fake Book", "author": "X", "identifiers": {}})
    asset_id = helpers.want(work_id, "audiobook")
    _client_row(kind)
    return downloads.grab_release(_release(work_id, protocol))["download_id"], asset_id


def test_nzbget_download_completes_from_history_and_imports(fake_server, tmp_path):
    src = tmp_path / "nzb" / "Fake.Book"
    src.mkdir(parents=True)
    (src / "book.m4b").write_bytes(b"audio" * 100)
    add_root(tmp_path / "lib", "managed")
    server = fake_server(Fake(queue=[{"NZBID": 7, "NZBName": "Fake Book", "RemainingSizeMB": 50, "FileSizeMB": 100, "DestDir": "/inter"}]))
    did, asset_id = _grab("nzbget", "usenet")
    assert db.one("SELECT client_download_id FROM downloads WHERE id=?", (did,))["client_download_id"] == "7"

    downloads.poll_all()  # still in the queue
    row = db.one("SELECT state,progress FROM downloads WHERE id=?", (did,))
    assert row["state"] == "downloading" and 0.4 < row["progress"] < 0.6

    server.queue = []
    server.history = [{"NZBID": 7, "Name": "Fake Book", "Status": "SUCCESS/ALL", "DestDir": str(src), "FinalDir": ""}]
    result = downloads.poll_all()
    assert result["completed"] == 1 and result["imported"] == 1
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "imported"
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_nzbget_failure_in_history_marks_the_download_failed_and_blocklists(fake_server):
    server = fake_server(Fake())
    did, asset_id = _grab("nzbget", "usenet")
    server.history = [{"NZBID": 7, "Name": "Fake Book", "Status": "FAILURE/HEALTH", "DestDir": ""}]
    downloads.poll_all()
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "failed"
    assert db.one("SELECT COUNT(*) n FROM blocklist")["n"] == 1
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "missing"


def test_nzbget_ignores_history_entries_that_are_neither_success_nor_failure(fake_server):
    server = fake_server(Fake())
    did, _ = _grab("nzbget", "usenet")
    server.history = [{"NZBID": 7, "Name": "Fake Book", "Status": "DELETED/MANUAL"}]
    downloads.poll_all()
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "downloading"


def test_qbittorrent_url_add_stores_no_id_and_poll_binds_the_hash_by_name(fake_server, tmp_path):
    src = tmp_path / "torrents" / "Fake Book"
    src.mkdir(parents=True)
    (src / "book.m4b").write_bytes(b"audio" * 100)
    add_root(tmp_path / "lib", "managed")
    torrent = {"hash": "h" * 40, "name": "Fake Book", "progress": 0.5, "state": "downloading", "content_path": str(src)}
    server = fake_server(Fake(torrents=[torrent]))
    did, asset_id = _grab("qbittorrent", "torrent")
    assert db.one("SELECT client_download_id FROM downloads WHERE id=?", (did,))["client_download_id"] is None

    downloads.poll_all()
    row = db.one("SELECT client_download_id,state FROM downloads WHERE id=?", (did,))
    assert row["client_download_id"] == "h" * 40 and row["state"] == "downloading"

    torrent.update(progress=1.0, state="uploading")
    result = downloads.poll_all()
    assert result["completed"] == 1 and result["imported"] == 1
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "imported"
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_qbittorrent_rows_that_stored_an_indexer_guid_are_rebound_by_name(fake_server):
    fake_server(Fake(torrents=[{"hash": "a" * 40, "name": "Fake Book", "progress": 0.2, "state": "downloading", "content_path": "/x"}]))
    did, _ = _grab("qbittorrent", "torrent")
    with db.tx() as conn:  # a row written by the old adapter
        conn.execute("UPDATE downloads SET client_download_id='http://idx.test/guid/1' WHERE id=?", (did,))
    downloads.poll_all()
    assert db.one("SELECT client_download_id FROM downloads WHERE id=?", (did,))["client_download_id"] == "a" * 40
