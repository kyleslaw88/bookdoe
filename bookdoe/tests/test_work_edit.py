"""Manual metadata correction - the terminal step when no provider has this book."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration, scheduler, metadata

client = TestClient(app)


def _work(title="Old Title", author="Old Author"):
    return migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})


def test_editing_scalar_fields_sets_metadata_source_manual():
    work_id = _work()
    r = client.patch(f"/api/v1/library/{work_id}", json={
        "title": "New Title", "subtitle": "A Subtitle", "description": "A new description.",
        "first_publish_date": "2020", "primary_language": "en",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "New Title"
    assert body["description"] == "A new description."
    assert body["metadata_source"] == "manual"
    assert body["metadata_status"] == "resolved"


def test_empty_string_clears_an_optional_field():
    work_id = _work()
    client.patch(f"/api/v1/library/{work_id}", json={"subtitle": "Has one"})
    r = client.patch(f"/api/v1/library/{work_id}", json={"subtitle": ""})
    assert r.json()["subtitle"] == ""


def test_omitted_fields_are_left_untouched():
    work_id = _work()
    client.patch(f"/api/v1/library/{work_id}", json={"description": "Keep me"})
    r = client.patch(f"/api/v1/library/{work_id}", json={"title": "Renamed Only"})
    assert r.json()["description"] == "Keep me"
    assert r.json()["title"] == "Renamed Only"


def test_empty_title_is_rejected():
    work_id = _work()
    r = client.patch(f"/api/v1/library/{work_id}", json={"title": ""})
    assert r.status_code == 422


def test_authors_are_replaced_and_link_to_existing_contributors():
    work_id = _work()
    with db.tx() as conn:
        cid = db.contributor_id_for_name(conn, "Existing Author")
    r = client.patch(f"/api/v1/library/{work_id}", json={"authors": ["Existing Author", "Brand New Author"]})
    assert r.status_code == 200
    names = {a["name"] for a in r.json()["authors"]}
    assert names == {"Existing Author", "Brand New Author"}
    # The existing contributor row was reused, not duplicated.
    dupes = db.all("SELECT id FROM contributors WHERE name='Existing Author' COLLATE NOCASE")
    assert len(dupes) == 1 and dupes[0]["id"] == cid


def test_series_are_replaced_and_reuse_existing_by_name():
    work_id = _work()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,'Existing Series','Existing Series',?,?)", (db.uid(), db.now(), db.now()))
    r = client.patch(f"/api/v1/library/{work_id}", json={"series": [{"name": "Existing Series", "position": "3"}]})
    assert r.status_code == 200
    assert r.json()["series"] == [{"id": db.one("SELECT id FROM series WHERE name='Existing Series'")["id"], "name": "Existing Series", "position": "3"}]
    assert db.one("SELECT COUNT(*) n FROM series WHERE name='Existing Series'")["n"] == 1


def test_editing_a_missing_work_is_404():
    r = client.patch("/api/v1/library/does-not-exist", json={"title": "X"})
    assert r.status_code == 404


def test_a_manually_edited_work_is_excluded_from_the_automatic_resync(monkeypatch):
    """The scheduled metadata-refresh job must never silently overwrite a correction
    an operator typed in by hand - upsert_work() replaces title unconditionally."""
    work_id = _work()
    client.patch(f"/api/v1/library/{work_id}", json={"title": "Manually Fixed Title"})

    calls = []
    monkeypatch.setattr(metadata, "refresh_work", lambda wid, **k: calls.append(wid) or {"status": "unmatched"})
    scheduler._metadata_refresh()
    assert work_id not in calls


def test_a_manually_edited_LINKED_work_is_excluded_from_automatic_resync(monkeypatch):
    work_id = _work()
    with db.tx() as conn:
        conn.execute("UPDATE works SET metadata_id='or-123' WHERE id=?", (work_id,))
    client.patch(f"/api/v1/library/{work_id}", json={"title": "Manually Fixed Title"})

    calls = []
    monkeypatch.setattr(metadata, "sync_work", lambda mid: calls.append(mid))
    scheduler._metadata_refresh()
    assert "or-123" not in calls
