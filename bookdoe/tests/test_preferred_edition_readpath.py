"""Part XXVII read path: preference used when set, today's behaviour when unset."""
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import editions as ed_svc, migration


def _work():
    wid = migration._find_or_create_stub({"title": "Read Path", "author": "A B", "identifiers": {}})
    db.execute("UPDATE works SET monitored=1 WHERE id=?", (wid,))
    return wid


def _edition(wid, title, date):
    eid = db.uid()
    t = db.now()
    db.execute("INSERT INTO editions(id,work_id,title,release_date,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (eid, wid, title, date, t, t))
    return eid


def _setup():
    wid = _work()
    e = [_edition(wid, "B", "2020-01-01"), _edition(wid, "A", "2010-01-01"), _edition(wid, "C", "2015-01-01")]
    return wid, e  # today's order: A(2010), C(2015), B(2020)


def _set(c, wid, kind, eid):
    assert c.put(f"/api/v1/library/{wid}/preferred-edition", json={"kind": kind, "edition_id": eid}).status_code == 200


def _calendar_dates(c, wid):
    return sorted(i["release_date"] for i in c.get("/api/v1/calendar").json()["items"] if i["work_id"] == wid)


def test_unset_matches_today():
    with TestClient(app) as c:
        wid, (b, a, cc) = _setup()
        d = c.get(f"/api/v1/library/{wid}").json()
        assert [x["id"] for x in d["editions"]] == [a, cc, b]
        assert d["preferred_ebook_edition"] is None and d["preferred_audiobook_edition"] is None
        assert _calendar_dates(c, wid) == ["2010-01-01", "2015-01-01", "2020-01-01"]
        w = db.one("SELECT * FROM works WHERE id=?", (wid,))
        assert ed_svc.preferred_edition(w, "ebook")["id"] == a
        assert ed_svc.preferred_edition(w, "audiobook")["id"] == a
        assert ed_svc.preferred_edition(w, "ebook", fallback=False) is None


def test_preference_applies_only_to_its_kind():
    with TestClient(app) as c:
        wid, (b, a, cc) = _setup()
        _set(c, wid, "ebook", b)
        w = db.one("SELECT * FROM works WHERE id=?", (wid,))
        assert ed_svc.preferred_edition(w, "ebook")["id"] == b
        assert ed_svc.preferred_edition(w, "audiobook")["id"] == a  # unchanged fallback
        d = c.get(f"/api/v1/library/{wid}").json()
        assert [x["id"] for x in d["editions"]] == [b, a, cc]
        assert d["preferred_audiobook_edition"] is None
        assert _calendar_dates(c, wid) == ["2020-01-01"]
        _set(c, wid, "audiobook", cc)
        d = c.get(f"/api/v1/library/{wid}").json()
        assert [x["id"] for x in d["editions"]] == [b, cc, a]
        assert _calendar_dates(c, wid) == ["2015-01-01", "2020-01-01"]


def test_repointed_or_deleted_edition_falls_back():
    with TestClient(app) as c:
        wid, (b, a, cc) = _setup()
        other = migration._find_or_create_stub({"title": "Other", "author": "Z Y", "identifiers": {}})
        _set(c, wid, "ebook", b)
        db.execute("UPDATE editions SET work_id=? WHERE id=?", (other, b))
        w = db.one("SELECT * FROM works WHERE id=?", (wid,))
        assert ed_svc.preferred_edition(w, "ebook")["id"] == a
        assert ed_svc.preferred_edition(w, "ebook", fallback=False) is None
        d = c.get(f"/api/v1/library/{wid}").json()
        assert [x["id"] for x in d["editions"]] == [a, cc]
        assert d["preferred_ebook_edition"] is None
        assert _calendar_dates(c, wid) == ["2010-01-01", "2015-01-01"]
        _set(c, wid, "audiobook", cc)
        db.execute("DELETE FROM editions WHERE id=?", (cc,))
        w = db.one("SELECT * FROM works WHERE id=?", (wid,))
        assert ed_svc.preferred_edition(w, "audiobook")["id"] == a
