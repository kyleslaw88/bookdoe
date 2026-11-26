"""Audiobook conversion: one chaptered M4B from MP3 / M4A / FLAC / Ogg / Opus files (issue #265).

Pure tool work, no database: ``plan_audio`` probes the sources and decides the encode,
``run_ffmpeg`` makes the file (argument list, never a shell, niced, with a timeout, a cancel
check and progress), ``verify_output`` re-probes it. services/conversion.py owns queueing,
paths, ownership and publication.

Design notes
- Order of the parts: the import's ``part_number`` when every part has a distinct one, else the
  tags' disc/track numbers when every file has distinct ones, else a natural sort of the path
  ("2" before "10"). Mixed evidence is not trusted piecemeal.
- Chapters are the file boundaries, titled from the ID3/Vorbis title when it is present and
  unique-ish, else "Chapter N". A single source that already carries chapters keeps them.
- AAC-LC sources with identical parameters are stream-copied (no generation loss); everything
  else is encoded at ``bitrate`` (default 64k mono / 96k stereo, channel count preserved up
  to stereo). Sources whose sample rate or channel count differ are joined with the concat
  filter and resampled; homogeneous sources use the (much cheaper) concat demuxer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

AUDIO_SOURCE_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
PROBE_TIMEOUT = 60
MAX_TOTAL_SECONDS = 400 * 3600       # a 400 h "audiobook" is a mis-grouped folder, not a book
STDERR_KEEP = 1500
TOOL_VERSION = "bookdoe-convert/1"


class ConversionError(RuntimeError):
    """A conversion could not be done. ``retryable`` False = retrying cannot help (bad input,
    refusal); ``skip`` marks a deliberate non-conversion (DRM, unsupported) rather than a failure."""

    def __init__(self, message: str, *, retryable: bool = True, skip: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.skip = skip


class Cancelled(ConversionError):
    def __init__(self) -> None:
        super().__init__("Cancelled", retryable=False)


def tool(name: str) -> str:
    """Absolute path of ffmpeg / ffprobe, or a ConversionError naming the missing tool."""
    path = shutil.which(name)
    if not path:
        raise ConversionError(f"{name} is not installed on this server", retryable=False)
    return path


def trim(text: str, keep: int = STDERR_KEEP) -> str:
    text = (text or "").strip()
    return text if len(text) <= keep else "..." + text[-keep:]


def natural_key(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


@dataclass
class Source:
    path: str
    duration: float
    codec: str
    sample_rate: int
    channels: int
    title: str | None = None
    track: int | None = None
    disc: int | None = None
    part_number: int | None = None
    chapters: list[dict[str, Any]] = field(default_factory=list)
    has_cover: bool = False
    size: int = 0


@dataclass
class AudioPlan:
    sources: list[Source]
    total_seconds: float
    channels: int
    sample_rate: int
    bitrate_kbps: int
    copy: bool
    use_filter: bool
    expected_chapters: int
    estimated_bytes: int
    order_basis: str


def _int_tag(value: Any) -> int | None:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def probe(path: str, *, pass_fds: tuple[int, ...] = ()) -> dict[str, Any]:
    """ffprobe JSON (format, streams, chapters) of one file; ConversionError when unreadable. ``pass_fds``: descriptors
    the child inherits (for a ``/proc/self/fd/N`` path)."""
    try:
        p = subprocess.run(
            [tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", path],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False, pass_fds=pass_fds)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"ffprobe timed out on {Path(path).name}")
    if p.returncode != 0:
        raise ConversionError(f"ffprobe could not read {Path(path).name}: {trim(p.stderr, 300)}", retryable=False)
    try:
        return json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise ConversionError(f"ffprobe returned unreadable output for {Path(path).name}", retryable=False)


def read_source(path: str, part_number: int | None = None, size: int = 0) -> Source:
    data = probe(path)
    fmt = data.get("format") or {}
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ConversionError(f"{Path(path).name} has no audio stream", retryable=False)
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    tags.update({str(k).lower(): v for k, v in (audio.get("tags") or {}).items() if str(k).lower() not in tags})
    try:
        duration = float(fmt.get("duration") or audio.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ConversionError(f"{Path(path).name} has no readable duration", retryable=False)
    chapters = []
    for c in data.get("chapters") or []:
        try:
            chapters.append({"start": float(c["start_time"]), "end": float(c["end_time"]),
                             "title": {str(k).lower(): v for k, v in (c.get("tags") or {}).items()}.get("title")})
        except (KeyError, TypeError, ValueError):
            continue
    return Source(
        path=path, duration=duration, codec=str(audio.get("codec_name") or ""),
        sample_rate=int(audio.get("sample_rate") or 0) or 44100, channels=int(audio.get("channels") or 0) or 2,
        title=(tags.get("title") or "").strip() or None, track=_int_tag(tags.get("track")), disc=_int_tag(tags.get("disc")),
        part_number=part_number, chapters=chapters, size=size,
        has_cover=any(s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
                      for s in data.get("streams") or []),
    )


def order_sources(sources: list[Source]) -> tuple[list[Source], str]:
    """Playback order and the evidence it came from (see the module docstring)."""
    parts = [s.part_number for s in sources]
    if len(sources) > 1 and all(p is not None for p in parts) and len(set(parts)) == len(parts):
        return sorted(sources, key=lambda s: (s.part_number, natural_key(s.path))), "part_number"
    keys = [(s.disc or 0, s.track) for s in sources]
    if len(sources) > 1 and all(s.track is not None for s in sources) and len(set(keys)) == len(keys):
        return sorted(sources, key=lambda s: (s.disc or 0, s.track, natural_key(s.path))), "track_tag"
    return sorted(sources, key=lambda s: natural_key(s.path)), "filename"


def plan_audio(files: list[dict[str, Any]], *, mono_kbps: int = 64, stereo_kbps: int = 96) -> AudioPlan:
    """Probe the source files (rows with ``path``, ``part_number``, ``size_bytes``) and decide the encode."""
    if not files:
        raise ConversionError("The source asset has no audio files on disk", retryable=False)
    sources = [read_source(f["path"], f.get("part_number"), int(f.get("size_bytes") or 0)) for f in files]
    ordered, basis = order_sources(sources)
    total = sum(s.duration for s in ordered)
    if total > MAX_TOTAL_SECONDS:
        raise ConversionError(f"Refusing: the sources add up to {total / 3600:.0f} hours, which is not one book", retryable=False)
    first = ordered[0]
    channels = 1 if first.channels == 1 else 2
    rate = first.sample_rate
    homogeneous = all(s.sample_rate == rate and min(s.channels, 2) == channels for s in ordered)
    copy = homogeneous and all(s.codec == "aac" for s in ordered)
    kbps = mono_kbps if channels == 1 else stereo_kbps
    if len(ordered) == 1 and ordered[0].chapters:
        expected = len(ordered[0].chapters)
    else:
        expected = len(ordered)
    est = sum(s.size for s in ordered) if copy else int(total * kbps * 1000 / 8 * 1.03) + 600_000
    return AudioPlan(sources=ordered, total_seconds=total, channels=channels, sample_rate=rate, bitrate_kbps=kbps, copy=copy,
                     use_filter=not homogeneous, expected_chapters=expected, estimated_bytes=est, order_basis=basis)


def _ffmeta_escape(value: Any) -> str:
    return re.sub(r"([=;#\\\n])", lambda m: "\\" + ("n" if m.group(1) == "\n" else m.group(1)), str(value))


def chapter_titles(plan: AudioPlan) -> list[str]:
    """ID3 titles when every file has one and they are not all identical (an album-wide title
    repeated on every track is a book title, not a chapter name), else 'Chapter N'."""
    titles = [s.title for s in plan.sources]
    if all(titles) and (len(set(titles)) > 1 or len(titles) == 1):
        return [str(t) for t in titles]
    return [f"Chapter {i}" for i in range(1, len(plan.sources) + 1)]


def build_ffmetadata(plan: AudioPlan, meta: dict[str, Any]) -> str:
    """FFMETADATA1 text: book tags plus (unless a single source keeps its own) file-boundary chapters."""
    lines = [";FFMETADATA1"]
    # iTunes-style atoms only (no `use_metadata_tags`): that flag switches the muxer to QuickTime
    # "mdta" keys, which carry arbitrary names but cannot hold the embedded cover. Series and the
    # ASIN therefore ride in the standard grouping / comment atoms.
    series = meta.get("series")
    if series and meta.get("series_position"):
        series = f"{series} #{meta['series_position']}"
    comment = "\n".join(x for x in (meta.get("description"), f"ASIN: {meta['asin']}" if meta.get("asin") else None) if x)
    tags = {
        "title": meta.get("title"), "album": meta.get("title"),
        "artist": meta.get("author"), "album_artist": meta.get("author"),
        "composer": meta.get("narrator"), "genre": "Audiobook", "date": meta.get("year"),
        "grouping": series, "comment": comment or None, "encoded_by": TOOL_VERSION,
    }
    for k, v in tags.items():
        if v not in (None, ""):
            lines.append(f"{k}={_ffmeta_escape(v)}")
    if not (len(plan.sources) == 1 and plan.sources[0].chapters):
        start = 0.0
        for title, s in zip(chapter_titles(plan), plan.sources):
            end = start + s.duration
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(start * 1000))}", f"END={int(round(end * 1000))}",
                      f"title={_ffmeta_escape(title)}"]
            start = end
    return "\n".join(lines) + "\n"


def _concat_list(plan: AudioPlan) -> str:
    return "".join("file '" + s.path.replace("'", "'\\''") + "'\n" for s in plan.sources)


def build_command(plan: AudioPlan, tmp_out: str, workdir: Path, cover: str | None, *, overwrite: bool = False) -> list[str]:
    """The ffmpeg argument list (inputs: audio, metadata, optional cover). ``-n`` (never overwrite) unless ``overwrite``:
    only for an output the caller created itself and hands over as ``/proc/self/fd/N``."""
    ffmpeg = tool("ffmpeg")
    cmd: list[str] = []
    nice = shutil.which("nice")
    if nice:
        cmd += [nice, "-n", "10"]
    cmd += [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:1", "-y" if overwrite else "-n"]
    if plan.use_filter:
        for s in plan.sources:
            cmd += ["-i", s.path]
        n = len(plan.sources)
        layout = "mono" if plan.channels == 1 else "stereo"
        chains = "".join(
            f"[{i}:a]aresample={plan.sample_rate},aformat=sample_fmts=fltp:channel_layouts={layout}[a{i}];" for i in range(n))
        graph = chains + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
        cmd += ["-filter_complex", graph]
        audio_map = ["-map", "[out]"]
        n_audio_inputs = n
    else:
        lst = workdir / "inputs.txt"
        lst.write_text(_concat_list(plan), encoding="utf-8")
        cmd += ["-f", "concat", "-safe", "0", "-i", str(lst)]
        audio_map = ["-map", "0:a:0"]
        n_audio_inputs = 1
    meta_index = n_audio_inputs
    cmd += ["-i", str(workdir / "meta.ffmeta")]
    cover_index = None
    if cover:
        cover_index = meta_index + 1
        cmd += ["-i", cover]
    cmd += audio_map
    if cover_index is not None:
        cmd += ["-map", f"{cover_index}:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += ["-map_metadata", str(meta_index)]
    if len(plan.sources) == 1 and plan.sources[0].chapters:
        cmd += ["-map_chapters", "0"]
    else:
        cmd += ["-map_chapters", str(meta_index)]
    if plan.copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.bitrate_kbps}k", "-ac", str(plan.channels), "-ar", str(plan.sample_rate)]
    cmd += ["-movflags", "+faststart", "-f", "ipod", tmp_out]
    return cmd


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill ffmpeg and the children it spawned; a surviving child keeps the stdout pipe open and hangs the read."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


def run_ffmpeg(plan: AudioPlan, meta: dict[str, Any], tmp_out: str, *, cover: str | None,
               on_progress: Callable[[float], None] | None = None,
               should_cancel: Callable[[], bool] | None = None, timeout: float | None = None,
               pass_fds: tuple[int, ...] = (), overwrite: bool = False) -> None:
    """Run the encode into ``tmp_out`` (a path that must not exist; or, with ``pass_fds`` and ``overwrite``, the
    ``/proc/self/fd/N`` of an output file the caller created and holds open). Raises ConversionError."""
    timeout = timeout or max(1800.0, plan.total_seconds * 0.5)
    workdir = Path(tempfile.mkdtemp(prefix="alx-convert-"))
    try:
        (workdir / "meta.ffmeta").write_text(build_ffmetadata(plan, meta), encoding="utf-8")
        cmd = build_command(plan, tmp_out, workdir, cover, overwrite=overwrite)
        err_file = workdir / "stderr.txt"
        with open(err_file, "wb") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True, pass_fds=pass_fds,
                                    start_new_session=True)
            state = {"why": None}
            done = threading.Event()

            def watchdog() -> None:
                deadline = time.monotonic() + timeout
                while not done.wait(0.1):
                    if should_cancel and should_cancel():
                        state["why"] = "cancel"
                    elif time.monotonic() > deadline:
                        state["why"] = "timeout"
                    if state["why"]:
                        _kill_group(proc)
                        return

            t = threading.Thread(target=watchdog, daemon=True)
            t.start()
            last = 0.0
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    key, _, value = line.strip().partition("=")
                    if key in ("out_time_us", "out_time_ms") and on_progress and plan.total_seconds > 0:
                        try:
                            seconds = int(value) / 1_000_000
                        except ValueError:
                            continue
                        now = time.monotonic()
                        if now - last >= 1.0:
                            last = now
                            on_progress(max(0.0, min(99.0, seconds / plan.total_seconds * 100)))
                proc.wait()
            finally:
                done.set()
                t.join(timeout=2)
                if proc.poll() is None:
                    _kill_group(proc)
                    proc.wait()
        if state["why"] == "cancel":
            raise Cancelled()
        if state["why"] == "timeout":
            raise ConversionError(f"ffmpeg exceeded its {int(timeout)} s time limit and was stopped")
        if proc.returncode != 0:
            raise ConversionError("ffmpeg failed: " + trim(err_file.read_text(errors="replace")))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def extract_cover(source_path: str, dest: Path) -> str | None:
    """First embedded picture of a source file as a JPEG/PNG, or None."""
    try:
        p = subprocess.run([tool("ffmpeg"), "-v", "error", "-nostdin", "-n", "-i", source_path, "-map", "0:v:0", "-frames:v", "1",
                            "-c", "copy", "-f", "image2", str(dest)], capture_output=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, ConversionError):
        return None
    return str(dest) if p.returncode == 0 and dest.exists() and dest.stat().st_size > 0 else None


def verify_output(path: str, plan: AudioPlan, *, pass_fds: tuple[int, ...] = ()) -> dict[str, Any]:
    """Re-probe the finished file: one AAC audio stream, the right chapter count, a duration
    within tolerance of the sources. Raises ConversionError (retryable) on a mismatch."""
    data = probe(path, pass_fds=pass_fds)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) != 1 or audio[0].get("codec_name") != "aac":
        raise ConversionError(f"Verification failed: expected one AAC audio stream, found {[s.get('codec_name') for s in audio]}")
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    tolerance = 2.0 + 0.002 * plan.total_seconds + 0.05 * len(plan.sources)
    if abs(duration - plan.total_seconds) > tolerance:
        raise ConversionError(f"Verification failed: output is {duration:.1f} s, the sources add up to {plan.total_seconds:.1f} s")
    chapters = len(data.get("chapters") or [])
    if chapters != plan.expected_chapters:
        raise ConversionError(f"Verification failed: output has {chapters} chapters, expected {plan.expected_chapters}")
    size = os.path.getsize(path)
    if size <= 0:
        raise ConversionError("Verification failed: the output file is empty")
    return {"duration_seconds": round(duration, 1), "chapters": chapters, "audio_streams": len(audio),
            "codec": audio[0].get("codec_name"), "channels": audio[0].get("channels"), "size_bytes": size,
            "has_cover": any(s.get("codec_type") == "video" for s in streams)}


def tool_signature(plan: AudioPlan) -> str:
    """Recorded in media_assets.derived_by: tool, mode and encode settings."""
    mode = "stream copy" if plan.copy else f"aac {plan.bitrate_kbps}k {'mono' if plan.channels == 1 else 'stereo'}"
    return f"{TOOL_VERSION} ffmpeg; {mode}"
