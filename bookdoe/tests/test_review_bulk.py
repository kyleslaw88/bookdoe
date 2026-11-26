"""Inbox filters and bulk actions over review tasks (services/review_bulk.py).

Authorization (admin only, CSRF), filter semantics (kind, state, text, per-kind
counts), select-all-with-filter across pages plus an exclude list, explicit ids,
idempotency, kids_decisions recorded through the same code the card buttons use,
no cross-kind leakage, per-kind allowed actions, audit, and a several-thousand
selection in bounded batches. Plus the real app.js driven in node against a live
server (filter, select all, bulk approve).
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import migration, review_bulk, tagging

import helpers
from test_inbox_ui import live, needs_node, run_ui  # noqa: F401  (fixture + harness reuse)

app = main_module.app


def _task(title, kind="kids_tag", *, confidence=0.9, status="open", work=True):
    wid = migration._find_or_create_stub({"title": title, "author": "A", "identifiers": {}}) if work else None
    if wid and kind == "kids_tag":
        helpers.own(wid)  # a metadata task's work stays file-less so it counts as hopeless
    tid, t = db.uid(), db.now()
    payload = db.json_dump({"verdict": "kids", "confidence": confidence, "reason": "r", "model": "m"}) if kind == "kids_tag" else "{}"
    with db.tx() as conn:
        conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,?,'work',?,?,?,?,0,?)",
                     (tid, kind, wid, title, payload, status, t))
    return tid, wid


def _tagged(wid):
    return db.one("SELECT 1 FROM work_tags wt JOIN tags t ON t.id=wt.tag_id WHERE wt.work_id=? AND t.name='kids'", (wid,)) is not None


def _status(tid):
    return db.one("SELECT status FROM review_tasks WHERE id=?", (tid,))["status"]


@pytest.fixture(autouse=True)
def propose_only():
    tagging.set_auto_approve(enabled=False)


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


@pytest.fixture
def sessions(local_mode):
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"})
        ah = {"X-CSRF-Token": r.json()["csrf"]}
        admin.post("/api/v1/users", json={"username": "jane", "password": "a-very-long-jane-password", "account_type": "adult"}, headers=ah)
        jane = TestClient(app)
        r = jane.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        yield admin, ah, jane, {"X-CSRF-Token": r.json()["csrf"]}


# ---------- filters ----------
def test_list_filters_by_kind_state_text_and_reports_counts(sessions):
    admin, ah, _, _ = sessions
    k1, _ = _task("Frog Book"); k2, _ = _task("Toad Book"); m1, _ = _task("Lost Novel", "metadata_match"); o1, _ = _task("Odd Thing", "something_else")
    _task("Old One", status="ignored")
    d = admin.get("/api/v1/review").json()
    assert d["total"] == 4 and d["kinds"] == {"kids_tag": 2, "metadata_match": 1, "something_else": 1}
    assert d["bulk_actions"] == {"kids_tag": ["approve", "reject", "ignore"], "kids_recheck": ["keep", "remove"]} and d["default_bulk_actions"] == ["ignore"]
    assert {i["id"] for i in admin.get("/api/v1/review?kind=kids_tag").json()["items"]} == {k1, k2}
    assert admin.get("/api/v1/review?kind=something_else").json()["items"][0]["bulk_actions"] == ["ignore"]
    assert [i["title"] for i in admin.get("/api/v1/review?q=toad").json()["items"]] == ["Toad Book"]
    assert admin.get("/api/v1/review?status=ignored").json()["total"] == 1
    assert admin.get("/api/v1/review?status=bogus").status_code == 422
    assert admin.get("/api/v1/review?kind=metadata_match&category=hopeless").json()["total"] == 1


# ---------- authorization ----------
def test_bulk_is_admin_only_and_csrf_protected(sessions):
    admin, ah, jane, jh = sessions
    tid, wid = _task("Frog Book")
    assert jane.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "ids": [tid]}, headers=jh).status_code == 403
    assert admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "ids": [tid]}).status_code == 403, "no CSRF token"
    assert not _tagged(wid) and _status(tid) == "open"
    assert jane.get("/api/v1/review").status_code == 403


# ---------- explicit ids and kids_decisions ----------
def test_bulk_approve_by_ids_records_decisions_and_is_idempotent(sessions):
    admin, ah, _, _ = sessions
    a, wa = _task("A"); b, wb = _task("B"); c, wc = _task("C")
    r = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "ids": [a, b, a, "missing"]}, headers=ah)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected"] == 3 and body["applied"] == 2 and body["skipped"] == 1
    assert {i["id"]: i["result"] for i in body["items"]} == {a: "applied", b: "applied", "missing": "skipped"}
    assert _tagged(wa) and _tagged(wb) and not _tagged(wc)
    for w in (wa, wb):
        d = tagging.kids_decision(w)
        assert d["via"] == "proposal_approved" and d["decided_by_name"] == "ada"
    # Again: already resolved, nothing changes, no duplicate history.
    again = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "ids": [a, b]}, headers=ah).json()
    assert again["applied"] == 0 and again["skipped"] == 2 and all("already" in i["reason"] for i in again["items"])
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='kids.approved'")["n"] == 2
    audit = db.all("SELECT message,data_json FROM history WHERE event_type='review.bulk' ORDER BY created_at")
    assert len(audit) == 2 and "Bulk approve on 3" in audit[0]["message"] and '"explicit_ids":true' in audit[0]["data_json"]


def test_bulk_reject_records_proposal_rejected_and_locks_the_classifier(sessions, monkeypatch):
    admin, ah, _, _ = sessions
    a, wa = _task("Rejected")
    r = admin.post("/api/v1/review/bulk", json={"action": "reject", "kind": "kids_tag", "ids": [a]}, headers=ah).json()
    assert r["applied"] == 1 and _status(a) == "ignored" and not _tagged(wa)
    assert tagging.kids_decision(wa)["via"] == "proposal_rejected"
    from app.services import llmclient
    monkeypatch.setattr(llmclient, "chat_json", lambda *a, **k: {"verdict": "kids", "confidence": 0.99, "audience_evidence": ["categories: Juvenile Fiction"]})
    monkeypatch.setattr(llmclient, "enabled", lambda: True)
    assert tagging.classify_kids_for_work(wa, force=True)["status"] == "manually_decided"


# ---------- select-all with filter and exclusions ----------
def test_select_all_with_filter_spans_pages_and_honours_exclusions(sessions):
    admin, ah, _, _ = sessions
    tasks = [_task(f"Kid {i:03d}") for i in range(120)]           # more than one Inbox page
    keep = [tasks[3][0], tasks[77][0]]
    other, wo = _task("Meta", "metadata_match")
    r = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "filter": {}, "exclude": keep}, headers=ah).json()
    assert r["selected"] == 118 and r["applied"] == 118 and r["failed"] == 0
    assert all(_status(t) == "open" for t in keep) and _status(other) == "open"
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_tag' AND status='open'")["n"] == 2
    # Text filter narrows the selection.
    x, wx = _task("Zebra Special"); y, wy = _task("Yak Special"); z, wz = _task("Plain")
    r = admin.post("/api/v1/review/bulk", json={"action": "reject", "kind": "kids_tag", "filter": {"q": "special"}}, headers=ah).json()
    assert r["selected"] == 2 and _status(x) == "ignored" and _status(y) == "ignored" and _status(z) == "open"


def test_filter_selection_requires_a_kind_and_never_crosses_kinds(sessions):
    admin, ah, _, _ = sessions
    k, wk = _task("Kid"); m, wm = _task("Meta", "metadata_match"); o, wo = _task("Other", "weird_kind")
    assert admin.post("/api/v1/review/bulk", json={"action": "ignore", "filter": {}}, headers=ah).status_code == 400
    # Explicit ids of another kind are skipped, never acted on.
    r = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "ids": [k, m, o]}, headers=ah).json()
    assert {i["id"]: i["result"] for i in r["items"]} == {k: "applied", m: "skipped", o: "skipped"}
    assert _status(m) == "open" and _status(o) == "open"
    # metadata_match and unknown kinds: ignore only; approve/reject are refused per item.
    r = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "metadata_match", "ids": [m]}, headers=ah).json()
    assert r["items"][0]["result"] == "skipped" and "not a bulk action" in r["items"][0]["reason"] and _status(m) == "open"
    r = admin.post("/api/v1/review/bulk", json={"action": "ignore", "kind": "metadata_match", "filter": {"category": "hopeless"}}, headers=ah).json()
    assert r["applied"] == 1 and _status(m) == "ignored"
    r = admin.post("/api/v1/review/bulk", json={"action": "ignore", "kind": "weird_kind", "filter": {}}, headers=ah).json()
    assert r["applied"] == 1 and _status(o) == "ignored" and not _tagged(wo)
    assert admin.post("/api/v1/review/bulk", json={"action": "delete", "kind": "kids_tag", "ids": [k]}, headers=ah).status_code == 422


def test_manual_off_decision_is_skipped_by_bulk_approve(sessions):
    admin, ah, _, _ = sessions
    a, wa = _task("Locked Off")
    with db.tx() as conn:
        conn.execute("INSERT INTO kids_decisions(work_id,state,via,decided_at) VALUES (?,'off','manual_untag',1)", (wa,))
    r = admin.post("/api/v1/review/bulk", json={"action": "approve", "kind": "kids_tag", "filter": {}}, headers=ah).json()
    assert r["applied"] == 0 and r["skipped"] == 1 and "by hand" in r["items"][0]["reason"] and not _tagged(wa)


@pytest.mark.timeout(400)  # writes 3000 rows one at a time; the CI --timeout 120 default is too short for it
def test_large_selection_runs_in_bounded_batches(sessions, monkeypatch):
    admin, ah, _, _ = sessions
    n = 3000
    t = db.now()
    with db.tx() as conn:
        for i in range(n):
            wid = db.uid()
            conn.execute("INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'unresolved',?,?)", (wid, f"W{i}", f"w{i}", t, t))
            conn.execute("INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,'kids_tag','work',?,?,?,'open',0,?)",
                         (db.uid(), wid, f"W{i}", '{"confidence":0.9}', t + i / 1e6))
    seen_chunks = []
    orig = review_bulk.db.all

    def spy(sql, params=()):
        if "SELECT id,kind,status FROM review_tasks WHERE id IN" in sql:
            seen_chunks.append(len(params))
        return orig(sql, params)
    monkeypatch.setattr(review_bulk.db, "all", spy)
    r = admin.post("/api/v1/review/bulk", json={"action": "reject", "kind": "kids_tag", "filter": {}}, headers=ah).json()
    assert r["selected"] == n and r["applied"] == n and r["failed"] == 0 and len(r["items"]) == n
    assert seen_chunks and max(seen_chunks) <= review_bulk.BATCH and len(seen_chunks) == -(-n // review_bulk.BATCH)
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE status='open'")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM kids_decisions WHERE via='proposal_rejected'")["n"] == n


# ---------- the real Inbox UI (node, live server) ----------
@needs_node
def test_inbox_ui_filters_selects_all_and_bulk_approves(live):
    ids = [_task(f"Kid {i}") for i in range(3)]
    m, wm = _task("Meta", "metadata_match")
    out = run_ui("""
      inboxState.review.kind = 'kids_tag';
      await loadInbox('review');
      const counts = {...inboxState.review.kinds};
      const total = inboxState.review.total;
      inboxState.review.selectAll = true; inboxState.review.excluded = new Set([inboxState.review.items[0].id]);
      const r = await runReviewBulk('approve');   // confirm() is auto-accepted by the harness
      return {counts, total, applied: r.applied, selected: r.selected, after: inboxState.review.total, kinds: inboxState.review.kinds};
    """, live)
    assert out["counts"] == {"kids_tag": 3, "metadata_match": 1} and out["total"] == 3
    assert out["selected"] == 2 and out["applied"] == 2 and out["after"] == 1
    assert out["kinds"] == {"kids_tag": 1, "metadata_match": 1}
    assert sum(_tagged(w) for _, w in ids) == 2 and _status(m) == "open"


def test_inbox_ui_source_persists_the_filter_and_confirms_bulk_actions():
    js = (__import__("pathlib").Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()
    assert "localStorage.setItem(REVIEW_FILTER_KEY" in js and "ev.shiftKey && s.lastPick != null" in js
    assert "if (!confirm(`${what}?${detail}`)) return;" in js, "every bulk action confirms with kind and count"
    assert "return toast('Pick one kind of task first; bulk actions never mix kinds.', 'bad');" in js
