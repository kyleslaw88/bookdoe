"""Undo for work merges (P5-25a): workmerge.reverse from the ledger, the safety rules around merge, and the
history/reverse API the merge dialog uses."""
from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import tagging, workmerge

from test_work_merge import _asset, _hashes, _kids_tag, _populate, _series, _user, _work

SKIP = ("work_merges", "work_merge_ledger", "history")


def _history_ids():
    with db.session() as conn:
        return {r[0] for r in conn.execute("SELECT id FROM history")}


def test_round_trip_restores_every_row(tmp_path):
    d = _populate(tmp_path)
    with db.session() as conn:
        before = _hashes(conn, skip=SKIP)
    history_before = _history_ids()
    res = workmerge.execute(d["a"], [d["b"]], {}, {"id": d["u"]}, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone() is None
        assert _hashes(conn, skip=SKIP) != before

    out = workmerge.reverse(res["merge_id"], user={"id": d["u"]})

    assert out["status"] == "reversed" and out["already_reversed"] is False and out["warnings"] == [] and out["restored_rows"] == res["ledger_rows"]
    with db.session() as conn:
        assert _hashes(conn, skip=SKIP) == before            # every table, every row, byte for byte
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone()
        assert conn.execute("SELECT status FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()[0] == "reversed"
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger WHERE merge_id=?", (res["merge_id"],)).fetchone()[0] == res["ledger_rows"]  # the ledger is kept
    # the only history difference: the merge event went with the undo and an audit event for the undo exists
    added = _history_ids() - history_before
    with db.session() as conn:
        (ev,) = [conn.execute("SELECT * FROM history WHERE id=?", (i,)).fetchone() for i in added]
    assert ev["event_type"] == workmerge.EVENT_REVERSED and ev["work_id"] == d["a"]
    data = json.loads(ev["data_json"])
    assert data["merge_id"] == res["merge_id"] and data["by"] == d["u"] and data["source_work_ids"] == [d["b"]] and data["forced"] is False
    assert history_before - _history_ids() == set()         # nothing the user had was lost
    assert all(p.exists() for p in d["files"].values())     # files on disk were never touched


def test_reverse_is_idempotent_and_allows_merging_again(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    workmerge.reverse(res["merge_id"])
    with db.session() as conn:
        snap = _hashes(conn)
    again = workmerge.reverse(res["merge_id"])
    assert again["already_reversed"] is True and again["restored_rows"] == 0
    with db.session() as conn:
        assert _hashes(conn) == snap
    res2 = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)       # the source exists again, so it can be merged again
    assert res2["merge_id"] != res["merge_id"]
    assert workmerge.reverse(res2["merge_id"])["status"] == "reversed"


def test_unknown_merge(tmp_path):
    with pytest.raises(workmerge.MergeNotFound):
        workmerge.reverse("nope")


def test_changes_since_the_merge_block_the_undo_until_forced(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.tx() as conn:   # a user rates the merged book afterwards: a row the merge wrote changed
        conn.execute("UPDATE ratings SET rating=1 WHERE work_id=? AND user_id=?", (d["a"], d["other"]))
        conn.execute("UPDATE works SET description='edited later' WHERE id=?", (d["a"],))
    with db.session() as conn:
        merged = _hashes(conn)
    with pytest.raises(workmerge.ReversalBlocked) as exc:
        workmerge.reverse(res["merge_id"])
    tables = {c["table"] for c in exc.value.conflicts}
    assert {"ratings", "works"} <= tables
    with db.session() as conn:
        assert _hashes(conn) == merged                      # nothing was written
        assert conn.execute("SELECT status FROM work_merges").fetchone()[0] == "executed"
    out = workmerge.reverse(res["merge_id"], force=True, user={"id": d["u"]})
    assert out["status"] == "reversed" and {w["table"] for w in out["warnings"]} >= {"ratings", "works"}
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone()
        ev = conn.execute("SELECT data_json FROM history WHERE event_type=?", (workmerge.EVENT_REVERSED,)).fetchone()
    assert json.loads(ev[0])["forced"] is True and json.loads(ev[0])["conflicts"]


def test_new_progress_after_the_merge_does_not_block_the_undo(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    late = _user("late")
    with db.tx() as conn:   # a row the merge did not write
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,4,5)", (d["a"], late))
    assert workmerge.reverse(res["merge_id"])["warnings"] == []


def test_a_database_refusal_rolls_back_marks_the_merge_and_can_be_retried(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        merged = _hashes(conn, skip=("work_merges",))
    with db.tx() as conn:
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON works BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(workmerge.ReversalFailed):
        workmerge.reverse(res["merge_id"])
    with db.session() as conn:
        assert _hashes(conn, skip=("work_merges",)) == merged
        assert conn.execute("SELECT status FROM work_merges").fetchone()[0] == "reversal_failed"
    with db.tx() as conn:
        conn.execute("DROP TRIGGER boom")
    assert workmerge.reverse(res["merge_id"])["status"] == "reversed"


def test_kids_tag_union_is_undone_with_the_merge(tmp_path):
    a, b = _work(), _work()
    tagging.add_tag(b, _kids_tag())
    res = workmerge.execute(a, [b], {"kids_tag_union": True}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_tags WHERE work_id=?", (a,)).fetchone()[0] == 1   # never dropped
    workmerge.reverse(res["merge_id"])
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_tags WHERE work_id=?", (a,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_tags WHERE work_id=?", (b,)).fetchone()[0] == 1


def test_list_merges_names_the_works_before_and_after_the_undo(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, {"id": d["u"]}, confirm_dissimilar=True)
    (m,) = workmerge.list_merges()
    assert m["id"] == res["merge_id"] and m["status"] == "executed" and m["canonical_title"] == "The Hobbit"
    assert m["sources"] == [{"id": d["b"], "title": "The Hobbit (reissue)"}] and m["created_by"] == d["u"]
    workmerge.reverse(res["merge_id"])
    (m,) = workmerge.list_merges()
    assert m["status"] == "reversed" and m["sources"] == [{"id": d["b"], "title": "The Hobbit (reissue)"}]


# ---- merge safety --------------------------------------------------------------------------------

def test_merge_refuses_the_canonical_as_a_source_and_an_already_merged_source(tmp_path):
    d = _populate(tmp_path)
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(d["a"], [d["a"]], {}, None, confirm_dissimilar=True)
    assert [b["code"] for b in exc.value.blockers] == ["source_equals_canonical"]
    other = _work("Elsewhere")
    workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(other, [d["b"]], {}, None, confirm_dissimilar=True)
    assert [b["code"] for b in exc.value.blockers] == ["source_already_merged"]


def _blockers(a, b, **kw):
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(a, [b], {}, None, **kw)
    return [x["code"] for x in exc.value.blockers]


def test_dissimilar_works_need_an_explicit_confirmation():
    black, sword = _work("The Black Circle", "Rick Riordan"), _work("The Sword Thief", "Rick Riordan")
    _series(black, "The 39 Clues", "5")
    _series(sword, "The 39 Clues", "3")
    assert _blockers(black, sword) == ["dissimilar_works"]
    stranger = _work("Cooking for Two", "Someone Else")
    assert _blockers(black, stranger) == ["dissimilar_works"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (sword,)).fetchone()
    assert workmerge.execute(black, [sword], {}, None, confirm_dissimilar=True)["source_work_ids"] == [sword]


@pytest.mark.parametrize("a,b", [
    ("Henry IV, Part 1", "Henry IV, Part 2"),
    ("Warriors: Book 1", "Warriors: Book 10"),
    ("Dune Messiah", "Children of Dune"),
    ("Dune: Messiah", "Dune: Children of Dune"),
    ("The Saga (Part 1)", "The Saga (Part 2)"),
])
def test_the_service_blocks_obviously_different_books_for_every_caller(a, b):
    x, y = _work(a, "A. Writer"), _work(b, "A. Writer")
    assert _blockers(x, y) == ["dissimilar_works"]
    assert workmerge.preview(x, [y])["ok"]                      # the preview stays advisory; execute is the gate


def test_a_shared_identifier_vouches_only_when_the_titles_agree():
    a, b = _work("Dune", "Frank Herbert"), _work("Dune (Dune Chronicles, #1)", "Frank Herbert")
    workmerge.execute(a, [b], {}, None)                           # the same book with a series note: no confirmation
    c, d = _work("Dune", "Frank Herbert"), _work("Dune", "F. Herbert")
    with db.tx() as conn:
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','9780441013593')", (c,))
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES ('ed-d',?,'Ed',1,1)", (d,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition','ed-d','isbn','9780441013593')")
    workmerge.execute(c, [d], {}, None)                           # same ISBN and same title, author spelled differently: fine
    e, f = _work("Totally Different", "X"), _work("Another Name", "Y")
    with db.tx() as conn:
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','9780000000001')", (e,))
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES ('ed-f',?,'Ed',1,1)", (f,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition','ed-f','isbn','9780000000001')")
    assert _blockers(e, f) == ["dissimilar_works"]                # one shared ISBN with different titles is not enough


def test_the_39_clues_suffix_variant_merges_without_confirmation_and_takes_the_clean_title():
    clean, dup = _work("The Black Circle", "Rick Riordan"), _work("The Black Circle (The 39 Clues, #5)", "Rick Riordan")
    _series(clean, "The 39 Clues", "5")
    _series(dup, "The 39 Clues", None)
    _asset(dup, "audiobook", "m4b")
    pre = workmerge.preview(dup, [clean])
    assert pre["ok"] and pre["evidence"][0]["title"]["state"] == "similar"
    workmerge.execute(dup, [clean], {"fields": {"title": "source"}}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT title FROM works WHERE id=?", (dup,)).fetchone()[0] == "The Black Circle"
        assert conn.execute("SELECT position FROM series_memberships WHERE work_id=?", (dup,)).fetchone()[0] == "5"   # the position moved over


def test_progress_conflicts_are_previewed_and_newest_can_win(tmp_path):
    d = _populate(tmp_path)
    with db.tx() as conn:
        for aid, pct, ts in ((d["a_epub"], 10.0, 100.0), (d["b_epub"], 80.0, 200.0)):
            conn.execute("INSERT INTO media_progress(asset_id,user_id,work_id,kind,position,percent,updated_at) VALUES (?,?,?,?,?,?,?)",
                         (aid, d["u"], d["a"] if aid == d["a_epub"] else d["b"], "ebook", pct, pct, ts))
    plan = workmerge.preview(d["a"], [d["b"]])
    (c,) = [c for c in plan["conflicts"] if c["type"] == "progress_conflict"]
    assert (c["canonical_percent"], c["source_percent"], c["newest"], c["choices"]) == (10.0, 80.0, "source", ["canonical", "newest"])
    res = workmerge.execute(d["a"], [d["b"]], {"per_user": "newest"}, None, confirm_dissimilar=True)
    with db.session() as conn:
        row = conn.execute("SELECT percent,updated_at FROM media_progress WHERE asset_id=? AND user_id=?", (d["a_epub"], d["u"])).fetchone()
    assert tuple(row) == (80.0, 200.0)
    workmerge.reverse(res["merge_id"])                      # and the undo brings the canonical's own progress back
    with db.session() as conn:
        assert tuple(conn.execute("SELECT percent,updated_at FROM media_progress WHERE asset_id=? AND user_id=?", (d["a_epub"], d["u"])).fetchone()) == (10.0, 100.0)
        assert tuple(conn.execute("SELECT percent,updated_at FROM media_progress WHERE asset_id=? AND user_id=?", (d["b_epub"], d["u"])).fetchone()) == (80.0, 200.0)


def test_canonical_progress_wins_by_default(tmp_path):
    d = _populate(tmp_path)
    with db.tx() as conn:
        for aid, wid, pct, ts in ((d["a_epub"], d["a"], 10.0, 100.0), (d["b_epub"], d["b"], 80.0, 200.0)):
            conn.execute("INSERT INTO media_progress(asset_id,user_id,work_id,kind,position,percent,updated_at) VALUES (?,?,?,?,?,?,?)", (aid, d["u"], wid, "ebook", pct, pct, ts))
    workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT percent FROM media_progress WHERE asset_id=? AND user_id=?", (d["a_epub"], d["u"])).fetchone()[0] == 10.0


# ---- API -----------------------------------------------------------------------------------------

@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_history_and_reverse_api_round_trip(tmp_path):
    d = _populate(tmp_path)
    client = TestClient(app)
    merged = client.post("/api/v1/works/merge", json={"canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}}).json()
    items = client.get("/api/v1/works/merges").json()["items"]
    assert [i["id"] for i in items] == [merged["merge_id"]] and items[0]["status"] == "executed"
    r = client.post(f"/api/v1/works/merges/{merged['merge_id']}/reverse", json={})
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    assert client.post(f"/api/v1/works/merges/{merged['merge_id']}/reverse").json()["already_reversed"] is True
    assert client.get("/api/v1/works/merges").json()["items"][0]["status"] == "reversed"
    assert client.post("/api/v1/works/merges/nope/reverse").status_code == 404


def test_reverse_api_conflict_is_409_and_force_overrides(tmp_path):
    d = _populate(tmp_path)
    client = TestClient(app)
    mid = client.post("/api/v1/works/merge", json={"canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}}).json()["merge_id"]
    with db.tx() as conn:
        conn.execute("UPDATE works SET description='later' WHERE id=?", (d["a"],))
    r = client.post(f"/api/v1/works/merges/{mid}/reverse", json={})
    assert r.status_code == 409 and r.json()["detail"]["conflicts"][0]["table"] == "works"
    assert client.post(f"/api/v1/works/merges/{mid}/reverse", json={"force": True}).status_code == 200
    assert client.post(f"/api/v1/works/merges/{mid}/reverse", json={"nonsense": 1}).status_code == 422


def test_merge_history_and_reverse_are_admin_only_with_csrf(tmp_path, local_mode):
    with TestClient(app) as anon, TestClient(app) as admin, TestClient(app) as adult:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        d = _populate(tmp_path)
        mid = admin.post("/api/v1/works/merge", json={"canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}}, headers=csrf).json()["merge_id"]
        assert admin.post("/api/v1/users", json={"username": "jane", "password": "a-very-long-jane-password", "account_type": "adult"}, headers=csrf).status_code == 200
        jane = adult.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        jcsrf = {"X-CSRF-Token": jane.json()["csrf"]}
        for client, headers, want in ((anon, {}, 401), (adult, jcsrf, 403)):
            assert client.get("/api/v1/works/merges").status_code == want
            assert client.get("/api/v1/works/merge-candidates", params={"work_ids": f"{d['a']},{d['b']}"}).status_code == want
            assert client.post(f"/api/v1/works/merges/{mid}/reverse", json={}, headers=headers).status_code == want
        assert admin.post(f"/api/v1/works/merges/{mid}/reverse", json={}).status_code == 403                                  # no CSRF
        assert admin.post(f"/api/v1/works/merges/{mid}/reverse", json={}, headers={"X-CSRF-Token": "forged"}).status_code == 403
        with db.session() as conn:
            assert conn.execute("SELECT status FROM work_merges").fetchone()[0] == "executed"
        assert admin.post(f"/api/v1/works/merges/{mid}/reverse", json={}, headers=csrf).status_code == 200
        with db.session() as conn:
            assert conn.execute("SELECT status FROM work_merges").fetchone()[0] == "reversed"
            assert json.loads(conn.execute("SELECT data_json FROM history WHERE event_type=?", (workmerge.EVENT_REVERSED,)).fetchone()[0])["by"]


def test_merge_api_reports_the_dissimilar_blocker_and_accepts_the_confirmation():
    black, sword = _work("The Black Circle", "Rick Riordan"), _work("The Sword Thief", "Rick Riordan")
    _series(black, "The 39 Clues", "5")
    _series(sword, "The 39 Clues", "3")
    client = TestClient(app)
    r = client.post("/api/v1/works/merge", json={"canonical": black, "sources": [sword], "resolutions": {}})
    assert r.status_code == 409 and r.json()["detail"]["blockers"][0]["code"] == "dissimilar_works"
    r = client.post("/api/v1/works/merge", json={"canonical": black, "sources": [sword], "resolutions": {"confirm_dissimilar": True}})
    assert r.status_code == 200
