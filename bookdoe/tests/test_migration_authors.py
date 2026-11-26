"""Regression tests for author attribution during Readarr migration.

Defect found during the 2026-09-24 Unraid deployment: all 11,609 migrated works
came through with the author "Unknown". Real Readarr carries no Name column on
Authors -- Books.AuthorMetadataId -> AuthorMetadata.Name is the real path. The
consequence was worse than missing data: with every author equal to "Unknown",
cross-instance merging degenerated into title-only matching, so unrelated books
that happen to share a title would be fused into one Work.
"""
import sqlite3
from pathlib import Path

from app import db
from app.services import migration

import pytest


@pytest.fixture(autouse=True)
def _managed_destination_roots(tmp_path):
    """The free-space preflight needs a managed destination share per media kind."""
    for kind in ("ebook", "audiobook"):
        dest = tmp_path / f"managed-{kind}"
        dest.mkdir()
        with db.tx() as c:
            c.execute(
                "INSERT INTO root_folders(id,name,path,media_kind,management_mode,enabled,created_at,updated_at)"
                " VALUES (?,?,?,?, 'managed',1,?,?)",
                (db.uid(), dest.name, str(dest), kind, db.now(), db.now()),
            )


def _readarr(path: Path, rows: list[tuple[int, str, str]]):
    """rows = (book_id, author_name, title) using the real Readarr schema."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE Authors(Id INTEGER PRIMARY KEY, CleanName TEXT, Path TEXT, AuthorMetadataId INTEGER);
        CREATE TABLE AuthorMetadata(Id INTEGER PRIMARY KEY, Name TEXT, SortName TEXT);
        CREATE TABLE Books(Id INTEGER PRIMARY KEY, AuthorMetadataId INTEGER, Title TEXT, Monitored INTEGER);
        CREATE TABLE Editions(Id INTEGER PRIMARY KEY, BookId INTEGER, Isbn13 TEXT, Asin TEXT);
        CREATE TABLE BookFiles(Id INTEGER PRIMARY KEY, EditionId INTEGER, Path TEXT, Size INTEGER);
        CREATE TABLE RootFolders(Id INTEGER PRIMARY KEY, Path TEXT, Name TEXT);
        """
    )
    seen: dict[str, int] = {}
    for bid, author, title in rows:
        if author not in seen:
            amid = len(seen) + 1
            seen[author] = amid
            c.execute("INSERT INTO AuthorMetadata VALUES (?,?,?)", (amid, author, author))
            c.execute("INSERT INTO Authors VALUES (?,?,?,?)", (amid, author.lower(), f"/books/{author}", amid))
        c.execute("INSERT INTO Books VALUES (?,?,?,1)", (bid, seen[author], title))
    c.execute("INSERT INTO RootFolders VALUES (50,'/books','Books')")
    c.commit()
    c.close()


def _books_from(path: Path, kind: str = "ebook"):
    a = migration.analyze_readarr(str(path), kind, "src")
    return a["analysis"]["books"], a["run_id"]


def test_author_names_come_from_authormetadata(tmp_path):
    src = tmp_path / "r.db"
    _readarr(src, [(10, "Andy Weir", "Project Hail Mary")])
    books, _ = _books_from(src)
    assert books[0]["author"] == "Andy Weir", "author must resolve via Books.AuthorMetadataId"


def test_authors_table_without_a_name_column_does_not_yield_unknown(tmp_path):
    src = tmp_path / "r.db"
    _readarr(src, [(10, "Adrian Tchaikovsky", "Children of Time"), (11, "Michael Crichton", "Sphere")])
    books, _ = _books_from(src)
    assert {b["author"] for b in books} == {"Adrian Tchaikovsky", "Michael Crichton"}
    assert "Unknown" not in {b["author"] for b in books}


def test_same_title_by_different_authors_must_not_merge(tmp_path):
    """The dangerous consequence of losing authors."""
    src = tmp_path / "r.db"
    _readarr(src, [(10, "Dan Brown", "Inferno"), (11, "Sherrilyn Kenyon", "Inferno")])
    _, run_id = _books_from(src)
    migration.reconcile_run(run_id, use_network=False)
    migration.execute_run(run_id)

    works = db.all("SELECT id,title FROM works WHERE title='Inferno'")
    assert len(works) == 2, "two different authors' Inferno must stay two Works"
    names = {r["name"] for r in db.all("SELECT name FROM contributors")}
    assert names == {"Dan Brown", "Sherrilyn Kenyon"}


def test_same_title_and_author_does_converge(tmp_path):
    """Convergence must still work when the evidence is genuine."""
    for n in ("a", "b"):
        src = tmp_path / f"{n}.db"
        _readarr(src, [(10, "Michael Crichton", "Sphere")])
        _, run_id = _books_from(src)
        migration.reconcile_run(run_id, use_network=False)
        migration.execute_run(run_id)
    assert len(db.all("SELECT id FROM works WHERE title='Sphere'")) == 1


def test_unknown_authors_never_merge_with_each_other(tmp_path):
    """Two authorless books sharing a title are not evidence of the same work."""
    src = tmp_path / "r.db"
    c = sqlite3.connect(src)
    c.executescript(
        """
        CREATE TABLE Authors(Id INTEGER PRIMARY KEY, CleanName TEXT);
        CREATE TABLE AuthorMetadata(Id INTEGER PRIMARY KEY, Name TEXT);
        CREATE TABLE Books(Id INTEGER PRIMARY KEY, AuthorMetadataId INTEGER, Title TEXT, Monitored INTEGER);
        CREATE TABLE Editions(Id INTEGER PRIMARY KEY, BookId INTEGER, Isbn13 TEXT, Asin TEXT);
        CREATE TABLE BookFiles(Id INTEGER PRIMARY KEY, EditionId INTEGER, Path TEXT, Size INTEGER);
        CREATE TABLE RootFolders(Id INTEGER PRIMARY KEY, Path TEXT, Name TEXT);
        """
    )
    c.execute("INSERT INTO Books VALUES (10,NULL,'Untitled Anthology',1)")
    c.execute("INSERT INTO Books VALUES (11,NULL,'Untitled Anthology',1)")
    c.execute("INSERT INTO RootFolders VALUES (50,'/books','Books')")
    c.commit(); c.close()
    _, run_id = _books_from(src)
    migration.reconcile_run(run_id, use_network=False)
    migration.execute_run(run_id)
    assert len(db.all("SELECT id FROM works WHERE title='Untitled Anthology'")) == 2
