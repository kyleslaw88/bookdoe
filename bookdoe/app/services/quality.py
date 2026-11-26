"""Owned-media quality evaluation (P5-26, Part XXIX).

Compares each owned asset with its quality profile and records 'satisfactory',
'upgrade_desired' or 'unevaluated' with reasons. It only reads stored columns (it never
runs ffprobe or opens a file) and its one write is media_assets.quality_json['evaluation'].
It takes no action: nothing here searches, grabs, replaces or deletes.
"""
from __future__ import annotations

from typing import Any

from .. import db
from . import releases

SATISFACTORY = "satisfactory"
UPGRADE_DESIRED = "upgrade_desired"
UNEVALUATED = "unevaluated"

# Optional profile keys added by P5-26 and their defaults; an old profile without them
# behaves exactly as before (format rank and abridged only).
PROFILE_QUALITY_DEFAULTS: dict[str, Any] = {
    "chapters_required": False,
    "embedded_cover_preferred": False,
    "min_bitrate_kbps": None,
    "preferred_codecs": [],
    "require_drm_free": False,
    "kepub_derivative_ok": True,
    "pdf_undesirable_if_epub": False,
}

EPUB_FORMATS = ("epub", "kepub")


def _result(state: str, reasons: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"state": state, "reasons": reasons, **(extra or {})}


def _is_kepub(asset: dict[str, Any], files: list[dict[str, Any]]) -> bool:
    if (asset.get("format") or "").lower() == "kepub":
        return True
    return bool(files) and all(str(f.get("path") or "").lower().endswith(".kepub.epub") for f in files)


def _format_reasons(fmt: str, kind: str, cfg: dict[str, Any]) -> list[str]:
    allowed, preferred = releases.profile_formats(cfg, kind)
    reasons = []
    if fmt not in allowed:
        reasons.append(f"Format {fmt} is not allowed by the profile")
    cutoff = cfg.get("cutoff")
    if cutoff and releases.format_rank(fmt, preferred) > releases.format_rank(cutoff, preferred):
        reasons.append(f"Format {fmt} is below the profile cutoff {cutoff}")
    return reasons


def _audiobook(asset: dict[str, Any], files: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    fmt = (asset.get("format") or "").lower()
    if not fmt:
        return _result(UNEVALUATED, ["Asset format is not recorded"])
    # has_embedded_cover is only set by a successful probe, so NULL means "never probed".
    if any(f.get("has_embedded_cover") is None for f in files):
        return _result(UNEVALUATED, ["Stream data has not been probed"])
    missing = []
    if cfg["min_bitrate_kbps"] is not None and any(not f.get("bitrate") for f in files):
        missing.append("bitrate")
    if cfg["preferred_codecs"] and any(not f.get("codec") for f in files):
        missing.append("codec")
    if missing:
        return _result(UNEVALUATED, [f"Stored {name} is missing" for name in missing])

    reasons = _format_reasons(fmt, "audiobook", cfg)
    abridged = None if asset.get("abridged") is None else bool(asset["abridged"])
    if releases.abridged_rejected(cfg, abridged):
        reasons.append("Abridged audiobook; the profile rejects abridged")
    if cfg["chapters_required"] and not sum(f.get("chapter_count") or 0 for f in files):
        reasons.append("No chapters; the profile requires them")
    if cfg["embedded_cover_preferred"] and not any(f.get("has_embedded_cover") for f in files):
        reasons.append("No embedded cover; the profile prefers one")
    if cfg["min_bitrate_kbps"] is not None:
        floor = float(cfg["min_bitrate_kbps"]) * 1000
        if any(f["bitrate"] < floor for f in files):
            reasons.append(f"Bitrate is below the profile minimum of {cfg['min_bitrate_kbps']} kbps")
    codecs = [c.lower() for c in cfg["preferred_codecs"]]
    if codecs:
        bad = sorted({f["codec"] for f in files if f["codec"].lower() not in codecs})
        if bad:
            reasons.append(f"Codec {', '.join(bad)} is not in the preferred codecs")
    return _result(UPGRADE_DESIRED if reasons else SATISFACTORY, reasons)


def _ebook(asset: dict[str, Any], files: list[dict[str, Any]], cfg: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
    fmt = (asset.get("format") or "").lower()
    if not fmt:
        return _result(UNEVALUATED, ["Asset format is not recorded"])
    drm_values = [(f.get("embedded") or {}).get("drm") for f in files]
    drm = True if any(v is True for v in drm_values) else False if all(v is False for v in drm_values) else None
    if cfg["require_drm_free"] and drm is None:
        return _result(UNEVALUATED, ["DRM state has not been detected"], {"drm": None})

    epub_owned = any(s.get("kind") == "ebook" and (s.get("format") or "").lower() in EPUB_FORMATS for s in siblings)
    kepub = fmt in EPUB_FORMATS and _is_kepub(asset, files)
    # A KEPUB is an EPUB with Kobo markup; unless the profile says otherwise it ranks as one.
    effective = "epub" if kepub and cfg["kepub_derivative_ok"] else "kepub" if kepub else fmt
    reasons = _format_reasons(effective, "ebook", cfg)
    if fmt == "pdf" and cfg["pdf_undesirable_if_epub"] and epub_owned:
        reasons.append("PDF undesirable when EPUB exists")
    if cfg["require_drm_free"] and drm:
        reasons.append("DRM-protected; the profile requires DRM-free")
    return _result(UPGRADE_DESIRED if reasons else SATISFACTORY, reasons, {"drm": drm})


def evaluate(asset: dict[str, Any], files: list[dict[str, Any]], profile: dict[str, Any] | None, siblings: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure evaluation of one asset: {state, reasons[, drm]}.

    ``files`` are the asset's present media_files rows (embedded_json already decoded
    into ``embedded``); ``siblings`` are the Work's other owned assets (kind, format).
    """
    if profile is None:
        return _result(UNEVALUATED, ["No quality profile applies"])
    if not files:
        return _result(UNEVALUATED, ["Asset has no present files"])
    cfg = {**PROFILE_QUALITY_DEFAULTS, **profile}
    if asset.get("kind") == "audiobook":
        return _audiobook(asset, files, cfg)
    return _ebook(asset, files, cfg, siblings)


def _profile_for(asset: dict[str, Any]) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM profiles WHERE id=?", (asset["profile_id"],)) if asset.get("profile_id") else None
    row = row or db.one("SELECT * FROM profiles WHERE media_kind=? AND is_default=1 ORDER BY id LIMIT 1", (asset["kind"],))
    return row


def evaluate_asset(asset_id: str) -> dict[str, Any] | None:
    """Evaluate one asset from stored data and persist it; None if the asset is unknown."""
    asset = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    if not asset:
        return None
    profile_row = _profile_for(asset)
    profile = db.json_load(profile_row["config_json"], {}) if profile_row else None
    if asset["status"] != "owned":
        outcome = _result(UNEVALUATED, ["Asset is not owned"])
    else:
        files = db.all("SELECT * FROM media_files WHERE asset_id=? AND missing=0 ORDER BY part_number,path", (asset_id,))
        for f in files:
            f["embedded"] = db.json_load(f.get("embedded_json"), {}) or {}
        siblings = db.all(
            """SELECT a.kind,a.format FROM media_assets a WHERE a.work_id=? AND a.id!=? AND a.status='owned' AND a.derived_from_asset_id IS NULL
               AND EXISTS (SELECT 1 FROM media_files f WHERE f.asset_id=a.id AND f.missing=0)""",
            (asset["work_id"], asset_id),
        )
        outcome = evaluate(asset, files, profile, siblings)
    outcome.update({"profile_id": profile_row["id"] if profile_row else None, "evaluated_at": db.now()})
    with db.tx() as conn:
        row = conn.execute("SELECT quality_json FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        if row:
            quality = db.json_load(row["quality_json"], {}) or {}
            quality["evaluation"] = outcome
            conn.execute("UPDATE media_assets SET quality_json=? WHERE id=?", (db.json_dump(quality), asset_id))
    return outcome


def stored_evaluation(asset_id: str) -> dict[str, Any] | None:
    """The last persisted evaluation, or None if the asset is unknown or was never evaluated."""
    row = db.one("SELECT quality_json FROM media_assets WHERE id=?", (asset_id,))
    if not row:
        return None
    return (db.json_load(row["quality_json"], {}) or {}).get("evaluation")


def evaluate_all(limit: int = 500) -> dict[str, int]:
    """Evaluate up to ``limit`` owned assets, never-evaluated and stalest first."""
    ids = [r["id"] for r in db.all(
        """SELECT id FROM media_assets WHERE status='owned' AND derived_from_asset_id IS NULL
           ORDER BY json_extract(quality_json,'$.evaluation.evaluated_at') ASC, id LIMIT ?""",
        (limit,),
    )]
    counts = {SATISFACTORY: 0, UPGRADE_DESIRED: 0, UNEVALUATED: 0}
    for asset_id in ids:
        outcome = evaluate_asset(asset_id)
        if outcome:
            counts[outcome["state"]] += 1
    return {"evaluated": len(ids), **counts}
