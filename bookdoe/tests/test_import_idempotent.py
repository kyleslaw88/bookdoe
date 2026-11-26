"""Import: retry after a partial failure, content verification, and the `move` guard."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import db
from app.services import downloads, importer, migration, scanner

import helpers


def add_root(path: Path, mode: str, kind: str = "audiobook") -> str:
    path.mkdir(parents=True, exist_ok=True)
    rid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,enabled,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (rid, f"{mode}-root", str(path), kind, mode, t, t),
        )
    return rid


def prepare_import(tmp_path: Path, download_id: str, names: list[str], content: dict[str, bytes] | None = None) -> Path:
    """Put completed files on disk, a managed root in place, and mark the download completed."""
    src = tmp_path / "dl" / f"payload-{download_id}"
    src.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        (src / n).write_bytes((content or {}).get(n, f"audio-{n}".encode() * 50))
    if not db.one("SELECT 1 FROM root_folders WHERE management_mode='managed'"):
        add_root(tmp_path / "library", "managed")
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET state='completed',download_path=? WHERE id=?", (str(src), download_id))
    return src


def _download(title="Retry Book"):
    from test_download_cancel import _download as make
    return make(state="completed", kind="sabnzbd", title=title)


def test_retry_after_a_failure_on_the_second_file_ends_imported_with_one_row_per_file(tmp_path, monkeypatch):
    did, work_id, asset_id = _download()
    prepare_import(tmp_path, did, ["a.m4b", "b.m4b"])
    real_inspect, calls = scanner.inspect_file, {"n": 0}

    def flaky(path, root=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk hiccup")
        return real_inspect(path, root=root)

    monkeypatch.setattr(scanner, "inspect_file", flaky)
    with pytest.raises(OSError):
        downloads.import_completed(did)
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "import_failed"
    # Half-applied no more: one file is recorded but the asset is not claimed as owned.
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 1
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] != "owned"

    result = downloads.import_completed(did)  # the retry
    assert len(result["files"]) == 2
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "imported"
    rows = db.all("SELECT path FROM media_files WHERE asset_id=?", (asset_id,))
    assert len(rows) == 2 and len({r["path"] for r in rows}) == 2
    assert not any(" (2)" in r["path"] for r in rows)  # the retry reused the earlier copy
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_importing_the_same_download_twice_does_not_duplicate_rows(tmp_path):
    did, _, asset_id = _download()
    prepare_import(tmp_path, did, ["a.m4b"])
    importer.import_download(did)
    importer.import_download(did)
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=?", (asset_id,))["n"] == 1


# ---------- B5: content verification ----------
def test_import_records_the_hash_and_fingerprint_of_what_it_copied(tmp_path):
    did, _, asset_id = _download()
    src = prepare_import(tmp_path, did, ["a.m4b"])
    importer.import_download(did)
    row = db.one("SELECT * FROM media_files WHERE asset_id=?", (asset_id,))
    assert row["sha256"] == scanner.full_sha256(src / "a.m4b")
    assert row["fingerprint"] == scanner.fingerprint(src / "a.m4b")


def test_a_corrupt_copy_with_the_right_size_is_detected(tmp_path, monkeypatch):
    did, _, _ = _download()
    prepare_import(tmp_path, did, ["a.m4b"])
    real_copy2 = importer.shutil.copy2

    def corrupting_copy(src, dst, *a, **k):
        real_copy2(src, dst, *a, **k)
        data = bytearray(Path(dst).read_bytes())
        data[10] ^= 0xFF  # same size, different bytes
        Path(dst).write_bytes(bytes(data))

    monkeypatch.setattr(importer.shutil, "copy2", corrupting_copy)
    with pytest.raises(IOError, match="Copy verification failed"):
        downloads.import_completed(did)
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0
    assert not list((tmp_path / "library").rglob("*.m4b"))
    assert not list((tmp_path / "library").rglob("*.bookdoe-part"))


def test_same_size_different_content_is_not_treated_as_the_same_file(tmp_path):
    src, dest = tmp_path / "src.m4b", tmp_path / "out" / "book.m4b"
    dest.parent.mkdir()
    src.write_bytes(b"A" * 1000)
    dest.write_bytes(b"B" * 1000)
    chosen = importer._unique_destination(dest, src)
    assert chosen == dest.with_name("book (2).m4b")
    dest.with_name("book (2).m4b").write_bytes(b"A" * 1000)
    assert importer._unique_destination(dest, src) == dest.with_name("book (2).m4b")  # identical copy is reused
    assert dest.read_bytes() == b"B" * 1000  # never overwritten


# ---------- B8: move must not delete from observed/adopted libraries ----------
@pytest.mark.parametrize("mode", ["observe", "adopt"])
def test_move_is_refused_when_the_source_is_inside_a_library_root(tmp_path, mode):
    did, _, asset_id = _download()
    lib = tmp_path / "existing"
    add_root(lib, mode)
    (lib / "pack").mkdir()
    (lib / "pack" / "a.m4b").write_bytes(b"x" * 500)
    prepare_import(tmp_path, did, [])
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET download_path=? WHERE id=?", (str(lib / "pack"), did))
    with pytest.raises(ValueError, match=f"inside the {mode} root"):
        downloads.import_completed(did, "move")
    assert (lib / "pack" / "a.m4b").exists()
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0
    assert db.one("SELECT state FROM downloads WHERE id=?", (did,))["state"] == "import_failed"


def test_move_is_refused_through_a_symlink_into_a_library_root(tmp_path):
    did, _, _ = _download()
    lib = tmp_path / "existing"
    add_root(lib, "adopt")
    (lib / "a.m4b").write_bytes(b"x" * 500)
    prepare_import(tmp_path, did, [])
    link = tmp_path / "dl" / "link"
    link.parent.mkdir(exist_ok=True)
    os.symlink(lib, link)
    with db.tx() as conn:
        conn.execute("UPDATE downloads SET download_path=? WHERE id=?", (str(link), did))
    with pytest.raises(ValueError, match="Refusing to move"):
        downloads.import_completed(did, "move")
    assert (lib / "a.m4b").exists()


def test_move_still_works_from_a_download_directory(tmp_path):
    did, _, asset_id = _download()
    add_root(tmp_path / "existing", "adopt")
    src = prepare_import(tmp_path, did, ["a.m4b"])
    importer.import_download(did, mode="move")
    assert not (src / "a.m4b").exists()
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE asset_id=?", (asset_id,))["n"] == 1


# ---------- #719: a retry after the naming inputs changed must not leave two sets ----------
def test_retry_after_the_naming_changed_ends_with_one_set_of_files(tmp_path, monkeypatch):
    did, work_id, asset_id = _download()
    names = [f"p{i:02d}.mp3" for i in range(1, 5)]
    prepare_import(tmp_path, did, names)
    real_inspect, calls = scanner.inspect_file, {"n": 0}

    def flaky(path, root=None):
        calls["n"] += 1
        if calls["n"] == 3:      # two attached, third copied but not recorded
            raise OSError("database is locked")
        return real_inspect(path, root=root)

    monkeypatch.setattr(scanner, "inspect_file", flaky)
    with pytest.raises(OSError):
        importer.import_download(did, mode="copy")
    # The work's metadata changes between the attempts, so the template renders other names.
    with db.tx() as conn:
        conn.execute("UPDATE works SET title=title||' Renamed' WHERE id=?", (work_id,))
    importer.import_download(did, mode="copy")
    rows = db.all("SELECT path FROM media_files WHERE asset_id=?", (asset_id,))
    assert len(rows) == 4
    on_disk = [p for p in (tmp_path / "library").rglob("*") if p.is_file()]
    assert len(on_disk) == 4 and {str(p) for p in on_disk} == {r["path"] for r in rows}


def test_conversion_guard_rejects_a_duplicated_part_set(tmp_path):
    from app.services import conversion
    a, b = tmp_path / "Book - 01.mp3", tmp_path / "Series 1 - Book - 01 - 2013.mp3"
    a.write_bytes(b"same audio" * 100)
    b.write_bytes(b"same audio" * 100)
    msg = conversion.duplicate_parts_problem([{"path": str(a), "part_number": 1}, {"path": str(b), "part_number": 2}])
    assert msg and "twice" in msg
    c, d = tmp_path / "c.mp3", tmp_path / "d.mp3"
    c.write_bytes(b"one" * 100)
    d.write_bytes(b"two" * 100)
    assert conversion.duplicate_parts_problem([{"path": str(c), "part_number": 1}, {"path": str(d), "part_number": 2}]) is None
    msg = conversion.duplicate_parts_problem([{"path": str(c), "part_number": 1}, {"path": str(d), "part_number": 1}])
    assert msg and "numbered" in msg
