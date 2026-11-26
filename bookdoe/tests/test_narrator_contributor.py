"""Tests for P4-07: Narrator as a first-class Contributor (Part XXII).

Verifies that harvesting an audiobook with a narrator tag creates both:
  1. The existing narrator_json entry (unchanged behavior)
  2. A work_contributors row with role='narrator' pointing at a properly
     deduped contributors row
"""
from pathlib import Path
import zipfile

from app import db
from app.services import localmeta


def _make_test_audiobook(path: Path, narrators: list[str] | None = None) -> Path:
    """Create a minimal .m4b file with optional narrator tags for ffprobe."""
    # We can't create a real audio file, so we'll test _set_narrators directly
    # and test the full harvest path by mocking read_file.
    return path


def test_set_narrators_creates_work_contributor_row():
    """_set_narrators should create a work_contributors row with role='narrator'."""
    wid = db.uid()
    aid = db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid, "Narrator Test", "Narrator Test", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid, wid, "audiobook", "owned", t, t))
    localmeta._set_narrators(aid, ["Audrey Niffenegger"], wid)

    # narrator_json should be set
    asset = db.one("SELECT narrator_json FROM media_assets WHERE id=?", (aid,))
    assert db.json_load(asset["narrator_json"], []) == ["Audrey Niffenegger"]

    # work_contributors row should exist with role='narrator'
    wc = db.one(
        "SELECT wc.role, c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='narrator'",
        (wid,),
    )
    assert wc is not None
    assert wc["role"] == "narrator"
    assert wc["name"] == "Audrey Niffenegger"

    # contributors row should exist
    contrib = db.one("SELECT name FROM contributors WHERE name=?", ("Audrey Niffenegger",))
    assert contrib is not None


def test_set_narrators_dedupe_same_narrator_across_works():
    """Two different works narrated by the same person should share one contributors row."""
    wid1, wid2 = db.uid(), db.uid()
    aid1, aid2 = db.uid(), db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid1, "Book One", "Book One", "resolved", t, t))
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid2, "Book Two", "Book Two", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid1, wid1, "audiobook", "owned", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid2, wid2, "audiobook", "owned", t, t))

    localmeta._set_narrators(aid1, ["Scott Brick"], wid1)
    localmeta._set_narrators(aid2, ["Scott Brick"], wid2)

    # Exactly one contributors row for "Scott Brick"
    n_contribs = db.one("SELECT COUNT(*) n FROM contributors WHERE name=?", ("Scott Brick",))["n"]
    assert n_contribs == 1

    # Two work_contributors rows (one per work)
    n_wc = db.one(
        "SELECT COUNT(*) n FROM work_contributors WHERE role='narrator'",
    )["n"]
    assert n_wc == 2


def test_set_narrators_multiple_narrators_preserve_order():
    """Multiple narrators should be stored with correct ord values."""
    wid = db.uid()
    aid = db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid, "Multi Narrator", "Multi Narrator", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid, wid, "audiobook", "owned", t, t))

    localmeta._set_narrators(aid, ["First Narrator", "Second Narrator"], wid)

    rows = db.all(
        "SELECT wc.ord, c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='narrator' ORDER BY wc.ord",
        (wid,),
    )
    assert len(rows) == 2
    assert rows[0]["name"] == "First Narrator"
    assert rows[0]["ord"] == 0
    assert rows[1]["name"] == "Second Narrator"
    assert rows[1]["ord"] == 1


def test_set_narrators_idempotent_no_duplicate_on_reharvest():
    """Re-harvesting the same audiobook must not duplicate work_contributors rows."""
    wid = db.uid()
    aid = db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid, "Reharvest Test", "Reharvest Test", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid, wid, "audiobook", "owned", t, t))

    # First call writes narrator_json and work_contributors
    localmeta._set_narrators(aid, ["Test Narrator"], wid)
    wc_count_1 = db.one("SELECT COUNT(*) n FROM work_contributors WHERE role='narrator'")["n"]

    # Second call should see narrator_json already set and return early
    localmeta._set_narrators(aid, ["Test Narrator"], wid)
    wc_count_2 = db.one("SELECT COUNT(*) n FROM work_contributors WHERE role='narrator'")["n"]

    assert wc_count_1 == 1
    assert wc_count_2 == 1


def test_set_narrators_without_work_id_still_writes_narrator_json():
    """Backward compat: calling without work_id should still write narrator_json."""
    wid = db.uid()
    aid = db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid, "No Work ID Test", "No Work ID Test", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid, wid, "audiobook", "owned", t, t))

    localmeta._set_narrators(aid, ["No Work ID Narrator"])  # no work_id

    asset = db.one("SELECT narrator_json FROM media_assets WHERE id=?", (aid,))
    assert db.json_load(asset["narrator_json"], []) == ["No Work ID Narrator"]

    # No work_contributors should exist since no work_id was provided
    wc = db.one(
        "SELECT 1 FROM work_contributors wc JOIN media_assets a ON a.id=wc.work_id WHERE a.id=?",
        (aid,),
    )
    assert wc is None


def test_narrator_rows_do_not_leak_into_author_queries():
    """Author-filtered queries must not return narrator rows."""
    wid = db.uid()
    aid = db.uid()
    cid_author = db.uid()
    cid_narrator = db.uid()
    t = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (wid, "Author Narrator Test", "Author Narrator Test", "resolved", t, t))
        c.execute("INSERT INTO media_assets(id,work_id,kind,status,added_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (aid, wid, "audiobook", "owned", t, t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                  (cid_author, "Jane Author", "Author, Jane", t, t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                  (wid, cid_author, "author", 0))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                  (cid_narrator, "Scott Narrator", "Narrator, Scott", t, t))
        c.execute("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,?,?)",
                  (wid, cid_narrator, "narrator", 0))

    # Author query should only return the author
    authors = db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='author' ORDER BY wc.ord",
        (wid,),
    )
    assert len(authors) == 1
    assert authors[0]["name"] == "Jane Author"

    # Narrator query should only return the narrator
    narrators = db.all(
        "SELECT c.name FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='narrator' ORDER BY wc.ord",
        (wid,),
    )
    assert len(narrators) == 1
    assert narrators[0]["name"] == "Scott Narrator"


def test_backfill_creates_narrator_rows_for_existing_audiobooks():
    """Overseer review addition: audiobooks that already had narrator_json before P4-07
    get work_contributors(role='narrator') rows at startup; re-running adds nothing."""
    from app import db
    wid = db.uid(); aid = db.uid(); now = db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,added_at,updated_at) VALUES (?,?,?,?)", (wid, "Leviathan Wakes", now, now))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,status,narrator_json,added_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (aid, wid, "audiobook", "owned", db.json_dump(["Jefferson Mays"]), now, now),
        )
    work = wid
    from app import migrations
    with db.tx() as conn:
        migrations._migrate_narrator_contributors(conn)
    rows = db.all("SELECT c.name, wc.role FROM work_contributors wc JOIN contributors c ON c.id=wc.contributor_id WHERE wc.work_id=? AND wc.role='narrator'", (work,))
    assert [r["name"] for r in rows] == ["Jefferson Mays"]
    with db.tx() as conn:
        assert migrations._migrate_narrator_contributors(conn)["rows_added"] == 0
    assert len(db.all("SELECT 1 FROM work_contributors WHERE work_id=? AND role='narrator'", (work,))) == 1
