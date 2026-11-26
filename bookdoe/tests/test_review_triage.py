"""Working down the Inbox backlog: exact-identifier auto-resolve, hopeless bulk-ignore,
category counts, and the admin-only routes."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import metadata, migration, review_triage

import helpers

client = TestClient(app)


def _work(title, identifiers=None):
    return migration._find_or_create_stub({"title": title, "author": "Test Author", "identifiers": identifiers or {}})


def _task(work_id, candidates=None):
    tid = db.uid()
    payload = {"candidates": candidates, "confidence": 0.5} if candidates else {"source": {"title": "x"}}
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO review_tasks(id,kind,entity_type,entity_id,title,payload_json,status,priority,created_at) VALUES (?,'metadata_match','work',?,?,?, 'open',50,?)",
            (tid, work_id, "t", db.json_dump(payload), db.now()),
        )
    return tid


def _status(tid):
    return db.one("SELECT status FROM review_tasks WHERE id=?", (tid,))["status"]


def _meta(mid, title):
    return {"id": mid, "title": title, "contributors": [], "identifiers": [], "editions": []}


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(metadata, "get_work", lambda mid: _meta(mid, f"Linked {mid}"))
    exact_isbn = _work("Exact By Isbn", {"isbn13": "9781234567897"})
    t_isbn = _task(exact_isbn, [{"id": "or-1", "title": "Exact", "identifiers": [{"namespace": "isbn13", "value": "978-1-2345-6789-7"}]}])
    exact_ol = _work("Exact By Candidate Id", {"openlibrary": "OL123456W"})
    t_ol = _task(exact_ol, [{"id": "OL123456W", "title": "Exact"}])
    differs = _work("Differs", {"isbn13": "9780000000002"})
    t_diff = _task(differs, [{"id": "or-3", "title": "Other", "identifiers": [{"namespace": "isbn13", "value": "9781111111111"}]}])
    hopeless = _work("Hopeless")
    t_hope = _task(hopeless)
    owned = _work("Owned No Ids")
    helpers.own(owned)
    t_owned = _task(owned)
    return dict(exact_isbn=exact_isbn, t_isbn=t_isbn, exact_ol=exact_ol, t_ol=t_ol, differs=differs, t_diff=t_diff,
                hopeless=hopeless, t_hope=t_hope, owned=owned, t_owned=t_owned)


def test_summary_counts_each_task_in_exactly_one_category(seeded):
    s = review_triage.summary()
    assert s["categories"] == {"identifier-exact": 2, "has-owned-files": 1, "hopeless": 1, "other": 1}
    assert s["total"] == 5


def test_auto_resolve_links_exact_matches_and_leaves_the_rest(seeded):
    r = review_triage.auto_resolve_identifier_matches(50)
    assert r["linked"] == 2 and r["errors"] == []
    assert _status(seeded["t_isbn"]) == "resolved" and _status(seeded["t_ol"]) == "resolved"
    assert db.one("SELECT metadata_id FROM works WHERE id=?", (seeded["exact_isbn"],))["metadata_id"] == "or-1"
    assert db.one("SELECT metadata_id FROM works WHERE id=?", (seeded["exact_ol"],))["metadata_id"] == "OL123456W"
    # A candidate whose identifier differs is a guess, not a match: untouched.
    assert _status(seeded["t_diff"]) == "open"
    assert db.one("SELECT metadata_id FROM works WHERE id=?", (seeded["differs"],))["metadata_id"] is None
    assert db.json_load(db.one("SELECT resolution_json FROM review_tasks WHERE id=?", (seeded["t_isbn"],))["resolution_json"], {})["auto"] == "identifier-exact"


def test_auto_resolve_respects_limit(seeded):
    assert review_triage.auto_resolve_identifier_matches(1)["linked"] == 1
    assert review_triage.summary()["categories"]["identifier-exact"] == 1


def test_ambiguous_candidates_are_left_for_a_person(monkeypatch):
    monkeypatch.setattr(metadata, "get_work", lambda mid: _meta(mid, mid))
    w = _work("Ambiguous", {"isbn13": "9781234567897"})
    ident = [{"namespace": "isbn13", "value": "9781234567897"}]
    t = _task(w, [{"id": "or-a", "identifiers": ident}, {"id": "or-b", "identifiers": ident}])
    assert review_triage.auto_resolve_identifier_matches(10)["linked"] == 0
    assert _status(t) == "open"


def test_ignore_hopeless_skips_works_with_files_ids_or_candidates(seeded):
    assert review_triage.bulk_ignore_hopeless(10) == {"ignored": 1}
    assert _status(seeded["t_hope"]) == "ignored"
    assert db.one("SELECT resolved_at FROM review_tasks WHERE id=?", (seeded["t_hope"],))["resolved_at"]
    for k in ("t_owned", "t_isbn", "t_ol", "t_diff"):
        assert _status(seeded[k]) == "open"


def test_ignore_hopeless_is_batched():
    tids = [_task(_work(f"H{i}")) for i in range(5)]
    assert review_triage.bulk_ignore_hopeless(2) == {"ignored": 2}
    assert sum(_status(t) == "open" for t in tids) == 3


def test_routes_summary_categories_and_actions(seeded):
    assert client.get("/api/v1/review/triage").json()["categories"]["hopeless"] == 1
    r = client.get("/api/v1/review?limit=1").json()
    assert r["total"] == 5 and r["categories"]["identifier-exact"] == 2
    ex = client.get("/api/v1/review?category=identifier-exact").json()
    assert ex["total"] == 2 and {i["id"] for i in ex["items"]} == {seeded["t_isbn"], seeded["t_ol"]}
    assert client.get("/api/v1/review?category=hopeless").json()["total"] == 1
    assert client.get("/api/v1/review?category=bogus").status_code == 422
    assert client.post("/api/v1/review/triage", json={"action": "ignore_hopeless", "limit": 10}).json() == {"ignored": 1}
    assert client.post("/api/v1/review/triage", json={"action": "auto_resolve", "limit": 10}).json()["linked"] == 2
    assert client.post("/api/v1/review/triage", json={"action": "nuke"}).status_code == 422
    assert client.post("/api/v1/review/triage", json={"action": "auto_resolve", "limit": 10, "x": 1}).status_code == 422


def test_triage_post_requires_admin(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(app) as admin:
        csrf = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"}).json()["csrf"]
        admin.post("/api/v1/users", json={"username": "reader", "password": "a-very-long-reader-password", "access_scope": "full", "is_admin": False},
                   headers={"X-CSRF-Token": csrf})
        reader = TestClient(app)
        rcsrf = reader.post("/api/v1/auth/login", json={"username": "reader", "password": "a-very-long-reader-password"}).json()["csrf"]
        body = {"action": "ignore_hopeless", "limit": 5}
        assert reader.post("/api/v1/review/triage", json=body, headers={"X-CSRF-Token": rcsrf}).status_code == 403
        assert admin.post("/api/v1/review/triage", json=body).status_code in (401, 403)  # no CSRF token
        assert admin.post("/api/v1/review/triage", json=body, headers={"X-CSRF-Token": csrf}).status_code == 200
        reader.close()


@pytest.mark.parametrize("config,expected", [(None, 25), ('{"batch": 3}', 3), ('{"batch": "junk"}', 25), ('{"batch": 99999}', 1000)])
def test_metadata_refresh_batch_comes_from_job_config(monkeypatch, config, expected):
    from app.services import scheduler
    for i in range(30):
        _work(f"Unlinked {i}")
    calls = []
    monkeypatch.setattr(metadata, "refresh_work", lambda wid, **kw: calls.append(wid) or {"status": "unmatched"})
    scheduler._metadata_refresh({"config_json": config})
    assert len(calls) == min(expected, 30)
