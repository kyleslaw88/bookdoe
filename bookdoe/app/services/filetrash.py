"""Removing redundant files safely: a per-root trash folder with retention, restore and purge (admin only, audited).

DEFAULT-DENY. Design:
* A file in a MANAGED, enabled root is MOVED (never copied, never deleted) to
  ``<root>/.bookdoe-trash/<YYYY-MM-DD>/<its path relative to the root>``. The folder is created one level at a time
  with ``os.mkdir`` and checked with ``lstat``; a symlinked trash folder (or any symlinked folder in it) is refused, so
  nothing can be redirected into the library. Scans skip the trash folder.
* Nothing is ever moved over an existing file: moves use ``os.link`` (fails if the target exists) then ``unlink``, or an
  ``O_CREAT|O_EXCL`` copy that is verified before the source is unlinked where hard links are unavailable.
* A file in an adopted or observe root is the user's own: only its RECORD is removed and the file stays (``record_only``).
  A file outside every root is refused. Trash files of a root that later became adopted/observe are NEVER purged by the
  job (the root is no longer ours to change): they wait until the root is managed again or the folder is cleaned by hand.
* Before anything moves, the disk is re-read: the file to remove and the copy to keep must be regular files (no symlink in
  the path, one hard link, distinct inodes, non-empty, readable), the keeper must not be in the removal set, and every
  removed part must keep its own distinct copy (per kind, format and PART).
* Two phases: a ``file_trash`` row with status ``moving`` is committed FIRST, then the file moves, then one transaction
  deletes the file record and marks the row ``trashed``. A failure after the move moves everything back and marks the rows
  ``reverted``; a failed move-back leaves ``moving`` rows that ``reconcile`` finishes or reverts from what is on disk.
* Retention (``trash.retention_days``, default 30): the ``trash-purge`` job deletes expired trash files for good.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import db
from . import paths as path_guard
from . import redundantfiles

RETENTION_KEY = "trash.retention_days"
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
BULK_CONFIRM_AT = 10
EVENT_TRASHED, EVENT_FILE, EVENT_RESTORED, EVENT_PURGED = "files.trashed", "file.trashed", "files.restored", "files.purged"


REVERT_FLAG = " | revert requested"
RECONCILE_GRACE_SECONDS = 600           # a 'moving' row younger than this may belong to a removal still running
BUSY_KEY = "trash.busy"
_LOCK = threading.RLock()
_held = threading.local()


@contextmanager
def _exclusive():
    """One remove/restore/purge/reconcile at a time in this process, plus an advisory ``trash.busy`` stamp in the database
    so another process's reconcile leaves a fresh 'moving' row alone."""
    with _LOCK:
        depth = getattr(_held, "depth", 0)
        _held.depth = depth + 1
        if depth == 0:
            db.set_setting(BUSY_KEY, time.time())
        try:
            yield
        finally:
            _held.depth = depth
            if depth == 0:
                db.set_setting(BUSY_KEY, None)


class TrashError(ValueError):
    """A refused request; ``code`` is stable for the API and UI."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code, self.detail = code, detail


def retention_days() -> int:
    try:
        v = int(db.get_setting(RETENTION_KEY, DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(1, min(v, MAX_RETENTION_DAYS))


def set_retention_days(days: int) -> int:
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_RETENTION_DAYS:
        raise TrashError("bad_retention", f"retention must be a whole number of days between 1 and {MAX_RETENTION_DAYS}")
    old = retention_days()
    db.set_setting(RETENTION_KEY, days)
    db.add_history("trash.retention", f"Trash retention changed from {old} to {days} days", data={"old": old, "new": days})
    return days


def _user_id(user: Any) -> str | None:
    return user.get("id") if isinstance(user, dict) else user


# ---- file system primitives ------------------------------------------------------------------------

class MoveAborted(OSError):
    """A move was undone because the file or its folder changed under us (or the original could not be unlinked)."""


class LinksUnsupported(MoveAborted):
    """Hard links do not work on this storage (EXDEV, EPERM, ENOTSUP: FUSE, CIFS, exFAT). bookdoe never falls back to a
    copy, so the move is refused; restore has its own rename-based path."""


def _ident(path: str) -> tuple[int, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISREG(st.st_mode) else None


def _rel_to_root(path: str, root: str) -> Path | None:
    p = Path(os.path.abspath(path))
    for base in (Path(os.path.abspath(root)), Path(os.path.realpath(root))):
        try:
            return p.relative_to(base)
        except ValueError:
            continue
    return None


def _open_dir(root: str, parts: Iterable[str]) -> int:
    """A directory handle reached component by component with O_NOFOLLOW|O_DIRECTORY: a folder that is (or was swapped for)
    a symlink is refused instead of followed, and later swaps cannot redirect operations made through the handle."""
    fd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except OSError:
        os.close(fd)
        raise


def _ident_at(name: str, fd: int) -> tuple[int, int] | None:
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISREG(st.st_mode) else None


_DIR_FD_OK = (os.link in os.supports_dir_fd and os.unlink in os.supports_dir_fd and os.stat in os.supports_dir_fd
              and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"))    # decided once, from the real functions


def _can_dir_fd() -> bool:
    return _DIR_FD_OK


def _move_by_handles(root: str, src: str, dst: str, expect: tuple[int, int] | None) -> bool:
    """link+unlink through directory handles (see _open_dir). False when the paths are not under ``root`` or directory handles are
    not available (the caller uses the path-based link); LinksUnsupported / MoveAborted when it cannot or must not proceed."""
    rs, rd = _rel_to_root(src, root), _rel_to_root(dst, root)
    if rs is None or rd is None or not _can_dir_fd():
        return False
    try:
        sfd = _open_dir(root, rs.parent.parts)
    except OSError as exc:
        raise MoveAborted(f"a folder of {src} is a symlink or gone: {exc}") from exc
    try:
        try:
            dfd = _open_dir(root, rd.parent.parts)
        except OSError as exc:
            raise MoveAborted(f"a folder of {dst} is a symlink or gone: {exc}") from exc
        try:
            if expect is not None and _ident_at(rs.name, sfd) != tuple(expect):
                raise MoveAborted(f"{src} is not the file that was checked")
            try:
                os.link(rs.name, rd.name, src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
            except FileExistsError:
                raise
            except OSError as exc:
                raise LinksUnsupported(redundantfiles.LINKS_MESSAGE + f" ({exc.strerror or exc})") from exc
            if expect is not None and (_ident_at(rd.name, dfd) != tuple(expect) or _ident_at(rs.name, sfd) != tuple(expect)):
                try:
                    os.unlink(rd.name, dir_fd=dfd)
                except OSError:
                    pass
                raise MoveAborted(f"{src} changed while it was being moved; nothing was moved")
            try:
                os.unlink(rs.name, dir_fd=sfd)
            except OSError as exc:
                try:
                    os.unlink(rd.name, dir_fd=dfd)          # take the new name away again: the original stays exactly as it was
                except OSError:
                    pass
                raise MoveAborted(f"could not remove the original {src}: {exc}") from exc
            return True
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def move_no_clobber(src: str | Path, dst: str | Path, expect: tuple[int, int] | None = None, root: str | Path | None = None) -> None:
    """Move a file without ever replacing ``dst``: FileExistsError if it exists. With ``root`` the final steps run through
    directory handles opened with O_NOFOLLOW (a folder swapped for a symlink cannot redirect the unlink onto another file).
    Otherwise: hard-link then unlink. There is NO copy fallback: where hard links fail (LinksUnsupported) the move is refused. With ``expect`` (st_dev, st_ino) the source must be that very file before, after the link, and
    just before it is unlinked; otherwise the link is undone and MoveAborted raised. If the original cannot be unlinked the
    new name is removed again, so nothing is left half-moved."""
    src, dst = str(src), str(dst)
    if root is not None and _move_by_handles(str(root), src, dst, expect):
        return
    if expect is not None and _ident(src) != tuple(expect):
        raise MoveAborted(f"{src} is not the file that was checked")
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:       # no copy fallback: a copy has a new inode and cannot be moved back safely
        raise LinksUnsupported(redundantfiles.LINKS_MESSAGE + f" ({exc.strerror or exc})") from exc
    linked = True
    if expect is not None and (linked and _ident(dst) != tuple(expect) or _ident(src) != tuple(expect)):
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise MoveAborted(f"{src} changed while it was being moved; nothing was moved")
    try:
        os.unlink(src)
    except OSError as exc:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise MoveAborted(f"could not remove the original {src}: {exc}") from exc


def _renameat2_noreplace(src: str, dst: str) -> bool:
    """renameat2(RENAME_NOREPLACE) via libc. True if it renamed; FileExistsError if dst exists; False if unavailable here."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        return False
    AT_FDCWD, RENAME_NOREPLACE = -100, 1
    if fn(AT_FDCWD, os.fsencode(src), AT_FDCWD, os.fsencode(dst), RENAME_NOREPLACE) == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(dst)
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EPERM, errno.EXDEV):
        return False
    raise OSError(err, os.strerror(err), dst)


def _rename_move(src: str, dst: str) -> None:
    """Move by rename where hard links are unsupported (restore and move-back only). Never overwrites: the free-name check,
    then renameat2(NOREPLACE) where the filesystem has it, else a plain rename under the process lock; afterwards the
    destination must be the very file that was at the source and the source gone, or MoveAborted."""
    src, dst = str(src), str(dst)
    ident = _ident(src)
    if ident is None:
        raise FileNotFoundError(src)
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    if not _renameat2_noreplace(src, dst):
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.rename(src, dst)
    if _ident(dst) != ident or os.path.lexists(src):
        raise MoveAborted(f"{src} was not moved cleanly to {dst}")


def move_back(src: str | Path, dst: str | Path, expect: tuple[int, int] | None = None, root: str | Path | None = None) -> None:
    """Restore / roll-back move: the safe link move where links work, else the no-overwrite rename."""
    try:
        move_no_clobber(src, dst, expect, root)
    except LinksUnsupported:
        _rename_move(str(src), str(dst))


def _mkdirs(base: Path, parts: Iterable[str]) -> Path:
    """Create ``parts`` below ``base`` one folder at a time; refuse a symlink or a non-folder anywhere on the way."""
    cur = base
    for name in parts:
        cur = cur / name
        try:
            os.mkdir(cur)
        except FileExistsError:
            pass
        st = os.lstat(cur)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise TrashError("unsafe_trash_folder", "The trash folder (or a folder inside it) is a symlink or not a folder; refusing to use it.", path=str(cur))
    return cur


def _rel(path: str, root_path: str) -> Path:
    p = Path(os.path.abspath(path))
    for base in (Path(os.path.abspath(root_path)), Path(os.path.realpath(root_path))):
        try:
            return p.relative_to(base)
        except ValueError:
            continue
    raise TrashError("outside_managed_roots", "This file is outside its library root.", path=path)


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    cur = start
    try:
        while cur != stop and stop in cur.parents:
            cur.rmdir()
            cur = cur.parent
    except OSError:
        pass


def _file_row(file_id: str) -> dict[str, Any] | None:
    return db.one("SELECT f.*,a.work_id,a.kind asset_kind,a.format asset_format,a.root_folder_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.id=?", (file_id,))


# ---- removal -----------------------------------------------------------------------------------------

def _verify(work_id: str, to_do: list[str], entries: dict[str, dict[str, Any]], cache: dict | None = None) -> dict[str, dict[str, Any]]:
    """Re-read the disk for every removal and its keeper. Returns per-file facts or raises TrashError (nothing moved yet)."""
    removal = set(to_do)
    facts: dict[str, dict[str, Any]] = {}
    for fid in to_do:
        row = _file_row(fid)
        if row is None:
            raise TrashError("not_redundant", "A file record changed while preparing; nothing was removed.", file_ids=[fid])
        info = redundantfiles.root_for(row["path"])
        if info["root"] is None:
            raise TrashError("outside_managed_roots", "This file is outside every library root; it cannot be removed here.", file_id=fid)
        me = redundantfiles.inspect(row["path"], info["root"]["path"])
        if not me["ok"] or me["nlink"] != 1:
            raise TrashError("unsafe_file", f"This file cannot be removed safely ({me.get('why') or 'it has several hard links'}).", file_id=fid)
        keeper_id = entries[fid]["keeper_id"]
        if keeper_id in removal:
            raise TrashError("last_copy", "The copy to keep is also being removed.", file_id=fid)
        krow = _file_row(keeper_id)
        if krow is None or krow["asset_id"] != row["asset_id"]:
            raise TrashError("last_copy", "The copy to keep no longer exists; nothing was removed.", file_id=fid)
        if krow["part_number"] != row["part_number"] or krow["asset_kind"] != row["asset_kind"] or krow["asset_format"] != row["asset_format"]:
            raise TrashError("last_copy", "The copy to keep is not the same part; every part must keep its own copy. Nothing was removed.", file_id=fid)
        kinfo = redundantfiles.root_for(krow["path"])
        kroot = kinfo["root"]["path"] if kinfo["root"] else None
        keep = redundantfiles.inspect(krow["path"], kroot) if kroot else {"ok": False, "why": "outside_root"}
        if not keep["ok"] or keep["nlink"] != 1:
            raise TrashError("last_copy", f"The copy to keep is not a safe, present file ({keep.get('why') or 'hard links'}); nothing was removed.", file_id=fid)
        if (keep["dev"], keep["ino"]) == (me["dev"], me["ino"]):
            raise TrashError("last_copy", "The copy to keep is the same physical file; nothing was removed.", file_id=fid)
        if entries[fid].get("checksum_identical"):
            # The stored checksum only nominated these two; the bytes on disk decide, compared to each other.
            try:
                mine, theirs = redundantfiles.fresh_sha256(row["path"], None, cache), redundantfiles.fresh_sha256(krow["path"], None, cache)
            except redundantfiles.HashUnavailable as exc:
                raise TrashError("unverified", f"The files could not be verified byte for byte now ({exc}); nothing was removed.", file_id=fid)
            redundantfiles._record_fresh_hash(row["id"], row["sha256"], mine)
            redundantfiles._record_fresh_hash(krow["id"], krow["sha256"], theirs)
            if mine != theirs:
                raise TrashError("content_changed", "These files are no longer identical (one was changed since it was scanned); nothing was removed.", file_id=fid)
        facts[fid] = {"row": row, "info": info, "inode": (me["dev"], me["ino"]), "keeper": keeper_id, "keeper_inode": (keep["dev"], keep["ino"]), "keeper_path": krow["path"]}
    inodes = [f["inode"] for f in facts.values()]
    if len(set(inodes)) != len(inodes):
        raise TrashError("unsafe_file", "Two of these records are the same physical file; nothing was removed.")
    if any(f["keeper_inode"] in set(inodes) for f in facts.values()):
        raise TrashError("last_copy", "A copy to keep is also being removed; nothing was removed.")
    return facts


def remove_redundant(work_id: str, file_ids: Iterable[str], user: Any = None, *, confirm_similar: bool = False, confirm_count: int | None = None) -> dict[str, Any]:
    # Hash BEFORE taking the process lock (a big audiobook can take seconds and must not hold up the Trash page); inside the
    # lock only files whose (device, inode, size, mtime) changed since are read again.
    cache: dict = {}
    try:
        redundantfiles.analyse(work_id, hash_cache=cache)
    except Exception:       # the real run below reports any problem properly
        pass
    with _exclusive():
        return _remove_redundant(work_id, file_ids, user, confirm_similar=confirm_similar, confirm_count=confirm_count, cache=cache)


def _remove_redundant(work_id: str, file_ids: Iterable[str], user: Any = None, *, confirm_similar: bool = False, confirm_count: int | None = None, cache: dict | None = None) -> dict[str, Any]:
    """Remove the listed files of one book, each flagged by ``redundantfiles.analyse`` right now. All or nothing: every
    refusal happens before a file moves. Similar-only (not byte-identical) removals need ``confirm_similar``; ten or more
    files need ``confirm_count`` equal to how many."""
    ids = list(dict.fromkeys(i for i in file_ids if i))
    if not ids:
        raise TrashError("nothing_selected", "Choose at least one redundant file to remove.")
    if not db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
        raise TrashError("work_not_found", "No such book.")
    reconcile()
    analysis = redundantfiles.analyse(work_id, hash_cache=cache)
    flagged = {r["id"]: (g, r) for g in analysis["groups"] for r in g["remove"]}
    logged = {json.loads(r["file_json"]).get("id") for r in db.all("SELECT file_json FROM file_trash WHERE work_id=? AND status IN ('trashed','moving')", (work_id,))}
    ignored = {i["id"]: i for i in analysis["ignored"]}
    to_do = [i for i in ids if i in flagged]
    gone = [i for i in ids if i not in flagged and i in logged]
    for i in ids:
        if i in to_do or i in gone:
            continue
        if i in ignored:
            why = ignored[i]["why"]
            raise TrashError("outside_managed_roots" if why == "outside_every_root" else "unsafe_file",
                             f"This file is not eligible for removal ({why.replace('_', ' ')}).", file_id=i)
        raise TrashError("not_redundant", "These files are not flagged as redundant (any more); nothing was removed.", file_ids=[i])
    if not to_do:
        return {"batch_id": None, "removed": [], "already_removed": gone, "bytes_moved": 0, "bytes_record_only": 0}
    chosen = set(to_do)
    for g, _ in {id(g): (g, r) for g, r in flagged.values()}.values():
        group_ids = {r["id"] for r in g["remove"]}
        if group_ids & chosen and g["requires_all"] and not group_ids <= chosen:
            raise TrashError("whole_set_required", "Remove all of a set of parts or none: a book never ends up with a mix of two editions.")
    if any(not flagged[i][0]["checksum_identical"] for i in to_do) and not confirm_similar:
        raise TrashError("confirmation_required", "These files are only similar, not byte-identical. Confirm that you want them removed.", needs="similar")
    if len(to_do) >= BULK_CONFIRM_AT and confirm_count != len(to_do):
        raise TrashError("confirmation_required", f"Removing {len(to_do)} files needs the number typed to confirm.", needs="count", count=len(to_do))

    facts = _verify(work_id, to_do, {i: flagged[i][1] for i in to_do}, cache)
    batch, when = db.uid(), db.now()
    expires = when + retention_days() * 86400
    day = datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%d")
    plans = []
    for fid in to_do:
        g, entry = flagged[fid]
        row, info = facts[fid]["row"], facts[fid]["info"]
        root_real = Path(os.path.realpath(info["root"]["path"]))
        rel = _rel(row["path"], info["root"]["path"])
        record_only = not info["mutable"]
        dest = None if record_only else root_real / path_guard.TRASH_DIR_NAME / day / rel
        plans.append({"row": row, "group": g, "log_id": db.uid(), "root": info["root"], "root_real": root_real, "rel": rel, "dest": dest, "record_only": record_only,
                      "inode": facts[fid]["inode"], "keeper_inode": facts[fid]["keeper_inode"], "keeper_path": facts[fid]["keeper_path"]})
    for root_real in {p["root_real"] for p in plans if not p["record_only"]}:       # fail closed where hard links do not work
        ok, code, msg = redundantfiles.link_support(str(root_real))
        if not ok:
            raise TrashError(code, msg, root=str(root_real))
    if os.path.lexists(str(plans[0]["root_real"] / path_guard.TRASH_DIR_NAME)) and any(not p["record_only"] for p in plans):
        if os.path.islink(plans[0]["root_real"] / path_guard.TRASH_DIR_NAME):
            raise TrashError("unsafe_trash_folder", "The trash folder is a symlink; refusing to use it.")

    # phase 1: the log row exists (status 'moving') before any file moves
    with db.tx() as conn:
        for p in plans:
            row = p["row"]
            conn.execute(
                "INSERT INTO file_trash(id,batch_id,root_id,root_path,work_id,asset_id,asset_kind,asset_format,original_path,relative_path,trash_path,size_bytes,sha256,"
                "record_only,reason,keeper_file_id,file_json,status,trashed_by,trashed_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'moving',?,?,?)",
                (p["log_id"], batch, p["root"]["id"], str(p["root_real"]), work_id, row["asset_id"], row["asset_kind"], row["asset_format"], row["path"], str(p["rel"]),
                 str(p["dest"]) if p["dest"] else None, row["size_bytes"] or 0, row["sha256"], int(p["record_only"]),
                 f"{p['group']['code']}: {p['group']['reason']}", facts[row["id"]]["keeper"],
                 db.json_dump({k: row[k] for k in row.keys() if k not in ("work_id", "asset_kind", "asset_format", "root_folder_id")}), _user_id(user), when, expires))
    moved: list[dict[str, Any]] = []

    failing: dict[str, Any] = {}

    def roll_back(why: BaseException) -> None:
        stuck = []
        for p in reversed(moved):
            try:
                move_back(p["dest"], p["row"]["path"], p["inode"], p["root_real"])
                p["done_back"] = True
            except OSError:
                stuck.append(p)
        with db.tx() as conn:
            for p in plans:
                if p in stuck:
                    # left 'moving' but flagged: reconcile() must put it back, never complete it as a removal
                    conn.execute("UPDATE file_trash SET reason=COALESCE(reason,'') || ? WHERE id=?", (REVERT_FLAG, p["log_id"]))
                    continue
                if failing.get("all") or failing.get("id") == p["log_id"]:
                    conn.execute("UPDATE file_trash SET status='failed', reason=COALESCE(reason,'') || ? WHERE id=? AND status='moving'", (f" | move failed: {failing['why']}", p["log_id"]))
                else:
                    conn.execute("UPDATE file_trash SET status='reverted' WHERE id=? AND status='moving'", (p["log_id"],))

    try:
        for p in plans:
            if p["dest"] is not None:
                parent = _mkdirs(p["root_real"], [path_guard.TRASH_DIR_NAME, day, *p["rel"].parent.parts])
                final = parent / p["rel"].name
                n = 0
                while os.path.lexists(final):
                    n += 1
                    final = parent / f"{p['rel'].name}.{n}"
                if str(final) != str(p["dest"]):
                    p["dest"] = final
                    with db.tx() as conn:
                        conn.execute("UPDATE file_trash SET trash_path=? WHERE id=?", (str(final), p["log_id"]))
                failing["id"], failing["why"] = p["log_id"], "unknown"
                try:
                    move_no_clobber(p["row"]["path"], p["dest"], p["inode"], p["root_real"])
                except OSError as exc:
                    failing["why"] = str(exc)
                    raise
                failing.clear()
                moved.append(p)
        for p in plans:
            if not p["record_only"] and (_ident(p["keeper_path"]) != tuple(p["keeper_inode"]) or _ident(str(p["dest"])) != tuple(p["inode"])):
                failing.update(all=True, why=f"the copy to keep ({Path(p['keeper_path']).name}) changed or vanished while its twin was being moved")
                with db.tx() as conn:
                    conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (db.uid(), work_id, p["row"]["asset_id"], "files.alert", f"ALERT: cleanup stopped and rolled back: {failing['why']}",
                                  db.json_dump({"keeper_path": p["keeper_path"], "removed_path": p["row"]["path"], "batch_id": batch}), db.now()))
                raise MoveAborted(failing["why"])
        moved_bytes = sum(p["row"]["size_bytes"] or 0 for p in plans if not p["record_only"])
        record_bytes = sum(p["row"]["size_bytes"] or 0 for p in plans if p["record_only"])
        _finalize(plans, work_id, batch, when, user, moved_bytes, record_bytes)
    except BaseException as exc:
        roll_back(exc)
        if isinstance(exc, MoveAborted):
            raise TrashError("move_failed", f"Nothing was removed: {exc}") from exc
        raise
    for p in plans:
        if not p["record_only"]:
            _prune_empty_dirs(Path(p["row"]["path"]).parent, p["root_real"])
    return {"batch_id": batch, "removed": [{"trash_id": p["log_id"], "file_id": p["row"]["id"], "path": p["row"]["path"], "size_bytes": p["row"]["size_bytes"] or 0,
                                           "record_only": p["record_only"], "expires_at": expires, "trash_dir": str(p["root_real"] / path_guard.TRASH_DIR_NAME / day) if not p["record_only"] else None}
                                          for p in plans],
            "already_removed": gone, "bytes_moved": moved_bytes, "bytes_record_only": record_bytes, "retention_days": retention_days()}


def _finalize(plans: list[dict[str, Any]], work_id: str, batch: str, when: float, user: Any, moved_bytes: int, record_bytes: int) -> None:
    """Phase 3: one transaction deletes the file records, marks the rows trashed and writes the audit rows."""
    with db.tx() as conn:
        for p in plans:
            row = p["row"]
            conn.execute("DELETE FROM media_files WHERE id=?", (row["id"],))
            conn.execute("UPDATE file_trash SET status='trashed' WHERE id=? AND status='moving'", (p["log_id"],))
            conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                         (db.uid(), work_id, row["asset_id"], EVENT_FILE, f"Removed {Path(row['path']).name}" + (" (record only)" if p["record_only"] else " (moved to trash)"),
                          db.json_dump({"trash_id": p["log_id"], "batch_id": batch, "file_id": row["id"], "path": row["path"], "size_bytes": row["size_bytes"] or 0,
                                        "record_only": p["record_only"], "trash_path": str(p["dest"]) if p["dest"] else None, "by": _user_id(user)}), when))
        conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                     (db.uid(), work_id, None, EVENT_TRASHED,
                      f"Removed {len(plans)} redundant file(s): {moved_bytes} bytes moved to trash, {record_bytes} bytes left in place (record only)",
                      db.json_dump({"batch_id": batch, "by": _user_id(user), "bytes_moved": moved_bytes, "bytes_record_only": record_bytes,
                                    "files": [{"id": p["row"]["id"], "path": p["row"]["path"], "record_only": p["record_only"], "trash_id": p["log_id"]} for p in plans]}), when))


def reconcile(grace: float | None = None) -> dict[str, int]:
    """Settle ``moving`` rows left by a crash or a failed move-back, from what is on disk. Only rows older than the grace
    period (a removal still running owns younger ones) and never while another removal holds the advisory ``trash.busy``
    stamp. Origin only: reverted. Trash only: the move finished, the removal is completed. Both names: the same inode
    means the crash came between link and unlink (the trash name is removed, reverted); different files are left for a
    human. Neither: marked ``lost`` with an alert in the history."""
    grace = RECONCILE_GRACE_SECONDS if grace is None else grace
    out = {"completed": 0, "reverted": 0, "unresolved": 0, "lost": 0}
    stamp = db.get_setting(BUSY_KEY)
    if getattr(_held, "depth", 0) == 0 and isinstance(stamp, (int, float)) and time.time() - stamp < RECONCILE_GRACE_SECONDS:
        if _LOCK.acquire(blocking=False):           # nobody in this process: the stamp is another process's running removal
            _LOCK.release()
            return out
    with _exclusive():
        now = db.now()
        for r in db.all("SELECT * FROM file_trash WHERE status='moving'"):
            if now - r["trashed_at"] < grace:
                continue
            row = json.loads(r["file_json"] or "{}")
            origin_id = _ident(r["original_path"])
            trash_id = _ident(r["trash_path"]) if r["trash_path"] else None
            if r["record_only"]:
                with db.tx() as conn:
                    conn.execute("DELETE FROM media_files WHERE id=?", (row.get("id"),))
                    conn.execute("UPDATE file_trash SET status='trashed' WHERE id=?", (r["id"],))
                out["completed"] += 1
            elif origin_id and not trash_id:
                with db.tx() as conn:
                    conn.execute("UPDATE file_trash SET status='reverted' WHERE id=?", (r["id"],))
                out["reverted"] += 1
            elif trash_id and not origin_id and REVERT_FLAG in (r["reason"] or ""):
                # a rollback asked for this file back: never finish it as a removal. Put it back, or fail loudly.
                try:
                    move_back(r["trash_path"], r["original_path"], None, r["root_path"])
                    new = "reverted"
                except OSError:
                    new = "failed"
                with db.tx() as conn:
                    conn.execute("UPDATE file_trash SET status=? WHERE id=?", (new, r["id"]))
                    if new == "failed":
                        conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                                     (db.uid(), r["work_id"], r["asset_id"], "files.alert", f"ALERT: {Path(r['original_path']).name} could not be put back and is still in the trash",
                                      db.json_dump({"trash_id": r["id"], "trash_path": r["trash_path"], "original_path": r["original_path"]}), now))
                out["reverted" if new == "reverted" else "unresolved"] += 1
            elif trash_id and not origin_id:
                with db.tx() as conn:
                    conn.execute("DELETE FROM media_files WHERE id=?", (row.get("id"),))
                    conn.execute("UPDATE file_trash SET status='trashed' WHERE id=?", (r["id"],))
                out["completed"] += 1
            elif origin_id and trash_id and origin_id == trash_id:
                try:
                    os.unlink(r["trash_path"])
                except OSError:
                    out["unresolved"] += 1
                    continue
                with db.tx() as conn:
                    conn.execute("UPDATE file_trash SET status='reverted' WHERE id=?", (r["id"],))
                out["reverted"] += 1
            elif origin_id and trash_id:
                out["unresolved"] += 1
            else:
                with db.tx() as conn:
                    conn.execute("UPDATE file_trash SET status='lost' WHERE id=?", (r["id"],))
                    conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                                 (db.uid(), r["work_id"], r["asset_id"], "files.lost", f"ALERT: {Path(r['original_path']).name} is in neither its folder nor the trash",
                                  db.json_dump({"trash_id": r["id"], "original_path": r["original_path"], "trash_path": r["trash_path"]}), now))
                out["lost"] += 1
    return out


def files_in_trash_for(work_ids: Iterable[str], file_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Trash entries (waiting or mid-move) that belong to these Works or file ids."""
    wids, fids = list(work_ids), set(file_ids)
    out = []
    for r in db.all("SELECT id,work_id,original_path,size_bytes,status,file_json FROM file_trash WHERE status IN ('trashed','moving')"):
        if r["work_id"] in wids or json.loads(r["file_json"] or "{}").get("id") in fids:
            out.append({"trash_id": r["id"], "path": r["original_path"], "size_bytes": r["size_bytes"], "status": r["status"]})
    return out


def list_trash(status: str = "trashed", limit: int = 200) -> dict[str, Any]:
    if status not in ("moving", "trashed", "restored", "purged", "reverted", "all"):
        raise TrashError("bad_status", "status must be trashed, restored, purged, reverted or all")
    reconcile()
    where, params = ("", ()) if status == "all" else ("WHERE t.status=?", (status,))
    rows = db.all(f"SELECT t.*,w.title work_title FROM file_trash t LEFT JOIN works w ON w.id=t.work_id {where} ORDER BY t.trashed_at DESC LIMIT ?", (*params, max(1, min(limit, 1000))))  # nosec B608
    now = db.now()
    items = []
    for r in rows:
        items.append({"id": r["id"], "batch_id": r["batch_id"], "work_id": r["work_id"], "work_title": r["work_title"], "asset_kind": r["asset_kind"], "asset_format": r["asset_format"],
                      "original_path": r["original_path"], "relative_path": r["relative_path"], "size_bytes": r["size_bytes"], "record_only": bool(r["record_only"]),
                      "reason": r["reason"], "status": r["status"], "trashed_at": r["trashed_at"], "expires_at": r["expires_at"],
                      "days_left": max(0, int((r["expires_at"] - now) // 86400)) if r["status"] == "trashed" else None, "expired": r["status"] == "trashed" and r["expires_at"] <= now})
    live = [i for i in items if i["status"] == "trashed"]
    return {"items": items, "retention_days": retention_days(),
            "totals": {"files": len(live), "bytes_in_trash": sum(i["size_bytes"] for i in live if not i["record_only"]),
                       "bytes_record_only": sum(i["size_bytes"] for i in live if i["record_only"]), "expired": sum(1 for i in live if i["expired"])}}


def _current_work(work_id: str | None) -> str | None:
    """The Work that holds this id's data now: itself, or the canonical it was merged into."""
    seen = set()
    while work_id and work_id not in seen:
        seen.add(work_id)
        if db.one("SELECT 1 x FROM works WHERE id=?", (work_id,)):
            return work_id
        nxt = None
        for m in db.all("SELECT canonical_work_id,source_work_ids_json FROM work_merges WHERE status='executed'"):
            if work_id in db.json_load(m["source_work_ids_json"], []):
                nxt = m["canonical_work_id"]
                break
        work_id = nxt
    return None


def restore(trash_id: str, user: Any = None) -> dict[str, Any]:
    with _exclusive():
        return _restore(trash_id, user)


def _restore(trash_id: str, user: Any = None) -> dict[str, Any]:
    """Put a trashed file (or a record-only removal) back where it was. The file is linked into place without ever
    replacing anything, so two restores to one path cannot both win."""
    r = db.one("SELECT * FROM file_trash WHERE id=?", (trash_id,))
    if not r:
        raise TrashError("not_found", "No such trash entry.")
    if r["status"] == "restored":
        return {"trash_id": trash_id, "status": "restored", "already_restored": True}
    if r["status"] in ("purged", "reverted"):
        raise TrashError("purged" if r["status"] == "purged" else "not_trashed", "This file was purged and cannot be restored." if r["status"] == "purged" else "This removal was reverted; there is nothing to restore.")
    if r["status"] != "trashed":
        raise TrashError("not_trashed", "This removal is still in progress.")
    row = json.loads(r["file_json"] or "{}")
    if row.get("build_undo"):
        from . import managedbuild           # lazy: managedbuild imports this module
        if managedbuild.trashed_copy_originals_missing(row.get("managed_copy_id")):
            raise TrashError("build_undo", "This is a managed copy removed by 'Undo build', and its original is gone or changed, so this copy may be "
                             "the book's only file. It stays in the trash and is never purged; move it back into the library folder by hand "
                             "and rescan to add it again.")
        raise TrashError("build_undo", "This is a managed copy removed by 'Undo build'; its original is still in place. Run the build again to recreate it.")
    if not row.get("id") or not row.get("asset_id") or not row.get("path"):
        raise TrashError("corrupt_entry", "The saved file record is incomplete; it cannot be restored.")
    if db.one("SELECT 1 x FROM media_files WHERE path=? OR id=?", (r["original_path"], row["id"])):
        raise TrashError("path_in_use", "A file record already exists for this path (a rescan may have added it back).")
    work = _current_work(r["work_id"])
    if work is None:
        raise TrashError("work_gone", "The book this file belonged to no longer exists.")
    src = dest = None
    if not r["record_only"]:
        root_row = db.one("SELECT * FROM root_folders WHERE id=?", (r["root_id"],))
        if not root_row or root_row["management_mode"] != "managed" or not root_row["enabled"]:
            raise TrashError("root_not_managed", "The library root is no longer an enabled managed root; the file was left in trash.")
        root_real = Path(os.path.realpath(root_row["path"]))
        trash_root = path_guard.trash_root_for(root_real, r["trash_path"])
        if os.path.islink(trash_root):
            raise TrashError("unsafe_trash_folder", "The trash folder is a symlink; refusing to use it.")
        s = redundantfiles.inspect(r["trash_path"] or "", str(trash_root))
        if not s["ok"] or trash_root.name not in Path(os.path.abspath(r["trash_path"])).parts:
            raise TrashError("trash_file_missing", "The trashed file is no longer in the trash folder (or is not a plain file).")
        rel = _rel(r["original_path"], root_row["path"])
        dest_parent = root_real
        for name in rel.parent.parts:        # the original folders may have been removed: recreate them, but never through a symlink
            dest_parent = _mkdirs(dest_parent, [name])
        dest = dest_parent / rel.name
        src = Path(r["trash_path"])
        if os.path.lexists(dest):
            raise TrashError("destination_exists", "A file already exists at the original path; nothing was overwritten.")
    elif not os.path.lexists(r["original_path"]):
        raise TrashError("file_missing", "The file is no longer where it was; there is nothing to re-link.")
    t = db.now()
    if src is not None:
        try:
            move_back(src, dest, None, root_real)
        except FileExistsError:
            raise TrashError("destination_exists", "A file already exists at the original path; nothing was overwritten.")
        except FileNotFoundError:
            raise TrashError("trash_file_missing", "The trashed file is no longer in the trash folder.")
    try:
        with db.tx() as conn:
            claimed = conn.execute("UPDATE file_trash SET status='restored',restored_at=? WHERE id=? AND status='trashed'", (t, trash_id)).rowcount
            if claimed != 1:
                raise TrashError("not_trashed", "Another restore got there first.")
            asset = conn.execute("SELECT id,work_id FROM media_assets WHERE id=?", (row["asset_id"],)).fetchone()
            if not asset:
                asset = conn.execute("SELECT id,work_id FROM media_assets WHERE work_id=? AND kind=? AND format IS ?", (work, r["asset_kind"], r["asset_format"])).fetchone()
            if not asset:
                aid = db.uid()
                conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at) VALUES (?,?,?,?,'owned',?,?,?)",
                             (aid, work, r["asset_kind"], r["asset_format"], r["root_id"], t, t))
                asset = {"id": aid}
            row["asset_id"] = asset["id"]
            cols = [c for c in row if c in {x["name"] for x in conn.execute("PRAGMA table_info(media_files)").fetchall()}]
            conn.execute(f"INSERT INTO media_files({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])  # nosec B608
            conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                         (db.uid(), work, asset["id"], EVENT_RESTORED, f"Restored {Path(r['original_path']).name}",
                          db.json_dump({"trash_id": trash_id, "by": _user_id(user), "path": r["original_path"], "record_only": bool(r["record_only"])}), t))
    except BaseException:
        if src is not None:
            try:
                move_back(dest, src)
            except OSError:
                pass
        raise
    if src is not None:
        _prune_empty_dirs(src.parent, path_guard.trash_root_for(Path(os.path.realpath(r["root_path"])), r["trash_path"]))
    return {"trash_id": trash_id, "status": "restored", "already_restored": False, "path": r["original_path"], "work_id": work}


def purge(ids: list[str] | None = None, *, expired_only: bool = True, user: Any = None, now: float | None = None) -> dict[str, Any]:
    with _exclusive():
        return _purge(ids, expired_only=expired_only, user=user, now=now)


def _purge(ids: list[str] | None = None, *, expired_only: bool = True, user: Any = None, now: float | None = None) -> dict[str, Any]:
    """Delete trashed files for good. With no ids: every expired entry (the retention job). With ids: those entries now.
    Record-only entries only get their log closed. A file whose root is not a managed root any more, or whose trash path is
    not a plain file inside the root's real trash folder, is skipped, never deleted."""
    now = db.now() if now is None else now
    reconcile()
    if ids:
        rows = [r for i in ids if (r := db.one("SELECT * FROM file_trash WHERE id=?", (i,)))]
    else:
        rows = db.all("SELECT * FROM file_trash WHERE status='trashed' AND expires_at<=?", (now,)) if expired_only else db.all("SELECT * FROM file_trash WHERE status='trashed'")
    purged, skipped, freed = [], [], 0
    for r in rows:
        if r["status"] != "trashed":
            continue
        fj = json.loads(r["file_json"] or "{}")
        if fj.get("build_undo"):
            from . import managedbuild       # lazy: managedbuild imports this module
            gone = managedbuild.trashed_copy_originals_missing(fj.get("managed_copy_id"))
            if gone:
                skipped.append({"id": r["id"], "reason": f"kept: the original ({gone[0]}) is gone or changed, so this managed copy may be "
                                                         "the book's only file; it is never purged"})
                continue
        if not r["record_only"]:
            root_row = db.one("SELECT * FROM root_folders WHERE id=?", (r["root_id"],))
            if not root_row or root_row["management_mode"] != "managed":
                skipped.append({"id": r["id"], "reason": "its library root is not a managed root any more; the file is left where it is"})
                continue
            tp = Path(os.path.abspath(r["trash_path"] or ""))
            trash_root = path_guard.trash_root_for(Path(os.path.realpath(root_row["path"])), r["trash_path"])
            if os.path.islink(trash_root) or trash_root not in tp.parents:
                skipped.append({"id": r["id"], "reason": "the stored trash path is not inside the trash folder"})
                continue
            try:
                if os.path.lexists(tp):
                    if any(os.path.islink(par) for par in tp.parents if trash_root in par.parents or par == trash_root):
                        skipped.append({"id": r["id"], "reason": "a folder in the trash path is a symlink"})
                        continue
                    os.unlink(tp)
                _prune_empty_dirs(tp.parent, trash_root)
            except OSError as exc:
                skipped.append({"id": r["id"], "reason": str(exc)})
                continue
            freed += r["size_bytes"] or 0
        with db.tx() as conn:
            conn.execute("UPDATE file_trash SET status='purged',purged_at=? WHERE id=? AND status='trashed'", (now, r["id"]))
        purged.append(r["id"])
    if purged:
        db.add_history(EVENT_PURGED, f"Purged {len(purged)} trashed file(s), {freed} bytes freed",
                       data={"trash_ids": purged, "bytes_freed": freed, "by": _user_id(user) or "trash-purge job", "explicit": bool(ids)})
    return {"purged": len(purged), "trash_ids": purged, "skipped": skipped, "bytes_freed": freed}


def purge_job() -> dict[str, Any]:
    """Scheduler entry (job kind ``trash-purge``): purge what has outlived the retention."""
    return purge()
