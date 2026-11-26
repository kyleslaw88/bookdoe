"""DB-driven maintenance detectors: missing files, files outside roots, DB/path mismatches (P5-19a)."""
from __future__ import annotations

import os
from pathlib import Path

from app import db
from app.services import maintenance

DETECTORS = ("missing-files", "files-outside-roots", "db-path-mismatch")


def _root(tmp_path: Path, name: str, mode: str = "adopt", enabled: int = 1) -> tuple[str, Path]:
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    rid, t = db.uid(), db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at)"
            " VALUES (?,?,?,'ebook',?,1,?,?,?)", (rid, name, str(base), mode, enabled, t, t))
    return rid, base


def _asset(title: str, status: str = "owned", root_id: str | None = None) -> str:
    wid, aid, t = db.uid(), db.uid(), db.now()
    with db.tx() as conn:
        conn.execute("INSERT INTO works(id,title,added_at,updated_at) VALUES (?,?,?,?)", (wid, title, t, t))
        conn.execute(
            "INSERT INTO media_assets(id,work_id,kind,format,status,root_folder_id,added_at,updated_at) VALUES (?,?,'ebook','epub',?,?,?,?)",
            (aid, wid, status, root_id, t, t))
    return aid


def _file(asset_id: str, path: str | Path, rel: str | None = None, missing: int = 0, write: bool = True) -> str:
    fid, p = db.uid(), Path(path)
    if write:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO media_files(id,asset_id,path,relative_path,missing,discovered_at,verified_at) VALUES (?,?,?,?,?,?,?)",
            (fid, asset_id, str(path), rel, missing, db.now(), 50.0))
    return fid


def _healthy(tmp_path: Path, title: str = "Dead World") -> tuple[str, Path, str, str]:
    rid, base = _root(tmp_path, "lib")
    aid = _asset(title, root_id=rid)
    fid = _file(aid, base / "A" / "f.epub", "A/f.epub")
    return rid, base, aid, fid


def _run():
    return maintenance.run_all(DETECTORS)


def _open(kind: str):
    return db.all("SELECT * FROM maintenance_issues WHERE kind=? AND status='open' ORDER BY fingerprint", (kind,))


def test_healthy_library_produces_no_issues(tmp_path):
    _healthy(tmp_path)
    result = _run()
    assert result["complete"] and result["opened"] == 0
    assert db.one("SELECT COUNT(*) n FROM maintenance_issues")["n"] == 0


def test_removed_file_yields_missing_file_and_restoring_resolves_it(tmp_path):
    _, base, aid, fid = _healthy(tmp_path)
    path = base / "A" / "f.epub"
    path.unlink()
    _run()
    (issue,) = _open("missing_file")
    detail = db.json_load(issue["detail_json"], {})
    assert issue["severity"] == "error" and issue["subject_id"] == fid
    assert detail["work_title"] == "Dead World" and detail["asset_id"] == aid and detail["verified_at"] == 50.0
    assert detail["on_disk"] is False and issue["repair_action"] == "unlink_missing"

    path.write_bytes(b"x")
    _run()
    assert _open("missing_file") == []
    assert db.one("SELECT status FROM maintenance_issues WHERE id=?", (issue["id"],))["status"] == "resolved"


def test_missing_flag_and_disk_disagreement_is_noted_and_non_sole_file_is_a_warning(tmp_path):
    _, base, aid, _ = _healthy(tmp_path)
    flagged = _file(aid, base / "A" / "g.epub", "A/g.epub", missing=1)  # flagged, but present on disk
    _run()
    (issue,) = _open("missing_file")
    detail = db.json_load(issue["detail_json"], {})
    assert issue["subject_id"] == flagged and issue["severity"] == "warning"
    assert detail["flag_disagrees_with_disk"] is True and issue["repair_action"] == "refresh_state"


def test_file_in_no_enabled_root_is_reported_and_adopted_root_clears_it(tmp_path):
    _healthy(tmp_path)
    other = tmp_path / "elsewhere"
    fid = _file(_asset("Stray"), other / "s.epub")
    _run()
    (issue,) = _open("file_outside_roots")
    assert issue["subject_id"] == fid and issue["repair_action"] == "relink_root"
    assert db.json_load(issue["detail_json"], {})["nearest_root"]["name"] == "lib"
    _root(tmp_path, "elsewhere", mode="adopt")
    _run()
    assert _open("file_outside_roots") == []


def test_file_under_a_disabled_root_is_outside_and_names_that_root(tmp_path):
    _, off = _root(tmp_path, "off", enabled=0)
    fid = _file(_asset("Parked"), off / "p.epub", "p.epub")
    _run()
    (issue,) = _open("file_outside_roots")
    near = db.json_load(issue["detail_json"], {})["nearest_root"]
    assert issue["subject_id"] == fid and near["name"] == "off" and near["enabled"] is False


def _mismatches():
    return {i["fingerprint"].split("|")[-1]: i for i in _open("db_path_mismatch")}


def test_mismatch_a_relative_path_differs_from_path(tmp_path):
    rid, base = _root(tmp_path, "lib")
    fid = _file(_asset("A", root_id=rid), base / "A" / "f.epub", "wrong/f.epub")
    _run()
    got = _mismatches()
    assert list(got) == ["relative_path"] and got["relative_path"]["subject_id"] == fid


def test_mismatch_b_asset_root_is_not_the_containing_root(tmp_path):
    _, base = _root(tmp_path, "lib")
    other_id, _ = _root(tmp_path, "other")
    fid = _file(_asset("B", root_id=other_id), base / "f.epub", "f.epub")
    _run()
    got = _mismatches()
    assert list(got) == ["root_folder"] and got["root_folder"]["subject_id"] == fid


def test_mismatch_c_owned_asset_without_a_live_file(tmp_path):
    rid, base = _root(tmp_path, "lib")
    aid = _asset("C", root_id=rid)
    _file(aid, base / "f.epub", "f.epub", missing=1, write=False)
    _run()
    got = _mismatches()
    assert list(got) == ["owned_no_live_file"] and got["owned_no_live_file"]["subject_id"] == aid
    assert got["owned_no_live_file"]["severity"] == "error"


def test_mismatch_d_unowned_asset_with_a_live_file(tmp_path):
    rid, base = _root(tmp_path, "lib")
    aid = _asset("D", status="none", root_id=rid)
    _file(aid, base / "f.epub", "f.epub")
    _run()
    got = _mismatches()
    assert list(got) == ["live_file_status"] and got["live_file_status"]["subject_id"] == aid


def test_mismatch_e_paths_differing_only_by_case_or_trailing_separator(tmp_path):
    rid, base = _root(tmp_path, "lib")
    aid = _asset("E", root_id=rid)
    first = _file(aid, base / "f.epub", "f.epub")
    _file(aid, str(base / "F.EPUB") + "/", None, write=False)
    _run()
    got = _mismatches()
    assert list(got) == ["duplicate_path"]
    detail = db.json_load(got["duplicate_path"]["detail_json"], {})
    assert len(detail["file_ids"]) == 2 and first in detail["file_ids"]


def test_detectors_do_not_write_to_an_adopted_root(tmp_path):
    _, base, _, _ = _healthy(tmp_path)
    _file(_asset("Gone"), base / "gone.epub", "gone.epub", write=False)

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in [base, *base.rglob("*")])

    before = snapshot()
    result = _run()
    assert result["opened"] > 0
    assert snapshot() == before


def test_stat_budget_truncates_the_run_and_resolves_nothing(tmp_path):
    rid, base = _root(tmp_path, "lib")
    for i in range(3):
        _file(_asset(f"T{i}", root_id=rid), base / f"{i}.epub", f"{i}.epub", missing=1, write=False)
    full = maintenance.run_detector("missing-files")
    assert full["complete"] and full["opened"] == 3
    cut = maintenance.run_detector("missing-files", limit=2)
    assert cut["complete"] is False and cut["auto_resolved"] == 0


def test_query_count_is_bounded_for_500_rows(tmp_path, monkeypatch):
    rid, base = _root(tmp_path, "lib")
    for i in range(500):
        _file(_asset(f"W{i}", root_id=rid), base / f"{i}.epub", f"{i}.epub")
    seen, real = [], db.connect

    def counting(*a, **kw):
        conn = real(*a, **kw)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(db, "connect", counting)
    for name in DETECTORS:
        found, complete = maintenance._collect(maintenance.DETECTORS[name], 10000, 60.0)
        assert complete and found == []
    selects = [s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) <= 8, len(selects)
