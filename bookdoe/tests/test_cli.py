"""`bookdoe` CLI: non-interactive setup, status, secret bootstrap and checks."""
import json

import pytest

from app import cli, db, secrets_provider, setup
from app.config import settings


@pytest.fixture
def local_mode(monkeypatch, tmp_path):
    object.__setattr__(settings, "auth_mode", "local")
    monkeypatch.setenv(secrets_provider.ENV_FILE_VAR, str(tmp_path / "setup.env"))
    monkeypatch.setattr(setup, "check_openreads", lambda url: {"ok": True, "url": url or "x"})
    yield tmp_path / "setup.env"
    object.__setattr__(settings, "auth_mode", "none")


def test_non_interactive_setup_needs_a_password_variable(local_mode, capsys):
    assert cli.main(["setup", "--non-interactive"]) == 2
    assert "admin-password-env" in capsys.readouterr().err


def test_non_interactive_setup_creates_admin_roots_and_env(local_mode, tmp_path, monkeypatch, capsys):
    books = tmp_path / "books"; books.mkdir()
    monkeypatch.setenv("ADMIN_PW", "cli-password-0123456")
    rc = cli.main(["setup", "--non-interactive", "--admin-user", "boss", "--admin-password-env", "ADMIN_PW",
                   "--public-url", "https://b.example.com", "--root", f"Books:{books}:ebook:adopt"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert db.one("SELECT username FROM users WHERE is_admin=1")["username"] == "boss"
    assert db.one("SELECT COUNT(*) n FROM root_folders")["n"] == 1
    parsed = secrets_provider.parse_env_file(local_mode.read_text())
    assert parsed["BOOKDOE_PUBLIC_URL"] == "https://b.example.com"
    assert "cli-password-0123456" not in out and "cli-password-0123456" not in local_mode.read_text()
    # Re-run: nothing duplicated, exit 0.
    assert cli.main(["setup", "--non-interactive", "--admin-password-env", "ADMIN_PW", "--root", f"Books:{books}:ebook"]) == 0
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_setup_refuses_when_a_check_fails_unless_forced(local_mode, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADMIN_PW", "cli-password-0123456")
    args = ["setup", "--non-interactive", "--admin-password-env", "ADMIN_PW", "--root", f"Gone:{tmp_path / 'gone'}:ebook"]
    assert cli.main(args) == 1
    assert "checks failed" in capsys.readouterr().err
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    assert cli.main(args + ["--force"]) == 0
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_bad_root_argument_is_a_usage_error(local_mode):
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--root", "only-a-name"])
    assert exc.value.code == 2


def test_status_and_secrets_check(local_mode, capsys, monkeypatch):
    assert cli.main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["setup_required"] is True and status["env_file"] == str(local_mode)
    assert cli.main(["secrets-check"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "env"
    monkeypatch.setenv("BOOKDOE_SECRETS_BACKEND", "infisical")
    try:
        assert cli.main(["secrets-check"]) == 1
        assert "INFISICAL_CLIENT_ID" in capsys.readouterr().out
    finally:
        monkeypatch.delenv("BOOKDOE_SECRETS_BACKEND")
        secrets_provider.reset()


def test_ensure_secret_command(local_mode, capsys, monkeypatch):
    original = settings.secret_key
    monkeypatch.delenv("BOOKDOE_SECRET_KEY")
    try:
        assert cli.main(["ensure-secret"]) == 0
        assert "generated" in capsys.readouterr().out
        assert "BOOKDOE_SECRET_KEY" in local_mode.read_text()
    finally:
        object.__setattr__(settings, "secret_key", original)
        monkeypatch.setenv("BOOKDOE_SECRET_KEY", original)
