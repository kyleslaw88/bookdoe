"""Wanted list author metadata, used to group a page of 8,000 rows on the frontend."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import migration

client = TestClient(app)


def _wanted_ebook(title, author):
    work_id = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at)"
            " VALUES (?,?,'ebook','missing',1,1,?,?)",
            (aid, work_id, t, t),
        )
    return work_id


def test_wanted_items_include_the_author_name():
    _wanted_ebook("Book One", "Jane Author")
    r = client.get("/api/v1/wanted?kind=ebook")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["author"] == "Jane Author"


def test_wanted_item_with_no_author_reports_unknown():
    work_id = migration._find_or_create_stub({"title": "Orphan Book", "author": "", "identifiers": {}})
    with db.tx() as conn:
        conn.execute("DELETE FROM work_contributors WHERE work_id=?", (work_id,))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at)"
            " VALUES (?,?,'ebook','missing',1,1,?,?)",
            (db.uid(), work_id, db.now(), db.now()),
        )
    items = client.get("/api/v1/wanted?kind=ebook").json()["items"]
    orphan = next(i for i in items if i["title"] == "Orphan Book")
    assert orphan["author"] == "Unknown author"
