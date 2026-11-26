"""series.reparse_all / undo_last_cleanup on a seeded database, the admin API around them,
the gap-count fixes, and the import paths that must use the same parser."""
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db, main, migrations
from app.config import settings as base_settings
from app.services import localmeta, metadata
from app.services import series as series_service

import helpers

ANETAKIS_1 = "The Anetakis Tycoons - 1, Bestselling Author Collection - 45, Mills & Boon Desire 2-in-1 - 322, Silhouette Desire"
ANETAKIS_2 = "The Anetakis Tycoons - 2, Bestselling Author Collection - 46, Mills & Boon Desire 2-in-1 - 330, Silhouette Desire"
TABLES = ("series", "series_memberships", "series_assertions", "series_monitors")


def _work(title, source=None):
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,added_at,updated_at,metadata_source) VALUES (?,?,?,?,?,?)",
                     (wid, title, title, t, t, source))
    return wid


def _series(name):
    sid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,sort_name,norm_key,created_at,updated_at) VALUES (?,?,?,NULL,?,?)", (sid, name, name, t, t))
    return sid


def _member(work, series, position, raw=None, status="auto_accepted"):
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work, series, position))
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,proposed_position,
               evidence_type,confidence,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.uid(), work, series, "openreads", raw or "x", "x", position, "free_text", 0.8, status, t, t))


def _snapshot(*tables):
    return {t: sorted(json.dumps(r, sort_keys=True, default=str) for r in db.all(f"SELECT * FROM {t}")) for t in tables}  # nosec B608


def _names():
    return sorted(((r["name"], r["position"], r["title"]) for r in db.all(
        "SELECT s.name,sm.position,w.title FROM series_memberships sm JOIN series s ON s.id=sm.series_id JOIN works w ON w.id=sm.work_id")),
        key=lambda t: tuple(str(x) for x in t))


@pytest.fixture
def seeded():
    """A library shaped like the production profile: junk names, compounds, catalog numbers,
    a followed junk-named series, a hand-edited position and a hand-edited work."""
    ids = {}
    ids["manwhore_junk"] = s = _series("[Manwhore")
    ids["w_man1"] = w = _work("Manwhore 1")
    _member(w, s, None, "[Manwhore")
    ids["manwhore"] = s2 = _series("Manwhore")
    ids["w_man2"] = w = _work("Manwhore 2")
    _member(w, s2, "2", "Manwhore #2")
    ids["a1"] = _series(ANETAKIS_1)
    ids["w_a1"] = w = _work("Anetakis One")
    _member(w, ids["a1"], "1920", ANETAKIS_1)
    ids["a2"] = _series(ANETAKIS_2)
    ids["w_a2"] = w = _work("Anetakis Two")
    _member(w, ids["a2"], "2", ANETAKIS_2)
    ids["knaur"] = s = _series("Knaur")
    ids["w_knaur"] = w = _work("Knaur Book")
    _member(w, s, "60291", "Knaur")
    ids["cocktail"] = s = _series("Cocktail #3)")
    ids["w_cock"] = w = _work("Cocktail Book")
    _member(w, s, "3", "Cocktail #3)")
    ids["vera"] = s = _series("Vera Stanhope ermittelt, 8; rororo")
    ids["w_vera"] = w = _work("Vera Book")
    _member(w, s, "27598", "Vera Stanhope ermittelt, 8; rororo")
    ids["real"] = s = _series("The Expanse")
    for i in (1, 2, 3):
        w = _work(f"Expanse {i}")
        _member(w, s, str(i), "The Expanse")
    ids["hand"] = s = _series("Hand Made")
    ids["w_hand"] = w = _work("Hand Book")
    _member(w, s, "7", "Hand Made")
    with db.tx() as conn:  # a person typed position 42: no assertion ever proposed it
        conn.execute("UPDATE series_memberships SET position='42' WHERE work_id=?", (w,))
    ids["manual_work"] = s = _series("[Curated")
    ids["w_manual"] = w = _work("Curated Book", source="manual")
    with db.tx() as conn:
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (w, s, "9"))
    helpers.own(ids["w_man1"])
    helpers.want(ids["w_a1"], "audiobook")
    with db.tx() as conn:
        conn.execute("INSERT INTO series_monitors(series_id,policy,ebook,audiobook,started_at,audiobook_policy,ebook_policy) VALUES (?,?,?,?,?,?,?)",
                     (ids["manwhore_junk"], "all", 1, 1, 1.0, "all", "none"))
        conn.execute("INSERT INTO series_monitors(series_id,policy,ebook,audiobook,started_at,audiobook_policy,ebook_policy) VALUES (?,?,?,?,?,?,?)",
                     (ids["a1"], "future", 0, 1, 2.0, "future", "none"))
    return ids


def test_dry_run_writes_nothing_and_reports_every_category(seeded):
    before = _snapshot(*TABLES, "works", "media_assets", "series_cleanup_runs", "series_cleanup_changes")
    plan = series_service.reparse_all(dry_run=True)
    assert _snapshot(*TABLES, "works", "media_assets", "series_cleanup_runs", "series_cleanup_changes") == before
    assert plan["dry_run"] is True
    c = {k: v["count"] for k, v in plan["categories"].items()}
    assert c["drop_junk"] == 1                      # Knaur
    assert c["merge"] >= 2                          # [Manwhore -> Manwhore, Anetakis 2 -> Anetakis 1's name
    assert c["clear_position"] == 2                 # 1920 and 27598 (60291 goes with its dropped membership)
    assert c["rename"] >= 2                         # Cocktail #3), Anetakis
    assert c["delete_empty_series"] >= 3
    assert c["monitor_repointed"] >= 1              # [Manwhore's followed row goes to Manwhore
    assert plan["totals"]["series_before"] == 10 and plan["totals"]["series_after"] < 10
    sample = plan["categories"]["drop_junk"]["samples"][0]
    assert sample["before"]["series"] == "Knaur" and sample["before"]["position"] == "60291" and sample["after"] is None


def test_samples_are_capped_at_fifty_per_category_but_counts_are_exact():
    s = _series("Foo")
    for i in range(60):
        w = _work(f"junk {i}")
        j = _series(f"[Junk{i}")
        _member(w, j, None, f"[Junk{i}")
    plan = series_service.reparse_all(dry_run=True)
    assert plan["categories"]["rename"]["count"] == 60
    assert len(plan["categories"]["rename"]["samples"]) == 50


def test_apply_fixes_names_positions_and_splits_compounds(seeded):
    works_before = _snapshot("works", "media_assets")
    result = series_service.reparse_all(dry_run=False, actor="ada")
    assert result["dry_run"] is False and result["run_id"]
    got = _names()
    assert ("Manwhore", None, "Manwhore 1") in got and ("Manwhore", "2", "Manwhore 2") in got
    assert ("The Anetakis Tycoons", "1", "Anetakis One") in got       # 1920 was a Silhouette line number
    assert ("The Anetakis Tycoons", "2", "Anetakis Two") in got
    assert ("Cocktail", "3", "Cocktail Book") in got
    assert ("Vera Stanhope ermittelt", "8", "Vera Book") in got        # 27598 replaced by the real 8
    assert not [n for n in got if n[2] == "Knaur Book"]               # publisher line: not a series
    assert ("Hand Made", "42", "Hand Book") in got                    # a position nobody proposed stays
    assert ("[Curated", "9", "Curated Book") in got                   # hand-edited work untouched
    names = {r["name"] for r in db.all("SELECT name FROM series")}
    assert not any(n.startswith("[") and n != "[Curated" for n in names) and "Knaur" not in names
    assert "The Expanse" in names
    assert db.one("SELECT COUNT(*) n FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE s.name=?", ("The Anetakis Tycoons",))["n"] == 2
    # the rejected pieces left a trace instead of vanishing
    rejected = {r["normalized_name"] for r in db.all("SELECT normalized_name FROM series_assertions WHERE status='rejected' AND series_id IS NULL")}
    assert {"Bestselling Author Collection", "Silhouette Desire", "rororo", "Knaur"} <= rejected
    # works and assets are never touched
    assert _snapshot("works", "media_assets") == works_before
    # and a second run finds nothing left to do
    again = series_service.reparse_all(dry_run=True)
    assert {k: v["count"] for k, v in again["categories"].items() if v["count"]} == {"skipped_manual": 1}


def test_monitors_survive_and_follow_the_surviving_series(seeded):
    series_service.reparse_all(dry_run=False)
    mons = {r["series_id"]: r for r in db.all("SELECT * FROM series_monitors")}
    assert len(mons) == 2
    man = db.one("SELECT id FROM series WHERE name='Manwhore'")["id"]
    ana = db.one("SELECT id FROM series WHERE name='The Anetakis Tycoons'")["id"]
    assert mons[man]["audiobook_policy"] == "all" and mons[ana]["audiobook_policy"] == "future"
    assert db.one("SELECT COUNT(*) n FROM series WHERE id NOT IN (SELECT series_id FROM series_monitors) AND name='Manwhore'")["n"] == 0
    # a merge where both rows were followed keeps the stronger policy per kind
    s_a, s_b = _series("[Merge Me"), _series("Merge Me")
    for s, pol in ((s_a, "all"), (s_b, "none")):
        w = _work(f"m {s_a}{s}")
        _member(w, s, None)
        with db.tx() as conn:
            conn.execute("INSERT INTO series_monitors(series_id,policy,ebook,audiobook,started_at,audiobook_policy,ebook_policy) VALUES (?,?,?,?,?,?,?)",
                         (s, pol, 0, 1, 1.0, pol, "none"))
    series_service.reparse_all(dry_run=False)
    kept = db.all("SELECT m.audiobook_policy FROM series_monitors m JOIN series s ON s.id=m.series_id WHERE s.name='Merge Me'")
    assert [r["audiobook_policy"] for r in kept] == ["all"]


def test_a_followed_series_with_nowhere_to_go_is_kept(seeded):
    with db.tx() as conn:
        conn.execute("INSERT INTO series_monitors(series_id,policy,ebook,audiobook,started_at,audiobook_policy,ebook_policy) VALUES (?,?,?,?,?,?,?)",
                     (seeded["knaur"], "all", 1, 1, 1.0, "all", "all"))
    series_service.reparse_all(dry_run=False)
    assert db.one("SELECT 1 FROM series WHERE id=?", (seeded["knaur"],))
    assert db.one("SELECT 1 FROM series_monitors WHERE series_id=?", (seeded["knaur"],))


def test_undo_restores_every_touched_row_exactly(seeded):
    before = _snapshot(*TABLES, "works", "media_assets")
    series_service.reparse_all(dry_run=False)
    assert _snapshot(*TABLES) != {t: before[t] for t in TABLES}
    assert series_service.last_cleanup() is not None
    out = series_service.undo_last_cleanup()
    assert out["skipped"] == 0 and out["restored"] > 0
    assert _snapshot(*TABLES, "works", "media_assets") == before
    assert series_service.last_cleanup() is None
    with pytest.raises(ValueError):
        series_service.undo_last_cleanup()
    # the cleanup can be run again after an undo
    series_service.reparse_all(dry_run=False)
    assert series_service.last_cleanup() is not None


def test_apply_is_one_transaction(seeded, monkeypatch):
    before = _snapshot(*TABLES, "series_cleanup_runs", "series_cleanup_changes")
    calls = {"n": 0}
    real = series_service.reparse_all.__globals__["_repoint_monitor"]

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(series_service.reparse_all.__globals__, "_repoint_monitor", boom)
    with pytest.raises(RuntimeError):
        series_service.reparse_all(dry_run=False)
    assert calls["n"] == 1
    assert _snapshot(*TABLES, "series_cleanup_runs", "series_cleanup_changes") == before


def test_manual_assertion_status_protects_a_membership():
    s = _series("[Pinned")
    w = _work("Pinned Book")
    _member(w, s, "5", "[Pinned", status="manually_confirmed")
    plan = series_service.reparse_all(dry_run=True)
    assert plan["categories"]["skipped_manual"]["count"] == 1 and plan["categories"]["rename"]["count"] == 0
    series_service.reparse_all(dry_run=False)
    assert _names() == [("[Pinned", "5", "Pinned Book")]


def test_merge_into_a_work_that_is_already_a_member_does_not_duplicate():
    a, b = _series("[Dup"), _series("Dup")
    w = _work("Both")
    _member(w, a, None, "[Dup")
    _member(w, b, "4", "Dup #4")
    series_service.reparse_all(dry_run=False)
    assert _names() == [("Dup", "4", "Both")]


def test_migration_37_is_idempotent_and_additive():
    with db.tx() as conn:
        assert migrations._migrate_series_cleanup_journal(conn)["already_applied"] == 1
        assert migrations._migrate_series_cleanup_journal(conn)["already_applied"] == 1
    cols = {r["name"] for r in db.all("PRAGMA table_info(series_cleanup_changes)")}
    assert {"run_id", "table_name", "op", "row_key", "before_json"} <= cols
    assert db.one("SELECT 1 FROM migration_steps WHERE version=37")


# ---- gap counts --------------------------------------------------------------------------

def test_gaps_ignore_catalog_numbers_years_and_short_series():
    g = series_service._gaps_from_positions
    assert g(["1", "2", "4"]) == [3]
    assert g(["1", "60291"]) == []                     # one sane position: no numbering to have gaps in
    assert g(["1", "2", "3", "1920", "60291"]) == []
    assert g(["1", "3", "27598"]) == [2]
    assert g(["1", None, "x"]) == []
    assert g(["2", "2"]) == []
    assert g(["1", "250"]) == []                       # 250 is over the default cap: not a position
    assert len(g(["1", "200"])) == 198                 # the plausible range is the ceiling
    assert g(["1", "250"], max_position=300) == list(range(2, 250))
    assert g(["1.5", "3"]) == []


def test_series_list_and_detail_gaps_use_only_sane_positions():
    s = _series("Gappy")
    for i, pos in enumerate(["1", "2", "4", "60291", "1920"]):
        _member(_work(f"G{i}"), s, pos, "Gappy")
    t = _series("Bogus")
    for i, pos in enumerate(["1", "60291"]):
        _member(_work(f"B{i}"), t, pos, "Bogus")
    with TestClient(main.app) as c:
        items = {r["name"]: r for r in c.get("/api/v1/series", params={"include_uncertain": True}).json()["items"]}
        detail = c.get(f"/api/v1/series/{s}").json()
    assert items["Gappy"]["gap_count"] == 1 and items["Gappy"]["work_count"] == 5
    assert items["Bogus"]["gap_count"] == 0
    assert detail["gaps"] == [3]


# ---- import paths use the same parser -------------------------------------------------------

def _meta(mid, title, series):
    return {"id": mid, "title": title, "contributors": [{"id": "a1", "name": "Author A", "role": "author"}],
            "series": series, "identifiers": [], "editions": []}


def test_openreads_compound_series_is_split_on_import():
    metadata.upsert_work(_meta("c1", "Anetakis One", [{"id": "or-1", "name": ANETAKIS_1, "position": "1920"}]))
    metadata.upsert_work(_meta("c2", "Anetakis Two", [{"id": "or-2", "name": ANETAKIS_2, "position": "2"}]))
    metadata.upsert_work(_meta("c3", "Mist", [{"id": "or-3", "name": "Mistborn, Era 2: Wax & Wayne (#3), The Mistborn Saga (#6)"}]))
    metadata.upsert_work(_meta("c4", "Vera", [{"id": "or-4", "name": "Vera Stanhope ermittelt, 8; rororo", "position": "27598"}]))
    metadata.upsert_work(_meta("c5", "Brk", [{"id": "or-5", "name": "[Manwhore"}, {"id": "or-6", "name": "Cocktail #3)"}]))
    got = _names()
    assert ("The Anetakis Tycoons", "1", "Anetakis One") in got and ("The Anetakis Tycoons", "2", "Anetakis Two") in got
    assert ("Mistborn, Era 2: Wax & Wayne", "3", "Mist") in got and ("The Mistborn Saga", "6", "Mist") in got
    assert ("Vera Stanhope ermittelt", "8", "Vera") in got
    assert ("Manwhore", None, "Brk") in got and ("Cocktail", "3", "Brk") in got
    all_names = {r["name"] for r in db.all("SELECT name FROM series")}
    assert not any("Silhouette" in n or "Collection" in n or "rororo" in n for n in all_names)
    # rejected pieces are recorded with the full raw value
    rej = db.all("SELECT raw_value,normalized_name FROM series_assertions WHERE status='rejected' AND series_id IS NULL")
    assert (ANETAKIS_1, "Silhouette Desire") in {(r["raw_value"], r["normalized_name"]) for r in rej}


def test_openreads_position_that_is_a_catalog_number_is_dropped():
    metadata.upsert_work(_meta("p1", "Knaur Thing", [{"id": "or-9", "name": "Dune Chronicles", "position": "60291"}]))
    assert _names() == [("Dune Chronicles", None, "Knaur Thing")]
    row = db.one("SELECT proposed_position FROM series_assertions WHERE normalized_name='Dune Chronicles'")
    assert row["proposed_position"] is None


def test_ignored_names_setting_blocks_new_imports():
    db.set_setting("series.ignored_names", ["Fantasy Klassiker"])
    metadata.upsert_work(_meta("i1", "Eins", [{"id": "or-i", "name": "Fantasy Klassiker", "position": "3"}]))
    assert _names() == []
    assert db.one("SELECT 1 FROM series_assertions WHERE normalized_name='Fantasy Klassiker' AND status='rejected'")


def test_max_position_setting_raises_the_cap():
    db.set_setting("series.max_position", 300)
    metadata.upsert_work(_meta("m1", "Long", [{"id": "or-m", "name": "Endless Saga", "position": "250"}]))
    assert _names() == [("Endless Saga", "250", "Long")]


def test_file_tag_series_goes_through_the_parser_too():
    w = _work("Tagged")
    out = localmeta._attach_series(w, "Knaur; Dune Chronicles, 2", None)
    assert out == [("Dune Chronicles", "2")]
    assert _names() == [("Dune Chronicles", "2", "Tagged")]
    assert db.one("SELECT 1 FROM series_assertions WHERE source='local' AND status='rejected' AND normalized_name='Knaur'")


# ---- admin API ------------------------------------------------------------------------------

@pytest.fixture
def household(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)
    with TestClient(main.app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        h = {"X-CSRF-Token": r.json()["csrf"]}
        clients = {}
        for name, kind in (("jane", "adult"), ("owen", "kid")):
            assert admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=h).status_code == 200
            c = TestClient(main.app)
            lr = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            clients[name] = (c, {"X-CSRF-Token": lr.json()["csrf"]})
        yield admin, h, clients


def test_cleanup_api_is_admin_only_csrf_protected_and_audited(household, seeded):
    admin, h, clients = household
    for name in ("jane", "owen"):
        c, ch = clients[name]
        assert c.get("/api/v1/admin/series/cleanup").status_code == 403
        assert c.post("/api/v1/admin/series/cleanup", json={"confirm": True}, headers=ch).status_code == 403
        assert c.post("/api/v1/admin/series/cleanup/undo", headers=ch).status_code == 403
        assert c.get("/api/v1/admin/series/settings").status_code == 403
        assert c.put("/api/v1/admin/series/settings", json={"ignored_names": ["x"]}, headers=ch).status_code == 403
    assert admin.post("/api/v1/admin/series/cleanup", json={"confirm": True}).status_code == 403   # no CSRF token
    assert admin.post("/api/v1/admin/series/cleanup/undo").status_code == 403
    assert admin.put("/api/v1/admin/series/settings", json={"ignored_names": ["x"]}).status_code == 403

    r = admin.get("/api/v1/admin/series/cleanup")
    assert r.status_code == 200 and r.json()["plan"]["dry_run"] is True and r.json()["last"] is None
    assert db.one("SELECT COUNT(*) n FROM series_cleanup_runs")["n"] == 0

    assert admin.post("/api/v1/admin/series/cleanup", json={}, headers=h).status_code == 400        # needs confirm
    assert db.one("SELECT COUNT(*) n FROM series_cleanup_runs")["n"] == 0
    r = admin.post("/api/v1/admin/series/cleanup", json={"confirm": True}, headers=h)
    assert r.status_code == 200 and r.json()["last"]["id"] == r.json()["plan"]["run_id"]
    assert db.one("SELECT 1 FROM history WHERE event_type='series.cleanup'")
    r = admin.post("/api/v1/admin/series/cleanup/undo", headers=h)
    assert r.status_code == 200 and r.json()["last"] is None and r.json()["undone"]["restored"] > 0
    assert db.one("SELECT 1 FROM history WHERE event_type='series.cleanup_undo'")
    assert admin.post("/api/v1/admin/series/cleanup/undo", headers=h).status_code == 404


def test_series_settings_api_round_trips_and_is_audited(household):
    admin, h, _ = household
    r = admin.put("/api/v1/admin/series/settings", json={"ignored_names": ["  Fantasy   Klassiker ", "fantasy klassiker", "Bastei*"], "max_position": 250}, headers=h)
    assert r.status_code == 200
    assert r.json() == {"ignored_names": ["Fantasy Klassiker", "Bastei*"], "max_position": 250}
    assert admin.get("/api/v1/admin/series/settings").json() == r.json()
    assert db.get_setting("series.ignored_names") == ["Fantasy Klassiker", "Bastei*"]
    assert db.one("SELECT 1 FROM history WHERE event_type='settings.series'")
    assert admin.put("/api/v1/admin/series/settings", json={"max_position": 0}, headers=h).status_code == 422
    assert admin.put("/api/v1/admin/series/settings", json={"bogus": 1}, headers=h).status_code == 422
