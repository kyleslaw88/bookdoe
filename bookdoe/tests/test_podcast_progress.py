"""Podcast played state (issue #515): the threshold rule and the manual mark played / unplayed endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import podcast_progress as pp
from app.services import podcasts


def _episode(duration, title="Pilot"):
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        sid = podcasts.get_or_create_season(conn, show, 1)
        return podcasts.insert_episode(conn, show_id=show, season_id=sid, title=title, release_date="2024-01-02",
                                       path="/a/1.mp3", duration_seconds=duration)


def _progress(ep):
    with db.session() as conn:
        return podcasts.get_progress(conn, ep, "anonymous")


# ---------- the pure rule ----------
def test_94_percent_is_not_played_and_95_percent_is():
    assert pp.is_played(940, 1000) is False
    assert pp.is_played(950, 1000) is True
    assert pp.is_played(1000, 1000) is True


def test_thirty_seconds_remaining_boundary():
    # 300 s episode: 95% is 285 s, so only the remaining-time rule decides at 270 s.
    assert pp.is_played(269, 300) is False   # 31 s left, 89.7%
    assert pp.is_played(270, 300) is True    # exactly 30 s left
    assert pp.is_played(271, 300) is True


def test_episode_shorter_than_thirty_seconds_is_played_only_at_its_end():
    assert pp.is_played(0, 20) is False
    assert pp.is_played(19, 20) is False
    assert pp.is_played(20, 20) is True


def test_thirty_second_episode_is_played_only_at_its_end():
    assert pp.is_played(0, 30) is False
    assert pp.is_played(29, 30) is False
    assert pp.is_played(30, 30) is True
    assert pp.is_played(0, 31) is False  # just above the boundary the remaining-time rule applies again
    assert pp.is_played(1, 31) is True


@pytest.mark.parametrize("duration", [None, 0, -5])
def test_unknown_duration_is_never_played(duration):
    assert pp.is_played(100, duration) is False


# ---------- progress write ----------
def test_progress_write_sets_played_with_the_rule():
    ep = _episode(1000)
    with db.tx() as conn:
        assert pp.write_progress(conn, ep, "anonymous", 940)[1]["played"] == 0
        _, row = pp.write_progress(conn, ep, "anonymous", 950)
    assert row["played"] == 1 and row["position_seconds"] == 950


def test_progress_write_for_unknown_episode_raises():
    with db.tx() as conn, pytest.raises(pp.EpisodeNotFound):
        pp.write_progress(conn, "missing", "anonymous", 1)


def test_progress_endpoint_applies_the_rule():
    ep = _episode(1000)
    with TestClient(app) as c:
        r = c.post(f"/api/player/v1/podcasts/episodes/{ep}/progress", json={"position_seconds": 940})
        assert r.status_code == 200 and r.json()["played"] == 0
        r = c.post(f"/api/player/v1/podcasts/episodes/{ep}/progress", json={"position_seconds": 950})
        assert r.json()["played"] == 1
        assert c.post(f"/api/player/v1/podcasts/episodes/{ep}/progress", json={"position_seconds": -1}).status_code == 422


# ---------- manual actions ----------
def test_mark_played_sets_played_regardless_of_position():
    ep = _episode(1000)
    with TestClient(app) as c:
        r = c.post(f"/api/player/v1/podcasts/episodes/{ep}/played")
    assert r.status_code == 200 and r.json()["played"] == 1
    assert _progress(ep)["played"] == 1 and _progress(ep)["position_seconds"] == 0


def test_mark_played_keeps_a_sub_threshold_position():
    ep = _episode(1000)
    with TestClient(app) as c:
        c.post(f"/api/player/v1/podcasts/episodes/{ep}/progress", json={"position_seconds": 300})
        assert _progress(ep)["played"] == 0
        r = c.post(f"/api/player/v1/podcasts/episodes/{ep}/played")
    assert r.status_code == 200
    assert _progress(ep)["played"] == 1 and _progress(ep)["position_seconds"] == 300


def test_mark_unplayed_resets_position_and_keeps_the_rows():
    ep = _episode(1000)
    with TestClient(app) as c:
        c.post(f"/api/player/v1/podcasts/episodes/{ep}/progress", json={"position_seconds": 990})
        assert _progress(ep)["played"] == 1
        r = c.post(f"/api/player/v1/podcasts/episodes/{ep}/unplayed")
    assert r.status_code == 200 and r.json()["played"] == 0 and r.json()["position_seconds"] == 0
    prog = _progress(ep)
    assert prog is not None and prog["played"] == 0 and prog["position_seconds"] == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM podcast_episode WHERE id=?", (ep,)).fetchone()[0] == 1


@pytest.mark.parametrize("action", ["played", "unplayed"])
def test_manual_actions_404_for_unknown_episode(action):
    with TestClient(app) as c:
        assert c.post(f"/api/player/v1/podcasts/episodes/nope/{action}").status_code == 404
