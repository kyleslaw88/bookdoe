"""Metadata that only the user's own files have.

"Dead World" (Convergence #6, 2026, Audible-exclusive) showed a correct cover and
nothing else: no description, no series, no year. OpenLibrary carries no descriptions
for that author at all, so no amount of searching would have produced any. The file
on disk, meanwhile, knew it was Convergence book 6, published 2026, narrated by
R.C. Bray. bookdoe was throwing that away.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app import db
from app.services import localmeta, migration


def _work(title="Dead World", author="Craig Alanson") -> str:
    return migration._find_or_create_stub({"title": title, "author": author, "identifiers": {}})


def _attach(work_id: str, path: Path, kind: str) -> str:
    asset_id, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,?,?,'owned',1,?,?)",
            (asset_id, work_id, kind, path.suffix.lstrip("."), t, t),
        )
        conn.execute(
            """INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at)
               VALUES (?,?,?,?, '{}',1,0,0,?)""",
            (db.uid(), asset_id, str(path), path.stat().st_size, t),
        )
    return asset_id


def _epub(path: Path, *, series="Convergence", index="6.0", description=None, date="2026-05-19T00:00:00+00:00") -> Path:
    extra = f'<dc:description>{description}</dc:description>' if description else ''
    opf = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Dead World</dc:title><dc:creator>Craig Alanson</dc:creator>
        <dc:language>en</dc:language><dc:date>{date}</dc:date>{extra}
        <meta name="calibre:series" content="{series}"/>
        <meta name="calibre:series_index" content="{index}"/>
      </metadata><manifest/></package>"""
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
    return path


def test_series_year_and_language_come_from_the_epub(tmp_path):
    work_id = _work()
    _attach(work_id, _epub(tmp_path / "book.epub"), "ebook")

    result = localmeta.harvest(work_id)

    assert result["applied"]["series"] == "Convergence #6"
    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    assert work["first_publish_date"] == "2026"
    assert work["primary_language"] == "en"
    row = db.one("""SELECT s.name, sm.position FROM series_memberships sm JOIN series s ON s.id=sm.series_id WHERE sm.work_id=?""", (work_id,))
    assert (row["name"], row["position"]) == ("Convergence", "6")


def test_description_is_taken_when_the_file_has_one(tmp_path):
    work_id = _work()
    _attach(work_id, _epub(tmp_path / "book.epub", description="A ship falls silent."), "ebook")
    localmeta.harvest(work_id)
    assert db.one("SELECT description FROM works WHERE id=?", (work_id,))["description"] == "A ship falls silent."


def test_provider_metadata_is_never_overwritten(tmp_path):
    """Harvesting fills gaps. A resolved work must come out untouched."""
    work_id = _work()
    with db.tx() as conn:
        conn.execute(
            "UPDATE works SET description=?,first_publish_date=?,primary_language=?,metadata_status='resolved' WHERE id=?",
            ("Publisher blurb", "1999", "fre", work_id),
        )
    _attach(work_id, _epub(tmp_path / "book.epub", description="File blurb"), "ebook")

    localmeta.harvest(work_id)

    work = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    assert work["description"] == "Publisher blurb"
    assert work["first_publish_date"] == "1999"
    assert work["primary_language"] == "fre"


def test_narrator_comes_from_the_audiobook_tags(monkeypatch, tmp_path):
    work_id = _work()
    path = tmp_path / "book.m4b"; path.write_bytes(b"audio")
    asset_id = _attach(work_id, path, "audiobook")

    monkeypatch.setattr(localmeta, "read_audio", lambda p: {"narrators": ["R.C. Bray"], "date": "2026"})
    localmeta.harvest(work_id)

    assert db.json_load(db.one("SELECT narrator_json FROM media_assets WHERE id=?", (asset_id,))["narrator_json"], []) == ["R.C. Bray"]
    assert db.one("SELECT first_publish_date FROM works WHERE id=?", (work_id,))["first_publish_date"] == "2026"


@pytest.mark.parametrize("title,expected", [
    ("Dead World: Convergence, Book 6", ("Dead World", "Convergence", "6")),
    ("Exit Strategy: The Murderbot Diaries, Book 4", ("Exit Strategy", "The Murderbot Diaries", "4")),
    ("Carl's Doomsday Scenario - Dungeon Crawler Carl, Book 2", ("Carl's Doomsday Scenario", "Dungeon Crawler Carl", "2")),
])
def test_series_is_parsed_from_an_audiobook_title(title, expected):
    m = localmeta._SERIES_IN_TITLE.match(title)
    assert m, title
    assert (m.group("title"), m.group("series").strip(), m.group("position")) == expected


@pytest.mark.parametrize("title", [
    "Dune",
    "I Give You My Body: How I Write Sex Scenes",     # a subtitle is not a series
    "Leviathan Wakes: The Expanse",                    # no position, so not a series entry
    "Book 6",
])
def test_a_subtitle_is_not_mistaken_for_a_series(title):
    """A loose pattern would turn every subtitled book into a fake series entry."""
    assert localmeta._SERIES_IN_TITLE.match(title) is None


def test_harvest_is_idempotent(tmp_path):
    work_id = _work()
    _attach(work_id, _epub(tmp_path / "book.epub"), "ebook")
    localmeta.harvest(work_id)
    second = localmeta.harvest(work_id)
    assert second["applied"] == {}
    assert db.one("SELECT COUNT(*) n FROM series_memberships WHERE work_id=?", (work_id,))["n"] == 1


def test_a_work_with_no_files_is_not_an_error():
    assert localmeta.harvest(_work())["applied"] == {}


def test_backfill_only_considers_works_with_files(tmp_path):
    with_file = _work("Dead World")
    _attach(with_file, _epub(tmp_path / "a.epub"), "ebook")
    _work("No Files At All", "Someone Else")

    result = localmeta.harvest_missing()

    assert result["filled"] == 1
    # A work with nothing on disk cannot be helped and must not be counted as tried.
    assert result["considered"] == 1


def test_backfill_advances_instead_of_rereading_the_same_files(tmp_path):
    """The first cut selected "works still missing something", so books whose files
    simply have no description were re-probed on every pass and the backfill never
    reached the rest of the library - it plateaued at 5 new works per 200 examined."""
    for i in range(3):
        wid = _work(f"Book {i}", "Craig Alanson")
        # No series/description in this file, so the work stays "incomplete" forever.
        _attach(wid, _epub(tmp_path / f"{i}.epub", series="", index="", date=""), "ebook")

    first = localmeta.harvest_missing()
    assert first["considered"] == 3

    second = localmeta.harvest_missing()
    assert second["considered"] == 0, "already-read works must not be probed again"


def test_a_new_file_is_read_on_import_even_after_a_backfill(tmp_path):
    work_id = _work()
    _attach(work_id, _epub(tmp_path / "a.epub", series="", index="", date=""), "ebook")
    localmeta.harvest_missing()
    assert localmeta.harvest_missing()["considered"] == 0

    # A work owns one asset per kind; extra files hang off that same asset.
    asset_id = db.one("SELECT id FROM media_assets WHERE work_id=?", (work_id,))["id"]
    richer = _epub(tmp_path / "b.epub")
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at)
               VALUES (?,?,?,?, '{}',1,0,0,?)""",
            (db.uid(), asset_id, str(richer), richer.stat().st_size, db.now()),
        )
    # The import path calls harvest() directly rather than waiting for the backfill.
    assert localmeta.harvest(work_id)["applied"]["series"] == "Convergence #6"
