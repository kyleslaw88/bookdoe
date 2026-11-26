"""Podcasts library type (#513): create + scan through the real entry points (setup.create_root, scanner.scan_root), no network."""
from __future__ import annotations

import pytest

from app import db, setup
from app.services import scanner

from tests.test_podcastmeta import _episode, _text
from tests.test_scanner import make_epub, seed_work


def _tree(root):
    _episode(root, "Alpha/Season 01/001 - Pilot.mp3", _text("TDRC", "2024-01-01"))
    _episode(root, "Alpha/Season 01/002 - Second.mp3", _text("TDRC", "2024-01-08"))
    _episode(root, "Alpha/Extras/2024-02-01 - Trailer.mp3", _text("TALB", "Extras"))
    _episode(root, "Beta/Season 01/2023-05-05 - Hello.mp3")
    return root


def _count(table):
    with db.session() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608


def test_create_podcasts_library_then_scan_produces_show_rows(tmp_path):
    root = _tree(tmp_path / "pods")
    row = setup.create_root("Podcasts", str(root), "audiobook", library_type="podcasts")
    assert row["library_type"] == "podcasts"
    assert _count("podcast_show") == 2  # create already ran the scanner
    with db.session() as conn:
        conn.execute("DELETE FROM podcast_show")
    assert _count("podcast_show") == 0
    result = scanner.scan_root(row["id"])
    assert result["library_type"] == "podcasts" and result["shows"] == 2
    with db.session() as conn:
        titles = sorted(r[0] for r in conn.execute("SELECT title FROM podcast_show"))
    assert titles == ["Alpha", "Beta"]
    assert _count("podcast_episode") == 4
    assert _count("scan_runs") == 0  # podcast scans take no book scan run


def test_books_library_scan_is_unchanged(tmp_path):
    books = tmp_path / "books"
    books.mkdir()
    make_epub(books / "Test Book.epub")
    seed_work()
    row = setup.create_root("Books", str(books), "ebook")
    assert row["library_type"] == "books"
    result = scanner.scan_root(row["id"])
    assert result["matched"] == 1 and "run_id" in result
    assert _count("podcast_show") == 0


def test_nonexistent_root_is_a_validation_error_and_creates_no_library(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        setup.create_root("Podcasts", str(tmp_path / "missing"), "audiobook", library_type="podcasts")
    assert _count("root_folders") == 0


def test_unknown_library_type_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="library type"):
        setup.create_root("X", str(tmp_path), "ebook", library_type="comics")
    assert _count("root_folders") == 0
