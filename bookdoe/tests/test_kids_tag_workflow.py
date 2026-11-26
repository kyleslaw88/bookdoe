"""Removing the kids tag (and any other tag) end to end: who may do it, that it is
recorded and final, that the kid's view drops the work at once, and the bulk path.

Real HTTP sessions (admin, adult, kid, a kid's paired device), as in
test_kids_access_control.py, because auth_mode=none would be full access for everyone.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import player_api
from app.config import settings as base_settings
from app.services import llmclient, migration, reader, tagging

import helpers
from test_player_api import pair

app = main_module.app


@pytest.fixture
def world(monkeypatch, tmp_path):
    replaced = dataclasses.replace(base_settings, auth_mode="local", public_url="http://books.example.test")
    for mod in (main_module, auth_module, player_api):
        monkeypatch.setattr(mod, "settings", replaced)
    monkeypatch.setattr(reader, "_ffprobe_chapters", lambda path: [])
    player_api._pair_hits.clear()
    tagging.set_auto_approve(enabled=False)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            assert admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah).status_code == 200
        w = {}
        for title in ("Backyard Starship", "Picture Book", "Plain Novel"):
            wid = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
            aid = helpers.own(wid)
            path = tmp_path / f"{title}.epub"
            path.write_bytes(b"PK\x03\x04 " + b"x" * 400)
            fid = db.uid()
            with db.tx() as conn:
                conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) VALUES (?,?,?,?,'adopt',?,?) ON CONFLICT DO NOTHING",
                             ("r1", "Root", str(tmp_path), "ebook", db.now(), db.now()))
                conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)",
                             (fid, aid, str(path), path.stat().st_size, db.now()))
            w[title] = {"id": wid, "file": fid}
        kids = tagging.kids_tag_id()
        # Backyard Starship was wrongly auto-approved; Picture Book is tagged by hand.
        tagging.add_tag(w["Backyard Starship"]["id"], kids, source="llm", confidence=0.9)
        tagging.set_kids_decision(w["Backyard Starship"]["id"], "on", via="auto_approved", by="classifier")
        tagging.set_kids_tag_manually(w["Picture Book"]["id"], True, by_user_id="x", by_name="x")
        out = {"admin": admin, "ah": ah, "works": w, "kids": kids}
        for name in ("jane", "owen"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            out[name], out[name + "_h"] = c, {"X-CSRF-Token": r.json()["csrf"]}
        out["dev"], _, _ = pair(out["owen"], out["owen_h"])
        yield out


def _tagged(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def _kid_sees(world, wid):
    """Every kid-facing surface; returns the set of surfaces that still show the work."""
    k, w = world["owen"], world["works"]
    seen = set()
    title = next(t for t, v in w.items() if v["id"] == wid)
    if wid in {i["id"] for i in k.get("/api/v1/library?limit=100").json()["items"]}: seen.add("library")
    if wid in {i["id"] for i in k.get("/api/v1/library?q=" + title.split()[0]).json()["items"]}: seen.add("search")
    if k.get(f"/api/v1/library/{wid}").status_code == 200: seen.add("detail")
    if wid in {i["id"] for i in k.get("/api/player/v1/recent").json()["recently_added"]}: seen.add("recent")
    if wid in {i["id"] for i in k.get("/api/player/v1/library").json()["items"]}: seen.add("player-library")
    if wid in {i["id"] for i in world["dev"].get("/api/player/v1/library").json()["items"]}: seen.add("device-library")
    if title in {p["metadata"]["title"] for p in k.get("/opds/v2/catalog.json").json()["publications"]}: seen.add("opds")
    if k.get(f"/api/v1/library/{wid}/files/{w[title]['file']}").status_code == 200: seen.add("media")
    if world["dev"].get(f"/api/v1/library/{wid}/files/{w[title]['file']}").status_code == 200: seen.add("device-media")
    return seen


def test_admin_removes_kids_tag_via_generic_endpoint_and_it_is_recorded(world):
    sid = world["works"]["Backyard Starship"]["id"]
    assert "library" in _kid_sees(world, sid)
    r = world["admin"].delete(f"/api/v1/library/{sid}/tags/{world['kids']}", headers=world["ah"])
    assert r.status_code == 200 and all(t["name"] != "kids" for t in r.json()["items"])
    assert not _tagged(sid)
    d = tagging.kids_decision(sid)
    assert d["state"] == "off" and d["via"] == "manual_untag" and d["decided_by"] and d["decided_at"]
    assert d["decided_by_name"] == "ada"
    assert db.one("SELECT 1 FROM history WHERE event_type='kids.manual_off' AND work_id=?", (sid,))
    assert _kid_sees(world, sid) == set()


def test_everything_a_kid_can_see_drops_immediately_including_player_etags(world):
    sid = world["works"]["Backyard Starship"]["id"]
    before = world["owen"].get("/api/player/v1/recent")
    etag = before.headers["etag"]
    assert sid in {i["id"] for i in before.json()["recently_added"]}
    home = world["dev"].get("/api/player/v1/home")
    assert sid in {i["id"] for i in home.json()["recently_added"]}
    world["admin"].delete(f"/api/v1/library/{sid}/tags/{world['kids']}", headers=world["ah"])
    # A kid device revalidating with its old ETag must get a fresh body, never a 304.
    again = world["owen"].get("/api/player/v1/recent", headers={"If-None-Match": etag})
    assert again.status_code == 200 and sid not in {i["id"] for i in again.json()["recently_added"]}
    assert "private" in again.headers["cache-control"] and "no-cache" in again.headers["cache-control"]
    home2 = world["dev"].get("/api/player/v1/home")
    assert sid not in {i["id"] for i in home2.json()["recently_added"]}
    assert world["owen"].get(f"/api/player/v1/library/{sid}").status_code == 404
    assert world["owen"].get(f"/api/player/v1/library/{sid}/cover").status_code == 404
    # The other kids-shelf book is untouched.
    assert "library" in _kid_sees(world, world["works"]["Picture Book"]["id"])


@pytest.mark.parametrize("who", ["jane", "owen", "dev", "anon"])
def test_non_admins_cannot_remove_the_kids_tag_anywhere(world, who):
    sid = world["works"]["Backyard Starship"]["id"]
    c = TestClient(app) if who == "anon" else world[who]
    h = {} if who in ("anon", "dev") else world[who + "_h"]
    for method, path, body in (
        ("delete", f"/api/v1/library/{sid}/tags/{world['kids']}", None),
        ("post", f"/api/v1/tags/{world['kids']}/remove-works", {"work_ids": [sid]}),
        ("post", "/api/v1/tags/kids-auto/revert", {"work_ids": [sid]}),
        ("post", f"/api/v1/library/{sid}/tags", {"tag_id": world["kids"]}),
    ):
        r = c.request(method.upper(), path, headers=h, json=body)
        assert r.status_code in (401, 403), (who, path, r.status_code)
    assert _tagged(sid) and tagging.kids_decision(sid)["via"] == "auto_approved"


def test_dedicated_kids_revert_endpoint_locks_off(world):
    sid = world["works"]["Backyard Starship"]["id"]
    r = world["admin"].post("/api/v1/tags/kids-auto/revert", json={"work_ids": [sid], "mode": "lock_off"}, headers=world["ah"])
    assert r.status_code == 200 and r.json()["reverted"] == 1
    assert tagging.kids_decision(sid)["via"] == "manual_untag" and _kid_sees(world, sid) == set()


def test_classifier_never_readds_after_manual_untag_even_with_auto_approve(world, monkeypatch):
    sid = world["works"]["Backyard Starship"]["id"]
    world["admin"].delete(f"/api/v1/library/{sid}/tags/{world['kids']}", headers=world["ah"])
    tagging.set_auto_approve(enabled=True)
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.99, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "x"})
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "m")
    for _ in range(3):
        tagging.classify_kids_for_work(sid)
        tagging.classify_kids_for_work(sid, force=True)
        tagging.classify_kids_missing(limit=50)
        tagging.auto_approve_pending()
    assert not _tagged(sid) and tagging.kids_decision(sid)["via"] == "manual_untag"
    assert _kid_sees(world, sid) == set()


def test_add_back_to_kids_shelf_is_a_manual_tag(world):
    sid = world["works"]["Plain Novel"]["id"]
    r = world["admin"].post(f"/api/v1/library/{sid}/tags", json={"tag_id": world["kids"]}, headers=world["ah"])
    assert r.status_code == 200 and _tagged(sid)
    assert tagging.kids_decision(sid)["via"] == "manual_tag"
    assert "library" in _kid_sees(world, sid)


def test_bulk_remove_reports_each_item_and_is_final(world):
    w = world["works"]
    ids = [w["Backyard Starship"]["id"], w["Picture Book"]["id"], w["Plain Novel"]["id"], "nope"]
    r = world["admin"].post(f"/api/v1/tags/{world['kids']}/remove-works", json={"work_ids": ids}, headers=world["ah"])
    assert r.status_code == 200, r.text
    res = {i["work_id"]: i["status"] for i in r.json()["results"]}
    assert res == {ids[0]: "removed", ids[1]: "removed", ids[2]: "not_tagged", "nope": "not_found"}
    assert r.json()["removed"] == 2
    for wid in ids[:3]:
        assert not _tagged(wid) and tagging.kids_decision(wid)["via"] == "manual_untag"
    assert db.one("SELECT 1 FROM history WHERE event_type='tag.bulk_removed'")
    assert all(_kid_sees(world, wid) == set() for wid in ids[:3])


def test_bulk_remove_needs_csrf_and_caps_the_batch(world):
    sid = world["works"]["Backyard Starship"]["id"]
    assert world["admin"].post(f"/api/v1/tags/{world['kids']}/remove-works", json={"work_ids": [sid]}).status_code == 403
    assert _tagged(sid)
    assert world["admin"].post(f"/api/v1/tags/{world['kids']}/remove-works", json={"work_ids": [str(i) for i in range(501)]}, headers=world["ah"]).status_code == 422
    assert world["admin"].post("/api/v1/tags/missing/remove-works", json={"work_ids": [sid]}, headers=world["ah"]).status_code == 404


def test_bulk_remove_of_an_ordinary_tag_and_generic_delete_are_audited(world):
    sid = world["works"]["Plain Novel"]["id"]
    tid = tagging.create_tag("cozy")
    tagging.add_tag(sid, tid)
    r = world["admin"].post(f"/api/v1/tags/{tid}/remove-works", json={"work_ids": [sid]}, headers=world["ah"])
    assert r.json()["results"] == [{"work_id": sid, "status": "removed"}]
    assert tagging.kids_decision(sid) is None, "an ordinary tag must not touch kids decisions"
    tagging.add_tag(sid, tid)
    world["admin"].delete(f"/api/v1/library/{sid}/tags/{tid}", headers=world["ah"])
    assert db.one("SELECT 1 FROM history WHERE event_type='tag.removed' AND work_id=?", (sid,))


def test_one_user_facing_label_for_the_kids_tag(world):
    for who in ("admin", "jane", "owen"):
        items = world[who].get("/api/v1/tags").json()["items"]
        kids = [t for t in items if t["name"] == "kids"]
        assert kids and kids[0]["label"] == "Kids shelf", who
        assert all(t["label"] == t["name"] for t in items if t["name"] != "kids")
    assert all(t["label"] == "Kids shelf" for t in world["admin"].get(f"/api/v1/library/{world['works']['Picture Book']['id']}/tags").json()["items"] if t["name"] == "kids")
    # A look-alike tag must not be creatable, and the system tag cannot be renamed or deleted.
    assert world["admin"].post("/api/v1/tags", json={"name": "Kids shelf"}, headers=world["ah"]).status_code == 400
    assert world["admin"].delete(f"/api/v1/tags/{world['kids']}", headers=world["ah"]).status_code == 400


# ---------- bulk add to the kids shelf ----------
def _add(world, ids, tag=None, **kw):
    return world["admin"].post(f"/api/v1/tags/{tag or world['kids']}/add-works", json={"work_ids": ids}, headers=kw.get("headers", world["ah"]))


def _open_task(wid, kind):
    tid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (tid, kind, "work", wid, "t", "open", "{}", db.now()))
    return tid


def test_bulk_add_reports_each_item_records_manual_tag_and_kid_sees_it(world):
    w = world["works"]
    plain, star, picture = w["Plain Novel"]["id"], w["Backyard Starship"]["id"], w["Picture Book"]["id"]
    assert _kid_sees(world, plain) == set()
    r = _add(world, [plain, star, picture, "nope", plain])
    assert r.status_code == 200, r.text
    res = {i["work_id"]: i for i in r.json()["results"]}
    assert res[plain]["status"] == "applied" and res[star]["status"] == "already" and res[picture]["status"] == "already"
    assert res["nope"]["status"] == "skipped" and res["nope"]["reason"]
    assert (r.json()["applied"], r.json()["already"], r.json()["skipped"], r.json()["failed"]) == (1, 2, 1, 0)
    d = tagging.kids_decision(plain)
    assert d["state"] == "on" and d["via"] == "manual_tag" and d["decided_by_name"] == "ada" and d["decided_at"]
    assert tagging.kids_decision(star)["via"] == "auto_approved", "an existing decision is left alone"
    assert db.one("SELECT 1 FROM history WHERE event_type='tag.bulk_added'")
    assert db.one("SELECT 1 FROM history WHERE event_type='kids.manual_on' AND work_id=?", (plain,))
    # Library, search, recent, OPDS and media files, for the kid and the kid's device.
    assert _kid_sees(world, plain) >= {"library", "search", "detail", "recent", "opds", "media", "device-media"}


def test_bulk_add_closes_open_proposal_and_recheck_and_beats_the_classifier(world, monkeypatch):
    plain = world["works"]["Plain Novel"]["id"]
    prop, recheck = _open_task(plain, "kids_tag"), _open_task(plain, "kids_recheck")
    assert _add(world, [plain]).json()["applied"] == 1
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE entity_id=? AND status='open'", (plain,))["n"] == 0
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (prop,))["status"] != "open" and db.one("SELECT status FROM review_tasks WHERE id=?", (recheck,))["status"] != "open"
    tagging.set_auto_approve(enabled=True)
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "not_kids", "confidence": 0.99, "reason": "x"})
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "m")
    for _ in range(3):
        tagging.classify_kids_for_work(plain)
        tagging.classify_kids_for_work(plain, force=True)
        tagging.classify_kids_missing(limit=50)
        tagging.auto_approve_pending()
    assert _tagged(plain) and tagging.kids_decision(plain)["via"] == "manual_tag"
    assert "library" in _kid_sees(world, plain)


def test_bulk_add_of_a_work_the_kid_already_sees_by_grant_is_harmless(world):
    plain = world["works"]["Plain Novel"]["id"]
    owen = db.one("SELECT id FROM users WHERE username='owen'")["id"]
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,granted_by,created_at) VALUES (?,?,?,?)", (owen, plain, owen, db.now()))
    assert "library" in _kid_sees(world, plain)
    assert _add(world, [plain]).json()["applied"] == 1
    assert "library" in _kid_sees(world, plain)


@pytest.mark.parametrize("who", ["jane", "owen", "dev", "anon"])
def test_non_admins_cannot_bulk_add(world, who):
    plain = world["works"]["Plain Novel"]["id"]
    c = TestClient(app) if who == "anon" else world[who]
    h = {} if who in ("anon", "dev") else world[who + "_h"]
    r = c.post(f"/api/v1/tags/{world['kids']}/add-works", json={"work_ids": [plain]}, headers=h)
    assert r.status_code in (401, 403), (who, r.status_code)
    assert not _tagged(plain) and tagging.kids_decision(plain) is None


def test_bulk_add_needs_csrf_caps_at_500_and_404s_unknown_tag(world):
    plain = world["works"]["Plain Novel"]["id"]
    assert world["admin"].post(f"/api/v1/tags/{world['kids']}/add-works", json={"work_ids": [plain]}).status_code == 403
    assert not _tagged(plain)
    assert _add(world, [str(i) for i in range(501)]).status_code == 422
    r = _add(world, [str(i) for i in range(500)])
    assert r.status_code == 200 and r.json()["skipped"] == 500
    assert _add(world, [], tag="x").status_code == 422
    assert _add(world, [plain], tag="missing").status_code == 404


def test_bulk_add_of_an_ordinary_tag_never_touches_kids_decisions(world):
    plain = world["works"]["Plain Novel"]["id"]
    tid = tagging.create_tag("cozy")
    assert _add(world, [plain], tag=tid).json()["results"] == [{"work_id": plain, "status": "applied"}]
    assert _add(world, [plain], tag=tid).json()["results"] == [{"work_id": plain, "status": "already"}]
    assert tagging.kids_decision(plain) is None and not _tagged(plain)


def test_single_add_also_closes_open_kids_proposals(world):
    plain = world["works"]["Plain Novel"]["id"]
    prop = _open_task(plain, "kids_tag")
    assert world["admin"].post(f"/api/v1/library/{plain}/tags", json={"tag_id": world["kids"]}, headers=world["ah"]).status_code == 200
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (prop,))["status"] != "open"
