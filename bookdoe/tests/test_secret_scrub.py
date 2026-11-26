"""C1 (review 2026-10-10): indexer and download-client API keys must never reach
`last_error`, the release-search error list or a test() response.

httpx puts the full request URL in HTTPStatusError's message, and newznab/SABnzbd carry
the key in the query string, so a 401 from a misconfigured indexer used to persist the
live key where every full-access account (and any write-scope API key) could read it.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.crypto import encrypt_secret, scrub_secrets
from app.integrations import downloaders, indexers
from app.services import releases

KEY = "s3cr3tapikey0123456789"


def _raise_401(url: str):
    req = httpx.Request("GET", url)
    resp = httpx.Response(401, request=req)
    raise httpx.HTTPStatusError(f"Client error '401 Unauthorized' for url '{url}'", request=req, response=resp)


def _seed_indexer(kind: str = "newznab") -> str:
    iid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO indexers(id,name,kind,base_url,api_key_enc,categories_json,priority,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, "Leaky", kind, "http://indexer.local", encrypt_secret(KEY), "[7000]", 50, 1, t, t),
        )
    return iid


def _seed_client() -> str:
    cid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO download_clients(id,name,kind,base_url,api_key_enc,priority,enabled,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, "SAB", "sabnzbd", "http://sab.local", encrypt_secret(KEY), 50, 1, "{}", t, t),
        )
    return cid


def test_scrub_masks_query_parameters_and_known_values():
    msg = f"401 for url 'http://x/api?t=caps&apikey={KEY}&o=xml' password=hunter2 then {KEY} again"
    out = scrub_secrets(msg, KEY)
    assert KEY not in out and "hunter2" not in out
    assert "apikey=[redacted]" in out and "password=[redacted]" in out
    # Harmless text is left alone, and a None secret is ignored.
    assert scrub_secrets("Connection refused", None) == "Connection refused"


def test_indexer_test_route_stores_and_returns_a_scrubbed_error(monkeypatch):
    iid = _seed_indexer()
    monkeypatch.setattr(indexers.httpx, "get", lambda url, **kw: _raise_401(f"{url}?t=caps&o=xml&apikey={KEY}"))
    with TestClient(main_module.app) as client:
        r = client.post(f"/api/v1/indexers/{iid}/test", json={})
        assert r.status_code == 200 and r.json()["ok"] is False
        assert KEY not in r.text
        listing = client.get("/api/v1/indexers").json()["items"]
        health = client.get("/api/v1/system/health").text
    stored = db.one("SELECT last_error FROM indexers WHERE id=?", (iid,))["last_error"]
    assert stored and "401" in stored
    assert KEY not in stored
    assert KEY not in str(listing) and KEY not in health


def test_release_search_errors_are_scrubbed(monkeypatch):
    _seed_indexer()
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)", (wid, "Dune", "Dune", t, t))
    monkeypatch.setattr(indexers.httpx, "get", lambda url, **kw: _raise_401(f"{url}?t=book&apikey={KEY}&q=dune"))
    result = releases.search_releases(wid, "ebook")
    assert result["errors"], "the failing indexer must still be reported"
    assert all(KEY not in err["error"] for err in result["errors"])
    stored = db.one("SELECT error FROM release_searches WHERE id=?", (result["search_id"],))["error"]
    assert KEY not in (stored or "")


def test_download_client_test_route_scrubs_the_key(monkeypatch):
    cid = _seed_client()
    monkeypatch.setattr(downloaders.httpx, "get", lambda url, **kw: _raise_401(f"{url}?mode=version&apikey={KEY}&output=json"))
    with TestClient(main_module.app) as client:
        r = client.post(f"/api/v1/download-clients/{cid}/test", json={})
        assert r.json()["ok"] is False and KEY not in r.text
        assert KEY not in client.get("/api/v1/download-clients").text
    assert KEY not in (db.one("SELECT last_error FROM download_clients WHERE id=?", (cid,))["last_error"] or "")


@pytest.mark.parametrize("kind", ["newznab", "prowlarr"])
def test_indexer_test_still_reports_plain_failures(monkeypatch, kind):
    iid = _seed_indexer(kind)
    monkeypatch.setattr(indexers.httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("Connection refused")))

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(indexers.httpx, "Client", _Client)
    item = db.one("SELECT * FROM indexers WHERE id=?", (iid,))
    assert indexers.test(item) == {"ok": False, "message": "Connection refused"}
