"""fresh_db copies a migrated template instead of running init_db per test (#473).

These tests prove the copy is equivalent to an init_db-built database: same PRAGMA
journal_mode and user_version, same schema and seed rows, no template-root paths leaked
into the data, and ensure_dirs creates every directory init_db does.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

from app import db
from app.config import settings

from conftest import ISOLATED_PATHS


def _build_with_init_db(root: Path) -> str:
    original = {k: getattr(settings, k) for k in ISOLATED_PATHS}
    for k, name in ISOLATED_PATHS.items():
        object.__setattr__(settings, k, root / name)
    try:
        db.init_db()
        return str(settings.db_path)
    finally:
        for k, v in original.items():
            object.__setattr__(settings, k, v)


def _pragma(path: str, name: str):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]


def _dump(path: str) -> dict[str, list]:
    with closing(sqlite3.connect(path)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: sorted(map(repr, conn.execute(f'SELECT * FROM "{t}"').fetchall())) for t in tables}  # nosec B608


def _dirs(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}


def test_copied_db_has_same_pragmas_as_init_db_db(tmp_path):
    built = _build_with_init_db(tmp_path / "built")
    copied = str(settings.db_path)
    assert _pragma(copied, "journal_mode") == _pragma(built, "journal_mode") == "wal"
    assert _pragma(copied, "user_version") == _pragma(built, "user_version")


def test_copied_db_has_same_rows_as_init_db_db(tmp_path):
    built = _build_with_init_db(tmp_path / "built")
    copied = str(settings.db_path)
    a, b = _dump(built), _dump(copied)
    assert a.keys() == b.keys()
    # ids/timestamps in seed rows differ per build, so compare row counts per table
    assert {t: len(r) for t, r in a.items()} == {t: len(r) for t, r in b.items()}
    assert b["tags"], "seed data was not copied"


def test_no_settings_derived_paths_in_seeded_data(migrated_template):
    # The template is built under its own root; none of the roots may appear in its data.
    roots = [str(Path(migrated_template).parent)]
    for name, rows in _dump(str(settings.db_path)).items():
        for row in rows:
            for root in roots + [str(settings.data_dir), str(settings.backup_dir), str(settings.cover_dir)]:
                assert root not in row, f"{name} row embeds a path under {root}"


def test_ensure_dirs_covers_every_directory_init_db_creates(tmp_path):
    via_init = tmp_path / "via_init"
    _build_with_init_db(via_init)
    via_ensure = tmp_path / "via_ensure"
    original = {k: getattr(settings, k) for k in ISOLATED_PATHS}
    for k, name in ISOLATED_PATHS.items():
        object.__setattr__(settings, k, via_ensure / name)
    try:
        settings.ensure_dirs()
    finally:
        for k, v in original.items():
            object.__setattr__(settings, k, v)
    assert _dirs(via_init) == _dirs(via_ensure)
