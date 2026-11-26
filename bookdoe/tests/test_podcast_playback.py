"""Episode manifest (its url is the existing /audio route) behind the Continue Listening tap (issue #522)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import podcasts

BYTES = b"ID3" + bytes(range(256)) * 4


def _episode(tmp_path, name="pilot.mp3", root=True):
    t = db.now()
    if root:
        with db.tx() as conn:
            conn.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,created_at,updated_at) "
                         "VALUES (?,?,?,?,'adopt',?,?)", (db.uid(), "Root", str(tmp_path), "audiobook", t, t))
    f = tmp_path / name
    f.write_bytes(BYTES)
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "The Show")
        season = podcasts.get_or_create_season(conn, show, 1)
        return podcasts.insert_episode(conn, show_id=show, season_id=season, title="Pilot", release_date="2024-01-01",
                                       path=str(f), position=1, duration_seconds=1000)


def test_manifest_gives_title_show_audio_url_and_resume_position(tmp_path):
    ep = _episode(tmp_path)
    with db.tx() as conn:
        podcasts.set_progress(conn, ep, played=False, position_seconds=250.0, user_id="anonymous")
    with TestClient(app) as c:
        m = c.get(f"/api/player/v1/podcasts/episodes/{ep}").json()
        assert (m["title"], m["show_title"], m["position_seconds"]) == ("Pilot", "The Show", 250.0)
        assert m["url"] == f"/api/player/v1/podcasts/episodes/{ep}/audio"
        r = c.get(m["url"], headers={"Range": "bytes=3-9"})
        assert r.status_code == 206 and r.content == BYTES[3:10]
        assert c.get(m["url"]).content == BYTES


def test_played_episode_restarts_from_the_beginning(tmp_path):
    ep = _episode(tmp_path)
    with db.tx() as conn:
        podcasts.set_progress(conn, ep, played=True, position_seconds=990.0, user_id="anonymous")
    with TestClient(app) as c:
        assert c.get(f"/api/player/v1/podcasts/episodes/{ep}").json()["position_seconds"] == 0.0


def test_unknown_episode_and_file_outside_every_root_are_404(tmp_path):
    outside = tmp_path / "lib"
    outside.mkdir()
    ep = _episode(tmp_path, root=False)
    with TestClient(app) as c:
        assert c.get("/api/player/v1/podcasts/episodes/nope").status_code == 404
        assert c.get(f"/api/player/v1/podcasts/episodes/{ep}").status_code == 404
