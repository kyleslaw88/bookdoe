"""Send to Kindle from the Player (Player API /kindle routes and the hardened v1 route).

Authorization (visibility predicate, device tokens refused), own-address editing with
validation and audit, best-file choice (EPUB first, Kindle-accepted formats only),
plain errors when nothing is sendable / no address / not configured, the per-account
rate limit, and the audit record without a mail body. SMTP is replaced by a fake that
records what would have been sent.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import player_api
from app.config import settings as base_settings
from app.services import devices, migration, tagging

import helpers

app = main_module.app


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=30):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("relay down")
        FakeSMTP.sent.append({"to": msg["To"], "from": msg["From"], "attachment": next((p.get_filename() for p in msg.walk() if p.get_filename()), None)})


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []; FakeSMTP.fail = False
    monkeypatch.setattr(devices.smtplib, "SMTP", FakeSMTP)
    player_api._kindle_sends.clear()
    return FakeSMTP


def _root(tmp_path):
    t = db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)", (db.uid(), "Root", str(tmp_path), "ebook", t, t))


def _book(tmp_path, title, files=("epub",), kids=False):
    _root(tmp_path)
    wid = migration._find_or_create_stub({"title": title, "author": "A", "identifiers": {}})
    assets = {}
    for ext in files:
        # One owned asset per format (helpers.own would reuse the single slot).
        aid, t = db.uid(), db.now()
        with db.tx() as conn:
            conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,'ebook',?,'owned',0,0,?,?)", (aid, wid, ext, t, t))
        p = tmp_path / f"{title}.{ext}"
        p.write_bytes(b"PK\x03\x04 data " * 10)
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,'{}',1,0,0,?)", (db.uid(), aid, str(p), 100, db.now()))
        assets[ext] = aid
    if kids:
        tagging.add_tag(wid, tagging.kids_tag_id())
    return wid, assets


@pytest.fixture
def world(monkeypatch, tmp_path, smtp):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    monkeypatch.setattr(player_api, "settings", replaced)
    devices.set_smtp_config(host="relay.test", port=25, username="", password=None, from_email="bookdoe@home.test", use_tls=False)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah)
        kids = _book(tmp_path, "Kids Book", ("mobi", "epub"), kids=True)
        adult = _book(tmp_path, "Adult Book", ("azw3",))
        audio_only = migration._find_or_create_stub({"title": "Audio Only", "author": "A", "identifiers": {}}); helpers.own(audio_only, "audiobook", "m4b")
        cbz = _book(tmp_path, "Comic", ("cbz",))
        s = {}
        for name in ("jane", "owen"):
            c = TestClient(app); r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            s[name] = (c, {"X-CSRF-Token": r.json()["csrf"]})
        s["ada"] = (admin, ah)
        yield s, {"kids": kids, "adult": adult, "audio": audio_only, "cbz": cbz}


def test_status_exposes_only_the_sender_and_own_address(world):
    s, w = world
    jane, jh = s["jane"]
    st = jane.get("/api/player/v1/kindle").json()
    assert st["configured"] is True and st["sender_address"] == "bookdoe@home.test" and st["kindle_email"] is None
    assert "host" not in st and "password" not in st and "username" not in st
    assert "epub" in st["formats"] and st["limit"]["sends"] == player_api.KINDLE_SEND_BUDGET


def test_own_email_is_validated_and_audited(world):
    s, w = world
    jane, jh = s["jane"]
    assert jane.put("/api/player/v1/kindle", json={"kindle_email": "nope"}, headers=jh).status_code == 400
    assert jane.put("/api/player/v1/kindle", json={"kindle_email": "x@y"}, headers=jh).status_code == 400
    assert jane.put("/api/player/v1/kindle", json={"kindle_email": " jane@kindle.com "}, headers=jh).json()["kindle_email"] == "jane@kindle.com"
    assert jane.get("/api/player/v1/kindle").json()["kindle_email"] == "jane@kindle.com"
    assert db.one("SELECT kindle_email FROM users WHERE username='jane'")["kindle_email"] == "jane@kindle.com"
    assert db.one("SELECT kindle_email FROM users WHERE username='owen'")["kindle_email"] is None, "own account only"
    assert jane.put("/api/player/v1/kindle", json={"kindle_email": None}, headers=jh).json()["kindle_email"] is None
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='account.kindle_email'")["n"] == 2
    # The legacy own-account route goes through the same validation and audit.
    assert jane.patch("/api/v1/auth/me", json={"kindle_email": "bad"}, headers=jh).status_code == 400
    assert jane.patch("/api/v1/auth/me", json={"kindle_email": "jane2@kindle.com"}, headers=jh).status_code == 200
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='account.kindle_email'")["n"] == 3


def test_best_file_is_epub_first_and_only_kindle_formats(world):
    s, w = world
    jane, jh = s["jane"]
    opts = jane.get(f"/api/player/v1/kindle/{w['kids'][0]}").json()["items"]
    assert [o["format"] for o in opts] == ["epub", "mobi"]
    assert jane.get(f"/api/player/v1/kindle/{w['adult'][0]}").json()["items"][0]["format"] == "azw3"
    assert jane.get(f"/api/player/v1/kindle/{w['audio']}").json()["items"] == []
    assert jane.get(f"/api/player/v1/kindle/{w['cbz'][0]}").json()["items"] == []


def test_send_picks_the_best_file_and_audits_without_a_body(world, smtp):
    s, w = world
    jane, jh = s["jane"]
    jane.put("/api/player/v1/kindle", json={"kindle_email": "jane@kindle.com"}, headers=jh)
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh)
    assert r.status_code == 200, r.text
    assert r.json()["format"] == "epub" and r.json()["to"] == "jane@kindle.com" and r.json()["filename"].endswith(".epub")
    assert smtp.sent == [{"to": "jane@kindle.com", "from": "bookdoe@home.test", "attachment": "Kids Book.epub"}]
    h = db.one("SELECT message,data_json,asset_id FROM history WHERE event_type='device.sent'")
    assert h["asset_id"] == w["kids"][1]["epub"] and '"user_id"' in h["data_json"] and "Sent from bookdoe" not in h["data_json"]
    # Naming the MOBI asset sends that one instead.
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={"asset_id": w["kids"][1]["mobi"]}, headers=jh)
    assert r.json()["format"] == "mobi"
    assert jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={"asset_id": "nope"}, headers=jh).status_code == 400


def test_plain_errors_when_nothing_sendable_no_address_or_not_configured(world, smtp):
    s, w = world
    jane, jh = s["jane"]
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh)
    assert r.status_code == 400 and "Kindle email" in r.json()["detail"]
    jane.put("/api/player/v1/kindle", json={"kindle_email": "jane@kindle.com"}, headers=jh)
    for wid in (w["audio"], w["cbz"][0]):
        r = jane.post(f"/api/player/v1/kindle/send/{wid}", json={}, headers=jh)
        assert r.status_code == 400 and "no file a Kindle accepts" in r.json()["detail"]
    smtp.fail = True
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh)
    assert r.status_code == 424 and "relay down" in r.json()["detail"], "a relay failure is a 424 with the reason, never a 5xx"
    smtp.fail = False
    devices.set_smtp_config(host="", port=25, username="", password=None, from_email="", use_tls=False)
    st = jane.get("/api/player/v1/kindle").json()
    assert st["configured"] is False and st["sender_address"] is None
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh)
    assert r.status_code == 400 and "not set up" in r.json()["detail"]
    assert len(smtp.sent) == 0


def test_kid_can_send_only_visible_works(world, smtp):
    s, w = world
    owen, oh = s["owen"]
    owen.put("/api/player/v1/kindle", json={"kindle_email": "owen@kindle.com"}, headers=oh)
    assert owen.get(f"/api/player/v1/kindle/{w['adult'][0]}").status_code == 404
    assert owen.post(f"/api/player/v1/kindle/send/{w['adult'][0]}", json={}, headers=oh).status_code == 404
    assert owen.post(f"/api/v1/library/{w['adult'][0]}/send/kindle", json={"asset_id": w["adult"][1]["azw3"]}, headers=oh).status_code == 404
    assert smtp.sent == []
    assert owen.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=oh).status_code == 200
    assert smtp.sent[0]["to"] == "owen@kindle.com"


def test_device_tokens_cannot_send_or_edit_the_address(world, smtp):
    s, w = world
    jane, jh = s["jane"]
    jane.put("/api/player/v1/kindle", json={"kindle_email": "jane@kindle.com"}, headers=jh)
    tok = auth_module.create_device(db.one("SELECT id FROM users WHERE username='jane'")["id"], name="reader")["token"]
    dev = TestClient(app, headers={"Authorization": f"Bearer {tok}"})
    assert dev.get("/api/player/v1/kindle").status_code == 403
    assert dev.put("/api/player/v1/kindle", json={"kindle_email": "x@y.com"}).status_code == 403
    assert dev.get(f"/api/player/v1/kindle/{w['kids'][0]}").status_code == 403
    assert dev.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}).status_code == 403
    assert dev.post(f"/api/v1/library/{w['kids'][0]}/send/kindle", json={"asset_id": w["kids"][1]["epub"]}).status_code == 403
    assert smtp.sent == [] and db.one("SELECT kindle_email FROM users WHERE username='jane'")["kindle_email"] == "jane@kindle.com"


def test_rate_limit_is_per_account(world, smtp):
    s, w = world
    jane, jh = s["jane"]; owen, oh = s["owen"]
    jane.put("/api/player/v1/kindle", json={"kindle_email": "jane@kindle.com"}, headers=jh)
    owen.put("/api/player/v1/kindle", json={"kindle_email": "owen@kindle.com"}, headers=oh)
    for _ in range(player_api.KINDLE_SEND_BUDGET):
        assert jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh).status_code == 200
    r = jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=jh)
    assert r.status_code == 429 and r.headers["Retry-After"]
    assert jane.post(f"/api/v1/library/{w['kids'][0]}/send/kindle", json={"asset_id": w["kids"][1]["epub"]}, headers=jh).status_code == 429, "the v1 route shares the budget"
    assert owen.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}, headers=oh).status_code == 200, "another account is not affected"
    assert len(smtp.sent) == player_api.KINDLE_SEND_BUDGET + 1


def test_csrf_and_sign_in_required(world):
    s, w = world
    jane, _ = s["jane"]
    assert jane.post(f"/api/player/v1/kindle/send/{w['kids'][0]}", json={}).status_code == 403
    assert TestClient(app).get("/api/player/v1/kindle").status_code == 401


def test_player_ui_wiring():
    js = (__import__("pathlib").Path(__file__).parent.parent / "app" / "static" / "player.js").read_text()
    assert "id=\"kindle-add\" href=\"#settings\"" in js and "id=\"kindle-send\"" in js and "if (!info.configured) return;" in js
    assert "b.disabled = true; const label = b.textContent; b.textContent = 'Sending…';" in js, "double submit is prevented"
    assert "approved senders" in js and "sender_address" in js
