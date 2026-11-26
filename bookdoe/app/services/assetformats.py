"""Asset format labels derived from an asset's present files.

``media_assets.format`` holds one value, but a folder can hold several (an EPUB and a MOBI of the
same book, an MP3 set next to an M4B). Payloads therefore also carry ``formats`` (every distinct
format present, best first) and ``primary_format`` (the best one by the default quality profile's
``preferred_formats``). The stored column is never rewritten: it is part of the
UNIQUE(work, kind, format) slot that conversion and supersede depend on.
"""
from __future__ import annotations

from typing import Any

from .. import db

DEFAULT_PREFERENCE = {
    "ebook": ["epub", "kepub", "azw3", "mobi", "azw", "pdf"],
    "audiobook": ["m4b", "m4a", "mp3", "flac", "opus", "ogg"],
}


def preference(kind: str) -> list[str]:
    """Preferred formats of the default profile for a kind, falling back to built-in order."""
    row = db.one("SELECT config_json FROM profiles WHERE media_kind=? AND is_default=1 ORDER BY id LIMIT 1", (kind,))
    order = list((db.json_load(row["config_json"], {}) if row else {}).get("preferred_formats") or [])
    # Formats the profile does not list still rank, after the listed ones.
    return order + [f for f in DEFAULT_PREFERENCE[kind] if f not in order]


def file_format(path: str) -> str:
    """Lower-case format of one file ('epub', 'kepub', 'mp3'...), from its name."""
    low = (path or "").lower()
    if low.endswith(".kepub.epub"):
        return "kepub"
    return low.rsplit(".", 1)[-1] if "." in low.rsplit("/", 1)[-1] else ""


READABLE_EBOOK = ("epub", "kepub", "pdf")
PLAYABLE_AUDIO = ("m4b", "m4a", "mp3", "flac", "ogg", "opus", "aac", "wav")


def sees_paths(user: dict[str, Any] | None) -> bool:
    """Only a signed-in administrator in the Management lane may see filesystem paths.
    Adults, kids, device tokens and the trusted-browser cookie never do."""
    from .. import auth

    return bool(user and user.get("is_admin") and not auth.is_kids_scope(user) and not user.get("_auth_method") == "device")


def scrub_for_viewer(assets: list[dict[str, Any]], user: dict[str, Any] | None) -> None:
    """Remove filesystem and acquisition internals from asset dicts for anyone who may not see
    them. Files keep id, size, duration, part number, a display ``name`` ('Track 3', never the
    raw file name), ``format`` and ``readable``. Call after ``annotate``."""
    if sees_paths(user):
        return
    for a in assets:
        for key in ("root_folder_id", "profile_id", "download_path"):
            a.pop(key, None)
        for i, f in enumerate(a.get("files") or [], 1):
            n = f.get("part_number") or i
            f["name"] = f"Track {n}" if a.get("kind") == "audiobook" else f"{(f.get('format') or 'file').upper()} file"
            for key in ("path", "relative_path", "container_path"):
                f.pop(key, None)


def annotate(assets: list[dict[str, Any]], order: dict[str, list[str]] | None = None) -> None:
    """Add formats, primary_format, file_count, total_size_bytes and total_duration_seconds
    (from present files) to each asset dict of a payload. Additive: nothing is removed."""
    order = order or {}
    for a in assets:
        kind = a.get("kind") if a.get("kind") in DEFAULT_PREFERENCE else "ebook"
        for f in a.get("files") or []:
            f["format"] = (f.get("path") or "").lower().rsplit(".", 1)[-1] if "." in (f.get("path") or "").rsplit("/", 1)[-1] else ""
            f["readable"] = f["format"] in (READABLE_EBOOK if a.get("kind") == "ebook" else PLAYABLE_AUDIO)
        present = [f for f in a.get("files") or [] if not f.get("missing")]
        found = {file_format(f["path"]) if f.get("path") else f.get("format", "") for f in present} - {""}
        if not found and a.get("format"):
            found = {str(a["format"]).lower()}
        if len(found) > 1:
            # Only a tie between formats needs the profile; a one-format asset costs no query.
            if kind not in order:
                order[kind] = preference(kind)
            rank = order[kind]
            formats = sorted(found, key=lambda x: (rank.index(x) if x in rank else len(rank), x))
        else:
            formats = sorted(found)
        a["formats"] = formats
        a["primary_format"] = formats[0] if formats else (str(a["format"]).lower() if a.get("format") else None)
        a["file_count"] = len(present)
        a["total_size_bytes"] = sum(int(f.get("size_bytes") or 0) for f in present)
        dur = sum(float(f.get("duration_seconds") or 0) for f in present)
        a["total_duration_seconds"] = dur or None
