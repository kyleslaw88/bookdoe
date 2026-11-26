"""Catalog duplicate detectors: duplicate Works, split duplicate Works, duplicate Editions (P5-21a, Part XXV)."""
from __future__ import annotations

import time

import pytest

from app import db
from app.services import maintenance
from app.services import catalogdupes
from app.services.catalogdupes import normalize_title, pair_id


def _work(title: str, author: str | None = "J. R. R. Tolkien", *, owned: str | None = None, monitored: str | None = None,
          metadata_id: str | None = None, idents: dict[str, str] | None = None) -> str:
    wid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,metadata_id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, metadata_id, title, title, t, t))
        if author:
            cid = db.contributor_id_for_name(conn, author)
            conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", (wid, cid))
        for kind, status, mon in ((owned, "owned", 0), (monitored, "missing", 1)):
            if kind:
                conn.execute("INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                             (db.uid(), wid, kind, status, mon, t, t))
        for ns, value in (idents or {}).items():
            conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,?,?)", (wid, ns, value))
    return wid


def _edition(work_id: str, **cols) -> str:
    eid, t = db.uid(), db.now()
    row = {"title": "The Hobbit", "publisher": "Allen & Unwin", "release_date": "1937-09-21", "language": "en", **cols}
    with db.tx() as conn:
        conn.execute("INSERT INTO editions(id,work_id,title,publisher,release_date,language,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (eid, work_id, row["title"], row["publisher"], row["release_date"], row["language"], t, t))
    return eid


def _issues(kind: str | None = None):
    where, args = ("WHERE kind=?", (kind,)) if kind else ("", ())
    return [{**r, "detail": db.json_load(r["detail_json"], {})} for r in db.all(f"SELECT * FROM maintenance_issues {where} ORDER BY fingerprint", args)]  # nosec B608


@pytest.mark.parametrize("raw,expected", [
    ("The Hobbit", "hobbit"),
    ("Hobbit, The", "hobbit"),
    ("The Hobbit: There and Back Again", "hobbit"),
    ("  THE   HOBBIT!  ", "hobbit"),
    ("A Game of Thrones", "game of thrones"),
    ("Game of Thrones, A", "game of thrones"),
    ("An Echo in the Dark", "echo in the dark"),
    ("Ender's Game", "enders game"),
    ("Ender’s Game", "enders game"),
    ("Theodore", "theodore"),
    ("Dune: Messiah", "dune"),
    ("", ""),
    (None, ""),
])
def test_normalize_title_table(raw, expected):
    assert normalize_title(raw) == expected


def test_pair_identity_is_order_independent():
    assert pair_id("b", "a") == pair_id("a", "b") == ("a", "b")


def test_hobbit_variants_yield_one_duplicate_work_with_signals():
    a = _work("The Hobbit")
    b = _work("Hobbit, The")
    r = maintenance.run_detector("duplicate-works")
    assert r["complete"] and r["opened"] == 1
    (issue,) = _issues()
    assert issue["kind"] == "duplicate_work" and issue["subject_type"] == "work_pair"
    assert issue["subject_id"] == ":".join(sorted([a, b])) and issue["repair_action"] == "merge_works"
    d = issue["detail"]
    assert {w["work_id"] for w in d["works"]} == {a, b}
    assert {w["title"] for w in d["works"]} == {"The Hobbit", "Hobbit, The"}
    assert all(w["authors"] == ["J. R. R. Tolkien"] and w["owned_assets"] == 0 for w in d["works"])
    assert d["signals"] == ["title_equal_normalized", "authors_equal"]
    assert d["confidence"] == "probable" and issue["severity"] == "info"  # fuzzy text only


def test_shared_identifier_across_entity_rows_makes_it_high_confidence():
    a = _work("The Hobbit")
    b = _work("Hobbit, The")
    eid = _edition(b)
    with db.tx() as conn:  # a work-level ISBN on one, the same value on the other's edition
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn13','9780547928227')", (a,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition',?,'isbn13','9780547928227')", (eid,))
    maintenance.run_detector("duplicate-works")
    (issue,) = _issues()
    assert issue["detail"]["confidence"] == "high" and issue["severity"] == "warning"
    assert "shared_identifier" in issue["detail"]["signals"]
    assert issue["detail"]["shared_identifiers"] == [{"namespace": "isbn13", "value": "9780547928227"}]


def test_both_sides_monitored_is_high_confidence():
    _work("The Hobbit", monitored="ebook")
    _work("Hobbit, The", monitored="audiobook")
    maintenance.run_detector("duplicate-works")
    (issue,) = _issues()
    assert issue["detail"]["confidence"] == "high" and "both_monitored_or_owned" in issue["detail"]["signals"]


def test_ebook_only_and_audiobook_only_works_are_a_split_duplicate():
    _work("The Hobbit", owned="ebook")
    _work("Hobbit, The", owned="audiobook")
    maintenance.run_detector("duplicate-works")
    (issue,) = _issues()
    assert issue["kind"] == "split_duplicate_work"
    assert "different_kinds" in issue["detail"]["signals"]
    kinds = sorted(w["owned_kinds"] for w in issue["detail"]["works"])
    assert kinds == [["audiobook"], ["ebook"]]
    assert issue["detail"]["canonical_work_id"]


def test_owned_vs_catalog_only_is_split_and_owned_work_is_canonical():
    owned = _work("The Hobbit", owned="ebook")
    _work("Hobbit, The", metadata_id="ol:W1")
    maintenance.run_detector("duplicate-works")
    (issue,) = _issues()
    assert issue["kind"] == "split_duplicate_work" and "owned_vs_catalog_only" in issue["detail"]["signals"]
    assert issue["detail"]["canonical_work_id"] == owned and "owned" in issue["detail"]["canonical_reason"]


def test_same_title_different_authors_yields_nothing():
    _work("The Hobbit", "J. R. R. Tolkien")
    _work("The Hobbit", "Somebody Else")
    _work("Orphan Title", None)
    _work("Orphan Title", None)
    r = maintenance.run_detector("duplicate-works")
    assert r["complete"] and r["found"] == 0 and _issues() == []


def test_overlapping_but_unequal_author_sets_are_a_duplicate_not_a_split():
    a = _work("Good Omens", "Neil Gaiman", owned="ebook")
    b = _work("Good Omens", "Neil Gaiman")
    with db.tx() as conn:
        cid = db.contributor_id_for_name(conn, "Terry Pratchett")
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',1)", (b, cid))
    maintenance.run_detector("duplicate-works")
    (issue,) = _issues()
    assert issue["kind"] == "duplicate_work" and "authors_overlap" in issue["detail"]["signals"]
    assert pair_id(a, b) == tuple(issue["subject_id"].split(":"))


def test_rerun_does_not_duplicate_and_resolves_when_fixed():
    _work("The Hobbit")
    b = _work("Hobbit, The")
    maintenance.run_detector("duplicate-works")
    r = maintenance.run_detector("duplicate-works")
    assert r["opened"] == 0 and r["refreshed"] == 1 and len(_issues()) == 1
    with db.tx() as conn:
        conn.execute("UPDATE works SET title='Something Else' WHERE id=?", (b,))
    r = maintenance.run_detector("duplicate-works")
    assert r["auto_resolved"] == 1 and _issues()[0]["status"] == "resolved"


def test_same_publisher_date_language_editions_are_duplicates():
    w = _work("The Hobbit")
    e1 = _edition(w)
    e2 = _edition(w, title="Hobbit, The")
    _edition(w, publisher="Houghton Mifflin")
    _edition(w, language="de")
    r = maintenance.run_detector("duplicate-editions")
    assert r["complete"] and r["opened"] == 1
    (issue,) = _issues("duplicate_edition")
    assert issue["subject_id"] == ":".join(sorted([e1, e2])) and issue["subject_type"] == "edition_pair"
    d = issue["detail"]
    assert d["work_id"] == w and {e["edition_id"] for e in d["editions"]} == {e1, e2}
    assert d["signals"] == ["title_publisher_language_date_equal"] and d["confidence"] == "probable"
    assert maintenance.run_detector("duplicate-editions")["opened"] == 0


def test_editions_of_different_works_or_without_publisher_do_not_match():
    _edition(_work("The Hobbit"))
    _edition(_work("The Hobbit", "Somebody Else"))
    w = _work("Other")
    _edition(w, publisher=None)
    _edition(w, publisher=None)
    assert maintenance.run_detector("duplicate-editions")["found"] == 0


def test_shared_authors_yield_single_pair_not_duplicated():
    """Two works sharing two authors are one pair, not one per shared author."""
    a = _work("The Hobbit", "J. R. R. Tolkien")
    b = _work("The Hobbit", "J. R. R. Tolkien")
    with db.tx() as conn:
        cid = db.contributor_id_for_name(conn, "Another Author")
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',1)", (a, cid))
        conn.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',1)", (b, cid))
    r = maintenance.run_detector("duplicate-works")
    assert r["complete"] and r["opened"] == 1
    (issue,) = _issues()
    assert issue["subject_id"] == ":".join(sorted([a, b]))


def test_large_same_title_disjoint_authors_yields_nothing():
    """Many same-title works with distinct authors produce no pairs, and the run completes."""
    n = 500
    t = db.now()
    with db.tx() as conn:
        for i in range(n):
            wid = f"wt{i:05d}"
            conn.execute(
                "INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)",
                (wid, "Same Title", "same title", t, t),
            )
            cid = f"ca{i:05d}"
            conn.execute(
                "INSERT INTO contributors(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (cid, f"Author {i}", t, t),
            )
            conn.execute(
                "INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                (wid, cid, "author", 0),
            )
    r = maintenance.run_detector("duplicate-works")
    assert r["complete"] and r["found"] == 0 and _issues() == []


def test_pair_cap_marks_run_incomplete_and_resolves_nothing():
    for _ in range(4):
        _work("Same Title")  # 6 pairs
    maintenance.run_detector("duplicate-works")
    assert len(_issues()) == 6
    r = maintenance.run_detector("duplicate-works", limit=3)
    assert r["complete"] is False and r["auto_resolved"] == 0
    assert all(i["status"] == "open" for i in _issues())


def test_pair_budget_is_global_across_title_groups():
    for title in ("Alpha Tale", "Beta Tale", "Gamma Tale"):
        for _ in range(4):
            _work(title)  # 6 pairs per title group, 18 in total
    limit = 7
    ctx = maintenance.DetectorContext(limit, 60)
    pairs = list(catalogdupes._work_pairs(catalogdupes._load_works(), ctx))
    assert len(pairs) == limit + 1  # generation stops one past the cap, across groups
    assert len({normalize_title(a["title"]) for a, _ in pairs}) > 1
    r = maintenance.run_detector("duplicate-works", limit=limit)
    assert r["complete"] is False and r["found"] == limit


def test_5000_works_run_without_quadratic_behaviour():
    t = db.now()
    n = 5000
    with db.tx() as conn:
        conn.execute("INSERT INTO contributors(id,name,created_at,updated_at) VALUES ('c1','Author One',?,?)", (t, t))
        conn.executemany("INSERT INTO works(id,title,sort_title,added_at,updated_at) VALUES (?,?,?,?,?)",
                         [(f"w{i:05d}", f"Title {i // 2}", "t", t, t) for i in range(n)])  # 2500 duplicate pairs
        conn.executemany("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,'c1','author',0)",
                         [(f"w{i:05d}",) for i in range(n)])
    start = time.perf_counter()
    r = maintenance.run_detector("duplicate-works")
    elapsed = time.perf_counter() - start
    assert r["found"] == 500 and r["complete"] is False  # capped at 500 pairs per run
    assert elapsed < 10, f"5,000 works took {elapsed:.1f}s (bound 10s)"
