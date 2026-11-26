"""The managed library build copy engine (#222, #224): temp file, verify, no-clobber publish, crash recovery, throttling,
pause/resume/cancel, concurrency, fail-closed publishing. Real temp roots and real files throughout; the adopted roots
are compared byte for byte (hash, size, mtime, inode, folder listings) before and after every scenario."""
from __future__ import annotations

import builtins
import errno
import os
import shutil
import stat
import threading
from pathlib import Path

import pytest

from app import db
from app.services import managedbuild as mb

import mb_fixtures as F
from mb_fixtures import Crash


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


def _paths():
    return sorted(r["path"] for r in db.all("SELECT path FROM media_files"))


# ---- the happy path ---------------------------------------------------------------------------------

def test_copy_verifies_publishes_and_switches_records_while_originals_stay_byte_identical(lib):
    w1, a1, e = F.ebook(lib, "Dune", "Frank Herbert")
    w2, a2, tracks = F.audioset(lib, "Big Book", "Ann Author", n=4)
    ids_before = {r["id"]: r["path"] for r in db.all("SELECT id,path FROM media_files")}
    before = _sources(lib)

    plan, run = F.build()

    assert run["status"] == "completed" and run["counts"] == {"done": 5}
    assert F.sha(lib["lib_e"]["path"] / "Frank Herbert" / "Dune" / "Dune.epub") == F.sha(e)
    for i, t in enumerate(tracks, 1):
        assert F.sha(lib["lib_a"]["path"] / "Ann Author" / "Big Book" / f"{i:02d}.m4b") == F.sha(t)     # a set keeps its file names
    assert _sources(lib) == before                       # hash, size, mtime, inode, mode, nlink, folder listings
    after = {r["id"]: r["path"] for r in db.all("SELECT id,path FROM media_files")}
    assert set(after) == set(ids_before)                 # same ids: nothing added, nothing lost
    assert all(str(lib["lib_e"]["path"]) in p or str(lib["lib_a"]["path"]) in p for p in after.values())
    copies = db.all("SELECT * FROM managed_copies WHERE status='active'")
    assert {c["source_path"] for c in copies} == set(ids_before.values())
    assert all(c["sha256"] and c["verify_mode"] == "full" for c in copies)
    assert db.one("SELECT root_folder_id FROM media_assets WHERE id=?", (a2,))["root_folder_id"] == lib["lib_a"]["id"]
    assert F.temps_anywhere(lib["tmp"]) == []


def test_idempotent_second_run_plans_and_copies_nothing(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    files = F.managed_files(lib)
    plan = mb.create_plan({})
    assert plan["book_count"] == 0 and plan["summary"]["actionable_items"] == 0
    with pytest.raises(mb.BuildRefused) as exc:
        mb.confirm_plan(plan["plan_id"], 0)
    assert exc.value.code == "empty"
    assert F.managed_files(lib) == files


# ---- verification and source changes -----------------------------------------------------------------

def _corrupt_after_copy(monkeypatch, times: int):
    real = mb._copy_stream
    n = {"left": times}

    def wrapper(sfd, dfd, tmp_name, digest, throttle, progress):
        total = real(sfd, dfd, tmp_name, digest, throttle, progress)
        if n["left"] > 0:
            n["left"] -= 1
            fd = os.open(tmp_name, os.O_WRONLY, dir_fd=dfd)
            os.pwrite(fd, b"\x00CORRUPT", 3)
            os.close(fd)
        return total
    monkeypatch.setattr(mb, "_copy_stream", wrapper)


def test_full_verification_catches_corruption_injected_after_the_copy(lib, monkeypatch):
    _, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    before = _sources(lib)
    _corrupt_after_copy(monkeypatch, times=99)
    plan, run = F.build()
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and "checksum" in it["error"] and it["attempts"] == mb.MAX_ATTEMPTS
    assert F.managed_files(lib) == [] and F.temps_anywhere(lib["tmp"]) == []
    assert _paths() == [str(e)] and db.one("SELECT COUNT(*) n FROM managed_copies")["n"] == 0
    assert _sources(lib) == before
    assert run["failures"][0]["title"] == "Dune"


def test_a_corrupt_first_attempt_is_retried_and_then_succeeds(lib, monkeypatch):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    _corrupt_after_copy(monkeypatch, times=1)
    plan, run = F.build()
    assert run["status"] == "completed" and _items(plan["plan_id"])[0]["attempts"] == 2
    assert F.sha(F.managed_files(lib)[0]) == F.sha(e)


def test_size_mode_does_not_reread_but_still_checks_the_size(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    calls = []
    monkeypatch.setattr(mb, "_hash_at", lambda dfd, name: calls.append(name) or "x")
    plan, run = F.build({"verify": "size"})
    assert run["status"] == "completed" and calls == []
    assert any("Only sizes" in w for w in plan["summary"]["warnings"])
    plan2 = mb.create_plan({"verify": "none"})
    assert any("Verification is OFF" in w for w in plan2["summary"]["warnings"])


def test_source_changed_after_planning_is_refused_and_not_copied(lib):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    with open(e, "ab") as fh:          # the user edits the file between the dry run and the run
        fh.write(b"more")
    run = mb.start(plan["plan_id"], submit=False)
    mb.execute(run["run_id"])
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and "changed since the dry run" in it["error"]
    assert F.managed_files(lib) == [] and _paths() == [str(e)]


def test_source_changed_during_the_copy_discards_the_copy(lib, monkeypatch):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    real = mb._copy_stream

    def touch_source(sfd, dfd, tmp_name, digest, throttle, progress):
        total = real(sfd, dfd, tmp_name, digest, throttle, progress)
        with open(e, "ab") as fh:
            fh.write(b"!")
        return total
    monkeypatch.setattr(mb, "_copy_stream", touch_source)
    plan, run = F.build()
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and F.managed_files(lib) == [] and F.temps_anywhere(lib["tmp"]) == []
    assert _paths() == [str(e)]


def test_source_changed_right_after_publishing_removes_the_published_copy(lib, monkeypatch):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    real = mb._publish

    def then_touch(*a, **k):
        out = real(*a, **k)
        os.utime(e, ns=(1, 1))
        return out
    monkeypatch.setattr(mb, "_publish", then_touch)
    plan, run = F.build()
    assert _items(plan["plan_id"])[0]["state"] == "failed"
    assert F.managed_files(lib) == [] and _paths() == [str(e)]


# ---- crash at every step, then restart + resume converges ----------------------------------------------

def _converged(lib, plan_id, n_files):
    assert F.temps_anywhere(lib["tmp"]) == []
    files = F.managed_files(lib)
    assert len(files) == n_files                                   # no orphans, no duplicates
    assert db.one("SELECT COUNT(*) n FROM managed_copies WHERE status='active'")["n"] == n_files
    assert sorted(_paths()) == sorted(str(f) for f in files)       # every record points at a managed copy that exists
    assert all(it["state"] == "done" for it in _items(plan_id))


@pytest.mark.parametrize("point", ["after_temp_written", "after_verify", "after_link_before_unlink", "after_publish", "after_records", "after_rename"])
def test_kill_at_each_step_then_restart_and_resume_converges(lib, monkeypatch, point):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author", n=3)
    before = _sources(lib)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)

    def boom(*a, **k):
        raise Crash()
    if point == "after_temp_written":
        monkeypatch.setattr(mb, "_verify", boom)
    elif point == "after_verify":
        real_pub = mb._publish
        monkeypatch.setattr(mb, "_publish", lambda *a, **k: boom())
    elif point == "after_link_before_unlink":
        real_link = os.link

        def link_then_die(*a, **k):
            real_link(*a, **k)
            raise Crash()
        monkeypatch.setattr(mb.os, "link", link_then_die)
    elif point == "after_publish":
        monkeypatch.setattr(mb, "_switch_unit", boom)
    elif point == "after_records":
        monkeypatch.setattr(mb, "_finish", boom)
    elif point == "after_rename":
        monkeypatch.setattr(mb.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "no links")))
        mb.reset_capability_cache()
        monkeypatch.setattr(mb, "publish_capability", lambda root, refresh=False: ("renameat2", ""))
        real_rn = mb._renameat2_noreplace

        def rn_then_die(*a):
            assert real_rn(*a)
            raise Crash()
        monkeypatch.setattr(mb, "_renameat2_noreplace", rn_then_die)
    with pytest.raises(Crash):
        mb.execute(run["run_id"])
    monkeypatch.undo()
    monkeypatch.setattr(mb, "RETRY_BACKOFF_SECONDS", 0.0)
    mb.reset_capability_cache()
    final = F.restart_and_resume(run["run_id"])
    assert final["status"] == "completed"
    _converged(lib, plan["plan_id"], 4)
    assert _sources(lib) == before


def test_pause_between_files_of_one_book_and_resume_finishes_without_duplicates(lib, monkeypatch):
    F.audioset(lib, "Big Book", "Ann Author", n=5)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._copy_one
    seen = []

    def pause_after_two(run_id, it, *a):
        real(run_id, it, *a)
        seen.append(it["id"])
        if len(seen) == 2:
            mb.pause(run_id)
    monkeypatch.setattr(mb, "_copy_one", pause_after_two)
    assert mb.execute(run["run_id"])["status"] == "paused"
    states = [i["state"] for i in _items(plan["plan_id"])]
    assert states == ["published", "published", "pending", "pending", "pending"]
    assert all("/audiobooks/" in p for p in _paths())          # the book is not switched until all its files are in
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    assert len(seen) == 5 and len(set(seen)) == 5             # nothing copied twice
    _converged(lib, plan["plan_id"], 5)


def test_cancel_keeps_finished_books_and_removes_unrecorded_copies(lib, monkeypatch):
    for i in range(4):
        F.ebook(lib, f"Book {i}", "Some Author")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._switch_unit

    def cancel_after_first(run_id, items, opts):
        real(run_id, items, opts)
        mb.cancel(run_id)
    monkeypatch.setattr(mb, "_switch_unit", cancel_after_first)
    assert mb.execute(run["run_id"])["status"] == "cancelled"
    states = sorted(i["state"] for i in _items(plan["plan_id"]))
    assert states == ["cancelled", "cancelled", "cancelled", "done"]
    assert len(F.managed_files(lib)) == 1 and F.temps_anywhere(lib["tmp"]) == []
    with pytest.raises(mb.BuildRefused):
        mb.resume(run["run_id"], submit=False)


# ---- collisions and names ---------------------------------------------------------------------------

def test_identical_file_already_at_the_destination_is_reused_not_copied_again(lib):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    dest = lib["lib_e"]["path"] / "Frank Herbert" / "Dune" / "Dune.epub"
    dest.parent.mkdir(parents=True)
    shutil.copyfile(e, dest)
    ino = dest.stat().st_ino
    plan, run = F.build()
    assert plan["summary"]["collisions"] == {"exists": 1}
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "done" and it["reused"] == 1
    assert F.managed_files(lib) == [dest] and dest.stat().st_ino == ino
    assert _paths() == [str(dest)]


def test_a_different_file_at_the_destination_is_never_overwritten(lib):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    dest = lib["lib_e"]["path"] / "Frank Herbert" / "Dune" / "Dune.epub"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"someone else's file")
    F.build()
    assert dest.read_bytes() == b"someone else's file"
    other = dest.with_name("Dune (2).epub")
    assert F.sha(other) == F.sha(e) and _paths() == [str(other)]


def test_case_insensitive_name_collision_gets_a_numbered_name(lib):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    existing = lib["lib_e"]["path"] / "Frank Herbert" / "Dune" / "DUNE.EPUB"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"x")
    plan, run = F.build()
    assert plan["summary"]["collisions"] == {"exists_case": 1}
    assert existing.read_bytes() == b"x"
    assert _paths() == [str(existing.with_name("Dune (2).epub"))]


def test_two_books_with_the_same_destination_get_distinct_names_in_the_plan(lib):
    F.ebook(lib, "Dune", "Frank Herbert", folder="a")
    w2, _, _ = F.ebook(lib, "Dune2", "Frank Herbert", folder="b")
    with db.tx() as c:                                          # two works that render to the same path
        c.execute("UPDATE works SET title='Dune' WHERE id=?", (w2,))
    plan, run = F.build()
    assert plan["summary"]["collisions"] == {"in_plan": 1}
    names = sorted(p.name for p in F.managed_files(lib))
    assert names == ["Dune (2).epub", "Dune.epub"] and run["status"] == "completed"


def test_unicode_and_long_names_are_normalised_and_fit_the_filesystem(lib):
    import unicodedata
    title = unicodedata.normalize("NFD", "Café Society: ") + "Ü" * 300 + " ..."
    F.ebook(lib, "x", "Zoë Author", data=b"abc" * 100)
    with db.tx() as c:
        c.execute("UPDATE works SET title=?", (title,))
    plan, run = F.build()
    assert run["status"] == "completed"
    f = F.managed_files(lib)[0]
    for part in f.relative_to(lib["lib_e"]["path"]).parts:
        assert len(part.encode()) <= mb.MAX_COMPONENT_BYTES and part == unicodedata.normalize("NFC", part) and not part.endswith((" ", "."))
    assert f.suffix == ".epub"


# ---- symlinks, permissions, space ----------------------------------------------------------------------

def test_symlinked_sources_are_refused(lib, tmp_path):
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"secret")
    link = lib["books"]["path"] / "Frank Herbert" / "Dune.epub"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    from conv_fixtures import add_book
    add_book(lib["books"], "Dune", "Frank Herbert", "ebook", [link])
    plan = mb.create_plan({})
    assert plan["summary"]["skips"]["symlink"]["count"] == 1 and plan["book_count"] == 0


def test_a_symlinked_destination_folder_is_refused_and_nothing_lands_outside(lib, tmp_path):
    F.ebook(lib, "Dune", "Frank Herbert")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (lib["lib_e"]["path"] / "Frank Herbert").symlink_to(elsewhere)
    plan, run = F.build()
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and "symbolic link" in it["error"]
    assert list(elsewhere.rglob("*")) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_permission_error_fails_the_book_and_the_run_continues(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.ebook(lib, "Emma", "Jane Austen")
    locked = lib["lib_e"]["path"] / "Frank Herbert"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        plan, run = F.build()
    finally:
        locked.chmod(0o755)
    by_title = {i["title"]: i for i in _items(plan["plan_id"])}
    assert by_title["Dune"]["state"] == "failed" and "Permission" in by_title["Dune"]["error"]
    assert by_title["Emma"]["state"] == "done" and run["status"] == "completed"


def test_stop_on_error_stops_the_run_at_the_first_failure(lib, monkeypatch):
    F.ebook(lib, "A", "X")
    F.ebook(lib, "B", "X")
    monkeypatch.setattr(mb, "_verify", lambda *a: (_ for _ in ()).throw(mb._ItemError("verify_failed", "bad")))
    plan, run = F.build({"stop_on_error": True})
    assert run["status"] == "stopped" and "stop_on_error" in run["stop_reason"]
    assert sorted(i["state"] for i in _items(plan["plan_id"])) == ["failed", "pending"]


def test_not_enough_space_is_refused_before_anything_starts(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    monkeypatch.setattr(mb.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 5))
    with pytest.raises(mb.BuildRefused) as exc:
        mb.start(plan["plan_id"], submit=False)
    assert exc.value.code == "preflight" and "not enough free space" in str(exc.value)
    assert db.one("SELECT executed_at FROM build_plans WHERE id=?", (plan["plan_id"],))["executed_at"] is None
    assert F.managed_files(lib) == []


def test_disk_full_during_the_copy_stops_cleanly_and_resume_finishes(lib, monkeypatch):
    F.ebook(lib, "A", "X")
    F.ebook(lib, "B", "X")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._write_all
    calls = {"n": 0}

    def full(fd, data):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(fd, data)
    monkeypatch.setattr(mb, "_write_all", full)
    assert mb.execute(run["run_id"])["status"] == "stopped"
    st = mb.run_status(run["run_id"])
    assert "space" in st["stop_reason"] and F.temps_anywhere(lib["tmp"]) == []
    assert sorted(i["state"] for i in _items(plan["plan_id"])) == ["done", "pending"]
    monkeypatch.setattr(mb, "_write_all", real)
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    _converged(lib, plan["plan_id"], 2)


def test_low_free_space_mid_run_stops_before_the_next_book(lib, monkeypatch):
    F.ebook(lib, "A", "X")
    F.ebook(lib, "B", "X")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._switch_unit

    def then_full(*a):
        real(*a)
        monkeypatch.setattr(mb.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 5))
    monkeypatch.setattr(mb, "_switch_unit", then_full)
    assert mb.execute(run["run_id"])["status"] == "stopped"
    assert sorted(i["state"] for i in _items(plan["plan_id"])) == ["done", "pending"]


# ---- fail closed where no-clobber publishing is impossible -----------------------------------------------

def _no_links(monkeypatch, err=errno.EXDEV):
    def nolink(*a, **k):
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(mb.os, "link", nolink)
    monkeypatch.setattr(mb, "_renameat2_noreplace", lambda *a: False)
    mb.reset_capability_cache()


@pytest.mark.parametrize("err", [errno.EXDEV, errno.EPERM])
def test_fails_closed_when_the_managed_root_cannot_publish_without_overwriting(lib, monkeypatch, err):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    _no_links(monkeypatch, err)
    with pytest.raises(mb.BuildRefused) as exc:
        mb.start(plan["plan_id"], submit=False)
    assert "without risking an overwrite" in str(exc.value) and "Hard Links" in str(exc.value)
    assert F.managed_files(lib) == []


def test_links_that_stop_working_mid_run_stop_the_run_with_nothing_half_done(lib, monkeypatch):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)           # preflight probed links: fine
    _no_links(monkeypatch, errno.EPERM)
    monkeypatch.setattr(mb, "publish_capability", lambda root, refresh=False: ("link", ""))
    assert mb.execute(run["run_id"])["status"] == "stopped"
    assert "overwrite" in mb.run_status(run["run_id"])["stop_reason"]
    assert F.managed_files(lib) == [] and F.temps_anywhere(lib["tmp"]) == [] and _paths() == [str(e)]
    assert _items(plan["plan_id"])[0]["state"] == "pending"


def test_renameat2_noreplace_is_used_where_hard_links_are_unsupported(lib, monkeypatch):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    monkeypatch.setattr(mb.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "no")))
    mb.reset_capability_cache()
    if mb.publish_capability(str(lib["lib_e"]["path"]))[0] != "renameat2":
        pytest.skip("this kernel/filesystem has no renameat2 NOREPLACE")
    plan, run = F.build()
    assert run["status"] == "completed" and F.sha(F.managed_files(lib)[0]) == F.sha(e)


# ---- throttle and concurrency -------------------------------------------------------------------------

def test_throttle_is_honoured_with_a_fake_clock(lib, monkeypatch):
    F.ebook(lib, "Big", "X", data=os.urandom(10 * 1024 * 1024))
    clock = {"t": 1000.0, "slept": 0.0}
    monkeypatch.setattr(mb, "_clock", lambda: clock["t"])

    def fake_sleep(s):
        clock["t"] += s
        clock["slept"] += s
    monkeypatch.setattr(mb, "_sleep", fake_sleep)
    plan, run = F.build({"throttle_mb_s": 1})
    assert run["status"] == "completed"
    assert 9.5 <= clock["slept"] <= 10.5          # 10 MiB at 1 MiB/s


def test_unthrottled_runs_never_sleep(lib, monkeypatch):
    F.ebook(lib, "Big", "X", data=os.urandom(1024 * 1024))
    monkeypatch.setattr(mb, "_sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    assert F.build({"throttle_mb_s": 0})[1]["status"] == "completed"


def test_concurrency_two_never_processes_an_item_twice(lib, monkeypatch):
    for i in range(12):
        F.ebook(lib, f"Book {i:02d}", "Author")
    F.audioset(lib, "Set", "Author", n=6)
    seen, lock, threads = [], threading.Lock(), set()
    real = mb._copy_one

    def record(run_id, it, *a):
        with lock:
            seen.append(it["id"])
            threads.add(threading.current_thread().name)
        real(run_id, it, *a)
    monkeypatch.setattr(mb, "_copy_one", record)
    plan, run = F.build({"concurrency": 2})
    assert run["status"] == "completed" and len(seen) == 18 and len(set(seen)) == 18
    _converged(lib, plan["plan_id"], 18)


def test_options_are_validated():
    for bad in ({"concurrency": 3}, {"verify": "maybe"}, {"mode": "move"}, {"throttle_mb_s": -1}, {"include": {"kinds": ["comic"]}},
                {"include": {"nope": 1}}, {"transfer": "move"}):
        with pytest.raises(mb.BuildRefused):
            mb.validate_options(bad)


# ---- the guard: nothing is ever written under an adopted/observe root -------------------------------------

def test_guard_no_code_path_writes_under_a_read_only_root_during_a_build(lib, monkeypatch, tmp_path):
    """Wraps every write-capable call the engine could use; any of them aimed under an adopt/observe root fails the test.
    Runs a build with failures, retries, a crash, recovery, resume and an undo. The adopted tree is also chmod read-only."""
    observe = __import__("conv_fixtures").root(tmp_path, "observe", "ebook", "legacy")
    (observe["path"] / "old.epub").write_bytes(b"old")
    F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author", n=3)
    _, _, bad = F.ebook(lib, "Bad", "X")
    guarded = [str(lib["books"]["path"]), str(lib["audiobooks"]["path"]), str(observe["path"])]
    hits: list[str] = []

    def resolve(p, dir_fd=None):
        p = os.fsdecode(p) if isinstance(p, (bytes, os.PathLike)) or not isinstance(p, str) else p
        if dir_fd is not None and not os.path.isabs(p):
            p = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), p)
        return os.path.abspath(p)

    def check(p, dir_fd=None, what=""):
        rp = resolve(p, dir_fd)
        if any(rp == g or rp.startswith(g + os.sep) for g in guarded):
            hits.append(f"{what} {rp}")

    W = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    real = {n: getattr(os, n) for n in ("open", "link", "rename", "replace", "mkdir", "unlink", "rmdir", "symlink", "chmod", "utime", "truncate")}
    real_open = builtins.open

    def g_open(path, flags, *a, dir_fd=None, **k):
        if flags & W:
            check(path, dir_fd, "os.open")
        return real["open"](path, flags, *a, dir_fd=dir_fd, **k)

    def two(name):
        def f(src, dst, *a, src_dir_fd=None, dst_dir_fd=None, **k):
            check(src, src_dir_fd, name)
            check(dst, dst_dir_fd, name)
            kw = {kk: v for kk, v in (("src_dir_fd", src_dir_fd), ("dst_dir_fd", dst_dir_fd)) if v is not None}
            return real[name](src, dst, *a, **kw, **k)
        return f

    def one(name):
        def f(path, *a, dir_fd=None, **k):
            if not isinstance(path, int):
                check(path, dir_fd, name)
            kw = {"dir_fd": dir_fd} if dir_fd is not None else {}
            return real[name](path, *a, **kw, **k)
        return f

    def b_open(file, mode="r", *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)) and any(c in mode for c in "wax+"):
            check(file, None, "open")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(os, "open", g_open)
    for n in ("link", "rename", "replace"):
        monkeypatch.setattr(os, n, two(n))
    for n in ("mkdir", "unlink", "rmdir", "chmod", "utime", "truncate"):
        monkeypatch.setattr(os, n, one(n))
    monkeypatch.setattr(builtins, "open", b_open)
    for root in guarded:                       # a read-only source fixture, as on the NAS
        for dirpath, dirs, files in os.walk(root):
            for f in files:
                real["chmod"](os.path.join(dirpath, f), 0o444)
            real["chmod"](dirpath, 0o555)
    try:
        before = _sources(lib)
        # failure (verify) on one book, a crash after publishing another, recovery, resume, undo of everything
        real_verify = mb._verify
        monkeypatch.setattr(mb, "_verify", lambda dfd, name, size, d, mode: (_ for _ in ()).throw(mb._ItemError("verify_failed", "x"))
                            if "Bad" in os.readlink(f"/proc/self/fd/{dfd}") else real_verify(dfd, name, size, d, mode))
        plan = F.plan_and_confirm({"include": {"root_ids": [lib["books"]["id"], lib["audiobooks"]["id"], observe["id"]]}})
        run = mb.start(plan["plan_id"], submit=False)
        real_switch = mb._switch_unit
        crashed = {"done": False}

        def crash_once(*a):
            if not crashed["done"]:
                crashed["done"] = True
                raise Crash()
            return real_switch(*a)
        monkeypatch.setattr(mb, "_switch_unit", crash_once)
        with pytest.raises(Crash):
            mb.execute(run["run_id"])
        F.restart_and_resume(run["run_id"])
        mb.undo(run_id=run["run_id"], confirm_count=mb.run_status(run["run_id"])["active_books"])
        assert _sources(lib) == before
    finally:
        for root in guarded:
            for dirpath, dirs, files in os.walk(root):
                real["chmod"](dirpath, 0o755)
    assert hits == []
