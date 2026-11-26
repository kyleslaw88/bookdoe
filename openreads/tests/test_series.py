import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

import db  # noqa: E402
import googlebooks as gb  # noqa: E402
import openlibrary as ol  # noqa: E402
import resources as res  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Wings of Fire -- bk. 6", ("Wings of Fire", "6")),
        ("Wings of Fire (6)", ("Wings of Fire", "6")),
        ("Wings of Fire (Book 6)", ("Wings of Fire", "6")),
        ("Wings of Fire #6", ("Wings of Fire", "6")),
        ("Wings of Fire, Book 6", ("Wings of Fire", "6")),
        ("Wings of Fire Book 6", ("Wings of Fire", "6")),
        ("Discworld ; 12", ("Discworld", "12")),
        ("Discworld, 0.5", ("Discworld", "0.5")),
        ("wings of fire", ("wings of fire", None)),
        ("Fahrenheit 451", ("Fahrenheit 451", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_series_string(raw, expected):
    assert ol.parse_series_string(raw) == expected


def test_structured_work_series(monkeypatch):
    monkeypatch.setattr(ol, "_series_name", lambda k: "Wings of Fire")
    raw = [{"series": {"key": "/series/OL335559L"}, "position": "6"}]
    assert ol._series_from_raw(raw) == [{"name": "Wings of Fire", "position": "6"}]


def test_merge_prefers_common_position_and_dedupes_case():
    extra = [
        {"name": "wings of fire", "position": None},
        {"name": "Wings of fire -- bk. 6", "position": None},
        {"name": "Wings of Fire", "position": "6"},
        {"name": "Wings of Fire", "position": "6"},
        {"name": "Wings of Fire", "position": "5"},
    ]
    assert ol._merge_series([], extra)[0]["position"] == "6"
    assert ol._merge_series([{"name": "Wings of Fire", "position": None}], extra) == [
        {"name": "Wings of Fire", "position": "6"}
    ]


def test_google_series_info():
    item = {
        "volumeInfo": {
            "title": "Moon Rising",
            "seriesInfo": {
                "bookDisplayNumber": "6",
                "volumeSeries": [{"seriesId": "abc", "orderNumber": 6}],
            },
        }
    }
    assert gb._volume_info(item)["series"] == [{"series_id": "abc", "position": "6"}]
    assert gb._volume_info({"volumeInfo": {}})["series"] == []


def test_end_to_end_response_shape(monkeypatch):
    db.init_db()
    monkeypatch.setattr(
        ol,
        "get_work",
        lambda k: {
            "title": "Moon Rising", "description": "", "author_keys": [],
            "series": [{"name": "Wings of Fire", "position": "6"}],
            "cover_url": None, "first_publish_date": None,
        },
    )
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    wid = db.get_or_create_work("OL1W", "Moon Rising")
    out = res.build_work_resource(wid, thin=True)
    (s,) = out["Series"]
    assert s["Title"] == "Wings of Fire"
    assert s["LinkItems"][0]["PositionInSeries"] == "6"
    assert s["LinkItems"][0]["SeriesPosition"] == 6
    # re-fetch with a corrected position updates the stored value
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": "Moon Rising", "description": "", "author_keys": [],
        "series": [{"name": "wings of fire", "position": "7"}],
        "cover_url": None, "first_publish_date": None})
    res.ensure_work(wid, force=True)
    assert db.series_for_work(wid)[0]["position"] == "7"
    assert len(db.series_for_work(wid)) == 1


def test_google_fills_missing_position(monkeypatch):
    db.init_db()
    wid = db.get_or_create_work("OL2W", "Moon Rising")
    sid = db.get_or_create_series("Wings of Fire")
    db.link_work_series(wid, sid, None)
    monkeypatch.setattr(gb, "find_by_isbn", lambda i: None)
    monkeypatch.setattr(
        gb, "find_by_title_author",
        lambda t, a=None: {"series": [{"series_id": "x", "position": "6"}]},
    )
    res._enrich_series_positions(wid, "Moon Rising")
    assert db.series_for_work(wid)[0]["position"] == "6"


def _link(series_id, work_id, pos):
    return {
        "Description": "", "ForeignId": series_id, "KCA": "", "Title": "Wings of Fire",
        "LinkItems": [{"ForeignWorkId": work_id, "PositionInSeries": str(pos),
                       "Primary": True, "SeriesPosition": pos}],
    }


def test_merge_series_unions_links_across_works():
    lists = [[_link(1, 10, 3)], [_link(1, 11, 1)], [_link(1, 12, 2), _link(2, 12, 1)],
             [_link(1, 10, 3)]]  # duplicate work link is not repeated
    out = res.merge_series(iter(lists))
    by_id = {s["ForeignId"]: s for s in out}
    assert [li["ForeignWorkId"] for li in by_id[1]["LinkItems"]] == [11, 12, 10]
    assert len(by_id[2]["LinkItems"]) == 1
    # inputs are not mutated
    assert len(lists[0][0]["LinkItems"]) == 1


def test_author_and_bulk_series_include_every_work(monkeypatch):
    db.init_db()
    per_work = {"OLA1W": "1", "OLA2W": "2", "OLA3W": "3"}
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "A", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works",
                        lambda k, limit=200: [{"ol_key": k2, "title": k2} for k2 in per_work])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": ["OLA1A"],
        "series": [{"name": "Wings of Fire", "position": per_work[k]}],
        "cover_url": None, "first_publish_date": None})
    aid = db.get_or_create_author("OLA1A")
    a = res.build_author_resource(aid, force=True)
    (s,) = a["Series"]
    assert len(s["LinkItems"]) == 3
    assert [li["PositionInSeries"] for li in s["LinkItems"]] == ["1", "2", "3"]
    assert {li["ForeignWorkId"] for li in s["LinkItems"]} == {w["ForeignId"] for w in a["Works"]}

    import main
    ids = [str(w["ForeignId"]) for w in a["Works"]]
    bulk = main._bulk_handler(ids)
    (bs,) = bulk["Series"]
    assert len(bs["LinkItems"]) == 3


def _sanitize_works_would_drop(works):
    """Mirror of the Readarr fork's SanitizeWorks: reject a work with an
    empty Books or empty Authors list."""
    return [w for w in works if not w["Books"] or not w["Authors"]]


def _stub_author(monkeypatch, work_keys, editions=None, languages=None):
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: languages or {})
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "A", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works",
                        lambda k, limit=200: [{"ol_key": k2, "title": k2} for k2 in work_keys])
    monkeypatch.setattr(ol, "get_work_editions", lambda k: (editions or {}).get(k, []))
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": [],
        "series": [{"name": "S", "position": "1"}], "cover_url": None, "first_publish_date": None})


def test_author_works_survive_sanitize_works(monkeypatch):
    db.init_db()
    _stub_author(monkeypatch, ["OLB1W", "OLB2W", "OLB3W"])
    aid = db.get_or_create_author("OLB1A")
    a = res.build_author_resource(aid, force=True)
    assert len(a["Works"]) == 3
    assert _sanitize_works_would_drop(a["Works"]) == []
    for w in a["Works"]:
        assert w["Authors"][0]["ForeignId"] == aid
        assert w["BestBookId"] == w["Books"][0]["ForeignId"] != 0
        assert w["Books"][0]["Contributors"] == [{"ForeignId": aid, "Role": "Author"}]
    # series links still merge across the (now surviving) works
    assert len(a["Series"][0]["LinkItems"]) == 3


def test_thin_placeholder_is_stable_and_adopted_by_real_edition(monkeypatch):
    db.init_db()
    _stub_author(monkeypatch, ["OLC1W"], editions={"OLC1W": [
        {"ol_key": "OLC1M", "title": "T", "isbn13": "9780000000001", "isbn10": None,
         "publisher": None, "release_date": None, "num_pages": None, "image_url": None,
         "physical_format": None}]})
    aid = db.get_or_create_author("OLC1A")
    first = res.build_author_resource(aid, force=True)["Works"][0]
    again = res.build_author_resource(aid)["Works"][0]
    placeholder_id = first["Books"][0]["ForeignId"]
    assert again["Books"][0]["ForeignId"] == placeholder_id  # no duplicate placeholders
    # full hydration later reuses that id for the first real edition
    full = res.build_work_resource(first["ForeignId"], enrich=False)
    assert [b["ForeignId"] for b in full["Books"]] == [placeholder_id]
    assert full["Books"][0]["Isbn13"] == "9780000000001"
    assert full["Books"][0]["KCA"] == "kca://book/openlibrary.OLC1M"
    assert len(db.editions_for_work(first["ForeignId"])) == 1


def test_author_works_english_only(monkeypatch):
    db.init_db()
    _stub_author(
        monkeypatch,
        ["OLD1W", "OLD2W", "OLD3W", "OLD4W", "OLD5W"],
        languages={
            "OLD1W": ["eng"],          # English
            "OLD2W": ["fre", "eng"],   # English work with a French edition
            "OLD3W": ["pol"],          # Polish only
            "OLD4W": ["spa", "fre"],   # no English
            # OLD5W: no language data at all
        },
    )
    aid = db.get_or_create_author("OLD1A")
    got = {w["Title"] for w in res.build_author_resource(aid, force=True)["Works"]}
    assert got == {"OLD1W", "OLD2W", "OLD5W"}  # unknown included by default
    assert _sanitize_works_would_drop(res.build_author_resource(aid)["Works"]) == []
    # series links only reference surviving works
    ids = {w["ForeignId"] for w in res.build_author_resource(aid)["Works"]}
    for s in res.build_author_resource(aid)["Series"]:
        assert {li["ForeignWorkId"] for li in s["LinkItems"]} <= ids

    monkeypatch.setattr(res, "UNKNOWN_LANGUAGE_POLICY", "exclude")
    got = {w["Title"] for w in res.build_author_resource(aid)["Works"]}
    assert got == {"OLD1W", "OLD2W"}


def test_cached_author_without_language_data_is_relisted(monkeypatch):
    db.init_db()
    _stub_author(monkeypatch, ["OLE1W", "OLE2W"], languages={"OLE1W": ["eng"], "OLE2W": ["ger"]})
    aid = db.get_or_create_author("OLE1A")
    # simulate rows cached before language tracking existed
    for k in ("OLE1W", "OLE2W"):
        db.link_work_author(db.get_or_create_work(k, k), aid)
    got = {w["Title"] for w in res.build_author_resource(aid)["Works"]}
    assert got == {"OLE1W"}


def test_is_english_work():
    assert res.is_english_work("eng") and res.is_english_work("fre,eng")
    assert not res.is_english_work("pol") and not res.is_english_work("spa,fre")
    assert res.is_english_work("") and res.is_english_work(None) and res.is_english_work("und")


# ---- duplicate-author aliases ------------------------------------------

def _stub_dup_author(monkeypatch):
    works = {"OLDUPA": ["OLX1W", "OLX2W"], "OLCANONA": ["OLX3W", "OLX4W", "OLX2W"]}
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "Dup Person", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works",
                        lambda k, limit=200: [{"ol_key": w, "title": w} for w in works[k]])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": [], "series": [],
        "cover_url": None, "first_publish_date": None})


def test_alias_resolution_and_groups(monkeypatch):
    import aliases
    monkeypatch.setitem(aliases.AUTHOR_ALIASES, "OLDUPA", "OLMID")
    monkeypatch.setitem(aliases.AUTHOR_ALIASES, "OLMID", "OLCANONA")  # chain
    assert aliases.canonical_key("OLDUPA") == "OLCANONA"
    assert aliases.canonical_key("OLCANONA") == "OLCANONA"
    assert aliases.canonical_key("OLUNRELATED") == "OLUNRELATED"
    assert aliases.group_keys("OLDUPA") == ["OLCANONA", "OLDUPA", "OLMID"]
    monkeypatch.setitem(aliases.AUTHOR_ALIASES, "OLCANONA", "OLDUPA")  # cycle terminates
    aliases.canonical_key("OLDUPA")


def test_duplicate_author_keys_resolve_to_one_author(monkeypatch):
    import aliases
    db.init_db()
    monkeypatch.setitem(aliases.AUTHOR_ALIASES, "OLDUPA", "OLCANONA")
    _stub_dup_author(monkeypatch)
    a = db.get_or_create_author("OLCANONA")
    assert db.get_or_create_author("OLDUPA") == a
    out = res.build_author_resource(a, force=True)
    assert out["ForeignId"] == a
    # union of both records' works, shared work not duplicated
    assert sorted(w["Title"] for w in out["Works"]) == ["OLX1W", "OLX2W", "OLX3W", "OLX4W"]
    assert all(w["Authors"][0]["ForeignId"] == a for w in out["Works"])
    assert _sanitize_works_would_drop(out["Works"]) == []


def test_preexisting_duplicate_rows_are_merged(monkeypatch):
    """Simulates prod: both keys already have separate ids/books before the
    alias exists; init_db() must fold them together and the old id must
    keep resolving."""
    import aliases
    db.init_db()
    dup = db.get_or_create_author("OLDUPB")   # created before alias existed
    canon = db.get_or_create_author("OLCANONB")
    w1 = db.get_or_create_work("OLY1W", "OLY1W")
    w2 = db.get_or_create_work("OLY2W", "OLY2W")
    db.link_work_author(w1, dup)
    db.link_work_author(w2, canon)
    assert dup != canon
    monkeypatch.setitem(aliases.AUTHOR_ALIASES, "OLDUPB", "OLCANONB")
    db.init_db()
    db.init_db()  # idempotent
    assert {w["id"] for w in db.works_for_author(canon)} == {w1, w2}
    assert db.works_for_author(dup) == []
    assert db.author_by_id(dup)["id"] == canon
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "P", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works", lambda k, limit=200: [])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": [], "series": [],
        "cover_url": None, "first_publish_date": None})
    by_old_id = res.build_author_resource(dup)
    by_new_id = res.build_author_resource(canon)
    assert by_old_id["ForeignId"] == by_new_id["ForeignId"] == canon
    assert len(by_old_id["Works"]) == len(by_new_id["Works"]) == 2


def test_env_aliases(monkeypatch):
    import aliases
    monkeypatch.setenv("OPENREADS_AUTHOR_ALIASES", '{"OLENVDUP": "OLENVCANON"}')
    assert aliases.canonical_key("OLENVDUP") == "OLENVCANON"


# ---- series-name normalization -------------------------------------------

import seriesnorm  # noqa: E402


def test_series_variants_share_one_key():
    variants = [
        "Molly Murphy", "A Molly Murphy mystery", "The Molly Murphy mysteries",
        "Molly Murphy Mystery", "molly murphy mysteries.",
    ]
    assert len({seriesnorm.norm_key(v) for v in variants}) == 1
    assert seriesnorm.norm_key("A royal spyness msytery") == seriesnorm.norm_key("Royal Spyness")
    assert seriesnorm.pick_canonical(variants) == "Molly Murphy"


def test_series_distinct_names_are_not_merged():
    for a, b in [
        ("Wings of Fire", "Wings of Fire Graphic Novel"),
        ("Wings of Fire", "Wings of fire. Legends"),
        ("Discworld", "The Long Earth"),
        ("Book 1", "Book 2"),
        ("Harry Potter 1", "Harry Potter 2"),
        ("The Lord of the Rings", "The Hobbit"),
    ]:
        assert not seriesnorm.similar(seriesnorm.norm_key(a), seriesnorm.norm_key(b)), (a, b)
    # a single legitimate series keeps its own name, article and all
    assert seriesnorm.pick_canonical(["The Lord of the Rings"]) == "The Lord of the Rings"


@pytest.mark.parametrize("name", [
    "Thorndike Press large print core", "(Thorndike Press Large Print Core Series)",
    "The Beeler Large Print mystery series", "Berkley Prime Crime mystery",
    "Random House Book Club", "Acme Publishing", "Anniversary Edition", "Kindle edition",
])
def test_imprints_detected(name):
    assert seriesnorm.is_imprint(name)


@pytest.mark.parametrize("name", [
    "Wings of Fire", "Discworld", "Molly Murphy", "The Lord of the Rings",
    "Thursday Murder Club", "Spirit Animals", "Constable Evan Evans Mystery",
])
def test_real_series_not_flagged_as_imprints(name):
    assert not seriesnorm.is_imprint(name)


def test_openlibrary_drops_imprints_and_parses_bracket_position():
    out = ol._series_from_raw([
        "Thorndike Press large print core", "Molly Murphy mysteries -- [16]", "Some Series",
    ])
    assert out == [
        {"name": "Molly Murphy mysteries", "position": "16"},
        {"name": "Some Series", "position": None},
    ]


def test_variants_collapse_in_db_and_response(monkeypatch):
    db.init_db()
    variant_by_work = {"OLV1W": "A Molly Murphy mystery", "OLV2W": "The Molly Murphy mysteries",
                       "OLV3W": "Molly Murphy", "OLV4W": "Thorndike Press large print core"}
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": [],
        "series": [{"name": variant_by_work[k], "position": k[3]}],
        "cover_url": None, "first_publish_date": None})
    ids = [db.get_or_create_work(k, k) for k in variant_by_work]
    for i in ids:
        res.build_work_resource(i, thin=True)  # ingest every variant first
    outs = [res.build_work_resource(i, thin=True) for i in ids]
    names = {(s["ForeignId"], s["Title"]) for o in outs for s in o["Series"]}
    assert len({n[0] for n in names}) == 1 and {n[1] for n in names} == {"Molly Murphy"}
    assert outs[3]["Series"] == []  # imprint dropped
    merged = res.merge_series(o["Series"] for o in outs)
    assert len(merged) == 1 and len(merged[0]["LinkItems"]) == 3


def test_clean_series_merges_and_purges_existing_rows():
    db.init_db()
    with db.get_conn() as c:  # rows as stored before normalization existed
        for n in ("Royal Spyness", "A royal spyness mystery", "Royal Spyness mysteries",
                  "Thorndike Press large print core", "Some Other Series"):
            c.execute("INSERT INTO series (name) VALUES (?)", (n,))
    wids = [db.get_or_create_work(f"OLZ{i}W", "t") for i in range(5)]
    with db.get_conn() as c:
        ids = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM series")}
        for w, n, pos in zip(wids, ("Royal Spyness", "A royal spyness mystery",
                                     "Royal Spyness mysteries", "Thorndike Press large print core",
                                     "Some Other Series"), ("1", "2", "3", None, None)):
            c.execute("INSERT INTO work_series (work_id, series_id, position) VALUES (?,?,?)",
                      (w, ids[n], pos))
    db.init_db()
    db.init_db()  # idempotent
    with db.get_conn() as c:
        names = sorted(r["name"] for r in c.execute("SELECT name FROM series"))
        assert names.count("Royal Spyness") == 1
        assert not any("Thorndike" in n for n in names)
        assert "Some Other Series" in names
        rs = c.execute("SELECT id FROM series WHERE name='Royal Spyness'").fetchone()["id"]
        got = sorted(r["position"] for r in c.execute(
            "SELECT position FROM work_series WHERE series_id=?", (rs,)))
        assert got == ["1", "2", "3"]
        assert c.execute("SELECT COUNT(*) FROM work_series WHERE work_id=?", (wids[3],)).fetchone()[0] == 0


def test_collins_style_aliases_configured():
    import aliases
    for dup in ("OL15002457A", "OL7922172A"):
        assert aliases.canonical_key(dup) == "OL22463A"
    assert aliases.group_keys("OL7922172A")[0] == "OL22463A"


def test_merged_variants_without_bare_form_get_bare_display_name():
    assert seriesnorm.bare_form("A Royal Spyness mystery") == "Royal Spyness"
    assert seriesnorm.bare_form("The Molly Murphy mysteries") == "Molly Murphy"
    db.init_db()
    a = db.get_or_create_series("A Zed Quux mystery")
    assert db.series_by_id(a)["name"] == "A Zed Quux mystery"  # lone name untouched
    assert db.get_or_create_series("The Zed Quux mysteries") == a
    assert db.series_by_id(a)["name"] == "Zed Quux"
    with db.get_conn() as c:
        for n in ("A Foo Bar mystery", "The Foo Bar mysteries"):
            c.execute("INSERT INTO series (name) VALUES (?)", (n,))
    db.init_db()
    with db.get_conn() as c:
        assert [r["name"] for r in c.execute("SELECT name FROM series WHERE name LIKE '%Foo Bar%'")] == ["Foo Bar"]


def test_author_response_uses_one_title_per_series(monkeypatch):
    db.init_db()
    variants = {"OLQ1W": "A Zorp Blat mystery", "OLQ2W": "The Zorp Blat mysteries", "OLQ3W": "Zorp Blat"}
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "A", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works",
                        lambda k, limit=200: [{"ol_key": w, "title": w} for w in variants])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": k, "description": "", "author_keys": [], "series": [{"name": variants[k], "position": k[3]}],
        "cover_url": None, "first_publish_date": None})
    a = res.build_author_resource(db.get_or_create_author("OLQAUTH"), force=True)
    assert [s["Title"] for s in a["Series"]] == ["Zorp Blat"]
    assert {s["Title"] for w in a["Works"] for s in w["Series"]} == {"Zorp Blat"}


# ---- duplicate-title works / graphic-novel names ---------------------------

def _stub_dups(monkeypatch, works, author_of=None, auth="OLDUPAUTH"):
    """works: {ol_key: (title, series-list-or-None)}; every work under OLDUPAUTH
    unless author_of overrides."""
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "A", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works",
                        lambda k, limit=200: [{"ol_key": w, "title": t} for w, (t, _) in works.items()
                                              if (author_of or {}).get(w, auth) == k])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": works[k][0], "description": "", "author_keys": [], "series": works[k][1] or [],
        "cover_url": None, "first_publish_date": None})


def _links_by_work(resource):
    return {w["ForeignId"]: [(s["Title"], s["LinkItems"][0]["PositionInSeries"]) for s in w["Series"]]
            for w in resource["Works"]}


@pytest.mark.parametrize("order", [("OLW1", "OLW2"), ("OLW2", "OLW1")])
def test_series_propagates_across_duplicate_title_works(monkeypatch, order):
    db.init_db()
    tagged = [{"name": "Zed Series", "position": "1"}]
    works = {order[0] + "P": ("Watsons Case", None), order[1] + "P": ("Watson's Case", tagged),
             order[0] + "Q": ("Untagged Other Book", None)}
    auth = "OLAUTH1" + order[0]
    _stub_dups(monkeypatch, works, auth=auth)
    aid = db.get_or_create_author(auth)
    a = res.build_author_resource(aid, force=True)
    by_title = {w["Title"]: w for w in a["Works"]}
    assert len(a["Works"]) == 3
    for t in ("Watsons Case", "Watson's Case"):
        assert [(s["Title"], s["LinkItems"][0]["PositionInSeries"]) for s in by_title[t]["Series"]] == [("Zed Series", "1")]
    assert by_title["Untagged Other Book"]["Series"] == []
    (s,) = a["Series"]
    assert {li["ForeignWorkId"] for li in s["LinkItems"]} == {
        by_title["Watsons Case"]["ForeignId"], by_title["Watson's Case"]["ForeignId"]}
    # duplicates are not merged or deleted, and nothing is persisted for the untagged twin
    assert len(db.works_for_author(aid)) == 3
    untagged = next(w for w in a["Works"] if w["Title"] == "Watsons Case")["ForeignId"]
    assert db.series_for_work(untagged) == []


def test_single_work_lookup_inherits_from_unfetched_twin(monkeypatch):
    """/work/{id} path: the twin hasn't been fetched yet."""
    db.init_db()
    works = {"OLS1W": ("Twin Book", None), "OLS2W": ("The Twin Book", [{"name": "Qux", "position": "4"}])}
    _stub_dups(monkeypatch, works, auth="OLAUTH2")
    aid = db.get_or_create_author("OLAUTH2")
    w1 = db.get_or_create_work("OLS1W", "Twin Book"); w2 = db.get_or_create_work("OLS2W", "The Twin Book")
    db.link_work_author(w1, aid); db.link_work_author(w2, aid)
    out = res.build_work_resource(w1, thin=True)
    assert [(s["Title"], s["LinkItems"][0]["PositionInSeries"]) for s in out["Series"]] == [("Qux", "4")]
    assert out["Series"][0]["LinkItems"][0]["ForeignWorkId"] == w1


def test_same_title_by_different_author_does_not_inherit(monkeypatch):
    db.init_db()
    works = {"OLG1W": ("Shared Title", None), "OLG2W": ("Shared Title", [{"name": "Other Series", "position": "2"}])}
    _stub_dups(monkeypatch, works, author_of={"OLG2W": "OLOTHERAUTH3"}, auth="OLAUTH3")
    a1 = db.get_or_create_author("OLAUTH3"); a2 = db.get_or_create_author("OLOTHERAUTH3")
    res.build_author_resource(a2, force=True)
    out = res.build_author_resource(a1, force=True)
    assert [w["Series"] for w in out["Works"]] == [[]]


def test_own_position_wins_and_missing_position_is_filled(monkeypatch):
    db.init_db()
    works = {"OLP1W": ("Pos Book", [{"name": "Pos Series", "position": None}]),
             "OLP2W": ("Pos Book", [{"name": "Pos Series", "position": "3"}])}
    _stub_dups(monkeypatch, works, auth="OLAUTH4")
    a = res.build_author_resource(db.get_or_create_author("OLAUTH4"), force=True)
    assert {s["LinkItems"][0]["PositionInSeries"] for w in a["Works"] for s in w["Series"]} == {"3"}


def test_graphic_novel_series_names_survive_normalizer():
    for n in ("Heroes of Olympus: The Graphic Novel", "The Heroes of Olympus: The Graphic Novels",
              "Wings of Fire Graphic Novel"):
        assert "novel" in seriesnorm.norm_key(n)
        assert "graphic novel" in seriesnorm.bare_form(n).lower()
    # the graphic-novel line is a different series from the main one...
    assert not seriesnorm.similar(seriesnorm.norm_key("The Heroes of Olympus"),
                                  seriesnorm.norm_key("Heroes of Olympus: The Graphic Novels"))
    # ...but singular/plural spellings of it are one series
    assert seriesnorm.similar(seriesnorm.norm_key("Heroes of Olympus: The Graphic Novel"),
                              seriesnorm.norm_key("The Heroes of Olympus: The Graphic Novels"))
    # ordinary generic suffixes are still stripped
    assert seriesnorm.norm_key("Discworld novels") == "discworld"
    assert seriesnorm.norm_key("Royal Spyness mysteries") == "royal spyness"


def test_graphic_novel_variants_merge_with_full_name():
    db.init_db()
    a = db.get_or_create_series("The Heroes of Olympus: The Graphic Novels")
    assert db.get_or_create_series("Heroes of Olympus: The Graphic Novel") == a
    assert "Graphic Novel" in db.series_by_id(a)["name"]
    assert db.get_or_create_series("The Heroes of Olympus") != a


def test_stored_bracket_position_fragment_is_cleaned():
    db.init_db()
    with db.get_conn() as c:
        for n in ("Frag Zork", "Frag Zork mysteries -- [16]"):
            c.execute("INSERT INTO series (name) VALUES (?)", (n,))
    w1 = db.get_or_create_work("OLF1W", "t"); w2 = db.get_or_create_work("OLF2W", "t")
    with db.get_conn() as c:
        ids = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM series")}
        c.execute("INSERT INTO work_series (work_id, series_id, position) VALUES (?,?,?)", (w1, ids["Frag Zork"], "1"))
        c.execute("INSERT INTO work_series (work_id, series_id, position) VALUES (?,?,NULL)", (w2, ids["Frag Zork mysteries -- [16]"]))
    db.init_db()
    with db.get_conn() as c:
        rows = c.execute("SELECT id,name FROM series WHERE name LIKE 'Frag Zork%'").fetchall()
        assert [r["name"] for r in rows] == ["Frag Zork"]
        got = sorted((r["work_id"], r["position"]) for r in c.execute(
            "SELECT work_id, position FROM work_series WHERE series_id=?", (rows[0]["id"],)))
        assert got == [(w1, "1"), (w2, "16")]


def test_update_rejects_unknown_column(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "openreads.db"))
    db.init_db()
    wid = db.get_or_create_work("/works/OL1W")
    with pytest.raises(ValueError):
        db.update_work(wid, **{"title=1, ol_key": "x"})
    with pytest.raises(ValueError):
        db.update_author(1, bogus="x")
    with pytest.raises(ValueError):
        db.update_edition(1, **{"x": 1})
    db.update_work(wid, title="Fine")
    with db.get_conn() as c:
        assert c.execute("SELECT title FROM works WHERE id=?", (wid,)).fetchone()[0] == "Fine"
