"""The managed library build dry run (plan v2): read-only, per-book decisions with reasons, filters for a pilot,
paginated items, SQL summary, expiry and the typed-count confirmation; fast and memory-bounded on 100k files."""
from __future__ import annotations

import os
import time
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services import managedbuild as mb, tagging

import conv_fixtures as cf
import mb_fixtures as F


@pytest.fixture
def lib(tmp_path):
    return F.library(tmp_path)


def test_the_dry_run_writes_nothing_to_any_root(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.audioset(lib, "Big Book", "Ann Author")
    roots = [lib[k]["path"] for k in ("books", "audiobooks", "lib_e", "lib_a")]
    before = F.snapshot(*roots)
    plan = mb.create_plan({})
    assert plan["book_count"] == 2 and plan["status"] == "ready"
    assert F.snapshot(*roots) == before


def test_skip_reasons_missing_unreadable_and_no_managed_root(lib):
    _, _, gone = F.ebook(lib, "Gone", "A")
    gone.unlink()
    _, _, ok = F.ebook(lib, "Fine", "B")
    w, a, tracks = F.audioset(lib, "Partial", "C", n=3)
    tracks[1].unlink()                                   # one track of a set missing: the whole book waits
    plan = mb.create_plan({})
    s = plan["summary"]
    assert plan["book_count"] == 1 and s["skips"]["missing"]["books"] == 2 and s["skips"]["missing"]["count"] == 4
    with db.tx() as c:
        c.execute("UPDATE root_folders SET enabled=0 WHERE id=?", (lib["lib_a"]["id"],))
    tracks[1].write_bytes(b"x")
    s2 = mb.create_plan({})["summary"]
    assert s2["skips"]["no_managed_root"]["books"] == 1


def test_pilot_filters_kind_root_work_author_series_tag_and_limit(lib):
    w1, _, _ = F.ebook(lib, "Dune", "Frank Herbert")
    w2, _, _ = F.ebook(lib, "Emma", "Jane Austen")
    w3, _, _ = F.audioset(lib, "Big Book", "Ann Author")
    author = db.one("SELECT contributor_id FROM work_contributors WHERE work_id=?", (w2,))["contributor_id"]
    sid = db.uid()
    with db.tx() as c:
        c.execute("INSERT INTO series(id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, "Saga", db.now(), db.now()))
        c.execute("INSERT INTO series_memberships(work_id,series_id,position) VALUES (?,?,'1')", (w3, sid))
    tag = tagging.kids_tag_id()
    tagging.add_tag(w1, tag)

    def works(include):
        p = mb.create_plan({"include": include})
        return {i["work_id"] for i in mb.list_items(p["plan_id"], limit=500)["items"] if i["action"] != "skip"}
    assert works({"kinds": ["audiobook"]}) == {w3}
    assert works({"root_ids": [lib["books"]["id"]]}) == {w1, w2}
    assert works({"work_ids": [w2]}) == {w2}
    assert works({"author_ids": [author]}) == {w2}
    assert works({"series_ids": [sid]}) == {w3}
    assert works({"tag_ids": [tag]}) == {w1}
    assert len(works({"limit_books": 2})) == 2


def test_observe_roots_are_only_included_when_chosen(lib, tmp_path):
    obs = cf.root(tmp_path, "observe", "ebook", "legacy")
    p = obs["path"] / "Old.epub"
    p.write_bytes(b"old" * 10)
    cf.add_book(obs, "Old", "O", "ebook", [p])
    assert mb.create_plan({})["book_count"] == 0
    plan = mb.create_plan({"include": {"root_ids": [obs["id"]]}})
    assert plan["book_count"] == 1 and any("observe" in w for w in plan["summary"]["warnings"])


def test_items_are_paginated_and_filterable(lib):
    for i in range(30):
        F.ebook(lib, f"Book {i:02d}", "Author")
    F.audioset(lib, "Set", "Author", n=4)
    plan = mb.create_plan({})
    pid = plan["plan_id"]
    page = mb.list_items(pid, limit=10, offset=0)
    assert page["total"] == 34 and len(page["items"]) == 10
    assert mb.list_items(pid, limit=10, offset=30)["items"][-1]["id"] == page["items"][0]["id"] + 33
    assert mb.list_items(pid, kind="audiobook")["total"] == 4
    assert mb.list_items(pid, q="Book 0")["total"] == 10
    assert mb.list_items(pid, limit=100000)["limit"] == mb.MAX_PAGE


def test_summary_space_destinations_and_time_estimate(lib, monkeypatch):
    F.ebook(lib, "Dune", "Frank Herbert", data=b"x" * 10_000_000)
    plan = mb.create_plan({"throttle_mb_s": 1, "verify": "size"})
    s = plan["summary"]
    d = {x["name"]: x for x in s["destinations"]}["library-ebooks"]
    assert d["required_bytes"] == 10_000_000 and d["margin_bytes"] >= mb.SPACE_MARGIN_MIN_BYTES and d["fits"] is True
    assert d["publish"] == "unprobed"                         # the dry run never writes a probe file
    assert 9 <= s["estimated_seconds"] <= 10                  # 10 MB at 1 MiB/s
    monkeypatch.setattr(mb.shutil, "disk_usage", lambda p: __import__("shutil")._ntuple_diskusage(1, 1, 1))
    s2 = mb.create_plan({})["summary"]
    assert not s2["destinations"][0]["fits"] and any("free space" in w for w in s2["warnings"])


def test_confirmation_needs_the_typed_book_count_and_only_the_latest_unexpired_plan(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    F.ebook(lib, "Emma", "Jane Austen")
    old = mb.create_plan({})
    new = mb.create_plan({})
    with pytest.raises(mb.BuildRefused) as e1:
        mb.confirm_plan(old["plan_id"], 2)
    assert e1.value.code == "not_latest"
    for bad in (None, 1, True, "2"):
        with pytest.raises(mb.BuildRefused) as e2:
            mb.confirm_plan(new["plan_id"], bad)
        assert e2.value.code == "confirmation_required"
    with db.tx() as c:
        c.execute("UPDATE build_plans SET expires_at=? WHERE id=?", (db.now() - 1, new["plan_id"]))
    with pytest.raises(mb.BuildRefused) as e3:
        mb.confirm_plan(new["plan_id"], 2)
    assert e3.value.code == "expired"
    fresh = mb.create_plan({})
    assert mb.confirm_plan(fresh["plan_id"], 2)["confirmed"] is True


def test_one_build_at_a_time(lib):
    F.ebook(lib, "Dune", "Frank Herbert")
    p1 = F.plan_and_confirm()
    run = mb.start(p1["plan_id"], submit=False)
    mb.pause(run["run_id"])
    mb.execute(run["run_id"])                      # stops at once: paused
    F.ebook(lib, "Emma", "Jane Austen")
    p2 = F.plan_and_confirm()
    with pytest.raises(mb.BuildRefused) as exc:
        mb.start(p2["plan_id"], submit=False)
    assert exc.value.code == "busy"


# tracemalloc slows the 100k-row plan several-fold and CI runs four xdist workers per runner VM, so this test needs
# more than the suite-wide `--timeout 120`; the memory bound below is the real contract, the wall clock is a sanity cap.
@pytest.mark.timeout(600)
def test_a_100k_file_plan_is_fast_paginated_and_memory_bounded(lib):
    """100,000 file rows (1,000 audiobook sets of 100 tracks; the files are not on disk, so every set is skipped as
    missing, which still exercises the lstat + decision + insert path for every file)."""
    t = db.now()
    base = str(lib["audiobooks"]["path"])
    with db.tx() as c:
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES ('c1','Bulk Author','Bulk Author',?,?)", (t, t))
        works, assets, files, wc = [], [], [], []
        for i in range(1000):
            wid, aid = f"w{i:05d}", f"a{i:05d}"
            works.append((wid, f"Book {i:05d}", f"Book {i:05d}", "resolved", t, t))
            wc.append((wid, "c1"))
            assets.append((aid, wid, t, t))
            for j in range(100):
                files.append((f"f{i:05d}-{j:03d}", aid, f"{base}/Bulk Author/Book {i:05d}/{j:03d}.mp3", j + 1, 1000, t))
        c.executemany("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,?,?,?)", works)
        c.executemany("INSERT INTO work_contributors(work_id,contributor_id,role,ord) VALUES (?,?,'author',0)", wc)
        c.executemany("INSERT INTO media_assets(id,work_id,kind,format,status,added_at,updated_at) VALUES (?,?,'audiobook','mp3','owned',?,?)", assets)
        c.executemany("INSERT INTO media_files(id,asset_id,path,part_number,size_bytes,adopted,managed,missing,discovered_at) VALUES (?,?,?,?,?,1,0,0,?)", files)
    tracemalloc.start()
    t0 = time.monotonic()
    plan = mb.create_plan({})
    took = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert plan["summary"]["items"] == 100_000 and plan["summary"]["skips"]["missing"]["books"] == 1000
    assert took < 400, took
    assert peak < 400 * 1024 * 1024, peak
    t1 = time.monotonic()
    page = mb.list_items(plan["plan_id"], limit=50, offset=99_950)
    again = mb.summarize(plan["plan_id"])
    assert len(page["items"]) == 50 and page["total"] == 100_000 and again["items"] == 100_000
    assert time.monotonic() - t1 < 5


def test_legacy_v1_endpoints_still_answer_and_the_unsafe_run_loop_is_gone(lib):
    _, _, e = F.ebook(lib, "Dune", "Frank Herbert")
    with TestClient(main_module.app) as c:
        d = c.post("/api/v1/jobs/managed-library-build/dry-run").json()
        assert d["transfer_mode"] == "copy" and d["totals"]["file_count"] == 1 and d["entries"][0]["source_path"] == str(e)
        assert c.get("/api/v1/jobs/managed-library-build/plan").json()["plan_id"] == d["plan_id"]
        assert c.post("/api/v1/jobs/managed-library-build/confirm", json={"plan_id": d["plan_id"]}).status_code == 409   # no typed count
        assert c.post("/api/v1/jobs/managed-library-build/confirm", json={"plan_id": d["plan_id"], "book_count": 1}).json()["confirmed"]
        r = c.post("/api/v1/jobs/managed-library-build/run", json={"plan_id": d["plan_id"]})
        assert r.status_code == 200 and "copied" not in r.json()          # the engine runs; no inline copy loop
        end = time.time() + 30
        while c.get(f"/api/v1/managed-build/runs/{r.json()['run_id']}").json()["status"] == "running" and time.time() < end:
            time.sleep(0.05)
    assert os.path.exists(e) and len(F.managed_files(lib)) == 1
    src = open(main_module.__file__).read()
    assert "shutil.copy2(entry" not in src and "developer_mode" not in src.split("managed-build")[1][:20000]
