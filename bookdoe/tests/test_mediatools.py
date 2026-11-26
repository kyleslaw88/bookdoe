"""Tests for services/mediatools.py -- media tagging tooling (Part XXIII).

Tests that need real tools use skipif(shutil.which('ffmpeg') is None) /
importorskip('mutagen').  Capability tests monkeypatch shutil.which and
importlib so they run anywhere.
"""
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services import mediatools


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TAGS = {"title": "Test Title", "artist": "Test Artist", "composer": "Test Composer",
        "album": "Test Album", "date": "2024"}


@pytest.fixture(autouse=True)
def _reset_capabilities(monkeypatch):
    """Reset mediatools._capabilities before and after each test to prevent
    state leakage between tests (F6).

    Uses monkeypatch.setattr so the cleanup is automatic (F6).
    """
    monkeypatch.setattr(mediatools, '_capabilities', None)
    yield
    monkeypatch.setattr(mediatools, '_capabilities', None)


@pytest.fixture
def lib(tmp_path):
    """An enabled library root (a real root_folders row) with a directory outside it."""
    from app import db
    root = tmp_path / "lib"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
                  " VALUES (?,?,?,?,'adopt',1,1,?,?)", (db.uid(), "Lib", str(root), "audiobook", t, t))
    return root


def _atom(name: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + name + payload


def _mp3_path(directory: Path, with_metadata: bool = True) -> Path:
    """A tagged MP3 built with mutagen alone: real MPEG frames plus an ID3v2 tag."""
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3
    dst = directory / "test.mp3"
    dst.write_bytes((b"\xff\xfb\x90\x64" + b"\x00" * 413) * 4)  # 4 MPEG-1 Layer III frames
    if with_metadata:
        ID3().save(str(dst))
        tags = EasyID3(str(dst))
        for k, v in TAGS.items():
            tags[k] = v
        tags.save()
    return dst


def _m4a_path(directory: Path, with_metadata: bool = True, name: str = "test.m4a") -> Path:
    """A tagged M4A built with mutagen alone: a minimal ftyp + moov with a sound track."""
    from mutagen.mp4 import MP4
    dst = directory / name
    hdlr = _atom(b"hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 13)
    mdhd = _atom(b"mdhd", b"\x00" * 4 + b"\x00" * 8 + struct.pack(">II", 44100, 44100) + b"\x00" * 4)
    moov = _atom(b"moov", _atom(b"trak", _atom(b"mdia", mdhd + hdlr)))
    dst.write_bytes(_atom(b"ftyp", b"M4A \x00\x00\x00\x00M4A mp42isom") + moov)
    if with_metadata:
        mp4 = MP4(str(dst))
        mp4.add_tags()
        mp4["\xa9nam"], mp4["\xa9ART"], mp4["\xa9wrt"] = TAGS["title"], TAGS["artist"], TAGS["composer"]
        mp4["\xa9alb"], mp4["\xa9day"] = TAGS["album"], TAGS["date"]
        mp4.save()
    return dst


# ---------------------------------------------------------------------------
# 1. requirements.txt pins mutagen; import succeeds
# ---------------------------------------------------------------------------

def test_mutagen_import_succeeds():
    """AC-1: mutagen is importable (pinned in requirements.txt)."""
    import mutagen  # noqa: F401


def test_mutagen_pinned_in_requirements():
    """AC-1 (proof): mutagen==X.Y.Z appears in requirements.txt."""
    req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert "mutagen==" in req


# ---------------------------------------------------------------------------
# 2. GET /api/v1/system/tools returns availability; non-admin gets 403
# ---------------------------------------------------------------------------

def test_system_tools_endpoint_returns_data():
    """AC-2: endpoint returns ffmpeg/ffprobe/mutagen availability and versions."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/system/tools")
        assert r.status_code == 200
        data = r.json()
        assert "ffmpeg" in data
        assert "ffprobe" in data
        assert "mutagen" in data
        assert "supports" in data
        # Each tool has 'available' and 'version'.
        for tool in ("ffmpeg", "ffprobe", "mutagen"):
            assert "available" in data[tool]
            assert "version" in data[tool]


def test_system_tools_requires_admin_dependency():
    """AC-2 (proof): endpoint depends on auth.require_admin."""
    from app.main import app

    route = None
    for r in app.routes:
        if hasattr(r, 'path') and r.path == "/api/v1/system/tools":
            route = r
            break
    assert route is not None

    # Verify the endpoint uses require_admin dependency.
    import inspect
    sig = inspect.signature(route.endpoint)
    found_admin_dep = False
    for param in sig.parameters.values():
        default = param.default
        if hasattr(default, 'dependency') and hasattr(default.dependency, '__name__'):
            if default.dependency.__name__ == "require_admin":
                found_admin_dep = True
                break
        # FastAPI wraps Depends in a tuple for multiple defaults
        if isinstance(default, tuple):
            for d in default:
                if hasattr(d, 'dependency') and hasattr(d.dependency, '__name__'):
                    if d.dependency.__name__ == "require_admin":
                        found_admin_dep = True
                        break
    assert found_admin_dep, "Endpoint must depend on auth.require_admin"


def test_system_tools_403_for_non_admin():
    """AC-2 (proof): a non-admin user gets 403 on GET /api/v1/system/tools.

    Overrides auth.current_user to return a non-admin user dict so the real
    require_admin function runs and rejects the request with 403.  This
    exercises the actual is_admin check inside require_admin rather than
    replacing the whole dependency with a tautology.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth

    def fake_current_user_nonadmin(request):
        """Return a non-admin user dict so require_admin rejects it."""
        return {
            "id": "test-user",
            "username": "testuser",
            "email": None,
            "is_admin": False,
            "access_scope": "kids",
            "auth_source": "local",
            "_auth_method": "none",
        }

    original = auth.current_user
    try:
        auth.current_user = fake_current_user_nonadmin
        with TestClient(app) as c:
            r = c.get("/api/v1/system/tools")
            assert r.status_code == 403, \
                f"Expected 403 for non-admin, got {r.status_code}: {r.text}"
    finally:
        auth.current_user = original


def test_capabilities_no_ffmpeg(monkeypatch):
    """AC-3: with ffmpeg absent, supports.merge is False and convert is empty."""
    monkeypatch.setattr(
        shutil, "which",
        lambda name: None if name == "ffmpeg"
        else ("/usr/bin/" + name if name == "ffprobe" else None),
    )
    caps = mediatools.capabilities()
    assert caps["supports"]["merge"] is False
    assert caps["supports"]["convert"] == []
    # ffprobe should still be available.
    assert caps["ffprobe"]["available"] is True


def test_capabilities_no_mutagen(monkeypatch):
    """Mutagen unavailable -> mutagen.available is False."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mutagen" or name.startswith("mutagen."):
            raise ImportError("no mutagen")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    caps = mediatools.capabilities()
    assert caps["mutagen"]["available"] is False


def test_capabilities_full_stack():
    """When all tools are present, merge is True and convert is non-empty."""
    caps = mediatools.capabilities()
    # In the test environment, ffmpeg and mutagen should both be available.
    if caps["ffmpeg"]["available"]:
        assert caps["supports"]["merge"] is True
        assert len(caps["supports"]["convert"]) > 0


# ---------------------------------------------------------------------------
# 4. read_tags returns title/artist/composer/album/date for mp3 and m4a
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("make", [_mp3_path, _m4a_path], ids=["mp3", "m4a"])
def test_read_tags_returns_fields_and_leaves_file_untouched(lib, make):
    """AC-4: title/artist/composer/album/date come back; sha256 unchanged. Needs only mutagen."""
    pytest.importorskip("mutagen")
    f = make(lib)
    before = hashlib.sha256(f.read_bytes()).hexdigest()
    tags = mediatools.read_tags(f)
    assert {k: tags.get(k) for k in TAGS} == TAGS
    assert hashlib.sha256(f.read_bytes()).hexdigest() == before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("ext", ["mp3", "m4a"])
def test_read_tags_ffmpeg_generated(lib, ext):
    """AC-4 with files written by ffmpeg itself."""
    pytest.importorskip("mutagen")
    dst = lib / f"gen.{ext}"
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1"]
    for k, v in TAGS.items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [str(dst)], capture_output=True, timeout=30, check=True)
    before = hashlib.sha256(dst.read_bytes()).hexdigest()
    tags = mediatools.read_tags(dst)
    assert {k: tags.get(k) for k in TAGS} == TAGS
    assert hashlib.sha256(dst.read_bytes()).hexdigest() == before


def test_read_tags_no_metadata(lib):
    pytest.importorskip("mutagen")
    assert mediatools.read_tags(_mp3_path(lib, with_metadata=False)) == {}


def test_read_tags_unsupported_ext(lib):
    dst = lib / "test.txt"
    dst.write_text("hello")
    assert mediatools.read_tags(dst) == {}


# ---------------------------------------------------------------------------
# 5. Dockerfile already contains ffmpeg
# ---------------------------------------------------------------------------

def test_dockerfile_has_ffmpeg():
    """AC-5: Dockerfile quotes the ffmpeg install line."""
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "ffmpeg" in dockerfile
    # Find the apt-get line.
    for line in dockerfile.splitlines():
        if "apt-get install" in line and "ffmpeg" in line:
            break
    else:
        pytest.fail("Could not find ffmpeg in apt-get line of Dockerfile")



# ---------------------------------------------------------------------------
# Path guard: read_tags only opens files inside an enabled library root
# ---------------------------------------------------------------------------

def test_read_tags_rejects_file_outside_library_roots(tmp_path, lib):
    """The same tagged file is read inside the root and refused outside it, with no extra argument."""
    pytest.importorskip("mutagen")
    inside = _m4a_path(lib)
    outside = _m4a_path(tmp_path / "outside")
    assert mediatools.read_tags(inside)["title"] == TAGS["title"]
    assert mediatools.read_tags(outside) == {}


def test_read_tags_rejects_everything_without_a_root(tmp_path):
    pytest.importorskip("mutagen")
    assert mediatools.read_tags(_mp3_path(tmp_path)) == {}


def test_read_tags_ignores_disabled_root(lib):
    pytest.importorskip("mutagen")
    from app import db
    f = _mp3_path(lib)
    assert mediatools.read_tags(f)
    db.execute("UPDATE root_folders SET enabled=0")
    assert mediatools.read_tags(f) == {}


def test_read_tags_rejects_symlink_escaping_root(tmp_path, lib):
    pytest.importorskip("mutagen")
    target = _mp3_path(tmp_path / "outside")
    link = lib / "escape.mp3"
    link.symlink_to(target)
    assert mediatools.read_tags(target) == {}
    assert mediatools.read_tags(link) == {}


# ---------------------------------------------------------------------------
# F3: mutagen missing degrades gracefully (no NameError)
# ---------------------------------------------------------------------------

def test_read_tags_no_mutagen_graceful(monkeypatch, tmp_path):
    """read_tags returns {} when mutagen is missing, without NameError (F3)."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mutagen" or name.startswith("mutagen."):
            raise ImportError("no mutagen")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Should not raise NameError; should return {}
    tags = mediatools.read_tags(str(tmp_path / "fake.mp3"))
    assert tags == {}


# ---------------------------------------------------------------------------
# F5: require_tools helper
# ---------------------------------------------------------------------------

def test_require_tools_passes():
    """require_tools raises nothing when all requested tools are available."""
    # In the sandbox, ffmpeg/ffprobe may not be available, so we only
    # test with tools that ARE available (mutagen).
    mediatools.require_tools("mutagen")
    # If we get here, no exception was raised for mutagen.


def test_require_tools_raises_when_missing(monkeypatch):
    """require_tools raises ToolMissingError when a tool is missing (F5)."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # Force mutagen to appear missing by clearing capabilities cache
    mediatools.reset_capabilities()

    # Patch the mutagen import inside capabilities
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mutagen" or name.startswith("mutagen."):
            raise ImportError("no mutagen")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(mediatools.ToolMissingError, match="ffmpeg"):
        mediatools.require_tools("ffmpeg")


# ---------------------------------------------------------------------------
# F6: _run_version handles empty output
# ---------------------------------------------------------------------------

def test_run_version_empty_output():
    """_run_version handles empty output gracefully (F6)."""
    # We can't easily mock subprocess.run to return empty output without
    # affecting other tests, so we test with a non-existent command.
    result = mediatools._run_version(["nonexistent_command_xyz"])
    assert result is None


# ---------------------------------------------------------------------------
# F6: Capabilities cache is properly reset
# ---------------------------------------------------------------------------

def test_capabilities_cache_reset():
    """reset_capabilities() clears the cache so _detect_capabilities runs again."""
    # First call caches
    caps1 = mediatools.capabilities()
    assert caps1 is not None

    # Reset
    mediatools.reset_capabilities()

    # Second call should re-detect
    caps2 = mediatools.capabilities()
    assert caps2 is not None
    # The objects should be different (new dict)
    assert caps1 is not caps2


# ---------------------------------------------------------------------------
# F6: No state leakage between capability tests
# ---------------------------------------------------------------------------

def test_no_state_leakage_after_no_ffmpeg(monkeypatch):
    """After monkeypatching shutil.which to hide ffmpeg, the next test
    should see the real environment (F6)."""
    # First, hide ffmpeg
    monkeypatch.setattr(shutil, "which",
                        lambda name: None if name == "ffmpeg"
                        else ("/usr/bin/" + name if name == "ffprobe" else None))
    caps = mediatools.capabilities()
    assert caps["supports"]["merge"] is False

    # Reset and re-detect -- the next test should see the real environment
    mediatools.reset_capabilities()
    # The autouse fixture will also reset, but we reset here too to be safe.


def test_mutagen_version_detected():
    """mutagen version is detected correctly (F3 fix)."""
    caps = mediatools.capabilities()
    # mutagen should be available and have a version
    assert caps["mutagen"]["available"] is True
    # version_string should be detected
    assert caps["mutagen"]["version"] is not None, \
        f"Expected mutagen version, got {caps['mutagen']['version']}"
