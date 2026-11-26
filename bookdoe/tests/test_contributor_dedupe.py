"""The authors page showed one row per book, not one row per author.

Readarr import inserted a fresh contributor row for every title it created, so a
library of 12,026 works produced 12,026 contributor rows for 303 real authors and
every author owned exactly one work. These tests pin both halves of the fix: the
import no longer creates duplicates, and existing duplicates are merged.
"""
from __future__ import annotations

from app import db
from app.services import migration, monitoring, repair


def _make_work(title: str, author: str) -> str:
    return migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})


def test_import_reuses_one_contributor_per_author():
    _make_work("Book One", "Craig Alanson")
    _make_work("Book Two", "Craig Alanson")
    _make_work("Book Three", "craig  alanson")

    rows = db.all("SELECT id,name FROM contributors")
    assert len(rows) == 1, rows
    assert rows[0]["name"] == "Craig Alanson"
    assert db.one("SELECT COUNT(*) n FROM work_contributors")["n"] == 3


def test_authors_list_has_one_row_per_author_with_real_work_counts():
    _make_work("Book One", "Craig Alanson")
    _make_work("Book Two", "Craig Alanson")
    _make_work("Other Book", "Martha Wells")

    authors = monitoring.list_authors()
    assert [a["name"] for a in authors] == ["Craig Alanson", "Martha Wells"]
    assert {a["name"]: a["work_count"] for a in authors} == {"Craig Alanson": 2, "Martha Wells": 1}


def _legacy_duplicate(name: str, title: str) -> tuple[str, str]:
    """Recreate the pre-fix shape: a contributor row created per work."""
    wid, cid, t = db.uid(), db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO works(id,title,sort_title,monitored,metadata_status,added_at,updated_at) VALUES (?,?,?,1,'unresolved',?,?)",
            (wid, title, title, t, t),
        )
        conn.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, name, name, t, t))
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid, cid))
    return wid, cid


def test_dedupe_merges_duplicate_rows_and_keeps_every_work():
    for i in range(5):
        _legacy_duplicate("Craig Alanson", f"Expeditionary Force {i}")
    _legacy_duplicate("Martha Wells", "All Systems Red")
    assert db.one("SELECT COUNT(*) n FROM contributors")["n"] == 6

    result = repair.dedupe_contributors()

    assert result["rows_removed"] == 4
    assert db.one("SELECT COUNT(*) n FROM contributors")["n"] == 2
    # Every work keeps its author; nothing is orphaned by the merge.
    assert db.one("SELECT COUNT(*) n FROM work_contributors")["n"] == 6
    authors = {a["name"]: a["work_count"] for a in monitoring.list_authors()}
    assert authors == {"Craig Alanson": 5, "Martha Wells": 1}


def test_dedupe_is_idempotent():
    for i in range(3):
        _legacy_duplicate("Craig Alanson", f"Book {i}")
    repair.dedupe_contributors()
    before = db.all("SELECT id FROM contributors")
    second = repair.dedupe_contributors()
    assert second["rows_removed"] == 0
    assert db.all("SELECT id FROM contributors") == before


def test_dedupe_moves_author_monitoring_to_the_surviving_row():
    _, first = _legacy_duplicate("Craig Alanson", "Book A")
    _, second = _legacy_duplicate("Craig Alanson", "Book B")
    monitoring.set_author_monitor(second, policy="all", ebook=True, audiobook=True)

    repair.dedupe_contributors()

    survivors = db.all("SELECT id FROM contributors")
    assert len(survivors) == 1
    assert survivors[0]["id"] == first
    monitor = db.one("SELECT * FROM author_monitors")
    assert monitor is not None and monitor["contributor_id"] == first
    assert monitor["policy"] == "all"


def test_two_people_sharing_a_name_are_not_merged():
    """A name collision between two distinct OpenReads records is not a duplicate."""
    _, first = _legacy_duplicate("John Smith", "Book A")
    _, second = _legacy_duplicate("John Smith", "Book B")
    with db.tx() as conn:
        conn.execute("UPDATE contributors SET metadata_id='or-1' WHERE id=?", (first,))
        conn.execute("UPDATE contributors SET metadata_id='or-2' WHERE id=?", (second,))

    result = repair.dedupe_contributors()

    assert result["groups_skipped"] == 1
    assert db.one("SELECT COUNT(*) n FROM contributors")["n"] == 2


def test_startup_repair_merges_then_stays_quiet():
    for i in range(3):
        _legacy_duplicate("Craig Alanson", f"Book {i}")
    first = repair.run_startup_repairs()
    assert first["dedupe_contributors"]["rows_removed"] == 2
    # A clean database reports nothing rather than logging a merge of zero rows.
    assert repair.run_startup_repairs() == {}


def test_startup_repair_catches_duplicates_that_appear_later():
    """It must not be a one-shot.

    Linking works to OpenReads used to mint a parallel author row, so duplicates
    could appear long after any first-boot repair had been marked done.
    """
    repair.run_startup_repairs()
    _legacy_duplicate("Craig Alanson", "Book A")
    _legacy_duplicate("Craig Alanson", "Book B")
    assert repair.run_startup_repairs()["dedupe_contributors"]["rows_removed"] == 1
    assert db.one("SELECT COUNT(*) n FROM contributors")["n"] == 1


def test_linking_a_work_adopts_the_existing_author_row():
    """The regression that made this repair self-healing in the first place."""
    from app.services import metadata

    work_id = _make_work("Project Hail Mary", "Andy Weir")
    meta = {
        "id": "or-phm",
        "title": "Project Hail Mary",
        "contributors": [{"id": "or-andy-weir", "name": "Andy Weir", "role": "author"}],
    }
    with db.tx() as conn:
        conn.execute("UPDATE works SET metadata_id=? WHERE id=?", ("or-phm", work_id))
    metadata.upsert_work(meta)

    rows = db.all("SELECT id,name,metadata_id FROM contributors")
    assert len(rows) == 1, rows
    assert rows[0]["metadata_id"] == "or-andy-weir"
    assert monitoring.list_authors()[0]["work_count"] == 1


def test_a_name_already_bound_to_another_provider_id_is_not_adopted():
    from app.services import metadata

    _make_work("Some Book", "John Smith")
    with db.tx() as conn:
        conn.execute("UPDATE contributors SET metadata_id='or-smith-1' WHERE name='John Smith'")
    metadata.upsert_work({
        "id": "or-other",
        "title": "Another Book",
        "contributors": [{"id": "or-smith-2", "name": "John Smith", "role": "author"}],
    })
    # Two different people who share a name stay two rows.
    assert db.one("SELECT COUNT(*) n FROM contributors")["n"] == 2
