"""Audiobook conversion to one chaptered M4B (issue #265) and the job framework around it.

The ffmpeg tests build tiny sine-wave MP3s with ID3 titles/track numbers in a temporary
managed root, convert them for real and ffprobe the result; they skip when ffmpeg/ffprobe are
absent (CI installs them). The framework tests (single flight, retry/backoff, disk refusal,
cancel, cleanup, idempotent rerun, concurrency) drive the real queue with a stubbed encoder so
they do not depend on the tool.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

import pytest

from app import db
from app.services import convert_audio, conversion, covers, scheduler

import conv_fixtures as cf

needs_ffmpeg = pytest.mark.skipif(not cf.HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")

TITLE, AUTHOR = "Watch the Skies", "Eric Flint"


@pytest.fixture(autouse=True)
def no_background_kick(monkeypatch):
    """Tests run the queue themselves, in the calling thread."""
    monkeypatch.setattr(conversion, "kick", lambda kind: None)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stub_files(rt, n=2, title=TITLE, author=AUTHOR) -> list[Path]:
    out = []
    for i in range(1, n + 1):
        p = rt["path"] / author / title / f"{i:02d}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3 not really audio " * 20 + p.name.encode())
        out.append(p)
    return out


def real_book(tmp_path, n=3, title=TITLE, author=AUTHOR, mode="managed"):
    rt = cf.root(tmp_path, mode, "audiobook")
    files = [cf.make_mp3(rt["path"] / author / title / f"{i:02d}.mp3", seconds=1 + i % 2, freq=300 + 80 * i,
                         title=f"Chapter title {i}", track=i) for i in range(1, n + 1)]
    work_id, asset_id = cf.add_book(rt, title, author, "audiobook", files)
    return rt, work_id, asset_id, files


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def run_all(kind="audio"):
    return conversion.run_queue(kind)


# ---------------------------------------------------------------------------
# Planning (no ffmpeg needed)
# ---------------------------------------------------------------------------

def _src(path, **kw):
    base = dict(duration=10.0, codec="mp3", sample_rate=22050, channels=1)
    return convert_audio.Source(path=path, **{**base, **kw})


def test_order_prefers_part_numbers_then_track_tags_then_natural_filenames():
    a = [_src("/x/10.mp3", part_number=2), _src("/x/2.mp3", part_number=1)]
    ordered, basis = convert_audio.order_sources(a)
    assert [s.path for s in ordered] == ["/x/2.mp3", "/x/10.mp3"] and basis == "part_number"
    b = [_src("/x/a.mp3", track=2, disc=1), _src("/x/b.mp3", track=9, disc=0), _src("/x/c.mp3", track=1, disc=1)]
    ordered, basis = convert_audio.order_sources(b)
    assert [s.path for s in ordered] == ["/x/b.mp3", "/x/c.mp3", "/x/a.mp3"] and basis == "track_tag"
    c = [_src("/x/Part 10.mp3"), _src("/x/Part 2.mp3"), _src("/x/Part 1.mp3")]
    ordered, basis = convert_audio.order_sources(c)
    assert [s.path for s in ordered] == ["/x/Part 1.mp3", "/x/Part 2.mp3", "/x/Part 10.mp3"] and basis == "filename"
    # evidence that is partial or contradictory is not trusted piecemeal
    d = [_src("/x/2.mp3", part_number=1), _src("/x/10.mp3", part_number=None)]
    assert convert_audio.order_sources(d)[1] == "filename"
    e = [_src("/x/b.mp3", part_number=1), _src("/x/a.mp3", part_number=1)]
    assert convert_audio.order_sources(e)[1] == "filename"


def test_chapter_titles_use_id3_when_distinct_else_chapter_n():
    plan = lambda titles: convert_audio.AudioPlan([_src(f"/x/{i}.mp3", title=t) for i, t in enumerate(titles)], 30, 1, 22050, 64, False, False, 3, 1, "filename")
    assert convert_audio.chapter_titles(plan(["Prologue", "One", "Two"])) == ["Prologue", "One", "Two"]
    assert convert_audio.chapter_titles(plan(["The Book", "The Book", "The Book"])) == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert convert_audio.chapter_titles(plan(["Prologue", None, "Two"])) == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_ffmetadata_escapes_and_lists_one_chapter_per_file():
    plan = convert_audio.AudioPlan([_src("/x/1.mp3", duration=1.5, title="A=B; #1"), _src("/x/2.mp3", duration=2.0, title="Second")],
                                   3.5, 1, 22050, 64, False, False, 2, 1, "filename")
    text = convert_audio.build_ffmetadata(plan, {"title": "T; x", "author": "A", "narrator": "N", "asin": "B000", "series": "S", "series_position": "2"})
    assert text.startswith(";FFMETADATA1\n")
    assert "title=T\\; x" in text and "composer=N" in text and "grouping=S \\#2" in text and "comment=ASIN: B000" in text
    assert text.count("[CHAPTER]") == 2
    assert "START=0\nEND=1500\ntitle=A\\=B\\; \\#1" in text and "START=1500\nEND=3500\ntitle=Second" in text


def test_ffmpeg_is_invoked_with_an_argument_list_never_a_shell(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        returncode = 0
        stdout = iter(())

        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, kw=kw)

        def wait(self): return 0
        def poll(self): return 0
        def kill(self): pass

    monkeypatch.setattr(convert_audio.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(convert_audio, "tool", lambda name: f"/usr/bin/{name}")
    plan = convert_audio.AudioPlan([_src("/x/it's 1.mp3"), _src("/x/2.mp3")], 20, 1, 22050, 64, False, False, 2, 1, "filename")
    convert_audio.run_ffmpeg(plan, {"title": "T"}, str(tmp_path / "out.partial"), cover=None)
    cmd = seen["cmd"]
    assert isinstance(cmd, list) and all(isinstance(c, str) for c in cmd)
    assert not seen["kw"].get("shell")
    assert "-n" in cmd and "-nostdin" in cmd and cmd[-1].endswith("out.partial")
    assert cmd[cmd.index("-b:a") + 1] == "64k" and cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"       # not use_metadata_tags: it drops the cover atom
    if convert_audio.shutil.which("nice"):
        assert cmd[:3] == [convert_audio.shutil.which("nice"), "-n", "10"]


def test_a_missing_ffmpeg_is_a_clear_permanent_error(monkeypatch):
    monkeypatch.setattr(convert_audio.shutil, "which", lambda name: None)
    with pytest.raises(convert_audio.ConversionError, match="ffmpeg is not installed") as exc:
        convert_audio.tool("ffmpeg")
    assert exc.value.retryable is False


def test_ffmpeg_timeout_kills_the_process(tmp_path, monkeypatch):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(convert_audio, "tool", lambda name: str(fake))
    monkeypatch.setattr(convert_audio.shutil, "which", lambda name: None)      # no `nice` wrapper
    plan = convert_audio.AudioPlan([_src("/x/1.mp3")], 10, 1, 22050, 64, False, False, 1, 1, "filename")
    with pytest.raises(convert_audio.ConversionError, match="time limit"):
        convert_audio.run_ffmpeg(plan, {"title": "T"}, str(tmp_path / "o.partial"), cover=None, timeout=0.2)


# ---------------------------------------------------------------------------
# Real conversion
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_mp3s_become_one_chaptered_m4b_next_to_the_originals(tmp_path):
    rt, work_id, asset_id, files = real_book(tmp_path, n=3)
    covers._store(work_id, cf.JPEG, "test")
    before = {f: sha(f) for f in files}
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET narrator_json=? WHERE id=?", (json.dumps(["Narrator N"]), asset_id))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('work',?,'asin','B0TESTASIN',?,1,1)", (work_id, "test"))
    res = conversion.enqueue(asset_id, user={"username": "ada"})
    assert res["created"] and res["conversion"]["status"] == "queued"
    out = run_all()
    assert out["done"] == 1 and out["failed"] == 0

    conv = db.one("SELECT * FROM conversions")
    assert conv["status"] == "done" and conv["percent"] == 100 and conv["attempts"] == 1 and conv["error"] is None
    final = Path(conv["output_path"])
    assert final == rt["path"] / AUTHOR / TITLE / f"{TITLE}.m4b"
    assert not list((rt["path"] / AUTHOR / TITLE).glob(".*partial")), "no temp file is left behind"
    assert {f: sha(f) for f in files} == before, "the originals are byte-identical"
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=? AND missing=0", (asset_id,))["n"] == 3

    d = db.one("SELECT * FROM media_assets WHERE id=?", (conv["output_asset_id"],))
    assert (d["kind"], d["format"], d["status"], d["monitored"], d["has_acquisition_intent"]) == ("audiobook", "m4b", "owned", 0, 0)
    assert d["derived_from_asset_id"] == asset_id and "aac 32k" not in d["derived_by"] and "aac" in d["derived_by"] and d["derived_at"]
    assert d["root_folder_id"] == rt["id"] and d["work_id"] == work_id
    f = db.one("SELECT * FROM media_files WHERE asset_id=?", (d["id"],))
    assert f["path"] == str(final) and f["managed"] == 1 and f["adopted"] == 0 and f["chapter_count"] == 3 and f["size_bytes"] == final.stat().st_size

    info = probe(final)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1 and audio[0]["codec_name"] == "aac" and audio[0]["channels"] == 1
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Chapter title 1", "Chapter title 2", "Chapter title 3"]
    total = sum(float(probe(p)["format"]["duration"]) for p in files)
    assert abs(float(info["format"]["duration"]) - total) < 0.5
    tags = {k.lower(): v for k, v in info["format"]["tags"].items()}
    assert tags["title"] == TITLE and tags["artist"] == AUTHOR and tags["composer"] == "Narrator N" and "ASIN: B0TESTASIN" in tags["comment"]
    assert any(s["codec_type"] == "video" and s["disposition"]["attached_pic"] == 1 for s in info["streams"]), "cover embedded"

    ev = {h["event_type"] for h in db.all("SELECT event_type FROM history WHERE work_id=?", (work_id,))}
    assert {"conversion.queued", "conversion.done"} <= ev


@needs_ffmpeg
def test_parts_are_joined_in_track_order_not_file_name_order(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    # names sort the wrong way round; the tags and part numbers say otherwise
    files = [cf.make_mp3(rt["path"] / AUTHOR / TITLE / name, seconds=1, freq=f, title=t, track=n)
             for name, f, t, n in (("a-last.mp3", 300, "Third", 3), ("b-first.mp3", 500, "First", 1), ("c-mid.mp3", 700, "Second", 2))]
    _, asset_id = cf.add_book(rt, TITLE, AUTHOR, "audiobook", files)
    with db.tx() as conn:
        conn.execute("UPDATE media_files SET part_number=NULL WHERE asset_id=?", (asset_id,))
    conversion.enqueue(asset_id)
    run_all()
    out = Path(db.one("SELECT output_path FROM conversions")["output_path"])
    assert [c["tags"]["title"] for c in probe(out)["chapters"]] == ["First", "Second", "Third"]


@needs_ffmpeg
def test_mixed_sample_rates_and_channels_are_resampled_and_joined(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    files = [cf.make_mp3(rt["path"] / AUTHOR / TITLE / "01.mp3", seconds=1, rate=22050, channels=1, title="One", track=1),
             cf.make_mp3(rt["path"] / AUTHOR / TITLE / "02.mp3", seconds=2, rate=44100, channels=2, title="Two", track=2)]
    _, asset_id = cf.add_book(rt, TITLE, AUTHOR, "audiobook", files)
    plan = convert_audio.plan_audio([{"path": str(p)} for p in files])
    assert plan.use_filter and plan.channels == 1 and plan.sample_rate == 22050
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    info = probe(Path(db.one("SELECT output_path FROM conversions")["output_path"]))
    assert abs(float(info["format"]["duration"]) - 3.0) < 0.3 and len(info["chapters"]) == 2


@needs_ffmpeg
def test_aac_parts_are_stream_copied_not_re_encoded(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    files = [cf.make_audio(rt["path"] / AUTHOR / TITLE / f"{i}.m4a", seconds=1, title=f"P{i}", track=i, codec_args=["-c:a", "aac", "-b:a", "48k"])
             for i in (1, 2)]
    _, asset_id = cf.add_book(rt, TITLE, AUTHOR, "audiobook", files, fmt="m4a")
    assert convert_audio.plan_audio([{"path": str(p)} for p in files]).copy
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    d = db.one("SELECT derived_by FROM media_assets WHERE derived_from_asset_id IS NOT NULL")
    assert "stream copy" in d["derived_by"]


@needs_ffmpeg
def test_an_existing_file_is_never_overwritten(tmp_path):
    rt, work_id, asset_id, files = real_book(tmp_path, n=2)
    taken = rt["path"] / AUTHOR / TITLE / f"{TITLE}.m4b"
    taken.write_bytes(b"someone else's file")
    conversion.enqueue(asset_id)
    run_all()
    out = Path(db.one("SELECT output_path FROM conversions")["output_path"])
    assert out.name == f"{TITLE} (2).m4b" and taken.read_bytes() == b"someone else's file"


@needs_ffmpeg
def test_rerun_is_idempotent_and_a_changed_source_reconverts(tmp_path):
    rt, work_id, asset_id, files = real_book(tmp_path, n=2)
    conversion.enqueue(asset_id)
    run_all()
    first = db.one("SELECT * FROM conversions")
    # asking again changes nothing
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "converted"
    assert run_all()["processed"] == 0
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE derived_from_asset_id IS NOT NULL")["n"] == 1

    # the source changes: re-convert reuses the derived asset, replaces its file, keeps the original
    cf.make_mp3(files[1], seconds=3, freq=900, title="Chapter title 2", track=2)
    option = conversion.options_for_assets([db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))])[asset_id]
    assert option["state"] == "stale"
    again = conversion.enqueue(asset_id)
    assert again["created"]
    run_all()
    second = db.one("SELECT * FROM conversions WHERE id=?", (again["conversion"]["id"],))
    assert second["status"] == "done" and second["output_asset_id"] == first["output_asset_id"]
    assert not Path(first["output_path"]).exists() and Path(second["output_path"]).exists()
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=?", (first["output_asset_id"],))["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE derived_from_asset_id IS NOT NULL")["n"] == 1
    assert files[0].exists() and files[1].exists()


# ---------------------------------------------------------------------------
# Eligibility and safety
# ---------------------------------------------------------------------------

def test_adopted_and_observed_roots_without_a_managed_root_are_refused_with_a_clear_message(tmp_path):
    for mode in ("adopt", "observe"):
        rt = cf.root(tmp_path, mode, "audiobook")
        files = stub_files(rt, 2, title=f"Book {mode}")
        _, asset_id = cf.add_book(rt, f"Book {mode}", AUTHOR, "audiobook", files)
        with pytest.raises(conversion.ConversionRefused) as exc:
            conversion.enqueue(asset_id)
        assert exc.value.code == "no_managed_root" and "read-only" in str(exc.value) and "managed audiobook" in str(exc.value)
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
    assert all(p.read_bytes().startswith(b"ID3") for p in rt["path"].rglob("*.mp3"))


def test_a_path_that_escapes_its_root_through_a_symlink_is_refused(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "01.mp3").write_bytes(b"ID3" + b"\0" * 100)
    (rt["path"] / "link").symlink_to(outside)
    _, asset_id = cf.add_book(rt, TITLE, AUTHOR, "audiobook", [rt["path"] / "link" / "01.mp3"])
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "outside_roots"


def test_only_convertible_audio_gets_an_offer(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    m4b = rt["path"] / "a" / "Book.m4b"
    m4b.parent.mkdir(parents=True)
    m4b.write_bytes(b"x" * 50)
    _, already = cf.add_book(rt, "Already", AUTHOR, "audiobook", [m4b])
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(already)
    assert exc.value.code == "already_preferred"
    # a real asset of the target format on the same work also blocks (the slot is taken)
    files = stub_files(rt, 1, title="Has m4b")
    work_id, mp3 = cf.add_book(rt, "Has m4b", AUTHOR, "audiobook", files)
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',0,?,?)",
                     (db.uid(), work_id, db.now(), db.now()))
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(mp3)
    assert exc.value.code == "has_preferred"
    # a copy cannot be converted again
    d = cf.add_derived(already, "epub", rt["path"] / "a" / "x.epub")
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(d)
    assert exc.value.code == "derived"


def test_disabling_a_kind_turns_its_conversions_off(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    _, asset_id = cf.add_book(rt, TITLE, AUTHOR, "audiobook", stub_files(rt, 2))
    conversion.save_settings({"enabled_audio": False})
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(asset_id)
    assert exc.value.code == "disabled"
    conversion.save_settings({"enabled_audio": True})
    assert conversion.enqueue(asset_id)["created"]
    conversion.save_settings({"enabled_audio": False})
    assert conversion.run_queue("audio")["processed"] == 0          # queued work waits, nothing is claimed
    assert db.one("SELECT status FROM conversions")["status"] == "queued"


# ---------------------------------------------------------------------------
# Job framework (encoder stubbed)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_encoder(monkeypatch):
    """Replace plan/ffmpeg/verify with stubs that write a small file, so the queue can be driven fast."""
    calls = {"n": 0, "behaviour": None}

    def plan_audio(files, mono_kbps=64, stereo_kbps=96):
        srcs = [convert_audio.Source(path=f["path"], duration=10.0, codec="mp3", sample_rate=22050, channels=1) for f in files]
        return convert_audio.AudioPlan(srcs, 10.0 * len(srcs), 1, 22050, mono_kbps, False, False, len(srcs), 123_456, "filename")

    def run_ffmpeg(plan, meta, tmp_out, *, cover, on_progress=None, should_cancel=None, timeout=None):
        calls["n"] += 1
        Path(tmp_out).write_bytes(b"partial")
        if on_progress:
            on_progress(42.0)
        if calls["behaviour"]:
            calls["behaviour"](tmp_out, should_cancel)
        Path(tmp_out).write_bytes(b"M4B" * 100)

    monkeypatch.setattr(convert_audio, "plan_audio", plan_audio)
    monkeypatch.setattr(convert_audio, "run_ffmpeg", run_ffmpeg)
    monkeypatch.setattr(convert_audio, "verify_output", lambda path, plan: {"duration_seconds": plan.total_seconds, "chapters": plan.expected_chapters})
    return calls


def queued_book(tmp_path, n=2, title=TITLE):
    rt = cf.root(tmp_path, "managed", "audiobook", name=f"root-{title}")
    work_id, asset_id = cf.add_book(rt, title, AUTHOR, "audiobook", stub_files(rt, n, title=title))
    return rt, work_id, asset_id


def test_enqueue_is_idempotent_while_active_and_the_queue_is_bounded(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    a = conversion.enqueue(asset_id)
    b = conversion.enqueue(asset_id)
    assert a["created"] and not b["created"] and a["conversion"]["id"] == b["conversion"]["id"]
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 1
    conversion.save_settings({"queue_limit": 1})
    _, _, other = queued_book(tmp_path, title="Another")
    with pytest.raises(conversion.ConversionRefused) as exc:
        conversion.enqueue(other)
    assert exc.value.code == "queue_full"


def test_failures_are_recorded_retried_with_backoff_then_parked(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)

    def boom(tmp_out, should_cancel):
        raise convert_audio.ConversionError("ffmpeg failed: invalid data found")

    fake_encoder["behaviour"] = boom
    conversion.save_settings({"max_attempts": 3, "retry_backoff_minutes": 10})
    conv_id = conversion.enqueue(asset_id)["conversion"]["id"]
    t0 = db.now()
    out = run_all()
    row = db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))
    assert out["retrying"] == 1 and (row["status"], row["attempts"]) == ("queued", 1)
    assert "invalid data" in row["error"] and 9 * 60 < row["next_attempt_at"] - t0 < 11 * 60
    assert not list(rt["path"].rglob(".*partial")), "the partial output is removed"
    assert run_all()["processed"] == 0 and fake_encoder["n"] == 1, "not retried before the backoff is over"

    with db.tx() as conn:
        conn.execute("UPDATE conversions SET next_attempt_at=? WHERE id=?", (db.now() - 1, conv_id))
    run_all()
    row = db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))
    assert (row["status"], row["attempts"]) == ("queued", 2) and row["next_attempt_at"] - db.now() > 19 * 60   # doubled
    with db.tx() as conn:
        conn.execute("UPDATE conversions SET next_attempt_at=? WHERE id=?", (db.now() - 1, conv_id))
    out = run_all()
    row = db.one("SELECT * FROM conversions WHERE id=?", (conv_id,))
    assert out["failed"] == 1 and (row["status"], row["attempts"]) == ("failed", 3) and "invalid data" in row["error"]
    assert run_all()["processed"] == 0 and fake_encoder["n"] == 3, "parked: never retried forever"
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE derived_from_asset_id IS NOT NULL")["n"] == 0
    assert db.one("SELECT 1 FROM history WHERE event_type='conversion.failed'")

    # an admin can retry a parked conversion
    conversion.retry(conv_id)
    fake_encoder["behaviour"] = None
    assert run_all()["done"] == 1


def test_permanent_errors_are_not_retried(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)

    def bad(tmp_out, should_cancel):
        raise convert_audio.ConversionError("x has no audio stream", retryable=False)

    fake_encoder["behaviour"] = bad
    conversion.enqueue(asset_id)
    assert run_all()["failed"] == 1 and db.one("SELECT attempts FROM conversions")["attempts"] == 1


def test_not_enough_disk_space_is_refused_before_anything_is_written(tmp_path, fake_encoder, monkeypatch):
    rt, work_id, asset_id = queued_book(tmp_path)
    monkeypatch.setattr(conversion, "free_bytes", lambda path: 100 * 1024 * 1024)     # estimate is 123 KB + 1 GiB margin
    conversion.save_settings({"max_attempts": 1})
    with pytest.raises(conversion.ConversionRefused) as refused:       # the request itself is refused, with the reason
        conversion.enqueue(asset_id)
    assert refused.value.code == "no_space" and "Not enough free space" in str(refused.value)
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
    # space that vanishes between queueing and running is caught by the runner
    monkeypatch.setattr(conversion, "free_bytes", lambda path: 10 * 1024 ** 3)
    conversion.enqueue(asset_id)
    monkeypatch.setattr(conversion, "free_bytes", lambda path: 100 * 1024 * 1024)
    out = run_all()
    row = db.one("SELECT * FROM conversions")
    assert out["failed"] == 1 and row["status"] == "failed" and "Not enough free disk space" in row["error"]
    assert fake_encoder["n"] == 0
    assert not list(rt["path"].rglob(".*partial")) and not list(rt["path"].rglob("*.m4b"))
    # with room it goes through
    monkeypatch.setattr(conversion, "free_bytes", lambda path: 10 * 1024 ** 3)
    conversion.retry(row["id"])
    assert run_all()["done"] == 1


def test_cancel_a_queued_and_a_running_conversion(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    queued = conversion.enqueue(asset_id)["conversion"]["id"]
    assert conversion.cancel(queued)["status"] == "cancelled"
    assert run_all()["processed"] == 0
    # a new request for the same asset is allowed once the cancelled one is history
    running = conversion.enqueue(asset_id)["conversion"]["id"]

    def cancel_midway(tmp_out, should_cancel):
        conversion.cancel(running)                    # arrives while the encoder is working
        assert should_cancel()
        raise convert_audio.Cancelled()

    fake_encoder["behaviour"] = cancel_midway
    out = run_all()
    row = db.one("SELECT * FROM conversions WHERE id=?", (running,))
    assert out["cancelled"] == 1 and row["status"] == "cancelled" and row["finished_at"]
    assert not list(rt["path"].rglob(".*partial")) and not list(rt["path"].rglob("*.m4b"))
    with pytest.raises(conversion.ConversionRefused):
        conversion.cancel(running)                    # already finished
    assert conversion.retry(running)["status"] == "queued"


def test_progress_and_status_are_visible_while_running(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    conv_id = conversion.enqueue(asset_id)["conversion"]["id"]
    seen = {}

    def look(tmp_out, should_cancel):
        row = db.one("SELECT status,percent,tmp_path,started_at FROM conversions WHERE id=?", (conv_id,))
        seen.update(row)
        seen["listed"] = conversion.list_conversions(group="active")["items"][0]

    fake_encoder["behaviour"] = look
    run_all()
    assert seen["status"] == "running" and seen["percent"] == 42.0 and seen["tmp_path"].endswith(".partial") and seen["started_at"]
    assert seen["listed"]["status"] == "running" and seen["listed"]["work_title"] == TITLE
    final = conversion.list_conversions(group="history")["items"][0]
    assert final["status"] == "done" and final["percent"] == 100 and final["output_name"] == f"{TITLE}.m4b"


def test_interrupted_runs_are_requeued_and_their_partial_files_removed(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    conv_id = conversion.enqueue(asset_id)["conversion"]["id"]
    partial = rt["path"] / AUTHOR / TITLE / f".{TITLE}.{conv_id[:8]}.partial"
    partial.write_bytes(b"half")
    with db.tx() as conn:       # what a killed process leaves behind
        conn.execute("UPDATE conversions SET status='running',tmp_path=?,percent=40 WHERE id=?", (str(partial), conv_id))
    assert conversion.reap_orphans() == 1
    row = db.one("SELECT status,percent,tmp_path,error FROM conversions WHERE id=?", (conv_id,))
    assert row["status"] == "queued" and row["percent"] == 0 and row["tmp_path"] is None and "start over" in row["error"]
    assert not partial.exists()
    assert run_all()["done"] == 1


def test_a_file_that_is_not_a_partial_is_never_removed_by_cleanup(tmp_path):
    rt = cf.root(tmp_path, "managed", "audiobook")
    keep = rt["path"] / "precious.m4b"
    keep.write_bytes(b"mine")
    conversion._remove_partial(str(keep))
    assert keep.exists()
    adopted = cf.root(tmp_path, "adopt", "audiobook")
    hidden = adopted["path"] / ".x.partial"
    hidden.write_bytes(b"theirs")
    conversion._remove_partial(str(hidden))
    assert hidden.exists(), "nothing is ever deleted outside a managed root"


def test_two_workers_convert_two_books_at_once(tmp_path, fake_encoder):
    conversion.save_settings({"audio_concurrency": 2})
    for t in ("One", "Two"):
        queued_book(tmp_path, title=t)
    for row in db.all("SELECT id FROM media_assets WHERE derived_from_asset_id IS NULL AND status='owned'"):
        conversion.enqueue(row["id"])
    barrier = threading.Barrier(2, timeout=10)

    def meet(tmp_out, should_cancel):
        barrier.wait()          # only passes if both conversions are in the encoder at the same time

    fake_encoder["behaviour"] = meet
    out = run_all()
    assert out["done"] == 2 and out["failed"] == 0 and len(set(out["conversion_ids"])) == 2


def test_one_active_run_per_kind_through_the_scheduler(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    job = db.one("SELECT * FROM jobs WHERE kind='convert-audio'")
    assert job["enabled"] == 1
    started, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=lambda: scheduler.run_exclusive("convert-audio", lambda: (started.set(), release.wait(10), {})[2]))
    holder.start()
    try:
        assert started.wait(10)
        with pytest.raises(scheduler.JobBusy):
            scheduler.submit_job(job["id"], trigger="manual")
    finally:
        release.set()
        holder.join(10)
    conversion.enqueue(asset_id)
    result = scheduler.run_job(job["id"])             # the real job body, in the pool
    assert result["status"] == "success" and result["result"]["done"] == 1
    run = db.one("SELECT status,entities_json FROM job_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (job["id"],))
    assert run["status"] == "success" and json.loads(run["entities_json"])["conversion_ids"]
    # an idle run leaves no trace (the job wakes every minute)
    assert scheduler.run_job(job["id"])["result"]["processed"] == 0
    assert db.one("SELECT COUNT(*) n FROM job_runs WHERE job_id=?", (job["id"],))["n"] == 1


def test_a_long_conversion_renews_its_lease(tmp_path, fake_encoder, monkeypatch):
    rt, work_id, asset_id = queued_book(tmp_path)
    conversion.enqueue(asset_id)
    renewed = []
    monkeypatch.setattr(scheduler, "touch_lease", lambda kind: renewed.append(kind))
    run_all()
    assert renewed and set(renewed) == {"convert-audio"}
    assert scheduler.touch_lease("nothing-running") is None


def test_a_converted_file_that_went_missing_can_be_converted_again(tmp_path, fake_encoder):
    rt, work_id, asset_id = queued_book(tmp_path)
    conversion.enqueue(asset_id)
    assert run_all()["done"] == 1
    row = db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    assert conversion.assess(row)["state"] == "converted"
    out = Path(db.one("SELECT output_path FROM conversions")["output_path"])
    with db.tx() as conn:       # the library-health job marks a vanished file missing
        conn.execute("UPDATE media_files SET missing=1 WHERE path=?", (str(out),))
    info = conversion.assess(row)
    assert info["state"] == "stale" and "missing" in info["reason"]
    again = conversion.enqueue(asset_id)
    assert again["created"] and run_all()["done"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE derived_from_asset_id IS NOT NULL")["n"] == 1
