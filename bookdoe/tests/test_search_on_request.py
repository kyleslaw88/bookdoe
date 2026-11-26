"""Search on request: approving a request searches once for just that book.

Only the indexer layer (releases.search_releases) and the download client are faked. The
trigger, the shared search_and_grab, the cooldown state, the daily cap, history and the
request payload are the real ones. The background pool is replaced by an inline runner so
the tests are deterministic.
"""
import pytest

from app import db
from app.integrations import downloaders
from app.services import family, migration, releases, request_search, scheduler, wanted_search

import helpers
from test_download_cancel import _client_row
from test_family_controls import families, local_mode  # noqa: F401  (fixtures)

H = 3600.0


# ---------- the matching check ----------
HP = ("Healing Politics", ["Abdul El-Sayed"])
M4A = ("Medicare for All", ["Abdul El-Sayed, Micah Johnson"])


def _rows(*rows):
    return [(w, r, e) for w, items in rows for r, e in items]


MISTBORN = ("Mistborn: The Final Empire", ["Brandon Sanderson"])


@pytest.mark.parametrize("work,release,expected", _rows(
    (HP, [
        ("Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed [ENG / EPUB]", True),
        ("Healing Politics by Abdul El-Sayed EPUB", True),
        ("Healing the Heart of Democracy: Courage to Create a Politics Worthy of the Human Spirit by Parker J Palmer [ENG / M4B]", False),
        ("The Politics of Trauma: Somatics, Healing, and Social Justice by Staci K Haines", False),
        ("Healing Politics [ENG / M4B]", False),                                  # right title, nobody named
        ("Healing Politics by Someone Else", False),                              # same title, other author
        ("healing politics - abdul elsayed (2020) mp3", True),                    # case, joined surname
        ("Healing Politics by Sayed", False),                                     # "sayed" alone is not the surname El-Sayed
    ]),
    (M4A, [
        ("Medicare for All: A Citizen's Guide by Abdul El-Sayed, Micah Johnson [ENG / M4B]", True),
        ("Medicare for All - Micah Johnson & Abdul El-Sayed.epub", True),
        ("Medicare for All by Micah Johnson", True),                              # one of several authors is enough
        ("Medicare for All: A Citizen's Guide by Somebody Else", False),
    ]),
    (("Dune", ["Frank Herbert"]), [
        ("Frank Herbert - Dune", True),
        ("Dune by Frank Herbert [ENG / M4B]", True),
        ("Frank Herbert - Dune Messiah", False),
        ("Frank Herbert - Children of Dune", False),
        ("Frank Herbert - Dune Chronicles Box Set 1-6", False),
        ("Dune Chronicles Box Set 1-6", False),
        ("Brian Herbert, Kevin J Anderson - Dune House Atreides", False),
        ("Dune: The Graphic Novel Frank Herbert", False),
    ]),
    (("It", ["Stephen King"]), [("Stephen King - It", True), ("Stephen King - If It Bleeds", False)]),
    (("Emma", ["Jane Austen"]), [("Jane Austen - Emma [EPUB]", True), ("Pride and Prejudice, Emma, Persuasion Collection", False),
                                 ("Jane Austen - Pride and Prejudice, Emma, Persuasion Collection", False)]),
    (("The Shining", ["Stephen King"]), [("Stephen King - The Shining", True), ("Doctor Sleep (sequel to The Shining) - Stephen King", False)]),
    (("Quiet", ["Susan Cain"]), [("Susan Cain - Quiet", True), ("Quiet Power: The Secret Strengths of Introverted Kids by Susan Cain", False)]),
    (("Educated", ["Tara Westover"]), [("Tara Westover - Educated", True), ("Educated: A Memoir (Summary and Analysis by QuickRead) Tara Westover", False),
                                       ("Educated Workbook Tara Westover", False), ("Educated Study Guide - Tara Westover", False)]),
    (("Educated: A Memoir", ["Tara Westover"]), [("Tara Westover - Educated [EPUB]", True), ("Tara Westover - Educated: A Memoir", True),
                                                 ("Educated: A Memoir (Summary and Analysis by QuickRead)", False)]),
    (("Sapiens: A Brief History of Humankind", ["Yuval Noah Harari"]), [
        ("Yuval Noah Harari - Sapiens (2015) EPUB", True), ("Sapiens: A Brief History of Humankind by Harari, Yuval Noah [ENG / M4B]", True),
        ("Yuval Noah Harari - Sapiens Summary", False)]),
    (("Ender's Game", ["Orson Scott Card"]), [("Orson.Scott.Card-Enders.Game.EPUB", True), ("Orson Scott Card - Ender's Game", True),
                                              ("Orson Scott Card - Ender's Shadow", False)]),
    (("The Snowman", ["Jo Nesb\u00f8"]), [("Jo Nesbo - The Snowman", True), ("Jo Nesbo - The Snowman Workbook", False)]),
    (("Smilla's Sense of Snow", ["Peter H\u00f8eg"]), [("Peter Hoeg - Smilla's Sense of Snow", True)]),
    (("The Name of the Wind", ["Patrick Rothfuss"]), [("Name of the Wind by Patrick Rothfuss [ENG / M4B]", True)]),   # leading article
    (("A Tale of Two Cities", ["Charles Dickens"]), [("Tale of Two Cities - Charles Dickens (unabridged)", True)]),
    (("Quiet: The Power of Introverts in a World That Can't Stop Talking", ["Susan Cain"]), [
        ("Quiet: The Power of Introverts in a World That Can't Stop Talking by Susan Cain [ENG / M4B]", True),
        ("Quiet by Susan Cain [ENG / M4B]", True), ("Quiet Power: The Secret Strengths of Introverted Kids by Susan Cain", False)]),
    (("Caf\u00e9 Society", ["Jos\u00e9 \u00c1lvarez"]), [("Cafe Society by Jose Alvarez EPUB", True)]),              # diacritics
    (("Foundation", ["Isaac Asimov"]), [("Foundation 01 - Foundation by Asimov, Isaac [ENG / M4B]", True), ("Foundation and Empire by Isaac Asimov", False)]),
    (("Anne of Green Gables (Anne of Green Gables, #1)", ["L. M. Montgomery"]), [("Anne of Green Gables by L.M. Montgomery [ENG / EPUB]", True)]),
    (("Gideon the Ninth", ["Tamsyn Muir"]), [("Harrow the Ninth by Tamsyn Muir [ENG / M4B]", False)]),
    (("Tom Sawyer", []), [("Tom Sawyer EPUB", True), ("Tom Sawyer Abroad", False)]),                    # no author on file: nothing to check
    (("Good Omens", ["Terry Pratchett", "Neil Gaiman"]), [("Good Omens by Neil Gaiman, Terry Pratchett [ENG / M4B]", True)]),
    (("The Hobbit", ["J.R.R. Tolkien Jr."]), [("The Hobbit by J.R.R. Tolkien [ENG / M4B]", True), ("The Hobbit by JRR Tolkien", True)]),
    (("1984", ["George Orwell"]), [("George Orwell - 1984 (1949) [EPUB]", True)]),                       # a year that is the title survives
))
def test_release_matches_work(work, release, expected):
    assert wanted_search.release_matches_work(work[0], work[1], release) is expected, release


def test_a_release_naming_no_author_is_not_grabbed():
    assert wanted_search.release_matches_work("Dune", ["Frank Herbert"], "Dune [EPUB]") is False


def test_series_tags_and_a_series_named_main_title():
    series = [{"name": "Mistborn", "position": 1}]
    m = lambda rel, ser=None: wanted_search.release_matches_work(MISTBORN[0], MISTBORN[1], rel, ser)  # noqa: E731
    assert m("Brandon Sanderson - The Final Empire (Mistborn 1)", series) is True
    assert m("Brandon Sanderson - The Final Empire (Mistborn 2)", series) is False      # other book number
    assert m("Brandon Sanderson - The Final Empire (Mistborn 1)") is False              # series unknown: a near miss for a human
    assert m("Brandon Sanderson - Mistborn: The Final Empire", series) is True
    assert wanted_search.release_matches_work("Dune: Messiah", ["Frank Herbert"], "Dune by Frank Herbert", ["Dune"]) is False
    assert wanted_search.release_matches_work("Dune: Messiah", ["Frank Herbert"], "Dune Messiah by Frank Herbert", ["Dune"]) is True


def _m(work, authors, release, series=None, kind=None):
    return wanted_search.release_matches_work(work, authors, release, series, kind)


@pytest.mark.parametrize("work,authors,series,release", [
    ("Outlander", ["Diana Gabaldon"], None, "Diana Gabaldon - Outlander: Dragonfly in Amber"),
    ("Outlander", ["Diana Gabaldon"], [{"name": "Outlander", "position": 1}], "Outlander: The Fiery Cross - Diana Gabaldon"),
    ("The Hunger Games", ["Suzanne Collins"], [{"name": "The Hunger Games", "position": 1}], "Suzanne Collins - The Hunger Games: Sunrise on the Reaping"),
    ("The Hunger Games", ["Suzanne Collins"], [{"name": "The Hunger Games", "position": 1}], "Suzanne Collins - The Hunger Games: The Ballad of Songbirds and Snakes"),
    ("Mistborn: The Final Empire", ["Brandon Sanderson"], [{"name": "Mistborn", "position": 1}], "Brandon Sanderson - Mistborn: The Well of Ascension"),
    ("Percy Jackson and the Olympians: The Lightning Thief", ["Rick Riordan"], None, "Rick Riordan - Percy Jackson and the Olympians: The Sea of Monsters"),
    ("Becoming", ["Michelle Obama"], None, "Michelle Obama - Becoming: Adapted for Young Readers"),
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe: Edicion en espanol"),
    ("Twilight", ["Stephenie Meyer"], None, "Stephenie Meyer - Twilight: Life and Death"),
    ("It", ["Stephen King"], None, "Stephen King - It: The Losers' Club"),
    ("The Hobbit", ["J.R.R. Tolkien"], None, "J.R.R. Tolkien - The Hobbit: BBC Radio Dramatisation"),
    ("Wool", ["Hugh Howey"], None, "Hugh Howey - Wool: The Silo Series Book Two"),
    # no subtitle on file, 2+ word main title, full author, but a not-the-book word in the subtitle
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: Adapted for Young Readers by Abdul El-Sayed"),
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: Edicion en espanol by Abdul El-Sayed"),
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: The Silo Series Book Two by Abdul El-Sayed"),
    # the free subtitle needs the full author, not a surname alone
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by El-Sayed"),
    # the work has a subtitle of its own: another subtitle is another book
    ("Healing Politics: Part One", ["Abdul El-Sayed"], None, "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed"),
    # the work is in a series named like its main title
    ("Healing Politics", ["Abdul El-Sayed"], [{"name": "Healing Politics", "position": 1}], "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed"),
    ("Healing Politics", ["Abdul El-Sayed"], [{"name": "The Healing Politics Saga", "position": 1}], "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed"),
    # another author with the same surname
    ("Dune", ["Frank Herbert"], None, "Dune: The Butlerian Jihad - Brian Herbert"),
    ("Dune", ["Frank Herbert"], None, "Dune: House Atreides - Brian Herbert, Kevin J. Anderson"),
    ("Dune", ["Frank Herbert"], None, "Dune by Herbert, Brian"),
    ("Dune", ["Frank Herbert"], None, "Dune by Herbert and Anderson"),
    # a work stored with a subtitle, no series: the bare one-word main title is not trusted
    ("Dune: Messiah", ["Frank Herbert"], None, "Frank Herbert - Dune"),
    ("Mistborn: The Final Empire", ["Brandon Sanderson"], None, "Brandon Sanderson - Mistborn"),
    ("Mistborn: The Final Empire", ["Brandon Sanderson"], [{"name": "Mistborn", "position": 1}], "Brandon Sanderson - Mistborn 2 - The Final Empire"),
    ("Hunger Games: Catching Fire", ["Suzanne Collins"], [{"name": "The Hunger Games", "position": 2}], "Suzanne Collins - Hunger Games"),
    # a narrator credit is only noise for audio, and a stray name is not a narrator when it is part of the title
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe [Perdita Weeks]"),
    ("Dune", ["Frank Herbert"], None, "Frank Herbert - Dune [M4B] - Dune Messiah"),
])
def test_wrong_books_are_not_matched(work, authors, series, release):
    assert _m(work, authors, release, series) is False, release


@pytest.mark.parametrize("work,authors,series,release,kind", [
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed [ENG / EPUB]", None),
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics by Abdul El-Sayed EPUB", None),
    ("Medicare for All", ["Abdul El-Sayed, Micah Johnson"], None, "Medicare for All: A Citizen's Guide by Abdul El-Sayed, Micah Johnson [ENG / M4B]", None),
    ("Healing Politics", ["Abdul El-Sayed"], None, "Healing Politics: A Doctor's Note on Treating the Insecurity Epidemic by Abdul El-Sayed [ENG / M4B]", "audiobook"),
    # a lone Hunger Games subtitle with no series on file: the documented residual risk
    ("The Hunger Games", ["Suzanne Collins"], None, "Suzanne Collins - The Hunger Games: Sunrise on the Reaping", None),
    # surname alone is fine for an exact title when nobody else is named
    ("Dune", ["Frank Herbert"], None, "Dune by Herbert [EPUB]", None),
    ("Dune", ["Frank Herbert"], None, "Herbert, Frank - Dune", None),
    ("Dune: Messiah", ["Frank Herbert"], None, "Frank Herbert - Dune Messiah", None),
    # narrator credits on audiobooks
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe - read by Perdita Weeks", None),
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe [Perdita Weeks]", "audiobook"),
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe [Perdita Weeks] [M4B]", None),
    ("The Martian", ["Andy Weir"], None, "Andy Weir - The Martian [R.C. Bray]", "audiobook"),
    ("The Martian", ["Andy Weir"], None, "Andy Weir - The Martian [R.C. Bray] MP3", None),
    ("Harry Potter and the Philosopher's Stone", ["J.K. Rowling"], None, "J.K. Rowling - Harry Potter and the Philosopher's Stone - Stephen Fry [Unabridged]", None),
    ("Dune", ["Frank Herbert"], None, "Frank Herbert - Dune (Dune 1) - read by Scott Brick", None),
    ("Dune", ["Frank Herbert"], None, "Frank Herbert - Dune - narrated by Scott Brick [M4B]", None),
    # publisher and edition noise
    ("Frankenstein", ["Mary Shelley"], None, "Mary Shelley - Frankenstein (Penguin Classics)", None),
    ("Hamlet", ["William Shakespeare"], None, "William Shakespeare - Hamlet (Folger Shakespeare Library)", None),
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe Deluxe Edition", None),
    ("Circe", ["Madeline Miller"], None, "Madeline Miller - Circe 10th Anniversary Edition EPUB", None),
    # series number prefixes with the series on file
    ("Mistborn: The Final Empire", ["Brandon Sanderson"], [{"name": "Mistborn", "position": 1}], "Brandon Sanderson - Mistborn 1 - The Final Empire", None),
    ("The Way of Kings", ["Brandon Sanderson"], [{"name": "The Stormlight Archive", "position": 1}], "Brandon Sanderson - Stormlight Archive 1 - The Way of Kings", None),
    ("The Way of Kings", ["Brandon Sanderson"], [{"name": "The Stormlight Archive", "position": 1}], "Brandon Sanderson - The Stormlight Archive 1 - The Way of Kings", None),
    # surname particles
    ("Rebecca", ["Daphne du Maurier"], None, "Du Maurier - Rebecca", None),
    ("Rebecca", ["Daphne du Maurier"], None, "Daphne du Maurier - Rebecca", None),
])
def test_right_books_are_matched(work, authors, series, release, kind):
    assert _m(work, authors, release, series, kind) is True, release


def test_other_edition_words_are_never_noise():
    assert _m("Circe", ["Madeline Miller"], "Madeline Miller - Circe Spanish Edition") is False
    assert _m("Circe", ["Madeline Miller"], "Madeline Miller - Circe (Translated Edition)") is False


def test_a_series_number_prefix_needs_the_series_and_number_to_agree():
    ser = [{"name": "Mistborn", "position": 1}]
    assert _m(MISTBORN[0], MISTBORN[1], "Brandon Sanderson - Mistborn 1 - The Final Empire") is False   # series not on file
    assert _m(MISTBORN[0], MISTBORN[1], "Brandon Sanderson - Mistborn 2 - The Final Empire", ser) is False


def _set_work(world, title, author, wid=None):
    wid = wid or world.fam["hidden"]
    with db.tx() as conn:
        conn.execute("UPDATE works SET title=? WHERE id=?", (title, wid))
        conn.execute("UPDATE contributors SET name=? WHERE id IN (SELECT contributor_id FROM work_contributors WHERE work_id=?)", (author, wid))


def test_search_grabs_none_when_free_subtitles_disagree(world):
    """Two results that both look like 'Healing Politics: <subtitle>' with different subtitles: ambiguous."""
    world.finds["audiobook"] = ["Healing Politics: A Doctor's Note on the Insecurity Epidemic by Abdul El-Sayed [ENG / M4B]",
                                "Healing Politics: The Insecurity Epidemic Revisited by Abdul El-Sayed [ENG / M4B]"]
    _set_work(world, "Healing Politics", "Abdul El-Sayed")
    body = _request(world, "pat", ["audiobook"])
    assert world.adds == []
    e = _events(body["id"])[0]
    assert e["data"]["outcome"] == "no_match" and len(e["data"]["near_misses"]) == 2


def test_search_grabs_a_lone_free_subtitle_and_prefers_an_exact_result(world):
    world.finds["audiobook"] = ["Healing Politics: A Doctor's Note on the Insecurity Epidemic by Abdul El-Sayed [ENG / M4B]"]
    _set_work(world, "Healing Politics", "Abdul El-Sayed")
    _request(world, "pat", ["audiobook"])
    assert len(world.adds) == 1


def test_search_prefers_the_exact_result_over_free_subtitle_ones(world):
    world.finds["audiobook"] = ["Healing Politics: One Subtitle by Abdul El-Sayed [ENG / M4B]",
                                "Healing Politics: Another Subtitle by Abdul El-Sayed [ENG / M4B]",
                                "Healing Politics by Abdul El-Sayed [ENG / M4B]"]
    _set_work(world, "Healing Politics", "Abdul El-Sayed")
    _request(world, "pat", ["audiobook"])
    assert len(world.adds) == 1
    assert db.one("SELECT raw_title FROM releases WHERE id=?", (world.adds[0],))["raw_title"] == "Healing Politics by Abdul El-Sayed [ENG / M4B]"


def test_surname_candidates_are_not_split_into_loose_pieces():
    forms = wanted_search._author_forms(["Abdul El-Sayed"])
    assert ("sayed",) not in forms and ("el", "sayed") in forms and ("elsayed",) in forms


# ---------- the trigger ----------
@pytest.fixture
def world(families, monkeypatch):  # noqa: F811
    """Fake indexers: `finds` maps asset kind to the release titles a search returns."""
    _client_row("sabnzbd")
    w = type("W", (), {})()
    w.finds = {}   # kind -> titles to return; a kind not listed returns the wanted book itself
    w.calls, w.adds, w.boom, w.errors = [], [], False, False
    w.inline = True

    def search(work_id, kind, automatic=False):
        w.calls.append((work_id, kind, automatic))
        if w.boom:
            raise RuntimeError("indexer exploded")
        sid = db.uid()
        with db.tx() as conn:
            conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'[]',?,?,'complete')", (sid, work_id, kind, int(automatic), db.now()))
        if w.errors:
            return {"items": [], "errors": [{"error": "timeout"}]}
        items = []
        wk = db.one("SELECT w.title,c.name au FROM works w LEFT JOIN work_contributors wc ON wc.work_id=w.id LEFT JOIN contributors c ON c.id=wc.contributor_id WHERE w.id=?", (work_id,))
        own = [f"{wk['title']} by {wk['au']} [ENG / {'M4B' if kind == 'audiobook' else 'EPUB'}]"]
        for title in w.finds.get(kind, own):
            rid = db.uid()
            with db.tx() as conn:
                conn.execute("INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_guid,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,?,?,0,?,?)",
                             (rid, sid, work_id, kind, rid, "usenet", title, "http://idx.test/x", 95, db.now()))
            items.append({"id": rid, "rejected": 0, "score": 95, "title": title})
        return {"items": items, "errors": []}

    monkeypatch.setattr(releases, "search_releases", search)
    monkeypatch.setattr(downloaders, "add", lambda c, rel: w.adds.append(rel["id"]) or {"ok": True, "id": f"job-{len(w.adds)}"})
    monkeypatch.setattr(request_search, "_enqueue", lambda fn, *a: fn(*a))
    w.fam = dict(families, fresh=migration._find_or_create_stub({"title": "Fresh Book", "author": "F", "identifiers": {}}))
    return w


def _events(req_id=None):
    rows = db.all("SELECT * FROM history WHERE event_type='request.search' ORDER BY created_at, id")
    out = [dict(r, data=db.json_load(r["data_json"], {})) for r in rows]
    return [e for e in out if req_id is None or e["data"].get("request_id") == req_id]


def _request(world, who, kinds, work="hidden"):
    c, h = world.fam["s"][who]
    r = c.post("/api/player/v1/requests", json={"work_id": world.fam[work], "kinds": kinds}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_adult_auto_approve_searches_once_per_kind_and_grabs(world):
    body = _request(world, "pat", ["audiobook", "ebook"], work="fresh")
    assert sorted(k for _, k, _ in world.calls) == ["audiobook", "ebook"] and all(a for _, _, a in world.calls)
    assert len(world.adds) == 2
    ev = _events(body["id"])
    assert sorted(e["data"]["kind"] for e in ev) == ["audiobook", "ebook"] and {e["data"]["outcome"] for e in ev} == {"grabbed"}
    assert "Fresh Book" in ev[0]["message"] and ev[0]["work_id"] == world.fam["fresh"]
    pat, _ = world.fam["s"]["pat"]
    item = pat.get("/api/player/v1/requests").json()["items"][0]
    assert item["search"]["state"] == "grabbed" and item["search"]["at"]
    assert item["friendly_status"] == "Being Prepared"


def test_the_search_runs_while_the_wanted_search_job_is_disabled(world):
    job = db.one("SELECT enabled FROM jobs WHERE kind='wanted-search'")
    assert job is not None and not job["enabled"], "the library-wide job is off by default"
    _request(world, "pat", ["audiobook"])
    assert len(world.calls) == 1


def test_kid_pending_request_does_not_search_and_approval_does(world):
    body = _request(world, "owen", ["audiobook"])
    assert world.calls == [] and _events() == []
    assert world.fam["s"]["owen"][0].get("/api/player/v1/requests").json()["items"][0]["search"] is None
    jane, jh = world.fam["s"]["jane"]
    assert jane.post(f"/api/player/v1/approvals/{body['id']}/approve", json={}, headers=jh).status_code == 200
    assert [k for _, k, _ in world.calls] == ["audiobook"] and len(world.adds) == 1
    assert _events(body["id"])[0]["data"]["outcome"] == "grabbed"
    # The approver's view and the Management view carry the same field.
    assert jane.get("/api/player/v1/approvals?status=approved").json()["items"][0]["search"]["state"] == "grabbed"
    admin, _ = world.fam["s"]["ada"]
    assert admin.get("/api/v1/requests").json()["items"][0]["search"]["state"] == "grabbed"


def test_kid_denial_never_searches(world):
    body = _request(world, "evan", ["audiobook"])
    jane, jh = world.fam["s"]["jane"]
    jane.post(f"/api/player/v1/approvals/{body['id']}/deny", json={}, headers=jh)
    assert world.calls == [] and _events() == []


def test_admin_approval_in_management_searches(world):
    body = _request(world, "mia", ["audiobook"])
    admin, ah = world.fam["s"]["ada"]
    assert admin.post(f"/api/v1/requests/{body['id']}/approve", headers=ah).status_code == 200
    assert [k for _, k, _ in world.calls] == ["audiobook"]


def test_setting_off_disables_it_and_the_admin_toggle_works(world):
    admin, ah = world.fam["s"]["ada"]
    assert admin.get("/api/v1/settings/requests").json()["search_on_approval"] is True
    r = admin.put("/api/v1/settings/requests", json={"adult_policy": "auto", "search_on_approval": False}, headers=ah)
    assert r.json()["search_on_approval"] is False and family.search_on_approval() is False
    _request(world, "pat", ["audiobook"])
    assert world.calls == [] and _events() == []
    admin.put("/api/v1/settings/requests", json={"adult_policy": "auto", "search_on_approval": True}, headers=ah)
    assert family.search_on_approval() is True


def test_a_failing_search_does_not_fail_the_request(world):
    world.boom = True
    body = _request(world, "pat", ["audiobook"])
    assert body["status"] == "approved"
    ev = _events(body["id"])
    assert [e["data"]["outcome"] for e in ev] == ["error"] and "exploded" in ev[0]["data"]["errors"][0]
    assert world.fam["s"]["pat"][0].get("/api/player/v1/requests").json()["items"][0]["search"]["state"] == "error"


def test_trigger_itself_never_raises(world, monkeypatch):
    def boom(fn, *a):
        raise RuntimeError("pool is gone")
    monkeypatch.setattr(request_search, "_enqueue", boom)
    body = _request(world, "pat", ["audiobook"])
    assert body["status"] == "approved"
    assert request_search._inflight_requests == set()


def test_a_miss_sets_a_cooldown_and_a_fresh_approval_clears_it(world):
    world.finds["audiobook"] = []
    first = _request(world, "pat", ["audiobook"])
    asset = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind='audiobook'", (world.fam["hidden"],))
    st = db.one("SELECT * FROM wanted_search_state WHERE asset_id=?", (asset["id"],))
    assert st["last_outcome"] == "no_result" and st["next_eligible_at"] > db.now()
    assert _events(first["id"])[0]["data"]["outcome"] == "no_result"
    # Approving another request for the same book is a new decision: the old miss does not block it.
    n = len(world.calls)
    second = _request(world, "sam", ["audiobook"])
    assert len(world.calls) == n + 1
    assert _events(second["id"])[0]["data"]["outcome"] == "no_result"


def test_a_cooldown_set_after_the_approval_still_blocks_the_search(world):
    asset = helpers.want(world.fam["hidden2"], "audiobook")
    wanted_search._record(asset, "no_result", 12)
    req = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO requests(id,user_id,work_id,title,kinds_json,status,created_at,updated_at) SELECT ?,id,?,'Second Hidden','[\"audiobook\"]','approved',?,? FROM users LIMIT 1",
                     (req, world.fam["hidden2"], db.now(), db.now()))
    out = request_search.run(req)
    assert world.calls == [] and out[0]["outcome"] == "skipped_cooldown"
    assert _events(req)[0]["data"]["outcome"] == "skipped_cooldown"


def test_approval_resets_the_cooldown_of_its_asset(world):
    asset = helpers.want(world.fam["hidden2"], "audiobook")
    wanted_search._record(asset, "no_match", 12)
    body = _request(world, "pat", ["audiobook"], work="hidden2")
    assert len(world.calls) == 1 and _events(body["id"])[0]["data"]["outcome"] == "grabbed"


def test_a_request_for_an_asset_being_searched_is_busy_not_cooldown(world):
    asset = helpers.want(world.fam["hidden2"], "audiobook")
    request_search._inflight_assets.add(asset)
    try:
        body = _request(world, "pat", ["audiobook"], work="hidden2")
    finally:
        request_search._inflight_assets.discard(asset)
    assert world.calls == [] and _events(body["id"])[0]["data"]["outcome"] == "busy"
    assert db.one("SELECT 1 x FROM wanted_search_state WHERE asset_id=?", (asset,)) is None


def test_daily_cap_is_respected(world):
    with db.tx() as conn:
        for _ in range(wanted_search.get_settings()["wanted_search_daily_cap"]):
            conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'[]',1,?,'complete')", (db.uid(), world.fam["shelf"], "ebook", db.now()))
    body = _request(world, "pat", ["audiobook"])
    assert world.calls == [] and _events(body["id"])[0]["data"]["outcome"] == "skipped_cap"
    assert body["status"] == "approved"


def test_a_wrong_book_is_not_grabbed_and_the_near_misses_are_recorded(world):
    world.finds["audiobook"] = ["Healing the Heart of Democracy: Courage by Parker J Palmer [ENG / M4B]"]
    body = _request(world, "pat", ["audiobook"])
    assert world.adds == []
    e = _events(body["id"])[0]
    assert e["data"]["outcome"] == "no_match" and "Parker J Palmer" in e["data"]["near_misses"][0]["title"]
    asset = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind='audiobook'", (world.fam["hidden"],))
    st = db.one("SELECT * FROM wanted_search_state WHERE asset_id=?", (asset["id"],))
    assert st["last_outcome"] == "no_match" and st["next_eligible_at"] > db.now(), "recorded, so the cooldown applies"
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset["id"],))["status"] == "missing"
    assert world.fam["s"]["pat"][0].get("/api/player/v1/requests").json()["items"][0]["search"]["state"] == "no_match"


def test_a_later_matching_release_is_grabbed_past_a_wrong_top_one(world):
    world.finds["audiobook"] = ["Healing the Heart of Democracy by Parker J Palmer [M4B]", "Hidden Book by B [ENG / M4B]"]
    _request(world, "pat", ["audiobook"])
    assert len(world.adds) == 1
    assert db.one("SELECT raw_title FROM releases WHERE id=?", (world.adds[0],))["raw_title"].startswith("Hidden Book")


def test_owned_or_downloading_assets_are_not_searched(world):
    _request(world, "pat", ["ebook"])      # Hidden Book already has an owned ebook
    assert world.calls == [] and _events() == []


def test_search_state_is_searching_while_it_runs(world, monkeypatch):
    seen = []
    monkeypatch.setattr(request_search, "_enqueue", lambda fn, *a: None)
    body = _request(world, "pat", ["audiobook"])    # queued, not run yet
    request_search._inflight_requests.add(body["id"])
    try:
        item = world.fam["s"]["pat"][0].get(f"/api/player/v1/requests/{body['id']}").json()
        seen.append(item["search"]["state"])
    finally:
        request_search._inflight_requests.discard(body["id"])
    assert seen == ["searching"]


# ---------- the job and the trigger share one function ----------
def test_the_job_uses_the_shared_function(world, monkeypatch):
    asset = helpers.want(world.fam["hidden2"], "audiobook")
    called = []
    real = wanted_search.search_and_grab
    monkeypatch.setattr(wanted_search, "search_and_grab", lambda a, ms=None, d=None: called.append(a["id"]) or real(a, ms, d))
    world.finds["audiobook"] = ["Second Hidden by C [ENG / M4B]"]
    res = scheduler._wanted_search()
    assert called == [asset] and res["grabbed"] == 1


def test_the_job_reports_no_match_with_near_misses(world):
    helpers.want(world.fam["hidden2"], "audiobook")
    world.finds["audiobook"] = ["Totally Different Book by Nobody [ENG / M4B]"]
    res = scheduler._wanted_search()
    assert res["grabbed"] == 0 and res["no_match"] == 1 and world.adds == []
    assert res["near_misses"][0]["releases"][0]["title"].startswith("Totally Different")


def test_request_trigger_uses_the_shared_function(world, monkeypatch):
    called = []
    real = wanted_search.search_and_grab
    monkeypatch.setattr(wanted_search, "search_and_grab", lambda a, ms=None, d=None: called.append(a["kind"]) or real(a, ms, d))
    _request(world, "pat", ["audiobook"])
    assert called == ["audiobook"]


# ---------- review fixes: duplicates, throttle, pacing, shutdown ----------
def _asset(world, work="hidden2", kind="audiobook"):
    return db.one("SELECT * FROM media_assets WHERE id=?", (helpers.want(world.fam[work], kind),))


def test_an_active_download_means_skipped_active_and_no_cooldown(world):
    asset = _asset(world)
    with db.tx() as conn:
        conn.execute("INSERT INTO downloads(id,work_id,asset_id,client_id,state,protocol,source_title,progress,added_at,updated_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
                     (db.uid(), asset["work_id"], asset["id"], db.one("SELECT id FROM download_clients LIMIT 1")["id"], "downloading", "usenet", "x", db.now(), db.now()))
    out, _ = wanted_search.search_and_grab(asset)
    assert out == "skipped_active" and world.calls == [] and world.adds == []
    assert db.one("SELECT 1 x FROM wanted_search_state WHERE asset_id=?", (asset["id"],)) is None


def test_an_owned_asset_is_not_grabbed(world):
    asset = _asset(world)
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET status='owned' WHERE id=?", (asset["id"],))
    assert wanted_search.search_and_grab(asset)[0] == "skipped_active" and world.calls == []


def test_the_job_and_a_request_search_racing_on_one_asset_grab_once(world, monkeypatch):
    import threading
    asset = _asset(world)
    gate, entered = threading.Event(), threading.Event()
    real = releases.search_releases

    def slow(work_id, kind, automatic=False):
        entered.set()
        gate.wait(5)
        return real(work_id, kind, automatic=automatic)

    monkeypatch.setattr(releases, "search_releases", slow)
    results = []
    t1 = threading.Thread(target=lambda: results.append(wanted_search.search_and_grab(dict(asset))[0]))
    t2 = threading.Thread(target=lambda: results.append(wanted_search.search_and_grab(dict(asset))[0]))
    t1.start()
    assert entered.wait(5)
    t2.start()
    gate.set()
    t1.join(10)
    t2.join(10)
    assert sorted(results) == ["grabbed", "skipped_active"] and len(world.adds) == 1
    assert db.one("SELECT COUNT(*) n FROM downloads WHERE asset_id=?", (asset["id"],))["n"] == 1


def test_the_daily_cap_cannot_be_overshot_by_two_workers(world, monkeypatch):
    import threading
    a1, a2 = _asset(world), _asset(world, "hidden", "audiobook")
    with db.tx() as conn:
        for _ in range(wanted_search.get_settings()["wanted_search_daily_cap"] - 1):
            conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'[]',1,?,'complete')", (db.uid(), world.fam["shelf"], "ebook", db.now()))
    gate, entered = threading.Event(), threading.Event()
    real = releases.search_releases
    monkeypatch.setattr(releases, "search_releases", lambda *a, **k: (entered.set(), gate.wait(5), real(*a, **k))[2])
    out = []
    t1 = threading.Thread(target=lambda: out.append(wanted_search.search_and_grab(dict(a1))[0]))
    t1.start()
    assert entered.wait(5)
    out.append(wanted_search.search_and_grab(dict(a2))[0])   # the last slot is taken while t1 is still searching
    gate.set()
    t1.join(10)
    assert sorted(out) == ["grabbed", "skipped_cap"]


def test_the_asset_is_rechecked_right_before_the_grab(world, monkeypatch):
    asset = _asset(world)
    real = releases.search_releases

    def search_then_unmonitor(work_id, kind, automatic=False):
        out = real(work_id, kind, automatic=automatic)
        with db.tx() as conn:
            conn.execute("UPDATE media_assets SET monitored=0 WHERE id=?", (asset["id"],))
        return out

    monkeypatch.setattr(releases, "search_releases", search_then_unmonitor)
    world.finds["audiobook"] = ["Second Hidden by C [ENG / M4B]"]
    assert wanted_search.search_and_grab(asset)[0] == "skipped_active" and world.adds == []


def test_the_job_does_not_count_or_cool_down_a_skipped_active_asset(world, monkeypatch):
    asset = _asset(world)
    monkeypatch.setattr(wanted_search, "search_and_grab", lambda a, ms=None, d=None: ("skipped_active", []))
    res = scheduler._wanted_search()
    assert res["searched"] == 0 and res["skipped_active"] == 1
    assert db.one("SELECT 1 x FROM wanted_search_state WHERE asset_id=?", (asset["id"],)) is None


def test_a_throttled_search_pauses_new_request_searches(world, monkeypatch):
    monkeypatch.setattr(wanted_search, "_throttle_until", 0.0)
    real = releases.search_releases
    monkeypatch.setattr(releases, "search_releases", lambda *a, **k: (real(*a, **k) and {"items": [], "errors": [{"error": "HTTP 429 Too Many Requests"}]}))
    first = _request(world, "pat", ["audiobook"])
    assert _events(first["id"])[0]["data"]["outcome"] == "throttled" and 590 < wanted_search.throttle_remaining() <= 600
    asset = db.one("SELECT id FROM media_assets WHERE work_id=? AND kind='audiobook'", (world.fam["hidden"],))
    assert db.one("SELECT 1 x FROM wanted_search_state WHERE asset_id=?", (asset["id"],)) is None, "a throttle says nothing about the book"
    n = len(world.calls)
    second = _request(world, "sam", ["audiobook"])
    assert len(world.calls) == n and _events(second["id"])[0]["data"]["outcome"] == "throttled"


def test_request_searches_are_paced_by_the_job_delay(world, monkeypatch):
    slept = []
    clock = [1000.0]
    monkeypatch.setattr(wanted_search, "monotonic", lambda: clock[0])
    monkeypatch.setattr(wanted_search, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(request_search, "_last_search_at", None)
    db.set_setting("acquisition.wanted_search_delay_seconds", 5.0)
    _request(world, "pat", ["audiobook", "ebook"], work="fresh")
    assert len(world.calls) == 2 and slept == [5.0]


def test_changing_the_score_floor_clears_no_match_and_no_result_rows(world):
    a1, a2, a3 = _asset(world)["id"], _asset(world, "hidden", "audiobook")["id"], _asset(world, "fresh", "ebook")["id"]
    wanted_search._record(a1, "no_match", 12)
    wanted_search._record(a2, "no_result", 12)
    wanted_search._record(a3, "error", 12)
    wanted_search.save_settings({"auto_grab_min_score": 70})
    left = {r["asset_id"] for r in db.all("SELECT asset_id FROM wanted_search_state")}
    assert left == {a3}


def test_every_outcome_has_ui_text_in_both_surfaces():
    import pathlib
    static = pathlib.Path(wanted_search.__file__).parents[1] / "static"
    for js, name in (("player.js", "SEARCH_TEXT"), ("app.js", "REQUEST_SEARCH_TEXT")):
        line = next(l for l in (static / js).read_text().splitlines() if f"const {name}" in l)
        for state in ("searching",) + request_search.STATES:
            assert f"{state}:" in line, (js, state)


def test_lifespan_shuts_the_request_pool_without_waiting(monkeypatch):
    import asyncio
    from app import main
    calls = []
    monkeypatch.setattr(main.request_search, "shutdown_pool", lambda wait=True: calls.append(wait))

    async def go():
        async with main.lifespan(main.app):
            pass

    asyncio.run(go())
    assert calls == [False]


def test_pool_shutdown_cancels_queued_searches():
    import inspect
    assert "cancel_futures=True" in inspect.getsource(request_search.shutdown_pool)


def test_a_client_that_refuses_leaves_no_download_row_and_the_row_exists_before_the_add(world, monkeypatch):
    from app.services import downloads
    asset = _asset(world)
    rid = db.uid()
    with db.tx() as conn:
        sid = db.uid()
        conn.execute("INSERT INTO release_searches(id,work_id,asset_kind,query_json,automatic,started_at,status) VALUES (?,?,?,'[]',0,?,'complete')", (sid, asset["work_id"], "audiobook", db.now()))
        conn.execute("INSERT INTO releases(id,search_id,work_id,asset_kind,indexer_guid,protocol,raw_title,download_url,rejected,score,created_at) VALUES (?,?,?,?,?,?,?,?,0,90,?)",
                     (rid, sid, asset["work_id"], "audiobook", rid, "usenet", "X", "http://idx.test/x", db.now()))
    seen = []

    def add(c, rel):
        seen.append(db.one("SELECT state,client_download_id FROM downloads WHERE release_id=?", (rid,)))
        return {"ok": False}

    monkeypatch.setattr(downloaders, "add", add)
    with pytest.raises(ValueError):
        downloads.grab_release(rid)
    assert seen[0]["state"] == "grabbed" and seen[0]["client_download_id"] is None
    assert db.one("SELECT COUNT(*) n FROM downloads WHERE release_id=?", (rid,))["n"] == 0
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset["id"],))["status"] == "missing"
