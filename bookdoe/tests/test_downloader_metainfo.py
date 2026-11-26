"""Regression tests for handing torrents to a network-isolated client.

Found during the 2026-09-24 Unraid deployment at Gate 7. torrent-add was passing the
indexer URL to Transmission, which runs inside an OpenVPN container. Transmission's
internal fetcher hung on the Prowlarr URL -- and because the RPC call blocks until
that fetch resolves, the symptom was a client-side timeout after 120s rather than any
usable error. bookdoe itself fetched the same URL in about one second.
"""
import base64

import httpx
import pytest

from app.integrations import downloaders

TORRENT = b"d8:announce30:https://tracker.example/announce4:infod4:name4:booke"
_REQ = httpx.Request("GET", "http://prowlarr.local/5/download")


def _resp(content: bytes) -> httpx.Response:
    # raise_for_status() requires a bound request.
    return httpx.Response(200, content=content, request=_REQ)


def _client():
    return {"kind": "transmission", "base_url": "http://tr.local:9091", "username_enc": None, "password_enc": None}


def test_a_real_torrent_is_sent_as_metainfo(monkeypatch):
    monkeypatch.setattr(downloaders.httpx, "get", lambda *a, **k: _resp(TORRENT))
    captured = {}

    def fake_request(c, method, arguments=None):
        captured["method"], captured["args"] = method, arguments
        return {"torrent-added": {"hashString": "abc123"}}

    monkeypatch.setattr(downloaders, "_transmission_request", fake_request)
    out = downloaders._transmission_add(_client(), {"download_url": "http://prowlarr.local/5/download?apikey=x"})

    assert out["id"] == "abc123"
    assert "metainfo" in captured["args"], "the client must not be asked to fetch the URL itself"
    assert "filename" not in captured["args"]
    assert base64.b64decode(captured["args"]["metainfo"]) == TORRENT


def test_magnet_links_are_passed_through_untouched(monkeypatch):
    called = {"get": False}

    def boom(*a, **k):
        called["get"] = True
        raise AssertionError("must not fetch a magnet link")

    monkeypatch.setattr(downloaders.httpx, "get", boom)
    captured = {}
    monkeypatch.setattr(downloaders, "_transmission_request", lambda c, m, a=None: captured.update(args=a) or {"torrent-added": {"hashString": "m"}})
    downloaders._transmission_add(_client(), {"download_url": "magnet:?xt=urn:btih:deadbeef"})
    assert captured["args"]["filename"].startswith("magnet:")
    assert called["get"] is False


def test_non_torrent_response_falls_back_to_the_url(monkeypatch):
    """An HTML error page must not be forwarded as if it were a torrent."""
    monkeypatch.setattr(downloaders.httpx, "get", lambda *a, **k: _resp(b"<html>rate limited</html>"))
    captured = {}
    monkeypatch.setattr(downloaders, "_transmission_request", lambda c, m, a=None: captured.update(args=a) or {"torrent-added": {}})
    downloaders._transmission_add(_client(), {"download_url": "http://prowlarr.local/5/download"})
    assert "filename" in captured["args"] and "metainfo" not in captured["args"]


def test_fetch_failure_falls_back_to_the_url(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(downloaders.httpx, "get", boom)
    captured = {}
    monkeypatch.setattr(downloaders, "_transmission_request", lambda c, m, a=None: captured.update(args=a) or {"torrent-added": {}})
    downloaders._transmission_add(_client(), {"download_url": "http://prowlarr.local/5/download"})
    assert "filename" in captured["args"], "a failed fetch must degrade to the old behaviour, not raise"


def test_metainfo_helper_rejects_non_bencode():
    import unittest.mock as mock
    with mock.patch.object(downloaders.httpx, "get", return_value=_resp(b"not a torrent")):
        assert downloaders._fetch_torrent_metainfo("http://x/y") is None


def test_metainfo_helper_accepts_bencode():
    import unittest.mock as mock
    with mock.patch.object(downloaders.httpx, "get", return_value=_resp(TORRENT)):
        assert downloaders._fetch_torrent_metainfo("http://x/y") == base64.b64encode(TORRENT).decode()
