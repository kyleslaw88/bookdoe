"""Work merge execute (P5-24, Part XXVI): one transaction, a row-level reversal ledger, nothing destroyed."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import tagging, workmerge


def _work(title="The Hobbit", author="J. R. R. Tolkien", **cols):
    wid, t = db.uid(), db.now()
    row = {"title": title, "added_at": t, "updated_at": t, **cols}
    with db.tx() as conn:
        conn.execute(f"INSERT INTO works(id,{','.join(row)}) VALUES (?,{','.join('?' * len(row))})", (wid, *row.values()))  # nosec B608
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)",
                     (wid, db.contributor_id_for_name(conn, author)))
    return wid


def _asset(work_id, kind="ebook", fmt="epub", files=(), monitored=0, intent=0, status="owned"):
    aid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,has_acquisition_intent,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, work_id, kind, fmt, status, monitored, intent, t, t))
        for p in files:
            conn.execute("INSERT INTO media_files(id,asset_id,path,discovered_at) VALUES (?,?,?,?)", (db.uid(), aid, str(p), t))
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


def _hashes(conn, skip=()):
    """(row count, hash of every row) per table, over the whole schema."""
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if name in skip:
            continue
        rows = sorted(json.dumps(list(r), default=str) for r in conn.execute(f'SELECT * FROM "{name}"'))  # nosec B608
        out[name] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
    return out


def _populate(tmp_path):
    """Two richly populated Works that overlap on an ebook asset, a user's rating/read state and a series."""
    a, b = _work(), _work(title="The Hobbit (reissue)", description="There and back again")
    u, other = _user("u"), _user("other")
    files = {n: tmp_path / n for n in ("a.epub", "b.epub", "b.m4b")}
    for p in files.values():
        p.write_bytes(p.name.encode())
    a_epub = _asset(a, "ebook", "epub", [files["a.epub"]], monitored=0, intent=0)
    b_epub = _asset(b, "ebook", "epub", [files["b.epub"]], monitored=1, intent=1, status="none")
    b_m4b = _asset(b, "audiobook", "m4b", [files["b.m4b"]], monitored=1, intent=1)
    with db.tx() as conn:
        for w in (a, b):
            conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,1,1)", (f"ed-{w}", w, "Ed"))
        conn.execute("UPDATE works SET preferred_ebook_edition_id=? WHERE id=?", (f"ed-{b}", b))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','111')", (b,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition',?,'isbn','222')", (f"ed-{b}",))
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'narrator',1)", (b, db.contributor_id_for_name(conn, "Rob Inglis")))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,5,1)", (a, u))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,2,1)", (b, u))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,3,1)", (b, other))
        conn.execute("INSERT INTO read_states(work_id,user_id,status,updated_at) VALUES (?,?,'reading',1)", (a, u))
        conn.execute("INSERT INTO read_states(work_id,user_id,status,updated_at) VALUES (?,?,'finished',1)", (b, u))
        conn.execute("INSERT INTO local_meta(work_id,harvested_at) VALUES (?,1)", (b,))
        conn.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,is_current,observed_at) VALUES ('ob','work',?,'title','x',1,1)", (b,))
        conn.execute("INSERT INTO series_assertions(id,work_id,raw_value,status,created_at,updated_at) VALUES ('sa',?,'Middle Earth #1','accepted',1,1)", (b,))
    _series(a, "Middle Earth", "1")
    _series(b, "Middle Earth", "1")
    _series(b, "Tolkien Legendarium", "3")
    db.add_history("note", "from b", work_id=b)
    tagging.add_tag(a, tagging.create_tag("classic"))
    tagging.add_tag(b, tagging.create_tag("fantasy"))
    return {"a": a, "b": b, "u": u, "other": other, "files": files, "a_epub": a_epub, "b_epub": b_epub, "b_m4b": b_m4b}


def _paths():
    with db.session() as conn:
        return sorted(r[0] for r in conn.execute("SELECT path FROM media_files"))


def test_merge_keeps_the_union_on_the_canonical_and_removes_the_source(tmp_path):
    d = _populate(tmp_path)
    a, b = d["a"], d["b"]
    paths_before = _paths()
    with db.session() as conn:
        files_before = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
        history_b = conn.execute("SELECT id FROM history WHERE work_id=?", (b,)).fetchall()

    res = workmerge.execute(a, [b], {}, {"id": d["u"]}, confirm_dissimilar=True)

    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() is None
        assert {r[0] for r in conn.execute("SELECT id FROM editions WHERE work_id=?", (a,))} == {f"ed-{a}", f"ed-{b}"}
        assert conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0] == files_before
        assert {(r["kind"], r["format"]) for r in conn.execute("SELECT * FROM media_assets WHERE work_id=?", (a,))} == {("ebook", "epub"), ("audiobook", "m4b")}
        assert conn.execute("SELECT COUNT(*) FROM media_assets WHERE work_id=?", (b,)).fetchone()[0] == 0
        assert {(r["namespace"], r["value"]) for r in conn.execute("SELECT * FROM identifiers WHERE entity_type='work' AND entity_id=?", (a,))} == {("isbn", "111")}
        assert conn.execute("SELECT 1 FROM identifiers WHERE entity_type='edition' AND entity_id=?", (f"ed-{b}",)).fetchone()
        assert {r["role"] for r in conn.execute("SELECT role FROM work_contributors WHERE work_id=?", (a,))} == {"author", "narrator"}
        assert {r["name"] for r in conn.execute("SELECT t.name FROM work_tags w JOIN tags t ON t.id=w.tag_id WHERE w.work_id=?", (a,))} == {"classic", "fantasy"}
        assert {r["name"] for r in conn.execute("SELECT s.name FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (a,))} == {"Middle Earth", "Tolkien Legendarium"}
        assert conn.execute("SELECT work_id FROM series_assertions WHERE id='sa'").fetchone()[0] == a
        assert conn.execute("SELECT work_id FROM local_meta").fetchone()[0] == a
        assert conn.execute("SELECT entity_id,is_current FROM metadata_observations WHERE id='ob'").fetchone()[:] == (a, 1)
        assert all(conn.execute("SELECT work_id FROM history WHERE id=?", (h[0],)).fetchone()[0] == a for h in history_b)
        merged = conn.execute("SELECT * FROM history WHERE work_id=? AND event_type='work.merged'", (a,)).fetchone()
        assert json.loads(merged["data_json"])["merge_id"] == res["merge_id"]
        assert conn.execute("SELECT preferred_ebook_edition_id FROM works WHERE id=?", (a,)).fetchone()[0] == f"ed-{b}"
        # the audiobook asset moved as is; the colliding ebook asset's flags were OR-ed onto the canonical asset
        epub = conn.execute("SELECT * FROM media_assets WHERE id=?", (d["a_epub"],)).fetchone()
        assert (epub["monitored"], epub["has_acquisition_intent"], epub["status"]) == (1, 1, "owned")
        m4b = conn.execute("SELECT * FROM media_assets WHERE id=?", (d["b_m4b"],)).fetchone()
        assert (m4b["work_id"], m4b["monitored"], m4b["has_acquisition_intent"]) == (a, 1, 1)
        assert conn.execute("SELECT asset_id FROM media_files WHERE path=?", (str(d["files"]["b.epub"]),)).fetchone()[0] == d["a_epub"]
        assert conn.execute("SELECT 1 FROM metadata_observations WHERE entity_id=? AND field_name='merged_work'", (a,)).fetchone()
    assert _paths() == paths_before
    assert {p.name: p.read_bytes() for p in d["files"].values()} == {n: n.encode() for n in d["files"]}


def test_ratings_and_read_state_stay_per_user_and_the_losers_go_to_the_ledger(tmp_path):
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        rows = {(r["user_id"]): r["rating"] for r in conn.execute("SELECT * FROM ratings WHERE work_id=?", (d["a"],))}
        assert rows == {d["u"]: 5, d["other"]: 3}
        assert conn.execute("SELECT status FROM read_states WHERE work_id=? AND user_id=?", (d["a"], d["u"])).fetchone()[0] == "reading"
        ledger = conn.execute("SELECT * FROM work_merge_ledger WHERE merge_id=? AND action='deleted'", (res["merge_id"],)).fetchall()
    before = {(r["table_name"], json.loads(r["before_json"]).get("user_id")): json.loads(r["before_json"]) for r in ledger if r["table_name"] in ("ratings", "read_states")}
    assert before[("ratings", d["u"])]["rating"] == 2 and before[("ratings", d["u"])]["work_id"] == d["b"]
    assert before[("read_states", d["u"])]["status"] == "finished"


def test_injected_failure_rolls_back_rows_and_ledger(tmp_path):
    d = _populate(tmp_path)
    with db.tx() as conn:
        conn.execute("CREATE TRIGGER boom BEFORE DELETE ON works BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with db.session() as conn:
        before = _hashes(conn)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert _hashes(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger").fetchone()[0] == 0


def test_replaying_the_ledger_in_reverse_reproduces_the_original(tmp_path):
    skip = ("work_merges", "work_merge_ledger")
    d = _populate(tmp_path)
    with db.session() as conn:
        original = _hashes(conn, skip=skip)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    scratch = sqlite3.connect(tmp_path / "scratch.db")  # a copy of the merged database; the live one is untouched
    with db.session() as conn:
        conn.backup(scratch)
        entries = conn.execute("SELECT * FROM work_merge_ledger WHERE merge_id=? ORDER BY id DESC", (res["merge_id"],)).fetchall()
    scratch.row_factory = sqlite3.Row
    scratch.execute("PRAGMA foreign_keys=OFF")  # replay restores rows; it must not cascade through the ones it has not yet
    assert len(entries) == res["ledger_rows"] > 0
    assert _hashes(scratch, skip=skip) != original
    for e in entries:
        pk = json.loads(e["pk_json"])
        scratch.execute(f'DELETE FROM "{e["table_name"]}" WHERE ' + " AND ".join(f'"{k}"=?' for k in pk), list(pk.values()))  # nosec B608
        if e["before_json"] is not None:
            row = json.loads(e["before_json"])
            cols = ",".join(f'"{c}"' for c in row)
            scratch.execute(f'INSERT INTO "{e["table_name"]}"({cols}) VALUES ({",".join("?" * len(row))})', list(row.values()))  # nosec B608
    assert _hashes(scratch, skip=skip) == original
    scratch.close()


def test_kids_tag_is_a_blocker_until_the_union_is_accepted(tmp_path):
    a, b = _work(), _work()
    tagging.add_tag(b, _kids_tag())
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    assert [x["code"] for x in exc.value.blockers] == ["kids_tag_mismatch"]
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
    workmerge.execute(a, [b], {"kids_tag_union": True}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT t.name FROM work_tags w JOIN tags t ON t.id=w.tag_id WHERE w.work_id=?", (a,)).fetchone()[0] == "kids"


def _kids_tag():
    with db.session() as conn:
        return conn.execute("SELECT id FROM tags WHERE name='kids'").fetchone()[0]


def test_field_choice_and_endpoint(tmp_path):
    d = _populate(tmp_path)
    client = TestClient(app)
    r = client.post("/api/v1/works/merge", json={"canonical": d["a"], "sources": [d["b"]], "resolutions": {"fields": {"title": "source"}, "confirm_dissimilar": True}})
    assert r.status_code == 200, r.text
    with db.session() as conn:
        w = conn.execute("SELECT title,description FROM works WHERE id=?", (d["a"],)).fetchone()
        assert (w["title"], w["description"]) == ("The Hobbit (reissue)", "There and back again")
        assert conn.execute("SELECT created_by FROM work_merges WHERE id=?", (r.json()["merge_id"],)).fetchone() is not None
    assert client.post("/api/v1/works/merge", json={"canonical": "nope", "sources": [d["b"]]}).status_code == 404
    assert client.post("/api/v1/works/merge", json={"canonical": d["a"], "sources": [d["b"]]}).status_code == 409
    assert workmerge.preview(d["a"], [d["b"]])["blockers"][0]["code"] == "source_already_merged"


def test_ledger_has_entries_for_every_modified_table(tmp_path):
    """Every table that execute modifies must have at least one ledger entry (insert/update/delete)."""
    d = _populate(tmp_path)
    a, b = d["a"], d["b"]
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        ledger_tables = {r["table_name"] for r in
                         conn.execute("SELECT DISTINCT table_name FROM work_merge_ledger WHERE merge_id=?", (res["merge_id"],)).fetchall()}
        assert "works" in ledger_tables
        assert "media_assets" in ledger_tables
        assert "media_files" in ledger_tables
        assert "editions" in ledger_tables
        assert "identifiers" in ledger_tables
        assert "work_contributors" in ledger_tables
        assert "work_tags" in ledger_tables
        assert "series_memberships" in ledger_tables
        assert "history" in ledger_tables
        assert "ratings" in ledger_tables
        assert "read_states" in ledger_tables
        actual_count = conn.execute("SELECT COUNT(*) FROM work_merge_ledger WHERE merge_id=?", (res["merge_id"],)).fetchone()[0]
        assert actual_count == res["ledger_rows"]


def test_ledger_actions_are_moved_updated_deleted_inserted(tmp_path):
    """The ledger should contain every action, with an after-image for all but deletes."""
    d = _populate(tmp_path)
    a, b = d["a"], d["b"]
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM work_merge_ledger WHERE merge_id=?", (res["merge_id"],)).fetchall()
        assert {r["action"] for r in rows} == {"moved", "updated", "deleted", "inserted"}
        for r in rows:
            assert (r["before_json"] is None) == (r["action"] == "inserted")
            assert (r["after_json"] is None) == (r["action"] == "deleted")
        moved = [r for r in rows if r["action"] == "moved" and r["table_name"] == "work_tags"]
        assert moved and all(json.loads(r["after_json"])["work_id"] == a and json.loads(r["before_json"])["work_id"] == b for r in moved)
        assert conn.execute("SELECT status FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()[0] == "executed"


def test_ledger_before_json_preserves_original_values(tmp_path):
    """For update and delete ops, before_json should contain the original row values."""
    d = _populate(tmp_path)
    a, b = d["a"], d["b"]
    with db.session() as conn:
        original = dict(conn.execute("SELECT * FROM works WHERE id=?", (b,)).fetchone())
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        moved = conn.execute(
            "SELECT before_json, after_json FROM work_merge_ledger WHERE merge_id=? AND table_name='work_tags' AND action='moved'",
            (res["merge_id"],)).fetchall()
        assert moved
        for row in moved:
            assert json.loads(row["before_json"])["work_id"] == b
            assert json.loads(row["after_json"])["work_id"] == a
        deleted = conn.execute(
            "SELECT before_json FROM work_merge_ledger WHERE merge_id=? AND table_name='works' AND action='deleted'",
            (res["merge_id"],)).fetchall()
        assert len(deleted) == 1
        before = json.loads(deleted[0]["before_json"])
        assert before["id"] == b and before["title"] == original["title"] and before["description"] == original["description"]


def test_merge_three_sources(tmp_path):
    """Merging more than two works: all sources are folded into the canonical."""
    canon = _work("The Hobbit", "J. R. R. Tolkien")
    src1 = _work("The Hobbit (1st ed)", "J. R. R. Tolkien")
    src2 = _work("The Hobbit (reissue)", "J. R. R. Tolkien")
    u1, u2 = _user("u1"), _user("u2")
    tagging.add_tag(src1, tagging.create_tag("first-edition"))
    tagging.add_tag(src2, tagging.create_tag("reissue"))
    _series(canon, "Middle Earth", "1")
    _series(src1, "Middle Earth", "1")
    _series(src2, "Tolkien Legendarium", "3")
    with db.tx() as conn:
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','111')", (src1,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','222')", (src2,))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,3,1)", (src1, u1))
        conn.execute("INSERT INTO ratings(work_id,user_id,rating,updated_at) VALUES (?,?,4,1)", (src2, u2))

    res = workmerge.execute(canon, [src1, src2], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (src1,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (src2,)).fetchone() is None
        tags = {r[0] for r in conn.execute(
            "SELECT t.name FROM work_tags w JOIN tags t ON t.id=w.tag_id WHERE w.work_id=?", (canon,)).fetchall()}
        assert "first-edition" in tags
        assert "reissue" in tags
        series = {r[0] for r in conn.execute(
            "SELECT s.name FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (canon,)).fetchall()}
        assert "Middle Earth" in series
        assert "Tolkien Legendarium" in series
        idents = {r[0] for r in conn.execute(
            "SELECT value FROM identifiers WHERE entity_type='work' AND entity_id=?", (canon,)).fetchall()}
        assert "111" in idents
        assert "222" in idents
        merge_data = json.loads(conn.execute(
            "SELECT source_work_ids_json FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()["source_work_ids_json"])
        assert set(merge_data) == {src1, src2}


def test_duplicate_source_ids_are_deduped(tmp_path):
    """Passing the same source ID twice should not cause errors or duplicate ledger entries."""
    a, b = _work(), _work("The Hobbit", "J. R. R. Tolkien")
    res = workmerge.execute(a, [b, b, b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() is None
        merge_events = conn.execute(
            "SELECT * FROM history WHERE work_id=? AND event_type='work.merged'", (a,)).fetchall()
        assert len(merge_events) == 1
        merge_data = json.loads(merge_events[0]["data_json"])
        assert merge_data["merged_work_ids"] == [b]


def test_max_sources_limit(tmp_path):
    """Merging more than MAX_SOURCES (20) should raise BadRequest."""
    canon = _work()
    sources = [_work() for _ in range(21)]
    with pytest.raises(workmerge.BadRequest) as exc:
        workmerge.execute(canon, [str(s) for s in sources], {}, None, confirm_dissimilar=True)
    assert "at most 20" in str(exc.value)


def test_empty_source_list_raises(tmp_path):
    """Passing an empty source list should raise BadRequest."""
    a = _work()
    with pytest.raises(workmerge.BadRequest) as exc:
        workmerge.execute(a, [], {}, None, confirm_dissimilar=True)
    assert "at least one source" in str(exc.value)


def test_asset_folding_edition_id_transfer(tmp_path):
    """When folding assets, if the canonical asset has no edition_id but the source does,
    the canonical should inherit the source's edition_id."""
    canon = _work()
    src = _work()
    canon_ed = "ed-canon"
    src_ed = "ed-src"
    _asset(canon, "ebook", "epub", ["/lib/canon.epub"])
    src_aid = _asset(src, "ebook", "epub", ["/lib/src.epub"])
    with db.tx() as conn:
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,1,1)", (canon_ed, canon, "Canon Ed"))
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,1,1)", (src_ed, src, "Src Ed"))
        conn.execute("UPDATE works SET preferred_ebook_edition_id=? WHERE id=?", (canon_ed, canon))
        conn.execute("UPDATE works SET preferred_ebook_edition_id=? WHERE id=?", (src_ed, src))
        conn.execute("UPDATE media_assets SET edition_id=? WHERE id=?", (src_ed, src_aid))
    workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        canon_asset = conn.execute("SELECT * FROM media_assets WHERE work_id=? AND kind='ebook'", (canon,)).fetchone()
        assert canon_asset["edition_id"] == src_ed


def test_asset_status_or_logic(tmp_path):
    """When folding assets, if either asset is 'owned', the result should be 'owned'."""
    canon = _work()
    src = _work()
    canon_aid = _asset(canon, "ebook", "epub", ["/lib/canon.epub"], status="none")
    _asset(src, "ebook", "epub", ["/lib/src.epub"], status="owned")
    workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        merged_asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (canon_aid,)).fetchone()
        assert merged_asset["status"] == "owned"


def test_asset_monitored_or_logic(tmp_path):
    """When folding assets, monitored flag is OR-ed: if either is monitored, result is monitored."""
    canon = _work()
    src = _work()
    canon_aid = _asset(canon, "ebook", "epub", ["/lib/canon.epub"], monitored=0)
    _asset(src, "ebook", "epub", ["/lib/src.epub"], monitored=1)
    workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        merged_asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (canon_aid,)).fetchone()
        assert merged_asset["monitored"] == 1


def test_asset_acquisition_intent_or_logic(tmp_path):
    """When folding assets, has_acquisition_intent flag is OR-ed."""
    canon = _work()
    src = _work()
    canon_aid = _asset(canon, "ebook", "epub", ["/lib/canon.epub"], intent=0)
    _asset(src, "ebook", "epub", ["/lib/src.epub"], intent=1)
    workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        merged_asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (canon_aid,)).fetchone()
        assert merged_asset["has_acquisition_intent"] == 1


def test_execute_fails_closed_on_a_dependent_table_it_does_not_handle(tmp_path):
    """A table with a cascading FK to works that HANDLED lacks would lose rows with the source Work, unledgered."""
    d = _populate(tmp_path)
    with db.tx() as conn:
        conn.execute("CREATE TABLE future_thing (id TEXT PRIMARY KEY, work_id TEXT REFERENCES works(id) ON DELETE CASCADE)")
        conn.execute("INSERT INTO future_thing(id,work_id) VALUES ('f1',?)", (d["b"],))
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    assert [b["code"] for b in exc.value.blockers] == ["unhandled_tables"]
    assert exc.value.blockers[0]["tables"] == ["future_thing.work_id"]
    with db.session() as conn:
        assert conn.execute("SELECT work_id FROM future_thing").fetchone()[0] == d["b"]
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger").fetchone()[0] == 0


def test_preview_reports_the_unhandled_tables_blocker_before_execute_is_tried(tmp_path):
    """The UI sees the same unhandled_tables guard in the preview, so it never offers a merge that would fail closed."""
    d = _populate(tmp_path)
    with db.tx() as conn:
        conn.execute("CREATE TABLE future_thing (id TEXT PRIMARY KEY, work_id TEXT REFERENCES works(id) ON DELETE CASCADE)")
    p = workmerge.preview(d["a"], [d["b"]])
    blocker = next(b for b in p["blockers"] if b["code"] == "unhandled_tables")
    assert blocker["tables"] == ["future_thing.work_id"]
    assert [(u["table"], u["column"]) for u in p["plan"]["unhandled"]] == [("future_thing", "work_id")]


def test_nonexistent_canonical_raises(tmp_path):
    """Merging with a nonexistent canonical should raise WorkNotFound."""
    with pytest.raises(workmerge.WorkNotFound):
        workmerge.execute("nonexistent", ["some-source"], {}, None, confirm_dissimilar=True)


def test_merge_with_no_conflicting_data(tmp_path):
    """Merging works that share nothing should still work and create a merge record."""
    a = _work("Book A", "Author A")
    b = _work("Book B", "Author B")
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (a,)).fetchone()
        merge = conn.execute("SELECT * FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()
        assert merge is not None
        assert json.loads(merge["source_work_ids_json"]) == [b]


def test_merge_preserves_media_file_paths(tmp_path):
    """File paths on disk should not change after merge; only asset_id references update."""
    d = _populate(tmp_path)
    paths_before = _paths()
    workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    paths_after = _paths()
    assert paths_before == paths_after
    for name, content in d["files"].items():
        assert (tmp_path / name).read_bytes() == name.encode()


def test_merge_with_user_id_in_ledger(tmp_path):
    """The created_by field in work_merges should record the user who performed the merge."""
    a, b = _work(), _work()
    user_id = _user("merger")
    res = workmerge.execute(a, [b], {}, {"id": user_id}, confirm_dissimilar=True)
    with db.session() as conn:
        merge = conn.execute("SELECT created_by FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()
        assert merge["created_by"] == user_id


def test_merge_without_user_id(tmp_path):
    """Merging without a user should set created_by to None."""
    a, b = _work(), _work()
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        merge = conn.execute("SELECT created_by FROM work_merges WHERE id=?", (res["merge_id"],)).fetchone()
        assert merge["created_by"] is None


def test_series_position_filled_when_canonical_lacks(tmp_path):
    """If the canonical has a series membership with an empty position and the source has one,
    the canonical's position should be filled from the source."""
    canon = _work()
    src = _work()
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM series WHERE name='Test Series'").fetchone()
        sid = row["id"] if row else db.uid()
        if not row:
            conn.execute("INSERT INTO series(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                         (sid, "Test Series", "Test Series", db.now(), db.now()))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (canon, sid, ""))
        conn.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (src, sid, "5"))
    res = workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        pos = conn.execute("SELECT position FROM series_memberships WHERE work_id=? AND series_id=?", (canon, sid)).fetchone()
        assert pos["position"] == "5"


def test_metadata_observations_current_flag_handled(tmp_path):
    """When moving observations, if the canonical already has a current observation for the
    same field, the source's current observation should be demoted."""
    canon = _work()
    src = _work()
    with db.tx() as conn:
        conn.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,is_current,observed_at) VALUES ('o1','work',?,'title','canon-title',1,1)", (canon,))
        conn.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,is_current,observed_at) VALUES ('o2','work',?,'title','src-title',1,1)", (src,))
    res = workmerge.execute(canon, [src], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        o1 = conn.execute("SELECT is_current FROM metadata_observations WHERE id='o1'").fetchone()
        o2 = conn.execute("SELECT is_current FROM metadata_observations WHERE id='o2'").fetchone()
        assert o1["is_current"] == 1, "Canonical's current observation should stay current"
        assert o2["is_current"] == 0, "Source's current observation should be demoted"


def test_ledger_ids_are_contiguous_within_a_merge(tmp_path):
    """A merge's ledger ids are contiguous and increasing, so reverse id order is the replay order."""
    d = _populate(tmp_path)
    res = workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        ids = [r["id"] for r in
               conn.execute("SELECT id FROM work_merge_ledger WHERE merge_id=? ORDER BY id", (res["merge_id"],)).fetchall()]
        assert ids == list(range(ids[0], ids[0] + len(ids))) and len(ids) == res["ledger_rows"]


def test_api_merge_returns_409_on_blockers(tmp_path):
    """The API should return 409 with blocker details when the merge is blocked."""
    d = _populate(tmp_path)
    client = TestClient(app)
    tagging.add_tag(d["b"], _kids_tag())
    r = client.post("/api/v1/works/merge", json={
        "canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}
    })
    assert r.status_code == 409
    body = r.json()
    assert "blockers" in body.get("detail", body)
    blockers = body.get("detail", body).get("blockers", body.get("blockers", []))
    assert blockers[0]["code"] == "kids_tag_mismatch"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger").fetchone()[0] == 0


def test_api_merge_returns_200_with_merge_id(tmp_path):
    """The API should return 200 with merge_id, canonical_work_id, source_work_ids, and ledger_rows."""
    d = _populate(tmp_path)
    client = TestClient(app)
    r = client.post("/api/v1/works/merge", json={
        "canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}
    })
    assert r.status_code == 200
    body = r.json()
    assert "merge_id" in body
    assert body["canonical_work_id"] == d["a"]
    assert body["source_work_ids"] == [d["b"]]
    assert isinstance(body["ledger_rows"], int)
    assert body["ledger_rows"] > 0


@pytest.mark.parametrize("trigger", [
    "BEFORE UPDATE ON media_files",     # while assets are folded, after the ledger has started
    "BEFORE UPDATE ON identifiers",     # after the assets, before the Work row is touched
    "BEFORE INSERT ON history",         # the very last write: the work_merged event
])
def test_failure_halfway_leaves_the_database_identical(tmp_path, trigger):
    d = _populate(tmp_path)
    with db.tx() as conn:
        conn.execute(f"CREATE TRIGGER boom {trigger} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with db.session() as conn:
        before = _hashes(conn)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        workmerge.execute(d["a"], [d["b"]], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        assert _hashes(conn) == before
    assert all(p.exists() for p in d["files"].values())


def test_preservation_counts_add_up_and_conflicts_are_in_the_ledger(tmp_path):
    d = _populate(tmp_path)
    a, b = d["a"], d["b"]

    def counts(conn, work_ids):
        marks = ",".join("?" * len(work_ids))
        eds = [r[0] for r in conn.execute(f"SELECT id FROM editions WHERE work_id IN ({marks})", work_ids)]  # nosec B608
        n = lambda sql, args=work_ids: conn.execute(sql, args).fetchone()[0]  # noqa: E731
        return {
            "editions": len(eds),
            "assets": n(f"SELECT COUNT(*) FROM media_assets WHERE work_id IN ({marks})"),  # nosec B608
            "files": n("SELECT COUNT(*) FROM media_files", []),
            "identifiers": n(f"SELECT COUNT(*) FROM identifiers WHERE (entity_type='work' AND entity_id IN ({marks}))"  # nosec B608
                             f" OR (entity_type='edition' AND entity_id IN ({','.join('?' * len(eds))}))", [*work_ids, *eds]),
            "tags": n(f"SELECT COUNT(DISTINCT tag_id) FROM work_tags WHERE work_id IN ({marks})"),  # nosec B608
            "ratings": n(f"SELECT COUNT(*) FROM ratings WHERE work_id IN ({marks})"),  # nosec B608
            "read_states": n(f"SELECT COUNT(*) FROM read_states WHERE work_id IN ({marks})"),  # nosec B608
            "history": n(f"SELECT COUNT(*) FROM history WHERE work_id IN ({marks})"),  # nosec B608
            "intent": n(f"SELECT COALESCE(MAX(has_acquisition_intent),0) FROM media_assets WHERE work_id IN ({marks})"),  # nosec B608
        }

    with db.session() as conn:
        pre = counts(conn, [a, b])
    res = workmerge.execute(a, [b], {}, None, confirm_dissimilar=True)
    with db.session() as conn:
        post = counts(conn, [a])
        deleted = {}
        for r in conn.execute("SELECT table_name FROM work_merge_ledger WHERE merge_id=? AND action='deleted'", (res["merge_id"],)):
            deleted[r[0]] = deleted.get(r[0], 0) + 1
    # one same-kind asset (with its file) folded, so one asset fewer; both users' ratings/read states
    # collided on u: the canonical's row wins and the source's lives on in the ledger
    assert post["editions"] == pre["editions"] == 2
    assert post["assets"] == pre["assets"] - deleted["media_assets"] == 2
    assert post["files"] == pre["files"] == 3
    assert post["identifiers"] == pre["identifiers"] == 2
    assert post["tags"] == pre["tags"] == 2
    assert post["ratings"] == pre["ratings"] - deleted["ratings"] == 2
    assert post["read_states"] == pre["read_states"] - deleted["read_states"] == 1
    assert post["history"] == pre["history"] + 1  # the source's note moved; one work.merged event was added
    assert post["intent"] == pre["intent"] == 1


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def _body(d, **over):
    return {"canonical": d["a"], "sources": [d["b"]], "resolutions": {"confirm_dissimilar": True}, **over}


def test_merge_endpoint_requires_a_signed_in_admin_and_csrf(tmp_path, local_mode):
    with TestClient(app) as anon, TestClient(app) as admin, TestClient(app) as adult:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})  # first user, before _populate adds users
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        d = _populate(tmp_path)
        assert anon.post("/api/v1/works/merge", json=_body(d)).status_code == 401
        r = admin.post("/api/v1/users", json={"username": "jane", "password": "a-very-long-jane-password", "account_type": "adult"}, headers=csrf)
        assert r.status_code == 200, r.text
        jane = adult.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        assert adult.post("/api/v1/works/merge", json=_body(d), headers={"X-CSRF-Token": jane.json()["csrf"]}).status_code == 403
        assert admin.post("/api/v1/works/merge", json=_body(d)).status_code == 403  # no CSRF token
        assert admin.post("/api/v1/works/merge", json=_body(d), headers={"X-CSRF-Token": "forged"}).status_code == 403
        with db.session() as conn:  # every rejection left the data alone
            assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
            assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone()
        r = admin.post("/api/v1/works/merge", json=_body(d), headers=csrf)
        assert r.status_code == 200, r.text
        with db.session() as conn:
            assert conn.execute("SELECT created_by FROM work_merges").fetchone()[0] is not None


@pytest.mark.parametrize("body,status", [
    ({"canonical": "x"}, 422),                                  # sources missing
    ({"canonical": "x", "sources": "y"}, 422),                  # sources not a list
    ({"canonical": "x", "sources": ["y"], "bogus": 1}, 422),    # unknown field (extra=forbid)
    ({"canonical": "x", "sources": []}, 400),                 # nothing to merge
    ({"canonical": "x", "sources": ["y"]}, 404),              # unknown canonical
])
def test_merge_endpoint_validation(tmp_path, body, status):
    d = _populate(tmp_path)
    r = TestClient(app).post("/api/v1/works/merge", json=body)
    assert r.status_code == status, r.text
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (d["b"],)).fetchone()


def test_merge_endpoint_rejects_canonical_among_sources_and_unknown_sources(tmp_path):
    d = _populate(tmp_path)
    client = TestClient(app)
    for body in (_body(d, sources=[d["a"]]), _body(d, sources=["no-such-work"])):
        r = client.post("/api/v1/works/merge", json=body)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["blockers"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
