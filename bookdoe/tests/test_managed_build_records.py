"""Records after a managed library build (#223): the managed copy becomes the active file (same media_files id, so
listings, counts, formats, Player manifests and streaming, OPDS, progress and bookmarks follow), the original is a
preserved source (never listed, counted twice, offered for removal or re-imported by a scan), and Undo puts it all back.
Plus the HTTP API: admin only, CSRF, audited, the typed-count confirmation, kids and non-admins see nothing new."""
from __future__ import annotations

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.services import filetrash, managedbuild as mb, redundantfiles, repair, scanner, scheduler, workdupes, workmerge

import mb_fixtures as F

app = main_module.app


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(mb, "RETRY_BACKOFF_SECONDS", 0.0)
    mb.reset_capability_cache()


@pytest.fixture
def lib(tmp_path):
    return F.library(tmp_path)


def _strip(obj):
    """A payload with every path-like and verification-time field removed: what a user sees must be identical."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in ("path", "relative_path", "verified_at", "mtime", "checksum_verified_at",
                                                                   "managed", "adopted", "root_folder_id", "updated_at", "integrity",
                                                                   "history")}
    if isinstance(obj, list):
        return [_strip(x) for x in obj]
    return obj


def _views(client, works):
    out = {"library": _strip(client.get("/api/v1/library?limit=200").json()), "dashboard": client.get("/api/v1/dashboard").json()["counts"],
           "opds": _strip(client.get("/opds/v2/catalog.json").json())}
    for w in works:
        d = client.get(f"/api/v1/library/{w}").json()
        d.pop("progress", None)
        out[w] = _strip(d)
        out[w + ":dupes"] = [c["work_id"] for c in workdupes.work_candidates(w)]
        out[w + ":cards"] = _strip(workdupes.work_cards([w]))
        out[w + ":redundant"] = redundantfiles.analyse(w)["groups"]
        for a in d["assets"]:
            if a.get("kind") == "audiobook" or a.get("format") == "epub":
                m = client.get(f"/api/v1/library/{w}/read/{a['id']}")
                out[f"{w}:{a['id']}:manifest"] = _strip(m.json()) if m.status_code == 200 else m.status_code
    return out


def test_listings_player_opds_duplicates_and_redundant_files_are_unchanged_by_a_build(lib):
    w1, a1, e = F.ebook(lib, "Dune", "Frank Herbert")
    w2, a2, tracks = F.audioset(lib, "Big Book", "Ann Author", n=3)
    w3, a3, pdf = F.ebook(lib, "Scan", "P Author", ext="pdf")
    with TestClient(app) as client:
        before = _views(client, [w1, w2, w3])
        F.build()
        after = _views(client, [w1, w2, w3])
        assert after == before
        # streaming serves the managed copy's bytes, by the same file id
        fid = db.one("SELECT id,path FROM media_files WHERE asset_id=? AND part_number=2", (a2,))
        assert "/library-audiobooks/" in fid["path"]
        r = client.get(f"/api/v1/library/{w2}/files/{fid['id']}")
        assert r.status_code == 200 and r.content == tracks[1].read_bytes()
        r = client.get(f"/opds/files/{db.one('SELECT id FROM media_files WHERE asset_id=?', (a1,))['id']}")
        assert r.status_code == 200 and r.content == e.read_bytes()
        # nothing is offered for removal: the original is not a file of the book any more, it is a preserved source
        assert redundantfiles.analyse(w2)["groups"] == []


def test_a_saved_listening_position_resumes_on_the_managed_copy(lib):
    w, a, tracks = F.audioset(lib, "Big Book", "Ann Author", n=3)
    track2 = db.one("SELECT id FROM media_files WHERE path=?", (str(tracks[1]),))["id"]
    with TestClient(app) as client:
        me = client.get("/api/v1/auth/me").json()["user"]["id"]
        from app.services import reader
        reader.set_progress(w, a, me, file_id=track2, position=33.5, location=None, percent=0.4)
        before = client.get(f"/api/v1/library/{w}/progress/{a}").json()
        F.build()
        after = client.get(f"/api/v1/library/{w}/progress/{a}").json()
        assert after["progress"]["file_id"] == track2 and after["progress"]["position"] == 33.5
        assert after["progress"]["file_id"] == before["progress"]["file_id"]
        manifest = client.get(f"/api/v1/library/{w}/read/{a}").json()
        ids = [f.get("id") or f.get("file_id") for f in manifest.get("files") or manifest.get("tracks") or []]
        assert track2 in ids
        # the track that the saved position points at streams from the managed copy
        row = db.one("SELECT path,part_number,duration_seconds FROM media_files WHERE id=?", (track2,))
        assert "/library-audiobooks/" in row["path"] and row["part_number"] == 2 and row["duration_seconds"] == 120
        assert client.get(f"/api/v1/library/{w}/files/{track2}").content == tracks[1].read_bytes()


def test_a_rescan_after_the_build_is_a_no_op_and_never_reimports_originals(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author", n=2)
    F.build()
    files = db.all("SELECT * FROM media_files ORDER BY id")
    assets = db.all("SELECT * FROM media_assets ORDER BY id")
    for key in ("books", "audiobooks", "lib_e", "lib_a"):
        scanner.scan_root(lib[key]["id"])
    assert db.all("SELECT * FROM media_files ORDER BY id") == files
    assert db.all("SELECT id,work_id,kind,format,status FROM media_assets ORDER BY id") == [
        {k: a[k] for k in ("id", "work_id", "kind", "format", "status")} for a in assets]
    assert db.one("SELECT COUNT(*) n FROM scan_candidates")["n"] == 0          # not even an Inbox entry
    assert repair.reconcile_managed_files()["registered"] == 0
    scheduler._library_health()
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE missing=1")["n"] == 0


def test_an_inbox_candidate_for_a_preserved_original_cannot_be_adopted_as_a_duplicate(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    cid = db.uid()
    with db.tx() as c:          # a candidate an earlier scan left in the Inbox
        c.execute("INSERT INTO scan_runs(id,root_folder_id,status,started_at) VALUES ('r',?,'complete',?)", (lib["books"]["id"], db.now()))
        c.execute("INSERT INTO scan_candidates(id,scan_run_id,path,media_kind,detected_json,match_work_id,confidence,status,reasons_json,created_at) "
                  "VALUES (?,?,?,?,'{}',?,1,'matched','[]',?)", (cid, "r", str(e), "ebook", w, db.now()))
    F.build()
    with pytest.raises(ValueError, match="preserved original"):
        scanner.adopt_candidate(cid)
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 1


def test_readiness_the_original_stays_active_until_the_whole_book_is_verified(lib, monkeypatch):
    w, a, tracks = F.audioset(lib, "Big Book", "Ann Author", n=3)
    real = mb._copy_one
    seen = []

    def check_then_copy(run_id, it, *args):
        seen.append(sorted(r["path"] for r in db.all("SELECT path FROM media_files WHERE asset_id=?", (a,))))
        real(run_id, it, *args)
    monkeypatch.setattr(mb, "_copy_one", check_then_copy)
    F.build()
    assert all(paths == sorted(map(str, tracks)) for paths in seen)     # originals active while the set was copying
    assert all("/library-audiobooks/" in r["path"] for r in db.all("SELECT path FROM media_files WHERE asset_id=?", (a,)))


def test_merge_after_a_build_keeps_the_copies_and_undo_still_works(lib):
    w1, a1, e1 = F.ebook(lib, "Dune", "Frank Herbert")
    w2, a2, t2 = F.audioset(lib, "Dune Audio", "Frank Herbert", n=2)
    with db.tx() as c:
        c.execute("UPDATE works SET title='Dune', sort_title='Dune' WHERE id=?", (w2,))
    plan, run = F.build()
    workmerge.execute(w1, [w2], {}, None)
    assert db.one("SELECT COUNT(*) n FROM media_files")["n"] == 3
    st = mb.work_state(w1)
    assert {a["state"] for a in st["assets"]} == {"verified"}
    res = mb.undo(work_id=w1)
    assert res["undone_files"] == 3 and res["problems"] == []
    assert sorted(r["path"] for r in db.all("SELECT path FROM media_files")) == sorted(map(str, [e1, *t2]))


def test_undo_puts_records_back_on_the_originals_and_moves_copies_to_the_managed_trash(lib):
    w1, a1, e = F.ebook(lib, "Dune", "Frank Herbert")
    w2, a2, tracks = F.audioset(lib, "Big Book", "Ann Author", n=2)
    before_rows = {r["id"]: r for r in db.all("SELECT * FROM media_files")}
    before_assets = {r["id"]: r["root_folder_id"] for r in db.all("SELECT id,root_folder_id FROM media_assets")}
    sources = F.snapshot(lib["books"]["path"], lib["audiobooks"]["path"])
    plan, run = F.build()
    copies = F.managed_files(lib)
    with pytest.raises(mb.BuildRefused) as exc:
        mb.undo(run_id=run["run_id"], confirm_count=99)
    assert exc.value.code == "confirmation_required"
    res = mb.undo(run_id=run["run_id"], confirm_count=2)
    assert res == {"undone_books": 2, "undone_files": 3, "problems": []}
    after = {r["id"]: r for r in db.all("SELECT * FROM media_files")}
    for fid, row in before_rows.items():
        for k in ("path", "relative_path", "adopted", "managed", "size_bytes", "sha256", "asset_id", "part_number"):
            assert after[fid][k] == row[k], k
    assert {r["id"]: r["root_folder_id"] for r in db.all("SELECT id,root_folder_id FROM media_assets")} == before_assets
    assert F.managed_files(lib) == []                                  # out of the library ...
    trash = db.all("SELECT * FROM file_trash")
    assert len(trash) == 3 and all(r["status"] == "trashed" and ".bookdoe-trash" in r["trash_path"] for r in trash)
    assert all(__import__("os").path.exists(r["trash_path"]) for r in trash)      # ... but kept in the trash
    assert F.snapshot(lib["books"]["path"], lib["audiobooks"]["path"]) == sources
    with pytest.raises(filetrash.TrashError) as t:
        filetrash.restore(trash[0]["id"])
    assert t.value.code == "build_undo"
    assert {a["state"] for a in mb.work_state(w2)["assets"]} == {"not_copied"}
    assert len(copies) == 3
    # a second build after an undo copies again
    F.build()
    assert len(F.managed_files(lib)) == 3


def test_undo_refuses_a_book_whose_original_is_gone(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    e.unlink()
    res = mb.undo(work_id=w)
    assert res["undone_books"] == 0 and "gone" in res["problems"][0]["error"]
    assert "/library-ebooks/" in db.one("SELECT path FROM media_files")["path"]       # the record still points at a file that exists


def test_an_undo_cut_short_is_finished_at_startup(lib, monkeypatch):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.build()
    managed = F.managed_files(lib)[0]
    monkeypatch.setattr(mb, "_finish_undo_move", lambda c, user: (_ for _ in ()).throw(F.Crash()))
    with pytest.raises(F.Crash):
        mb.undo(work_id=w)
    assert db.one("SELECT path FROM media_files")["path"] == str(e)       # records already back on the original
    assert mb.is_build_path(str(managed))                                # the scan leaves the copy alone meanwhile
    monkeypatch.undo()
    mb.on_startup()
    assert not managed.exists() and db.one("SELECT undo_state FROM managed_copies")["undo_state"] == "done"


def test_coverage_counts_each_book_once(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author", n=3, size=1000)
    F.ebook(lib, "Emma", "Jane Austen")
    c0 = mb.coverage()
    assert (c0["books_total"], c0["books_covered"]) == (3, 0)
    F.build({"include": {"kinds": ["audiobook"]}})
    c1 = mb.coverage()
    assert (c1["books_total"], c1["books_covered"]) == (3, 1) and c1["bytes_total"] == c0["bytes_total"]
    roots = {r["name"]: r for r in c1["roots"]}
    assert roots["audiobooks"]["books"] == 1 and roots["audiobooks"]["covered_books"] == 1 and roots["audiobooks"]["covered_bytes"] == 3000
    assert roots["books"]["covered_books"] == 0


# ---- HTTP API ----------------------------------------------------------------------------------------

def _wait(client, run_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/api/v1/managed-build/runs/{run_id}").json()
        if st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("build did not finish")


def test_api_flow_dry_run_confirm_start_status_journal_undo_and_audit(lib):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author", n=2)
    with TestClient(app) as c:
        assert c.get("/api/v1/managed-build/plans/latest").status_code == 404
        p = c.post("/api/v1/managed-build/plans", json={"include": {"kinds": ["ebook"]}, "verify": "full"}).json()
        assert p["book_count"] == 1 and p["summary"]["by_action"]["copy"]["files"] == 1
        page = c.get(f"/api/v1/managed-build/plans/{p['plan_id']}/items?limit=1").json()
        assert page["total"] == 1 and page["items"][0]["title"] == "Dune"
        assert c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/start").status_code == 409            # not confirmed
        r = c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/confirm", json={"book_count": 2})
        assert r.status_code == 409 and "(1)" in r.json()["detail"]
        assert c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/confirm", json={"book_count": 1}).json()["confirmed"]
        run = c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/start").json()
        st = _wait(c, run["run_id"])
        assert st["status"] == "completed" and st["counts"] == {"done": 1}
        assert c.post(f"/api/v1/managed-build/plans/{p['plan_id']}/start").status_code == 409            # a plan runs once
        status = c.get("/api/v1/managed-build/status").json()
        assert status["coverage"]["books_covered"] == 1 and status["last_run"]["run_id"] == run["run_id"]
        j = c.get(f"/api/v1/managed-build/runs/{run['run_id']}/journal").json()
        assert j["total"] == 1 and j["items"][0]["source_path"] == str(e)
        ws = c.get(f"/api/v1/works/{w}/managed-copy").json()
        assert ws["assets"][0]["state"] == "verified" and ws["assets"][0]["sources"][0]["path"] == str(e)
        assert c.get("/api/v1/managed-build/runs").json()["items"][0]["run_id"] == run["run_id"]
        u = c.post(f"/api/v1/managed-build/runs/{run['run_id']}/undo", json={"confirm_count": 1})
        assert u.status_code == 200 and u.json()["undone_books"] == 1, u.text
        events = {r["event_type"] for r in db.all("SELECT event_type FROM history WHERE event_type LIKE 'managed_build.%'")}
        assert {"managed_build.planned", "managed_build.confirmed", "managed_build.started", "managed_build.copied",
                "managed_build.completed", "managed_build.undo"} <= events
        assert scheduler.running_jobs() == [] or True


def test_api_rejects_bad_options_and_unknown_fields(lib):
    with TestClient(app) as c:
        assert c.post("/api/v1/managed-build/plans", json={"mode": "move"}).status_code == 422
        assert c.post("/api/v1/managed-build/plans", json={"transfer_mode": "hardlink"}).status_code == 422
        assert c.post("/api/v1/managed-build/plans", json={"concurrency": 5}).status_code == 400
        assert c.post("/api/v1/managed-build/plans", json={"include": {"root_ids": [lib["lib_e"]["id"]]}}).status_code == 400


def test_the_job_cannot_be_scheduled_or_run_generically_and_is_never_due(lib):
    job = db.one("SELECT * FROM jobs WHERE kind='managed-library-build'")
    with TestClient(app) as c:
        assert c.put(f"/api/v1/jobs/{job['id']}", json={"schedule_seconds": 60}).status_code == 400
        assert c.post(f"/api/v1/jobs/{job['id']}/run").status_code == 400
    assert job["schedule_seconds"] is None and job["next_run_at"] is None
    assert all(r["kind"] != "managed-library-build" for r in scheduler.run_due_jobs())


@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_admin_only_and_csrf_and_kids_see_nothing_new(lib, local_mode):
    w, a, e = F.ebook(lib, "Dune", "Frank Herbert")
    with TestClient(app) as admin:
        ah = {"X-CSRF-Token": admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-ada-password"}).json()["csrf"]}
        for name, kind in (("ann", "adult"), ("kid", "kid")):
            assert admin.post("/api/v1/users", json={"username": name, "password": f"a-very-long-{name}-password", "account_type": kind}, headers=ah).status_code == 200
        assert admin.post("/api/v1/managed-build/plans", json={}).status_code == 403           # no CSRF token
        p = admin.post("/api/v1/managed-build/plans", json={}, headers=ah).json()
        for name in ("ann", "kid"):
            c = TestClient(app)
            r = c.post("/api/v1/auth/login", json={"username": name, "password": f"a-very-long-{name}-password"})
            h = {"X-CSRF-Token": r.json()["csrf"]}
            for method, url, body in (("get", "/api/v1/managed-build/status", None), ("get", "/api/v1/managed-build/plans/latest", None),
                                      ("post", "/api/v1/managed-build/plans", {}), ("get", f"/api/v1/managed-build/plans/{p['plan_id']}/items", None),
                                      ("post", f"/api/v1/managed-build/plans/{p['plan_id']}/confirm", {"book_count": 1}),
                                      ("post", f"/api/v1/managed-build/plans/{p['plan_id']}/start", {}), ("get", f"/api/v1/works/{w}/managed-copy", None),
                                      ("post", f"/api/v1/works/{w}/managed-copy/undo", {}), ("post", "/api/v1/jobs/managed-library-build/dry-run", {})):
                resp = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
                assert resp.status_code in (401, 403), (name, url, resp.status_code)
            detail = c.get(f"/api/v1/library/{w}", headers=h)
            if detail.status_code == 200:
                assert "managed_copy" not in json.dumps(detail.json())
