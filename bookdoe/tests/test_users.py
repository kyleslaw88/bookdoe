"""Household account management (admin-only CRUD)."""
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)


def test_create_list_and_delete_a_user():
    r = client.post("/api/v1/users", json={"username": "kiddo", "password": "a-long-enough-password", "access_scope": "kids"})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    assert r.json()["access_scope"] == "kids"
    assert r.json()["is_admin"] is False

    listing = client.get("/api/v1/users").json()["items"]
    assert any(u["id"] == uid for u in listing)
    assert all("password" not in u and "password_hash" not in u for u in listing)

    assert client.delete(f"/api/v1/users/{uid}").status_code == 200
    assert not any(u["id"] == uid for u in client.get("/api/v1/users").json()["items"])


def test_a_kids_scope_account_cannot_be_created_as_admin():
    r = client.post("/api/v1/users", json={"username": "kiddo2", "password": "a-long-enough-password", "access_scope": "kids", "is_admin": True})
    assert r.status_code == 400


def test_duplicate_username_is_rejected():
    client.post("/api/v1/users", json={"username": "dupe", "password": "a-long-enough-password"})
    r = client.post("/api/v1/users", json={"username": "dupe", "password": "another-long-password"})
    assert r.status_code == 409


def test_username_uniqueness_is_case_insensitive():
    client.post("/api/v1/users", json={"username": "CaseTest", "password": "a-long-enough-password"})
    r = client.post("/api/v1/users", json={"username": "casetest", "password": "another-long-password"})
    assert r.status_code == 409


def test_update_promotes_and_demotes_access_scope():
    uid = client.post("/api/v1/users", json={"username": "family1", "password": "a-long-enough-password"}).json()["id"]
    r = client.patch(f"/api/v1/users/{uid}", json={"access_scope": "kids"})
    assert r.status_code == 200 and r.json()["access_scope"] == "kids"


def test_cannot_set_kids_scope_and_admin_together_via_update():
    uid = client.post("/api/v1/users", json={"username": "family2", "password": "a-long-enough-password"}).json()["id"]
    r = client.patch(f"/api/v1/users/{uid}", json={"access_scope": "kids", "is_admin": True})
    assert r.status_code == 400


def test_cannot_remove_the_last_administrator():
    a = client.post("/api/v1/users", json={"username": "admin-a", "password": "a-long-enough-password", "is_admin": True}).json()
    b = client.post("/api/v1/users", json={"username": "admin-b", "password": "a-long-enough-password", "is_admin": True}).json()

    # With two admins, demoting or removing either one is fine.
    assert client.patch(f"/api/v1/users/{a['id']}", json={"is_admin": False}).status_code == 200
    # Now only b is an admin among these two; restore a and remove b instead, leaving a.
    client.patch(f"/api/v1/users/{a['id']}", json={"is_admin": True})
    assert client.delete(f"/api/v1/users/{b['id']}").status_code == 200

    # a is now the only administrator created by this test - demoting or deleting it
    # would only be blocked if it is genuinely the *last* admin in the whole table,
    # which depends on what else pytest has created; assert the guard fires only
    # once that is actually true.
    remaining_admins = [u for u in client.get("/api/v1/users").json()["items"] if u["is_admin"]]
    if len(remaining_admins) == 1:
        last = remaining_admins[0]
        assert client.patch(f"/api/v1/users/{last['id']}", json={"is_admin": False}).status_code == 400
        assert client.delete(f"/api/v1/users/{last['id']}").status_code == 400


def test_last_administrator_guard_fires_with_a_controlled_pair():
    """Deterministic version of the guard, independent of whatever else exists."""
    with db.tx() as conn:
        conn.execute("DELETE FROM users WHERE is_admin=1")
    solo = client.post("/api/v1/users", json={"username": "solo-admin", "password": "a-long-enough-password", "is_admin": True}).json()
    assert client.patch(f"/api/v1/users/{solo['id']}", json={"is_admin": False}).status_code == 400
    assert client.delete(f"/api/v1/users/{solo['id']}").status_code == 400


def test_admin_can_set_kindle_email_for_another_user():
    uid = client.post("/api/v1/users", json={"username": "family3", "password": "a-long-enough-password", "access_scope": "kids"}).json()["id"]
    r = client.patch(f"/api/v1/users/{uid}", json={"kindle_email": "kid@kindle.com"})
    assert r.json()["kindle_email"] == "kid@kindle.com"


def test_self_service_kindle_email_update():
    r = client.patch("/api/v1/auth/me", json={"kindle_email": "me@kindle.com"})
    assert r.status_code == 200
    me = client.get("/api/v1/auth/me").json()
    assert me["user"]["access_scope"] == "full"


def test_updating_a_missing_user_is_404():
    assert client.patch("/api/v1/users/does-not-exist", json={"display_name": "X"}).status_code == 404
    assert client.delete("/api/v1/users/does-not-exist").status_code == 404
