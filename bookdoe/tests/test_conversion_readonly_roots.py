"""Conversion of books that live in READ-ONLY (adopt/observe) library roots.

The converted file is written into the managed root of the same kind (``<root>/<Author>/<Title>/<Title>.m4b``);
the source files stay exactly where they are. Real temp roots, real ffmpeg-generated MP3s and a generated MOBI.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import os
import shutil
from pathlib import Path

import pytest

from app import db
from app.services import conversion, derived, detectors_db, maintenance, redundantfiles, repair, scanner

import conv_fixtures as cf
from test_player_api import local_mode, world  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not cf.HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
TITLE, AUTHOR = "Wayward Pines", "Blake Crouch"


@pytest.fixture(autouse=True)
def no_background_kick(monkeypatch):
    monkeypatch.setattr(conversion, "kick", lambda kind: None)


def snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    """name -> (sha256, mtime_ns) of every file and folder below ``folder``."""
    out = {}
    for p in sorted(folder.rglob("*")):
        out[str(p.relative_to(folder))] = (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir", p.stat().st_mtime_ns)
    return out


def ro_book(tmp_path, *, mode="adopt", title=TITLE, author=AUTHOR, real=True, n=3):
    """An owned audiobook of MP3 files in a read-only root."""
    ro = cf.root(tmp_path, mode, "audiobook", name=f"{mode}-audio")
    files = []
    for i in range(1, n + 1):
        p = ro["path"] / author / title / f"{i:02d}.mp3"
        if real:
            cf.make_mp3(p, seconds=1 + i % 2, freq=300 + 90 * i, title=f"Chapter {i}", track=i)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"ID3 not really audio " * 20 + p.name.encode())
        files.append(p)
    work_id, asset_id = cf.add_book(ro, title, author, "audiobook", files)
    return ro, work_id, asset_id, files


def managed_audio(tmp_path, name="managed-audio"):
    return cf.root(tmp_path, "managed", "audiobook", name=name)


def run_all(kind="audio"):
    return conversion.run_queue(kind)


def derived_of(asset_id):
    return db.one("SELECT * FROM media_assets WHERE derived_from_asset_id=?", (asset_id,))


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_an_adopted_book_converts_into_the_managed_root_and_the_source_is_untouched(tmp_path):
    ro, work_id, asset_id, files = ro_book(tmp_path)
    mg = managed_audio(tmp_path)
    before = snapshot(ro["path"])
    info = conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))
    assert info["state"] == "available" and info["cross_root"] is True and info["root"]["id"] == mg["id"]
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1

    final = mg["path"] / AUTHOR / TITLE / f"{TITLE}.m4b"
    assert final.is_file() and final.stat().st_size > 0
    assert snapshot(ro["path"]) == before, "hash, mtime and the folder listing of the read-only root are unchanged"
    assert not list(mg["path"].rglob("*.partial")) and not list(mg["path"].rglob(".*"))

    d = derived_of(asset_id)
    assert (d["status"], d["monitored"], d["has_acquisition_intent"], d["format"]) == ("owned", 0, 0, "m4b")
    assert d["root_folder_id"] == mg["id"] and d["derived_by"] and d["derived_at"]
    f = db.all("SELECT * FROM media_files WHERE asset_id=?", (d["id"],))
    assert len(f) == 1 and f[0]["path"] == str(final) and f[0]["relative_path"] == f"{AUTHOR}/{TITLE}/{TITLE}.m4b" and f[0]["managed"] == 1
    # the source asset still points at the adopted root and its own files
    src = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    assert src["root_folder_id"] == ro["id"] and db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=? AND path LIKE ?", (asset_id, str(ro["path"]) + "%"))["n"] == 3
    # the audit trail names the destination
    ev = db.one("SELECT message,data_json FROM history WHERE event_type='conversion.done'")
    assert "saved in" in ev["message"] and f"{AUTHOR}/{TITLE}/{TITLE}.m4b" in ev["message"]
    queued = db.one("SELECT message FROM history WHERE event_type='conversion.queued'")["message"]
    assert mg["path"].name in queued and "original stays" in queued
    assert conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))["state"] == "converted"


@needs_ffmpeg
def test_observe_roots_work_the_same_and_a_mobi_converts_into_the_managed_ebook_root(tmp_path):
    ro, work_id, asset_id, files = ro_book(tmp_path, mode="observe", title="Observed", n=2)
    managed_audio(tmp_path)
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    assert (tmp_path / "managed-audio" / AUTHOR / "Observed" / "Observed.m4b").is_file()

    ro_e = cf.root(tmp_path, "adopt", "ebook", name="adopt-ebook")
    mobi = cf.make_mobi(ro_e["path"] / "Anne Author" / "Tiny Book" / "Tiny Book.mobi")
    mg_e = cf.root(tmp_path, "managed", "ebook", name="managed-ebook")
    w, a = cf.add_book(ro_e, "Tiny Book", "Anne Author", "ebook", [mobi])
    before = snapshot(ro_e["path"])
    conversion.enqueue(a)
    assert run_all("ebook")["done"] == 1
    assert (mg_e["path"] / "Anne Author" / "Tiny Book" / "Tiny Book.epub").is_file()
    assert snapshot(ro_e["path"]) == before
    # an audiobook never lands in the ebook root and vice versa
    assert not list(mg_e["path"].rglob("*.m4b")) and not list((tmp_path / "managed-audio").rglob("*.epub"))


def test_books_in_a_managed_root_still_convert_next_to_the_source(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    other = managed_audio(tmp_path, "other-managed")
    files = [rt["path"] / "Eric Flint" / "Skies" / f"{i}.mp3" for i in (1, 2)]
    for p in files:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3" * 100 + p.name.encode())
    _, asset_id = cf.add_book(rt, "Skies", "Eric Flint", "audiobook", files)
    info = conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))
    assert info["state"] == "available" and info["cross_root"] is False and info["root"]["id"] == rt["id"]
    assert info["out_dir"] == str(rt["path"] / "Eric Flint" / "Skies")
    assert not list(other["path"].iterdir())


# ---------------------------------------------------------------------------
# Naming, collisions, symlinks
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_names_are_sanitised_and_a_taken_name_gets_a_numeric_suffix_never_an_overwrite(tmp_path):
    title, author = 'Dune: "The" <Best>? Book/Part*', "A/B C. "
    ro, work_id, asset_id, _ = ro_book(tmp_path, title="Dune Messiah", author="Frank Herbert", n=1)
    with db.tx() as conn:
        conn.execute("UPDATE works SET title=? WHERE id=?", (title, work_id))
    mg = managed_audio(tmp_path)
    info = conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))
    out = Path(info["out_dir"])
    assert out.parent == mg["path"] / "Frank Herbert", out
    name = out.name
    assert not set('<>"|?*/\\') & set(name) and ":" not in name and name == name.strip(" .") and name
    # a file of that name already sits in the destination (a stray, or another book): it is never replaced
    out.mkdir(parents=True)
    stray = out / f"{name}.m4b"
    stray.write_bytes(b"someone else's file")
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    assert stray.read_bytes() == b"someone else's file"
    got = sorted(p.name for p in out.glob("*.m4b"))
    assert got == sorted([f"{name}.m4b", f"{name} (2).m4b"])
    assert db.one("SELECT path FROM media_files WHERE asset_id=?", (derived_of(asset_id)["id"],))["path"] == str(out / f"{name} (2).m4b")


@needs_ffmpeg
def test_a_symlink_in_the_destination_is_refused_and_nothing_is_written_through_it(tmp_path):
    ro, work_id, asset_id, _ = ro_book(tmp_path, n=2)
    mg = managed_audio(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = mg["path"] / AUTHOR
    link.symlink_to(elsewhere, target_is_directory=True)
    # a link that leaves the root is refused when the offer is computed ...
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "outside_roots"
    # ... and so is one that stays inside the root, when the folders are made
    link.unlink()
    inside = mg["path"] / "inside"
    inside.mkdir()
    link.symlink_to(inside, target_is_directory=True)
    conversion.save_settings({"max_attempts": 1})
    conversion.enqueue(asset_id)
    out = run_all()
    row = db.one("SELECT * FROM conversions")
    assert out["failed"] == 1 and row["status"] == "failed" and "symbolic link" in row["error"]
    assert list(elsewhere.iterdir()) == [] and list(inside.iterdir()) == []
    assert derived_of(asset_id) is None


def test_a_failed_run_removes_the_folders_it_made(tmp_path, monkeypatch):
    from app.services import convert_audio

    ro, work_id, asset_id, _ = ro_book(tmp_path, real=False, n=2)
    mg = managed_audio(tmp_path)
    monkeypatch.setattr(convert_audio, "plan_audio", lambda files, **kw: convert_audio.AudioPlan(
        [convert_audio.Source(path=f["path"], duration=1.0, codec="mp3", sample_rate=22050, channels=1) for f in files], 2, 1, 22050, 64, False, False, 2, 1, "filename"))

    def boom(plan, meta, out, **kw):
        Path(out).write_bytes(b"half")
        raise convert_audio.ConversionError("encoder crashed", retryable=False)

    monkeypatch.setattr(convert_audio, "run_ffmpeg", boom)
    conversion.enqueue(asset_id)
    assert run_all()["failed"] == 1
    assert list(mg["path"].iterdir()) == [], "no partial file and no empty Author/Title folders are left behind"


# ---------------------------------------------------------------------------
# Choosing the destination
# ---------------------------------------------------------------------------

def test_no_managed_root_is_blocked_with_a_visible_reason(tmp_path):
    ro, work_id, asset_id, _ = ro_book(tmp_path, real=False)
    info = conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))
    assert info["state"] == "blocked" and info["code"] == "no_managed_root"
    assert "read-only" in info["reason"] and "managed audiobook" in info["reason"]
    opt = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))])[asset_id]
    assert opt["code"] == "no_managed_root" and opt["reason"] == info["reason"]
    # a managed EBOOK root is not a home for an audiobook
    cf.root(tmp_path, "managed", "ebook", name="only-ebooks")
    assert conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))["code"] == "no_managed_root"
    # a disabled managed root does not count either
    off = managed_audio(tmp_path)
    with db.tx() as conn:
        conn.execute("UPDATE root_folders SET enabled=0 WHERE id=?", (off["id"],))
    assert conversion.assess(db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,)))["code"] == "no_managed_root"


def test_default_destination_is_the_first_enabled_managed_root_of_the_kind_and_a_setting_overrides_it(tmp_path):
    ro, work_id, asset_id, _ = ro_book(tmp_path, real=False)
    mixed = cf.root(tmp_path, "managed", "mixed", name="managed-mixed")
    first = managed_audio(tmp_path, "managed-a")
    second = managed_audio(tmp_path, "managed-b")
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    assert conversion.assess(asset)["root"]["id"] == first["id"], "an exact-kind root beats a mixed one; the oldest wins"
    conversion.save_settings({"audiobook_output_root": second["id"]})
    assert conversion.assess(asset)["root"]["id"] == second["id"]
    # a chosen root that stopped being usable blocks with a reason instead of silently going elsewhere
    with db.tx() as conn:
        conn.execute("UPDATE root_folders SET management_mode='adopt' WHERE id=?", (second["id"],))
    info = conversion.assess(asset)
    assert info["code"] == "no_managed_root" and "Settings > Conversion" in info["reason"]


def test_the_destination_setting_only_accepts_managed_roots_of_the_kind(world, tmp_path):
    admin, ah = world["admin"]
    ro, *_ = ro_book(tmp_path, real=False)
    mg = managed_audio(tmp_path)
    eb = cf.root(tmp_path, "managed", "ebook", name="eb")
    for bad in (ro["id"], eb["id"], "no-such-root"):
        assert admin.put("/api/v1/settings/conversion", json={"audiobook_output_root": bad}, headers=ah).status_code == 422
    r = admin.put("/api/v1/settings/conversion", json={"audiobook_output_root": mg["id"]}, headers=ah)
    assert r.status_code == 200 and r.json()["audiobook_output_root"] == mg["id"]
    s = admin.get("/api/v1/settings/conversion").json()
    assert s["output_roots"]["audiobook"]["effective"]["id"] == mg["id"] and [x["id"] for x in s["output_roots"]["audiobook"]["roots"]] == [mg["id"]]
    assert admin.put("/api/v1/settings/conversion", json={"audiobook_output_root": ""}, headers=ah).json()["audiobook_output_root"] == ""
    assert s["auto_on_import"] is False


# ---------------------------------------------------------------------------
# Free space is the destination's, not the source's
# ---------------------------------------------------------------------------

def test_free_space_is_checked_on_the_destination_filesystem(tmp_path, monkeypatch):
    ro, work_id, asset_id, _ = ro_book(tmp_path, real=False)
    mg = managed_audio(tmp_path)
    seen = []

    def free(path):
        seen.append(str(path))
        return 10 * 1024 ** 4 if str(path).startswith(str(ro["path"])) else 100 * 1024 * 1024   # huge at the source, tiny at the destination

    monkeypatch.setattr(conversion, "free_bytes", free)
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    opt = conversion.options_for_assets([asset])[asset_id]
    assert opt["code"] == "no_space" and "Not enough free space" in opt["reason"] and mg["path"].name in opt["reason"]
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "no_space"
    assert seen and all(str(mg["path"]) in p for p in seen), seen
    assert list(mg["path"].iterdir()) == [] and db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0


# ---------------------------------------------------------------------------
# Safety: nothing under a read-only root is ever written
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_no_write_of_any_kind_happens_under_a_read_only_root(tmp_path, monkeypatch):
    ro, work_id, asset_id, files = ro_book(tmp_path)
    ro_e = cf.root(tmp_path, "observe", "ebook", name="observe-ebook")
    mobi = cf.make_mobi(ro_e["path"] / "Anne Author" / "Tiny Book" / "Tiny Book.mobi")
    ew, ea = cf.add_book(ro_e, "Tiny Book", "Anne Author", "ebook", [mobi])
    mg = managed_audio(tmp_path)
    cf.root(tmp_path, "managed", "ebook", name="managed-ebook")
    guarded = (str(ro["path"]), str(ro_e["path"]))
    violations: list[str] = []

    def under(p) -> bool:
        try:
            return os.path.abspath(os.fspath(p)).startswith(guarded)
        except TypeError:
            return False

    def guard(name, real, *positions):
        def wrapper(*a, **k):
            if any(i < len(a) and under(a[i]) for i in positions):
                violations.append(f"{name}{tuple(a[:2])}")
                raise PermissionError(f"write guard: {name}")
            return real(*a, **k)
        return wrapper

    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)) and set(mode) & set("wax+") and under(file):
            violations.append(f"open({file!r}, {mode!r})")
            raise PermissionError("write guard: open")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "replace", guard("replace", os.replace, 0, 1))
    monkeypatch.setattr(os, "rename", guard("rename", os.rename, 0, 1))
    monkeypatch.setattr(os, "link", guard("link", os.link, 0, 1))
    monkeypatch.setattr(os, "symlink", guard("symlink", os.symlink, 1))
    monkeypatch.setattr(os, "unlink", guard("unlink", os.unlink, 0))
    monkeypatch.setattr(os, "remove", guard("remove", os.remove, 0))
    monkeypatch.setattr(os, "rmdir", guard("rmdir", os.rmdir, 0))
    monkeypatch.setattr(os, "mkdir", guard("mkdir", os.mkdir, 0))
    monkeypatch.setattr(os, "makedirs", guard("makedirs", os.makedirs, 0))
    monkeypatch.setattr(shutil, "copyfile", guard("copyfile", shutil.copyfile, 1))
    real_mkdir = Path.mkdir
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: (violations.append(f"Path.mkdir({self})"), (_ for _ in ()).throw(PermissionError("write guard")))[1]
                        if under(self) else real_mkdir(self, *a, **k))
    # the guard itself works
    with pytest.raises(PermissionError):
        (ro["path"] / "probe").mkdir()
    violations.clear()

    before = snapshot(ro["path"]), snapshot(ro_e["path"])
    conversion.enqueue(asset_id)
    conversion.enqueue(ea)
    assert run_all()["done"] == 1 and run_all("ebook")["done"] == 1
    monkeypatch.undo()
    assert violations == []
    assert (snapshot(ro["path"]), snapshot(ro_e["path"])) == before
    assert (mg["path"] / AUTHOR / TITLE / f"{TITLE}.m4b").is_file()


# ---------------------------------------------------------------------------
# Blocked states stay visible with a reason (the book page shows them)
# ---------------------------------------------------------------------------

def test_drm_and_other_blocks_are_reported_not_hidden(tmp_path):
    ro_e = cf.root(tmp_path, "adopt", "ebook", name="adopt-ebook")
    cf.root(tmp_path, "managed", "ebook", name="managed-ebook")
    drm = cf.make_mobi(ro_e["path"] / "A" / "Locked" / "Locked.mobi", title="Locked", drm=True)
    pdf = ro_e["path"] / "A" / "Plain" / "Plain.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF" * 20)
    epub = ro_e["path"] / "A" / "Done" / "Done.epub"
    epub.parent.mkdir(parents=True)
    epub.write_bytes(b"PK" * 20)
    _, a_drm = cf.add_book(ro_e, "Locked", "A", "ebook", [drm])
    _, a_pdf = cf.add_book(ro_e, "Plain", "A", "ebook", [pdf])
    _, a_epub = cf.add_book(ro_e, "Done", "A", "ebook", [epub])
    opts = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (x,)) for x in (a_drm, a_pdf, a_epub)])
    assert opts[a_drm]["state"] == "blocked" and opts[a_drm]["code"] == "drm" and "DRM" in opts[a_drm]["reason"]
    assert opts[a_pdf]["code"] == "unsupported" and "PDF" in opts[a_pdf]["reason"]
    assert opts[a_epub]["code"] == "already_preferred"
    # none of the public options leaks a path
    assert not any(str(tmp_path) in str(v) for o in opts.values() for v in o.values())


def test_the_book_page_carries_the_destination_note_for_a_read_only_book(world, tmp_path):
    admin, ah = world["admin"]
    jane, jh = world["jane"]
    ro, work_id, asset_id, _ = ro_book(tmp_path, real=False)
    managed_audio(tmp_path)
    a = next(x for x in admin.get(f"/api/v1/library/{work_id}").json()["assets"] if x["id"] == asset_id)
    assert a["convert"]["state"] == "available" and a["convert"]["cross_root"] is True and a["convert"]["note"].startswith("Saved in your managed library")
    assert "convert" not in next(x for x in jane.get(f"/api/v1/library/{work_id}").json()["assets"] if x["id"] == asset_id)
    r = admin.post(f"/api/v1/assets/{asset_id}/convert", json={}, headers=ah)
    assert r.status_code == 202 and r.json()["created"] is True
    again = next(x for x in admin.get(f"/api/v1/library/{work_id}").json()["assets"] if x["id"] == asset_id)
    assert again["convert"]["conversion"]["status"] == "queued"


# ---------------------------------------------------------------------------
# Every consumer copes with a derived asset whose files live under another root
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_rescans_health_checks_and_cleanup_treat_the_cross_root_copy_as_the_derived_asset(tmp_path):
    ro, work_id, asset_id, files = ro_book(tmp_path, n=2)
    mg = managed_audio(tmp_path)
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    d = derived_of(asset_id)
    final = mg["path"] / AUTHOR / TITLE / f"{TITLE}.m4b"
    n_assets = db.one("SELECT COUNT(*) n FROM media_assets")["n"]
    n_files = db.one("SELECT COUNT(*) n FROM media_files")["n"]

    # scanner: walking the managed root does not turn the m4b into a new candidate or a duplicate asset
    run = scanner.scan_root(mg["id"])
    assert run["files_seen"] >= 1
    assert db.one("SELECT COUNT(*) n FROM scan_candidates WHERE path=?", (str(final),))["n"] == 0
    assert repair.reconcile_managed_files()["registered"] == 0
    assert db.one("SELECT COUNT(*) n FROM media_assets")["n"] == n_assets and db.one("SELECT COUNT(*) n FROM media_files")["n"] == n_files
    # ... and walking the adopted root does not touch the converted copy either
    scanner.scan_root(ro["id"])
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=?", (d["id"],))["n"] == 1

    ctx = maintenance.DetectorContext(10000, 60.0)
    # library health: no root mismatch, no file outside roots, nothing missing
    assert [f for f in detectors_db._db_path_mismatch(ctx)] == []
    assert [f for f in detectors_db._file_outside_roots(ctx)] == []
    assert [f for f in detectors_db._missing_file(ctx)] == []

    # redundant-files cleanup: the converted copy never makes the original redundant
    plan = redundantfiles.analyse(work_id)
    assert plan["groups"] == [] and any(n["code"] == "original_kept" for n in plan["notes"])

    # a real M4B arriving later supersedes the derivative; its file in the managed root goes with it
    with db.tx() as conn:
        paths = derived.supersede(conn, work_id, "audiobook", "m4b")
    derived.remove_generated_files(paths)
    assert not final.exists() and derived_of(asset_id) is None
    assert all(Path(f).exists() for f in files), "supersede never reaches into the read-only root"


@needs_ffmpeg
def test_library_payload_lists_both_assets_with_their_own_formats(world, tmp_path):
    admin, ah = world["admin"]
    ro, work_id, asset_id, _ = ro_book(tmp_path, n=2)
    managed_audio(tmp_path)
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    assets = {a["id"]: a for a in admin.get(f"/api/v1/library/{work_id}").json()["assets"]}
    d = derived_of(asset_id)
    assert assets[asset_id]["formats"] == ["mp3"] and assets[d["id"]]["formats"] == ["m4b"]
    assert assets[d["id"]]["derived_from_asset_id"] == asset_id and assets[d["id"]]["derived_label"] == "Converted from MP3"
    assert "convert" not in assets[d["id"]] or assets[d["id"]]["convert"] is None, "a converted copy is never offered conversion"


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

def test_bulk_dry_run_covers_read_only_books_with_categories_destinations_and_space(world, tmp_path):
    admin, ah = world["admin"]
    ro, w1, a1, _ = ro_book(tmp_path, real=False, title="One")
    mg = managed_audio(tmp_path)
    # a book that already has an M4B, and one that is DRM locked
    m4b = ro["path"] / AUTHOR / "Has" / "Has.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x" * 100)
    w3, _ = cf.add_book(ro, "Has", AUTHOR, "audiobook", [m4b])
    ro_e = cf.root(tmp_path, "adopt", "ebook", name="adopt-ebook")
    cf.root(tmp_path, "managed", "ebook", name="managed-ebook")
    drm = cf.make_mobi(ro_e["path"] / "A" / "Locked" / "Locked.mobi", title="Locked", drm=True)
    w4, _ = cf.add_book(ro_e, "Locked", "A", "ebook", [drm])
    mobi = cf.make_mobi(ro_e["path"] / "A" / "Tiny" / "Tiny.mobi")
    w5, _ = cf.add_book(ro_e, "Tiny", "A", "ebook", [mobi])

    r = admin.post("/api/v1/conversions/bulk", json={"work_ids": [w1, w3, w4, w5]}, headers=ah)
    plan = r.json()
    assert (plan["eligible"], plan["audio"], plan["ebook"], plan["into_managed_library"]) == (2, 1, 1, 2)
    assert plan["categories"] == {"will_convert": 2, "already_preferred": 1, "already_queued": 0, "blocked": 1}
    assert plan["blocked"]["drm"]["count"] == 1 and "DRM" in plan["blocked"]["drm"]["reason"]
    dests = {d["name"]: d for d in plan["destinations"]}
    assert set(dests) == {mg["path"].name, "managed-ebook"} and all(d["cross_root"] and d["free_bytes"] and d["fits"] for d in dests.values())
    assert plan["estimated_bytes"] == sum(d["estimated_bytes"] for d in plan["destinations"]) > 0
    assert plan["margin_bytes"] == 1024 * 1024 * 1024 and plan["fits"] is True
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0

    # the estimate must fit in free space minus the margin, else the start is refused (all or nothing)
    real_free = conversion.free_bytes
    conversion.free_bytes = lambda path: plan["margin_bytes"] + 10
    try:
        tight = admin.post("/api/v1/conversions/bulk", json={"work_ids": [w1, w3, w4, w5]}, headers=ah).json()
        assert tight["fits"] is False and tight["shortfall"]
        r = admin.post("/api/v1/conversions/bulk", json={"work_ids": [w1, w5], "dry_run": False, "confirm": True}, headers=ah)
        assert r.status_code == 409 and "Not enough free space" in r.json()["detail"]
        assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
    finally:
        conversion.free_bytes = real_free
    # a larger margin from Settings has the same effect, and room makes it go
    conversion.save_settings({"disk_margin_mb": 1_048_576})
    assert admin.post("/api/v1/conversions/bulk", json={"work_ids": [w1]}, headers=ah).json()["fits"] is (shutil.disk_usage(mg["path"]).free > 1_048_576 * 1024 * 1024 + 10 ** 6)
    conversion.save_settings({"disk_margin_mb": 1024})
    r = admin.post("/api/v1/conversions/bulk", json={"work_ids": [w1, w5], "dry_run": False, "confirm": True}, headers=ah)
    assert r.status_code == 200 and r.json()["queued"] == 2


def test_auto_convert_on_import_stays_off_by_default():
    assert conversion.DEFAULTS["auto_on_import"] is False and conversion.get_settings()["auto_on_import"] is False
