"""Duplicate-Work candidates for the merge UI: series groups, a book's candidates, a picked set, the detector's
series-suffix handling and the false-positive guards (the owner's 39 Clues examples, verbatim)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import catalogdupes, maintenance, workdupes

from test_work_merge import _asset, _series, _work


RICK = "Rick Riordan"
CLUES = "The 39 Clues"


def _clues():
    """The 39 Clues as the owner sees it: each of two books present twice, the second copy with the series baked into its title."""
    ids = {
        "maze": _work("The Maze of Bones", RICK),
        "sword": _work("The sword thief", RICK),
        "sword_dup": _work("The Sword Thief (The 39 Clues, #3)", RICK),
        "circle": _work("The Black Circle", RICK),
        "circle_dup": _work("The Black Circle (The 39 Clues, #5)", RICK),
    }
    _series(ids["maze"], CLUES, "1")
    _series(ids["sword"], CLUES, "3")
    _series(ids["sword_dup"], CLUES, None)
    _series(ids["circle"], CLUES, "5")
    _series(ids["circle_dup"], CLUES, None)
    return ids


def _series_id(name=CLUES):
    return db.one("SELECT id FROM series WHERE name=?", (name,))["id"]


def test_39_clues_examples_make_exactly_two_groups():
    ids = _clues()
    groups = workdupes.series_groups(_series_id())
    assert sorted(sorted(g["work_ids"]) for g in groups) == sorted([sorted([ids["sword"], ids["sword_dup"]]), sorted([ids["circle"], ids["circle_dup"]])])
    # The Sword Thief and The Black Circle are different books of one series: never in one group.
    assert not any(ids["sword"] in g["work_ids"] and ids["circle"] in g["work_ids"] for g in groups)
    assert all(ids["maze"] not in g["work_ids"] for g in groups)
    assert all("title_equal_after_series_suffix" in g["signals"] and "same_authors" in g["signals"] for g in groups)


def test_the_owned_copy_is_the_suggested_canonical_and_the_clean_title_is_kept():
    ids = _clues()
    _asset(ids["circle_dup"], "audiobook", "m4b")      # the suffixed record holds the owned audiobook
    g = next(g for g in workdupes.series_groups(_series_id()) if ids["circle"] in g["work_ids"])
    s = g["suggestion"]
    assert s["canonical_work_id"] == ids["circle_dup"] and "owned assets" in s["reason"]
    assert s["title_after_merge"] == "The Black Circle" and s["resolutions"] == {"fields": {"title": "source"}}


def test_cards_carry_what_the_dialog_shows():
    ids = _clues()
    _asset(ids["circle_dup"], "audiobook", "m4b", files=["/x/a.m4b"])
    card = next(c for c in workdupes.work_cards([ids["circle_dup"]]))
    assert card["title"] == "The Black Circle (The 39 Clues, #5)" and card["stem"] == "The Black Circle" and card["suffix_position"] == "5"
    assert card["authors"] == [RICK] and card["owned_assets"] == 1 and card["assets"][0]["files"] == 1
    assert card["series"] == [{"id": _series_id(), "name": CLUES, "position": None}]
    assert {"progress", "tags", "identifiers", "added_at", "monitored", "kids_shelf", "cover_url"} <= set(card)


@pytest.mark.parametrize("title,series,stem,pos", [
    ("The Black Circle (The 39 Clues, #5)", None, "The Black Circle", "5"),
    ("The Sword Thief (The 39 Clues, #3)", CLUES, "The Sword Thief", "3"),
    ("Dune: Book 1", None, "Dune", "1"),
    ("Dune, #1", None, "Dune", "1"),
    ("Dune - Book 1", None, "Dune", "1"),
    ("Dune (Dune Chronicles, Book 1)", None, "Dune", "1"),
    ("Dune [Dune, #1]", None, "Dune", "1"),
    ("The 39 Clues: The Black Circle", CLUES, "The Black Circle", None),
    ("The 39 Clues #5: The Black Circle", CLUES, "The Black Circle", "5"),
    ("The Black Circle (The 39 Clues)", CLUES, "The Black Circle", None),
    ("Fahrenheit 451", None, "Fahrenheit 451", None),
    ("Catch-22", None, "Catch-22", None),
    ("(#5)", None, "(#5)", None),
])
def test_series_suffix_variants(title, series, stem, pos):
    assert catalogdupes.strip_series_suffix(title, series) == (stem, pos)


def test_title_key_makes_the_variants_equal_and_the_books_different():
    k = catalogdupes.title_key
    assert k("The Black Circle (The 39 Clues, #5)") == k("The Black Circle") == k("black circle")
    assert k("The Sword Thief") != k("The Black Circle")


def test_different_positions_never_group_even_with_identical_titles():
    a, b = _work("Book One", "A. Writer"), _work("Book One", "A. Writer")
    _series(a, "S", "1")
    _series(b, "S", "2")
    assert workdupes.series_groups(_series_id("S")) == []


def test_part_markers_with_different_numbers_are_two_books():
    a, b = _work("The Saga (Part 1)", "A. Writer"), _work("The Saga (Part 2)", "A. Writer")
    _series(a, "S", None)
    _series(b, "S", None)
    assert workdupes.series_groups(_series_id("S")) == []
    assert workdupes.work_candidates(a) == []


def test_different_authors_or_languages_do_not_group():
    a, b = _work("Same Title", "A. Writer"), _work("Same Title", "B. Other")
    c, d = _work("Wort", "A. Writer", primary_language="de"), _work("Wort", "A. Writer", primary_language="en")
    for w in (a, b, c, d):
        _series(w, "S", None)
    assert workdupes.series_groups(_series_id("S")) == []


def test_an_unplaced_copy_joins_only_when_the_title_has_a_single_position():
    p1, p2, loose = _work("Echo", "A. Writer"), _work("Echo (Echo, #2)", "A. Writer"), _work("Echo", "A. Writer")
    _series(p1, "S", "1")
    _series(p2, "S", None)
    _series(loose, "S", None)
    # 'Echo' at 1, 'Echo (...#2)' at 2 (from the title) and an unplaced 'Echo': two positions -> the unplaced one is ambiguous, nothing groups
    assert workdupes.series_groups(_series_id("S")) == []


def test_book_candidates_use_title_and_identifier_and_skip_other_volumes():
    ids = _clues()
    cands = workdupes.work_candidates(ids["circle"])
    assert [c["work_id"] for c in cands] == [ids["circle_dup"]]
    assert "title_equal_after_series_suffix" in cands[0]["signals"] and cands[0]["confidence"] == "high"
    twin = _work("Totally Different Title", "Someone Else")
    with db.tx() as conn:
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,'isbn','9781')", (ids["maze"],))
        conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES ('ed-twin',?,'Ed',1,1)", (twin,))
        conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition','ed-twin','isbn','9781')")
    c2 = workdupes.work_candidates(ids["maze"])
    assert [c["work_id"] for c in c2] == [twin] and c2[0]["signals"] == ["shared_identifier"]


def test_a_similar_title_by_the_same_author_is_a_probable_candidate():
    a, b = _work("The Lord of the Rings", "J. R. R. Tolkien"), _work("The Lord of the Ring", "J. R. R. Tolkien")
    c = workdupes.work_candidates(a)
    assert [x["work_id"] for x in c] == [b] and c[0]["signals"] == ["similar_title"] and c[0]["confidence"] == "probable"


def test_describe_warns_when_the_pick_is_not_one_book():
    ids = _clues()
    d = workdupes.describe([ids["circle"], ids["sword"]])
    assert [w["code"] for w in d["warnings"]] == ["different_books"] and len(d["cards"]) == 2
    assert workdupes.describe([ids["circle"], ids["circle_dup"]])["warnings"] == []


def test_detector_finds_suffix_variants_and_skips_other_volumes():
    ids = _clues()
    r = maintenance.run_detector("duplicate-works")
    assert r["complete"]
    pairs = {frozenset(i["subject_id"].split(":")) for i in db.all("SELECT subject_id FROM maintenance_issues WHERE kind IN ('duplicate_work','split_duplicate_work')")}
    assert pairs == {frozenset((ids["sword"], ids["sword_dup"])), frozenset((ids["circle"], ids["circle_dup"]))}
    issue = db.one("SELECT detail_json FROM maintenance_issues WHERE subject_id=?", (":".join(sorted((ids["circle"], ids["circle_dup"]))),))
    assert "series_suffix_variant" in db.json_load(issue["detail_json"])["signals"]


def test_detector_does_not_pair_two_volumes_that_share_a_stem():
    _work("The Saga (Part 1)", "A. Writer")
    _work("The Saga (Part 2)", "A. Writer")
    maintenance.run_detector("duplicate-works")
    assert db.all("SELECT 1 FROM maintenance_issues WHERE kind IN ('duplicate_work','split_duplicate_work')") == []


def test_candidates_api_is_admin_only_and_shapes_each_scope(monkeypatch):
    ids = _clues()
    client = TestClient(app)
    r = client.get("/api/v1/works/merge-candidates", params={"series_id": _series_id()})
    assert r.status_code == 200 and len(r.json()["groups"]) == 2 and all(g["cards"] and g["suggestion"] for g in r.json()["groups"])
    r = client.get("/api/v1/works/merge-candidates", params={"work_id": ids["sword"]})
    assert r.status_code == 200 and [c["work_id"] for c in r.json()["candidates"]] == [ids["sword_dup"]]
    r = client.get("/api/v1/works/merge-candidates", params={"work_ids": f"{ids['sword']},{ids['sword_dup']}"})
    assert r.status_code == 200 and r.json()["suggestion"]["canonical_work_id"] in (ids["sword"], ids["sword_dup"])
    assert client.get("/api/v1/works/merge-candidates", params={"work_ids": ids["sword"]}).status_code == 400
    assert client.get("/api/v1/works/merge-candidates").status_code == 400
    assert client.get("/api/v1/works/merge-candidates", params={"series_id": "nope"}).status_code == 404
    assert client.get("/api/v1/works/merge-candidates", params={"work_ids": f"{ids['sword']},missing"}).status_code == 404
