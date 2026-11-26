"""Kids shelf suggestions: scan -> review task -> admin approves -> tag.

The kids tag decides what a child's account can see, so the classifier is never allowed
to write it. These tests pin that: the scan skips books already on the shelf, proposes
instead of tagging, only an admin's approval writes the tag, and anything unexpected
from the LLM fails closed (no proposal) rather than defaulting to "kids".
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import llmclient, migration, tagging

import helpers

app = main_module.app
APP_JS = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()


@pytest.fixture(autouse=True)
def propose_only():
    """These tests pin the propose-then-approve path. Auto-approval (on by default since
    2026-10-11, see test_kids_auto_approve.py) is switched off for them so a confident
    verdict waits in the Inbox as it always did."""
    tagging.set_auto_approve(enabled=False)


def _work(title="Some Book", author="Some Author", *, owned=True, description=None):
    wid = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    if owned:
        helpers.own(wid)
    if description:
        with db.tx() as conn:
            conn.execute("UPDATE works SET description=? WHERE id=?", (description, wid))
    return wid


def _answer(monkeypatch, raw):
    calls = []

    def fake(system, user, **kw):
        calls.append(user)
        return raw
    monkeypatch.setattr(llmclient, "chat_json", fake)
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "primary")
    return calls


def _open_task(work_id):
    return db.one("SELECT * FROM review_tasks WHERE kind='kids_tag' AND entity_id=? AND status='open'", (work_id,))


def _tagged(work_id):
    return any(t["name"] == "kids" for t in tagging.work_tags(work_id))


KIDS = {"verdict": "kids", "confidence": 0.9, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "a picture book about a bear"}


# ---------- the scan ----------
def test_scan_skips_books_already_on_the_kids_shelf_without_asking_the_llm(monkeypatch):
    calls = _answer(monkeypatch, KIDS)
    tagged = _work("Already Tagged")
    tagging.add_tag(tagged, tagging.kids_tag_id())
    assert tagging.classify_kids_for_work(tagged) == {"work_id": tagged, "status": "already_tagged"}
    assert calls == []
    # and the batch never even selects it
    assert tagging.classify_kids_missing(limit=10)["considered"] == 0
    assert calls == []


def test_scan_proposes_and_never_writes_the_tag(monkeypatch):
    calls = _answer(monkeypatch, KIDS)
    wid = _work("Goodnight Moon", "Margaret Wise Brown", description="A bunny says goodnight to everything in the room.")
    result = tagging.classify_kids_missing(limit=10)
    assert result["considered"] == 1 and result["proposed"] == 1 and result["remaining"] == 0
    assert not _tagged(wid)
    assert db.one("SELECT COUNT(*) n FROM work_tags")["n"] == 0, "nothing in work_tags without an approval"
    task = _open_task(wid)
    payload = db.json_load(task["payload_json"], {})
    assert task["title"] == "Goodnight Moon"
    assert payload["confidence"] == 0.9 and payload["reason"] == KIDS["reason"] and payload["signal"] == "llm"
    assert payload["source"]["authors"] == ["Margaret Wise Brown"]
    # Only text metadata already on record goes to the model.
    assert len(calls) == 1 and calls[0].startswith("Title: Goodnight Moon\nAuthor(s): Margaret Wise Brown\n")
    assert calls[0].endswith("Description: A bunny says goodnight to everything in the room."), "title, author and description are still sent"
    # A kids account still cannot see it: the proposal is not the tag.
    assert not tagging.is_visible_to(wid, {"access_scope": "kids"})


def test_scan_covers_owned_books_only_and_does_not_re_propose(monkeypatch):
    calls = _answer(monkeypatch, KIDS)
    owned, wanted = _work("Owned Kids Book"), _work("Wanted Kids Book", owned=False)
    helpers.want(wanted)
    assert tagging.classify_kids_missing(limit=10)["considered"] == 1
    assert _open_task(owned) and not _open_task(wanted)
    # second pass: the open proposal and the remembered verdict both keep it out
    assert tagging.classify_kids_missing(limit=10)["considered"] == 0
    assert tagging.classify_kids_for_work(owned)["status"] == "already_proposed"
    assert len(calls) == 1
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag'")["n"] == 1


def test_force_refreshes_an_open_proposal_instead_of_duplicating_it(monkeypatch):
    _answer(monkeypatch, KIDS)
    wid = _work()
    tagging.classify_kids_for_work(wid)
    _answer(monkeypatch, {"verdict": "kids", "confidence": 0.81, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "second look"})
    assert tagging.classify_kids_for_work(wid, force=True)["proposed"] is True
    tasks = db.all("SELECT payload_json FROM review_tasks WHERE kind='kids_tag' AND entity_id=?", (wid,))
    assert len(tasks) == 1 and db.json_load(tasks[0]["payload_json"], {})["confidence"] == 0.81


@pytest.mark.parametrize("raw", [
    None,                                                     # unreachable / unparsable (llmclient returns None)
    "kids",                                                   # not an object
    {"verdict": "yes", "confidence": 0.99},                   # unknown verdict
    {"verdict": "kids"},                                      # kids with no confidence
    {"verdict": "kids", "confidence": "high"},                # confidence not a number
    {"verdict": "kids", "confidence": True},                  # bool is not a confidence
    {"verdict": "kids", "confidence": 1.7, "audience_evidence": ["categories: Juvenile Fiction"]},                   # out of range
    {"verdict": "kids", "confidence": -0.2},
    {"confidence": 0.99, "reason": "looks fine"},             # no verdict at all
    {"verdict": ["kids"], "confidence": 0.99},
])
def test_a_malformed_llm_answer_fails_closed(monkeypatch, raw):
    _answer(monkeypatch, raw)
    wid = _work()
    assert tagging.classify_kids_for_work(wid) == {"work_id": wid, "status": "abstained"}
    assert _open_task(wid) is None and not _tagged(wid)
    # Not remembered as checked either, so it is retried once the model behaves.
    assert db.one("SELECT 1 FROM kids_classification WHERE work_id=?", (wid,)) is None
    assert tagging.classify_kids_missing(limit=10)["considered"] == 1


@pytest.mark.parametrize("raw", [
    {"verdict": "not_kids", "confidence": 0.95, "reason": "graphic violence"},
    {"verdict": "uncertain", "confidence": 0.5, "reason": "no description"},
    {"verdict": "uncertain"},  # confidence optional when not proposing anything
    {"verdict": "kids", "confidence": 0.74, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "just under the bar"},
])
def test_non_confident_verdicts_are_remembered_but_not_proposed(monkeypatch, raw):
    _answer(monkeypatch, raw)
    wid = _work()
    result = tagging.classify_kids_for_work(wid)
    assert result["status"] == "classified" and result["proposed"] is False
    assert _open_task(wid) is None and not _tagged(wid)
    assert db.one("SELECT verdict FROM kids_classification WHERE work_id=?", (wid,))["verdict"] == raw["verdict"]
    assert tagging.classify_kids_missing(limit=10)["considered"] == 0


def test_reason_is_truncated_display_text_not_trusted_structure(monkeypatch):
    _answer(monkeypatch, {"verdict": "kids", "confidence": 0.9, "audience_evidence": ["categories: Juvenile Fiction"], "reason": {"nested": "object"}})
    wid = _work()
    assert tagging.classify_kids_for_work(wid)["proposed"] is True
    assert db.json_load(_open_task(wid)["payload_json"], {})["reason"] is None


# ---------- approve / reject ----------
def test_approving_is_the_only_path_to_the_tag(monkeypatch):
    _answer(monkeypatch, KIDS)
    wid = _work("Frog and Toad")
    tagging.classify_kids_for_work(wid)
    task = _open_task(wid)
    out = tagging.approve_kids_proposal(task["id"], decided_by="ada")
    assert out == {"ok": True, "work_id": wid, "tagged": True, "auto": False}
    assert _tagged(wid) and tagging.is_visible_to(wid, {"access_scope": "kids"})
    row = db.one("SELECT source,confidence FROM work_tags WHERE work_id=?", (wid,))
    assert row["source"] == "llm" and row["confidence"] == 0.9
    done = db.one("SELECT status,resolution_json FROM review_tasks WHERE id=?", (task["id"],))
    assert done["status"] == "resolved" and db.json_load(done["resolution_json"], {}) == {"action": "approved", "by": "ada", "confidence": 0.9, "model": "primary"}
    assert db.one("SELECT event_type FROM history WHERE work_id=?", (wid,))["event_type"] == "kids.approved"
    with pytest.raises(ValueError):
        tagging.approve_kids_proposal(task["id"], decided_by="ada")  # already decided


def test_rejecting_changes_nothing_about_the_book_and_is_not_asked_again(monkeypatch):
    calls = _answer(monkeypatch, KIDS)
    wid = _work()
    tagging.classify_kids_for_work(wid)
    task = _open_task(wid)
    assert tagging.reject_kids_proposal(task["id"], decided_by="ada") == {"ok": True, "work_id": wid, "tagged": False}
    assert not _tagged(wid)
    done = db.one("SELECT status,resolution_json FROM review_tasks WHERE id=?", (task["id"],))
    assert done["status"] == "ignored" and db.json_load(done["resolution_json"], {})["action"] == "rejected"
    assert tagging.classify_kids_missing(limit=10)["considered"] == 0 and len(calls) == 1


def test_approve_refuses_a_task_of_another_kind():
    wid, t = _work(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES ('m1','metadata_match','work',?,?,'{}','open',50,?)", (wid, "x", t))
    with pytest.raises(ValueError):
        tagging.approve_kids_proposal("m1", decided_by="ada")
    assert not _tagged(wid)


# ---------- HTTP: who may decide, and what the Inbox sees ----------
@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    return replaced


@pytest.fixture
def household(local_mode, monkeypatch):
    """Admin, a full-access non-admin and a kids account, plus one open suggestion."""
    _answer(monkeypatch, KIDS)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        admin_csrf = r.json()["csrf"]
        for username, scope in (("pat", "full"), ("junior", "kids")):
            r = admin.post("/api/v1/users", json={"username": username, "password": f"a-very-long-{username}-password", "access_scope": scope}, headers={"X-CSRF-Token": admin_csrf})
            assert r.status_code == 200, r.text
        wid = _work("Suggested Book")
        tagging.classify_kids_for_work(wid)
        sessions = {"admin": (admin, admin_csrf)}
        for username in ("pat", "junior"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": username, "password": f"a-very-long-{username}-password"})
            sessions[username] = (c, r.json()["csrf"])
        yield {"sessions": sessions, "work": wid, "task": _open_task(wid)["id"]}


def test_only_an_admin_can_approve_or_reject(household):
    tid, wid = household["task"], household["work"]
    for who in ("pat", "junior"):
        c, csrf = household["sessions"][who]
        for action in ("approve", "reject"):
            r = c.post(f"/api/v1/review/{tid}/kids-tag/{action}", headers={"X-CSRF-Token": csrf})
            assert r.status_code == 403, (who, action, r.text)
    assert not _tagged(wid) and _open_task(wid)
    admin, csrf = household["sessions"]["admin"]
    assert admin.post(f"/api/v1/review/{tid}/kids-tag/approve").status_code == 403, "CSRF token required on a cookie session"
    r = admin.post(f"/api/v1/review/{tid}/kids-tag/approve", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["tagged"] is True
    assert _tagged(wid)
    assert admin.post(f"/api/v1/review/{tid}/kids-tag/reject", headers={"X-CSRF-Token": csrf}).status_code == 404


def test_kid_can_see_the_book_only_after_approval(household):
    wid, tid = household["work"], household["task"]
    kid, _ = household["sessions"]["junior"]
    assert kid.get(f"/api/v1/library/{wid}").status_code == 404
    admin, csrf = household["sessions"]["admin"]
    admin.post(f"/api/v1/review/{tid}/kids-tag/approve", headers={"X-CSRF-Token": csrf})
    assert kid.get(f"/api/v1/library/{wid}").status_code == 200


def test_inbox_lists_suggestions_separately_from_metadata_matches(household):
    admin, _ = household["sessions"]["admin"]
    wid, t = household["work"], db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES ('m1','metadata_match','work',?,?,'{}','open',50,?)", (wid, "Unmatched", t))
    kids = admin.get("/api/v1/review", params={"kind": "kids_tag"}).json()
    assert kids["total"] == 1 and kids["items"][0]["id"] == household["task"]
    assert kids["items"][0]["payload"]["reason"] == KIDS["reason"]
    meta = admin.get("/api/v1/review", params={"kind": "metadata_match"}).json()
    assert [i["id"] for i in meta["items"]] == ["m1"]
    assert admin.get("/api/v1/review").json()["total"] == 2
    assert admin.get("/api/v1/review", params={"kind": "bogus"}).json()["total"] == 0  # any kind may be asked for; unknown ones are simply empty


def test_rejecting_over_http_records_who(household):
    admin, csrf = household["sessions"]["admin"]
    r = admin.post(f"/api/v1/review/{household['task']}/kids-tag/reject", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["tagged"] is False
    assert db.json_load(db.one("SELECT resolution_json FROM review_tasks WHERE id=?", (household["task"],))["resolution_json"], {}) == {"action": "rejected", "by": "ada"}
    assert not _tagged(household["work"])


# ---------- the scan route ----------
def test_scan_route_runs_in_the_background_and_only_proposes(monkeypatch):
    calls = _answer(monkeypatch, KIDS)
    ids = [_work(f"Book {i}") for i in range(3)]
    with TestClient(app) as c:  # background tasks run before TestClient returns
        r = c.post("/api/v1/tags/classify-library", json={"limit": 2})
        assert r.status_code == 200, r.text
        assert r.json() == {"started": True, "limit": 2, "pending": 3}
    assert len(calls) == 2
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag' AND status='open'")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM work_tags")["n"] == 0
    assert tagging.kids_scan_pending() == 1


def test_scan_route_refuses_when_the_llm_is_off(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: False)
    _work()
    with TestClient(app) as c:
        r = c.post("/api/v1/tags/classify-library", json={})
        assert r.status_code == 409
        assert c.post("/api/v1/tags/classify-library", json={"limit": 0}).status_code == 422


# ---------- the UI ----------
def test_inbox_lists_kids_suggestions_whose_approve_confirms_and_posts_the_approve_route():
    """Kids suggestions live in the unified review section (filter kind=kids_tag); the
    per-card actions still go through the kids-tag approve/reject routes with a confirm."""
    assert "review: { model: reviewModel, actions: reviewActions, url: '/api/v1/review?'" in APP_JS
    assert "function reviewModel(t) { return t.kind === 'kids_tag' ? kidsSuggestionModel(t) : t.kind === 'kids_recheck' ? kidsRecheckModel(t) : reviewTaskModel(t); }" in APP_JS
    fn = APP_JS[APP_JS.index("function kidsSuggestionActions("):APP_JS.index("async function runKidsScan(")]
    assert "confirmPost(" in fn and "/kids-tag/approve`" in fn and "/kids-tag/reject`" in fn
    assert "'/api/v1/tags/classify-library'" in APP_JS
    assert "id=\"kids-scan\"" in APP_JS and "runKidsScan(20)" in APP_JS