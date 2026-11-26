"""Upgrading an existing install must never lock the administrator out.

The pre-change user rows have only `is_admin` and `access_scope` ('full'/'kids');
`account_type` derives from them with no migration: is_admin=1 -> admin,
'full' -> adult, 'kids' -> kid. Nobody existing becomes `pending` (that value is only
ever written by OIDC provisioning of a NEW identity). An existing administrator
keeps Management across the schema upgrade, across an OIDC sign-in that carries no
groups claim at all (both role-source modes), and is protected by the last-admin guard.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import migrations
from app.config import settings as base_settings

app = main_module.app
PW = "a-very-long-password-123"


def build_pre_change_install():
    """Rows exactly as production stored them before this branch (local admin linked to
    Authentik by sub, two local household accounts, one 'kids'), then the new tables
    dropped and their migration rows removed, so init_db runs 17-20 as an upgrade."""
    t = db.now()
    from app.crypto import hash_password
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,email,password_hash,auth_source,auth_subject,is_admin,access_scope,created_at,updated_at) VALUES ('u-ada','ada','ada@example.com',?, 'local','ada-authentik-sub',1,'full',?,?)", (hash_password(PW), t, t))
        conn.execute("INSERT INTO users(id,username,password_hash,auth_source,is_admin,access_scope,created_at,updated_at) VALUES ('u-wife','wife',?, 'local',0,'full',?,?)", (hash_password(PW), t, t))
        conn.execute("INSERT INTO users(id,username,password_hash,auth_source,is_admin,access_scope,created_at,updated_at) VALUES ('u-owen','owen',?, 'local',0,'kids',?,?)", (hash_password(PW), t, t))
        conn.execute("INSERT INTO users(id,username,auth_source,auth_subject,is_admin,access_scope,created_at,updated_at) VALUES ('u-oidc','oidcadmin','oidc','oidc-sub-2',1,'full',?,?)", (t, t))
        for table in ("kids_decisions", "device_codes", "devices", "approver_assignments", "requests", "tag_grants", "content_grants",
                      "user_preferences", "favorites", "bookmarks"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DELETE FROM migration_steps WHERE version>=17")


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_upgrade_keeps_types_and_nobody_falls_into_pending():
    build_pre_change_install()
    assert {r["version"] for r in db.all("SELECT version FROM migration_steps")} == set(range(1, 17))
    db.init_db()  # the upgrade: migrations 17-21
    assert {r["version"] for r in db.all("SELECT version FROM migration_steps")} == {v for v, _, _ in migrations.MIGRATIONS}  # every registered step, whatever main adds later
    types = {u["username"]: auth_module.account_type(u) for u in db.all("SELECT * FROM users")}
    assert types == {"ada": "admin", "wife": "adult", "owen": "kid", "oidcadmin": "admin"}
    assert db.one("SELECT COUNT(*) n FROM users WHERE access_scope='pending'")["n"] == 0


def test_existing_admin_keeps_management_after_upgrade(local_mode):
    build_pre_change_install()
    db.init_db()
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "ada", "password": PW})
        assert r.status_code == 200
        me = c.get("/api/v1/auth/me").json()["user"]
        assert me["account_type"] == "admin" and me["management"] is True
        assert c.get("/manage").status_code == 200 and "app.js" in c.get("/manage").text
        for path in ("/api/v1/dashboard", "/api/v1/users", "/api/v1/requests", "/api/v1/devices", "/api/v1/settings/requests"):
            assert c.get(path).status_code == 200, path
        w = TestClient(app); w.post("/api/v1/auth/login", json={"username": "wife", "password": PW})
        assert w.get("/api/v1/library").status_code == 200 and w.get("/api/v1/dashboard").status_code == 403
        o = TestClient(app); o.post("/api/v1/auth/login", json={"username": "owen", "password": PW})
        assert o.get("/api/v1/library").status_code == 200 and o.get("/manage").status_code == 403


@pytest.fixture
def oidc(monkeypatch):
    def _set(**kw):
        repl = dataclasses.replace(base_settings, auth_mode="oidc", **kw)
        monkeypatch.setattr(auth_module, "settings", repl)
        monkeypatch.setattr(main_module, "settings", repl)
    return _set


@pytest.mark.parametrize("role_source", ["provision", "groups"])
@pytest.mark.parametrize("groups", [None, [], ["unrelated-group"]])
def test_existing_admin_signing_in_via_oidc_without_a_mapped_group_stays_admin(oidc, role_source, groups):
    oidc(oidc_role_source=role_source)
    build_pre_change_install()
    db.init_db()
    # Matched on the stored sub (the way every production sign-in resolves).
    row = auth_module._upsert_external_user("ada", "ada@example.com", source="oidc", subject="ada-authentik-sub", groups=groups)
    assert row["id"] == "u-ada" and auth_module.account_type(row) == "admin"
    assert row["session_version"] == 1, "no session was revoked"
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 4, "no duplicate account was created"


def test_groups_mode_cannot_demote_the_only_administrator_even_with_a_kid_claim(oidc):
    oidc(oidc_role_source="groups")
    build_pre_change_install()
    db.init_db()
    with db.tx() as conn:
        conn.execute("DELETE FROM users WHERE id='u-oidc'")  # ada is the only admin now
    row = auth_module._upsert_external_user("ada", None, source="oidc", subject="ada-authentik-sub", groups=["bookdoe-kid"])
    assert auth_module.account_type(row) == "admin"
    assert db.one("SELECT 1 FROM history WHERE event_type='auth.role_claim_refused'")


def test_default_role_source_is_provision_and_new_identities_land_pending(oidc):
    assert base_settings.oidc_role_source == "provision"
    oidc()
    build_pre_change_install()
    db.init_db()
    newcomer = auth_module._upsert_external_user("friend", "f@example.com", source="oidc", subject="friend-sub", groups=["bookdoe-admin"])
    assert auth_module.account_type(newcomer) == "admin", "provisioning honours a mapped group for a NEW identity"
    stranger = auth_module._upsert_external_user("stranger", None, source="oidc", subject="s-sub", groups=None)
    assert auth_module.account_type(stranger) == "pending"
    # And an existing adult who happens to be in the admin group is NOT promoted in provision mode.
    with db.tx() as conn:
        conn.execute("UPDATE users SET auth_subject='wife-sub' WHERE id='u-wife'")
    wife = auth_module._upsert_external_user("wife", None, source="oidc", subject="wife-sub", groups=["bookdoe-admin"])
    assert auth_module.account_type(wife) == "adult"
