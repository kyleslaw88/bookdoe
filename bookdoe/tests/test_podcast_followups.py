"""Podcast follow-ups (#541): root pickers, missing episodes, square mobile covers, Player shell show page."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import podcast_queue as pq, podcasts

USER = "anonymous"

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bookdoe" / "app" / "static"


def _show(missing=()):
    """Show with season 1 (a, b, c); titles in ``missing`` are flagged missing. Returns (show_id, {title: id})."""
    ids = {}
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        sid = podcasts.get_or_create_season(conn, show, 1)
        for pos, title in enumerate("abc", 1):
            ids[title] = podcasts.insert_episode(conn, show_id=show, season_id=sid, title=title, release_date=f"2024-01-0{pos}",
                                                 path=f"/p/{title}.mp3", position=pos, duration_seconds=60)
        for title in missing:
            podcasts.set_missing(conn, ids[title], True)
    return show, ids


def test_next_unplayed_skips_missing_episode():
    show, ids = _show(missing=("a",))
    with db.session() as conn:
        assert pq.next_unplayed(conn, show, USER)["title"] == "b"


def test_play_next_skips_missing_episode_and_ends_when_only_missing_remain():
    _, ids = _show(missing=("b",))
    with db.session() as conn:
        assert pq.play_next(conn, ids["a"], USER)["title"] == "c"
    with db.tx() as conn:
        podcasts.set_missing(conn, ids["c"], True)
    with db.session() as conn:
        assert pq.play_next(conn, ids["a"], USER) is None


def test_play_next_from_a_missing_episode_still_works():
    _, ids = _show(missing=("a",))
    with db.session() as conn:
        assert pq.play_next(conn, ids["a"], USER)["title"] == "b"


def test_library_api_still_lists_missing_episode_with_marker():
    show, ids = _show(missing=("b",))
    with TestClient(app) as c:
        eps = c.get(f"/api/v1/podcasts/{show}").json()["seasons"][0]["episodes"]
        assert {e["title"]: e["missing"] for e in eps} == {"a": False, "b": True, "c": False}
        assert c.get(f"/api/v1/podcasts/episodes/{ids['b']}/audio").status_code == 404
        player = c.get(f"/api/player/v1/podcasts/shows/{show}").json()["seasons"][0]["episodes"]
        assert [e["missing"] for e in player] == [False, True, False]
        assert c.get(f"/api/player/v1/podcasts/shows/{show}/next").json()["episode"]["title"] == "a"


def test_episode_audio_streams_the_file(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"ID3audio")
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "S")
        sid = podcasts.get_or_create_season(conn, show, 1)
        ep = podcasts.insert_episode(conn, show_id=show, season_id=sid, title="t", release_date="2024-01-01", path=str(f))
    with TestClient(app) as c:
        for base in ("/api/v1/podcasts", "/api/player/v1/podcasts"):
            r = c.get(f"{base}/episodes/{ep}/audio")
            assert r.status_code == 200 and r.content == b"ID3audio"
        assert c.get("/api/v1/podcasts/episodes/nope/audio").status_code == 404


def test_podcasts_option_in_both_root_pickers():
    app_js = (STATIC / "app.js").read_text()
    picker = re.search(r"function openAddRoot\(\).*?\n", app_js).group(0)
    assert '<option value="podcasts">Podcasts</option>' in picker
    assert "rootPayload(" in picker
    setup_html = (STATIC / "setup.html").read_text()
    assert '<option value="podcasts">Podcasts</option>' in setup_html
    assert "library_type: pod ? 'podcasts' : 'books'" in setup_html


def test_management_api_creates_a_podcasts_root(tmp_path):
    pods = tmp_path / "pods"
    pods.mkdir()
    with TestClient(app) as c:
        r = c.post("/api/v1/roots", json={"name": "Pods", "path": str(pods), "media_kind": "audiobook", "library_type": "podcasts"})
    assert r.status_code == 200, r.text
    row = db.one("SELECT library_type,management_mode FROM root_folders WHERE path=?", (str(pods),))
    assert (row["library_type"], row["management_mode"]) == ("podcasts", "observe")


def test_setup_wizard_apply_creates_a_podcasts_root(tmp_path, monkeypatch):
    from app import secrets_provider, setup
    from app.config import settings
    object.__setattr__(settings, "auth_mode", "local")
    monkeypatch.setenv(secrets_provider.ENV_FILE_VAR, str(tmp_path / "setup.env"))
    pods = tmp_path / "pods"
    pods.mkdir()
    try:
        req = setup.SetupRequest(
            admin={"username": "owner", "password": "a-long-password-123", "email": "o@example.com"},
            public_url="https://books.example.com/",
            roots=[{"name": "Pods", "path": str(pods), "media_kind": "audiobook", "library_type": "podcasts"}],
            metadata={}, secrets={"backend": "env"})
        result = setup.apply(req)
    finally:
        object.__setattr__(settings, "auth_mode", "none")
    assert result["roots"][0]["status"] == "created"
    assert db.one("SELECT library_type FROM root_folders WHERE path=?", (str(pods),))["library_type"] == "podcasts"


def test_mobile_covers_pick_the_frame_from_the_loaded_art():
    """CoverImage has no caller flag: both apps size the frame from the image via the unit-tested core helper."""
    swift = (ROOT / "ios/App/Views/CoverImage.swift").read_text()
    assert "CoverSizing.heightFactor(" in swift and "size * heightFactor" in swift
    kt = (ROOT / "android/app/src/main/kotlin/bookdoe/app/ui/Cover.kt").read_text()
    assert "CoverAspect.isSquare(" in kt and "ContentScale.Fit" in kt
    assert "testSquareArtGetsSquareFrame" in (ROOT / "ios/Packages/BookdoeKit/Tests/BookdoeKitTests/CoverSizingTests.swift").read_text()
    assert "squareArtIsNotCropped" in (ROOT / "android/core/src/test/kotlin/bookdoe/core/CoverAspectTest.kt").read_text()


def test_player_shell_endpoints_serve_show_page_and_playback(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3audio")
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        sid = podcasts.get_or_create_season(conn, show, 1)
        e1 = podcasts.insert_episode(conn, show_id=show, season_id=sid, title="one", release_date="2024-01-01",
                                     path=str(audio), position=1, duration_seconds=600)
        e2 = podcasts.insert_episode(conn, show_id=show, season_id=sid, title="two", release_date="2024-01-02",
                                     path=str(audio), position=2, duration_seconds=600)
    base = "/api/player/v1/podcasts"
    with TestClient(app) as c:
        listing = c.get(base)
        assert listing.status_code == 200 and [s["title"] for s in listing.json()["shows"]] == ["Show"]
        page = c.get(f"{base}/shows/{show}")
        assert page.status_code == 200
        assert [e["id"] for e in page.json()["seasons"][0]["episodes"]] == [e1, e2]
        assert c.get(f"{base}/shows/{show}/cover").status_code in (200, 404)  # no artwork on disk: 404, the page hides the img
        assert c.get(f"{base}/episodes/{e1}/audio").content == b"ID3audio"
        assert c.get(f"{base}/episodes/{e1}/progress").json() == {"episode_id": e1, "progress": None}
        # the exact body player.js sends: seconds for both fields
        put = c.put(f"{base}/episodes/{e1}/progress", json={"position_seconds": 42.5, "client_updated_at": 1700000000.5})
        assert put.status_code == 200 and put.json()["applied"] is True
        assert put.json()["progress"]["position_seconds"] == 42.5
        got = c.get(f"{base}/episodes/{e1}/progress").json()["progress"]
        assert got["position_seconds"] == 42.5 and got["played"] is False
        played = c.post(f"{base}/episodes/{e1}/played", json={})
        assert played.status_code == 200 and bool(played.json()["played"])
        assert c.get(f"{base}/episodes/{e1}/next").json()["episode"]["id"] == e2
        assert c.get(f"{base}/shows/{show}/next").json()["episode"]["id"] == e2


def test_player_shell_routes_and_serves_podcast_page():
    js = (STATIC / "player.js").read_text()
    assert "if (r === 'podcasts' && id) return await renderPodcastShow(id);" in js
    assert "if (r === 'podcasts') return await renderPodcasts();" in js
    assert "/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/audio" in js
    assert "{ position_seconds: audio.currentTime, client_updated_at: Date.now() / 1000 }" in js
    with TestClient(app) as c:
        assert "player.js" in c.get("/app").text
