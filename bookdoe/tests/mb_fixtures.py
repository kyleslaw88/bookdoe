"""Shared fixtures for the managed library build tests: a real temp library with two adopted (read-only) roots and two
managed roots, books with real files, and helpers that run a whole build synchronously in the test thread."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app import db
from app.services import managedbuild as mb

import conv_fixtures as cf


def templates() -> None:
    with db.tx() as c:
        c.execute("UPDATE naming_templates SET folder_template='{Author}/{Title}', file_template='{Title}.{Ext}', "
                  "multipart_template='{Title} - {Part:02}.{Ext}'")


def allow_runs(on: bool = True) -> None:
    """The admin switch managed_build.run_enabled (default OFF); the engine tests run builds, so they switch it on."""
    db.set_setting("managed_build.run_enabled", on)


def library(tmp_path: Path) -> dict:
    templates()
    allow_runs()
    return {"books": cf.root(tmp_path, "adopt", "ebook", "books"), "audiobooks": cf.root(tmp_path, "adopt", "audiobook", "audiobooks"),
            "lib_e": cf.root(tmp_path, "managed", "ebook", "library-ebooks"), "lib_a": cf.root(tmp_path, "managed", "audiobook", "library-audiobooks"),
            "tmp": tmp_path}


def _adopted(asset_id: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE media_files SET adopted=1, managed=0 WHERE asset_id=?", (asset_id,))


def ebook(lib: dict, title: str, author: str, ext: str = "epub", data: bytes | None = None, folder: str | None = None) -> tuple[str, str, Path]:
    p = lib["books"]["path"] / (folder or author) / f"{title}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else (title.encode() + os.urandom(64)) * 50)
    w, a = cf.add_book(lib["books"], title, author, "ebook", [p])
    _adopted(a)
    return w, a, p


def audioset(lib: dict, title: str, author: str, n: int = 3, ext: str = "m4b", size: int = 4000) -> tuple[str, str, list[Path]]:
    paths = []
    for i in range(1, n + 1):
        p = lib["audiobooks"]["path"] / author / title / f"{i:02d}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))
        paths.append(p)
    w, a = cf.add_book(lib["audiobooks"], title, author, "audiobook", paths)
    _adopted(a)
    with db.tx() as c:
        for i, p in enumerate(paths, 1):
            c.execute("UPDATE media_files SET duration_seconds=? WHERE path=?", (60 * i, str(p)))
    return w, a, paths


def snapshot(*roots: Path) -> dict[str, tuple]:
    """Everything a write could change: per file sha256, size, mtime_ns, inode, mode; per folder mtime_ns, inode, listing."""
    out: dict[str, tuple] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            st = os.lstat(dirpath)
            out[dirpath] = ("dir", st.st_mtime_ns, st.st_ino, st.st_mode, tuple(sorted(dirnames + filenames)))
            for f in filenames:
                p = os.path.join(dirpath, f)
                st = os.lstat(p)
                h = hashlib.sha256(Path(p).read_bytes()).hexdigest() if not os.path.islink(p) else os.readlink(p)
                out[p] = ("file", h, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode, st.st_nlink)
    return out


def plan_and_confirm(options: dict | None = None) -> dict:
    plan = mb.create_plan(options or {})
    mb.confirm_plan(plan["plan_id"], plan["book_count"])
    return plan


def build(options: dict | None = None) -> tuple[dict, dict]:
    """Plan, confirm, start (no background thread) and execute in this thread. Returns (plan, final run status)."""
    plan = plan_and_confirm(options)
    run = mb.start(plan["plan_id"], submit=False)
    mb.execute(run["run_id"])
    return plan, mb.run_status(run["run_id"])


def restart_and_resume(run_id: str) -> dict:
    """What a container restart does (startup recovery), then Resume and run to the end."""
    mb.on_startup()
    assert db.one("SELECT status FROM build_runs WHERE id=?", (run_id,))["status"] in ("interrupted", "paused", "stopped")
    mb.resume(run_id, submit=False)
    mb.execute(run_id)
    return mb.run_status(run_id)


def temps_anywhere(root: Path) -> list[str]:
    return [str(p) for p in root.rglob(f"{mb.TEMP_PREFIX}*")]


def managed_files(lib: dict) -> list[Path]:
    out = []
    for key in ("lib_e", "lib_a"):
        base = lib[key]["path"]
        out += [p for p in base.rglob("*") if p.is_file() and ".bookdoe-trash" not in p.parts]
    return sorted(out)


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Crash(BaseException):
    """A simulated kill: not an Exception, so no cleanup handler runs, exactly like a process dying."""
