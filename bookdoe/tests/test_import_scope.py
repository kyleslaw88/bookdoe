"""Regression tests for import scope — the worst defect of the 2026-09-24 deployment.

Gate 7 imported ONE 889 MB audiobook and bookdoe began copying 826 files /
71 GB into the library. Transmission's poll reported `downloadDir`, which is the
client's *shared* completed-downloads directory, so `discover_download_files()`
rglob'd 447 unrelated completed downloads. Only the read-only bind mounts on the
real libraries kept the damage inside the (new, empty) managed root.

Two independent defences are tested here:
  1. each client must report the download's OWN path, not its shared base directory;
  2. an import whose payload dwarfs the advertised release size must be refused.
"""
from pathlib import Path

import pytest

from app import db
from app.integrations import downloaders
from app.services import importer


# ---------- defence 1: per-download paths ----------

def test_transmission_reports_the_torrents_own_path(monkeypatch):
    monkeypatch.setattr(downloaders, "_transmission_request", lambda c, m, a=None: {
        "torrents": [{"hashString": "h1", "name": "My Book.m4b", "percentDone": 1.0, "isFinished": True, "downloadDir": "/data/downloads/torrents/completed"}]
    })
    items = downloaders._transmission_poll({"base_url": "http://tr:9091"})
    assert items[0]["path"] == "/data/downloads/torrents/completed/My Book.m4b", (
        "must not report the shared downloadDir - that imports every other download"
    )


def test_transmission_path_is_not_the_bare_shared_directory(monkeypatch):
    shared = "/data/downloads/torrents/completed"
    monkeypatch.setattr(downloaders, "_transmission_request", lambda c, m, a=None: {
        "torrents": [
            {"hashString": "a", "name": "Book A", "percentDone": 1.0, "isFinished": True, "downloadDir": shared},
            {"hashString": "b", "name": "Book B", "percentDone": 1.0, "isFinished": True, "downloadDir": shared},
        ]
    })
    paths = [x["path"] for x in downloaders._transmission_poll({"base_url": "http://tr:9091"})]
    assert shared not in paths
    assert len(set(paths)) == 2, "two downloads must not resolve to the same directory"


def test_qbittorrent_falls_back_to_save_path_plus_name():
    assert downloaders._join_download_path("/downloads", "Thing") == "/downloads/Thing"
    assert downloaders._join_download_path(None, "Thing") is None
    assert downloaders._join_download_path("/downloads", None) == "/downloads"


def test_sabnzbd_poll_surfaces_completed_history_storage(monkeypatch):
    class R:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    calls = []

    def fake_get(url, params=None, timeout=None, follow_redirects=None):
        calls.append(params["mode"])
        if params["mode"] == "queue":
            return R({"queue": {"slots": []}})
        return R({"history": {"slots": [{"nzo_id": "n1", "name": "Some Book", "status": "Completed", "storage": "/data/downloads/sabnzbd/complete/Some Book"}]}})

    monkeypatch.setattr(downloaders.httpx, "get", fake_get)
    out = downloaders._sab_poll({"base_url": "http://sab:8080", "api_key_enc": None})
    assert "history" in calls, "a finished job leaves the queue; history is the only source of its path"
    done = [x for x in out if x["state"] == "completed"]
    assert done and done[0]["path"] == "/data/downloads/sabnzbd/complete/Some Book"


# ---------- defence 2: size sanity ----------

def _guard(monkeypatch, release_size, file_sizes, source="/data/downloads/torrents/completed"):
    dl = {"id": "d1", "release_id": "r1"}
    monkeypatch.setattr(importer.db, "one", lambda *a, **k: {"size_bytes": release_size} if release_size else None)
    importer._guard_against_oversized_import(dl, source, [_FakeBig(n) for n in file_sizes])


class _FakeBig:
    """A Path-like whose stat() reports a chosen size."""

    def __init__(self, size: int):
        self._size = size

    def stat(self):
        class S:
            st_size = 0
        s = S()
        s.st_size = self._size
        return s


def test_oversized_payload_is_refused(monkeypatch):
    """The exact Gate 7 failure: a 0.9 GB release resolving to tens of GB of files."""
    with pytest.raises(ValueError, match="shared download directory"):
        _guard(monkeypatch, 900 * 1024 ** 2, [20 * 1024 ** 3] * 6)


def test_correctly_sized_payload_is_allowed(monkeypatch):
    _guard(monkeypatch, 900 * 1024 ** 2, [889 * 1024 ** 2])


def test_multipart_slightly_over_is_allowed(monkeypatch):
    """Multipart sets can exceed the advertised size a little; that must not trip."""
    _guard(monkeypatch, 1024 ** 3, [int(1.4 * 1024 ** 3)])


def test_guard_is_skipped_when_release_size_is_unknown(monkeypatch):
    _guard(monkeypatch, None, [99 * 1024 ** 3])
