"""Podcast storage (issue #510): migration step 43, stable episode identity, progress keyed by episode id."""
from __future__ import annotations

import sqlite3

import pytest

from app import db, migrations
from app.services import podcasts

from tests.test_migrations_framework import _wipe, build_production_after_d3d036d

TABLES = ("podcast_show", "podcast_season", "podcast_episode", "podcast_progress")


def _episode(conn, title="Pilot", date="2024-01-02", path="/a/1.mp3", season=1):
    show = podcasts.get_or_create_show(conn, "Show")
    sid = podcasts.get_or_create_season(conn, show, season)
    return show, podcasts.insert_episode(conn, show_id=show, season_id=sid, title=title, release_date=date, path=path)


def test_migration_applies_to_an_empty_database():
    _wipe()
    db.init_db()
    with db.session() as conn:
        assert all(migrations._table_exists(conn, t) for t in TABLES)
        assert conn.execute("SELECT 1 FROM migration_steps WHERE version=43").fetchone()
    assert migrations.run_pending_migrations() == []


def test_migration_applies_to_a_database_at_the_previous_version(monkeypatch):
    # Start from the pre-framework production fixture and take it up to step 42 only (the registry is trimmed, no step runs differently).
    build_production_after_d3d036d()
    monkeypatch.setattr(migrations, "MIGRATIONS", [m for m in migrations.MIGRATIONS if m[0] < 43])
    applied = migrations.run_pending_migrations()
    assert applied and max(r["version"] for r in applied) == 42
    with db.session() as conn:
        assert not any(migrations._table_exists(conn, t) for t in TABLES)
        assert conn.execute("SELECT 1 FROM migration_steps WHERE version=43").fetchone() is None
        works_before = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    monkeypatch.undo()
    assert [r["version"] for r in migrations.run_pending_migrations()] == [43, 44, 45, 46, 47, 48]
    with db.session() as conn:
        assert all(migrations._table_exists(conn, t) for t in TABLES)
        assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == works_before > 0
    assert migrations.run_pending_migrations() == []


def test_episode_in_another_shows_season_is_rejected():
    with db.session() as conn:
        show_a = podcasts.get_or_create_show(conn, "Show A")
        show_b = podcasts.get_or_create_show(conn, "Show B")
        season_b = podcasts.get_or_create_season(conn, show_b, 1)
        with pytest.raises(sqlite3.IntegrityError, match="season"):
            podcasts.insert_episode(conn, show_id=show_a, season_id=season_b, title="Pilot", release_date="2024-01-02",
                                    path="/a/1.mp3")
        with pytest.raises(sqlite3.IntegrityError, match="season"):
            podcasts.insert_episode(conn, show_id=show_a, season_id="no-such-season", title="Pilot",
                                    release_date="2024-01-02", path="/a/1.mp3")
        assert conn.execute("SELECT COUNT(*) FROM podcast_episode").fetchone()[0] == 0


def test_duplicate_show_title_and_release_date_is_rejected():
    with db.session() as conn:
        show, _ = _episode(conn)
        sid = podcasts.get_or_create_season(conn, show, 1)
        with pytest.raises(sqlite3.IntegrityError):
            podcasts.insert_episode(conn, show_id=show, season_id=sid, title="Pilot", release_date="2024-01-02",
                                    path="/elsewhere/other.mp3")
        # same title on another date is a different episode
        podcasts.insert_episode(conn, show_id=show, season_id=sid, title="Pilot", release_date="2024-01-09", path="/a/2.mp3")


def test_path_update_keeps_episode_id_and_progress_row():
    with db.session() as conn:
        show, ep = _episode(conn)
        podcasts.set_progress(conn, ep, played=True, position_seconds=42.5)
        assert podcasts.update_episode_path(conn, ep, "/reorganized/pilot.mp3")
        found = podcasts.find_episode(conn, show, "Pilot", "2024-01-02")
        assert found["id"] == ep and found["path"] == "/reorganized/pilot.mp3"
        prog = podcasts.get_progress(conn, ep)
        assert prog["played"] == 1 and prog["position_seconds"] == 42.5
        assert conn.execute("SELECT COUNT(*) FROM podcast_progress").fetchone()[0] == 1
        assert not podcasts.update_episode_path(conn, "missing", "/x.mp3")


def test_extras_marker_round_trips_and_differs_from_numbered_seasons():
    with db.session() as conn:
        show = podcasts.get_or_create_show(conn, "Show")
        zero = podcasts.get_or_create_season(conn, show, 0)
        one = podcasts.get_or_create_season(conn, show, 1)
        extras = podcasts.get_or_create_season(conn, show, None)
        assert len({zero, one, extras}) == 3
        assert podcasts.get_or_create_season(conn, show, None) == extras  # stable
        ep = podcasts.insert_episode(conn, show_id=show, season_id=extras, title="Bonus", release_date="2024-03-04", path="/b.mp3")
        row = podcasts.find_episode(conn, show, "Bonus", "2024-03-04")
        assert row["id"] == ep
        season = podcasts.season_of(conn, row["season_id"])
        assert season["is_extras"] == 1 and season["number"] is None
        assert podcasts.season_of(conn, zero)["is_extras"] == 0 and podcasts.season_of(conn, zero)["number"] == 0
