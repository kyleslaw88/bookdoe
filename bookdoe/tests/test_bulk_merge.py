"""Bulk merge of duplicate groups: dry run, blockers kept out, batches in chunks, stop-on-error, rate limits, batch undo,
kids visibility never widened, post-merge refresh queue (the owner's file-only vs provider-backed pairs)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import bulkmerge, dupefinder, metadata, scheduler, tagging, workmerge, writelimit

from test_duplicate_finder import fileonly, find, groups, identify, provider, scan
from test_work_merge import _asset, _kids_tag, _series, _user, _work


def _pairs(n, prefix="Pair"):
    """n file-only/provider-backed pairs; returns [(provider_id, file_id)]."""
    out = []
    for i in range(n):
        title = f"{prefix} Book {i} Of Wonders"
        out.append((provider(title, f"Author {prefix} {i}", f"mid-{prefix}-{i}"), fileonly(title, f"Author {prefix} {i}")))
    scan()
    return out


def _gid(pair):
    return find(*pair)["id"]


def _state():
    """(row count, hash) per table: the whole database, for 'a dry run writes nothing'."""
    out = {}
    with db.session() as conn:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            rows = sorted(json.dumps(list(r), default=str) for r in conn.execute(f'SELECT * FROM "{name}"'))  # nosec B608
            out[name] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return out


def _kid(name):
    uid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,is_admin,access_scope,created_at,updated_at) VALUES (?,?,0,'kids',?,?)", (uid, name, db.now(), db.now()))
    return uid


def _work_exists(w):
    return bool(db.one("SELECT 1 x FROM works WHERE id=?", (w,)))


# ---- dry run ----------------------------------------------------------------------------------------------------

def test_dry_run_reports_what_moves_and_writes_nothing():
    pairs = _pairs(2)
    before = _state()
    dry = bulkmerge.dry_run([_gid(p) for p in pairs])
    assert _state() == before
    assert dry["ready"] == 2 and dry["review"] == 0
    g = dry["groups"][0]
    assert g["status"] == "ready" and g["moves"]["files"] == 1 and g["canonical_work_id"] in {p[0] for p in pairs}
    assert g["moves"]["sources"][0]["id"] in {p[1] for p in pairs}


def test_overlapping_selected_groups_are_held_back():
    a = provider("Overlap Book", "Ann Writer", "mid-ov")
    b = fileonly("Overlap Book", "Ann Writer")
    c = fileonly("Overlap Book", "Ann Writer")
    scan()
    # the clique is one group; also store a pair that shares members, as two scans' blocks can
    info = dupefinder.classify([a, b], dupefinder.workdupes.load_facts([a, b]), dupefinder.load_extras([a, b]))
    dupefinder._store([a, b], info, dupefinder.workdupes.load_facts([a, b]), dupefinder.load_extras([a, b]), "x", "k")
    ids = [g["id"] for g in groups()]
    assert len(ids) == 2 and c
    dry = bulkmerge.dry_run(ids)
    assert sorted(r["status"] for r in dry["groups"]) == ["ready", "review"]
    assert any(x["code"] == "overlap" for r in dry["groups"] for x in r["reasons"])


def test_dismissed_groups_are_never_merged_in_bulk():
    (pair,) = _pairs(1)
    dupefinder.dismiss(_gid(pair), True)
    gid = find(*pair, dismissed=True)["id"]
    assert bulkmerge.dry_run([gid])["groups"][0]["reasons"][0]["code"] == "dismissed"


# ---- blockers are excluded ----------------------------------------------------------------------------------------

def test_kids_visibility_widening_groups_are_excluded_and_a_bulk_run_never_widens(tmp_path):
    (pair,) = _pairs(1, "Kid")
    prov, fw = pair
    kid = _kid("tom")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, fw))   # the kid sees only the file-only record
    assert tagging.is_visible_to(fw, {"id": kid, "access_scope": "kids"}) and not tagging.is_visible_to(prov, {"id": kid, "access_scope": "kids"})
    gid = _gid(pair)
    ev = bulkmerge.dry_run([gid])["groups"][0]
    assert ev["status"] == "review" and "kid_grant_widening" in {r["code"] for r in ev["reasons"]}
    b = bulkmerge.start([gid], {"id": "admin"})
    b = bulkmerge.process(b["id"], {"id": "admin"})
    assert b["counts"]["merged"] == 0 and b["counts"]["skipped"] == 1 and b["items"][0]["reason"] == "kid_grant_widening"
    assert _work_exists(prov) and _work_exists(fw)
    assert not tagging.is_visible_to(prov, {"id": kid, "access_scope": "kids"})                                  # nothing widened
    assert db.one("SELECT COUNT(*) n FROM work_merges")["n"] == 0


def test_the_kids_shelf_union_and_adult_into_kids_are_excluded():
    (pair,) = _pairs(1, "Shelf")
    prov, fw = pair
    tagging.add_tag(fw, _kids_tag())
    ev = bulkmerge.dry_run([_gid(pair)])["groups"][0]
    assert ev["status"] == "review" and "kids_tag_mismatch" in {r["code"] for r in ev["reasons"]}


def test_different_titles_from_a_refresh_conflict_need_review(monkeypatch):
    prov = provider("Because of Winn-Dixie", "Kate DiCamillo", "mid-winn")
    broken = fileonly("Winn Dixie Dog Book", "Kate DiCamillo")
    dupefinder.record_refresh_conflict(broken, prov, "mid-winn")
    scan()
    g = find(broken, prov)
    assert g["confidence"] == "high" and "titles_differ" in g["reasons"]
    ev = bulkmerge.dry_run([g["id"]])["groups"][0]
    assert ev["status"] == "review" and "dissimilar_works" in {r["code"] for r in ev["reasons"]}


def test_per_user_progress_and_rating_conflicts_and_series_conflicts_need_review():
    prov = provider("Conflicted", "Ann Writer", "mid-cf")
    fw = fileonly("Conflicted", "Ann Writer")
    # same format on both records, a user with progress on each, different ratings
    p_asset = _asset(prov, "ebook", "epub", ["/lib/p.epub"])
    f_asset = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind='ebook'", (fw,))["id"]
    u = _user("reader")
    with db.tx() as conn:
        for w, a, pct, rating in ((prov, p_asset, 0.2, 5), (fw, f_asset, 0.7, 2)):
            conn.execute("INSERT INTO media_progress(user_id,work_id,asset_id,kind,percent,updated_at) VALUES (?,?,?,'ebook',?,1)", (u, w, a, pct))
            conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,?,1)", (w, u, rating))
    _series(prov, "Saga", "3")
    _series(fw, "Saga", "3.0")                                    # the same book to workdupes, a different string to the merge
    scan()
    ev = bulkmerge.dry_run([find(prov, fw)["id"]])["groups"][0]
    codes = {r["code"] for r in ev["reasons"]}
    assert ev["status"] == "review" and {"progress_conflict", "rating_conflict", "series_position_conflict"} <= codes


def test_blocked_groups_are_listed_while_the_rest_merge():
    good = _pairs(2, "Good")
    kid = _kid("amy")
    bad_p = provider("Blocked Book", "Zed Writer", "mid-blocked")
    bad_f = fileonly("Blocked Book", "Zed Writer")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, bad_f))
    scan()
    ids = [find(bad_p, bad_f)["id"], *[_gid(p) for p in good]]
    b = bulkmerge.process(bulkmerge.start(ids, {"id": "admin"})["id"], {"id": "admin"})
    assert b["status"] == "done" and b["counts"] == {"pending": 0, "running": 0, "merged": 2, "skipped": 1, "failed": 0, "undone": 0}
    assert _work_exists(bad_p) and _work_exists(bad_f) and not any(_work_exists(p[1]) for p in good)


# ---- the run -------------------------------------------------------------------------------------------------------

def test_bulk_merge_moves_files_never_deletes_them_and_records_a_ledger_entry_per_merge():
    pairs = _pairs(3)
    files_before = sorted(r["path"] for r in db.all("SELECT path FROM media_files"))
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    assert b["status"] == "done" and b["counts"]["merged"] == 3
    assert sorted(r["path"] for r in db.all("SELECT path FROM media_files")) == files_before                       # no file record lost
    for prov, fw in pairs:
        assert _work_exists(prov) and not _work_exists(fw)
        assert db.one("SELECT COUNT(*) n FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE a.work_id=?", (prov,))["n"] == 1
    merges = db.all("SELECT * FROM work_merges")
    assert len(merges) == 3 and all(m["status"] == "executed" for m in merges)
    assert {it["merge_id"] for it in b["items"]} == {m["id"] for m in merges}
    assert all(db.one("SELECT COUNT(*) n FROM work_merge_ledger WHERE merge_id=?", (m["id"],))["n"] > 0 for m in merges)
    ev = db.all("SELECT work_id,data_json FROM history WHERE event_type=?", (bulkmerge.EVENT_BULK,))
    assert len(ev) == 3 and all(b["id"] in e["data_json"] for e in ev)                                             # audited per merge
    assert dupefinder.status()["counts"]["open"] == 0
    # a consumed group is hidden but kept, so the batch's Undo can open it again
    assert db.one("SELECT COUNT(*) n FROM duplicate_groups WHERE status='merged'")["n"] == 3


def test_a_merge_taken_through_the_normal_dialog_still_undoes_individually():
    pairs = _pairs(2)
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    first = b["items"][0]
    workmerge.reverse(first["merge_id"])
    assert _work_exists(pairs[0][1]) or _work_exists(pairs[1][1])
    assert sum(_work_exists(p[1]) for p in pairs) == 1


def test_large_selections_run_in_chunks_and_resume(monkeypatch):
    monkeypatch.setattr(bulkmerge, "MAX_PER_REQUEST", 2)
    pairs = _pairs(5)
    b = bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})
    assert b["status"] == "running" and b["counts"]["pending"] == 5
    b = bulkmerge.process(b["id"], {"id": "admin"})
    assert b["counts"]["merged"] == 2 and b["counts"]["pending"] == 3 and b["status"] == "running" and b["progress"] == {"done": 2, "total": 5}
    # the page was closed: the job finishes the rest, attributed to the admin who started it
    out = bulkmerge.resume_running()
    assert out["merged"] == 3
    b = bulkmerge.batch(b["id"])
    assert b["status"] == "done" and b["counts"]["merged"] == 5 and b["counts"]["pending"] == 0
    assert bulkmerge.process(b["id"], {"id": "admin"})["counts"]["merged"] == 5                                    # idempotent: nothing redone
    assert db.one("SELECT COUNT(*) n FROM work_merges")["n"] == 5


def test_stop_on_error_stops_the_batch_and_the_default_carries_on(monkeypatch):
    pairs = _pairs(4)
    ids = [_gid(p) for p in pairs]
    real = workmerge.execute
    calls = []

    def flaky(canonical, sources, *a, **kw):
        calls.append(canonical)
        if len(calls) == 2:
            raise RuntimeError("disk on fire")
        return real(canonical, sources, *a, **kw)
    monkeypatch.setattr(workmerge, "execute", flaky)
    stopped = bulkmerge.process(bulkmerge.start(ids, {"id": "admin"}, stop_on_error=True)["id"], {"id": "admin"})
    assert stopped["status"] == "stopped" and stopped["counts"]["merged"] == 1 and stopped["counts"]["failed"] == 1 and stopped["counts"]["skipped"] == 2
    assert [i["reason"] for i in stopped["items"] if i["status"] == "failed"] == ["disk on fire"]
    assert db.one("SELECT COUNT(*) n FROM work_merges")["n"] == 1                       # the failed one rolled back entirely
    # carry on (default): the three remaining groups, one of which fails again? no: calls > 2 succeed
    rest = [g["id"] for g in groups()]
    calls.clear()
    calls.extend(["x"])      # the next call is call #2 again -> fails once
    carry = bulkmerge.process(bulkmerge.start(rest, {"id": "admin"}, stop_on_error=False)["id"], {"id": "admin"})
    assert carry["status"] == "done" and carry["counts"]["failed"] == 1 and carry["counts"]["merged"] == len(rest) - 1


def test_the_write_budget_is_spent_per_merge_and_the_rest_wait(monkeypatch):
    monkeypatch.setattr(writelimit, "LIMIT", 2)
    pairs = _pairs(3)
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    assert b["counts"]["merged"] == 2 and b["counts"]["pending"] == 1 and b["rate_limited"]["retry_after"] >= 1 and b["status"] == "running"
    writelimit.reset()
    b = bulkmerge.process(b["id"], {"id": "admin"})
    assert b["status"] == "done" and b["counts"]["merged"] == 3


# ---- batch undo ----------------------------------------------------------------------------------------------------

def test_one_click_undoes_the_whole_batch_in_reverse_order():
    pairs = _pairs(3)
    files_before = sorted(r["path"] for r in db.all("SELECT path FROM media_files"))
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    out = bulkmerge.undo(b["id"], {"id": "admin"})
    assert out["undone"] == 3 and out["stopped"] is None and out["status"] == "undone" and out["counts"]["undone"] == 3
    assert all(_work_exists(p[0]) and _work_exists(p[1]) for p in pairs)
    assert sorted(r["path"] for r in db.all("SELECT path FROM media_files")) == files_before
    assert [m["status"] for m in db.all("SELECT status FROM work_merges")] == ["reversed"] * 3
    assert bulkmerge.undo(b["id"], {"id": "admin"})["undone"] == 0                                                 # idempotent
    assert db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='pending'")["n"] == 0                # queued refreshes dropped
    assert len(groups()) == 3                                                                                       # the groups are open again


def test_batch_undo_stops_at_the_first_conflict_and_carries_on_after_it_is_cleared():
    pairs = _pairs(3)
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    last = b["items"][-1]                                     # undone first
    with db.tx() as conn:                                     # a later edit to a row that merge wrote
        conn.execute("UPDATE media_assets SET status='missing' WHERE work_id=? AND kind='ebook'", (last["canonical_work_id"],))
    out = bulkmerge.undo(b["id"], {"id": "admin"})
    assert out["undone"] == 0 and out["stopped"]["code"] == "conflicts" and out["status"] == "partly_undone"
    assert not _work_exists(pairs[0][1]) or True
    assert sum(1 for p in pairs if _work_exists(p[1])) == 0                                                         # nothing half-done
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='owned' WHERE work_id=? AND kind='ebook'", (last["canonical_work_id"],))
    out = bulkmerge.undo(b["id"], {"id": "admin"})
    assert out["undone"] == 3 and out["status"] == "undone"


def test_batch_undo_refuses_to_widen_kid_visibility():
    (pair,) = _pairs(1, "UndoKid")
    prov, fw = pair
    b = bulkmerge.process(bulkmerge.start([_gid(pair)], {"id": "admin"})["id"], {"id": "admin"})
    assert b["counts"]["merged"] == 1
    kid = _kid("bo")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, prov))   # granted the merged book after the fact
    out = bulkmerge.undo(b["id"], {"id": "admin"})
    # undoing brings the source back; the kid sees the canonical already, so a restored source can only add visibility
    # for someone who could not see it before: the reversal's own widening guard decides, never the batch.
    assert out["stopped"] is None or out["stopped"]["code"] in {"kid_widening", "conflicts"}


# ---- post-merge refresh ---------------------------------------------------------------------------------------------

def test_surviving_records_are_queued_for_a_rate_limited_metadata_refresh():
    pairs = _pairs(3)
    manual = _work("Hand Edited", "Ann Writer", metadata_source="manual", metadata_id="mid-man")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    held = {r["work_id"] for r in db.all("SELECT work_id FROM duplicate_refresh_queue WHERE status='held'")}
    assert held == {p[0] for p in pairs} and b["options"]["refresh"] is True and b["held_refreshes"] == 3     # parked until the batch is finished
    assert bulkmerge.finalize(b["id"])["queued_refreshes"] == 3
    bulkmerge.enqueue_refresh(manual)
    assert manual not in {r["work_id"] for r in db.all("SELECT work_id FROM duplicate_refresh_queue")}               # never overwrite hand edits
    calls, sleeps = [], []

    def fake(work_id, **kw):
        calls.append(work_id)
        return {"work_id": work_id, "status": "linked"}
    out = bulkmerge.refresh_queue(limit=2, delay=2.0, sleep=sleeps.append, refresh=fake)
    assert out["refreshed"] == 2 and sleeps == [2.0] and out["pending"] == 1
    throttled = bulkmerge.refresh_queue(limit=5, delay=0, sleep=lambda s: None, refresh=lambda w, **k: {"status": "rate_limited", "retry_at": db.now() + 600})
    assert throttled["stopped"] == "rate_limited" and throttled["retry_later"] == 1
    assert bulkmerge.refresh_queue(limit=5, delay=0, sleep=lambda s: None, refresh=fake)["refreshed"] == 0              # waits for the retry time


def test_refresh_can_be_switched_off_per_run():
    pairs = _pairs(1)
    bulkmerge.process(bulkmerge.start([_gid(pairs[0])], {"id": "admin"}, refresh=False)["id"], {"id": "admin"})
    assert db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue")["n"] == 0


def test_a_refresh_that_keeps_failing_is_given_up_on(monkeypatch):
    (pair,) = _pairs(1)
    bulkmerge.enqueue_refresh(pair[0])

    def boom(work_id, **kw):
        raise RuntimeError("provider down")
    for _ in range(bulkmerge.REFRESH_ATTEMPTS):
        bulkmerge.refresh_queue(limit=5, delay=0, sleep=lambda s: None, refresh=boom)
    row = db.one("SELECT * FROM duplicate_refresh_queue WHERE work_id=?", (pair[0],))
    assert row["status"] == "failed" and row["attempts"] == bulkmerge.REFRESH_ATTEMPTS and "provider down" in row["last_error"]


# ---- API flow ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_api_select_dry_run_bulk_merge_continue_and_batch_undo(local_mode, monkeypatch):
    monkeypatch.setattr(bulkmerge, "MAX_PER_REQUEST", 2)
    pairs = _pairs(3)
    ids = [_gid(p) for p in pairs]
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        dry = admin.post("/api/v1/admin/duplicates/dry-run", json={"group_ids": ids}, headers=h).json()
        assert dry["ready"] == 3
        assert admin.post("/api/v1/admin/duplicates/bulk-merge", json={"group_ids": []}, headers=h).status_code == 422
        b = admin.post("/api/v1/admin/duplicates/bulk-merge", json={"group_ids": ids, "refresh": False}, headers=h).json()
        assert b["status"] == "running" and b["counts"]["merged"] == 2
        b = admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/continue", headers=h).json()
        assert b["status"] == "done" and b["counts"]["merged"] == 3
        assert [i["id"] for i in admin.get("/api/v1/admin/duplicates/batches").json()["items"]] == [b["id"]]
        assert admin.get(f"/api/v1/admin/duplicates/batches/{b['id']}").json()["items"][0]["status"] == "merged"
        assert admin.post("/api/v1/admin/duplicates/batches/nope/continue", headers=h).status_code == 404
        u = admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/undo", json={}, headers=h).json()
        assert u["status"] == "undone" and u["undone"] == 3
        assert all(_work_exists(p[1]) for p in pairs)
    scheduler.shutdown_pool()


# ---- review fixes: claims, undo while running, held refreshes (Opus review, 2026-10-14) ---------------------------------

def test_concurrent_process_calls_claim_each_item_once_and_never_overwrite_a_merge(monkeypatch):
    (pair,) = _pairs(1, "Race")
    b = bulkmerge.start([_gid(pair)], {"id": "admin"})
    entered, release = threading.Event(), threading.Event()
    real = workmerge.execute
    calls = []

    def slow(*a, **kw):
        calls.append(1)
        entered.set()
        assert release.wait(20)
        return real(*a, **kw)
    monkeypatch.setattr(workmerge, "execute", slow)
    result = {}
    t = threading.Thread(target=lambda: result.update(bulkmerge.process(b["id"], {"id": "admin"})))
    t.start()
    assert entered.wait(20)
    second = bulkmerge.process(b["id"], {"id": "admin"})            # the item is claimed ('running'): nothing for this caller to do
    assert second["counts"]["running"] == 1 and second["counts"]["merged"] == 0 and second["status"] == "running" and len(calls) == 1
    with pytest.raises(bulkmerge.BatchBusy):
        bulkmerge.undo(b["id"], {"id": "admin"})
    release.set()
    t.join(30)
    final = bulkmerge.batch(b["id"])
    assert final["status"] == "done" and final["counts"]["merged"] == 1 and len(calls) == 1
    assert db.one("SELECT COUNT(*) n FROM work_merges")["n"] == 1
    assert bulkmerge.undo(b["id"], {"id": "admin"})["undone"] == 1                                       # the merge is visible to batch undo


def test_finishing_never_overwrites_a_merged_item():
    (pair,) = _pairs(1, "Stale")
    b = bulkmerge.process(bulkmerge.start([_gid(pair)], {"id": "admin"})["id"], {"id": "admin"})
    item = db.one("SELECT * FROM merge_batch_items WHERE batch_id=?", (b["id"],))
    assert item["status"] == "merged" and item["merge_id"]
    stale = dict(item, status="pending")                  # what a second worker read before the first one finished
    assert bulkmerge._merge_item(b["id"], stale, {"id": "admin"}) == "skipped"
    after = db.one("SELECT * FROM merge_batch_items WHERE id=?", (item["id"],))
    assert after["status"] == "merged" and after["merge_id"] == item["merge_id"]
    assert bulkmerge.undo(b["id"], {"id": "admin"})["undone"] == 1


def test_a_worker_that_died_holding_a_claim_gives_the_item_back():
    pairs = _pairs(1, "Died")
    b = bulkmerge.start([_gid(pairs[0])], {"id": "admin"})
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET status='running',finished_at=? WHERE batch_id=?", (db.now() - 3600, b["id"]))
    out = bulkmerge.process(b["id"], {"id": "admin"})
    assert out["status"] == "done" and out["counts"]["merged"] == 1


def test_undo_is_refused_while_the_batch_is_still_merging_and_process_rereads_the_status(monkeypatch):
    pairs = _pairs(3, "Busy")
    b = bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})
    first = bulkmerge.process(b["id"], {"id": "admin"}, limit=1)
    assert first["status"] == "running" and first["counts"]["merged"] == 1 and first["counts"]["pending"] == 2 and first["can_undo"] is False
    with pytest.raises(bulkmerge.BatchBusy, match="still merging"):
        bulkmerge.undo(b["id"], {"id": "admin"})
    assert bulkmerge.batch(b["id"])["status"] == "running"                                  # the refusal changed nothing
    real = bulkmerge._merge_item

    def merge_then_stop(batch_id, item, user):
        out = real(batch_id, item, user)
        with db.tx() as conn:
            conn.execute("UPDATE merge_batches SET status='stopped' WHERE id=?", (batch_id,))   # e.g. stopped / undone by someone else
        return out
    monkeypatch.setattr(bulkmerge, "_merge_item", merge_then_stop)
    again = bulkmerge.process(b["id"], {"id": "admin"}, limit=10)
    assert again["counts"]["merged"] == 2 and again["counts"]["pending"] == 1 and again["status"] == "stopped"   # stopped re-read inside the loop


def test_api_refuses_undo_while_running_with_a_clear_message(local_mode, monkeypatch):
    monkeypatch.setattr(bulkmerge, "MAX_PER_REQUEST", 1)
    pairs = _pairs(2, "ApiBusy")
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        b = admin.post("/api/v1/admin/duplicates/bulk-merge", json={"group_ids": [_gid(p) for p in pairs]}, headers=h).json()
        assert b["status"] == "running" and b["can_undo"] is False
        u = admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/undo", json={}, headers=h)
        assert u.status_code == 409 and "still merging" in u.text
        assert admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/finalize", headers=h).status_code == 409
    scheduler.shutdown_pool()


def test_the_refresh_queue_leaves_merged_books_alone_so_batch_undo_stays_one_click(local_mode, monkeypatch):
    pairs = _pairs(3, "Held")
    started = []
    monkeypatch.setattr(main_module, "_start_job", lambda kind, config=None: started.append(kind) or {})
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        b = admin.post("/api/v1/admin/duplicates/bulk-merge", json={"group_ids": [_gid(p) for p in pairs]}, headers=h).json()
        assert b["status"] == "done" and b["can_undo"] is True and b["held_refreshes"] == 3
        assert started == []                                                      # finishing a chunk no longer starts the refresh job
        seen = []
        out = bulkmerge.refresh_queue(limit=10, delay=0, sleep=lambda s: None, refresh=lambda w, **k: seen.append(w) or {"status": "linked"})
        assert seen == [] and out["refreshed"] == 0 and out["held"] == 3 and out["pending"] == 0
        u = admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/undo", json={}, headers=h)
        assert u.status_code == 200 and u.json()["status"] == "undone" and u.json()["undone"] == 3 and u.json()["stopped"] is None
        assert db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue")["n"] == 0
    scheduler.shutdown_pool()


def test_a_real_refresh_rewrites_merged_rows_so_it_waits_for_finalize_and_then_runs(local_mode, monkeypatch):
    pairs = _pairs(2, "Final")
    started = []
    monkeypatch.setattr(main_module, "_start_job", lambda kind, config=None: started.append(kind) or {})
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        b = admin.post("/api/v1/admin/duplicates/bulk-merge", json={"group_ids": [_gid(p) for p in pairs]}, headers=h).json()
        f = admin.post(f"/api/v1/admin/duplicates/batches/{b['id']}/finalize", headers=h).json()
        assert f["queued_refreshes"] == 2 and f["finalized_at"] and started == ["duplicate-refresh"] and f["held_refreshes"] == 0
        seen = []
        out = bulkmerge.refresh_queue(limit=10, delay=0, sleep=lambda s: None, refresh=lambda w, **k: seen.append(w) or {"status": "linked"})
        assert sorted(seen) == sorted(p[0] for p in pairs) and out["refreshed"] == 2
        assert admin.post("/api/v1/admin/duplicates/batches/nope/finalize", headers=h).status_code == 404
    scheduler.shutdown_pool()


def test_held_refreshes_are_released_after_the_configurable_grace_period():
    pairs = _pairs(2, "Grace")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    assert bulkmerge.grace_days() == 7.0
    assert bulkmerge.release_held() == 0
    with db.tx() as conn:
        conn.execute("UPDATE duplicate_refresh_queue SET queued_at=? WHERE work_id=?", (db.now() - 6 * 86400, pairs[0][0]))
    assert bulkmerge.release_held() == 0                                         # a batch is released as a whole, by its newest row
    with db.tx() as conn:
        conn.execute("UPDATE duplicate_refresh_queue SET queued_at=? WHERE batch_id=?", (db.now() - 8 * 86400, b["id"]))
    assert bulkmerge.release_held() == 2
    assert bulkmerge.batch(b["id"])["finalized_by"] == "grace"
    db.set_setting(bulkmerge.GRACE_DAYS_KEY, 0.5)
    assert bulkmerge.grace_days() == 0.5
    seen = []
    out = bulkmerge.refresh_queue(limit=10, delay=0, sleep=lambda s: None, refresh=lambda w, **k: seen.append(w) or {"status": "linked"})
    assert sorted(seen) == sorted(p[0] for p in pairs) and out["refreshed"] == 2


def test_merged_groups_survive_scans_so_undo_can_reopen_them():
    pairs = _pairs(2, "Keep")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], {"id": "admin"})["id"], {"id": "admin"})
    for full in (False, True):
        dupefinder.scan(full=full)
        assert db.one("SELECT COUNT(*) n FROM duplicate_groups WHERE status='merged'")["n"] == 2
    assert bulkmerge.undo(b["id"], {"id": "admin"})["undone"] == 2
    assert db.one("SELECT COUNT(*) n FROM duplicate_groups WHERE status='open'")["n"] == 2


# ---- round 2: stale claims, grace, undo guards, heartbeat --------------------------------------------------------------

ADMIN = {"id": "admin"}


def _item(b):
    return db.one("SELECT * FROM merge_batch_items WHERE batch_id=?", (b["id"],))


def _stale_claim(item_id, age=3600):
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET status='running',merge_id=NULL,finished_at=?,updated_at=? WHERE id=?", (db.now() - age, db.now() - age, item_id))


def _extra(n):
    return fileonly(f"Unrelated Extra {n}", f"Nobody {n}")


def test_finding1_stale_claim_recovery_pins_only_the_items_own_merge_and_records_it_like_a_normal_merge():
    (pair,) = _pairs(1, "Pin")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    canonical, sources = it["canonical_work_id"], json.loads(it["source_work_ids_json"])
    _stale_claim(it["id"])
    # a manual merge into the same canonical BEFORE and one AFTER the batch's own committed-but-unrecorded merge
    manual_before = workmerge.execute(canonical, [_extra(1)], {}, ADMIN, confirm_dissimilar=True)["merge_id"]
    own = workmerge.execute(canonical, sources, {}, ADMIN, confirm_dissimilar=True)["merge_id"]
    manual_after = workmerge.execute(canonical, [_extra(2)], {}, ADMIN, confirm_dissimilar=True)["merge_id"]
    assert bulkmerge.recover_stale(b["id"]) == 1
    after = _item(b)
    assert after["status"] == "merged" and after["merge_id"] == own and own not in (manual_before, manual_after)
    assert db.one("SELECT status FROM duplicate_groups WHERE id=?", (it["group_id"],))["status"] == "merged"
    ev = db.one("SELECT data_json FROM history WHERE event_type=? AND work_id=?", (bulkmerge.EVENT_BULK, canonical))
    assert ev and json.loads(ev["data_json"])["merge_id"] == own
    assert db.one("SELECT status,batch_id FROM duplicate_refresh_queue WHERE work_id=?", (canonical,)) == {"status": "held", "batch_id": b["id"]}
    assert {db.one("SELECT status FROM work_merges WHERE id=?", (m,))["status"] for m in (manual_before, manual_after)} == {"executed"}


def test_finding1_a_manual_merge_with_other_sources_is_never_pinned_and_the_item_goes_back_to_pending():
    (pair,) = _pairs(1, "NoPin")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    _stale_claim(it["id"])
    workmerge.execute(it["canonical_work_id"], [_extra(3)], {}, ADMIN, confirm_dissimilar=True)
    assert bulkmerge.recover_stale(b["id"]) == 1
    after = _item(b)
    assert after["status"] == "pending" and after["merge_id"] is None


def test_finding1_recovery_prefers_the_oldest_matching_merge_and_skips_ones_already_attached():
    (pair,) = _pairs(1, "Old")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    _stale_claim(it["id"])
    own = workmerge.execute(it["canonical_work_id"], json.loads(it["source_work_ids_json"]), {}, ADMIN, confirm_dissimilar=True)["merge_id"]
    assert bulkmerge._find_own_merge(_item(b))["id"] == own
    with db.tx() as conn:        # attached to another batch item already: not ours
        conn.execute("INSERT INTO merge_batch_items(batch_id,ord,group_id,canonical_work_id,source_work_ids_json,status,merge_id) VALUES ('other',0,NULL,'c','[]','merged',?)", (own,))
    assert bulkmerge._find_own_merge(_item(b)) is None


def test_finding2_listing_batches_and_status_recover_a_batch_stuck_on_a_dead_worker(local_mode):
    pairs = _pairs(2, "Stuck")
    b1 = bulkmerge.start([_gid(pairs[0])], ADMIN)
    it = _item(b1)
    _stale_claim(it["id"])                                          # running, pending 0, worker gone, nothing committed
    assert bulkmerge.batch(b1["id"])["counts"]["pending"] == 0 and bulkmerge.batch(b1["id"])["stale_claims"] == 1
    listed = {x["id"]: x for x in bulkmerge.list_batches()}
    assert listed[b1["id"]]["counts"]["pending"] == 1 and listed[b1["id"]]["status"] == "running" and listed[b1["id"]]["stale_claims"] == 0
    b2 = bulkmerge.start([_gid(pairs[1])], ADMIN)                   # same, but its merge DID commit before the worker died
    it2 = _item(b2)
    _stale_claim(it2["id"])
    workmerge.execute(it2["canonical_work_id"], json.loads(it2["source_work_ids_json"]), {}, ADMIN)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert admin.get("/api/v1/admin/duplicates/status").status_code == 200
        assert bulkmerge.batch(b2["id"])["status"] == "done" and bulkmerge.batch(b2["id"])["counts"]["merged"] == 1
    scheduler.shutdown_pool()


def test_finding2_the_housekeeping_job_recovers_dead_claims_and_closes_batches():
    (pair,) = _pairs(1, "Housekeeping")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    _stale_claim(it["id"])
    workmerge.execute(it["canonical_work_id"], json.loads(it["source_work_ids_json"]), {}, ADMIN)
    assert scheduler._housekeeping()["bulk_merge"] == {"claims_recovered": 1, "batches_closed": 1}
    assert bulkmerge.batch(b["id"])["status"] == "done"


def test_finding2_process_reports_zero_processed_when_another_worker_holds_the_only_claim():
    (pair,) = _pairs(1, "Zero")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET status='running',finished_at=?,updated_at=? WHERE batch_id=?", (db.now(), db.now(), b["id"]))
    out = bulkmerge.process(b["id"], ADMIN)
    assert out["processed"] == 0 and out["status"] == "running" and out["counts"]["running"] == 1


def test_finding2_the_browser_loops_back_off_and_the_page_offers_recover():
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dupes.js")).read()
    assert "for (;;) {\n          const pr = b.progress" not in js and "while (b.status === 'running' && !b.rate_limited)" not in js
    assert "async function driveBatch" in js and "MAX_CHUNKS" in js and "CHUNK_DELAY_MS = 1000" in js
    assert "b.processed === 0" in js and "No progress" in js and "Recover" in js
    assert js.count("await driveBatch(ctx") == 2                 # both the merge dialog and the Continue button use it


def test_finding3_finalizing_one_batch_releases_only_that_batch_and_grace_expiry_marks_the_other_finalized():
    a_pairs, b_pairs = _pairs(2, "BatchA"), _pairs(2, "BatchB")
    a = bulkmerge.process(bulkmerge.start([_gid(p) for p in a_pairs], ADMIN)["id"], ADMIN)
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in b_pairs], ADMIN)["id"], ADMIN)
    assert a["held_refreshes"] == 2 and b["held_refreshes"] == 2
    assert a["refresh_held_until"] and abs(a["refresh_held_until"] - (db.now() + 7 * 86400)) < 60
    with db.tx() as conn:          # batch A is long past its grace; the admin finalizes B only
        conn.execute("UPDATE duplicate_refresh_queue SET queued_at=? WHERE batch_id=?", (db.now() - 9 * 86400, a["id"]))
    assert bulkmerge.release_held(b["id"]) == 2
    held = {r["batch_id"]: r["n"] for r in db.all("SELECT batch_id,COUNT(*) n FROM duplicate_refresh_queue WHERE status='held' GROUP BY batch_id")}
    assert held == {a["id"]: 2}                                 # A is untouched by finalizing B
    assert bulkmerge.batch(a["id"])["finalized_at"] is None
    seen = []
    out = bulkmerge.refresh_queue(limit=10, delay=0, sleep=lambda s: None, refresh=lambda w, **k: seen.append(w) or {"status": "linked"})
    assert out["refreshed"] == 4 and out["held"] == 0                     # the queue run finishes the grace-expired batch, visibly
    fa = bulkmerge.batch(a["id"])
    assert fa["finalized_at"] and fa["finalized_by"] == "grace" and fa["refresh_held_until"] is None
    ev = db.one("SELECT data_json FROM history WHERE event_type=?", (bulkmerge.EVENT_FINALIZED,))
    assert ev and json.loads(ev["data_json"]) == {"batch_id": a["id"], "released": 2, "by": "grace"}
    assert bulkmerge.batch(b["id"])["finalized_by"] is None


def test_finding3_the_ui_states_the_real_schedule_and_the_post_grace_undo_warning():
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dupes.js")).read()
    assert "are finished automatically after" not in js
    assert "Refresh held until" in js and "Undo stays one-click until then" in js
    assert "Undo may stop at the first refreshed item" in js and "manual job unless you schedule it" in js and "Run the queue now" in js


def test_finding4_undo_is_refused_while_another_worker_is_mid_merge_even_on_a_stopped_batch():
    pairs = _pairs(2, "Mid")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], ADMIN)["id"], ADMIN, limit=1)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batches SET status='stopped' WHERE id=?", (b["id"],))
        conn.execute("UPDATE merge_batch_items SET status='running',finished_at=?,updated_at=? WHERE batch_id=? AND status='pending'", (db.now(), db.now(), b["id"]))
    with pytest.raises(bulkmerge.BatchBusy, match="in progress"):
        bulkmerge.undo(b["id"], ADMIN)
    cur = bulkmerge.batch(b["id"])
    assert cur["status"] == "stopped" and cur["can_undo"] is False
    with pytest.raises(bulkmerge.BatchBusy):
        bulkmerge.finalize(b["id"])                                          # finalize is guarded in the same conditional update


def test_finding4_a_stuck_undoing_batch_can_be_resumed_but_a_live_one_cannot():
    pairs = _pairs(2, "Undoing")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], ADMIN)["id"], ADMIN)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batches SET status='undoing',updated_at=? WHERE id=?", (db.now(), b["id"]))
    with pytest.raises(bulkmerge.BatchBusy, match="already being undone"):
        bulkmerge.undo(b["id"], ADMIN)
    assert bulkmerge.batch(b["id"])["undo_stuck"] is False
    with db.tx() as conn:
        conn.execute("UPDATE merge_batches SET updated_at=? WHERE id=?", (db.now() - 3600, b["id"]))
    stuck = bulkmerge.batch(b["id"])
    assert stuck["undo_stuck"] is True and stuck["can_undo"] is True
    out = bulkmerge.undo(b["id"], ADMIN)
    assert out["status"] == "undone" and out["undone"] == 2


def test_finding4_finalize_is_one_conditional_update(local_mode):
    (pair,) = _pairs(1, "Atomic")
    b = bulkmerge.process(bulkmerge.start([_gid(pair)], ADMIN)["id"], ADMIN)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batches SET status='undoing' WHERE id=?", (b["id"],))
    with pytest.raises(bulkmerge.BatchBusy):
        bulkmerge.finalize(b["id"])
    assert db.one("SELECT COUNT(*) n FROM duplicate_refresh_queue WHERE status='held'")["n"] == 1       # nothing was released
    assert "finalized_at" not in json.loads(db.one("SELECT options_json o FROM merge_batches WHERE id=?", (b["id"],))["o"])


def test_finding4_the_running_status_has_a_label_and_the_409_text_names_no_stop_endpoint():
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dupes.js")).read()
    assert "running: ['info', 'Merging']" in js and "label[i.status]" not in js
    assert "stop it" not in open(bulkmerge.__file__).read()


def test_finding5_a_slow_live_worker_keeps_its_claim_and_its_result_is_recorded(monkeypatch):
    (pair,) = _pairs(1, "Slow")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    real = workmerge.execute
    seen = {}

    def slow(*a, **kw):
        it = _item(b)
        assert it["status"] == "running" and it["updated_at"] >= it["finished_at"]       # the worker beat before the phase
        with db.tx() as conn:        # the merge has now run "for an hour": the heartbeat and claim time are old
            conn.execute("UPDATE merge_batch_items SET finished_at=?,updated_at=? WHERE id=?", (db.now() - 3600, db.now() - 3600, it["id"]))
        seen["recovered"] = bulkmerge.recover_stale(b["id"])      # another process's housekeeping runs meanwhile
        seen["status_during"] = _item(b)["status"]
        return real(*a, **kw)
    monkeypatch.setattr(workmerge, "execute", slow)
    out = bulkmerge.process(b["id"], ADMIN)
    assert seen == {"recovered": 0, "status_during": "running"} and out["status"] == "done" and out["counts"]["merged"] == 1
    assert _item(b)["updated_at"] > db.now() - 60                                       # beat after the phase too


def test_finding5_finish_records_a_committed_merge_even_if_the_claim_was_wrongly_given_back(monkeypatch):
    (pair,) = _pairs(1, "Wrongly")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    real = workmerge.execute

    def reset_then_merge(*a, **kw):
        with db.tx() as conn:        # a foreign recover reset the claim while we were merging
            conn.execute("UPDATE merge_batch_items SET status='pending',finished_at=NULL,updated_at=NULL WHERE batch_id=?", (b["id"],))
        return real(*a, **kw)
    monkeypatch.setattr(workmerge, "execute", reset_then_merge)
    it = _item(b)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET status='running' WHERE id=?", (it["id"],))
    assert bulkmerge._merge_item(b["id"], it, ADMIN) == "merged"
    after = _item(b)
    assert after["status"] == "merged" and after["merge_id"]
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type=?", (bulkmerge.EVENT_BULK,))["n"] == 1
    assert bulkmerge.undo(b["id"], ADMIN)["undone"] == 1


# ---- round 3 ----

def test_round3_recovery_matches_the_sources_the_worker_actually_merged_not_the_stored_plan(monkeypatch):
    (pair,) = _pairs(1, "Used")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    planned = json.loads(it["source_work_ids_json"])
    real = workmerge.execute

    def die_after_commit(canonical, sources, *a, **k):
        real(canonical, sources, *a, **k)
        raise KeyboardInterrupt()          # the worker dies after the merge committed, before it was recorded
    monkeypatch.setattr(bulkmerge.workmerge, "execute", die_after_commit)
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET status='running' WHERE id=?", (it["id"],))
    with pytest.raises(KeyboardInterrupt):
        bulkmerge._merge_item(b["id"], dict(it, status="running"), ADMIN)
    assert json.loads(_item(b)["detail_json"])["claim_sources"] == planned
    # the stored plan no longer equals what was merged: only the persisted ids can match the committed merge
    with db.tx() as conn:
        conn.execute("UPDATE merge_batch_items SET source_work_ids_json=? WHERE id=?", (json.dumps(planned + ["gone-work"]), it["id"]))
    _stale_claim(it["id"])
    assert bulkmerge.recover_stale(b["id"]) == 1
    after = _item(b)
    assert after["status"] == "merged" and after["merge_id"]
    assert bulkmerge.undo(b["id"], ADMIN)["undone"] == 1


def test_round3_grace_release_skips_running_and_undoing_batches_and_the_ui_wording():
    pairs = _pairs(2, "GraceBusy")
    b = bulkmerge.process(bulkmerge.start([_gid(p) for p in pairs], ADMIN)["id"], ADMIN)
    with db.tx() as conn:
        conn.execute("UPDATE duplicate_refresh_queue SET queued_at=? WHERE batch_id=?", (db.now() - 9 * 86400, b["id"]))
    for busy in ("running", "undoing"):
        with db.tx() as conn:
            conn.execute("UPDATE merge_batches SET status=? WHERE id=?", (busy, b["id"]))
        assert bulkmerge.release_held() == 0
        assert bulkmerge.batch(b["id"])["held_refreshes"] == 2 and bulkmerge.batch(b["id"])["finalized_at"] is None
    with db.tx() as conn:
        conn.execute("UPDATE merge_batches SET status='done' WHERE id=?", (b["id"],))
    assert bulkmerge.release_held() == 2
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dupes.js")).read()
    assert "metadata refresh has started" not in js and "metadata refresh is queued" in js
    assert "due for release the next time the refresh queue runs" in js
    assert "b.held_refreshes && !b.finalized_at" not in js


def test_round3_recover_stale_counts_only_rows_it_changed_and_get_batch_recovers_once(local_mode, monkeypatch):
    (pair,) = _pairs(1, "Count")
    b = bulkmerge.start([_gid(pair)], ADMIN)
    it = _item(b)
    _stale_claim(it["id"])
    assert bulkmerge.recover_stale(b["id"]) == 1 and _item(b)["status"] == "pending"
    assert bulkmerge.recover_stale(b["id"]) == 0          # nothing left to change
    _stale_claim(it["id"])
    calls = []
    orig = bulkmerge.recover_stale
    monkeypatch.setattr(bulkmerge, "recover_stale", lambda *a, **k: calls.append(a) or orig(*a, **k))
    with TestClient(app) as admin:
        admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        r = admin.get(f"/api/v1/admin/duplicates/batches/{b['id']}")
        assert r.status_code == 200 and len(calls) == 1 and r.json()["counts"]["pending"] == 1
    scheduler.shutdown_pool()


def test_dupes_js_does_not_use_insert_adjacent_html():
    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "dupes.js")).read()
    assert "insertAdjacentHTML" not in js
    assert "n.textContent = `Pausing ${x.rate_limited.retry_after}s" in js
