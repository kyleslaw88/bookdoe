"""Tests for GET /api/v1/files/{file_id} (Part XXIII).

Covers all acceptance criteria from issue #37, plus the review findings:
- F1: kids-scope returns 404
- F2: symlink escape returns exact reason string
- F3: nested disabled child root inside enabled parent
- F4: outside-root file existence not reported as missing
- F5: regression test compares full response shape
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module, db, main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import files as files_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_work(title: str = "Test Book") -> tuple[str, str, str]:
    """Return (work_id, asset_id, file_id) for a single owned ebook file."""
    wid = db.uid()
    cid = db.uid()
    eid = db.uid()
    aid = db.uid()
    fid = db.uid()
    now = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
            (wid, title, title, "resolved", now, now),
        )
        c.execute(
            "INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
            (cid, "Test Author", "Author, Test", now, now),
        )
        c.execute(
            "INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
            (wid, cid, "author", 0),
        )
        c.execute(
            "INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (eid, wid, title, now, now),
        )
        c.execute(
            "INSERT INTO media_assets(id,work_id,edition_id,kind,format,status,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, wid, eid, "ebook", "epub", "owned", now, now),
        )
    return wid, aid, fid


def _insert_file(fid: str, aid: str, path: Path, *,
                 adopted: int = 0, managed: int = 0, missing: int = 0,
                 verified_at: float | None = None,
                 embedded: dict | None = None,
                 size_bytes: int | None = None,
                 mtime: float | None = None) -> None:
    now = db.now()
    try:
        rel = str(path.relative_to(path.parent.parent))
    except ValueError:
        rel = path.name
    size = size_bytes if size_bytes is not None else path.stat().st_size
    mtime_val = mtime if mtime is not None else path.stat().st_mtime
    with db.tx() as c:
        c.execute(
            """INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,
               fingerprint,container,bitrate,duration_seconds,embedded_json,adopted,managed,missing,
               discovered_at,verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, aid, str(path), rel, 1, size, mtime_val,
             "fp1", "epub", None, None, db.json_dump(embedded or {"title": "embedded title"}),
             adopted, managed, missing, now, verified_at),
        )


def _insert_root(rid: str, name: str, path: str, mode: str = "managed",
                 enabled: int = 1) -> None:
    now = db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, name, path, "ebook", mode, 1, enabled, now, now),
        )


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

class TestIsMutable:
    def test_managed_root_is_mutable(self, tmp_path):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Managed", str(root))
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is True
        assert reason == ""

    def test_adopt_root_is_not_mutable(self, tmp_path):
        root = tmp_path / "adopt"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Adopt", str(root), mode="adopt")
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is False
        assert "adopt" in reason.lower()

    def test_observe_root_is_not_mutable(self, tmp_path):
        root = tmp_path / "observe"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Observe", str(root), mode="observe")
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is False
        assert "observe" in reason.lower()

    def test_disabled_root_is_not_mutable(self, tmp_path):
        root = tmp_path / "disabled"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Disabled", str(root), enabled=0)
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is False
        assert "disabled" in reason.lower()

    def test_outside_any_root_is_not_mutable(self, tmp_path):
        book = tmp_path / "book.epub"
        book.write_bytes(b"x")
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is False
        assert "outside" in reason.lower()

    def test_symlink_escape_is_not_mutable(self, tmp_path):
        """F2: symlink escape returns exact 'symlink escape' reason and identifies the root."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "book.epub"
        target.write_bytes(b"x")
        link = root / "book.epub"
        link.symlink_to(target)
        rid = db.uid()
        _insert_root(rid, "Root", str(root))
        mutable, reason = files_service.is_mutable({"path": str(link)})
        assert mutable is False
        assert reason == "symlink escape"
        # Verify the service-level resolution identifies the root
        result = files_service._resolve_root(str(link))
        assert result.root is not None
        assert result.root["id"] == rid
        assert result.reason == "symlink escape"

    def test_longest_root_wins(self, tmp_path):
        parent = tmp_path / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        book = child / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Parent", str(parent), mode="adopt")
        _insert_root(db.uid(), "Child", str(child), mode="managed")
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is True
        assert reason == ""

    # F3: nested disabled child root inside enabled parent
    def test_nested_disabled_child_root(self, tmp_path):
        """A file under a disabled child root nested inside an enabled parent
        should report 'root disabled' (not 'adopted root' from the parent)."""
        parent = tmp_path / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        book = child / "book.epub"
        book.write_bytes(b"x")
        _insert_root(db.uid(), "Parent", str(parent), mode="adopt", enabled=1)
        _insert_root(db.uid(), "Child", str(child), mode="managed", enabled=0)
        mutable, reason = files_service.is_mutable({"path": str(book)})
        assert mutable is False
        assert "disabled" in reason.lower()


# ---------------------------------------------------------------------------

class TestFileDetailEndpoint:
    def test_managed_root_returns_mutable_true(self, tmp_path, client):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, verified_at=now)
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mutable"] is True
        assert body["root"]["management_mode"] == "managed"
        assert body["root"]["name"] == "Managed"
        assert body["integrity"]["state"] == "ok"

    def test_adopt_root_returns_mutable_false_with_reason(self, tmp_path, client):
        root = tmp_path / "adopt"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, adopted=1)
        _insert_root(rid, "Adopt", str(root), mode="adopt")

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mutable"] is False
        assert "adopt" in body["mutable_reason"].lower()
        assert body["root"]["management_mode"] == "adopt"

    def test_outside_root_returns_null_root(self, tmp_path, client):
        book = tmp_path / "book.epub"
        book.write_bytes(b"x")
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book)

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["root"] is None
        assert body["mutable"] is False
        assert "outside" in body["mutable_reason"].lower()

    def test_missing_file_returns_integrity_missing(self, tmp_path, client):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, missing=1)
        _insert_root(rid, "Managed", str(root))
        book.unlink()

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["integrity"]["state"] == "missing"
        assert body["missing"] is True

    def test_absent_on_disk_returns_integrity_missing(self, tmp_path, client):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        # Do not create the file on disk.
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, size_bytes=0, mtime=now)
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["integrity"]["state"] == "missing"

    def test_unknown_id_returns_404(self, client):
        r = client.get("/api/v1/files/does-not-exist")
        assert r.status_code == 404

    def test_embedded_is_decoded_object(self, tmp_path, client):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, embedded={"title": "From File", "chapters": 12})
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["embedded"] == {"title": "From File", "chapters": 12}
        assert isinstance(body["embedded"], dict)

    def test_response_fields(self, tmp_path, client):
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work(title="Response Fields Book")
        _insert_file(fid, aid, book, verified_at=now)
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == fid
        assert body["path"] == str(book)
        assert body["relative_path"] is not None
        assert body["part_number"] == 1
        assert body["size_bytes"] == book.stat().st_size
        assert body["mtime"] is not None
        assert body["sha256"] is None
        assert body["fingerprint"] == "fp1"
        assert body["container"] == "epub"
        assert body["codec"] is None
        assert body["bitrate"] is None
        assert body["duration_seconds"] is None
        assert body["adopted"] is False
        assert body["managed"] is False
        assert body["missing"] is False
        assert body["discovered_at"] is not None
        assert body["verified_at"] == now
        assert body["asset"]["id"] == aid
        assert body["asset"]["kind"] == "ebook"
        assert body["asset"]["format"] == "epub"
        assert body["asset"]["status"] == "owned"
        assert body["work"]["id"] == wid
        assert body["work"]["title"] == "Response Fields Book"
        assert body["root"]["id"] == rid
        assert body["root"]["name"] == "Managed"
        assert body["root"]["management_mode"] == "managed"
        assert body["mutable"] is True
        assert body["integrity"]["state"] == "ok"
        assert body["integrity"]["verified_at"] == now

    # F1: kids-scope returns 404
    def test_kids_scope_returns_404(self, tmp_path, client, monkeypatch):
        """F1: kids-scope users get 404, not 403, for file detail."""
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, verified_at=now)
        _insert_root(rid, "Managed", str(root))

        # Switch to local auth mode so we can create users.
        replaced = dataclasses.replace(base_settings, auth_mode="local")
        monkeypatch.setattr(auth_module, "settings", replaced)
        monkeypatch.setattr(main_module, "settings", replaced)

        # Create a kids user and log in
        admin_client = TestClient(app)
        r = admin_client.post("/api/v1/auth/setup", json={
            "username": "admin", "password": "a-very-long-admin-password"
        })
        assert r.status_code == 200, r.text
        admin_csrf = r.json()["csrf"]

        r = admin_client.post(
            "/api/v1/users",
            json={"username": "junior", "password": "a-very-long-kid-password", "access_scope": "kids"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert r.status_code == 200, r.text

        kid = TestClient(app)
        r = kid.post("/api/v1/auth/login", json={
            "username": "junior", "password": "a-very-long-kid-password"
        })
        assert r.status_code == 200, r.text

        # Kids user should get 404, not 403
        r = kid.get(f"/api/v1/files/{fid}")
        assert r.status_code == 404, f"Expected 404 for kids user, got {r.status_code}: {r.text}"

    # F4: file outside enabled root that exists on disk should not report missing
    def test_outside_root_existing_file_not_missing(self, tmp_path, client):
        """F4: A file outside every enabled root that exists on disk should
        NOT report integrity.state='missing' just because no root matched."""
        book = tmp_path / "book.epub"
        book.write_bytes(b"x")
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, verified_at=db.now())

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        # root is None because no root matched
        assert body["root"] is None
        # The file exists on disk, so integrity should NOT be 'missing'
        # It should be 'ok' since verified_at is set
        assert body["integrity"]["state"] == "ok"

    # F4: file under disabled root that exists on disk should not report missing
    def test_disabled_root_existing_file_not_missing(self, tmp_path, client):
        """F4: A file under a disabled root that exists on disk should
        NOT report integrity.state='missing' just because the root is disabled."""
        root = tmp_path / "disabled"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work()
        _insert_file(fid, aid, book, verified_at=now)
        _insert_root(rid, "Disabled", str(root), enabled=0)

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()
        # The file exists on disk, so integrity should NOT be 'missing'
        assert body["integrity"]["state"] == "ok"
        # But mutable should be false with 'root disabled' reason
        assert body["mutable"] is False
        assert "disabled" in body["mutable_reason"].lower()


# ---------------------------------------------------------------------------
# Regression: library detail response unchanged (F5)
# ---------------------------------------------------------------------------

class TestLibraryDetailRegression:
    def test_library_detail_files_equality(self, tmp_path, client):
        """F5: Regression — the library detail endpoint returns the same
        trimmed file shape as before.  Compares the complete files entry
        with == against a fixed expected dict built from known seeded values
        (ids, path, size, container, etc.).

        This is a byte-for-byte equality check on the files list, not just
        a key-set or type assertion.  Any accidental addition or removal of
        fields in _library_summary is caught immediately.
        """
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        wid, aid, fid = _seed_work(title="Regression Equality Book")
        _insert_file(fid, aid, book)
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/library/{wid}")
        assert r.status_code == 200, r.text
        work = r.json()

        # The work summary must have an 'assets' list
        assert "assets" in work
        assert len(work["assets"]) == 1

        files = work["assets"][0]["files"]
        assert len(files) == 1
        f = files[0]

        # Build the exact expected dict from the known seeded values.
        # The trimmed shape from _library_summary is:
        #   id, path, part_number, size_bytes, container, codec, bitrate,
        #   duration_seconds, adopted, managed, missing
        expected_file = {
            "id": fid,
            "path": str(book),
            "part_number": 1,
            "size_bytes": 1,  # b"x" is 1 byte
            "container": "epub",
            "codec": None,
            "bitrate": None,
            "duration_seconds": None,
            "adopted": 0,
            "managed": 0,
            "missing": 0,
            # additive (compact file lists): the format and whether the reader can open it
            "format": "epub",
            "readable": True,
        }
        assert f == expected_file, (
            f"Files entry mismatch.\n"
            f"  expected: {expected_file}\n"
            f"  actual:   {f}"
        )

    def test_file_detail_equality(self, tmp_path, client):
        """F5: The full file detail response matches a fixed expected dict
        built from known seeded values.  Compares the entire response body
        with == against the expected structure (values checked for exact
        equality, not just type).

        We seed known values, read the response, then build an expected dict
        from those same known values and assert equality.  This proves the
        endpoint returns exactly what we seeded — no extra fields, no missing
        fields, no type coercion surprises.
        """
        root = tmp_path / "managed"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"x")
        rid = db.uid()
        now = db.now()
        wid, aid, fid = _seed_work(title="File Detail Equality Book")
        _insert_file(fid, aid, book, verified_at=now)
        _insert_root(rid, "Managed", str(root))

        r = client.get(f"/api/v1/files/{fid}")
        assert r.status_code == 200, r.text
        body = r.json()

        # Read back computed values from the DB so the expected dict
        # matches exactly what the endpoint returns.
        file_row = db.one(
            "SELECT id,path,relative_path,part_number,size_bytes,mtime,discovered_at "
            "FROM media_files WHERE id=?", (fid,),
        )

        expected_body = {
            "id": fid,
            "path": str(book),
            "relative_path": file_row["relative_path"],
            "part_number": 1,
            "size_bytes": 1,  # b"x" is 1 byte
            "mtime": book.stat().st_mtime,
            "sha256": None,
            "fingerprint": "fp1",
            "container": "epub",
            "codec": None,
            "sample_rate": None,
            "channels": None,
            "chapter_count": None,
            "has_embedded_cover": None,
            "bitrate": None,
            "duration_seconds": None,
            "adopted": False,
            "managed": False,
            "missing": False,
            "discovered_at": file_row["discovered_at"],
            "verified_at": now,
            "embedded": {"title": "embedded title"},
            "asset": {
                "id": aid,
                "kind": "ebook",
                "format": "epub",
                "status": "owned",
            },
            "work": {
                "id": wid,
                "title": "File Detail Equality Book",
            },
            "root": {
                "id": rid,
                "name": "Managed",
                "management_mode": "managed",
            },
            "mutable": True,
            "mutable_reason": "",
            "integrity": {
                "state": "ok",
                "verified_at": now,
                "detail": {},
            },
        }
        assert body == expected_body, (
            f"File detail response mismatch.\n"
            f"  expected: {expected_body}\n"
            f"  actual:   {body}"
        )
