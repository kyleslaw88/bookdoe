"""Regression tests for dual-source convergence and cross-container path remapping.

Both defects were found during the 2026-09-24 Unraid deployment while migrating
three real Readarr instances into one catalogue:

CC-003  Migrating a second audiobook source onto a Work that already owned an
        audiobook asset created a *second* asset, violating
        UNIQUE(work_id, kind, format) the moment its format was set. Convergence
        itself triggered the crash.
CC-004  Readarr records absolute paths as its own container saw them
        (/calibre_data/ebooks/...). bookdoe mounts the same files elsewhere, so
        every ebook file was reported missing.
"""
import sqlite3
from pathlib import Path

import pytest

from app import db
from app.services import migration


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


def _src(path: Path, title: str, author: str, files: list[tuple[str, int | None]], root: str):
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
    c.execute("INSERT INTO AuthorMetadata VALUES (1,?)", (author,))
    c.execute("INSERT INTO Authors VALUES (1,1,?)", (root,))
    c.execute("INSERT INTO Books VALUES (10,1,?,1)", (title,))
    c.execute("INSERT INTO Editions VALUES (20,10,NULL,NULL,1)")
    for i, (fp, part) in enumerate(files):
        c.execute("INSERT INTO BookFiles VALUES (?,20,?,?,?,'X')", (30 + i, fp, 10, part))
    c.execute("INSERT INTO RootFolders VALUES (50,?,'R')", (root,))
    c.commit()
    c.close()


def _add_root(path: str, kind: str):
    with db.tx() as c:
        c.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
            " VALUES (?,?,?,?, 'adopt',0,1,?,?)",
            (db.uid(), path, path, kind, db.now(), db.now()),
        )


def test_two_audiobook_sources_converge_on_one_asset(tmp_path):
    """CC-003: the same book from two Readarr instances must not create two assets."""
    lib = tmp_path / "audiobooks"
    lib.mkdir()
    m4b = lib / "Winnie-the-Pooh.m4b"
    m4b.write_bytes(b"audio")
    _add_root(str(lib), "audiobook")

    for n in ("a", "b"):
        src = tmp_path / f"{n}.db"
        _src(src, "Winnie-the-Pooh", "A.A. Milne", [(str(m4b), None)], str(lib))
        a = migration.analyze_readarr(str(src), "audiobook", n)
        migration.reconcile_run(a["run_id"], use_network=False)
        r = migration.execute_run(a["run_id"])          # must not raise IntegrityError
        assert r["works"] == 1

    works = db.all("SELECT id FROM works")
    assert len(works) == 1, "both sources must converge on ONE Work"
    assets = db.all("SELECT * FROM media_assets WHERE kind='audiobook'")
    assert len(assets) == 1, "one Work owns exactly one audiobook asset"
    assert assets[0]["status"] == "owned"
    files = db.all("SELECT * FROM media_files")
    assert len(files) == 1, "the same path must not be recorded twice"


def test_one_work_can_own_both_an_ebook_and_an_audiobook_asset(tmp_path):
    """The headline success criterion: one Work, two media kinds, both owned."""
    eb_dir = tmp_path / "books"; eb_dir.mkdir()
    ab_dir = tmp_path / "audiobooks"; ab_dir.mkdir()
    epub = eb_dir / "Sphere.epub"; epub.write_bytes(b"ebook")
    m4b = ab_dir / "Sphere.m4b"; m4b.write_bytes(b"audio")
    _add_root(str(eb_dir), "ebook")
    _add_root(str(ab_dir), "audiobook")

    for name, kind, f in (("ebooks", "ebook", epub), ("audio", "audiobook", m4b)):
        src = tmp_path / f"{name}.db"
        _src(src, "Sphere", "Michael Crichton", [(str(f), None)], str(f.parent))
        a = migration.analyze_readarr(str(src), kind, name)
        migration.reconcile_run(a["run_id"], use_network=False)
        migration.execute_run(a["run_id"])

    assert len(db.all("SELECT id FROM works")) == 1
    kinds = {a["kind"]: a for a in db.all("SELECT * FROM media_assets")}
    assert set(kinds) == {"ebook", "audiobook"}
    assert kinds["ebook"]["status"] == "owned" and kinds["ebook"]["format"] == "epub"
    assert kinds["audiobook"]["status"] == "owned" and kinds["audiobook"]["format"] == "m4b"


def test_multiple_formats_of_one_kind_share_a_single_asset(tmp_path):
    """epub + mobi are two MediaFiles of one ebook asset, not two assets."""
    d = tmp_path / "books"; d.mkdir()
    epub = d / "Sand.epub"; epub.write_bytes(b"a")
    mobi = d / "Sand.mobi"; mobi.write_bytes(b"b")
    _add_root(str(d), "ebook")
    src = tmp_path / "e.db"
    _src(src, "Sand", "Hugh Howey", [(str(epub), None), (str(mobi), None)], str(d))
    a = migration.analyze_readarr(str(src), "ebook", "e")
    migration.reconcile_run(a["run_id"], use_network=False)
    r = migration.execute_run(a["run_id"])
    assert r["files"] == 2
    assert len(db.all("SELECT id FROM media_assets WHERE kind='ebook'")) == 1


def test_absolute_paths_are_remapped_onto_bookdoe_roots(tmp_path):
    """CC-004: Readarr's own container paths must be translated, not reported missing."""
    real = tmp_path / "books-legacy"; real.mkdir()
    book = real / "Author" / "Title"
    book.mkdir(parents=True)
    epub = book / "Title.epub"; epub.write_bytes(b"ebook")
    _add_root(str(real), "ebook")

    # Readarr recorded it under a path that does not exist in this container.
    readarr_path = "/calibre_data/ebooks/Author/Title/Title.epub"
    src = tmp_path / "e.db"
    _src(src, "Title", "Author", [(readarr_path, None)], "/calibre_data/ebooks/")

    a = migration.analyze_readarr(str(src), "ebook", "e")
    migration.reconcile_run(a["run_id"], use_network=False)
    r = migration.execute_run(a["run_id"])
    assert r["files"] == 1, "path should be remapped onto the bookdoe root"
    assert r["missing_files"] == 0
    assert db.one("SELECT path FROM media_files")["path"] == str(epub)


def test_unresolvable_absolute_path_is_reported_missing_not_invented(tmp_path):
    d = tmp_path / "books"; d.mkdir()
    _add_root(str(d), "ebook")
    src = tmp_path / "e.db"
    _src(src, "Ghost", "Nobody", [("/nowhere/at/all/Ghost.epub", None)], "/nowhere/")
    a = migration.analyze_readarr(str(src), "ebook", "e")
    migration.reconcile_run(a["run_id"], use_network=False)
    r = migration.execute_run(a["run_id"])
    assert r["files"] == 0 and r["missing_files"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0


def test_migration_still_never_moves_or_modifies_media(tmp_path):
    d = tmp_path / "books"; d.mkdir()
    epub = d / "Keep.epub"; epub.write_bytes(b"original-bytes")
    before = epub.read_bytes()
    _add_root(str(d), "ebook")
    src = tmp_path / "e.db"
    _src(src, "Keep", "Author", [(str(epub), None)], str(d))
    a = migration.analyze_readarr(str(src), "ebook", "e")
    migration.reconcile_run(a["run_id"], use_network=False)
    migration.execute_run(a["run_id"])
    assert epub.exists() and epub.read_bytes() == before
    mf = db.one("SELECT * FROM media_files WHERE path=?", (str(epub),))
    assert mf["adopted"] == 1 and mf["managed"] == 0


def test_source_roots_that_this_container_cannot_see_are_not_imported(tmp_path):
    """CC-005: Readarr's own container paths must not become dead bookdoe roots."""
    d = tmp_path / "books"; d.mkdir()
    epub = d / "X.epub"; epub.write_bytes(b"e")
    _add_root(str(d), "ebook")
    before = {r["path"] for r in db.all("SELECT path FROM root_folders")}

    src = tmp_path / "e.db"
    _src(src, "X", "A", [(str(epub), None)], "/calibre_data/ebooks/")
    a = migration.analyze_readarr(str(src), "ebook", "e")
    migration.reconcile_run(a["run_id"], use_network=False)
    migration.execute_run(a["run_id"])

    after = {r["path"] for r in db.all("SELECT path FROM root_folders")}
    assert after == before, "a source root invisible to this container must be skipped"
    assert "/calibre_data/ebooks/" not in after


def test_source_root_already_covered_is_not_duplicated(tmp_path):
    """A second root over the same files would adopt the same media twice."""
    d = tmp_path / "audiobooks"; d.mkdir()
    sub = d / "library"; sub.mkdir()
    m4b = sub / "B.m4b"; m4b.write_bytes(b"a")
    _add_root(str(d), "audiobook")
    before = {r["path"] for r in db.all("SELECT path FROM root_folders")}

    src = tmp_path / "a.db"
    _src(src, "B", "A", [(str(m4b), None)], str(sub))   # nested inside an existing root
    a = migration.analyze_readarr(str(src), "audiobook", "a")
    migration.reconcile_run(a["run_id"], use_network=False)
    migration.execute_run(a["run_id"])

    after = {r["path"] for r in db.all("SELECT path FROM root_folders")}
    assert after == before, "a root nested inside a configured root must be skipped"


def test_bind_mount_aliases_are_not_added_as_duplicate_roots(tmp_path, monkeypatch):
    """CC-007: the same directory reachable at two container paths is ONE root.

    /audiobooks and /data/audiobooks/library commonly resolve to the same host
    directory. They share no path prefix, so prefix comparison cannot catch it;
    adding both would adopt every file twice.
    """
    real = tmp_path / "audiobooks"
    real.mkdir()
    m4b = real / "B.m4b"; m4b.write_bytes(b"a")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        import pytest; pytest.skip("symlinks unavailable")
    _add_root(str(real), "audiobook")
    before = {r["path"] for r in db.all("SELECT path FROM root_folders")}

    src = tmp_path / "a.db"
    _src(src, "B", "A", [(str(alias / "B.m4b"), None)], str(alias))
    a = migration.analyze_readarr(str(src), "audiobook", "a")
    migration.reconcile_run(a["run_id"], use_network=False)
    migration.execute_run(a["run_id"])

    after = {r["path"] for r in db.all("SELECT path FROM root_folders")}
    assert after == before, "an alias of an existing root must not be added again"


def test_file_paths_are_recorded_inside_a_configured_root(tmp_path):
    """A file visible at two paths must be recorded under the managed root."""
    real = tmp_path / "audiobooks"; real.mkdir()
    m4b = real / "B.m4b"; m4b.write_bytes(b"a")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        import pytest; pytest.skip("symlinks unavailable")
    _add_root(str(real), "audiobook")

    src = tmp_path / "a.db"
    # Readarr recorded it via the alias path, which exists but is not a root.
    _src(src, "B", "A", [(str(alias / "B.m4b"), None)], str(alias))
    a = migration.analyze_readarr(str(src), "audiobook", "a")
    migration.reconcile_run(a["run_id"], use_network=False)
    r = migration.execute_run(a["run_id"])
    assert r["files"] == 1
    recorded = db.one("SELECT path FROM media_files")["path"]
    assert recorded == str(real / "B.m4b"), f"expected the root-relative path, got {recorded}"
