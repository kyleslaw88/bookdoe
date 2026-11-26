"""Audience metadata for the kids classifier: normalization, storage, the gate that reads it,
the resumable backfill job, the admin routes and the migration.

The provider payloads are what OpenReads returns for the recorded Google Books / Open Library
responses in openreads/tests/fixtures/audience (juvenile, young adult, mature, ambiguous).
Nothing here touches the network; the classifier-path tests make any outbound call fail loudly.
"""
import dataclasses
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db, migrations
from app import main as main_module
from app.config import settings as base_settings
from app.services import audience, kidsgate, kidsrecheck, metadata, provider_results, scheduler, tagging

import helpers
from test_kids_evidence_flow import legacy, llm, old_auto  # noqa: F401  (fixtures)
from test_kids_gate import BLURB_ADVENTURE, MG_BLURB, book, classify, gate, ids

app = main_module.app
T0 = 1_700_000_000.0


# ---------------------------------------------------------------- provider payloads (OpenReads shape)
def payload(google_categories=None, maturity=None, subjects=None, extra=None):
    out = []
    if google_categories is not None:
        out.append({"provider": "google_books", "field": "categories", "value": google_categories, "observed_at": T0})
    if maturity:
        out.append({"provider": "google_books", "field": "maturity_rating", "value": maturity, "observed_at": T0})
    if subjects is not None:
        out.append({"provider": "openlibrary", "field": "subjects", "value": subjects, "observed_at": T0})
    return out + (extra or [])


JUVENILE = payload(["Juvenile Fiction / Action & Adventure / General"], "NOT_MATURE", ["Juvenile fiction", "Lighthouses", "Open Library Staff Picks"])
YOUNG_ADULT = payload(["Young Adult Fiction / Fantasy / General"], "NOT_MATURE", ["Young adult fiction", "Fantasy fiction", "Queens"])
MATURE = payload(["Fiction / Romance / Erotica"], "MATURE", ["Erotica", "Romance", "Billionaires"])
AMBIGUOUS = payload(["Fiction / General"], "NOT_MATURE", ["Fiction", "Road trips", "Accessible book"])
DISAGREE = payload(["Juvenile Fiction / General"], "MATURE", ["Juvenile fiction"])  # Google says mature, both say juvenile


def with_signals(wid, items):
    audience.store(wid, items)
    return wid


def linked_book(title, mid=None, added=None, own=False, **kw):
    wid = book(title, (f"Author of {title}",), **kw)
    with db.tx() as c:
        c.execute("UPDATE works SET metadata_id=?,added_at=? WHERE id=?", (mid or f"md-{title}", added or db.now(), wid))
    if own:
        helpers.own(wid)
    return wid


# ---------------------------------------------------------------- normalization
@pytest.mark.parametrize("kind,value,norm", [
    ("category", "Juvenile Fiction / Animals / General", "juvenile"),
    ("category", "Juvenile Nonfiction / Science & Nature", "juvenile"),
    ("category", "Young Adult Fiction / Fantasy / General", "young_adult"),
    ("category", "Fiction / Romance / Erotica", "adult_genre"),
    ("category", "Fiction / Thrillers / Suspense", "adult_genre"),
    ("category", "Fiction / Fantasy / LitRPG", "adult_genre"),
    ("category", "Juvenile Fiction / Horror & Ghost Stories", "adult_genre"),   # hard adult beats juvenile
    ("category", "Fiction / General", "neutral"),
    ("subject", "Juvenile literature", "juvenile"),
    ("subject", "Children's stories", "juvenile"),
    ("subject", "Picture books for children", "juvenile"),
    ("subject", "Middle grade fiction", "juvenile"),
    ("subject", "Young adult fiction", "young_adult"),
    ("subject", "Romance", "adult_genre"),
    ("subject", "Adult fiction", "adult_audience"),
    ("subject", "Accessible book", "neutral"),
    ("maturity", "MATURE", "mature"),
    ("maturity", "NOT_MATURE", "neutral"),
    ("age_range", "8-12", "juvenile"),
    ("age_range", "Ages 9 to 12", "juvenile"),
    ("age_range", "12+", "neutral"),
    ("age_range", "14-18", "neutral"),
    ("publisher", "Scholastic", "neutral"),       # not an audience kind
])
def test_normalize_term_table(kind, value, norm):
    assert kidsgate.normalize_term(kind, value) == norm


# ---------------------------------------------------------------- storage
def test_store_writes_observations_with_provider_source_and_normalized_rows():
    wid = book("Lighthouse", publisher=None)
    n = audience.store(wid, JUVENILE + [{"provider": "google_books", "field": "publisher", "value": "Scholastic Inc.", "observed_at": T0}])
    rows = {(r["provider"], r["kind"], r["value"]): r for r in audience.signals_for_work(wid)}
    assert n == len(rows) == 6
    assert rows[("google_books", "category", "Juvenile Fiction / Action & Adventure / General")]["norm"] == "juvenile"
    assert rows[("google_books", "maturity", "NOT_MATURE")]["norm"] == "neutral"
    assert rows[("openlibrary", "subject", "Juvenile fiction")]["fetched_at"] == T0
    obs = db.all("SELECT field_name,value,source,observed_at FROM metadata_observations WHERE entity_id=? AND source IN ('google_books','openlibrary')", (wid,))
    by = {(o["source"], o["field_name"]): o for o in obs}
    assert json.loads(by[("openlibrary", "subjects")]["value"]) == ["Juvenile fiction", "Lighthouses", "Open Library Staff Picks"]
    assert by[("google_books", "maturity_rating")]["value"] == "NOT_MATURE" and by[("google_books", "maturity_rating")]["observed_at"] == T0


def test_store_is_idempotent_and_replaces_a_providers_changed_answer():
    wid = book("Lighthouse")
    audience.store(wid, JUVENILE)
    first = (audience.signals_for_work(wid), db.one("SELECT COUNT(*) n FROM metadata_observations")["n"])
    audience.store(wid, JUVENILE)
    assert (audience.signals_for_work(wid), db.one("SELECT COUNT(*) n FROM metadata_observations")["n"]) == first
    audience.store(wid, payload(["Fiction / General"], "NOT_MATURE", ["Fiction"]))
    kinds = {(r["provider"], r["kind"], r["value"]) for r in audience.signals_for_work(wid)}
    assert ("google_books", "category", "Fiction / General") in kinds
    assert ("google_books", "category", "Juvenile Fiction / Action & Adventure / General") not in kinds
    assert not any(k[0] == "openlibrary" and k[2] == "Lighthouses" for k in kinds)


def test_store_ignores_unknown_works_and_fields():
    assert audience.store("no-such-work", JUVENILE) == 0
    wid = book("X")
    assert audience.store(wid, [{"provider": "google_books", "field": "bogus", "value": "x"}, {"provider": "", "field": "categories", "value": ["a"]}]) == 0


def test_metadata_sync_stores_audience_that_openreads_returns(monkeypatch):
    meta = {"id": "mid-1", "title": "The Lighthouse Map", "contributors": [], "identifiers": [], "series": [], "editions": [], "audience": JUVENILE}
    wid = metadata.upsert_work(meta)
    assert {r["kind"] for r in audience.signals_for_work(wid)} >= {"category", "subject", "maturity"}
    metadata.upsert_work({**meta, "audience": None})     # a payload without audience leaves what is stored
    assert audience.signals_for_work(wid)


# ---------------------------------------------------------------- the gate reads it
def test_juvenile_category_from_a_provider_is_a_strong_signal_with_its_provider_named():
    wid = with_signals(book("Lighthouse Map", description=MG_BLURB), JUVENILE)   # no children's publisher at all
    g = gate(wid)
    assert g["passed"] is True and ids(g["strong_signals"]) == {"juvenile_category"}
    assert any("[google_books]" in s["detail"] for s in g["strong_signals"]) and any("[openlibrary]" in s["detail"] for s in g["strong_signals"])


def test_without_stored_metadata_the_same_book_is_not_approvable():
    g = gate(book("Lighthouse Map", description=MG_BLURB))
    assert g["passed"] is False and g["blockers"] == ["no_strong_audience_signal"]


def test_young_adult_is_neither_strong_nor_hard():
    g = gate(with_signals(book("Crown of Ash", description=MG_BLURB), YOUNG_ADULT))
    assert g["strong_signals"] == [] and g["adult_signals"] == [] and g["blockers"] == ["no_strong_audience_signal"]


def test_mature_book_is_blocked_by_hard_signals():
    g = gate(with_signals(book("Midnight Contract", description=MG_BLURB, publisher="Scholastic"), MATURE))
    assert "hard_adult_signals" in g["blockers"] and {"maturity_flag", "adult_genre"} <= ids(g["adult_signals"])


def test_ambiguous_metadata_is_not_enough():
    g = gate(with_signals(book("Long Way", description=MG_BLURB), AMBIGUOUS))
    assert g["passed"] is False and g["strong_signals"] == [] and g["adult_signals"] == []


def test_provider_disagreement_hard_adult_wins_and_strong_signals_are_set_aside():
    """One provider says MATURE while the categories say Juvenile Fiction (and the publisher is a kids' house)."""
    wid = with_signals(book("Contested", description=MG_BLURB, publisher="Scholastic"), DISAGREE)
    g = gate(wid)
    assert g["passed"] is False
    assert g["strong_signals"] == [], "strong signals count only when no provider says mature"
    assert {"hard_adult_signals", "no_strong_audience_signal"} <= set(g["blockers"])
    assert ids(g["provider_conflict"]) == {"juvenile_category", "children_publisher"}
    assert any(r.startswith("Providers disagree") for r in g["reasons"])
    assert "[google_books]" in next(s["detail"] for s in g["adult_signals"] if s["id"] == "maturity_flag")


def test_adult_category_from_one_provider_beats_a_juvenile_one_from_another():
    wid = with_signals(book("Split", description=MG_BLURB), payload(["Juvenile Fiction / General"], "NOT_MATURE", ["Thrillers"]))
    g = gate(wid)
    assert g["passed"] is False and "hard_adult_signals" in g["blockers"] and ids(g["adult_signals"]) == {"adult_genre"}


def test_provider_age_range_up_to_twelve_is_strong():
    wid = with_signals(book("Ages", description=MG_BLURB), [{"provider": "openlibrary", "field": "age_range", "value": "8-12", "observed_at": T0}])
    assert gate(wid)["passed"] is True


def test_evidence_and_prompt_carry_the_stored_fields(llm):
    wid = with_signals(book("Lighthouse Map", description=MG_BLURB), JUVENILE)
    ev = kidsgate.gather_evidence(wid)
    assert ev["categories"] == ["Juvenile Fiction / Action & Adventure / General"] and ev["maturity"] == ["NOT_MATURE"]
    assert ev["subjects"][0] == "Juvenile fiction"
    prompt = kidsgate.render_prompt(ev)
    assert "Categories: Juvenile Fiction" in prompt and "Audience metadata from: google_books (category, maturity); openlibrary (subject)" in prompt
    assert "Not on record" not in prompt or "categories/subjects/genres" not in prompt.split("Not on record")[-1]


def no_network(monkeypatch):
    """Any outbound HTTP or socket use fails the test."""
    def boom(*a, **k):
        raise AssertionError("the classifier path made a network call")
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(metadata, "_client", boom)
    monkeypatch.setattr(metadata, "fetch_audience", boom)
    import socket
    monkeypatch.setattr(socket.socket, "connect", boom)


def test_classifier_path_reads_stored_data_only_and_auto_approves(llm, monkeypatch):
    wid = with_signals(book("Lighthouse Map", description=MG_BLURB), JUVENILE)
    no_network(monkeypatch)
    r = tagging.classify_kids_for_work(wid)
    assert r["auto_approved"] is True and len(llm["_calls"]) == 1
    assert "Categories: Juvenile Fiction" in llm["_calls"][0]


def test_classifier_with_a_mature_provider_flag_never_asks_the_model_to_approve(llm, monkeypatch):
    wid = with_signals(book("Contested", description=MG_BLURB, publisher="Scholastic"), DISAGREE)
    no_network(monkeypatch)
    r = tagging.classify_kids_for_work(wid)
    assert not r.get("auto_approved")
    assert tagging.kids_decision(wid) is None or tagging.kids_decision(wid)["via"] != "auto_approved"


def test_recheck_dry_run_uses_the_new_signals_and_reports_coverage(legacy, monkeypatch):
    no_network(monkeypatch)
    before = kidsrecheck.recheck_auto_approved(dry_run=True)
    assert (before["pass"], before["fail"], before["with_audience_metadata"]) == (1, 4, 0)
    audience.store(legacy["no_signal"], JUVENILE)          # was: no strong audience signal
    audience.store(legacy["passes"], DISAGREE)             # was passing; a provider now says mature
    after = kidsrecheck.recheck_auto_approved(dry_run=True)
    assert (after["pass"], after["fail"], after["with_audience_metadata"]) == (1, 4, 2)
    assert after["audience_coverage"]["with_audience"] == 2
    assert db.one("SELECT COUNT(*) n FROM review_tasks WHERE kind='kids_recheck'")["n"] == 0, "dry run writes nothing"


# ---------------------------------------------------------------- the backfill job
@pytest.fixture
def fake_openreads(monkeypatch):
    """audience.backfill's only outbound call, served from a dict: metadata_id -> payload, or an exception."""
    calls, answers = [], {}

    def fetch(mid):
        calls.append(mid)
        a = answers.get(mid, {"audience": JUVENILE, "complete": True, "status": {"openlibrary": "ok", "google_books": "ok"}})
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(metadata, "fetch_audience", fetch)
    monkeypatch.setattr(audience, "_sleep", lambda s: None)
    answers["_calls"] = calls
    return answers


def http_error(code):
    req = httpx.Request("POST", "http://openreads/api/v2/works/x/audience")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def test_backfill_is_newest_first_owned_first_and_resumes_without_repeats(fake_openreads):
    old = linked_book("Old", added=T0 - 300, own=False)
    mid = linked_book("Mid", added=T0 - 200)
    new = linked_book("New", added=T0 - 100)
    owned_old = linked_book("OwnedOld", added=T0 - 900, own=True)
    r1 = audience.backfill(limit=2)
    assert fake_openreads["_calls"] == ["md-OwnedOld", "md-New"] and r1["stored"] == 2 and r1["remaining"] == 2
    r2 = audience.backfill(limit=2)
    assert fake_openreads["_calls"][2:] == ["md-Mid", "md-Old"] and r2["remaining"] == 0
    r3 = audience.backfill(limit=2)
    assert r3["considered"] == 0 and len(fake_openreads["_calls"]) == 4, "a finished work is never asked again"
    assert all(audience.signals_for_work(w) for w in (old, mid, new, owned_old))
    cov = audience.coverage()
    assert (cov["total_works"], cov["with_audience"], cov["checked"], cov["pending"]) == (4, 4, 4, 0)


def test_backfill_skips_works_without_a_metadata_link_and_counts_them(fake_openreads):
    book("Unlinked")
    linked_book("Linked")
    audience.backfill(limit=10)
    assert fake_openreads["_calls"] == ["md-Linked"]
    cov = audience.coverage()
    assert cov["unlinked"] == 1 and cov["total_works"] == 2 and cov["with_audience"] == 1


def test_backfill_honours_the_wall_clock_budget(fake_openreads, monkeypatch):
    for i in range(5):
        linked_book(f"B{i}", added=T0 - i)
    clock = {"t": 0.0}
    monkeypatch.setattr(audience, "_monotonic", lambda: clock["t"])
    orig = metadata.fetch_audience

    def slow(mid):
        clock["t"] += 6.0
        return orig(mid)
    monkeypatch.setattr(metadata, "fetch_audience", slow)
    r = audience.backfill(limit=5, budget_seconds=10)
    assert r["stored"] == 2 and r["budget_stopped"] == 3 and r["stopped"] == "budget" and r["remaining"] == 3


def test_backfill_batch_is_capped_and_paced(fake_openreads, monkeypatch):
    for i in range(3):
        linked_book(f"C{i}", added=T0 - i)
    pauses = []
    monkeypatch.setattr(audience, "_sleep", pauses.append)
    audience.backfill(limit=10_000, pause=0.7)
    assert pauses == [0.7, 0.7], "paused between works, not before the first"
    assert len(fake_openreads["_calls"]) == 3


def test_backfill_stops_on_throttling_and_does_not_hammer(fake_openreads, monkeypatch):
    first = linked_book("First", added=T0)
    linked_book("Second", added=T0 - 1)
    fake_openreads["md-First"] = http_error(429)
    r = audience.backfill(limit=5)
    assert r["stopped"] == "rate_limited" and r["errors"] == 1 and fake_openreads["_calls"] == ["md-First"]
    assert provider_results.retry_at(audience.PROVIDER, "work", first), "a retry is scheduled"
    assert db.one("SELECT 1 FROM work_audience_status WHERE work_id=?", (first,)) is None, "throttled is not an answer"
    fake_openreads["_calls"].clear()
    fake_openreads["md-First"] = {"audience": JUVENILE, "complete": True, "status": {}}
    r = audience.backfill(limit=5)                       # First is deferred until its retry time; Second goes ahead
    assert fake_openreads["_calls"] == ["md-Second"] and r["deferred"] == 1


def test_backfill_gives_up_after_two_consecutive_unavailable_answers(fake_openreads):
    for i in range(4):
        linked_book(f"D{i}", added=T0 - i)
    for i in range(4):
        fake_openreads[f"md-D{i}"] = http_error(503)
    r = audience.backfill(limit=4)
    assert r["stopped"] == "provider_unavailable" and len(fake_openreads["_calls"]) == 2


def test_an_incomplete_answer_is_retried_later_then_closed(fake_openreads, monkeypatch):
    wid = linked_book("Partial")
    clock = {"t": T0}
    monkeypatch.setattr(db, "now", lambda: clock["t"])
    fake_openreads["md-Partial"] = {"audience": payload(None, None, ["Fiction"]), "complete": False, "status": {"google_books": "deferred"}}
    audience.backfill(limit=5)
    st = db.one("SELECT * FROM work_audience_status WHERE work_id=?", (wid,))
    assert st["complete"] == 0 and st["attempts"] == 1 and audience.coverage()["pending"] == 1
    audience.backfill(limit=5)
    assert len(fake_openreads["_calls"]) == 1, "not retried before it is due"
    for _ in range(audience.MAX_ATTEMPTS - 1):
        clock["t"] += audience.RETRY_AFTER_SECONDS + 1
        audience.backfill(limit=5)
    st = db.one("SELECT * FROM work_audience_status WHERE work_id=?", (wid,))
    assert st["complete"] == 1 and st["attempts"] == audience.MAX_ATTEMPTS
    assert len(fake_openreads["_calls"]) == audience.MAX_ATTEMPTS


def test_a_work_openreads_does_not_know_is_closed_not_retried(fake_openreads):
    wid = linked_book("Gone")
    fake_openreads["md-Gone"] = http_error(404)
    audience.backfill(limit=5)
    audience.backfill(limit=5)
    assert fake_openreads["_calls"] == ["md-Gone"] and db.one("SELECT complete FROM work_audience_status WHERE work_id=?", (wid,))["complete"] == 1


def test_job_is_registered_and_runs_on_the_scheduler_without_blocking_it(fake_openreads, monkeypatch):
    """The kind is seeded, dispatched by _execute, single-flight, and a slow run does not stop tick()."""
    linked_book("Slow")
    job = db.one("SELECT * FROM jobs WHERE kind='audience-backfill'")
    assert job and job["schedule_seconds"] == 600 and job["enabled"] == 1
    entered, release = threading.Event(), threading.Event()
    real = metadata.fetch_audience

    def blocking(mid):
        entered.set()
        release.wait(10)
        return real(mid)
    monkeypatch.setattr(metadata, "fetch_audience", blocking)
    db.execute("UPDATE jobs SET enabled=0")
    db.execute("UPDATE jobs SET enabled=1,next_run_at=NULL WHERE kind='audience-backfill'")
    try:
        started = scheduler.tick()                            # returns at once with the run still going
        assert [x["status"] for x in started if x["kind"] == "audience-backfill"] == ["running"]
        assert entered.wait(5)
        assert scheduler.heartbeat() is not None
        again = scheduler.tick()                              # next tick: not due / never queued twice
        assert all(x["status"] != "running" for x in again if x["kind"] == "audience-backfill")
        with pytest.raises(scheduler.JobBusy):
            scheduler.submit_job(job["id"], trigger="manual")
        assert [r["kind"] for r in scheduler.running_jobs()] == ["audience-backfill"]
    finally:
        release.set()
        scheduler.shutdown_pool(wait=True)
    run = db.one("SELECT status,result_json FROM job_runs WHERE kind='audience-backfill'")
    assert run["status"] == "success" and json.loads(run["result_json"])["stored"] == 1


def test_job_config_overrides_batch(fake_openreads):
    for i in range(4):
        linked_book(f"E{i}", added=T0 - i)
    r = audience.backfill_job({"config_json": json.dumps({"batch": 2})})
    assert r["stored"] == 2
    assert audience.backfill_job({"config_json": "not json"})["stored"] == 2    # default batch = settings.audience_batch (6) covers the rest
    assert audience.coverage()["pending"] == 0


# ---------------------------------------------------------------- routes and permissions
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
        scheduler.shutdown_pool(wait=True)


def test_run_backfill_now_is_admin_csrf_audited_and_single_flight(household, fake_openreads, monkeypatch):
    admin, ah, others = household
    linked_book("Routed")
    url = "/api/v1/tags/audience-backfill"
    assert TestClient(app).post(url, json={}).status_code in (401, 403)           # anonymous
    for name in ("owen", "jane"):
        c, h = others[name]
        assert c.post(url, json={}, headers=h).status_code == 403                  # kid, adult
    assert admin.post(url, json={}).status_code == 403                             # admin without CSRF
    assert fake_openreads["_calls"] == []

    entered, release = threading.Event(), threading.Event()
    real = metadata.fetch_audience

    def blocking(mid):
        entered.set()
        release.wait(10)
        return real(mid)
    monkeypatch.setattr(metadata, "fetch_audience", blocking)
    r = admin.post(url, json={}, headers=ah)
    assert r.status_code == 200 and r.json()["started"] is True, "answered while the batch is still running"
    assert entered.wait(5)
    assert admin.post(url, json={}, headers=ah).status_code == 409
    h = admin.get("/api/v1/system/health").json()
    assert h["audience_backfill"]["running"] is True and h["audience_backfill"]["total_works"] == 1
    release.set()
    scheduler.shutdown_pool(wait=True)
    assert admin.get("/api/v1/system/health").json()["audience_backfill"]["with_audience"] == 1
    ev = db.one("SELECT message,data_json FROM history WHERE event_type='audience.backfill_started'")
    assert ev and "ada" in ev["message"]


def test_settings_report_coverage_to_admins_only(household):
    admin, ah, others = household
    wid = linked_book("Covered")
    book("Bare")
    audience.store(wid, JUVENILE)
    cov = admin.get("/api/v1/settings/kids").json()["audience_coverage"]
    assert (cov["with_audience"], cov["total_works"]) == (1, 2)
    for name in ("owen", "jane"):
        assert others[name][0].get("/api/v1/settings/kids").status_code == 403


def test_book_page_shows_stored_categories_and_maturity_with_provider(household, llm):
    admin, ah, others = household
    wid = with_signals(book("Contested", description=MG_BLURB, publisher="Scholastic"), DISAGREE)
    helpers.own(wid)
    tagging.classify_kids_for_work(wid)
    k = admin.get(f"/api/v1/library/{wid}").json()["kids_evidence"]
    seen = {(m["provider"], m["kind"], m["value"]) for m in k["audience_metadata"]}
    assert ("google_books", "maturity", "MATURE") in seen and ("openlibrary", "subject", "Juvenile fiction") in seen
    assert k["provider_conflict"] and k["gate"]["passed"] is False
    kid, _ = others["owen"]
    assert "kids_evidence" not in kid.get(f"/api/v1/library/{wid}").json()


# ---------------------------------------------------------------- migration
def _tables():
    with db.session() as conn:
        return {r["name"]: "".join(r["sql"].split()) for r in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE '%work_audience%' AND sql IS NOT NULL")}


def _counts():
    with db.session() as conn:
        return json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=31").fetchone()[0])


def test_audience_migration_is_additive_and_idempotent():
    assert 31 in [v for v, _, _ in migrations.MIGRATIONS]
    fresh = _tables()
    assert {"work_audience_signals", "work_audience_status", "idx_work_audience_signals_kind", "idx_work_audience_status_pending"} <= set(fresh)
    assert _counts() == {"tables_created": 2, "already_applied": 0}
    wid = book("Keep me")
    audience.store(wid, JUVENILE, status={"ok": 1})
    before = audience.signals_for_work(wid), db.one("SELECT COUNT(*) n FROM works")["n"]
    # pre-change database: tables absent, step unrecorded -> the step recreates them with the same DDL
    with db.session() as conn:
        conn.execute("DROP TABLE work_audience_signals")
        conn.execute("DROP TABLE work_audience_status")
        conn.execute("DELETE FROM migration_steps WHERE version=31")
        conn.commit()
    assert migrations.run_pending_migrations()
    assert _counts() == {"tables_created": 2, "already_applied": 0} and _tables() == fresh
    # already-applied: tables present, step row missing -> nothing changes, nothing lost
    audience.store(wid, JUVENILE, status={"ok": 1})
    with db.session() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=31")
        conn.commit()
    assert migrations.run_pending_migrations()
    assert _counts() == {"tables_created": 0, "already_applied": 1} and _tables() == fresh
    assert (audience.signals_for_work(wid), db.one("SELECT COUNT(*) n FROM works")["n"]) == before
