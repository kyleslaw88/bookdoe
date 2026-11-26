"""Safety review round 1 of the managed library build: one regression test (or more) per finding.

1. Undo must not report success for a managed copy that was renamed/moved, nor log a trash entry for someone else's file.
2. A published copy that vanishes before its book is recorded is copied again, and a book that keeps losing it terminates.
3. Any unexpected exception (ValueError, sqlite3.OperationalError, the TypeError in _switch_convert) leaves the run
   stopped and resumable, never 'running' without a worker.
4. Undo deletes only converted assets the build created, and keeps any with listening/reading history.
5. Conversion outputs are created through the folder handle with unpredictable names: a symlink planted at the old
   predictable temp name cannot redirect a write outside the managed root.
6. Folder creation, unlink and the crash-recovery sweep go through O_NOFOLLOW directory handles.
7. Running a build needs the admin switch managed_build.run_enabled (default OFF): Start/Resume answer 409 while it is off.
8. The migration-step test tolerates other branches' steps.
The originals in the adopted roots are compared byte for byte wherever a build runs."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app import migrations
from app.services import conversion, convert_ebook, managedbuild as mb, reader

import conv_fixtures as cf
import mb_fixtures as F

app = main_module.app
needs_ffmpeg = pytest.mark.skipif(not cf.HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


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


def _watchdog(monkeypatch, limit=40):
    """Cancel the run after ``limit`` claims: on the old code the scenarios below loop forever; with the fixes they end
    on their own long before this. The tests assert the run was NOT cancelled by the watchdog."""
    real = mb._claim
    calls = {"n": 0}

    def counted(plan_id, wid):
        calls["n"] += 1
        if calls["n"] > limit:
            with db.tx() as c:
                c.execute("UPDATE build_runs SET cancel_requested=1 WHERE plan_id=?", (plan_id,))
        return real(plan_id, wid)
    monkeypatch.setattr(mb, "_claim", counted)
    return calls


def _mobi_book(lib, title="Tiny Book", author="Anne Author"):
    src = cf.make_mobi(lib["books"]["path"] / author / f"{title}.mobi", title=title, author=author)
    w, a = cf.add_book(lib["books"], title, author, "ebook", [src])
    with db.tx() as c:
        c.execute("UPDATE media_files SET adopted=1, managed=0 WHERE asset_id=?", (a,))
    return w, a, src


def _me():
    with TestClient(app) as client:
        return client.get("/api/v1/auth/me").json()["user"]["id"]


# ---- 1. Undo of a renamed/moved managed copy; a taken trash name -------------------------------------

def test_undo_refuses_a_book_whose_managed_copy_was_renamed_and_keeps_the_original_preserved(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]
    renamed = copy.with_name("Dune (renamed).epub")
    os.rename(copy, renamed)                          # what a rename in the managed library does: file and record move
    with db.tx() as c:
        c.execute("UPDATE media_files SET path=? WHERE path=?", (str(renamed), str(copy)))
    before = _sources(lib)
    res = mb.undo(work_id=w)
    assert res["undone_books"] == 0 and res["undone_files"] == 0
    assert res["problems"][0]["code"] == "copy_moved" and "renamed or moved" in res["problems"][0]["error"]
    mc = db.one("SELECT * FROM managed_copies")
    assert mc["status"] == "active" and mc["undo_state"] is None          # not reported as undone
    assert db.one("SELECT path FROM media_files")["path"] == str(renamed) and renamed.exists()
    assert mb.is_build_path(str(e))                    # the original stays a preserved source: no scan re-adopts it
    assert db.one("SELECT COUNT(*) n FROM file_trash")["n"] == 0
    assert _sources(lib) == before


def test_undo_with_the_trash_name_taken_moves_the_copy_to_a_free_name_and_logs_only_its_own_file(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]
    taken = Path(mb._trash_path(str(lib["lib_e"]["path"]), str(copy)))
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"someone else's file")
    res = mb.undo(work_id=w)
    assert res["undone_books"] == 1 and res["undone_files"] == 1 and res["problems"] == []
    assert not copy.exists()                                              # never left in place while reported moved
    assert taken.read_bytes() == b"someone else's file"                   # the other file is untouched
    row = db.one("SELECT * FROM file_trash")
    assert row["trash_path"] != str(taken) and row["trash_path"].endswith("Dune (2).epub")
    assert F.sha(Path(row["trash_path"])) == F.sha(e)                     # the logged trash entry is our copy
    assert db.one("SELECT undo_state,trash_path FROM managed_copies") == {"undo_state": "done", "trash_path": row["trash_path"]}
    assert db.one("SELECT path FROM media_files")["path"] == str(e)


def test_undo_reports_a_copy_it_could_not_move_and_keeps_it_out_of_scans(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    copy = F.managed_files(lib)[0]
    monkeypatch.setattr(mb, "_move_to_trash", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "I/O error")))
    res = mb.undo(work_id=w)
    assert res["undone_files"] == 0 and res["problems"][0]["code"] == "move_failed"
    assert copy.exists() and db.one("SELECT COUNT(*) n FROM file_trash")["n"] == 0
    assert db.one("SELECT undo_state FROM managed_copies")["undo_state"] == "failed"
    assert mb.is_build_path(str(copy))


# ---- 2. A published copy that vanishes before the record switch ------------------------------------

def test_a_published_copy_that_vanishes_before_the_switch_is_copied_again_and_the_run_completes(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    before = _sources(lib)
    real = mb._switch_unit
    gone = []

    def vanish_once(run_id, items, opts):
        if not gone:
            for it in items:
                os.unlink(it["final_path"])
                gone.append(it["final_path"])
        return real(run_id, items, opts)
    monkeypatch.setattr(mb, "_switch_unit", vanish_once)
    calls = _watchdog(monkeypatch)
    plan, run = F.build()
    assert run["status"] == "completed" and calls["n"] < 40, (run, calls)
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "done" and it["attempts"] == 2
    assert Path(gone[0]).exists() and F.sha(Path(gone[0])) == F.sha(e)
    assert db.one("SELECT path FROM media_files")["path"] == gone[0]
    assert _sources(lib) == before


def test_a_published_copy_that_keeps_vanishing_fails_the_book_after_max_attempts(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    real = mb._switch_unit

    def vanish(run_id, items, opts):
        for it in items:
            os.unlink(it["final_path"])
        return real(run_id, items, opts)
    monkeypatch.setattr(mb, "_switch_unit", vanish)
    calls = _watchdog(monkeypatch)
    plan, run = F.build()
    assert run["status"] == "completed" and calls["n"] < 40, (run, calls)
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and it["attempts"] == mb.MAX_ATTEMPTS and "disappeared" in it["error"]
    assert F.managed_files(lib) == [] and F.temps_anywhere(lib["tmp"]) == []


# ---- 3. Unexpected exceptions never leave a run 'running' -------------------------------------------

def _settled(run_id, plan_id):
    r = db.one("SELECT * FROM build_runs WHERE id=?", (run_id,))
    assert r["status"] != "running"
    assert db.one("SELECT COUNT(*) n FROM build_plan_items WHERE plan_id=? AND claimed_by IS NOT NULL", (plan_id,))["n"] == 0
    assert run_id not in mb._executing and run_id not in mb._live
    return r


def test_an_unexpected_value_error_fails_the_book_stops_the_run_and_resume_finishes(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    before = _sources(lib)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    monkeypatch.setattr(mb, "_copy_one", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom in a book")))
    out = mb.execute(run["run_id"])                                 # no exception escapes
    r = _settled(run["run_id"], plan["plan_id"])
    assert out["status"] == "stopped" and r["status"] == "stopped" and "ValueError" in r["stop_reason"] and "boom in a book" in r["stop_reason"]
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and "ValueError" in it["error"]
    monkeypatch.undo()
    monkeypatch.setattr(mb, "RETRY_BACKOFF_SECONDS", 0.0)
    mb.retry_item(run["run_id"], it["id"])
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "completed"
    assert db.one("SELECT path FROM media_files")["path"].startswith(str(lib["lib_e"]["path"]))
    assert _sources(lib) == before


def test_a_database_error_outside_a_book_stops_the_run_and_cancel_or_resume_work(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.ebook(lib, "Emma", "Jane Austen")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    real = mb._claim
    state = {"n": 0}

    def flaky(plan_id, wid):
        state["n"] += 1
        if state["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(plan_id, wid)
    monkeypatch.setattr(mb, "_claim", flaky)
    out = mb.execute(run["run_id"])
    r = _settled(run["run_id"], plan["plan_id"])
    assert out["status"] == "stopped" and "OperationalError" in r["stop_reason"] and "database is locked" in r["stop_reason"]
    monkeypatch.setattr(mb, "_claim", real)
    mb.resume(run["run_id"], submit=False)
    mb.execute(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "completed"
    assert {i["state"] for i in _items(plan["plan_id"])} == {"done"}


def test_an_error_escaping_the_run_loop_itself_stops_the_run(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    monkeypatch.setattr(mb, "_finish", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    out = mb.execute(run["run_id"])
    r = _settled(run["run_id"], plan["plan_id"])
    assert out["status"] == "stopped" and "disk I/O error" in r["stop_reason"]
    monkeypatch.undo()
    mb.cancel(run["run_id"])
    assert mb.run_status(run["run_id"])["status"] == "cancelled"


def test_switch_convert_with_neither_a_conversion_nor_a_file_record_fails_the_book_without_a_type_error(lib, monkeypatch):
    w, a, src = _mobi_book(lib)
    before = _sources(lib)

    def fake_to_epub(source, dest, *, title=None, authors=None, dest_fd=None):
        data = b"PK\x03\x04 not really an epub" * 20
        if dest_fd is not None:
            os.write(dest_fd, data)
        else:
            Path(dest).write_bytes(data)
        return {"mode": "fake", "tool": "fake"}

    def lost_register(conv, *args, **kwargs):         # the conversion row vanishes and no file record is made
        with db.tx() as c:
            c.execute("DELETE FROM conversions WHERE id=?", (conv["id"],))
    monkeypatch.setattr(convert_ebook, "to_epub", fake_to_epub)
    monkeypatch.setattr(conversion, "_register", lost_register)
    plan = F.plan_and_confirm()
    assert plan["summary"]["by_action"]["convert"]["items"] == 1
    run = mb.start(plan["plan_id"], submit=False)
    mb.execute(run["run_id"])                                       # no TypeError
    _settled(run["run_id"], plan["plan_id"])
    it = _items(plan["plan_id"])[0]
    assert it["state"] == "failed" and "not recorded" in it["error"]
    assert F.managed_files(lib) == [] and F.temps_anywhere(lib["tmp"]) == []
    assert db.one("SELECT COUNT(*) n FROM managed_copies")["n"] == 0
    assert _sources(lib) == before


# ---- 4. Undo of converted books: only what the build created, never history --------------------------

@pytest.mark.parametrize("history", ["progress", "bookmark"])
def test_undo_keeps_a_converted_book_with_listening_or_reading_history(lib, history):
    w, a, src = _mobi_book(lib)
    plan, run = F.build()
    assert run["status"] == "completed", run
    d = db.one("SELECT * FROM media_assets WHERE derived_from_asset_id=?", (a,))
    f = db.one("SELECT * FROM media_files WHERE asset_id=?", (d["id"],))
    me = _me()
    if history == "progress":
        reader.set_progress(w, d["id"], me, file_id=f["id"], position=None, location="epubcfi(/6/4)", percent=0.3)
    else:
        with db.tx() as c:
            c.execute("INSERT INTO bookmarks(id,user_id,asset_id,work_id,location,created_at) VALUES (?,?,?,?,?,?)",
                      (db.uid(), me, d["id"], w, "epubcfi(/6/2)", db.now()))
    res = mb.undo(run_id=run["run_id"], confirm_count=1)
    assert res["undone_books"] == 0 and res["problems"][0]["code"] == "kept_history"
    assert res["problems"][0]["error"].startswith(mb.KEPT_HISTORY)
    assert db.one("SELECT id FROM media_assets WHERE id=?", (d["id"],))                      # kept
    assert Path(f["path"]).exists()
    assert db.one("SELECT COUNT(*) n FROM media_progress WHERE asset_id=?", (d["id"],))["n"] == (history == "progress")
    assert db.one("SELECT COUNT(*) n FROM bookmarks WHERE asset_id=?", (d["id"],))["n"] == (history == "bookmark")
    assert db.one("SELECT status,undo_state FROM managed_copies") == {"status": "active", "undo_state": None}


def test_undo_never_deletes_a_converted_asset_that_existed_before_the_build(lib):
    w, a, src = _mobi_book(lib)
    old = db.uid()
    with db.tx() as c:          # an earlier conversion whose file is gone: the build re-makes it into that same asset
        c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at,derived_from_asset_id,derived_by,derived_at) "
                  "VALUES (?,?,'ebook','epub','owned',?,?,?,?,'earlier',?)", (old, w, lib["lib_e"]["id"], db.now(), db.now(), a, db.now()))
        c.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,adopted,managed,missing,discovered_at) VALUES (?,?,?,10,0,1,1,?)",
                  (db.uid(), old, str(lib["lib_e"]["path"] / "gone.epub"), db.now()))
    plan, run = F.build()
    assert run["status"] == "completed", run
    mc = db.one("SELECT * FROM managed_copies")
    assert mc["asset_id"] == old and mc["created_by_build"] == 0
    res = mb.undo(work_id=w)
    assert res["undone_books"] == 0 and res["problems"][0]["code"] == "kept_preexisting"
    assert db.one("SELECT id FROM media_assets WHERE id=?", (old,))


def test_undo_deletes_a_converted_asset_the_build_created_when_it_has_no_history(lib):
    w, a, src = _mobi_book(lib)
    before = _sources(lib)
    plan, run = F.build()
    assert db.one("SELECT created_by_build FROM managed_copies")["created_by_build"] == 1
    res = mb.undo(run_id=run["run_id"], confirm_count=1)
    assert res == {"undone_books": 1, "undone_files": 1, "problems": []}
    assert db.one("SELECT 1 x FROM media_assets WHERE derived_from_asset_id=?", (a,)) is None
    assert F.managed_files(lib) == [] and _sources(lib) == before


def test_both_undo_confirm_screens_warn_that_books_with_history_are_kept():
    js = (Path(main_module.__file__).parent / "static" / "managedbuild.js").read_text()
    assert "listened to or read" in js and "kept" in js
    assert "confirm(`Move the managed copies of this book to the managed trash" in js and "${UNDO_KEEPS}" in js     # book page
    assert "mb-undo-note" in js and "e(UNDO_KEEPS)" in js                                                          # run history


# ---- 5. Conversion outputs through the folder handle -------------------------------------------------

def _plant(run_id, item, target):
    folder = Path(item["dest_path"]).parent
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / f"{mb.TEMP_PREFIX}{run_id[:8]}-{item['id']}{mb.TEMP_SUFFIX}"      # the old, predictable temp name
    link.symlink_to(target)
    return link


def test_a_symlink_planted_at_the_old_temp_name_cannot_redirect_an_ebook_conversion(lib, tmp_path):
    w, a, src = _mobi_book(lib)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.epub"
    victim.write_bytes(b"precious, not ours")
    before = _sources(lib)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    link = _plant(run["run_id"], _items(plan["plan_id"])[0], victim)
    mb.execute(run["run_id"])
    assert victim.read_bytes() == b"precious, not ours"
    assert link.is_symlink()                                   # never followed, never removed
    assert mb.run_status(run["run_id"])["status"] == "completed"
    out = db.one("SELECT f.path FROM media_files f JOIN media_assets d ON d.id=f.asset_id WHERE d.derived_from_asset_id=?", (a,))["path"]
    assert out.startswith(str(lib["lib_e"]["path"])) and not os.path.islink(out)
    assert _sources(lib) == before


@needs_ffmpeg
def test_a_symlink_planted_at_the_old_temp_name_cannot_redirect_an_audio_conversion(lib, tmp_path):
    files = [cf.make_mp3(lib["audiobooks"]["path"] / "Eric Flint" / "Skies" / f"{i:02d}.mp3", seconds=1, freq=300 + 80 * i,
                         title=f"Chapter {i}", track=i) for i in (1, 2)]
    w, a = cf.add_book(lib["audiobooks"], "Skies", "Eric Flint", "audiobook", files)
    with db.tx() as c:
        c.execute("UPDATE media_files SET adopted=1, managed=0 WHERE asset_id=?", (a,))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "created-through-a-link.m4b"            # dangling: ffmpeg -n would create it through the link
    before = _sources(lib)
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    link = _plant(run["run_id"], _items(plan["plan_id"])[0], victim)
    mb.execute(run["run_id"])
    assert not victim.exists() and list(outside.iterdir()) == []
    assert link.is_symlink()
    st = mb.run_status(run["run_id"])
    assert st["status"] == "completed", st
    d = db.one("SELECT f.path FROM media_files f JOIN media_assets d ON d.id=f.asset_id WHERE d.derived_from_asset_id=?", (a,))
    assert d["path"].startswith(str(lib["lib_a"]["path"])) and os.path.getsize(d["path"]) > 0
    assert _sources(lib) == before


def test_temp_names_are_unpredictable():
    a, b = mb._temp_name("run12345678", 7), mb._temp_name("run12345678", 7)
    assert a != b and a.startswith(f"{mb.TEMP_PREFIX}run12345-7-") and a.endswith(mb.TEMP_SUFFIX)


def test_to_epub_never_writes_through_an_existing_name_or_a_link(tmp_path):
    src = cf.make_mobi(tmp_path / "in" / "Tiny.mobi", title="Tiny")
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = tmp_path / "out.epub"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        convert_ebook.to_epub(src, link)
    assert victim.read_bytes() == b"keep"


# ---- 6. Directory handles: mkdir + fsync, unlink, recovery sweep ---------------------------------------

def test_ensure_dirs_creates_folders_through_handles_and_fsyncs_each_new_parent(lib, monkeypatch):
    root = os.path.realpath(lib["lib_e"]["path"])
    synced = []
    real = os.fsync
    monkeypatch.setattr(mb.os, "fsync", lambda fd: (synced.append(os.readlink(f"/proc/self/fd/{fd}")), real(fd))[1])
    fd, made = mb._ensure_dirs(root, ["A", "B"])
    try:
        assert os.readlink(f"/proc/self/fd/{fd}") == os.path.join(root, "A", "B")
    finally:
        os.close(fd)
    assert made == [((), "A"), (("A",), "B")]
    assert synced == [root, os.path.join(root, "A")]          # the parent of each new folder
    mb._prune(root, made)
    assert not os.path.exists(os.path.join(root, "A"))


def test_unlink_if_never_removes_a_file_outside_the_managed_roots(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    assert mb._unlink_if(str(e)) is False and e.exists()                # an original in an adopted root
    inside = lib["lib_e"]["path"] / "x.part"
    inside.write_bytes(b"x")
    assert mb._unlink_if(str(inside)) is True and not inside.exists()


def test_the_crash_recovery_sweep_never_follows_a_symlinked_destination_folder(lib, tmp_path):
    F.ebook(lib, "Dune", "Frank Herbert")
    plan = F.plan_and_confirm()
    run = mb.start(plan["plan_id"], submit=False)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Dune").mkdir(parents=True)
    bait = elsewhere / "Dune" / f"{mb.TEMP_PREFIX}{run['run_id'][:8]}-1{mb.TEMP_SUFFIX}"
    bait.write_bytes(b"not ours")
    (lib["lib_e"]["path"] / "Frank Herbert").symlink_to(elsewhere)
    out = mb.recover_run(run["run_id"])
    assert bait.read_bytes() == b"not ours" and out["temps_removed"] == 0


# ---- 7. The admin switch ---------------------------------------------------------------------------

def test_run_and_resume_answer_409_while_the_run_switch_is_off_and_dry_run_plan_and_undo_still_work(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.allow_runs(False)
    with TestClient(app) as c:
        assert c.get("/api/v1/managed-build/settings").json() == {"run_enabled": False}
        assert c.get("/api/v1/managed-build/status").json()["run_enabled"] is False
        p = c.post("/api/v1/managed-build/plans", json={}).json()
        assert p["book_count"] == 1
        assert c.get(f"/api/v1/managed-build/plans/{p['plan_id']}/items").status_code == 200
        assert c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/confirm", json={"book_count": 1}).json()["confirmed"]
        r = c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/start")
        assert r.status_code == 409 and "switched off" in r.json()["detail"]
        r = c.post("/api/v1/jobs/managed-library-build/run", json={"plan_id": p["plan_id"]})          # the v1 path too
        assert r.status_code == 409 and "switched off" in r.json()["detail"]
        assert db.one("SELECT COUNT(*) n FROM build_runs")["n"] == 0 and F.managed_files(lib) == []
        assert c.put("/api/v1/managed-build/settings", json={"run_enabled": "yes"}).status_code == 422
        assert c.put("/api/v1/managed-build/settings", json={"run_enabled": True}).json() == {"run_enabled": True}
        run = mb.start(p["plan_id"], submit=False)
        mb.pause(run["run_id"])
        mb.execute(run["run_id"])
        assert mb.run_status(run["run_id"])["status"] == "paused"
        assert c.put("/api/v1/managed-build/settings", json={"run_enabled": False}).json() == {"run_enabled": False}
        r = c.post(f"/api/v1/managed-build/runs/{run['run_id']}/resume")
        assert r.status_code == 409 and "switched off" in r.json()["detail"]
        F.allow_runs(True)
        mb.resume(run["run_id"], submit=False)
        mb.execute(run["run_id"])
        F.allow_runs(False)
        u = c.post(f"/api/v1/managed-build/runs/{run['run_id']}/undo", json={"confirm_count": 1})     # undo of finished work
        assert u.status_code == 200 and u.json()["undone_books"] == 1
    assert {r["event_type"] for r in db.all("SELECT event_type FROM history WHERE event_type='managed_build.run_enabled'")}


def test_the_run_switch_is_off_on_a_fresh_database():
    db.set_setting(mb.RUN_ENABLED_KEY, None)
    assert mb.run_enabled() is False
    with pytest.raises(mb.BuildRefused) as exc:
        mb.start("no-such-plan", submit=False)
    assert exc.value.code == "run_disabled"


# ---- 8. Migration numbering ------------------------------------------------------------------------

