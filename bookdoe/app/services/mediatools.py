"""Media file tooling: ffmpeg/ffprobe availability and mutagen-based tag reading.

This module is the Phase-5 prerequisite gate for later tag-writing, cover-embedding,
merge and conversion tasks.  It is deliberately read-only -- no writes, no side effects.

The existing ``services/tagging.py`` handles the kids/user tag system (database-backed);
this module handles *file-level* media metadata via external binaries and the mutagen
library.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import db
from . import paths

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

# Cached result of _detect_capabilities(); set once at first call.
_capabilities: dict[str, Any] | None = None


def _run_version(cmd: list[str]) -> str | None:
    """Return the version string from *cmd --version* or None on failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        # --version typically writes to stderr; fall back to stdout.
        out = p.stderr or p.stdout
        if not out:
            return None
        # Grab the first token that looks like a version.
        for token in out.split():
            if token and token[0].isdigit():
                return token
        return out.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _detect_capabilities() -> dict[str, Any]:
    """Detect available tools and return a capabilities dict.

    Structure::

        {
            "ffmpeg": {"available": bool, "version": str|None},
            "ffprobe": {"available": bool, "version": str|None},
            "mutagen": {"available": bool, "version": str|None},
            "supports": {
                "retag": [list of supported extensions],
                "embed_cover": [list of supported extensions],
                "merge": bool,
                "convert": [(from_ext, to_ext), ...],
            },
        }
    """
    global _capabilities

    # --- ffmpeg / ffprobe ---
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")

    ffmpeg_info: dict[str, Any] = {
        "available": ffmpeg_path is not None,
        "version": _run_version(["ffmpeg", "-version"]) if ffmpeg_path else None,
    }
    ffprobe_info: dict[str, Any] = {
        "available": ffprobe_path is not None,
        "version": _run_version(["ffprobe", "-version"]) if ffprobe_path else None,
    }

    # --- mutagen ---
    mutagen_available = False
    mutagen_version: str | None = None
    try:
        import mutagen  # noqa: F811  # type: ignore[no-redef]

        mutagen_available = True
        # mutagen.version_string is a module-level attribute (not a submodule).
        mutagen_version = getattr(mutagen, "version_string", None) or getattr(
            mutagen, "__version__", None
        )
    except ImportError:
        pass

    # --- supported formats ---
    # Mutagen supports these natively (MP4/M4B/M4A, MP3/ID3, FLAC, Ogg).
    # EPUB editing uses stdlib zipfile + ElementTree (already in scanner.py).
    retag_exts = ["m4b", "m4a", "mp3", "flac", "ogg", "epub"]
    embed_cover_exts = ["m4b", "m4a", "mp3", "flac", "ogg"]

    # Merge and convert require ffmpeg.
    has_ffmpeg = ffmpeg_info["available"]
    merge_supported = has_ffmpeg
    convert_list: list[tuple[str, str]] = []
    if has_ffmpeg:
        # ffmpeg can convert between any audio formats it supports.
        # We list the common ones we care about.
        convert_list = [
            ("mp3", "m4a"),
            ("mp3", "flac"),
            ("flac", "mp3"),
            ("flac", "m4a"),
            ("ogg", "mp3"),
            ("ogg", "m4a"),
            ("m4a", "mp3"),
            ("m4a", "flac"),
        ]

    result = {
        "ffmpeg": ffmpeg_info,
        "ffprobe": ffprobe_info,
        "mutagen": {
            "available": mutagen_available,
            "version": mutagen_version,
        },
        "supports": {
            "retag": retag_exts,
            "embed_cover": embed_cover_exts,
            "merge": merge_supported,
            "convert": convert_list,
        },
    }

    # Log a startup warning (not an error) when a tool is missing.
    if not ffmpeg_info["available"]:
        log.warning(
            "ffmpeg is not available on PATH; merge/convert will be disabled."
        )
    if not ffprobe_info["available"]:
        log.warning(
            "ffprobe is not available on PATH; audio metadata via ffprobe will be disabled."
        )
    if not mutagen_available:
        log.warning(
            "mutagen is not installed; file-level tag reading will be disabled."
        )

    return result


def capabilities() -> dict[str, Any]:
    """Return the computed capabilities dict (computed once, cached)."""
    global _capabilities
    if _capabilities is None:
        _capabilities = _detect_capabilities()
    return _capabilities


def reset_capabilities() -> None:
    """Reset the cached capabilities (for testing)."""
    global _capabilities
    _capabilities = None


# ---------------------------------------------------------------------------
# Tag reading (read-only)
# ---------------------------------------------------------------------------

# Extensions that mutagen can handle.
_MUTAGEN_EXTS = frozenset(
    {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wma"}
)

# Mutagen tag-key -> normalised field name mapping.
_TAG_MAP: dict[str, str] = {
    "title": "title",
    "artist": "artist",
    "composer": "composer",
    "album": "album",
    "date": "date",
    "tracknumber": "track",
    "discnumber": "disc",
    "genre": "genre",
    "comment": "comment",
    "description": "description",
    "narrator": "narrator",
}

# MP4 (M4A/M4B) atom keys -> normalised field name mapping.
# MP4 uses different key names than ID3/Ogg/FLAC.
_MP4_ATOM_MAP: dict[str, str] = {
    "\xa9nam": "title",   # ©nam
    "\xa9ART": "artist",  # ©ART
    "\xa9wrt": "composer",  # ©wrt
    "\xa9alb": "album",   # ©alb
    "\xa9day": "date",    # ©day
    "trkn": "track",
    "disk": "disc",
    "\xa9gen": "genre",   # ©gen
    "cmt ": "comment",
    "\xa9cmt": "comment",  # ©cmt
    "\xa9des": "description",  # ©des
    "\xa9ART": "artist",
    "----:com.apple.iTunes:narrator": "narrator",
}


def _read_mp4_tags(audio: Any) -> dict[str, Any]:
    """Read tags from an MP4/M4A/M4B file, mapping MP4 atoms to normalised fields."""
    result: dict[str, Any] = {}
    for atom_key, field in _MP4_ATOM_MAP.items():
        values = audio.get(atom_key)
        if values is None:
            continue
        if isinstance(values, list):
            val = values[0] if values else None
        else:
            val = values
        if val and str(val).strip():
            result[field] = str(val).strip()
    return result


def _read_easy_tags(audio: Any) -> dict[str, Any]:
    """Read tags from a file opened with easy=True (e.g. MP3)."""
    result: dict[str, Any] = {}
    for mutagen_key, field in _TAG_MAP.items():
        values = audio.get(mutagen_key)
        if values is None:
            continue
        if isinstance(values, list):
            val = values[0] if values else None
        else:
            val = values
        if val and str(val).strip():
            result[field] = str(val).strip()
    return result


def _library_root_for(path: Path) -> Path | None:
    """The enabled library root that safely contains *path* (symlinks resolved), or None."""
    for r in db.all("SELECT path FROM root_folders WHERE enabled=1 ORDER BY LENGTH(path) DESC"):
        resolved = paths.resolve_safe(Path(r["path"]), path)
        if resolved is not None:
            return resolved
    return None


def read_tags(path: str | Path) -> dict[str, Any]:
    """Read metadata tags from *path* using mutagen (read-only).

    The file must lie inside an enabled library root (``root_folders``) once symlinks
    are resolved; anything else returns ``{}``.  The check goes through
    ``paths.resolve_safe`` (Part XLII) and needs no extra argument from the caller.

    Returns a normalised dict with keys: title, artist, composer, album, date,
    track, disc, genre, comment, narrator.  Only keys with non-empty values are
    included.  The file is opened read-only.

    .. note::  Called from tests today; production call sites will be added
       in P5-15a/b (tag writing) and P5-16 (cover embedding).
    """
    raw = Path(path)
    ext = raw.suffix.lower()

    if ext not in _MUTAGEN_EXTS:
        return {}

    resolved = _library_root_for(raw)
    if resolved is None:
        return {}

    try:
        import mutagen
    except ImportError:
        return {}

    try:
        # Use easy=True only for MP3; MP4 and other formats need full tag access
        # to map MP4 atoms (©nam, ©ART, etc.) to normalised fields.
        audio = mutagen.File(str(resolved), easy=ext == ".mp3")
    except (mutagen.MutagenError, OSError):
        # For MP3 files, fall back to EasyID3 in case the file has an ID3 tag
        # but no valid MPEG frame (e.g. a tag-only file).
        if ext == ".mp3":
            try:
                from mutagen.easyid3 import EasyID3
                audio = EasyID3(str(resolved))
            except (mutagen.MutagenError, OSError):
                return {}
        else:
            return {}

    if audio is None:
        return {}

    # MP4/M4A/M4B need special handling for atom keys.
    if ext in (".m4a", ".m4b"):
        return _read_mp4_tags(audio)

    return _read_easy_tags(audio)


# ---------------------------------------------------------------------------
# Tool requirement helper
# ---------------------------------------------------------------------------

class ToolMissingError(RuntimeError):
    """Raised when a required tool (ffmpeg/mutagen) is not available."""

    def __init__(self, tool: str) -> None:
        super().__init__(f"Required tool '{tool}' is not available. "
                         "Install it before proceeding.")


def require_tools(*tools: str) -> None:
    """Raise ToolMissingError if any required tool is missing.

    Args:
        *tools: Tool names to check (e.g. "ffmpeg", "mutagen").

    Raises:
        ToolMissingError: If any tool is not available.
    """
    caps = capabilities()
    for tool in tools:
        if tool == "ffmpeg":
            if not caps["ffmpeg"]["available"]:
                raise ToolMissingError("ffmpeg")
        elif tool == "ffprobe":
            if not caps["ffprobe"]["available"]:
                raise ToolMissingError("ffprobe")
        elif tool == "mutagen":
            if not caps["mutagen"]["available"]:
                raise ToolMissingError("mutagen")
        else:
            log.warning("Unknown tool '%s' in require_tools", tool)
