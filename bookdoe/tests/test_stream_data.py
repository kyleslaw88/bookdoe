"""P5-02: codec / sample rate / channels / chapters / embedded cover persisted on media_files."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import downloads, migration, repair, scanner, scheduler

from test_import_idempotent import _download, add_root, prepare_import
from test_migration import make_readarr, seed_local_work
from test_managed_reconcile import _owned_asset

M4B_PROBE = {
    "format": {"duration": "3600.5", "bit_rate": "64000", "tags": {"title": "Dead World", "artist": "Craig Alanson"}},
    "streams": [
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2, "bits_per_raw_sample": "16"},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
    ],
    "chapters": [
        {"start_time": "0.000000", "end_time": "10.5", "tags": {"title": "Opening"}},
        {"start_time": "10.5", "end_time": "20.0", "tags": {"TITLE": "Two"}},
        {"start_time": "20.0", "end_time": "30.0"},
    ],
}
MP3_PROBE = {
    "format": {"duration": "120", "bit_rate": "128000", "tags": {}},
    "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "22050", "channels": 1}],
    "chapters": [],
}


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode


@pytest.fixture
def canned(monkeypatch):
    """Canned ffprobe JSON chosen by file extension; records the paths probed."""
    probed: list[str] = []

    def fake_run(args, **kwargs):
        assert args[0] == "ffprobe" and kwargs.get("timeout") == 20
        probed.append(args[-1])
        data = M4B_PROBE if args[-1].endswith(".m4b") else MP3_PROBE
        return _Result(json.dumps(data))

    monkeypatch.setattr(scanner.subprocess, "run", fake_run)
    return probed


@pytest.fixture
def no_ffprobe(monkeypatch):
    def boom(args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(scanner.subprocess, "run", boom)


def _audio_root(tmp_path: Path, mode: str = "adopt", name: str = "lib") -> Path:
    base = tmp_path / name
    add_root(base, mode)
    return base


def _audio_work(title: str = "Dead World") -> tuple[str, str]:
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid, title, title, t, t))
    return wid, _owned_asset(wid, "audiobook")


def _row(path: Path) -> dict:
    return db.one("SELECT * FROM media_files WHERE path=?", (str(path),))


def _insert_unprobed(asset_id: str, path: Path, *, missing: int = 0) -> str:
    fid = db.uid()
    st = path.stat() if path.exists() else None
    with db.tx() as c:
        c.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,mtime,fingerprint,container,embedded_json,adopted,managed,missing,discovered_at) "
            "VALUES (?,?,?,?,?,?,?,'{}',1,0,?,?)",
            (fid, asset_id, str(path), st.st_size if st else 0, st.st_mtime if st else None, "fp-" + path.name, path.suffix.lstrip("."), missing, db.now()),
        )
    return fid


def test_scan_and_adopt_writes_stream_columns_for_m4b_and_mp3(tmp_path, monkeypatch, canned):
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kw: {})
    base = _audio_root(tmp_path)
    (base / "Craig Alanson" / "Dead World").mkdir(parents=True)
    m4b = base / "Craig Alanson" / "Dead World" / "Dead World.m4b"
    mp3 = base / "Craig Alanson" / "Dead World" / "Extra.mp3"
    m4b.write_bytes(b"m4b" * 100)
    mp3.write_bytes(b"mp3" * 100)
    root = db.one("SELECT id FROM root_folders")
    wid, _ = _audio_work()

    scanner.scan_root(root["id"], auto_adopt_exact=False)
    for c in db.all("SELECT id FROM scan_candidates"):
        scanner.adopt_candidate(c["id"], work_id=wid)

    r = _row(m4b)
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == ("aac", 44100, 2, 3, 1)
    detail = json.loads(r["embedded_json"])
    assert detail["chapters"][0] == {"start": 0.0, "end": 10.5, "title": "Opening"}
    assert detail["chapters"][1]["title"] == "Two" and detail["chapters"][2]["title"] is None
    assert detail["bit_depth"] == 16
    r = _row(mp3)
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == ("mp3", 22050, 1, 0, 0)


def test_chapter_detail_is_capped_but_count_is_true(tmp_path, monkeypatch):
    many = {**M4B_PROBE, "chapters": [{"start_time": str(i), "end_time": str(i + 1)} for i in range(750)]}
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: _Result(json.dumps(many)))
    f = tmp_path / "long.m4b"
    f.write_bytes(b"x")
    fp = scanner._ffprobe(f)
    assert len(fp["chapters"]) == scanner.MAX_CHAPTERS == 500
    assert fp["chapter_count"] == 750


def test_import_download_populates_stream_columns(tmp_path, canned):
    did, _, asset_id = _download()
    prepare_import(tmp_path, did, ["a.m4b"])
    downloads.import_completed(did)
    r = db.one("SELECT * FROM media_files WHERE asset_id=?", (asset_id,))
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == ("aac", 44100, 2, 3, 1)


def test_reconcile_managed_files_populates_stream_columns(tmp_path, canned):
    base = _audio_root(tmp_path, "managed", "managed")
    wid = migration._find_or_create_stub({"title": "Dead World", "author": "Craig Alanson", "identifiers": {}})
    _owned_asset(wid, "audiobook")
    path = base / "Craig Alanson" / "Dead World" / "Dead World.m4b"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio" * 10)
    assert repair.reconcile_managed_files()["registered"] == 1
    r = _row(path)
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == ("aac", 44100, 2, 3, 1)


def test_readarr_migration_populates_stream_columns(tmp_path, monkeypatch, canned):
    media = tmp_path / "book.m4b"
    media.write_bytes(b"audio" * 10)
    source = tmp_path / "readarr.db"
    make_readarr(source, tmp_path, media)
    seed_local_work()
    add_root(tmp_path / "managed", "managed")
    a = migration.analyze_readarr(str(source), "audiobook", "Readarr Audio")
    migration.reconcile_run(a["run_id"], use_network=False)
    monkeypatch.setattr(migration, "_free_space_bytes", lambda _p: 10 * 1024**3)
    migration.execute_run(a["run_id"])
    r = _row(media)
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == ("aac", 44100, 2, 3, 1)


def test_ebook_rows_keep_null_stream_columns(tmp_path, canned):
    base = _audio_root(tmp_path)
    epub = base / "a.epub"
    epub.write_bytes(b"not an epub")
    detected = scanner.inspect_file(epub, root=base)
    assert set(scanner.probe_columns(detected).values()) == {None}
    assert canned == []


def test_probe_columns_is_all_none_without_a_probe():
    assert scanner.probe_columns({"title": "x"}) == {
        "codec": None, "sample_rate": None, "channels": None, "chapter_count": None, "has_embedded_cover": None,
    }


def test_scan_succeeds_and_columns_stay_null_without_ffprobe(tmp_path, monkeypatch, no_ffprobe):
    monkeypatch.setattr(scanner.metadata, "resolve", lambda **kw: {})
    base = _audio_root(tmp_path)
    (base / "A" / "B").mkdir(parents=True)
    f = base / "A" / "B" / "b.m4b"
    f.write_bytes(b"x" * 50)
    wid, _ = _audio_work("B")
    root = db.one("SELECT id FROM root_folders")
    result = scanner.scan_root(root["id"], auto_adopt_exact=False)
    assert result["errors"] == 0 and result["files_seen"] == 1
    scanner.adopt_candidate(db.one("SELECT id FROM scan_candidates")["id"], work_id=wid)
    r = _row(f)
    assert (r["codec"], r["sample_rate"], r["channels"], r["chapter_count"], r["has_embedded_cover"]) == (None,) * 5
    assert scanner.backfill_stream_data()["filled"] == 0
    assert _row(f)["sample_rate"] is None


def test_ffprobe_failure_exit_degrades_to_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: _Result("", returncode=1))
    assert scanner._ffprobe(tmp_path / "x.m4b") == {}
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffprobe", 20)))
    assert scanner._ffprobe(tmp_path / "x.m4b") == {}


def test_backfill_fills_at_most_limit_is_idempotent_and_skips_missing(tmp_path, canned):
    base = _audio_root(tmp_path)
    _, asset_id = _audio_work()
    files = []
    for i in range(3):
        f = base / f"part{i}.m4b"
        f.write_bytes(b"audio" * (i + 1))
        files.append(f)
    gone = base / "gone.m4b"
    gone.write_bytes(b"audio")
    _insert_unprobed(asset_id, gone, missing=1)
    for f in files:
        _insert_unprobed(asset_id, f)
    before = {str(f): (_row(f)["size_bytes"], _row(f)["mtime"], _row(f)["fingerprint"], _row(f)["missing"]) for f in files}

    assert scanner.backfill_stream_data(limit=2)["filled"] == 2
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE sample_rate IS NOT NULL")["n"] == 2
    assert scanner.backfill_stream_data(limit=2)["filled"] == 1
    assert scanner.backfill_stream_data(limit=2) == {"candidates": 0, "filled": 0}
    assert len(canned) == 3  # the second and third runs probed nothing already filled

    for f in files:
        r = _row(f)
        assert (r["size_bytes"], r["mtime"], r["fingerprint"], r["missing"]) == before[str(f)]
        assert (r["codec"], r["sample_rate"], r["has_embedded_cover"]) == ("aac", 44100, 1)
        assert json.loads(r["embedded_json"])["chapter_count"] == 3
    g = _row(gone)
    assert g["missing"] == 1 and g["sample_rate"] is None


def test_backfill_never_probes_outside_a_root_or_ebooks(tmp_path, canned):
    base = _audio_root(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "o.m4b").write_bytes(b"x")
    (base / "link.m4b").symlink_to(outside / "o.m4b")
    wid, asset_id = _audio_work()
    _insert_unprobed(asset_id, outside / "o.m4b")
    _insert_unprobed(asset_id, base / "link.m4b")
    ebook_asset = _owned_asset(wid, "ebook")
    (base / "e.epub").write_bytes(b"x")
    _insert_unprobed(ebook_asset, base / "e.epub")
    assert scanner.backfill_stream_data()["filled"] == 0
    assert canned == []


def test_library_health_job_runs_the_backfill(tmp_path, canned):
    base = _audio_root(tmp_path)
    _, asset_id = _audio_work()
    f = base / "a.mp3"
    f.write_bytes(b"audio")
    _insert_unprobed(asset_id, f)
    result = scheduler._library_health()
    assert result["stream_data"]["filled"] == 1
    assert _row(f)["codec"] == "mp3" and _row(f)["has_embedded_cover"] == 0


def test_file_detail_endpoint_exposes_the_new_fields(tmp_path, canned):
    base = _audio_root(tmp_path)
    _, asset_id = _audio_work()
    f = base / "a.m4b"
    f.write_bytes(b"audio")
    fid = _insert_unprobed(asset_id, f)
    client = TestClient(app)
    body = client.get(f"/api/v1/files/{fid}").json()
    assert (body["codec"], body["sample_rate"], body["channels"], body["chapter_count"], body["has_embedded_cover"]) == (None,) * 5
    scanner.backfill_stream_data()
    body = client.get(f"/api/v1/files/{fid}").json()
    assert (body["codec"], body["sample_rate"], body["channels"], body["chapter_count"], body["has_embedded_cover"]) == ("aac", 44100, 2, 3, True)


def test_library_summary_files_list_includes_codec(tmp_path, canned):
    from app.main import _batch_library_summary, _library_summary

    base = _audio_root(tmp_path)
    wid, asset_id = _audio_work()
    f = base / "a.m4b"
    f.write_bytes(b"audio")
    _insert_unprobed(asset_id, f)
    work = db.one("SELECT * FROM works WHERE id=?", (wid,))
    for summarise in (lambda: _library_summary(work), lambda: _batch_library_summary([work])[0]):
        assert summarise()["assets"][0]["files"][0]["codec"] is None
    scanner.backfill_stream_data()
    for summarise in (lambda: _library_summary(work), lambda: _batch_library_summary([work])[0]):
        assert summarise()["assets"][0]["files"][0]["codec"] == "aac"


@pytest.mark.skipif(not (shutil.which("ffprobe") and shutil.which("ffmpeg")), reason="ffmpeg/ffprobe not installed")
def test_real_ffprobe_on_a_generated_silent_file(tmp_path):
    f = tmp_path / "silence.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "1", "-c:a", "aac", str(f)],
        check=True, timeout=60,
    )
    fp = scanner._ffprobe(f)
    cols = scanner.probe_columns(fp)
    assert cols == {"codec": "aac", "sample_rate": 22050, "channels": 1, "chapter_count": 0, "has_embedded_cover": 0}
