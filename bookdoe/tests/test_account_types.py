"""Account types (admin / adult / kid / pending) and the Management vs Player boundary.

Phase 1 of the Player/Management work. Three things are pinned here:

1. One derivation: `auth.account_type` reads the stored is_admin/access_scope pair, and
   `columns_for_account_type` is its inverse, so no route ever reasons about the raw
   columns.
2. Default deny: a pending identity (authenticated by Authentik, placed by nobody) can
   see who it is and sign out, nothing else. An adult reaches no Management API and
   cannot load /manage; an admin reaches both shells.
3. Authentik group mapping: groups decide the type of a NEW account (least privilege,
   `pending` when unmapped), optionally govern existing accounts
   (BOOKDOE_OIDC_ROLE_SOURCE=groups) with the last-admin guard, and contradictory
   claims resolve to the most restrictive reading. Every change is a history event.
"""
import dataclasses

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import migration

import helpers

app = main_module.app


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


def _login(username, password):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c, r.json()["csrf"]


@pytest.fixture
def household(local_mode):
    """admin (ada), adult (jane), kid (owen) and a pending identity (newbie), each with a
    live session, plus one owned work on the kids shelf and one off it."""
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        admin_csrf = r.json()["csrf"]
        h = {"X-CSRF-Token": admin_csrf}
        for name, kind in (("jane", "adult"), ("owen", "kid"), ("newbie", "pending")):
            r = admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["account_type"] == kind
        kids_work = migration._find_or_create_stub({"title": "Kids Book", "author": "Author A", "identifiers": {}})
        helpers.own(kids_work)
        from app.services import tagging
        tagging.add_tag(kids_work, tagging.kids_tag_id())
        adult_work = migration._find_or_create_stub({"title": "Adult Book", "author": "Author B", "identifiers": {}})
        helpers.own(adult_work)
        adult, adult_csrf = _login("jane", "a-very-long-jane-password")
        kid, kid_csrf = _login("owen", "a-very-long-owen-password")
        pending, pending_csrf = _login("newbie", "a-very-long-newbie-password")
        yield {
            "admin": admin, "admin_csrf": admin_csrf, "adult": adult, "adult_csrf": adult_csrf,
            "kid": kid, "kid_csrf": kid_csrf, "pending": pending, "pending_csrf": pending_csrf,
            "kids_work": kids_work, "adult_work": adult_work,
        }


# ---------- derivation ----------
@pytest.mark.parametrize("row,expected", [
    ({"is_admin": 1, "access_scope": "full"}, "admin"),
    ({"is_admin": 0, "access_scope": "full"}, "adult"),
    ({"is_admin": 0, "access_scope": None}, "adult"),   # rows created before access_scope existed
    ({"is_admin": 0, "access_scope": "kids"}, "kid"),
    ({"is_admin": 0, "access_scope": "pending"}, "pending"),
    (None, "pending"),
])
def test_account_type_is_derived_from_the_stored_columns(row, expected):
    assert auth_module.account_type(row) == expected


@pytest.mark.parametrize("kind", auth_module.ACCOUNT_TYPES)
def test_columns_for_account_type_round_trips(kind):
    is_admin, scope = auth_module.columns_for_account_type(kind)
    assert auth_module.account_type({"is_admin": is_admin, "access_scope": scope}) == kind


def test_unknown_account_type_is_refused():
    with pytest.raises(ValueError):
        auth_module.columns_for_account_type("superuser")


# ---------- /auth/me reports the type ----------
def test_me_reports_account_type_and_management_flag(household):
    for who, kind, mgmt in (("admin", "admin", True), ("adult", "adult", False), ("kid", "kid", False), ("pending", "pending", False)):
        me = household[who].get("/api/v1/auth/me").json()
        assert me["authenticated"] is True
        assert me["user"]["account_type"] == kind, who
        assert me["user"]["management"] is mgmt, who


# ---------- pending: default deny ----------
@pytest.mark.parametrize("path", ["/api/v1/library", "/api/v1/authors", "/api/v1/series", "/api/v1/tags", "/api/v1/dashboard", "/api/v1/users"])
def test_pending_account_is_refused_everywhere_with_content(household, path):
    r = household["pending"].get(path)
    assert r.status_code == 403, f"{path} -> {r.status_code}"
    assert r.json()["detail"] == auth_module.PENDING_DETAIL


def test_pending_account_cannot_read_a_book_it_knows_the_id_of(household):
    for path in (f"/api/v1/library/{household['kids_work']}", f"/api/v1/library/{household['kids_work']}/cover"):
        assert household["pending"].get(path).status_code == 403


def test_pending_account_can_sign_out(household):
    r = household["pending"].post("/api/v1/auth/logout", json={}, headers={"X-CSRF-Token": household["pending_csrf"]})
    assert r.status_code == 200
    assert household["pending"].get("/api/v1/auth/me").status_code == 401


def test_pending_account_cannot_change_its_own_settings(household):
    r = household["pending"].patch("/api/v1/auth/me", json={"kindle_email": "x@y.z"}, headers={"X-CSRF-Token": household["pending_csrf"]})
    assert r.status_code == 403


# ---------- adult: Player yes, Management no ----------
def test_adult_sees_the_whole_library(household):
    titles = {w["title"] for w in household["adult"].get("/api/v1/library").json()["items"]}
    assert titles == {"Kids Book", "Adult Book"}


MANAGEMENT_GETS = [
    "/api/v1/dashboard", "/api/v1/wanted", "/api/v1/review", "/api/v1/history", "/api/v1/calendar", "/api/v1/roots",
    "/api/v1/indexers", "/api/v1/download-clients", "/api/v1/path-mappings", "/api/v1/queue", "/api/v1/settings/import",
    "/api/v1/naming", "/api/v1/profiles", "/api/v1/system/health", "/api/v1/jobs", "/api/v1/failures", "/api/v1/users",
    "/api/v1/discover?q=x", "/api/v1/search?q=x", "/api/v1/search/federated?q=x", "/api/v1/search-candidates",
    "/api/v1/scan-candidates", "/api/v1/migrations", "/api/v1/settings/devices", "/api/v1/settings/acquisition",
    "/api/v1/system/backups",
]


@pytest.mark.parametrize("path", MANAGEMENT_GETS)
def test_adult_is_refused_every_management_api(household, path):
    r = household["adult"].get(path)
    assert r.status_code == 403, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("path", MANAGEMENT_GETS)
def test_kid_is_refused_every_management_api(household, path):
    assert household["kid"].get(path).status_code == 403, path


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/v1/library", {"metadata_id": "x"}),
    ("post", "/api/v1/library/{w}/refresh", {}),
    ("patch", "/api/v1/library/{w}", {"title": "Hacked"}),
    ("patch", "/api/v1/library/{w}/monitor", {"monitored": True}),
    ("post", "/api/v1/library/{w}/tags", {"tag_id": "x"}),
    ("post", "/api/v1/library/{w}/search/ebook", {}),
    ("post", "/api/v1/releases/x/grab", {}),
    ("post", "/api/v1/roots", {"name": "x", "path": "/x", "media_kind": "ebook"}),
    ("post", "/api/v1/users", {"username": "evil", "password": "a-very-long-evil-password", "account_type": "admin"}),
    ("put", "/api/v1/settings/acquisition", {"auto_grab_min_score": 1}),
    ("put", "/api/v1/settings/acquisition", {"wanted_search_daily_cap": 5000}),
    ("post", "/api/v1/wanted/search-now", {}),
])
def test_adult_cannot_mutate_library_or_system_state(household, method, path, body):
    r = getattr(household["adult"], method)(path.format(w=household["adult_work"]), json=body, headers={"X-CSRF-Token": household["adult_csrf"]})
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


def test_adult_keeps_personal_surfaces(household):
    h = {"X-CSRF-Token": household["adult_csrf"]}
    w = household["adult_work"]
    assert household["adult"].put(f"/api/v1/library/{w}/rating", json={"rating": 4}, headers=h).status_code == 200
    assert household["adult"].put(f"/api/v1/library/{w}/read-state", json={"status": "reading"}, headers=h).status_code == 200
    assert household["adult"].get(f"/api/v1/library/{w}/progress/does-not-exist").status_code == 404
    assert household["adult"].get("/api/v1/authors").status_code == 200
    assert household["adult"].get("/api/v1/series").status_code == 200


# ---------- shells ----------
def test_forbidden_management_page_carries_the_configured_product_name(household, monkeypatch):
    import dataclasses
    from app import main as main_module
    r = household["kid"].get("/manage")
    assert r.status_code == 403 and "<title>bookdoe</title>" in r.text and "bookdoe-mark.svg" in r.text
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(main_module.settings, app_name="Acme Reads"))
    assert "<title>Acme Reads</title>" in household["kid"].get("/manage").text


def test_management_shell_is_served_only_to_administrators(household):
    assert household["admin"].get("/manage").status_code == 200
    assert "app.js" in household["admin"].get("/manage/settings").text
    for who in ("adult", "kid", "pending"):
        r = household[who].get("/manage")
        assert r.status_code == 403, who
        assert "Management is for administrators" in r.text
        assert "app.js" not in r.text, "the forbidden page must not ship the Management bundle"
        assert household[who].get("/manage/library/x").status_code == 403


def test_player_shell_is_served_to_every_account_and_to_nobody(household):
    for who in ("admin", "adult", "kid", "pending"):
        for path in ("/", "/app", "/app/library/x"):
            r = household[who].get(path)
            assert r.status_code == 200 and "player.js" in r.text, (who, path)
    with TestClient(app) as anon:
        assert "player.js" in anon.get("/app").text
        # Signed out: the Management shell loads so its sign-in screen can run; the
        # API behind it still refuses everything.
        assert anon.get("/manage").status_code == 200
        assert anon.get("/api/v1/dashboard").status_code == 401


def test_api_typos_stay_404_in_both_shells(household):
    assert household["admin"].get("/api/v1/nope").status_code == 404
    assert household["admin"].get("/auth/nope").status_code == 404


# ---------- user administration ----------
def test_admin_changes_a_type_and_the_change_is_audited_and_ends_sessions(household):
    h = {"X-CSRF-Token": household["admin_csrf"]}
    users = {u["username"]: u for u in household["admin"].get("/api/v1/users").json()["items"]}
    assert users["newbie"]["account_type"] == "pending"
    r = household["admin"].patch(f"/api/v1/users/{users['newbie']['id']}", json={"account_type": "adult"}, headers=h)
    assert r.status_code == 200 and r.json()["account_type"] == "adult"
    # The pending session is still valid (an upgrade does not revoke) and now sees the library.
    assert household["pending"].get("/api/v1/library").status_code == 200
    # Demoting the adult to kid revokes her session.
    r = household["admin"].patch(f"/api/v1/users/{users['jane']['id']}", json={"account_type": "kid"}, headers=h)
    assert r.status_code == 200
    assert household["adult"].get("/api/v1/auth/me").status_code == 401
    events = [h_["message"] for h_ in db.all("SELECT message FROM history WHERE event_type='auth.account_type_changed'")]
    assert any("newbie" in m and "pending -> adult" in m for m in events)
    assert any("jane" in m and "adult -> kid" in m for m in events)


def test_admin_cannot_make_a_kid_or_pending_account_an_administrator_by_mixing_fields(household):
    h = {"X-CSRF-Token": household["admin_csrf"]}
    owen = next(u for u in household["admin"].get("/api/v1/users").json()["items"] if u["username"] == "owen")
    r = household["admin"].patch(f"/api/v1/users/{owen['id']}", json={"is_admin": True}, headers=h)
    assert r.status_code == 400


def test_adult_cannot_touch_user_administration(household):
    h = {"X-CSRF-Token": household["adult_csrf"]}
    me = household["adult"].get("/api/v1/auth/me").json()["user"]
    assert household["adult"].patch(f"/api/v1/users/{me['id']}", json={"account_type": "admin"}, headers=h).status_code == 403
    assert household["adult"].get("/api/v1/users").status_code == 403


# ---------- Authentik group mapping ----------
@pytest.fixture
def oidc_settings(monkeypatch):
    def _set(**kw):
        repl = dataclasses.replace(base_settings, auth_mode="oidc", **kw)
        monkeypatch.setattr(auth_module, "settings", repl)
        monkeypatch.setattr(main_module, "settings", repl)
        return repl
    return _set


def _signin(username, groups, subject=None, **kw):
    return auth_module._upsert_external_user(username, f"{username}@example.com", source="oidc", subject=subject or f"sub-{username}", groups=groups, **kw)


@pytest.mark.parametrize("groups,expected", [
    (["bookdoe-admin"], ("admin", False)),
    (["bookdoe-adult"], ("adult", False)),
    (["bookdoe-kid"], ("kid", False)),
    (["bookdoe-admin", "bookdoe-adult"], ("admin", False)),
    (["bookdoe-kid", "bookdoe-adult"], ("pending", True)),
    (["bookdoe-kid", "bookdoe-admin"], ("pending", True)),
    (["something-else"], (None, False)),
    ([], (None, False)),
    (None, (None, False)),
    ("bookdoe-kid", ("kid", False)),      # a bare string claim is tolerated
    ({"not": "a list"}, (None, False)),
])
def test_role_from_groups(oidc_settings, groups, expected):
    oidc_settings()
    assert auth_module.role_from_groups(groups) == expected


def test_new_identities_are_provisioned_by_group_and_default_to_pending(oidc_settings):
    oidc_settings()
    auth_module.create_local_user("ada", "a-very-long-admin-password", is_admin=True)  # not an empty install
    assert auth_module.account_type(_signin("mum", ["bookdoe-adult"])) == "adult"
    assert auth_module.account_type(_signin("owen", ["bookdoe-kid"])) == "kid"
    assert auth_module.account_type(_signin("stranger", ["other"])) == "pending"
    assert auth_module.account_type(_signin("noclaim", None)) == "pending"
    assert auth_module.account_type(_signin("confused", ["bookdoe-kid", "bookdoe-admin"])) == "pending"
    kinds = {r["message"].split(" as ")[1].split(" ")[0] for r in db.all("SELECT message FROM history WHERE event_type='auth.account_provisioned'")}
    assert kinds == {"adult", "kid", "pending"}


def test_first_identity_on_an_empty_install_is_still_the_admin_unless_bootstrap_admins_are_named(oidc_settings):
    oidc_settings()
    assert auth_module.account_type(_signin("ada", None)) == "admin"


def test_bootstrap_admins_become_admin_and_turn_off_the_first_user_rule(oidc_settings):
    oidc_settings(oidc_bootstrap_admins="ada, sub-wife")
    assert auth_module.account_type(_signin("random", None)) == "pending", "with a bootstrap list, first-comer is not admin"
    assert auth_module.account_type(_signin("ada", None)) == "admin"
    assert auth_module.account_type(_signin("wife", ["bookdoe-kid"], subject="sub-wife")) == "admin", "bootstrap by sub wins over the claim"


def test_provision_mode_leaves_existing_accounts_alone(oidc_settings):
    oidc_settings(oidc_role_source="provision")
    first = _signin("owen", ["bookdoe-kid"])
    assert auth_module.account_type(first) == "kid"
    again = _signin("owen", ["bookdoe-admin"])
    assert again["id"] == first["id"]
    assert auth_module.account_type(again) == "kid", "a later claim must not change what the admin set"


def test_groups_mode_follows_the_claim_with_audit_and_session_revocation(oidc_settings):
    oidc_settings(oidc_role_source="groups")
    auth_module.create_local_user("ada", "a-very-long-admin-password", is_admin=True)
    first = _signin("jane", ["bookdoe-adult"])
    sv = first["session_version"]
    demoted = _signin("jane", ["bookdoe-kid"])
    assert auth_module.account_type(demoted) == "kid"
    assert demoted["session_version"] == sv + 1, "a demotion must end outstanding sessions"
    promoted = _signin("jane", ["bookdoe-adult"])
    assert auth_module.account_type(promoted) == "adult"
    assert promoted["session_version"] == sv + 1, "an upgrade does not revoke"
    changes = db.all("SELECT message FROM history WHERE event_type='auth.account_type_changed'")
    assert len(changes) == 2 and all("groups claim" in c["message"] for c in changes)


def test_groups_mode_never_acts_on_a_missing_or_unmapped_claim(oidc_settings):
    oidc_settings(oidc_role_source="groups")
    auth_module.create_local_user("ada", "a-very-long-admin-password", is_admin=True)
    jane = _signin("jane", ["bookdoe-adult"])
    assert auth_module.account_type(_signin("jane", None)) == "adult"
    assert auth_module.account_type(_signin("jane", ["unrelated"])) == "adult"
    assert auth_module.account_type(_signin("jane", ["bookdoe-kid", "bookdoe-adult"])) == "adult", "contradiction: unchanged"
    kinds = {r["event_type"] for r in db.all("SELECT event_type FROM history WHERE event_type LIKE 'auth.role_claim_%'")}
    assert kinds == {"auth.role_claim_ignored", "auth.role_claim_inconsistent"}
    assert db.one("SELECT session_version FROM users WHERE id=?", (jane["id"],))["session_version"] == jane["session_version"]


def test_groups_mode_never_demotes_the_last_administrator(oidc_settings):
    oidc_settings(oidc_role_source="groups")
    ada = _signin("ada", ["bookdoe-admin"])
    assert auth_module.account_type(ada) == "admin"
    still = _signin("ada", ["bookdoe-kid"])
    assert auth_module.account_type(still) == "admin"
    assert db.one("SELECT 1 FROM history WHERE event_type='auth.role_claim_refused'")
    # With a second admin present the demotion goes through.
    auth_module.create_local_user("backup", "a-very-long-admin-password", is_admin=True)
    assert auth_module.account_type(_signin("ada", ["bookdoe-adult"])) == "adult"


def test_allowed_groups_gate_still_applies_before_mapping(oidc_settings):
    oidc_settings(oidc_allowed_groups="family")
    auth_module.create_local_user("ada", "a-very-long-admin-password", is_admin=True)
    with pytest.raises(HTTPException) as e:
        _signin("stranger", ["bookdoe-admin"])
    assert e.value.status_code == 403
    assert auth_module.account_type(_signin("member", ["family", "bookdoe-adult"])) == "adult"


def test_claims_only_matter_when_they_come_from_the_validated_token(oidc_settings, household):
    """A client cannot assert a role: /auth/me ignores any body or header, and the
    users API refuses non-admins. The only path from a claim to a type is the OIDC
    callback, after signature, issuer, audience and nonce checks."""
    r = household["kid"].get("/api/v1/auth/me", headers={"X-bookdoe-Account-Type": "admin", "groups": "bookdoe-admin"})
    assert r.json()["user"]["account_type"] == "kid"


# ---------- next-path safety for the OIDC return ----------
@pytest.mark.parametrize("value,expected", [
    ("/manage", "/manage"), ("/app#home", "/app#home"), (None, "/"), ("", "/"),
    ("//evil.example", "/"), ("https://evil.example/", "/"), ("/\\evil", "/"), ("/" + "a" * 600, "/"),
])
def test_safe_next_path(value, expected):
    assert auth_module.safe_next_path(value) == expected


def test_pairing_code_is_accepted_without_the_dash_and_in_any_case():
    """The person may type the code however it reads off the device: with or without the dash, any case."""
    from app import auth
    assert auth.normalize_user_code("ghkm-7t4p") == "GHKM-7T4P"
    assert auth.normalize_user_code("GHKM7T4P") == "GHKM-7T4P"
    assert auth.normalize_user_code(" ghkm 7t4p ") == "GHKM-7T4P"
