"""Files in a managed root that the database has lost track of.

During the library-root rename the epub bookdoe had already imported for "Dead
World" kept its place on disk but lost its media_files row, leaving the ebook asset
marked owned with no file behind it. A managed root only ever holds files bookdoe
itself wrote, laid out as <Author>/<Title>/file, so recovery is deterministic.
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app.services import migration, repair


def _managed_root(tmp_path: Path, kind: str = "ebook") -> Path:
    base = tmp_path / kind
    base.mkdir(parents=True, exist_ok=True)
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)
               VALUES (?,?,?,?, 'managed',1,1,?,?)""",
            (db.uid(), f"managed-{kind}", str(base), kind, db.now(), db.now()),
        )
    return base


def _owned_asset(work_id: str, kind: str = "ebook") -> str:
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,?,'owned',1,?,?)",
            (asset_id, work_id, kind, t, t),
        )
    return asset_id


def _place(base: Path, author: str, title: str, name: str) -> Path:
    directory = base / author / title
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"not really an epub, but a real file")
    return path


def test_orphaned_managed_file_is_reattached_to_its_asset(tmp_path):
    base = _managed_root(tmp_path)
    work_id = migration._find_or_create_stub({"title": "Dead World", "author": "Craig Alanson", "identifiers": {}})
    asset_id = _owned_asset(work_id)
    path = _place(base, "Craig Alanson", "Dead World", "Dead World.epub")
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0

    result = repair.reconcile_managed_files()

    assert result["registered"] == 1
    row = db.one("SELECT * FROM media_files WHERE path=?", (str(path),))
    assert row is not None
    assert row["asset_id"] == asset_id
    assert row["managed"] == 1 and row["missing"] == 0
    assert db.one("SELECT format FROM media_assets WHERE id=?", (asset_id,))["format"] == "epub"


def test_reconcile_is_idempotent(tmp_path):
    base = _managed_root(tmp_path)
    work_id = migration._find_or_create_stub({"title": "Dead World", "author": "Craig Alanson", "identifiers": {}})
    _owned_asset(work_id)
    _place(base, "Craig Alanson", "Dead World", "Dead World.epub")

    repair.reconcile_managed_files()
    second = repair.reconcile_managed_files()

    assert second["registered"] == 0
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 1


def test_a_file_with_no_matching_work_is_left_alone(tmp_path):
    base = _managed_root(tmp_path)
    _place(base, "Nobody At All", "Unknown Book", "Unknown Book.epub")

    result = repair.reconcile_managed_files()

    assert result["registered"] == 0 and result["unmatched"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0


def test_an_ambiguous_title_and_author_is_reported_not_guessed(tmp_path):
    base = _managed_root(tmp_path)
    # Two works with the same title and author is exactly the case where attaching
    # the file to "the" work would be attaching it to the wrong one half the time.
    for _ in range(2):
        wid, t = db.uid(), db.now()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'unresolved',?,?)",
                (wid, "Dead World", "Dead World", t, t),
            )
            cid = db.contributor_id_for_name(conn, "Craig Alanson")
            conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid, cid))
        _owned_asset(wid)
    _place(base, "Craig Alanson", "Dead World", "Dead World.epub")

    result = repair.reconcile_managed_files()

    assert result["registered"] == 0 and result["ambiguous"] == 1
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 0


def test_adopt_roots_are_untouched(tmp_path):
    """Only managed roots are bookdoe's own layout; adopt roots are the user's."""
    base = tmp_path / "adopted"
    (base / "Craig Alanson" / "Dead World").mkdir(parents=True)
    (base / "Craig Alanson" / "Dead World" / "Dead World.epub").write_bytes(b"x")
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)
               VALUES (?,?,?, 'ebook','adopt',1,1,?,?)""",
            (db.uid(), "adopted", str(base), db.now(), db.now()),
        )
    work_id = migration._find_or_create_stub({"title": "Dead World", "author": "Craig Alanson", "identifiers": {}})
    _owned_asset(work_id)

    assert repair.reconcile_managed_files() == {"files_seen": 0, "registered": 0, "ambiguous": 0, "unmatched": 0}
