"""Format conversion to the preferred formats, originals kept (issue #265).

Audiobooks (MP3, M4A, FLAC, Ogg, Opus ...) become ONE chaptered M4B and MOBI/AZW/AZW3 ebooks
become EPUB. The result is a derived asset (services/derived.py), never a replacement: the
source asset and its files are not touched, and the derivative never counts as a second owned
copy. The tool work lives in convert_audio.py / convert_ebook.py; this module owns the rest:

- settings (``conversion.*`` keys), eligibility (``assess``), enqueueing (idempotent, bounded),
- the queue and history table ``conversions`` and the job body (``run_queue``) that the
  scheduler runs as the ``convert-audio`` / ``convert-ebook`` jobs, off the scheduler loop,
  one active run per kind (the lease), N workers inside it, a time budget per run,
- read-only (adopt/observe) roots are never written: a book living there is converted INTO the managed
  root of its kind (``<root>/<Author>/<Title>/<Title>.m4b``); a book in a managed root keeps its output next to it,
- safety: managed roots only, ``resolve_safe`` on every path, no overwrite (hard link
  publish), temp file in the destination folder then an atomic publish, a free-space check,
  partial files removed on every failure path, failures retried with backoff then parked.

Resumable means the queue survives a restart (a run found 'running' by a new process goes
back to 'queued' and its partial file is removed); the encode itself starts over.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings as app_settings
from . import convert_audio, convert_ebook, covers, derived, files as files_svc, scanner
from . import paths as path_guard
from .convert_audio import Cancelled, ConversionError

log = logging.getLogger(__name__)

KEY_PREFIX = "conversion."
KINDS: dict[str, dict[str, str]] = {
    "audio": {"job": "convert-audio", "media_kind": "audiobook", "target": "m4b", "label": "M4B"},
    "ebook": {"job": "convert-ebook", "media_kind": "ebook", "target": "epub", "label": "EPUB"},
}
KIND_FOR_MEDIA = {v["media_kind"]: k for k, v in KINDS.items()}
ACTIVE = ("queued", "running")

DEFAULTS: dict[str, Any] = {
    "enabled_audio": True,
    "enabled_ebook": True,
    "audio_bitrate_mono_kbps": 64,
    "audio_bitrate_stereo_kbps": 96,
    "audio_concurrency": 1,
    "ebook_concurrency": 1,
    # Policy for books arriving through the importer. Off: converting is always a deliberate act.
    "auto_on_import": False,
    # True: the automatic and bulk paths leave a book alone once it has a converted copy,
    # even if its source changed since. False: they re-convert it (the per-asset button always can).
    "only_if_no_preferred": True,
    "max_attempts": 3,
    "retry_backoff_minutes": 15,
    "disk_margin_mb": 1024,
    "queue_limit": 1000,
    # Where converted files of books in READ-ONLY (adopt/observe) roots go: the id of a managed root
    # of the matching kind. "" = automatic (the first enabled managed root of that kind).
    "audiobook_output_root": "",
    "ebook_output_root": "",
}
LIMITS = {
    "audio_bitrate_mono_kbps": (16, 256), "audio_bitrate_stereo_kbps": (32, 320),
    "audio_concurrency": (1, 4), "ebook_concurrency": (1, 4), "max_attempts": (1, 10),
    "retry_backoff_minutes": (1, 1440), "disk_margin_mb": (64, 1_048_576), "queue_limit": (1, 20000),
}
BULK_MAX_WORKS = 5000

_inflight: set[str] = set()
_inflight_lock = threading.Lock()


class ConversionRefused(ValueError):
    """The request is valid but cannot be done now; ``code`` is stable for the UI and tests."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings() -> dict[str, Any]:
    out = {}
    for name, default in DEFAULTS.items():
        value = db.get_setting(KEY_PREFIX + name, default)
        if isinstance(default, str):
            ok = isinstance(value, str)
        else:
            ok = isinstance(value, bool) if isinstance(default, bool) else isinstance(value, int) and not isinstance(value, bool)
        out[name] = value if ok else default
    return out


def validate_changes(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in DEFAULTS:
            raise ValueError(f"Unknown conversion setting: {name}")
        default = DEFAULTS[name]
        if isinstance(default, str):
            clean[name] = _validate_output_root(name, value)
            continue
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
            clean[name] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError(f"{name} must be a whole number")
        lo, hi = LIMITS[name]
        if not lo <= int(value) <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        clean[name] = int(value)
    return clean


def _validate_output_root(name: str, value: Any) -> str:
    """A destination-root setting is "" (automatic) or the id of an enabled managed root of the right kind."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a library root id, or empty for automatic")
    if not value:
        return ""
    media = "audiobook" if name.startswith("audiobook") else "ebook"
    root = db.one("SELECT * FROM root_folders WHERE id=?", (value,))
    if not root or not root["enabled"] or root["management_mode"] != "managed" or root["media_kind"] not in (media, "mixed"):
        raise ValueError(f"{name} must be an enabled managed {media} library root (converted files are only written to managed roots)")
    return value


def save_settings(changes: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
    """Persist validated changes and write one audit entry listing old -> new values."""
    clean = validate_changes(changes)
    before = get_settings()
    for name, value in clean.items():
        db.set_setting(KEY_PREFIX + name, value)
    after = get_settings()
    changed = {k: [before[k], after[k]] for k in clean if before[k] != after[k]}
    if changed:
        db.add_history("settings.conversion",
                       "Conversion settings changed: " + ", ".join(f"{k} {v[0]} -> {v[1]}" for k, v in changed.items()) + f" (by {actor or 'unknown'})",
                       data=changed)
    return after


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _present_files(asset_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM media_files WHERE asset_id=? AND missing=0 ORDER BY part_number, path", (asset_id,))


def duplicate_parts_problem(sources: list[dict[str, Any]]) -> str | None:
    """Why merging these audio parts would repeat audio, or None. Two parts with the same size and
    content, or one part number used twice (two numbering schemes side by side), mean a
    duplicated set (issue #719): never merge both."""
    by_size: dict[int, list[dict[str, Any]]] = {}
    for f in sources:
        try:
            size = os.stat(f["path"]).st_size
        except OSError:
            size = int(f.get("size_bytes") or 0)
        by_size.setdefault(size, []).append(f)
    for group in by_size.values():
        seen: dict[str, str] = {}
        for f in group if len(group) > 1 else []:
            try:
                fp = f.get("fingerprint") or scanner.fingerprint(Path(f["path"]))
            except OSError:
                continue
            if fp in seen:
                return (f"These files contain the same audio twice ({Path(seen[fp]).name} and {Path(f['path']).name}); "
                        "remove the duplicate files before converting")
            seen[fp] = f["path"]
    numbers = [f["part_number"] for f in sources if f.get("part_number") is not None]
    if len(numbers) != len(set(numbers)):
        return "The parts are numbered in two different ways (a part number appears twice); remove the leftover set before converting"
    return None


def _signature(paths: list[str]) -> str:
    """Identity of the source files (names, sizes, mtimes): a changed source re-converts."""
    parts = []
    for p in sorted(paths):
        try:
            st = os.stat(p)
            parts.append([os.path.basename(p), st.st_size, int(st.st_mtime)])
        except OSError:
            parts.append([os.path.basename(p), -1, 0])
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def _common_dir(paths: list[str]) -> str:
    return os.path.commonpath([os.path.dirname(p) for p in paths])


CROSS_ROOT_NOTE = "Saved in your managed library; the original stays where it is"


def destination_root(kind: str, cfg: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """The managed root that receives converted files of books living in read-only roots:
    the root chosen in Settings, else the first enabled managed root of the kind (a 'mixed'
    root only when no root of the exact kind exists). Returns (root, None) or (None, why)."""
    cfg = cfg or get_settings()
    media = KINDS[kind]["media_kind"]
    chosen = cfg.get(f"{media}_output_root") or ""
    if chosen:
        root = db.one("SELECT * FROM root_folders WHERE id=?", (chosen,))
        if root and root["enabled"] and root["management_mode"] == "managed" and root["media_kind"] in (media, "mixed"):
            return root, None
        return None, (f"The {media} destination chosen in Settings > Conversion is no longer an enabled managed {media} library root; "
                      "choose another one there")
    for wanted in (media, "mixed"):
        root = db.one("SELECT * FROM root_folders WHERE enabled=1 AND management_mode='managed' AND media_kind=? ORDER BY created_at, id LIMIT 1", (wanted,))
        if root:
            return root, None
    return None, (f"This book is in a read-only library root and there is no managed {media} library root to save the converted file in. "
                  f"Add a managed {media} root in Settings > Libraries")


def output_root_choices() -> dict[str, Any]:
    """For Settings > Conversion: the managed roots that can receive converted files, per media kind, which one is
    in effect now (``effective``) and how much space it has."""
    cfg = get_settings()
    out: dict[str, Any] = {}
    for kind, spec in KINDS.items():
        media = spec["media_kind"]
        roots = db.all("SELECT id,name,path,media_kind FROM root_folders WHERE enabled=1 AND management_mode='managed' AND media_kind IN (?,'mixed') "
                       "ORDER BY (media_kind='mixed'), created_at, id", (media,))
        eff, why = destination_root(kind, cfg)
        free = None
        if eff:
            try:
                free = free_bytes(eff["path"])
            except OSError:
                pass
        out[media] = {"roots": [{"id": r["id"], "name": r["name"], "path": r["path"]} for r in roots],
                      "effective": {"id": eff["id"], "name": eff["name"], "path": eff["path"], "free_bytes": free} if eff else None,
                      "problem": why}
    return out


def destination_names(work_id: str, kind: str) -> tuple[str, str]:
    """(author folder, title folder and file stem) for ``<root>/<Author>/<Title>/<Title>.<ext>``, sanitised
    with the importer's rules and the kind's naming-template options."""
    from . import importer

    ctx = importer._work_context(work_id)
    opts = importer._template_opts(db.one("SELECT * FROM naming_templates WHERE media_kind=?", (KINDS[kind]["media_kind"],)))
    author = importer._safe_component(str(ctx["Author"]), fallback="Unknown Author", **opts)
    title = importer._safe_component(str(ctx["Title"]), fallback="Untitled", **opts)[:150].strip(" .") or "Untitled"
    return author, title


def _blocked(base: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {**base, "state": "blocked", "code": code, "reason": reason}


def assess(asset: dict[str, Any], *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Can this asset be converted? ``state``: available | stale (a derivative exists but the
    source changed) | converted (up to date) | blocked (``code`` and ``reason`` say why).
    Reads the database and stats the files; writes nothing."""
    cfg = cfg or get_settings()
    kind = KIND_FOR_MEDIA.get(asset.get("kind") or "")
    base: dict[str, Any] = {"asset_id": asset["id"], "work_id": asset["work_id"], "kind": kind,
                            "target": KINDS[kind]["target"] if kind else None}
    if asset.get("derived_from_asset_id"):
        return _blocked(base, "derived", "This is already a converted copy")
    if kind is None:
        return _blocked(base, "unsupported", "Only ebooks and audiobooks can be converted")
    label = KINDS[kind]["label"]
    if not cfg[f"enabled_{kind}"]:
        return _blocked(base, "disabled", f"{label} conversion is switched off in Settings > Conversion")
    if asset.get("status") != "owned":
        return _blocked(base, "not_owned", "Only an owned book can be converted")
    present = _present_files(asset["id"])
    if not present:
        return _blocked(base, "no_files", "No file for this asset is on disk")
    exts = {Path(f["path"]).suffix.lower() for f in present}
    target_ext = "." + KINDS[kind]["target"]
    if target_ext in exts:
        return _blocked(base, "already_preferred", f"Already a {label} file")
    if kind == "audio":
        sources = [f for f in present if Path(f["path"]).suffix.lower() in convert_audio.AUDIO_SOURCE_EXTS]
        if not sources:
            return _blocked(base, "unsupported", "No audio file in a convertible format")
        problem = duplicate_parts_problem(sources)
        if problem:
            return _blocked(base, "duplicate_parts", problem)
    else:
        sources = [f for f in present if Path(f["path"]).suffix.lower() in convert_ebook.EBOOK_SOURCE_EXTS][:1]
        if not sources:
            names = ", ".join(sorted(e.lstrip(".").upper() for e in exts)) or "this"
            return _blocked(base, "unsupported", f"{names} files cannot be converted to EPUB (supported: MOBI, AZW, AZW3)")
        ok, why = convert_ebook.convertible(Path(sources[0]["path"]))
        if not ok:
            drm = bool(why and "DRM" in why)
            return _blocked(base, "drm" if drm else "unsupported", why or "Cannot be converted")
    # Where the output goes: next to the source when that root is managed; otherwise (adopt/observe
    # roots are the user's files and stay read-only) into the managed root of the same kind. Never anywhere else.
    resolved = [files_svc._resolve_root(f["path"]) for f in sources]
    if any(r.root is None or not r.enabled for r in resolved):
        return _blocked(base, "outside_roots", "A source file is outside every enabled library root")
    if len({r.root["id"] for r in resolved}) != 1:
        return _blocked(base, "mixed_roots", "The files of this book are spread over more than one library root")
    src_root = resolved[0].root
    src_root_path = Path(src_root["path"])
    cross_root = not resolved[0].mutable
    if any(path_guard.resolve_safe(src_root_path, Path(f["path"])) is None for f in sources):
        return _blocked(base, "outside_roots", "A path escapes its library root; refusing")
    if cross_root:
        root, why = destination_root(kind, cfg)
        if root is None:
            return _blocked(base, "no_managed_root", why or "No managed library root to save the converted file in")
        root_path = Path(root["path"])
        author, title = destination_names(asset["work_id"], kind)
        out_dir = root_path / author / title
    else:
        root = src_root
        root_path = src_root_path
        out_dir = Path(_common_dir([f["path"] for f in sources]))
    if path_guard.resolve_safe(root_path, out_dir) is None:
        return _blocked(base, "outside_roots", "A path escapes its library root; refusing")
    signature = _signature([f["path"] for f in sources])
    info = {**base, "files": sources, "root": root, "out_dir": str(out_dir), "signature": signature,
            "estimated_bytes": _estimate(kind, sources, cfg), "cross_root": cross_root,
            "destination": root["name"], "source_root": src_root["name"], "source_mode": resolved[0].mode,
            "note": CROSS_ROOT_NOTE if cross_root else None}
    other = db.one("SELECT * FROM media_assets WHERE work_id=? AND kind=? AND format=? AND id!=?",
                   (asset["work_id"], asset["kind"], KINDS[kind]["target"], asset["id"]))
    if other and not other.get("derived_from_asset_id"):
        return _blocked(base, "has_preferred", f"This book already has a {label} asset")
    if other and other["derived_from_asset_id"] != asset["id"]:
        return _blocked(base, "has_preferred", f"This book already has a converted {label} copy made from another file")
    last = db.one("SELECT source_signature signature FROM conversions WHERE output_asset_id=? AND status='done' "
                  "ORDER BY finished_at DESC LIMIT 1", (other["id"],)) if other else None
    if other:
        info["derived_asset_id"] = other["id"]
        if not _present_files(other["id"]):
            return {**info, "state": "stale", "code": "stale", "reason": "The converted file is missing"}
        if last and last["signature"] == signature:
            return {**info, "state": "converted", "code": "converted", "reason": f"Already converted to {label}"}
        return {**info, "state": "stale", "code": "stale", "reason": "The source changed since it was converted"}
    return {**info, "state": "available", "code": None, "reason": None}


def _estimate(kind: str, sources: list[dict[str, Any]], cfg: dict[str, Any]) -> int:
    if kind == "ebook":
        return int(sum(int(f.get("size_bytes") or 0) for f in sources) * 1.3) + 200_000
    seconds = sum(int(f.get("duration_seconds") or 0) for f in sources)
    channels = max([int(f.get("channels") or 2) for f in sources] or [2])
    kbps = cfg["audio_bitrate_mono_kbps"] if channels == 1 else cfg["audio_bitrate_stereo_kbps"]
    if seconds <= 0:
        return int(sum(int(f.get("size_bytes") or 0) for f in sources) * 0.6)
    return int(seconds * kbps * 1000 / 8 * 1.03) + 600_000


def free_bytes(path: str) -> int:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def public_option(info: dict[str, Any]) -> dict[str, Any]:
    """The part of an assessment the API and UI may see (no file rows, no root dict, no paths)."""
    return {k: info.get(k) for k in ("asset_id", "kind", "target", "state", "code", "reason", "estimated_bytes", "derived_asset_id",
                                     "cross_root", "destination", "source_root", "note")}


def space_problem(info: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Why the DESTINATION filesystem (not the source's) cannot take this output now, or None."""
    needed = int(info.get("estimated_bytes") or 0)
    free = free_bytes(info["out_dir"])
    margin = cfg["disk_margin_mb"] * 1024 * 1024
    if free - needed < margin:
        return (f"Not enough free space in {info['destination']}: about {needed // 1048576} MB needed plus a {cfg['disk_margin_mb']} MB margin, "
                f"{free // 1048576} MB free")
    return None


def options_for_assets(assets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-asset conversion options for the Management book page: the assessment plus the
    latest conversion row of the asset (status, percent, error) so the page shows progress."""
    cfg = get_settings()
    out: dict[str, dict[str, Any]] = {}
    queued = db.one("SELECT COUNT(*) n FROM conversions WHERE status='queued'")["n"]
    for a in assets:
        if a.get("derived_from_asset_id") or KIND_FOR_MEDIA.get(a.get("kind") or "") is None:
            continue
        try:
            info = assess(a, cfg=cfg)
            if info["state"] in ("available", "stale"):
                why = space_problem(info, cfg)
                if why:
                    info = _blocked(info, "no_space", why)
                elif queued >= cfg["queue_limit"]:
                    info = _blocked(info, "queue_full", f"The conversion queue is full ({cfg['queue_limit']} waiting); try again when it has drained")
            opt = public_option(info)
        except OSError as exc:       # an unreadable folder must not break the book page
            opt = {"asset_id": a["id"], "state": "blocked", "code": "error", "reason": f"Cannot check: {exc.strerror or exc}"}
        last = db.one("SELECT id,status,percent,error,attempts,next_attempt_at FROM conversions WHERE source_asset_id=? "
                      "ORDER BY created_at DESC, rowid DESC LIMIT 1", (a["id"],))
        opt["conversion"] = last
        out[a["id"]] = opt
    return out


# ---------------------------------------------------------------------------
# Enqueue / cancel / retry / list
# ---------------------------------------------------------------------------


def _row(conv_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in ("tmp_path", "output_path", "params_json", "result_json", "cancel_requested")}
    out["result"] = db.json_load(row.get("result_json"), {}) or {}
    out["output_name"] = os.path.basename(row["output_path"]) if row.get("output_path") else None
    return out


def enqueue(asset_id: str, *, trigger: str = "manual", user: dict[str, Any] | None = None,
            allow_stale: bool = True) -> dict[str, Any]:
    """Queue one conversion. Idempotent: an active (queued/running) row for the same source and
    target is returned, an up-to-date conversion is refused. Raises ConversionRefused."""
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    if not asset:
        raise ConversionRefused("not_found", "Asset not found")
    cfg = get_settings()
    info = assess(asset, cfg=cfg)
    if info["state"] == "blocked":
        raise ConversionRefused(info["code"], info["reason"])
    if info["state"] == "converted":
        raise ConversionRefused("converted", info["reason"])
    if info["state"] == "stale" and not allow_stale:
        raise ConversionRefused("converted", "Already converted (the source changed; re-convert it from the book page)")
    target = info["target"]
    existing = db.one("SELECT * FROM conversions WHERE source_asset_id=? AND target_format=? AND status IN ('queued','running')", (asset_id, target))
    if existing:
        return {"created": False, "conversion": _public(existing)}
    if trigger == "manual":      # bulk checks the total up front; a single request checks its own output
        why = space_problem(info, cfg)
        if why:
            raise ConversionRefused("no_space", why)
    queued = db.one("SELECT COUNT(*) n FROM conversions WHERE status='queued'")["n"]
    if queued >= cfg["queue_limit"]:
        raise ConversionRefused("queue_full", f"The conversion queue is full ({cfg['queue_limit']} waiting); try again when it has drained")
    conv_id, now = db.uid(), db.now()
    by = (user or {}).get("username") or (user or {}).get("id")
    params = {"mono_kbps": cfg["audio_bitrate_mono_kbps"], "stereo_kbps": cfg["audio_bitrate_stereo_kbps"],
              "destination_root_id": info["root"]["id"], "destination": info["destination"], "cross_root": info["cross_root"]}
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,source_signature,params_json,estimated_bytes,"
                "trigger_source,requested_by,created_at) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?)",
                (conv_id, info["kind"], asset["work_id"], asset_id, target, info["signature"], db.json_dump(params),
                 info["estimated_bytes"], trigger, by, now))
    except Exception as exc:        # the partial unique index lost a race with a concurrent enqueue
        if "UNIQUE" in str(exc):
            row = db.one("SELECT * FROM conversions WHERE source_asset_id=? AND target_format=? AND status IN ('queued','running')", (asset_id, target))
            if row:
                return {"created": False, "conversion": _public(row)}
        raise
    db.add_history("conversion.queued", f"Queued {KINDS[info['kind']]['label']} conversion ({trigger}{', ' + by if by else ''})"
                   + (f"; the file will be saved in {info['destination']}, the original stays in {info['source_root']}" if info["cross_root"] else ""),
                   work_id=asset["work_id"], asset_id=asset_id,
                   data={"conversion_id": conv_id, "trigger": trigger, "destination_root_id": info["root"]["id"], "destination": info["destination"],
                         "cross_root": info["cross_root"]})
    kick(info["kind"])
    return {"created": True, "conversion": _public(_row(conv_id))}


def kick(kind: str) -> None:
    """Start the kind's job now (a no-op while one is running: it drains the queue itself)."""
    from . import scheduler

    job = db.one("SELECT id FROM jobs WHERE kind=?", (KINDS[kind]["job"],))
    if not job:
        return
    try:
        scheduler.submit_job(job["id"], trigger="manual")
    except scheduler.JobBusy:
        pass
    except Exception:       # never fail the request that queued the work
        log.warning("could not start the %s job", kind, exc_info=True)


PREFERRED_CODES = ("already_preferred", "has_preferred", "converted", "derived")


def _plan_all(work_ids: list[str], cfg: dict[str, Any]) -> dict[str, Any]:
    work_ids = list(dict.fromkeys(work_ids))
    if len(work_ids) > BULK_MAX_WORKS:
        raise ConversionRefused("too_many", f"At most {BULK_MAX_WORKS} books per request")
    eligible: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    reasons: dict[str, str] = {}
    dests: dict[str, dict[str, Any]] = {}
    for wid in work_ids:
        assets = db.all("SELECT * FROM media_assets WHERE work_id=? AND status='owned' AND derived_from_asset_id IS NULL ORDER BY kind,format", (wid,))
        counted = False
        for a in assets:
            if KIND_FOR_MEDIA.get(a["kind"]) is None:
                continue
            counted = True
            info = assess(a, cfg=cfg)
            if info["state"] == "available" or (info["state"] == "stale" and not cfg["only_if_no_preferred"]):
                if db.one("SELECT 1 FROM conversions WHERE source_asset_id=? AND target_format=? AND status IN ('queued','running')", (a["id"], info["target"])):
                    skipped["already_queued"] = skipped.get("already_queued", 0) + 1
                    continue
                eligible.append({"asset_id": a["id"], "work_id": wid, "kind": info["kind"], "target": info["target"],
                                 "estimated_bytes": info["estimated_bytes"], "state": info["state"], "cross_root": info["cross_root"],
                                 "destination": info["destination"]})
                d = dests.setdefault(info["root"]["id"], {"root_id": info["root"]["id"], "name": info["root"]["name"], "path": info["root"]["path"],
                                                          "count": 0, "estimated_bytes": 0, "cross_root": info["cross_root"]})
                d["count"] += 1
                d["estimated_bytes"] += info["estimated_bytes"]
            else:
                code = info["code"] or info["state"]
                skipped[code] = skipped.get(code, 0) + 1
                reasons.setdefault(code, info["reason"])
        if not counted:
            skipped["no_owned_files"] = skipped.get("no_owned_files", 0) + 1
            reasons.setdefault("no_owned_files", "No owned audiobook or ebook file to convert")
    margin = cfg["disk_margin_mb"] * 1024 * 1024
    short: list[str] = []
    for d in dests.values():
        try:
            d["free_bytes"] = free_bytes(d["path"])
        except OSError:
            d["free_bytes"] = None
        d["margin_bytes"] = margin
        d["fits"] = d["free_bytes"] is None or d["estimated_bytes"] <= d["free_bytes"] - margin
        if not d["fits"]:
            short.append(f"{d['name']}: about {d['estimated_bytes'] // 1048576} MB needed plus a {cfg['disk_margin_mb']} MB margin, "
                         f"{(d['free_bytes'] or 0) // 1048576} MB free")
    destinations = list(dests.values())
    first_free = destinations[0]["free_bytes"] if destinations else None
    blocked = {c: n for c, n in skipped.items() if c not in PREFERRED_CODES and c != "already_queued"}
    return {"works": len(work_ids), "eligible": len(eligible), "audio": sum(e["kind"] == "audio" for e in eligible),
            "ebook": sum(e["kind"] == "ebook" for e in eligible), "estimated_bytes": sum(e["estimated_bytes"] for e in eligible),
            "free_bytes": first_free, "skipped": skipped, "skipped_reasons": reasons, "originals_kept": True, "items": eligible,
            "categories": {"will_convert": len(eligible), "already_preferred": sum(skipped.get(c, 0) for c in PREFERRED_CODES),
                           "already_queued": skipped.get("already_queued", 0), "blocked": sum(blocked.values())},
            "blocked": {c: {"count": n, "reason": reasons.get(c)} for c, n in blocked.items()},
            "into_managed_library": sum(e["cross_root"] for e in eligible), "destinations": destinations,
            "margin_bytes": margin, "fits": not short, "shortfall": short}


def plan_bulk(work_ids: list[str], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dry run for a selection of works: what would be queued, what is skipped and why, and the
    disk it would use. Writes nothing."""
    plan = _plan_all(work_ids, cfg or get_settings())
    plan["items"] = plan["items"][:200]
    return plan


def enqueue_bulk(work_ids: list[str], *, user: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_settings()
    plan = _plan_all(work_ids, cfg)
    if not plan["fits"]:      # all-or-nothing: a half-converted selection that runs the disk full helps nobody
        raise ConversionRefused("no_space", "Not enough free space for this selection (" + "; ".join(plan["shortfall"]) + "). Select fewer books or free space.")
    queued = existing = 0
    errors: list[dict[str, str]] = []
    for item in plan.pop("items"):
        try:
            res = enqueue(item["asset_id"], trigger="bulk", user=user, allow_stale=not cfg["only_if_no_preferred"])
            queued += int(res["created"])
            existing += int(not res["created"])
        except ConversionRefused as exc:
            errors.append({"asset_id": item["asset_id"], "error": str(exc)})
            if exc.code == "queue_full":
                break
    db.add_history("conversion.bulk", f"Bulk conversion requested for {plan['works']} book(s): {queued} queued",
                   data={"works": plan["works"], "queued": queued, "by": (user or {}).get("username")})
    return {**plan, "queued": queued, "already_queued": existing, "errors": errors[:50]}


def maybe_auto_enqueue(asset_id: str) -> dict[str, Any] | None:
    """Importer hook: queue a conversion of a freshly imported asset when the policy says so
    (off by default). Never raises: an import must not fail because of this."""
    try:
        if not get_settings()["auto_on_import"]:
            return None
        return enqueue(asset_id, trigger="auto", allow_stale=False)
    except ConversionRefused as exc:
        log.info("auto-conversion of %s not queued: %s", asset_id, exc)
    except Exception:
        log.warning("auto-conversion of %s failed to queue", asset_id, exc_info=True)
    return None


def cancel(conv_id: str, user: dict[str, Any] | None = None) -> dict[str, Any]:
    row = _row(conv_id)
    if not row:
        raise ConversionRefused("not_found", "Conversion not found")
    if row["status"] == "queued":
        with db.tx() as conn:
            conn.execute("UPDATE conversions SET status='cancelled',finished_at=?,error='Cancelled' WHERE id=? AND status='queued'", (db.now(), conv_id))
    elif row["status"] == "running":
        with db.tx() as conn:
            conn.execute("UPDATE conversions SET cancel_requested=1 WHERE id=?", (conv_id,))
    else:
        raise ConversionRefused("not_active", f"A {row['status']} conversion cannot be cancelled")
    db.add_history("conversion.cancel", f"Cancelled {row['target_format'].upper()} conversion", work_id=row["work_id"],
                   asset_id=row["source_asset_id"], data={"conversion_id": conv_id, "by": (user or {}).get("username")})
    return _public(_row(conv_id))


def retry(conv_id: str, user: dict[str, Any] | None = None) -> dict[str, Any]:
    row = _row(conv_id)
    if not row:
        raise ConversionRefused("not_found", "Conversion not found")
    if row["status"] not in ("failed", "cancelled", "skipped"):
        raise ConversionRefused("not_retryable", f"A {row['status']} conversion cannot be retried")
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (row["source_asset_id"],))
    if not asset:
        raise ConversionRefused("not_found", "The source asset no longer exists")
    info = assess(asset)
    if info["state"] == "blocked":
        raise ConversionRefused(info["code"], info["reason"])
    if info["state"] == "converted":
        raise ConversionRefused("converted", info["reason"])
    try:
        with db.tx() as conn:
            conn.execute("UPDATE conversions SET status='queued',attempts=0,error=NULL,next_attempt_at=NULL,percent=0,finished_at=NULL,"
                         "cancel_requested=0,source_signature=?,estimated_bytes=? WHERE id=?", (info["signature"], info["estimated_bytes"], conv_id))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ConversionRefused("already_queued", "This asset already has an active conversion")
        raise
    db.add_history("conversion.retry", f"Retrying {row['target_format'].upper()} conversion", work_id=row["work_id"],
                   asset_id=row["source_asset_id"], data={"conversion_id": conv_id, "by": (user or {}).get("username")})
    kick(row["kind"])
    return _public(_row(conv_id))


def list_conversions(*, group: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Queue and history for Management. ``group``: 'active' (queued/running), 'history' (the rest)."""
    where, params = "1=1", []
    if group == "active":
        where = "c.status IN ('queued','running')"
    elif group == "history":
        where = "c.status NOT IN ('queued','running')"
    rows = db.all(
        f"""SELECT c.*, w.title work_title, a.format source_format FROM conversions c
            JOIN works w ON w.id=c.work_id LEFT JOIN media_assets a ON a.id=c.source_asset_id
            WHERE {where} ORDER BY CASE c.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, c.created_at DESC, c.rowid DESC
            LIMIT ? OFFSET ?""", (*params, limit, offset))  # nosec B608
    total = db.one(f"SELECT COUNT(*) n FROM conversions c WHERE {where}")["n"]  # nosec B608
    counts = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM conversions GROUP BY status")}
    items = []
    for r in rows:
        item = _public(r)
        item["work_title"], item["source_format"] = r["work_title"], r["source_format"]
        items.append(item)
    return {"items": items, "total": total, "counts": counts, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Runner (the job body)
# ---------------------------------------------------------------------------


def _update(conv_id: str, **fields: Any) -> None:
    cols = ",".join(f"{k}=?" for k in fields)
    with db.tx() as conn:
        conn.execute(f"UPDATE conversions SET {cols} WHERE id=?", (*fields.values(), conv_id))  # nosec B608


def _safe_partial(path: str | None) -> bool:
    """True for a temp file this module made: a hidden .partial inside a managed root."""
    if not path:
        return False
    p = Path(path)
    if not (p.name.startswith(".") and p.name.endswith(".partial")):
        return False
    mutable, _ = files_svc.is_mutable({"path": str(p)})
    return mutable


def _remove_partial(path: str | None) -> None:
    if _safe_partial(path):
        try:
            Path(path).unlink()   # type: ignore[arg-type]
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not remove partial conversion file %s", path, exc_info=True)


def reap_orphans() -> int:
    """Rows 'running' that no thread of THIS process owns were left by a stopped process:
    their partial file is removed and they go back to the queue (the next run starts over)."""
    with _inflight_lock:
        mine = set(_inflight)
    rows = [r for r in db.all("SELECT id,tmp_path FROM conversions WHERE status='running'") if r["id"] not in mine]
    for r in rows:
        _remove_partial(r["tmp_path"])
        _update(r["id"], status="queued", percent=0, tmp_path=None, started_at=None, error="Interrupted: the app stopped while this was converting; it will start over")
    if rows:
        log.warning("requeued %d interrupted conversion(s)", len(rows))
    return len(rows)


def _claim(kind: str) -> dict[str, Any] | None:
    now = db.now()
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM conversions WHERE kind=? AND status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "ORDER BY created_at, rowid LIMIT 1", (kind, now)).fetchone()
        if not row:
            return None
        n = conn.execute("UPDATE conversions SET status='running',started_at=?,heartbeat_at=?,attempts=attempts+1,percent=0 "
                         "WHERE id=? AND status='queued'", (now, now, row["id"])).rowcount
        if not n:
            return None
        with _inflight_lock:
            _inflight.add(row["id"])
        return dict(row)


def run_queue(kind: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """The convert-audio / convert-ebook job body: drain the queue of one kind within a time
    budget using ``<kind>_concurrency`` workers. The scheduler guarantees one run per kind."""
    cfg = get_settings()
    result = {"processed": 0, "done": 0, "failed": 0, "skipped": 0, "retrying": 0, "cancelled": 0, "conversion_ids": []}
    if not cfg[f"enabled_{kind}"]:
        return result
    reap_orphans()
    budget = max(60.0, float(app_settings.job_lease_seconds) / 2)
    started = time.monotonic()
    lock = threading.Lock()

    def worker() -> None:
        while time.monotonic() - started < budget:
            conv = _claim(kind)
            if conv is None:
                return
            try:
                outcome = _process(conv, cfg)
            except Exception as exc:        # a bug in a handler must not strand the row as 'running'
                log.exception("conversion %s crashed", conv["id"])
                outcome = _record_failure(conv, ConversionError(f"Unexpected error: {exc}"), cfg)
            finally:
                with _inflight_lock:
                    _inflight.discard(conv["id"])
            with lock:
                result["processed"] += 1
                result[outcome] = result.get(outcome, 0) + 1
                result["conversion_ids"].append(conv["id"])

    n = cfg[f"{kind}_concurrency"]
    if n <= 1:
        worker()
    else:
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"alx-conv-{kind}") as pool:
            for f in [pool.submit(worker) for _ in range(n)]:
                f.result()
    return result


def _heartbeat(kind: str, conv_id: str, percent: float | None = None) -> None:
    from . import scheduler

    fields: dict[str, Any] = {"heartbeat_at": db.now()}
    if percent is not None:
        fields["percent"] = round(percent, 1)
    _update(conv_id, **fields)
    scheduler.touch_lease(KINDS[kind]["job"])


def _record_failure(conv: dict[str, Any], exc: ConversionError, cfg: dict[str, Any]) -> str:
    """Store the outcome of a failed attempt. Returns the counter to bump in the run result."""
    now = db.now()
    message = str(exc)[:1800]
    row = _row(conv["id"]) or conv
    _remove_partial(row.get("tmp_path"))
    if isinstance(exc, Cancelled):
        _update(conv["id"], status="cancelled", error="Cancelled", finished_at=now, tmp_path=None, percent=0)
        return "cancelled"
    if exc.skip:
        _update(conv["id"], status="skipped", error=message, finished_at=now, tmp_path=None)
        db.add_history("conversion.skipped", f"Conversion skipped: {message}", work_id=conv["work_id"], asset_id=conv["source_asset_id"],
                       data={"conversion_id": conv["id"], "reason": message})
        return "skipped"
    attempts = int(row.get("attempts") or conv.get("attempts") or 1)
    if exc.retryable and attempts < cfg["max_attempts"]:
        delay = cfg["retry_backoff_minutes"] * 60 * (2 ** (attempts - 1))
        _update(conv["id"], status="queued", error=message, next_attempt_at=now + delay, tmp_path=None, percent=0)
        return "retrying"
    _update(conv["id"], status="failed", error=message, finished_at=now, next_attempt_at=None, tmp_path=None)
    db.add_history("conversion.failed", f"Conversion failed: {message}", work_id=conv["work_id"], asset_id=conv["source_asset_id"],
                   data={"conversion_id": conv["id"], "error": message, "attempts": attempts})
    return "failed"


def _stem(text: str) -> str:
    text = re.sub(r"[<>:\"|?*\x00-\x1f/\\]", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:150] or "Book"


def _publish(tmp: Path, directory: Path, stem: str, ext: str) -> Path:
    """Move the finished temp file to its final name WITHOUT ever replacing a file: a hard link
    fails if the name is taken (then the next free "name (2)" is tried), and the temp name is
    dropped afterwards. Filesystems without hard links fall back to a rename guarded by an
    existence check."""
    for i in range(1, 200):
        dest = directory / f"{stem}{'' if i == 1 else f' ({i})'}{ext}"
        if dest.exists():
            continue
        try:
            os.link(tmp, dest)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.EACCES):
                raise
            if dest.exists():
                continue
            os.rename(tmp, dest)
            return dest
        os.unlink(tmp)
        return dest
    raise ConversionError("Could not find a free file name for the converted file", retryable=False)


def _book_meta(work_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,)) or {}
    authors = [r["name"] for r in db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord", (work_id,))]
    series = db.one("SELECT s.name,sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=? ORDER BY s.name LIMIT 1", (work_id,))
    asin = db.one(
        "SELECT value FROM identifiers WHERE namespace IN ('asin','audible') AND ((entity_type='work' AND entity_id=?) OR "
        "(entity_type='edition' AND entity_id IN (SELECT id FROM editions WHERE work_id=?))) ORDER BY verified DESC LIMIT 1", (work_id, work_id))
    narrators = db.json_load(asset.get("narrator_json"), []) or [r["name"] for r in db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='narrator' ORDER BY wc.ord", (work_id,))]
    year = re.search(r"\d{4}", str(w.get("first_publish_date") or ""))
    desc = re.sub(r"<[^>]+>", "", str(w.get("description") or "")).strip()
    return {"title": w.get("title") or "Untitled", "author": ", ".join(authors) or None, "narrator": ", ".join(n for n in narrators if n) or None,
            "series": series["name"] if series else None, "series_position": series["position"] if series else None,
            "asin": asin["value"] if asin else None, "year": year.group() if year else None, "description": desc[:1000] or None,
            "authors": authors}


def _process(conv: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Run one claimed conversion to a terminal or retry state. Returns the result counter name."""
    kind = conv["kind"]
    created: list[Path] = []      # folders this run made inside the managed root; pruned again if it fails
    try:
        asset = db.one("SELECT * FROM media_assets WHERE id=?", (conv["source_asset_id"],))
        if not asset:
            raise ConversionError("The source asset no longer exists", retryable=False)
        info = assess(asset, cfg=cfg)
        if info["state"] == "blocked":
            raise ConversionError(info["reason"], retryable=False,
                                  skip=info["code"] in ("drm", "unsupported", "already_preferred", "has_preferred", "disabled"))
        if info["state"] == "converted":
            raise ConversionError("Already converted and the source has not changed", retryable=False, skip=True)
        root = info["root"]
        root_path = Path(root["path"])
        out_dir = Path(info["out_dir"])
        meta = _book_meta(asset["work_id"], asset)
        if info["cross_root"]:
            stem = destination_names(asset["work_id"], kind)[1]      # <Title>/<Title>.<ext> in the managed root
        else:
            stem = _stem(meta["title"] if kind == "audio" else Path(info["files"][0]["path"]).stem)
        tmp = out_dir / f".{stem[:80]}.{conv['id'][:8]}.partial"
        if path_guard.resolve_safe(root_path, tmp) is None:
            raise ConversionError("Refusing: the output path escapes the library root", retryable=False)
        _remove_partial(str(tmp))
        _update(conv["id"], tmp_path=str(tmp), source_signature=info["signature"])
        should_cancel = lambda: bool((_row(conv["id"]) or {}).get("cancel_requested"))   # noqa: E731

        if kind == "audio":
            plan = convert_audio.plan_audio(info["files"], mono_kbps=cfg["audio_bitrate_mono_kbps"], stereo_kbps=cfg["audio_bitrate_stereo_kbps"])
            _check_space(out_dir, plan.estimated_bytes, cfg)
            created += _ensure_dir(root_path, out_dir, strict=info["cross_root"])
            _update(conv["id"], estimated_bytes=plan.estimated_bytes)
            scratch = tempfile.mkdtemp(prefix="alx-cover-")
            cover = _cover_for(asset["work_id"], plan, Path(scratch))
            last = [0.0]

            def progress(pct: float) -> None:
                if time.monotonic() - last[0] >= 2.0:
                    last[0] = time.monotonic()
                    _heartbeat(kind, conv["id"], pct)

            try:
                convert_audio.run_ffmpeg(plan, meta, str(tmp), cover=cover, on_progress=progress, should_cancel=should_cancel)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            report = convert_audio.verify_output(str(tmp), plan)
            report.update({"order_basis": plan.order_basis, "mode": "stream copy" if plan.copy else f"aac {plan.bitrate_kbps}k", "sources": len(plan.sources)})
            derived_by, ext = convert_audio.tool_signature(plan), ".m4b"
            duration = int(report["duration_seconds"])
        else:
            src = Path(info["files"][0]["path"])
            _check_space(out_dir, int(src.stat().st_size * 3) + 5_000_000, cfg)
            created += _ensure_dir(root_path, out_dir, strict=info["cross_root"])
            _heartbeat(kind, conv["id"], 5)
            report = convert_ebook.to_epub(src, tmp, title=meta["title"], authors=meta["authors"])
            derived_by, ext, duration = f"{convert_ebook.TOOL_VERSION} {report['tool']}; {report['mode']}", ".epub", None
        if should_cancel():
            raise Cancelled()
        final = _publish(tmp, out_dir, stem, ext)
        try:
            _register(conv, asset, info, final, derived_by, duration, report)
        except Exception:
            try:
                final.unlink()
            except OSError:
                pass
            raise
        return "done"
    except ConversionError as exc:
        outcome = _record_failure(conv, exc, cfg)       # removes the partial file first, so its folder can go
        _prune_dirs(created)
        return outcome
    except Exception:
        _remove_partial(((_row(conv["id"]) or {}).get("tmp_path")))
        _prune_dirs(created)
        raise


def _ensure_dir(root_path: Path, target: Path, *, strict: bool) -> list[Path]:
    """Create ``target`` (and missing parents) INSIDE the managed root, one level at a time, and return
    the folders made. ``strict`` (a destination we lay out ourselves) refuses a symlink or a plain file
    in any component below the root; every level is re-checked with ``resolve_safe``."""
    try:
        rel = target.relative_to(root_path)
    except ValueError:
        raise ConversionError("Refusing: the output folder is outside the library root", retryable=False)
    made: list[Path] = []
    cur = root_path
    for part in rel.parts:
        cur = cur / part
        if strict and cur.is_symlink():
            raise ConversionError(f"Refusing: {part!r} in the destination folder is a symbolic link", retryable=False)
        if path_guard.resolve_safe(root_path, cur) is None:
            raise ConversionError("Refusing: the output folder escapes the library root", retryable=False)
        if cur.exists():
            if not cur.is_dir():
                raise ConversionError(f"Refusing: {part!r} in the destination folder is a file", retryable=False)
            continue
        cur.mkdir()
        made.append(cur)
    return made


def _prune_dirs(made: list[Path]) -> None:
    """Remove folders a failed run created, deepest first, only while empty."""
    for d in reversed(made):
        try:
            d.rmdir()
        except OSError:
            break


def _check_space(directory: Path, needed: int, cfg: dict[str, Any]) -> None:
    free = free_bytes(str(directory))
    margin = cfg["disk_margin_mb"] * 1024 * 1024
    if free - needed < margin:
        raise ConversionError(
            f"Not enough free disk space: the conversion needs about {needed // 1048576} MB plus a {cfg['disk_margin_mb']} MB margin, "
            f"{free // 1048576} MB is free where the converted file is saved")


def _cover_for(work_id: str, plan: convert_audio.AudioPlan, workdir: Path) -> str | None:
    hit = covers.cached(work_id)
    if hit and Path(hit["path"]).is_file():
        return str(hit["path"])
    for src in plan.sources:
        if src.has_cover:
            got = convert_audio.extract_cover(src.path, workdir / "cover.jpg")
            if got:
                return got
    return None


def _register(conv: dict[str, Any], asset: dict[str, Any], info: dict[str, Any], final: Path, derived_by: str,
              duration: int | None, report: dict[str, Any]) -> None:
    """Create (or refresh) the derived asset and its file row, close the conversion, in one transaction."""
    root = info["root"]
    root_path = Path(root["path"])
    cross_root = bool(info.get("cross_root"))
    detected = scanner.inspect_file(final, root=root_path)
    size = final.stat().st_size
    sha = scanner.full_sha256(final) if size <= app_settings.scan_hash_full_max_mb * 1024 * 1024 else None
    pc = scanner.probe_columns(detected)
    fmt = KINDS[conv["kind"]]["target"]
    now = db.now()
    stale_paths: list[str] = []
    with db.tx() as conn:
        real = conn.execute("SELECT id FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id IS NULL",
                            (asset["work_id"], asset["kind"], fmt)).fetchone()
        if real:
            raise ConversionError(f"A real {fmt.upper()} asset appeared while converting; the converted copy was discarded", retryable=False)
        mine = conn.execute("SELECT * FROM media_assets WHERE work_id=? AND kind=? AND format=? AND derived_from_asset_id=?",
                            (asset["work_id"], asset["kind"], fmt, asset["id"])).fetchone()
        if mine:
            asset_id = mine["id"]
            stale_paths = [r["path"] for r in conn.execute("SELECT path FROM media_files WHERE asset_id=?", (asset_id,)).fetchall()]
            conn.execute("DELETE FROM media_files WHERE asset_id=?", (asset_id,))
            conn.execute("UPDATE media_assets SET status='owned',derived_by=?,derived_at=?,duration_seconds=?,updated_at=? WHERE id=?",
                         (derived_by, now, duration or detected.get("duration_seconds"), now, asset_id))
        else:
            asset_id = db.uid()
            conn.execute(
                """INSERT INTO media_assets(id,work_id,edition_id,kind,format,status,monitored,has_acquisition_intent,profile_id,root_folder_id,
                                            narrator_json,duration_seconds,abridged,quality_json,upgrade_policy,added_at,updated_at,
                                            derived_from_asset_id,derived_by,derived_at)
                   VALUES (?,?,?,?,?,'owned',0,0,?,?,?,?,?, '{}','none',?,?,?,?,?)""",
                (asset_id, asset["work_id"], asset.get("edition_id"), asset["kind"], fmt, asset.get("profile_id"), root["id"],
                 asset.get("narrator_json") or "[]", duration or detected.get("duration_seconds"), asset.get("abridged"), now, now,
                 asset["id"], derived_by, now))
        conn.execute(
            """INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,sha256,fingerprint,container,codec,sample_rate,channels,
                                       chapter_count,has_embedded_cover,bitrate,duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,0,?,?)""",
            (db.uid(), asset_id, str(final), str(final.relative_to(root_path)), None, size, final.stat().st_mtime, sha, detected.get("fingerprint"),
             final.suffix.lower().lstrip("."), pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"],
             detected.get("bitrate"), detected.get("duration_seconds"), db.json_dump(detected), now, now))
        conn.execute(
            "UPDATE conversions SET status='done',percent=100,finished_at=?,error=NULL,output_asset_id=?,output_path=?,tmp_path=NULL,"
            "result_json=?,next_attempt_at=NULL,source_signature=? WHERE id=?",
            (now, asset_id, str(final), db.json_dump({**report, "size_bytes": size}), info["signature"], conv["id"]))
    derived.remove_generated_files([p for p in stale_paths if p != str(final)])
    db.add_history("conversion.done", f"Converted to {fmt.upper()}: {final.name} (original kept"
                   + (f"; saved in {root['name']} as {final.relative_to(root_path)}" if cross_root else "") + ")",
                   work_id=asset["work_id"], asset_id=asset_id,
                   data={"conversion_id": conv["id"], "source_asset_id": asset["id"], "file": final.name, "size_bytes": size,
                         "destination_root_id": root["id"], "destination": str(final.relative_to(root_path)), "cross_root": cross_root})
