"""P5-08: independent per-kind series follow policy (Part XV)."""
import json

from fastapi.testclient import TestClient

from app import db, main, migrations
from app.services import metadata, series as series_service
from tests.test_migrations_framework import _wipe, steps


def _book(title, series_id, *, ebook_status="none", audio_status="none"):
    wid = db.uid()
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, title, title, "resolved", t, t))
        for kind, status in (("ebook", ebook_status), ("audiobook", audio_status)):
            conn.execute(
                "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,?,0,?,?)",
                (db.uid(), wid, kind, status, 1 if status == "owned" else 0, t, t),
            )
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, series_id, "1"))
    return wid


def _series(name="Kind Series"):
    sid = db.uid()
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, name, name, t, t))
    return sid


def _asset(work_id, kind):
    return db.one("SELECT status,monitored,has_acquisition_intent FROM media_assets WHERE work_id=? AND kind=?", (work_id, kind))


def _put(sid, body):
    with TestClient(main.app) as c:
        return c.put(f"/api/v1/series/{sid}/monitor", json=body)


def test_audiobook_all_ebook_none_sets_intent_only_on_audiobooks():
    sid = _series()
    w1, w2 = _book("One", sid), _book("Two", sid)
    r = _put(sid, {"audiobook_policy": "all", "ebook_policy": "none"})
    assert r.status_code == 200
    assert (r.json()["audiobook_policy"], r.json()["ebook_policy"]) == ("all", "none")
    for w in (w1, w2):
        assert _asset(w, "audiobook") == {"status": "missing", "monitored": 1, "has_acquisition_intent": 1}
        assert _asset(w, "ebook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}
    assert any(s["id"] == sid and s["audiobook_policy"] == "all" and s["ebook_policy"] == "none" for s in series_service.list_series())


def test_future_alone_changes_no_existing_asset():
    sid = _series()
    w = _book("One", sid)
    before = (_asset(w, "audiobook"), _asset(w, "ebook"), db.one("SELECT monitored FROM works WHERE id=?", (w,)))
    assert _put(sid, {"audiobook_policy": "future"}).status_code == 200
    assert (_asset(w, "audiobook"), _asset(w, "ebook"), db.one("SELECT monitored FROM works WHERE id=?", (w,))) == before
    assert series_service.stored_kind_policies(sid) == ("future", "none")


def test_future_policy_gives_intent_to_a_later_member_by_kind():
    sid = _series()
    assert _put(sid, {"audiobook_policy": "future", "ebook_policy": "none"}).status_code == 200
    late = _book("Late", sid)
    with db.tx() as conn:
        series_service.apply_future_policy(conn, late, sid)
    assert _asset(late, "audiobook")["has_acquisition_intent"] == 1
    assert _asset(late, "ebook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}


def test_omitted_kind_with_no_stored_row_does_not_clear():
    """When no series_monitors row exists and one kind is omitted, the omitted
    kind must NOT clear existing monitored/intent on non-owned assets.

    This covers the case where a library already has ebook assets with
    monitored=1 (e.g. from a prior manual action or author Follow) and the
    operator calls set_series_monitor with only audiobook_policy. The ebook
    state must be left untouched.

    An explicit ebook_policy:'none' must still clear, confirming the explicit
    flag is what gates the clearing logic.
    """
    sid = db.uid()
    t = db.now()
    wid = db.uid()
    # Create a series with a member that has an ebook with monitored=1
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, "Existing Book", "Existing Book", "resolved", t, t))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (db.uid(), wid, "ebook", "none", 1, 1, t, t),
        )
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (db.uid(), wid, "audiobook", "none", 0, 0, t, t),
        )
        conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, "Omitted Kind Series", "Omitted Kind Series", t, t))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, "1"))

    # Verify no series_monitors row exists
    assert db.one("SELECT series_id FROM series_monitors WHERE series_id=?", (sid,)) is None

    # PUT only audiobook_policy: 'future' -- ebook_policy is omitted
    r = _put(sid, {"audiobook_policy": "future"})
    assert r.status_code == 200

    # The ebook asset must be LEFT UNTOUCHED (monitored=1, intent=1)
    assert _asset(wid, "ebook") == {"status": "none", "monitored": 1, "has_acquisition_intent": 1}

    # The audiobook should be unchanged too (policy is 'future', not 'all')
    assert _asset(wid, "audiobook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}

    # Now test that an EXPLICIT ebook_policy:'none' DOES clear
    r2 = _put(sid, {"audiobook_policy": "future", "ebook_policy": "none"})
    assert r2.status_code == 200
    assert _asset(wid, "ebook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}


def test_upsert_work_joining_a_followed_series_records_intent():
    sid = db.uid()
    t = db.now()
    from app.services import seriesnorm
    norm = seriesnorm.norm_key("Provider Series")
    with db.tx() as conn:
        conn.execute("INSERT INTO series(id,name,sort_name,norm_key,created_at,updated_at) VALUES (?,?,?,?,?,?)", (sid, "Provider Series", "Provider Series", norm, t, t))
    assert _put(sid, {"audiobook_policy": "future", "ebook_policy": "none"}).status_code == 200
    wid = metadata.upsert_work({"id": "ol:new", "title": "New Book", "series": [{"name": "Provider Series"}]})
    assert _asset(wid, "audiobook") == {"status": "missing", "monitored": 1, "has_acquisition_intent": 1}
    assert _asset(wid, "ebook")["has_acquisition_intent"] == 0


def test_none_clears_unowned_intent_but_never_demotes_owned():
    sid = _series()
    owned, wanted = _book("Owned", sid, audio_status="owned"), _book("Wanted", sid)
    assert _put(sid, {"audiobook_policy": "all", "ebook_policy": "all"}).status_code == 200
    assert _put(sid, {"audiobook_policy": "none", "ebook_policy": "none"}).status_code == 200
    assert _asset(owned, "audiobook")["status"] == "owned"
    assert _asset(owned, "audiobook")["monitored"] == 1
    assert _asset(wanted, "audiobook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}
    assert _asset(wanted, "ebook") == {"status": "none", "monitored": 0, "has_acquisition_intent": 0}


def test_legacy_body_maps_onto_per_kind_policies():
    sid = _series()
    w = _book("One", sid)
    r = _put(sid, {"policy": "all", "ebook": False, "audiobook": True})
    assert r.status_code == 200
    assert (r.json()["audiobook_policy"], r.json()["ebook_policy"]) == ("all", "none")
    row = db.one("SELECT policy,ebook,audiobook FROM series_monitors WHERE series_id=?", (sid,))
    assert (row["policy"], row["ebook"], row["audiobook"]) == ("all", 0, 1)
    assert _asset(w, "audiobook")["has_acquisition_intent"] == 1 and _asset(w, "ebook")["has_acquisition_intent"] == 0


def test_unknown_field_and_mixed_shapes_return_422():
    sid = _series()
    assert _put(sid, {"audiobook_policy": "all", "monitored": False}).status_code == 422
    assert _put(sid, {"audiobook_policy": "all", "policy": "all"}).status_code == 422
    assert _put(sid, {"audiobook_policy": "sometimes"}).status_code == 422


def _old_shape_database():
    _wipe()
    with db.session() as conn:
        conn.executescript(db.SCHEMA + db.EXTRA_SCHEMA)
        conn.execute("DROP TABLE series_monitors")
        conn.execute(
            "CREATE TABLE series_monitors (series_id TEXT PRIMARY KEY REFERENCES series(id) ON DELETE CASCADE, "
            "policy TEXT NOT NULL CHECK(policy IN ('all','future','none')) DEFAULT 'future', "
            "ebook INTEGER NOT NULL DEFAULT 0, audiobook INTEGER NOT NULL DEFAULT 1, started_at REAL NOT NULL, last_refresh_at REAL)"
        )
        t = db.now()
        for sid, policy, ebook, audio in (("s-a", "all", 0, 1), ("s-b", "future", 1, 1), ("s-c", "none", 1, 0)):
            conn.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, sid, t, t))
            conn.execute("INSERT INTO series_monitors(series_id,policy,ebook,audiobook,started_at) VALUES (?,?,?,?,?)", (sid, policy, ebook, audio, t))
        conn.commit()


def test_migration_backfills_old_rows_and_is_already_applied_on_rerun():
    _old_shape_database()
    db.init_db()
    assert 12 in steps()
    with db.session() as conn:
        got = {r["series_id"]: (r["audiobook_policy"], r["ebook_policy"]) for r in conn.execute("SELECT * FROM series_monitors")}
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=12").fetchone()[0])
        conn.execute("DELETE FROM migration_steps WHERE version=12")
        rerun = migrations._migrate_series_kind_policies(conn)
    assert got == {"s-a": ("all", "none"), "s-b": ("future", "future"), "s-c": ("none", "none")}
    assert counts == {"rows_backfilled": 3, "already_applied": 0}
    assert rerun["already_applied"] == 1


def test_fresh_database_has_columns_and_step_is_already_applied():
    _wipe()
    db.init_db()
    with db.session() as conn:
        assert {"audiobook_policy", "ebook_policy"} <= migrations._columns(conn, "series_monitors")
        counts = json.loads(conn.execute("SELECT row_counts_json FROM migration_steps WHERE version=12").fetchone()[0])
    assert counts["already_applied"] == 1
    assert migrations.run_pending_migrations() == []
