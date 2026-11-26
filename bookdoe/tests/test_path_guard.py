"""Tests for the path guard service (F-05).

Covers:
- Root validation: absolute, exists, not /, not inside data dir, no overlap.
- Path resolution: ``..`` traversal rejected, symlink escapes rejected.
- Safe scanning: symlinks pointing outside root are skipped.
- OPDS file serving: files outside roots are rejected.
- Root creation via API: 422 on invalid roots.
"""
from pathlib import Path
import tempfile
import os

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import paths as path_guard


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_root(tmp_path):
    """A temporary directory used as a library root."""
    return tmp_path / "library"


@pytest.fixture
def valid_root(tmp_root):
    """Create the directory so it exists."""
    tmp_root.mkdir(parents=True)
    return tmp_root


@pytest.fixture
def data_dir(tmp_path):
    """A temporary data directory."""
    d = tmp_path / "data"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# validate_root tests
# ---------------------------------------------------------------------------

class TestValidateRoot:
    """Unit tests for path_guard.validate_root()."""

    def test_non_absolute_rejected(self, tmp_path):
        errors = path_guard.validate_root("relative/path")
        assert errors
        assert "absolute" in errors[0].lower()

    def test_nonexistent_rejected(self, tmp_path):
        errors = path_guard.validate_root(str(tmp_path / "does_not_exist"))
        assert errors
        assert "not exist" in errors[0].lower()

    def test_not_directory_rejected(self, tmp_path):
        f = tmp_path / "a_file"
        f.touch()
        errors = path_guard.validate_root(str(f))
        assert errors
        assert "not a directory" in errors[0].lower()

    def test_root_slash_rejected(self):
        errors = path_guard.validate_root("/")
        assert errors
        assert "filesystem root" in errors[0].lower() or "cannot be" in errors[0].lower()

    def test_inside_data_dir_rejected(self, tmp_path, data_dir):
        inside = tmp_path / "inside_data"
        inside.mkdir()
        errors = path_guard.validate_root(str(inside), data_dir=data_dir)
        # inside is NOT inside data_dir, so it should pass this check
        # Let's put it inside data_dir
        inside2 = data_dir / "subdir"
        inside2.mkdir()
        errors = path_guard.validate_root(str(inside2), data_dir=data_dir)
        assert errors
        assert "data directory" in errors[0].lower()

    def test_no_overlap_with_self(self, valid_root):
        errors = path_guard.validate_root(
            str(valid_root),
            existing_roots=[{"id": "r1", "name": "existing", "path": str(valid_root)}],
        )
        assert errors
        assert "overlaps" in errors[0].lower() or "inside" in errors[0].lower()

    def test_no_overlap_nested(self, tmp_path):
        parent = tmp_path / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        errors = path_guard.validate_root(
            str(child),
            existing_roots=[{"id": "r1", "name": "parent", "path": str(parent)}],
        )
        assert errors
        assert "inside" in errors[0].lower()

    def test_no_overlap_parent_of_existing(self, tmp_path):
        parent = tmp_path / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        errors = path_guard.validate_root(
            str(parent),
            existing_roots=[{"id": "r1", "name": "child", "path": str(child)}],
        )
        assert errors
        assert "inside" in errors[0].lower()

    def test_valid_root_passes(self, valid_root):
        errors = path_guard.validate_root(
            str(valid_root),
            data_dir=Path("/config"),
            existing_roots=[{"id": "r1", "name": "other", "path": str(valid_root.parent / "other")}],
        )
        assert errors == []

    def test_valid_root_no_existing_roots(self, valid_root):
        errors = path_guard.validate_root(str(valid_root))
        assert errors == []


# ---------------------------------------------------------------------------
# resolve_safe tests
# ---------------------------------------------------------------------------

class TestResolveSafe:
    """Unit tests for path_guard.resolve_safe()."""

    def test_normal_file_inside_root(self, valid_root):
        f = valid_root / "book.epub"
        f.touch()
        result = path_guard.resolve_safe(valid_root, f)
        assert result is not None
        assert result == f.resolve()

    def test_nested_file_inside_root(self, valid_root):
        subdir = valid_root / "subdir"
        subdir.mkdir()
        f = subdir / "book.epub"
        f.touch()
        result = path_guard.resolve_safe(valid_root, f)
        assert result is not None

    def test_dotdot_traversal_rejected(self, valid_root):
        """A path containing .. that escapes the root must be rejected."""
        outside = valid_root.parent / "outside"
        outside.mkdir(exist_ok=True)
        # Construct a path that uses .. to escape
        escaped = valid_root / ".." / "outside" / "file.txt"
        result = path_guard.resolve_safe(valid_root, escaped)
        assert result is None

    def test_dotdot_to_parent_rejected(self, valid_root):
        escaped = valid_root / ".." / "file.txt"
        result = path_guard.resolve_safe(valid_root, escaped)
        assert result is None

    def test_symlink_escape_rejected(self, tmp_path, valid_root):
        """A symlink pointing outside the root must be rejected."""
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "secret.txt"
        target.touch()
        link = valid_root / "link_to_outside"
        link.symlink_to(target)
        result = path_guard.resolve_safe(valid_root, link)
        assert result is None

    def test_symlink_inside_is_allowed(self, valid_root):
        """A symlink pointing inside the root is allowed."""
        target = valid_root / "real_file.txt"
        target.touch()
        link = valid_root / "link_to_real"
        link.symlink_to(target)
        result = path_guard.resolve_safe(valid_root, link)
        assert result is not None

    def test_absolute_path_outside_rejected(self, valid_root):
        result = path_guard.resolve_safe(valid_root, Path("/etc/passwd"))
        assert result is None

    def test_relative_path_outside_rejected(self, valid_root):
        result = path_guard.resolve_safe(valid_root, Path("../etc/passwd"))
        assert result is None


# ---------------------------------------------------------------------------
# scan_safe tests
# ---------------------------------------------------------------------------

class TestScanSafe:
    """Unit tests for path_guard.scan_safe()."""

    def test_normal_files_yielded(self, valid_root):
        (valid_root / "book1.epub").touch()
        (valid_root / "book2.epub").touch()
        files = list(path_guard.scan_safe(valid_root))
        assert len(files) == 2

    def test_nested_files_yielded(self, valid_root):
        subdir = valid_root / "subdir"
        subdir.mkdir()
        (valid_root / "book1.epub").touch()
        (subdir / "book2.epub").touch()
        files = list(path_guard.scan_safe(valid_root))
        # scan_safe yields all files (not just media), caller filters by extension
        assert len(files) == 2

    def test_symlink_to_outside_skipped(self, tmp_path, valid_root):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.epub").touch()
        link = valid_root / "escape_link"
        link.symlink_to(outside)
        files = list(path_guard.scan_safe(valid_root))
        # The symlink directory should be skipped entirely
        assert len(files) == 0

    def test_symlink_file_to_outside_skipped(self, tmp_path, valid_root):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.epub").touch()
        link = valid_root / "escape.epub"
        link.symlink_to(outside / "secret.epub")
        files = list(path_guard.scan_safe(valid_root))
        assert len(files) == 0

    def test_symlink_inside_is_included(self, valid_root):
        real = valid_root / "real.epub"
        real.touch()
        link = valid_root / "link.epub"
        link.symlink_to(real)
        files = list(path_guard.scan_safe(valid_root))
        # Both the real file and the symlink resolve to the same file,
        # but scan_safe yields each path it encounters that resolves inside
        assert len(files) >= 1

    def test_non_media_files_yielded(self, valid_root):
        """scan_safe yields ALL files; the caller (scanner/repair) filters by extension."""
        (valid_root / "readme.txt").touch()
        (valid_root / "book.epub").touch()
        files = list(path_guard.scan_safe(valid_root))
        # scan_safe yields all files, not just media
        assert len(files) == 2

    def test_broken_symlink_skipped(self, valid_root):
        link = valid_root / "broken_link"
        link.symlink_to(valid_root / "nonexistent")
        files = list(path_guard.scan_safe(valid_root))
        assert len(files) == 0


# ---------------------------------------------------------------------------
# API root creation tests
# ---------------------------------------------------------------------------

class TestRootCreation:
    """Integration tests for root creation via the API."""

    def test_create_valid_root(self, tmp_path, client):
        root = tmp_path / "valid_library"
        root.mkdir()
        r = client.post(
            "/api/v1/roots",
            json={
                "name": "Valid Library",
                "path": str(root),
                "media_kind": "ebook",
            },
        )
        assert r.status_code == 200, r.text

    def test_create_root_slash_rejected(self, client):
        r = client.post(
            "/api/v1/roots",
            json={
                "name": "Root",
                "path": "/",
                "media_kind": "ebook",
            },
        )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"

    def test_create_root_relative_rejected(self, client):
        r = client.post(
            "/api/v1/roots",
            json={
                "name": "Relative",
                "path": "relative/path",
                "media_kind": "ebook",
            },
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"

    def test_create_root_nonexistent_rejected(self, tmp_path, client):
        r = client.post(
            "/api/v1/roots",
            json={
                "name": "Ghost",
                "path": str(tmp_path / "bookdoe_test_nonexistent_12345"),
                "media_kind": "ebook",
            },
        )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"

    def test_create_overlapping_root_rejected(self, tmp_path, client):
        parent = tmp_path / "parent"
        child = parent / "child"
        parent.mkdir()
        child.mkdir()
        # Create parent first
        r1 = client.post(
            "/api/v1/roots",
            json={"name": "Parent", "path": str(parent), "media_kind": "ebook"},
        )
        assert r1.status_code == 200, r1.text
        # Try to create child as a separate root
        r2 = client.post(
            "/api/v1/roots",
            json={"name": "Child", "path": str(child), "media_kind": "ebook"},
        )
        assert r2.status_code == 422, f"Expected 422, got {r2.status_code}: {r2.text}"


# ---------------------------------------------------------------------------
# OPDS file serving tests
# ---------------------------------------------------------------------------

class TestOPDSFileServing:
    """Integration tests for OPDS file serving path guard."""

    def test_file_inside_root_is_served(self, tmp_path, client):
        """A file inside a known root should be served."""
        root = tmp_path / "library"
        root.mkdir()
        book = root / "book.epub"
        book.write_bytes(b"fake epub content")

        # Seed the database with a root and a media file
        wid = db.uid()
        eid = db.uid()
        cid = db.uid()
        aid = db.uid()
        fid = db.uid()
        rid = db.uid()
        now = db.now()

        with db.tx() as c:
            c.execute(
                "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                (wid, "Test Book", "Test Book", "resolved", now, now),
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
                (eid, wid, "Test Book", now, now),
            )
            c.execute(
                "INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                (aid, wid, "ebook", "owned", now, now),
            )
            c.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, "Test Root", str(root), "ebook", "adopt", 1, 1, now, now),
            )
            c.execute(
                "INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,fingerprint,container,bitrate,duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, aid, str(book), "book.epub", 1, book.stat().st_size, book.stat().st_mtime, "fp1", "epub", None, None, "{}", 0, 0, 0, now, now),
            )

        r = client.get(f"/opds/files/{fid}")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_file_outside_root_rejected(self, tmp_path, client):
        """A file outside any known root should be rejected with 403."""
        root = tmp_path / "library"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        book = outside / "book.epub"
        book.write_bytes(b"fake epub content")

        wid = db.uid()
        eid = db.uid()
        cid = db.uid()
        aid = db.uid()
        fid = db.uid()
        rid = db.uid()
        now = db.now()

        with db.tx() as c:
            c.execute(
                "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                (wid, "Test Book", "Test Book", "resolved", now, now),
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
                (eid, wid, "Test Book", now, now),
            )
            c.execute(
                "INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                (aid, wid, "ebook", "owned", now, now),
            )
            c.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, "Test Root", str(root), "ebook", "adopt", 1, 1, now, now),
            )
            c.execute(
                "INSERT INTO media_files(id,asset_id,path,relative_path,part_number,size_bytes,mtime,fingerprint,container,bitrate,duration_seconds,embedded_json,adopted,managed,missing,discovered_at,verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, aid, str(book), "../outside/book.epub", 1, book.stat().st_size, book.stat().st_mtime, "fp1", "epub", None, None, "{}", 0, 0, 0, now, now),
            )

        r = client.get(f"/opds/files/{fid}")
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


# --- Overseer review additions (2026-10-03) -------------------------------------------

def test_scan_safe_does_not_follow_directory_symlink_cycles(tmp_path):
    from app.services import paths as pg
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "book.epub").write_text("x")
    (root / "a" / "loop").symlink_to(root)          # cycle back to the root
    (root / "inside").symlink_to(root / "a")        # in-root dir symlink: not followed either
    found = sorted(p.relative_to(root).as_posix() for p in pg.scan_safe(root))
    assert found == ["a/book.epub"]


def test_scan_safe_skips_unreadable_directory(tmp_path, monkeypatch):
    from app.services import paths as pg
    root = tmp_path / "root"
    (root / "ok").mkdir(parents=True)
    (root / "ok" / "b.m4b").write_text("x")
    (root / "locked").mkdir()
    real_iterdir = type(root).iterdir
    def fake_iterdir(self):
        if self.name == "locked":
            raise PermissionError("denied")
        return real_iterdir(self)
    monkeypatch.setattr(type(root), "iterdir", fake_iterdir)
    found = [p.name for p in pg.scan_safe(root)]
    assert found == ["b.m4b"]


def test_validate_root_normalises_dotdot_into_data_dir(tmp_path):
    from app.services import paths as pg
    data = tmp_path / "config"; data.mkdir()
    books = tmp_path / "books"; books.mkdir()
    errors = pg.validate_root(str(books / ".." / "config"), data_dir=data, existing_roots=[])
    assert any("data directory" in e for e in errors)


def test_validate_root_resolves_symlink_into_existing_root(tmp_path):
    from app.services import paths as pg
    books = tmp_path / "books"; (books / "sub").mkdir(parents=True)
    alias = tmp_path / "alias"; alias.symlink_to(books / "sub")
    errors = pg.validate_root(str(alias), data_dir=tmp_path / "cfg", existing_roots=[{"name": "Ebooks", "path": str(books)}])
    assert any("inside existing root" in e for e in errors)
