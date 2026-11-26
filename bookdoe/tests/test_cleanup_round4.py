"""Final-review rules: fail CLOSED where hard links do not work (Unraid/FUSE, CIFS, exFAT), restore by no-overwrite rename
there, and per-work consent for the Kids shelf in a three-way merge."""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import filetrash, redundantfiles, tagging, workmerge

from test_redundant_files_trash import _pair, _trash_rows
from test_work_merge import _kids_tag, _work


@pytest.fixture(params=[errno.EXDEV, errno.EPERM, errno.ENOTSUP])
def no_links(request, monkeypatch):
    code = request.param

    def refuse(*a, **k):
        raise OSError(code, os.strerror(code))
    monkeypatch.setattr(os, "link", refuse)
    return code


def test_where_hard_links_fail_the_removal_is_refused_and_nothing_changes(tmp_path, no_links):
    rt, w, aid, ids, g = _pair(tmp_path)                        # the analysis probes the root with a throwaway file
    out = redundantfiles.analyse(w)
    (grp,) = out["groups"]
    (entry,) = grp["remove"]
    assert entry["unsupported"] is True and entry["file_will_move"] is False and "Remove them by hand" in entry["note"]
    assert not [p for p in (rt["path"] / ".bookdoe-trash").rglob("*") if p.is_file()]      # the probe cleaned up after itself
    victim = Path(entry["path"])
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [entry["id"]])
    assert exc.value.code == "links_unsupported" and "does not support the safe move" in str(exc.value)
    assert victim.read_bytes() == b"E" * 400 and os.stat(victim).st_nlink == 1
    assert _trash_rows() == []                                  # no 'moving' row, no log row at all
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 2
    r = TestClient(app).post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": [entry["id"]]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "links_unsupported"


def test_the_capability_is_probed_once_per_root(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    calls = []
    real = os.link
    monkeypatch.setattr(os, "link", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    redundantfiles.reset_link_cache()
    assert redundantfiles.link_support(str(rt["path"]))[0] is True
    redundantfiles.link_support(str(rt["path"]))
    redundantfiles.analyse(w)
    assert len(calls) == 1


def test_restore_without_hard_links_renames_without_overwriting(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"RN" * 100)
    victim = Path(g["remove"][0]["path"])
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    (row,) = _trash_rows()
    trashed_ino = os.stat(row["trash_path"]).st_ino
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"someone else's file")
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device link")))
    with pytest.raises(filetrash.TrashError) as exc:           # a taken name is refused on this path too
        filetrash.restore(row["id"])
    assert exc.value.code == "destination_exists" and victim.read_bytes() == b"someone else's file" and Path(row["trash_path"]).exists()
    victim.unlink()
    assert filetrash.restore(row["id"])["status"] == "restored"
    assert victim.read_bytes() == b"RN" * 100 and os.stat(victim).st_ino == trashed_ino and not Path(row["trash_path"]).exists()
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM media_files WHERE id=?", (g["remove"][0]["id"],)).fetchone()


def test_the_plain_rename_path_checks_the_destination_and_the_source(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    monkeypatch.setattr(filetrash, "_renameat2_noreplace", lambda s, d: False)       # no renameat2 on this system
    with pytest.raises(FileExistsError):
        filetrash._rename_move(str(a), str(b))
    assert a.read_bytes() == b"AAA" and b.read_bytes() == b"BBB"
    c = tmp_path / "c"
    ino = os.stat(a).st_ino
    filetrash._rename_move(str(a), str(c))
    assert c.read_bytes() == b"AAA" and os.stat(c).st_ino == ino and not a.exists()
    with pytest.raises(FileNotFoundError):
        filetrash._rename_move(str(a), str(tmp_path / "d"))


def test_renameat2_never_replaces_where_the_filesystem_has_it(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    try:
        got = filetrash._renameat2_noreplace(str(a), str(tmp_path / "free"))
    except OSError:
        got = False
    if got:
        assert (tmp_path / "free").read_bytes() == b"AAA" and not a.exists()
        a.write_bytes(b"AAA")
        with pytest.raises(FileExistsError):
            filetrash._renameat2_noreplace(str(a), str(b))
        assert b.read_bytes() == b"BBB" and a.read_bytes() == b"AAA"


# ---- Kids shelf: every work that would gain the tag is named and consented to -------------------------

def test_a_three_way_merge_needs_per_work_consent_before_an_adult_third_work_joins_the_kids_shelf():
    a, b, c = _work("Same", "X"), _work("Same", "X"), _work("Same", "X")
    tagging.add_tag(b, _kids_tag())                             # the kept record and the third one are adult, the second is on the shelf
    pre = workmerge.preview(a, [b, c])
    (blk,) = [x for x in pre["blockers"] if x["code"] == "kids_tag_mismatch"]
    assert {x["id"] for x in blk["gaining"]} == {a, c} and blk["adult_sources"] == [c]
    for res in ({}, {"kids_tag_union": True}, {"kids_tag_union": True, "kids_tag_union_works": [b]}):
        with pytest.raises(workmerge.MergeBlocked) as exc:
            workmerge.execute(a, [b, c], res, None)
        assert [x["code"] for x in exc.value.blockers] == ["kids_tag_mismatch"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merges").fetchone()[0] == 0 and conn.execute("SELECT 1 FROM works WHERE id=?", (c,)).fetchone()
    workmerge.execute(a, [b, c], {"kids_tag_union": True, "kids_tag_union_works": [c]}, None)


def test_a_two_way_union_still_needs_only_the_union_flag():
    a, b = _work("Same", "X"), _work("Same", "X")
    tagging.add_tag(b, _kids_tag())
    (blk,) = [x for x in workmerge.preview(a, [b])["blockers"] if x["code"] == "kids_tag_mismatch"]
    assert blk["adult_sources"] == [] and [x["id"] for x in blk["gaining"]] == [a]
    workmerge.execute(a, [b], {"kids_tag_union": True}, None)
