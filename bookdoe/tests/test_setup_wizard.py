"""First-run setup: wizard API, page routing, env file writing, secret bootstrap."""
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app import db, secrets_provider, setup
from app.config import settings
from app.main import app


@pytest.fixture
def local_mode(monkeypatch, tmp_path):
    object.__setattr__(settings, "auth_mode", "local")
    monkeypatch.setenv(secrets_provider.ENV_FILE_VAR, str(tmp_path / "setup.env"))
    yield tmp_path / "setup.env"
    object.__setattr__(settings, "auth_mode", "none")


def _payload(tmp_path, **over):
    books = tmp_path / "books"
    books.mkdir(exist_ok=True)
    base = {"admin": {"username": "owner", "password": "a-long-password-123", "email": "o@example.com"},
            "public_url": "https://books.example.com/",
            "roots": [{"name": "Books", "path": str(books), "media_kind": "ebook", "management_mode": "adopt"}],
            "metadata": {"google_books_api_key": "gb-key-value"},
            "secrets": {"backend": "env"}}
    base.update(over)
    return base


# ---------- env file ----------
def test_write_env_file_merges_quotes_and_is_private(tmp_path):
    p = tmp_path / "setup.env"
    setup.write_env_file(p, {"A": "1", "B": "two words", "C": 'q"uote'})
    setup.write_env_file(p, {"A": None, "D": "4"})
    parsed = secrets_provider.parse_env_file(p.read_text())
    assert parsed == {"B": "two words", "C": 'q"uote', "D": "4"}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_ensure_secret_key_generates_once_and_respects_env(tmp_path, monkeypatch):
    p = tmp_path / "setup.env"
    monkeypatch.delenv("BOOKDOE_SECRET_KEY")
    original = settings.secret_key
    try:
        assert setup.ensure_secret_key(p) is True
        first = secrets_provider.parse_env_file(p.read_text())["BOOKDOE_SECRET_KEY"]
        assert len(first) == 64 and os.environ["BOOKDOE_SECRET_KEY"] == first and settings.secret_key == first
        monkeypatch.delenv("BOOKDOE_SECRET_KEY")
        assert setup.ensure_secret_key(p) is False  # the file already holds one
        assert os.environ["BOOKDOE_SECRET_KEY"] == first
        assert setup.ensure_secret_key(p) is False  # env holds one
    finally:
        object.__setattr__(settings, "secret_key", original)
        monkeypatch.setenv("BOOKDOE_SECRET_KEY", original)


# ---------- routing ----------
def test_setup_page_and_root_redirect_only_while_empty(local_mode):
    with TestClient(app) as c:
        assert c.get("/setup").status_code == 200
        assert b"Set up bookdoe" in c.get("/setup").content
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        assert c.get("/api/v1/setup/status").json()["setup_required"] is True
    db.one("SELECT 1")  # the suite's leak check wants no open handles; db.one closes its own


def test_setup_routes_close_after_first_admin(local_mode, tmp_path):
    with TestClient(app) as c:
        r = c.post("/api/v1/setup/complete", json=_payload(tmp_path))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["csrf"] and body["roots"][0]["status"] == "created"
        # The session cookie from completion is a signed-in administrator.
        assert c.get("/api/v1/auth/me").json()["user"]["account_type"] == "admin"
        assert c.get("/setup", follow_redirects=False).status_code == 303
        assert c.get("/", follow_redirects=False).status_code == 200
        assert c.post("/api/v1/setup/complete", json=_payload(tmp_path)).status_code == 403
        assert c.post("/api/v1/setup/check", json={}).status_code == 403
        assert c.get("/api/v1/setup/status").json()["setup_required"] is False  # admin session
    with TestClient(app) as anon:
        assert anon.get("/api/v1/setup/status").status_code in (401, 403)


def test_setup_is_not_offered_outside_local_mode(tmp_path):
    with TestClient(app) as c:  # auth_mode none in the suite
        assert c.get("/setup", follow_redirects=False).status_code == 303
        assert c.post("/api/v1/setup/complete", json=_payload(tmp_path)).status_code == 403


# ---------- apply ----------
def test_complete_writes_env_file_without_the_admin_password(local_mode, tmp_path):
    with TestClient(app) as c:
        r = c.post("/api/v1/setup/complete", json=_payload(tmp_path))
        assert r.status_code == 200, r.text
    text = local_mode.read_text()
    parsed = secrets_provider.parse_env_file(text)
    assert parsed["BOOKDOE_PUBLIC_URL"] == "https://books.example.com"
    assert parsed["BOOKDOE_GOOGLE_BOOKS_API_KEY"] == "gb-key-value"
    assert parsed["BOOKDOE_SECRETS_BACKEND"] == "env"
    assert "a-long-password-123" not in text
    summary = (settings.data_dir / setup.SETUP_SUMMARY).read_text()
    assert "gb-key-value" not in summary and "a-long-password-123" not in summary
    assert db.one("SELECT COUNT(*) n FROM root_folders")["n"] == 1
    assert db.one("SELECT 1 FROM history WHERE event_type='setup.completed'")


def test_apply_is_idempotent_for_roots_and_never_makes_a_second_admin(local_mode, tmp_path):
    req = setup.SetupRequest(**_payload(tmp_path))
    first = setup.apply(req)
    second = setup.apply(req)
    assert first["admin_user_id"] and second["admin_user_id"] is None
    assert second["roots"][0]["status"] == "exists"
    assert db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM root_folders")["n"] == 1


def test_infisical_choice_is_persisted_and_env_choice_removes_it(local_mode, tmp_path):
    inf = {"backend": "infisical", "infisical": {"site_url": "https://inf.example.com", "client_id": "cid", "client_secret": "csec-value",
                                                  "project_id": "p1", "environment": "prod", "secret_path": "/"}}
    setup.apply(setup.SetupRequest(**_payload(tmp_path, secrets=inf)))
    parsed = secrets_provider.parse_env_file(local_mode.read_text())
    assert parsed["BOOKDOE_SECRETS_BACKEND"] == "infisical" and parsed["INFISICAL_CLIENT_SECRET"] == "csec-value"
    setup.apply(setup.SetupRequest(**_payload(tmp_path)))
    parsed = secrets_provider.parse_env_file(local_mode.read_text())
    assert parsed["BOOKDOE_SECRETS_BACKEND"] == "env" and "INFISICAL_CLIENT_SECRET" not in parsed


def test_rejected_root_is_reported_not_fatal(local_mode, tmp_path):
    bad = {"name": "Nope", "path": str(tmp_path / "missing" / ".." / "x"), "media_kind": "ebook"}
    payload = _payload(tmp_path, roots=[bad, {"name": "Config", "path": str(settings.data_dir), "media_kind": "ebook"}])
    result = setup.apply(setup.SetupRequest(**payload))
    assert {r["status"] for r in result["roots"]} == {"rejected"}
    assert result["admin_user_id"]


# ---------- checks ----------
def test_checks_report_paths_openreads_and_secrets(local_mode, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "check_openreads", lambda url: {"ok": False, "url": url or "x", "error": "OpenReads not reachable (ConnectError)"})
    books = tmp_path / "books"; books.mkdir()
    req = setup.SetupCheckRequest(roots=[{"name": "B", "path": str(books)}, {"name": "Rel", "path": "relative/path"},
                                         {"name": "Gone", "path": str(tmp_path / "gone")}],
                                  secrets={"backend": "infisical", "infisical": {"client_id": "c", "client_secret": "s", "project_id": "p"}})
    out = setup.run_checks(req)
    assert [p["ok"] for p in out["paths"]] == [True, False, False]
    assert "absolute" in out["paths"][1]["error"] and "exist" in out["paths"][2]["error"]
    assert out["openreads"]["ok"] is False
    assert out["secrets"]["backend"] == "infisical" and out["secrets"]["ok"] is False  # no real server
    assert "s" != out["secrets"].get("error")  # message, not a value
    assert out["ok"] is False
    with TestClient(app) as c:
        r = c.post("/api/v1/setup/check", json={"roots": [{"name": "B", "path": str(books)}]})
        assert r.status_code == 200 and r.json()["secrets"] == {"backend": "env", "ok": True, "env_file": str(local_mode)}


def test_managed_root_must_be_writable(tmp_path):
    ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
    try:
        if os.access(ro, os.W_OK):  # running as root: permission bits do not apply
            pytest.skip("cannot make a directory unwritable for this user")
        out = setup.check_path(setup.RootSpec(name="M", path=str(ro), management_mode="managed"))
        assert out["ok"] is False and "writable" in out["error"]
        assert setup.check_path(setup.RootSpec(name="A", path=str(ro), management_mode="adopt"))["ok"] is True
    finally:
        ro.chmod(0o700)


def test_management_add_root_uses_the_shared_validation(tmp_path):
    books = tmp_path / "b"; books.mkdir()
    with TestClient(app) as c:
        r = c.post("/api/v1/roots", json={"name": "B", "path": str(books), "media_kind": "ebook"})
        assert r.status_code == 200, r.text
        assert c.post("/api/v1/roots", json={"name": "B2", "path": str(books), "media_kind": "ebook"}).status_code == 409
        assert c.post("/api/v1/roots", json={"name": "R", "path": "rel", "media_kind": "ebook"}).status_code == 400


# ---------- acquisition steps follow the flag ----------
def test_wizard_has_no_acquisition_step_while_the_flag_is_off(local_mode, monkeypatch):
    monkeypatch.setenv("BOOKDOE_ACQUISITION_ENABLED", "0")
    with TestClient(app) as c:
        html = c.get("/setup").text
    low = html.lower()
    for word in ("indexer", "download client", "acquisition", "data-panel=\"acq\"", "<!--acq"):
        assert word not in low


def test_wizard_shows_the_acquisition_step_while_the_flag_is_on(local_mode, monkeypatch):
    monkeypatch.setenv("BOOKDOE_ACQUISITION_ENABLED", "1")
    with TestClient(app) as c:
        html = c.get("/setup").text
    assert 'data-panel="acq"' in html and 'data-step="acq"' in html
    assert "Indexer name" in html and "Download client name" in html
