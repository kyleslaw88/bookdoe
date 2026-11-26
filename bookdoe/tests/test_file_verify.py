"""Verify and rescan one MediaFile (P5-14, Part XXIII)."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import fileverify, scanner


def _seed(path: Path, root_mode: str = "managed", *, in_root: bool = True, kind: str = "ebook") -> str:
    """Insert a root (unless in_root is False), asset and a media_files row from the real file."""
    now = db.now()
    wid, aid, fid = db.uid(), db.uid(), db.uid()
    detected = scanner.inspect_file(path, path.parent)
    pc = scanner.probe_columns(detected)
    with db.tx() as c:
        if in_root:
            c.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (db.uid(), "Root", str(path.parent), kind, root_mode, 1, 1, now, now),
            )
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, "T", "T", "resolved", now, now))
        c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                  (aid, wid, kind, path.suffix.lstrip("."), "owned", now, now))
        c.execute(
            """INSERT INTO media_files(id,asset_id,path,size_bytes,mtime,fingerprint,container,codec,sample_rate,channels,chapter_count,
               has_embedded_cover,duration_seconds,embedded_json,adopted,managed,missing,discovered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (fid, aid, str(path), detected["size_bytes"], detected["mtime"], detected["fingerprint"], detected["extension"],
             pc["codec"], pc["sample_rate"], pc["channels"], pc["chapter_count"], pc["has_embedded_cover"],
             detected.get("duration_seconds"), db.json_dump(detected), int(root_mode == "adopt"), int(root_mode == "managed"), now),
        )
    return fid


def _epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("OEBPS/content.opf", "<package/>")
    return path


def _row(fid: str) -> dict:
    return db.one("SELECT * FROM media_files WHERE id=?", (fid,))


def _check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


def _tree(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


def test_untouched_file_is_ok_and_sets_verified_at(tmp_path):
    fid = _seed(_epub(tmp_path / "b.epub"))
    assert _row(fid)["integrity_state"] is None and _row(fid)["verified_at"] is None
    result = fileverify.verify_file(fid)
    assert result["state"] == "ok"
    assert {"exists", "size", "mtime", "fingerprint", "container"} <= {c["name"] for c in result["checks"]}
    row = _row(fid)
    assert row["integrity_state"] == "ok" and row["verified_at"] is not None
    assert db.json_load(row["integrity_detail_json"], {})["checks"] == result["checks"]


def test_appended_byte_is_changed_then_rescan_makes_it_ok(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    before = _row(fid)
    with path.open("ab") as f:
        f.write(b"x")
    result = fileverify.verify_file(fid)
    assert result["state"] == "changed"
    assert _check(result, "size")["ok"] is False and _check(result, "fingerprint")["ok"] is False
    after = _row(fid)
    assert (after["size_bytes"], after["fingerprint"]) == (before["size_bytes"], before["fingerprint"])  # verify never overwrites

    assert fileverify.rescan_file(fid)["rescanned"] is True
    rescanned = _row(fid)
    assert rescanned["size_bytes"] == before["size_bytes"] + 1 and rescanned["fingerprint"] != before["fingerprint"]
    assert fileverify.verify_file(fid)["state"] == "ok"


def test_deep_verify_compares_stored_sha256(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    assert "sha256" not in {c["name"] for c in fileverify.verify_file(fid, deep=True)["checks"] if c["ok"] is not None}
    with db.tx() as c:
        c.execute("UPDATE media_files SET sha256=? WHERE id=?", (scanner.full_sha256(path), fid))
    assert fileverify.verify_file(fid, deep=True)["state"] == "ok"
    assert "sha256" not in {c["name"] for c in fileverify.verify_file(fid)["checks"]}
    with db.tx() as c:
        c.execute("UPDATE media_files SET sha256='bad' WHERE id=?", (fid,))
    result = fileverify.verify_file(fid, deep=True)
    assert result["state"] == "changed" and _check(result, "sha256")["ok"] is False


def test_deleted_file_is_missing_and_restored_file_clears_it(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    data = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    path.unlink()
    assert fileverify.verify_file(fid)["state"] == "missing"
    assert _row(fid)["missing"] == 1 and _row(fid)["integrity_state"] == "missing"
    path.write_bytes(data)
    os.utime(path, ns=(mtime, mtime))
    assert fileverify.verify_file(fid)["state"] == "ok"
    assert _row(fid)["missing"] == 0


def test_rescan_of_deleted_file_sets_missing(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    path.unlink()
    out = fileverify.rescan_file(fid)
    assert out["rescanned"] is False and out["verify"]["state"] == "missing"
    assert _row(fid)["missing"] == 1


def test_truncated_epub_is_damaged(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    with db.tx() as c:  # the row describes the truncated file, so only the container is wrong
        detected = scanner.inspect_file(path, tmp_path)
        c.execute("UPDATE media_files SET size_bytes=?,mtime=?,fingerprint=? WHERE id=?",
                  (detected["size_bytes"], detected["mtime"], detected["fingerprint"], fid))
    result = fileverify.verify_file(fid)
    assert result["state"] == "damaged" and _check(result, "container")["ok"] is False
    assert _row(fid)["integrity_state"] == "damaged"


def test_epub_without_container_xml_is_damaged(tmp_path):
    path = tmp_path / "b.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
    fid = _seed(path)
    assert fileverify.verify_file(fid)["state"] == "damaged"


@pytest.mark.parametrize("probe,state", [
    ({"duration_seconds": 120}, "ok"),
    ({"duration_seconds": None}, "damaged"),
    ({}, "damaged"),
])
def test_audio_needs_a_successful_probe_with_duration(tmp_path, monkeypatch, probe, state):
    path = tmp_path / "b.m4b"
    path.write_bytes(b"audio")
    monkeypatch.setattr(scanner, "_ffprobe", lambda p: probe)
    fid = _seed(path, kind="audiobook")
    assert fileverify.verify_file(fid)["state"] == state


def test_path_outside_every_root_is_not_read(tmp_path, monkeypatch):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path, in_root=False)

    def boom(*a, **k):
        raise AssertionError("read a path outside every root")
    monkeypatch.setattr(scanner, "fingerprint", boom)
    monkeypatch.setattr(scanner, "inspect_file", boom)
    result = fileverify.verify_file(fid)
    assert result["state"] == "changed" and result["checks"] == [{"name": "outside_root", "ok": False}]
    assert _row(fid)["missing"] == 0
    assert fileverify.rescan_file(fid)["rescanned"] is False


def test_symlink_escaping_the_root_is_outside_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    real = _epub(outside / "b.epub")
    (root / "link.epub").symlink_to(real)
    now = db.now()
    fid = _seed(real, in_root=False)
    with db.tx() as c:
        c.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (db.uid(), "R", str(root), "ebook", "adopt", 1, 1, now, now))
        c.execute("UPDATE media_files SET path=? WHERE id=?", (str(root / "link.epub"), fid))
    assert fileverify.verify_file(fid)["checks"] == [{"name": "outside_root", "ok": False}]


def test_unknown_id_returns_none():
    assert fileverify.verify_file("nope") is None and fileverify.rescan_file("nope") is None


@pytest.mark.parametrize("mode", ["adopt", "observe"])
def test_verify_and_rescan_never_change_adopted_or_observed_tree(tmp_path, mode):
    lib = tmp_path / "lib"
    (lib / "Author").mkdir(parents=True)
    path = _epub(lib / "Author" / "b.epub")
    fid = _seed(path, root_mode=mode)
    with path.open("ab") as f:
        f.write(b"x")
    snapshot = _tree(lib)
    assert fileverify.verify_file(fid, deep=True)["state"] == "changed"
    assert fileverify.rescan_file(fid)["rescanned"] is True
    assert fileverify.verify_file(fid)["state"] == "ok"
    assert _tree(lib) == snapshot


def test_rescan_refreshes_stream_columns_and_embedded_json(tmp_path, monkeypatch):
    path = tmp_path / "b.m4b"
    path.write_bytes(b"audio")
    probe = {"duration_seconds": 60, "bitrate": 64000, "codec": "aac", "sample_rate": 44100, "channels": 1,
             "chapter_count": 2, "has_embedded_cover": 0, "title": "One"}
    monkeypatch.setattr(scanner, "_ffprobe", lambda p: dict(probe))
    fid = _seed(path, kind="audiobook")
    probe.update(duration_seconds=90, bitrate=96000, codec="mp3", sample_rate=48000, channels=2, chapter_count=5, title="Two")
    path.write_bytes(b"audio longer")
    assert fileverify.rescan_file(fid)["verify"]["state"] == "ok"
    row = _row(fid)
    assert (row["duration_seconds"], row["bitrate"], row["codec"], row["sample_rate"], row["channels"], row["chapter_count"]) == (90, 96000, "mp3", 48000, 2, 5)
    assert db.json_load(row["embedded_json"], {})["title"] == "Two"


def test_routes_verify_and_rescan_and_detail_exposes_integrity(tmp_path):
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    client = TestClient(app)
    assert client.get(f"/api/v1/files/{fid}").json()["integrity"]["state"] == "unverified"
    r = client.post(f"/api/v1/files/{fid}/verify?deep=true")
    assert r.status_code == 200 and r.json()["state"] == "ok"
    integrity = client.get(f"/api/v1/files/{fid}").json()["integrity"]
    assert integrity["state"] == "ok" and integrity["detail"]["deep"] is True and integrity["verified_at"]
    with path.open("ab") as f:
        f.write(b"x")
    assert client.post(f"/api/v1/files/{fid}/verify").json()["state"] == "changed"
    assert client.get(f"/api/v1/files/{fid}").json()["integrity"]["state"] == "changed"
    r = client.post(f"/api/v1/files/{fid}/rescan")
    assert r.status_code == 200 and r.json()["rescanned"] is True and r.json()["verify"]["state"] == "ok"
    assert client.post("/api/v1/files/nope/verify").status_code == 404
    assert client.post("/api/v1/files/nope/rescan").status_code == 404


def _asset_status(fid: str) -> str:
    return db.one("SELECT a.status FROM media_assets a JOIN media_files f ON f.asset_id=a.id WHERE f.id=?", (fid,))["status"]


def test_last_file_missing_clears_owned_and_returning_file_restores_it(tmp_path):
    from app.services import scheduler
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    data = path.read_bytes()
    path.unlink()
    scheduler._library_health()
    assert _row(fid)["missing"] == 1
    assert _asset_status(fid) == "none"
    path.write_bytes(data)
    scheduler._library_health()
    assert _asset_status(fid) == "owned"


def test_one_of_two_files_missing_stays_owned(tmp_path):
    from app.services import scheduler
    a = _epub(tmp_path / "a.epub")
    fid = _seed(a)
    b = _epub(tmp_path / "b.epub")
    aid = db.one("SELECT asset_id FROM media_files WHERE id=?", (fid,))["asset_id"]
    with db.tx() as c:
        c.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,missing,discovered_at) VALUES (?,?,?,?,0,?)",
                  (db.uid(), aid, str(b), b.stat().st_size, db.now()))
    b.unlink()
    scheduler._library_health()
    assert _asset_status(fid) == "owned"


def test_verify_of_deleted_last_file_clears_and_stale_owned_row_is_corrected(tmp_path):
    from app.services import maintenance
    path = _epub(tmp_path / "b.epub")
    fid = _seed(path)
    with db.tx() as c:  # stale row as production had it: flagged missing, still owned
        c.execute("UPDATE media_files SET missing=1 WHERE id=?", (fid,))
    assert _asset_status(fid) == "owned"
    maintenance.scan_job()
    assert _asset_status(fid) == "none"
