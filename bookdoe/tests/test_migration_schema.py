"""Regression tests for Readarr's real BookFiles linkage.

Defect found during the 2026-09-24 Unraid deployment: analysis reported
files=0 against all three production Readarr databases. Real Readarr links
files to editions (BookFiles.EditionId -> Editions.Id -> Editions.BookId);
the analyzer only looked for a BookFiles.BookId column, which does not exist.
The pre-existing test fixture used a synthetic BookId schema and so passed.
"""
import sqlite3
from pathlib import Path

from app.services import migration


def _real_readarr(path: Path, media: Path, *, orphan: bool = False, parts: bool = False):
    """Schema mirroring production Readarr: files hang off Editions, not Books."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE Authors(Id INTEGER PRIMARY KEY, AuthorMetadataId INTEGER, Path TEXT);
        CREATE TABLE AuthorMetadata(Id INTEGER PRIMARY KEY, Name TEXT);
        CREATE TABLE Books(Id INTEGER PRIMARY KEY, AuthorMetadataId INTEGER, Title TEXT, Monitored INTEGER);
        CREATE TABLE Editions(Id INTEGER PRIMARY KEY, BookId INTEGER, Isbn13 TEXT, Asin TEXT, Monitored INTEGER);
        CREATE TABLE BookFiles(Id INTEGER PRIMARY KEY, EditionId INTEGER, Path TEXT, Size INTEGER, Part INTEGER, Quality TEXT);
        CREATE TABLE RootFolders(Id INTEGER PRIMARY KEY, Path TEXT, Name TEXT);
        """
    )
    c.execute("INSERT INTO AuthorMetadata VALUES (1,'Jane Author')")
    c.execute("INSERT INTO Authors VALUES (1,1,'/books/Jane Author')")
    c.execute("INSERT INTO Books VALUES (10,1,'Test Book',1)")
    c.execute("INSERT INTO Editions VALUES (20,10,'9781234567897',NULL,1)")
    c.execute("INSERT INTO BookFiles VALUES (30,20,?,?,NULL,'EPUB')", (str(media), media.stat().st_size))
    if parts:
        c.execute("INSERT INTO BookFiles VALUES (31,20,?,?,1,'MP3')", (str(media) + ".part1", 11))
        c.execute("INSERT INTO BookFiles VALUES (32,20,?,?,2,'MP3')", (str(media) + ".part2", 12))
    if orphan:
        # Edition 99 does not exist - Readarr leaves such rows behind after deletes.
        c.execute("INSERT INTO BookFiles VALUES (40,99,'/books/orphan.epub',5,NULL,'EPUB')")
    c.execute("INSERT INTO RootFolders VALUES (50,'/books','Books')")
    c.commit()
    c.close()


def test_files_are_found_through_the_edition_relationship(tmp_path):
    media = tmp_path / "book.epub"
    media.write_bytes(b"stable-bytes")
    src = tmp_path / "readarr.db"
    _real_readarr(src, media)

    a = migration.analyze_readarr(str(src), "ebook", "real-schema")
    books = a["analysis"]["books"]
    assert len(books) == 1
    # The regression: this was 0 because BookFiles has no BookId column.
    assert len(books[0]["files"]) == 1, "file must resolve via EditionId -> Editions.BookId"
    assert books[0]["files"][0]["path"] == str(media)
    assert a["summary"]["files"] == 1


def test_multipart_audiobook_files_are_all_captured(tmp_path):
    media = tmp_path / "book.m4b"
    media.write_bytes(b"stable-bytes")
    src = tmp_path / "readarr.db"
    _real_readarr(src, media, parts=True)

    a = migration.analyze_readarr(str(src), "audiobook", "real-schema-multipart")
    files = a["analysis"]["books"][0]["files"]
    assert len(files) == 3
    assert sorted(f["part"] for f in files if f["part"] is not None) == [1, 2]


def test_orphaned_book_files_are_reported_not_silently_dropped(tmp_path):
    media = tmp_path / "book.epub"
    media.write_bytes(b"stable-bytes")
    src = tmp_path / "readarr.db"
    _real_readarr(src, media, orphan=True)

    a = migration.analyze_readarr(str(src), "ebook", "real-schema-orphan")
    assert a["analysis"]["orphan_files"] == 1
    assert any("no longer exists" in w for w in a["analysis"]["warnings"])
    # The attributable file is still found.
    assert a["summary"]["files"] == 1


def test_legacy_bookid_schema_is_still_supported(tmp_path):
    """Forks/older schemas with a direct BookFiles.BookId must keep working."""
    media = tmp_path / "book.epub"
    media.write_bytes(b"stable-bytes")
    src = tmp_path / "readarr.db"
    c = sqlite3.connect(src)
    c.executescript(
        """
        CREATE TABLE Authors(Id INTEGER PRIMARY KEY, Name TEXT);
        CREATE TABLE Books(Id INTEGER PRIMARY KEY, AuthorId INTEGER, Title TEXT, Monitored INTEGER);
        CREATE TABLE Editions(Id INTEGER PRIMARY KEY, BookId INTEGER, Isbn13 TEXT, ASIN TEXT);
        CREATE TABLE BookFiles(Id INTEGER PRIMARY KEY, BookId INTEGER, Path TEXT, Size INTEGER);
        CREATE TABLE RootFolders(Id INTEGER PRIMARY KEY, Path TEXT, Name TEXT);
        """
    )
    c.execute("INSERT INTO Authors VALUES (1,'Jane Author')")
    c.execute("INSERT INTO Books VALUES (10,1,'Test Book',1)")
    c.execute("INSERT INTO Editions VALUES (20,10,'9781234567897',NULL)")
    c.execute("INSERT INTO BookFiles VALUES (30,10,?,?)", (str(media), media.stat().st_size))
    c.execute("INSERT INTO RootFolders VALUES (40,'/books','Books')")
    c.commit()
    c.close()

    a = migration.analyze_readarr(str(src), "ebook", "legacy-schema")
    assert a["summary"]["files"] == 1


def test_analysis_never_opens_the_source_writable(tmp_path):
    media = tmp_path / "book.epub"
    media.write_bytes(b"stable-bytes")
    src = tmp_path / "readarr.db"
    _real_readarr(src, media)
    before = src.read_bytes()
    migration.analyze_readarr(str(src), "ebook", "immutability")
    assert src.read_bytes() == before
