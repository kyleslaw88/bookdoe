"""Round-3 safety rules: the real kid-visibility predicate before and after every merge and undo, directory-handle moves and
the keeper re-check, read-only analysis, hashing outside the lock, the single-worker warning."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from app import db
from app import main as main_module
from app.services import filetrash, redundantfiles, tagging, workmerge

import conv_fixtures as cf
from test_cleanup_round2 import _kid, _sees
from test_redundant_files_trash import _book, _pair, _trash_rows
from test_work_merge import _kids_tag, _work


def _blocked(a, b, **res):
    with pytest.raises(workmerge.MergeBlocked) as exc:
        workmerge.execute(a, [b], res, None)
    return exc.value.blockers


def _intact(a, b):
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() and conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0


# ---- 1. the real predicate, every way a kid can see the canonical -----------------------------------

def test_a_canonical_a_kid_sees_through_a_content_grant_blocks_an_adult_source():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("tom")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, a))
    assert _sees(kid, a) and not _sees(kid, b)
    (blk,) = [x for x in workmerge.preview(a, [b])["blockers"]]
    assert blk["code"] == "kid_visibility_widening" and blk["kids"] == ["tom"]
    (hit,) = _blocked(a, b)
    assert hit["code"] == "kid_visibility_widening" and hit["kids"] == ["tom"]
    _intact(a, b)
    assert [x["code"] for x in _blocked(a, b, kids_widen_grant=True)] == ["kid_visibility_widening"]      # the wrong box does not count
    workmerge.execute(a, [b], {"kids_widen_visibility": True}, None)
    assert _sees(kid, a)


def test_a_canonical_visible_through_a_collection_grant_blocks_an_adult_source():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("ann")
    tag = tagging.create_tag("approved-collection")
    tagging.add_tag(a, tag)
    with db.tx() as conn:
        conn.execute("INSERT INTO tag_grants(user_id,tag_id,created_at) VALUES (?,?,1)", (kid, tag))
    assert _sees(kid, a) and not _sees(kid, b)
    assert [x["code"] for x in _blocked(a, b)] == ["kid_visibility_widening"]
    _intact(a, b)
    workmerge.execute(a, [b], {"kids_widen": True}, None)             # the umbrella key still consents to everything


def test_the_kids_tag_variant_names_its_own_blocker_and_its_own_box():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("sam")
    tagging.add_tag(a, _kids_tag())
    assert [x["code"] for x in _blocked(a, b)] == ["adult_into_kids_canonical"]
    assert [x["code"] for x in _blocked(a, b, kids_widen_visibility=True)] == ["adult_into_kids_canonical"]
    workmerge.execute(a, [b], {"kids_widen_adult": True}, None)


def test_the_mirror_cases_a_grant_or_collection_on_the_other_record():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("amy")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, b))
    assert [x["code"] for x in _blocked(a, b)] == ["kid_grant_widening"]
    assert [x["code"] for x in _blocked(a, b, kids_widen_tag_grant=True)] == ["kid_grant_widening"]
    workmerge.execute(a, [b], {"kids_widen_grant": True}, None)
    assert _sees(kid, a)
    c, d, kid2 = _work("Other", "X"), _work("Other", "X"), _kid("bo")
    tag = tagging.create_tag("shared-collection")
    tagging.add_tag(d, tag)
    with db.tx() as conn:
        conn.execute("INSERT INTO tag_grants(user_id,tag_id,created_at) VALUES (?,?,1)", (kid2, tag))
    assert [x["code"] for x in _blocked(c, d)] == ["tag_grant_widening"]


def test_nothing_blocks_when_the_kid_sees_both_or_neither():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("lee")
    assert workmerge.preview(a, [b])["blockers"] == []                    # sees neither
    workmerge.execute(a, [b], {}, None)
    c, d = _work("Both", "X"), _work("Both", "X")
    with db.tx() as conn:
        for w in (c, d):
            conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, w))
    assert workmerge.preview(c, [d])["blockers"] == []                    # sees both
    workmerge.execute(c, [d], {}, None)


def test_execute_enforces_the_real_predicate_even_if_the_preview_missed_it(monkeypatch):
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("tom")
    with db.tx() as conn:
        conn.execute("INSERT INTO content_grants(user_id,work_id,created_at) VALUES (?,?,1)", (kid, a))
    monkeypatch.setattr(workmerge, "_kid_widening", lambda *x: [])        # a preview with no opinion
    assert [x["code"] for x in _blocked(a, b)] == ["kid_visibility_widening"]
    _intact(a, b)
    assert not db.one("SELECT 1 x FROM content_grants WHERE work_id=?", (b,))


# ---- 2. every undo gets the same check -----------------------------------------------------------

def test_a_clean_undo_cannot_bring_back_a_kids_book_after_the_admin_took_the_merged_one_off_the_shelf():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("kim")
    kids = _kids_tag()
    tagging.add_tag(a, kids)
    tagging.add_tag(b, kids)
    res = workmerge.execute(a, [b], {}, None)                              # both on the shelf: nothing widens
    with db.tx() as conn:                                                   # later the admin un-shelves the merged book (A's own row, not in the ledger)
        conn.execute("DELETE FROM work_tags WHERE work_id=?", (a,))
    assert not _sees(kid, a)
    with pytest.raises(workmerge.ReversalRefused) as exc:                  # no row conflict, not forced
        workmerge.reverse(res["merge_id"])
    assert exc.value.code == "kid_widening" and exc.value.detail["kids"] == ["kim"]
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (b,)).fetchone() is None
    assert workmerge.reverse(res["merge_id"], allow_kid_widening=True)["status"] == "reversed"


def test_an_ordinary_undo_still_works_with_kid_accounts_around():
    a, b, kid = _work("Same", "X"), _work("Same", "X"), _kid("kim")
    tagging.add_tag(a, _kids_tag())
    tagging.add_tag(b, _kids_tag())
    res = workmerge.execute(a, [b], {}, None)
    assert workmerge.reverse(res["merge_id"])["status"] == "reversed" and _sees(kid, b)


# ---- 3. directory handles and the keeper re-check ------------------------------------------------

def test_a_folder_swapped_for_a_symlink_to_the_keepers_folder_between_link_and_unlink_cannot_delete_the_keeper(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"KEEP" * 50)
    victim, keeper = Path(g["remove"][0]["path"]), Path(g["keep"]["path"])
    real_link = os.link

    def link_then_swap(*a, **k):
        real_link(*a, **k)
        victim.parent.rename(victim.parent.with_name("copy.moved"))        # the folder we hold a handle on moves away...
        os.symlink(keeper.parent, victim.parent, target_is_directory=True)  # ...and a symlink to the keeper's folder takes its place
    monkeypatch.setattr(os, "link", link_then_swap)
    out = filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    monkeypatch.undo()
    assert out["bytes_moved"] == 200 and keeper.read_bytes() == b"KEEP" * 50      # the keeper's only name survived
    (row,) = _trash_rows()
    assert Path(row["trash_path"]).read_bytes() == b"KEEP" * 50 and row["status"] == "trashed"
    assert not (victim.parent.with_name("copy.moved") / victim.name).exists()       # removed from the folder the handle pointed at


def test_if_the_keeper_changes_during_the_move_everything_is_rolled_back_and_an_alert_is_raised(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"KEEP" * 50)
    victim, keeper = Path(g["remove"][0]["path"]), Path(g["keep"]["path"])
    real = filetrash.move_no_clobber
    done = []

    def move_then_lose_keeper(src, dst, *a):
        real(src, dst, *a)
        if not done:
            done.append(1)
            keeper.unlink()                                                  # something deletes the kept copy right after the move
    monkeypatch.setattr(filetrash, "move_no_clobber", move_then_lose_keeper)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert exc.value.code == "move_failed" and "changed or vanished" in str(exc.value)
    assert victim.read_bytes() == b"KEEP" * 50                               # linked back to its original name
    assert [r["status"] for r in _trash_rows()] == ["failed"]
    assert not any((rt["path"] / ".bookdoe-trash").rglob("*.epub"))
    (alert,) = db.all("SELECT message FROM history WHERE event_type='files.alert'")
    assert "ALERT" in alert["message"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 2


# ---- 4. the analysis is read-only apart from the verification columns ----------------------------

def test_opening_the_analysis_changes_nothing_but_the_verification_columns(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"RO" * 100)
    Path(g["remove"][0]["path"]).write_bytes(b"XY" * 100)                    # silently changed on disk
    def snap():
        with db.session() as conn:
            files = [tuple(r) for r in conn.execute("SELECT id,asset_id,path,size_bytes,sha256,mtime,integrity_state,missing FROM media_files ORDER BY id")]
            return files, [tuple(r) for r in conn.execute("SELECT * FROM file_trash")], [tuple(r) for r in conn.execute("SELECT id,work_id,format,status FROM media_assets")]
    before = snap()
    redundantfiles.analyse(w)
    assert snap() == before
    assert db.one("SELECT checksum_verified FROM media_files WHERE id=?", (g["remove"][0]["id"],))["checksum_verified"] == hashlib.sha256(b"XY" * 100).hexdigest()
    assert db.one("SELECT sha256 FROM media_files WHERE id=?", (g["remove"][0]["id"],))["sha256"] == hashlib.sha256(b"RO" * 100).hexdigest()


# ---- 5. hashing happens outside the lock ---------------------------------------------------------

def test_the_trash_page_is_not_held_up_by_hashing(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    started, release = threading.Event(), threading.Event()
    real = redundantfiles.fresh_sha256

    def slow(path, deadline=None, cache=None):
        started.set()
        assert release.wait(10)
        return real(path, deadline, cache)
    monkeypatch.setattr(redundantfiles, "fresh_sha256", slow)
    t = threading.Thread(target=lambda: filetrash.remove_redundant(w, [g["remove"][0]["id"]]))
    t.start()
    assert started.wait(10)
    done = threading.Event()
    threading.Thread(target=lambda: (filetrash.list_trash(), done.set())).start()
    assert done.wait(3)                                                        # listed while the big file is still being hashed
    release.set()
    t.join(20)
    assert _trash_rows()[0]["status"] == "trashed"


def test_each_file_is_read_once_and_a_file_changed_after_hashing_is_read_again(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"ONCE" * 100)
    count = []
    real_sha = hashlib.sha256
    monkeypatch.setattr(hashlib, "sha256", lambda *a, **k: (count.append(1), real_sha(*a, **k))[1])
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert len(count) == 2                                                     # one read per file, not two
    monkeypatch.undo()
    rt2, w2, aid2, ids2, g2 = _pair(tmp_path / "two", title="Emma", data=b"EDIT" * 100, rels=("e/a.epub", "e/b.epub"))
    victim = Path(g2["remove"][0]["path"])
    real_excl = filetrash._exclusive

    from contextlib import contextmanager

    @contextmanager
    def edit_before_lock():
        victim.write_bytes(b"edit" * 100)                                       # same size, new bytes and mtime, after the pre-hash
        with real_excl():
            yield
    monkeypatch.setattr(filetrash, "_exclusive", edit_before_lock)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w2, [g2["remove"][0]["id"]])
    assert exc.value.code == "not_redundant" and victim.read_bytes() == b"edit" * 100      # re-read inside the lock: no longer identical
    assert db.one("SELECT checksum_verified FROM media_files WHERE id=?", (g2["remove"][0]["id"],))["checksum_verified"] == hashlib.sha256(b"edit" * 100).hexdigest()


# ---- single worker ------------------------------------------------------------------------------

def test_more_than_one_worker_is_warned_about():
    assert main_module.worker_count_warning({}) is None
    assert main_module.worker_count_warning({"WEB_CONCURRENCY": "1"}) is None
    assert main_module.worker_count_warning({"WEB_CONCURRENCY": "x"}) is None
    msg = main_module.worker_count_warning({"UVICORN_WORKERS": "4"})
    assert "UVICORN_WORKERS=4" in msg and "single worker" in msg
