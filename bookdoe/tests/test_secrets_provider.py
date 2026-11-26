"""Secrets provider: precedence (environment, env file, backend), reference schemes,
the Infisical client against a fake server, and failure messages that never carry
a value."""
import json

import httpx
import pytest

from app import secrets_provider as sp


# ---------- env file parsing and precedence ----------
def test_parse_env_file_handles_quotes_comments_and_export():
    text = """
# comment
export A=1
B="two words"   # not a comment inside quotes
C='single $literal'
D=plain # inline comment
E=
bad line without equals
9BAD=nope
F="line\\nbreak"
"""
    out = sp.parse_env_file(text)
    assert out == {"A": "1", "B": "two words", "C": "single $literal", "D": "plain", "E": "", "F": "line\nbreak"}


def test_env_file_never_overrides_process_environment(tmp_path):
    f = tmp_path / "setup.env"
    f.write_text("BOOKDOE_SECRET_KEY=from-file-value-0123456789\nONLY_IN_FILE=yes\n")
    env = {"BOOKDOE_SECRET_KEY": "from-env"}
    added = sp.load_env_file(f, environ=env)
    assert added == ["ONLY_IN_FILE"]
    assert env["BOOKDOE_SECRET_KEY"] == "from-env"
    assert env["ONLY_IN_FILE"] == "yes"


def test_missing_env_file_is_not_an_error(tmp_path):
    assert sp.load_env_file(tmp_path / "nope.env", environ={}) == []


def test_default_env_file_lives_in_the_data_dir(tmp_path):
    (tmp_path / "setup.env").write_text("X=1\n")
    env = {"BOOKDOE_DATA_DIR": str(tmp_path)}
    assert sp.load_env_file(environ=env) == ["X"]


def test_env_backend_is_the_default_and_needs_nothing():
    r = sp.SecretResolver({"BOOKDOE_SECRET_KEY": "abc"})
    assert r.backend_name == "env"
    assert r.backend() is None
    assert r.get("BOOKDOE_SECRET_KEY") == "abc"
    assert r.get("MISSING", "dflt") == "dflt"
    assert r.check() == {"backend": "env", "ok": True, "env_file": "/config/setup.env"}


def test_unknown_backend_is_a_clear_error():
    r = sp.SecretResolver({"BOOKDOE_SECRETS_BACKEND": "vault"})
    with pytest.raises(sp.SecretError, match="not a known backend"):
        r.get("ANY")
    assert r.check()["ok"] is False


# ---------- references ----------
def test_file_reference_reads_a_docker_secret(tmp_path):
    f = tmp_path / "key"
    f.write_text("file-held-value\n")
    r = sp.SecretResolver({"BOOKDOE_SECRET_KEY": f"file://{f}"})
    assert r.get("BOOKDOE_SECRET_KEY") == "file-held-value"


def test_file_reference_failure_names_the_variable_not_the_value(tmp_path):
    r = sp.SecretResolver({"BOOKDOE_SECRET_KEY": f"file://{tmp_path}/absent"})
    with pytest.raises(sp.SecretError) as exc:
        r.get("BOOKDOE_SECRET_KEY")
    assert "BOOKDOE_SECRET_KEY" in str(exc.value)


def test_env_reference_aliases_another_variable():
    r = sp.SecretResolver({"OPENREADS_API_KEY": "env://SHARED", "SHARED": "shared-value"})
    assert r.get("OPENREADS_API_KEY") == "shared-value"
    with pytest.raises(sp.SecretError, match="env://NOPE"):
        sp.SecretResolver({"K": "env://NOPE"}).get("K")


def test_infisical_reference_without_backend_is_refused():
    r = sp.SecretResolver({"BOOKDOE_SECRET_KEY": "infisical://SECRET_KEY"})
    with pytest.raises(sp.SecretError, match="BOOKDOE_SECRETS_BACKEND is not 'infisical'"):
        r.get("BOOKDOE_SECRET_KEY")


# ---------- Infisical against a fake server ----------
class FakeInfisical:
    """Just enough of the Infisical REST API: universal-auth login and raw secret GET."""

    def __init__(self, secrets: dict[tuple[str, str], str], *, client_id="cid-123456789", client_secret="csec", token_ttl=300):
        self.secrets = secrets  # (path, name) -> value
        self.client_id, self.client_secret, self.token_ttl = client_id, client_secret, token_ttl
        self.logins = 0
        self.gets = 0
        self.issued: list[str] = []
        self.revoked: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            body = json.loads(request.content)
            self.logins += 1
            if body != {"clientId": self.client_id, "clientSecret": self.client_secret}:
                return httpx.Response(401, json={"message": "bad identity"})
            tok = f"tok{self.logins}"
            self.issued.append(tok)
            return httpx.Response(200, json={"accessToken": tok, "expiresIn": self.token_ttl, "tokenType": "Bearer"})
        if request.url.path.startswith("/api/v3/secrets/raw/"):
            self.gets += 1
            auth = request.headers.get("authorization", "")
            tok = auth.removeprefix("Bearer ")
            if tok not in self.issued or tok in self.revoked:
                return httpx.Response(401, json={"message": "expired"})
            name = request.url.path.rsplit("/", 1)[1]
            params = dict(request.url.params)
            assert params["workspaceId"] == "proj-1" and params["environment"] == "prod"
            value = self.secrets.get((params["secretPath"], name))
            if value is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"secret": {"secretKey": name, "secretValue": value, "version": 1}})
        return httpx.Response(500)


def _provider(fake: FakeInfisical, env_extra=None, **kwargs) -> sp.InfisicalProvider:
    env = {"INFISICAL_CLIENT_ID": fake.client_id, "INFISICAL_CLIENT_SECRET": fake.client_secret, "INFISICAL_PROJECT_ID": "proj-1",
           "INFISICAL_SITE_URL": "https://infisical.test"}
    env.update(env_extra or {})
    factory = lambda: httpx.Client(base_url="https://infisical.test", transport=httpx.MockTransport(fake.handler))  # noqa: E731
    return sp.InfisicalProvider(env, client_factory=factory, **kwargs)


def test_infisical_requires_its_three_variables():
    with pytest.raises(sp.SecretError) as exc:
        sp.InfisicalProvider({"INFISICAL_CLIENT_ID": "x"})
    assert "INFISICAL_CLIENT_SECRET" in str(exc.value) and "INFISICAL_PROJECT_ID" in str(exc.value)
    assert "x" not in str(exc.value).replace("INFISICAL", "")  # the message carries names only


def test_infisical_logs_in_once_and_caches_secrets():
    fake = FakeInfisical({("/", "BOOKDOE_SECRET_KEY"): "the-key-value"})
    p = _provider(fake)
    assert p.get("BOOKDOE_SECRET_KEY") == "the-key-value"
    assert p.get("BOOKDOE_SECRET_KEY") == "the-key-value"
    assert fake.logins == 1 and fake.gets == 1
    assert p.get("ABSENT") is None


def test_infisical_token_is_renewed_after_expiry_and_on_401():
    clock = [0.0]
    fake = FakeInfisical({("/", "A"): "1", ("/", "B"): "2"}, token_ttl=100)
    p = _provider(fake, cache_ttl=0.0, clock=lambda: clock[0])
    assert p.get("A") == "1"
    clock[0] = 200.0  # past expiresIn - 30s
    assert p.get("B") == "2"
    assert fake.logins == 2
    fake.revoked.add(fake.issued[-1])  # server-side revocation -> 401 -> one re-login
    assert p.get("A") == "1"
    assert fake.logins == 3


def test_infisical_bad_identity_message_has_status_and_prefix_only():
    fake = FakeInfisical({}, client_secret="right")
    p = _provider(fake, env_extra={"INFISICAL_CLIENT_SECRET": "wrong"})
    with pytest.raises(sp.SecretError) as exc:
        p.get("ANY")
    msg = str(exc.value)
    assert "HTTP 401" in msg and fake.client_id[:8] in msg
    assert "wrong" not in msg and "right" not in msg
    assert p.check()["ok"] is False and "error" in p.check()


def test_infisical_unexpected_status_is_reported_without_values():
    def handler(request):
        if "login" in request.url.path:
            return httpx.Response(200, json={"accessToken": "t", "expiresIn": 300})
        return httpx.Response(503, text="upstream value-looking-text")
    env = {"INFISICAL_CLIENT_ID": "c", "INFISICAL_CLIENT_SECRET": "s", "INFISICAL_PROJECT_ID": "proj-1"}
    p = sp.InfisicalProvider(env, client_factory=lambda: httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler)))
    with pytest.raises(sp.SecretError, match="HTTP 503"):
        p.get("K")


def test_resolver_falls_back_to_infisical_only_when_env_is_empty():
    fake = FakeInfisical({("/", "OPENREADS_API_KEY"): "from-infisical", ("/", "BOOKDOE_SECRET_KEY"): "never-used"})
    env = {"BOOKDOE_SECRETS_BACKEND": "infisical", "BOOKDOE_SECRET_KEY": "from-env"}
    r = sp.SecretResolver(env, backend=_provider(fake))
    assert r.get("BOOKDOE_SECRET_KEY") == "from-env"
    assert r.get("OPENREADS_API_KEY") == "from-infisical"
    assert r.get("NOT_ANYWHERE", "d") == "d"
    assert r.check()["ok"] is True and r.check()["backend"] == "infisical"


def test_infisical_reference_with_folder_path():
    fake = FakeInfisical({("/bookdoe", "OIDC_SECRET"): "folder-value", ("/", "PLAIN"): "root-value"})
    env = {"BOOKDOE_SECRETS_BACKEND": "infisical",
           "BOOKDOE_OIDC_CLIENT_SECRET": "infisical:///bookdoe/OIDC_SECRET",
           "OPENREADS_API_KEY": "infisical://PLAIN",
           "X": "infisical://MISSING"}
    r = sp.SecretResolver(env, backend=_provider(fake))
    assert r.get("BOOKDOE_OIDC_CLIENT_SECRET") == "folder-value"
    assert r.get("OPENREADS_API_KEY") == "root-value"
    with pytest.raises(sp.SecretError, match="no secret named MISSING"):
        r.get("X")


def test_backend_factory_errors_surface_through_check_and_get():
    env = {"BOOKDOE_SECRETS_BACKEND": "infisical"}  # no INFISICAL_* at all
    r = sp.SecretResolver(env)
    with pytest.raises(sp.SecretError, match="INFISICAL_CLIENT_ID"):
        r.get("BOOKDOE_SECRET_KEY")
    assert r.check()["ok"] is False


# ---------- config integration ----------
def test_process_resolver_applies_references(monkeypatch):
    monkeypatch.setenv("BOOKDOE_OIDC_CLIENT_SECRET", "env://ALIAS")
    monkeypatch.setenv("ALIAS", "aliased-oidc-secret")
    sp.reset()
    try:
        assert sp.secret("BOOKDOE_OIDC_CLIENT_SECRET") == "aliased-oidc-secret"
        assert sp.is_reference("infisical://X") and not sp.is_reference("plain")
    finally:
        sp.reset()


def test_every_credential_setting_goes_through_the_provider():
    """config.py must not read a credential with os.getenv: that would bypass the env
    file and the backend for exactly the values they exist for."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("app", "config.py").read_text()
    for name in ("BOOKDOE_SECRET_KEY", "BOOKDOE_BOOTSTRAP_PASSWORD", "BOOKDOE_OIDC_CLIENT_SECRET", "OPENREADS_API_KEY",
                 "BOOKDOE_GOOGLE_BOOKS_API_KEY", "BOOKDOE_HARDCOVER_API_KEY", "BOOKDOE_AUDIOBOOKDB_API_KEY", "BOOKDOE_LLM_API_KEY"):
        assert f'_secret("{name}"' in src, name
        assert f'os.getenv("{name}"' not in src, name
