"""Library State filter semantics (Complete / Partly owned / Nothing owned), the card chips payload, per-work counts and the
placeholder-asset cleanup and creation guard (the owner's 11 'Missing' books, 2026-10-14)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import metadata, ownership, workmerge

from test_work_merge import _asset, _series, _work

client = TestClient(app)


def _shapes():
    """The production shapes: EPUB owned + a monitored audiobook placeholder (the 11), EPUB+MP3 owned beside a redundant
    empty ebook placeholder (Armageddon, Lights Out), a fully owned book and a wanted-only book."""
    s = {}
    s["epub_wants_audio"] = _work("Epub Wants Audio", "A One")
    _asset(s["epub_wants_audio"], "ebook", "epub", ["/lib/a.epub"])
    _asset(s["epub_wants_audio"], "audiobook", None, status="missing", monitored=1, intent=1)
    s["armageddon"] = _work("Armageddon", "A Two")
    _asset(s["armageddon"], "ebook", "epub", ["/lib/b.epub"])
    _asset(s["armageddon"], "audiobook", "mp3", ["/lib/b.mp3"])
    s["armageddon_placeholder"] = _asset(s["armageddon"], "ebook", None, status="missing", monitored=1, intent=1)
    s["complete"] = _work("Complete Book", "A Three")
    _asset(s["complete"], "ebook", "epub", ["/lib/c.epub"], monitored=1, intent=1)
    s["wanted_only"] = _work("Wanted Only", "A Four")
    _asset(s["wanted_only"], "ebook", None, status="missing", monitored=1, intent=1)
    s["unmonitored_placeholder"] = _work("Quiet Placeholder", "A Five")
    _asset(s["unmonitored_placeholder"], "ebook", "epub", ["/lib/e.epub"])
    _asset(s["unmonitored_placeholder"], "audiobook", None, status="none", monitored=0)
    return s


def _ids(**params):
    r = client.get("/api/v1/library", params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return {w["title"] for w in r.json()["items"]}


def test_the_eleven_works_shape_is_partly_owned_not_missing():
    _shapes()
    assert _ids(status="partial") == {"Epub Wants Audio"}
    assert _ids(status="complete") == {"Armageddon", "Complete Book", "Quiet Placeholder"}
    assert _ids(status="nothing") == {"Wanted Only"}


def test_a_redundant_ebook_placeholder_beside_the_owned_ebook_does_not_make_a_book_partly_owned():
    s = _shapes()
    # Armageddon owns EPUB + MP3; its leftover empty ebook placeholder is monitored but the ebook kind IS owned.
    card = next(w for w in client.get("/api/v1/library", params={"status": "complete"}).json()["items"] if w["title"] == "Armageddon")
    assert card["state"] == "complete" and card["missing_formats"] == [] and card["wanted"] == []
    assert s["armageddon_placeholder"]


def test_an_unmonitored_work_is_never_partly_owned_or_wanted():
    s = _shapes()
    with db.tx() as conn:            # the 11-shape and the wanted-only shape, but the WORK is unmonitored
        conn.execute("UPDATE works SET monitored=0 WHERE id IN (?,?)", (s["epub_wants_audio"], s["wanted_only"]))
    assert _ids(status="partial") == set()
    assert "Epub Wants Audio" in _ids(status="complete") and "Wanted Only" not in _ids(status="nothing")
    counts = ownership.state_counts("1=1", ())
    assert counts["partial"] == 0 and counts["nothing"] == 0
    card = next(w for w in client.get("/api/v1/library", params={"status": "complete"}).json()["items"] if w["title"] == "Epub Wants Audio")
    assert card["state"] == "complete" and card["missing_formats"] == [] and card["wanted"] == []
    detail = client.get(f"/api/v1/works/{s['epub_wants_audio']}").json()
    assert detail.get("state", "complete") == "complete"


def test_legacy_query_values_keep_working():
    _shapes()
    # status=owned was "owns something" (the default); status=missing used to match any monitored-not-owned asset.
    assert {"Epub Wants Audio", "Armageddon", "Complete Book", "Quiet Placeholder"} == _ids(status="owned")
    assert _ids(status="missing") == {"Epub Wants Audio"}            # = Partly owned now
    # kind + raw asset status still answers "is THAT medium missing" (bookmarked #library?kind=audiobook&status=missing)
    assert _ids(kind="audiobook", status="missing") == {"Epub Wants Audio"}
    assert "Wanted Only" not in _ids(status="owned")


def test_card_payload_lists_the_missing_formats_for_chips():
    _shapes()
    items = {w["title"]: w for w in client.get("/api/v1/library", params={"limit": 500}).json()["items"]}
    w = items["Epub Wants Audio"]
    assert w["state"] == "partial" and w["missing_formats"] == ["audiobook"] and w["owned_kinds"] == ["ebook"]
    assert w["wanted"][0]["kind"] == "audiobook" and w["wanted"][0]["asset_id"]
    assert items["Quiet Placeholder"]["state"] == "complete" and items["Quiet Placeholder"]["missing_formats"] == []
    detail = client.get(f"/api/v1/library/{w['id']}").json()
    assert detail["state"] == "partial" and detail["missing_formats"] == ["audiobook"]


def test_state_counts_match_the_filtered_lists():
    _shapes()
    c = client.get("/api/v1/library-state-counts").json()
    assert c == {"owned": 4, "complete": 3, "partial": 1, "nothing": 1}
    for state, n in (("complete", 3), ("partial", 1), ("nothing", 1)):
        assert client.get("/api/v1/library", params={"status": state}).json()["total"] == n
    # the counts honour the other filters
    assert client.get("/api/v1/library-state-counts", params={"q": "Armageddon"}).json()["complete"] == 1


def test_series_and_authors_count_owned_works_not_assets():
    a = _work("Book A", "Series Author")
    b = _work("Book B", "Series Author")
    for w in (a, b):
        _series(w, "Counting Saga", "1" if w == a else "2")
    _asset(a, "ebook", "epub", ["/lib/a.epub"])
    _asset(a, "audiobook", "m4b", ["/lib/a.m4b"])        # two owned assets, one owned book
    _asset(b, "ebook", None, status="missing", monitored=1, intent=1)
    series = next(s for s in client.get("/api/v1/series", params={"include_uncertain": True}).json()["items"] if s["name"] == "Counting Saga")
    assert (series["work_count"], series["owned_count"]) == (2, 1)
    detail = client.get(f"/api/v1/series/{series['id']}").json()
    assert detail["owned_count"] == 1 and {w["title"]: w["state"] for w in detail["works"]} == {"Book A": "complete", "Book B": "nothing"}
    author = next(x for x in client.get("/api/v1/authors").json()["items"] if x["name"] == "Series Author")
    assert (author["work_count"], author["owned_works"], author["owned_audio"], author["owned_ebooks"]) == (2, 1, 1, 1)


# ---- placeholder cleanup ---------------------------------------------------------------------------------------

def _count(table="media_assets", where="1=1", params=()):
    return db.one(f"SELECT COUNT(*) n FROM {table} WHERE {where}", params)["n"]  # nosec B608


def test_cleanup_dry_run_then_apply_is_idempotent_audited_and_keeps_the_wish():
    s = _shapes()
    before = _count()
    dry = ownership.cleanup_placeholders(apply=False)
    assert dry["dry_run"] and dry["found"] == 1 and dry["items"][0]["asset_id"] == s["armageddon_placeholder"] and _count() == before
    done = ownership.cleanup_placeholders(apply=True, user={"id": "admin"})
    assert done["removed"] == 1 and _count() == before - 1
    assert not db.one("SELECT 1 x FROM media_assets WHERE id=?", (s["armageddon_placeholder"],))
    # the wish moved onto the owned ebook asset
    owned = db.one("SELECT monitored,has_acquisition_intent FROM media_assets WHERE work_id=? AND kind='ebook' AND status='owned'", (s["armageddon"],))
    assert owned["monitored"] == 1 and owned["has_acquisition_intent"] == 1
    ev = db.one("SELECT data_json FROM history WHERE event_type=? AND work_id=?", (ownership.EVENT_CLEANUP, s["armageddon"]))
    assert ev and s["armageddon_placeholder"] in ev["data_json"]
    again = ownership.cleanup_placeholders(apply=True)
    assert again["found"] == 0 and again["removed"] == 0
    # the audiobook placeholder of the 11 is a real wish for a missing kind: never touched
    assert db.one("SELECT 1 x FROM media_assets WHERE work_id=? AND kind='audiobook' AND status='missing'", (s["epub_wants_audio"],))


@pytest.mark.parametrize("reference", ["file", "download", "progress", "bookmark", "conversion", "derived"])
def test_cleanup_never_deletes_a_placeholder_that_has_data(reference):
    w = _work("Has Data", "Someone")
    _asset(w, "ebook", "epub", ["/lib/x.epub"])
    ph = _asset(w, "ebook", None, status="missing", monitored=1, intent=1)
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,is_admin,created_at,updated_at) VALUES ('u1','u1',0,1,1)")
        if reference == "file":
            conn.execute("INSERT INTO media_files(id,asset_id,path,discovered_at) VALUES ('f1',?,'/lib/ph.epub',1)", (ph,))
        elif reference == "download":
            conn.execute("INSERT INTO downloads(id,work_id,asset_id,state,added_at,updated_at) VALUES ('d1',?,?,'grabbed',?,?)", (w, ph, t, t))
        elif reference == "progress":
            conn.execute("INSERT INTO media_progress(user_id,work_id,asset_id,kind,percent,updated_at) VALUES ('u1',?,?,'ebook',0.5,?)", (w, ph, t))
        elif reference == "bookmark":
            conn.execute("INSERT INTO bookmarks(id,user_id,work_id,asset_id,created_at) VALUES ('b1','u1',?,?,?)", (w, ph, t))
        elif reference == "conversion":
            conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,created_at) VALUES ('c1','ebook',?,?,'epub',?)", (w, ph, t))
        else:
            conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,derived_from_asset_id,added_at,updated_at) VALUES ('der',?,'ebook','azw3','owned',?,?,?)", (w, ph, t, t))
    assert ownership.cleanup_placeholders(apply=True)["removed"] == 0
    assert db.one("SELECT 1 x FROM media_assets WHERE id=?", (ph,))


def test_cleanup_endpoint_is_admin_csrf_and_dry_by_default():
    s = _shapes()
    r = client.post("/api/v1/admin/placeholder-assets/cleanup", json={})
    assert r.status_code == 200 and r.json()["dry_run"] and r.json()["found"] == 1
    assert db.one("SELECT 1 x FROM media_assets WHERE id=?", (s["armageddon_placeholder"],))
    assert client.post("/api/v1/admin/placeholder-assets/cleanup", json={"apply": True}).json()["removed"] == 1
    assert client.post("/api/v1/admin/placeholder-assets/cleanup", json={"bogus": 1}).status_code == 422


# ---- creation guard ----------------------------------------------------------------------------------------------

def test_ensure_assets_never_adds_a_second_slot_for_an_owned_kind():
    w = _work("Guarded", "Someone")
    _asset(w, "ebook", "epub", ["/lib/g.epub"])
    metadata._ensure_assets(w)
    metadata._ensure_assets(w)
    assert _count(where="work_id=? AND kind='ebook'", params=(w,)) == 1
    assert _count(where="work_id=? AND kind='audiobook'", params=(w,)) == 1      # the missing kind still gets its empty slot
    with db.tx() as conn:
        assert ownership.may_create_placeholder(conn, w, "ebook") is False


def test_a_merge_folds_a_placeholder_into_the_owned_asset_of_its_kind_and_undo_restores_it():
    canonical = _work("Fold Me", "Someone")
    source = _work("Fold Me", "Someone")
    _asset(canonical, "ebook", "epub", ["/lib/fold.epub"])
    ph = _asset(source, "ebook", None, status="missing", monitored=1, intent=1)
    res = workmerge.execute(canonical, [source], {}, None)
    kinds = db.all("SELECT kind,format,status,monitored FROM media_assets WHERE work_id=? ORDER BY kind,format", (canonical,))
    assert len(kinds) == 1 and kinds[0]["status"] == "owned" and kinds[0]["monitored"] == 1      # monitoring OR-ed in, no second slot
    assert not db.one("SELECT 1 x FROM media_assets WHERE id=?", (ph,))
    workmerge.reverse(res["merge_id"])
    assert db.one("SELECT status FROM media_assets WHERE id=?", (ph,))["status"] == "missing"
    assert _count(where="work_id=?", params=(source,)) == 1


def test_the_placeholder_dry_run_never_takes_the_write_lock(monkeypatch):
    s = _shapes()
    monkeypatch.setattr(db, "tx", lambda: pytest.fail("a dry run must not open a write transaction"))
    dry = ownership.cleanup_placeholders(apply=False)
    assert dry["dry_run"] and dry["found"] == 1 and dry["items"][0]["asset_id"] == s["armageddon_placeholder"]
    monkeypatch.undo()
    assert ownership.cleanup_placeholders(apply=True)["removed"] == 1               # apply is still the transactional path


def test_the_placeholder_dry_run_does_not_wait_for_a_writer():
    import threading
    _shapes()
    holding, release = threading.Event(), threading.Event()

    def writer():
        with db.tx() as conn:                       # BEGIN IMMEDIATE + the global lock, held until released
            conn.execute("UPDATE works SET updated_at=updated_at")
            holding.set()
            release.wait(30)
    t = threading.Thread(target=writer)
    t.start()
    assert holding.wait(10)
    try:
        out = {}
        reader = threading.Thread(target=lambda: out.update(ownership.cleanup_placeholders(apply=False)))
        reader.start()
        reader.join(10)
        assert not reader.is_alive() and out["found"] == 1                           # answered while the write lock was held
    finally:
        release.set()
        t.join(30)


def test_a_merge_does_not_fold_or_repoint_a_placeholder_something_still_references():
    canonical = _work("Grab Me", "Someone")
    source = _work("Grab Me", "Someone")
    _asset(canonical, "ebook", "epub", ["/lib/grab.epub"])
    ph = _asset(source, "ebook", None, status="missing", monitored=1, intent=1)
    free = _asset(source, "audiobook", None, status="missing", monitored=1, intent=1)
    _asset(canonical, "audiobook", "mp3", ["/lib/grab.mp3"])
    t = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO downloads(id,work_id,asset_id,state,added_at,updated_at) VALUES ('dl1',?,?,'grabbed',?,?)", (source, ph, t, t))
    plan = workmerge.preview(canonical, [source])
    folded = {c["source_asset_id"] for c in plan["conflicts"] if c["type"] == "asset_collision" and c.get("placeholder")}
    assert free in folded and ph not in folded                                      # the unreferenced one folds, the grabbed one is left alone
    assert any(n.get("action") == "kept" and n["asset_id"] == ph and "referenced" in n["reason"] for n in plan["plan"]["derived_assets"])
    workmerge.execute(canonical, [source], {}, None)
    assert db.one("SELECT asset_id FROM downloads WHERE id='dl1'")["asset_id"] == ph      # the download still points at its own asset
    assert db.one("SELECT 1 x FROM media_assets WHERE id=?", (ph,))
    assert not db.one("SELECT 1 x FROM media_assets WHERE id=?", (free,))
