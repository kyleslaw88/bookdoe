"""Podcast folder scanner (#511): fixture tree of synthetic mp3s in a temp dir, real scan entry point, no network."""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from app import db
from app.services import podcasts, podcastscan
from app.services.podcastscan import scan_podcasts

from tests.test_podcastmeta import _episode, _text


def _tree(root: Path) -> Path:
    """Alpha: Season 01 (2 eps), Season 02 (1), Extras (1), cover.jpg. Beta: Season 01 (1), no cover."""
    _episode(root, "Alpha/Season 01/001 - Pilot.mp3", _text("TDRC", "2024-01-01"))
    _episode(root, "Alpha/Season 01/002 - Second.mp3", _text("TDRC", "2024-01-08"), _text("TRCK", "2/2"))
    _episode(root, "Alpha/Season 02/2024-03-01 - Return.mp3")
    _episode(root, "Alpha/Extras/2024-02-01 - Trailer.mp3", _text("TALB", "Extras"))
    (root / "Alpha" / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    _episode(root, "Beta/Season 01/2023-05-05 - Hello.mp3", _text("TIT2", "Hello There"))
    return root


def _counts() -> dict[str, int]:
    with db.session() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # nosec B608
                for t in ("podcast_show", "podcast_season", "podcast_episode")}


def test_scan_creates_shows_seasons_and_episodes(tmp_path):
    tmp_path = tmp_path / 'pods'
    result = scan_podcasts(_tree(tmp_path))
    assert _counts() == {"podcast_show": 2, "podcast_season": 4, "podcast_episode": 5}
    assert (result.shows, result.episodes_added, result.episodes_updated, result.skipped) == (2, 5, 0, [])
    with db.session() as conn:
        eps = {r["title"]: r for r in conn.execute("SELECT * FROM podcast_episode")}
    assert "Hello There" in eps and eps["Second"]["position"] == 2  # tags win, path fills the rest


def test_rescan_unchanged_tree_creates_no_duplicates(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    before = _counts()
    with db.session() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM podcast_episode")}
    again = scan_podcasts(tmp_path)
    assert _counts() == before
    assert (again.episodes_added, again.episodes_updated) == (0, 5)
    with db.session() as conn:
        assert {r["id"] for r in conn.execute("SELECT id FROM podcast_episode")} == ids


def test_extras_kept_separate_from_numbered_seasons(tmp_path):
    tmp_path = tmp_path / 'pods'
    scan_podcasts(_tree(tmp_path))
    with db.session() as conn:
        rows = conn.execute(
            "SELECT s.number, s.is_extras, e.title FROM podcast_episode e JOIN podcast_season s ON s.id=e.season_id "
            "JOIN podcast_show h ON h.id=e.show_id WHERE h.title='Alpha'").fetchall()
        extras = [r for r in rows if r["is_extras"]]
        assert [(r["number"], r["title"]) for r in extras] == [(None, "Trailer")]
        assert sorted(r["number"] for r in rows if not r["is_extras"]) == [1, 1, 2]


def test_bad_file_is_logged_and_scan_continues(tmp_path, caplog):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    (tmp_path / "Alpha/Season 01/003 - Broken.mp3").write_bytes(b"this is not an mp3 at all")
    with caplog.at_level(logging.WARNING, logger=podcastscan.log.name):
        result = scan_podcasts(tmp_path)
    assert [Path(p).name for p in result.skipped] == ["003 - Broken.mp3"]
    assert "Broken.mp3" in caplog.text
    assert _counts()["podcast_episode"] == 5


def test_exception_in_one_file_does_not_stop_scan(tmp_path, monkeypatch, caplog):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    real = podcastscan.parse_episode

    def flaky(path):
        if Path(path).name.startswith("002"):
            raise RuntimeError("boom")
        return real(path)

    monkeypatch.setattr(podcastscan, "parse_episode", flaky)
    result = scan_podcasts(tmp_path)
    assert len(result.skipped) == 1 and _counts()["podcast_episode"] == 4


def test_has_cover_follows_cover_jpg(tmp_path):
    tmp_path = tmp_path / 'pods'
    scan_podcasts(_tree(tmp_path))
    with db.session() as conn:
        covers = {r["title"]: r["has_cover"] for r in conn.execute("SELECT title,has_cover FROM podcast_show")}
    assert covers == {"Alpha": 1, "Beta": 0}
    (tmp_path / "Alpha" / "cover.jpg").unlink()
    scan_podcasts(tmp_path)
    with db.session() as conn:
        assert conn.execute("SELECT has_cover FROM podcast_show WHERE title='Alpha'").fetchone()[0] == 0


def test_non_mp3_and_dotfiles_are_skipped(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    (tmp_path / "Alpha/Season 01/notes.txt").write_text("x")
    (tmp_path / "Alpha/Season 01/001 - Pilot.m4a").write_bytes(b"x")
    _episode(tmp_path, "Alpha/Season 01/._003 - Hidden.mp3")
    _episode(tmp_path, ".hidden/Season 01/2024-01-01 - Nope.mp3")
    result = scan_podcasts(tmp_path)
    assert _counts()["podcast_episode"] == 5 and result.skipped == []
    assert _counts()["podcast_show"] == 2


def test_folder_names_are_the_fallback_without_tags(tmp_path):
    tmp_path = tmp_path / 'pods'
    _episode(tmp_path, "Folder Show/Season 07/2022-02-02 - Untagged.mp3")
    scan_podcasts(tmp_path)
    with db.session() as conn:
        row = conn.execute(
            "SELECT h.title show, s.number, e.release_date FROM podcast_episode e JOIN podcast_show h ON h.id=e.show_id "
            "JOIN podcast_season s ON s.id=e.season_id").fetchone()
    assert (row["show"], row["number"], row["release_date"]) == ("Folder Show", 7, "2022-02-02")


def test_file_without_release_date_is_skipped_not_fatal(tmp_path):
    tmp_path = tmp_path / 'pods'
    _episode(tmp_path, "Alpha/Season 01/001 - NoDate.mp3")
    result = scan_podcasts(tmp_path)
    assert len(result.skipped) == 1 and _counts()["podcast_episode"] == 0


def test_rescan_after_move_keeps_episode_id_and_progress(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    with db.session() as conn:
        ep = conn.execute("SELECT id FROM podcast_episode WHERE title='Pilot'").fetchone()["id"]
        podcasts.set_progress(conn, ep, played=False, position_seconds=12.0)
    (tmp_path / "Alpha/Season 05").mkdir()
    (tmp_path / "Alpha/Season 01/001 - Pilot.mp3").rename(tmp_path / "Alpha/Season 05/001 - Pilot.mp3")
    scan_podcasts(tmp_path)
    with db.session() as conn:
        row = conn.execute("SELECT id,path FROM podcast_episode WHERE title='Pilot'").fetchone()
        assert row["id"] == ep and row["path"].endswith("Season 05/001 - Pilot.mp3")
        assert podcasts.get_progress(conn, ep)["position_seconds"] == 12.0


def test_podcast_scan_job_walks_the_configured_folder(tmp_path, monkeypatch):
    from app.services import scheduler
    job = db.one("SELECT * FROM jobs WHERE kind='podcast-scan'")
    assert job and job["enabled"] == 1
    monkeypatch.setattr(podcastscan, "settings", dataclasses.replace(podcastscan.settings, podcasts_dir=None))
    assert scheduler.run_job(job["id"])["result"]["skipped"] is True
    monkeypatch.setattr(podcastscan, "settings", dataclasses.replace(podcastscan.settings, podcasts_dir=_tree(tmp_path / "pods")))
    out = scheduler.run_job(job["id"])
    assert out["status"] == "success" and out["result"]["episodes_added"] == 5
    assert _counts() == {"podcast_show": 2, "podcast_season": 4, "podcast_episode": 5}
    scheduler.shutdown_pool()


def _row(title):
    with db.session() as conn:
        return dict(conn.execute("SELECT e.*, s.number season_number FROM podcast_episode e "
                                 "JOIN podcast_season s ON s.id=e.season_id WHERE e.title=?", (title,)).fetchone())


def test_move_flat_to_season_folder_keeps_id_progress_and_updates_season(tmp_path):
    tmp_path = tmp_path / 'pods'
    _episode(tmp_path, "Alpha/2024-01-01 - Pilot.mp3", _text("TPOS", "1"))
    _episode(tmp_path, "Alpha/2024-01-08 - Other.mp3", _text("TPOS", "1"))
    scan_podcasts(tmp_path)
    pilot, other = _row("Pilot"), _row("Other")
    with db.session() as conn:
        podcasts.set_progress(conn, pilot["id"], played=True, position_seconds=99.0)
    (tmp_path / "Alpha/Season 03").mkdir()
    (tmp_path / "Alpha/2024-01-01 - Pilot.mp3").rename(tmp_path / "Alpha/Season 03/2024-01-01 - Pilot.mp3")
    result = scan_podcasts(tmp_path)
    after = _row("Pilot")
    assert after["id"] == pilot["id"] and after["path"].endswith("Season 03/2024-01-01 - Pilot.mp3")
    assert (result.episodes_added, result.episodes_moved, result.episodes_missing) == (0, 1, 0)
    assert _counts()["podcast_episode"] == 2
    with db.session() as conn:
        prog = podcasts.get_progress(conn, pilot["id"])
        assert (prog["played"], prog["position_seconds"]) == (1, 99.0)
    other_after = _row("Other")  # nothing else changed
    assert other_after["id"] == other["id"] and other_after["path"] == other["path"] and not other_after["missing"]


def test_move_to_another_numbered_season_updates_season_number(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    before = _row("Return")
    assert before["season_number"] == 2
    (tmp_path / "Alpha/Season 04").mkdir()
    (tmp_path / "Alpha/Season 02/2024-03-01 - Return.mp3").rename(tmp_path / "Alpha/Season 04/2024-03-01 - Return.mp3")
    # the path-derived season follows the folder only when no TPOS tag says otherwise (tags win)
    scan_podcasts(tmp_path)
    after = _row("Return")
    assert after["id"] == before["id"] and after["season_number"] == 4


def test_rename_with_same_title_and_date_keeps_id(tmp_path):
    tmp_path = tmp_path / 'pods'
    _episode(tmp_path, "Alpha/Season 01/001 - Old Name.mp3", _text("TIT2", "Pilot"), _text("TDRC", "2024-01-01"))
    scan_podcasts(tmp_path)
    before = _row("Pilot")
    old = tmp_path / "Alpha/Season 01/001 - Old Name.mp3"
    old.rename(old.with_name("pilot-final.mp3"))
    scan_podcasts(tmp_path)
    after = _row("Pilot")
    assert after["id"] == before["id"] and after["path"].endswith("pilot-final.mp3")
    assert _counts()["podcast_episode"] == 1


def test_missing_file_is_flagged_and_progress_kept(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    ep = _row("Second")
    with db.session() as conn:
        podcasts.set_progress(conn, ep["id"], played=False, position_seconds=33.0)
    (tmp_path / "Alpha/Season 01/002 - Second.mp3").unlink()
    result = scan_podcasts(tmp_path)
    assert result.episodes_missing == 1 and _counts()["podcast_episode"] == 5
    assert _row("Second")["missing"] == 1 and not _row("Pilot")["missing"]
    with db.session() as conn:
        assert podcasts.get_progress(conn, ep["id"])["position_seconds"] == 33.0
    assert scan_podcasts(tmp_path).episodes_missing == 0  # flagged once, not re-counted


def test_missing_episode_that_reappears_is_cleared_with_same_id(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    ep = _row("Second")
    src = tmp_path / "Alpha/Season 01/002 - Second.mp3"
    parked = tmp_path / "parked.bin"
    src.rename(parked)
    scan_podcasts(tmp_path)
    assert _row("Second")["missing"] == 1
    parked.rename(src)
    scan_podcasts(tmp_path)
    after = _row("Second")
    assert after["id"] == ep["id"] and after["missing"] == 0


def test_unreadable_file_still_on_disk_is_not_flagged_missing(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    (tmp_path / "Alpha/Season 01/002 - Second.mp3").write_bytes(b"corrupted")
    result = scan_podcasts(tmp_path)
    assert len(result.skipped) == 1 and result.episodes_missing == 0 and not _row("Second")["missing"]


def test_absent_root_flags_nothing(tmp_path):
    tmp_path = tmp_path / 'pods'
    _tree(tmp_path)
    scan_podcasts(tmp_path)
    result = scan_podcasts(tmp_path / "unmounted")
    assert result.episodes_missing == 0
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM podcast_episode WHERE missing=1").fetchone()[0] == 0


def _real_mp3(path: Path, seconds: float) -> None:
    """Valid CBR MPEG-1 Layer III, 128 kbps / 44.1 kHz (417-byte frames of 1152 samples), no TLEN."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * round(seconds * 44100 / 1152))


def test_scan_measures_duration_of_fixture_mp3(tmp_path):
    tmp_path = tmp_path / "pods"
    _real_mp3(tmp_path / "Alpha/Season 01/2024-01-01 - Timed.mp3", 12.0)
    scan_podcasts(tmp_path)
    with db.session() as conn:
        d = conn.execute("SELECT duration_seconds FROM podcast_episode").fetchone()[0]
    assert d is not None and abs(d - 12.0) <= 1


def test_rescan_fills_null_duration(tmp_path):
    tmp_path = tmp_path / "pods"
    _real_mp3(tmp_path / "Alpha/Season 01/2024-01-01 - Timed.mp3", 8.0)
    scan_podcasts(tmp_path)
    with db.session() as conn:
        conn.execute("UPDATE podcast_episode SET duration_seconds=NULL")
    scan_podcasts(tmp_path)
    with db.session() as conn:
        d = conn.execute("SELECT duration_seconds FROM podcast_episode").fetchone()[0]
    assert d is not None and abs(d - 8.0) <= 1


def test_corrupt_file_has_no_duration_and_scan_continues(tmp_path):
    from app.services.podcastmeta import measure_duration
    tmp_path = tmp_path / "pods"
    bad = tmp_path / "Alpha/Season 01/2024-01-01 - Bad.mp3"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not audio at all")
    _real_mp3(tmp_path / "Alpha/Season 01/2024-01-08 - Good.mp3", 5.0)
    assert measure_duration(bad) is None
    result = scan_podcasts(tmp_path)
    assert result.episodes_added == 1 and str(bad) in result.skipped


def test_tlen_used_only_when_file_cannot_be_measured(tmp_path):
    from app.services.podcastmeta import measure_duration
    p = _episode(tmp_path, "S/2024-01-01 - T.mp3", _text("TLEN", "90000"))
    assert measure_duration(p) == 90.0
