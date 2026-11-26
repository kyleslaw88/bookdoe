"""P4-05: metadata field API - choose, manual, restore-automatic, lock, unlock (Part XXI)."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import metadata

client = TestClient(app)


def _meta(**over):
    m = {"id": "or-fields", "title": "Field Book", "contributors": [], "series": [], "identifiers": [], "editions": []}
    m.update(over)
    return m


def _url(wid, field, action=""):
    return f"/api/v1/library/{wid}/metadata/{field}" + (f"/{action}" if action else "")


def _desc(wid):
    return db.one("SELECT description FROM works WHERE id=?", (wid,))["description"]


def _events(wid):
    return [h["event_type"] for h in db.all("SELECT event_type FROM history WHERE work_id=? ORDER BY created_at,rowid", (wid,))]


def _two_provider_work():
    wid = metadata.upsert_work(_meta(description="first"))
    metadata.upsert_work(_meta(description="second"))
    return wid


def test_get_lists_value_source_observations_and_lock():
    wid = _two_provider_work()
    body = client.get(_url(wid, "description")).json()
    assert body["value"] == "second" and body["source"] == "provider" and body["locked"] is False
    assert [o["value"] for o in body["observations"]] == ["second", "first"]
    assert [o["is_current"] for o in body["observations"]] == [True, False]


def test_unknown_work_or_field_is_404():
    wid = _two_provider_work()
    assert client.get(_url("nope", "description")).status_code == 404
    assert client.get(_url(wid, "bogus")).status_code == 404


def test_choose_flips_current_and_updates_column():
    wid = _two_provider_work()
    first = [o for o in client.get(_url(wid, "description")).json()["observations"] if o["value"] == "first"][0]
    r = client.post(_url(wid, "description", "choose"), json={"observation_id": first["id"]})
    assert r.status_code == 200, r.text
    assert _desc(wid) == "first"
    assert r.json()["current_observation_id"] == first["id"]
    assert [o["is_current"] for o in r.json()["observations"]].count(True) == 1
    assert "metadata.field_chosen" in _events(wid)


def test_choose_rejects_foreign_observation():
    wid = _two_provider_work()
    other = metadata.upsert_work(_meta(id="or-other", description="x"))
    foreign = client.get(_url(other, "description")).json()["observations"][0]["id"]
    assert client.post(_url(wid, "description", "choose"), json={"observation_id": foreign}).status_code == 404
    assert _desc(wid) == "second"


def test_choosing_title_updates_sort_title():
    wid = metadata.upsert_work(_meta(title="The Alpha"))
    metadata.upsert_work(_meta(title="Beta"))
    old = [o for o in client.get(_url(wid, "title")).json()["observations"] if o["value"] == "The Alpha"][0]
    client.post(_url(wid, "title", "choose"), json={"observation_id": old["id"]})
    row = db.one("SELECT title,sort_title FROM works WHERE id=?", (wid,))
    assert (row["title"], row["sort_title"]) == ("The Alpha", "Alpha, The")


def test_manual_applies_records_locks_and_survives_ingest():
    wid = _two_provider_work()
    r = client.post(_url(wid, "description", "manual"), json={"value": "mine"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == "mine" and body["source"] == "manual" and body["locked"] is True
    assert _desc(wid) == "mine"
    # provider ingest and local harvest paths must not overwrite it
    metadata.upsert_work(_meta(description="provider again"))
    assert _desc(wid) == "mine"
    after = client.get(_url(wid, "description")).json()
    assert after["value"] == "mine" and after["source"] == "manual"
    assert "provider again" in [o["value"] for o in after["observations"]]
    assert "metadata.field_manual" in _events(wid)


def test_manual_title_cannot_be_empty():
    wid = _two_provider_work()
    assert client.post(_url(wid, "title", "manual"), json={"value": "  "}).status_code == 400


def test_restore_automatic_unlocks_and_picks_best_non_manual():
    wid = _two_provider_work()
    # a lower-confidence, newer local observation competes with the provider ones
    with db.tx() as conn:
        from app.services import observations
        observations.record(conn, "work", wid, "description", "from file", "local", applied=False, t=db.now() + 10)
    client.post(_url(wid, "description", "manual"), json={"value": "mine"})
    r = client.post(_url(wid, "description", "restore-automatic"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] is False
    assert body["value"] == "second" and body["source"] == "provider"
    assert _desc(wid) == "second"
    assert "metadata.field_restored" in _events(wid)
    # unlocked again, so the next refresh can change it
    metadata.upsert_work(_meta(description="third"))
    assert _desc(wid) == "third"


def test_restore_without_automatic_observation_keeps_value():
    wid = _two_provider_work()
    with db.tx() as conn:
        conn.execute("DELETE FROM metadata_observations WHERE entity_id=? AND field_name='subtitle'", (wid,))
    client.post(_url(wid, "subtitle", "manual"), json={"value": "Mine"})
    body = client.post(_url(wid, "subtitle", "restore-automatic")).json()
    assert body["locked"] is False and body["value"] == "Mine"


def test_lock_and_unlock_never_change_value():
    wid = _two_provider_work()
    r = client.post(_url(wid, "description", "lock"))
    assert r.status_code == 200 and r.json()["locked"] is True
    assert _desc(wid) == "second"
    metadata.upsert_work(_meta(description="ignored"))
    assert _desc(wid) == "second"
    r = client.post(_url(wid, "description", "unlock"))
    assert r.json()["locked"] is False and _desc(wid) == "second"
    assert client.post(_url(wid, "description", "unlock")).status_code == 200  # idempotent
    events = _events(wid)
    assert "metadata.field_locked" in events and "metadata.field_unlocked" in events


def test_history_event_shape():
    wid = _two_provider_work()
    client.post(_url(wid, "description", "lock"))
    h = [h for h in db.all("SELECT event_type,data_json FROM history WHERE work_id=?", (wid,)) if h["event_type"] == "metadata.field_locked"][0]
    assert db.json_load(h["data_json"], {}) == {"entity_type": "work", "field": "description", "action": "locked"}
