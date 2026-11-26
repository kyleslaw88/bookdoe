import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "abdb.db"))

import audiobookdb as abdb  # noqa: E402
import db  # noqa: E402
import openlibrary as ol  # noqa: E402
import resources as res  # noqa: E402
import seriesnorm  # noqa: E402


def _book(title, author, series=None, pos="1", sid="S1"):
    b = {"title": title, "id": "b_" + title,
         "people": [{"credit": f"{author}|Author", "person": {"id": "p", "name": author},
                     "role": {"name": "Author"}}]}
    if series:
        b["seriesWithId"] = [{"id": sid, "title": series, "ordinal": pos,
                              "position": {"value": pos, "kind": "point", "sortKey": 1}}]
        b["series"] = [series]
    return b


@pytest.fixture
def abdb_on(monkeypatch):
    """Enable the source with a fake search function; returns the call log."""
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    monkeypatch.setattr(abdb, "_bucket", abdb.TokenBucket(1000, 1))  # not the subject of most tests
    with db.get_conn() as c:
        c.execute("DELETE FROM enrichment_cache")
        c.execute("DELETE FROM source_usage")
    state = {"calls": [], "results": {}}

    def fake(query):
        state["calls"].append(query)
        for needle, results in state["results"].items():
            if needle.lower() in query.lower():
                return results
        return []

    monkeypatch.setattr(abdb, "_post_search", fake)
    return state


# ---- parsing / matching -------------------------------------------------------

@pytest.mark.parametrize("raw,exp", [
    ("1", "1"), (1, "1"), ("Book 1", "1"), ("2, Dramatized Adaptation", "2"),
    ("0.5", "0.5"), (".5", ".5"), ("1-3", "1-3"), ("", None), (None, None), ("Prequel", None),
])
def test_clean_position(raw, exp):
    assert abdb.clean_position(raw) == exp


def test_same_person():
    assert abdb.same_person("Wilkie Collins", "wilkie  collins")
    assert abdb.same_person("Tui T. Sutherland", "Tui Sutherland")
    assert not abdb.same_person("Wilkie Collins", "Rachel Collins")
    assert not abdb.same_person("Wilkie Collins", "Peter Collins")
    assert not abdb.same_person("", "Wilkie Collins")


def test_core_title_and_scoring():
    assert abdb.core_title("A Royal Pain (A Royal Spyness Mystery)") == abdb.core_title("A Royal Pain")
    assert abdb.core_title("Watsons Case") == abdb.core_title("Watson's Case")
    assert abdb.core_title("Moon Rising, Book 6") == abdb.core_title("Moon Rising")
    assert abdb._title_score("Hawkes Above", "Hawkes Above") == 1.0
    assert abdb._title_score("Hawkes Above", "Hawk Help Us") < abdb.MIN_TITLE_SCORE


def test_numbered_titles_do_not_fuzzy_match_other_numbers():
    assert abdb._title_score("The Thursday Murder Club 2", "The Thursday Murder Club") == 0.0
    assert abdb._title_score("Thursday Murder Club 2", "Thursday Murder Club 3") == 0.0
    assert abdb._title_score("Thursday Murder Club : (the Thursday Murder Club 1)", "The Thursday Murder Club") == 1.0
    results = [_book("The Thursday Murder Club", "Richard Osman", "Thursday Murder Club", "1")]
    assert abdb.pick_match(results, "The Thursday Murder Club 2", ["Richard Osman"]) is None


def test_pick_match_requires_author_and_title():
    results = [
        _book("Watson's Case", "Lori Foster", "Other", "2"),       # same title, wrong author
        _book("Watson's Case Reloaded", "Wilkie Collins", "Nope", "9"),  # right author, other title
        _book("Watson's Case", "Wilkie Collins", "Molly Murphy Mysteries", "1"),
    ]
    m = abdb.pick_match(results, "Watsons Case", ["Wilkie Collins"])
    assert m["seriesWithId"][0]["title"] == "Molly Murphy Mysteries"
    assert abdb.pick_match(results, "Watsons Case", ["Somebody Else"]) is None
    assert abdb.pick_match([], "x", ["y"]) is None


def test_pick_match_prefers_edition_with_series():
    results = [_book("Hawkes Above", "Wilkie Collins"), _book("Hawkes Above", "Wilkie Collins", "Constable Evans", "1")]
    assert abdb._series_of(abdb.pick_match(results, "Hawkes Above", ["Wilkie Collins"]))[0]["name"] == "Constable Evans"


def test_series_of_drops_imprints_and_reads_list_shape():
    b = _book("X", "A", "Thorndike Press large print core")
    assert abdb._series_of(b) == []
    listy = {"series": [{"series": {"id": "s", "title": "Old Shape"}, "position": {"value": "4"}}]}
    assert abdb._series_of(listy) == [{"name": "Old Shape", "position": "4", "external_id": "s"}]


# ---- find_series: caching, misses, failures ------------------------------------

def test_find_series_hit_is_cached(abdb_on):
    abdb_on["results"]["Watsons Case"] = [_book("Watson's Case", "Wilkie Collins", "Molly Murphy Mysteries", "1")]
    first = abdb.find_series("Watsons Case", ["Wilkie Collins"])
    assert first == [{"name": "Molly Murphy Mysteries", "position": "1", "external_id": "S1"}]
    assert abdb.find_series("Watsons Case", ["Wilkie Collins"]) == first
    assert abdb.find_series("The Watsons Case", ["wilkie collins"]) == first  # same key: article/case-insensitive
    assert len(abdb_on["calls"]) == 1


def test_find_series_not_found_is_graceful_and_cached(abdb_on):
    assert abdb.find_series("Christmas at Baker Street", ["Wilkie Collins"]) == []
    assert abdb.find_series("Christmas at Baker Street", ["Wilkie Collins"]) == []
    assert len(abdb_on["calls"]) == 1
    # standalone: found, but no series data -> also []
    abdb_on["results"]["Farleigh"] = [_book("In Cranford Field", "Wilkie Collins")]
    assert abdb.find_series("In Cranford Field", ["Wilkie Collins"]) == []


def test_find_series_failure_is_not_cached(abdb_on, monkeypatch):
    monkeypatch.setattr(abdb, "_post_search", lambda q: None)  # timeout / limit
    assert abdb.find_series("Some Book", ["Some Author"]) is None
    assert db.enrichment_get("audiobookdb", abdb.lookup_key("Some Book", ["Some Author"])) is None


def test_find_series_skips_bundles_and_missing_author_and_offline(abdb_on):
    assert abdb.find_series("Thursday Murder Club 5 Book Mystery Box Set", ["Richard Osman"]) is None
    assert abdb.find_series("Some Title", []) is None
    assert abdb.find_series("Some Title", ["Someone"], allow_network=False) is None
    assert abdb_on["calls"] == []


def test_disabled_makes_no_calls(abdb_on, monkeypatch):
    monkeypatch.setattr(abdb, "ENABLED", False)
    assert abdb.find_series("Hawkes Above", ["Wilkie Collins"]) is None
    assert abdb_on["calls"] == []


# ---- rate limiting / budget -------------------------------------------------------

def _mock_client(monkeypatch, handler):
    monkeypatch.setattr(abdb, "_client", httpx.Client(base_url="https://x/api", transport=httpx.MockTransport(handler)))


def test_requests_are_spaced(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    monkeypatch.setattr(abdb, "_next_call_at", 0.0)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json=[]))
    # abdb.time.time is a fake clock that only the mocked sleep advances, so the
    # spacing maths never sees real time passing between calls (a slow runner
    # used to shorten each wait by the milliseconds elapsed since the last call)
    clock = [1000.0]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(abdb.time, "time", lambda: clock[0])
    monkeypatch.setattr(abdb.time, "sleep", fake_sleep)
    for _ in range(4):
        abdb._post_search("q")
    # the first request goes out at once; each later one waits exactly one MIN_INTERVAL
    assert len(sleeps) == 3
    for s in sleeps:
        assert s == pytest.approx(abdb.MIN_INTERVAL, abs=0.001)
    assert clock[0] == pytest.approx(1000.0 + 3 * abdb.MIN_INTERVAL, abs=0.001)
    assert 1 / abdb.MIN_INTERVAL <= 5          # never above the anonymous 5 req/s


def test_daily_cap_stops_requests(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    monkeypatch.setattr(abdb, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(abdb, "DAILY_CAP", 3)
    with db.get_conn() as c:
        c.execute("DELETE FROM source_usage")
    hits = []
    _mock_client(monkeypatch, lambda req: (hits.append(1), httpx.Response(200, json=[]))[1])
    for _ in range(6):
        abdb._post_search("q")
    assert len(hits) == 3 and db.usage_count("audiobookdb", abdb._today()) == 3


def test_server_budget_header_pauses_for_the_day(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    monkeypatch.setattr(abdb, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(abdb, "DAILY_CAP", 10_000)
    hits = []
    _mock_client(monkeypatch, lambda req: (hits.append(1), httpx.Response(200, json=[], headers={"x-ratelimit-budget": "5"}))[1])
    assert abdb._post_search("q") == []
    assert abdb._post_search("q") is None and len(hits) == 1


def test_429_and_5xx_back_off(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(abdb, "DAILY_CAP", 10_000)
    for status in (429, 503):
        monkeypatch.setattr(abdb, "_backoff_until", 0.0)
        monkeypatch.setattr(abdb, "_exhausted_day", None)
        hits = []
        _mock_client(monkeypatch, lambda req, s=status: (hits.append(1), httpx.Response(s, headers={"retry-after": "30"}))[1])
        assert abdb._post_search("q") is None
        assert abdb._post_search("q") is None and len(hits) == 1  # second call blocked by backoff


def test_timeout_backs_off(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    def boom(req):
        raise httpx.ConnectTimeout("t")
    _mock_client(monkeypatch, boom)
    assert abdb._post_search("q") is None
    assert abdb._backoff_until > 0


# ---- series-name matching across sources -----------------------------------------------

def test_cross_source_series_names_normalize_together():
    k = seriesnorm.norm_key
    assert seriesnorm.similar(k("Molly Murphy Mysteries"), k("Molly Murphy"))
    assert seriesnorm.similar(k("Constable Evans"), k("Constable Evan Evans Mystery"))
    assert seriesnorm.similar(k("Royal Spyness"), k("A Royal Spyness mystery"))
    for a, b in [("Harry Potter", "Harry Potter and the Cursed Child"), ("Wings of Fire", "Wings of Fire Legends"),
                 ("Wings of Fire", "Wings of Fire Graphic Novel"), ("Anne of Green Gables", "Anne of Avonlea")]:
        assert not seriesnorm.similar(k(a), k(b)), (a, b)


# ---- end to end through the response builders ----------------------------------------------

def _stub_ol(monkeypatch, works, auth):
    """works: {ol_key: (title, [ {name, position} ])}"""
    monkeypatch.setattr(ol, "get_author", lambda k: {"name": "Wilkie Collins", "description": "", "image_url": ""})
    monkeypatch.setattr(ol, "get_author_works", lambda k, limit=200: [{"ol_key": w, "title": t} for w, (t, _) in works.items()])
    monkeypatch.setattr(ol, "get_author_work_languages", lambda k, limit=200: {})
    monkeypatch.setattr(ol, "get_work_editions", lambda k: [])
    monkeypatch.setattr(ol, "get_work", lambda k: {
        "title": works[k][0], "description": "", "author_keys": [], "series": works[k][1],
        "cover_url": None, "first_publish_date": None})


def _series_by_title(a):
    return {w["Title"]: [(s["Title"], s["LinkItems"][0]["PositionInSeries"], s["ForeignId"]) for s in w["Series"]] for w in a["Works"]}


def test_enrichment_fills_gaps_and_shares_series_rows(abdb_on, monkeypatch):
    works = {
        "OLAB1W": ("Watsons Case", []),                                         # gap, Audible has it
        "OLAB2W": ("Hawkes Above", []),                                         # gap, differently-named series
        "OLAB3W": ("Christmas at Baker Street", []),                          # gap, Audible doesn't know it
        "OLAB4W": ("Death of Riley", [{"name": "Molly Murphy", "position": "2"}]),  # OL already complete
        "OLAB5W": ("Hawk Help Us", [{"name": "Constable Evan Evans Mystery", "position": None}]),  # position gap
    }
    _stub_ol(monkeypatch, works, "OLABAUTH")
    abdb_on["results"] = {
        "Watsons Case": [_book("Watson's Case", "Wilkie Collins", "Molly Murphy Mysteries", "1", "MM")],
        "Hawkes Above": [_book("Hawkes Above", "Wilkie Collins", "Constable Evans", "1", "CE")],
        "Hawk Help Us": [_book("Hawk Help Us", "Wilkie Collins", "Constable Evans", "3", "CE")],
    }
    aid = db.get_or_create_author("OLABAUTH")
    a = res.build_author_resource(aid, force=True)
    got = _series_by_title(a)
    (mm_name, mm_pos, mm_id), = got["Watsons Case"]
    assert mm_pos == "1"
    # joins the series row Open Library's spelling of the same series uses
    assert got["Death of Riley"][0][2] == mm_id and got["Death of Riley"][0][1] == "2"
    assert mm_name == "Molly Murphy"                                            # bare form wins the display name
    ce = {got["Hawkes Above"][0][2], got["Hawk Help Us"][0][2]}
    assert len(ce) == 1                                                        # "Constable Evans" == "Constable Evan Evans Mystery"
    assert got["Hawk Help Us"][0][1] == "3" and got["Hawkes Above"][0][1] == "1"
    assert got["Christmas at Baker Street"] == []                             # graceful miss, no series invented
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM series").fetchone()[0] >= 2
        names = [r["name"] for r in c.execute("SELECT name FROM series WHERE name LIKE '%Murphy%' OR name LIKE '%Evan%'")]
    assert len(names) == 2, names                                              # no parallel duplicate rows
    # complete Open Library data (Death of Riley) was never looked up
    assert not any("Death of Riley" in q for q in abdb_on["calls"])


def test_audiobookdb_position_beats_openlibrary_and_survives_refresh(abdb_on, monkeypatch):
    works = {"OLAC1W": ("Some Title", [{"name": "Zed Series", "position": None}])}
    _stub_ol(monkeypatch, works, "OLACAUTH")
    abdb_on["results"] = {"Some Title": [_book("Some Title", "Wilkie Collins", "Zed Series", "7")]}
    aid = db.get_or_create_author("OLACAUTH")
    res.build_author_resource(aid, force=True)
    wid = db.works_for_author(aid)[0]["id"]
    assert db.series_for_work(wid)[0]["position"] == "7"
    # a later Open Library refresh reporting a different position must not clobber it
    works["OLAC1W"] = ("Some Title", [{"name": "Zed Series", "position": "3"}])
    res.ensure_work(wid, force=True)
    assert db.series_for_work(wid)[0]["position"] == "7"


def test_open_library_data_is_the_fallback(abdb_on, monkeypatch):
    works = {"OLAD1W": ("Only In OL", [{"name": "Ol Series", "position": None}])}
    _stub_ol(monkeypatch, works, "OLADAUTH")
    aid = db.get_or_create_author("OLADAUTH")
    a = res.build_author_resource(aid, force=True)   # AudiobookDB: nothing found
    assert _series_by_title(a)["Only In OL"][0][0] == "Ol Series"


def test_per_build_lookup_budget_and_progressive_fill(abdb_on, monkeypatch):
    works = {f"OLAE{i}W": (f"Book Number{i}", []) for i in range(6)}
    _stub_ol(monkeypatch, works, "OLAEAUTH")
    monkeypatch.setattr(res, "ABDB_MAX_LOOKUPS_PER_BUILD", 2)
    abdb_on["results"] = {f"Number{i}": [_book(f"Book Number{i}", "Wilkie Collins", "Prog Series", str(i + 1))] for i in range(6)}
    aid = db.get_or_create_author("OLAEAUTH")
    a1 = res.build_author_resource(aid, force=True)
    assert len(abdb_on["calls"]) == 2
    assert sum(1 for v in _series_by_title(a1).values() if v) == 2
    a2 = res.build_author_resource(aid)               # next refresh continues; cached ones are free
    assert len(abdb_on["calls"]) == 4
    assert sum(1 for v in _series_by_title(a2).values() if v) == 4


def test_bulk_path_uses_cache_only(abdb_on, monkeypatch):
    works = {"OLAF1W": ("Cached Book", []), "OLAF2W": ("Uncached Book", [])}
    _stub_ol(monkeypatch, works, "OLAFAUTH")
    aid = db.get_or_create_author("OLAFAUTH")
    res.ensure_author(aid, force=True)
    res.ensure_author_works(aid, force=True)
    ids = {w["title"]: w["id"] for w in db.works_for_author(aid)}
    db.enrichment_put("audiobookdb", abdb.lookup_key("Cached Book", ["Wilkie Collins"]), True,
                      {"series": [{"name": "Cache Series", "position": "5", "external_id": "c"}]})
    out = res._build_work_safe(ids["Cached Book"])
    out2 = res._build_work_safe(ids["Uncached Book"])
    assert [(s["Title"], s["LinkItems"][0]["PositionInSeries"]) for s in out["Series"]] == [("Cache Series", "5")]
    assert out2["Series"] == []
    assert abdb_on["calls"] == []                      # bulk never hits the network


# ---- shared lookup allowance (covers /work/{id} as well as author builds) ---------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_token_bucket_burst_then_refill():
    clock = _Clock()
    b = abdb.TokenBucket(3, 10, clock=clock)
    assert [b.try_take() for _ in range(4)] == [True, True, True, False]
    clock.t += 9.9
    assert not b.try_take()
    clock.t += 0.2                       # one token after 10s
    assert b.try_take() and not b.try_take()
    clock.t += 10_000                    # never above capacity
    assert [b.try_take() for _ in range(4)] == [True, True, True, False]


def _many_works(monkeypatch, n, prefix):
    works = {f"OL{prefix}{i}W": (f"Solo Title {prefix}{i}", []) for i in range(n)}
    _stub_ol(monkeypatch, works, prefix)
    return works


def test_work_id_burst_is_capped_like_an_author_build(abdb_on, monkeypatch):
    """Readarr refreshing many books one /work/{id} at a time must not get an
    uncapped side door: same shared allowance, then cache-only."""
    clock = _Clock()
    monkeypatch.setattr(abdb, "_bucket", abdb.TokenBucket(5, 60, clock=clock))
    _many_works(monkeypatch, 12, "WK")
    aid = db.get_or_create_author("WKAUTH")
    res.ensure_author(aid, force=True)
    res.ensure_author_works(aid, force=True)
    ids = [w["id"] for w in db.works_for_author(aid)]
    assert len(ids) == 12
    for wid in ids:                                    # the /work/{id} path: not thin, no per-build budget
        out = res.build_work_resource(wid, enrich=True)
        assert out is not None                         # capped lookups still return the OL response
    assert len(abdb_on["calls"]) == 5                  # burst only
    clock.t += 120                                     # 2 tokens refill -> a later refresh continues
    for wid in ids:
        res.build_work_resource(wid, enrich=True)
    assert len(abdb_on["calls"]) == 7


def test_work_burst_and_author_build_draw_from_one_allowance(abdb_on, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(abdb, "_bucket", abdb.TokenBucket(6, 3600, clock=clock))
    _many_works(monkeypatch, 10, "SH")
    aid = db.get_or_create_author("SHAUTH")
    monkeypatch.setattr(res, "ABDB_MAX_LOOKUPS_PER_BUILD", 4)
    res.build_author_resource(aid, force=True)         # per-build cap 4
    assert len(abdb_on["calls"]) == 4
    ids = [w["id"] for w in db.works_for_author(aid)]
    for wid in ids:
        res.build_work_resource(wid, enrich=True)      # only 2 tokens left in the shared bucket
    assert len(abdb_on["calls"]) == 6


def test_work_path_still_honours_daily_cap_and_spacing(monkeypatch):
    db.init_db()
    monkeypatch.setattr(abdb, "ENABLED", True)
    monkeypatch.setattr(abdb, "_backoff_until", 0.0)
    monkeypatch.setattr(abdb, "_exhausted_day", None)
    monkeypatch.setattr(abdb, "_bucket", abdb.TokenBucket(1000, 1))
    monkeypatch.setattr(abdb, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(abdb, "DAILY_CAP", 3)
    with db.get_conn() as c:
        c.execute("DELETE FROM enrichment_cache"); c.execute("DELETE FROM source_usage")
    hits = []
    _mock_client(monkeypatch, lambda req: (hits.append(1), httpx.Response(200, json=[]))[1])
    for i in range(8):
        abdb.find_series(f"Distinct Title {i}", ["Some Author"])
    assert len(hits) == 3


def test_denied_by_allowance_is_not_cached_as_a_miss(abdb_on, monkeypatch):
    monkeypatch.setattr(abdb, "_bucket", abdb.TokenBucket(0, 1e9))
    assert abdb.find_series("Some Title", ["Some Author"]) is None
    assert abdb_on["calls"] == []
    assert db.enrichment_get("audiobookdb", abdb.lookup_key("Some Title", ["Some Author"])) is None


# ---- single-flight ------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402


def _slow_search(state, delay=0.25, results=None):
    def fake(query):
        with state["lock"]:
            state["calls"].append(query)
        time.sleep(delay)
        return results if results is not None else []
    return fake


def _run_threads(n, target):
    out = [None] * n
    def run(i):
        out[i] = target(i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not any(t.is_alive() for t in ts)
    return out


def test_concurrent_identical_lookups_make_one_request(abdb_on, monkeypatch):
    state = {"calls": [], "lock": threading.Lock()}
    hit = [_book("Watson's Case", "Wilkie Collins", "Molly Murphy Mysteries", "1")]
    monkeypatch.setattr(abdb, "_post_search", _slow_search(state, results=hit))
    outs = _run_threads(8, lambda i: abdb.find_series(
        "Watsons Case" if i % 2 else "The Watson's Case", [["Wilkie Collins"], ["wilkie  collins"]][i % 2]))
    assert len(state["calls"]) == 1
    assert all(o == outs[0] and o[0]["name"] == "Molly Murphy Mysteries" for o in outs)
    assert abdb._inflight == {}                       # nothing left registered
    assert len(_run_threads(3, lambda i: abdb.find_series("Watsons Case", ["Wilkie Collins"]))) == 3
    assert len(state["calls"]) == 1                   # later callers hit the cache


def test_followers_share_a_leader_failure_without_retrying(abdb_on, monkeypatch):
    calls = []
    def failing(query):
        calls.append(query)
        time.sleep(0.2)
        return None
    monkeypatch.setattr(abdb, "_post_search", failing)
    outs = _run_threads(5, lambda i: abdb.find_series("Flaky Title", ["Some Author"]))
    assert outs == [None] * 5 and len(calls) == 1     # no stampede on failure
    assert abdb._inflight == {}
    assert abdb.find_series("Flaky Title", ["Some Author"]) is None
    assert len(calls) == 2                            # a later request may try again


def test_different_lookups_are_not_serialized(abdb_on, monkeypatch):
    """Two different books must be in flight at the same time. Each call
    waits for the other to arrive; if de-duplication serialized them this
    would deadlock and time out."""
    barrier = threading.Barrier(2, timeout=5)
    calls = []
    def fake(query):
        calls.append(query)
        barrier.wait()
        return []
    monkeypatch.setattr(abdb, "_post_search", fake)
    outs = _run_threads(2, lambda i: abdb.find_series(["Alpha Book", "Beta Book"][i], ["Some Author"]))
    assert outs == [[], []] and sorted(calls) == ["Alpha Book Some Author", "Beta Book Some Author"]


def test_duplicate_wait_is_bounded(abdb_on, monkeypatch):
    monkeypatch.setattr(abdb, "FLIGHT_WAIT_SECONDS", 0.2)
    release = threading.Event()
    def stuck(query):
        release.wait(5)
        return []
    monkeypatch.setattr(abdb, "_post_search", stuck)
    leader = threading.Thread(target=lambda: abdb.find_series("Stuck Title", ["Some Author"]))
    leader.start()
    time.sleep(0.1)
    started = time.time()
    assert abdb.find_series("Stuck Title", ["Some Author"]) is None
    assert time.time() - started < 2
    release.set(); leader.join(5)


def test_overlapping_author_builds_do_not_repeat_lookups(abdb_on, monkeypatch):
    """The real incident: client retries left several identical builds running
    at once, each repeating the same lookups before any had cached them."""
    state = {"calls": [], "lock": threading.Lock()}
    monkeypatch.setattr(abdb, "_post_search", _slow_search(state, delay=0.05))
    _many_works(monkeypatch, 8, "OV")
    aid = db.get_or_create_author("OVAUTH")
    res.ensure_author(aid, force=True)
    res.ensure_author_works(aid, force=True)
    for w in db.works_for_author(aid):                 # OL data already cached, as in production
        res.ensure_work(w["id"])
    _run_threads(4, lambda i: res.build_author_resource(aid))
    assert len(state["calls"]) == 8                    # one per distinct title, not 4 x 8
