"""Tests for linking an external (OIDC/proxy) identity to an existing account.

Defect found 2026-09-24: the OIDC callback 500'd with
`sqlite3.IntegrityError: UNIQUE constraint failed: users.username`.

_upsert_external_user() looked the user up by (username, auth_source) while the
schema declares `username TEXT UNIQUE` on its own. The documented rollout -- create a
local administrator, validate, then switch to OIDC -- guarantees the administrator
keeps the same username across both sources, so the lookup missed and the INSERT
collided. Every OIDC login failed.
"""
import pytest

from app import auth, db


def _local_user(username="ada", admin=True):
    uid = auth.create_local_user(username, "a-long-enough-password", email="ada@example.com", is_admin=admin)
    return db.one("SELECT * FROM users WHERE id=?", (uid,))


def test_oidc_login_adopts_the_existing_local_account():
    """The exact production failure."""
    local = _local_user()
    linked = auth._upsert_external_user("ada", "ada@authentik.example", source="oidc", display_name="Ada")
    assert linked["id"] == local["id"], "must reuse the existing row, not insert a duplicate"
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_admin_rights_survive_linking():
    _local_user(admin=True)
    linked = auth._upsert_external_user("ada", None, source="oidc")
    assert linked["is_admin"] == 1


def test_local_break_glass_still_works_after_linking():
    """auth_source must not be rewritten - login_local() filters on it."""
    _local_user()
    auth._upsert_external_user("ada", "new@example.com", source="oidc")
    row = db.one("SELECT * FROM users WHERE username='ada'")
    assert row["auth_source"] == "local", "rewriting auth_source would disable local login"
    assert row["password_hash"], "the local password must be preserved"
    assert auth.login_local("ada", "a-long-enough-password") is not None


def test_linking_is_recorded_in_history():
    _local_user()
    auth._upsert_external_user("ada", None, source="oidc")
    events = {r["event_type"] for r in db.all("SELECT event_type FROM history")}
    assert "auth.account_linked" in events


def test_email_and_display_name_are_updated_from_the_provider():
    _local_user()
    linked = auth._upsert_external_user("ada", "fresh@example.com", source="oidc", display_name="Ada S")
    assert linked["email"] == "fresh@example.com"
    assert linked["display_name"] == "Ada S"


def test_missing_provider_values_do_not_erase_existing_ones():
    _local_user()
    auth._upsert_external_user("ada", "first@example.com", source="oidc", display_name="Ada")
    linked = auth._upsert_external_user("ada", None, source="oidc", display_name=None)
    assert linked["email"] == "first@example.com"
    assert linked["display_name"] == "Ada"


def test_a_brand_new_external_user_is_created():
    created = auth._upsert_external_user("newcomer", "n@example.com", source="oidc")
    assert created["username"] == "newcomer"
    assert created["auth_source"] == "oidc"


def test_first_external_user_on_an_empty_install_becomes_admin():
    assert db.one("SELECT COUNT(*) n FROM users")["n"] == 0
    first = auth._upsert_external_user("firstie", None, source="oidc")
    assert first["is_admin"] == 1


def test_second_external_user_is_not_admin():
    auth._upsert_external_user("firstie", None, source="oidc")
    second = auth._upsert_external_user("second", None, source="oidc")
    assert second["is_admin"] == 0


def test_repeated_logins_do_not_duplicate_the_account():
    for _ in range(3):
        auth._upsert_external_user("ada", "k@example.com", source="oidc")
    assert db.one("SELECT COUNT(*) n FROM users WHERE username='ada'")["n"] == 1


def test_proxy_source_links_the_same_way():
    local = _local_user()
    linked = auth._upsert_external_user("ada", None, source="proxy")
    assert linked["id"] == local["id"]


# --- security review C4: match on sub, verified email, then first-link by username ---
def test_sub_match_wins_over_username():
    """The same person keeps their account when the IdP renames them, and a different
    person who now holds the old name does not get it."""
    local = _local_user("ada")
    auth._upsert_external_user("ada", None, source="oidc", subject="sub-ada")
    other = _local_user("ada2", admin=False)
    # IdP renames sub-ada to "ada2": the sub, not the name, picks the account.
    again = auth._upsert_external_user("ada2", None, source="oidc", subject="sub-ada")
    assert again["id"] == local["id"] and again["id"] != other["id"]


def test_a_different_sub_cannot_take_over_a_linked_username():
    from fastapi import HTTPException
    _local_user("ada")
    auth._upsert_external_user("ada", None, source="oidc", subject="sub-real")
    with pytest.raises(HTTPException) as e:
        auth._upsert_external_user("ada", None, source="oidc", subject="sub-impostor")
    assert e.value.status_code == 403


def test_unverified_email_does_not_link():
    local = _local_user("ada")  # email ada@example.com
    user = auth._upsert_external_user("stranger", "ada@example.com", source="oidc", subject="sub-x", email_verified=False)
    assert user["id"] != local["id"]
    assert user["is_admin"] == 0


def test_verified_email_links_to_the_matching_account():
    local = _local_user("ada")
    user = auth._upsert_external_user("different-name", "ada@example.com", source="oidc", subject="sub-y", email_verified=True)
    assert user["id"] == local["id"] and user["auth_subject"] == "sub-y"


def test_second_login_does_not_emit_a_second_account_linked():
    _local_user("ada")
    for _ in range(3):
        auth._upsert_external_user("ada", None, source="oidc", subject="sub-ada")
    n = db.one("SELECT COUNT(*) n FROM history WHERE event_type='auth.account_linked'")["n"]
    assert n == 1


def test_group_allow_list_refuses_a_stranger_but_not_a_member(monkeypatch):
    import dataclasses
    from fastapi import HTTPException
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, oidc_allowed_groups="family"))
    with pytest.raises(HTTPException) as e:
        auth._upsert_external_user("stranger", None, source="oidc", subject="s1", groups=["other"])
    assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        auth._upsert_external_user("stranger", None, source="oidc", subject="s1", groups=None)
    ok = auth._upsert_external_user("member", None, source="oidc", subject="s2", groups=["x", "family"])
    assert ok["username"] == "member"
    # An already-linked account is not locked out by a later change to the list.
    again = auth._upsert_external_user("member", None, source="oidc", subject="s2", groups=[])
    assert again["id"] == ok["id"]
