"""Round-2 safety rules for merge and cleanup: fresh checksums, the reconcile/purge/restore lock, half-done moves, swapped
folders, kid visibility on merge and undo, forced undo after a purge, single-file sets, and the settings rate limit."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import filetrash, redundantfiles, tagging, workmerge, writelimit

import conv_fixtures as cf
from test_redundant_files_trash import _book, _pair, _parts, _sha, _trash_rows
from test_work_merge import _kids_tag, _work


# ---- 1. the disk decides, not the stored checksum -------------------------------------------------

def test_an_edited_copy_with_a_stale_checksum_is_not_identical_and_nothing_is_moved(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"A" * 400)
    stale = redundantfiles.analyse(w)                       # taken while both copies were still identical
    assert stale["groups"] and stale["groups"][0]["checksum_identical"]
    victim = Path(g["remove"][0]["path"])
    victim.write_bytes(b"B" * 400)                           # same size, different bytes: its content exists nowhere else
    fresh = redundantfiles.analyse(w)
    assert fresh["groups"] == []
    row = db.one("SELECT sha256,checksum_verified,checksum_verified_at FROM media_files WHERE id=?", (g["remove"][0]["id"],))
    assert row["sha256"] == _sha(b"A" * 400)                                                # the scanner's checksum is never overwritten
    assert row["checksum_verified"] == _sha(b"B" * 400) and row["checksum_verified_at"]     # the disk's answer is kept beside it
    (alert,) = db.all("SELECT message,data_json FROM history WHERE event_type='file.checksum_changed'")
    assert "ALERT" in alert["message"] and db.json_load(alert["data_json"])["disk_sha256"] == _sha(b"B" * 400)
    redundantfiles.analyse(w)
    assert len(db.all("SELECT 1 FROM history WHERE event_type='file.checksum_changed'")) == 1      # one notice, not one per page view
    # a caller holding the stale analysis is stopped by the removal-time re-hash
    monkeypatch.setattr(redundantfiles, "analyse", lambda wid, **k: stale)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert exc.value.code == "content_changed" and victim.read_bytes() == b"B" * 400 and not _trash_rows()


def test_files_that_cannot_be_rehashed_are_never_offered(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    monkeypatch.setattr(redundantfiles, "HASH_MAX_BYTES", 10)
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and {i["why"] for i in out["ignored"]} == {"unverified"} and "larger than" in out["ignored"][0]["detail"]
    monkeypatch.setattr(redundantfiles, "HASH_MAX_BYTES", 1 << 30)
    monkeypatch.setattr(redundantfiles, "HASH_BUDGET_SECONDS", -1)
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and "budget" in out["ignored"][0]["detail"]


def test_an_unreadable_file_stops_the_removal(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    real = redundantfiles.fresh_sha256

    def broken(path, deadline=None, cache=None):
        if path == g["remove"][0]["path"]:
            raise redundantfiles.HashUnavailable("input/output error")
        return real(path, deadline, cache)
    stale = redundantfiles.analyse(w)
    monkeypatch.setattr(redundantfiles, "analyse", lambda wid, **k: stale)
    monkeypatch.setattr(redundantfiles, "fresh_sha256", broken)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert exc.value.code == "unverified" and Path(g["remove"][0]["path"]).exists() and not _trash_rows()


# ---- 2/7/8. the lock, the grace period, and what reconcile decides from the disk ------------------

def _moving_row(rt, w, aid, origin: Path, trash: Path, age=3600.0):
    fid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,discovered_at) VALUES (?,?,?,?,1)", (fid, aid, str(origin), 5))
        conn.execute("INSERT INTO file_trash(id,batch_id,root_id,root_path,work_id,asset_id,asset_kind,asset_format,original_path,relative_path,trash_path,size_bytes,record_only,file_json,status,trashed_at,expires_at) "
                     "VALUES (?,?,?,?,?,?,'ebook','epub',?,?,?,5,0,?,'moving',?,?)",
                     (f"m-{fid}", "b", rt["id"], str(rt["path"]), w, aid, str(origin), origin.name, str(trash), db_json({"id": fid, "asset_id": aid, "path": str(origin)}), db.now() - age, db.now() + 1e6))
    return f"m-{fid}", fid


def db_json(v):
    return db.json_dump(v)


def _row(tid):
    return db.one("SELECT status FROM file_trash WHERE id=?", (tid,))["status"]


def test_reconcile_leaves_a_fresh_moving_row_alone_and_settles_old_ones_from_the_disk(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, aid, _ = _book(rt, files=[("keep.epub", b"k" * 9, {})])
    trash_dir = rt["path"] / ".bookdoe-trash" / "2026-10-14"
    trash_dir.mkdir(parents=True)
    # (1) the move finished: file only in trash
    o1, t1 = rt["path"] / "one.epub", trash_dir / "one.epub"
    t1.write_bytes(b"1" * 5)
    r1, f1 = _moving_row(rt, w, aid, o1, t1, age=5)
    assert filetrash.reconcile() == {"completed": 0, "reverted": 0, "unresolved": 0, "lost": 0} and _row(r1) == "moving"      # younger than the grace period
    # (2) origin only: never moved
    o2, t2 = rt["path"] / "two.epub", trash_dir / "two.epub"
    o2.write_bytes(b"2" * 5)
    r2, _ = _moving_row(rt, w, aid, o2, t2)
    # (3) both names, same inode: a kill between link and unlink
    o3, t3 = rt["path"] / "three.epub", trash_dir / "three.epub"
    o3.write_bytes(b"3" * 5)
    os.link(o3, t3)
    r3, _ = _moving_row(rt, w, aid, o3, t3)
    # (4) both names, different files: left for a human
    o4, t4 = rt["path"] / "four.epub", trash_dir / "four.epub"
    o4.write_bytes(b"4" * 5)
    t4.write_bytes(b"x" * 5)
    r4, _ = _moving_row(rt, w, aid, o4, t4)
    # (5) neither: lost, with an alert
    r5, _ = _moving_row(rt, w, aid, rt["path"] / "five.epub", trash_dir / "five.epub")
    with db.tx() as conn:
        conn.execute("UPDATE file_trash SET trashed_at=? WHERE id=?", (db.now() - 3600, r1))
    out = filetrash.reconcile()
    assert out == {"completed": 1, "reverted": 2, "unresolved": 1, "lost": 1}
    assert [_row(r) for r in (r1, r2, r3, r4, r5)] == ["trashed", "reverted", "reverted", "moving", "lost"]
    assert o3.read_bytes() == b"3" * 5 and not t3.exists() and o4.exists() and t4.exists()           # the interrupted link was cleaned up
    assert db.one("SELECT 1 x FROM history WHERE event_type='files.lost'")
    assert db.one("SELECT 1 x FROM media_files WHERE path=?", (str(o1),)) is None                    # the finished move got its record removed


def test_reconcile_waits_for_a_removal_that_is_still_running(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    started, release = threading.Event(), threading.Event()
    real = filetrash._finalize

    def slow(*a, **k):
        started.set()
        assert release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(filetrash, "_finalize", slow)
    out = {}
    t1 = threading.Thread(target=lambda: out.update(removed=filetrash.remove_redundant(w, [victim["id"]])))
    t1.start()
    assert started.wait(10)
    res = {}
    t2 = threading.Thread(target=lambda: res.update(r=filetrash.reconcile(grace=0)))      # even with no grace, the lock makes it wait
    t2.start()
    t2.join(0.3)
    assert t2.is_alive() and _trash_rows()[0]["status"] == "moving"
    release.set()
    t1.join(10)
    t2.join(10)
    assert res["r"]["reverted"] == 0
    (row,) = _trash_rows()
    assert row["status"] == "trashed" and Path(row["trash_path"]).exists() and not Path(victim["path"]).exists()
    assert filetrash.restore(row["id"])["status"] == "restored"                           # not an invisible, unrestorable file


def test_a_purge_cannot_run_in_the_middle_of_a_restore(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"RST" * 30)
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    (row,) = _trash_rows()
    started, release = threading.Event(), threading.Event()
    real = filetrash.move_no_clobber
    seen = []

    def slow(src, dst, *a):
        if not seen:
            seen.append(1)
            started.set()
            assert release.wait(10)
        return real(src, dst, *a)
    monkeypatch.setattr(filetrash, "move_no_clobber", slow)
    t1 = threading.Thread(target=lambda: filetrash.restore(row["id"]))
    t1.start()
    assert started.wait(10)
    out = {}
    t2 = threading.Thread(target=lambda: out.update(r=filetrash.purge([row["id"]])))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()                                  # blocked by the restore's lock
    release.set()
    t1.join(10)
    t2.join(10)
    assert out["r"]["purged"] == 0                        # by the time purge runs the entry is already restored
    assert Path(g["remove"][0]["path"]).read_bytes() == b"RST" * 30
    assert _trash_rows()[0]["status"] == "restored"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE path=?", (g["remove"][0]["path"],)).fetchone()[0] == 1


# ---- 3/4. a move that cannot finish leaves everything as it was --------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_if_the_original_cannot_be_unlinked_the_new_name_is_removed_and_the_row_is_failed(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = Path(g["remove"][0]["path"])
    victim.parent.chmod(0o555)                              # a read-only folder: link works, unlink fails
    try:
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w, [g["remove"][0]["id"]])
        assert exc.value.code == "move_failed"
        assert victim.read_bytes() == b"E" * 400 and os.stat(victim).st_nlink == 1             # untouched, no stray second name
        assert not any((rt["path"] / ".bookdoe-trash").rglob("*.epub"))
        (row,) = _trash_rows()
        assert row["status"] == "failed" and "move failed" in row["reason"]
        with db.session() as conn:
            assert conn.execute("SELECT 1 FROM media_files WHERE id=?", (g["remove"][0]["id"],)).fetchone()
    finally:
        victim.parent.chmod(0o755)
    assert redundantfiles.analyse(w)["groups"]              # still a candidate: it was never half-linked


def test_a_folder_swapped_for_a_symlink_after_the_check_aborts_the_move(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = Path(g["remove"][0]["path"])
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / victim.name).write_bytes(b"decoy, not the checked file")
    real_verify = filetrash._verify

    def swap_after_verify(*a, **k):
        out = real_verify(*a, **k)
        victim.parent.rename(victim.parent.with_name("copy.real"))
        os.symlink(decoy_dir, victim.parent, target_is_directory=True)
        return out
    monkeypatch.setattr(filetrash, "_verify", swap_after_verify)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert exc.value.code == "move_failed"
    assert (decoy_dir / victim.name).read_bytes() == b"decoy, not the checked file"
    assert (victim.parent.with_name("copy.real") / victim.name).read_bytes() == b"E" * 400
    assert not any((rt["path"] / ".bookdoe-trash").rglob("*.epub")) and [r["status"] for r in _trash_rows()] == ["failed"]


# ---- 9. single-file sets -------------------------------------------------------------------------

def test_two_single_file_copies_numbered_part_one_are_not_offered_as_similar(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, *_ = _book(rt, "Dune", "audiobook", "mp3", _parts(rt, "a", 1, salt=b"A") + _parts(rt, "b", 1, size=1003, salt=b"B"))
    assert redundantfiles.analyse(w)["groups"] == []


# ---- 5. kids must not gain access through a merge ------------------------------------------------

def _kid(name):
    uid = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,is_admin,access_scope,created_at,updated_at) VALUES (?,?,0,'kids',?,?)", (uid, name, db.now(), db.now()))
    return uid


def _sees(kid, work):
    return tagging.is_visible_to(work, {"id": kid, "access_scope": "kids"})


def _codes(a, b):
    return [x["code"] for x in workmerge.preview(a, [b])["blockers"]]


def _enforced(a, b, **res):
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(a, [b], res, None)
    return [x["code"] for x in exc.value.blockers]


def test_a_kids_grant_on_the_other_record_cannot_widen_the_kept_one_without_consent():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), None
    kid = _kid("tom")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, b))
    assert _sees(kid, b) and not _sees(kid, a)
    pre = workmerge.preview(a, [b])
    (blk,) = [x for x in pre["blockers"] if x["code"] == "kid_grant_widening"]
    assert blk["kids"] == ["tom"] and blk["proposed_resolution"] == {"action": "kids_widen"}
    assert _enforced(a, b) == ["kid_grant_widening"] and not _sees(kid, a)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0
    workmerge.execute(a, [b], {"kids_widen": True}, None)
    assert _sees(kid, a)                                   # only because the admin said so


def test_a_tag_grant_only_the_other_record_carries_cannot_widen_the_kept_one_without_consent():
    a, b = _work("Same", "X"), _work("Same", "X")
    kid = _kid("ann")
    tag = tagging.create_tag("approved-collection")
    tagging.add_tag(b, tag)
    with db.tx() as conn:
        conn.execute("INSERT INTO tag_grants(user_id,tag_id,created_at) VALUES (?,?,1)", (kid, tag))
    assert _sees(kid, b) and not _sees(kid, a)
    assert _enforced(a, b) == ["tag_grant_widening"] and not _sees(kid, a)
    workmerge.execute(a, [b], {"kids_widen": True}, None)
    assert _sees(kid, a)


def test_an_adult_record_cannot_be_merged_into_a_kids_shelf_record_without_consent():
    a, b = _work("Same", "X"), _work("Same", "X")
    kid = _kid("sam")
    tagging.add_tag(a, _kids_tag())
    assert _sees(kid, a) and not _sees(kid, b)
    (blk,) = [x for x in workmerge.preview(a, [b])["blockers"] if x["code"] == "adult_into_kids_canonical"]
    assert blk["kids"] == ["sam"]
    assert _enforced(a, b) == ["adult_into_kids_canonical"]
    assert db.one("SELECT 1 x FROM works WHERE id=?", (b,))
    workmerge.execute(a, [b], {"kids_widen": True}, None)


def test_the_mirror_case_and_records_the_kid_already_sees_do_not_double_block():
    a, b = _work("Same", "X"), _work("Same", "X")
    kid = _kid("amy")
    tagging.add_tag(b, _kids_tag())
    assert _codes(a, b) == ["kids_tag_mismatch"]                    # a kids source into an adult canonical: the existing blocker
    c, d = _work("Other", "X"), _work("Other", "X")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, c))
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, d))
    assert _codes(c, d) == []                                       # she sees both already: nothing widens
    workmerge.execute(c, [d], {}, None)


def test_a_forced_undo_cannot_bring_back_a_kids_book_for_a_kid_who_cannot_see_the_merged_one():
    a, b = _work("Same", "X"), _work("Same", "X")
    kid = _kid("kim")
    tagging.add_tag(b, _kids_tag())
    res = workmerge.execute(a, [b], {"kids_tag_union": True}, None)
    with db.tx() as conn:        # later: the admin takes the merged book off the Kids shelf
        conn.execute("DELETE FROM work_tags WHERE work_id=?", (a,))
    assert not _sees(kid, a)
    with pytest.raises(workmerge.ReversalRefused) as exc:
        workmerge.reverse(res["merge_id"], force=True)
    assert exc.value.code == "kid_widening" and exc.value.detail["kids"] == ["kim"]
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() is None and conn.execute("SELECT status FROM work_merges").fetchone()[0] == "executed"
    assert workmerge.reverse(res["merge_id"], force=True, allow_kid_widening=True)["status"] == "reversed"


# ---- 6. a forced undo never recreates a record for a file that is gone ----------------------------

def test_a_forced_undo_after_a_purge_skips_the_record_of_the_deleted_file(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"BC" * 200
    a, _, (fa,) = _book(rt, "Same", files=[("a/Same.epub", data, {})])
    b, _, (fb,) = _book(rt, "Same", files=[("b/longer-name/Same.epub", data, {})])
    res = workmerge.execute(a, [b], {}, None)
    (g,) = redundantfiles.analyse(a)["groups"]
    gone = g["remove"][0]
    assert gone["id"] == fb
    filetrash.remove_redundant(a, [fb])
    (row,) = _trash_rows()
    filetrash.purge([row["id"]])
    with pytest.raises(workmerge.ReversalBlocked):
        workmerge.reverse(res["merge_id"])
    out = workmerge.reverse(res["merge_id"], force=True)
    assert [s["path"] for s in out["skipped_files"]] == [gone["path"]]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE path=?", (gone["path"],)).fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone()
    assert Path(g["keep"]["path"]).exists()


# ---- 11. the settings write is limited too ---------------------------------------------------------

def test_the_trash_settings_write_shares_the_admin_rate_limit(monkeypatch):
    monkeypatch.setattr(writelimit, "LIMIT", 3)
    client = TestClient(app)
    codes = [client.put("/api/v1/trash/settings", json={"retention_days": 10 + i}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
