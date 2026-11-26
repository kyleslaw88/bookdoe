"""The evidence-based kids classifier end to end (fake LLM, no network) and the one-time
re-check of existing auto-approvals.

Pins: the prompt carries the evidence; a confident verdict is auto-approved ONLY through the
deterministic gate; everything else becomes an Inbox proposal that says why; hard adult
signals decide not_kids without the model; manual decisions stay final and nothing loops;
the re-check is dry-run first, never silently untags, and keep/remove record manual decisions.
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import kidsrecheck, llmclient, tagging

import helpers
from test_kids_gate import BLURB_ADVENTURE, MG_BLURB, book

app = main_module.app


@pytest.fixture
def llm(monkeypatch):
    """The model's answer per title: answers[title] = dict; default is a confident kids verdict."""
    answers, calls = {}, []

    def fake(system, user, **kw):
        calls.append(user)
        title = user.split("\n", 1)[0].removeprefix("Title: ")
        return answers.get(title, {"verdict": "kids", "confidence": 0.97, "reason": "gentle story",
                                   "audience_evidence": ["description: a children's story"], "adult_signals": []})
    monkeypatch.setattr(llmclient, "chat_json", fake)
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    monkeypatch.setattr(llmclient, "model_name", lambda: "qwen-test")
    answers["_calls"] = calls
    return answers


@pytest.fixture
def household(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        for name, kind in (("owen", "kid"), ("jane", "adult")):
            admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah)
        others = {}
        for name in ("owen", "jane"):
            c = TestClient(app)
            lr = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            others[name] = (c, {"X-CSRF-Token": lr.json().get("csrf", "")})
        yield admin, ah, others
        for c, _ in others.values():
            c.close()


def tagged(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def open_proposal(wid):
    return db.one("SELECT * FROM review_tasks WHERE kind='kids_tag' AND entity_id=? AND status='open'", (wid,))


def mg(title, **kw):
    kw.setdefault("description", MG_BLURB)
    kw.setdefault("publisher", "Scholastic")
    return book(title, (f"Author of {title}",), **kw)


# ---------------------------------------------------------------- prompt + gate in the classifier
def test_classifier_sends_the_evidence_fields(llm):
    wid = mg("Lighthouse", series=("Coast Tales", "3"), obs={"categories": "Juvenile Fiction", "age_range": "8-12"})
    tagging.classify_kids_for_work(wid)
    prompt = llm["_calls"][0]
    for needle in ("Title: Lighthouse", "Author(s): Author of Lighthouse", "Series: Coast Tales #3", "Publisher: Scholastic",
                   "Categories: Juvenile Fiction", "Age range: 8-12", "Pages: 320", "Language: en", f"Description: {MG_BLURB}"):
        assert needle in prompt, needle


def test_adult_space_opera_with_a_juvenile_blurb_is_proposed_not_auto_approved(llm):
    """The 2026-10-12 incident: 0.9 confidence on a sci-fi adventure blurb. Even at 0.97 there is no strong signal."""
    wid = book("Backyard Starship", ("J.N. Chaney",), publisher="Variant Publishing")
    r = tagging.classify_kids_for_work(wid)
    assert r["proposed"] is True and r["auto_approved"] is False and r["gate_blockers"] == ["no_strong_audience_signal"]
    assert not tagged(wid) and tagging.kids_decision(wid) is None
    payload = db.json_load(open_proposal(wid)["payload_json"], {})
    assert "No strong audience signal" in payload["not_auto_approved"] and payload["gate"]["passed"] is False
    assert payload["audience_evidence"] == ["description: a children's story"] and payload["model"] == "qwen-test"
    kc = db.one("SELECT * FROM kids_classification WHERE work_id=?", (wid,))
    detail = db.json_load(kc["detail_json"], {})
    assert kc["verdict"] == "kids" and detail["source"] == "llm" and detail["gate"]["blockers"] == ["no_strong_audience_signal"]


def test_true_middle_grade_is_auto_approved_with_its_signals_recorded(llm):
    wid = mg("The Lighthouse Map", obs={"categories": "Juvenile Fiction / Action & Adventure"})
    r = tagging.classify_kids_for_work(wid)
    assert r["auto_approved"] is True and tagged(wid) and open_proposal(wid) is None
    d = tagging.kids_decision(wid)
    assert d["via"] == "auto_approved" and set(db.json_load(d["note"], {})["strong_signals"]) == {"juvenile_category", "children_publisher"}


def test_hard_adult_signal_forces_not_kids_without_asking_the_model(llm):
    wid = mg("Wolves", obs={"categories": "Juvenile Fiction; Fiction / LitRPG"})
    r = tagging.classify_kids_for_work(wid)
    assert r["verdict"] == "not_kids" and r["decided_by"] == "adult_signals" and r["proposed"] is False
    assert llm["_calls"] == [] and not tagged(wid) and open_proposal(wid) is None
    kc = db.one("SELECT verdict,reason,detail_json FROM kids_classification WHERE work_id=?", (wid,))
    assert kc["verdict"] == "not_kids" and "LitRPG" in kc["reason"] and db.json_load(kc["detail_json"], {})["source"] == "deterministic"
    assert tagging.classify_kids_for_work(wid)["status"] == "already_checked" and llm["_calls"] == []


def test_a_hard_signal_found_later_closes_the_open_proposal_on_force(llm):
    wid = mg("Later")
    tagging.set_auto_approve(enabled=False)
    tagging.classify_kids_for_work(wid)
    assert open_proposal(wid)
    with db.tx() as c:
        c.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,source,confidence,is_current,observed_at) VALUES (?,'work',?,'maturity','MATURE','provider',1,1,1)", (db.uid(), wid))
    assert tagging.classify_kids_for_work(wid, force=True)["verdict"] == "not_kids"
    assert open_proposal(wid) is None


def test_an_answer_without_cited_evidence_abstains(llm):
    llm["No Cite"] = {"verdict": "kids", "confidence": 0.99, "reason": "x", "audience_evidence": []}
    wid = mg("No Cite")
    assert tagging.classify_kids_for_work(wid)["status"] == "abstained"
    assert db.one("SELECT 1 FROM kids_classification WHERE work_id=?", (wid,)) is None and not tagged(wid)


def test_model_reported_adult_signals_block_auto_approval_but_still_propose(llm):
    llm["Edgy"] = {"verdict": "kids", "confidence": 0.99, "reason": "x", "audience_evidence": ["publisher: Scholastic"], "adult_signals": ["description: war crimes"]}
    wid = mg("Edgy")
    r = tagging.classify_kids_for_work(wid)
    assert r["proposed"] and not r["auto_approved"] and r["gate_blockers"] == ["llm_reported_adult_signals"]


def test_require_strong_evidence_setting_off_restores_confidence_only_approval_but_not_over_adult_signals(llm):
    tagging.set_auto_approve(require_strong=False)
    plain = book("Plain Adventure", ("Pat",), publisher="Variant")
    assert tagging.classify_kids_for_work(plain)["auto_approved"] is True
    adult = book("Plain Adult", ("Quinn",), obs={"genres": "Erotica"})
    assert tagging.classify_kids_for_work(adult)["verdict"] == "not_kids"
    short = book("Short Blurb", ("Rae",), description="Ages 8-12.", publisher="Scholastic")
    assert tagging.classify_kids_for_work(short)["auto_approved"] is False


def test_threshold_still_applies_and_default_is_095(llm):
    assert tagging.auto_threshold() == 0.95
    llm["Under"] = {"verdict": "kids", "confidence": 0.9, "reason": "x", "audience_evidence": ["publisher: Scholastic"]}
    r = tagging.classify_kids_for_work(mg("Under"))
    assert r["proposed"] and not r["auto_approved"] and r["gate_blockers"] == ["below_threshold"]


def test_manual_locks_and_repeat_runs_never_loop(llm):
    wid = mg("Locked")
    tagging.set_kids_tag_manually(wid, False, by_user_id=None, by_name="ada")
    for _ in range(3):
        assert tagging.classify_kids_for_work(wid, force=True)["status"] == "manually_decided"
        tagging.classify_kids_missing(limit=50); tagging.auto_approve_pending()
    assert llm["_calls"] == [] and not tagged(wid)
    ok = mg("Fine")
    tagging.classify_kids_for_work(ok)
    calls = len(llm["_calls"])
    for _ in range(3):
        tagging.classify_kids_missing(limit=50); tagging.auto_approve_pending()
    assert len(llm["_calls"]) == calls and tagged(ok)


def test_sweep_applies_the_same_gate_to_waiting_proposals(llm):
    tagging.set_auto_approve(enabled=False)
    good, weak = mg("Sweep Good"), book("Sweep Weak", ("Zed",), publisher="Variant")
    tagging.classify_kids_for_work(good); tagging.classify_kids_for_work(weak)
    tagging.set_auto_approve(enabled=True)
    out = tagging.auto_approve_pending()
    assert out["approved"] == 1 and out["skipped"] == 1 and tagged(good) and not tagged(weak)
    assert "No strong audience signal" in db.json_load(open_proposal(weak)["payload_json"], {})["not_auto_approved"]
    assert tagging.auto_approve_pending()["approved"] == 0


# ---------------------------------------------------------------- settings and book page
def test_settings_route_exposes_and_audits_the_new_setting(household):
    admin, ah, others = household
    cfg = admin.get("/api/v1/settings/kids").json()
    assert cfg["kids_auto_threshold"] == 0.95 and cfg["kids_require_strong_evidence"] is True
    r = admin.put("/api/v1/settings/kids", json={"kids_require_strong_evidence": False}, headers=ah)
    assert r.status_code == 200 and r.json()["kids_require_strong_evidence"] is False and r.json()["kids_auto_threshold"] == 0.95
    h = db.one("SELECT message FROM history WHERE event_type='settings.kids' ORDER BY created_at DESC")
    assert "not required" in h["message"]
    assert admin.put("/api/v1/settings/kids", json={"kids_auto_threshold": 0.5}, headers=ah).status_code == 400
    assert admin.put("/api/v1/settings/kids", json={"kids_require_strong_evidence": True}).status_code in (401, 403)
    c, ch = others["jane"]
    assert c.put("/api/v1/settings/kids", json={"kids_require_strong_evidence": True}, headers=ch).status_code in (401, 403)
    assert c.get("/api/v1/settings/kids").status_code in (401, 403)


def test_book_page_explains_why_it_is_on_the_shelf(household, llm):
    admin, ah, others = household
    ok = mg("Shown", obs={"categories": "Juvenile Fiction"})
    tagging.classify_kids_for_work(ok)
    k = admin.get(f"/api/v1/library/{ok}").json()["kids_evidence"]
    assert k["verdict"] == "kids" and k["auto_approved"] is True and k["gate"]["passed"] is True
    assert {s["id"] for s in k["strong_signals"]} == {"juvenile_category", "children_publisher"}
    assert k["audience_evidence"] == ["description: a children's story"] and k["model"] == "qwen-test"
    # An old-style auto-approval that does not pass today's gate says so.
    weak = book("Old Weak", ("Zed",), publisher="Variant")
    tagging.add_tag(weak, tagging.kids_tag_id(), source="llm", confidence=0.9)
    tagging.set_kids_decision(weak, "on", via="auto_approved", by=None, note=db.json_dump({"confidence": 0.9, "model": "old"}))
    k = admin.get(f"/api/v1/library/{weak}").json()["kids_evidence"]
    assert k["auto_approved"] and k["gate"]["passed"] is False and "no_strong_audience_signal" in k["gate"]["blockers"] and k["has_cited_evidence"] is False
    # A kid never gets the evidence block.
    kid, _ = others["owen"]
    assert "kids_evidence" not in kid.get(f"/api/v1/library/{ok}").json()


# ---------------------------------------------------------------- the re-check of existing auto-approvals
def old_auto(title, *, authors=None, description=MG_BLURB, publisher=None, obs=None, conf=0.97, verdict="kids", series=None):
    """A book auto-approved under the OLD rules: tagged, decision auto_approved, stored verdict only."""
    wid = book(title, tuple(authors or (f"Author of {title}",)), description=description, publisher=publisher, obs=obs, series=series)
    helpers.own(wid)
    tagging.add_tag(wid, tagging.kids_tag_id(), source="llm", confidence=conf)
    tagging.set_kids_decision(wid, "on", via="auto_approved", by=None, note=db.json_dump({"confidence": conf, "model": "old-model", "threshold": 0.85}))
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO kids_classification(work_id,verdict,confidence,reason,checked_at) VALUES (?,?,?,?,?)", (wid, verdict, conf, "aligns with middle-grade", db.now()))
    return wid


@pytest.fixture
def legacy(llm):
    """Five old auto-approvals: one that passes the gate and four that do not (one reason each)."""
    p = {
        "passes": old_auto("Real Middle Grade", publisher="Scholastic", obs={"categories": "Juvenile Fiction"}),
        "no_signal": old_auto("Pirates Nebula", authors=["J.N. Chaney"], description=BLURB_ADVENTURE, publisher="Variant"),
        "adult_genre": old_auto("Fields of Fire", authors=["J.N. Chaney"], description=BLURB_ADVENTURE, obs={"categories": "Science Fiction / LitRPG"}),
        "no_desc": old_auto("Mystery", description="", publisher="Scholastic"),
        "low_conf": old_auto("Defiant Exile", authors=["Lee"], publisher="Scholastic", conf=0.85),
    }
    llm["_calls"].clear()
    return p


def test_dry_run_counts_and_writes_nothing(household, legacy, llm):
    admin, ah, _ = household
    before = db.one("SELECT COUNT(*) n FROM review_tasks")["n"]
    d = admin.get("/api/v1/tags/kids-auto/recheck").json()
    assert (d["checked"], d["pass"], d["fail"], d["dry_run"]) == (5, 1, 4, True)
    assert d["by_blocker"]["no_strong_audience_signal"] == 2 and d["by_blocker"]["description_too_short"] == 1
    assert d["by_blocker"]["hard_adult_signals"] == 1 and d["by_blocker"]["below_threshold"] == 1
    assert admin.post("/api/v1/tags/kids-auto/recheck", json={}, headers=ah).json()["dry_run"] is True  # dry-run is the default
    assert db.one("SELECT COUNT(*) n FROM review_tasks")["n"] == before and all(tagged(w) for w in legacy.values())
    assert llm["_calls"] == [], "the re-check never calls the model"


def test_recheck_queues_failures_without_untagging_and_is_idempotent(household, legacy):
    admin, ah, _ = household
    r = admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah).json()
    assert (r["pass"], r["fail"], r["queued"]) == (1, 4, 4)
    assert all(tagged(w) for w in legacy.values()), "nothing is untagged by the re-check"
    assert all(tagging.kids_decision(w)["via"] == "auto_approved" for w in legacy.values())
    tasks = admin.get("/api/v1/review?kind=kids_recheck").json()
    assert tasks["total"] == 4 and tasks["kinds"]["kids_recheck"] == 4 and tasks["bulk_actions"]["kids_recheck"] == ["keep", "remove"]
    by_title = {t["title"]: t["payload"] for t in tasks["items"]}
    assert "Pirates Nebula" in by_title and by_title["Pirates Nebula"]["blockers"] == ["no_strong_audience_signal"]
    assert any("LitRPG" in x for x in by_title["Fields of Fire"]["reasons"]) and "Real Middle Grade" not in by_title
    again = admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah).json()
    assert again["queued"] == 0 and again["refreshed"] == 4 and admin.get("/api/v1/review?kind=kids_recheck").json()["total"] == 4


def test_fixed_evidence_closes_the_task_on_the_next_run(household, legacy):
    admin, ah, _ = household
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    with db.tx() as c:
        c.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,source,confidence,is_current,observed_at) VALUES (?,'work',?,'categories','Juvenile Fiction','provider',1,1,1)", (db.uid(), legacy["no_signal"]))
    r = admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah).json()
    assert (r["pass"], r["fail"], r["closed"], r["queued"]) == (2, 3, 1, 0)
    assert db.one("SELECT status FROM review_tasks WHERE kind='kids_recheck' AND entity_id=?", (legacy["no_signal"],))["status"] == "resolved"
    assert admin.get("/api/v1/review?kind=kids_recheck").json()["total"] == 3 and tagged(legacy["no_signal"])


def test_bulk_keep_records_manual_tag_and_bulk_remove_records_manual_untag(household, legacy, llm):
    admin, ah, _ = household
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    tasks = {t["title"]: t["id"] for t in admin.get("/api/v1/review?kind=kids_recheck").json()["items"]}
    keep = admin.post("/api/v1/review/bulk", json={"action": "keep", "kind": "kids_recheck", "ids": [tasks["Pirates Nebula"], tasks["Mystery"]]}, headers=ah).json()
    assert keep["applied"] == 2 and keep["failed"] == 0
    remove = admin.post("/api/v1/review/bulk", json={"action": "remove", "kind": "kids_recheck", "ids": [tasks["Fields of Fire"], tasks["Defiant Exile"]]}, headers=ah).json()
    assert remove["applied"] == 2
    for t in ("Pirates Nebula", "Mystery"):
        w = db.one("SELECT id FROM works WHERE title=?", (t,))["id"]
        assert tagged(w) and tagging.kids_decision(w)["via"] == "manual_tag" and tagging.kids_decision(w)["state"] == "on"
    for t in ("Fields of Fire", "Defiant Exile"):
        w = db.one("SELECT id FROM works WHERE title=?", (t,))["id"]
        assert not tagged(w) and tagging.kids_decision(w)["via"] == "manual_untag" and tagging.kids_decision(w)["state"] == "off"
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_recheck' AND status='open'")["n"] == 0
    # Final: the classifier and another re-check leave them alone, and the passing book was never touched.
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    for w in legacy.values():
        assert tagging.classify_kids_for_work(w, force=True)["status"] in ("manually_decided", "already_tagged")
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_recheck' AND status='open'")["n"] == 0
    assert tagging.kids_decision(legacy["passes"])["via"] == "auto_approved" and llm["_calls"] == []
    # A repeat of the same bulk is a no-op, and kids_tag-only verbs are refused for this kind.
    again = admin.post("/api/v1/review/bulk", json={"action": "keep", "kind": "kids_recheck", "ids": [tasks["Pirates Nebula"]]}, headers=ah).json()
    assert again["applied"] == 0 and again["skipped"] == 1
    wrong = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_recheck", "ids": [tasks["Pirates Nebula"]]}, headers=ah).json()
    assert wrong["applied"] == 0


def test_single_item_keep_and_remove_routes_and_a_decided_book_is_not_actionable(household, legacy):
    admin, ah, _ = household
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    tasks = {t["title"]: t["id"] for t in admin.get("/api/v1/review?kind=kids_recheck").json()["items"]}
    assert admin.post(f"/api/v1/review/{tasks['Mystery']}/kids-recheck/keep", headers=ah).json()["tagged"] is True
    assert admin.post(f"/api/v1/review/{tasks['Mystery']}/kids-recheck/keep", headers=ah).status_code == 409
    assert admin.post(f"/api/v1/review/{tasks['Defiant Exile']}/kids-recheck/remove", headers=ah).json()["tagged"] is False
    # A person acts on the book elsewhere (book page untag) while its task is open: the task closes, nothing is double-applied.
    w = legacy["no_signal"]
    tagging.set_kids_tag_manually(w, False, by_user_id=None, by_name="ada")
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (tasks["Pirates Nebula"],))["status"] == "ignored"
    assert admin.post(f"/api/v1/review/{tasks['Pirates Nebula']}/kids-recheck/keep", headers=ah).status_code == 409
    assert not tagged(w), "an ignored task cannot re-add the tag"


def test_manual_decisions_are_outside_the_recheck(household, legacy):
    admin, ah, _ = household
    tagging.set_kids_tag_manually(legacy["no_signal"], True, by_user_id=None, by_name="ada")  # a human re-confirmed it
    d = admin.get("/api/v1/tags/kids-auto/recheck").json()
    assert d["checked"] == 4 and d["fail"] == 3


def test_recheck_is_admin_only_and_needs_csrf(household, legacy):
    admin, ah, others = household
    assert admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}).status_code in (401, 403), "no CSRF token"
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_recheck'")["n"] == 0
    anon = TestClient(app)
    for client, headers in ((anon, {}), others["jane"], others["owen"]):
        assert client.get("/api/v1/tags/kids-auto/recheck").status_code in (401, 403)
        assert client.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=headers).status_code in (401, 403)
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    t = db.one("SELECT id FROM review_tasks WHERE kind='kids_recheck' LIMIT 1")["id"]
    for client, headers in ((anon, {}), others["jane"], others["owen"]):
        assert client.post(f"/api/v1/review/{t}/kids-recheck/keep", headers=headers).status_code in (401, 403)
        assert client.post("/api/v1/review/bulk", json={"action": "remove", "kind": "kids_recheck", "ids": [t]}, headers=headers).status_code in (401, 403)
    assert admin.post(f"/api/v1/review/{t}/kids-recheck/remove").status_code in (401, 403), "no CSRF token"
    assert db.one("SELECT status FROM review_tasks WHERE id=?", (t,))["status"] == "open" and all(tagged(w) for w in legacy.values())
    anon.close()


def test_recheck_history_is_audited(household, legacy):
    admin, ah, _ = household
    admin.post("/api/v1/tags/kids-auto/recheck", json={"dry_run": False}, headers=ah)
    h = db.one("SELECT message FROM history WHERE event_type='kids.recheck'")
    assert "5 auto-approved" in h["message"] and "4 need review" in h["message"]
    assert kidsrecheck.GROUP_LABEL == "Auto-tagged without audience evidence"
