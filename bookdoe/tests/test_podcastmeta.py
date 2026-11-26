"""Podcast episode ID3 parser (#509): synthetic ID3v2 headers, no audio, no network."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.services.podcastmeta import EXTRAS, EpisodeMeta, PodcastTagError, parse_episode

MPEG_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 32


def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _text(fid: str, value: str) -> bytes:
    return _frame(fid, b"\x03" + value.encode("utf-8"))


def _lang(fid: str, value: str, desc: str = "") -> bytes:
    return _frame(fid, b"\x03eng" + desc.encode() + b"\x00" + value.encode("utf-8"))


def _frame(fid: str, body: bytes) -> bytes:
    return fid.encode() + _synchsafe(len(body)) + b"\x00\x00" + body


def _tag(*frames: bytes) -> bytes:
    body = b"".join(frames)
    return b"ID3\x04\x00\x00" + _synchsafe(len(body)) + body


def _episode(root: Path, rel: str, *frames: bytes, raw: bytes | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else (_tag(*frames) if frames else b"") + MPEG_SYNC)
    return path


def test_full_tags_win_over_path(tmp_path):
    p = _episode(
        tmp_path, "Folder Show/Season 09/099 - Path Title.mp3",
        _text("TPE1", "Tag Show"), _text("TALB", "Season 2"), _text("TPOS", "2"), _text("TRCK", "3/13"),
        _text("TIT2", "Tag Title"), _text("TDRC", "2024-05-06"), _lang("COMM", "About it"),
        _frame("APIC", b"\x00image/png\x00\x03\x00" + b"\x89PNG" * 10),
    )
    assert parse_episode(p) == EpisodeMeta(
        show="Tag Show", season=2, position=3, title="Tag Title",
        date=date(2024, 5, 6), description="About it", has_cover=True,
    )


def test_no_tags_falls_back_to_path(tmp_path):
    p = _episode(tmp_path, "My Show/Season 03/2023-11-02 - Pilot Episode.mp3")
    assert parse_episode(p) == EpisodeMeta(
        show="My Show", season=3, position=None, title="Pilot Episode",
        date=date(2023, 11, 2), description="", has_cover=False,
    )


def test_numbered_filename_gives_position(tmp_path):
    p = _episode(tmp_path, "My Show/Season 01/007 - Seven.mp3")
    meta = parse_episode(p)
    assert (meta.position, meta.title, meta.date) == (7, "Seven", None)


def test_partial_tags_trck_fraction_and_missing_tpos(tmp_path):
    p = _episode(tmp_path, "My Show/Season 04/012 - Path Title.mp3", _text("TRCK", "3/13"), _text("TPE2", "Album Artist"))
    meta = parse_episode(p)
    assert meta.position == 3
    assert meta.season == 4  # no TPOS or TALB: the Season folder
    assert meta.show == "Album Artist"
    assert meta.title == "Path Title"


def test_comm_wins_over_uslt(tmp_path):
    p = _episode(tmp_path, "S/Season 01/001 - T.mp3", _lang("USLT", "lyrics text"), _lang("COMM", "comment text"))
    assert parse_episode(p).description == "comment text"


def test_uslt_used_when_no_comm_and_empty_when_neither(tmp_path):
    only_uslt = _episode(tmp_path, "S/Season 01/001 - T.mp3", _lang("USLT", "lyrics text"))
    assert parse_episode(only_uslt).description == "lyrics text"
    neither = _episode(tmp_path, "S/Season 01/002 - U.mp3", _text("TIT2", "U"))
    meta = parse_episode(neither)
    assert isinstance(meta, EpisodeMeta) and meta.description == ""


def test_extras_folder_and_talb(tmp_path):
    folder = parse_episode(_episode(tmp_path, "S/Extras/2022-01-01 - Bonus.mp3"))
    assert folder.season is EXTRAS
    tagged = parse_episode(_episode(tmp_path, "S/Season 01/001 - X.mp3", _text("TALB", "Extras")))
    assert tagged.season is EXTRAS


def test_malformed_date_falls_back_to_path_then_none(tmp_path):
    with_name = _episode(tmp_path, "S/Season 01/2020-02-03 - T.mp3", _text("TDRC", "2020-13-45"))
    assert parse_episode(with_name).date == date(2020, 2, 3)
    without = _episode(tmp_path, "S/Season 01/005 - T.mp3", _text("TDRC", "last tuesday"))
    assert parse_episode(without).date is None
    year_only = _episode(tmp_path, "S/Season 01/006 - T.mp3", _text("TDRC", "2021"))
    assert parse_episode(year_only).date is None


def test_utf16_title_and_id3v23_frames(tmp_path):
    title = "Café".encode("utf-16")  # BOM + little endian
    body = b"\x01" + title + b"\x00\x00"
    frame = b"TIT2" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    raw = b"ID3\x03\x00\x00" + _synchsafe(len(frame)) + frame + MPEG_SYNC
    meta = parse_episode(_episode(tmp_path, "S/Season 01/001 - T.mp3", raw=raw))
    assert meta.title == "Café"


def test_truncated_tag_does_not_crash(tmp_path):
    full = _tag(_text("TIT2", "Hello"), _text("TPE1", "Show"))
    p = _episode(tmp_path, "S/Season 01/001 - T.mp3", raw=full[:-6])
    assert isinstance(parse_episode(p), EpisodeMeta)


def test_errors_are_typed_not_raised(tmp_path):
    missing = parse_episode(tmp_path / "S/Season 01/001 - Gone.mp3")
    assert missing == PodcastTagError(str(tmp_path / "S/Season 01/001 - Gone.mp3"), "unreadable")
    wrong_ext = _episode(tmp_path, "S/Season 01/001 - T.m4a", raw=MPEG_SYNC)
    assert parse_episode(wrong_ext).reason == "not_mp3"
    not_audio = _episode(tmp_path, "S/Season 01/002 - T.mp3", raw=b"just some text, not audio")
    assert parse_episode(not_audio).reason == "not_mp3"
    empty = _episode(tmp_path, "S/Season 01/003 - T.mp3", raw=b"")
    assert parse_episode(empty).reason == "not_mp3"
    (tmp_path / "dir.mp3").mkdir()
    assert parse_episode(tmp_path / "dir.mp3").reason == "unreadable"


def test_path_with_embedded_nul_is_unreadable_not_raised(tmp_path):
    bad = str(tmp_path / "S/Season 01/001 - T\x00.mp3")
    assert parse_episode(bad) == PodcastTagError(bad, "unreadable")


def test_v24_tag_level_unsync_flag_is_undone_for_frames_without_the_frame_flag(tmp_path):
    body = b"\x00\xffx"  # latin-1 text "ÿx"
    unsynced = b"\x00\xff\x00x"
    frame = b"TIT2" + _synchsafe(len(unsynced)) + b"\x00\x00" + unsynced  # no per-frame 0x0002 flag
    raw = b"ID3\x04\x00\x80" + _synchsafe(len(frame)) + frame + MPEG_SYNC
    assert body.replace(b"\xff", b"\xff\x00") == unsynced
    assert parse_episode(_episode(tmp_path, "S/Season 01/001 - T.mp3", raw=raw)).title == "ÿx"


def test_v24_tag_flag_and_frame_flag_together_decode_once(tmp_path):
    # UTF-16BE U+FF00 is the bytes ff 00; unsynchronised once it is ff 00 00. A second pass would eat the real 0x00.
    unsynced = b"\x02\xff\x00\x00"
    frame = b"TIT2" + _synchsafe(len(unsynced)) + b"\x00\x02" + unsynced
    raw = b"ID3\x04\x00\x80" + _synchsafe(len(frame)) + frame + MPEG_SYNC
    assert parse_episode(_episode(tmp_path, "S/Season 01/001 - T.mp3", raw=raw)).title == "\uff00"


def test_v23_date_comes_from_tyer_and_tdat(tmp_path):
    def v23(*pairs):
        frames = b""
        for fid, val in pairs:
            body = b"\x00" + val.encode("latin-1")
            frames += fid.encode() + len(body).to_bytes(4, "big") + b"\x00\x00" + body
        return b"ID3\x03\x00\x00" + _synchsafe(len(frames)) + frames + MPEG_SYNC

    full = _episode(tmp_path, "S/Season 01/001 - T.mp3", raw=v23(("TYER", "2019"), ("TDAT", "0703")))
    assert parse_episode(full).date == date(2019, 3, 7)  # TDAT is DDMM
    year_only = _episode(tmp_path, "S/Season 01/2020-02-03 - T.mp3", raw=v23(("TYER", "2019")))
    assert parse_episode(year_only).date == date(2020, 2, 3)  # a year alone falls back to the filename date
    tdrc_wins = _episode(tmp_path, "S/Season 01/003 - T.mp3", _text("TDRC", "2024-05-06"))
    assert parse_episode(tdrc_wins).date == date(2024, 5, 6)
