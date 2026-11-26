"""Automatic wanted search: fair ordering, draining in batches, budget, daily cap, cooldown.

Only the indexer layer (releases.search_releases) and the download client are faked; the
job, its state table, the grab path, the settings API and the database are the real ones.
Time is a fake clock: no test sleeps.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.integrations import downloaders
from app.services import migration, scheduler, wanted_search

import helpers
from test_download_cancel import _client_row

client = TestClient(main_module.app)
H = 3600.0


class World:
    """Fake indexers + fake clock. `matches` is the set of work ids a search finds."""

    def __init__(self, monkeypatch):
        self.t = 1_000_000.0
        self.mono = 0.0
        self.per_search = 0.0          # fake seconds a search "takes"
        self.matches: set[str] = set()
        self.calls: list[tuple[str, bool]] = []
        self.errors: dict[str, list] = {}    # work_id -> errors list returned with no items
        self.raises: dict[str, str] = {}
        self.rid_by_work: dict[str, str] = {}
        self.sleeps: list[float] = []
        monkeypatch.setattr(db, "now", lambda: self.t)
        monkeypatch.setattr(wanted_search, "monotonic", lambda: self.mono)
        monkeypatch.setattr(wanted_search, "sleep", self._sleep)
        monkeypatch.setattr(scheduler.releases, "search_releases", self.search)
        monkeypatch.setattr(downloaders, "add", lambda c, rel: {"ok": True, "id": "job-" + rel["id"]})

    def _sleep(self, s):
        self.sleeps.append(s)
        self.mono += s
        self.t += s

    def search(self, work_id, kind, automatic=False):
        self.calls.append((work_id, automatic))
        sid = db.uid()
        with db.tx() as conn:  # what the real search records; the daily cap counts these rows
            conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'[]',?,?,'complete')", (sid, work_id, kind, int(automatic), self.t))
        self.mono += self.per_search
        self.t += self.per_search
        if work_id in self.raises:
            raise RuntimeError(self.raises[work_id])
        if work_id in self.errors:
            return {"items": [], "errors": self.errors[work_id]}
        if work_id in self.matches:
            rid = db.uid()
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_guid,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                    (rid, sid, work_id, kind, rid, "usenet", "Match", "http://idx.test/x", 95, self.t))
            w = db.one("SELECT title FROM works WHERE id=?", (work_id,))["title"]
            return {"items": [{"id": rid, "rejected": 0, "score": 95, "title": f"{w} by Author {int(w[-2:])} [ENG / M4B]"}], "errors": []}
        return {"items": [], "errors": []}


@pytest.fixture
def world(monkeypatch):
    _client_row("sabnzbd")
    return World(monkeypatch)


def make_assets(n, kind="audiobook"):
    """n wanted assets; ids in creation order (added_at increases)."""
    out = []
    for i in range(n):
        w = migration._find_or_create_stub({"title": f"Book {i:02d}", "author": f"Author {i}", "identifiers": {}})
        a = helpers.want(w, kind)
        with db.tx() as conn:
            conn.execute("UPDATE media_assets SET added_at=? WHERE id=?", (1000 + i, a))
        out.append((w, a))
    return out


def run_job(**cfg):
    job = {"config_json": json.dumps(cfg)} if cfg else None
    return wanted_search.run(job)


def state(asset_id):
    return db.one("SELECT * FROM wanted_search_state WHERE asset_id=?", (asset_id,))


# ---- starvation -----------------------------------------------------------------------

def test_assets_that_never_match_do_not_starve_the_rest(world):
    assets = make_assets(6)
    world.matches = {assets[5][0]}               # only the last one ever matches
    first = run_job(batch=5, delay_seconds=0)
    # Everything eligible is tried once in the same run (drained), the last one is grabbed.
    assert first["searched"] == 6 and first["grabbed"] == 1
    assert {w for w, _ in assets} == {w for w, _ in world.calls}


def test_budget_cut_runs_reach_the_sixth_asset_on_the_next_run(world):
    """Runs cut short by the budget after 5 searches (the old LIMIT 5): the 5 that never
    match cooled down, so the next run starts with the sixth, which then matches."""
    assets = make_assets(6)
    world.per_search = 2.0
    r2 = run_job(budget_seconds=10, delay_seconds=0)
    assert r2["stopped_reason"] == "budget" and r2["searched"] == 5
    world.calls.clear()
    world.matches = {assets[5][0]}
    r3 = run_job(budget_seconds=10, delay_seconds=0)
    assert world.calls[0][0] == assets[5][0] and r3["grabbed"] == 1
    assert r3["skipped_on_cooldown"] == 5 and r3["searched"] == 1


def test_never_searched_first_then_least_recently_searched(world):
    assets = make_assets(3)
    with db.tx() as conn:
        conn.execute("INSERT INTO wanted_search_state(asset_id,last_auto_search_at,misses,next_eligible_at,last_outcome,updated_at) VALUES (?,?,0,NULL,'error',?)", (assets[0][1], world.t - 100, world.t))
        conn.execute("INSERT INTO wanted_search_state(asset_id,last_auto_search_at,misses,next_eligible_at,last_outcome,updated_at) VALUES (?,?,0,NULL,'error',?)", (assets[1][1], world.t - 500, world.t))
    run_job(delay_seconds=0)
    assert [w for w, _ in world.calls][:3] == [assets[2][0], assets[1][0], assets[0][0]]


# ---- draining -------------------------------------------------------------------------

def test_28_eligible_with_batch_20_are_two_batches_in_one_run(world):
    make_assets(28)
    res = run_job(batch=20, delay_seconds=0)
    assert (res["eligible"], res["searched"], res["batches"], res["stopped_reason"]) == (28, 28, 2, "drained")
    assert res["no_result"] == 28 and res["grabbed"] == 0 and res["skipped_on_cooldown"] == 0
    assert len(world.calls) == 28 and all(auto for _, auto in world.calls)


def test_a_second_run_right_after_finds_everything_cooling_down(world):
    make_assets(5)
    run_job(delay_seconds=0)
    world.calls.clear()
    res = run_job(delay_seconds=0)
    assert res["searched"] == 0 and res["skipped_on_cooldown"] == 5 and res["stopped_reason"] == "drained"
    assert world.calls == []


def test_budget_stops_the_run_and_the_next_run_continues_where_it_left_off(world):
    assets = make_assets(10)
    world.per_search = 2.0
    res = run_job(budget_seconds=10, delay_seconds=0)
    assert (res["stopped_reason"], res["searched"]) == ("budget", 5)
    res2 = run_job(budget_seconds=60, delay_seconds=0)
    assert (res2["stopped_reason"], res2["searched"], res2["skipped_on_cooldown"]) == ("drained", 5, 5)
    assert [w for w, _ in world.calls] == [w for w, _ in assets]


def test_budget_never_overshoots_by_more_than_one_search(world):
    make_assets(30)
    world.per_search = 3.0
    res = run_job(budget_seconds=20, delay_seconds=0)
    assert res["stopped_reason"] == "budget"
    assert res["searched"] == 7 and res["elapsed"] <= 20 + 3.0   # checked before each search
    assert res["eligible"] == 30


def test_pacing_sleep_between_searches_uses_the_fake_clock(world):
    make_assets(3)
    run_job(delay_seconds=1.5)
    assert world.sleeps == [1.5, 1.5]    # not before the first search


def test_daily_cap_ends_the_run_cleanly(world):
    make_assets(10)
    db.set_setting("acquisition.wanted_search_daily_cap", 4)
    # 3 automatic searches already happened in the last day; manual ones do not count.
    with db.tx() as conn:
        for auto in (1, 1, 1, 0, 0):
            conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,NULL,'ebook','[]',?,?,'complete')", (db.uid(), auto, world.t - 3600))
        conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,NULL,'ebook','[]',1,?,'complete')", (db.uid(), world.t - 90000))  # outside 24 h
    res = run_job(delay_seconds=0)
    assert res["stopped_reason"] == "daily_cap" and res["searched"] == 1
    assert run_job(delay_seconds=0)["searched"] == 0
    world.t += 2 * 86400
    assert run_job(delay_seconds=0)["searched"] == 4   # a fresh window: the cap bites again at 4


# ---- cooldown -------------------------------------------------------------------------

def test_cooldown_doubles_per_miss_up_to_seven_days():
    assert [wanted_search.cooldown_seconds(n, 12) / H for n in range(0, 7)] == [0, 12, 24, 48, 96, 168, 168]
    assert wanted_search.cooldown_seconds(3, 6) / H == 24


def test_miss_sets_cooldown_and_asset_returns_after_it(world):
    (w, a), = make_assets(1)
    run_job(delay_seconds=0)
    s = state(a)
    assert s["misses"] == 1 and s["next_eligible_at"] == world.t + 12 * H and s["last_outcome"] == "no_result"
    world.t += 12 * H - 1
    assert run_job(delay_seconds=0)["searched"] == 0
    world.t += 2
    assert run_job(delay_seconds=0)["searched"] == 1
    assert state(a)["misses"] == 2 and state(a)["next_eligible_at"] == world.t + 24 * H


def test_a_grab_resets_the_backoff(world):
    (w, a), = make_assets(1)
    run_job(delay_seconds=0)
    world.t += 13 * H
    world.matches = {w}
    res = run_job(delay_seconds=0)
    assert res["grabbed"] == 1
    s = state(a)
    assert s["misses"] == 0 and s["next_eligible_at"] is None and s["last_outcome"] == "grabbed"


def test_manual_search_is_neither_blocked_nor_counted(world):
    (w, a), = make_assets(1)
    run_job(delay_seconds=0)
    before = state(a)
    r = client.post(f"/api/v1/library/{w}/search/audiobook")   # the real route, our fake indexer layer
    assert r.status_code == 200
    assert world.calls[-1] == (w, False)
    after = state(a)
    assert dict(after) == dict(before)                         # a manual search is not a miss
    assert wanted_search.searches_last_24h() == 1               # and does not eat the daily cap


def test_monitoring_change_and_work_edit_reset_the_cooldown(world):
    (w, a), = make_assets(1)
    run_job(delay_seconds=0)
    assert state(a)
    assert client.patch(f"/api/v1/assets/{a}", json={"monitored": True}).status_code == 200
    assert state(a) is None
    run_job(delay_seconds=0)
    assert state(a)
    assert client.patch(f"/api/v1/library/{w}", json={"title": "Edited title"}).status_code == 200
    assert state(a) is None
    run_job(delay_seconds=0)
    assert client.patch(f"/api/v1/library/{w}/monitor", json={"monitored": False}).status_code == 200
    assert state(a) is None


def test_unwanting_between_runs_drops_the_old_cooldown(world):
    (w, a), = make_assets(1)
    run_job(delay_seconds=0)
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET monitored=0,has_acquisition_intent=0 WHERE id=?", (a,))
    run_job(delay_seconds=0)
    assert state(a) is None


# ---- throttling and errors ------------------------------------------------------------

def test_a_429_stops_the_run_and_does_not_count_as_a_miss(world):
    assets = make_assets(5)
    world.errors[assets[1][0]] = [{"indexer": "p", "query": "q", "error": "Client error '429 Too Many Requests' for url 'http://p/api'"}]
    res = run_job(delay_seconds=0)
    assert res["stopped_reason"] == "throttled" and res["searched"] == 2
    assert res["error_count"] == 1 and res["no_result"] == 1
    s = state(assets[1][1])
    assert s["misses"] == 0 and s["next_eligible_at"] is None and s["last_outcome"] == "error"


def test_repeated_indexer_errors_stop_with_error_rate(world):
    assets = make_assets(12)
    for w, _ in assets:
        world.raises[w] = "connection refused"
    res = run_job(delay_seconds=0)
    assert res["stopped_reason"] == "error_rate" and res["searched"] == wanted_search.ERROR_STREAK_STOP
    assert len(res["errors"]) == 5 and res["errors"][0]["error"] == "connection refused"


def test_a_failing_asset_is_not_retried_within_the_same_run(world):
    assets = make_assets(3)
    world.raises[assets[0][0]] = "boom"
    res = run_job(delay_seconds=0)
    assert res["searched"] == 3 and res["stopped_reason"] == "drained"
    assert [w for w, _ in world.calls].count(assets[0][0]) == 1


# ---- crash safety, locking, payload ---------------------------------------------------

def test_a_crash_mid_run_leaves_finished_work_recorded_and_nothing_else(world, monkeypatch):
    assets = make_assets(4)
    real = world.search
    def exploding(work_id, kind, automatic=False):
        if work_id == assets[2][0]:
            raise KeyboardInterrupt()          # not an Exception: kills the run like a worker crash
        return real(work_id, kind, automatic)
    monkeypatch.setattr(scheduler.releases, "search_releases", exploding)
    with pytest.raises(KeyboardInterrupt):
        run_job(delay_seconds=0)
    assert state(assets[0][1]) and state(assets[1][1])
    assert state(assets[2][1]) is None and state(assets[3][1]) is None
    assert {r["status"] for r in db.all("SELECT status FROM media_assets WHERE id IN (%s)" % ",".join("'%s'" % a for _, a in assets))} == {"missing"}  # nosec B608
    monkeypatch.setattr(scheduler.releases, "search_releases", real)
    res = run_job(delay_seconds=0)
    assert res["searched"] == 2 and {w for w, _ in world.calls[-2:]} == {assets[2][0], assets[3][0]}


def test_no_double_run_and_the_manual_endpoint_obeys_single_flight(world):
    job_id = db.one("SELECT id FROM jobs WHERE kind='wanted-search'")["id"]
    make_assets(1)
    gate = threading.Event()
    release = threading.Event()
    real = world.search
    def blocking(work_id, kind, automatic=False):
        gate.set()
        release.wait(5)
        return real(work_id, kind, automatic)
    scheduler.releases.search_releases = blocking
    try:
        item = scheduler.submit_job(job_id, trigger="schedule")
        assert gate.wait(5)
        with pytest.raises(scheduler.JobBusy):
            scheduler.submit_job(job_id, trigger="schedule")
        assert client.post("/api/v1/wanted/search-now").status_code == 409
        release.set()
        item["future"].result(10)
    finally:
        release.set()
    assert client.post("/api/v1/wanted/search-now").status_code == 202


def test_job_result_payload_and_default_disabled(world):
    (w, a), *_ = assets = make_assets(3)
    world.matches = {w}
    job = db.one("SELECT * FROM jobs WHERE kind='wanted-search'")
    assert job["enabled"] == 0                     # ships disabled
    out = scheduler.run_job(job["id"], trigger="manual")
    res = out["result"]
    assert out["status"] == "success"
    assert {"eligible", "searched", "grabbed", "skipped_on_cooldown", "no_result", "errors", "stopped_reason", "batches", "elapsed"} <= set(res)
    assert (res["eligible"], res["searched"], res["grabbed"], res["no_result"], res["batches"], res["stopped_reason"]) == (3, 3, 1, 2, 1, "drained")
    stored = json.loads(db.one("SELECT result_json FROM job_runs WHERE id=?", (out["run_id"],))["result_json"])
    assert stored["stopped_reason"] == "drained" and stored["searched"] == 3


def test_score_floor_is_unchanged(world):
    (w, a), = make_assets(1)
    real = world.search
    def low(work_id, kind, automatic=False):
        r = real(work_id, kind, automatic)
        return {"items": [{"id": "x", "rejected": 0, "score": 79}], "errors": []}
    scheduler.releases.search_releases = low
    res = run_job(delay_seconds=0)
    assert res["grabbed"] == 0 and res["no_result"] == 1


# ---- settings, audit, wanted API --------------------------------------------------------

def test_settings_defaults_validation_audit():
    got = client.get("/api/v1/settings/acquisition").json()
    assert got == {"auto_grab_min_score": 80, "wanted_search_daily_cap": 300, "wanted_search_batch": 20,
                   "wanted_search_budget_minutes": 20, "wanted_search_cooldown_hours": 12, "wanted_search_delay_seconds": 1.0}
    r = client.put("/api/v1/settings/acquisition", json={"wanted_search_batch": 50, "wanted_search_daily_cap": 100})
    assert r.status_code == 200 and r.json()["wanted_search_batch"] == 50 and r.json()["auto_grab_min_score"] == 80
    assert wanted_search.get_settings()["wanted_search_daily_cap"] == 100
    for bad in ({"wanted_search_batch": 0}, {"wanted_search_batch": 101}, {"wanted_search_daily_cap": 0},
                {"wanted_search_budget_minutes": 31}, {"wanted_search_cooldown_hours": 0},
                {"wanted_search_delay_seconds": -1}, {"nope": 1}):
        assert client.put("/api/v1/settings/acquisition", json=bad).status_code == 422, bad
    assert wanted_search.get_settings()["wanted_search_batch"] == 50
    hist = db.all("SELECT message FROM history WHERE event_type='settings.acquisition'")
    assert len(hist) == 1 and "wanted_search_batch 20 -> 50" in hist[0]["message"]


def test_settings_drive_the_run_and_job_config_overrides(world):
    make_assets(5)
    db.set_setting("acquisition.wanted_search_batch", 2)
    db.set_setting("acquisition.wanted_search_delay_seconds", 0)
    assert wanted_search.run(None)["batches"] == 3
    with db.tx() as conn:
        conn.execute("DELETE FROM wanted_search_state")
    assert wanted_search.run({"config_json": json.dumps({"batch": 5})})["batches"] == 1


def test_wanted_api_shows_last_search_cooldown_and_not_found_filter(world):
    assets = make_assets(3)
    world.matches = set()
    run_job(delay_seconds=0, batch=1, budget_seconds=10)           # all three searched, all missed
    with db.tx() as conn:   # one asset was never searched
        conn.execute("DELETE FROM wanted_search_state WHERE asset_id=?", (assets[2][1],))
    items = {i["id"]: i for i in client.get("/api/v1/wanted?kind=audiobook").json()["items"]}
    a0, a2 = items[assets[0][1]], items[assets[2][1]]
    assert a0["cooling_down"] is True and a0["auto_misses"] == 1 and a0["next_eligible_at"] == world.t + 12 * H
    assert a0["last_auto_search_at"] and a2["last_auto_search_at"] is None and a2["cooling_down"] is False
    nf = client.get("/api/v1/wanted?kind=audiobook&not_found=1").json()
    assert nf["total"] == 2 and {i["id"] for i in nf["items"]} == {assets[0][1], assets[1][1]}
    assert client.get("/api/v1/wanted?kind=audiobook").json()["total"] == 3


def test_wanted_page_markup_has_the_filter_and_run_button():
    js = open(main_module.__file__.replace("main.py", "static/app.js")).read()
    for needle in ("wanted-notfound", "wanted-run-now", "cooling down until", "/api/v1/wanted/search-now", "ws-run"):
        assert needle in js
