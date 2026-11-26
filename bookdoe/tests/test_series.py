"""Series visibility, monitoring, and gap detection."""
from app import db
from app.services import migration, series as series_service, tagging, metadata


def _work_in_series(title, author, series_name, position):
    work_id = migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM series WHERE name=? COLLATE NOCASE", (series_name,)).fetchone()
        sid = row["id"] if row else db.uid()
        if not row:
            conn.execute("INSERT INTO series(id,metadata_id,name,sort_name,created_at,updated_at) VALUES (?,NULL,?,?,?,?)", (sid, series_name, series_name, db.now(), db.now()))
        conn.execute("INSERT OR IGNORE INTO series_memberships(work_id,series_id,position) VALUES (?,?,?)", (work_id, sid, position))
    return work_id, sid


def _assert_series(work_id, series_id, status, raw_value="Test Series #1"):
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO series_assertions(id,work_id,series_id,source,raw_value,normalized_name,
               proposed_position,evidence_type,confidence,status,created_at,updated_at)
               VALUES (?,?,?,'openreads',?,?,NULL,'free_text',0.5,?,?,?)""",
            (db.uid(), work_id, series_id, raw_value, raw_value, status, db.now(), db.now()),
        )


def test_list_series_counts_works_and_owned():
    w1, sid = _work_in_series("Book One", "Author A", "The Chronicles", "1")
    w2, _ = _work_in_series("Book Two", "Author A", "The Chronicles", "2")
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','owned',1,?,?)", (db.uid(), w1, db.now(), db.now()))
        conn.execute("INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','missing',1,?,?)", (db.uid(), w2, db.now(), db.now()))

    items = {s["name"]: s for s in series_service.list_series()}
    assert items["The Chronicles"]["work_count"] == 2
    assert items["The Chronicles"]["owned_count"] == 1


def test_series_detail_orders_by_position_and_reports_gaps():
    _work_in_series("Book One", "Author A", "Numbered", "1")
    _work_in_series("Book Three", "Author A", "Numbered", "3")
    _work_in_series("Book Five", "Author A", "Numbered", "5")
    sid = db.one("SELECT id FROM series WHERE name='Numbered'")["id"]

    detail = series_service.series_detail(sid)
    assert [w["title"] for w in detail["works"]] == ["Book One", "Book Three", "Book Five"]
    assert detail["gaps"] == [2, 4]
    # frontend bookCard() unconditionally does w.authors.map(...) - a missing key
    # here throws mid-render and silently strands the user on the old page.
    assert all(w["authors"] for w in detail["works"])
    assert detail["works"][0]["authors"][0]["name"] == "Author A"


def test_a_single_entry_series_has_no_gaps():
    _work_in_series("Solo", "Author A", "Standalone-ish", "1")
    sid = db.one("SELECT id FROM series WHERE name='Standalone-ish'")["id"]
    assert series_service.series_detail(sid)["gaps"] == []


def test_monitoring_a_series_propagates_to_member_works_and_assets():
    w1, sid = _work_in_series("Book One", "Author A", "The Chronicles", "1")
    w2, _ = _work_in_series("Book Two", "Author A", "The Chronicles", "2")
    for w in (w1, w2):
        with db.tx() as conn:
            conn.execute("UPDATE works SET monitored=0 WHERE id=?", (w,))
            conn.execute("INSERT INTO media_assets(id,work_id,kind,status,monitored,added_at,updated_at) VALUES (?,?,'ebook','missing',0,?,?)", (db.uid(), w, db.now(), db.now()))

    series_service.set_series_monitor(sid, policy="all", ebook=True, audiobook=False)

    for w in (w1, w2):
        assert db.one("SELECT monitored FROM works WHERE id=?", (w,))["monitored"] == 1
        asset = db.one("SELECT monitored FROM media_assets WHERE work_id=? AND kind='ebook'", (w,))
        assert asset["monitored"] == 1


def test_monitor_policy_none_turns_monitoring_off():
    w1, sid = _work_in_series("Book One", "Author A", "The Chronicles", "1")
    series_service.set_series_monitor(sid, policy="all", ebook=True, audiobook=True)
    series_service.set_series_monitor(sid, policy="none", ebook=True, audiobook=True)
    assert db.one("SELECT monitored FROM works WHERE id=?", (w1,))["monitored"] == 0


def test_series_not_found_is_a_clean_error():
    import pytest
    with pytest.raises(ValueError):
        series_service.set_series_monitor("nope", policy="all", ebook=True, audiobook=True)
    assert series_service.series_detail("nope") is None


def test_kids_scope_only_sees_kid_tagged_members():
    w1, sid = _work_in_series("Book One", "Author A", "The Chronicles", "1")
    w2, _ = _work_in_series("Book Two", "Author A", "The Chronicles", "2")
    tagging.add_tag(w1, tagging.kids_tag_id())
    kids_user = {"access_scope": "kids"}

    detail = series_service.series_detail(sid, kids_user)
    assert [w["id"] for w in detail["works"]] == [w1]

    listing = {s["name"]: s for s in series_service.list_series(kids_user)}
    assert listing["The Chronicles"]["work_count"] == 1


def test_a_series_with_no_kid_visible_works_is_absent_for_kids():
    _work_in_series("Adult Book", "Author A", "Grownup Series", "1")
    kids_user = {"access_scope": "kids"}
    assert "Grownup Series" not in {s["name"] for s in series_service.list_series(kids_user)}


def test_discover_more_excludes_known_titles_and_filters_by_series_name(monkeypatch):
    _work_in_series("Book One", "Author A", "The Chronicles", "1")
    sid = db.one("SELECT id FROM series WHERE name='The Chronicles'")["id"]

    def fake_search(query, limit=20):
        return [
            {"title": "Book One", "series": [{"name": "The Chronicles"}]},  # already known
            {"title": "Book Two", "series": [{"name": "The Chronicles"}]},  # new, matching series
            {"title": "Unrelated", "series": [{"name": "Some Other Series"}]},  # wrong series
        ]

    monkeypatch.setattr(metadata, "search", fake_search)
    result = series_service.discover_more(sid)
    assert [c["title"] for c in result["candidates"]] == ["Book Two"]


def test_single_book_series_with_only_uncertain_assertion_is_hidden_by_default():
    w1, sid = _work_in_series("Solo", "Author A", "Maybe Series", "1")
    _assert_series(w1, sid, "uncertain")

    assert "Maybe Series" not in {s["name"] for s in series_service.list_series()}
    assert "Maybe Series" in {s["name"] for s in series_service.list_series(include_uncertain=True)}


def test_single_book_series_with_only_rejected_assertion_is_hidden_by_default():
    w1, sid = _work_in_series("Solo", "Author A", "Junky Series", "1")
    _assert_series(w1, sid, "rejected")

    assert "Junky Series" not in {s["name"] for s in series_service.list_series()}
    assert "Junky Series" in {s["name"] for s in series_service.list_series(include_uncertain=True)}


def test_single_book_series_with_auto_accepted_assertion_always_shows():
    w1, sid = _work_in_series("Solo", "Author A", "Confident Series", "1")
    _assert_series(w1, sid, "auto_accepted")

    assert "Confident Series" in {s["name"] for s in series_service.list_series()}
    assert "Confident Series" in {s["name"] for s in series_service.list_series(include_uncertain=True)}


def test_single_book_series_with_manually_confirmed_assertion_always_shows():
    w1, sid = _work_in_series("Solo", "Author A", "Confirmed Series", "1")
    _assert_series(w1, sid, "manually_confirmed")

    assert "Confirmed Series" in {s["name"] for s in series_service.list_series()}


def test_multi_book_series_shows_even_with_only_uncertain_assertions():
    w1, sid = _work_in_series("Book One", "Author A", "Corroborated Series", "1")
    w2, _ = _work_in_series("Book Two", "Author A", "Corroborated Series", "2")
    _assert_series(w1, sid, "uncertain")
    _assert_series(w2, sid, "uncertain")

    assert "Corroborated Series" in {s["name"] for s in series_service.list_series()}


def test_series_with_no_assertions_at_all_is_not_hidden():
    _work_in_series("Solo", "Author A", "Pre-Evidence Series", "1")

    assert "Pre-Evidence Series" in {s["name"] for s in series_service.list_series()}


def test_discover_more_reports_upstream_failure_without_raising(monkeypatch):
    _work_in_series("Book One", "Author A", "The Chronicles", "1")
    sid = db.one("SELECT id FROM series WHERE name='The Chronicles'")["id"]
    monkeypatch.setattr(metadata, "search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    result = series_service.discover_more(sid)
    assert result["candidates"] == [] and "down" in result["error"]
