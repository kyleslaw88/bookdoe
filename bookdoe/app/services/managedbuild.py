"""Managed library build (#219, #222, #223, #224): copy (or convert) every book of the adopted, read-only libraries
into the MANAGED roots, verified, resumable and undoable, without ever touching the originals.

Stages and invariants
=====================
* PLAN (``create_plan``) is read-only: it stats files and reads the database, writes only plan rows. Per book (asset)
  it decides COPY (already a preferred format: EPUB, M4B), CONVERT (MOBI/AZW/AZW3 -> EPUB, an MP3/M4A/FLAC/Ogg/Opus set
  -> one chaptered M4B, through the conversion engine), COPY AS-IS (no preferred target, not convertible) or SKIP with a
  reason. Items live in ``build_plan_items`` (paginated, filtered); the summary is computed with SQL. Each item records
  the source signature (size, mtime_ns, inode, device); a source that changed since planning is refused.
* RUN (``execute``) is the body of the manual-only ``managed-library-build`` job (thread pool, lease renewed while it
  works, one run at a time). A book is the unit of work: all its files are copied, verified and published first, then
  ONE transaction switches its records. Per file: re-stat the source, copy to ``.alx-build-<run>-<item>-<random>.part``
  INSIDE the destination folder (created one level at a time inside the managed root, never through a symlink) while hashing,
  fsync, verify (size always; ``full`` re-reads the copy and compares sha256), publish with a no-clobber primitive
  (hard link then unlink, or renameat2(RENAME_NOREPLACE)); a filesystem that offers neither stops the run (fail
  CLOSED, never a racy exists-check + rename), then re-stat the source (changed during the copy: discarded, retried).
* WRITES happen only under managed roots and only through directory handles: folders are made with mkdirat and opened
  with O_NOFOLLOW|O_DIRECTORY one level at a time from the managed root (``_ensure_dirs``, ``_open_managed_dir``), files
  are created with O_CREAT|O_EXCL|O_NOFOLLOW through that handle and removed through it (``_unlink_if`` refuses any path
  outside a managed root). Conversion tools get the already-open output file (/proc/self/fd/N or a descriptor), never a
  name in the managed root. Adopted and observe roots are opened read-only (O_RDONLY|O_NOFOLLOW), never written, renamed,
  linked into or given temp files.
* RUNNING needs the admin switch ``managed_build.run_enabled`` (default off; switching it off pauses a running build
  after the current file); an unexpected error stops the run
  (``_abort_run``) instead of leaving it 'running'.
* JOURNAL: item states pending -> copying -> publishing -> published -> done are committed before the step they
  announce, so ``recover_run`` can always settle a crash: temp leftovers are removed, a link that happened is kept, a
  published-but-unrecorded book is recorded on resume. The original stays the active file until its book is switched.
* RECORDS: the active media_files row keeps its id and is re-pointed at the managed copy; the original is kept as a
  preserved source in ``managed_copies`` (never listed, counted, offered for removal or edited). See docs/DOMAIN_MODEL.md.
* UNDO moves the managed copies of a run or a book into the managed root's trash (``_move_to_trash``: directory
  handles, inode checked, no-clobber link or renameat2 NOREPLACE, refused otherwise) and points the records back at the
  originals, one transaction per book. The originals are never touched. A book whose original(s) are gone (for a
  converted book: any of its sources) or whose copy was renamed/moved is refused; a converted asset is deleted only if
  the build created it and it has no history. The trash never purges an undone copy whose original has gone since.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import db
from . import convert_audio, convert_ebook, derived, filetrash, redundantfiles
from . import paths as path_guard

log = logging.getLogger(__name__)

JOB_KIND = "managed-library-build"
TEMP_PREFIX = ".alx-build-"
TEMP_SUFFIX = ".part"
CHUNK_BYTES = 8 * 1024 * 1024
PLAN_TTL_SECONDS = 24 * 3600
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 30.0
SPACE_MARGIN_FRACTION = 0.05
SPACE_MARGIN_MIN_BYTES = 1024 ** 3
DEFAULT_RATE_BPS = 40 * 1024 * 1024        # assumed until a run has measured the real throughput
AUDIO_ENCODE_SPEED = 25.0                   # seconds of audio encoded per second (rough; the dry run says so)
FLUSH_SECONDS = 2.0
LEASE_TOUCH_SECONDS = 5.0
MAX_PAGE = 500
PLAN_INSERT_BATCH = 2000
ACTIONABLE = ("copy", "convert", "copy_as_is")
ACTIVE_RUN = ("running", "paused", "interrupted", "stopped")
RESUMABLE = ("paused", "interrupted", "stopped")
PREFERRED = derived.PREFERRED_FORMAT
MEASURED_KEY = "managed_build.measured_bps"
RUN_ENABLED_KEY = "managed_build.run_enabled"
RUN_DISABLED_MESSAGE = ("Running a managed library build is switched off. An admin can switch it on in the build panel "
                        "(\"Allow builds to run\"). Dry runs, plans and Undo of finished work keep working while it is off.")
SWITCHED_OFF_REASON = ("Paused because \"Allow builds to run\" was switched off; switch it on and press Resume to continue")
WINDOWS_RESERVED ={"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_COMPONENT_BYTES = 240
MAX_PATH_BYTES = 4000

# Seams for the tests (a fake clock for the throttle); production uses the real ones.
_clock: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

SKIP_REASONS = {
    "missing": "The file is not on disk (or its record is marked missing)",
    "unreadable": "The file cannot be read",
    "symlink": "The file (or a folder above it) is a symbolic link; links are never followed",
    "not_regular": "Not a regular file",
    "outside_roots": "The file is outside every library root",
    "root_disabled": "Its library root is disabled",
    "no_managed_root": "There is no enabled managed root for this kind of book",
    "no_template": "There is no naming template for this kind of book",
    "not_convertible": "No preferred format to convert to (kept as it is only when 'non-convertible: copy')",
    "drm": "DRM-protected; it cannot be converted",
    "already_converted": "Already converted into the managed library",
    "conversion_queued": "A conversion of this book is queued or running; let it finish first",
    "path_too_long": "The destination path would be too long for the filesystem",
    "mixed_roots": "The files of this book are spread over several roots",
}


class BuildRefused(ValueError):
    """A request that cannot be done now; ``code`` is stable for the API and UI."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code, self.detail = code, detail


class _ItemError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, stop_run: str | None = None):
        super().__init__(message)
        self.code, self.retryable, self.stop_run = code, retryable, stop_run


class _Interrupted(Exception):
    """Pause or cancel was requested: stop at this safe point (between files)."""


class PublishUnsupported(_ItemError):
    """No no-clobber publish (hard links or renameat2 NOREPLACE) on this filesystem: fail closed."""

    def __init__(self, message: str):
        super().__init__("links_unsupported", message, stop_run="links_unsupported")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

DEFAULT_OPTIONS: dict[str, Any] = {
    "mode": "preferred", "non_convertible": "copy", "throttle_mb_s": 50, "verify": "full", "concurrency": 1,
    "stop_on_error": False, "include": {},
}
INCLUDE_KEYS = ("kinds", "root_ids", "work_ids", "author_ids", "series_ids", "tag_ids", "limit_books")


def validate_options(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults filled in, every value checked. Raises BuildRefused('bad_options')."""
    raw = dict(raw or {})
    unknown = set(raw) - set(DEFAULT_OPTIONS)
    if unknown:
        raise BuildRefused("bad_options", f"Unknown option(s): {', '.join(sorted(unknown))}")
    o = {**DEFAULT_OPTIONS, **{k: v for k, v in raw.items() if v is not None}}
    if o["mode"] not in ("preferred", "as_is"):
        raise BuildRefused("bad_options", "mode must be 'preferred' or 'as_is'")
    if o["non_convertible"] not in ("copy", "skip"):
        raise BuildRefused("bad_options", "non_convertible must be 'copy' or 'skip'")
    if o["verify"] not in ("full", "size", "none"):
        raise BuildRefused("bad_options", "verify must be 'full', 'size' or 'none'")
    t = o["throttle_mb_s"]
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 5000:
        raise BuildRefused("bad_options", "throttle_mb_s must be between 0 (no limit) and 5000")
    c = o["concurrency"]
    if isinstance(c, bool) or c not in (1, 2):
        raise BuildRefused("bad_options", "concurrency must be 1 or 2")
    if not isinstance(o["stop_on_error"], bool):
        raise BuildRefused("bad_options", "stop_on_error must be true or false")
    inc = o["include"] or {}
    if not isinstance(inc, dict) or set(inc) - set(INCLUDE_KEYS):
        raise BuildRefused("bad_options", f"include accepts only {', '.join(INCLUDE_KEYS)}")
    clean: dict[str, Any] = {}
    for k in INCLUDE_KEYS[:-1]:
        v = inc.get(k)
        if v in (None, []):
            continue
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(v) > 5000:
            raise BuildRefused("bad_options", f"include.{k} must be a list of ids")
        clean[k] = list(dict.fromkeys(v))
    if "kinds" in clean and set(clean["kinds"]) - {"ebook", "audiobook"}:
        raise BuildRefused("bad_options", "include.kinds may only hold 'ebook' and 'audiobook'")
    lim = inc.get("limit_books")
    if lim is not None:
        if isinstance(lim, bool) or not isinstance(lim, int) or lim < 1:
            raise BuildRefused("bad_options", "include.limit_books must be a positive whole number")
        clean["limit_books"] = lim
    o["include"] = clean
    o["throttle_mb_s"] = float(t)
    return o


# ---------------------------------------------------------------------------
# Small file-system helpers
# ---------------------------------------------------------------------------

class _Roots:
    """All roots, longest path first, for a lexical path -> root lookup without a query per file."""

    def __init__(self) -> None:
        rows = db.all("SELECT * FROM root_folders")
        self.by_id = {r["id"]: r for r in rows}
        self.items = sorted(((os.path.normpath(r["path"]), r) for r in rows), key=lambda t: -len(t[0]))

    def for_path(self, path: str) -> dict[str, Any] | None:
        p = os.path.normpath(path)
        for rp, r in self.items:
            if p == rp or p.startswith(rp.rstrip(os.sep) + os.sep):
                return r
        return None


def managed_root_for(kind: str) -> dict[str, Any] | None:
    """The managed root that receives books of ``kind``: the oldest enabled managed root of that kind, else a 'mixed' one.
    (Mirrors conversion.destination_root from PR #353, minus its Settings override; adopt that helper once it is on main.)"""
    for wanted in (kind, "mixed"):
        row = db.one("SELECT * FROM root_folders WHERE enabled=1 AND management_mode='managed' AND media_kind=? "
                     "ORDER BY created_at, id LIMIT 1", (wanted,))
        if row:
            return row
    return None


def _sig(path: str) -> dict[str, int] | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "dev": st.st_dev, "mode": st.st_mode}


def _same_sig(a: dict[str, Any] | None, size: int, mtime_ns: int, ino: int | None, dev: int | None) -> bool:
    if not a:
        return False
    return a["size"] == size and a["mtime_ns"] == mtime_ns and (ino is None or a["ino"] == ino) and (dev is None or a["dev"] == dev)


def _symlinked_parent(path: str, root_path: str, cache: dict[str, bool] | None = None) -> bool:
    """True when a folder between the root and the file is a symlink (links are never followed)."""
    root = os.path.normpath(root_path)
    cur = os.path.dirname(os.path.normpath(path))
    while cur and cur != root and cur.startswith(root):
        if cache is not None and cur in cache:
            if cache[cur]:
                return True
        else:
            hit = os.path.islink(cur)
            if cache is not None:
                cache[cur] = hit
            if hit:
                return True
        cur = os.path.dirname(cur)
    return False


def _free_bytes(path: str) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(str(probe)).free


def _margin(required: int) -> int:
    return max(SPACE_MARGIN_MIN_BYTES, int(required * SPACE_MARGIN_FRACTION))


def _fit_component(name: str) -> str:
    """One path component: NFC, no trailing dots/spaces, no Windows device name, at most MAX_COMPONENT_BYTES (the
    extension is kept)."""
    name = unicodedata.normalize("NFC", name).rstrip(" .") or "_"
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        stem, ext = name, ""
    if stem.split(".")[0].lower() in WINDOWS_RESERVED:
        stem = stem + "_"
    tail = ("." + ext) if ext else ""
    while len((stem + tail).encode("utf-8")) > MAX_COMPONENT_BYTES and stem:
        stem = stem[:-1]
    stem = stem.rstrip(" .") or "_"
    return stem + tail


def _suffixed(name: str, n: int) -> str:
    if n <= 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 8:
        return _fit_component(f"{name} ({n})")
    return _fit_component(f"{stem} ({n}).{ext}")


_O_DIR = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

Made = list[tuple[tuple[str, ...], str]]          # folders created by _ensure_dirs: (parent parts below the root, name)


def _ensure_dirs(root_real: str, parts: Iterable[str]) -> tuple[int, Made]:
    """Create ``parts`` below the managed root one level at a time THROUGH DIRECTORY HANDLES (mkdirat + openat with
    O_NOFOLLOW|O_DIRECTORY): a symlink or a file on the way is refused, never followed, and a folder swapped for a link
    after it was checked cannot redirect anything. Each new folder's parent is fsynced so the entry is durable before
    a file is written into it. Returns (handle of the deepest folder, folders made); the caller closes the handle. On a
    failure the folders made so far are pruned again."""
    made: Made = []
    walked: list[str] = []
    fd = os.open(root_real, _O_DIR)
    try:
        for name in parts:
            if name in ("", ".", "..") or os.sep in name or (os.altsep and os.altsep in name):
                raise _ItemError("outside_root", "Refusing: the destination folder escapes the managed root")
            try:
                os.mkdir(name, 0o755, dir_fd=fd)
                made.append((tuple(walked), name))
                os.fsync(fd)                    # the new folder's entry is on the disk before anything goes into it
            except FileExistsError:
                pass
            try:
                nfd = os.open(name, _O_DIR | _O_NOFOLLOW, dir_fd=fd)
            except OSError:
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    raise
                if stat.S_ISLNK(st.st_mode):
                    raise _ItemError("symlink", f"Refusing: {name!r} in the destination folder is a symbolic link")
                if not stat.S_ISDIR(st.st_mode):
                    raise _ItemError("not_a_folder", f"Refusing: {name!r} in the destination folder is a file")
                raise
            os.close(fd)
            fd = nfd
            walked.append(name)
        return fd, made
    except Exception:
        os.close(fd)
        _prune(root_real, made)
        raise


def _prune(root_real: str, made: Made) -> None:
    """Remove (empty) folders _ensure_dirs made, deepest first, through directory handles."""
    for parent, name in reversed(made):
        try:
            pfd = filetrash._open_dir(root_real, parent)
        except OSError:
            break
        try:
            os.rmdir(name, dir_fd=pfd)
        except OSError:
            break
        finally:
            os.close(pfd)


def _open_managed_dir(dir_path: str) -> int | None:
    """A handle on ``dir_path`` reached from its MANAGED root with O_NOFOLLOW at every level, or None when the folder
    is not inside a managed root (or a folder on the way is a link or gone). Only managed roots are ever written."""
    for r in db.all("SELECT path FROM root_folders WHERE management_mode='managed' ORDER BY length(path) DESC"):
        rel = filetrash._rel_to_root(dir_path, r["path"])
        if rel is None or ".." in rel.parts:
            continue
        try:
            return filetrash._open_dir(r["path"], rel.parts)
        except OSError:
            return None
    return None


def _hash_fd(fd: int) -> str:
    h = hashlib.sha256()
    off = 0
    while True:
        b = os.pread(fd, CHUNK_BYTES, off)
        if not b:
            break
        h.update(b)
        off += len(b)
    return h.hexdigest()


def _temp_name(run_id: str, item_id: int) -> str:
    """An unpredictable temp name (random suffix): nobody can plant a link at it in advance."""
    return f"{TEMP_PREFIX}{run_id[:8]}-{item_id}-{secrets.token_hex(8)}{TEMP_SUFFIX}"


def _is_our_temp(path: str | None, roots: _Roots | None = None) -> bool:
    """A temp file this module made: our name pattern, inside an enabled managed root."""
    if not path:
        return False
    name = os.path.basename(path)
    if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
        return False
    r = (roots or _Roots()).for_path(path)
    return bool(r and r["management_mode"] == "managed" and r["enabled"])


def _unlink_if(path: str, ident: tuple[int, int] | None = None) -> bool:
    """Remove ``path`` if it is a regular file (and, with ``ident``, that very file) INSIDE A MANAGED ROOT: the folder is
    opened from the root with O_NOFOLLOW at every level and the file is checked and unlinked through that handle.
    A path outside the managed roots is never removed."""
    if not path:
        return False
    fd = _open_managed_dir(os.path.dirname(path))
    if fd is None:
        return False
    name = os.path.basename(path)
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (ident is not None and (st.st_dev, st.st_ino) != tuple(ident)):
            return False
        os.unlink(name, dir_fd=fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _ident_str(dev: int, ino: int) -> str:
    return f"{dev}:{ino}"


def _parse_ident(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    a, _, b = s.partition(":")
    try:
        return int(a), int(b)
    except ValueError:
        return None


_LIBC: Any = None


def _renameat2_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> bool:
    """renameat2(RENAME_NOREPLACE) through directory handles. True when renamed; FileExistsError when the name is taken;
    False when the kernel/filesystem does not offer it."""
    global _LIBC
    try:
        if _LIBC is None:
            _LIBC = ctypes.CDLL(None, use_errno=True)
        fn = _LIBC.renameat2
    except (OSError, AttributeError):
        return False
    if fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(dst)
    if err in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV):
        return False
    raise OSError(err, os.strerror(err), dst)


_CAP: dict[str, tuple[str | None, str]] = {}
UNSUPPORTED_MESSAGE = ("This managed root cannot publish files without risking an overwrite (no hard links and no "
                       "renameat2 NOREPLACE). On Unraid enable hard-link support for the share (Settings > Global Share "
                       "Settings > Tunable (support Hard Links)) or point the managed root at a disk path; nothing was copied.")


def publish_capability(root_path: str, *, refresh: bool = False) -> tuple[str | None, str]:
    """('link'|'renameat2'|None, message): how this managed root can publish without ever replacing a file. Probed once
    per root with throwaway files inside its trash folder (the only place a probe may write), then cached."""
    real = os.path.realpath(root_path)
    if not refresh and real in _CAP:
        return _CAP[real]
    ok, code, _msg = redundantfiles.link_support(real)
    if ok:
        result: tuple[str | None, str] = ("link", "")
    elif code == "unsafe_trash_folder":
        return (None, "The trash folder of this managed root is a symlink or not a folder; refusing to use it.")
    else:
        result = (None, UNSUPPORTED_MESSAGE)
        probe = os.path.join(real, path_guard.TRASH_DIR_NAME)
        try:
            os.makedirs(probe, exist_ok=True)
            if os.path.islink(probe):
                raise OSError("symlinked trash folder")
            a, b = os.path.join(probe, f".rn-{db.uid()}"), os.path.join(probe, f".rn-{db.uid()}")
            os.close(os.open(a, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            fd = os.open(probe, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if _renameat2_noreplace(fd, os.path.basename(a), fd, os.path.basename(b)):
                    result = ("renameat2", "")
            finally:
                os.close(fd)
                for x in (a, b):
                    try:
                        os.unlink(x)
                    except OSError:
                        pass
        except OSError:
            pass
    _CAP[real] = result
    return result


def reset_capability_cache() -> None:
    _CAP.clear()
    redundantfiles.reset_link_cache()


# ---------------------------------------------------------------------------
# PLAN (read-only)
# ---------------------------------------------------------------------------

def _work_contexts(work_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Naming-template context for many works with three queries (not three per work)."""
    import re

    out: dict[str, dict[str, Any]] = {}
    ids = list(work_ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        marks = ",".join("?" * len(chunk))
        for w in db.all(f"SELECT id,title,first_publish_date FROM works WHERE id IN ({marks})", tuple(chunk)):  # nosec B608
            m = re.search(r"\d{4}", str(w["first_publish_date"] or ""))
            out[w["id"]] = {"Author": "Unknown Author", "Title": w["title"], "Year": m.group() if m else "",
                            "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}
        seen: set[str] = set()
        for r in db.all(f"SELECT wc.work_id,c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id "  # nosec B608
                        f"WHERE wc.role='author' AND wc.work_id IN ({marks}) ORDER BY wc.work_id, wc.ord", tuple(chunk)):
            if r["work_id"] not in seen and r["work_id"] in out:
                seen.add(r["work_id"])
                out[r["work_id"]]["Author"] = r["name"]
        seen = set()
        for r in db.all(f"SELECT sm.work_id,s.name,sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id "  # nosec B608
                        f"WHERE sm.work_id IN ({marks}) ORDER BY sm.work_id, s.name", tuple(chunk)):
            if r["work_id"] not in seen and r["work_id"] in out:
                seen.add(r["work_id"])
                c = out[r["work_id"]]
                c["Series"], c["SeriesPosition"] = r["name"], r["position"] or ""
                c["SeriesPrefix"] = f"{r['name']} {r['position']} - " if r["position"] else f"{r['name']} - "
    return out


def _asset_query(include: dict[str, Any]) -> tuple[str, list[Any]]:
    where = ["a.status='owned'", "a.derived_from_asset_id IS NULL", "a.kind IN ('ebook','audiobook')"]
    params: list[Any] = []

    def _in(col: str, values: list[str]) -> str:
        params.extend(values)
        return f"{col} IN ({','.join('?' * len(values))})"
    if include.get("kinds"):
        where.append(_in("a.kind", include["kinds"]))
    if include.get("work_ids"):
        where.append(_in("a.work_id", include["work_ids"]))
    if include.get("author_ids"):
        where.append("EXISTS (SELECT 1 FROM work_contributors wc WHERE wc.work_id=a.work_id AND " + _in("wc.contributor_id", include["author_ids"]) + ")")  # nosec B608
    if include.get("series_ids"):
        where.append("EXISTS (SELECT 1 FROM series_memberships sm WHERE sm.work_id=a.work_id AND " + _in("sm.series_id", include["series_ids"]) + ")")  # nosec B608
    if include.get("tag_ids"):
        where.append("EXISTS (SELECT 1 FROM work_tags wt WHERE wt.work_id=a.work_id AND " + _in("wt.tag_id", include["tag_ids"]) + ")")  # nosec B608
    sql = ("SELECT a.id,a.work_id,a.kind,a.format,w.title,w.sort_title FROM media_assets a JOIN works w ON w.id=a.work_id WHERE "  # nosec B608
           + " AND ".join(where) + " ORDER BY COALESCE(w.sort_title,w.title) COLLATE NOCASE, a.work_id, a.kind, a.id")
    return sql, params


def _templates() -> dict[str, dict[str, Any]]:
    return {r["media_kind"]: r for r in db.all("SELECT * FROM naming_templates")}


def _render_name(tpl: dict[str, Any], ctx: dict[str, Any], ext: str, part: int | None, multi: bool) -> tuple[str, str]:
    from . import importer

    opts = importer._template_opts(tpl)
    folder = importer._render(tpl["folder_template"], ctx, ext="", part=None, **opts)
    if multi:
        if tpl.get("multipart_template"):
            name = importer._render(tpl["multipart_template"], ctx, ext=ext, part=part, **opts)
        else:
            base = importer._render(tpl["file_template"], ctx, ext=ext, part=None, **opts)
            stem, dot, e = base.rpartition(".")
            name = f"{stem} - {int(part or 0):02d}.{e}" if dot else f"{base} - {int(part or 0):02d}"
    else:
        name = importer._render(tpl["file_template"], ctx, ext=ext, part=None, **opts)
    name = Path(name).name if "/" in name else name
    if ext and not name.lower().endswith("." + ext.lower()):
        name = name.rstrip(" .") + "." + ext          # the importer's 220-character cut can drop the extension
    parts = [_fit_component(p) for p in Path(folder).parts if p not in ("", ".", "..", "/")]
    return str(Path(*parts)) if parts else "Unknown", _fit_component(name)


class _DirListing:
    """Case-insensitive name lookups in destination folders (listed once per folder)."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, str]] = {}

    def taken(self, path: str) -> str | None:
        d, name = os.path.split(path)
        if d not in self._cache:
            try:
                self._cache[d] = {unicodedata.normalize("NFC", n).casefold(): n for n in os.listdir(d)}
            except OSError:
                self._cache[d] = {}
        return self._cache[d].get(unicodedata.normalize("NFC", name).casefold())


def _convert_check(kind: str, asset: dict[str, Any], src: list[dict[str, Any]], all_files: list[dict[str, Any]],
                   ffmpeg: bool) -> tuple[bool, str | None]:
    """(convertible, skip code) for the preferred-mode decision on a non-preferred book."""
    pref = PREFERRED[kind]
    exts = {Path(f["path"]).suffix.lower() for f in src}
    if len(src) != len(all_files):
        return False, "mixed_roots"
    if db.one("SELECT 1 x FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id IS NULL",
              (asset["work_id"], kind, pref)):
        return False, "not_convertible"          # the book already has a real EPUB/M4B asset; that one is copied
    if kind == "audiobook":
        if not exts <= convert_audio.AUDIO_SOURCE_EXTS:
            return False, "not_convertible"
        if not ffmpeg:
            return False, "not_convertible"
    else:
        if len(src) != 1 or not exts <= convert_ebook.EBOOK_SOURCE_EXTS:
            return False, "not_convertible"
        ok, why = convert_ebook.convertible(Path(src[0]["path"]))
        if not ok:
            return False, "drm" if why and "DRM" in why else "not_convertible"
    if db.one("SELECT 1 x FROM conversions WHERE source_asset_id=? AND status IN ('queued','running')", (asset["id"],)):
        return False, "conversion_queued"
    return True, None


def create_plan(options: dict[str, Any] | None = None, *, user: Any = None) -> dict[str, Any]:
    """The dry run: decide every book, store the items, return the summary. Writes nothing to any library root."""
    from . import conversion

    opts = validate_options(options)
    inc = opts["include"]
    roots = _Roots()
    if not any(r["management_mode"] == "managed" and r["enabled"] for r in roots.by_id.values()):
        raise BuildRefused("no_managed_root", "No managed library root is configured. Add a managed root in Settings > Library roots first.")
    if inc.get("root_ids"):
        for rid in inc["root_ids"]:
            r = roots.by_id.get(rid)
            if not r:
                raise BuildRefused("bad_options", "Unknown library root in include.root_ids")
            if r["management_mode"] == "managed":
                raise BuildRefused("bad_options", f"{r['name']} is a managed root; choose the adopted or observe roots to copy from")
        source_ids = set(inc["root_ids"])
    else:
        source_ids = {r["id"] for r in roots.by_id.values() if r["management_mode"] == "adopt"}
    templates = _templates()
    dest_for = {k: managed_root_for(k) for k in ("ebook", "audiobook")}
    ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    ccfg = conversion.get_settings()
    plan_id, now = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO build_plans(id,plan_json,created_at,version,status,options_json,created_by,expires_at) "
                     "VALUES (?,'{}',?,2,'building',?,?,?)", (plan_id, now, db.json_dump(opts), _uid(user), now + PLAN_TTL_SECONDS))
    try:
        _fill_plan(plan_id, opts, roots, source_ids, templates, dest_for, ffmpeg, ccfg)
        summary = summarize(plan_id)
    except Exception:
        with db.tx() as conn:
            conn.execute("UPDATE build_plans SET status='failed' WHERE id=?", (plan_id,))
        raise
    with db.tx() as conn:
        conn.execute("UPDATE build_plans SET status='ready',summary_json=?,book_count=?,item_count=? WHERE id=?",
                     (db.json_dump(summary), summary["books"], summary["items"], plan_id))
    db.add_history("managed_build.planned", f"Managed library build dry run: {summary['books']} book(s), "
                   f"{summary['files']} file(s), {_gb(summary['bytes'])} to copy or convert",
                   data={"plan_id": plan_id, "options": opts, "by": _uid(user)})
    return plan_payload(plan_id)


def _fill_plan(plan_id: str, opts: dict[str, Any], roots: _Roots, source_ids: set[str], templates: dict[str, Any],
               dest_for: dict[str, Any], ffmpeg: bool, ccfg: dict[str, Any]) -> None:
    from . import conversion

    inc = opts["include"]
    sql, params = _asset_query(inc)
    assets = db.all(sql, tuple(params))
    limit = inc.get("limit_books")
    batch: list[tuple] = []
    claimed: dict[tuple[str, str], int] = {}     # (dest root, casefolded relative path) -> times used
    listing = _DirListing()
    link_cache: dict[str, bool] = {}
    books: set[str] = set()
    contexts: dict[str, dict[str, Any]] = {}

    def flush() -> None:
        if batch:
            with db.tx() as conn:
                conn.executemany(
                    "INSERT INTO build_plan_items(plan_id,unit_key,part_index,work_id,asset_id,file_id,file_ids_json,title,kind,source_root_id,"
                    "source_path,src_size,src_mtime_ns,src_ino,src_dev,sources_json,action,format,target_format,dest_root_id,dest_path,size_bytes,"
                    "est_bytes,skip_code,skip_reason,collision,state,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch)
            batch.clear()

    for start in range(0, len(assets), 400):
        chunk = assets[start:start + 400]
        marks = ",".join("?" * len(chunk))
        files_by_asset: dict[str, list[dict[str, Any]]] = {}
        for f in db.all(f"SELECT id,asset_id,path,part_number,size_bytes,missing,duration_seconds,channels FROM media_files "  # nosec B608
                        f"WHERE asset_id IN ({marks})", tuple(a["id"] for a in chunk)):
            files_by_asset.setdefault(f["asset_id"], []).append(f)
        need_ctx = {a["work_id"] for a in chunk if a["work_id"] not in contexts}
        contexts.update(_work_contexts(need_ctx))
        for a in chunk:
            files = files_by_asset.get(a["id"], [])
            src: list[dict[str, Any]] = []
            for f in files:
                r = roots.for_path(f["path"])
                f["_root"] = r
                if r is not None and r["id"] in source_ids:
                    src.append(f)
            if not src:
                continue                      # nothing of this book lives in a source root: not part of this build
            if limit and a["work_id"] not in books and len(books) >= limit:
                continue
            src.sort(key=lambda f: (f["part_number"] if f["part_number"] is not None else 10 ** 9, convert_audio.natural_key(os.path.basename(f["path"]))))
            kind, now = a["kind"], db.now()
            title = (contexts.get(a["work_id"]) or {}).get("Title") or a["title"]
            unit_problem: str | None = None
            sigs: dict[str, dict[str, Any] | None] = {}
            for f in src:
                r = f["_root"]
                s = _sig(f["path"])
                sigs[f["id"]] = s
                code = None
                if not r["enabled"]:
                    code = "root_disabled"
                elif f["missing"] or s is None:
                    code = "missing"
                elif stat.S_ISLNK(s["mode"]) or _symlinked_parent(f["path"], r["path"], link_cache):
                    code = "symlink"
                elif not stat.S_ISREG(s["mode"]):
                    code = "not_regular"
                elif not os.access(f["path"], os.R_OK):
                    code = "unreadable"
                if code and not unit_problem:
                    unit_problem = code
            dest_root = dest_for.get(kind)
            tpl = templates.get(kind)
            pref = PREFERRED[kind]
            exts = {Path(f["path"]).suffix.lower() for f in src}
            action, skip = "copy", None
            if unit_problem:
                action, skip = "skip", unit_problem
            elif dest_root is None:
                action, skip = "skip", "no_managed_root"
            elif tpl is None:
                action, skip = "skip", "no_template"
            elif exts == {"." + pref}:
                action = "copy"
            elif opts["mode"] == "as_is":
                action = "copy_as_is"
            else:
                derived_row = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id=?",
                                     (a["work_id"], kind, pref, a["id"]))
                if derived_row and _derived_in_managed(derived_row["id"], roots):
                    action, skip = "skip", "already_converted"
                else:
                    ok, why = _convert_check(kind, a, src, files, ffmpeg)
                    if ok:
                        action = "convert"
                    elif why in ("conversion_queued", "mixed_roots") and opts["non_convertible"] == "skip":
                        action, skip = "skip", why
                    else:
                        action, skip = ("copy_as_is", None) if opts["non_convertible"] == "copy" else ("skip", why or "not_convertible")
            ctx = contexts.get(a["work_id"]) or {"Author": "Unknown Author", "Title": a["title"], "Year": "", "Series": "",
                                                  "SeriesPosition": "", "SeriesPrefix": ""}

            def dest_for_name(folder: str, name: str) -> tuple[str | None, str | None, str | None]:
                if dest_root is None:
                    return None, None, None
                rel_folder = folder
                n, collision = 1, None
                while True:
                    cand = _suffixed(name, n)
                    key = (dest_root["id"], unicodedata.normalize("NFC", os.path.join(rel_folder, cand)).casefold())
                    full = os.path.join(dest_root["path"], rel_folder, cand)
                    if key in claimed:
                        collision = "in_plan"
                        n += 1
                        continue
                    on_disk = os.path.lexists(full) or listing.taken(full)
                    if on_disk and n == 1:
                        collision = "exists" if os.path.lexists(full) else "exists_case"
                    if on_disk and n > 1:
                        n += 1
                        continue
                    claimed[key] = 1
                    if len(full.encode("utf-8")) > MAX_PATH_BYTES:
                        return full, collision, "path_too_long"
                    return full, collision, None

            if action == "convert":
                target = pref
                folder, name = _render_name(tpl, ctx, target, None, False)
                dest, collision, too_long = dest_for_name(folder, name)
                if too_long:
                    action, skip = "skip", "path_too_long"
                est = conversion._estimate("audio" if kind == "audiobook" else "ebook", src, ccfg)
                first = src[0]
                s0 = sigs[first["id"]] or {}
                sources = [{"file_id": f["id"], "path": f["path"], "size": (sigs[f["id"]] or {}).get("size"),
                            "mtime_ns": (sigs[f["id"]] or {}).get("mtime_ns"), "ino": (sigs[f["id"]] or {}).get("ino"),
                            "dev": (sigs[f["id"]] or {}).get("dev")} for f in src]
                batch.append((plan_id, a["id"], None, a["work_id"], a["id"], first["id"], db.json_dump([f["id"] for f in src]), title, kind,
                              first["_root"]["id"], first["path"], sum(int((sigs[f["id"]] or {}).get("size") or 0) for f in src),
                              s0.get("mtime_ns"), s0.get("ino"), s0.get("dev"), db.json_dump(sources), action,
                              ",".join(sorted(e.lstrip(".") for e in exts)), target, dest_root["id"], dest,
                              sum(int((sigs[f["id"]] or {}).get("size") or 0) for f in src),
                              0 if action == "skip" else est, skip, SKIP_REASONS.get(skip or "", None), collision,
                              "skipped" if action == "skip" else "pending", now))
                if action != "skip":
                    books.add(a["work_id"])
                continue
            multi = len(src) > 1
            parts_ok = multi and len({f["part_number"] for f in src}) == len(src) and all(f["part_number"] for f in src)
            for idx, f in enumerate(src, 1):
                s = sigs[f["id"]] or {}
                ext = Path(f["path"]).suffix.lower().lstrip(".") or "bin"
                dest = collision = None
                item_action, item_skip = action, skip
                if tpl is not None and dest_root is not None:
                    folder, name = _render_name(tpl, ctx, ext, (f["part_number"] if parts_ok else idx) if multi else None, multi)
                    if multi:
                        # A multi-file book keeps its own file names (and so its part order and the track titles the Player
                        # derives from them) inside the templated book folder; only a single file is renamed by the template.
                        name = _fit_component(os.path.basename(f["path"]))
                    dest, collision, too_long = dest_for_name(folder, name)
                    if too_long and item_action != "skip":
                        item_action, item_skip = "skip", "path_too_long"
                size = int(s.get("size") or f["size_bytes"] or 0)
                batch.append((plan_id, a["id"], idx, a["work_id"], a["id"], f["id"], None, title, kind, f["_root"]["id"], f["path"],
                              s.get("size"), s.get("mtime_ns"), s.get("ino"), s.get("dev"), None, item_action, ext,
                              ext if item_action != "skip" else None, dest_root["id"] if dest_root else None, dest, size,
                              size if item_action != "skip" else 0, item_skip, SKIP_REASONS.get(item_skip or "", None), collision,
                              "skipped" if item_action == "skip" else "pending", now))
                if item_action != "skip":
                    books.add(a["work_id"])
            if len(batch) >= PLAN_INSERT_BATCH:
                flush()
    flush()


def _derived_in_managed(asset_id: str, roots: _Roots) -> bool:
    files = db.all("SELECT path FROM media_files WHERE asset_id=? AND missing=0", (asset_id,))
    return bool(files) and all((roots.for_path(f["path"]) or {}).get("management_mode") == "managed" for f in files)


def _gb(n: int | float | None) -> str:
    return f"{(n or 0) / 1024 ** 3:.2f} GB"


def _uid(user: Any) -> str | None:
    return user.get("username") or user.get("id") if isinstance(user, dict) else user


def summarize(plan_id: str) -> dict[str, Any]:
    """Counts and bytes by action, kind and source root, skip reasons, collisions, destination space and a time
    estimate. SQL aggregates only: cheap on a 100k-item plan."""
    roots = {r["id"]: r for r in db.all("SELECT id,name,path,management_mode,media_kind,enabled FROM root_folders")}
    by_action: dict[str, Any] = {}
    for r in db.all("SELECT action, COUNT(*) items, COUNT(DISTINCT unit_key) books, COALESCE(SUM(size_bytes),0) bytes, "
                    "COALESCE(SUM(est_bytes),0) est, COALESCE(SUM(CASE WHEN file_ids_json IS NOT NULL THEN json_array_length(file_ids_json) ELSE 1 END),0) files "
                    "FROM build_plan_items WHERE plan_id=? GROUP BY action", (plan_id,)):
        by_action[r["action"]] = {"items": r["items"], "assets": r["books"], "files": r["files"], "bytes": r["bytes"], "est_bytes": r["est"]}
    act = "action IN ('copy','convert','copy_as_is')"
    by_kind = {r["kind"]: {"items": r["n"], "est_bytes": r["est"]} for r in db.all(
        f"SELECT kind, COUNT(*) n, COALESCE(SUM(est_bytes),0) est FROM build_plan_items WHERE plan_id=? AND {act} GROUP BY kind", (plan_id,))}  # nosec B608
    by_root = [{"root_id": r["source_root_id"], "name": (roots.get(r["source_root_id"]) or {}).get("name"), "items": r["n"],
                "bytes": r["b"], "est_bytes": r["est"]} for r in db.all(
        f"SELECT source_root_id, COUNT(*) n, COALESCE(SUM(size_bytes),0) b, COALESCE(SUM(est_bytes),0) est FROM build_plan_items "  # nosec B608
        f"WHERE plan_id=? AND {act} GROUP BY source_root_id", (plan_id,))]
    skips = {r["skip_code"]: {"count": r["n"], "books": r["b"], "reason": SKIP_REASONS.get(r["skip_code"], r["skip_code"])} for r in db.all(
        "SELECT skip_code, COUNT(*) n, COUNT(DISTINCT unit_key) b FROM build_plan_items WHERE plan_id=? AND action='skip' GROUP BY skip_code", (plan_id,))}
    collisions = {r["collision"]: r["n"] for r in db.all(
        "SELECT collision, COUNT(*) n FROM build_plan_items WHERE plan_id=? AND collision IS NOT NULL GROUP BY collision", (plan_id,))}
    books = db.one(f"SELECT COUNT(DISTINCT work_id) n FROM build_plan_items WHERE plan_id=? AND {act}", (plan_id,))["n"]  # nosec B608
    items = db.one("SELECT COUNT(*) n FROM build_plan_items WHERE plan_id=?", (plan_id,))["n"]
    dests = []
    for r in db.all(f"SELECT dest_root_id, COALESCE(SUM(est_bytes),0) need FROM build_plan_items WHERE plan_id=? AND {act} GROUP BY dest_root_id", (plan_id,)):  # nosec B608
        root = roots.get(r["dest_root_id"]) or {}
        try:
            free = _free_bytes(root["path"]) if root else None
        except OSError:
            free = None
        margin = _margin(r["need"])
        cap = _CAP.get(os.path.realpath(root["path"])) if root else None
        dests.append({"root_id": r["dest_root_id"], "name": root.get("name"), "path": root.get("path"), "required_bytes": r["need"],
                      "margin_bytes": margin, "free_bytes": free, "fits": free is not None and free - r["need"] >= margin,
                      "publish": (cap[0] or "unsupported") if cap else "unprobed"})
    plan = db.one("SELECT options_json FROM build_plans WHERE id=?", (plan_id,)) or {}
    opts = db.json_load(plan.get("options_json"), {}) or DEFAULT_OPTIONS
    copy_bytes = sum((by_action.get(a) or {}).get("bytes", 0) for a in ("copy", "copy_as_is"))
    seconds = _estimate_seconds(plan_id, copy_bytes, opts)
    warnings = []
    if opts.get("verify") == "none":
        warnings.append("Verification is OFF: a damaged copy would not be noticed. Use 'full' unless you have a reason not to.")
    elif opts.get("verify") == "size":
        warnings.append("Only sizes are compared after copying; 'full' re-reads every copy and compares checksums.")
    if any(not d["fits"] for d in dests):
        warnings.append("Not enough free space in a managed root for this plan (see Destinations).")
    if skips.get("drm"):
        warnings.append(f"{skips['drm']['count']} DRM-protected book(s) cannot be converted.")
    if any((roots.get(r["root_id"]) or {}).get("management_mode") == "observe" for r in by_root):
        warnings.append("Books from an observe-only root are included; their copies become managed books.")
    if collisions:
        warnings.append(f"{sum(collisions.values())} destination name(s) are already taken; those files get a numbered name (or reuse an identical file).")
    copy_items = sum((by_action.get(a) or {}).get("items", 0) for a in ACTIONABLE)
    return {"items": items, "books": books, "files": sum((by_action.get(a) or {}).get("files", 0) for a in ACTIONABLE),
            "actionable_items": copy_items, "bytes": sum((by_action.get(a) or {}).get("est_bytes", 0) for a in ACTIONABLE),
            "source_bytes": sum((by_action.get(a) or {}).get("bytes", 0) for a in ACTIONABLE),
            "by_action": by_action, "by_kind": by_kind, "by_root": by_root, "skips": skips, "collisions": collisions,
            "destinations": dests, "estimated_seconds": seconds, "rate_bytes_per_second": _rate(opts), "warnings": warnings,
            "originals_kept": True}


def _rate(opts: dict[str, Any]) -> float:
    measured = db.get_setting(MEASURED_KEY)
    rate = float(measured) if isinstance(measured, (int, float)) and measured > 0 else float(DEFAULT_RATE_BPS)
    t = float(opts.get("throttle_mb_s") or 0)
    return min(rate, t * 1024 * 1024) if t > 0 else rate


def _estimate_seconds(plan_id: str, copy_bytes: int, opts: dict[str, Any]) -> int:
    rate = _rate(opts)
    secs = copy_bytes / rate * (1.6 if opts.get("verify") == "full" else 1.0)
    conv = db.one("SELECT COALESCE(SUM(f.duration_seconds),0) d, COALESCE(SUM(CASE WHEN i.kind='ebook' THEN 1 ELSE 0 END),0) e "
                  "FROM build_plan_items i LEFT JOIN media_files f ON i.kind='audiobook' AND i.file_ids_json IS NOT NULL "
                  "AND f.id IN (SELECT value FROM json_each(i.file_ids_json)) WHERE i.plan_id=? AND i.action='convert'", (plan_id,))
    secs += (conv["d"] or 0) / AUDIO_ENCODE_SPEED / max(1, int(opts.get("concurrency") or 1)) + (conv["e"] or 0) * 5
    return int(secs)


def plan_payload(plan_id: str) -> dict[str, Any]:
    p = db.one("SELECT * FROM build_plans WHERE id=?", (plan_id,))
    if not p:
        raise BuildRefused("not_found", "No such plan")
    if (p.get("version") or 1) < 2:
        raise BuildRefused("old_plan", "This plan was made by an older version; run a new dry run")
    latest = db.one("SELECT id FROM build_plans ORDER BY created_at DESC, rowid DESC LIMIT 1")
    run = db.one("SELECT id,status FROM build_runs WHERE plan_id=?", (plan_id,))
    summary = db.json_load(p.get("summary_json"), None) or (summarize(plan_id) if p["status"] == "ready" else {})
    return {"plan_id": p["id"], "status": p["status"], "created_at": p["created_at"], "expires_at": p["expires_at"],
            "expired": bool(p["expires_at"] and p["expires_at"] < db.now()), "latest": bool(latest and latest["id"] == p["id"]),
            "confirmed": p["confirmed_at"] is not None, "executed": p["executed_at"] is not None,
            "book_count": p["book_count"], "options": db.json_load(p["options_json"], {}), "summary": summary,
            "run": run, "transfer_mode": "copy", "originals_kept": True}


def latest_plan() -> dict[str, Any]:
    p = db.one("SELECT id FROM build_plans WHERE version>=2 ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if not p:
        raise BuildRefused("not_found", "No dry run yet; run a dry run first")
    return plan_payload(p["id"])


ITEM_FILTERS = ("action", "state", "kind", "root_id", "q", "collision", "work_id")


def list_items(plan_id: str, *, limit: int = 50, offset: int = 0, **filters: Any) -> dict[str, Any]:
    """One page of plan items (a 100k-item plan is never sent whole)."""
    limit = max(1, min(int(limit), MAX_PAGE))
    where, params = ["plan_id=?"], [plan_id]
    if filters.get("action"):
        where.append("action=?"); params.append(filters["action"])
    if filters.get("state"):
        if filters["state"] == "problem":
            where.append("state IN ('failed')")
        else:
            where.append("state=?"); params.append(filters["state"])
    if filters.get("kind"):
        where.append("kind=?"); params.append(filters["kind"])
    if filters.get("root_id"):
        where.append("source_root_id=?"); params.append(filters["root_id"])
    if filters.get("work_id"):
        where.append("work_id=?"); params.append(filters["work_id"])
    if filters.get("collision"):
        where.append("collision IS NOT NULL")
    if filters.get("q"):
        where.append("(title LIKE ? ESCAPE '\\' OR source_path LIKE ? ESCAPE '\\')")
        params += [db.like_pattern(filters["q"])] * 2
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM build_plan_items WHERE {w}", tuple(params))["n"]  # nosec B608
    rows = db.all(f"SELECT id,unit_key,part_index,work_id,asset_id,file_id,title,kind,source_root_id,source_path,action,format,target_format,"  # nosec B608
                  f"dest_root_id,dest_path,size_bytes,est_bytes,skip_code,skip_reason,collision,state,attempts,error,final_path,reused,percent "
                  f"FROM build_plan_items WHERE {w} ORDER BY id LIMIT ? OFFSET ?", (*params, limit, max(0, int(offset))))
    return {"items": rows, "total": total, "limit": limit, "offset": max(0, int(offset))}


def confirm_plan(plan_id: str, book_count: Any, *, user: Any = None) -> dict[str, Any]:
    p = db.one("SELECT * FROM build_plans WHERE id=?", (plan_id,))
    latest = db.one("SELECT id FROM build_plans ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if not p or not latest or latest["id"] != plan_id:
        raise BuildRefused("not_latest", "Only the most recent dry run can be confirmed")
    if (p.get("version") or 1) < 2 or p["status"] != "ready":
        raise BuildRefused("not_ready", "This plan is not complete; run a new dry run")
    if p["executed_at"] is not None:
        raise BuildRefused("executed", "This plan has already been run")
    if p["expires_at"] and p["expires_at"] < db.now():
        raise BuildRefused("expired", "This plan is older than 24 hours; run a new dry run")
    if not p["book_count"]:
        raise BuildRefused("empty", "This plan has nothing to copy")
    if isinstance(book_count, bool) or not isinstance(book_count, int) or book_count != p["book_count"]:
        raise BuildRefused("confirmation_required", f"Type the number of books in this plan ({p['book_count']}) to confirm it",
                           book_count=p["book_count"])
    with db.tx() as conn:
        conn.execute("UPDATE build_plans SET confirmed_at=?,confirmed_by=? WHERE id=? AND confirmed_at IS NULL", (db.now(), _uid(user), plan_id))
    db.add_history("managed_build.confirmed", f"Managed library build plan confirmed ({p['book_count']} book(s))",
                   data={"plan_id": plan_id, "by": _uid(user)})
    return plan_payload(plan_id)


# ---------------------------------------------------------------------------
# Preflight and run control
# ---------------------------------------------------------------------------

_control = threading.RLock()


def preflight(plan_id: str) -> list[str]:
    """Reasons the plan cannot start now (empty list = go). Probes each destination root's no-clobber publish."""
    problems: list[str] = []
    for d in db.all("SELECT dest_root_id, COALESCE(SUM(est_bytes),0) need FROM build_plan_items WHERE plan_id=? AND state='pending' "
                    "GROUP BY dest_root_id", (plan_id,)):
        root = db.one("SELECT * FROM root_folders WHERE id=?", (d["dest_root_id"],))
        if not root or not root["enabled"] or root["management_mode"] != "managed":
            problems.append("A destination root is no longer an enabled managed root; run a new dry run")
            continue
        if not os.path.isdir(root["path"]) or os.path.islink(root["path"]):
            problems.append(f"{root['name']}: the folder {root['path']} is not reachable")
            continue
        if _not_real_path(root):
            problems.append(_not_real_path(root))
            continue
        how, msg = publish_capability(root["path"])
        if how is None:
            problems.append(f"{root['name']}: {msg}")
        try:
            free = _free_bytes(root["path"])
        except OSError as exc:
            problems.append(f"{root['name']}: cannot read the free space ({exc})")
            continue
        margin = _margin(d["need"])
        if free - d["need"] < margin:
            problems.append(f"{root['name']}: not enough free space: {_gb(d['need'])} needed plus a {_gb(margin)} margin, {_gb(free)} free")
    for s in db.all("SELECT DISTINCT source_root_id FROM build_plan_items WHERE plan_id=? AND state='pending'", (plan_id,)):
        root = db.one("SELECT * FROM root_folders WHERE id=?", (s["source_root_id"],))
        if not root or not root["enabled"] or root["management_mode"] not in ("adopt", "observe"):
            problems.append("A source root changed (removed, disabled or now managed) since the dry run; run a new dry run")
            break
        if _not_real_path(root):
            problems.append(_not_real_path(root))
    return problems


def _not_real_path(root: dict[str, Any]) -> str | None:
    """A plain-language problem when the root's stored path is not its real path (a symlink somewhere in it, or a
    '..'): every safety check compares paths under the stored root, so the build refuses to run through a link."""
    stored = root["path"] or ""
    real = os.path.realpath(stored)
    if os.path.isabs(stored) and real == os.path.normpath(stored):
        return None
    return (f"{root['name']}: the folder {stored} is reached through a symbolic link (its real location is {real}). "
            f"Edit this library root in Settings > Library roots and set its path to {real}, then run a new dry run")


def _job_id() -> str:
    row = db.one("SELECT id FROM jobs WHERE kind=?", (JOB_KIND,))
    if not row:
        raise BuildRefused("no_job", "The managed-library-build job is missing")
    return row["id"]


def active_run() -> dict[str, Any] | None:
    return db.one(f"SELECT * FROM build_runs WHERE status IN ({','.join('?' * len(ACTIVE_RUN))}) ORDER BY started_at DESC LIMIT 1", ACTIVE_RUN)  # nosec B608


def run_enabled() -> bool:
    """The admin switch ``managed_build.run_enabled`` (default OFF): while it is off a build cannot start or resume, and
    switching it off pauses a running build after the file it is on (``_stop_requested``). Dry runs, plans, Pause,
    Cancel and Undo of finished work stay available."""
    return db.get_setting(RUN_ENABLED_KEY, False) is True


def set_run_enabled(enabled: bool, *, user: Any = None) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise BuildRefused("bad_request", "run_enabled must be true or false")
    db.set_setting(RUN_ENABLED_KEY, enabled)
    if not enabled:
        # Switching it off PAUSES a running build after the file it is on (the workers also check the switch itself).
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET pause_requested=1 WHERE status='running'")
    db.add_history("managed_build.run_enabled", "Managed library build: running builds " + ("allowed" if enabled else "switched off"),
                   data={"run_enabled": enabled, "by": _uid(user)})
    return {"run_enabled": enabled}


def _require_run_enabled() -> None:
    if not run_enabled():
        raise BuildRefused("run_disabled", RUN_DISABLED_MESSAGE)


def start(plan_id: str, *, user: Any = None, submit: bool = True) -> dict[str, Any]:
    """Run a confirmed, unexpired, latest plan once. Nothing starts automatically; this is the only entry point."""
    _require_run_enabled()
    with _control:
        p = db.one("SELECT * FROM build_plans WHERE id=?", (plan_id,))
        latest = db.one("SELECT id FROM build_plans ORDER BY created_at DESC, rowid DESC LIMIT 1")
        if not p or not latest or latest["id"] != plan_id:
            raise BuildRefused("not_latest", "Only the most recent dry run can be run")
        if (p.get("version") or 1) < 2 or p["status"] != "ready":
            raise BuildRefused("not_ready", "This plan is not complete; run a new dry run")
        if p["confirmed_at"] is None:
            raise BuildRefused("not_confirmed", "Plan has not been confirmed; confirm it (type the number of books) before running the build")
        if p["executed_at"] is not None:
            raise BuildRefused("executed", "This plan has already been run")
        if p["expires_at"] and p["expires_at"] < db.now():
            raise BuildRefused("expired", "This plan is older than 24 hours; run a new dry run")
        other = active_run()
        if other:
            raise BuildRefused("busy", f"Another build is {other['status']}; finish, resume or cancel it first", run_id=other["id"])
        problems = preflight(plan_id)
        if problems:
            raise BuildRefused("preflight", "The build cannot start: " + "; ".join(problems), problems=problems)
        run_id, now = db.uid(), db.now()
        with db.tx() as conn:
            claimed = conn.execute("UPDATE build_plans SET executed_at=? WHERE id=? AND executed_at IS NULL", (now, plan_id)).rowcount
            if not claimed:
                raise BuildRefused("executed", "This plan has already been run")
            conn.execute("INSERT INTO build_runs(id,plan_id,status,options_json,started_by,started_at,heartbeat_at) VALUES (?,?,'running',?,?,?,?)",
                         (run_id, plan_id, p["options_json"], _uid(user), now, now))
        db.add_history("managed_build.started", f"Managed library build started ({p['book_count']} book(s))",
                       data={"run_id": run_id, "plan_id": plan_id, "by": _uid(user)})
    if submit:
        _submit()
    return run_status(run_id)


def _submit(tries: int = 0) -> None:
    from . import scheduler

    try:
        scheduler.submit_job(_job_id(), trigger="manual")
    except scheduler.JobBusy:
        # The previous job thread is still finishing (a pause a moment ago). It re-checks for a running build before it
        # exits; in case it already looked, try again shortly (bounded) so a resumed build never sits without a worker.
        if tries < 30:
            t = threading.Timer(1.0, _submit, kwargs={"tries": tries + 1})
            t.daemon = True
            t.start()


def pause(run_id: str, *, user: Any = None) -> dict[str, Any]:
    r = _run(run_id)
    if r["status"] != "running":
        raise BuildRefused("not_running", f"A {r['status']} build cannot be paused")
    with db.tx() as conn:
        conn.execute("UPDATE build_runs SET pause_requested=1 WHERE id=?", (run_id,))
    db.add_history("managed_build.pause", "Managed library build: pause requested", data={"run_id": run_id, "by": _uid(user)})
    return run_status(run_id)


def resume(run_id: str, *, user: Any = None, submit: bool = True) -> dict[str, Any]:
    _require_run_enabled()
    with _control:
        r = _run(run_id)
        if r["status"] not in RESUMABLE:
            raise BuildRefused("not_resumable", f"A {r['status']} build cannot be resumed")
        for other in db.all("SELECT id,status FROM build_runs WHERE status='running' AND id!=?", (run_id,)):
            raise BuildRefused("busy", "Another build is running", run_id=other["id"])
        problems = preflight(r["plan_id"])
        if problems:
            raise BuildRefused("preflight", "The build cannot resume: " + "; ".join(problems), problems=problems)
        recover_run(run_id)
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET status='running',pause_requested=0,stop_reason=NULL,heartbeat_at=?,finished_at=NULL WHERE id=?",
                         (db.now(), run_id))
    db.add_history("managed_build.resumed", "Managed library build resumed", data={"run_id": run_id, "by": _uid(user)})
    if submit:
        _submit()
    return run_status(run_id)


def cancel(run_id: str, *, user: Any = None) -> dict[str, Any]:
    """Stop at the next safe point. Books already copied stay (Undo build removes them); pending ones are cancelled."""
    with _control:
        r = _run(run_id)
        if r["status"] == "running":
            with db.tx() as conn:
                conn.execute("UPDATE build_runs SET cancel_requested=1 WHERE id=?", (run_id,))
        elif r["status"] in RESUMABLE:
            recover_run(run_id)
            _cancel_pending(run_id, r["plan_id"])
        else:
            raise BuildRefused("not_active", f"A {r['status']} build cannot be cancelled")
    db.add_history("managed_build.cancel", "Managed library build: cancel requested", data={"run_id": run_id, "by": _uid(user)})
    return run_status(run_id)


def _cancel_pending(run_id: str, plan_id: str) -> None:
    """Pending items and units that never switched are cancelled; their published files (ours) are removed."""
    for it in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND state='published' AND reused=0", (plan_id,)):
        _unlink_if(it["final_path"], _parse_ident(it["temp_ident"]))
    with db.tx() as conn:
        conn.execute("UPDATE build_plan_items SET state='cancelled',claimed_by=NULL,updated_at=? WHERE plan_id=? AND state IN ('pending','published')",
                     (db.now(), plan_id))
        conn.execute("UPDATE build_runs SET status='cancelled',finished_at=?,cancel_requested=0 WHERE id=?", (db.now(), run_id))


def retry_item(run_id: str, item_id: int, *, user: Any = None) -> dict[str, Any]:
    """A failed file goes back to the queue (with the rest of its book); a finished run becomes resumable."""
    r = _run(run_id)
    it = db.one("SELECT * FROM build_plan_items WHERE id=? AND plan_id=?", (item_id, r["plan_id"]))
    if not it or it["state"] not in ("failed", "skipped") or it["action"] == "skip":
        raise BuildRefused("not_retryable", "Only a failed (or admin-skipped) file can be retried")
    with db.tx() as conn:
        conn.execute("UPDATE build_plan_items SET state='pending',attempts=0,error=NULL,next_attempt_at=NULL,claimed_by=NULL,updated_at=? "
                     "WHERE plan_id=? AND unit_key=? AND state IN ('failed','skipped') AND action!='skip'", (db.now(), r["plan_id"], it["unit_key"]))
        if r["status"] == "completed":
            conn.execute("UPDATE build_runs SET status='stopped',stop_reason='A failed file was queued again; press Resume' WHERE id=?", (run_id,))
    db.add_history("managed_build.retry", f"Managed library build: retrying {it['title']}", data={"run_id": run_id, "item_id": item_id, "by": _uid(user)})
    return run_status(run_id)


def skip_item(run_id: str, item_id: int, *, user: Any = None) -> dict[str, Any]:
    r = _run(run_id)
    it = db.one("SELECT * FROM build_plan_items WHERE id=? AND plan_id=?", (item_id, r["plan_id"]))
    if not it or it["state"] not in ("failed", "pending"):
        raise BuildRefused("not_skippable", "Only a failed or waiting file can be skipped")
    with db.tx() as conn:
        conn.execute("UPDATE build_plan_items SET state='skipped',skip_code='admin',skip_reason='Skipped by an admin',claimed_by=NULL,updated_at=? "
                     "WHERE plan_id=? AND unit_key=? AND state IN ('failed','pending')", (db.now(), r["plan_id"], it["unit_key"]))
    db.add_history("managed_build.skip", f"Managed library build: skipped {it['title']}", data={"run_id": run_id, "item_id": item_id, "by": _uid(user)})
    return run_status(run_id)


def _run(run_id: str) -> dict[str, Any]:
    r = db.one("SELECT * FROM build_runs WHERE id=?", (run_id,))
    if not r:
        raise BuildRefused("not_found", "No such build run")
    return r


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------

def recover_run(run_id: str) -> dict[str, int]:
    """Settle a run's journal from the disk (no worker of it may be live): temp leftovers removed, a link that happened
    kept, everything else back to pending. Never touches a file that is not provably ours."""
    r = _run(run_id)
    roots = _Roots()
    out = {"temps_removed": 0, "published": 0, "requeued": 0}
    for it in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND state IN ('copying','verifying','publishing')", (r["plan_id"],)):
        tident = _parse_ident(it["temp_ident"])
        final_ok = False
        if it["state"] == "publishing" and it["final_path"] and tident:
            fs = _sig(it["final_path"])
            final_ok = bool(fs and (fs["dev"], fs["ino"]) == tident)
        if it["temp_path"] and _is_our_temp(it["temp_path"], roots) and _unlink_if(it["temp_path"]):
            out["temps_removed"] += 1
        with db.tx() as conn:
            if final_ok:
                conn.execute("UPDATE build_plan_items SET state='published',temp_path=NULL,claimed_by=NULL,updated_at=? WHERE id=?", (db.now(), it["id"]))
                out["published"] += 1
            else:
                conn.execute("UPDATE build_plan_items SET state='pending',temp_path=NULL,final_path=NULL,temp_ident=NULL,claimed_by=NULL,percent=NULL,"
                             "updated_at=? WHERE id=?", (db.now(), it["id"]))
                out["requeued"] += 1
    # Sweep: any of our temp names left in the destination folders of this plan (belt and braces).
    prefix = f"{TEMP_PREFIX}{run_id[:8]}-"
    dirs = {os.path.dirname(x["dest_path"]) for x in db.all(
        "SELECT DISTINCT dest_path FROM build_plan_items WHERE plan_id=? AND dest_path IS NOT NULL AND state NOT IN ('skipped')", (r["plan_id"],))}
    for d in dirs:
        dfd = _open_managed_dir(d)          # from the managed root, O_NOFOLLOW at every level; None outside managed roots
        if dfd is None:
            continue
        try:
            for n in os.listdir(dfd):
                if not (n.startswith(prefix) and n.endswith(TEMP_SUFFIX)) or not _is_our_temp(os.path.join(d, n), roots):
                    continue
                try:
                    st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        os.unlink(n, dir_fd=dfd)
                        out["temps_removed"] += 1
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            os.close(dfd)
    with db.tx() as conn:
        conn.execute("UPDATE build_plan_items SET claimed_by=NULL WHERE plan_id=? AND claimed_by IS NOT NULL", (r["plan_id"],))
        conn.execute("UPDATE conversions SET status='cancelled',error='Interrupted managed library build',finished_at=? "
                     "WHERE trigger_source='build' AND status IN ('queued','running')", (db.now(),))
    return out


def on_startup() -> dict[str, int]:
    """At app start (before the scheduler): a build that was 'running' belonged to the old process. It becomes
    'interrupted' (Resume continues it; nothing restarts on its own) and its journal is settled. Also finishes an
    undo that was cut short. Costs no query at all on a start with nothing to recover (two UPDATE ... RETURNING)."""
    with db.tx() as conn:
        ids = [r["id"] for r in conn.execute(
            "UPDATE build_runs SET status='interrupted',stop_reason='The app restarted while building; press Resume to continue' "
            "WHERE status='running' RETURNING id").fetchall()]
        undo_pending = conn.execute("UPDATE managed_copies SET undo_state=undo_state WHERE undo_state IN ('undoing','moving') RETURNING id").fetchall()
    for run_id in ids:
        try:
            recover_run(run_id)
        except Exception:
            log.exception("managed build recovery failed for %s", run_id)
    if undo_pending:
        try:
            recover_undo()
        except Exception:
            log.exception("managed build undo recovery failed")
    if ids:
        log.warning("managed library build interrupted by a restart; it waits for Resume")
    return {"interrupted": len(ids)}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class _Throttle:
    """A shared MB/s budget for all workers of a run: after each chunk, sleep until the bytes so far fit the limit."""

    def __init__(self, mb_s: float) -> None:
        self.rate = mb_s * 1024 * 1024 if mb_s and mb_s > 0 else 0.0
        self.lock = threading.Lock()
        self.start = _clock()
        self.bytes = 0

    def consume(self, n: int) -> None:
        if not self.rate:
            return
        with self.lock:
            self.bytes += n
            due = self.start + self.bytes / self.rate
        wait = due - _clock()
        if wait > 0:
            _sleep(wait)


class _Progress:
    def __init__(self, run_id: str, plan_id: str) -> None:
        self.run_id, self.lock = run_id, threading.Lock()
        tot = db.one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN action='convert' THEN est_bytes ELSE size_bytes END),0) b, "
                     "COUNT(DISTINCT unit_key) u FROM build_plan_items WHERE plan_id=? AND action IN ('copy','convert','copy_as_is')", (plan_id,))
        done = db.one("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN action='convert' THEN est_bytes ELSE size_bytes END),0) b FROM build_plan_items "
                      "WHERE plan_id=? AND state IN ('done','skipped','cancelled') AND action IN ('copy','convert','copy_as_is')", (plan_id,))
        self.files_total, self.bytes_total, self.units_total = tot["n"], tot["b"], tot["u"]
        self.files_done, self.bytes_done_before = done["n"], done["b"]
        self.bytes_now = 0
        self.t0 = _clock()
        self.current: dict[str, str] = {}
        self.last_flush = 0.0
        self.last_touch = 0.0

    def add(self, n: int) -> None:
        with self.lock:
            self.bytes_now += n
        self.maybe_flush()

    def file_done(self) -> None:
        with self.lock:
            self.files_done += 1

    def snapshot(self) -> dict[str, Any]:
        el = max(1e-6, _clock() - self.t0)
        rate = self.bytes_now / el
        done = self.bytes_done_before + self.bytes_now
        left = max(0, self.bytes_total - done)
        return {"files_done": self.files_done, "files_total": self.files_total, "bytes_done": done, "bytes_total": self.bytes_total,
                "bytes_per_second": round(rate), "eta_seconds": int(left / rate) if rate > 1 else None,
                "current": sorted(set(self.current.values())), "updated_at": db.now()}

    def maybe_flush(self, force: bool = False) -> None:
        now = _clock()
        if not force and now - self.last_flush < FLUSH_SECONDS:
            return
        self.last_flush = now
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET progress_json=?,heartbeat_at=? WHERE id=?", (db.json_dump(self.snapshot()), db.now(), self.run_id))
        if now - self.last_touch >= LEASE_TOUCH_SECONDS:
            self.last_touch = now
            from . import scheduler
            scheduler.touch_lease(JOB_KIND)


_live: dict[str, _Progress] = {}


def job_body() -> dict[str, Any]:
    """The managed-library-build job: execute every run marked 'running' (normally one)."""
    out: dict[str, Any] = {"runs": []}
    for _ in range(3):
        r = db.one("SELECT id FROM build_runs WHERE status='running' ORDER BY started_at LIMIT 1")
        if not r:
            break
        out["runs"].append(execute(r["id"]))
    return out


def execute(run_id: str) -> dict[str, Any]:
    """Run (or continue) one build until it is done, paused, cancelled or stopped. Workers stop only at safe points
    (between files); a crash anywhere leaves a journal that ``recover_run`` settles."""
    with _executing_lock:
        if run_id in _executing:            # a second thread for the same run (a lease that expired under a slow file)
            return {"run_id": run_id, "status": "already_running"}
        _executing.add(run_id)
    try:
        for _ in range(1000):
            try:
                out = _execute(run_id)
            except Exception as exc:          # never leave a run 'running' without a worker: Resume or Cancel must work
                return _abort_run(run_id, exc)
            # Resumed while this thread was finishing (its submit met this very job slot): keep going here.
            if (db.one("SELECT status FROM build_runs WHERE id=?", (run_id,)) or {}).get("status") != "running":
                return out
        return out
    finally:
        with _executing_lock:
            _executing.discard(run_id)


def _unexpected(exc: BaseException) -> str:
    return f"Stopped by an unexpected error ({type(exc).__name__}: {str(exc)[:500]}); press Resume to continue or Cancel"


def _abort_run(run_id: str, exc: BaseException) -> dict[str, Any]:
    """Last line of defence for an error outside a book (database, a bug): the run becomes 'stopped' with the error as its
    reason and every claim is released, so Resume, Cancel and Undo work. The journal is settled by recover_run on Resume."""
    log.exception("managed library build %s stopped by an unexpected error", run_id)
    msg = _unexpected(exc)
    _live.pop(run_id, None)
    try:
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET status='stopped',stop_reason=?,pause_requested=0 WHERE id=? AND status='running'", (msg, run_id))
            conn.execute("UPDATE build_plan_items SET claimed_by=NULL WHERE plan_id=(SELECT plan_id FROM build_runs WHERE id=?) "
                         "AND claimed_by IS NOT NULL", (run_id,))
        db.add_history("managed_build.stopped", f"Managed library build stopped: {msg}", data={"run_id": run_id, "error": repr(exc)[:1000]})
    except Exception:
        log.exception("managed library build %s: could not record the stop", run_id)
    return {"run_id": run_id, "status": "stopped", "error": msg}


_executing: set[str] = set()
_executing_lock = threading.Lock()


def _execute(run_id: str) -> dict[str, Any]:
    run = _run(run_id)
    if run["status"] != "running":
        return {"run_id": run_id, "status": run["status"]}
    recover_run(run_id)
    opts = validate_options(db.json_load(run["options_json"], {}))
    plan_id = run["plan_id"]
    progress = _Progress(run_id, plan_id)
    _live[run_id] = progress
    throttle = _Throttle(opts["throttle_mb_s"])
    stop: dict[str, Any] = {}
    roots = _Roots()

    def work(wid: str) -> None:
        while not stop:
            if _stop_requested(run_id):         # pause, cancel, or the run switch turned off (a pause)
                return
            unit = _claim(plan_id, wid)
            if unit is None:
                wait = db.one("SELECT MIN(next_attempt_at) t FROM build_plan_items WHERE plan_id=? AND state IN ('pending','published') "
                              "AND claimed_by IS NULL", (plan_id,))["t"]
                if wait is None:
                    return
                _sleep(min(5.0, max(0.05, wait - db.now())))
                continue
            try:
                _process_unit(run_id, plan_id, unit, opts, throttle, progress, roots, wid, stop)
            finally:
                progress.current.pop(wid, None)

    def worker(wid: str) -> None:
        try:
            work(wid)
        except Exception as exc:              # outside a book (claiming, flags): stop the run cleanly, the others too
            log.exception("managed library build %s: worker %s failed", run_id, wid)
            stop.setdefault("reason", _unexpected(exc))

    workers = [f"w{i}-{run_id[:6]}" for i in range(opts["concurrency"])]
    if len(workers) == 1:
        worker(workers[0])
    else:
        with ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="alx-build") as pool:
            for fut in [pool.submit(worker, w) for w in workers]:
                fut.result()
    progress.maybe_flush(force=True)
    _live.pop(run_id, None)
    return _finish(run_id, plan_id, stop, progress)


def _finish(run_id: str, plan_id: str, stop: dict[str, Any], progress: _Progress) -> dict[str, Any]:
    f = db.one("SELECT * FROM build_runs WHERE id=?", (run_id,))
    now = db.now()
    snap = progress.snapshot()
    if progress.bytes_now > 64 * 1024 * 1024 and snap["bytes_per_second"]:
        db.set_setting(MEASURED_KEY, snap["bytes_per_second"])
    if f["cancel_requested"]:
        _cancel_pending(run_id, plan_id)
        status, msg = "cancelled", "Managed library build cancelled"
    elif stop:
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET status='stopped',stop_reason=? WHERE id=?", (stop.get("reason"), run_id))
        status, msg = "stopped", f"Managed library build stopped: {stop.get('reason')}"
    elif f["pause_requested"]:
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET status='paused',pause_requested=0,stop_reason=? WHERE id=?",
                         (None if run_enabled() else SWITCHED_OFF_REASON, run_id))
        status, msg = "paused", "Managed library build paused" + ("" if run_enabled() else " (running builds were switched off)")
    else:
        left = db.one("SELECT COUNT(*) n FROM build_plan_items WHERE plan_id=? AND state IN ('pending','published')", (plan_id,))["n"]
        if left:
            with db.tx() as conn:
                conn.execute("UPDATE build_runs SET status='stopped',stop_reason='Work left; press Resume' WHERE id=?", (run_id,))
            status, msg = "stopped", "Managed library build stopped with work left"
        else:
            with db.tx() as conn:
                conn.execute("UPDATE build_runs SET status='completed',finished_at=? WHERE id=?", (now, run_id))
            status, msg = "completed", "Managed library build completed"
    counts = {r["state"]: r["n"] for r in db.all("SELECT state, COUNT(*) n FROM build_plan_items WHERE plan_id=? GROUP BY state", (plan_id,))}
    db.add_history("managed_build." + status, f"{msg}: {counts.get('done', 0)} done, {counts.get('failed', 0)} failed, "
                   f"{counts.get('skipped', 0)} skipped", data={"run_id": run_id, "counts": counts, "progress": snap})
    return {"run_id": run_id, "status": status, "counts": counts}


def _claim(plan_id: str, wid: str) -> list[dict[str, Any]] | None:
    """Atomically take the next book: every item of one unit that still needs work. Two workers never get the same unit."""
    now = db.now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT unit_key FROM build_plan_items WHERE plan_id=? AND state IN ('pending','published') AND claimed_by IS NULL "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) AND unit_key NOT IN (SELECT unit_key FROM build_plan_items WHERE plan_id=? "
            "AND claimed_by IS NOT NULL) ORDER BY id LIMIT 1", (plan_id, now, plan_id)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE build_plan_items SET claimed_by=? WHERE plan_id=? AND unit_key=? AND state IN ('pending','published') "
                     "AND claimed_by IS NULL", (wid, plan_id, row["unit_key"]))
        items = conn.execute("SELECT * FROM build_plan_items WHERE plan_id=? AND unit_key=? AND claimed_by=? ORDER BY id",
                             (plan_id, row["unit_key"], wid)).fetchall()
    return [dict(i) for i in items]


def _set(item_id: int, **fields: Any) -> None:
    fields["updated_at"] = db.now()
    cols = ",".join(f"{k}=?" for k in fields)
    with db.tx() as conn:
        conn.execute(f"UPDATE build_plan_items SET {cols} WHERE id=?", (*fields.values(), item_id))  # nosec B608


def _process_unit(run_id: str, plan_id: str, unit: list[dict[str, Any]], opts: dict[str, Any], throttle: _Throttle,
                  progress: _Progress, roots: _Roots, wid: str, stop: dict[str, Any]) -> None:
    progress.current[wid] = unit[0]["title"] or ""
    work = [i for i in unit if i["state"] in ("pending", "published")]
    if not work:
        _release(unit)
        return
    try:
        # One attempt of this book (counted here, before anything can fail, so a book that keeps failing in any step
        # reaches MAX_ATTEMPTS and stops being retried). A published copy that vanished goes back to 'pending' and is
        # copied again within the same attempt budget.
        with db.tx() as conn:
            conn.executemany("UPDATE build_plan_items SET attempts=attempts+1,updated_at=? WHERE id=?", [(db.now(), i["id"]) for i in work])
        if _requeue_lost(plan_id, unit[0]["unit_key"]):
            work = [dict(r) for r in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND unit_key=? AND state IN ('pending','published') "
                                            "ORDER BY id", (plan_id, unit[0]["unit_key"]))]
        if any(i["action"] == "convert" for i in work):
            it = next(i for i in work if i["action"] == "convert")
            if it["state"] == "pending":
                _convert_one(run_id, it, opts, progress)
                it = dict(db.one("SELECT * FROM build_plan_items WHERE id=?", (it["id"],)))
            _switch_convert(run_id, it, opts)
            progress.file_done()
        else:
            for n, it in enumerate(work):
                if n and _stop_requested(run_id):
                    raise _Interrupted()
                if it["state"] == "pending":
                    _copy_one(run_id, it, opts, throttle, progress, roots)
            fresh = [dict(r) for r in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND unit_key=? ORDER BY id", (plan_id, unit[0]["unit_key"]))]
            _switch_unit(run_id, [i for i in fresh if i["action"] in ("copy", "copy_as_is") and i["state"] in ("published",)], opts)
            for _ in work:
                progress.file_done()
    except _Interrupted:
        # the published files of this book stay journaled; Resume finishes it. A pause is not a failed attempt.
        with db.tx() as conn:
            conn.executemany("UPDATE build_plan_items SET attempts=MAX(0,attempts-1) WHERE id=?", [(i["id"],) for i in work])
    except _ItemError as exc:
        _fail_unit(run_id, plan_id, unit, exc, opts, stop)
    except OSError as exc:
        _fail_unit(run_id, plan_id, unit, _classify(exc), opts, stop)
    except sqlite3.OperationalError as exc:
        if not _transient_db_error(exc):
            _unexpected_in_book(run_id, plan_id, unit, exc, opts, stop)
        else:
            # 'database is locked' (another writer held the database past the busy timeout): the book waits and is tried
            # again later; it does not count as an attempt and never fails the book for good. The run goes on.
            log.warning("managed library build %s: database busy on %r; retrying later", run_id, unit[0].get("title"))
            try:
                with db.tx() as conn:
                    conn.executemany("UPDATE build_plan_items SET attempts=MAX(0,attempts-1),error=?,next_attempt_at=?,updated_at=? "
                                     "WHERE id=? AND state IN ('pending','published')",
                                     [(f"The database was busy ({exc}); trying again", db.now() + RETRY_BACKOFF_SECONDS, db.now(), i["id"])
                                      for i in work])
            except Exception:
                log.exception("managed library build %s: could not record the retry", run_id)
                stop.setdefault("reason", _unexpected(exc))
    except Exception as exc:
        _unexpected_in_book(run_id, plan_id, unit, exc, opts, stop)
    finally:
        _release(unit)


def _transient_db_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("database is locked" in msg or "database is busy" in msg or "database table is locked" in msg)


def _unexpected_in_book(run_id: str, plan_id: str, unit: list[dict[str, Any]], exc: BaseException, opts: dict[str, Any],
                        stop: dict[str, Any]) -> None:
    """A bug or a (non-transient) database error inside a book: the book fails (its unrecorded copies are removed) and
    the run stops with the error, instead of the exception leaving the run 'running' with no worker."""
    log.error("managed library build %s: unexpected error on %r", run_id, unit[0].get("title"), exc_info=exc)
    msg = _unexpected(exc)
    try:
        _fail_unit(run_id, plan_id, unit, _ItemError("internal_error", f"Unexpected error: {type(exc).__name__}: {str(exc)[:500]}"),
                   {**opts, "stop_on_error": False}, stop)
    except Exception:
        log.exception("managed library build %s: could not mark the book failed", run_id)
    stop.setdefault("reason", msg)


def _stop_requested(run_id: str) -> bool:
    """True at a safe point (between files) when the run must stop: pause or cancel asked, the run no longer 'running',
    or the admin switch "Allow builds to run" turned off, which is recorded as a pause request (Resume continues)."""
    r = db.one("SELECT pause_requested,cancel_requested,status FROM build_runs WHERE id=?", (run_id,)) or {}
    if r.get("pause_requested") or r.get("cancel_requested") or r.get("status") != "running":
        return True
    if not run_enabled():
        with db.tx() as conn:
            conn.execute("UPDATE build_runs SET pause_requested=1 WHERE id=? AND status='running'", (run_id,))
        return True
    return False


def _release(unit: list[dict[str, Any]]) -> None:
    with db.tx() as conn:
        conn.executemany("UPDATE build_plan_items SET claimed_by=NULL WHERE id=?", [(i["id"],) for i in unit])


def _classify(exc: OSError) -> _ItemError:
    e = exc.errno
    if e in (errno.ENOSPC, errno.EDQUOT):
        return _ItemError("no_space", f"The disk is full ({exc.strerror or exc})", stop_run="The managed root ran out of space")
    if e in (errno.EACCES, errno.EPERM, errno.EROFS):
        return _ItemError("permission", f"Permission denied: {exc.strerror or exc}")
    if e == errno.ENOENT:
        return _ItemError("missing", f"A file or folder vanished: {exc.strerror or exc}", retryable=True)
    return _ItemError("io_error", f"I/O error: {exc.strerror or exc}", retryable=True)


def _fail_unit(run_id: str, plan_id: str, unit: list[dict[str, Any]], exc: _ItemError, opts: dict[str, Any], stop: dict[str, Any]) -> None:
    """One file of a book failed: the book is not switched. Retry later (backoff) while attempts remain; otherwise the
    book fails and the copies it already published are removed again (they are ours: same inode as the journal)."""
    msg = str(exc)[:1500]
    _requeue_lost(plan_id, unit[0]["unit_key"])
    attempts = max([int((db.one("SELECT attempts FROM build_plan_items WHERE id=?", (i["id"],)) or {}).get("attempts") or 0) for i in unit] or [0])
    retry = exc.retryable and attempts < MAX_ATTEMPTS and not exc.stop_run
    if exc.stop_run:
        stop.setdefault("reason", f"{exc.stop_run}: {msg}" if exc.stop_run != msg else msg)
    now = db.now()
    if retry:
        with db.tx() as conn:
            conn.execute("UPDATE build_plan_items SET error=?,next_attempt_at=?,updated_at=? WHERE plan_id=? AND unit_key=? AND state IN ('pending','published')",
                         (msg, now + RETRY_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), now, plan_id, unit[0]["unit_key"]))
        return
    if exc.stop_run:
        with db.tx() as conn:            # a run-level stop (space, publish capability): the book waits, it is not failed,
            # and this was not an attempt of the book (the run stopped, not the book): Resume tries it with its full budget
            conn.execute("UPDATE build_plan_items SET error=?,attempts=MAX(0,attempts-1),updated_at=? WHERE plan_id=? AND unit_key=? "
                         "AND state IN ('pending','published')", (msg, now, plan_id, unit[0]["unit_key"]))
        return
    for it in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND unit_key=? AND state='published' AND reused=0", (plan_id, unit[0]["unit_key"])):
        _unlink_if(it["final_path"], _parse_ident(it["temp_ident"]))
    with db.tx() as conn:
        conn.execute("UPDATE build_plan_items SET state='failed',error=?,final_path=NULL,claimed_by=NULL,updated_at=? WHERE plan_id=? AND unit_key=? "
                     "AND state IN ('pending','published')", (msg, now, plan_id, unit[0]["unit_key"]))
    db.add_history("managed_build.item_failed", f"Managed library build: {unit[0]['title']} failed: {msg}",
                   work_id=unit[0]["work_id"], asset_id=unit[0]["asset_id"], data={"run_id": run_id, "code": exc.code})
    if opts.get("stop_on_error"):
        stop.setdefault("reason", f"stopped on the first error (stop_on_error): {msg}")


def _still_published(it: dict[str, Any]) -> bool:
    s = _sig(it["final_path"] or "")
    return bool(s and stat.S_ISREG(s["mode"]) and (s["dev"], s["ino"]) == _parse_ident(it["temp_ident"]))


def _requeue_lost(plan_id: str, unit_key: str) -> int:
    """Published copies of this book that are no longer there (or no longer ours) go back to 'pending' so they are
    copied again; otherwise the book would be retried forever against a file that does not exist."""
    lost = [i["id"] for i in db.all("SELECT * FROM build_plan_items WHERE plan_id=? AND unit_key=? AND state='published'", (plan_id, unit_key))
            if not _still_published(i)]
    if lost:
        with db.tx() as conn:
            conn.executemany("UPDATE build_plan_items SET state='pending',final_path=NULL,temp_ident=NULL,temp_path=NULL,sha256=NULL,reused=0,"
                             "updated_at=? WHERE id=? AND state='published'", [(db.now(), i) for i in lost])
    return len(lost)


def _dest_root(it: dict[str, Any]) -> dict[str, Any]:
    root = db.one("SELECT * FROM root_folders WHERE id=?", (it["dest_root_id"],))
    if not root or not root["enabled"] or root["management_mode"] != "managed":
        raise _ItemError("not_managed", "The destination is no longer an enabled managed root", stop_run="destination root changed")
    return root


def _dest_parts(root: dict[str, Any], dest: str) -> tuple[str, list[str], str]:
    root_real = os.path.realpath(root["path"])
    try:
        rel = Path(os.path.normpath(dest)).relative_to(os.path.normpath(root["path"]))
    except ValueError:
        raise _ItemError("outside_root", "Refusing: the destination is outside the managed root")
    if any(p in ("..", "") for p in rel.parts) or not rel.parts:
        raise _ItemError("outside_root", "Refusing: bad destination path")
    if path_guard.resolve_safe(Path(root_real), Path(root_real) / rel.parent) is None and os.path.lexists(Path(root_real) / rel.parent):
        raise _ItemError("symlink", "Refusing: the destination folder escapes the managed root")
    return root_real, list(rel.parent.parts), rel.name


def _check_source(it: dict[str, Any], roots: _Roots) -> dict[str, Any]:
    """The source must be the very file that was planned: same size, mtime, inode and device, a regular file reached
    without a symlink, inside an enabled adopt/observe root."""
    src = it["source_path"]
    r = roots.for_path(src)
    if r is None or r["management_mode"] == "managed" or not r["enabled"]:
        raise _ItemError("outside_roots", "The source is no longer in an enabled adopted/observe root")
    s = _sig(src)
    if s is None:
        raise _ItemError("missing", "The source file is gone")
    if stat.S_ISLNK(s["mode"]) or _symlinked_parent(src, r["path"]):
        raise _ItemError("symlink", "The source (or a folder above it) is a symbolic link; refused")
    if not stat.S_ISREG(s["mode"]):
        raise _ItemError("not_regular", "The source is not a regular file")
    if not _same_sig(s, it["src_size"], it["src_mtime_ns"], it["src_ino"], it["src_dev"]):
        raise _ItemError("source_changed", "The source file changed since the dry run (size, time or inode); run a new dry run for it")
    return s


def _space_check(path: str, needed: int) -> None:
    free = _free_bytes(path)
    if free - needed < SPACE_MARGIN_MIN_BYTES:
        raise _ItemError("no_space", f"Free space is low ({_gb(free)} free, {_gb(needed)} needed plus a {_gb(SPACE_MARGIN_MIN_BYTES)} margin)",
                         stop_run="Free space in the managed root ran low")


def _open_src(path: str) -> int:
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


def _write_all(fd: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _copy_stream(sfd: int, dfd: int, tmp_name: str, digest: Any, throttle: _Throttle, progress: _Progress) -> tuple[int, tuple[int, int]]:
    """Copy into a NEW temp file created through the folder handle (O_CREAT|O_EXCL|O_NOFOLLOW: never an existing file,
    never through a link). Returns (bytes written, (dev, ino) of the temp)."""
    tfd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o644, dir_fd=dfd)
    total = 0
    tst = os.fstat(tfd)
    try:
        while True:
            chunk = os.read(sfd, CHUNK_BYTES)
            if not chunk:
                break
            _write_all(tfd, chunk)
            if digest is not None:
                digest.update(chunk)
            total += len(chunk)
            throttle.consume(len(chunk))
            progress.add(len(chunk))
        os.fsync(tfd)
        try:
            os.posix_fadvise(tfd, 0, 0, os.POSIX_FADV_DONTNEED)     # the verify re-read should come from the disk, not the cache
        except (AttributeError, OSError):
            pass
    finally:
        os.close(tfd)
    return total, (tst.st_dev, tst.st_ino)


def _hash_at(dfd: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
    h = hashlib.sha256()
    try:
        while True:
            b = os.read(fd, CHUNK_BYTES)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def _hash_path(path: str) -> str:
    fd = _open_src(path)
    h = hashlib.sha256()
    try:
        while True:
            b = os.read(fd, CHUNK_BYTES)
            if not b:
                break
            h.update(b)
    finally:
        os.close(fd)
    return h.hexdigest()


def _verify(dfd: int, tmp_name: str, size: int, digest: str | None, mode: str) -> None:
    """Size always; 'full' re-reads the copy from the disk and compares its sha256 with the source stream's."""
    st = os.stat(tmp_name, dir_fd=dfd, follow_symlinks=False)
    if st.st_size != size:
        raise _ItemError("verify_failed", f"Verification failed: the copy has {st.st_size} bytes, the source {size}", retryable=True)
    if mode == "full" and digest is not None and _hash_at(dfd, tmp_name) != digest:
        raise _ItemError("verify_failed", "Verification failed: the copy's checksum differs from the source's; the copy was discarded", retryable=True)


def _name_taken(dfd: int, name: str) -> str | None:
    """The name a new file would collide with: exact, or (case-insensitive targets) the same name in another case."""
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return name
    except FileNotFoundError:
        pass
    want = unicodedata.normalize("NFC", name).casefold()
    for n in os.listdir(dfd):
        if unicodedata.normalize("NFC", n).casefold() == want:
            return n
    return None


def _referenced(path: str) -> bool:
    return bool(db.one("SELECT 1 x FROM media_files WHERE path=?", (path,)) or db.one("SELECT 1 x FROM managed_copies WHERE managed_path=?", (path,))
                or db.one("SELECT 1 x FROM build_plan_items WHERE final_path=? AND state IN ('publishing','published','done')", (path,))
                or derived.is_generated_path(path))


def _try_reuse(it: dict[str, Any], dfd: int, dir_path: str, base: str, size: int) -> tuple[str, str, tuple[int, int]] | None:
    """An identical file already at the destination name and owned by no record: use it, copy nothing (idempotent re-run).
    Opened ONCE through the folder handle with O_NOFOLLOW (a link is never reused or read through) and O_NONBLOCK (a FIFO
    cannot hang the run); size, link count and checksum are taken from that one open file, and the name must still hold
    the same file afterwards. Returns (path, sha256, (dev, ino))."""
    try:
        fd = os.open(base, os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
    except OSError:
        return None                      # not there, a symlink (ELOOP) or unreadable: copy instead
    full = os.path.join(dir_path, base)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size or st.st_nlink != 1 or _referenced(full):
            return None
        theirs = _hash_fd(fd)
    finally:
        os.close(fd)
    if theirs != _hash_path(it["source_path"]):
        return None
    try:
        again = os.stat(base, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return None
    if (again.st_dev, again.st_ino) != (st.st_dev, st.st_ino):
        return None
    return full, theirs, (st.st_dev, st.st_ino)


def _publish(it: dict[str, Any], dfd: int, dir_path: str, tmp_name: str, base: str, tident: tuple[int, int], how: str) -> str:
    """Give the verified temp file its final name WITHOUT ever replacing a file: hard link (fails if the name exists)
    then unlink the temp, or renameat2(NOREPLACE). A taken name (any case, or referenced by a record) gets ' (2)'..."""
    for n in range(1, 500):
        cand = _suffixed(base, n)
        full = os.path.join(dir_path, cand)
        if _name_taken(dfd, cand) or _referenced(full):
            continue
        _set(it["id"], state="publishing", final_path=full, temp_ident=_ident_str(*tident))
        try:
            if how == "link":
                os.link(tmp_name, cand, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
            elif not _renameat2_noreplace(dfd, tmp_name, dfd, cand):
                raise PublishUnsupported(UNSUPPORTED_MESSAGE)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EMLINK):
                raise PublishUnsupported(UNSUPPORTED_MESSAGE + f" ({exc.strerror or exc})")
            raise
        st = os.stat(cand, dir_fd=dfd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != tuple(tident):
            raise _ItemError("publish_race", "The published file is not the verified copy; stopped", stop_run="publish check failed")
        if how == "link":
            os.unlink(tmp_name, dir_fd=dfd)
        os.fsync(dfd)
        return full
    raise _ItemError("no_name", "Could not find a free file name for the copy")


def _copy_one(run_id: str, it: dict[str, Any], opts: dict[str, Any], throttle: _Throttle, progress: _Progress, roots: _Roots) -> None:
    root = _dest_root(it)
    how, msg = publish_capability(root["path"])
    if how is None:
        raise PublishUnsupported(msg)
    root_real, parts, base = _dest_parts(root, it["dest_path"])
    src_sig = _check_source(it, roots)
    size = src_sig["size"]
    _space_check(root_real, size)
    made: Made = []
    tmp_name = _temp_name(run_id, it["id"])
    dfd = -1
    temp_full = None
    try:
        dfd, made = _ensure_dirs(root_real, parts)
        dir_path = os.path.join(root_real, *parts)
        reused = _try_reuse(it, dfd, dir_path, base, size)
        if reused:
            _set(it["id"], state="published", final_path=reused[0], sha256=reused[1], reused=1, temp_ident=_ident_str(*reused[2]))
            progress.add(0)
            return
        temp_full = os.path.join(dir_path, tmp_name)
        _set(it["id"], state="copying", temp_path=temp_full, final_path=None, temp_ident=None)       # journal BEFORE the temp exists
        digest = hashlib.sha256() if opts["verify"] != "none" else None
        sfd = _open_src(it["source_path"])
        try:
            fst = os.fstat(sfd)
            if not _same_sig({"size": fst.st_size, "mtime_ns": fst.st_mtime_ns, "ino": fst.st_ino, "dev": fst.st_dev},
                             it["src_size"], it["src_mtime_ns"], it["src_ino"], it["src_dev"]):
                raise _ItemError("source_changed", "The source file changed since the dry run; run a new dry run for it")
            written, tident = _copy_stream(sfd, dfd, tmp_name, digest, throttle, progress)
            after = os.fstat(sfd)
        finally:
            os.close(sfd)
        if written != size or not _same_sig({"size": after.st_size, "mtime_ns": after.st_mtime_ns, "ino": after.st_ino, "dev": after.st_dev},
                                            size, src_sig["mtime_ns"], src_sig["ino"], src_sig["dev"]):
            raise _ItemError("source_changed_during_copy", "The source changed while it was being copied; the copy was discarded", retryable=True)
        hexd = digest.hexdigest() if digest is not None else None
        _verify(dfd, tmp_name, size, hexd, opts["verify"])
        tst = os.stat(tmp_name, dir_fd=dfd, follow_symlinks=False)
        if (tst.st_dev, tst.st_ino) != tident:
            raise _ItemError("publish_race", "The temp file was replaced while it was being verified; stopped", stop_run="publish check failed")
        final = _publish(it, dfd, dir_path, tmp_name, base, tident, how)
        temp_full = None
        post = _sig(it["source_path"])
        if not _same_sig(post, size, src_sig["mtime_ns"], src_sig["ino"], src_sig["dev"]):
            _unlink_if(final, tident)
            raise _ItemError("source_changed_during_copy", "The source changed while it was being copied; the copy was discarded", retryable=True)
        _set(it["id"], state="published", final_path=final, sha256=hexd, temp_path=None)
    except Exception:
        _discard_temp(dfd, tmp_name if temp_full is not None else None)
        cur = db.one("SELECT state FROM build_plan_items WHERE id=?", (it["id"],))
        if cur and cur["state"] in ("copying", "verifying", "publishing"):
            _set(it["id"], state="pending", temp_path=None, final_path=None, temp_ident=None)
        if dfd >= 0:
            os.close(dfd)
            dfd = -1
        _prune(root_real, made)
        raise
    finally:
        if dfd >= 0:
            os.close(dfd)


def _discard_temp(dfd: int, tmp_name: str | None) -> None:
    """Remove our temp file through the folder handle (only a regular file; a link planted there is left alone)."""
    if dfd < 0 or not tmp_name:
        return
    try:
        if stat.S_ISREG(os.stat(tmp_name, dir_fd=dfd, follow_symlinks=False).st_mode):
            os.unlink(tmp_name, dir_fd=dfd)
    except OSError:
        pass


def _switch_unit(run_id: str, items: list[dict[str, Any]], opts: dict[str, Any]) -> None:
    """ONE transaction per book: every active media_files row of the book is re-pointed at its verified managed copy (same
    id, so progress, bookmarks and listings follow), the original is kept as a preserved source, the items are done.
    Until this commits the original is the active file."""
    if not items:
        return
    now = db.now()
    first = items[0]
    root = db.one("SELECT * FROM root_folders WHERE id=?", (first["dest_root_id"],))
    stats = {}
    for it in items:
        try:
            st = os.lstat(it["final_path"])
        except FileNotFoundError:
            raise _ItemError("published_lost", "A copied file of this book disappeared before it was recorded; copying again", retryable=True)
        if (st.st_dev, st.st_ino) != _parse_ident(it["temp_ident"]) or not stat.S_ISREG(st.st_mode):
            raise _ItemError("published_lost", "A copied file changed before it was recorded; copying again", retryable=True)
        stats[it["id"]] = st
    with db.tx() as conn:
        asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (first["asset_id"],)).fetchone()
        for it in items:
            row = conn.execute("SELECT * FROM media_files WHERE id=?", (it["file_id"],)).fetchone()
            if row is None or row["path"] != it["source_path"]:
                raise _ItemError("record_changed", "The book's file record changed since the dry run (rescan, merge or cleanup); run a new dry run")
            cur_asset = conn.execute("SELECT * FROM media_assets WHERE id=?", (row["asset_id"],)).fetchone()
            st = stats[it["id"]]
            rel = os.path.relpath(it["final_path"], root["path"]) if root else None
            conn.execute(
                "INSERT INTO managed_copies(id,run_id,item_id,kind,work_id,asset_id,file_id,source_path,source_root_id,source_size,source_sig,"
                "source_row_json,asset_root_before,managed_path,managed_root_id,managed_ident,sha256,size_bytes,verify_mode,status,created_at) "
                "VALUES (?,?,?,'copy',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (db.uid(), run_id, it["id"], cur_asset["work_id"], row["asset_id"], row["id"], row["path"], it["source_root_id"], it["src_size"],
                 json.dumps([it["src_size"], it["src_mtime_ns"], it["src_ino"], it["src_dev"]]), db.json_dump(dict(row)),
                 cur_asset["root_folder_id"], it["final_path"], it["dest_root_id"], _ident_str(st.st_dev, st.st_ino), it["sha256"],
                 st.st_size, opts["verify"], now))
            conn.execute(
                "UPDATE media_files SET path=?,relative_path=?,adopted=0,managed=1,size_bytes=?,mtime=?,sha256=COALESCE(?,sha256),"
                "checksum_verified=COALESCE(?,checksum_verified),checksum_verified_at=CASE WHEN ? IS NULL THEN checksum_verified_at ELSE ? END,"
                "verified_at=?,missing=0,integrity_state='ok',integrity_detail_json='{}' WHERE id=? AND path=?",
                (it["final_path"], rel, st.st_size, st.st_mtime, it["sha256"], it["sha256"], it["sha256"], now, now, row["id"], row["path"]))
            conn.execute("UPDATE build_plan_items SET state='done',temp_path=NULL,error=NULL,updated_at=? WHERE id=?", (now, it["id"]))
        if asset:
            left = conn.execute("SELECT path FROM media_files WHERE asset_id=?", (asset["id"],)).fetchall()
            if root and all(os.path.normpath(f["path"]).startswith(os.path.normpath(root["path"]) + os.sep) for f in left):
                conn.execute("UPDATE media_assets SET root_folder_id=?,updated_at=? WHERE id=?", (root["id"], now, asset["id"]))
            conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                         (db.uid(), asset["work_id"], asset["id"], "managed_build.copied",
                          f"Managed copy verified: {len(items)} file(s) copied into {root['name'] if root else 'the managed library'} (originals kept)",
                          db.json_dump({"run_id": run_id, "files": [i["final_path"] for i in items], "verify": opts["verify"]}), now))


# ---- convert -----------------------------------------------------------------------------------

def _convert_one(run_id: str, it: dict[str, Any], opts: dict[str, Any], progress: _Progress) -> None:
    """Make the preferred-format file with the conversion engine's tools, straight into a temp name in the managed root,
    then publish it like a copy. The sources are only read."""
    from . import conversion

    roots = _Roots()
    root = _dest_root(it)
    how, msg = publish_capability(root["path"])
    if how is None:
        raise PublishUnsupported(msg)
    root_real, parts, base = _dest_parts(root, it["dest_path"])
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (it["asset_id"],))
    if not asset:
        raise _ItemError("record_changed", "The book's record is gone; run a new dry run")
    sources = db.json_load(it["sources_json"], []) or []
    files = []
    for s in sources:
        row = db.one("SELECT * FROM media_files WHERE id=?", (s["file_id"],))
        if not row or row["path"] != s["path"]:
            raise _ItemError("record_changed", "The book's file records changed since the dry run; run a new dry run")
        _check_source({**it, "source_path": s["path"], "src_size": s["size"], "src_mtime_ns": s["mtime_ns"], "src_ino": s["ino"], "src_dev": s["dev"]}, roots)
        files.append(row)
    _space_check(root_real, int(it["est_bytes"] or 0))
    cfg = conversion.get_settings()
    made: Made = []
    tmp_name = _temp_name(run_id, it["id"])
    dfd = ofd = -1
    temp_full = None
    try:
        dfd, made = _ensure_dirs(root_real, parts)
        dir_path = os.path.join(root_real, *parts)
        temp_full = os.path.join(dir_path, tmp_name)
        _set(it["id"], state="copying", temp_path=temp_full, final_path=None, temp_ident=None, percent=0)    # journal BEFORE the temp exists
        # The output file is created HERE, through the folder handle (O_CREAT|O_EXCL|O_NOFOLLOW: never an existing file,
        # never through a link), and the tools write into that open file: ffmpeg/ffprobe get /proc/self/fd/N (the fd is
        # passed to them), the EPUB is written to the descriptor. No tool ever opens a name in the managed root.
        ofd = os.open(tmp_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o644, dir_fd=dfd)
        ost = os.fstat(ofd)
        tident = (ost.st_dev, ost.st_ino)
        meta = conversion._book_meta(asset["work_id"], asset)

        def cancelled() -> bool:
            r = db.one("SELECT cancel_requested FROM build_runs WHERE id=?", (run_id,))
            return bool(r and r["cancel_requested"])
        last = [0.0]

        def on_progress(pct: float) -> None:
            if _clock() - last[0] >= FLUSH_SECONDS:
                last[0] = _clock()
                _set(it["id"], percent=round(pct, 1))
                progress.maybe_flush()
        try:
            if it["kind"] == "audiobook":
                plan = convert_audio.plan_audio(files, mono_kbps=cfg["audio_bitrate_mono_kbps"], stereo_kbps=cfg["audio_bitrate_stereo_kbps"])
                report = _encode_audio_into(ofd, plan, meta, asset, on_progress, cancelled)
                report.update({"order_basis": plan.order_basis, "mode": "stream copy" if plan.copy else f"aac {plan.bitrate_kbps}k", "sources": len(plan.sources)})
                derived_by, duration = convert_audio.tool_signature(plan), int(report["duration_seconds"])
            else:
                report = convert_ebook.to_epub(Path(files[0]["path"]), None, dest_fd=ofd, title=meta["title"], authors=meta["authors"])
                derived_by, duration = f"{convert_ebook.TOOL_VERSION} {report['tool']}; {report['mode']}", None
        except convert_audio.ConversionError as exc:
            raise _ItemError("convert_failed", f"Conversion failed: {exc}", retryable=exc.retryable and not exc.skip)
        os.fsync(ofd)
        ost = os.fstat(ofd)
        size = ost.st_size
        if (ost.st_dev, ost.st_ino) != tident:
            raise _ItemError("publish_race", "The converted file is not the file that was created; stopped", stop_run="publish check failed")
        if size <= 0:
            raise _ItemError("verify_failed", "The converted file is empty", retryable=True)
        hexd = _hash_fd(ofd) if opts["verify"] != "none" else None
        os.close(ofd)
        ofd = -1
        for s in sources:                   # changed while converting: discard, retry
            if not _same_sig(_sig(s["path"]), s["size"], s["mtime_ns"], s["ino"], s["dev"]):
                raise _ItemError("source_changed_during_copy", "A source file changed while converting; the result was discarded", retryable=True)
        tst = os.stat(tmp_name, dir_fd=dfd, follow_symlinks=False)
        if (tst.st_dev, tst.st_ino) != tident:
            raise _ItemError("publish_race", "The temp file was replaced while converting; stopped", stop_run="publish check failed")
        final = _publish(it, dfd, dir_path, tmp_name, base, tident, how)
        temp_full = None
        _set(it["id"], state="published", final_path=final, sha256=hexd, temp_path=None, percent=100,
             error=None, sources_json=db.json_dump({"sources": sources, "derived_by": derived_by, "duration": duration, "report": report}))
        progress.add(size)
    except Exception:
        if ofd >= 0:
            os.close(ofd)
            ofd = -1
        _discard_temp(dfd, tmp_name if temp_full is not None else None)
        cur = db.one("SELECT state FROM build_plan_items WHERE id=?", (it["id"],))
        if cur and cur["state"] in ("copying", "verifying", "publishing"):
            _set(it["id"], state="pending", temp_path=None, final_path=None, temp_ident=None)
        if dfd >= 0:
            os.close(dfd)
            dfd = -1
        _prune(root_real, made)
        raise
    finally:
        if ofd >= 0:
            os.close(ofd)
        if dfd >= 0:
            os.close(dfd)


def _proc_fd_ok() -> bool:
    return os.path.isdir("/proc/self/fd")


def _encode_audio_into(ofd: int, plan: Any, meta: dict[str, Any], asset: dict[str, Any], on_progress: Callable[[float], None],
                       cancelled: Callable[[], bool]) -> dict[str, Any]:
    """Encode the M4B into the already-open output file ``ofd``. ffmpeg and ffprobe are handed the descriptor
    (/proc/self/fd/N with pass_fds) so they never open a path in the managed root. Where /proc is not available the
    encode goes to a private 0700 scratch folder and is then copied into ``ofd``."""
    from . import conversion

    scratch = tempfile.mkdtemp(prefix="alx-build-")          # 0700, ours: the cover, and the fallback output
    try:
        cover = conversion._cover_for(asset["work_id"], plan, Path(scratch))
        if _proc_fd_ok():
            out = f"/proc/self/fd/{ofd}"
            convert_audio.run_ffmpeg(plan, meta, out, cover=cover, on_progress=on_progress, should_cancel=cancelled,
                                     pass_fds=(ofd,), overwrite=True)
            return convert_audio.verify_output(out, plan, pass_fds=(ofd,))
        out = os.path.join(scratch, "out.m4b")
        convert_audio.run_ffmpeg(plan, meta, out, cover=cover, on_progress=on_progress, should_cancel=cancelled)
        report = convert_audio.verify_output(out, plan)
        sfd = os.open(out, os.O_RDONLY | _O_NOFOLLOW)
        try:
            os.ftruncate(ofd, 0)
            off = 0
            while True:
                b = os.read(sfd, CHUNK_BYTES)
                if not b:
                    break
                view = memoryview(b)
                while view:
                    n = os.pwrite(ofd, view, off)
                    off += n
                    view = view[n:]
        finally:
            os.close(sfd)
        return report
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _switch_convert(run_id: str, it: dict[str, Any], opts: dict[str, Any]) -> None:
    """Register the published conversion as a derived asset (conversion._register: the same records a normal conversion
    makes) and journal it in managed_copies. Resumable: a conversion already registered for this file is only journaled."""
    from . import conversion

    if it["state"] != "published":
        return
    if not _still_published(it):
        raise _ItemError("published_lost", "The converted file changed before it was recorded; converting again", retryable=True)
    final = it["final_path"]
    extra = db.json_load(it["sources_json"], {}) or {}
    sources = extra.get("sources") if isinstance(extra, dict) else extra
    root = db.one("SELECT * FROM root_folders WHERE id=?", (it["dest_root_id"],))
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (it["asset_id"],))
    if not asset or not root:
        raise _ItemError("record_changed", "The book's record is gone; run a new dry run")
    conv = db.one("SELECT * FROM conversions WHERE output_path=? AND status='done'", (final,))
    if isinstance(extra, dict) and "preexisting_asset_id" not in extra:
        # Remember, BEFORE registering, whether the book already had a converted asset (conversion._register then reuses
        # it): Undo may only ever delete an asset this build created.
        pre = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id=?",
                     (asset["work_id"], asset["kind"], it["target_format"], asset["id"]))
        extra["preexisting_asset_id"] = pre["id"] if pre else None
        _set(it["id"], sources_json=db.json_dump(extra))
    if not conv:
        conv_id, now = db.uid(), db.now()
        kind = "audio" if it["kind"] == "audiobook" else "ebook"
        # In _inflight BEFORE the 'running' row is committed, until it is settled: the conversion queue's reap_orphans
        # requeues every 'running' row that no thread of this process owns, and must never take this one.
        with conversion._inflight_lock:
            conversion._inflight.add(conv_id)
        try:
            try:
                with db.tx() as conn:
                    conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,source_signature,params_json,estimated_bytes,"
                                 "trigger_source,requested_by,created_at,started_at,attempts) VALUES (?,?,?,?,?,'running',?,?,?,'build',?,?,?,1)",
                                 (conv_id, kind, asset["work_id"], asset["id"], it["target_format"], conversion._signature([s["path"] for s in sources]),
                                  db.json_dump({"managed_build_run": run_id, "destination_root_id": root["id"]}), it["est_bytes"], "managed-build", now, now))
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    raise _ItemError("conversion_queued", "A conversion of this book is already queued or running; retry when it has finished", retryable=True)
                raise
            try:
                info = {"root": root, "signature": conversion._signature([s["path"] for s in sources])}
                conversion._register(dict(db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))), asset, info, Path(final),
                                     extra.get("derived_by") or "managed-build", extra.get("duration"), extra.get("report") or {})
            except Exception as exc:               # never leave the conversion 'running' (it would block every retry)
                with db.tx() as conn:
                    conn.execute("UPDATE conversions SET status='failed',error=?,finished_at=? WHERE id=? AND status IN ('queued','running')",
                                 (str(exc)[:1000], db.now(), conv_id))
                if isinstance(exc, convert_audio.ConversionError):
                    raise _ItemError("convert_failed", str(exc))
                raise
        finally:
            with conversion._inflight_lock:
                conversion._inflight.discard(conv_id)
        conv = db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))
    frow = db.one("SELECT id,asset_id FROM media_files WHERE path=?", (final,))
    if frow is None:
        # Registering left no file record for the converted book (its conversion or asset vanished meanwhile): fail the
        # book (its unrecorded output is removed) instead of journaling a copy nobody can find.
        raise _ItemError("record_changed", "The converted book was not recorded (its conversion or file record is missing); "
                         "run a new dry run for it")
    st = os.lstat(final)
    now = db.now()
    pre_id = extra.get("preexisting_asset_id") if isinstance(extra, dict) else None
    created = 0 if (pre_id and pre_id == frow["asset_id"]) else 1
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO managed_copies(id,run_id,item_id,kind,work_id,asset_id,file_id,source_asset_id,source_path,source_root_id,source_size,"
            "source_sig,source_row_json,managed_path,managed_root_id,managed_ident,sha256,size_bytes,verify_mode,conversion_id,status,created_at,"
            "created_by_build) VALUES (?,?,?,'convert',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
            (db.uid(), run_id, it["id"], asset["work_id"], frow["asset_id"], frow["id"],
             asset["id"], it["source_path"], it["source_root_id"], it["src_size"], json.dumps(sources), "{}", final, root["id"],
             _ident_str(st.st_dev, st.st_ino), it["sha256"], st.st_size, opts["verify"], conv["id"] if conv else None, now, created))
        conn.execute("UPDATE build_plan_items SET state='done',error=NULL,updated_at=? WHERE id=?", (now, it["id"]))


# ---------------------------------------------------------------------------
# Status, coverage, per-book state
# ---------------------------------------------------------------------------

def run_status(run_id: str) -> dict[str, Any]:
    r = _run(run_id)
    live = _live.get(run_id)
    prog = live.snapshot() if live else db.json_load(r["progress_json"], {})
    counts = {x["state"]: x["n"] for x in db.all("SELECT state, COUNT(*) n FROM build_plan_items WHERE plan_id=? AND action!='skip' GROUP BY state",
                                                 (r["plan_id"],))}
    failures = db.all("SELECT id,title,source_path,error,attempts,kind FROM build_plan_items WHERE plan_id=? AND state='failed' ORDER BY id LIMIT 50",
                      (r["plan_id"],))
    plan = db.one("SELECT book_count FROM build_plans WHERE id=?", (r["plan_id"],)) or {}
    copies = db.one("SELECT COUNT(*) n, COUNT(DISTINCT work_id) w FROM managed_copies WHERE run_id=? AND status='active'", (run_id,))
    return {"run_id": r["id"], "plan_id": r["plan_id"], "status": r["status"], "started_at": r["started_at"], "finished_at": r["finished_at"],
            "started_by": r["started_by"], "stop_reason": r["stop_reason"], "pause_requested": bool(r["pause_requested"]),
            "cancel_requested": bool(r["cancel_requested"]), "progress": prog or {}, "counts": counts, "failures": failures,
            "book_count": plan.get("book_count"), "active_copies": copies["n"], "active_books": copies["w"],
            "options": db.json_load(r["options_json"], {})}


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for r in db.all("SELECT id FROM build_runs ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 100)),)):
        s = run_status(r["id"])
        s.pop("failures", None)
        out.append(s)
    return out


def status() -> dict[str, Any]:
    """Everything the Overview card, the roots banner and the build panel need in one call."""
    act = active_run()
    last = db.one("SELECT id FROM build_runs ORDER BY started_at DESC LIMIT 1")
    try:
        plan = latest_plan()
    except BuildRefused:
        plan = None
    return {"active_run": run_status(act["id"]) if act else None, "last_run": run_status(last["id"]) if last else None,
            "plan": plan, "coverage": coverage(), "run_enabled": run_enabled()}


def coverage() -> dict[str, Any]:
    """'N of M books have a verified managed copy (X of Y GB)' overall and per root. A book is covered when every book
    file it has in an adopted/observe root has a verified managed copy (or a converted copy in the managed library)."""
    roots = _Roots()
    rows = db.all("SELECT f.id,f.path,f.size_bytes,f.asset_id,a.work_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id "
                  "WHERE a.status='owned' AND a.derived_from_asset_id IS NULL AND a.kind IN ('ebook','audiobook')")
    copied = {r["file_id"]: r for r in db.all("SELECT file_id,source_root_id,source_size FROM managed_copies WHERE status='active' AND kind='copy'")}
    converted = {r["source_asset_id"]: r for r in db.all("SELECT source_asset_id,source_root_id,source_size FROM managed_copies "
                                                         "WHERE status='active' AND kind='convert'")}
    per_root: dict[str, dict[str, Any]] = {rid: {"works": set(), "covered_works": set(), "bytes": 0, "covered_bytes": 0, "files": 0}
                                           for rid in roots.by_id}
    work_state: dict[str, bool] = {}
    for f in rows:
        r = roots.for_path(f["path"])
        size = int(f["size_bytes"] or 0)
        if f["id"] in copied:
            c = copied[f["id"]]
            src_root = c["source_root_id"]
            covered = True
            if src_root in per_root:
                per_root[src_root]["works"].add(f["work_id"])
                per_root[src_root]["bytes"] += int(c["source_size"] or size)
                per_root[src_root]["covered_bytes"] += int(c["source_size"] or size)
                per_root[src_root]["files"] += 1
            work_state[f["work_id"]] = work_state.get(f["work_id"], True) and covered
            if r is not None and r["id"] in per_root:
                pr = per_root[r["id"]]
                pr["works"].add(f["work_id"]); pr["bytes"] += size; pr["files"] += 1
            continue
        if r is None:
            continue
        pr = per_root[r["id"]]
        pr["works"].add(f["work_id"]); pr["bytes"] += size; pr["files"] += 1
        if r["management_mode"] in ("adopt", "observe"):
            covered = f["asset_id"] in converted
            if covered:
                pr["covered_bytes"] += size
            work_state[f["work_id"]] = work_state.get(f["work_id"], True) and covered
    covered_works = {w for w, ok in work_state.items() if ok}
    out_roots = []
    for rid, pr in per_root.items():
        root = roots.by_id[rid]
        if root["management_mode"] in ("adopt", "observe"):
            pr["covered_works"] = pr["works"] & covered_works
        out_roots.append({"id": rid, "name": root["name"], "path": root["path"], "media_kind": root["media_kind"], "mode": root["management_mode"],
                          "enabled": bool(root["enabled"]), "books": len(pr["works"]), "files": pr["files"], "bytes": pr["bytes"],
                          "covered_books": len(pr["covered_works"]) if root["management_mode"] != "managed" else None,
                          "covered_bytes": pr["covered_bytes"] if root["management_mode"] != "managed" else None})
    total_bytes = sum(x["bytes"] for x in out_roots if x["mode"] in ("adopt", "observe"))
    total_cov = sum(x["covered_bytes"] or 0 for x in out_roots if x["mode"] in ("adopt", "observe"))
    # bytes of copied books count once, against their source root (their file rows now live in the managed root)
    return {"books_total": len(work_state), "books_covered": len(covered_works), "bytes_total": total_bytes, "bytes_covered": total_cov,
            "roots": out_roots}


def work_state(work_id: str) -> dict[str, Any]:
    """Per-asset managed-copy state for a book page (admin): verified / converted / copying n% / not copied yet, plus the
    preserved source copies (shown only in an admin disclosure)."""
    roots = _Roots()
    assets = db.all("SELECT id,kind,format,derived_from_asset_id FROM media_assets WHERE work_id=? AND status='owned' ORDER BY kind,format", (work_id,))
    act = active_run()
    out = []
    for a in assets:
        if a["derived_from_asset_id"]:
            continue
        files = db.all("SELECT id,path,size_bytes FROM media_files WHERE asset_id=?", (a["id"],))
        copies = db.all("SELECT * FROM managed_copies WHERE status='active' AND ((kind='copy' AND file_id IN (SELECT id FROM media_files WHERE asset_id=?)) "
                        "OR (kind='convert' AND source_asset_id=?)) ORDER BY created_at", (a["id"], a["id"]))
        in_source = [f for f in files if (roots.for_path(f["path"]) or {}).get("management_mode") in ("adopt", "observe")]
        in_managed = [f for f in files if (roots.for_path(f["path"]) or {}).get("management_mode") == "managed"]
        state, percent = "not_copied", None
        if any(c["kind"] == "convert" for c in copies):
            state = "converted"
        elif copies and not in_source:
            state = "verified"
        elif copies:
            state = "partial"
        elif files and not in_source and in_managed:
            state = "managed"           # it always lived in a managed root
        if act and state in ("not_copied", "partial"):
            it = db.one("SELECT COUNT(*) n, SUM(CASE WHEN state IN ('done','published') THEN 1 ELSE 0 END) d, MAX(percent) p FROM build_plan_items "
                        "WHERE plan_id=? AND asset_id=? AND state NOT IN ('skipped','cancelled','failed')", (act["plan_id"], a["id"]))
            if it and it["n"]:
                state = "copying"
                percent = round(100 * (it["d"] or 0) / it["n"]) if not it["p"] else round(it["p"])
        out.append({"asset_id": a["id"], "kind": a["kind"], "format": a["format"], "state": state, "percent": percent,
                    "files": len(files), "managed_files": len(in_managed),
                    "sources": [{"path": c["source_path"], "size_bytes": c["source_size"], "kind": c["kind"], "managed_path": c["managed_path"],
                                 "verified": c["verify_mode"], "copied_at": c["created_at"], "run_id": c["run_id"]} for c in copies]})
    return {"work_id": work_id, "assets": out, "active_run_id": act["id"] if act else None}


def is_build_path(path: str) -> bool:
    """True for a path the build owns or preserves: a preserved original (kept out of the library), a managed copy, a copy
    published but not yet recorded, or one an undo is moving. Scans, reconcile and adoption leave these alone, so a rescan
    after a build is a no-op for them."""
    if os.path.basename(path).startswith(TEMP_PREFIX):
        return True
    if db.one("SELECT 1 x FROM managed_copies WHERE (source_path=? AND status='active') OR (managed_path=? AND (status='active' OR undo_state IN ('undoing','moving','failed')))",
              (path, path)):
        return True
    return db.one("SELECT 1 x FROM build_plan_items WHERE final_path=? AND state IN ('publishing','published')", (path,)) is not None


# ---------------------------------------------------------------------------
# UNDO
# ---------------------------------------------------------------------------

_undo_lock = threading.Lock()


def _copies_for(run_id: str | None, work_id: str | None) -> list[dict[str, Any]]:
    if run_id:
        return db.all("SELECT * FROM managed_copies WHERE run_id=? AND status='active' ORDER BY created_at", (run_id,))
    return db.all("SELECT * FROM managed_copies WHERE status='active' AND (file_id IN (SELECT f.id FROM media_files f JOIN media_assets a ON a.id=f.asset_id "
                  "WHERE a.work_id=?) OR source_asset_id IN (SELECT id FROM media_assets WHERE work_id=?) OR asset_id IN (SELECT id FROM media_assets "
                  "WHERE work_id=?)) ORDER BY created_at", (work_id, work_id, work_id))


def undo(*, run_id: str | None = None, work_id: str | None = None, confirm_count: Any = None, user: Any = None) -> dict[str, Any]:
    """Undo build for a run or one book: the managed copies go to the managed root's trash (kept for the retention period,
    nothing deleted), the records point at the originals again (one transaction per book). Originals are never touched.
    A book is REFUSED (listed in ``problems``, nothing changed for it) when its original is gone, when its managed copy
    was renamed or moved since the build, or (converted books) when the converted asset existed before the build or has
    listening/reading history (progress, bookmarks) or other references: that asset and its file are kept."""
    if not run_id and not work_id:
        raise BuildRefused("bad_request", "Say which run or which book to undo")
    act = active_run()
    if act and act["status"] == "running":
        raise BuildRefused("busy", "A build is running; pause or cancel it before undoing")
    with _undo_lock:
        recover_undo()
        copies = _copies_for(run_id, work_id)
        if not copies:
            return {"undone_books": 0, "undone_files": 0, "problems": []}
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in copies:
            groups.setdefault(_group_key(c), []).append(c)
        books = len({_current_work(c) for c in copies})
        if run_id and (isinstance(confirm_count, bool) or confirm_count != books):
            raise BuildRefused("confirmation_required", f"Type the number of books to undo ({books}) to confirm", book_count=books)
        problems: list[dict[str, Any]] = []
        done_books = done_files = 0
        for key, group in groups.items():
            try:
                n, file_problems = _undo_group(group, user)
                done_files += n
                done_books += 1
                problems += [{"asset_id": key, **p} for p in file_problems]
            except BuildRefused as exc:
                problems.append({"asset_id": key, "code": exc.code, "error": str(exc)})
    db.add_history("managed_build.undo", f"Undo managed library build: {done_books} book(s), {done_files} file(s) moved to the managed trash"
                   + (f"; {len(problems)} problem(s) (books kept or refused)" if problems else ""), work_id=work_id,
                   data={"run_id": run_id, "work_id": work_id, "by": _uid(user), "problems": problems})
    return {"undone_books": done_books, "undone_files": done_files, "problems": problems}


def _group_key(c: dict[str, Any]) -> str:
    if c["kind"] == "convert":
        return "convert:" + (c["asset_id"] or c["id"])
    row = db.one("SELECT asset_id FROM media_files WHERE id=?", (c["file_id"],))
    return "copy:" + (row["asset_id"] if row else c["asset_id"] or c["id"])


def _current_work(c: dict[str, Any]) -> str | None:
    row = db.one("SELECT a.work_id FROM media_files f JOIN media_assets a ON a.id=f.asset_id WHERE f.id=?", (c["file_id"],)) if c["file_id"] else None
    return row["work_id"] if row else c["work_id"]


def _trash_path(root_path: str, managed_path: str) -> str:
    day = datetime.fromtimestamp(db.now(), tz=timezone.utc).strftime("%Y-%m-%d")
    rel = os.path.relpath(managed_path, root_path)
    return os.path.join(os.path.realpath(root_path), path_guard.TRASH_DIR_NAME, day, rel)


def asset_references(conn: Any, asset_id: str) -> list[str]:
    """What would be lost (deleted by ON DELETE CASCADE) or orphaned if this asset were deleted: listening/reading
    progress, bookmarks, a download record, an asset converted from it. Empty = nothing but the asset itself."""
    out = []
    for sql, what in (("SELECT 1 FROM media_progress WHERE asset_id=? LIMIT 1", "listening/reading progress"),
                      ("SELECT 1 FROM bookmarks WHERE asset_id=? LIMIT 1", "bookmarks"),
                      ("SELECT 1 FROM downloads WHERE asset_id=? LIMIT 1", "a download record"),
                      ("SELECT 1 FROM media_assets WHERE derived_from_asset_id=? LIMIT 1", "a book converted from it")):
        try:
            if conn.execute(sql, (asset_id,)).fetchone():
                out.append(what)
        except sqlite3.OperationalError:
            continue                     # a table this database does not have
    return out


KEPT_HISTORY = "kept: has listening/reading history"


def _check_undoable(conn: Any, c: dict[str, Any]) -> None:
    """Raise BuildRefused when this copy must not be undone now (see undo). Runs before anything changes AND again inside
    the switching transaction, so a change in between cannot slip through."""
    if c["kind"] == "copy":
        row = conn.execute("SELECT path FROM media_files WHERE id=?", (c["file_id"],)).fetchone()
        if row is None:
            raise BuildRefused("record_gone", f"The book's file record for {c['managed_path']} is gone (removed or merged away); nothing was undone for this book")
        if row["path"] != c["managed_path"]:
            raise BuildRefused("copy_moved", f"The managed copy was renamed or moved since the build (the book's file is now {row['path']}); "
                               "nothing was undone for this book and its original stays preserved")
        return
    if not c["created_by_build"]:
        raise BuildRefused("kept_preexisting", "kept: the converted book existed before this build; its asset and file were not removed")
    if not c["asset_id"] or conn.execute("SELECT 1 FROM media_assets WHERE id=? AND derived_from_asset_id IS NOT NULL", (c["asset_id"],)).fetchone() is None:
        raise BuildRefused("record_gone", "The converted book's record is gone; nothing was undone for this book")
    rows = conn.execute("SELECT path FROM media_files WHERE asset_id=?", (c["asset_id"],)).fetchall()
    if [r["path"] for r in rows] != [c["managed_path"]]:
        raise BuildRefused("copy_moved", "The converted copy was renamed, moved or given other files since the build; nothing was undone for this book")
    refs = asset_references(conn, c["asset_id"])
    if refs:
        raise BuildRefused("kept_history", f"{KEPT_HISTORY} ({', '.join(refs)}); the converted copy stays in the managed library")


def originals_missing(c: dict[str, Any]) -> list[str]:
    """The originals an undo of this managed copy would fall back on that are gone or changed (empty = all there).
    A copy falls back on its one original (size as saved); a converted book on EVERY source file of its conversion
    (each checked against the signature saved at planning: a regular file of the same size). Undo refuses a book with
    any missing original, and the trash never purges such a copy: it may be the book's only file."""
    if c["kind"] == "copy":
        wanted = [(c["source_path"], c["source_size"])]
    else:
        srcs = db.json_load(c.get("source_sig"), None)
        if isinstance(srcs, dict):
            srcs = srcs.get("sources")
        if not isinstance(srcs, list) or not srcs:
            return [c["source_path"] or "(the sources of this conversion are not recorded)"]
        wanted = [(s.get("path") if isinstance(s, dict) else None, s.get("size") if isinstance(s, dict) else None) for s in srcs]
    out = []
    for path, size in wanted:
        s = _sig(path) if path else None
        if s is None or not stat.S_ISREG(s["mode"]) or (size is not None and s["size"] != size):
            out.append(path or "(unknown)")
    return out


def trashed_copy_originals_missing(managed_copy_id: str | None) -> list[str]:
    """For a file_trash entry made by Undo build: the originals that are gone now (non-empty = the trashed copy may be
    the book's only file; the trash keeps it and never purges it). An unknown journal row counts as missing (kept)."""
    c = db.one("SELECT * FROM managed_copies WHERE id=?", (managed_copy_id,)) if managed_copy_id else None
    if not c:
        return ["(the build journal for this copy is missing)"]
    return originals_missing(c)


def _undo_group(group: list[dict[str, Any]], user: Any) -> tuple[int, list[dict[str, Any]]]:
    # 1. refuse up front: an original that is gone (the record would point at nothing), a managed copy whose record moved
    #    (renamed/moved: the UPDATE would match nothing while the copy is reported undone), a converted asset that is not
    #    ours to delete or has history.
    for c in group:
        gone = originals_missing(c)
        if gone:
            raise BuildRefused("source_gone", f"The original {gone[0]} is gone or changed; undo would leave the book without a file"
                               + ("" if c["kind"] == "copy" else "; the converted copy stays the book's file"))
    with db.session() as conn:
        for c in group:
            _check_undoable(conn, c)
    # 2. journal the intent
    now = db.now()
    with db.tx() as conn:
        for c in group:
            root = conn.execute("SELECT path FROM root_folders WHERE id=?", (c["managed_root_id"],)).fetchone()
            conn.execute("UPDATE managed_copies SET undo_state='undoing',trash_path=? WHERE id=?",
                         (_trash_path(root["path"], c["managed_path"]) if root else None, c["id"]))
    # 3. one transaction: records back on the originals; every statement must hit exactly its row or nothing changes
    try:
        with db.tx() as conn:
            for c in group:
                _check_undoable(conn, c)
                gone = originals_missing(c)          # again, right before the switch: an original removed meanwhile
                if gone:
                    raise BuildRefused("source_gone", f"The original {gone[0]} is gone or changed; undo would leave the book without a file")
                if c["kind"] == "copy":
                    src = json.loads(c["source_row_json"] or "{}")
                    n = conn.execute("UPDATE media_files SET path=?,relative_path=?,adopted=?,managed=?,size_bytes=?,mtime=?,sha256=?,checksum_verified=?,"
                                     "checksum_verified_at=?,integrity_state=?,integrity_detail_json=?,missing=0 WHERE id=? AND path=?",
                                     (c["source_path"], src.get("relative_path"), src.get("adopted", 1), src.get("managed", 0), src.get("size_bytes", c["source_size"]),
                                      src.get("mtime"), src.get("sha256"), src.get("checksum_verified"), src.get("checksum_verified_at"),
                                      src.get("integrity_state"), src.get("integrity_detail_json") or "{}", c["file_id"], c["managed_path"])).rowcount
                    if n != 1:
                        raise BuildRefused("copy_moved", f"The managed copy {c['managed_path']} is no longer the book's file; nothing was undone for this book")
                    row = conn.execute("SELECT asset_id FROM media_files WHERE id=?", (c["file_id"],)).fetchone()
                    if row and c["asset_root_before"] is not None:
                        conn.execute("UPDATE media_assets SET root_folder_id=?,updated_at=? WHERE id=?", (c["asset_root_before"], now, row["asset_id"]))
                else:
                    conn.execute("UPDATE conversions SET status='superseded',output_asset_id=NULL,error='Undone (managed library build undo)',finished_at=? "
                                 "WHERE output_asset_id=?", (now, c["asset_id"]))
                    n = conn.execute("DELETE FROM media_assets WHERE id=? AND derived_from_asset_id IS NOT NULL", (c["asset_id"],)).rowcount
                    if n != 1:
                        raise BuildRefused("record_gone", "The converted book's record is gone; nothing was undone for this book")
                conn.execute("UPDATE managed_copies SET status='undone',undo_state='moving',undone_at=?,undone_by=? WHERE id=?", (now, _uid(user), c["id"]))
    except BuildRefused:
        with db.tx() as conn:          # nothing was switched: back to active, as if undo was never asked
            conn.executemany("UPDATE managed_copies SET undo_state=NULL,trash_path=NULL WHERE id=? AND status='active'", [(c["id"],) for c in group])
        raise
    # 4. move the copies into the managed trash (no-clobber), log them for purge
    moved, problems = 0, []
    for c in group:
        err = _finish_undo_move(db.one("SELECT * FROM managed_copies WHERE id=?", (c["id"],)), user)
        if err:
            problems.append({"code": "move_failed", "error": f"{c['managed_path']}: {err}"})
        else:
            moved += 1
    return moved, problems


def _finish_undo_move(c: dict[str, Any], user: Any) -> str | None:
    """Move one undone copy into the managed trash and log it in file_trash. Returns an error text (None = moved, or the
    copy was already gone). Never reports a move that did not happen: 'moved' means the file now at the trash path is
    the very copy this build made (same device and inode). A trash name already taken by another file gets ' (2)'...;
    the chosen name is journaled before the move, and the move itself never replaces a file."""
    root = db.one("SELECT * FROM root_folders WHERE id=?", (c["managed_root_id"],))
    ident = _parse_ident(c["managed_ident"])

    def ours(path: str | None) -> bool:
        s = _sig(path) if path else None
        return bool(s and ident and stat.S_ISREG(s["mode"]) and (s["dev"], s["ino"]) == tuple(ident))
    here = _sig(c["managed_path"])
    trash = c["trash_path"]
    err: str | None = None
    moved = ours(trash)                       # an earlier attempt already moved it (recovery)
    if not moved:
        if not root or not trash:
            err = "the managed root is gone; the copy was left in place" if here else None
        elif here is None:
            err = None                        # the copy was already gone: nothing to move, the records are back on the originals
        elif not ours(c["managed_path"]):
            err = "the file at the managed path is not the copy this build made; left in place"
        else:
            root_real = os.path.realpath(root["path"])
            base_dir, base_name = os.path.split(trash)
            err = "no free name in the managed trash; the copy was left in place"
            for n in range(1, 200):
                cand = os.path.join(base_dir, _suffixed(base_name, n))
                if os.path.lexists(cand):
                    continue
                with db.tx() as conn:          # journal the name BEFORE the move (recovery checks it)
                    conn.execute("UPDATE managed_copies SET trash_path=? WHERE id=?", (cand, c["id"]))
                trash = cand
                try:
                    _move_to_trash(root_real, c["managed_path"], cand, ident)
                except FileExistsError:
                    continue                   # taken in between: next name
                except (OSError, _ItemError, filetrash.TrashError) as exc:
                    err = f"could not move it to the managed trash ({exc}); the copy was left in place"
                    break
                _prune_dirs_at(root_real, _rel_parts(root_real, c["managed_path"])[:-1])
                err = None
                break
            moved = ours(trash)
            if not err and not moved:
                err = "the copy is not in the managed trash after the move; left as it is"
    now = db.now()
    with db.tx() as conn:
        if moved and root:
            conn.execute(
                "INSERT INTO file_trash(id,batch_id,root_id,root_path,work_id,asset_id,asset_kind,asset_format,original_path,relative_path,trash_path,"
                "size_bytes,sha256,record_only,reason,keeper_file_id,file_json,status,trashed_by,trashed_at,expires_at) "
                "VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,0,?,NULL,?,'trashed',?,?,?)",
                (db.uid(), c["run_id"] or c["id"], root["id"], os.path.realpath(root["path"]), c["work_id"], c["asset_id"], c["managed_path"],
                 os.path.relpath(c["managed_path"], root["path"]), trash, c["size_bytes"], c["sha256"],
                 "managed-build undo: a copy of a preserved original", db.json_dump({"build_undo": True, "managed_copy_id": c["id"]}),
                 _uid(user), now, now + filetrash.retention_days() * 86400))
        conn.execute("UPDATE managed_copies SET undo_state=?,undo_error=?,trash_path=? WHERE id=?",
                     ("failed" if err else "done", err, trash if moved else c["trash_path"], c["id"]))
    return err


def _rel_parts(root_real: str, path: str) -> tuple[str, ...]:
    rel = filetrash._rel_to_root(path, root_real)
    if rel is None or not rel.parts or any(p in ("", ".", "..") for p in rel.parts):
        raise filetrash.MoveAborted(f"{path} is not inside the managed root {root_real}; refusing to move it")
    return rel.parts


def _move_to_trash(root_real: str, src_path: str, dst_path: str, ident: tuple[int, int] | None) -> None:
    """Undo's move of a managed copy into the managed trash, entirely THROUGH DIRECTORY HANDLES opened from the managed
    root with O_NOFOLLOW at every level (a folder swapped for a symlink is refused, never followed): the trash folders
    are made with mkdirat (``_ensure_dirs``), the copy must be the expected file (device and inode) right before the
    move, and the move never replaces anything: a hard link then unlink (the link checked again before the unlink), or
    renameat2(RENAME_NOREPLACE) where links are not supported. With neither the move is REFUSED (never a path-based
    or overwriting rename). FileExistsError when the trash name is taken."""
    if ident is None:
        raise filetrash.MoveAborted(f"{src_path}: the build journal has no identity for this copy; refusing to move it")
    sparts, dparts = _rel_parts(root_real, src_path), _rel_parts(root_real, dst_path)
    try:
        sfd = filetrash._open_dir(root_real, sparts[:-1])
    except OSError as exc:
        raise filetrash.MoveAborted(f"a folder of {src_path} is a symlink or gone: {exc}") from exc
    try:
        dfd, made = _ensure_dirs(root_real, dparts[:-1])
        try:
            sname, dname = sparts[-1], dparts[-1]
            if filetrash._ident_at(sname, sfd) != tuple(ident):
                raise filetrash.MoveAborted(f"{src_path} is not the copy this build made; nothing was moved")
            linked = False
            try:
                os.link(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
                linked = True
            except FileExistsError:
                raise
            except OSError:
                pass                                 # no hard links here: the no-replace rename below, or refuse
            if linked:
                if filetrash._ident_at(dname, dfd) != tuple(ident) or filetrash._ident_at(sname, sfd) != tuple(ident):
                    try:
                        os.unlink(dname, dir_fd=dfd)
                    except OSError:
                        pass
                    raise filetrash.MoveAborted(f"{src_path} changed while it was being moved; nothing was moved")
                try:
                    os.unlink(sname, dir_fd=sfd)
                except OSError as exc:
                    try:
                        os.unlink(dname, dir_fd=dfd)
                    except OSError:
                        pass
                    raise filetrash.MoveAborted(f"could not remove {src_path}: {exc}") from exc
            elif not _renameat2_noreplace(sfd, sname, dfd, dname):
                raise filetrash.MoveAborted("this managed root offers neither hard links nor a no-replace rename, so the copy "
                                            "cannot be moved safely; it was left in place")
            if filetrash._ident_at(dname, dfd) != tuple(ident):
                raise filetrash.MoveAborted(f"the file in the managed trash is not the copy this build made ({dst_path})")
        except BaseException:
            os.close(dfd)
            dfd = -1
            _prune(root_real, made)
            raise
        finally:
            if dfd >= 0:
                os.close(dfd)
    finally:
        os.close(sfd)


def _prune_dirs_at(root_real: str, parts: tuple[str, ...]) -> None:
    """Remove the now-empty folders ``parts`` (below the managed root, deepest first) through directory handles opened
    with O_NOFOLLOW; stops at the first folder that is not empty or not reachable. Never removes the root itself."""
    parts = tuple(parts)
    if any(p in ("", ".", "..") for p in parts):
        return
    for n in range(len(parts), 0, -1):
        try:
            pfd = filetrash._open_dir(root_real, parts[:n - 1])
        except OSError:
            return
        try:
            os.rmdir(parts[n - 1], dir_fd=pfd)
        except OSError:
            return
        finally:
            os.close(pfd)


def recover_undo() -> int:
    """Finish undos cut short: 'undoing' (records not switched yet) goes back to active; 'moving' completes the move."""
    n = 0
    with db.tx() as conn:
        n += conn.execute("UPDATE managed_copies SET undo_state=NULL,trash_path=NULL WHERE status='active' AND undo_state='undoing'").rowcount
    for c in db.all("SELECT * FROM managed_copies WHERE status='undone' AND undo_state='moving'"):
        _finish_undo_move(c, None)
        n += 1
    return n


def journal(run_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    _run(run_id)
    limit = max(1, min(int(limit), MAX_PAGE))
    total = db.one("SELECT COUNT(*) n FROM managed_copies WHERE run_id=?", (run_id,))["n"]
    rows = db.all("SELECT m.id,m.kind,m.work_id,w.title,m.source_path,m.managed_path,m.size_bytes,m.verify_mode,m.status,m.undo_state,m.trash_path,"
                  "m.undo_error,m.created_at,m.undone_at FROM managed_copies m LEFT JOIN works w ON w.id=m.work_id WHERE m.run_id=? "
                  "ORDER BY m.created_at LIMIT ? OFFSET ?", (run_id, limit, max(0, int(offset))))
    books = db.one("SELECT COUNT(DISTINCT work_id) n FROM managed_copies WHERE run_id=? AND status='active'", (run_id,))["n"]
    return {"items": rows, "total": total, "active_books": books, "limit": limit, "offset": max(0, int(offset))}


def legacy_entries(plan_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """The v1 response shape (entries) for the old /jobs/managed-library-build endpoints."""
    return [{"work_id": r["work_id"], "kind": r["kind"], "source_path": r["source_path"], "destination_path": r["dest_path"],
             "size_bytes": r["size_bytes"], "action": r["action"], "collision": bool(r["collision"]), "skip_reason": r["skip_reason"]}
            for r in db.all("SELECT * FROM build_plan_items WHERE plan_id=? ORDER BY id LIMIT ?", (plan_id, limit))]
