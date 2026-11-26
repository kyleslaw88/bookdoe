"""Read-only podcast library routes (issue #514), seeded directly through the #510 storage functions."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import podcasts

from tests.test_player_api import local_mode  # noqa: F401  (fixture)

BASE = "/api/v1/podcasts"
USER = "anonymous"


def _seed(tmp_path):
    """Alpha: seasons 1, 2, 10 + Extras, with a cover.jpg on disk. Beta: season 1 only, no cover file."""
    alpha = tmp_path / "Alpha"
    (alpha / "Season 01").mkdir(parents=True)
    (alpha / "Extras").mkdir()
    (alpha / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    ids = {}
    with db.tx() as conn:
        a = podcasts.get_or_create_show(conn, "Alpha")
        b = podcasts.get_or_create_show(conn, "Beta")
        # Extras and season 10 first on purpose: ordering must not follow insertion order.
        rows = ((a, None, "Recap", "2024-05-01", 1, None, "Extras/recap.mp3"),
                (a, 10, "Ten", "2024-04-01", 1, "About ten", "Season 10/ten.mp3"),
                (a, 2, "Two", "2024-03-01", 1, "", "Season 02/two.mp3"),
                (a, 1, "One", "2024-01-01", 1, "First episode", "Season 01/one.mp3"),
                (b, 1, "Solo", "2024-02-01", 1, None, "Season 01/solo.mp3"))
        for show, number, title, date, pos, desc, rel in rows:
            root = alpha if show == a else tmp_path / "Beta"
            sid = podcasts.get_or_create_season(conn, show, number)
            ids[title] = podcasts.insert_episode(
                conn, show_id=show, season_id=sid, title=title, release_date=date, position=pos, description=desc,
                duration_seconds=600.0, path=str(root / rel))
    return {"alpha": a, "beta": b, "ids": ids}


def test_show_list_counts_numbered_seasons_and_extras_separately(tmp_path):
    seed = _seed(tmp_path)
    r = TestClient(app).get(BASE)
    assert r.status_code == 200, r.text
    shows = {s["title"]: s for s in r.json()["shows"]}
    assert [s["title"] for s in r.json()["shows"]] == ["Alpha", "Beta"]
    assert shows["Alpha"]["season_count"] == 3 and shows["Alpha"]["has_extras"] is True
    assert shows["Beta"]["season_count"] == 1 and shows["Beta"]["has_extras"] is False
    assert shows["Alpha"]["cover_url"] == f"{BASE}/{seed['alpha']}/cover"


def test_seasons_ordered_numerically_with_extras_last(tmp_path):
    seed = _seed(tmp_path)
    r = TestClient(app).get(f"{BASE}/{seed['alpha']}")
    assert r.status_code == 200, r.text
    seasons = r.json()["seasons"]
    assert [(s["number"], s["is_extras"]) for s in seasons] == [(1, False), (2, False), (10, False), (None, True)]
    assert [e["title"] for s in seasons for e in s["episodes"]] == ["One", "Two", "Ten", "Recap"]


def test_episode_fields_description_present_and_empty_when_absent(tmp_path):
    seed = _seed(tmp_path)
    r = TestClient(app).get(f"{BASE}/{seed['alpha']}")
    eps = {e["title"]: e for s in r.json()["seasons"] for e in s["episodes"]}
    assert eps["One"]["description"] == "First episode"
    assert eps["Recap"]["description"] == ""   # NULL in the database
    assert eps["Two"]["description"] == ""     # empty string in the database
    one = eps["One"]
    assert one["release_date"] == "2024-01-01" and one["duration_seconds"] == 600.0
    assert one["id"] == seed["ids"]["One"] and one["played"] is False


def test_played_state_is_reported_per_user(tmp_path):
    seed = _seed(tmp_path)
    with db.tx() as conn:
        podcasts.set_progress(conn, seed["ids"]["Two"], played=True, position_seconds=0, user_id=USER)
        podcasts.set_progress(conn, seed["ids"]["One"], played=True, position_seconds=0, user_id="someone-else")
    eps = {e["title"]: e for s in TestClient(app).get(f"{BASE}/{seed['alpha']}").json()["seasons"]
           for e in s["episodes"]}
    assert eps["Two"]["played"] is True
    assert eps["One"]["played"] is False


def test_unknown_show_is_404(tmp_path):
    _seed(tmp_path)
    c = TestClient(app)
    assert c.get(f"{BASE}/nope").status_code == 404
    assert c.get(f"{BASE}/nope/cover").status_code == 404


def test_cover_served_from_stored_cover_jpg(tmp_path):
    seed = _seed(tmp_path)
    r = TestClient(app).get(f"{BASE}/{seed['alpha']}/cover")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8\xff\xe0fakejpeg"


def test_show_without_cover_returns_404_for_cover_url(tmp_path):
    seed = _seed(tmp_path)
    r = TestClient(app).get(f"{BASE}/{seed['beta']}/cover")
    assert r.status_code == 404


def _mp3(path, picture=None, mime="image/jpeg"):
    """A tiny mp3 stub carrying an ID3v2 tag, with an APIC frame when `picture` is given."""
    from mutagen.id3 import APIC, ID3, TIT2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 64)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="x"))
    if picture is not None:
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=picture))
    tags.save(str(path))


def _show_with(tmp_path, name, episodes):
    """episodes: (title, date, picture|None, mime). Returns the show id."""
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, name)
        sid = podcasts.get_or_create_season(conn, show, 1)
        for title, date, picture, mime in episodes:
            f = tmp_path / name / "Season 01" / f"{title}.mp3"
            _mp3(f, picture, mime)
            podcasts.insert_episode(conn, show_id=show, season_id=sid, title=title, release_date=date, position=1,
                                    duration_seconds=1.0, path=str(f), has_cover=picture is not None)
    return show


def test_embedded_cover_used_when_no_cover_jpg_newest_episode_wins(tmp_path):
    png = b"\x89PNG\r\n\x1a\nnewest"
    show = _show_with(tmp_path, "Gamma", [("old", "2023-01-01", b"\xff\xd8\xffold", "image/jpeg"),
                                         ("new", "2024-01-01", png, "image/png"),
                                         ("newest-bare", "2025-01-01", None, "image/jpeg")])
    c = TestClient(app)
    r = c.get(f"{BASE}/{show}/cover")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png" and r.content == png
    assert c.get(f"{BASE}/{show}").json()["cover_url"] == f"{BASE}/{show}/cover"
    assert [s["cover_url"] for s in c.get(BASE).json()["shows"]] == [f"{BASE}/{show}/cover"]
    assert not list((tmp_path / "Gamma").rglob("*.jpg")) and not list((tmp_path / "Gamma").rglob("*.png"))


def test_embedded_jpeg_cover_is_cached_and_not_reread(tmp_path, monkeypatch):
    from app import podcast_library_api as lib
    jpg = b"\xff\xd8\xff\xe0embedded"
    show = _show_with(tmp_path, "Delta", [("e1", "2024-01-01", jpg, "image/jpeg")])
    c = TestClient(app)
    r = c.get(f"{BASE}/{show}/cover")
    assert r.headers["content-type"] == "image/jpeg" and r.content == jpg
    monkeypatch.setattr(lib, "_read_apic", lambda p: (_ for _ in ()).throw(AssertionError("re-read")))
    assert c.get(f"{BASE}/{show}/cover").content == jpg


def test_cover_jpg_wins_over_embedded_artwork(tmp_path):
    show = _show_with(tmp_path, "Eps", [("e1", "2024-01-01", b"\xff\xd8\xffembedded", "image/jpeg")])
    (tmp_path / "Eps" / "cover.jpg").write_bytes(b"\xff\xd8\xfffile")
    assert TestClient(app).get(f"{BASE}/{show}/cover").content == b"\xff\xd8\xfffile"


def test_show_with_neither_source_is_404_and_has_no_cover_url(tmp_path):
    show = _show_with(tmp_path, "Zeta", [("e1", "2024-01-01", None, "image/jpeg")])
    c = TestClient(app)
    assert c.get(f"{BASE}/{show}/cover").status_code == 404
    assert c.get(f"{BASE}/{show}").json()["cover_url"] is None


def test_player_cover_route_serves_embedded_artwork(tmp_path):
    show = _show_with(tmp_path, "Eta", [("e1", "2024-01-01", b"\xff\xd8\xffx", "image/jpeg")])
    r = TestClient(app).get(f"/api/player/v1/podcasts/shows/{show}/cover")
    assert r.status_code == 200 and r.content == b"\xff\xd8\xffx"


def test_podcast_routes_require_sign_in_like_library_routes(local_mode, tmp_path):  # noqa: F811
    seed = _seed(tmp_path)
    c = TestClient(app)
    assert c.get("/api/v1/library").status_code == 401   # the existing library route
    for url in (BASE, f"{BASE}/{seed['alpha']}", f"{BASE}/{seed['alpha']}/cover"):
        assert c.get(url).status_code == 401, url


AUDIO = "/api/player/v1/podcasts/episodes"


def test_episode_audio_streams_the_file_with_range_support(tmp_path):
    seed = _seed(tmp_path)
    one = tmp_path / "Alpha" / "Season 01" / "one.mp3"
    one.write_bytes(bytes(range(256)) * 4)
    c = TestClient(app)
    url = f"{AUDIO}/{seed['ids']['One']}/audio"
    full = c.get(url)
    assert full.status_code == 200 and full.headers["content-type"] == "audio/mpeg" and len(full.content) == 1024
    assert full.headers["content-disposition"] == "inline"
    part = c.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == one.read_bytes()[10:20]


def test_episode_audio_404_for_unknown_episode_and_missing_file(tmp_path):
    seed = _seed(tmp_path)
    c = TestClient(app)
    assert c.get(f"{AUDIO}/nope/audio").status_code == 404
    assert c.get(f"{AUDIO}/{seed['ids']['Two']}/audio").status_code == 404  # row exists, the file was never written
