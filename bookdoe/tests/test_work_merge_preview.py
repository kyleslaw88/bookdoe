"""Work merge preview (P5-23, Part XXVI): evidence, per-table plan, conflicts, blockers; read-only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.services import tagging, workmerge

import helpers


def _work(title="The Hobbit", author="J. R. R. Tolkien", **cols):
    """A fresh Work row. Not migration._find_or_create_stub, which would reuse the same title+author."""
    wid, t = db.uid(), db.now()
    row = {"title": title, "added_at": t, "updated_at": t, **cols}
    with db.tx() as conn:
        conn.execute(f"INSERT INTO works(id,{','.join(row)}) VALUES (?,{','.join('?' * len(row))})", (wid, *row.values()))  # nosec B608
        cid = db.contributor_id_for_name(conn, author)
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid, cid))
    return wid


def _asset(work_id, kind="ebook", fmt="epub", files=(), **cols):
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, work_id, kind, fmt, cols.get("status", "owned"), cols.get("monitored", 0), cols.get("intent", 0), t, t))
        for p in files:
            conn.execute("INSERT INTO media_files(id,asset_id,path,discovered_at) VALUES (?,?,?,?)", (db.uid(), aid, p, t))
    return aid


def _user(name):
    uid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,is_admin,created_at,updated_at) VALUES (?,?,0,?,?)", (uid, name, db.now(), db.now()))
    return uid


def _series(work_id, name, position):
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM series WHERE name=?", (name,)).fetchone()
        sid = row["id"] if row else db.uid()
        if not row:
            conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, name, name, db.now(), db.now()))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work_id, sid, position))
    return sid


def _entry(plan, table, column=None):
    return next(e for e in plan["plan"]["tables"] if e["table"] == table and (column is None or e["column"] == column))


def _types(res):
    return [c["type"] for c in res["conflicts"]]


def _snapshot():
    """Row counts of every table, so 'no row changes' is checked over the whole schema."""
    with db.session() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}  # nosec B608


def test_counts_of_everything_that_moves_and_the_asset_collision():
    canon, src = _work(), _work("The Hobbit", "J. R. R. Tolkien")
    a_can = _asset(canon, "ebook", "epub", ["/lib/a/hobbit.epub"])
    a_src = _asset(src, "ebook", "epub", ["/lib/b/hobbit.epub", "/lib/b/hobbit2.epub"])
    _asset(src, "audiobook", "m4b", ["/lib/b/hobbit.m4b"])
    with db.tx() as conn:
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("ed-src", src, "Ed", 1, 1))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','111')", (src,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition','ed-src','isbn','222')")
        conn.execute("INSERT INTO local_meta(work_id,harvested_at) VALUES (?,1)", (src,))
        conn.execute("INSERT INTO cover_cache(work_id,status,updated_at) VALUES (?, 'none', 1)", (src,))
        conn.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,is_current,observed_at) VALUES ('o1','work',?,'title','x',1,1)", (src,))
        conn.execute("INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at) VALUES ('work',?,'title',1)", (src,))
    db.add_history("note", "hi", work_id=src)
    tagging.add_tag(src, tagging.create_tag("favourite"))

    res = workmerge.preview(canon, [src])

    assert res["ok"] is True
    assert _entry(res, "media_assets", "work_id")["rows"] == 2
    assert _entry(res, "media_assets", "work_id")["move"] == 1
    files = _entry(res, "media_files")
    assert (files["rows"], files["reparented"], files["move"]) == (3, 2, 3)
    assert _entry(res, "editions")["rows"] == 1
    assert _entry(res, "identifiers")["rows"] == 2
    assert _entry(res, "local_meta")["move"] == 1
    assert _entry(res, "cover_cache")["move"] == 1
    assert _entry(res, "history", "work_id")["rows"] >= 1
    assert _entry(res, "work_tags")["move"] == 1
    assert _entry(res, "metadata_observations")["rows"] == 1
    assert _entry(res, "metadata_field_locks")["rows"] == 1
    collision = next(c for c in res["conflicts"] if c["type"] == "asset_collision")
    assert (collision["kind"], collision["format"]) == ("ebook", "epub")
    assert collision["source_asset_id"] == a_src and collision["canonical_asset_id"] == a_can
    assert collision["files_to_reparent"] == 2
    assert res["plan"]["unhandled"] == []


def test_ratings_read_states_series_position_and_preferred_edition_conflicts():
    canon, src = _work(), _work()
    u1, u2 = _user("a"), _user("b")
    with db.tx() as conn:
        for w, rating in ((canon, 5), (src, 2)):
            conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,?,1)", (w, u1, rating))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,4,1)", (src, u2))
        conn.execute("INSERT INTO read_states(work_id,user_id,status,updated_at) VALUES (?,?,'reading',1)", (canon, u1))
        conn.execute("INSERT INTO read_states(work_id,user_id,status,updated_at) VALUES (?,?,'finished',1)", (src, u1))
        for w in (canon, src):
            conn.execute("INSERT INTO editions(id,work_id,created_at,updated_at) VALUES (?,?,1,1)", (f"ed-{w}", w))
            conn.execute("UPDATE works SET preferred_ebook_edition_id=? WHERE id=?", (f"ed-{w}", w))
    _series(canon, "Middle Earth", "1")
    _series(src, "Middle Earth", "2")

    res = workmerge.preview(canon, [src])
    by = {c["type"]: c for c in res["conflicts"]}

    assert by["rating_conflict"]["canonical_value"] == 5 and by["rating_conflict"]["source_value"] == 2
    assert "canonical wins" in by["rating_conflict"]["proposed_resolution"]
    assert by["read_state_conflict"]["source_value"] == "finished"
    assert by["series_position_conflict"]["canonical_position"] == "1" and by["series_position_conflict"]["source_position"] == "2"
    assert by["series_position_conflict"]["proposed_resolution"]
    assert by["preferred_edition_conflict"]["kind"] == "ebook"
    assert _entry(res, "ratings")["rows"] == 2 and _entry(res, "ratings")["absorbed"] == 1
    assert _entry(res, "ratings")["move"] == 1
    assert {"table": "ratings", "rows": 1, "preserved_in": "merge ledger"} in res["lost"]


def test_field_and_field_lock_conflicts():
    canon = _work("The Hobbit", description="Bilbo", first_publish_date="1937")
    src = _work("The Hobbit", description="There and back again", first_publish_date="1937")
    with db.tx() as conn:
        for w in (canon, src):
            conn.execute("INSERT INTO metadata_field_locks(entity_type,entity_id,field_name,locked_at) VALUES ('work',?,'description',1)", (w,))
    res = workmerge.preview(canon, [src])
    fields = [c for c in res["conflicts"] if c["type"] == "field_conflict"]
    assert [f["field"] for f in fields] == ["description"]
    assert fields[0]["choices"] == ["canonical", "source"]
    assert [c["field"] for c in res["conflicts"] if c["type"] == "field_lock_conflict"] == ["description"]
    assert _entry(res, "metadata_field_locks")["absorbed"] == 1


def test_kids_tag_on_source_only_is_a_tag_union_proposal_not_a_drop():
    canon, src = _work(), _work()
    tagging.add_tag(src, tagging.kids_tag_id())
    res = workmerge.preview(canon, [src])
    b = next(b for b in res["blockers"] if b["code"] == "kids_tag_mismatch")
    assert b["work_id"] == src
    assert b["proposed_resolution"] == {"action": "tag_union", "add_tags": ["kids"], "to_work_id": canon}
    assert "kids" in [t["name"] for t in res["plan"]["tag_union"]]
    assert res["ok"] is False
    # the reverse direction loses nothing: the canonical keeps its tag
    assert not [b for b in workmerge.preview(src, [canon])["blockers"] if b["code"] == "kids_tag_mismatch"]


def test_blockers_for_self_merge_missing_and_already_merged_sources():
    canon, other = _work(), _work()
    db.add_history("work_merged", "merged", data={"merged_work_id": "gone"})
    res = workmerge.preview(canon, [canon, "gone", "never-existed", other])
    codes = {b["work_id"]: b["code"] for b in res["blockers"] if "work_id" in b}
    assert codes == {canon: "source_equals_canonical", "gone": "source_already_merged", "never-existed": "source_not_found"}
    assert [s["id"] for s in res["sources"]] == [other]
    with pytest.raises(workmerge.WorkNotFound):
        workmerge.preview("nope", [other])
    with pytest.raises(workmerge.BadRequest):
        workmerge.preview(canon, [])


def test_acquisition_and_ownership_are_or_ed():
    canon, src = _work(monitored=0), _work(monitored=1)
    _asset(canon, "ebook", "epub", status="none")
    _asset(src, "ebook", "epub", status="owned", monitored=1, intent=1)
    res = workmerge.preview(canon, [src])
    acq = res["plan"]["acquisition"]
    assert acq["rule"] == "or" and acq["work_monitored"]["after"] is True
    assert acq["owned_assets"]["sources"] == 1 and acq["acquisition_intent_assets"]["sources"] == 1
    assert next(c for c in res["conflicts"] if c["type"] == "asset_collision")["flags_after"] == {"owned": True, "monitored": True, "acquisition_intent": True}


def test_evidence_states_and_maintenance_signals():
    canon = _work("The Hobbit", "J. R. R. Tolkien")
    src = _work("Hobbit, The", "J. R. R. Tolkien")
    with db.tx() as conn:
        for w, eid in ((canon, "e1"), (src, "e2")):
            conn.execute("INSERT INTO editions(id,work_id,created_at,updated_at) VALUES (?,?,1,1)", (eid, w))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','X1')", (canon,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition','e2','isbn','x1')", ())
    from app.services import maintenance
    maintenance.run_detector("duplicate-works")
    ev = workmerge.preview(canon, [src])["evidence"][0]
    assert ev["title"]["state"] == "similar"
    assert ev["author"]["state"] == "exact"
    assert ev["identifier"]["state"] == "exact"
    assert ev["series"]["state"] == "unavailable"
    assert ev["maintenance"]["kind"] in ("duplicate_work", "split_duplicate_work")
    assert "title_equal_normalized" in ev["maintenance"]["signals"]


def test_table_inventory_lists_handled_and_reports_a_new_table_as_unhandled():
    with db.session() as conn:
        inv = workmerge.table_inventory(conn)
        assert inv["unhandled"] == [], "every current table referencing works/assets/editions must be handled"
        handled = {(d["table"], d["column"]) for d in inv["handled"]}
        assert {("media_files", "asset_id"), ("work_tags", "work_id"), ("editions", "work_id"), ("works", "preferred_ebook_edition_id")} <= handled
        assert ("metadata_observations", "entity_id") in handled
        # every FK-derived table is in HANDLED, and nothing in HANDLED is stale
        assert {(d["table"], d["column"]) for d in workmerge.dependent_tables(conn)} == set(workmerge.HANDLED)
    with db.tx() as conn:
        conn.execute("CREATE TABLE future_thing (id TEXT PRIMARY KEY, work_id TEXT REFERENCES works(id))")
    with db.session() as conn:
        assert [(d["table"], d["column"]) for d in workmerge.table_inventory(conn)["unhandled"]] == [("future_thing", "work_id")]
    res = workmerge.preview(_work(), [_work()])
    assert res["plan"]["unhandled"][0]["table"] == "future_thing"
    assert any(b["code"] == "unhandled_tables" and b["tables"] == ["future_thing.work_id"] for b in res["blockers"])
    assert res["ok"] is False


def test_preview_writes_nothing_database_bytes_and_row_counts_unchanged():
    canon, src = _work(), _work()
    _asset(canon, files=["/lib/a.epub"])
    _asset(src, files=["/lib/b.epub"])
    tagging.add_tag(src, tagging.kids_tag_id())
    with db.session() as conn:  # fold the WAL in so file bytes are comparable
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path = Path(settings.db_path)
    with TestClient(app) as c:  # startup seeds rows (anonymous user, jobs); measure after it
        c.get("/health")
        with db.session() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_hash, before_counts = hashlib.sha256(path.read_bytes()).hexdigest(), _snapshot()
        wal = Path(str(path) + "-wal")
        workmerge.preview(canon, [src])
        assert c.get(f"/api/v1/works/merge-preview?canonical={canon}&sources={src}").status_code == 200
    assert _snapshot() == before_counts
    assert not wal.exists() or wal.stat().st_size == 0
    with db.session() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_api_route_validates_and_returns_the_preview():
    canon, s1, s2 = _work(), _work(), _work()
    with TestClient(app) as c:
        r = c.get("/api/v1/works/merge-preview", params={"canonical": canon, "sources": f"{s1},{s2}"})
        assert r.status_code == 200
        body = r.json()
        assert [s["id"] for s in body["sources"]] == [s1, s2]
        assert len(body["evidence"]) == 2 and body["ok"] is True
        assert c.get("/api/v1/works/merge-preview", params={"canonical": "nope", "sources": s1}).status_code == 404
        assert c.get("/api/v1/works/merge-preview", params={"canonical": canon, "sources": " , "}).status_code == 400
        assert c.get("/api/v1/works/merge-preview", params={"canonical": canon}).status_code == 422
