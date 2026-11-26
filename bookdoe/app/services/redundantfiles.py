"""Redundant-file analysis for one book (after a merge folds several copies together). Read-only.

DEFAULT-DENY: a wrong suggestion here destroys media, so a file is only offered when the evidence is strong
and every doubt means "not offered".

Two kinds of redundancy are reported, each labelled differently:

* ``identical``  (``checksum_identical``) the same sha256 and size in one asset. Files with no stored checksum
  are never matched on size or duration alone.
* ``similar`` / ``lower_quality``  (not byte-identical) two COMPLETE sets of audiobook parts, each in its own
  folder, with the same part numbers, every part pair within 1 s of duration and within 0.5% of size (or, for
  ``lower_quality``, the same duration at a clearly lower bitrate). Only whole sets are matched against each
  other (never one file against two, never a part against a different part, never a set against an incomplete
  one), and a set is removed all together or not at all (``requires_all``): an edition is never mixed.

Never offered, as a file to remove or as the copy to keep: a record whose path (or any folder of it inside the
root) is a symlink, a missing/empty/unreadable file, a file outside every library root, and any file that shares
its inode with another record or has several hard links (hardlinked names are ONE physical file: removing a name
frees nothing and the other name is not an independent copy). The last copy of a kind and format, and of every
part, is therefore always kept; filetrash re-checks this against the disk at removal time.

A converted copy (M4B from MP3, EPUB from MOBI) does not make its original redundant: it is a note only.
"""
from __future__ import annotations

import itertools
import os
import stat
from pathlib import Path
from typing import Any

from .. import db
from . import files as files_service

HASH_MAX_BYTES = 16 * 1024 ** 3        # a file bigger than this is never re-hashed, so never offered as identical
HASH_BUDGET_SECONDS = 120.0            # one analysis re-hashes for at most this long; the rest is reported, not offered
ROOT_LABEL = {"adopt": "Adopted", "observe": "Observe-only", "managed": "Managed"}
DURATION_TOLERANCE_S = 1.0
SIZE_TOLERANCE = 0.005
LOW_BITRATE_RATIO = 0.7


def root_for(path: str) -> dict[str, Any]:
    """How a file may be treated: the root that holds it, its mode, and whether the FILE (not only its record) may be moved."""
    r = files_service._resolve_root(path)
    return {"root": r.root, "mode": r.mode, "mutable": bool(r.mutable), "reason": r.reason}


def inspect(path: str, root_path: str | None = None) -> dict[str, Any]:
    """Facts about the file a record points at, read from the disk with lstat: ``ok`` only for a present, regular,
    non-empty, readable file that is not a symlink and has no symlinked folder between it and the root."""
    p = Path(os.path.abspath(path))
    try:
        st = os.lstat(p)
    except OSError:
        return {"ok": False, "why": "missing"}
    if stat.S_ISLNK(st.st_mode):
        return {"ok": False, "why": "symlink"}
    if not stat.S_ISREG(st.st_mode):
        return {"ok": False, "why": "not_regular"}
    if root_path:
        bases = {os.path.abspath(root_path), os.path.realpath(root_path)}
        for parent in p.parents:
            if str(parent) in bases:
                break
            try:
                if os.path.islink(parent):
                    return {"ok": False, "why": "symlinked_folder"}
            except OSError:
                return {"ok": False, "why": "unreadable"}
        else:
            return {"ok": False, "why": "outside_root"}
    if st.st_size == 0:
        return {"ok": False, "why": "empty"}
    if not os.access(p, os.R_OK):
        return {"ok": False, "why": "unreadable"}
    return {"ok": True, "why": "", "dev": st.st_dev, "ino": st.st_ino, "nlink": st.st_nlink, "size": st.st_size}


class HashUnavailable(OSError):
    """The file could not be hashed now (unreadable, too big, or the time budget ran out)."""


def signature(path: str) -> tuple[int, int, int, int] | None:
    """(st_dev, st_ino, size, mtime_ns) of the file as it is now; None if it is not a plain file."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) if stat.S_ISREG(st.st_mode) else None


def fresh_sha256(path: str, deadline: float | None = None, cache: dict | None = None) -> str:
    """The sha256 of the bytes on disk right now, streamed. With a ``cache`` ({path: (signature, hash)}) a file whose
    signature (device, inode, size, mtime) is unchanged since it was hashed is not read again. Raises HashUnavailable
    (never guesses)."""
    import hashlib
    import time
    sig = signature(path)
    if cache is not None and sig is not None and path in cache and cache[path][0] == sig:
        return cache[path][1]
    try:
        if os.lstat(path).st_size > HASH_MAX_BYTES:
            raise HashUnavailable(f"larger than {HASH_MAX_BYTES >> 30} GiB; not re-hashed")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                if deadline is not None and time.monotonic() > deadline:
                    raise HashUnavailable("checksum time budget used up")
        digest = h.hexdigest()
    except HashUnavailable:
        raise
    except OSError as exc:
        raise HashUnavailable(str(exc)) from exc
    if cache is not None and sig is not None and signature(path) == sig:      # only trust a hash taken from a file that did not change meanwhile
        cache[path] = (sig, digest)
    return digest


def _record_fresh_hash(file_id: str, stored: str | None, fresh: str) -> None:
    """Remember what the disk says in the verification columns. The scanner owns ``sha256``: it is never overwritten, so a
    silent change or bit rot stays visible as a disagreement; the first time a disagreement is seen an alert goes into the
    history (a visible 'checksum changed' notice)."""
    now = db.now()
    with db.tx() as conn:
        row = conn.execute("SELECT path,asset_id,checksum_verified FROM media_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return
        conn.execute("UPDATE media_files SET checksum_verified=?, checksum_verified_at=? WHERE id=?", (fresh, now, file_id))
        if stored and stored != fresh and row["checksum_verified"] != fresh:
            asset = conn.execute("SELECT work_id FROM media_assets WHERE id=?", (row["asset_id"],)).fetchone()
            conn.execute("INSERT INTO history(id,work_id,asset_id,event_type,message,data_json,created_at) VALUES (?,?,?,?,?,?,?)",
                         (db.uid(), asset["work_id"] if asset else None, row["asset_id"], "file.checksum_changed",
                          f"ALERT: {Path(row['path']).name} no longer matches its stored checksum (changed or damaged since it was scanned)",
                          db.json_dump({"file_id": file_id, "path": row["path"], "stored_sha256": stored, "disk_sha256": fresh}), now))


LINKS_MESSAGE = ("This storage does not support the safe move bookdoe needs (hard links); files here cannot be removed "
                 "automatically. Remove them by hand.")
_LINK_CAP: dict[str, tuple[bool, str, str]] = {}


def reset_link_cache() -> None:
    _LINK_CAP.clear()


def link_support(root_path: str) -> tuple[bool, str, str]:
    """(ok, code, message): can files be moved into this root's trash folder with a hard link, the only move bookdoe
    uses (never a copy)? Probed once per root with a throwaway file in the trash folder (removed again) and cached.
    code is 'ok', 'links_unsupported' (EXDEV, EPERM, ENOTSUP ... on this filesystem: FUSE, CIFS, exFAT) or
    'unsafe_trash_folder' (the trash folder or one above it is a symlink)."""
    root = os.path.realpath(root_path)
    if root in _LINK_CAP:
        return _LINK_CAP[root]
    result: tuple[bool, str, str]
    probe_dir = None
    try:
        cur = root
        for name in (".bookdoe-trash", ".probe"):
            cur = os.path.join(cur, name)
            try:
                os.mkdir(cur)
            except FileExistsError:
                pass
            st = os.lstat(cur)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise NotADirectoryError(cur)
        probe_dir = cur
        a, b = os.path.join(probe_dir, f"a-{db.uid()}"), os.path.join(probe_dir, f"b-{db.uid()}")
        fd = os.open(a, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            os.link(a, b)
            ok = os.lstat(a).st_nlink == 2 and os.lstat(b).st_ino == os.lstat(a).st_ino
        finally:
            for x in (b, a):
                try:
                    os.unlink(x)
                except OSError:
                    pass
        result = (True, "ok", "") if ok else (False, "links_unsupported", LINKS_MESSAGE)
    except NotADirectoryError:
        return (False, "unsafe_trash_folder", "The trash folder is a symlink or not a folder; refusing to use it.")     # not cached: fixable
    except OSError:
        result = (False, "links_unsupported", LINKS_MESSAGE)
    finally:
        if probe_dir:
            try:
                os.rmdir(probe_dir)
            except OSError:
                pass
    _LINK_CAP[root] = result
    return result


def _quality(f: dict[str, Any]) -> tuple:
    """Higher is better: in a managed root, hashed, higher bitrate, bigger, then the older record."""
    return (f["_managed_root"], bool(f["sha256"]), f["bitrate"] or 0, f["size_bytes"] or 0, -(f["discovered_at"] or 0), -len(f["path"]), f["path"])


def _describe(f: dict[str, Any]) -> dict[str, Any]:
    return {"id": f["id"], "path": f["path"], "name": Path(f["path"]).name, "size_bytes": f["size_bytes"] or 0, "duration_seconds": f["duration_seconds"],
            "part_number": f["part_number"], "bitrate": f["bitrate"], "sha256": f["sha256"], "missing": bool(f["missing"]),
            "root": f["_root_mode"], "root_name": f["_root_name"], "folder": str(Path(f["path"]).parent)}


def _remove_entry(f: dict[str, Any], keeper: dict[str, Any], today: str) -> dict[str, Any]:
    d = _describe(f)
    d["keeper_id"] = keeper["id"]
    d["checksum_identical"] = bool(f.get("_verified_hash") and f.get("_verified_hash") == keeper.get("_verified_hash"))
    d["file_will_move"] = bool(f["_managed_root"])
    d["trash_dir"] = str(Path(f["_root_path"]) / ".bookdoe-trash" / today) if f["_managed_root"] else None
    d["unsupported"] = False
    if f["_managed_root"]:
        ok, code, msg = link_support(f["_root_path"])
        d["unsupported"], d["unsupported_code"] = not ok, code
        d["note"] = "Moved to the library's trash folder; restorable until it expires." if ok else msg
        d["file_will_move"] = ok
    else:
        d["note"] = f"{ROOT_LABEL.get(f['_root_mode'], f['_root_mode'])} root: only the record is removed; the file stays on disk (a rescan may add it back)."
    return d


def _similar(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """'similar' / 'lower_quality' when two audio parts with the same part number are the same recording, else None."""
    da, db_ = a["duration_seconds"], b["duration_seconds"]
    if da is None or db_ is None or abs(da - db_) > DURATION_TOLERANCE_S:
        return None
    sa, sb = a["size_bytes"] or 0, b["size_bytes"] or 0
    if sa and sb and abs(sa - sb) <= SIZE_TOLERANCE * max(sa, sb):
        return "similar"
    ba, bb = a["bitrate"], b["bitrate"]
    if ba and bb and min(ba, bb) < LOW_BITRATE_RATIO * max(ba, bb):
        return "lower_quality"
    return None


def analyse(work_id: str, hash_cache: dict | None = None) -> dict[str, Any]:
    """Redundant-file groups for one Work. Files and file records are never touched; the only writes are the verification
    columns and an alert when a fresh hash disagrees with the stored one. ``hash_cache`` lets a caller that hashed already
    (before taking a lock) skip files that have not changed since."""
    from datetime import datetime, timezone
    today = datetime.fromtimestamp(db.now(), tz=timezone.utc).strftime("%Y-%m-%d")
    assets = db.all("SELECT id,kind,format,status,derived_from_asset_id FROM media_assets WHERE work_id=? ORDER BY kind,format", (work_id,))
    groups: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    import time
    deadline = time.monotonic() + HASH_BUDGET_SECONDS
    for a in assets:
        rows = db.all("SELECT id,path,size_bytes,duration_seconds,part_number,bitrate,sha256,missing,discovered_at FROM media_files WHERE asset_id=? ORDER BY part_number,path", (a["id"],))
        if len(rows) < 2:
            continue
        eligible: list[dict[str, Any]] = []
        for f in rows:
            res = root_for(f["path"])
            if res["root"] is None:
                ignored.append({"id": f["id"], "path": f["path"], "why": "outside_every_root"})
                continue
            insp = inspect(f["path"], res["root"]["path"])
            if not insp["ok"]:
                ignored.append({"id": f["id"], "path": f["path"], "why": insp["why"]})
                continue
            f.update(_managed_root=res["mutable"], _root_mode=res["mode"] or "outside", _root_name=res["root"]["name"], _root_path=res["root"]["path"],
                     _dev=insp["dev"], _ino=insp["ino"], _nlink=insp["nlink"], _size_on_disk=insp["size"])
            eligible.append(f)
        # Hardlinked names are one physical file: never offered, never counted as another copy.
        seen = itertools.groupby(sorted(eligible, key=lambda f: (f["_dev"], f["_ino"])), key=lambda f: (f["_dev"], f["_ino"]))
        shared = {k for k, g in seen if len(list(g)) > 1}
        keep_ok = []
        for f in eligible:
            if (f["_dev"], f["_ino"]) in shared or f["_nlink"] > 1:
                ignored.append({"id": f["id"], "path": f["path"], "why": "hardlinked"})
            else:
                keep_ok.append(f)
        consumed: set[str] = set()
        # 1. byte-identical: the stored checksums only NOMINATE candidates; the disk decides. Every nominated file is
        #    re-hashed now and two files are identical only when their fresh hashes match each other.
        nominated: dict[tuple, list[dict[str, Any]]] = {}
        for f in keep_ok:
            if f["sha256"] and f["_size_on_disk"]:
                nominated.setdefault((f["sha256"], f["_size_on_disk"]), []).append(f)
        by_hash: dict[tuple, list[dict[str, Any]]] = {}
        for key, members in nominated.items():
            if len(members) < 2:
                continue
            for f in members:
                try:
                    fresh = fresh_sha256(f["path"], deadline, hash_cache)
                except HashUnavailable as exc:
                    ignored.append({"id": f["id"], "path": f["path"], "why": "unverified", "detail": str(exc)})
                    consumed.add(f["id"])         # not identical to anything, and not a "similar" candidate either
                    continue
                _record_fresh_hash(f["id"], f["sha256"], fresh)
                f["_verified_hash"] = fresh
                by_hash.setdefault((fresh, f["_size_on_disk"]), []).append(f)
        for members in by_hash.values():
            if len(members) < 2:
                for m in members:
                    consumed.add(m["id"])
                continue
            ranked = sorted(members, key=_quality, reverse=True)
            keep, rest = ranked[0], ranked[1:]
            consumed.update(m["id"] for m in members)
            remove = [_remove_entry(f, keep, today) for f in rest]
            groups.append({"id": keep["id"], "asset_id": a["id"], "kind": a["kind"], "format": a["format"], "code": "identical", "checksum_identical": True,
                           "requires_all": False, "reason": "same checksum", "keep": _describe(keep), "keeps": [_describe(keep)], "remove": remove,
                           "bytes_freed": sum(r["size_bytes"] for r in remove if r["file_will_move"]),
                           "recommendation": f"Keep {Path(keep['path']).name}; the {len(remove)} other file{'s are' if len(remove) != 1 else ' is'} byte-for-byte identical."})
        # 2. whole sets of audio parts that are the same recording
        if a["kind"] == "audiobook":
            by_dir: dict[str, list[dict[str, Any]]] = {}
            for f in keep_ok:
                if f["id"] not in consumed:
                    by_dir.setdefault(str(Path(f["path"]).parent), []).append(f)
            usable: dict[str, dict[Any, dict[str, Any]]] = {}
            for d, fs in by_dir.items():
                parts = [f["part_number"] for f in fs]
                whole = len([r for r in rows if str(Path(r["path"]).parent) == d])    # every record of the folder, offered or not
                if len(fs) >= 2 and all(p is not None for p in parts) and len(set(parts)) == len(parts) and whole == len(fs):
                    usable[d] = {f["part_number"]: f for f in fs}
            pairs = []
            for d1, d2 in itertools.combinations(sorted(usable), 2):
                p1, p2 = usable[d1], usable[d2]
                if set(p1) != set(p2):
                    continue
                codes = [_similar(p1[k], p2[k]) for k in p1]
                if all(codes):
                    pairs.append((d1, d2, "lower_quality" if "lower_quality" in codes else "similar"))
            counts: dict[str, int] = {}
            for d1, d2, _ in pairs:
                counts[d1] = counts.get(d1, 0) + 1
                counts[d2] = counts.get(d2, 0) + 1
            for d1, d2, code in pairs:
                if counts[d1] > 1 or counts[d2] > 1:
                    continue            # a set that matches two others is ambiguous: offer nothing
                s1, s2 = list(usable[d1].values()), list(usable[d2].values())

                def score(fs):
                    return (sum(1 for f in fs if f["_managed_root"]), sum(f["bitrate"] or 0 for f in fs), sum(f["size_bytes"] or 0 for f in fs),
                            -max(f["discovered_at"] or 0 for f in fs))
                keep_set, rm_set = (s1, s2) if score(s1) >= score(s2) else (s2, s1)
                keeper_by_part = {f["part_number"]: f for f in keep_set}
                remove = [_remove_entry(f, keeper_by_part[f["part_number"]], today) for f in sorted(rm_set, key=lambda f: f["part_number"])]
                keeps = [_describe(f) for f in sorted(keep_set, key=lambda f: f["part_number"])]
                groups.append({"id": keeps[0]["id"], "asset_id": a["id"], "kind": a["kind"], "format": a["format"], "code": code, "checksum_identical": False,
                               "requires_all": True, "reason": "two complete sets of the same parts, same durations; not byte-identical",
                               "keep": keeps[0], "keeps": keeps, "remove": remove,
                               "bytes_freed": sum(r["size_bytes"] for r in remove if r["file_will_move"]),
                               "recommendation": "Keep both unless you know the second set is a worse copy of the first. Remove all of one set or none."})
    for d in db.all("SELECT d.id,d.format,d.kind,s.id src_id,s.format src_format FROM media_assets d JOIN media_assets s ON s.id=d.derived_from_asset_id WHERE d.work_id=?", (work_id,)):
        notes.append({"code": "original_kept", "asset_id": d["src_id"],
                      "message": f"The original {str(d['src_format']).upper()} copy is kept although a converted {str(d['format']).upper()} copy exists: "
                                 f"it is the only copy of {d['kind']}/{d['src_format']}, and a converted copy never makes its original redundant."})
    return {"work_id": work_id, "groups": groups, "notes": notes, "ignored": ignored,
            "totals": {"groups": len(groups), "files": sum(len(g["remove"]) for g in groups), "bytes_freed": sum(g["bytes_freed"] for g in groups),
                       "identical_files": sum(len(g["remove"]) for g in groups if g["checksum_identical"]),
                       "similar_files": sum(len(g["remove"]) for g in groups if not g["checksum_identical"])}}
