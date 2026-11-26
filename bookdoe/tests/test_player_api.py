"""Phase 4: the Player API for devices.

Pairing (device code -> person approves in the Player -> device polls for a token),
token confinement (Player API and media bytes only; never Management, never admin),
revocation and rotation, the compact library, the delivery manifests, batch progress
with the conflict rule, incremental sync, and that a kid's device sees exactly the
kid's shelf.
"""
import dataclasses
import os

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import player_api
from app.config import settings as base_settings
from app.services import migration, reader, tagging

import helpers

app = main_module.app


def _root(tmp_path, kind="ebook"):
    t = db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)",
                         (db.uid(), "Root", str(tmp_path), kind, t, t))


def _book(tmp_path, title, kind="ebook", ext="epub", parts=1):
    _root(tmp_path, kind)
    work_id = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = helpers.own(work_id, kind, ext)
    fids = []
    for n in range(1, parts + 1):
        p = tmp_path / f"{title}-{n}.{ext}"
        p.write_bytes((b"PK\x03\x04 " if ext == "epub" else b"ID3") + bytes([n]) * 400)
        fid, t = db.uid(), db.now()
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,?,?,'{}',1,0,0,?)",
                         (fid, aid, str(p), n if parts > 1 else None, p.stat().st_size, 600 if kind == "audiobook" else None, t))
        fids.append(fid)
    return work_id, aid, fids


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local", public_url="http://books.example.test")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    monkeypatch.setattr(player_api, "settings", replaced)
    monkeypatch.setattr(reader, "_ffprobe_chapters", lambda path: [{"index": 0, "title": "One", "start": 0.0, "end": 300.0}])
    player_api._pair_hits.clear()
    return replaced


def _login(name):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}


def pair(person_client, person_headers, name="Kitchen reader", platform="esp32"):
    """Run the whole pairing dance and return (device client with Bearer, device id, raw token)."""
    anon = TestClient(app)
    r = anon.post("/api/player/v1/device/code", json={"name": name, "platform": platform, "client_version": "0.1"})
    assert r.status_code == 201, r.text
    codes = r.json()
    assert codes["verification_uri"] == "http://books.example.test/app#pair"
    assert codes["interval"] == 5 and codes["expires_in"] == 900
    # Polling before approval: pending.
    r = anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    assert r.status_code == 400 and r.json()["error"] == "authorization_pending"
    # The person approves in their Player (typed with lower case and no dash: tolerated).
    r = person_client.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"].replace("-", "").lower()}, headers=person_headers)
    assert r.status_code == 200, r.text
    # Device polls again (after the interval; the clock is driven, not slept).
    with db.tx() as conn:
        conn.execute("UPDATE device_codes SET last_poll_at=NULL")
    r = anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token"].startswith("alxd_") and tok["token_type"] == "Bearer"
    dev = TestClient(app, headers={"Authorization": f"Bearer {tok['token']}"})
    return dev, tok["device_id"], tok["token"]


@pytest.fixture
def world(local_mode, tmp_path):
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah)
        kids = _book(tmp_path, "Kids Book")
        tagging.add_tag(kids[0], tagging.kids_tag_id())
        adult = _book(tmp_path, "Adult Book")
        audio = _book(tmp_path, "Spoken Book", "audiobook", "mp3", parts=2)
        jane = _login("jane"); owen = _login("owen")
        yield {"admin": (admin, ah), "jane": jane, "owen": owen, "kids": kids, "adult": adult, "audio": audio}


# ---------- pairing and token lifecycle ----------
def test_pairing_flow_mints_a_user_scoped_token(world):
    jane, jh = world["jane"]
    dev, did, _ = pair(jane, jh)
    me = dev.get("/api/player/v1/me").json()
    assert me["username"] == "jane" and me["account_type"] == "adult" and me["device"]["id"] == did
    assert me["management"] is False and "player" in me["scopes"]
    mine = jane.get("/api/player/v1/devices").json()["items"]
    assert [d["name"] for d in mine] == ["Kitchen reader"] and "token" not in mine[0] and mine[0]["token_prefix"].startswith("alxd_")
    assert db.one("SELECT 1 FROM history WHERE event_type='device.registered'")


def test_device_code_is_single_use_and_expires(world):
    jane, jh = world["jane"]
    anon = TestClient(app)
    codes = anon.post("/api/player/v1/device/code", json={}).json()
    jane.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}, headers=jh)
    with db.tx() as conn:
        conn.execute("UPDATE device_codes SET last_poll_at=NULL")
    assert anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]}).status_code == 200
    r = anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    assert r.status_code == 400 and r.json()["error"] == "expired_token", "a consumed code cannot mint a second token"
    # Expired code.
    codes = anon.post("/api/player/v1/device/code", json={}).json()
    with db.tx() as conn:
        conn.execute("UPDATE device_codes SET expires_at=0 WHERE user_code=?", (codes["user_code"],))
    assert jane.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}, headers=jh).status_code == 404
    assert anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]}).json()["error"] == "expired_token"
    # Unknown code.
    assert anon.post("/api/player/v1/device/token", json={"device_code": "nope-nope-nope"}).json()["error"] == "expired_token"


def test_denied_code_never_becomes_a_token(world):
    jane, jh = world["jane"]
    anon = TestClient(app)
    codes = anon.post("/api/player/v1/device/code", json={}).json()
    assert jane.post("/api/player/v1/device/deny", json={"user_code": codes["user_code"]}, headers=jh).status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE device_codes SET last_poll_at=NULL")
    r = anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    assert jane.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}, headers=jh).status_code == 404


def test_polling_too_fast_is_told_to_slow_down(world):
    anon = TestClient(app)
    codes = anon.post("/api/player/v1/device/code", json={}).json()
    anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    r = anon.post("/api/player/v1/device/token", json={"device_code": codes["device_code"]})
    assert r.json()["error"] == "slow_down"


def test_pairing_requests_are_rate_limited_per_ip(world):
    anon = TestClient(app)
    for _ in range(player_api._PAIR_BUDGET):
        assert anon.post("/api/player/v1/device/code", json={}).status_code == 201
    r = anon.post("/api/player/v1/device/code", json={})
    assert r.status_code == 429 and r.headers["Retry-After"]


def test_only_a_signed_in_person_can_approve(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    anon = TestClient(app)
    codes = anon.post("/api/player/v1/device/code", json={}).json()
    assert anon.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}).status_code == 401
    # A device cannot approve another device (no credential laundering), nor can an API key.
    assert dev.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}).status_code == 403
    _, raw = auth_module.create_api_key(db.one("SELECT id FROM users WHERE username='jane'")["id"], "k", ["write"], creator_is_admin=False)
    key = TestClient(app, headers={"Authorization": f"Bearer {raw}"})
    assert key.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}).status_code == 403
    # Pending accounts cannot approve either.
    auth_module.create_local_user("newbie", "a-very-long-newbie-password", is_admin=False, access_scope="pending")
    p, ph = _login("newbie")
    assert p.post("/api/player/v1/device/approve", json={"user_code": codes["user_code"]}, headers=ph).status_code == 403


def test_revoked_token_stops_working_everywhere(world):
    jane, jh = world["jane"]
    dev, did, raw = pair(jane, jh)
    w, a, fids = world["adult"]
    assert dev.get(f"/api/v1/library/{w}/files/{fids[0]}").status_code == 200
    assert jane.delete(f"/api/player/v1/devices/{did}", headers=jh).status_code == 200
    assert dev.get("/api/player/v1/me").status_code == 401
    assert dev.get(f"/api/v1/library/{w}/files/{fids[0]}").status_code == 401
    assert dev.get("/api/player/v1/library").status_code == 401
    assert jane.get("/api/player/v1/devices").json()["items"] == []
    assert db.one("SELECT 1 FROM history WHERE event_type='device.revoked'")


def test_admin_can_revoke_anyones_device_but_a_user_only_their_own(world):
    admin, ah = world["admin"]; jane, jh = world["jane"]; owen, oh = world["owen"]
    dev, did, _ = pair(jane, jh)
    assert owen.delete(f"/api/player/v1/devices/{did}", headers=oh).status_code == 404
    listing = admin.get("/api/v1/devices").json()["items"]
    assert [d["owner"] for d in listing] == ["jane"]
    assert jane.get("/api/v1/devices").status_code == 403
    assert admin.delete(f"/api/v1/devices/{did}", headers=ah).status_code == 200
    assert dev.get("/api/player/v1/me").status_code == 401


def test_rotation_replaces_the_token_and_kills_the_old_one(world):
    jane, jh = world["jane"]
    dev, did, old = pair(jane, jh)
    r = dev.post("/api/player/v1/devices/rotate")
    assert r.status_code == 200
    fresh = r.json()
    assert fresh["token"] != old and fresh["device_id"] != did
    assert dev.get("/api/player/v1/me").status_code == 401, "old token is dead"
    dev2 = TestClient(app, headers={"Authorization": f"Bearer {fresh['token']}"})
    assert dev2.get("/api/player/v1/me").json()["device"]["id"] == fresh["device_id"]
    assert jane.post("/api/player/v1/devices/rotate", headers=jh).status_code == 403, "only a device rotates itself"
    names = [d["name"] for d in jane.get("/api/player/v1/devices").json()["items"]]
    assert names == ["Kitchen reader"], "one live device after rotation"


def test_device_token_is_confined_to_the_player_api_and_media_bytes(world):
    admin, ah = world["admin"]
    # Even an ADMIN's device gets no admin and no Management.
    dev, _, _ = pair(admin, ah)
    me = dev.get("/api/player/v1/me").json()
    assert me["account_type"] == "admin" and me["management"] is False
    for path in ("/api/v1/dashboard", "/api/v1/users", "/api/v1/library", "/api/v1/auth/api-keys", "/api/v1/devices", "/api/v1/requests"):
        assert dev.get(path).status_code == 403, path
    assert dev.get("/manage").status_code == 403
    assert dev.post("/api/v1/users", json={"username": "x", "password": "a-very-long-x-password"}).status_code == 403
    assert dev.post("/api/v1/auth/api-keys", json={"name": "x"}).status_code == 403
    w, a, fids = world["adult"]
    assert dev.get(f"/api/v1/library/{w}/files/{fids[0]}").status_code == 200
    assert dev.get(f"/api/v1/library/{w}/cover").status_code == 404  # no cover cached: a content 404, not a 403
    assert dev.get("/api/player/v1/library").status_code == 200
    # Management shell must not treat the device as an admin either (a browser would not send Bearer, but be sure).
    assert "app.js" not in dev.get("/manage").text


def test_device_of_an_account_made_pending_is_locked_out(world):
    admin, ah = world["admin"]; jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    jid = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    admin.patch(f"/api/v1/users/{jid}", json={"account_type": "pending"}, headers=ah)
    assert dev.get("/api/player/v1/library").status_code == 403


# ---------- library, manifests, bytes ----------
def test_device_library_is_compact_and_bounded_by_visibility(world):
    jane, jh = world["jane"]; owen, oh = world["owen"]
    jdev, _, _ = pair(jane, jh); odev, _, _ = pair(owen, oh, name="Owen's reader")
    lib = jdev.get("/api/player/v1/library").json()
    assert {w["title"] for w in lib["items"]} == {"Kids Book", "Adult Book", "Spoken Book"} and lib["total"] == 3
    item = next(w for w in lib["items"] if w["title"] == "Spoken Book")
    assert item["formats"] == ["audiobook"] and item["assets"][0]["readable"] is True
    for forbidden in ("files", "path", "monitored", "metadata_status", "has_acquisition_intent"):
        assert forbidden not in item and forbidden not in item["assets"][0]
    kid_lib = odev.get("/api/player/v1/library").json()
    assert [w["title"] for w in kid_lib["items"]] == ["Kids Book"]
    assert odev.get(f"/api/player/v1/library/{world['adult'][0]}").status_code == 404
    assert odev.get(f"/api/player/v1/ebook/{world['adult'][0]}/{world['adult'][1]}").status_code == 404
    assert odev.get(f"/api/v1/library/{world['adult'][0]}/files/{world['adult'][2][0]}").status_code == 404
    assert odev.get("/api/player/v1/library?kind=audiobook").json()["total"] == 0
    assert odev.get("/api/player/v1/authors").json()["total"] == 1


def test_pagination_etag_and_incremental_catalog(world, tmp_path):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    for i in range(5):
        _book(tmp_path, f"Extra {i}")
    page1 = dev.get("/api/player/v1/library?limit=3&offset=0")
    page2 = dev.get("/api/player/v1/library?limit=3&offset=3")
    assert page1.json()["total"] == 8 and len(page1.json()["items"]) == 3 and len(page2.json()["items"]) == 3
    assert page1.headers["ETag"] and dev.get("/api/player/v1/library?limit=3&offset=0", headers={"If-None-Match": page1.headers["ETag"]}).status_code == 304
    mark = page1.json()["server_time"]
    assert dev.get(f"/api/player/v1/library?updated_since={mark}").json()["total"] == 0
    with db.tx() as conn:
        conn.execute("UPDATE works SET updated_at=? WHERE title='Extra 2'", (mark + 10,))
    changed = dev.get(f"/api/player/v1/library?updated_since={mark}").json()
    assert [w["title"] for w in changed["items"]] == ["Extra 2"]
    s = dev.get(f"/api/player/v1/sync?since={mark}").json()
    assert len(s["library_changed"]) == 1 and s["server_time"] >= mark
    assert dev.get("/api/player/v1/library?limit=500").status_code == 422, "page size is capped"


def test_ebook_manifest_and_download_with_range(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    w, a, fids = world["adult"]
    m = dev.get(f"/api/player/v1/ebook/{w}/{a}").json()
    assert m["format"] == "epub" and m["download_url"] == f"/api/v1/library/{w}/files/{fids[0]}" and m["progress"] is None
    full = dev.get(m["download_url"])
    assert full.status_code == 200 and full.headers["content-type"] == "application/epub+zip"
    part = dev.get(m["download_url"], headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
    assert dev.get(f"/api/player/v1/audio/{w}/{a}").status_code == 404, "wrong kind"


def test_audio_manifest_streams_and_reports_chapters(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    w, a, fids = world["audio"]
    m = dev.get(f"/api/player/v1/audio/{w}/{a}").json()
    assert [f["id"] for f in m["files"]] == fids and m["duration_seconds"] == 1200 and m["download_eligible"] is True
    assert m["chapters"] == [], "chapters are listed only for single-file books"
    r = dev.get(m["files"][1]["url"], headers={"Range": "bytes=0-2"})
    assert r.status_code == 206 and r.headers["content-type"] == "audio/mpeg"


# ---------- progress sync ----------
def test_batch_progress_applies_the_conflict_rule_per_item(world):
    jane, jh = world["jane"]
    dev, did, _ = pair(jane, jh)
    (wa, aa, _), (ws, as_, fids) = world["adult"], world["audio"]
    r = dev.put("/api/player/v1/progress", json={"items": [
        {"asset_id": aa, "location": "epubcfi(/6/2!/4/1:0)", "percent": 0.2, "client_updated_at": 100},
        {"asset_id": as_, "file_id": fids[1], "position": 42.5, "percent": 0.53, "duration": 600, "client_updated_at": 100},
        {"asset_id": "nope", "position": 1},
    ]})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items[0]["applied"] is True and items[1]["applied"] is True and items[2]["error"] == "not_found"
    assert items[1]["device_id"] == did, "a device's writes are attributed to it by default"
    # Browser moved on at t=200; the device sends an offline update from t=150: stale.
    jane.put(f"/api/v1/library/{wa}/progress/{aa}", json={"location": "epubcfi(/6/8!/4/1:0)", "percent": 0.4, "client_updated_at": 200}, headers=jh)
    r = dev.put("/api/player/v1/progress", json={"items": [{"asset_id": aa, "location": "epubcfi(/6/4!/4/1:0)", "percent": 0.3, "client_updated_at": 150}]}).json()
    assert r["items"][0]["applied"] is False and r["items"][0]["percent"] == 0.4
    allp = dev.get("/api/player/v1/progress").json()["items"]
    assert {p["asset_id"]: p["percent"] for p in allp} == {aa: 0.4, as_: 0.53}
    one = dev.get(f"/api/player/v1/progress/{as_}").json()["progress"]
    assert one["position"] == 42.5 and one["file_id"] == fids[1] and one["duration"] == 600
    assert dev.put(f"/api/player/v1/progress/{as_}", json={"asset_id": "other", "position": 1}).status_code == 400
    assert dev.put("/api/player/v1/progress", json={"items": [{"asset_id": aa, "position": 1}] * 101}).status_code == 422


def test_two_devices_of_two_users_never_cross(world):
    jane, jh = world["jane"]; owen, oh = world["owen"]
    jdev, _, _ = pair(jane, jh); odev, _, _ = pair(owen, oh, name="Owen's reader")
    wk, ak, _ = world["kids"]
    jdev.put(f"/api/player/v1/progress/{ak}", json={"asset_id": ak, "location": "jane", "percent": 0.9})
    odev.put(f"/api/player/v1/progress/{ak}", json={"asset_id": ak, "location": "owen", "percent": 0.1})
    assert jdev.get(f"/api/player/v1/progress/{ak}").json()["progress"]["location"] == "jane"
    assert odev.get(f"/api/player/v1/progress/{ak}").json()["progress"]["location"] == "owen"
    assert odev.get("/api/player/v1/sync").json()["progress"][0]["percent"] == 0.1
    assert odev.put("/api/player/v1/progress", json={"items": [{"asset_id": world["adult"][1], "position": 3}]}).json()["items"][0]["error"] == "not_found"


def test_capabilities_are_public_and_me_requires_auth(local_mode):
    with TestClient(app) as c:
        cap = c.get("/api/player/v1/capabilities").json()
        assert cap["api_version"] == "1" and cap["auth"]["device_pairing"] is True and cap["limits"]["page_max"] == 200
        assert c.get("/api/player/v1/me").status_code == 401
