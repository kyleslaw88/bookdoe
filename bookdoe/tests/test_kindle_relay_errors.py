"""Relay failures reach the person as plain words, never as a 5xx (Cloudflare replaces
origin 5xx bodies with its own page, which is what the owner saw). Each mapping is driven
through a real socket SMTP server that misbehaves on cue, plus the implicit-TLS mode
selection and the Management test-email route. Exceptions are logged server-side
without credentials or the message body.
"""
import dataclasses
import logging
import socket
import threading

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import devices, migration, tagging

import helpers

app = main_module.app


class MisbehavingSMTP:
    """A minimal SMTP server on 127.0.0.1 with one scripted fault: 'starttls' (502 on
    STARTTLS), 'auth' (535 on AUTH), 'rcpt' (550 on RCPT), 'sender' (553 on MAIL),
    'drop' (close after greeting) or None (accept everything)."""

    def __init__(self, fault=None):
        self.fault = fault
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.accepted = []
        self.thread = threading.Thread(target=self._serve, daemon=True); self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        try:
            self._dialogue(conn)
        except OSError:
            pass  # a client that hung up mid-way (a TLS handshake against us, a reset)
        finally:
            conn.close()

    def _dialogue(self, conn):
        f = conn.makefile("rwb", buffering=0)
        send = lambda line: f.write((line + "\r\n").encode())
        send("220 misbehaving ready")
        if self.fault == "drop":
            conn.close(); return
        data_mode, buf = False, []
        while True:
            line = f.readline()
            if not line:
                break
            if data_mode:
                if line in (b".\r\n", b".\n"):
                    data_mode = False; self.accepted.append(b"".join(buf)); buf = []; send("250 OK queued")
                else:
                    buf.append(line)
                continue
            cmd = line.decode(errors="replace").strip(); up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                send("250-misbehaving"); send("250-STARTTLS" if self.fault != "starttls" else "250-SIZE 1000000"); send("250 AUTH PLAIN LOGIN")
            elif up == "STARTTLS":
                send("502 STARTTLS not available here")   # the fault; a real TLS server would say 220
            elif up.startswith("AUTH"):
                send("535 5.7.8 Authentication credentials invalid" if self.fault == "auth" else "235 ok")
            elif up.startswith("MAIL FROM:"):
                send("553 5.1.8 Sender address rejected: not owned by user" if self.fault == "sender" else "250 OK")
            elif up.startswith("RCPT TO:"):
                send("550 5.1.1 The email account that you tried to reach does not exist" if self.fault == "rcpt" else "250 OK")
            elif up == "DATA":
                send("354 go ahead"); data_mode = True
            elif up == "QUIT":
                send("221 bye"); break
            else:
                send("250 OK")

    def close(self):
        # shutdown() is what unblocks accept(); close() alone leaves the thread parked.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(5)


@pytest.fixture
def relay():
    servers = []

    def make(fault=None):
        s = MisbehavingSMTP(fault); servers.append(s); return s
    yield make
    for s in servers:
        s.close()


def _configure(port, *, tls_mode="none", username="", password=None):
    devices.set_smtp_config(host="127.0.0.1", port=port, username=username, password=password, from_email="bookdoe@home.test", tls_mode=tls_mode)


@pytest.fixture
def world(monkeypatch, tmp_path):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password", "email": "ada@home.test"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        admin.put("/api/player/v1/kindle", json={"kindle_email": "ada@kindle.com"}, headers=ah)
        t = db.now()
        with db.tx() as conn:
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES ('r','R',?,'ebook','adopt',?,?)", (str(tmp_path), t, t))
        wid = migration._find_or_create_stub({"title": "Book", "author": "A", "identifiers": {}})
        aid = helpers.own(wid, "ebook", "epub")
        p = tmp_path / "Book.epub"; p.write_bytes(b"PK\x03\x04 data " * 10)
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES ('f',?,?,100,'{}',1,0,0,?)", (aid, str(p), t))
        yield admin, ah, wid, aid


def _send(admin, ah, wid):
    return admin.post(f"/api/player/v1/kindle/send/{wid}", json={}, headers=ah)


def test_plain_relay_delivers(world, relay):
    admin, ah, wid, aid = world
    srv = relay(None); _configure(srv.port)
    r = _send(admin, ah, wid)
    assert r.status_code == 200, r.text
    assert srv.accepted and b"Book.epub" in srv.accepted[0]


@pytest.mark.parametrize("fault,tls_mode,username,status,kind,needle", [
    ("starttls", "starttls", "", 424, "starttls_unsupported", "does not support TLS (STARTTLS)"),
    ("auth", "none", "user", 424, "auth", "rejected the username or password: 535 5.7.8 Authentication credentials invalid"),
    ("rcpt", "none", "", 424, "recipient_refused", "rejected the recipient (ada@kindle.com: 550 5.1.1 The email account"),
    ("sender", "none", "", 424, "sender_refused", "rejected the sender bookdoe@home.test: 553 5.1.8"),
    ("drop", "none", "", 424, "disconnected", "dropped the connection"),
])
def test_relay_faults_map_to_plain_424_messages(world, relay, caplog, fault, tls_mode, username, status, kind, needle):
    admin, ah, wid, aid = world
    srv = relay(fault); _configure(srv.port, tls_mode=tls_mode, username=username, password="secret-pw" if username else None)
    with caplog.at_level(logging.WARNING, logger="app.services.devices"):
        r = _send(admin, ah, wid)
    assert r.status_code == status, r.text
    detail = r.json()["detail"]
    assert needle in detail, detail
    assert f"127.0.0.1:{srv.port}" in detail
    # Logged server-side with the exception type, without the password or the body.
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert f"mail delivery failed [{kind}]" in logged and "secret-pw" not in logged and "Sent from bookdoe" not in logged
    # The v1 route answers the same way.
    r = admin.post(f"/api/v1/library/{wid}/send/kindle", json={"asset_id": aid}, headers=ah)
    assert r.status_code == 424 and needle in r.json()["detail"]


def test_closed_port_and_unknown_host_are_unreachable_424(world, caplog):
    admin, ah, wid, aid = world
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    _configure(port)
    r = _send(admin, ah, wid)
    assert r.status_code == 424 and r.json()["detail"] == f"Could not reach the mail server 127.0.0.1:{port}: connection refused"
    devices.set_smtp_config(host="no-such-host.invalid", port=25, username="", password=None, from_email="a@b.test", tls_mode="none")
    r = _send(admin, ah, wid)
    assert r.status_code == 424 and "hostname not found" in r.json()["detail"]


def test_implicit_tls_mode_uses_smtp_ssl_and_a_wrong_port_explains_itself(world, relay, monkeypatch):
    admin, ah, wid, aid = world
    used = {}

    class FakeSSL:
        def __init__(self, host, port, timeout=30, context=None):
            used["cls"] = "SMTP_SSL"; used["port"] = port

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, msg): used["sent"] = msg["To"]
    with pytest.MonkeyPatch.context() as mp:   # scoped: the world fixture's own patches must survive
        mp.setattr(devices.smtplib, "SMTP_SSL", FakeSSL)
        _configure(465, tls_mode="ssl")
        assert devices.get_smtp_config()["tls_mode"] == "ssl" and devices.get_smtp_config()["use_tls"] is False
        assert _send(admin, ah, wid).status_code == 200 and used == {"cls": "SMTP_SSL", "port": 465, "sent": "ada@kindle.com"}
    # Implicit TLS against a plain-text relay: the handshake fails and the message names the port rule.
    srv = relay(None); _configure(srv.port, tls_mode="ssl")
    r = _send(admin, ah, wid)
    assert r.status_code == 424 and "465 expects implicit TLS, 587 expects STARTTLS" in r.json()["detail"]


def test_legacy_use_tls_flag_still_maps_and_settings_round_trip(world):
    admin, ah, wid, aid = world
    r = admin.put("/api/v1/settings/devices", json={"host": "h", "port": 587, "from_email": "a@b.test", "use_tls": False}, headers=ah)
    assert r.json()["tls_mode"] == "none" and r.json()["use_tls"] is False
    r = admin.put("/api/v1/settings/devices", json={"host": "h", "port": 465, "from_email": "a@b.test", "tls_mode": "ssl"}, headers=ah)
    assert r.json()["tls_mode"] == "ssl" and "password" not in r.json()


def test_test_email_route_reports_the_relay_answer(world, relay):
    admin, ah, wid, aid = world
    srv = relay("rcpt"); _configure(srv.port)
    r = admin.post("/api/v1/settings/devices/test", json={}, headers=ah)
    assert r.status_code == 200 and r.json()["ok"] is False and "rejected the recipient (ada@home.test" in r.json()["message"]
    good = relay(None); _configure(good.port)
    r = admin.post("/api/v1/settings/devices/test", json={}, headers=ah)
    assert r.json()["ok"] is True and "Sent a test message" in r.json()["message"] and good.accepted
    assert db.one("SELECT 1 FROM history WHERE event_type='device.smtp_test'")
    # Admin only; needs an email on the account.
    db.execute("UPDATE users SET email=NULL WHERE username='ada'")
    assert admin.post("/api/v1/settings/devices/test", json={}, headers=ah).status_code == 400


def test_delivery_times_out_off_the_request_thread(world, monkeypatch):
    admin, ah, wid, aid = world
    monkeypatch.setattr(devices, "SMTP_TIMEOUT", 0.2)
    hang = threading.Event()

    def never(cfg, msg):
        hang.wait(20)   # longer than SMTP_TIMEOUT + the 5 s grace: the request must give up first
    monkeypatch.setattr(devices, "_deliver_now", never)
    _configure(2525)
    r = _send(admin, ah, wid)
    hang.set()      # release the worker right away so teardown does not wait on it
    assert r.status_code == 424 and "timed out after 0 s" in r.json()["detail"]


def test_player_shows_the_reason_and_management_has_the_test_button():
    import pathlib
    static = pathlib.Path(__file__).parent.parent / "app" / "static"
    player = (static / "player.js").read_text()
    assert "catch (er) { toast(er.message, 'bad', 7000); b.textContent = label; b.disabled = false; }" in player, "the 424 detail is shown verbatim and the button re-enabled"
    app_js = (static / "app.js").read_text()
    assert 'id="smtp-test"' in app_js and "/api/v1/settings/devices/test" in app_js and "name=\"tls_mode\"" in app_js
