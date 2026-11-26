"""A human's kids-shelf decision beats the classifier, permanently, on every path.

`kids_decisions` records who put a work on or off the shelf and how. The classifier
(single work, forced, batch scan, the scheduled job) never asks, writes or proposes
about a decided work; a manual untag closes any open proposal; approving a stale
proposal against a manual "off" is refused. Repeated classifier runs after a manual
tag or untag leave the manual state unchanged - no loop.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import llmclient, migration, scheduler, tagging

import helpers

app = main_module.app
KIDS = {"verdict": "kids", "confidence": 0.95, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "a picture book"}


@pytest.fixture(autouse=True)
def _propose_only():
    """Pins the propose-then-approve path; auto-approval (default on) is tested in
    test_kids_auto_approve.py."""
    tagging.set_auto_approve(enabled=False)


def _work(title):
    wid = migration._find_or_create_stub({"title": title, "author": "A", "identifiers": {}})
    helpers.own(wid)
    return wid


@pytest.fixture
def llm(monkeypatch):
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        return dict(KIDS)
    monkeypatch.setattr(llmclient, "chat_json", fake)
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "primary")
    return calls


@pytest.fixture
def admin(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        yield c, {"X-CSRF-Token": r.json()["csrf"]}


def _on_shelf(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def _open_proposals(wid):
    return db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag' AND entity_id=? AND status='open'", (wid,))["n"]


def _run_classifier_everywhere(wid):
    """Every code path the classifier has."""
    out = [tagging.classify_kids_for_work(wid), tagging.classify_kids_for_work(wid, force=True), tagging.classify_kids_missing(limit=50)]
    job = db.one("SELECT id FROM jobs WHERE kind='tag-classify'")
    out.append(scheduler.run_job(job["id"]))
    return out


def test_migration_20_backfills_existing_kids_tags_as_decisions():
    wid = _work("Already tagged")
    tagging.add_tag(wid, tagging.kids_tag_id(), source="llm", confidence=0.9)
    with db.tx() as conn:
        conn.execute("DROP TABLE kids_decisions")
        conn.execute("DELETE FROM migration_steps WHERE version=20")
    from app import migrations
    results = migrations.run_pending_migrations()
    assert [r["version"] for r in results] == [20]
    d = tagging.kids_decision(wid)
    assert d["state"] == "on" and d["via"] == "backfill"
    assert tagging.classify_kids_for_work(wid)["status"] == "manually_decided"


def test_manual_untag_survives_repeated_classifier_runs(admin, llm):
    c, h = admin
    wid = _work("Picture Book")
    # The classifier would say kids with high confidence; an admin takes it off by hand.
    assert c.post(f"/api/v1/library/{wid}/tags", json={"tag_id": tagging.kids_tag_id()}, headers=h).status_code == 200
    assert _on_shelf(wid)
    assert c.delete(f"/api/v1/library/{wid}/tags/{tagging.kids_tag_id()}", headers=h).status_code == 200
    assert not _on_shelf(wid)
    d = c.get(f"/api/v1/library/{wid}").json()["kids_decision"]
    assert d["state"] == "off" and d["via"] == "manual_untag" and d["by"] == "ada"
    for _ in range(3):
        results = _run_classifier_everywhere(wid)
        assert results[0]["status"] == "manually_decided" and results[1]["status"] == "manually_decided"
        assert results[2]["considered"] == 0
        assert not _on_shelf(wid), "classifier re-added a manually removed book"
        assert _open_proposals(wid) == 0, "classifier re-proposed a manually removed book"
    assert llm == [], "the LLM was never even consulted about a decided work"
    assert tagging.kids_decision(wid)["state"] == "off"


def test_manual_tag_survives_repeated_classifier_runs(admin, llm):
    c, h = admin
    wid = _work("Hand Picked")
    c.post(f"/api/v1/library/{wid}/tags", json={"tag_id": tagging.kids_tag_id()}, headers=h)
    d = c.get(f"/api/v1/library/{wid}").json()["kids_decision"]
    assert d["state"] == "on" and d["via"] == "manual_tag"
    for _ in range(3):
        _run_classifier_everywhere(wid)
        assert _on_shelf(wid) and _open_proposals(wid) == 0
    assert db.one("SELECT source FROM work_tags WHERE work_id=?", (wid,))["source"] == "manual"
    assert llm == []


def test_manual_untag_closes_an_open_proposal_and_approval_is_refused(admin, llm):
    c, h = admin
    wid = _work("Proposed Book")
    assert tagging.classify_kids_for_work(wid)["proposed"] is True
    assert _open_proposals(wid) == 1
    task = db.one("SELECT id FROM review_tasks WHERE entity_id=? AND status='open'", (wid,))
    # Admin decides by hand (off) while the proposal sits in the Inbox.
    c.delete(f"/api/v1/library/{wid}/tags/{tagging.kids_tag_id()}", headers=h)
    assert _open_proposals(wid) == 0, "the stale proposal is closed"
    r = c.post(f"/api/v1/review/{task['id']}/kids-tag/approve", headers=h)
    assert r.status_code in (404, 409) and not _on_shelf(wid)
    # Force a fresh classification: still nothing.
    assert tagging.classify_kids_for_work(wid, force=True)["status"] == "manually_decided"
    assert _open_proposals(wid) == 0


def test_approved_and_rejected_proposals_become_decisions(admin, llm):
    c, h = admin
    a, b = _work("Approve Me"), _work("Reject Me")
    tagging.classify_kids_for_work(a); tagging.classify_kids_for_work(b)
    ta = db.one("SELECT id FROM review_tasks WHERE entity_id=?", (a,))["id"]
    tb = db.one("SELECT id FROM review_tasks WHERE entity_id=?", (b,))["id"]
    assert c.post(f"/api/v1/review/{ta}/kids-tag/approve", headers=h).status_code == 200
    assert c.post(f"/api/v1/review/{tb}/kids-tag/reject", headers=h).status_code == 200
    assert tagging.kids_decision(a)["via"] == "proposal_approved" and tagging.kids_decision(a)["decided_by_name"] == "ada"
    assert tagging.kids_decision(b)["via"] == "proposal_rejected"
    before = len(llm)
    for wid in (a, b):
        for _ in range(2):
            _run_classifier_everywhere(wid)
    assert _on_shelf(a) and not _on_shelf(b) and len(llm) == before
    detail = c.get(f"/api/v1/library/{a}").json()
    assert detail["kids_decision"]["via"] == "proposal_approved"
    assert [t["source"] for t in detail["tags"] if t["name"] == "kids"] == ["llm"], "provenance of the tag itself is kept"
    # A later manual change wins again and is recorded as such.
    c.delete(f"/api/v1/library/{a}/tags/{tagging.kids_tag_id()}", headers=h)
    assert tagging.kids_decision(a)["via"] == "manual_untag" and not _on_shelf(a)


def test_undecided_works_are_still_classified(admin, llm):
    wid = _work("Fresh Book")
    assert tagging.classify_kids_for_work(wid)["status"] == "classified"
    assert len(llm) == 1 and _open_proposals(wid) == 1


def test_management_shows_the_decision_source():
    js = (__import__("pathlib").Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()
    assert "kidsDecisionTitle(w)" in js and "classifier, approved" in js and "Kids shelf: excluded by hand" in js
