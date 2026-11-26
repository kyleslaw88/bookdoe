"""Asset format labels (feat/compact-file-lists): assets carry every format present (``formats``)
and a ``primary_format`` chosen by the profile's preferred order, additively, in the Management
work detail and the Player payloads. The Player never lists files or paths."""
import json

from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services import assetformats, migration, tagging

import helpers
from test_player_api import _login, _root, local_mode, pair, world  # noqa: F401


def _asset(tmp_path, title, kind, names, fmt, dur=None, derived_from=None):
    """A work with one owned asset holding the named files (stored format ``fmt``)."""
    _root(tmp_path, kind)
    work_id = migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})
    aid = helpers.own(work_id, kind, fmt)
    for n, name in enumerate(names, 1):
        p = tmp_path / name
        p.write_bytes(b"x" * 300)
        with db.tx() as conn:
            conn.execute("INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,duration_seconds,embedded_json,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,?,?,'{}',1,0,0,?)",
                         (db.uid(), aid, str(p), n if len(names) > 1 else None, 300, dur, db.now()))
    return work_id, aid


def _detail(work_id):
    with TestClient(main_module.app) as c:
        return {a["id"]: a for a in c.get(f"/api/v1/library/{work_id}").json()["assets"]}


def test_epub_plus_mobi_lists_both_with_epub_primary(tmp_path):
    w, a = _asset(tmp_path, "Watch the Skies", "ebook", ["watch the skies.epub", "watch the skies.mobi"], "mobi")
    asset = _detail(w)[a]
    assert asset["formats"] == ["epub", "mobi"] and asset["primary_format"] == "epub"
    assert asset["format"] == "mobi"          # the stored value is untouched for existing consumers
    assert asset["file_count"] == 2


def test_mobi_only_and_azw3_over_mobi_order(tmp_path):
    w, a = _asset(tmp_path, "Old Book", "ebook", ["old.mobi"], "mobi")
    assert _detail(w)[a]["formats"] == ["mobi"] and _detail(w)[a]["primary_format"] == "mobi"
    w2, a2 = _asset(tmp_path, "Mixed Book", "ebook", ["m.mobi", "m.pdf", "m.azw3"], "mobi")
    assert _detail(w2)[a2]["formats"] == ["azw3", "mobi", "pdf"] and _detail(w2)[a2]["primary_format"] == "azw3"


def test_many_mp3_is_one_format_with_totals(tmp_path):
    w, a = _asset(tmp_path, "Long Audio", "audiobook", [f"{i:02d}.mp3" for i in range(1, 48)], "mp3", dur=600)
    asset = _detail(w)[a]
    assert asset["formats"] == ["mp3"] and asset["primary_format"] == "mp3"
    assert asset["file_count"] == 47 and asset["total_duration_seconds"] == 47 * 600 and asset["total_size_bytes"] == 47 * 300


def test_derived_m4b_beside_mp3_original(tmp_path):
    import conv_fixtures as cf
    w, a = _asset(tmp_path, "Derived Audio", "audiobook", ["1.mp3", "2.mp3"], "mp3", dur=60)
    d = cf.add_derived(a, "m4b", tmp_path / "Derived Audio.m4b")
    got = _detail(w)
    assert got[a]["formats"] == ["mp3"] and got[d]["formats"] == ["m4b"] and got[d]["primary_format"] == "m4b"
    assert got[d]["derived_label"] == "Converted from MP3"


def test_preference_follows_the_default_profile(tmp_path):
    with db.tx() as conn:
        conn.execute("UPDATE profiles SET config_json=json_set(config_json,'$.preferred_formats',json('[\"mobi\",\"epub\"]')) WHERE id='ebook-default'")
    w, a = _asset(tmp_path, "Profile Book", "ebook", ["p.epub", "p.mobi"], "epub")
    assert _detail(w)[a]["primary_format"] == "mobi"
    assert assetformats.file_format("/x/Book.kepub.epub") == "kepub"


def test_missing_files_do_not_count():
    assets = [{"kind": "ebook", "format": "epub", "files": [{"path": "/a/b.epub", "missing": 1}, {"path": "/a/b.mobi", "missing": 0, "size_bytes": 5}]}]
    assetformats.annotate(assets)
    assert assets[0]["formats"] == ["mobi"] and assets[0]["file_count"] == 1


def test_player_payload_has_formats_but_no_paths_or_file_lists(world, tmp_path):  # noqa: F811
    wid, aid = _asset(tmp_path, "Both Formats", "ebook", ["both.epub", "both.mobi"], "mobi")
    audio, _ = _asset(tmp_path, "Many Parts", "audiobook", [f"Chapter {i}.mp3" for i in range(1, 13)], "mp3", dur=600)
    tagging.add_tag(wid, tagging.kids_tag_id())
    for who in ("jane", "owen"):
        c, h = world[who]
        dev, _, _ = pair(c, h, name=f"{who} reader")
        lib = dev.get("/api/player/v1/library").json()
        by = {w["title"]: w for w in lib["items"]}
        e = by["Both Formats"]["assets"][0]
        assert e["formats"] == ["epub", "mobi"] and e["primary_format"] == "epub" and e["file_count"] == 2
        raw = json.dumps(lib) + json.dumps(dev.get(f"/api/player/v1/library/{wid}").json())
        for leaked in (str(tmp_path), "both.epub", ".mobi\"", "\"files\"", "\"path\""):
            assert leaked not in raw, leaked
    jane_dev, _, _ = pair(*world["jane"], name="jane two")
    many = next(w for w in jane_dev.get("/api/player/v1/library").json()["items"] if w["title"] == "Many Parts")["assets"][0]
    assert many["formats"] == ["mp3"] and many["file_count"] == 12 and many["duration_seconds"] == 12 * 600
    assert "Chapter 1" not in json.dumps(many)
