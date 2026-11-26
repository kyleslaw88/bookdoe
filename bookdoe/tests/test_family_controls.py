"""Phase 3: family controls.

Grants widen a kid's shelf (book or collection) on every surface; requests from kids
wait for approval and from adults go straight to acquisition intent; approval records
intent on the existing media_assets columns (no second engine) and grants the book to
the kid; denial touches nothing. Approvers decide only for the kids an administrator
assigned to them: cross-family requests are invisible and undecidable, an approver
cannot assign themselves, cannot see a kid's reading state, and cannot reach
Management. Every assignment and grant is a History event.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import family, migration, tagging

import helpers

app = main_module.app


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


def _login(name):
    c = TestClient(app)
    r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}


@pytest.fixture
def families(local_mode, monkeypatch):
    """Two families in one household. the owner (admin); Jane (adult) approves for Owen and
    Evan (kids); Sam (adult, a friend) approves for Mia (kid). Pat is an adult with no
    kids. Three owned books: one on the kids shelf, two not. OpenReads is mocked."""
    monkeypatch.setattr(family.metadata, "search", lambda q, limit=20: [
        {"id": "or-1", "title": f"External {q}", "authors": ["Ext Author"], "first_publish_date": "2020"}])
    monkeypatch.setattr(family.metadata, "get_work", lambda mid: {"id": mid, "title": "External Book", "contributors": [{"name": "Ext Author", "role": "author"}]})

    def fake_upsert(meta, monitored=True):
        wid = migration._find_or_create_stub({"title": meta["title"], "author": "Ext Author", "identifiers": {}})
        with db.tx() as conn:
            conn.execute("UPDATE works SET metadata_id=? WHERE id=?", (meta["id"], wid))
        return wid
    monkeypatch.setattr(family.metadata, "upsert_work", fake_upsert)

    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        ids = {}
        for name, kind in (("jane", "adult"), ("sam", "adult"), ("pat", "adult"), ("owen", "kid"), ("evan", "kid"), ("mia", "kid")):
            r = admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah)
            ids[name] = r.json()["id"]
        ids["ada"] = admin.get("/api/v1/auth/me").json()["user"]["id"]
        for kid in ("owen", "evan"):
            assert admin.put(f"/api/v1/users/{ids[kid]}/approvers/{ids['jane']}", headers=ah).status_code == 200
        assert admin.put(f"/api/v1/users/{ids['mia']}/approvers/{ids['sam']}", headers=ah).status_code == 200
        shelf = migration._find_or_create_stub({"title": "Shelf Book", "author": "A", "identifiers": {}}); helpers.own(shelf)
        tagging.add_tag(shelf, tagging.kids_tag_id())
        hidden = migration._find_or_create_stub({"title": "Hidden Book", "author": "B", "identifiers": {}}); helpers.own(hidden)
        hidden2 = migration._find_or_create_stub({"title": "Second Hidden", "author": "C", "identifiers": {}}); helpers.own(hidden2)
        sessions = {n: _login(n) for n in ("jane", "sam", "pat", "owen", "evan", "mia")}
        sessions["ada"] = (admin, ah)
        yield {"s": sessions, "ids": ids, "shelf": shelf, "hidden": hidden, "hidden2": hidden2}


# ---------- grants widen the shelf ----------
def test_book_grant_makes_one_book_visible_to_one_kid_on_every_surface(families):
    s, ids = families["s"], families["ids"]
    owen, oh = s["owen"]; evan, _ = s["evan"]; jane, jh = s["jane"]
    hidden = families["hidden"]
    assert owen.get(f"/api/v1/library/{hidden}").status_code == 404
    r = jane.put(f"/api/player/v1/approvals/kids/{ids['owen']}/grants/{hidden}", json={"note": "school"}, headers=jh)
    assert r.status_code == 200, r.text
    assert owen.get(f"/api/v1/library/{hidden}").status_code == 200
    assert {w["title"] for w in owen.get("/api/v1/library").json()["items"]} == {"Shelf Book", "Hidden Book"}
    # Evan (same approver, different kid) does not get it.
    assert evan.get(f"/api/v1/library/{hidden}").status_code == 404
    assert {w["title"] for w in evan.get("/api/v1/library").json()["items"]} == {"Shelf Book"}
    # Revoking closes it again.
    assert jane.delete(f"/api/player/v1/approvals/kids/{ids['owen']}/grants/{hidden}", headers=jh).status_code == 200
    assert owen.get(f"/api/v1/library/{hidden}").status_code == 404
    events = [h["event_type"] for h in db.all("SELECT event_type FROM history WHERE event_type LIKE 'grant.%'")]
    assert events.count("grant.added") == 1 and events.count("grant.removed") == 1


def test_collection_grant_is_admin_only_and_follows_the_tag(families):
    s, ids = families["s"], families["ids"]
    admin, ah = s["ada"]; owen, _ = s["owen"]; jane, jh = s["jane"]
    tid = tagging.create_tag("classics")
    tagging.add_tag(families["hidden"], tid)
    assert jane.put(f"/api/v1/users/{ids['owen']}/tag-grants/{tid}", headers=jh).status_code == 403
    assert admin.put(f"/api/v1/users/{ids['owen']}/tag-grants/{tid}", headers=ah).status_code == 200
    assert owen.get(f"/api/v1/library/{families['hidden']}").status_code == 200
    tagging.add_tag(families["hidden2"], tid)   # a book later added to the collection follows
    assert owen.get(f"/api/v1/library/{families['hidden2']}").status_code == 200
    assert owen.get(f"/api/v1/library/{families['hidden2']}/cover").status_code == 404  # no cover cached, but not a visibility 404
    assert admin.delete(f"/api/v1/users/{ids['owen']}/tag-grants/{tid}", headers=ah).status_code == 200
    assert owen.get(f"/api/v1/library/{families['hidden']}").status_code == 404


def test_kid_cannot_grant_to_itself_or_see_grant_surfaces(families):
    s, ids = families["s"], families["ids"]
    owen, oh = s["owen"]
    assert owen.put(f"/api/player/v1/approvals/kids/{ids['owen']}/grants/{families['hidden']}", headers=oh).status_code == 403
    assert owen.get("/api/player/v1/approvals").status_code == 403
    assert owen.get("/api/player/v1/approvals/kids").status_code == 403
    assert owen.put(f"/api/v1/users/{ids['owen']}/grants/{families['hidden']}", headers=oh).status_code == 403


# ---------- requests ----------
def test_kid_request_waits_for_approval_and_creates_no_intent(families):
    s = families["s"]; owen, oh = s["owen"]
    found = owen.get("/api/player/v1/requests/search?q=Hidden").json()
    hit = next(x for x in found["in_library"] if x["title"] == "Hidden Book")
    assert hit["visible"] is False and hit["owned_kinds"] == ["ebook"]
    r = owen.post("/api/player/v1/requests", json={"work_id": families["hidden"], "kinds": ["audiobook"]}, headers=oh)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending" and r.json()["friendly_status"] == "Awaiting Approval"
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE work_id=? AND has_acquisition_intent=1", (families["hidden"],))["n"] == 0
    mine = owen.get("/api/player/v1/requests").json()
    assert mine["total"] == 1 and mine["items"][0]["title"] == "Hidden Book"
    # A second open request for the same book is refused.
    assert owen.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=oh).status_code == 400


def test_adult_request_is_auto_approved_into_acquisition_intent(families):
    s = families["s"]; pat, ph = s["pat"]
    r = pat.post("/api/player/v1/requests", json={"work_id": families["hidden"], "kinds": ["audiobook"]}, headers=ph)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "approved" and body["friendly_status"] == "Requested"
    a = db.one("SELECT * FROM media_assets WHERE work_id=? AND kind='audiobook'", (families["hidden"],))
    assert a["status"] == "missing" and a["monitored"] == 1 and a["has_acquisition_intent"] == 1
    assert db.one("SELECT monitored FROM works WHERE id=?", (families["hidden"],))["monitored"] == 1
    # The wanted search now sees exactly this asset (the existing engine).
    from app.services import scheduler
    wanted = [x["id"] for x in db.all("SELECT a.id FROM media_assets a JOIN works w ON w.id=a.work_id WHERE a.monitored=1 AND a.has_acquisition_intent=1 AND w.monitored=1 AND a.status='missing'")]
    assert wanted == [a["id"]]
    assert scheduler  # imported to make the dependency explicit


def test_adult_policy_setting_makes_adults_wait_too(families):
    s = families["s"]; admin, ah = s["ada"]; pat, ph = s["pat"]
    assert admin.get("/api/v1/settings/requests").json() == {"adult_policy": "auto", "kid_external_search": True, "search_on_approval": True}
    assert admin.put("/api/v1/settings/requests", json={"adult_policy": "approve"}, headers=ah).json()["adult_policy"] == "approve"
    r = pat.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=ph)
    assert r.json()["status"] == "pending"
    assert pat.put("/api/v1/settings/requests", json={"adult_policy": "auto"}, headers=ph).status_code == 403


def test_external_request_creates_the_work_on_approval_and_grants_it(families):
    s, ids = families["s"], families["ids"]
    owen, oh = s["owen"]; jane, jh = s["jane"]
    ext = owen.get("/api/player/v1/requests/search?q=Dragons").json()["external"]
    assert ext and ext[0]["metadata_id"] == "or-1"
    r = owen.post("/api/player/v1/requests", json={"metadata_id": "or-1", "title": ext[0]["title"], "author": "Ext Author", "kinds": ["ebook", "audiobook"]}, headers=oh)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert db.one("SELECT 1 FROM works WHERE metadata_id='or-1'") is None, "nothing is created until approval"
    pending = jane.get("/api/player/v1/approvals").json()
    assert [p["id"] for p in pending["items"]] == [rid] and pending["items"][0]["requester"] == "owen"
    r = jane.post(f"/api/player/v1/approvals/{rid}/approve", json={"note": "ok"}, headers=jh)
    assert r.status_code == 200, r.text
    w = db.one("SELECT id FROM works WHERE metadata_id='or-1'")
    assert w, "approval created the work"
    kinds = {a["kind"]: a for a in db.all("SELECT kind,status,has_acquisition_intent FROM media_assets WHERE work_id=?", (w["id"],))}
    assert kinds["ebook"]["status"] == "missing" and kinds["audiobook"]["has_acquisition_intent"] == 1
    assert db.one("SELECT 1 FROM content_grants WHERE user_id=? AND work_id=?", (ids["owen"], w["id"])), "the kid may see what was approved"
    mine = owen.get("/api/player/v1/requests").json()["items"][0]
    assert mine["friendly_status"] == "Requested" and mine["decided_by"] == "jane"
    # Once the file lands (import marks the asset owned) the kid sees it as Available.
    helpers.own(w["id"], "ebook"); helpers.own(w["id"], "audiobook", "m4b")
    assert owen.get("/api/player/v1/requests").json()["items"][0]["friendly_status"] == "Available"
    assert owen.get(f"/api/v1/library/{w['id']}").status_code == 200


def test_denied_request_creates_no_intent_and_no_grant(families):
    s, ids = families["s"], families["ids"]
    evan, eh = s["evan"]; jane, jh = s["jane"]
    r = evan.post("/api/player/v1/requests", json={"work_id": families["hidden"], "kinds": ["ebook", "audiobook"]}, headers=eh)
    rid = r.json()["id"]
    assert jane.post(f"/api/player/v1/approvals/{rid}/deny", json={"note": "not yet"}, headers=jh).status_code == 200
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE work_id=? AND has_acquisition_intent=1", (families["hidden"],))["n"] == 0
    assert db.one("SELECT 1 FROM content_grants WHERE user_id=?", (ids["evan"],)) is None
    mine = evan.get("/api/player/v1/requests").json()["items"][0]
    assert mine["friendly_status"] == "Unavailable" and mine["decision_note"] == "not yet"
    assert evan.get(f"/api/v1/library/{families['hidden']}").status_code == 404
    # Deciding twice is refused.
    assert jane.post(f"/api/player/v1/approvals/{rid}/approve", json={}, headers=jh).status_code == 400


def test_being_prepared_status_reflects_an_active_download(families):
    s = families["s"]; pat, ph = s["pat"]
    rid = pat.post("/api/player/v1/requests", json={"work_id": families["hidden"], "kinds": ["ebook"]}, headers=ph).json()["id"]
    a = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind='ebook'", (families["hidden"],))
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='missing' WHERE id=?", (a["id"],))
        conn.execute("INSERT INTO downloads(id,work_id,asset_id,state,added_at,updated_at) VALUES (?,?,?,'downloading',?,?)", (db.uid(), families["hidden"], a["id"], db.now(), db.now()))
    assert pat.get(f"/api/player/v1/requests/{rid}").json()["friendly_status"] == "Being Prepared"


def test_requester_can_cancel_only_their_own_pending_request(families):
    s = families["s"]; owen, oh = s["owen"]; evan, eh = s["evan"]
    rid = owen.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=oh).json()["id"]
    assert evan.delete(f"/api/player/v1/requests/{rid}", headers=eh).status_code == 404
    assert evan.get(f"/api/player/v1/requests/{rid}").status_code == 404
    assert owen.delete(f"/api/player/v1/requests/{rid}", headers=oh).json()["status"] == "cancelled"


# ---------- approver scoping ----------
def test_approver_sees_and_decides_only_their_own_kids(families):
    s = families["s"]
    owen, oh = s["owen"]; mia, mh = s["mia"]; jane, jh = s["jane"]; sam, sh = s["sam"]; pat, ph = s["pat"]
    owen_req = owen.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=oh).json()["id"]
    mia_req = mia.post("/api/player/v1/requests", json={"work_id": families["hidden2"]}, headers=mh).json()["id"]
    assert [r["id"] for r in jane.get("/api/player/v1/approvals").json()["items"]] == [owen_req]
    assert [r["id"] for r in sam.get("/api/player/v1/approvals").json()["items"]] == [mia_req]
    assert [k["username"] for k in jane.get("/api/player/v1/approvals/kids").json()["items"]] == ["evan", "owen"]
    # Cross-family: Jane cannot decide Mia's request, by either verb, and cannot even read it.
    assert jane.post(f"/api/player/v1/approvals/{mia_req}/approve", json={}, headers=jh).status_code == 403
    assert jane.post(f"/api/player/v1/approvals/{mia_req}/deny", json={}, headers=jh).status_code == 403
    assert jane.get(f"/api/player/v1/requests/{mia_req}").status_code == 404
    assert db.one("SELECT status FROM requests WHERE id=?", (mia_req,))["status"] == "pending"
    # An adult with no kids has no approval surface at all.
    assert pat.get("/api/player/v1/approvals").status_code == 403
    assert pat.post(f"/api/player/v1/approvals/{owen_req}/approve", json={}, headers=ph).status_code == 403
    assert pat.get("/api/v1/auth/me").json()["user"]["approver_for"] == 0
    assert jane.get("/api/v1/auth/me").json()["user"]["approver_for"] == 2
    # The admin sees both.
    admin, ah = s["ada"]
    assert {r["id"] for r in admin.get("/api/player/v1/approvals").json()["items"]} == {owen_req, mia_req}
    assert {r["id"] for r in admin.get("/api/v1/requests?status=pending").json()["items"]} == {owen_req, mia_req}


def test_approver_cannot_grant_to_another_familys_kid(families):
    s, ids = families["s"], families["ids"]
    jane, jh = s["jane"]; mia, _ = s["mia"]
    assert jane.put(f"/api/player/v1/approvals/kids/{ids['mia']}/grants/{families['hidden']}", headers=jh).status_code == 403
    assert jane.get(f"/api/player/v1/approvals/kids/{ids['mia']}/grants", headers=jh).status_code == 403
    assert jane.get(f"/api/player/v1/approvals/kids/{ids['mia']}/search?q=Hidden", headers=jh).status_code == 403
    assert jane.get(f"/api/player/v1/approvals/kids/does-not-exist/grants", headers=jh).status_code == 403, "no existence probing"
    assert mia.get(f"/api/v1/library/{families['hidden']}").status_code == 404


def test_approver_cannot_decide_an_adults_request(families):
    s = families["s"]; pat, ph = s["pat"]; jane, jh = s["jane"]; admin, ah = s["ada"]
    admin.put("/api/v1/settings/requests", json={"adult_policy": "approve"}, headers=ah)
    rid = pat.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=ph).json()["id"]
    assert jane.post(f"/api/player/v1/approvals/{rid}/approve", json={}, headers=jh).status_code == 403
    assert all(r["id"] != rid for r in jane.get("/api/player/v1/approvals").json()["items"])
    assert admin.post(f"/api/v1/requests/{rid}/approve", headers=ah).status_code == 200


def test_only_admins_create_or_remove_assignments_and_it_is_audited(families):
    s, ids = families["s"], families["ids"]
    jane, jh = s["jane"]; pat, ph = s["pat"]; admin, ah = s["ada"]; owen, oh = s["owen"]
    # Escalation attempts: an adult assigning themselves, a kid assigning anyone.
    assert pat.put(f"/api/v1/users/{ids['owen']}/approvers/{ids['pat']}", headers=ph).status_code == 403
    assert jane.put(f"/api/v1/users/{ids['mia']}/approvers/{ids['jane']}", headers=jh).status_code == 403
    assert owen.put(f"/api/v1/users/{ids['owen']}/approvers/{ids['owen']}", headers=oh).status_code == 403
    assert family.can_act_for(db.one("SELECT * FROM users WHERE id=?", (ids["pat"],)), ids["owen"]) is False
    # Only adults can be approvers, only kids can have them.
    assert admin.put(f"/api/v1/users/{ids['owen']}/approvers/{ids['evan']}", headers=ah).status_code == 400
    assert admin.put(f"/api/v1/users/{ids['pat']}/approvers/{ids['jane']}", headers=ah).status_code == 400
    assert admin.put(f"/api/v1/users/{ids['owen']}/approvers/{ids['ada']}", headers=ah).status_code == 400
    fam = admin.get(f"/api/v1/users/{ids['owen']}/family").json()
    assert [a["username"] for a in fam["approvers"]] == ["jane"]
    assert admin.delete(f"/api/v1/users/{ids['owen']}/approvers/{ids['jane']}", headers=ah).status_code == 200
    assert [k["username"] for k in jane.get("/api/player/v1/approvals/kids").json()["items"]] == ["evan"]
    events = db.all("SELECT event_type,message FROM history WHERE event_type LIKE 'approver.%' ORDER BY created_at")
    assert [e["event_type"] for e in events] == ["approver.assigned"] * 3 + ["approver.removed"]
    assert "jane" in events[-1]["message"] and "owen" in events[-1]["message"] and "ada" in events[-1]["message"]


def test_assignment_goes_inert_when_the_kid_grows_up(families):
    s, ids = families["s"], families["ids"]
    admin, ah = s["ada"]; jane, jh = s["jane"]; owen, oh = s["owen"]
    rid = owen.post("/api/player/v1/requests", json={"work_id": families["hidden"]}, headers=oh).json()["id"]
    admin.patch(f"/api/v1/users/{ids['owen']}", json={"account_type": "adult"}, headers=ah)
    assert jane.post(f"/api/player/v1/approvals/{rid}/approve", json={}, headers=jh).status_code == 403
    assert [k["username"] for k in jane.get("/api/player/v1/approvals/kids").json()["items"]] == ["evan"]


def test_approver_never_reaches_a_kids_reading_state_or_management(families):
    s, ids = families["s"], families["ids"]
    jane, jh = s["jane"]; owen, oh = s["owen"]
    shelf = families["shelf"]
    aid = helpers.own(shelf)
    owen.put(f"/api/v1/library/{shelf}/progress/{aid}", json={"location": "owen-private", "percent": 0.3}, headers=oh)
    owen.put(f"/api/player/v1/favorites/{shelf}", headers=oh)
    # Jane's own view of the same book carries her state, never Owen's.
    assert jane.get(f"/api/v1/library/{shelf}/progress/{aid}").json()["progress"] is None
    assert jane.get(f"/api/v1/library/{shelf}").json()["progress"] == []
    assert jane.get("/api/player/v1/favorites").json()["total"] == 0
    assert jane.get("/api/player/v1/continue").json()["items"] == []
    assert jane.get(f"/api/player/v1/bookmarks?asset_id={aid}").json()["total"] == 0
    # No management surface opens for an approver.
    for path in ("/api/v1/users", "/api/v1/requests", f"/api/v1/users/{ids['owen']}/family", "/api/v1/dashboard", "/api/v1/settings/requests"):
        assert jane.get(path).status_code == 403, path
    assert jane.get("/manage").status_code == 403
    # Nothing in the Player API exposes another user's progress by id either.
    openapi = jane.get("/openapi.json").json()
    for path in openapi["paths"]:
        if path.startswith("/api/player/v1/approvals"):
            assert "progress" not in path and "bookmark" not in path and "favorite" not in path


def test_discover_search_for_a_kid_shows_titles_only_for_hidden_books(families):
    s = families["s"]; owen, _ = s["owen"]
    found = owen.get("/api/player/v1/requests/search?q=Book").json()
    by_title = {x["title"]: x for x in found["in_library"]}
    assert by_title["Shelf Book"]["visible"] is True
    hidden = by_title["Hidden Book"]
    assert hidden["visible"] is False and hidden["has_cover"] is False
    assert set(hidden) == {"id", "title", "authors", "owned_kinds", "visible", "has_cover"}


def test_kid_external_search_can_be_turned_off(families):
    s = families["s"]; owen, oh = s["owen"]; admin, ah = s["ada"]
    admin.put("/api/v1/settings/requests", json={"adult_policy": "auto", "kid_external_search": False}, headers=ah)
    assert owen.get("/api/player/v1/requests/search?q=Dragons").json()["external"] == []
    assert owen.post("/api/player/v1/requests", json={"metadata_id": "or-1", "title": "x"}, headers=oh).status_code == 403


def test_pending_and_unplaced_accounts_cannot_request(local_mode):
    with TestClient(app) as c:
        c.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        auth_module.create_local_user("newbie", "a-very-long-newbie-password", is_admin=False, access_scope="pending")
        p = TestClient(app)
        r = p.post("/api/v1/auth/login", json={"username": "newbie", "password": "a-very-long-newbie-password"})
        assert p.post("/api/player/v1/requests", json={"work_id": "x"}, headers={"X-CSRF-Token": r.json()["csrf"]}).status_code == 403
