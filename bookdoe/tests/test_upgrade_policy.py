"""P5-27 (Part XXIX): upgrade policy is an explicit per-asset opt-in, off by default.

An owned asset whose stored evaluation says 'upgrade_desired' is searched only when its
upgrade_policy is 'search', and the search never grabs, downloads or touches a file."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import helpers
from app import db, migrations
from app.main import app
from app.services import downloads, migration, scheduler, upgrades
from test_inbox_ui import needs_node, run_ui
from test_kids_access_control import household, local_mode  # noqa: F401  (fixtures)

client = TestClient(app)


@pytest.fixture
def indexer(monkeypatch):
    """One enabled indexer whose network call returns a release matching the work."""
    t = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO indexers(id,name,kind,base_url,enabled,created_at,updated_at) VALUES ('ix1','Test','torznab','http://ix.invalid',1,?,?)",
            (t, t),
        )
    searched: list[dict] = []

    def fake_search(indexer, plan):
        searched.append(plan)
        return [{"title": "Upgrade Me - Some Author [EPUB]", "guid": "g1", "download_url": "http://ix.invalid/1",
                 "size": 1000, "protocol": "torrent", "seeders": 5}]

    monkeypatch.setattr("app.services.releases.ix.search", fake_search)
    return searched


@pytest.fixture
def no_grab(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("grab_release must never run for an owned asset")
    monkeypatch.setattr(downloads, "grab_release", boom)


def _owned(title="Upgrade Me", state="upgrade_desired", policy="none") -> str:
    work_id = migration._find_or_create_stub({"title": title, "author": "Some Author", "identifiers": {}})
    asset_id = helpers.own(work_id, "ebook", "pdf")
    quality = json.dumps({"evaluation": {"state": state, "reasons": ["Format pdf is below the profile cutoff epub"], "evaluated_at": db.now()}})
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET quality_json=?,upgrade_policy=? WHERE id=?", (quality, policy, asset_id))
    return asset_id


def _policy(asset_id: str) -> str:
    return db.one("SELECT upgrade_policy FROM media_assets WHERE id=?", (asset_id,))["upgrade_policy"]


def _searches() -> int:
    return db.one("SELECT COUNT(*) n FROM release_searches")["n"]


# ---------- default and PATCH ----------
def test_new_assets_default_to_none():
    assert _policy(_owned()) == "none"


def test_patch_sets_policy_and_writes_history():
    a = _owned()
    r = client.patch(f"/api/v1/assets/{a}", json={"upgrade_policy": "search"})
    assert r.status_code == 200 and r.json()["upgrade_policy"] == "search"
    assert _policy(a) == "search"
    h = db.one("SELECT * FROM history WHERE asset_id=? AND event_type='asset.upgrade_policy'", (a,))
    assert json.loads(h["data_json"]) == {"from": "none", "to": "search"}


def test_patch_rejects_unknown_policy_and_leaves_row_alone():
    a = _owned()
    assert client.patch(f"/api/v1/assets/{a}", json={"upgrade_policy": "auto"}).status_code == 422
    assert _policy(a) == "none"


def test_patch_without_policy_keeps_it_and_writes_no_policy_history():
    a = _owned(policy="search")
    client.patch(f"/api/v1/assets/{a}", json={"monitored": True})
    assert _policy(a) == "search"
    assert db.one("SELECT COUNT(*) n FROM history WHERE event_type='asset.upgrade_policy'")["n"] == 0


# ---------- the job ----------
def test_policy_none_is_never_searched_and_has_no_candidates(indexer, no_grab):
    _owned(policy="none")
    result = upgrades.search_job()
    assert result["searched"] == 0 and indexer == [] and _searches() == 0
    assert client.get("/api/v1/upgrades").json() == {"items": [], "total": 0}


def test_flag_only_is_never_searched(indexer, no_grab):
    _owned(policy="flag_only")
    assert upgrades.search_job()["searched"] == 0 and indexer == []


def test_only_upgrade_desired_owned_assets_are_searched(indexer, no_grab):
    _owned("Satisfied", state="satisfactory", policy="search")
    _owned("Never Evaluated", state="unevaluated", policy="search")
    wanted = _owned("Not Owned", policy="search")
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='missing' WHERE id=?", (wanted,))
    assert upgrades.search_job()["searched"] == 0 and indexer == []


def test_policy_search_is_searched_and_candidates_listed_without_grab(indexer, no_grab):
    a = _owned(policy="search")
    row = db.one("SELECT * FROM media_assets WHERE id=?", (a,))
    downloads_before = db.one("SELECT COUNT(*) n FROM downloads")["n"]
    result = scheduler._execute({"kind": "upgrade-search"})
    assert result["searched"] == 1 and result["with_candidates"] == 1 and result["asset_ids"] == [a] and result["errors"] == []
    assert db.one("SELECT automatic FROM release_searches")["automatic"] == 1

    items = client.get("/api/v1/upgrades").json()["items"]
    assert [i["asset_id"] for i in items] == [a]
    assert items[0]["releases"][0]["raw_title"].startswith("Upgrade Me")
    # nothing was grabbed and the owned asset is unchanged
    assert db.one("SELECT COUNT(*) n FROM downloads")["n"] == downloads_before
    after = db.one("SELECT * FROM media_assets WHERE id=?", (a,))
    assert (after["status"], after["format"]) == (row["status"], row["format"])


def test_withdrawing_the_opt_in_hides_earlier_candidates(indexer, no_grab):
    a = _owned(policy="search")
    upgrades.search_job()
    assert client.get("/api/v1/upgrades").json()["total"] == 1
    client.patch(f"/api/v1/assets/{a}", json={"upgrade_policy": "none"})
    assert client.get("/api/v1/upgrades").json()["total"] == 0


def test_search_job_is_bounded_and_rotates(indexer, no_grab):
    ids = [_owned(f"Book {i}", policy="search") for i in range(upgrades.SEARCH_LIMIT + 2)]
    first = upgrades.search_job()
    assert first["searched"] == upgrades.SEARCH_LIMIT
    second = upgrades.search_job()
    assert set(second["asset_ids"]) >= set(ids) - set(first["asset_ids"])


def test_upgrade_module_does_not_reference_grab_release():
    import inspect
    assert "grab_release" not in inspect.getsource(upgrades)


def test_upgrade_search_job_is_seeded_but_off_by_default():
    job = db.one("SELECT * FROM jobs WHERE kind='upgrade-search'")
    assert job["enabled"] == 0


def test_kids_cannot_read_upgrades_or_set_policy(household):
    kid, csrf = household["kid"], {"X-CSRF-Token": household["kid_csrf"]}
    a = _owned(policy="search")
    assert kid.get("/api/v1/upgrades").status_code == 403
    assert kid.patch(f"/api/v1/assets/{a}", json={"upgrade_policy": "none"}, headers=csrf).status_code == 403
    assert _policy(a) == "search"


# ---------- wanted-search regression ----------
def test_wanted_search_still_grabs_missing_assets_and_ignores_owned(monkeypatch):
    work_id = migration._find_or_create_stub({"title": "Missing Book", "author": "Some Author", "identifiers": {}})
    helpers.want(work_id, "ebook")
    owned = _owned("Owned Desired", policy="search")
    searched, grabbed = [], []
    monkeypatch.setattr(scheduler.releases, "search_releases",
                        lambda w, k, automatic=False: searched.append(w) or {"items": [{"id": "r1", "rejected": False, "score": 95, "title": "Missing Book by Some Author [EPUB]"}]})
    monkeypatch.setattr(downloads, "grab_release", lambda rid, *a, **k: grabbed.append(rid))
    result = scheduler._wanted_search()
    assert searched == [work_id] and grabbed == ["r1"] and result["grabbed"] == 1
    assert owned not in result["asset_ids"]


# ---------- migration 28 ----------
def test_migration_28_adds_column_with_default_and_is_idempotent():
    from test_migrations_framework import _baseline_schema, _wipe, snapshot
    _wipe()
    with db.session() as conn:
        _baseline_schema(conn)
        # A database from before P5-27: no upgrade_policy column.
        conn.execute("ALTER TABLE media_assets DROP COLUMN upgrade_policy")
        t = db.now()
        conn.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES ('w','w','w','resolved',?,?)", (t, t))
        conn.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES ('a','w','ebook','owned',?,?)", (t, t))
        conn.commit()
        assert "upgrade_policy" not in migrations._columns(conn, "media_assets")
    db.init_db()
    with db.session() as conn:
        assert conn.execute("SELECT upgrade_policy FROM media_assets WHERE id='a'").fetchone()[0] == "none"
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=28").fetchone()[0])
    assert counts == {"columns_added": 1, "already_applied": 0}

    before = snapshot()
    db.init_db()
    assert snapshot() == before


def test_migration_28_is_already_applied_on_a_fresh_database():
    from test_migrations_framework import _wipe
    _wipe()
    db.init_db()
    with db.session() as conn:
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=28").fetchone()[0])
    assert counts == {"columns_added": 0, "already_applied": 1}


# ---------- badges (real app.js in node) ----------
BADGES = """
state.me = {access_scope: %s};
const mk = (st, q, p) => ({id:'a1', kind:'ebook', format:'epub', status:st, monitored:0, quality_state:q, upgrade_policy:p, files:[]});
const w = {id:'w1', progress:[]};
const full = %s;
return {
  satisfied: assetRow(mk('owned','satisfactory','none'), w, full),
  desired: assetRow(mk('owned','upgrade_desired','search'), w, full),
  unevaluated: assetRow(mk('owned','unevaluated','flag_only'), w, full),
  missing: assetRow(mk('missing','unevaluated','none'), w, full),
  table: assetStatus({assets:[mk('owned','upgrade_desired','search')]}, 'ebook'),
};
"""


@needs_node
def test_badges_render_for_full_access():
    out = run_ui(BADGES % ("'full'", "true"))
    assert 'badge quality-badge good" data-quality="satisfactory">Satisfactory' in out["satisfied"]
    assert 'badge quality-badge warn" data-quality="upgrade_desired">Upgrade desired' in out["desired"]
    assert 'badge quality-badge " data-quality="unevaluated">Unevaluated' in out["unevaluated"]
    assert 'data-policy="search"' in out["desired"] and 'data-policy="flag_only"' in out["unevaluated"]
    assert '<select class="asset-policy" data-asset="a1"' in out["desired"] and '<option value="search" selected>' in out["desired"]
    assert "Satisfactory" not in out["missing"] and "asset-policy" not in out["missing"]
    assert "Upgrade desired" in out["table"] and "policy-chip" in out["table"]


@needs_node
def test_kids_scope_sees_state_only():
    out = run_ui(BADGES % ("'kids'", "false"))
    assert "Upgrade desired" in out["desired"] and "Satisfactory" in out["satisfied"]
    for html in (out["desired"], out["unevaluated"], out["table"]):
        assert "policy-chip" not in html and "asset-policy" not in html
