"""The security boundary the whole "kids account" feature rests on.

A kids-scope account must see nothing but the shelf the owner explicitly tagged 'kids' -
not other books, not the acquisition machinery, not settings, not even the existence
of an author whose every work is off-limits. This runs against real HTTP sessions
(genuine login, genuine CSRF tokens) rather than the auth_mode=none shortcut every
other test file uses, because the shortcut IS full access and would prove nothing
about this boundary.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import migration, tagging

import helpers

app = main_module.app


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


@pytest.fixture
def household(local_mode):
    """A signed-in admin session and a signed-in kids session, each with its own
    cookie jar and CSRF token, plus one kid-visible and one kid-invisible work."""
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert r.status_code == 200, r.text
        admin_csrf = r.json()["csrf"]

        r = admin.post(
            "/api/v1/users",
            json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert r.status_code == 200, r.text

        # Both must be owned to be library-visible at all: since vNext Phase 1 the
        # Library lists owned works, and this fixture is testing the kids boundary,
        # not the owned/wanted split.
        kids_work = migration._find_or_create_stub({"title": "Kids Book", "author": "Author A", "identifiers": {}})
        helpers.own(kids_work)
        tagging.add_tag(kids_work, tagging.kids_tag_id())
        adult_work = migration._find_or_create_stub({"title": "Adult Book", "author": "Author B", "identifiers": {}})
        helpers.own(adult_work)

        kid = TestClient(app)
        r = kid.post("/api/v1/auth/login", json={"username": "junior", "password": "a-very-long-kid-password"})
        assert r.status_code == 200, r.text
        kid_csrf = r.json()["csrf"]

        yield {
            "admin": admin, "admin_csrf": admin_csrf,
            "kid": kid, "kid_csrf": kid_csrf,
            "kids_work": kids_work, "adult_work": adult_work,
        }


# ---------- identity ----------
def test_kid_session_reports_kids_scope(household):
    me = household["kid"].get("/api/v1/auth/me").json()
    assert me["authenticated"] is True
    assert me["user"]["access_scope"] == "kids"
    assert me["user"]["is_admin"] is False


# ---------- library visibility ----------
def test_library_listing_shows_only_the_kid_tagged_work(household):
    items = household["kid"].get("/api/v1/library").json()["items"]
    ids = {i["id"] for i in items}
    assert household["kids_work"] in ids
    assert household["adult_work"] not in ids


def test_admin_sees_both_works(household):
    items = household["admin"].get("/api/v1/library").json()["items"]
    ids = {i["id"] for i in items}
    assert {household["kids_work"], household["adult_work"]} <= ids


def test_work_detail_of_a_hidden_book_is_404_not_403(household):
    """404, not 403 - a kids account must not even learn the book exists."""
    r = household["kid"].get(f"/api/v1/library/{household['adult_work']}")
    assert r.status_code == 404


def test_work_detail_of_a_visible_book_succeeds(household):
    r = household["kid"].get(f"/api/v1/library/{household['kids_work']}")
    assert r.status_code == 200
    assert r.json()["history"] == [], "acquisition history is operator noise on a kids account"


def test_cover_of_a_hidden_book_is_404(household):
    r = household["kid"].get(f"/api/v1/library/{household['adult_work']}/cover")
    assert r.status_code == 404


# ---------- acquisition / settings surfaces are fully blocked ----------
@pytest.mark.parametrize("path", [
    "/api/v1/dashboard", "/api/v1/discover?q=x", "/api/v1/wanted", "/api/v1/review",
    "/api/v1/history", "/api/v1/calendar", "/api/v1/roots", "/api/v1/indexers",
    "/api/v1/download-clients", "/api/v1/path-mappings", "/api/v1/queue",
    "/api/v1/settings/import", "/api/v1/naming", "/api/v1/profiles",
    "/api/v1/system/health", "/api/v1/jobs", "/api/v1/failures",
    "/api/v1/works/merge-preview?canonical=a&sources=b",
])
def test_acquisition_and_settings_surfaces_are_blocked_for_kids(household, path):
    r = household["kid"].get(path)
    assert r.status_code == 403, f"{path} -> {r.status_code}"


def test_admin_only_surfaces_remain_blocked_for_kids_too(household):
    r = household["kid"].get("/api/v1/users", headers={"X-CSRF-Token": household["kid_csrf"]})
    assert r.status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/v1/library", {"metadata_id": "x"}),
    ("post", "/api/v1/library/{adult}/refresh", {}),
    ("post", "/api/v1/library/{adult}/cover/refresh", {}),
    ("patch", "/api/v1/library/{adult}/monitor", {"monitored": True}),
    ("patch", "/api/v1/library/{adult}", {"title": "Hacked"}),
    ("post", "/api/v1/library/{adult}/tags", {"tag_id": "x"}),
    ("post", "/api/v1/library/{adult}/classify", {}),
    ("post", "/api/v1/roots", {"name": "x", "path": "/x", "media_kind": "ebook"}),
    ("post", "/api/v1/releases/x/grab", {}),
])
def test_mutation_endpoints_are_blocked_for_kids(household, method, path, body):
    resolved = path.format(adult=household["adult_work"])
    r = getattr(household["kid"], method)(resolved, json=body, headers={"X-CSRF-Token": household["kid_csrf"]})
    assert r.status_code == 403, f"{method} {resolved} -> {r.status_code}: {r.text}"


# ---------- what a kids account IS allowed to do ----------
def test_kid_can_rate_their_own_visible_book(household):
    r = household["kid"].put(
        f"/api/v1/library/{household['kids_work']}/rating", json={"rating": 5},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 200 and r.json()["rating"] == 5


def test_kid_cannot_rate_a_hidden_book(household):
    r = household["kid"].put(
        f"/api/v1/library/{household['adult_work']}/rating", json={"rating": 5},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 404


def test_kid_can_set_their_own_read_state(household):
    r = household["kid"].put(
        f"/api/v1/library/{household['kids_work']}/read-state", json={"status": "reading"},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 200


def test_kid_can_view_recommendations_for_their_own_book(household):
    r = household["kid"].get(f"/api/v1/library/{household['kids_work']}/recommendations")
    assert r.status_code == 200
    assert r.json()["algorithm"] == "heuristic"


def test_kid_can_update_their_own_kindle_email(household):
    r = household["kid"].patch(
        "/api/v1/auth/me", json={"kindle_email": "junior@kindle.com"},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 200
    me = household["kid"].get("/api/v1/auth/me").json()
    # kindle_email is not echoed by /auth/me today; confirm via the admin's user list instead.
    users = {u["username"]: u for u in household["admin"].get("/api/v1/users").json()["items"]}
    assert users["junior"]["kindle_email"] == "junior@kindle.com"


def test_kid_cannot_promote_themselves_via_the_self_update_endpoint(household):
    """SelfUpdateRequest only exposes kindle_email - there is no field to smuggle
    access_scope or is_admin through this endpoint even if one were sent."""
    r = household["kid"].patch(
        "/api/v1/auth/me", json={"kindle_email": "x@y.com", "is_admin": True, "access_scope": "full"},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 200
    me = household["kid"].get("/api/v1/auth/me").json()
    assert me["user"]["is_admin"] is False and me["user"]["access_scope"] == "kids"


# ---------- authors ----------
def test_authors_listing_excludes_authors_with_no_visible_work(household):
    items = household["kid"].get("/api/v1/authors").json()["items"]
    names = {a["name"] for a in items}
    assert "Author A" in names
    assert "Author B" not in names


def test_author_detail_of_a_fully_hidden_author_is_404(household):
    author_b = db.one("SELECT c.id FROM contributors c WHERE c.name='Author B'")["id"]
    r = household["kid"].get(f"/api/v1/authors/{author_b}")
    assert r.status_code == 404


def test_author_detail_of_a_visible_author_hides_their_other_hidden_works(household):
    author_a_id = db.one("SELECT c.id FROM contributors c WHERE c.name='Author A'")["id"]
    hidden_by_a = migration._find_or_create_stub({"title": "Author A's Other Book", "author": "Author A", "identifiers": {}})
    r = household["kid"].get(f"/api/v1/authors/{author_a_id}")
    assert r.status_code == 200
    titles = {w["title"] for w in r.json()["works"]}
    assert titles == {"Kids Book"}
    assert "Author A's Other Book" not in titles


# ---------- OPDS ----------
def test_opds_catalog_only_lists_kid_visible_works(household, tmp_path):
    from app.services import migration as mig
    # Give both works an actual file so OPDS (which only lists works with a link)
    # includes them.
    for work_id, name in ((household["kids_work"], "kids.epub"), (household["adult_work"], "adult.epub")):
        path = tmp_path / name
        path.write_bytes(b"x")
        # Reuse the work's one ebook asset rather than inserting a second: a Work
        # owns at most one asset per kind, and the fixture already owns this one.
        asset_id, t = helpers.own(work_id), db.now()
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)", (db.uid(), asset_id, str(path), path.stat().st_size, t))

    body = household["kid"].get("/opds/v2/catalog.json").json()
    titles = {p["metadata"]["title"] for p in body["publications"]}
    assert titles == {"Kids Book"}


def test_opds_file_download_of_a_hidden_book_is_404(household, tmp_path):
    path = tmp_path / "adult.epub"
    path.write_bytes(b"x")
    asset_id, t = helpers.own(household["adult_work"]), db.now()
    file_id = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)", (file_id, asset_id, str(path), path.stat().st_size, t))
    r = household["kid"].get(f"/opds/files/{file_id}")
    assert r.status_code == 404


# ---------- series ----------
def test_series_listing_and_detail_respect_kids_visibility(household):
    with db.tx() as conn:
        sid = db.uid()
        conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,'Grownup Series','Grownup Series',?,?)", (sid, db.now(), db.now()))
        conn.execute("INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,'1')", (household["adult_work"], sid))

    names = {s["name"] for s in household["kid"].get("/api/v1/series").json()["items"]}
    assert "Grownup Series" not in names
    assert household["kid"].get(f"/api/v1/series/{sid}").status_code == 404


# ---------- account creation guardrails, exercised through the real endpoint ----------
def test_household_admin_cannot_be_impersonated_by_creating_a_second_admin_as_a_kid(household):
    r = household["kid"].post(
        "/api/v1/users", json={"username": "sneaky", "password": "another-long-password", "is_admin": True},
        headers={"X-CSRF-Token": household["kid_csrf"]},
    )
    assert r.status_code == 403
