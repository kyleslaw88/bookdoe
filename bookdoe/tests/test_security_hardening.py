"""Security review items C2, C3, C5, C6, C7 (see docs/SECURITY.md)."""
import base64
import dataclasses

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, crypto, db, migrations
from app import main as main_module
from app.config import settings as base_settings
from app.services import devices, migration, tagging

import helpers

app = main_module.app
PW = "a-very-long-password"


def _set_settings(monkeypatch, **changes):
    replaced = dataclasses.replace(base_settings, **changes)
    for mod in (main_module, auth, crypto):
        monkeypatch.setattr(mod, "settings", replaced)
    return replaced


@pytest.fixture(autouse=True)
def _clean_throttle(monkeypatch):
    auth._throttle.clear()
    auth._opds_cache.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth, "_clock", lambda: clock["t"])
    return clock


@pytest.fixture
def clock(_clean_throttle):
    return _clean_throttle


@pytest.fixture
def local(monkeypatch):
    return _set_settings(monkeypatch, auth_mode="local")


# ---------------- C3 ----------------
def test_empty_secret_key_refuses_to_start_outside_none_mode(monkeypatch):
    for mode in ("local", "oidc", "proxy"):
        _set_settings(monkeypatch, secret_key="", auth_mode=mode)
        with pytest.raises(RuntimeError, match="BOOKDOE_SECRET_KEY"):
            crypto.ensure_secret_configured()


def test_empty_secret_key_only_warns_in_none_mode(monkeypatch, caplog):
    _set_settings(monkeypatch, secret_key="", auth_mode="none")
    with caplog.at_level("WARNING", logger="app.crypto"):
        crypto.ensure_secret_configured()
    assert "hostname" in caplog.text
    assert crypto._base_secret()  # fallback still works


def test_configured_secret_key_starts_in_every_mode(monkeypatch):
    _set_settings(monkeypatch, secret_key="x" * 32, auth_mode="local")
    crypto.ensure_secret_configured()


def test_lifespan_aborts_startup_without_a_secret(monkeypatch):
    _set_settings(monkeypatch, secret_key="", auth_mode="local")
    with pytest.raises(RuntimeError, match="BOOKDOE_SECRET_KEY"):
        with TestClient(app):
            pass


# ---------------- C2 ----------------
def _proxy_request(host, header_value="ada"):
    from fastapi import Request
    headers = [(b"x-authentik-username", header_value.encode())] if header_value else []
    return Request({"type": "http", "method": "GET", "headers": headers, "client": (host, 4444)})


def test_proxy_header_accepted_from_a_trusted_proxy(monkeypatch):
    _set_settings(monkeypatch, auth_mode="proxy", trusted_proxies="10.0.0.0/24, 192.168.1.5")
    assert auth.current_user(_proxy_request("10.0.0.7"))["username"] == "ada"
    assert auth.current_user(_proxy_request("192.168.1.5"))["username"] == "ada"


def test_proxy_header_refused_from_an_untrusted_client(monkeypatch):
    _set_settings(monkeypatch, auth_mode="proxy", trusted_proxies="10.0.0.0/24")
    assert auth.current_user(_proxy_request("203.0.113.9")) is None
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 0, "an untrusted header must not even provision a user"


def test_proxy_mode_with_empty_list_fails_closed_and_warns(monkeypatch, caplog):
    _set_settings(monkeypatch, auth_mode="proxy", trusted_proxies="")
    assert auth.current_user(_proxy_request("10.0.0.7")) is None
    with caplog.at_level("WARNING", logger="app.auth"):
        auth.warn_insecure_config()
    assert "TRUSTED_PROXIES is empty" in caplog.text


def test_forwarded_for_is_only_believed_from_a_trusted_proxy(monkeypatch):
    from fastapi import Request
    _set_settings(monkeypatch, trusted_proxies="10.0.0.1")

    def req(peer):
        return Request({"type": "http", "method": "POST", "client": (peer, 1),
                        "headers": [(b"x-forwarded-for", b"198.51.100.4, 10.0.0.1")]})
    assert auth.client_ip(req("10.0.0.1")) == "198.51.100.4"
    assert auth.client_ip(req("203.0.113.9")) == "203.0.113.9"


# ---------------- C5 ----------------
def _login(client, user, pw):
    return client.post("/api/v1/auth/login", json={"username": user, "password": pw})


def test_five_failures_lock_the_account_for_sixty_seconds(local, clock):
    auth.create_local_user("ada", PW)
    c = TestClient(app)
    for _ in range(5):
        assert _login(c, "ada", "wrong-password-xx").status_code == 401
    r = _login(c, "ada", PW)  # correct password, but locked
    assert r.status_code == 429 and "Retry-After" in r.headers
    clock["t"] += 59
    assert _login(c, "ada", PW).status_code == 429
    clock["t"] += 2
    assert _login(c, "ada", PW).status_code == 200


def test_failures_across_usernames_lock_the_ip(local, clock):
    auth.create_local_user("ada", PW)
    c = TestClient(app)
    for i in range(5):
        _login(c, f"ghost{i}", "wrong-password-xx")
    assert _login(c, "ada", PW).status_code == 429  # same client IP is locked
    clock["t"] += 61
    assert _login(c, "ada", PW).status_code == 200


def test_success_resets_the_username_counter(local, clock):
    auth.create_local_user("ada", PW)
    c = TestClient(app, client=("198.51.100.1", 1))
    for _ in range(4):
        _login(c, "ada", "wrong-password-xx")
    assert _login(c, "ada", PW).status_code == 200
    assert auth._throttle.get("user:ada") is None


def test_unknown_user_costs_the_same_argon2_work(monkeypatch):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda stored, pw: calls.append(stored) or real(stored, pw))
    auth.create_local_user("ada", PW)
    assert auth.login_local("nobody", PW) is None
    assert len(calls) == 1 and calls[0] == auth._DUMMY_HASH
    assert auth.login_local("ada", "wrong-password-xx") is None
    assert len(calls) == 2


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_opds_basic_is_throttled(local, clock):
    auth.create_local_user("ada", PW)
    c = TestClient(app)
    for _ in range(5):
        assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", "wrong-password-xx")).status_code == 401
    assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", PW)).status_code == 429


def test_opds_basic_success_is_cached_for_five_minutes(local, clock, monkeypatch):
    auth.create_local_user("ada", PW)
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda stored, pw: calls.append(1) or real(stored, pw))
    c = TestClient(app)
    for _ in range(3):
        assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", PW)).status_code == 200
    assert len(calls) == 1
    clock["t"] += 301
    assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", PW)).status_code == 200
    assert len(calls) == 2


def test_opds_cache_does_not_outlive_a_password_change(local, clock):
    uid = auth.create_local_user("ada", PW)
    c = TestClient(app)
    assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", PW)).status_code == 200
    with db.tx() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (crypto.hash_password("another-long-password"), uid))
        auth.bump_session_version(conn, uid)
    assert c.get("/opds/v1/catalog.xml", headers=_basic("ada", PW)).status_code == 401


# ---------------- C6 ----------------
def _admin_and_user(local):
    admin = TestClient(app)
    r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": PW})
    csrf = r.json()["csrf"]
    r = admin.post("/api/v1/users", json={"username": "junior", "password": "junior-long-password"},
                   headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    other = TestClient(app, client=("198.51.100.2", 1))
    assert _login(other, "junior", "junior-long-password").status_code == 200
    return admin, csrf, other, r.json()["id"]


def test_password_change_invalidates_an_existing_session_cookie(local):
    admin, csrf, other, uid = _admin_and_user(local)
    assert other.get("/api/v1/auth/me").status_code == 200
    r = admin.patch(f"/api/v1/users/{uid}", json={"password": "brand-new-long-password"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert other.get("/api/v1/auth/me").status_code == 401
    assert admin.get("/api/v1/auth/me").status_code == 200, "other users' sessions are untouched"
    fresh = TestClient(app, client=("198.51.100.3", 1))
    assert _login(fresh, "junior", "brand-new-long-password").status_code == 200


def test_demotion_invalidates_sessions_but_a_plain_edit_does_not(local):
    admin, csrf, other, uid = _admin_and_user(local)
    admin.patch(f"/api/v1/users/{uid}", json={"display_name": "Junior"}, headers={"X-CSRF-Token": csrf})
    assert other.get("/api/v1/auth/me").status_code == 200
    admin.patch(f"/api/v1/users/{uid}", json={"access_scope": "kids"}, headers={"X-CSRF-Token": csrf})
    assert other.get("/api/v1/auth/me").status_code == 401


def test_session_without_a_version_still_works_for_version_one(local):
    uid = auth.create_local_user("ada", PW)
    from fastapi import Request
    token = crypto.sign_session({"uid": uid, "username": "ada", "admin": True, "sid": "s"})  # pre-C6 token
    req = Request({"type": "http", "method": "GET", "headers": [(b"cookie", f"{auth.SESSION_COOKIE}={token}".encode())]})
    assert auth.current_user(req)["id"] == uid


def test_migration_14_applies_to_a_pre_migration_database():
    with db.session() as conn:
        conn.execute("DROP TABLE users")
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT, display_name TEXT, "
                     "password_hash TEXT, auth_source TEXT NOT NULL DEFAULT 'local', is_admin INTEGER NOT NULL DEFAULT 0, "
                     "created_at REAL NOT NULL, updated_at REAL NOT NULL, last_login_at REAL)")
        conn.execute("INSERT INTO users(id,username,created_at,updated_at) VALUES ('u','old',1,1)")
        conn.commit()
        step = next(fn for v, _, fn in migrations.MIGRATIONS if v == 14)
        assert step(conn)["columns_added"] == 2
        assert step(conn)["already_applied"] == 1
        row = conn.execute("SELECT session_version, auth_subject FROM users").fetchone()
        assert row[0] == 1 and row[1] is None


# ---------------- C7 ----------------
def _owned_ebook(tmp_path, root, link_to=None):
    work = migration._find_or_create_stub({"title": f"Book {root.parent.name}", "author": "A", "identifiers": {}})
    root.mkdir(parents=True, exist_ok=True)
    path = root / "book.epub"
    if link_to:
        path.symlink_to(link_to)
    else:
        path.write_bytes(b"epub")
    asset_id, fid, t = db.uid(), db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,'ebook','adopt',?,?)",
                     (db.uid(), "Books", str(root), t, t))
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','epub','owned',1,?,?)",
                     (asset_id, work, t, t))
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)",
                     (fid, asset_id, str(path), 4, t))
    return work, asset_id, fid


def test_kobo_and_kindle_refuse_a_symlink_that_escapes_the_root(tmp_path):
    outside = tmp_path / "secret.epub"
    outside.write_bytes(b"not for you")
    work, asset, _ = _owned_ebook(tmp_path, tmp_path / "lib", link_to=outside)
    with pytest.raises(ValueError, match="outside the library roots"):
        devices.kepub_for_asset(work, asset)
    devices.set_smtp_config(host="smtp.example.com", port=587, username="", password=None, from_email="me@example.com")
    with pytest.raises(ValueError, match="outside the library roots"):
        devices.send_to_kindle(work, asset, "kid@kindle.com")


def test_opds_file_refuses_an_escaping_symlink_and_serves_a_normal_file(tmp_path):
    outside = tmp_path / "secret.epub"
    outside.write_bytes(b"not for you")
    _, _, bad = _owned_ebook(tmp_path / "a", tmp_path / "a" / "lib", link_to=outside)
    c = TestClient(app)
    assert c.get(f"/opds/files/{bad}").status_code == 403
    _, _, good = _owned_ebook(tmp_path / "b", tmp_path / "b" / "lib")
    r = c.get(f"/opds/files/{good}")
    assert r.status_code == 200 and r.content == b"epub"


def _kids_household(local):
    admin = TestClient(app)
    csrf = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": PW}).json()["csrf"]
    admin.post("/api/v1/users", json={"username": "junior", "password": "junior-long-password", "access_scope": "kids"},
               headers={"X-CSRF-Token": csrf})
    kid = TestClient(app, client=("198.51.100.2", 1))
    assert _login(kid, "junior", "junior-long-password").status_code == 200
    kids_work = migration._find_or_create_stub({"title": "Kids Book", "author": "A", "identifiers": {}})
    helpers.own(kids_work)
    tagging.add_tag(kids_work, tagging.kids_tag_id())
    adult = migration._find_or_create_stub({"title": "Adult Book", "author": "B", "identifiers": {}})
    helpers.own(adult)
    secret_tag = tagging.create_tag("surprise-party-gift")
    tagging.add_tag(adult, secret_tag)
    return admin, kid, kids_work


def test_kids_tag_list_only_shows_tags_on_visible_works(local):
    admin, kid, kids_work = _kids_household(local)
    names = {t["name"] for t in kid.get("/api/v1/tags").json()["items"]}
    assert "surprise-party-gift" not in names and "kids" in names
    assert "surprise-party-gift" in {t["name"] for t in admin.get("/api/v1/tags").json()["items"]}
    kids_tag = next(t for t in kid.get("/api/v1/tags").json()["items"] if t["name"] == "kids")
    assert kids_tag["work_count"] == 1


def test_recommendations_force_needs_write_scope(local, monkeypatch):
    admin, kid, kids_work = _kids_household(local)
    monkeypatch.setattr(main_module.recommendations, "recommend_for_work", lambda *a, **k: {"items": [], "forced": k.get("force")})
    # Kids account: normal read fine, force refused.
    assert kid.get(f"/api/v1/library/{kids_work}/recommendations").status_code == 200
    assert kid.get(f"/api/v1/library/{kids_work}/recommendations?force=true").status_code == 403
    # Read-only API key: refused; write key and a full session: allowed.
    uid = db.one("SELECT id FROM users WHERE username='ada'")["id"]
    _, ro = auth.create_api_key(uid, "ro", ["read"])
    _, rw = auth.create_api_key(uid, "rw", ["write"])
    url = f"/api/v1/library/{kids_work}/recommendations?force=true"
    plain = TestClient(app, client=("198.51.100.9", 1))
    assert plain.get(url, headers={"Authorization": f"Bearer {ro}"}).status_code == 403
    assert plain.get(f"/api/v1/library/{kids_work}/recommendations", headers={"Authorization": f"Bearer {ro}"}).status_code == 200
    assert plain.get(url, headers={"Authorization": f"Bearer {rw}"}).status_code == 200
    assert admin.get(url).status_code == 200


def test_opds1_feed_is_well_formed_and_escapes_metadata(local, clock, monkeypatch):
    from xml.etree import ElementTree as ET
    auth.create_local_user("ada", PW)
    pub = {"metadata": {"identifier": "i<d>", "title": 'T</title><x a="1">&', "author": [{"name": "A&<B>"}]},
           "links": [{"href": "http://h/f?a=1&b=<2>", "type": 'x"y'}]}
    monkeypatch.setattr(main_module, "_opds_publications",
                        lambda *a, **k: ([pub], 50, 100, []))
    r = TestClient(app).get("/opds/v1/catalog.xml?limit=50", headers=_basic("ada", PW))
    assert r.status_code == 200
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.content)
    entry = root.find("a:entry", ns)
    assert entry.find("a:title", ns).text == pub["metadata"]["title"]
    assert entry.find("a:author/a:name", ns).text == "A&<B>"
    assert entry.find("a:link", ns).attrib["href"] == "http://h/f?a=1&b=<2>"
    assert len(root.findall("a:entry", ns)) == 1
    nxt = [l for l in root.findall("a:link", ns) if l.get("rel") == "next"][0]
    assert nxt.get("href").endswith("?limit=50&offset=50")
