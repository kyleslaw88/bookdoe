"""Redundant-file cleanup after a merge: analysis, the per-root trash folder, restore, purge, and the safety rules
(default-deny): symlinks, hard links, partial/mixed audiobook sets, symlinked trash folders, last copy per part,
crash-safe two-phase moves, race-free restore, confirmations, rate limit and audit."""
from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import filetrash, paths, redundantfiles, scheduler, workmerge, writelimit

import conv_fixtures as cf
from test_work_merge import _work

DAY = 86400


def _file(rt, rel: str, data: bytes) -> Path:
    p = rt["path"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _book(rt, title="Dune", kind="ebook", fmt="epub", files=(), work=None):
    """A work with one owned asset holding real files. ``files`` are (relative path, bytes, extra media_files columns)."""
    work = work or _work(title, "Frank Herbert")
    aid, t = db.uid(), db.now()
    ids = []
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at) VALUES (?,?,?,?,'owned',?,?,?)",
                     (aid, work, kind, fmt, rt["id"], t, t))
        for rel, data, extra in files:
            p = rt["path"] / rel
            if not (p.exists() or p.is_symlink()):
                _file(rt, rel, data)
            fid = db.uid()
            cols = {"size_bytes": len(data), "sha256": _sha(data), **extra}
            conn.execute(f"INSERT INTO media_files(id,asset_id,path,relative_path,discovered_at,{','.join(cols)}) VALUES (?,?,?,?,?,{','.join('?' * len(cols))})",  # nosec B608
                         (fid, aid, str(p), rel, t, *cols.values()))
            ids.append(fid)
    return work, aid, ids


def _trash_rows(status=None):
    rows = db.all("SELECT * FROM file_trash ORDER BY trashed_at,id")
    return [r for r in rows if status is None or r["status"] == status]


def _parts(rt, folder, n=3, size=1000, dur=600, bitrate=128000, salt=b"", skip=()):
    out = []
    for i in range(1, n + 1):
        if i in skip:
            continue
        out.append((f"{folder}/{i:02d}.mp3", (b"%s-%s-%d-" % (salt, folder.encode(), i)) * (size // 10), {"part_number": i, "duration_seconds": dur, "bitrate": bitrate}))
    return out


# ---- analysis: byte-identical ----------------------------------------------------------------------

def test_identical_ebook_copies_are_one_group_with_a_keep_and_a_remove(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"same book" * 100
    w, aid, (f1, f2) = _book(rt, files=[("A/Dune.epub", data, {}), ("B/Dune (1).epub", data, {})])
    out = redundantfiles.analyse(w)
    (g,) = out["groups"]
    assert g["code"] == "identical" and g["checksum_identical"] is True and g["requires_all"] is False and g["asset_id"] == aid
    assert {g["keep"]["id"], g["remove"][0]["id"]} == {f1, f2} and len(g["remove"]) == 1 and g["remove"][0]["keeper_id"] == g["keep"]["id"]
    assert g["remove"][0]["file_will_move"] is True and g["remove"][0]["trash_dir"].endswith(".bookdoe-trash/" + datetime.fromtimestamp(db.now(), tz=timezone.utc).strftime("%Y-%m-%d"))
    assert out["totals"]["bytes_freed"] == 900 and out["totals"]["identical_files"] == 1 and out["totals"]["similar_files"] == 0


def test_files_without_a_checksum_are_never_matched_on_size_alone(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, *_ = _book(rt, files=[("a.epub", b"q" * 50, {"sha256": None}), ("b.epub", b"q" * 50, {"sha256": None})])
    assert redundantfiles.analyse(w)["groups"] == []
    w2, *_ = _book(rt, "Emma", "audiobook", "mp3", [("c1.mp3", b"1" * 70, {"sha256": None, "duration_seconds": 100}), ("c2.mp3", b"2" * 70, {"sha256": None, "duration_seconds": 100})])
    assert redundantfiles.analyse(w2)["groups"] == []


def test_a_converted_copy_does_not_make_its_original_redundant(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, src, _ = _book(rt, "Spoken", "audiobook", "mp3", [("s/1.mp3", b"m" * 300, {"part_number": 1}), ("s/2.mp3", b"n" * 300, {"part_number": 2})])
    cf.add_derived(src, "m4b", rt["path"] / "s" / "Spoken.m4b")
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and [n["code"] for n in out["notes"]] == ["original_kept"]


def test_missing_and_empty_files_are_never_offered_or_kept(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, aid, (f1, f2) = _book(rt, files=[("a.epub", b"q" * 50, {}), ("b.epub", b"q" * 50, {})])
    (rt["path"] / "a.epub").unlink()
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and {i["why"] for i in out["ignored"]} == {"missing"}


# ---- analysis: audio is default-deny ---------------------------------------------------------------

def test_distinct_audio_files_are_never_flagged(tmp_path):
    rt = cf.root(tmp_path, "managed")
    # chapters without part numbers, equal duration, sizes within 0.5%: only a checksum could say they are the same
    files = [(f"ch{n}.mp3", bytes([n]) * (1000 + n), {"duration_seconds": 600}) for n in range(1, 6)]
    w, *_ = _book(rt, "Dune", "audiobook", "mp3", files)
    assert redundantfiles.analyse(w)["groups"] == []
    # numbered parts of one set differ in part number: never matched with each other
    w2, *_ = _book(rt, "Emma", "audiobook", "mp3", _parts(rt, "emma", 4))
    assert redundantfiles.analyse(w2)["groups"] == []


def test_matching_is_never_transitive(tmp_path):
    rt = cf.root(tmp_path, "managed")
    files = _parts(rt, "set1", 2, dur=600) + [("loose/x.mp3", b"loose" * 200, {"duration_seconds": 600})]    # un-numbered, same duration/size as both parts
    w, *_ = _book(rt, "Dune", "audiobook", "mp3", files)
    assert redundantfiles.analyse(w)["groups"] == []


def test_a_set_is_only_matched_against_a_complete_set_of_the_same_parts(tmp_path):
    rt = cf.root(tmp_path, "managed")
    # abridged (2 parts) vs unabridged (3 parts)
    w, *_ = _book(rt, "Dune", "audiobook", "mp3", _parts(rt, "unabridged", 3) + _parts(rt, "abridged", 3, skip=(3,)))
    assert redundantfiles.analyse(w)["groups"] == []
    # same part numbers but one duration differs by more than a second
    other = _parts(rt, "cut", 3)
    other[1] = (other[1][0], other[1][1], {**other[1][2], "duration_seconds": 640})
    w2, *_ = _book(rt, "Emma", "audiobook", "mp3", _parts(rt, "full", 3, salt=b"e") + other)
    assert redundantfiles.analyse(w2)["groups"] == []
    # a stray extra file in one folder makes that set incomplete
    w3, *_ = _book(rt, "Ulysses", "audiobook", "mp3", _parts(rt, "u1", 2) + _parts(rt, "u2", 2) + [("u2/extra.mp3", b"zz" * 80, {})])
    assert redundantfiles.analyse(w3)["groups"] == []


def test_two_complete_sets_are_one_similar_group_that_is_removed_whole_and_only_when_confirmed(tmp_path):
    rt = cf.root(tmp_path, "managed")
    files = _parts(rt, "edA", 3, size=1000, salt=b"A", bitrate=128000) + _parts(rt, "edB", 3, size=1003, salt=b"B", bitrate=128000)
    w, aid, ids = _book(rt, "Dune", "audiobook", "mp3", files)
    out = redundantfiles.analyse(w)
    (g,) = out["groups"]
    assert g["code"] == "similar" and g["checksum_identical"] is False and g["requires_all"] is True and len(g["remove"]) == 3 and len(g["keeps"]) == 3
    assert {r["keeper_id"] for r in g["remove"]} <= {k["id"] for k in g["keeps"]}
    assert "Keep both" in g["recommendation"] and out["totals"]["similar_files"] == 3 and out["totals"]["identical_files"] == 0
    rm = [r["id"] for r in g["remove"]]
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, rm[:2], None, confirm_similar=True)
    assert exc.value.code == "whole_set_required"
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, rm)
    assert exc.value.code == "confirmation_required" and exc.value.detail["needs"] == "similar"
    assert _trash_rows() == []
    done = filetrash.remove_redundant(w, rm, None, confirm_similar=True)
    assert len(done["removed"]) == 3
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 3
    assert all((rt["path"] / k["folder"] / k["name"]).exists() if False else Path(k["path"]).exists() for k in g["keeps"])


def test_a_lower_bitrate_set_is_labelled_lower_quality_and_loses(tmp_path):
    rt = cf.root(tmp_path, "managed")
    low = [(rel, b"l" * 400 + bytes([i]), {**x, "bitrate": 32000}) for i, (rel, _d, x) in enumerate(_parts(rt, "low", 2), 1)]
    w, *_ = _book(rt, "Dune", "audiobook", "mp3", _parts(rt, "high", 2, size=1000, bitrate=128000) + low)
    (g,) = redundantfiles.analyse(w)["groups"]
    assert g["code"] == "lower_quality" and all("/high/" in k["path"] for k in g["keeps"]) and all("/low/" in r["path"] for r in g["remove"])


def test_twelve_track_audiobook_with_one_track_twice_offers_exactly_that_track(tmp_path):
    rt = cf.root(tmp_path, "managed")
    files = _parts(rt, "book", 12)
    dup = (f"book/05 (copy).mp3", files[4][1], dict(files[4][2]))
    w, aid, ids = _book(rt, "Dune", "audiobook", "mp3", files + [dup])
    (g,) = redundantfiles.analyse(w)["groups"]
    assert g["code"] == "identical" and len(g["remove"]) == 1 and g["keep"]["part_number"] == 5
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    with db.session() as conn:
        assert [r[0] for r in conn.execute("SELECT part_number FROM media_files WHERE asset_id=? ORDER BY part_number", (aid,))] == list(range(1, 13))
    # the guard counts per part: a keeper that is another part never satisfies the removal
    w2, aid2, ids2 = _book(rt, "Emma", "audiobook", "mp3", _parts(rt, "e", 3) + [("e/02b.mp3", _parts(rt, "e", 3)[1][1], dict(_parts(rt, "e", 3)[1][2]))])
    g2 = redundantfiles.analyse(w2)["groups"][0]
    wrong = dict(g2, remove=[dict(g2["remove"][0], keeper_id=ids2[2])])      # the keeper is part 3
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redundantfiles, "analyse", lambda wid, **k: {"groups": [wrong], "ignored": [], "notes": [], "totals": {}})
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w2, [g2["remove"][0]["id"]])
    assert exc.value.code == "last_copy"


# ---- symlinks and hard links -------------------------------------------------------------------------

def test_a_symlink_copy_is_never_the_kept_copy_and_the_real_file_is_never_removed(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"real book" * 50
    real = _file(rt, "A/Dune.epub", data)
    (rt["path"] / "B").mkdir()
    os.symlink(real, rt["path"] / "B" / "Dune.epub")
    w, aid, (fa, fb) = _book(rt, files=[("A/Dune.epub", data, {}), ("B/Dune.epub", data, {})])
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and {i["why"] for i in out["ignored"]} == {"symlink"}
    for fid in (fa, fb):
        with pytest.raises(filetrash.TrashError):
            filetrash.remove_redundant(w, [fid])
    assert real.read_bytes() == data and not _trash_rows()
    # even if some caller forged an analysis that keeps the symlink and removes the real file, the disk check refuses
    forged = {"groups": [{"id": fb, "asset_id": aid, "kind": "ebook", "format": "epub", "code": "identical", "checksum_identical": True, "requires_all": False, "reason": "x",
                          "keep": {"id": fb}, "keeps": [{"id": fb}], "remove": [{"id": fa, "keeper_id": fb, "file_will_move": True}]}], "ignored": [], "notes": [], "totals": {}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redundantfiles, "analyse", lambda wid, **k: forged)
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w, [fa])
    assert exc.value.code == "last_copy" and real.read_bytes() == data and not _trash_rows()


def test_a_record_inside_a_symlinked_folder_is_ignored(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"x" * 90
    _file(rt, "A/Dune.epub", data)
    os.symlink(rt["path"] / "A", rt["path"] / "B", target_is_directory=True)
    w, *_ = _book(rt, files=[("A/Dune.epub", data, {}), ("B/Dune.epub", data, {})])
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and {i["why"] for i in out["ignored"]} == {"symlinked_folder"}


def test_hard_links_are_one_physical_file_and_never_offered(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"linked" * 40
    real = _file(rt, "A/Dune.epub", data)
    (rt["path"] / "B").mkdir()
    os.link(real, rt["path"] / "B" / "Dune.epub")
    w, aid, (fa, fb) = _book(rt, files=[("A/Dune.epub", data, {}), ("B/Dune.epub", data, {})])
    out = redundantfiles.analyse(w)
    assert out["groups"] == [] and {i["why"] for i in out["ignored"]} == {"hardlinked"}
    # a forged offer of one name (keeping the other) is refused by the inode check
    forged = {"groups": [{"id": fb, "asset_id": aid, "kind": "ebook", "format": "epub", "code": "identical", "checksum_identical": True, "requires_all": False, "reason": "x",
                          "keep": {"id": fb}, "keeps": [{"id": fb}], "remove": [{"id": fa, "keeper_id": fb, "file_will_move": True}]}], "ignored": [], "notes": [], "totals": {}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redundantfiles, "analyse", lambda wid, **k: forged)
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w, [fa])
    assert exc.value.code == "unsafe_file" and (rt["path"] / "A" / "Dune.epub").exists() and (rt["path"] / "B" / "Dune.epub").exists()


# ---- removal into the trash ----------------------------------------------------------------------

def _pair(tmp_path, mode="managed", title="Dune", data=b"E" * 400, rels=("Frank/Dune/Dune.epub", "Frank/Dune/copy/Dune.epub")):
    rt = cf.root(tmp_path, mode)
    w, aid, ids = _book(rt, title, files=[(rels[0], data, {}), (rels[1], data, {})])
    return rt, w, aid, ids, redundantfiles.analyse(w)["groups"][0]


def test_removal_moves_the_file_into_the_roots_trash_keeps_the_relative_path_and_audits_each_file(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    out = filetrash.remove_redundant(w, [victim["id"]], {"id": "admin-1"})
    assert out["bytes_moved"] == 400 and out["bytes_record_only"] == 0 and out["retention_days"] == 30
    (row,) = _trash_rows()
    rel = Path(victim["path"]).relative_to(rt["path"])
    assert Path(row["trash_path"]).is_relative_to(rt["path"].resolve() / ".bookdoe-trash") and str(row["trash_path"]).endswith(str(rel))
    assert Path(row["trash_path"]).read_bytes() == b"E" * 400 and not Path(victim["path"]).exists() and Path(g["keep"]["path"]).exists()
    assert row["status"] == "trashed" and row["record_only"] == 0 and row["size_bytes"] == 400 and row["trashed_by"] == "admin-1"
    assert abs((row["expires_at"] - row["trashed_at"]) - 30 * DAY) < 1
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM media_files WHERE id=?", (victim["id"],)).fetchone() is None
        batch = conn.execute("SELECT * FROM history WHERE event_type='files.trashed'").fetchone()
        per_file = conn.execute("SELECT * FROM history WHERE event_type='file.trashed'").fetchall()
    assert json.loads(batch["data_json"])["bytes_moved"] == 400 and len(per_file) == 1
    d = json.loads(per_file[0]["data_json"])
    assert d["file_id"] == victim["id"] and d["trash_path"] == row["trash_path"] and d["by"] == "admin-1" and d["size_bytes"] == 400
    assert redundantfiles.analyse(w)["groups"] == []
    again = filetrash.remove_redundant(w, [victim["id"]], None)
    assert again["removed"] == [] and again["already_removed"] == [victim["id"]] and len(_trash_rows()) == 1
    assert filetrash.list_trash()["totals"]["bytes_in_trash"] == 400


def test_ten_or_more_files_need_the_count_typed(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"same" * 50
    w, aid, ids = _book(rt, files=[(f"c{n}/Dune.epub", data, {}) for n in range(11)])
    g = redundantfiles.analyse(w)["groups"][0]
    rm = [r["id"] for r in g["remove"]]
    assert len(rm) == 10
    for bad in (None, 9, 11):
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w, rm, None, confirm_count=bad)
        assert exc.value.code == "confirmation_required" and exc.value.detail["needs"] == "count"
    assert not _trash_rows()
    assert len(filetrash.remove_redundant(w, rm, None, confirm_count=10)["removed"]) == 10
    assert len(db.all("SELECT 1 FROM history WHERE event_type='file.trashed'")) == 10


def test_the_trash_folder_is_not_scanned_as_library_content(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    seen = [p for p in paths.scan_safe(rt["path"])]
    assert seen and not any(".bookdoe-trash" in p.parts for p in seen)
    # a file symlink into the trash is not library content either
    trashed = Path(_trash_rows()[0]["trash_path"])
    os.symlink(trashed, rt["path"] / "sneaky.epub")
    assert not any(p.name == "sneaky.epub" for p in paths.scan_safe(rt["path"]))


def test_a_symlinked_trash_folder_is_refused_and_nothing_moves(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    shutil.rmtree(rt["path"] / ".bookdoe-trash", ignore_errors=True)       # (the analysis probe made a real one)
    os.symlink(rt["path"], rt["path"] / ".bookdoe-trash", target_is_directory=True)    # trash -> the library itself
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [victim["id"]])
    assert exc.value.code == "unsafe_trash_folder" and Path(victim["path"]).exists() and not _trash_rows("trashed")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 2


def test_a_symlinked_dated_folder_inside_the_trash_is_refused_and_rolled_back(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    day = datetime.fromtimestamp(db.now(), tz=timezone.utc).strftime("%Y-%m-%d")
    (rt["path"] / ".bookdoe-trash").mkdir(exist_ok=True)
    os.symlink(rt["path"], rt["path"] / ".bookdoe-trash" / day, target_is_directory=True)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [victim["id"]])
    assert exc.value.code == "unsafe_trash_folder" and Path(victim["path"]).exists()
    assert [r["status"] for r in _trash_rows()] == ["reverted"]
    assert not any(p.name == "Dune.epub" and "Frank/Dune/copy" not in str(p) and p.parent == rt["path"] for p in rt["path"].iterdir())


def test_restore_puts_file_and_record_back_and_is_idempotent(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"R" * 70)
    victim = g["remove"][0]
    filetrash.remove_redundant(w, [victim["id"]])
    (row,) = _trash_rows()
    out = filetrash.restore(row["id"], {"id": "admin-1"})
    assert out["status"] == "restored" and Path(victim["path"]).read_bytes() == b"R" * 70
    with db.session() as conn:
        restored = conn.execute("SELECT * FROM media_files WHERE id=?", (victim["id"],)).fetchone()
        assert restored["path"] == victim["path"] and restored["asset_id"] == aid
        assert conn.execute("SELECT COUNT(*) FROM history WHERE event_type='files.restored'").fetchone()[0] == 1
    assert filetrash.restore(row["id"])["already_restored"] is True
    assert not any((rt["path"] / ".bookdoe-trash").rglob("*.epub"))


def test_restore_refuses_to_overwrite_and_when_the_book_is_gone(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"R" * 70)
    victim = g["remove"][0]
    filetrash.remove_redundant(w, [victim["id"]])
    (row,) = _trash_rows()
    Path(victim["path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(victim["path"]).write_bytes(b"someone else's file")
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.restore(row["id"])
    assert exc.value.code == "destination_exists" and Path(victim["path"]).read_bytes() == b"someone else's file" and Path(row["trash_path"]).exists()
    Path(victim["path"]).unlink()
    with db.tx() as conn:
        conn.execute("DELETE FROM works WHERE id=?", (w,))
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.restore(row["id"])
    assert exc.value.code == "work_gone"


def test_two_restores_racing_for_one_path_have_exactly_one_winner_and_lose_no_data(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"RACE" * 30)
    victim = g["remove"][0]
    filetrash.remove_redundant(w, [victim["id"]])
    (row,) = _trash_rows()
    # a second trash entry for the same original path, holding different bytes (an older removal)
    twin = Path(row["trash_path"]).with_name("Dune.epub.twin")
    twin.write_bytes(b"OLDER" * 30)
    file_json = json.loads(row["file_json"])
    file_json.update(id=db.uid())
    with db.tx() as conn:
        conn.execute("INSERT INTO file_trash(id,batch_id,root_id,root_path,work_id,asset_id,asset_kind,asset_format,original_path,relative_path,trash_path,size_bytes,record_only,file_json,status,trashed_at,expires_at) "
                     "VALUES ('twin','b',?,?,?,?,?,?,?,?,?,150,0,?,'trashed',1,?)",
                     (row["root_id"], row["root_path"], row["work_id"], row["asset_id"], row["asset_kind"], row["asset_format"], row["original_path"], row["relative_path"], str(twin),
                      json.dumps(file_json), db.now() + DAY))
    barrier, results = threading.Barrier(2), {}

    def go(tid):
        barrier.wait()
        try:
            results[tid] = filetrash.restore(tid)["status"]
        except filetrash.TrashError as exc:
            results[tid] = exc.code
    threads = [threading.Thread(target=go, args=(t,)) for t in (row["id"], "twin")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    won = [t for t, r in results.items() if r == "restored"]
    assert len(won) == 1, results
    lost = [t for t in results if t not in won][0]
    assert results[lost] in ("destination_exists", "path_in_use", "not_trashed")
    survivor = Path(victim["path"]).read_bytes()
    assert survivor in (b"RACE" * 30, b"OLDER" * 30)
    loser_file = Path(row["trash_path"]) if lost == row["id"] else twin
    assert loser_file.exists() and loser_file.read_bytes() != survivor           # the loser's data is untouched in the trash
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE path=?", (victim["path"],)).fetchone()[0] == 1


def test_the_same_entry_restored_twice_at_once_restores_once(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"ONCE" * 30)
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    (row,) = _trash_rows()
    barrier, results = threading.Barrier(2), []

    def go():
        barrier.wait()
        try:
            results.append(filetrash.restore(row["id"]).get("already_restored"))
        except filetrash.TrashError as exc:
            results.append(exc.code)
    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert Path(g["remove"][0]["path"]).read_bytes() == b"ONCE" * 30
    assert results.count(False) == 1, results
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 2


def test_move_no_clobber_never_replaces_and_never_copies(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    with pytest.raises(FileExistsError):
        filetrash.move_no_clobber(a, b)
    assert a.read_bytes() == b"AAA" and b.read_bytes() == b"BBB"
    monkeypatch.setattr(os, "link", lambda *x, **k: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device link")))
    c = tmp_path / "c"
    with pytest.raises(filetrash.LinksUnsupported):
        filetrash.move_no_clobber(a, c)                       # no copy fallback any more
    assert a.read_bytes() == b"AAA" and not c.exists()


def test_purge_deletes_only_expired_files_and_restore_is_then_impossible(tmp_path):
    rt = cf.root(tmp_path, "managed")
    items = []
    for title, ch in (("One", b"1"), ("Two", b"2")):
        w, _, _ = _book(rt, title, files=[(f"{title}/a.epub", ch * 30, {}), (f"{title}/b.epub", ch * 30, {})])
        items.append(w)
        filetrash.remove_redundant(w, [redundantfiles.analyse(w)["groups"][0]["remove"][0]["id"]])
    rows = {r["work_id"]: r for r in _trash_rows()}
    w1, w2 = items
    with db.tx() as conn:
        conn.execute("UPDATE file_trash SET expires_at=? WHERE id=?", (db.now() - 5, rows[w1]["id"]))
    out = filetrash.purge()
    assert out["purged"] == 1 and out["bytes_freed"] == 30 and not Path(rows[w1]["trash_path"]).exists() and Path(rows[w2]["trash_path"]).exists()
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.restore(rows[w1]["id"])
    assert exc.value.code == "purged" and filetrash.purge()["purged"] == 0
    out = filetrash.purge([rows[w2]["id"]], user={"id": "admin-1"})
    assert out["purged"] == 1 and not Path(rows[w2]["trash_path"]).exists()
    with db.session() as conn:
        evs = conn.execute("SELECT data_json FROM history WHERE event_type='files.purged' ORDER BY created_at").fetchall()
    assert len(evs) == 2 and json.loads(evs[1][0])["by"] == "admin-1"


def test_trash_of_a_root_that_became_adopted_is_never_purged(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    (row,) = _trash_rows()
    with db.tx() as conn:
        conn.execute("UPDATE root_folders SET management_mode='adopt' WHERE id=?", (rt["id"],))
        conn.execute("UPDATE file_trash SET expires_at=1")
    out = filetrash.purge()
    assert out["purged"] == 0 and out["skipped"] and Path(row["trash_path"]).exists()


def test_the_retention_is_configurable_and_the_purge_job_uses_it(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    assert filetrash.retention_days() == 30 and filetrash.set_retention_days(7) == 7
    filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    (row,) = _trash_rows()
    assert abs((row["expires_at"] - row["trashed_at"]) - 7 * DAY) < 1
    for bad in (0, -1, 100000, True, "5"):
        with pytest.raises(filetrash.TrashError):
            filetrash.set_retention_days(bad)
    with db.tx() as conn:
        conn.execute("UPDATE file_trash SET expires_at=?", (db.now() - 1,))
    job = db.one("SELECT * FROM jobs WHERE kind='trash-purge'")
    assert job and job["schedule_seconds"] == DAY
    scheduler.run_job(job["id"])
    assert db.one("SELECT status FROM file_trash")["status"] == "purged"


def test_a_forged_trash_path_is_never_deleted_or_restored(tmp_path):
    rt = cf.root(tmp_path, "managed")
    decoy = tmp_path / "outside" / "precious.txt"
    decoy.parent.mkdir()
    decoy.write_text("keep me")
    with db.tx() as conn:
        conn.execute("INSERT INTO file_trash(id,batch_id,root_id,root_path,original_path,relative_path,trash_path,size_bytes,file_json,status,trashed_at,expires_at) "
                     "VALUES ('t1','b',?,?,?,?,?,7,'{}','trashed',1,2)", (rt["id"], str(rt["path"]), str(rt["path"] / "x.epub"), "x.epub", str(decoy)))
    out = filetrash.purge()
    assert out["purged"] == 0 and out["skipped"] and decoy.read_text() == "keep me"
    with pytest.raises(filetrash.TrashError):
        filetrash.restore("t1")
    assert decoy.exists()


# ---- crash safety: the log row comes first ---------------------------------------------------------

def test_a_failure_after_the_move_puts_the_file_back_and_marks_the_row_reverted(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]

    def boom(*a, **k):
        raise RuntimeError("injected after the move")
    monkeypatch.setattr(filetrash, "_finalize", boom)
    with pytest.raises(RuntimeError, match="injected"):
        filetrash.remove_redundant(w, [victim["id"]])
    assert Path(victim["path"]).read_bytes() == b"E" * 400
    assert [r["status"] for r in _trash_rows()] == ["reverted"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_files WHERE asset_id=?", (aid,)).fetchone()[0] == 2
    assert not any((rt["path"] / ".bookdoe-trash").rglob("*.epub"))


def test_a_failed_move_back_leaves_a_flagged_moving_row_that_reconcile_puts_back_never_completes(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    real_move = filetrash.move_no_clobber
    calls = []

    def flaky(src, dst, *a):
        calls.append(1)
        if len(calls) >= 2:
            raise OSError("disk went away")           # the move back fails
        return real_move(src, dst, *a)
    monkeypatch.setattr(filetrash, "_finalize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    monkeypatch.setattr(filetrash, "move_no_clobber", flaky)
    with pytest.raises(RuntimeError):
        filetrash.remove_redundant(w, [victim["id"]])
    (row,) = _trash_rows()
    assert row["status"] == "moving" and filetrash.REVERT_FLAG in row["reason"]            # logged, flagged, not lost
    assert Path(row["trash_path"]).exists() and not Path(victim["path"]).exists()
    monkeypatch.setattr(filetrash, "move_no_clobber", real_move)
    out = filetrash.reconcile(grace=0)
    assert out == {"completed": 0, "reverted": 1, "unresolved": 0, "lost": 0}            # the rollback asked for it back: it comes back
    assert _trash_rows()[0]["status"] == "reverted" and Path(victim["path"]).read_bytes() == b"E" * 400
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM media_files WHERE id=?", (victim["id"],)).fetchone()      # the record never lost its file


def test_reconcile_reverts_a_moving_row_whose_file_is_back_at_its_origin(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    real_move = filetrash.move_no_clobber
    n = []
    monkeypatch.setattr(filetrash, "_finalize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
    monkeypatch.setattr(filetrash, "move_no_clobber", lambda s, d, *a: real_move(s, d, *a) if not n.append(1) and len(n) == 1 else (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(RuntimeError):
        filetrash.remove_redundant(w, [victim["id"]])
    (row,) = _trash_rows()
    os.rename(row["trash_path"], victim["path"])                           # an operator puts it back by hand
    assert filetrash.reconcile(grace=0) == {"completed": 0, "reverted": 1, "unresolved": 0, "lost": 0}
    assert _trash_rows()[0]["status"] == "reverted"


def test_a_database_failure_before_any_move_changes_nothing(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path)
    victim = g["remove"][0]
    with db.tx() as conn:
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON file_trash BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception, match="injected"):
        filetrash.remove_redundant(w, [victim["id"]])
    assert Path(victim["path"]).read_bytes() == b"E" * 400


# ---- refusals ------------------------------------------------------------------------------------

def test_adopted_roots_lose_only_the_record_and_say_so(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, mode="adopt", data=b"A" * 90)
    victim = g["remove"][0]
    assert victim["file_will_move"] is False and "only the record" in victim["note"] and victim["trash_dir"] is None
    out = filetrash.remove_redundant(w, [victim["id"]])
    assert out["bytes_moved"] == 0 and out["bytes_record_only"] == 90 and out["removed"][0]["record_only"] is True
    assert Path(victim["path"]).read_bytes() == b"A" * 90 and not (rt["path"] / ".bookdoe-trash").exists()
    (row,) = _trash_rows()
    assert row["record_only"] == 1 and row["trash_path"] is None and row["status"] == "trashed"
    assert filetrash.restore(row["id"])["status"] == "restored"
    with db.session() as conn:
        assert conn.execute("SELECT path FROM media_files WHERE id=?", (victim["id"],)).fetchone()[0] == victim["path"]


def test_files_outside_every_root_are_refused_and_nothing_changes(tmp_path):
    rt = cf.root(tmp_path, "managed")
    stray = tmp_path / "elsewhere" / "Dune.epub"
    stray.parent.mkdir()
    stray.write_bytes(b"S" * 55)
    w, aid, ids = _book(rt, files=[("a/Dune.epub", b"S" * 55, {})])
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,sha256,discovered_at) VALUES ('stray',?,?,55,?,1)", (aid, str(stray), _sha(b"S" * 55)))
    assert redundantfiles.analyse(w)["groups"] == []
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, ["stray"])
    assert exc.value.code == "outside_managed_roots" and stray.exists() and _trash_rows() == []


def test_symlinks_escaping_the_root_are_never_moved(tmp_path):
    rt = cf.root(tmp_path, "managed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dune.epub").write_bytes(b"L" * 66)
    (rt["path"] / "link").symlink_to(outside, target_is_directory=True)
    w, aid, ids = _book(rt, files=[("keep/Dune.epub", b"L" * 66, {})])
    with db.tx() as conn:
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,sha256,discovered_at) VALUES ('linked',?,?,66,?,1)", (aid, str(rt["path"] / "link" / "Dune.epub"), _sha(b"L" * 66)))
    with pytest.raises(filetrash.TrashError):
        filetrash.remove_redundant(w, ["linked"])
    assert (outside / "Dune.epub").exists()


def test_the_last_copy_and_the_keeper_are_never_removed(tmp_path, monkeypatch):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"K" * 20)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["keep"]["id"]])
    assert exc.value.code == "not_redundant"
    for bad, code in (([], "nothing_selected"), (["no-such-file"], "not_redundant")):
        with pytest.raises(filetrash.TrashError) as exc:
            filetrash.remove_redundant(w, bad)
        assert exc.value.code == code
    real = redundantfiles.analyse

    calls = []

    def keeper_vanishes(work_id, **k):
        out = real(work_id, **k)
        calls.append(1)
        if len(calls) >= 2:                      # the first call is the pre-hash outside the lock; vanish after the real analysis
            Path(out["groups"][0]["keep"]["path"]).unlink()
        return out
    monkeypatch.setattr(redundantfiles, "analyse", keeper_vanishes)
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.remove_redundant(w, [g["remove"][0]["id"]])
    assert exc.value.code == "last_copy" and Path(g["remove"][0]["path"]).exists() and _trash_rows() == []


# ---- the real flow: merge two copies, then clean up; undo rules ----------------------------------

def test_merge_then_cleanup_frees_the_duplicate_and_the_undo_waits_for_the_restore(tmp_path):
    rt = cf.root(tmp_path, "managed")
    data = b"BC" * 200
    a, _, (fa,) = _book(rt, "The Black Circle", files=[("Rick/Black Circle/a.epub", data, {})])
    b, _, (fb,) = _book(rt, "The Black Circle (The 39 Clues, #5)", files=[("Rick/Black Circle (The 39 Clues, #5)/b.epub", data, {})])
    res = workmerge.execute(a, [b], {}, None)
    (g,) = redundantfiles.analyse(a)["groups"]
    assert {g["keep"]["id"], g["remove"][0]["id"]} == {fa, fb}
    filetrash.remove_redundant(a, [g["remove"][0]["id"]], {"id": "admin-1"})
    for force in (False, True):
        with pytest.raises(workmerge.ReversalRefused) as exc:
            workmerge.reverse(res["merge_id"], force=force)
        assert exc.value.code == "files_in_trash" and exc.value.detail["files"][0]["path"] == g["remove"][0]["path"]
    with db.session() as conn:
        assert conn.execute("SELECT status FROM work_merges").fetchone()[0] == "executed"
    (row,) = _trash_rows("trashed")
    filetrash.restore(row["id"])
    assert workmerge.reverse(res["merge_id"])["status"] == "reversed"


def test_a_chained_merge_must_be_undone_from_the_latest(tmp_path):
    a, b, c = _work("Alpha", "X"), _work("Alpha", "X"), _work("Alpha", "X")
    m1 = workmerge.execute(a, [b], {}, None)
    m2 = workmerge.execute(c, [a], {}, None)             # the canonical of the first merge is merged away
    for force in (False, True):
        with pytest.raises(workmerge.ReversalRefused) as exc:
            workmerge.reverse(m1["merge_id"], force=force)
        assert exc.value.code == "later_merge" and "Undo that merge first" in exc.value.message
    with db.session() as conn:
        assert conn.execute("SELECT 1 FROM works WHERE id=?", (a,)).fetchone() is None
    assert workmerge.reverse(m2["merge_id"])["status"] == "reversed"
    assert workmerge.reverse(m1["merge_id"])["status"] == "reversed"
    with db.session() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM works")} == {a, b, c}


def test_old_merges_are_pruned_but_recent_ones_and_chains_keep_working(tmp_path):
    a, b = _work("Alpha", "X"), _work("Alpha", "X")
    c, d = _work("Beta", "Y"), _work("Beta", "Y")
    old = workmerge.execute(a, [b], {}, None)
    new = workmerge.execute(c, [d], {}, None)
    assert workmerge.ledger_retention_days() == 365
    with db.tx() as conn:
        conn.execute("UPDATE work_merges SET created_at=? WHERE id=?", (db.now() - 400 * DAY, old["merge_id"]))
    assert workmerge.prune_ledger() == {"merges_pruned": 1}
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger WHERE merge_id=?", (old["merge_id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_merge_ledger WHERE merge_id=?", (new["merge_id"],)).fetchone()[0] == new["ledger_rows"]
    assert workmerge.reverse(new["merge_id"])["status"] == "reversed"
    # a chain is never cut in the middle: an old merge whose canonical a kept merge folded away stays
    e, f, g = _work("Gamma", "Z"), _work("Gamma", "Z"), _work("Gamma", "Z")
    m1 = workmerge.execute(e, [f], {}, None)
    m2 = workmerge.execute(g, [e], {}, None)
    with db.tx() as conn:
        conn.execute("UPDATE work_merges SET created_at=? WHERE id=?", (db.now() - 400 * DAY, m1["merge_id"]))
    assert workmerge.prune_ledger() == {"merges_pruned": 0}
    assert scheduler._housekeeping()["work_merges_pruned"] == 0


# ---- API -----------------------------------------------------------------------------------------

@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_trash_api_end_to_end(tmp_path):
    rt, w, aid, ids, g = _pair(tmp_path, data=b"P" * 100)
    client = TestClient(app)
    r = client.get(f"/api/v1/works/{w}/redundant-files")
    assert r.status_code == 200 and r.json()["totals"]["bytes_freed"] == 100 and r.json()["groups"][0]["checksum_identical"] is True
    victim = r.json()["groups"][0]["remove"][0]["id"]
    assert client.get("/api/v1/works/nope/redundant-files").status_code == 404
    assert client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": []}).status_code == 400
    assert client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": ["zzz"]}).status_code == 409
    r = client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": [victim]})
    assert r.status_code == 200 and r.json()["bytes_moved"] == 100 and r.json()["removed"][0]["trash_dir"].endswith(
        datetime.fromtimestamp(db.now(), tz=timezone.utc).strftime("%Y-%m-%d"))
    t = client.get("/api/v1/trash").json()
    assert t["totals"]["bytes_in_trash"] == 100 and len(t["items"]) == 1
    tid = t["items"][0]["id"]
    assert client.put("/api/v1/trash/settings", json={"retention_days": 14, "ledger_retention_days": 30}).json() == {"retention_days": 14, "ledger_retention_days": 30}
    assert client.put("/api/v1/trash/settings", json={"retention_days": 0}).status_code == 400
    assert client.put("/api/v1/trash/settings", json={"retention_days": 5, "ledger_retention_days": 0}).status_code == 400
    assert client.post("/api/v1/trash/purge", json={}).status_code == 400
    assert client.post(f"/api/v1/trash/{tid}/restore").json()["status"] == "restored"
    assert client.post("/api/v1/trash/nope/restore").status_code == 404
    victim2 = client.get(f"/api/v1/works/{w}/redundant-files").json()["groups"][0]["remove"][0]["id"]
    client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": [victim2]})
    tid2 = client.get("/api/v1/trash").json()["items"][0]["id"]
    r = client.post("/api/v1/trash/purge", json={"ids": [tid2]})
    assert r.status_code == 200 and r.json()["purged"] == 1 and r.json()["bytes_freed"] == 100
    assert client.post(f"/api/v1/trash/{tid2}/restore").status_code == 409


def test_the_api_demands_the_similar_confirmation_and_the_typed_count(tmp_path):
    rt = cf.root(tmp_path, "managed")
    w, aid, ids = _book(rt, "Dune", "audiobook", "mp3", _parts(rt, "edA", 2, salt=b"A") + _parts(rt, "edB", 2, size=1003, salt=b"B"))
    client = TestClient(app)
    g = client.get(f"/api/v1/works/{w}/redundant-files").json()["groups"][0]
    rm = [r["id"] for r in g["remove"]]
    r = client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": rm})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "confirmation_required" and r.json()["detail"]["needs"] == "similar"
    assert client.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": rm, "confirm_similar": True}).status_code == 200


def test_the_write_actions_have_a_per_admin_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(writelimit, "LIMIT", 3)
    client = TestClient(app)
    codes = [client.post("/api/v1/works/merges/nope/reverse", json={}).status_code for _ in range(5)]
    assert codes == [404, 404, 404, 429, 429]
    r = client.post("/api/v1/trash/nope/restore")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    writelimit.reset()
    assert client.post("/api/v1/works/merges/nope/reverse", json={}).status_code == 404


def test_trash_endpoints_are_admin_only_with_csrf(tmp_path, local_mode):
    with TestClient(app) as anon, TestClient(app) as admin, TestClient(app) as adult:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        rt, w, aid, ids, g = _pair(tmp_path, data=b"P" * 100)
        victim = g["remove"][0]["id"]
        assert admin.post("/api/v1/users", json={"username": "jane", "password": "a-very-long-jane-password", "account_type": "adult"}, headers=csrf).status_code == 200
        jane = adult.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        jcsrf = {"X-CSRF-Token": jane.json()["csrf"]}
        calls = [("get", f"/api/v1/works/{w}/redundant-files", None), ("get", "/api/v1/trash", None),
                 ("post", f"/api/v1/works/{w}/redundant-files/remove", {"file_ids": [victim]}), ("post", "/api/v1/trash/x/restore", {}),
                 ("post", "/api/v1/trash/purge", {"expired": True}), ("put", "/api/v1/trash/settings", {"retention_days": 5})]
        for method, url, body in calls:
            kw = {"json": body} if body is not None else {}
            assert getattr(anon, method)(url, **kw).status_code == 401, url
            assert getattr(adult, method)(url, headers=jcsrf, **kw).status_code == 403, url
            if method != "get":
                assert getattr(admin, method)(url, **kw).status_code == 403, url
                assert getattr(admin, method)(url, headers={"X-CSRF-Token": "forged"}, **kw).status_code == 403, url
        assert Path(g["remove"][0]["path"]).exists() and _trash_rows() == []
        assert admin.post(f"/api/v1/works/{w}/redundant-files/remove", json={"file_ids": [victim]}, headers=csrf).status_code == 200
        assert len(_trash_rows()) == 1
