"""Podcast episode metadata: ID3v2 frames with a path-based fallback (parent #508).

Pure parsing: reads one file, returns a typed record or a typed error. It does not walk
the tree, touch the DB or write to disk. Expected layout: <Show>/<Season NN | Extras>/<NNN or
YYYY-MM-DD> - <Title>.mp3. Frames are read by hand; mutagen only measures the duration.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

import mutagen
from mutagen.mp3 import MP3

log = logging.getLogger(__name__)


class Extras(Enum):
    """Season marker for the Extras folder (an int season is a numbered one)."""

    EXTRAS = "extras"


EXTRAS = Extras.EXTRAS


@dataclass(frozen=True)
class EpisodeMeta:
    show: str | None
    season: int | Extras | None
    position: int | None
    title: str | None
    date: date | None
    description: str
    has_cover: bool


@dataclass(frozen=True)
class PodcastTagError:
    path: str
    reason: str  # "unreadable" | "not_mp3"


_FILENAME_RE = re.compile(r"^(?P<lead>\d{4}-\d{2}-\d{2}|\d{1,3}) - (?P<title>.+)\.mp3$", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^Season\s+(\d+)$", re.IGNORECASE)
_NUM_RE = re.compile(r"^\s*(\d+)")


def _season_from_name(name: str) -> int | Extras | None:
    """Parse 'Season N' / 'Extras' (folder name or TALB value)."""
    name = name.strip()
    if name.lower() == "extras":
        return EXTRAS
    m = _SEASON_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def _parse_date(value: str | None) -> date | None:
    """Strict YYYY-MM-DD (a longer timestamp keeps its date part); anything else is malformed."""
    if not value:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:$|T)", value.strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _v23_date(tyer: str, tdat: str) -> date | None:
    """ID3v2.3 date: TYER 'YYYY' plus TDAT 'DDMM'. A year alone is not a full date, so it yields None."""
    year, dm = tyer.strip(), tdat.strip()
    if not re.fullmatch(r"\d{4}", year) or not re.fullmatch(r"\d{4}", dm):
        return None
    return _parse_date(f"{year}-{dm[2:]}-{dm[:2]}")


def _leading_int(value: str | None) -> int | None:
    """'3/13' -> 3. Zero and non-numeric values are treated as missing."""
    m = _NUM_RE.match(value or "")
    n = int(m.group(1)) if m else 0
    return n or None


def _synchsafe(b: bytes) -> int:
    n = 0
    for x in b:
        n = (n << 7) | (x & 0x7F)
    return n


def _decode(enc: int, data: bytes) -> str:
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc, "latin-1")
    return data.decode(codec, errors="replace")


def _split_terminated(enc: int, data: bytes) -> tuple[bytes, bytes]:
    """Split at the first string terminator (two aligned zero bytes for UTF-16)."""
    if enc in (1, 2):
        for i in range(0, len(data) - 1, 2):
            if data[i : i + 2] == b"\x00\x00":
                return data[:i], data[i + 2 :]
        return data, b""
    i = data.find(b"\x00")
    return (data, b"") if i < 0 else (data[:i], data[i + 1 :])


def _text_frame(body: bytes) -> str:
    """First value of a text frame (later values are null-separated)."""
    if not body:
        return ""
    first, _ = _split_terminated(body[0], body[1:])
    return _decode(body[0], first).strip("\x00 \t\r\n﻿")


def _lang_text_frame(body: bytes) -> tuple[str, str]:
    """COMM/USLT body -> (short descriptor, text)."""
    if len(body) < 4:
        return "", ""
    enc = body[0]
    desc, text = _split_terminated(enc, body[4:])
    return _decode(enc, desc).strip("\x00﻿ "), _decode(enc, text).strip("\x00﻿ \r\n")


def _read_frames(tag: bytes, version: int, flags: int) -> list[tuple[str, bytes]]:
    """Frames of an ID3v2.3/2.4 tag body; stops quietly at padding or truncation."""
    if flags & 0x80 and version == 3:  # v2.3: the whole tag is unsynchronised, sizes included
        tag = tag.replace(b"\xff\x00", b"\xff")
    pos = 0
    if flags & 0x40 and len(tag) >= 4:  # extended header
        pos = _synchsafe(tag[:4]) if version == 4 else 4 + int.from_bytes(tag[:4], "big")
    frames: list[tuple[str, bytes]] = []
    while pos + 10 <= len(tag):
        fid = tag[pos : pos + 4]
        if not re.fullmatch(rb"[A-Z0-9]{4}", fid):
            break
        raw = tag[pos + 4 : pos + 8]
        size = _synchsafe(raw) if version == 4 else int.from_bytes(raw, "big")
        fflags = int.from_bytes(tag[pos + 8 : pos + 10], "big")
        body = tag[pos + 10 : pos + 10 + size]
        pos += 10 + size
        if len(body) < size:
            break
        if version == 4:
            skip = fflags & 0x000C  # compressed / encrypted
            if fflags & 0x0001:  # data length indicator
                body = body[4:]
            # v2.4 sizes are counted after unsynchronisation, so the tag-level flag is applied per frame body. One pass
            # either way: a writer that sets both flags must not be decoded twice.
            if fflags & 0x0002 or flags & 0x80:
                body = body.replace(b"\xff\x00", b"\xff")
        else:
            skip = fflags & 0x00C0
        if skip:
            continue
        frames.append((fid.decode("ascii"), body))
    return frames


def _read_tag(f) -> dict[str, list[bytes]] | None:
    """Frames by id, {} when the file has no usable ID3v2 tag, None when it is not an mp3."""
    head = f.read(10)
    if head[:3] == b"ID3" and len(head) == 10:
        version, flags = head[3], head[5]
        if version not in (3, 4) or any(b & 0x80 for b in head[6:10]):
            return {}
        frames: dict[str, list[bytes]] = {}
        for fid, body in _read_frames(f.read(_synchsafe(head[6:10])), version, flags):
            frames.setdefault(fid, []).append(body)
        return frames
    # No tag: still has to look like MPEG audio (frame sync), not an arbitrary file.
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return {}
    return None


def _description(frames: dict[str, list[bytes]]) -> str:
    """COMM wins over USLT; iTunes' technical comments (iTunNORM etc.) are not descriptions."""
    for fid in ("COMM", "USLT"):
        for body in frames.get(fid, []):
            desc, text = _lang_text_frame(body)
            if text and not desc.startswith("iTun"):
                return text
    return ""


def _tlen_seconds(path: Path) -> float | None:
    """TLEN (milliseconds, a text frame) as seconds; None when absent or not a positive number."""
    try:
        with path.open("rb") as f:
            frames = _read_tag(f)
    except (OSError, ValueError):
        return None
    for body in (frames or {}).get("TLEN", []):
        t = _text_frame(body)
        if t.isdigit() and int(t) > 0:
            return int(t) / 1000
    return None


def measure_duration(path: str | Path) -> float | None:
    """Real length in seconds, measured with mutagen; TLEN only when the file cannot be measured. None if neither."""
    p = Path(path)
    try:
        if p.suffix.lower() == ".mp3":
            length = MP3(p).info.length
        else:
            audio = mutagen.File(p)
            length = audio.info.length if audio is not None else None
        if length and length > 0:
            return float(length)
    except Exception:  # mutagen raises many types on bad data; an unmeasurable file is not an error here
        log.debug("podcast duration: %s not measurable", p, exc_info=True)
    return _tlen_seconds(p)


def parse_episode(path: str | Path) -> EpisodeMeta | PodcastTagError:
    """Read one episode file; tags win, the path fills any missing or malformed field."""
    p = Path(path)
    if p.suffix.lower() != ".mp3":
        return PodcastTagError(str(p), "not_mp3")
    try:
        with p.open("rb") as f:
            frames = _read_tag(f)
    except (OSError, ValueError):  # ValueError: embedded NUL byte in the path
        return PodcastTagError(str(p), "unreadable")
    if frames is None:
        return PodcastTagError(str(p), "not_mp3")

    def text(fid: str) -> str:
        return next((t for t in (_text_frame(b) for b in frames.get(fid, [])) if t), "")

    m = _FILENAME_RE.match(p.name)
    lead, name_title = (m.group("lead"), m.group("title").strip()) if m else ("", "")
    name_date = _parse_date(lead) if "-" in lead else None
    name_pos = int(lead) if lead and "-" not in lead else None

    folder_show = p.parent.parent.name or None
    folder_season = _season_from_name(p.parent.name)
    tag_album = _season_from_name(text("TALB"))
    tpos = _leading_int(text("TPOS"))
    if tag_album is EXTRAS:
        season = EXTRAS
    else:
        season = tpos or tag_album or folder_season

    return EpisodeMeta(
        show=text("TPE1") or text("TPE2") or folder_show,
        season=season,
        position=_leading_int(text("TRCK")) or name_pos,
        title=text("TIT2") or name_title or None,
        date=_parse_date(text("TDRC")) or _v23_date(text("TYER"), text("TDAT")) or name_date,
        description=_description(frames),
        has_cover=any(len(b) > 0 for b in frames.get("APIC", [])),
    )
