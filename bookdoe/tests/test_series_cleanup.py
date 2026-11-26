"""D1/D9: junk series are gone and stay gone, the backfill gives the uncertain filter
evidence, and the series/authors listings are paged server-side."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main, migrations
from app.services import metadata

JUNK = ["#20056/1", "(Gollancz detection)", "(Gollancz SF)", "100 pages", "1028", "10|18"]


def _work(title):
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)", (wid, title, title, t, t))
    return wid


def _series_with(name, positions):
    """A series row plus one work per entry of `positions` (None = no position)."""
    sid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, name, name, t, t))
    for i, pos in enumerate(positions):
        w = _work(f"{name} book {i}")
        with db.tx() as conn:
            conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (w, sid, pos))
    return sid


def _names(**params):
    with TestClient(main.app) as c:
        r = c.get("/api/v1/series", params=params)
    assert r.status_code == 200
    return r.json()


def _rerun_step_13():
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=13")
    return migrations.run_pending_migrations()


def test_migration_13_deletes_junk_and_backfills_assertions_on_a_pre_13_database():
    for j in JUNK:
        _series_with(j, ["1"])
    real = _series_with("Real Multi", ["1", "2"])
    single_no_pos = _series_with("Lonely", [None])
    assert db.one("SELECT COUNT(*) n FROM series_assertions")["n"] == 0  # pre-13 shape: no evidence

    ran = _rerun_step_13()

    assert [r["version"] for r in ran] == [13]
    assert ran[0]["row_counts"]["junk_series_deleted"] == len(JUNK)
    assert ran[0]["row_counts"]["assertions_backfilled"] == 3  # 2 real memberships + 1 lonely
    left = {r["name"] for r in db.all("SELECT name FROM series")}
    assert left == {"Real Multi", "Lonely"}
    assert db.one("SELECT COUNT(*) n FROM series_memberships")["n"] == 3  # junk memberships went too
    status = {r["series_id"]: r["status"] for r in db.all("SELECT series_id,status FROM series_assertions")}
    # bare name + no ordinal + no other member scores 0.35, i.e. 'rejected' (below the 0.4
    # uncertain floor) exactly as _link_series would have recorded it; both statuses are hidden.
    assert status[single_no_pos] in ("uncertain", "rejected")
    assert db.one("SELECT evidence_type FROM series_assertions WHERE series_id=?", (single_no_pos,))["evidence_type"] == "free_text"
    assert {r["evidence_type"] for r in db.all("SELECT evidence_type FROM series_assertions WHERE series_id=?", (real,))} == {"explicit_position"}

    names = {s["name"] for s in _names()["items"]}
    assert names == {"Real Multi"}                      # single-work, no ordinal: hidden by default
    assert {s["name"] for s in _names(include_uncertain=True)["items"]} == {"Real Multi", "Lonely"}


def test_migration_13_is_idempotent():
    _series_with("Real Multi", ["1", "2"])
    _rerun_step_13()
    n = db.one("SELECT COUNT(*) n FROM series_assertions")["n"]
    ran = _rerun_step_13()
    assert ran[0]["row_counts"] == {"junk_series_deleted": 0, "assertions_backfilled": 0}
    assert db.one("SELECT COUNT(*) n FROM series_assertions")["n"] == n


def _meta(mid, title, series):
    return {"id": mid, "title": title, "contributors": [{"id": "a1", "name": "Author A", "role": "author"}],
            "series": series, "identifiers": [], "editions": []}


def test_series_listing_never_contains_the_live_junk_through_the_normal_link_path():
    for i, name in enumerate(JUNK):
        metadata.upsert_work(_meta(f"j{i}", f"Junk book {i}", [{"id": f"js{i}", "name": name, "position": "1"}]))
    for series in ("The Expanse", "Discworld"):
        for n in (1, 2):
            metadata.upsert_work(_meta(f"{series}{n}", f"{series} {n}", [{"id": f"id-{series}", "name": series, "position": str(n)}]))

    for params in ({}, {"include_uncertain": True}):
        names = {s["name"] for s in _names(**params)["items"]}
        assert names == {"The Expanse", "Discworld"}, names
    # and the rejected links left a trace
    assert db.one("SELECT COUNT(*) n FROM series_assertions WHERE status='rejected'")["n"] == len(JUNK)


def test_series_list_honours_limit_offset_q_and_returns_total():
    for name in ("Alpha", "Beta", "Gamma", "Delta"):
        _series_with(name, ["1", "2", "4"] if name == "Beta" else ["1", "2"])
    _rerun_step_13()
    body = _names(limit=2, offset=1)
    assert body["total"] == 4
    assert [s["name"] for s in body["items"]] == ["Beta", "Delta"]
    assert next(s for s in body["items"] if s["name"] == "Beta")["gap_count"] == 1
    assert [s["name"] for s in _names(q="ga")["items"]] == ["Gamma"]
    assert _names(q="ga")["total"] == 1
    assert _names(q="100%")["total"] == 0  # LIKE wildcards in the query are literal


def test_series_list_limit_must_be_positive():
    with TestClient(main.app) as c:
        assert c.get("/api/v1/series?limit=0").status_code == 422


def test_gap_counts_do_not_cost_one_query_per_series(monkeypatch):
    for i in range(30):
        _series_with(f"S{i:02d}", ["1", "3"])
    _rerun_step_13()
    seen = []
    real = db.connect

    def counting(*a, **kw):
        conn = real(*a, **kw)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(db, "connect", counting)
    body = _names()
    selects = [s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert body["total"] == 30 and all(s["gap_count"] == 1 for s in body["items"])
    assert len(selects) <= 7, len(selects)  # constant: one extra read for the series.max_position setting


def test_authors_list_honours_limit_offset_q_and_total():
    t = db.now()
    with db.tx() as conn:
        for cid, name in (("c1", "Ann Adams"), ("c2", "Bob Brown"), ("c3", "Cy Adler")):
            conn.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, name, name, t, t))
    with TestClient(main.app) as c:
        full = c.get("/api/v1/authors").json()
        page = c.get("/api/v1/authors?limit=1&offset=1").json()
        filtered = c.get("/api/v1/authors?q=ad").json()
    assert full["total"] == 3 and len(full["items"]) == 3
    assert [a["name"] for a in page["items"]] == ["Bob Brown"] and page["total"] == 3
    assert {a["name"] for a in filtered["items"]} == {"Ann Adams", "Cy Adler"} and filtered["total"] == 2
