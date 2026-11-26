"""Tags, and the safety-relevant kids classifier.

The kids tag is not a taste label - it is what a restricted account's entire library
view is built on (see test_kids_access_control.py for the access-control side). These
tests pin the classifier's actual safety rules: it never writes the tag itself (a
confident verdict becomes a review task an admin approves - see
test_kids_review_queue.py), only a high-confidence verdict is even proposed, and a
disabled/unreachable LLM must abstain rather than silently deciding either way.
"""
import pytest
from app import db
from app.services import migration, tagging, llmclient

import helpers


def _work(title="Some Book", author="Some Author"):
    return migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})


def test_kids_tag_is_seeded_as_a_system_tag():
    tags = {t["name"]: t for t in tagging.list_tags()}
    assert "kids" in tags
    assert tags["kids"]["kind"] == "system"


def test_system_tag_cannot_be_deleted():
    import pytest
    kid_id = tagging.kids_tag_id()
    with pytest.raises(ValueError, match="system tag"):
        tagging.delete_tag(kid_id)


def test_manual_tag_lifecycle():
    tid = tagging.create_tag("cozy mystery", color="#abcdef")
    work_id = _work()
    tagging.add_tag(work_id, tid)
    assert [t["name"] for t in tagging.work_tags(work_id)] == ["cozy mystery"]
    tagging.remove_tag(work_id, tid)
    assert tagging.work_tags(work_id) == []
    tagging.delete_tag(tid)
    assert tid not in {t["id"] for t in tagging.list_tags()}


def test_creating_the_same_tag_name_twice_reuses_the_row():
    a = tagging.create_tag("Adventure")
    b = tagging.create_tag("adventure")  # case-insensitive
    assert a == b


def test_kids_visibility_is_permissive_for_full_access_and_binds_the_kid_id():
    assert tagging.kids_visibility(None) == ("1=1", ())
    assert tagging.kids_visibility({"access_scope": "full"}) == ("1=1", ())
    clause, params = tagging.kids_visibility({"id": "k'; DROP TABLE works; --", "access_scope": "kids"})
    assert params == ("k'; DROP TABLE works; --",) * 2 and "DROP" not in clause, "the viewer id is bound, never spliced"
    assert clause.count("?") == len(params)


def test_is_visible_to_gates_on_the_kids_tag():
    work_id = _work()
    kids_user = {"id": "u1", "access_scope": "kids"}
    assert tagging.is_visible_to(work_id, kids_user) is False
    tagging.add_tag(work_id, tagging.kids_tag_id())
    assert tagging.is_visible_to(work_id, kids_user) is True
    assert tagging.is_visible_to(work_id, {"access_scope": "full"}) is True
    assert tagging.is_visible_to(work_id, None) is True


def test_classification_abstains_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: False)
    work_id = _work()
    result = tagging.classify_kids_for_work(work_id)
    assert result["status"] == "abstained"
    assert db.one("SELECT * FROM kids_classification WHERE work_id=?", (work_id,)) is None
    assert tagging.work_tags(work_id) == []


@pytest.fixture(autouse=True)
def _propose_only():
    """Pins the propose-then-approve path; auto-approval (default on) is tested in
    test_kids_auto_approve.py."""
    tagging.set_auto_approve(enabled=False)


def test_a_confident_kids_verdict_proposes_but_does_not_tag(monkeypatch):
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.92, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "picture book"})
    work_id = _work("Goodnight Moon")
    result = tagging.classify_kids_for_work(work_id)
    assert {k: result[k] for k in ("work_id", "status", "verdict", "confidence", "proposed", "auto_approved")} == {
        "work_id": work_id, "status": "classified", "verdict": "kids", "confidence": 0.92, "proposed": True, "auto_approved": False}
    assert tagging.work_tags(work_id) == [], "the classifier never writes the tag; an admin approves the suggestion"
    task = db.one("SELECT * FROM review_tasks WHERE kind='kids_tag' AND entity_id=?", (work_id,))
    assert task["status"] == "open" and task["title"] == "Goodnight Moon"
    assert db.json_load(task["payload_json"], {})["confidence"] == 0.92


def test_a_low_confidence_kids_verdict_is_not_even_proposed(monkeypatch):
    """The tag is an access gate; a low-confidence "maybe" should not clutter the queue."""
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.4, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "unclear"})
    work_id = _work()
    result = tagging.classify_kids_for_work(work_id)
    assert result["proposed"] is False
    assert tagging.work_tags(work_id) == []
    assert db.one("SELECT 1 FROM review_tasks WHERE kind='kids_tag' AND entity_id=?", (work_id,)) is None
    # Still recorded, so the batch job does not ask again.
    assert db.one("SELECT verdict FROM kids_classification WHERE work_id=?", (work_id,))["verdict"] == "kids"


def test_a_not_kids_verdict_never_tags_and_is_remembered(monkeypatch):
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "not_kids", "confidence": 0.97, "reason": "graphic violence"})
    work_id = _work()
    tagging.classify_kids_for_work(work_id)
    assert tagging.work_tags(work_id) == []
    assert db.one("SELECT verdict FROM kids_classification WHERE work_id=?", (work_id,))["verdict"] == "not_kids"


def test_manual_removal_is_never_reinstated_by_the_batch_job(monkeypatch):
    """A human explicitly removing the tag is a deliberate decision the automatic
    batch pass must not silently undo."""
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.9, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "x"})
    work_id = _work()
    tagging.classify_kids_for_work(work_id)
    task = db.one("SELECT id FROM review_tasks WHERE kind='kids_tag' AND entity_id=?", (work_id,))
    tagging.approve_kids_proposal(task["id"], decided_by="ada")
    assert tagging.is_visible_to(work_id, {"access_scope": "kids"})
    tagging.remove_tag(work_id, tagging.kids_tag_id())

    # already classified -> the batch runner skips it entirely
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    result = tagging.classify_kids_missing()
    assert result["considered"] == 0
    assert not tagging.is_visible_to(work_id, {"access_scope": "kids"})


def test_an_unparsable_llm_response_is_an_abstention_not_a_crash(monkeypatch):
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: None)
    work_id = _work()
    result = tagging.classify_kids_for_work(work_id)
    assert result["status"] == "abstained"


def test_batch_skips_already_checked_and_respects_limit(monkeypatch):
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "not_kids", "confidence": 0.9, "reason": "x"})
    ids = [_work(f"Book {i}") for i in range(5)]
    for wid in ids:
        helpers.own(wid)  # the scan covers owned works only
    result = tagging.classify_kids_missing(limit=3)
    assert result["considered"] == 3
    remaining = tagging.classify_kids_missing(limit=10)
    assert remaining["considered"] == 2


def test_force_reclassifies_an_already_checked_work(monkeypatch):
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "not_kids", "confidence": 0.9, "reason": "x"})
    work_id = _work()
    tagging.classify_kids_for_work(work_id)
    assert tagging.classify_kids_for_work(work_id)["status"] == "already_checked"
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.9, "audience_evidence": ["categories: Juvenile Fiction"], "reason": "x"})
    forced = tagging.classify_kids_for_work(work_id, force=True)
    assert forced["status"] == "classified" and forced["proposed"] is True
