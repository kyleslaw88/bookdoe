"""P5-03: ASIN, abridged, publisher and release date from audiobook tags (Part XXVIII).

ffprobe's output is faked at the subprocess boundary, so read_audio and harvest both
run for real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import db
from app.services import localmeta, migration


def _probe(monkeypatch, tags: dict) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"format": {"tags": tags}}), stderr="")

    monkeypatch.setattr(localmeta.subprocess, "run", fake_run)


def _book(tmp_path: Path, title="Dead World", *, publisher=None, release_date=None, abridged=None, with_edition=True):
    work_id = migration._find_or_create_stub({"title": title, "author": "Craig Alanson", "identifiers": {}})
    asset_id, edition_id, t = db.uid(), (db.uid() if with_edition else None), db.now()
    path = tmp_path / f"{asset_id}.m4b"
    path.write_bytes(b"audio")
    with db.tx() as conn:
        if edition_id:
            conn.execute(
                "INSERT INTO editions(id,work_id,publisher,release_date,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (edition_id, work_id, publisher, release_date, t, t),
            )
        conn.execute(
            "INSERT INTO media_assets(id,work_id,edition_id,kind,format,status,monitored,abridged,added_at,updated_at) VALUES (?,?,?,'audiobook','m4b','owned',1,?,?,?)",
            (asset_id, work_id, edition_id, abridged, t, t),
        )
        conn.execute(
            "INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?, '{}',1,0,0,?)",
            (db.uid(), asset_id, str(path), 5, t),
        )
    return work_id, asset_id, edition_id


def _asins(entity_id):
    return db.all("SELECT * FROM identifiers WHERE namespace='asin' AND entity_id=?", (entity_id,))


FULL = {"asin": "B0CXYZ1234", "publisher": "Podium Audio", "date": "2026-05-19", "unabridged": "1"}


def test_empty_fields_gain_exactly_the_tag_values(monkeypatch, tmp_path):
    work_id, asset_id, edition_id = _book(tmp_path)
    _probe(monkeypatch, FULL)

    result = localmeta.harvest(work_id)

    ed = db.one("SELECT publisher,release_date FROM editions WHERE id=?", (edition_id,))
    assert (ed["publisher"], ed["release_date"]) == ("Podium Audio", "2026-05-19")
    assert db.one("SELECT abridged FROM media_assets WHERE id=?", (asset_id,))["abridged"] == 0
    (ident,) = _asins(edition_id)
    assert (ident["entity_type"], ident["value"], ident["source"], ident["verified"], ident["confidence"]) == (
        "edition", "B0CXYZ1234", "file-tags", 0, 0.7)
    assert {"asin", "publisher", "release_date", "abridged"} <= set(result["applied"])
    stored = db.json_load(db.one("SELECT applied_json FROM local_meta WHERE work_id=?", (work_id,))["applied_json"], {})
    assert stored["asin"] == "B0CXYZ1234" and stored["publisher"] == "Podium Audio"
    obs = db.one("SELECT source,is_current FROM metadata_observations WHERE entity_id=? AND field_name='publisher'", (edition_id,))
    assert (obs["source"], obs["is_current"]) == ("file-tags", 1)


def test_asin_attaches_to_the_work_without_an_edition(monkeypatch, tmp_path):
    work_id, _, _ = _book(tmp_path, with_edition=False)
    _probe(monkeypatch, {"asin": "B0CXYZ1234"})
    localmeta.harvest(work_id)
    (ident,) = _asins(work_id)
    assert ident["entity_type"] == "work"


def test_populated_fields_are_never_overwritten(monkeypatch, tmp_path):
    work_id, asset_id, edition_id = _book(tmp_path, publisher="Original Pub", release_date="2020-01-01", abridged=1)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('edition',?,'asin','B0AAAAAAAA','audible',1,1.0)",
            (edition_id,),
        )
    _probe(monkeypatch, FULL)

    result = localmeta.harvest(work_id)

    ed = db.one("SELECT publisher,release_date FROM editions WHERE id=?", (edition_id,))
    assert (ed["publisher"], ed["release_date"]) == ("Original Pub", "2020-01-01")
    assert db.one("SELECT abridged FROM media_assets WHERE id=?", (asset_id,))["abridged"] == 1
    assert [(r["value"], r["source"]) for r in _asins(edition_id)] == [("B0AAAAAAAA", "audible")]
    assert not {"asin", "publisher", "release_date", "abridged"} & set(result["applied"])


@pytest.mark.parametrize("bad", ["B0SHORT", "X0CXYZ1234", "b0cxyz-234", "B0CXYZ12345"])
def test_malformed_asin_is_ignored(monkeypatch, tmp_path, bad):
    work_id, _, edition_id = _book(tmp_path)
    _probe(monkeypatch, {"asin": bad})
    localmeta.harvest(work_id)
    assert _asins(edition_id) == []


def test_asin_owned_by_another_work_is_skipped(monkeypatch, tmp_path):
    other_work, _, other_edition = _book(tmp_path, title="Other Book")
    work_id, _, edition_id = _book(tmp_path)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO identifiers(entity_type,entity_id,namespace,value,source,verified,confidence) VALUES ('edition',?,'asin','B0CXYZ1234','audible',1,1.0)",
            (other_edition,),
        )
    _probe(monkeypatch, {"asin": "B0CXYZ1234", "publisher": "Podium Audio"})

    result = localmeta.harvest(work_id)

    assert _asins(edition_id) == []
    assert [r["entity_id"] for r in db.all("SELECT entity_id FROM identifiers WHERE value='B0CXYZ1234'")] == [other_edition]
    assert "asin" not in result["applied"]
    assert db.one("SELECT publisher FROM editions WHERE id=?", (edition_id,))["publisher"] == "Podium Audio"


def test_asin_token_in_comment_is_found(monkeypatch, tmp_path):
    work_id, _, edition_id = _book(tmp_path)
    _probe(monkeypatch, {"comment": "Audible ASIN B0CXYZ1234 - enjoy"})
    localmeta.harvest(work_id)
    assert [r["value"] for r in _asins(edition_id)] == ["B0CXYZ1234"]


@pytest.mark.parametrize("tags,expected", [
    ({"abridged": "1"}, 1),
    ({"abridged": "0"}, 0),
    ({"abridged": "yes"}, 1),
    ({"unabridged": "1"}, 0),
    ({"unabridged": "false"}, 1),
    ({"comment": "Unabridged, read by R.C. Bray"}, 0),
    ({"comment": "An abridged edition"}, 1),
    ({"comment": "nothing to see"}, None),
])
def test_abridged_variants(monkeypatch, tmp_path, tags, expected):
    work_id, asset_id, _ = _book(tmp_path)
    _probe(monkeypatch, tags)
    localmeta.harvest(work_id)
    assert db.one("SELECT abridged FROM media_assets WHERE id=?", (asset_id,))["abridged"] == expected


def test_publisher_and_date_tag_fallbacks(monkeypatch, tmp_path):
    work_id, _, edition_id = _book(tmp_path)
    _probe(monkeypatch, {"label": "Tantor", "releasedate": "2026-06-01", "date": "2025"})
    localmeta.harvest(work_id)
    ed = db.one("SELECT publisher,release_date FROM editions WHERE id=?", (edition_id,))
    assert (ed["publisher"], ed["release_date"]) == ("Tantor", "2026-06-01")


def test_existing_harvest_behaviour_is_unchanged(monkeypatch, tmp_path):
    work_id, asset_id, _ = _book(tmp_path)
    _probe(monkeypatch, {
        "title": "Dead World: Convergence, Book 6", "composer": "R.C. Bray",
        "description": "A blurb", "date": "2026-05-19", "language": "eng",
    })
    localmeta.harvest(work_id)
    work = db.one("SELECT description,first_publish_date,primary_language FROM works WHERE id=?", (work_id,))
    assert (work["description"], work["first_publish_date"], work["primary_language"]) == ("A blurb", "2026", "eng")
    assert db.json_load(db.one("SELECT narrator_json FROM media_assets WHERE id=?", (asset_id,))["narrator_json"], []) == ["R.C. Bray"]
    assert db.one("SELECT s.name FROM series_memberships m JOIN series s ON s.id=m.series_id WHERE m.work_id=?", (work_id,))["name"] == "Convergence"
