"""Safety review round 2 of the managed library build: one regression test (or more) per finding.

1. Undo of a converted book whose originals are gone is refused (source_gone) and the converted file stays the book's
   file; the trash never purges an Undo-build copy whose original is gone (it may be the only copy).
2. Switching "Allow builds to run" off pauses a running build after the current file.
3. The migration step is 41 (39 and 40 belong to other branches).
4. A build conversion is in conversion._inflight before its 'running' row is committed: reap_orphans never requeues it.
5. Preflight refuses a root whose stored path is not its real path (a symlink in it).
6. 'database is locked' inside a book is retried, never fails the book; a run-level stop is not an attempt; Undo's move
   goes through directory handles, checks the inode and refuses where no no-replace move exists.
The originals in the adopted roots are compared byte for byte wherever a build runs."""
from __future__ import annotations

import contextlib
import errno
import os
import sqlite3
from pathlib import Path

import pytest

from app import db
from app import main as main_module
from app import migrations
from app.services import conversion, filetrash, managedbuild as mb

import conv_fixtures as cf
import mb_fixtures as F


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(mb, "RETRY_BACKOFF_SECONDS", 0.0)
    mb.reset_capability_cache()
    yield
    mb.reset_capability_cache()


@pytest.fixture
def lib(tmp_path):
    return F.library(tmp_path)


def _sources(lib):
    return F.snapshot(lib["books"]["path"], lib["audiobooks"]["path"])


def _items(plan_id):
    return db.all("SELECT * FROM build_plan_items WHERE plan_id=? ORDER BY id", (plan_id,))


def _mobi_book(lib, title="Tiny Book", author="Anne Author"):
    src = cf.make_mobi(lib["books"]["path"] / author / f"{title}.mobi", title=title, author=author)
    w, a = cf.add_book(lib["books"], title, author, "ebook", [src])
    with db.tx() as c:
        c.execute("UPDATE media_files SET adopted=1, managed=0 WHERE asset_id=?", (a,))
    return w, a, src


# ---- 1. Undo never leaves a book without a file ------------------------------------------------------

def test_undo_refuses_a_converted_book_whose_original_is_gone_and_keeps_the_converted_file(lib):
    w, a, src = _mobi_book(lib)
    plan, run = F.build()
    assert run["status"] == "completed", run
    d = db.one("SELECT * FROM media_assets WHERE derived_from_asset_id=?", (a,))
    f = db.one("SELECT * FROM media_files WHERE asset_id=?", (d["id"],))
    os.unlink(src)                                         # the only original is gone
    res = mb.undo(run_id=run["run_id"], confirm_count=1)
    assert res["undone_books"] == 0 and res["undone_files"] == 0
    assert res["problems"][0]["code"] == "source_gone" and str(src) in res["problems"][0]["error"]
    assert db.one("SELECT id FROM media_assets WHERE id=?", (d["id"],))              # the converted book is kept
    assert db.one("SELECT path FROM media_files WHERE id=?", (f["id"],))["path"] == f["path"] and Path(f["path"]).exists()
    assert db.one("SELECT status,undo_state FROM managed_copies") == {"status": "active", "undo_state": None}
    assert db.one("SELECT COUNT(*) n FROM file_trash")["n"] == 0


def test_undo_refuses_a_converted_book_when_any_one_of_its_sources_changed(lib):
    w, a, src = _mobi_book(lib)
    plan, run = F.build()
    with open(src, "ab") as fh:                            # a different size than the saved signature
        fh.write(b"tail")
    res = mb.undo(work_id=w)
    assert res["undone_books"] == 0 and res["problems"][0]["code"] == "source_gone"
    assert db.one("SELECT status FROM managed_copies")["status"] == "active"


def test_the_trash_never_purges_or_hides_an_undo_build_copy_whose_original_is_gone(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    res = mb.undo(work_id=w)
    assert res["undone_files"] == 1
    t = db.one("SELECT * FROM file_trash")
    os.unlink(e)                                           # now the trashed managed copy is the only copy of the book
    out = filetrash.purge(expired_only=False)
    assert out["purged"] == 0 and out["skipped"][0]["id"] == t["id"] and "only file" in out["skipped"][0]["reason"]
    assert Path(t["trash_path"]).exists() and db.one("SELECT status FROM file_trash")["status"] == "trashed"
    with pytest.raises(filetrash.TrashError) as exc:
        filetrash.restore(t["id"])
    assert exc.value.code == "build_undo" and "never purged" in str(exc.value)


def test_the_trash_still_purges_an_undo_build_copy_whose_original_is_in_place(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    mb.undo(work_id=w)
    t = db.one("SELECT * FROM file_trash")
    out = filetrash.purge(expired_only=False)
    assert out["purged"] == 1 and not Path(t["trash_path"]).exists() and e.exists()


# ---- 2. "Allow builds to run" off pauses a running build --------------------------------------------

@pytest.mark.parametrize("how", ["api", "setting"])
def test_switching_runs_off_pauses_a_running_build_after_the_current_file(lib, monkeypatch, how):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.ebook(lib, "Emma", "Jane Austen")
    F.ebook(lib, "Ulysses", "James Joyce")
    before = _sources(lib)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._copy_one
    done = []

    def copy_then_switch_off(*a, **k):
        real(*a, **k)
        done.append(1)
        if len(done) == 1:                                 # the admin switches it off while the first file is copied
            mb.set_run_enabled(False) if how == "api" else F.allow_runs(False)
    monkeypatch.setattr(mb, "_copy_one", copy_then_switch_off)
    out = mb.execute(run["run_id"])
    r = db.one("SELECT * FROM build_runs WHERE id=?", (run["run_id"],))
    assert out["status"] == "paused" and r["status"] == "paused" and "switched off" in r["stop_reason"]
    states = sorted(i["state"] for i in _items(plan["plan_id"]))
    assert states == ["done", "pending", "pending"]        # the current file finished; nothing else started
    assert {i["attempts"] for i in _items(plan["plan_id"]) if i["state"] == "pending"} == {0}
    with pytest.raises(mb.BuildRefused):
        mb.resume(run["run_id"], submit=False)             # still off: Resume refused
    F.allow_runs(True)
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "completed"
    assert _sources(lib) == before


def test_switching_runs_off_pauses_between_the_files_of_one_book(lib, monkeypatch):
    w, a, paths = F.audioset(lib, "Dune", "Frank Herbert", n=3)
    plan = F.plan_and_confirm({"mode": "as_is"})
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._copy_one
    done = []

    def copy_then_switch_off(*a, **k):
        real(*a, **k)
        done.append(1)
        if len(done) == 1:
            F.allow_runs(False)
    monkeypatch.setattr(mb, "_copy_one", copy_then_switch_off)
    mb.execute(run["run_id"])
    assert db.one("SELECT status FROM build_runs WHERE id=?", (run["run_id"],))["status"] == "paused"
    assert len(done) == 1                                  # stopped after the current file
    assert db.one("SELECT COUNT(*) n FROM managed_copies")["n"] == 0      # the book was not switched half-way
    F.allow_runs(True)
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "completed"
    assert db.one("SELECT COUNT(*) n FROM managed_copies")["n"] == 3


def test_the_switch_text_says_turning_it_off_pauses_a_running_build():
    js = (Path(main_module.__file__).parent / "static" / "managedbuild.js").read_text()
    assert "Turning this off pauses a running build after the current file" in js
    assert "builds cannot start or resume." not in js      # the old text that claimed nothing about a running build


# ---- 3. Migration step 41 ------------------------------------------------------------------------------

def test_the_managed_build_migration_is_step_41_after_discover_chat_and_the_duplicate_finder():
    steps = {n: v for v, n, _ in migrations.MIGRATIONS}
    assert (steps["discover_chat"], steps["duplicate_finder"], steps["managed_build"]) == (39, 40, 41)
    assert dict(migrations.VALIDATIONS).get(41) is migrations._validate_managed_build
    assert [v for v, _, _ in migrations.MIGRATIONS][38:41] == [39, 40, 41]


def test_a_fresh_database_with_the_gap_at_39_and_40_records_step_41():
    rows = {r["version"] for r in db.all("SELECT version FROM migration_steps")}
    assert 41 in rows
    assert {"build_plan_items", "build_runs", "managed_copies"} <= {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type='table'")}


# ---- 4. reap_orphans never takes a build conversion ---------------------------------------------------

def test_reap_orphans_right_after_the_build_commits_its_running_conversion_does_not_requeue_it(lib, monkeypatch):
    w, a, src = _mobi_book(lib)
    real_tx = db.tx
    reaped: list[int] = []
    busy = {"on": False}

    @contextlib.contextmanager
    def tx():
        with real_tx() as c:
            yield c
        if busy["on"] or reaped:
            return
        if db.one("SELECT id FROM conversions WHERE trigger_source='build' AND status='running'"):
            busy["on"] = True                              # the conversion worker's startup sweep runs at this very moment
            try:
                reaped.append(conversion.reap_orphans())
            finally:
                busy["on"] = False
    monkeypatch.setattr(db, "tx", tx)
    plan, run = F.build()
    monkeypatch.setattr(db, "tx", real_tx)
    assert reaped == [0], reaped
    assert run["status"] == "completed", run
    conv = db.one("SELECT status,error FROM conversions WHERE trigger_source='build'")
    assert conv == {"status": "done", "error": None}
    assert not conversion._inflight


# ---- 5. Preflight refuses a root reached through a symlink --------------------------------------------

@pytest.mark.parametrize("which", ["lib_e", "books"])
def test_preflight_refuses_a_root_whose_stored_path_goes_through_a_symlink(lib, which):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    before = _sources(lib)
    plan = F.plan_and_confirm()
    alias = lib["tmp"] / "alias"
    alias.symlink_to(lib["tmp"], target_is_directory=True)
    linked = alias / lib[which]["path"].name
    assert linked.is_dir() and not linked.is_symlink()     # the root folder itself is not a link; a folder above it is
    with db.tx() as c:
        c.execute("UPDATE root_folders SET path=? WHERE id=?", (str(linked), lib[which]["id"]))
    problems = mb.preflight(plan["plan_id"])
    assert any("symbolic link" in p and str(lib[which]["path"]) in p and "Edit this library root" in p for p in problems), problems
    with pytest.raises(mb.BuildRefused) as exc:
        mb.start(plan["plan_id"], submit=False)
    assert exc.value.code == "preflight"
    assert F.managed_files(lib) == [] and _sources(lib) == before


def test_preflight_accepts_roots_stored_at_their_real_path(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    assert mb.preflight(plan["plan_id"]) == []


# ---- 6. Transient database errors, run-level stops, Undo's move ---------------------------------------

def test_a_database_locked_error_inside_a_book_is_retried_and_never_fails_the_book(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    before = _sources(lib)
    real = mb._copy_one
    n = {"calls": 0}

    def locked_a_few_times(*args, **kw):
        n["calls"] += 1
        if n["calls"] <= mb.MAX_ATTEMPTS + 2:              # more often than the attempt budget
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kw)
    monkeypatch.setattr(mb, "_copy_one", locked_a_few_times)
    plan, run = F.build()
    assert run["status"] == "completed", run
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "done" and it["attempts"] == 1
    assert db.one("SELECT path FROM media_files")["path"].startswith(str(lib["lib_e"]["path"]))
    assert _sources(lib) == before


def test_a_non_transient_database_error_in_a_book_still_fails_it_and_stops_the_run(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    monkeypatch.setattr(mb, "_copy_one", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such column: x")))
    plan, run = F.build()
    assert run["status"] == "stopped" and _items(plan["plan_id"])[0]["state"] == "failed"


@pytest.mark.parametrize("err", ["no_space", "publish"])
def test_a_run_level_stop_is_not_an_attempt_of_the_book(lib, monkeypatch, err):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._copy_one

    def stop_run(*a, **k):
        if err == "no_space":
            raise OSError(errno.ENOSPC, "No space left on device")
        raise mb.PublishUnsupported(mb.UNSUPPORTED_MESSAGE)
    monkeypatch.setattr(mb, "_copy_one", stop_run)
    for _ in range(mb.MAX_ATTEMPTS + 1):                   # stopped (and resumed) more often than the attempt budget
        mb.execute(run["run_id"])
        r = db.one("SELECT status FROM build_runs WHERE id=?", (run["run_id"],))
        assert r["status"] == "stopped"
        it = _items(plan["plan_id"])[0]
        assert it["state"] == "pending" and it["attempts"] == 0
        mb.resume(run["run_id"], submit=False)
    monkeypatch.setattr(mb, "_copy_one", real)
    mb.execute(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "completed"


def test_undo_refuses_to_move_a_copy_where_neither_links_nor_a_no_replace_rename_work(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]

    def no_links(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_links)
    monkeypatch.setattr(mb, "_renameat2_noreplace", lambda *a, **k: False)
    monkeypatch.setattr(filetrash, "_renameat2_noreplace", lambda *a, **k: False)
    res = mb.undo(work_id=w)
    assert res["undone_files"] == 0 and res["problems"][0]["code"] == "move_failed" and "neither hard links" in res["problems"][0]["error"]
    assert copy.exists() and db.one("SELECT COUNT(*) n FROM file_trash")["n"] == 0
    assert db.one("SELECT undo_state FROM managed_copies")["undo_state"] == "failed"
    assert mb.is_build_path(str(copy))
    assert db.one("SELECT path FROM media_files")["path"] == str(e) and e.exists()


def test_undo_moves_by_rename_through_handles_where_links_are_unsupported(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]
    ino = copy.stat().st_ino

    def no_links(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_links)
    res = mb.undo(work_id=w)
    if res["problems"]:                                    # this filesystem has no renameat2 either: refused, left in place
        assert res["problems"][0]["code"] == "move_failed" and copy.exists()
        return
    t = db.one("SELECT * FROM file_trash")
    assert not copy.exists() and Path(t["trash_path"]).stat().st_ino == ino
    assert not copy.parent.exists()                        # the emptied folder was pruned (through handles)


def test_the_undo_move_checks_the_inode_and_never_follows_a_swapped_folder(lib, tmp_path):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]
    root_real = os.path.realpath(lib["lib_e"]["path"])
    st = copy.stat()
    trash = os.path.join(root_real, ".bookdoe-trash", "2026-10-14", "Frank Herbert", "Dune", "Dune.epub")
    with pytest.raises(filetrash.MoveAborted):                                       # not the expected file
        mb._move_to_trash(root_real, str(copy), trash, (st.st_dev, st.st_ino + 1))
    assert copy.exists() and not os.path.lexists(trash)
    # a folder of the copy swapped for a symlink to another place: refused, nothing there is touched
    outside = tmp_path / "outside" / "Dune"
    outside.mkdir(parents=True)
    victim = outside / copy.name
    victim.write_bytes(b"not ours")
    real_dir = copy.parent.with_name("Dune-real")
    os.rename(copy.parent, real_dir)
    copy.parent.symlink_to(outside, target_is_directory=True)
    vst = victim.stat()
    with pytest.raises(filetrash.MoveAborted):
        mb._move_to_trash(root_real, str(copy), trash, (vst.st_dev, vst.st_ino))
    assert victim.read_bytes() == b"not ours" and not os.path.lexists(trash)
