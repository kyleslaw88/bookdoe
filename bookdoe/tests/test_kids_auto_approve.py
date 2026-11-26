"""Kids auto-approval (the owner, 2026-10-11): a confident classifier proposal is applied at
once, through the same approval code a person's click uses, recorded as
via='auto_approved' with model and confidence. Default ON; threshold never below the
proposal threshold. Manual decisions always win; a human untag of an auto-approved
book locks it off; repeated runs change nothing; the kill switch stops new approvals
and leaves tags alone; the sweep approves only OPEN proposals at or above the
threshold; revert has two documented modes; kids see auto-tagged works at once.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app import migrations
from app.config import settings as base_settings
from app.services import llmclient, migration, scheduler, tagging

import helpers

app = main_module.app


def _work(title):
    """An owned book with what the evidence gate needs to say yes to a confident verdict: a real
    description and a children's publisher (the gate itself is pinned in test_kids_gate.py)."""
    # One author per book: authors share a prior (kidsgate), and these tests are about the approval mechanics.
    wid = migration._find_or_create_stub({"title": title, "author": f"Author of {title}", "identifiers": {}})
    helpers.own(wid)
    t = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE works SET description=? WHERE id=?", ("A gentle picture book about a bear who learns to share his honey with the whole forest.", wid))
        conn.execute("INSERT INTO editions(id,work_id,publisher,created_at,updated_at) VALUES (?,?,?,?,?)", (db.uid(), wid, "Scholastic", t, t))
    return wid


@pytest.fixture
def llm(monkeypatch):
    """The model's answer is per title: `verdicts[title] = (verdict, confidence)`."""
    verdicts = {}
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        title = user.split("\n", 1)[0].removeprefix("Title: ")
        v, c = verdicts.get(title, ("kids", 0.95))
        return {"verdict": v, "confidence": c, "reason": "test", "audience_evidence": ["publisher: Scholastic"] if v == "kids" else []}
    monkeypatch.setattr(llmclient, "chat_json", fake)
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "qwen-test")
    verdicts["_calls"] = calls
    return verdicts


@pytest.fixture
def household(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        admin.post("/api/v1/users", json={"username": "owen", "password": "a-very-long-owen-password", "account_type": "kid"}, headers=ah)
        kid = TestClient(app)
        kid.post("/api/v1/auth/login", json={"username": "owen", "password": "a-very-long-owen-password"})
        yield admin, ah, kid


def _tagged(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def _open(wid):
    return db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag' AND entity_id=? AND status='open'", (wid,))["n"]


def _job():
    return scheduler.run_job(db.one("SELECT id FROM jobs WHERE kind='tag-classify'")["id"])


# ---------- defaults and migration ----------
def test_default_on_with_the_proposal_threshold_and_floor():
    assert tagging.auto_approve_enabled() is True
    assert tagging.auto_threshold() == tagging.AUTO_THRESHOLD_DEFAULT == 0.95  # the owner's stopgap, now the default
    assert tagging.require_strong_evidence() is True
    with pytest.raises(ValueError):
        tagging.set_auto_approve(threshold=0.5)
    assert tagging.set_auto_approve(threshold=0.9)["kids_auto_threshold"] == 0.9
    db.set_setting(tagging.AUTO_THRESHOLD_KEY, 0.1)  # a bad stored value is floored on read
    assert tagging.auto_threshold() == tagging.KIDS_PROPOSE_THRESHOLD


def test_migration_22_widens_the_check_and_keeps_rows():
    wid = _work("Kept")
    tagging.set_kids_decision(wid, "on", via="manual_tag", by=None)
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=22")
        conn.execute("DROP TABLE kids_decisions")
        conn.execute("CREATE TABLE kids_decisions (work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE, state TEXT NOT NULL CHECK(state IN ('on','off')), "
                     "via TEXT NOT NULL CHECK(via IN ('manual_tag','manual_untag','proposal_approved','proposal_rejected','backfill')), decided_by TEXT, decided_at REAL NOT NULL, note TEXT)")
        conn.execute("INSERT INTO kids_decisions(work_id,state,via,decided_at) VALUES (?,'on','manual_tag',1)", (wid,))
    assert [r["version"] for r in migrations.run_pending_migrations()] == [22]
    assert tagging.kids_decision(wid)["via"] == "manual_tag"
    tagging.set_kids_decision(wid, "on", via="auto_approved", by=None)  # the widened CHECK accepts it
    assert migrations.run_pending_migrations() == []


# ---------- the auto path ----------
def test_confident_verdict_is_tagged_at_once_with_audit(household, llm):
    admin, ah, kid = household
    wid = _work("Sunny Picture Book")
    r = tagging.classify_kids_for_work(wid)
    assert r["proposed"] is True and r["auto_approved"] is True
    assert _tagged(wid) and _open(wid) == 0, "the proposal was created and resolved in the same pass"
    d = tagging.kids_decision(wid)
    assert d["via"] == "auto_approved" and d["decided_by"] is None
    detail = admin.get(f"/api/v1/library/{wid}").json()["kids_decision"]
    assert detail["via"] == "auto_approved" and detail["confidence"] == 0.95 and detail["model"] == "qwen-test"
    assert [t["source"] for t in admin.get(f"/api/v1/library/{wid}").json()["tags"] if t["name"] == "kids"] == ["llm"]
    h = db.one("SELECT message FROM history WHERE event_type='kids.auto_approved' AND work_id=?", (wid,))
    assert h and "0.95" in h["message"] and "qwen-test" in h["message"]
    task = db.one("SELECT status,resolution_json FROM review_tasks WHERE entity_id=?", (wid,))
    assert task["status"] == "resolved" and '"auto_approved"' in task["resolution_json"]


def test_threshold_boundary_and_below_threshold_still_goes_to_the_inbox(household, llm):
    tagging.set_auto_approve(threshold=0.9)
    llm["Exactly At"] = ("kids", 0.9)
    llm["Just Below"] = ("kids", 0.89)
    llm["Proposed Only"] = ("kids", 0.8)     # above the proposal threshold, below auto
    llm["Not Confident"] = ("kids", 0.5)     # below the proposal threshold: nothing
    llm["Grown Up"] = ("not_kids", 0.99)
    ids = {t: _work(t) for t in ("Exactly At", "Just Below", "Proposed Only", "Not Confident", "Grown Up")}
    out = tagging.classify_kids_missing(limit=50)
    assert out == {**out, "auto_approved": 1, "proposed": 3, "swept_pending": 0}
    assert _tagged(ids["Exactly At"]) and _open(ids["Exactly At"]) == 0
    for t in ("Just Below", "Proposed Only"):
        assert not _tagged(ids[t]) and _open(ids[t]) == 1, t
    assert not _tagged(ids["Not Confident"]) and _open(ids["Not Confident"]) == 0
    assert not _tagged(ids["Grown Up"])


def test_kid_sees_an_auto_tagged_work_and_not_an_untagged_one(household, llm):
    admin, ah, kid = household
    llm["Teen Thriller"] = ("not_kids", 0.9)
    a, b = _work("Gentle Story"), _work("Teen Thriller")
    assert kid.get(f"/api/v1/library/{a}").status_code == 404
    _job()
    assert kid.get(f"/api/v1/library/{a}").status_code == 200
    assert kid.get(f"/api/v1/library/{b}").status_code == 404
    assert [w["title"] for w in kid.get("/api/v1/library").json()["items"]] == ["Gentle Story"]
    assert kid.get(f"/api/v1/library/{b}/cover").status_code == 404


# ---------- manual decisions always win ----------
def test_manual_off_is_never_overridden_by_auto(household, llm):
    admin, ah, _ = household
    wid = _work("Looks Kiddy")
    admin.post(f"/api/v1/library/{wid}/tags", json={"tag_id": tagging.kids_tag_id()}, headers=ah)
    admin.delete(f"/api/v1/library/{wid}/tags/{tagging.kids_tag_id()}", headers=ah)
    for _ in range(3):
        assert tagging.classify_kids_for_work(wid, force=True)["status"] == "manually_decided"
        _job()
        assert not _tagged(wid) and _open(wid) == 0
    assert llm["_calls"] == []
    assert tagging.kids_decision(wid)["via"] == "manual_untag"


def test_rejected_proposal_stays_rejected_and_pending_off_is_skipped_by_the_sweep(household, llm):
    admin, ah, _ = household
    tagging.set_auto_approve(enabled=False)
    wid = _work("Rejected One")
    tagging.classify_kids_for_work(wid)
    task = db.one("SELECT id FROM review_tasks WHERE entity_id=?", (wid,))["id"]
    admin.post(f"/api/v1/review/{task}/kids-tag/reject", headers=ah)
    # A second pending work carries a manual 'off' but still has an open task (edge: decision written directly).
    other = _work("Pending But Off")
    tagging.classify_kids_for_work(other)
    with db.tx() as conn:
        conn.execute("INSERT INTO kids_decisions(work_id,state,via,decided_at) VALUES (?,'off','manual_untag',1)", (other,))
    tagging.set_auto_approve(enabled=True)
    sweep = tagging.auto_approve_pending()
    assert sweep["approved"] == 0 and sweep["skipped"] == 1
    assert not _tagged(wid) and not _tagged(other)
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (task,))["status"] == "ignored"


def test_human_untag_of_an_auto_approved_book_locks_it_off(household, llm):
    admin, ah, kid = household
    wid = _work("Was Auto")
    tagging.classify_kids_for_work(wid)
    assert _tagged(wid) and kid.get(f"/api/v1/library/{wid}").status_code == 200
    admin.delete(f"/api/v1/library/{wid}/tags/{tagging.kids_tag_id()}", headers=ah)
    assert kid.get(f"/api/v1/library/{wid}").status_code == 404
    for _ in range(3):
        tagging.classify_kids_for_work(wid, force=True); tagging.auto_approve_pending(); _job()
    assert not _tagged(wid) and tagging.kids_decision(wid)["via"] == "manual_untag"


def test_repeated_runs_are_a_fixed_point(household, llm):
    ids = [_work(f"Book {i}") for i in range(4)]
    first = tagging.classify_kids_missing(limit=50)
    assert first["auto_approved"] == 4 and len(llm["_calls"]) == 4
    for _ in range(3):
        again = tagging.classify_kids_missing(limit=50)
        assert again["considered"] == 0 and again["auto_approved"] == 0 and again["swept_pending"] == 0
        _job()
    assert len(llm["_calls"]) == 4 and all(_tagged(w) for w in ids)
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='kids.auto_approved'")["n"] == 4


# ---------- the sweep and the kill switch ----------
def test_sweep_approves_pending_proposals_on_enable_and_on_the_job(household, llm):
    admin, ah, _ = household
    tagging.set_auto_approve(enabled=False)
    llm["Low"] = ("kids", 0.8)
    hi, lo = _work("High"), _work("Low")
    tagging.classify_kids_missing(limit=50)
    assert _open(hi) == 1 and _open(lo) == 1 and not _tagged(hi), "off: proposals wait in the Inbox"
    # Turning it on from Management sweeps what is waiting (threshold 0.9 > Low's 0.8).
    r = admin.put("/api/v1/settings/kids", json={"kids_auto_approve": True, "kids_auto_threshold": 0.9}, headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["sweep"]["approved"] == 1 and r.json()["kids_auto_approve"] is True
    assert _tagged(hi) and _open(hi) == 0 and not _tagged(lo) and _open(lo) == 1
    assert tagging.kids_decision(hi)["via"] == "auto_approved"
    # Lowering the threshold to the floor sweeps the rest on the next job run.
    admin.put("/api/v1/settings/kids", json={"kids_auto_threshold": 0.75}, headers=ah)
    assert _tagged(lo)
    assert _job()["status"] == "success"
    # Idempotent: another sweep and job change nothing.
    assert tagging.auto_approve_pending()["approved"] == 0
    assert admin.get("/api/v1/settings/kids").json()["auto_approved_count"] == 2


def test_kill_switch_stops_new_approvals_and_keeps_existing_tags(household, llm):
    admin, ah, _ = household
    a = _work("Before Off")
    tagging.classify_kids_for_work(a)
    assert _tagged(a)
    r = admin.put("/api/v1/settings/kids", json={"kids_auto_approve": False}, headers=ah)
    assert r.json()["kids_auto_approve"] is False and r.json()["sweep"] is None
    b = _work("After Off")
    out = tagging.classify_kids_for_work(b)
    assert out["proposed"] is True and out["auto_approved"] is False and _open(b) == 1 and not _tagged(b)
    assert tagging.auto_approve_pending() == {"enabled": False, "approved": 0, "skipped": 0}
    _job()
    assert _tagged(a) and not _tagged(b)
    assert db.one("SELECT 1 FROM history WHERE event_type='settings.kids'")


def test_settings_require_admin_and_validate(household, llm):
    admin, ah, kid = household
    assert kid.get("/api/v1/settings/kids").status_code == 403
    assert admin.put("/api/v1/settings/kids", json={"kids_auto_threshold": 0.5}, headers=ah).status_code == 400
    assert admin.put("/api/v1/settings/kids", json={"bogus": 1}, headers=ah).status_code == 422


# ---------- revert ----------
def test_revert_lock_off_and_reevaluate(household, llm):
    admin, ah, kid = household
    manual = _work("Manual Pick")
    admin.post(f"/api/v1/library/{manual}/tags", json={"tag_id": tagging.kids_tag_id()}, headers=ah)
    a, b = _work("Auto A"), _work("Auto B")
    tagging.classify_kids_missing(limit=50)
    listing = admin.get("/api/v1/tags/kids-auto").json()
    assert {x["title"] for x in listing["items"]} == {"Auto A", "Auto B"} and listing["items"][0]["model"] == "qwen-test"
    # lock_off: tag gone, manual off recorded, classifier never returns.
    r = admin.post("/api/v1/tags/kids-auto/revert", json={"work_ids": [a], "mode": "lock_off"}, headers=ah)
    assert r.json()["reverted"] == 1
    assert not _tagged(a) and tagging.kids_decision(a)["via"] == "manual_untag"
    assert tagging.classify_kids_for_work(a, force=True)["status"] == "manually_decided"
    assert kid.get(f"/api/v1/library/{a}").status_code == 404
    # reevaluate: tag, decision and verdict gone; the next scan asks again and auto-approves again.
    calls_before = len(llm["_calls"])
    r = admin.post("/api/v1/tags/kids-auto/revert", json={"mode": "reevaluate"}, headers=ah)   # all remaining auto-approved = Auto B
    assert r.json()["work_ids"] == [b] and not _tagged(b) and tagging.kids_decision(b) is None
    assert _tagged(manual), "a manual decision is never reverted by the bulk action"
    tagging.classify_kids_missing(limit=50)
    assert len(llm["_calls"]) == calls_before + 1 and _tagged(b)
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='kids.auto_reverted'")["n"] == 2
    assert kid.post("/api/v1/tags/kids-auto/revert", json={"mode": "lock_off"}).status_code == 403
