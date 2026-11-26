"""Knowing about a book is not wanting it.

The bug this locks down: every Work that entered the catalog got an ebook *and* an
audiobook asset at status='missing', monitored=1, which is the same state as "the
user asked for this". 13,975 assets in the live library were in that state, so the
Wanted list, the dashboard counters and the automatic search job all treated the
entire discovered catalog as a download queue.

Discovery now creates an inert slot (status='none', monitored=0, no intent) and
acquisition intent is recorded only where something deliberately asks for a medium:
the per-asset toggle, an author Follow policy, a Series monitor policy, or wanted
state carried in from the Readarr migration.
"""
from fastapi.testclient import TestClient

import helpers

from app import db
from app.main import app
from app.services import metadata, migration, monitoring, scheduler, series

client = TestClient(app)


def _discovered(title: str = "Discovered Book", author: str = "Some Author") -> str:
    """A work that the catalog knows about and nobody has asked for."""
    work_id = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    with db.tx() as conn:
        conn.execute("DELETE FROM media_assets WHERE work_id=?", (work_id,))
    metadata._ensure_assets(work_id)
    return work_id


def _assets(work_id: str) -> dict:
    return {a["kind"]: a for a in db.all("SELECT * FROM media_assets WHERE work_id=?", (work_id,))}


# ---------- discovery creates inert slots ----------
def test_discovery_creates_slots_that_nothing_is_asking_for():
    assets = _assets(_discovered())
    assert set(assets) == {"ebook", "audiobook"}
    for kind, a in assets.items():
        assert a["status"] == "none", kind
        assert a["monitored"] == 0, kind
        assert a["has_acquisition_intent"] == 0, kind


def test_discovery_does_not_create_a_second_slot_for_a_kind():
    work_id = _discovered()
    helpers.own(work_id, "ebook")
    metadata._ensure_assets(work_id)
    n = db.one("SELECT COUNT(*) n FROM media_assets WHERE work_id=? AND kind='ebook'", (work_id,))["n"]
    assert n == 1


def test_a_discovered_work_is_not_in_the_wanted_list():
    _discovered()
    assert client.get("/api/v1/wanted?kind=ebook").json()["items"] == []


def test_a_discovered_work_is_not_in_the_library():
    work_id = _discovered()
    assert work_id not in {i["id"] for i in client.get("/api/v1/library").json()["items"]}


def test_an_owned_work_is_in_the_library():
    work_id = _discovered()
    helpers.own(work_id)
    assert work_id in {i["id"] for i in client.get("/api/v1/library").json()["items"]}


# ---------- the per-asset toggle ----------
def test_toggling_an_asset_on_records_intent_and_makes_it_wanted():
    work_id = _discovered()
    asset_id = _assets(work_id)["ebook"]["id"]

    r = client.patch(f"/api/v1/assets/{asset_id}", json={"monitored": True})
    assert r.status_code == 200, r.text
    assert r.json()["has_acquisition_intent"] == 1
    assert r.json()["status"] == "missing"

    assert work_id in {i["work_id"] for i in client.get("/api/v1/wanted?kind=ebook").json()["items"]}


def test_toggling_an_asset_back_off_withdraws_it_from_wanted():
    work_id = _discovered()
    asset_id = _assets(work_id)["ebook"]["id"]
    client.patch(f"/api/v1/assets/{asset_id}", json={"monitored": True})

    r = client.patch(f"/api/v1/assets/{asset_id}", json={"monitored": False})
    assert r.json()["has_acquisition_intent"] == 0
    assert r.json()["status"] == "none"
    assert client.get("/api/v1/wanted?kind=ebook").json()["items"] == []


def test_unwanting_an_owned_asset_does_not_disown_it():
    work_id = _discovered()
    asset_id = helpers.own(work_id, "ebook")
    r = client.patch(f"/api/v1/assets/{asset_id}", json={"monitored": False})
    assert r.json()["status"] == "owned"


# ---------- automation that legitimately expresses intent ----------
def test_a_follow_policy_records_intent_for_the_media_it_selected():
    work_id = _discovered()
    monitoring._apply_asset_monitoring(work_id, {"ebook": True, "audiobook": False}, True)
    assets = _assets(work_id)
    assert (assets["ebook"]["has_acquisition_intent"], assets["ebook"]["status"]) == (1, "missing")
    assert (assets["audiobook"]["has_acquisition_intent"], assets["audiobook"]["status"]) == (0, "none")


def test_a_series_monitor_policy_records_intent_for_its_members():
    work_id = _discovered()
    sid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, "Expeditionary Force", "expeditionary force", t, t))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work_id, sid, "1"))

    series.set_series_monitor(sid, policy="all", ebook=False, audiobook=True)
    assets = _assets(work_id)
    assert (assets["audiobook"]["has_acquisition_intent"], assets["audiobook"]["status"]) == (1, "missing")
    assert (assets["ebook"]["has_acquisition_intent"], assets["ebook"]["status"]) == (0, "none")


def test_wanted_state_imported_from_readarr_is_kept():
    """Existing user data: a book Readarr was already hunting stays wanted."""
    work_id = migration._find_or_create_stub({"title": "Legacy Wanted", "author": "Craig Alanson", "identifiers": {}})
    with db.tx() as conn:
        conn.execute("DELETE FROM media_assets WHERE work_id=?", (work_id,))
    asset_id = migration._asset_for_migration(work_id, "ebook", True)
    a = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    assert (a["status"], a["monitored"], a["has_acquisition_intent"]) == ("missing", 1, 1)
    assert work_id in {i["work_id"] for i in client.get("/api/v1/wanted?kind=ebook").json()["items"]}


# ---------- everything that consumes "wanted" agrees ----------
def test_the_automatic_search_job_leaves_discovered_books_alone(monkeypatch):
    """The consequential one: the scheduler, not the Wanted page, spends bandwidth."""
    calls = []
    monkeypatch.setattr(
        scheduler.releases, "search_releases",
        lambda work_id, kind, automatic=False: calls.append((work_id, kind)) or {"items": []},
    )

    discovered = _discovered("Nobody Asked For This")
    scheduler._wanted_search()
    assert calls == [], "auto-search must not hunt for merely discovered books"

    helpers.want(discovered, "ebook")
    scheduler._wanted_search()
    assert (discovered, "ebook") in calls


def test_the_dashboard_wanted_count_matches_the_wanted_list():
    _discovered("Only Discovered")
    helpers.want(_discovered("Actually Wanted", "Another Author"), "ebook")

    counts = client.get("/api/v1/dashboard").json()["counts"]
    assert counts["wanted_ebooks"] == client.get("/api/v1/wanted?kind=ebook").json()["total"] == 1


# ---------- the migration that resolves the inherited contradiction ----------
def test_startup_demotes_wanted_rows_that_nothing_asked_for():
    """The 13,975 rows the Readarr migration left at 'missing' with no intent.

    With the versioned migration framework, the backfill only runs once on first
    startup.  This test simulates a production database that has the schema but has
    not yet run the migration: it deletes the migration step record so the
    migration framework will re-apply the backfill.
    """
    work_id = _discovered()
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='missing',monitored=1,has_acquisition_intent=0 WHERE work_id=?", (work_id,))

    # Simulate a DB that has the schema but hasn't run migration v3 yet
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=3")

    db.init_db()

    for kind, a in _assets(work_id).items():
        assert (a["status"], a["monitored"]) == ("none", 0), kind
    assert client.get("/api/v1/wanted?kind=ebook").json()["items"] == []


def test_startup_leaves_genuinely_wanted_rows_alone():
    """Genuinely wanted rows (has_acquisition_intent=1) survive init_db."""
    work_id = _discovered()
    helpers.want(work_id, "ebook")
    # Re-run migration v3 so the backfill logic executes
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=3")
    db.init_db()
    a = _assets(work_id)["ebook"]
    assert (a["status"], a["monitored"], a["has_acquisition_intent"]) == ("missing", 1, 1)


def test_startup_never_touches_an_owned_asset():
    """Owned assets are never demoted by the migration."""
    work_id = _discovered()
    helpers.own(work_id, "ebook")
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=3")
    db.init_db()
    assert _assets(work_id)["ebook"]["status"] == "owned"


def test_an_asset_inserted_without_a_status_is_not_wanted():
    """The column defaults themselves must describe a slot nobody asked for."""
    work_id = _discovered()
    with db.tx() as conn:
        conn.execute("DELETE FROM media_assets WHERE work_id=?", (work_id,))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,added_at,updated_at) VALUES (?,?,'ebook',?,?)",
            (db.uid(), work_id, db.now(), db.now()),
        )
    a = _assets(work_id)["ebook"]
    assert (a["status"], a["monitored"], a["has_acquisition_intent"]) == ("none", 0, 0)
