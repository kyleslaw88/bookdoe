"""The deterministic kids-shelf gate (services/kidsgate), driven through real records.

A table of realistic books is written to the database (works, contributors, editions,
series, metadata observations, prior classifications) and every row asserts what the gate
says about a confident "kids" verdict. Live incident, 2026-10-12: adult space opera and
LitRPG with a juvenile-sounding blurb scored 0.8-0.9 and were auto-approved on the strength
of title + description alone; none of them has a strong audience signal in its metadata.
"""
import pytest

from app import db
from app.services import kidsgate

BLURB_ADVENTURE = ("A teenage boy inherits a battered starship and an AI companion with a sense of humour, and sets "
                   "out on a sci-fi adventure across the nebula with nothing but his wits and a loyal crew.")
MG_BLURB = "Twelve-year-old Mia finds a map in her grandmother's attic and sets off with her dog to find the lost lighthouse."


def book(title, authors=("Some Author",), description=BLURB_ADVENTURE, publisher=None, series=None, obs=None, language="en"):
    """A work with the records the evidence gatherer reads. `series` = (name, position); `obs` = {field: value}."""
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,description,primary_language,added_at,updated_at) VALUES (?,?,?,?,?,?)", (wid, title, description, language, t, t))
        for i, name in enumerate(authors):
            row = c.execute("SELECT id FROM contributors WHERE name=?", (name,)).fetchone()
            cid = row["id"] if row else db.uid()
            if not row:
                c.execute("INSERT INTO contributors(id,name,created_at,updated_at) VALUES (?,?,?,?)", (cid, name, t, t))
            c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?, 'author',?)", (wid, cid, i))
        eid = db.uid()
        c.execute("INSERT INTO editions(id,work_id,publisher,pages,created_at,updated_at) VALUES (?,?,?,?,?,?)", (eid, wid, publisher, 320, t, t))
        if series:
            row = c.execute("SELECT id FROM series WHERE name=?", (series[0],)).fetchone()
            sid = row["id"] if row else db.uid()
            if not row:
                c.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, series[0], t, t))
            c.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (wid, sid, series[1]))
        for field, value in (obs or {}).items():
            c.execute("INSERT INTO metadata_observations(id,entity_type,entity_id,field_name,value,source,confidence,is_current,observed_at) "
                      "VALUES (?,'work',?,?,?,'provider',1,1,?)", (db.uid(), wid, field, value, t))
    return wid


def classify(wid, verdict, via=None, conf=0.9):
    """Record a prior outcome for a book: a classifier verdict and/or a decision."""
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO kids_classification(work_id,verdict,confidence,checked_at) VALUES (?,?,?,?)", (wid, verdict, conf, db.now()))
        if via:
            state = "off" if via in ("manual_untag", "proposal_rejected") else "on"
            c.execute("INSERT OR REPLACE INTO kids_decisions(work_id,state,via,decided_at) VALUES (?,?,?,?)", (wid, state, via, db.now()))


def gate(wid, *, verdict="kids", conf=0.97, threshold=0.95, require_strong=True, llm_adult=None):
    return kidsgate.evaluate_gate(kidsgate.gather_evidence(wid), verdict=verdict, confidence=conf, threshold=threshold,
                                  require_strong=require_strong, llm_adult_signals=llm_adult)


def ids(items):
    return {i["id"] for i in items}


# ------------------------------------------------------------------ the fixture table
def case_adult_space_opera_juvenile_blurb():
    return book("Backyard Starship", ("J.N. Chaney",), publisher="Variant Publishing")


def case_adult_space_opera_with_adult_category():
    return book("Wolves Among the Stars", ("J.N. Chaney",), publisher="Variant Publishing",
                obs={"categories": "Fiction / Science Fiction / Military; Fiction / LitRPG"})


def case_true_middle_grade_juvenile_category():
    return book("The Lighthouse Map", ("Ann Writer",), description=MG_BLURB, publisher="Scholastic Inc.",
                obs={"categories": "Juvenile Fiction / Action & Adventure / General"})


def case_children_publisher_only():
    return book("Puffin Tale", ("Pat Puffin",), description=MG_BLURB, publisher="Puffin Books")


def case_no_description_but_juvenile():
    return book("Mystery Title", description="", publisher="Scholastic", obs={"categories": "Juvenile Fiction"})


def case_short_description():
    return book("Short", description="A kids' book.", publisher="Scholastic")


def case_explicit_flag():
    return book("Flagged", description=MG_BLURB, publisher="Scholastic", obs={"maturity": "MATURE", "categories": "Juvenile Fiction"})


def case_not_mature_flag_is_fine():
    return book("Unflagged", description=MG_BLURB, obs={"maturity": "NOT_MATURE", "categories": "Juvenile Fiction"})


def case_age_range_observation():
    return book("Ages", description=MG_BLURB, obs={"age_range": "Ages 8-12"})


def case_age_range_open_ended():
    return book("Ages open", description=MG_BLURB, obs={"age_range": "12+"})


def case_age_range_ya():
    return book("Ages ya", description=MG_BLURB, obs={"age_range": "13-17"})


def case_age_range_in_description():
    return book("Blurb ages", description="A funny, fast-moving adventure about two sisters and a runaway goat. Perfect for ages 8-12 and for reading aloud.")


def case_negated_children_book():
    return book("Not Kids", description="Despite the cartoon cover this is not a children's book: it follows a mercenary crew through three bloody wars.")


def case_adult_text_marker():
    return book("GameLit", description="A LitRPG adventure where a young hero levels up in a virtual world, perfect for fans of progression systems and bold crews.",
                publisher="Scholastic", obs={"categories": "Juvenile Fiction"})


def case_young_adult_not_strong():
    return book("YA", description=MG_BLURB, obs={"categories": "Young Adult Fiction / Fantasy"})


def case_young_adult_not_hard():
    return book("YA2", description=MG_BLURB, publisher="Scholastic", obs={"categories": "Young Adult Fiction / Fantasy"})


TABLE = [
    # (case, passes, expected blockers (subset), expected strong ids, expected hard ids)
    (case_adult_space_opera_juvenile_blurb, False, {"no_strong_audience_signal"}, set(), set()),
    (case_adult_space_opera_with_adult_category, False, {"hard_adult_signals", "no_strong_audience_signal"}, set(), {"adult_genre"}),
    (case_true_middle_grade_juvenile_category, True, set(), {"juvenile_category", "children_publisher"}, set()),
    (case_children_publisher_only, True, set(), {"children_publisher"}, set()),
    (case_no_description_but_juvenile, False, {"description_too_short"}, {"juvenile_category", "children_publisher"}, set()),
    (case_short_description, False, {"description_too_short"}, {"children_publisher", "description_audience_statement"}, set()),
    # 2026-10-12 provider disagreement rule: a mature flag voids the juvenile signals, so the book also has no strong signal.
    (case_explicit_flag, False, {"hard_adult_signals", "no_strong_audience_signal"}, set(), {"maturity_flag"}),
    (case_not_mature_flag_is_fine, True, set(), {"juvenile_category"}, set()),
    (case_age_range_observation, True, set(), {"age_range_upper_bound"}, set()),
    (case_age_range_open_ended, False, {"no_strong_audience_signal"}, set(), set()),
    (case_age_range_ya, False, {"no_strong_audience_signal"}, set(), set()),
    (case_age_range_in_description, True, set(), {"age_range_upper_bound"}, set()),
    (case_negated_children_book, False, {"no_strong_audience_signal"}, set(), set()),
    (case_adult_text_marker, False, {"hard_adult_signals"}, {"juvenile_category", "children_publisher"}, {"adult_text_marker"}),
    (case_young_adult_not_strong, False, {"no_strong_audience_signal"}, set(), set()),
    (case_young_adult_not_hard, True, set(), {"children_publisher"}, set()),
]


@pytest.mark.parametrize("case,passes,blockers,strong,hard", TABLE, ids=[t[0].__name__ for t in TABLE])
def test_gate_outcome_for_realistic_books(case, passes, blockers, strong, hard):
    g = gate(case())
    assert g["passed"] is passes, g
    assert blockers <= set(g["blockers"]), g
    assert ids(g["strong_signals"]) == strong
    assert ids(g["adult_signals"]) == hard
    assert g["reasons"] or passes  # a refusal always says why


# ------------------------------------------------------------------ priors
def test_author_prior_blocks_even_a_juvenile_looking_book():
    a = book("Adult One", ("Dark Author",)); classify(a, "not_kids")
    b = book("Adult Two", ("Dark Author",)); classify(b, "not_kids", via="proposal_rejected")
    c = book("Sounds Young", ("Dark Author",), description=MG_BLURB, publisher="Scholastic", obs={"categories": "Juvenile Fiction"})
    g = gate(c)
    assert not g["passed"] and "hard_adult_signals" in g["blockers"]
    assert ids(g["adult_signals"]) == {"author_prior_not_kids"}


def test_one_not_kids_verdict_is_shown_to_the_model_but_is_not_a_hard_signal():
    w = book("Single adult", ("Pat Author",)); classify(w, "not_kids")
    c = book("Maybe young", ("Pat Author",), description=MG_BLURB, publisher="Scholastic")
    g = gate(c)
    assert g["passed"] and not g["adult_signals"]
    assert "Author prior (Pat Author): 1 other book(s) judged not_kids" in kidsgate.render_prompt(kidsgate.gather_evidence(c))


def test_author_prior_needs_a_majority_of_not_kids():
    for i in range(2):
        w = book(f"Kids {i}", ("Mixed Author",)); classify(w, "kids", via="proposal_approved")
    w = book("Adult", ("Mixed Author",)); classify(w, "not_kids")
    w = book("Adult 2", ("Mixed Author",)); classify(w, "not_kids")
    c = book("New one", ("Mixed Author",), description=MG_BLURB, publisher="Scholastic")
    assert gate(c)["passed"], "2 confirmed kids vs 2 not_kids is not a majority"


def test_auto_approved_siblings_never_vouch_for_each_other():
    """The auto-approvals are the thing being second-guessed: they cannot count as kids evidence."""
    for i in range(3):
        w = book(f"Auto {i}", ("Chaney",)); classify(w, "kids", via="auto_approved")
    for i in range(2):
        w = book(f"Known adult {i}", ("Chaney",)); classify(w, "not_kids")
    c = book("Next", ("Chaney",), description=MG_BLURB, publisher="Scholastic")
    g = gate(c)
    assert not g["passed"] and ids(g["adult_signals"]) == {"author_prior_not_kids"}


def test_series_with_kids_and_adult_entries():
    kid = book("Series Kid", ("A",), series=("Mixed Saga", "1")); classify(kid, "kids", via="manual_tag")
    adult1 = book("Series Adult 1", ("B",), series=("Mixed Saga", "2")); classify(adult1, "not_kids")
    adult2 = book("Series Adult 2", ("C",), series=("Mixed Saga", "3")); classify(adult2, "not_kids")
    entry = book("Series Entry", ("D",), description=MG_BLURB, publisher="Scholastic", series=("Mixed Saga", "4"))
    g = gate(entry)
    assert not g["passed"] and ids(g["adult_signals"]) == {"series_prior_not_kids"}
    clean = book("Clean 1", ("E",), series=("Kind Saga", "1")); classify(clean, "kids", via="proposal_approved")
    nxt = book("Clean 2", ("F",), description=MG_BLURB, publisher="Scholastic", series=("Kind Saga", "2"))
    assert gate(nxt)["passed"]


# ------------------------------------------------------------------ gate arithmetic
def test_threshold_and_verdict_conditions():
    wid = case_true_middle_grade_juvenile_category()
    assert gate(wid, conf=0.95)["passed"]
    assert gate(wid, conf=0.94)["blockers"] == ["below_threshold"]
    assert gate(wid, verdict="not_kids")["blockers"] == ["llm_not_kids"]
    assert gate(wid, verdict="uncertain")["blockers"] == ["llm_not_kids"]
    assert gate(wid, llm_adult=["description: sexual content"])["blockers"] == ["llm_reported_adult_signals"]


def test_require_strong_setting_relaxes_only_the_strong_signal_condition():
    wid = case_adult_space_opera_juvenile_blurb()
    assert gate(wid)["blockers"] == ["no_strong_audience_signal"]
    assert gate(wid, require_strong=False)["passed"]
    adult = case_adult_space_opera_with_adult_category()
    assert not gate(adult, require_strong=False)["passed"], "hard adult signals are never relaxed"
    assert not gate(case_short_description(), require_strong=False)["passed"], "a description is always required"


def test_signal_tables_are_the_single_pinned_definition():
    assert set(kidsgate.STRONG_SIGNALS) == {"juvenile_category", "children_publisher", "age_range_upper_bound", "description_audience_statement"}
    assert set(kidsgate.HARD_ADULT_SIGNALS) == {"maturity_flag", "adult_audience_category", "adult_genre", "adult_text_marker",
                                                "author_prior_not_kids", "series_prior_not_kids"}
    assert kidsgate.STRONG_AGE_UPPER_BOUND == 12 and kidsgate.MIN_DESCRIPTION_CHARS == 80
    seen_strong, seen_hard = set(), set()
    for case, *_ in TABLE:
        g = gate(case())
        seen_strong |= ids(g["strong_signals"]); seen_hard |= ids(g["adult_signals"])
    wid = book("Said so", description="A cheerful middle-grade novel about a brave little snail who races a rabbit across the whole county fair.")
    seen_strong |= ids(gate(wid)["strong_signals"])
    wid = book("Grown", obs={"categories": "Adult Fiction"}); seen_hard |= ids(gate(wid)["adult_signals"])
    assert seen_strong == set(kidsgate.STRONG_SIGNALS)
    assert seen_hard >= {"maturity_flag", "adult_audience_category", "adult_genre", "adult_text_marker"}


@pytest.mark.parametrize("value,expected", [("8-12", (8, 12)), ("Ages 8 to 12", (8, 12)), ("12+", (12, None)), ("8 and up", (8, None)),
                                            ({"min": 4, "max": 7}, (4, 7)), ("grown up", None), ("0-3", (0, 3))])
def test_parse_age_range(value, expected):
    assert kidsgate.parse_age_range(value) == expected


def test_grade_range_in_description_counts_only_through_grade_six():
    kid = book("Grades", description="A warm classroom story for grades 3-5 about a class pet that learns to read, with plenty of laughs.")
    assert "age_range_upper_bound" in ids(gate(kid)["strong_signals"])
    teen = book("Grades hs", description="A sharp story for grades 8-10 about a class pet that learns to read, with plenty of laughs.")
    assert not gate(teen)["strong_signals"]


# ------------------------------------------------------------------ evidence + prompt
def test_evidence_collects_only_what_exists():
    wid = book("Full", ("A. One", "B. Two"), description=MG_BLURB, publisher="Puffin", series=("Saga", "2"),
               obs={"categories": '["Juvenile Fiction", "Fantasy"]', "age_range": "8-12", "imprint": "Puffin Chapters"})
    with db.tx() as c:
        c.execute("UPDATE editions SET duration_seconds=7200 WHERE work_id=?", (wid,))
    ev = kidsgate.gather_evidence(wid)
    assert ev["authors"] == ["A. One", "B. Two"] and ev["publishers"] == ["Puffin"] and ev["categories"] == ["Juvenile Fiction", "Fantasy"]
    assert ev["series"] == [{"name": "Saga", "position": "2"}] and ev["pages"] == 320 and ev["duration_minutes"] == 120
    assert ev["age_range"] == ["8-12"] and ev["imprint"] == ["Puffin Chapters"] and ev["language"] == "en"
    bare = kidsgate.gather_evidence(book("Bare", description="x" * 100))
    assert not any(k in bare for k in ("categories", "subjects", "age_range", "maturity", "series", "publishers", "narrators"))


def test_prompt_carries_the_evidence_fields_and_says_what_is_missing():
    a = book("Prior One", ("Chaney",)); classify(a, "not_kids")
    wid = book("Void Drifter", ("Chaney",), publisher="Variant", series=("Void", "2"), obs={"categories": "Science Fiction / Space Opera"})
    text = kidsgate.render_prompt(kidsgate.gather_evidence(wid))
    assert text.splitlines()[0] == "Title: Void Drifter"
    for needle in ("Author(s): Chaney", "Series: Void #2", "Publisher: Variant", "Categories: Science Fiction / Space Opera",
                   "Author prior (Chaney): 1 other book(s) judged not_kids, 0 confirmed kids", "Not on record", "age range", "Description:"):
        assert needle in text, needle
    for needle in ("audience_evidence", "adult_signals", "NOT audience evidence by itself"):
        assert needle in kidsgate.KIDS_SYSTEM_PROMPT


# ------------------------------------------------------------------ strict parsing
GOOD = {"verdict": "kids", "confidence": 0.9, "reason": "r", "audience_evidence": ["categories: Juvenile Fiction"], "adult_signals": []}


def test_parse_answer_is_strict():
    assert kidsgate.parse_answer(GOOD)["audience_evidence"] == ["categories: Juvenile Fiction"]
    assert kidsgate.parse_answer({**GOOD, "verdict": "not_kids", "audience_evidence": [], "adult_signals": ["x"]})["verdict"] == "not_kids"
    assert kidsgate.parse_answer({"verdict": "uncertain", "confidence": 0.3})["adult_signals"] == []
    for bad in (None, "kids", [], {**GOOD, "verdict": "maybe"}, {**GOOD, "confidence": None}, {**GOOD, "confidence": "0.9"}, {**GOOD, "confidence": True},
                {**GOOD, "confidence": 1.5}, {**GOOD, "audience_evidence": []}, {**GOOD, "audience_evidence": "categories"},
                {**GOOD, "audience_evidence": [1]}, {k: v for k, v in GOOD.items() if k != "audience_evidence"}, {**GOOD, "adult_signals": "none"}):
        assert kidsgate.parse_answer(bad) is None, bad
