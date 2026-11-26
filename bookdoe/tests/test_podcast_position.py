"""Podcast resume position synced across devices (issue #516)."""
from __future__ import annotations

from app import db
from app.services import podcasts

from tests.test_player_api import local_mode, pair, world, _login  # noqa: F401  (fixtures)

BASE = "/api/player/v1/podcasts/episodes"


def _episode(path="/a/1.mp3", duration=None):
    with db.tx() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        season = podcasts.get_or_create_season(conn, show, 1)
        return podcasts.insert_episode(conn, show_id=show, season_id=season, title="Pilot",
                                       release_date="2024-01-02", path=path,
                                       duration_seconds=duration)


def test_position_written_on_device_a_is_read_on_device_b(world):
    jane, jh = world["jane"]
    dev_a, _, _ = pair(jane, jh, name="Phone")
    dev_b, _, _ = pair(jane, jh, name="Tablet")
    ep = _episode()
    ts = db.now() - 100
    r = dev_a.put(f"{BASE}/{ep}/progress", json={"position_seconds": 123.5, "client_updated_at": ts})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    got = dev_b.get(f"{BASE}/{ep}/progress")
    assert got.status_code == 200, got.text
    assert got.json()["progress"]["position_seconds"] == 123.5
    assert got.json()["progress"]["client_updated_at"] == ts


def test_older_write_does_not_overwrite_newer_position(world):
    jane, jh = world["jane"]
    dev_a, _, _ = pair(jane, jh, name="Phone")
    dev_b, _, _ = pair(jane, jh, name="Tablet")
    ep = _episode()
    now = db.now()
    assert dev_a.put(f"{BASE}/{ep}/progress", json={"position_seconds": 500, "client_updated_at": now - 10}).json()["applied"]
    r = dev_b.put(f"{BASE}/{ep}/progress", json={"position_seconds": 40, "client_updated_at": now - 50})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False
    assert r.json()["progress"]["position_seconds"] == 500
    assert dev_a.get(f"{BASE}/{ep}/progress").json()["progress"]["position_seconds"] == 500
    # a newer write does win
    assert dev_b.put(f"{BASE}/{ep}/progress", json={"position_seconds": 600, "client_updated_at": now - 5}).json()["applied"]
    assert dev_a.get(f"{BASE}/{ep}/progress").json()["progress"]["position_seconds"] == 600


def test_position_survives_a_path_change(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    ep = _episode()
    dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 77, "client_updated_at": db.now() - 20})
    with db.tx() as conn:
        assert podcasts.update_episode_path(conn, ep, "/reorganized/pilot.mp3")
    got = dev.get(f"{BASE}/{ep}/progress").json()
    assert got["progress"]["position_seconds"] == 77


def test_unknown_episode_is_404_and_other_user_does_not_see_position(world):
    jane, jh = world["jane"]
    owen, oh = world["owen"]
    ep = _episode()
    assert jane.get(f"{BASE}/nope/progress").status_code == 404
    jane.put(f"{BASE}/{ep}/progress", json={"position_seconds": 9, "client_updated_at": db.now() - 5}, headers=jh)
    assert owen.get(f"{BASE}/{ep}/progress").json()["progress"] is None


def test_put_past_the_threshold_marks_played(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    ep = _episode(duration=1000)
    now = db.now()
    r = dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 950, "client_updated_at": now - 10})
    assert r.json()["progress"]["played"] is True
    r = dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 100, "client_updated_at": now - 5})
    assert r.json()["progress"]["played"] is False


def test_post_and_put_share_one_timestamp_guard(world):
    jane, jh = world["jane"]
    dev_a, _, _ = pair(jane, jh, name="Phone")
    dev_b, _, _ = pair(jane, jh, name="Tablet")
    ep = _episode()
    now = db.now()
    assert dev_a.put(f"{BASE}/{ep}/progress", json={"position_seconds": 500, "client_updated_at": now - 10}).json()["applied"]
    r = dev_b.post(f"{BASE}/{ep}/progress", json={"position_seconds": 40, "client_updated_at": now - 50})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False and r.json()["position_seconds"] == 500
    # a POST without a timestamp is stamped with server time and wins
    r = dev_b.post(f"{BASE}/{ep}/progress", json={"position_seconds": 700})
    assert r.json()["applied"] is True
    assert dev_a.get(f"{BASE}/{ep}/progress").json()["progress"]["position_seconds"] == 700
    # and a PUT older than that POST loses
    r = dev_a.put(f"{BASE}/{ep}/progress", json={"position_seconds": 5, "client_updated_at": now - 1})
    assert r.json()["applied"] is False


def test_put_after_mark_unplayed_orders_by_time(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    ep = _episode(duration=1000)
    now = db.now()
    dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 990, "client_updated_at": now - 100})
    assert dev.post(f"{BASE}/{ep}/unplayed", headers=jh).status_code == 200
    # recorded before the reset: stale
    assert dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 400, "client_updated_at": now - 50}).json()["applied"] is False
    # recorded after the reset: applied
    r = dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 400, "client_updated_at": db.now() + 5})
    assert r.json()["applied"] is True and r.json()["progress"]["position_seconds"] == 400


def test_put_requires_a_timestamp(world):
    jane, jh = world["jane"]
    dev, _, _ = pair(jane, jh)
    ep = _episode()
    assert dev.put(f"{BASE}/{ep}/progress", json={"position_seconds": 1}).status_code == 422
