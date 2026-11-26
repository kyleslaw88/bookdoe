"""Staying signed in: "Remember me for N days" and "Trust this device".

Remember me: the 24 h default, the long cookie, its OIDC state binding, sliding renewal and
its cap, invalidation. Trust this device: the mint endpoint's permissions, the HttpOnly
cookie, the Player-only cookie lane (and every Player UI route reachable in it, every
Management route not), CSRF, rotation with a grace window, revocation, limits, idle expiry
and the audit trail.
"""
import dataclasses
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app import main as main_module
from app import player_api
from app.config import settings as base_settings
from app.crypto import verify_session
from app.services import migration, scheduler, signin, tagging

import helpers
from test_oidc_callback_e2e import oidc  # noqa: F401  (fixture: mocked IdP, JWKS and token exchange)

app = main_module.app
DAY = 86400


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local", public_url="http://books.example.test")
    for mod in (main_module, auth, player_api):
        monkeypatch.setattr(mod, "settings", replaced)
    player_api._trust_hits.clear()
    return replaced


def _book(tmp_path, title):
    t = db.now()
    with db.tx() as conn:
        if not conn.execute("SELECT 1 FROM root_folders WHERE path=?", (str(tmp_path),)).fetchone():
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?)",
                         (db.uid(), "Root", str(tmp_path), "ebook", t, t))
    work_id = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = helpers.own(work_id, "ebook", "epub")
    p = tmp_path / f"{title}.epub"
    p.write_bytes(b"PK\x03\x04 " + b"x" * 400)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,'{}',1,0,0,?)",
                     (db.uid(), aid, str(p), p.stat().st_size, db.now()))
    return work_id, aid


def _login(name, remember=False):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password", "remember": remember})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}, r


def _jar(client, name):
    """Value of the (newest) cookie called `name` in a client's jar, whatever its domain."""
    values = [c.value for c in client.cookies.jar if c.name == name]
    return values[-1] if values else None


def _put_cookie(client, name, value):
    for c in [c for c in client.cookies.jar if c.name == name]:
        client.cookies.jar.clear(c.domain, c.path, c.name)
    client.cookies.set(name, value, domain="testserver.local")


def _cookie_attrs(response, name):
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            return header
    return None


def _max_age(header):
    m = re.search(r"Max-Age=(\d+)", header or "")
    return int(m.group(1)) if m else None


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
        yield {"admin": (admin, ah), "kids": kids, "adult": adult, "tmp": tmp_path}


def _trust(who, **body):
    """Sign in as `name`, trust the browser, and return (session client+headers, device-only client)."""
    c, h, _ = _login(who)
    r = c.post("/api/player/v1/devices/trust", json=body, headers=h, follow_redirects=False)
    assert r.status_code == 201, r.text
    token = _jar(c, auth.DEVICE_COOKIE)
    dev = TestClient(app)
    _put_cookie(dev, auth.DEVICE_COOKIE, token)
    return (c, h), dev, token


def _device_csrf(dev):
    r = dev.get("/api/player/v1/me")
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf"]}


# ---------------------------------------------------------------- remember me
def test_default_sign_in_is_24_hours_and_unremembered(world):
    _, _, r = _login("jane")
    assert _max_age(_cookie_attrs(r, auth.SESSION_COOKIE)) == DAY
    assert not db.one("SELECT 1 FROM history WHERE event_type='session.remembered'")


def test_remember_me_sets_a_90_day_cookie_with_an_expiry_claim(world):
    c, _, r = _login("jane", remember=True)
    header = _cookie_attrs(r, auth.SESSION_COOKIE)
    assert _max_age(header) == 90 * DAY and "HttpOnly" in header
    claims = verify_session(_jar(c, auth.SESSION_COOKIE))
    assert claims["rem"] == 1 and claims["abs"] - time.time() == pytest.approx(180 * DAY, abs=30)
    assert db.one("SELECT 1 FROM history WHERE event_type='session.remembered'")


def test_remember_me_settings_gate_it(world):
    admin, ah = world["admin"]
    signin.update({"auth.remember_allowed_types": ["admin"]})
    assert _max_age(_cookie_attrs(_login("jane", remember=True)[2], auth.SESSION_COOKIE)) == DAY       # adult not allowed
    signin.update({"auth.remember_allowed_types": ["admin", "adult", "kid"], "auth.remember_days": 0})
    assert _max_age(_cookie_attrs(_login("jane", remember=True)[2], auth.SESSION_COOKIE)) == DAY       # disabled
    signin.update({"auth.remember_days": 30})
    assert _max_age(_cookie_attrs(_login("owen", remember=True)[2], auth.SESSION_COOKIE)) == 30 * DAY


def test_oidc_remember_is_bound_to_the_signed_state_not_the_callback_query(oidc):  # noqa: F811
    base = "https://bookdoe.example.com"
    # Remembered: the wish rides inside the signed, state-bound cookie.
    with TestClient(app, base_url=base) as c:
        r = c.get("/auth/login?next=/app&remember=1", follow_redirects=False)
        state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
        saved = verify_session(_jar(c, auth.OIDC_COOKIE))
        assert saved["remember"] is True and saved["state"] == state
        oidc["nonce"] = saved["nonce"]
        cb = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
        assert cb.status_code == 302, cb.text
        assert _max_age(_cookie_attrs(cb, "__Host-" + auth.SESSION_COOKIE)) == 90 * DAY
    # Not asked for at /auth/login: a forged `remember=1` on the callback buys nothing.
    with TestClient(app, base_url=base) as c:
        r = c.get("/auth/login?next=/app", follow_redirects=False)
        state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
        oidc["nonce"] = verify_session(_jar(c, auth.OIDC_COOKIE))["nonce"]
        cb = c.get(f"/auth/callback?code=abc&state={state}&remember=1", follow_redirects=False)
        assert _max_age(_cookie_attrs(cb, "__Host-" + auth.SESSION_COOKIE)) == DAY
    # A tampered cookie (flag flipped) fails its signature, so the state check fails.
    with TestClient(app, base_url=base) as c:
        r = c.get("/auth/login?next=/app", follow_redirects=False)
        state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
        body, sig = _jar(c, auth.OIDC_COOKIE).split(".")
        _put_cookie(c, auth.OIDC_COOKIE, "A" + body + "." + sig)
        assert c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False).status_code == 400


class _Clock:
    def __init__(self, monkeypatch):
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)


def test_sliding_renewal_thresholds_and_the_absolute_cap(world, monkeypatch):
    clock = _Clock(monkeypatch)
    signin.update({"auth.remember_absolute_days": 100})
    c, _, _ = _login("jane", remember=True)

    def hit():
        r = c.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        return _max_age(_cookie_attrs(r, auth.SESSION_COOKIE))

    clock.offset = 30 * DAY
    assert hit() is None                      # 60 days left: more than half, no re-issue
    clock.offset = 50 * DAY
    assert abs(hit() - 50 * DAY) <= 2                  # 40 days left < 45: renewed, capped at 100 days from sign-in
    assert hit() is None                      # right after: not every request
    clock.offset = 80 * DAY
    assert hit() is None                      # already at the cap, nothing more to grant
    clock.offset = 101 * DAY
    assert c.get("/api/v1/auth/me").status_code == 401   # past the absolute end


def test_unremembered_sessions_never_renew(world, monkeypatch):
    clock = _Clock(monkeypatch)
    c, _, _ = _login("jane")
    clock.offset = 20 * 3600
    r = c.get("/api/v1/auth/me")
    assert r.status_code == 200 and _cookie_attrs(r, auth.SESSION_COOKIE) is None
    clock.offset = 25 * 3600
    assert c.get("/api/v1/auth/me").status_code == 401


def test_session_version_bumps_still_end_remembered_sessions(world):
    admin, ah = world["admin"]
    c, _, _ = _login("jane", remember=True)
    assert c.get("/api/v1/auth/me").status_code == 200
    jane = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    assert admin.patch(f"/api/v1/users/{jane}", json={"password": "a-brand-new-password-1"}, headers=ah).status_code == 200
    assert c.get("/api/v1/auth/me").status_code == 401
    c2, h2, _ = _login("owen", remember=True)
    r = c2.post("/api/v1/auth/logout-all", json={}, headers=h2)
    assert r.status_code == 200 and c2.get("/api/v1/auth/me").status_code == 401


# ---------------------------------------------------------------- trusting a device
def test_trust_endpoint_sets_an_httponly_cookie_and_returns_no_token(world):
    c, h, _ = _login("jane")
    r = c.post("/api/player/v1/devices/trust", json={"name": "Jane's iPad"}, headers={**h, "User-Agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605 Version/17.0 Safari/604.1"})
    assert r.status_code == 201
    header = _cookie_attrs(r, auth.DEVICE_COOKIE)
    assert "HttpOnly" in header and "SameSite=lax" in header and "Path=/" in header and _max_age(header) == 10 * 365 * DAY
    assert "Secure" not in header                               # the test deployment is http
    token = _jar(c, auth.DEVICE_COOKIE)
    assert token.startswith("alxd_") and token not in r.text   # never in the body
    assert r.json()["device"]["name"] == "Jane's iPad"
    row = db.one("SELECT * FROM devices WHERE kind='browser'")
    assert row["token_hash"] == auth._hash(token) and token not in str(row)       # only the hash is stored
    assert row["ua_summary"] == "Safari on iPadOS" and row["first_seen_at"]
    assert db.one("SELECT 1 FROM history WHERE event_type='device.trusted'")


def test_the_device_cookie_is_secure_over_https(world, monkeypatch):
    https = dataclasses.replace(auth.settings, public_url="https://books.example.test")
    monkeypatch.setattr(auth, "settings", https)
    c = TestClient(app, base_url="https://books.example.test")
    r = c.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
    sess = _cookie_attrs(r, "__Host-" + auth.SESSION_COOKIE)
    assert "Secure" in sess and "Path=/" in sess and "Domain" not in sess
    r = c.post("/api/player/v1/devices/trust", json={}, headers={"X-CSRF-Token": r.json()["csrf"]})
    dev = _cookie_attrs(r, "__Host-" + auth.DEVICE_COOKIE)
    assert "Secure" in dev and "Path=/" in dev and "Domain" not in dev and "HttpOnly" in dev
    assert _cookie_attrs(r, auth.DEVICE_COOKIE) is None
    assert c.get("/api/v1/auth/me").json()["user"]["username"] == "jane"
    # A token minted by this code is never accepted under the legacy name.
    legacy = TestClient(app, base_url="https://books.example.test")
    legacy.cookies.set(auth.SESSION_COOKIE, c.cookies.get("__Host-" + auth.SESSION_COOKIE), domain="books.example.test")
    assert legacy.get("/api/v1/auth/me").status_code == 401


def test_trust_endpoint_needs_session_and_csrf(world):
    c, h, _ = _login("jane")
    assert c.post("/api/player/v1/devices/trust", json={}).status_code == 403                       # no CSRF
    assert c.post("/api/player/v1/devices/trust", json={}, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert TestClient(app).post("/api/player/v1/devices/trust", json={}).status_code == 401
    # A Bearer device, a cookie device and an API key cannot mint trust.
    jane = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    made = auth.create_device(jane, name="Reader")
    bearer = TestClient(app, headers={"Authorization": f"Bearer {made['token']}"})
    assert bearer.post("/api/player/v1/devices/trust", json={}).status_code == 403
    _, key = auth.create_api_key(jane, "k", ["write"])
    assert TestClient(app, headers={"Authorization": f"Bearer {key}"}).post("/api/player/v1/devices/trust", json={}).status_code == 403
    (_, _), dev, _ = _trust("jane")
    assert dev.post("/api/player/v1/devices/trust", json={}, headers=_device_csrf(dev)).status_code == 403
    assert db.one("SELECT COUNT(*) n FROM devices WHERE kind='browser'")["n"] == 1


def test_cookie_lane_is_player_only_and_applies_kid_visibility(world):
    kids_work, adult_work = world["kids"][0], world["adult"][0]
    (_, _), jane, _ = _trust("jane")
    me = jane.get("/api/player/v1/me").json()
    assert me["device"]["via"] == "cookie" and me["management"] is False and me["csrf"]
    titles = {w["title"] for w in jane.get("/api/player/v1/library").json()["items"]}
    assert titles == {"Kids Book", "Adult Book"}
    assert jane.get(f"/api/v1/library/{adult_work}").status_code == 200
    (_, _), owen, _ = _trust("owen")
    assert [w["title"] for w in owen.get("/api/player/v1/library").json()["items"]] == ["Kids Book"]
    assert [w["title"] for w in owen.get("/api/v1/library").json()["items"]] == ["Kids Book"]
    assert owen.get(f"/api/v1/library/{adult_work}").status_code == 404
    assert owen.get(f"/api/player/v1/library/{adult_work}").status_code == 404
    assert owen.get(f"/api/v1/library/{kids_work}").status_code == 200
    detail = owen.get(f"/api/v1/library/{kids_work}").json()
    assert detail["history"] == [] and "kids_decision" not in detail
    assert owen.get("/api/v1/users").status_code == 403
    assert owen.post("/api/v1/auth/api-keys", json={"name": "x"}, headers=_device_csrf(owen)).status_code == 403


def test_an_administrators_trusted_browser_never_reaches_management(world):
    (_, _), ada, _ = _trust("ada")
    h = _device_csrf(ada)
    assert ada.get("/api/player/v1/library").status_code == 200
    me = ada.get("/api/v1/auth/me").json()
    assert me["user"]["management"] is False and me["user"]["auth_method"] == "device"
    for method, path in [("GET", "/api/v1/users"), ("GET", "/api/v1/roots"), ("GET", "/api/v1/dashboard"), ("GET", "/api/v1/settings/signin"),
                         ("PUT", "/api/v1/settings/signin"), ("GET", "/api/v1/jobs"), ("POST", "/api/v1/auth/api-keys"), ("GET", "/api/v1/devices"),
                         ("DELETE", "/api/v1/devices/x"), ("PATCH", "/api/v1/users/x"), ("GET", "/api/v1/system/health"), ("POST", "/api/v1/library"),
                         ("PATCH", "/api/v1/library/x"), ("GET", "/api/v1/library/x/tags"), ("POST", "/api/v1/auth/logout-all")]:
        r = ada.request(method, path, headers=h, json={} if method != "GET" else None)
        assert r.status_code == 403, (method, path, r.status_code)
    # The Management shell loads only so its own sign-in can run; its API is closed.
    assert ada.get("/manage").status_code == 200
    (_, _), jane, _ = _trust("jane")
    assert jane.get("/manage").status_code == 403


def test_cookie_lane_writes_need_the_csrf_token(world):
    work_id = world["adult"][0]
    (_, _), jane, _ = _trust("jane")
    url = f"/api/player/v1/favorites/{work_id}"
    assert jane.put(url).status_code == 403
    assert jane.put(url, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert jane.put(url, headers=_device_csrf(jane)).status_code == 200
    legacy = f"/api/v1/library/{work_id}/rating"
    assert jane.put(legacy, json={"rating": 4}).status_code == 403
    assert jane.put(legacy, json={"rating": 4}, headers=_device_csrf(jane)).status_code == 200
    # Another device's token is not this device's token.
    (_, _), owen, _ = _trust("owen")
    assert jane.put(url, headers=_device_csrf(owen)).status_code == 403


def test_session_wins_and_the_device_cookie_never_upgrades(world):
    (_, _), ada_dev, token = _trust("ada")
    c, h, _ = _login("jane")
    _put_cookie(c, auth.DEVICE_COOKIE, token)
    assert c.get("/api/v1/auth/me").json()["user"]["username"] == "jane"
    # Only the device cookie: still a device identity, and no session is ever issued.
    r = ada_dev.get("/api/v1/auth/me")
    assert r.json()["user"]["auth_method"] == "device" and _cookie_attrs(r, auth.SESSION_COOKIE) is None
    assert _jar(ada_dev, auth.SESSION_COOKIE) is None


def test_a_lapsed_session_falls_back_to_the_device_cookie(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (c, _), dev, token = _trust("jane")
    _put_cookie(c, auth.DEVICE_COOKIE, token)
    clock.offset = 2 * DAY                        # the 24 h session is gone
    me = c.get("/api/v1/auth/me").json()
    assert me["user"]["auth_method"] == "device"


# ---------------------------------------------------------------- rotation and revocation
def test_token_rotates_after_30_days_with_a_grace_window(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), dev, old = _trust("jane")
    did = db.one("SELECT id FROM devices WHERE kind='browser'")["id"]
    clock.offset = 10 * DAY
    assert _cookie_attrs(dev.get("/api/player/v1/me"), auth.DEVICE_COOKIE) is None      # too early
    clock.offset = 31 * DAY
    r = dev.get("/api/player/v1/me")
    header = _cookie_attrs(r, auth.DEVICE_COOKIE)
    assert header and _max_age(header) == 10 * 365 * DAY
    new = _jar(dev, auth.DEVICE_COOKIE)
    assert new != old and new.startswith("alxd_")
    assert db.one("SELECT COUNT(*) n FROM devices WHERE kind='browser'")["n"] == 1 and db.one("SELECT id FROM devices WHERE kind='browser'")["id"] == did
    assert db.one("SELECT 1 FROM history WHERE event_type='device.rotated'")
    # The replaced token survives a request race for a few minutes, then stops.
    racer = TestClient(app); _put_cookie(racer, auth.DEVICE_COOKIE, old)
    assert racer.get("/api/player/v1/me").status_code == 200
    assert _cookie_attrs(racer.get("/api/player/v1/me"), auth.DEVICE_COOKIE) is None   # no second rotation
    # The new token is not rotated again for another 30 days.
    assert _cookie_attrs(dev.get("/api/player/v1/me"), auth.DEVICE_COOKIE) is None
    clock.offset = 31 * DAY + auth.TRUST_ROTATE_GRACE + 5
    assert dev.get("/api/player/v1/me").status_code == 200     # the holder of the new token is fine


def test_a_device_with_no_expiry_stays_valid_for_years(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), dev, _ = _trust("owen")
    for years in range(1, 4):
        clock.offset = years * 365 * DAY
        assert dev.get("/api/player/v1/me").status_code == 200


def test_owner_can_list_and_revoke_trusted_devices_and_it_takes_effect_at_once(world):
    (c, h), dev, _ = _trust("jane", name="Kitchen tablet")
    listing = c.get("/api/player/v1/devices").json()
    mine = [d for d in listing["items"] if d["kind"] == "browser"]
    assert mine[0]["name"] == "Kitchen tablet" and listing["current_device_id"] == mine[0]["id"] and "token_hash" not in mine[0]
    assert dev.get("/api/player/v1/me").status_code == 200
    assert c.delete(f"/api/player/v1/devices/{mine[0]['id']}", headers=h).status_code == 200
    r = dev.get("/api/player/v1/me")
    assert r.status_code == 401 and "Max-Age=0" in _cookie_attrs(r, auth.DEVICE_COOKIE)    # and the dead cookie is dropped
    assert db.one("SELECT 1 FROM history WHERE event_type='device.revoked'")


def test_admin_sees_and_revokes_any_trusted_device(world):
    admin, ah = world["admin"]
    (_, _), owen, _ = _trust("owen")
    items = admin.get("/api/v1/devices").json()["items"]
    dev = next(d for d in items if d["kind"] == "browser")
    assert dev["owner"] == "owen" and dev["ua_summary"]
    assert admin.delete(f"/api/v1/devices/{dev['id']}", headers=ah).status_code == 200
    assert owen.get("/api/player/v1/me").status_code == 401


def test_signing_out_forgets_the_trusted_device(world):
    (c, h), dev, _ = _trust("jane")
    r = dev.post("/api/v1/auth/logout", json={}, headers=_device_csrf(dev))
    assert r.status_code == 200
    assert dev.get("/api/player/v1/me").status_code == 401
    (c2, h2), dev2, _ = _trust("owen")
    assert c2.post("/api/v1/auth/logout", json={}, headers=h2).status_code == 200      # from the session of that same browser
    assert dev2.get("/api/player/v1/me").status_code == 401


def test_trust_survives_password_change_and_role_change_unless_asked(world):
    admin, ah = world["admin"]
    jane_id = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    (_, _), dev, _ = _trust("jane")
    c, _, _ = _login("jane")          # a plain browser session, without the device cookie
    admin.patch(f"/api/v1/users/{jane_id}", json={"password": "a-brand-new-password-1"}, headers=ah)
    assert c.get("/api/v1/auth/me").status_code == 401                 # sessions end ...
    assert dev.get("/api/player/v1/me").status_code == 200             # ... the trusted device stays
    admin.patch(f"/api/v1/users/{jane_id}", json={"account_type": "kid"}, headers=ah)
    assert dev.get("/api/player/v1/me").json()["account_type"] == "kid"
    admin.patch(f"/api/v1/users/{jane_id}", json={"display_name": "J", "revoke_devices": True}, headers=ah)
    assert dev.get("/api/player/v1/me").status_code == 401


def test_demotion_to_pending_revokes_and_deletion_removes(world):
    admin, ah = world["admin"]
    owen_id = db.one("SELECT id FROM users WHERE username='owen'")["id"]
    (_, _), owen, _ = _trust("owen")
    admin.patch(f"/api/v1/users/{owen_id}", json={"account_type": "pending"}, headers=ah)
    assert owen.get("/api/player/v1/me").status_code == 401
    assert db.one("SELECT revoked_at FROM devices WHERE kind='browser'")["revoked_at"]
    jane_id = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    (_, _), jane, _ = _trust("jane")
    assert admin.delete(f"/api/v1/users/{jane_id}", headers=ah).status_code == 200
    assert jane.get("/api/player/v1/me").status_code == 401
    assert db.one("SELECT COUNT(*) n FROM devices WHERE user_id=?", (jane_id,))["n"] == 0


def test_sign_out_everywhere_keeps_trusted_devices_unless_ticked(world):
    (c, h), dev, _ = _trust("jane")
    assert c.post("/api/v1/auth/logout-all", json={}, headers=h).json()["trusted_devices_revoked"] == 0
    assert dev.get("/api/player/v1/me").status_code == 200
    c2, h2, _ = _login("jane")
    assert c2.post("/api/v1/auth/logout-all", json={"revoke_trusted_devices": True}, headers=h2).json()["trusted_devices_revoked"] == 1
    assert dev.get("/api/player/v1/me").status_code == 401


# ---------------------------------------------------------------- limits, settings, housekeeping
def test_per_user_limit_refuses_with_a_clear_message_and_retrust_replaces(world):
    signin.update({"auth.trusted_devices_max": 2})
    c, h, _ = _login("jane")
    assert c.post("/api/player/v1/devices/trust", json={}, headers=h).status_code == 201
    assert c.post("/api/player/v1/devices/trust", json={}, headers=h).status_code == 201     # same browser: replaces, not a second
    assert db.one("SELECT COUNT(*) n FROM devices WHERE kind='browser' AND revoked_at IS NULL")["n"] == 1
    for _ in range(2):
        other, oh, _ = _login("jane")
        r = other.post("/api/player/v1/devices/trust", json={}, headers=oh)
    assert r.status_code == 409 and "limit is 2" in r.json()["detail"]


def test_trust_endpoint_is_rate_limited(world):
    signin.update({"auth.trusted_devices_max": 50})
    c, h, _ = _login("jane")
    codes = [c.post("/api/player/v1/devices/trust", json={}, headers=h).status_code for _ in range(7)]
    assert codes[:5] == [201] * 5 and codes[5] == 429 and "Retry-After" in c.post("/api/player/v1/devices/trust", json={}, headers=h).headers


def test_trust_allowed_types_gate_creation_and_use(world):
    signin.update({"auth.trust_device_allowed_types": ["adult", "admin"]})
    c, h, _ = _login("owen")
    assert c.post("/api/player/v1/devices/trust", json={}, headers=h).status_code == 403
    signin.update({"auth.trust_device_allowed_types": ["adult", "admin", "kid"]})
    (_, _), owen, _ = _trust("owen")
    assert owen.get("/api/player/v1/me").status_code == 200
    signin.update({"auth.trust_device_allowed_types": ["adult", "admin"]})
    assert owen.get("/api/player/v1/me").status_code == 401         # switching it off ends existing kid devices


def test_idle_housekeeping_revokes_only_when_configured(world):
    (_, _), dev, _ = _trust("jane")
    db.set_setting("auth.trusted_device_idle_days", 0)
    with db.tx() as conn:
        conn.execute("UPDATE devices SET last_seen_at=?", (db.now() - 400 * DAY,))
    assert auth.revoke_idle_trusted_devices() == 0 and dev.get("/api/player/v1/me").status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE devices SET last_seen_at=?", (db.now() - 40 * DAY,))
    signin.update({"auth.trusted_device_idle_days": 30})
    assert scheduler._housekeeping()["idle_trusted_devices_revoked"] == 1
    assert dev.get("/api/player/v1/me").status_code == 401


def test_signin_settings_are_admin_only_validated_and_audited(world):
    admin, ah = world["admin"]
    c, h, _ = _login("jane")
    assert c.get("/api/v1/settings/signin").status_code == 403
    assert c.put("/api/v1/settings/signin", json={"remember_days": 5}, headers=h).status_code == 403
    assert admin.get("/api/v1/settings/signin").json() == {
        "auth.remember_days": 90, "auth.remember_allowed_types": ["admin", "adult", "kid"], "auth.remember_absolute_days": 180,
        "auth.trust_device_allowed_types": ["admin", "adult", "kid"], "auth.trusted_devices_max": 10, "auth.trusted_device_idle_days": 0}
    r = admin.put("/api/v1/settings/signin", json={"remember_days": 14, "remember_allowed_types": ["admin"], "trusted_device_idle_days": 60}, headers=ah)
    assert r.status_code == 200 and r.json()["auth.remember_days"] == 14 and r.json()["auth.remember_allowed_types"] == ["admin"]
    ev = db.one("SELECT data_json FROM history WHERE event_type='settings.signin'")
    assert ev and "90" in ev["data_json"] and "14" in ev["data_json"]
    for bad in ({"remember_days": -1}, {"remember_days": 9999}, {"remember_allowed_types": ["pending"]}, {"trusted_devices_max": 0}):
        assert admin.put("/api/v1/settings/signin", json=bad, headers=ah).status_code == 400
    assert admin.put("/api/v1/settings/signin", json={"bogus": 1}, headers=ah).status_code == 422


def test_unauthenticated_me_advertises_the_remember_option(world):
    body = TestClient(app).get("/api/v1/auth/me").json()
    assert body["remember_days"] == 90
    signin.update({"auth.remember_days": 0})
    assert TestClient(app).get("/api/v1/auth/me").json()["remember_days"] == 0


def test_user_agent_and_ip_summaries_keep_little():
    assert auth.ua_summary("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605 Version/17.0 Mobile Safari/604.1") == "Safari on iOS"
    assert auth.ua_summary("Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36") == "Chrome on Linux"
    assert auth.ua_summary(None) == "Browser"
    assert auth.ip_prefix("203.0.113.77") == "203.0.113.0/24"
    assert auth.ip_prefix("2001:db8:1:2:3:4:5:6") == "2001:db8:1::/48"
    assert auth.ip_prefix("nonsense") is None


# ---------------------------------------------------------------- every route the Player UI calls
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
API_CALL = re.compile(r"""api\(\s*([`'"])(/api/[^`'"]*)\1(?:\s*,\s*\{([^}]*)\})?""")
API_LITERAL = re.compile(r"""[`'"](/api/[^`'"]*)[`'"]""")


def _normalise(path):
    path = path.split("?")[0]
    return re.sub(r"\$\{[^}]*\}", "x", path).rstrip("/") or "/"


def player_ui_routes():
    routes = set()
    for name in ("player.js", "consume.js", "pwa.js", "sw.js"):
        text = (STATIC / name).read_text()
        for m in API_LITERAL.finditer(text):
            routes.add(("GET", _normalise(m.group(1))))
        # The progress saver builds its URL once and calls api(url, { method: 'PUT' ... }).
        for m in re.finditer(r"const url = `(/api/[^`]*)`", text):
            for v in re.findall(r"api\(url,\s*\{\s*method:\s*'(\w+)'", text):
                routes.add((v, _normalise(m.group(1))))
        for m in API_CALL.finditer(text):
            verbs = re.findall(r"""['"](GET|PUT|POST|PATCH|DELETE)['"]""", m.group(3) or "") or ["GET"]
            for v in verbs:
                routes.add((v, _normalise(m.group(2))))
    return {r for r in routes if not r[1].startswith("/api/v1/auth/")}


def test_every_route_the_player_ui_calls_is_reachable_with_the_device_cookie(world):
    routes = player_ui_routes()
    paths = {p for _, p in routes}
    # The extraction must really see the UI (guards against a regex that silently finds nothing).
    assert len(routes) >= 25
    for needed in ("/api/v1/library", "/api/v1/library/x/rating", "/api/v1/library/x/progress/x", "/api/v1/library/x/read/x", "/api/v1/authors",
                   "/api/v1/series", "/api/v1/tags", "/api/player/v1/home", "/api/player/v1/kindle", "/api/player/v1/preferences",
                   "/api/player/v1/bookmarks", "/api/player/v1/favorites/x", "/api/player/v1/requests", "/api/player/v1/devices/trust"):
        assert needed in paths, needed
    assert ("PUT", "/api/v1/library/x/progress/x") in routes and ("DELETE", "/api/v1/library/x/progress/x") in routes
    (_, _), jane, _ = _trust("jane")
    h = _device_csrf(jane)
    refused = []
    for method, path in sorted(routes):
        if path == "/api/player/v1/devices/trust":
            continue        # session-only by design: asserted separately
        r = jane.request(method, path.replace("/x", "/nope"), headers=h, json={} if method != "GET" else None)
        if r.status_code == 403 and ("Device credentials" in r.text or "not available to a device" in r.text):
            refused.append((method, path))
    assert refused == []


def test_the_cookie_lane_does_not_widen_the_bearer_lane(world):
    jane = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    bearer = TestClient(app, headers={"Authorization": f"Bearer {auth.create_device(jane, name='Reader')['token']}"})
    for method, path in (("GET", "/api/v1/library"), ("GET", "/api/v1/authors"), ("PUT", "/api/v1/library/x/rating")):
        assert bearer.request(method, path, json={}).status_code == 403, (method, path)
    assert bearer.get("/api/player/v1/library").status_code == 200


def test_cookie_devices_can_send_to_kindle_but_not_rotate_or_pair(world):
    (_, _), jane, _ = _trust("jane")
    h = _device_csrf(jane)
    assert jane.get("/api/player/v1/kindle").status_code == 200
    assert jane.post("/api/player/v1/devices/rotate", headers=h).status_code == 403
    assert jane.post("/api/player/v1/device/approve", json={"user_code": "ABCD-EFGH"}, headers=h).status_code == 403


# ---------------------------------------------------------------- security review fixes
def test_a_browser_token_is_not_a_bearer_token_and_a_paired_token_is_not_a_cookie(world):
    (_, _), jane, token = _trust("jane")
    jane_id = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    bearer = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert bearer.get("/api/player/v1/me").status_code == 401
    # The rotate escape: a stolen cookie value must not mint a paired token.
    assert bearer.post("/api/player/v1/devices/rotate").status_code == 401
    assert db.one("SELECT COUNT(*) n FROM devices WHERE kind='paired'")["n"] == 0
    # No CSRF-free write either.
    assert bearer.put(f"/api/player/v1/favorites/{world['adult'][0]}").status_code == 401
    paired = auth.create_device(jane_id, name="Reader")["token"]
    c = TestClient(app)
    _put_cookie(c, auth.DEVICE_COOKIE, paired)
    assert c.get("/api/player/v1/me").status_code == 401


def test_reuse_of_a_replaced_token_after_the_grace_window_revokes_the_device(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (c, h), dev, old = _trust("jane")
    thief = TestClient(app); _put_cookie(thief, auth.DEVICE_COOKIE, old)
    clock.offset = 31 * DAY
    assert thief.get("/api/player/v1/me").status_code == 200        # the thief is first after 30 days and wins the rotation
    stolen_current = _jar(thief, auth.DEVICE_COOKIE)
    assert stolen_current != old
    clock.offset = 31 * DAY + auth.TRUST_ROTATE_GRACE + 5
    r = dev.get("/api/player/v1/me")                                  # the victim still holds the replaced token
    assert r.status_code == 401 and "Max-Age=0" in _cookie_attrs(r, auth.DEVICE_COOKIE)
    row = db.one("SELECT revoked_at,reuse_detected_at FROM devices WHERE kind='browser'")
    assert row["revoked_at"] and row["reuse_detected_at"]
    assert db.one("SELECT 1 FROM history WHERE event_type='device.reuse_detected'")
    again = TestClient(app); _put_cookie(again, auth.DEVICE_COOKIE, stolen_current)
    assert again.get("/api/player/v1/me").status_code == 401         # both tokens are dead
    jane_session, _, _ = _login("jane")
    alerts = jane_session.get("/api/player/v1/devices").json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["detected_at"]
    admin, _, _ = _login("ada")
    flagged = [d for d in admin.get("/api/v1/devices?include_revoked=true").json()["items"] if d["reuse_detected_at"]]
    assert len(flagged) == 1 and flagged[0]["owner"] == "jane"


def test_a_replaced_token_inside_the_grace_window_recovers_with_a_fresh_cookie(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), dev, old = _trust("jane")
    clock.offset = 31 * DAY
    dev.get("/api/player/v1/me")                                     # rotation #1; the response is "lost":
    lost = TestClient(app); _put_cookie(lost, auth.DEVICE_COOKIE, old)
    clock.offset = 31 * DAY + 60                                      # the browser comes back with the old token, within 5 minutes
    r = lost.get("/api/player/v1/me")
    assert r.status_code == 200 and _cookie_attrs(r, auth.DEVICE_COOKIE)
    fresh = _jar(lost, auth.DEVICE_COOKIE)
    assert fresh != old
    clock.offset = 31 * DAY + 60 + auth.TRUST_ROTATE_GRACE + 5
    assert lost.get("/api/player/v1/me").status_code == 200           # it no longer depends on the replaced token
    assert db.one("SELECT revoked_at FROM devices WHERE kind='browser'")["revoked_at"] is None


def test_racing_requests_with_the_same_token_rotate_once(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), dev, old = _trust("jane")
    clock.offset = 31 * DAY
    a = TestClient(app); b = TestClient(app)
    _put_cookie(a, auth.DEVICE_COOKIE, old); _put_cookie(b, auth.DEVICE_COOKIE, old)
    assert a.get("/api/player/v1/me").status_code == 200 and b.get("/api/player/v1/me").status_code == 200
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='device.rotated'")["n"] == 1


def test_trusted_here_and_forget_ignore_a_dead_replaced_token(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (c, h), dev, old = _trust("jane")
    clock.offset = 31 * DAY
    dev.get("/api/player/v1/me")
    clock.offset = 31 * DAY + auth.TRUST_ROTATE_GRACE + 5
    s, _, _ = _login("jane")
    _put_cookie(s, auth.DEVICE_COOKIE, old)                           # a session plus a dead token
    assert s.get("/api/v1/auth/me").json()["signin"]["trusted_here"] is False
    jane_id = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [(b"cookie", f"{auth.DEVICE_COOKIE}={old}".encode())]})
    assert auth.device_id_for_cookie(req, jane_id) is None


def test_expired_trusted_device_is_not_here(world):
    (c, _), dev, token = _trust("owen")
    with db.tx() as conn:
        conn.execute("UPDATE devices SET expires_at=?", (db.now() - 5,))
    s, _, _ = _login("owen"); _put_cookie(s, auth.DEVICE_COOKIE, token)
    assert s.get("/api/v1/auth/me").json()["signin"]["trusted_here"] is False


def test_approvals_and_grants_need_a_real_session(world):
    admin, ah = world["admin"]
    owen_id = db.one("SELECT id FROM users WHERE username='owen'")["id"]
    adult_work = world["adult"][0]
    url = f"/api/player/v1/approvals/kids/{owen_id}/grants/{adult_work}"
    (_, _), ada_dev, _ = _trust("ada")
    h = _device_csrf(ada_dev)
    assert ada_dev.put(url, headers=h).status_code == 403
    assert ada_dev.get("/api/player/v1/approvals").status_code == 403
    assert ada_dev.get("/api/player/v1/approvals/kids").status_code == 403
    assert ada_dev.delete(url, headers=h).status_code == 403
    bearer = TestClient(app, headers={"Authorization": "Bearer " + auth.create_device(db.one("SELECT id FROM users WHERE username='ada'")["id"], name="R")["token"]})
    assert bearer.put(url).status_code == 403 and bearer.get("/api/player/v1/approvals").status_code == 403
    assert admin.put(url, headers=ah).status_code == 200            # the session path still works
    assert admin.get("/api/player/v1/approvals").status_code == 200
    assert tagging.is_visible_to(adult_work, db.one("SELECT * FROM users WHERE id=?", (owen_id,)))
    assert ada_dev.get("/api/v1/auth/me").json()["user"]["approver_for"] >= 0


def test_the_device_limit_holds_under_parallel_requests(world):
    import threading
    jane_id = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    results = []
    def go():
        try:
            auth.create_device(jane_id, name="t", kind="browser", max_browser=3); results.append("ok")
        except auth.DeviceLimitError:
            results.append("limit")
    threads = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert results.count("ok") == 3 and db.one("SELECT COUNT(*) n FROM devices WHERE kind='browser'")["n"] == 3


def test_signing_out_on_a_shared_browser_revokes_the_other_members_device(world):
    (_, _), jane_dev, jane_token = _trust("jane")
    owen, oh, _ = _login("owen")                                      # owen signs in on jane's trusted browser
    _put_cookie(owen, auth.DEVICE_COOKIE, jane_token)
    r = owen.post("/api/v1/auth/logout", json={}, headers=oh)
    assert r.status_code == 200 and "Max-Age=0" in _cookie_attrs(r, auth.DEVICE_COOKIE)
    assert db.one("SELECT COUNT(*) n FROM devices WHERE kind='browser' AND revoked_at IS NULL")["n"] == 0   # no orphan
    # Trusting that browser for owen replaces, not stacks, a leftover registration of someone else.
    (_, _), jane_dev2, tok2 = _trust("jane")
    owen2, oh2, _ = _login("owen"); _put_cookie(owen2, auth.DEVICE_COOKIE, tok2)
    assert owen2.post("/api/player/v1/devices/trust", json={}, headers=oh2).status_code == 201
    assert jane_dev2.get("/api/player/v1/me").status_code == 401


# ---------------------------------------------------------------- https: raw Set-Cookie, prefixes, legacy names
HTTPS = "https://books.example.test"


def _old_token(username, **over):
    """A session cookie exactly as the code before this PR minted it (no `v`, no remember claims)."""
    from app.crypto import sign_session
    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    claims = {"uid": row["id"], "username": username, "admin": bool(row["is_admin"]), "sid": "old-sid-" + username, "sv": row.get("session_version", 1)}
    claims.update(over)
    return sign_session(claims)


def _go_https(local_mode, monkeypatch):
    s = dataclasses.replace(local_mode, public_url=HTTPS)
    for mod in (main_module, auth, player_api):
        monkeypatch.setattr(mod, "settings", s)
    return s


@pytest.fixture
def https_mode(local_mode, monkeypatch):
    return _go_https(local_mode, monkeypatch)


def _raw(response):
    """[(name, value, attrs-dict)] of the Set-Cookie headers in the order the server sent them."""
    out = []
    for h in response.headers.get_list("set-cookie"):
        first, *rest = [p.strip() for p in h.split(";")]
        name, _, value = first.partition("=")
        attrs = {}
        for part in rest:
            k, _, v = part.partition("=")
            attrs[k.lower()] = v
        out.append((name, value.strip('"'), attrs))
    return out


def _deleted(entry):
    return entry[1] == "" or entry[2].get("max-age") == "0"


def _assert_consistent(response):
    """No deletion of a name that the same response sets (in either order)."""
    raw = _raw(response)
    for name, _, _ in raw:
        sets = [e for e in raw if e[0] == name and not _deleted(e)]
        dels = [e for e in raw if e[0] == name and _deleted(e)]
        assert not (sets and dels), f"{name} is both set and deleted: {raw}"
    return raw


def _is_host_cookie(entry):
    n, _, a = entry
    return n.startswith("__Host-") and "secure" in a and a.get("path") == "/" and "domain" not in a and "httponly" in a


def _https_client():
    return TestClient(app, base_url=HTTPS)


def test_https_local_login_emits_one_prefixed_session_and_only_deletes_the_legacy_name(world, https_mode):
    for remember, days in ((False, 1), (True, 90)):
        c = _https_client()
        r = c.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password", "remember": remember})
        raw = _assert_consistent(r)
        sets = [e for e in raw if not _deleted(e)]
        assert [e[0] for e in sets] == ["__Host-bookdoe_session"] and _is_host_cookie(sets[0])
        assert int(sets[0][2]["max-age"]) == days * DAY
        assert [e[0] for e in raw if _deleted(e)] == ["bookdoe_session"]
        # A real cookie jar ends with a working session, and a reload (a fresh request) stays signed in.
        assert c.get("/api/v1/auth/me").json()["user"]["username"] == "jane"
        assert c.get("/api/v1/auth/me").status_code == 200


def test_https_trust_sets_the_prefixed_device_cookie_without_deleting_it(world, https_mode):
    c = _https_client()
    r = c.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
    r = c.post("/api/player/v1/devices/trust", json={}, headers={"X-CSRF-Token": r.json()["csrf"]})
    raw = _assert_consistent(r)
    assert [e[0] for e in raw] == ["__Host-bookdoe_device"] and _is_host_cookie(raw[0])
    assert int(raw[0][2]["max-age"]) == 10 * 365 * DAY
    dev = _https_client(); dev.cookies.set("__Host-bookdoe_device", raw[0][1], domain="books.example.test")
    assert dev.get("/api/player/v1/me").status_code == 200


def test_https_oidc_callback_sets_one_prefixed_session(oidc):  # noqa: F811
    for remember in (False, True):
        with _https_client_for("https://bookdoe.example.com") as c:
            r = c.get("/auth/login?next=/app" + ("&remember=1" if remember else ""), follow_redirects=False)
            state = re.search(r"state=([^&]+)", r.headers["location"]).group(1)
            oidc["nonce"] = verify_session(_jar(c, auth.OIDC_COOKIE))["nonce"]
            cb = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
            raw = _assert_consistent(cb)
            sets = [e for e in raw if not _deleted(e) and e[0].endswith("bookdoe_session")]
            assert [e[0] for e in sets] == ["__Host-bookdoe_session"] and _is_host_cookie(sets[0])
            assert int(sets[0][2]["max-age"]) == (90 if remember else 1) * DAY
            assert c.get("/api/v1/auth/me").json()["authenticated"] is True


def _https_client_for(base):
    return TestClient(app, base_url=base)


def test_https_sign_out_and_sign_out_everywhere_delete_the_prefixed_cookie(world, https_mode):
    for path, body in (("/api/v1/auth/logout", {}), ("/api/v1/auth/logout-all", {})):
        c = _https_client()
        r = c.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        out = c.post(path, json=body, headers={"X-CSRF-Token": r.json()["csrf"]})
        assert out.status_code == 200
        raw = _assert_consistent(out)
        dels = {e[0]: e for e in raw if _deleted(e)}
        assert set(dels) >= {"__Host-bookdoe_session", "bookdoe_session"}
        host = dels["__Host-bookdoe_session"]
        assert "secure" in host[2] and host[2].get("path") == "/" and "domain" not in host[2]
        assert c.get("/api/v1/auth/me").status_code == 401        # the cookie jar really lost the session


def test_https_ignores_a_planted_legacy_cookie_when_a_prefixed_one_exists_or_for_devices(world, local_mode, monkeypatch):
    planted = _old_token("owen")
    (_, _), _, token = _trust("owen")
    _go_https(local_mode, monkeypatch)
    victim = _https_client()
    victim.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
    victim.cookies.set(auth.SESSION_COOKIE, planted, domain="books.example.test")
    assert victim.get("/api/v1/auth/me").json()["user"]["username"] == "jane"          # prefixed wins
    # With the prefixed cookie present but invalid, the planted one is still not consulted.
    victim.cookies.set("__Host-" + auth.SESSION_COOKIE, "garbage", domain="books.example.test")
    for c in [c for c in victim.cookies.jar if c.name == "__Host-" + auth.SESSION_COOKIE and c.value != "garbage"]:
        victim.cookies.jar.clear(c.domain, c.path, c.name)
    assert victim.get("/api/v1/auth/me").status_code == 401
    # A planted legacy DEVICE cookie never authenticates over https.
    lone = _https_client(); lone.cookies.set(auth.DEVICE_COOKIE, token, domain="books.example.test")
    assert lone.get("/api/player/v1/me").status_code == 401


def test_https_legacy_session_is_migrated_once_to_a_fresh_24h_session_inside_the_window(world, local_mode, monkeypatch):
    legacy = _old_token("jane")
    _go_https(local_mode, monkeypatch)
    db.set_setting(auth.LEGACY_UNTIL_KEY, db.now() + DAY)
    c = _https_client(); c.cookies.set(auth.SESSION_COOKIE, legacy, domain="books.example.test")
    r = c.get("/api/v1/auth/me")
    assert r.json()["user"]["username"] == "jane"
    raw = _assert_consistent(r)                                         # migrated: __Host- set, legacy deleted
    sets = [e for e in raw if not _deleted(e)]
    assert [e[0] for e in sets] == ["__Host-bookdoe_session"] and _is_host_cookie(sets[0])
    assert int(sets[0][2]["max-age"]) == DAY                            # a normal 24 h session, never a remember lifetime
    assert [e[0] for e in raw if _deleted(e)] == ["bookdoe_session"]
    claims = verify_session(sets[0][1])
    assert claims["sid"] != "old-sid-jane" and claims["v"] == 2 and "rem" not in claims
    assert db.one("SELECT 1 FROM history WHERE event_type='session.legacy_migrated'")
    # Migrated once: from now on the prefixed cookie is what counts (and the CSRF token is the new session's).
    assert c.get("/api/v1/auth/me").json()["csrf"] == r.json()["csrf"]
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='session.legacy_migrated'")["n"] == 1


def test_https_legacy_session_is_refused_after_the_window_for_new_format_tokens_pending_users_and_with_a_device_cookie(world, local_mode, monkeypatch):
    new_format, _, _ = _login("owen")
    planted_new = _jar(new_format, auth.SESSION_COOKIE)                 # a valid token, but minted by the new code
    admin, ah = world["admin"]
    owen = db.one("SELECT id FROM users WHERE username='jane'")["id"]
    old_jane, old_owen = _old_token("jane"), _old_token("owen")
    (_, _), _, dev_token = _trust("owen")
    pending_id = auth.create_local_user("zed", "a-very-long-zed-password", is_admin=False, access_scope="pending")
    old_zed = _old_token("zed")
    _go_https(local_mode, monkeypatch)
    db.set_setting(auth.LEGACY_UNTIL_KEY, db.now() + DAY)

    def probe(cookies):
        c = _https_client()
        for name, value in cookies.items():
            c.cookies.set(name, value, domain="books.example.test")
        return c.get("/api/v1/auth/me").status_code

    assert probe({auth.SESSION_COOKIE: planted_new}) == 401             # the attacker's own new-format token, planted as legacy
    assert probe({auth.SESSION_COOKIE: old_zed}) in (401, 403)           # a pending user is refused (never signed in as pending via legacy)
    # An old-format token planted for a victim who has a prefixed device cookie: refused, the device lane wins.
    c = _https_client()
    c.cookies.set(auth.SESSION_COOKIE, old_jane, domain="books.example.test")
    c.cookies.set("__Host-" + auth.DEVICE_COOKIE, dev_token, domain="books.example.test")
    me = c.get("/api/v1/auth/me").json()
    assert me["user"]["username"] == "owen" and me["user"]["auth_method"] == "device"
    # A stale sv (password change) is refused.
    with db.tx() as conn:
        auth.bump_session_version(conn, db.one("SELECT id FROM users WHERE username='jane'")["id"])
    assert probe({auth.SESSION_COOKIE: old_jane}) == 401
    # After the window, a legit old-format cookie is ignored entirely.
    db.set_setting(auth.LEGACY_UNTIL_KEY, db.now() - 1)
    assert probe({auth.SESSION_COOKIE: old_owen}) == 401


def test_the_legacy_deadline_is_fixed_one_day_after_first_use(local_mode):
    t0 = db.now()
    until = auth.legacy_cookie_until()
    assert until == pytest.approx(t0 + 1 * DAY, abs=5) and auth.legacy_cookie_until() == until


def test_alternating_use_of_a_replaced_token_is_caught(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), victim, old = _trust("jane")
    thief = TestClient(app); _put_cookie(thief, auth.DEVICE_COOKIE, old)
    clock.offset = 31 * DAY
    assert thief.get("/api/player/v1/me").status_code == 200            # rotation (thief holds the newest token)
    clock.offset += 20
    assert victim.get("/api/player/v1/me").status_code == 200           # the replaced token re-rotates once
    clock.offset += 20
    assert thief.get("/api/player/v1/me").status_code == 401            # its token is now the replaced one, used twice: reuse
    assert db.one("SELECT reuse_detected_at FROM devices WHERE kind='browser'")["reuse_detected_at"]
    assert victim.get("/api/player/v1/me").status_code == 401           # the whole registration is dead
    assert db.one("SELECT 1 FROM history WHERE event_type='device.reuse_detected'")


def test_ping_pong_never_runs_unnoticed_for_many_rounds(world, monkeypatch):
    clock = _Clock(monkeypatch)
    (_, _), a, tok = _trust("owen")
    b = TestClient(app); _put_cookie(b, auth.DEVICE_COOKIE, tok)
    clock.offset = 31 * DAY
    a.get("/api/player/v1/me")
    seen = []
    for i in range(6):
        who = b if i % 2 == 0 else a
        clock.offset += 30
        seen.append(who.get("/api/player/v1/me").status_code)
        if seen[-1] == 401:
            break
    assert 401 in seen and db.one("SELECT revoked_at FROM devices WHERE kind='browser'")["revoked_at"]


def test_a_trusted_browser_cannot_change_the_kindle_address_but_can_use_it(world):
    c, h, _ = _login("jane")
    assert c.put("/api/player/v1/kindle", json={"kindle_email": "jane@kindle.com"}, headers=h).status_code == 200
    (_, _), dev, _ = _trust("jane")
    dh = _device_csrf(dev)
    assert dev.put("/api/player/v1/kindle", json={"kindle_email": "evil@example.com"}, headers=dh).status_code == 403
    assert dev.put("/api/player/v1/kindle", json={"kindle_email": None}, headers=dh).status_code == 403
    assert dev.get("/api/player/v1/kindle").json()["kindle_email"] == "jane@kindle.com"
