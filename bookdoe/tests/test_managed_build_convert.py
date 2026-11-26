"""CONVERT items of the managed library build: an MP3 set becomes ONE chaptered M4B and a MOBI becomes an EPUB, made by
the conversion engine's tools straight into the managed root and registered as derived assets (the same records a
normal conversion makes). The sources in the adopted roots are only read. Needs ffmpeg/ffprobe (CI installs them)."""
from __future__ import annotations

import json
import subprocess

import pytest

from app import db
from app.services import managedbuild as mb

import conv_fixtures as cf
import mb_fixtures as F

needs_ffmpeg = pytest.mark.skipif(not cf.HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(mb, "RETRY_BACKOFF_SECONDS", 0.0)
    mb.reset_capability_cache()


@pytest.fixture
def lib(tmp_path):
    return F.library(tmp_path)


def _mp3_set(lib, title="Watch the Skies", author="Eric Flint", n=3):
    files = [cf.make_mp3(lib["audiobooks"]["path"] / author / title / f"{i:02d}.mp3", seconds=1 + i % 2, freq=300 + 80 * i,
                         title=f"Chapter title {i}", track=i) for i in range(1, n + 1)]
    w, a = cf.add_book(lib["audiobooks"], title, author, "audiobook", files)
    with db.tx() as c:
        c.execute("UPDATE media_files SET adopted=1, managed=0 WHERE asset_id=?", (a,))
    return w, a, files


@needs_ffmpeg
def test_mp3_set_becomes_one_chaptered_m4b_derived_asset_in_the_managed_root(lib):
    w, a, files = _mp3_set(lib)
    before = F.snapshot(lib["audiobooks"]["path"])
    plan = mb.create_plan({})
    assert plan["summary"]["by_action"]["convert"]["files"] == 3 and plan["book_count"] == 1
    item = mb.list_items(plan["plan_id"])["items"][0]
    assert item["action"] == "convert" and item["dest_path"].endswith("/Eric Flint/Watch the Skies/Watch the Skies.m4b")
    mb.confirm_plan(plan["plan_id"], 1)
    run = mb.start(plan["plan_id"], submit=False)
    mb.execute(run["run_id"])
    st = mb.run_status(run["run_id"])
    assert st["status"] == "completed", st
    derived = db.one("SELECT * FROM media_assets WHERE derived_from_asset_id=?", (a,))
    assert derived and derived["format"] == "m4b" and derived["root_folder_id"] == lib["lib_a"]["id"]
    f = db.one("SELECT * FROM media_files WHERE asset_id=?", (derived["id"],))
    assert f["path"] == item["dest_path"] and f["managed"] == 1
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", f["path"]],
                                      capture_output=True, text=True, check=True).stdout)
    assert len(probe["chapters"]) == 3
    # the source asset and its MP3s are untouched and stay the owned asset
    assert F.snapshot(lib["audiobooks"]["path"]) == before
    assert sorted(r["path"] for r in db.all("SELECT path FROM media_files WHERE asset_id=?", (a,))) == sorted(map(str, files))
    assert db.one("SELECT status,trigger_source FROM conversions WHERE output_asset_id=?", (derived["id"],)) == {"status": "done", "trigger_source": "build"}
    assert {x["state"] for x in mb.work_state(w)["assets"]} == {"converted"}
    assert mb.coverage()["books_covered"] == 1
    assert F.temps_anywhere(lib["tmp"]) == []
    # a second dry run sees it is already converted into the managed library
    again = mb.create_plan({})
    assert again["book_count"] == 0 and again["summary"]["skips"]["already_converted"]["books"] == 1
    # undo removes the derived asset and moves the M4B to the managed trash; the MP3s stay
    res = mb.undo(work_id=w)
    assert res["undone_files"] == 1
    assert db.one("SELECT 1 x FROM media_assets WHERE derived_from_asset_id=?", (a,)) is None
    assert F.managed_files(lib) == [] and F.snapshot(lib["audiobooks"]["path"]) == before


@needs_ffmpeg
def test_mobi_becomes_an_epub_in_the_managed_root(lib):
    src = cf.make_mobi(lib["books"]["path"] / "Anne Author" / "Tiny Book.mobi", title="Tiny Book", author="Anne Author")
    w, a = cf.add_book(lib["books"], "Tiny Book", "Anne Author", "ebook", [src])
    before = F.snapshot(lib["books"]["path"])
    plan, run = F.build()
    assert plan["summary"]["by_action"]["convert"]["items"] == 1 and run["status"] == "completed", run
    d = db.one("SELECT * FROM media_assets WHERE derived_from_asset_id=?", (a,))
    f = db.one("SELECT path FROM media_files WHERE asset_id=?", (d["id"],))
    assert f["path"].endswith("/library-ebooks/Anne Author/Tiny Book/Tiny Book.epub")
    import zipfile
    with zipfile.ZipFile(f["path"]) as z:
        assert z.read("mimetype") == b"application/epub+zip"
    assert F.snapshot(lib["books"]["path"]) == before


def test_drm_mobi_is_copied_as_is_or_skipped_per_option(lib):
    src = cf.make_mobi(lib["books"]["path"] / "A" / "Locked.mobi", title="Locked", drm=True)
    cf.add_book(lib["books"], "Locked", "A", "ebook", [src])
    p1 = mb.create_plan({"non_convertible": "copy"})
    assert mb.list_items(p1["plan_id"])["items"][0]["action"] == "copy_as_is"
    p2 = mb.create_plan({"non_convertible": "skip"})
    it = mb.list_items(p2["plan_id"])["items"][0]
    assert it["action"] == "skip" and it["skip_code"] == "drm" and p2["summary"]["warnings"]


@needs_ffmpeg
def test_as_is_mode_copies_the_mp3s_unchanged(lib):
    w, a, files = _mp3_set(lib)
    plan, run = F.build({"mode": "as_is"})
    assert run["status"] == "completed"
    assert sorted(p.name for p in F.managed_files(lib)) == ["01.mp3", "02.mp3", "03.mp3"]
    assert db.one("SELECT 1 x FROM media_assets WHERE derived_from_asset_id=?", (a,)) is None


def test_a_book_that_already_has_an_epub_copies_that_and_does_not_convert_the_mobi(lib):
    w, a_epub, e = F.ebook(lib, "Dune", "Frank Herbert")
    m = cf.make_mobi(lib["books"]["path"] / "Frank Herbert" / "Dune.mobi", title="Dune")
    a_mobi = db.uid()
    with db.tx() as c:
        c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at) VALUES (?,?,'ebook','mobi','owned',?,?,?)",
                  (a_mobi, w, lib["books"]["id"], db.now(), db.now()))
        c.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,1,0,0,?)",
                  (db.uid(), a_mobi, str(m), m.stat().st_size, db.now()))
    items = {i["format"]: i for i in mb.list_items(mb.create_plan({"non_convertible": "skip"})["plan_id"])["items"]}
    assert items["epub"]["action"] == "copy" and items["mobi"]["action"] == "skip"
