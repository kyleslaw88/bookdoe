"""Series navigation and series-level tagging.

The book detail names its series (id, name, position) so both shells can link to the series
page; the series page gives an admin per-book shelf/tag state and the bulk actions reuse the
tag endpoints (so the kids shelf records a manual decision per book); and a kid's view of a
series contains only the books that kid may see - no hidden titles, no counts, no numbering
gaps that would reveal a hidden book.
"""
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import player_api
from app.config import settings as base_settings
from app.services import migration, tagging

import helpers
from test_player_api import pair

app = main_module.app
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _add_series(name, members):
    sid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,?,?,?,?)", (sid, name, name, db.now(), db.now()))
        for wid, pos in members:
            conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, pos))
    return sid


@pytest.fixture
def world(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local", public_url="http://books.example.test")
    for mod in (main_module, auth_module, player_api):
        monkeypatch.setattr(mod, "settings", replaced)
    player_api._pair_hits.clear()
    tagging.set_auto_approve(enabled=False)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            assert admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah).status_code == 200
        w = {}
        for title in ("Daniel X 1", "Daniel X 2 Secret", "Daniel X 4 Secret", "Daniel X 6", "Hidden Saga 1", "Hidden Saga 2", "Standalone"):
            wid = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
            aid = helpers.own(wid)
            with db.tx() as conn:
                conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,'{}',1,0,0,?)",
                             (db.uid(), aid, f"/nonexistent/{wid}.epub", 10, db.now()))
            w[title] = wid
        kids = tagging.kids_tag_id()
        for t in ("Daniel X 1", "Daniel X 6"):
            tagging.set_kids_tag_manually(w[t], True, by_user_id="x", by_name="x")
        series = _add_series("Daniel X", [(w["Daniel X 1"], "1"), (w["Daniel X 2 Secret"], "2"), (w["Daniel X 4 Secret"], "4"), (w["Daniel X 6"], "6")])
        hidden = _add_series("Hidden Saga", [(w["Hidden Saga 1"], "1"), (w["Hidden Saga 2"], "2")])
        out = {"admin": admin, "ah": ah, "works": w, "kids": kids, "series": series, "hidden": hidden}
        for name in ("jane", "owen"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            out[name], out[name + "_h"] = c, {"X-CSRF-Token": r.json()["csrf"]}
        out["dev"], _, _ = pair(out["owen"], out["owen_h"])
        yield out


def _on_shelf(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def test_book_detail_names_its_series_for_the_link(world):
    d = world["admin"].get(f"/api/v1/library/{world['works']['Daniel X 2 Secret']}").json()
    assert d["series"] == [{"id": world["series"], "name": "Daniel X", "position": "2"}]
    assert world["admin"].get(f"/api/v1/library/{world['works']['Standalone']}").json()["series"] == []
    kid = world["owen"].get(f"/api/v1/library/{world['works']['Daniel X 1']}").json()
    assert kid["series"][0]["id"] == world["series"]


def test_admin_series_page_has_every_member_with_shelf_state_and_tags(world):
    tid = tagging.create_tag("cozy")
    tagging.add_tag(world["works"]["Daniel X 2 Secret"], tid)
    d = world["admin"].get(f"/api/v1/series/{world['series']}").json()
    assert [w["position"] for w in d["works"]] == ["1", "2", "4", "6"]
    by = {w["title"]: w for w in d["works"]}
    assert by["Daniel X 1"]["on_kids_shelf"] is True and by["Daniel X 2 Secret"]["on_kids_shelf"] is False
    assert [t["name"] for t in by["Daniel X 2 Secret"]["tags"]] == ["cozy"]
    assert [t["label"] for t in by["Daniel X 1"]["tags"]] == ["Kids shelf"]
    assert d["gaps"] == [3, 5]
    assert world["jane"].get(f"/api/v1/series/{world['series']}").json()["gaps"] == [3, 5]


def _all_text(resp):
    return json.dumps(resp.json())


@pytest.mark.parametrize("who", ["owen", "dev"])
def test_kid_sees_only_visible_members_and_no_trace_of_hidden_ones(world, who):
    c = world[who]
    w = world["works"]
    d = c.get(f"/api/v1/series/{world['series']}") if who == "owen" else None
    if d is not None:
        j = d.json()
        assert [x["title"] for x in j["works"]] == ["Daniel X 1", "Daniel X 6"]
        assert j["gaps"] == [], "numbering gaps would reveal hidden books"
        assert "tags" not in j["works"][0] and "on_kids_shelf" not in j["works"][0]
        assert "Secret" not in _all_text(d) and w["Daniel X 2 Secret"] not in _all_text(d)
    lst = c.get("/api/v1/series?limit=100") if who == "owen" else c.get("/api/player/v1/series")
    items = lst.json()["items"]
    assert [i["name"] for i in items] == ["Daniel X"], "a series with no visible book does not exist for a kid"
    mine = items[0]
    assert mine.get("work_count", mine.get("book_count")) == 2 and mine.get("gap_count", 0) == 0
    lib = c.get(f"/api/v1/library?series_id={world['series']}&limit=100") if who == "owen" else c.get(f"/api/player/v1/library?series_id={world['series']}")
    assert sorted(i["title"] for i in lib.json()["items"]) == ["Daniel X 1", "Daniel X 6"]
    assert "Secret" not in _all_text(lib)


def test_kid_gets_404_for_a_series_with_no_visible_member_and_adults_do_not(world):
    assert world["owen"].get(f"/api/v1/series/{world['hidden']}").status_code == 404
    assert world["owen"].get("/api/v1/series/does-not-exist").status_code == 404
    assert world["jane"].get(f"/api/v1/series/{world['hidden']}").status_code == 200
    lib = world["owen"].get(f"/api/v1/library?series_id={world['hidden']}&limit=100").json()
    assert lib["items"] == [] and lib["total"] == 0


def test_series_wide_add_and_remove_use_the_bulk_endpoints_and_record_decisions(world, monkeypatch):
    w = world["works"]
    ids = [x["id"] for x in world["admin"].get(f"/api/v1/series/{world['series']}").json()["works"]]
    assert len(ids) == 4
    r = world["admin"].post(f"/api/v1/tags/{world['kids']}/add-works", json={"work_ids": ids, "series_id": world["series"], "scope": "all"}, headers=world["ah"])
    assert r.status_code == 200, r.text
    assert (r.json()["applied"], r.json()["already"], r.json()["skipped"], r.json()["failed"]) == (2, 2, 0, 0)
    for t in ("Daniel X 2 Secret", "Daniel X 4 Secret"):
        d = tagging.kids_decision(w[t])
        assert _on_shelf(w[t]) and d["state"] == "on" and d["via"] == "manual_tag" and d["decided_by_name"] == "ada"
    ev = db.one("SELECT data_json FROM history WHERE event_type='tag.bulk_added'")
    data = json.loads(ev["data_json"])
    assert data["context"] == {"series_id": world["series"], "scope": "all", "filter": "series"} and data["count"] == 2
    # The classifier cannot move a decided work.
    monkeypatch.setattr(tagging.llmclient, "enabled", lambda: True)
    monkeypatch.setattr(tagging.llmclient, "chat_json", lambda *a, **k: {"verdict": "adult", "confidence": 0.99, "audience_evidence": [], "reason": "x"})
    tagging.classify_kids_for_work(w["Daniel X 2 Secret"], force=True)
    assert _on_shelf(w["Daniel X 2 Secret"]) and tagging.kids_decision(w["Daniel X 2 Secret"])["via"] == "manual_tag"
    # The kid now sees every member, with the true numbering still hidden from them.
    kd = world["owen"].get(f"/api/v1/series/{world['series']}").json()
    assert [x["position"] for x in kd["works"]] == ["1", "2", "4", "6"] and kd["gaps"] == []
    # Subset removal: only the ticked books lose the shelf, each as a recorded manual untag.
    r = world["admin"].post(f"/api/v1/tags/{world['kids']}/remove-works", json={"work_ids": ids[1:3], "series_id": world["series"], "scope": "selected"}, headers=world["ah"])
    assert r.json()["removed"] == 2
    assert not _on_shelf(w["Daniel X 2 Secret"]) and tagging.kids_decision(w["Daniel X 2 Secret"])["via"] == "manual_untag"
    assert _on_shelf(w["Daniel X 1"]) and _on_shelf(w["Daniel X 6"])
    rm = json.loads(db.one("SELECT data_json FROM history WHERE event_type='tag.bulk_removed'")["data_json"])
    assert rm["context"]["series_id"] == world["series"] and rm["context"]["scope"] == "selected"
    # Ordinary tag across the series, with chips visible in the series payload.
    tid = tagging.create_tag("favourite")
    assert world["admin"].post(f"/api/v1/tags/{tid}/add-works", json={"work_ids": ids, "series_id": world["series"], "scope": "all"}, headers=world["ah"]).json()["applied"] == 4
    chips = world["admin"].get(f"/api/v1/series/{world['series']}").json()["works"]
    assert all("favourite" in [t["name"] for t in x["tags"]] for x in chips)


def test_unknown_series_context_is_refused_and_changes_nothing(world):
    wid = world["works"]["Standalone"]
    r = world["admin"].post(f"/api/v1/tags/{world['kids']}/add-works", json={"work_ids": [wid], "series_id": "nope"}, headers=world["ah"])
    assert r.status_code == 404 and not _on_shelf(wid)


@pytest.mark.parametrize("who", ["jane", "owen", "dev", "anon"])
def test_only_an_admin_may_run_series_level_actions(world, who):
    ids = [world["works"]["Daniel X 2 Secret"]]
    c = TestClient(app) if who == "anon" else world[who]
    h = {} if who in ("anon", "dev") else world[who + "_h"]
    body = {"work_ids": ids, "series_id": world["series"], "scope": "all"}
    for verb in ("add-works", "remove-works"):
        assert c.post(f"/api/v1/tags/{world['kids']}/{verb}", json=body, headers=h).status_code in (401, 403)
    assert not _on_shelf(ids[0]) and tagging.kids_decision(ids[0]) is None
    assert world["admin"].post(f"/api/v1/tags/{world['kids']}/add-works", json=body).status_code == 403, "CSRF is required"


def test_both_shells_link_the_book_to_its_series():
    app_js, player_js = (STATIC / "app.js").read_text(), (STATIC / "player.js").read_text()
    assert "Series: ${e(s.name)}" in app_js and "#series/${escAttr(s.id)}" in app_js
    assert "Series: ${e(s.name)}" in player_js and "#series/${e(s.id)}" in player_js
    for label in ("series-kids-add", "series-kids-remove", "series-tag-add", "series-tag-remove", "series-select-all"):
        assert label in app_js
