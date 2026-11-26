"""Cancelling / removing queue items: DELETE /api/v1/downloads/{id}.

The download client is faked at the HTTP layer (httpx), so the real adapters in
integrations/downloaders.py build and send the requests; only the remote server is
simulated.
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.integrations import downloaders
from app.services import downloads, migration

import helpers
from test_inbox_ui import NODE, needs_node, live, run_ui  # noqa: F401  (live is a fixture)

client = TestClient(main_module.app)

_REAL_CLIENT = httpx.Client


class FakeServer:
    """Records every request the adapters send and answers like the real client would."""

    def __init__(self, torrents=None, fail=None, nzbget_queue_hit=True, on_request=None):
        self.on_request = on_request  # runs while the client call is in flight
        self.calls: list[tuple[str, str, dict]] = []
        self.torrents = torrents or []
        self.fail = fail
        self.nzbget_queue_hit = nzbget_queue_hit

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError(self.fail)
        if self.on_request:
            self.on_request()
        path = request.url.path
        body = request.content.decode() if request.content else ""
        params = dict(request.url.params)
        self.calls.append((request.method, path, {"params": params, "body": body}))
        if path == "/api":  # SABnzbd
            return httpx.Response(200, json={"status": True})
        if path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=self.torrents)
        if path == "/api/v2/torrents/delete":
            return httpx.Response(200, text="")
        if path == "/jsonrpc":  # NZBGet
            cmd = json.loads(body)["params"][0]
            return httpx.Response(200, json={"result": self.nzbget_queue_hit or cmd == "HistoryFinalDelete"})
        if path == "/transmission/rpc":
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        return httpx.Response(404)


@pytest.fixture
def fake_server(monkeypatch):
    def install(server: FakeServer) -> FakeServer:
        transport = httpx.MockTransport(server.handle)

        def get(url, **kw):
            with _REAL_CLIENT(transport=transport) as c:
                return c.get(url, **{k: v for k, v in kw.items() if k in {"params", "headers"}})

        def post(url, **kw):
            with _REAL_CLIENT(transport=transport) as c:
                return c.post(url, **{k: v for k, v in kw.items() if k in {"json", "data", "headers", "auth"}})

        def make_client(**kw):
            kw.pop("transport", None)
            return _REAL_CLIENT(transport=transport, **kw)

        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "Client", make_client)
        return server
    return install


def _client_row(kind: str) -> str:
    cid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO download_clients(id,name,kind,base_url,priority,enabled,created_at,updated_at) VALUES (?,?,?,?,50,1,?,?)",
            (cid, f"{kind}-1", kind, "http://dl.test:8080", t, t),
        )
    return cid


def _download(state="downloading", kind="sabnzbd", client_job=None, title="Some Book", wanted=True, error=None):
    client_job = client_job or ("42" if kind == "nzbget" else "job-1")
    work_id = migration._find_or_create_stub({"title": title, "author": "X", "identifiers": {}})
    asset_id = helpers.want(work_id, "audiobook") if wanted else helpers.own(work_id, "audiobook")
    cid = _client_row(kind)
    did, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO downloads(id,work_id,asset_id,client_id,client_download_id,state,protocol,source_title,error,progress,added_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,0.4,?,?)",
            (did, work_id, asset_id, cid, client_job, state, "usenet" if kind in {"sabnzbd", "nzbget"} else "torrent", title, error, t, t),
        )
    return did, work_id, asset_id


def _state(did):
    return db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"]


def _history(event="download.cancelled"):
    return db.all("SELECT * FROM history WHERE event_type=? ORDER BY created_at", (event,))


# ---------- every state ----------
@pytest.mark.parametrize("state", ["import_failed", "completed", "import_pending", "importing"])
def test_failed_or_stuck_item_is_cancelled_and_leaves_every_queue_view(state, fake_server):
    fake_server(FakeServer())
    did, _, _ = _download(state=state, error="epub is not an audiobook" if state == "import_failed" else None)
    assert [x["id"] for x in client.get("/api/v1/queue").json()["items"]] == [did]
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "cancelled"
    assert _state(did) == "cancelled"
    assert client.get("/api/v1/queue").json()["items"] == []
    dash = client.get("/api/v1/dashboard").json()
    assert dash["queue"] == []
    assert dash["counts"]["active_downloads"] == 0
    assert db.one("SELECT COUNT(*) n FROM downloads WHERE id=?", (did,))["n"] == 1  # row kept for History


@pytest.mark.parametrize("state", ["grabbed", "downloading"])
def test_active_item_is_cancelled(state, fake_server):
    fake_server(FakeServer())
    did, _, _ = _download(state=state)
    assert client.get("/api/v1/dashboard").json()["counts"]["active_downloads"] == 1
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    assert _state(did) == "cancelled"
    assert client.get("/api/v1/dashboard").json()["counts"]["active_downloads"] == 0


def test_imported_item_is_refused_and_untouched(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="imported")
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 400
    assert "already imported" in r.json()["detail"]
    assert _state(did) == "imported"
    assert server.calls == [] and _history() == []


def test_already_cancelled_item_is_refused(fake_server):
    fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 400


def test_state_change_during_the_client_call_wins_over_the_cancel(fake_server):
    holder = {}

    def importer_finishes():  # a concurrent import lands while the client is being called
        with db.tx() as conn:
            conn.execute("UPDATE downloads SET state='imported' WHERE id=?", (holder["did"],))

    fake_server(FakeServer(on_request=importer_finishes))
    did, _, _ = _download(state="downloading")
    holder["did"] = did
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 400 and "changed state" in r.json()["detail"]
    assert _state(did) == "imported"
    assert _history() == []


def test_unknown_id_is_404():
    assert client.delete("/api/v1/downloads/nope").status_code == 404


# ---------- client removal, one fake per kind ----------
def test_sabnzbd_job_is_removed_from_queue_and_history_with_its_data(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(kind="sabnzbd", client_job="SABnzbd_nzo_abc")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    deletes = [c[2]["params"] for c in server.calls if c[2]["params"].get("name") == "delete"]
    assert {(p["mode"], p["value"], p["del_files"]) for p in deletes} == {("queue", "SABnzbd_nzo_abc", "1"), ("history", "SABnzbd_nzo_abc", "1")}
    assert _history()[0]["data_json"] and json.loads(_history()[0]["data_json"])["removed_from_client"] is True


def test_nzbget_job_is_deleted_from_the_active_queue(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(kind="nzbget", client_job="42")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    sent = [json.loads(c[2]["body"]) for c in server.calls]
    assert [(m["method"], m["params"]) for m in sent] == [("editqueue", ["GroupFinalDelete", "", [42]])]


def test_nzbget_finished_job_falls_through_to_history(fake_server):
    server = fake_server(FakeServer(nzbget_queue_hit=False))
    did, _, _ = _download(state="import_failed", kind="nzbget", client_job="42")
    assert client.delete(f"/api/v1/downloads/{did}?remove_from_client=true").status_code == 200
    cmds = [json.loads(c[2]["body"])["params"][0] for c in server.calls]
    assert cmds == ["GroupFinalDelete", "HistoryFinalDelete"]


def test_qbittorrent_torrent_is_deleted_with_files(fake_server):
    server = fake_server(FakeServer(torrents=[{"hash": "abc123", "name": "Some Book"}]))
    did, _, _ = _download(kind="qbittorrent", client_job="abc123")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    delete = [c for c in server.calls if c[1] == "/api/v2/torrents/delete"]
    assert len(delete) == 1 and "hashes=abc123" in delete[0][2]["body"] and "deleteFiles=true" in delete[0][2]["body"]


def test_qbittorrent_never_deletes_by_name_when_the_stored_id_is_not_a_hash(fake_server):
    server = fake_server(FakeServer(torrents=[{"hash": "realhash", "name": "Some Book"}]))
    did, _, _ = _download(kind="qbittorrent", client_job="indexer-guid-1", title="Some Book")
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 200 and "not a torrent hash" in r.json()["client_error"]
    assert not [c for c in server.calls if c[1] == "/api/v2/torrents/delete"]  # files untouched
    assert _state(did) == "cancelled"


def test_transmission_torrent_is_removed_with_local_data(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(kind="transmission", client_job="deadbeef")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    sent = json.loads(server.calls[-1][2]["body"])
    assert sent == {"method": "torrent-remove", "arguments": {"ids": ["deadbeef"], "delete-local-data": True}}


# ---------- client trouble never blocks the cancel ----------
@pytest.mark.parametrize("kind", ["sabnzbd", "nzbget", "qbittorrent", "transmission"])
def test_unreachable_client_still_cancels_and_records_the_error(kind, fake_server):
    fake_server(FakeServer(fail="connection refused"))
    did, _, _ = _download(kind=kind)
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 200
    assert "connection refused" in r.json()["client_error"]
    assert _state(did) == "cancelled"
    entry = _history()[0]
    assert "connection refused" in entry["message"]
    assert "connection refused" in json.loads(entry["data_json"])["client_error"]


def test_job_already_gone_from_the_client_still_cancels(fake_server):
    fake_server(FakeServer(torrents=[]))
    did, _, _ = _download(kind="qbittorrent", client_job="gone", title="Some Book")
    r = client.delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 200 and "not found" in r.json()["client_error"]
    assert _state(did) == "cancelled"


def test_missing_client_row_still_cancels(fake_server):
    fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET client_id=NULL WHERE id=?", (did,))
    r = client.delete(f"/api/v1/downloads/{did}?remove_from_client=true")
    assert r.status_code == 200 and r.json()["client_error"]


def test_remove_from_client_false_keeps_the_client_job_for_a_finished_item(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    assert client.delete(f"/api/v1/downloads/{did}?remove_from_client=false").status_code == 200
    assert server.calls == []
    assert _state(did) == "cancelled"
    assert json.loads(_history()[0]["data_json"])["removed_from_client"] is False


def test_remove_from_client_defaults_to_false_for_a_finished_item(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    assert client.delete(f"/api/v1/downloads/{did}").status_code == 200
    assert server.calls == []


def test_remove_from_client_true_is_honoured_for_a_finished_item(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    assert client.delete(f"/api/v1/downloads/{did}?remove_from_client=true").status_code == 200
    assert server.calls


def test_active_item_is_always_stopped_in_the_client_even_with_the_flag_off(fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="downloading")
    assert client.delete(f"/api/v1/downloads/{did}?remove_from_client=false").status_code == 200
    assert server.calls


# ---------- history, poll loop, asset ----------
def test_history_gets_a_download_cancelled_entry(fake_server):
    fake_server(FakeServer())
    did, work_id, asset_id = _download(state="import_failed", title="Mistaken Epub")
    client.delete(f"/api/v1/downloads/{did}")
    (entry,) = _history()
    assert entry["work_id"] == work_id and entry["asset_id"] == asset_id
    assert "Mistaken Epub" in entry["message"]
    assert json.loads(entry["data_json"])["previous_state"] == "import_failed"
    assert any(x["event_type"] == "download.cancelled" for x in client.get("/api/v1/history").json()["items"])


def test_cancelled_item_is_not_touched_by_the_poll_loop(fake_server, monkeypatch):
    fake_server(FakeServer())
    did, _, _ = _download(kind="sabnzbd", client_job="job-1")
    client.delete(f"/api/v1/downloads/{did}")

    # A client that still lists the job must not resurrect the cancelled row.
    monkeypatch.setattr(downloaders, "poll", lambda c: [{"id": "job-1", "name": "Some Book", "progress": 0.9, "state": "completed", "path": "/x"}])
    result = downloads.poll_all()
    assert result["updated"] == 0
    assert _state(did) == "cancelled"


def test_wanted_asset_can_be_searched_and_grabbed_again(fake_server):
    fake_server(FakeServer())
    did, work_id, asset_id = _download(state="downloading")
    with db.tx() as conn:  # a slot left looking in-flight by an older build
        conn.execute("UPDATE media_assets SET status='grabbing' WHERE id=?", (asset_id,))
    client.delete(f"/api/v1/downloads/{did}")
    asset = db.one("SELECT status,monitored,has_acquisition_intent FROM media_assets WHERE id=?", (asset_id,))
    assert (asset["status"], asset["monitored"], asset["has_acquisition_intent"]) == ("missing", 1, 1)
    assert asset_id in [w["id"] for w in db.all("SELECT a.id FROM media_assets a WHERE a.status='missing' AND a.monitored=1 AND a.has_acquisition_intent=1")]
    r = client.get("/api/v1/wanted")
    assert r.status_code == 200 and work_id in json.dumps(r.json())
    # And the next grab for that work links to the same asset again.
    rid = _insert_release(work_id)
    grab = client.post(f"/api/v1/releases/{rid}/grab", json={})
    assert grab.status_code == 200, grab.text
    new = db.one("SELECT asset_id,state FROM downloads WHERE id=?", (grab.json()["download_id"],))
    assert new["asset_id"] == asset_id and new["state"] == "downloading"


def test_owned_asset_is_not_demoted_by_cancelling_a_redundant_download(fake_server):
    fake_server(FakeServer())
    did, _, asset_id = _download(state="import_failed", wanted=False)
    client.delete(f"/api/v1/downloads/{did}")
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def _insert_release(work_id) -> str:
    rid = db.uid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO releases(id,work_id,asset_kind,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,0,90,?)",
            (rid, work_id, "audiobook", "usenet", "Good Audiobook", "http://idx.test/nzb/1", db.now()),
        )
    return rid


# ---------- auth ----------
@pytest.fixture
def household(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(main_module.app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert r.status_code == 200, r.text
        admin_csrf = r.json()["csrf"]
        r = admin.post("/api/v1/users", json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"}, headers={"X-CSRF-Token": admin_csrf})
        assert r.status_code == 200, r.text
        kid = TestClient(main_module.app)
        r = kid.post("/api/v1/auth/login", json={"username": "junior", "password": "a-very-long-kid-password"})
        yield {"admin": admin, "admin_csrf": admin_csrf, "kid": kid, "kid_csrf": r.json()["csrf"]}


def test_delete_requires_csrf(household, fake_server):
    fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    r = household["admin"].delete(f"/api/v1/downloads/{did}")
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert _state(did) == "import_failed"
    assert household["admin"].delete(f"/api/v1/downloads/{did}", headers={"X-CSRF-Token": household["admin_csrf"]}).status_code == 200


def test_kids_user_gets_403_and_nothing_changes(household, fake_server):
    server = fake_server(FakeServer())
    did, _, _ = _download(state="import_failed")
    r = household["kid"].delete(f"/api/v1/downloads/{did}", headers={"X-CSRF-Token": household["kid_csrf"]})
    assert r.status_code == 403
    assert _state(did) == "import_failed" and server.calls == []


# ---------- Queue page (real app.js against a live server) ----------
UI_SETUP = """
  const els = {};
  const mk = () => ({ innerHTML: '', textContent: '', value: '', dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, querySelector: sel => els[sel] ||= mk(), querySelectorAll: () => [] });
  document.querySelector = sel => els[sel] ||= mk();
  document.querySelectorAll = () => [];
  const toasts = []; toast = (m, k) => toasts.push([m, k || 'good']);
"""


@needs_node
def test_queue_page_offers_cancel_or_remove_and_shows_the_failure_reason(live):
    _download(state="downloading", title="Active One")
    _download(state="import_failed", title="Failed One", error="epub release for an audiobook <b>x</b>")
    _download(state="paused", title="Paused One")
    html = run_ui(UI_SETUP + "await renderQueue(); return els['#view'].innerHTML;", live)
    rows = {title: row for title in ("Active One", "Failed One") for row in html.split("<tr>") if title in row}
    assert ">Cancel</button>" in rows["Active One"] and ">Remove</button>" not in rows["Active One"]
    assert ">Remove</button>" in rows["Failed One"] and ">Cancel</button>" not in rows["Failed One"]
    assert "epub release for an audiobook &lt;b&gt;x&lt;/b&gt;" in rows["Failed One"]  # escaped
    assert "queue-error" not in rows["Active One"]
    paused = next(row for row in html.split("<tr>") if "Paused One" in row)  # backend would 400: no button
    assert "cancel-download" not in paused


@needs_node
def test_confirming_the_modal_cancels_the_download_toasts_and_refreshes_the_queue(live):
    did, _, _ = _download(state="import_failed", title="Failed One")
    out = run_ui(UI_SETUP + f"""
      const realRender = renderQueue; let refreshed; renderQueue = () => refreshed = realRender();
      openCancelDownload('{did}', false);
      await els['#cancel-confirm'].onclick();
      await refreshed;  // the click must have triggered a queue refresh
      const html = els['#view'].innerHTML, openPart = html.split('Recently completed')[0];
      return [toasts, openPart.includes('Failed One'), openPart.includes('Queue is clear'), html.includes('Recently completed')];
    """, live)
    # Gone from the open queue; kept (not deleted) in the Recently completed history.
    assert out == [[["Download removed", "good"]], False, True, True]
    assert _state(did) == "cancelled"
