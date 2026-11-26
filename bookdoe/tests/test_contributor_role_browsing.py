"""P4-08: role-parameterised browsing on /api/v1/authors (Part XXII).

Every test starts from an empty database (conftest's autouse ``fresh_db``) and seeds
the same fixed rows with fixed ids and timestamps, so responses are deterministic
and the tests pass in any order.
"""
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import tagging

client = TestClient(main_module.app)

T = 1000.0


def _seed():
    """Ann authors Books One and Two, Bob authors Book Three; Nina narrates the two
    audiobooks of Ann's books and Eddie edits Book One. Ann is monitored."""
    with db.tx() as c:
        for cid, name, sort in (("c-ann", "Ann Author", "Author, Ann"), ("c-bob", "Bob Writer", "Writer, Bob"),
                                ("c-nina", "Nina Narrator", "Narrator, Nina"), ("c-eddie", "Eddie Editor", "Editor, Eddie")):
            c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                      (cid, name, sort, T, T))
        for wid, title, date in (("w-1", "Book One", "2001"), ("w-2", "Book Two", "2002"), ("w-3", "Book Three", "2003")):
            c.execute("INSERT INTO works(id,title,sort_title,first_publish_date,metadata_status,added_at,updated_at)"
                      " VALUES (?,?,?,?,'resolved',?,?)", (wid, title, title, date, T, T))
        for wid, cid, role in (("w-1", "c-ann", "author"), ("w-2", "c-ann", "author"), ("w-3", "c-bob", "author"),
                               ("w-1", "c-nina", "narrator"), ("w-2", "c-nina", "narrator"), ("w-1", "c-eddie", "editor")):
            c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,0)", (wid, cid, role))
        for aid, wid, kind, fmt, narrators in (("a-1", "w-1", "audiobook", "m4b", '["Nina Narrator"]'),
                                               ("a-2", "w-2", "audiobook", "m4b", '["Nina Narrator"]'),
                                               ("a-3", "w-3", "ebook", "epub", "[]")):
            c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,narrator_json,added_at,updated_at)"
                      " VALUES (?,?,?,?,'owned',?,?,?)", (aid, wid, kind, fmt, narrators, T, T))
        c.execute("INSERT INTO author_monitors(contributor_id,policy,ebook,audiobook,started_at,last_refresh_at)"
                  " VALUES ('c-ann','future',0,1,2000.0,3000.0)")


# Captured by running GET /api/v1/authors, /authors/c-ann and /authors/c-nina against
# _seed() with main's code before this change (f7ec4a6: hardcoded role='author').
GOLDEN_LIST = (
    '{"items": [{"id": "c-ann", "metadata_id": null, "name": "Ann Author", "sort_name": "Author, Ann", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": "future", "monitor_ebook": 0, "monitor_audiobook": 1, "monitor_started_at": 2000.0, "last_refresh_at": 3000.0, "last_error": null, "work_count": 2, "owned_audio": 2, "owned_ebooks": 0, "owned_works": 2}, {"id": "c-eddie", "metadata_id": null, "name": "Eddie Editor", "sort_name": "Editor, Eddie", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": null, "monitor_ebook": null, "monitor_audiobook": null, "monitor_started_at": null, "last_refresh_at": null, "last_error": null, "work_count": 0, "owned_audio": 0, "owned_ebooks": 0, "owned_works": 0}, {"id": "c-nina", "metadata_id": null, "name": "Nina Narrator", "sort_name": "Narrator, Nina", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": null, "monitor_ebook": null, "monitor_audiobook": null, "monitor_started_at": null, "last_refresh_at": null, "last_error": null, "work_count": 0, "owned_audio": 0, "owned_ebooks": 0, "owned_works": 0}, {"id": "c-bob", "metadata_id": null, "name": "Bob Writer", "sort_name": "Writer, Bob", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": null, "monitor_ebook": null, "monitor_audiobook": null, "monitor_started_at": null, "last_refresh_at": null, "last_error": null, "work_count": 1, "owned_audio": 0, "owned_ebooks": 1, "owned_works": 1}], "total": 4}'
)
GOLDEN_ANN_DETAIL = (
    '{"id": "c-ann", "metadata_id": null, "name": "Ann Author", "sort_name": "Author, Ann", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": "future", "monitor_ebook": 0, "monitor_audiobook": 1, "monitor_started_at": 2000.0, "last_refresh_at": 3000.0, "last_error": null, "works": [{"id": "w-2", "title": "Book Two", "cover_url": null, "first_publish_date": "2002", "monitored": 1, "metadata_status": "resolved", "assets": [{"id": "a-2", "kind": "audiobook", "format": "m4b", "status": "owned", "monitored": 0}]}, {"id": "w-1", "title": "Book One", "cover_url": null, "first_publish_date": "2001", "monitored": 1, "metadata_status": "resolved", "assets": [{"id": "a-1", "kind": "audiobook", "format": "m4b", "status": "owned", "monitored": 0}]}]}'
)
GOLDEN_NINA_DETAIL = (
    '{"id": "c-nina", "metadata_id": null, "name": "Nina Narrator", "sort_name": "Narrator, Nina", "image_url": null, "created_at": 1000.0, "updated_at": 1000.0, "policy": null, "monitor_ebook": null, "monitor_audiobook": null, "monitor_started_at": null, "last_refresh_at": null, "last_error": null, "works": []}'
)


def test_default_list_matches_pre_change_golden():
    _seed()
    r = client.get("/api/v1/authors")
    assert r.status_code == 200
    # json.dumps keeps key order, so field order is pinned too, not just values.
    assert json.dumps(r.json()) == GOLDEN_LIST


def test_default_detail_matches_pre_change_golden():
    _seed()
    assert json.dumps(client.get("/api/v1/authors/c-ann").json()) == GOLDEN_ANN_DETAIL
    # A narrator-only contributor still answers 200 with no works by default, as before.
    assert json.dumps(client.get("/api/v1/authors/c-nina").json()) == GOLDEN_NINA_DETAIL
    assert client.get("/api/v1/authors/missing").status_code == 404


def test_default_author_list_still_includes_every_contributor_row():
    """Pre-existing behaviour kept on purpose: the default listing shows every
    contributor (narrators included, with work_count 0), not only role holders."""
    _seed()
    items = {a["name"]: a for a in client.get("/api/v1/authors").json()["items"]}
    assert set(items) == {"Ann Author", "Bob Writer", "Nina Narrator", "Eddie Editor"}
    assert items["Nina Narrator"]["work_count"] == 0
    assert items["Ann Author"]["work_count"] == 2


def test_narrator_list_and_detail():
    _seed()
    items = client.get("/api/v1/authors?role=narrator").json()["items"]
    assert [a["name"] for a in items] == ["Nina Narrator"]
    assert items[0]["work_count"] == 2 and items[0]["owned_audio"] == 2 and items[0]["policy"] is None
    d = client.get("/api/v1/authors/c-nina?role=narrator").json()
    assert [w["title"] for w in d["works"]] == ["Book Two", "Book One"]
    assert d["policy"] is None


@pytest.mark.parametrize("role,holder,title,non_holder", [
    ("narrator", "c-nina", "Book One", "c-ann"),
    ("editor", "c-eddie", "Book One", "c-nina"),
])
def test_non_author_role_list_detail_and_404(role, holder, title, non_holder):
    _seed()
    items = client.get(f"/api/v1/authors?role={role}").json()["items"]
    assert holder in [a["id"] for a in items]
    assert non_holder not in [a["id"] for a in items]
    d = client.get(f"/api/v1/authors/{holder}?role={role}")
    assert d.status_code == 200 and title in [w["title"] for w in d.json()["works"]]
    assert client.get(f"/api/v1/authors/{non_holder}?role={role}").status_code == 404


@pytest.mark.parametrize("role", ["translator", "illustrator"])
def test_roles_without_data_list_nobody(role):
    _seed()
    assert client.get(f"/api/v1/authors?role={role}").json() == {"items": [], "total": 0}
    assert client.get(f"/api/v1/authors/c-ann?role={role}").status_code == 404


def test_unknown_role_rejected():
    assert client.get("/api/v1/authors?role=bogus").status_code == 400
    assert client.get("/api/v1/authors/x?role=bogus").status_code == 400


def test_work_exposes_narrator_ids_for_links():
    _seed()
    d = client.get("/api/v1/library/w-1").json()
    assert d["narrators"] == [{"id": "c-nina", "name": "Nina Narrator"}]
    assert d["assets"][0]["narrators"] == ["Nina Narrator"]
    assert d["assets"][0]["narrator_ids"] == ["c-nina"]


@pytest.fixture
def kid(monkeypatch):
    """A real signed-in kids-scope session (auth_mode=none would be full access)."""
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(main_module.app) as admin:
        csrf = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"}).json()["csrf"]
        r = admin.post("/api/v1/users", json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"},
                       headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200, r.text
    kid = TestClient(main_module.app)
    assert kid.post("/api/v1/auth/login", json={"username": "junior", "password": "a-very-long-kid-password"}).status_code == 200
    return kid


def test_kids_scope_hides_narrator_whose_works_are_all_hidden(kid):
    _seed()
    tagging.add_tag("w-3", tagging.kids_tag_id())  # only Bob's book is kid-visible
    assert kid.get("/api/v1/authors?role=narrator").json() == {"items": [], "total": 0}
    assert kid.get("/api/v1/authors/c-nina?role=narrator").status_code == 404


def test_kids_scope_narrator_detail_shows_only_visible_works(kid):
    _seed()
    tagging.add_tag("w-1", tagging.kids_tag_id())
    assert [a["id"] for a in kid.get("/api/v1/authors?role=narrator").json()["items"]] == ["c-nina"]
    d = kid.get("/api/v1/authors/c-nina?role=narrator")
    assert d.status_code == 200 and [w["title"] for w in d.json()["works"]] == ["Book One"]
