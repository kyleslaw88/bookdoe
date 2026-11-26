"""Library-wide duplicate finder: candidates on realistic sets, ranking, refresh-conflict harvest, dismissals,
incremental and resumable scans, permissions (the owner's file-only vs provider-backed pairs)."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app import main as main_module
from app.config import settings as base_settings
from app.main import app
from app.services import dupefinder, metadata, scheduler

from test_work_merge import _asset, _series, _work

DESC = "A long enough description of the book from the provider so that it counts as a real description."


def provider(title, author, mid, **cols):
    """A provider-backed Work: metadata_id, description, cover, no owned files."""
    return _work(title, author, metadata_id=mid, metadata_source="provider", description=DESC, cover_url="https://img/c.jpg",
                 first_publish_date="2000-01-01", **cols)


def fileonly(title, author, ext="epub", **cols):
    """A Work created from files: no provider record, owned files, 'From file'."""
    w = _work(title, author, metadata_source="local", **cols)
    _asset(w, "ebook" if ext in ("epub", "pdf", "mobi") else "audiobook", ext, [f"/lib/{w}.{ext}"])
    return w


def identify(work_id, ns, value, edition=False):
    """An identifier on the Work, or (``edition``) on one of its editions: the same ISBN can sit on a Work and on another's edition."""
    with db.tx() as conn:
        if edition:
            eid = db.uid()
            conn.execute("INSERT INTO editions(id,work_id,title,created_at,updated_at) VALUES (?,?,?,1,1)", (eid, work_id, "Ed"))
            conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('edition',?,?,?)", (eid, ns, value))
        else:
            conn.execute("INSERT INTO identifiers(entity_type,entity_id,namespace,value) VALUES ('work',?,?,?)", (work_id, ns, value))


def groups(**kw):
    return dupefinder.list_groups(with_cards=False, limit=200, **kw)["items"]


def scan(**kw):
    out = dupefinder.scan(**kw)
    assert out["status"] == "done", out
    return out


def pair_of(*ids):
    return sorted(ids)


def find(*ids, **kw):
    return next((g for g in groups(**kw) if sorted(g["work_ids"]) == sorted(ids)), None)


# ---- detection on realistic sets -----------------------------------------------------------------------

def test_39_clues_pair_is_file_vs_provider_high_and_the_provider_record_is_recommended():
    file_w = fileonly("The Black Circle (The 39 Clues, #5)", "Patrick Carman", "m4b")
    prov = provider("The Black Circle", "Patrick Carman", "mid-circle")
    _series(prov, "The 39 Clues", "5")
    other = provider("The Sword Thief", "Rick Riordan", "mid-sword")
    _series(other, "The 39 Clues", "3")
    scan()
    g = find(file_w, prov)
    assert g and g["confidence"] == "high" and g["kind"] == "file_vs_provider"
    assert {"file_only_vs_provider", "same_authors"} <= set(g["reasons"])
    assert g["canonical_work_id"] == prov and "provider record" in g["canonical_reason"] and g["has_owned"]
    assert len(groups()) == 1                 # the Sword Thief is a different book by a different author


def test_because_of_winn_dixie_file_only_vs_provider_backed_with_full_cards():
    prov = provider("Because of Winn-Dixie", "Kate DiCamillo", "mid-winn")
    identify(prov, "isbn", "9780763644734")
    file_w = fileonly("Because of Winn-Dixie", "Kate DiCamillo", "epub")
    scan()
    g = dupefinder.list_groups()["items"][0]
    assert g["confidence"] == "high" and g["canonical_work_id"] == prov
    m = {x["id"]: x for x in g["members"]}
    assert [x["id"] for x in g["members"]][0] == prov                      # recommended first
    assert m[file_w]["source_label"] == "From file" and m[prov]["source_label"] == "Provider record"
    assert m[file_w]["assets"][0]["format"] == "epub" and m[file_w]["assets"][0]["files"] == 1 and m[prov]["assets"] == [] or not m[prov]["assets"][0]["owned"]
    assert m[prov]["quality"]["score"] > m[file_w]["quality"]["score"] and "description" in m[prov]["quality"]["has"] and "description" in m[file_w]["quality"]["lacks"]


def test_same_title_different_authors_is_not_a_pair():
    provider("The Ghost", "Robert Harris", "mid-g1")
    fileonly("The Ghost", "Some Other Writer", "epub")
    scan()
    assert groups() == []


def test_series_volumes_are_not_pairs():
    a = fileonly("Dune (Book 1)", "Frank Herbert")
    b = provider("Dune (Book 10)", "Frank Herbert", "mid-d10")
    c = fileonly("Foundation: Book 1", "Isaac Asimov")
    d = provider("Foundation: Book 10", "Isaac Asimov", "mid-f10")
    scan()
    assert groups() == [] and a and b and c and d


def test_no_transitive_grouping():
    # B has no author: compatible with A and with C, but A (Smith) and C (Jones) are different books.
    a = fileonly("Shared Title", "Ann Smith")
    b = fileonly("Shared Title", "Ann Smith")
    c = provider("Shared Title", "Bob Jones", "mid-st")
    scan()
    assert find(a, b) and not find(a, b, c) and not any(c in g["work_ids"] for g in groups())


def test_a_clique_is_one_group_and_a_chain_is_listed_as_pairs():
    a = fileonly("Triple", "Ann Smith")
    b = fileonly("Triple", "Ann Smith")
    c = provider("Triple", "Ann Smith", "mid-triple")
    scan()
    g = find(a, b, c)
    assert g and g["canonical_work_id"] == c and g["confidence"] == "high"
    # chain: X(no author)~Y(Ann), X~Z(Bob), Y!~Z -> pairs only
    x = fileonly("Chain Book", "Placeholder")
    with db.tx() as conn:
        conn.execute("DELETE FROM work_contributors WHERE work_id=?", (x,))
    y = fileonly("Chain Book", "Ann Smith")
    z = fileonly("Chain Book", "Bob Jones")
    scan(full=True)
    assert find(x, y) and find(x, z) and not find(y, z) and not find(x, y, z)


def test_provider_clusters_need_identifier_overlap_for_high():
    p1 = provider("Same Edition Twice", "Jane Doe", "mid-e1")
    p2 = provider("Same Edition Twice", "Jane Doe", "mid-e2")
    scan()
    assert find(p1, p2)["confidence"] == "medium" and find(p1, p2)["kind"] == "title_author"
    identify(p1, "isbn", "9781234567897")
    identify(p2, "isbn", "9781234567897", edition=True)
    scan(full=True)                      # identifiers carry no timestamp: an identifier-only edit is picked up by a full scan
    g = find(p1, p2)
    assert g["confidence"] == "high" and g["kind"] == "identifier" and "shared_identifier" in g["reasons"]


def test_titles_that_differ_are_grouped_only_through_an_identifier_and_flagged():
    a = fileonly("Totally Different Title", "Jane Doe")
    b = provider("Another Name Entirely", "Jane Doe", "mid-diff")
    identify(a, "isbn", "9780000000002")
    identify(b, "isbn", "9780000000002", edition=True)
    scan()
    g = find(a, b)
    assert g and g["kind"] == "identifier" and g["confidence"] == "medium" and "titles_differ" in g["reasons"]


def test_unknown_authors_on_both_sides_is_low_and_unknown_on_the_file_side_is_medium():
    a = fileonly("Mystery Title", "Placeholder")
    b = fileonly("Mystery Title", "Placeholder")
    p = provider("Another Mystery", "Placeholder", "mid-am")
    q = fileonly("Another Mystery", "Placeholder")
    with db.tx() as conn:
        for w in (a, b, q):
            conn.execute("DELETE FROM work_contributors WHERE work_id=?", (w,))
    scan()
    assert find(a, b)["confidence"] == "low"
    assert find(p, q)["confidence"] == "medium" and "authors_unknown" in find(p, q)["reasons"]


# ---- refresh conflicts ------------------------------------------------------------------------------------------

class FakeProvider:
    """metadata.resolve stand-in: titles map to provider ids; unknown titles do not match."""

    def __init__(self, table, rate_limit_after=None):
        self.table, self.calls, self.rate_limit_after = table, [], rate_limit_after

    def __call__(self, identifiers=None, title=None, authors=None, **kw):
        self.calls.append(title)
        mid = self.table.get(title)
        return {"match": {"id": mid} if mid else None, "confidence": 0.97 if mid else 0, "method": "fake", "candidates": []}


def test_a_refresh_that_hits_the_title_conflict_records_it_and_makes_a_top_candidate(monkeypatch):
    prov = provider("Because of Winn-Dixie", "Kate DiCamillo", "mid-winn")
    broken = fileonly("Because of Winn Dixie (Kate DiCamillo)", "Kate DiCamillo")
    monkeypatch.setattr(metadata, "resolve", FakeProvider({"Because of Winn Dixie (Kate DiCamillo)": "mid-winn"}))
    with pytest.raises(ValueError, match="already linked"):
        metadata.refresh_work(broken)
    row = db.one("SELECT * FROM work_refresh_conflicts WHERE work_id=?", (broken,))
    assert row["conflict_work_id"] == prov and row["metadata_id"] == "mid-winn" and row["resolved_at"] is None
    scan()
    g = find(broken, prov)
    assert g["kind"] == "refresh_conflict" and g["confidence"] == "high" and "refresh_conflict" in g["reasons"]
    assert g["canonical_work_id"] == prov
    assert dupefinder.status()["open_conflicts"] == 1


def test_harvest_retries_file_only_works_in_bulk_rate_limited_and_resumable(monkeypatch):
    prov = provider("Holes", "Louis Sachar", "mid-holes")
    works = {n: fileonly(n, "Louis Sachar") for n in ("Holes", "Stargirl", "Hoot", "Loser")}
    fake = FakeProvider({"Holes": "mid-holes"})
    monkeypatch.setattr(metadata, "resolve", fake)
    sleeps = []
    out = dupefinder.harvest(limit=2, delay=1.5, sleep=sleeps.append)
    assert out["tried"] == 2 and len(fake.calls) == 2 and sleeps == [1.5]                  # rate limited between calls, capped per run
    out2 = dupefinder.harvest(limit=10, delay=0, sleep=sleeps.append)
    assert out["tried"] + out2["tried"] == 4 and out2["wrapped"] is True                    # the cursor walks the whole library
    assert sorted(fake.calls) == sorted(works) and out["conflicts"] + out2["conflicts"] == 1
    assert db.one("SELECT conflict_work_id FROM work_refresh_conflicts WHERE work_id=?", (works["Holes"],))["conflict_work_id"] == prov
    assert db.get_setting(dupefinder.HARVEST_CURSOR_KEY) == ""                              # wrapped: next run starts over


def test_harvest_stops_when_the_provider_throttles_and_does_not_skip_that_work(monkeypatch):
    ws = sorted(fileonly(n, "Writer") for n in ("A One", "B Two", "C Three"))
    seen = []

    def throttled(work_id, **kw):
        seen.append(work_id)
        return {"work_id": work_id, "status": "rate_limited", "retry_at": db.now() + 60}
    out = dupefinder.harvest(limit=10, delay=0, sleep=lambda s: None, lookup=throttled)
    assert out["stopped"] == "rate_limited" and len(seen) == 1 and out["tried"] == 0
    assert db.get_setting(dupefinder.HARVEST_CURSOR_KEY) in ("", None)                  # cursor did NOT move past the throttled work
    seen.clear()
    ok = dupefinder.harvest(limit=10, delay=0, sleep=lambda s: None, lookup=lambda w, **k: seen.append(w) or {"status": "unmatched"})
    assert seen == ws and ok["tried"] == 3                                                # the throttled one is retried first


def test_harvest_only_records_conflicts_and_never_links_or_rewrites_metadata(monkeypatch):
    prov = provider("Holes", "Louis Sachar", "mid-holes")
    held = fileonly("Holes", "Louis Sachar")
    free = fileonly("Hoot", "Carl Hiaasen", description="my own description")
    monkeypatch.setattr(metadata, "resolve", FakeProvider({"Holes": "mid-holes", "Hoot": "mid-hoot"}))
    monkeypatch.setattr(metadata, "link_work", lambda *a, **k: pytest.fail("harvest must not link"))
    monkeypatch.setattr(metadata, "refresh_work", lambda *a, **k: pytest.fail("harvest must not refresh"))
    monkeypatch.setattr(metadata, "upsert_work", lambda *a, **k: pytest.fail("harvest must not write metadata"))
    before = db.all("SELECT id,title,description,metadata_id,metadata_source,updated_at FROM works ORDER BY id")
    out = dupefinder.harvest(limit=10, delay=0, sleep=lambda s: None)
    assert out["conflicts"] == 1 and out["linkable"] == 1 and out["tried"] == 2
    assert db.all("SELECT id,title,description,metadata_id,metadata_source,updated_at FROM works ORDER BY id") == before
    assert db.one("SELECT conflict_work_id FROM work_refresh_conflicts WHERE work_id=?", (held,))["conflict_work_id"] == prov
    assert not db.one("SELECT 1 x FROM work_refresh_conflicts WHERE work_id=?", (free,))


def test_harvest_ignores_hand_edited_and_linked_works(monkeypatch):
    manual = _work("Manual Book", "Writer", metadata_source="manual")
    linked = provider("Linked Book", "Writer", "mid-l")
    plain = fileonly("Plain Book", "Writer")
    fake = FakeProvider({})
    monkeypatch.setattr(metadata, "resolve", fake)
    dupefinder.harvest(limit=50, delay=0, sleep=lambda s: None)
    assert fake.calls == ["Plain Book"] and manual and linked and plain


def test_a_conflict_clears_when_the_work_links_after_all(monkeypatch):
    w = fileonly("Late Linker", "Writer")
    other = provider("Late Linker", "Writer", "mid-ll")
    dupefinder.record_refresh_conflict(w, other, "mid-ll")
    assert len(dupefinder.open_conflicts()) == 1
    with db.tx() as conn:
        conn.execute("UPDATE works SET metadata_id=NULL WHERE id=?", (other,))
    monkeypatch.setattr(metadata, "get_work", lambda mid: {"id": mid, "title": "Late Linker", "contributors": [], "editions": []})
    monkeypatch.setattr(metadata, "upsert_work", lambda meta: w)
    metadata.link_work(w, "mid-ll")
    assert dupefinder.open_conflicts() == []


# ---- dismissals, incremental, resumable ---------------------------------------------------------------------------

def test_dismissed_groups_stay_dismissed_until_a_member_changes():
    prov = provider("Hatchet", "Gary Paulsen", "mid-hatchet")
    fw = fileonly("Hatchet", "Gary Paulsen")
    scan()
    gid = find(prov, fw)["id"]
    dupefinder.dismiss(gid, True, {"id": "admin"})
    assert groups() == [] and len(groups(dismissed=True)) == 1
    scan(full=True)
    assert groups() == [] and find(prov, fw, dismissed=True)["dismissed"]            # remembered through a full re-scan
    with db.tx() as conn:
        conn.execute("UPDATE works SET title='Hatchet (Book 1)',updated_at=? WHERE id=?", (db.now() + 5, fw))   # a member changes identity
    scan()
    assert find(prov, fw) and not find(prov, fw)["dismissed"]
    dupefinder.dismiss(gid, True)
    dupefinder.dismiss(gid, False)
    assert find(prov, fw)


def test_incremental_scan_only_looks_at_changed_works(monkeypatch):
    p1 = provider("Old Pair", "Ann Writer", "mid-op")
    f1 = fileonly("Old Pair", "Ann Writer")
    first = scan()
    assert first["mode"] == "full" and first["groups_found"] == 1
    old_scan_id = db.one("SELECT scan_id FROM duplicate_groups")["scan_id"]
    p2 = provider("New Pair", "Bea Writer", "mid-np")
    f2 = fileonly("New Pair", "Bea Writer")
    touched = []
    real = dupefinder._process_blocks
    monkeypatch.setattr(dupefinder, "_process_blocks", lambda keys, blocks, sid: touched.extend(keys) or real(keys, blocks, sid))
    second = scan()
    assert second["mode"] == "incremental" and touched == ["new pair"]               # the untouched pair's block is not re-read
    assert find(p2, f2) and find(p1, f1)
    assert db.one("SELECT scan_id FROM duplicate_groups WHERE members_key=?", (",".join(sorted((p1, f1))),))["scan_id"] == old_scan_id
    # fixing a title makes the old group retire without a full scan
    with db.tx() as conn:
        conn.execute("UPDATE works SET title='Something Else Entirely',updated_at=? WHERE id=?", (db.now() + 5, f1))
    scan()
    assert not find(p1, f1)


def test_status_reports_last_scanned_and_counts():
    assert dupefinder.status()["last_complete_at"] is None
    provider("Counted", "Ann Writer", "mid-c")
    fileonly("Counted", "Ann Writer")
    scan()
    st = dupefinder.status()
    assert st["last_complete_at"] and st["counts"]["high"] == 1 and st["counts"]["open"] == 1 and st["status"] == "idle"


def test_an_interrupted_scan_resumes_from_its_cursor_and_finds_the_same_groups():
    for i in range(6):
        provider(f"Book Number {i} Alpha", "Ann Writer", f"mid-r{i}")
        fileonly(f"Book Number {i} Alpha", "Ann Writer")
    part = dupefinder.scan(chunk_blocks=2, max_chunks=1)
    assert part["status"] == "partial" and part["blocks_done"] == 2 and part["blocks_total"] == 6
    st = db.one("SELECT status,cursor,scan_id FROM duplicate_scan_state")
    assert st["status"] == "partial" and st["cursor"]                                  # resumable, not 'running' forever
    scan_id = st["scan_id"]
    mid = db.one("SELECT COUNT(*) n FROM duplicate_groups")["n"]
    assert mid == 2                                                                   # partial results are already stored
    done = dupefinder.scan(chunk_blocks=2)
    assert done["status"] == "done" and done["scan_id"] == scan_id and done["blocks_done"] == 6
    assert len(groups()) == 6


def test_scan_job_runs_off_the_scheduler_loop_through_the_job_framework():
    provider("Via Job", "Ann Writer", "mid-vj")
    fileonly("Via Job", "Ann Writer")
    job = db.one("SELECT * FROM jobs WHERE kind='duplicate-scan'")
    assert job and job["schedule_seconds"] is None and job["enabled"] == 1               # manual-only: never due on the loop
    assert scheduler.run_due_jobs(block=True) == [] or all(r.get("kind") != "duplicate-scan" for r in scheduler.run_due_jobs())
    out = scheduler.run_job(job["id"])
    assert out["status"] == "success" and out["result"]["status"] == "done"
    assert len(groups()) == 1
    scheduler.shutdown_pool()


# ---- API ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def local_mode(monkeypatch):
    replaced = dataclasses.replace(base_settings, auth_mode="local")
    monkeypatch.setattr(main_module, "settings", replaced)
    monkeypatch.setattr(auth_module, "settings", replaced)


def test_duplicate_endpoints_are_admin_only_and_writes_need_csrf(local_mode):
    prov = provider("Gated", "Ann Writer", "mid-gate")
    fw = fileonly("Gated", "Ann Writer")
    scan()
    gid = find(prov, fw)["id"]
    with TestClient(app) as anon, TestClient(app) as admin, TestClient(app) as adult:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        assert admin.post("/api/v1/users", json={"username": "jane", "password": "a-very-long-jane-password", "account_type": "adult"}, headers=csrf).status_code == 200
        jane = adult.post("/api/v1/auth/login", json={"username": "jane", "password": "a-very-long-jane-password"})
        jcsrf = {"X-CSRF-Token": jane.json()["csrf"]}
        reads = ["/api/v1/admin/duplicates", "/api/v1/admin/duplicates/status", "/api/v1/admin/duplicates/batches"]
        writes = [("/api/v1/admin/duplicates/scan", {}), (f"/api/v1/admin/duplicates/{gid}/dismiss", {}), ("/api/v1/admin/duplicates/dry-run", {"group_ids": [gid]}),
                  ("/api/v1/admin/duplicates/bulk-merge", {"group_ids": [gid]}), ("/api/v1/admin/duplicates/harvest", {}),
                  ("/api/v1/admin/placeholder-assets/cleanup", {"apply": True})]
        for client, headers, want in ((anon, {}, 401), (adult, jcsrf, 403)):
            for url in reads:
                assert client.get(url).status_code == want, url
            for url, body in writes:
                assert client.post(url, json=body, headers=headers).status_code == want, url
        for url, body in writes:
            assert admin.post(url, json=body).status_code == 403, url                   # no CSRF token
        assert admin.get("/api/v1/admin/duplicates").json()["total"] == 1
        assert admin.get("/api/v1/admin/duplicates", params={"confidence": "low"}).json()["total"] == 0
        assert admin.get("/api/v1/admin/duplicates", params={"q": "gated"}).json()["total"] == 1
        assert admin.get("/api/v1/admin/duplicates", params={"has_owned": False}).json()["total"] == 0
        assert admin.post(f"/api/v1/admin/duplicates/{gid}/dismiss", json={}, headers=csrf).json()["dismissed"] is True
        assert admin.get("/api/v1/admin/duplicates").json()["total"] == 0
        assert admin.get("/api/v1/admin/duplicates", params={"dismissed": True}).json()["total"] == 1
        assert admin.post("/api/v1/admin/duplicates/nope/dismiss", json={}, headers=csrf).status_code == 404
        r = admin.post("/api/v1/admin/duplicates/scan", json={"full": True}, headers=csrf)
        assert r.status_code == 202 and r.json()["kind"] == "duplicate-scan"
    scheduler.shutdown_pool()


# ---- review fixes: interrupted scans, GET is read-only, merged groups kept (Opus review, 2026-10-14) ---------------------

def test_a_budget_expired_scan_is_partial_resumable_and_never_stuck_running():
    for i in range(4):
        provider(f"Budget Book {i} Alpha", "Ann Writer", f"mid-b{i}")
        fileonly(f"Budget Book {i} Alpha", "Ann Writer")
    part = dupefinder.scan(budget_seconds=-1)                       # budget already spent
    assert part["status"] == "partial"
    st = dupefinder.status()
    assert st["status"] == "partial" and st["resumable"] is True and st["interrupted"] is False
    assert db.one("SELECT status FROM duplicate_scan_state")["status"] == "partial"
    done = dupefinder.scan()
    assert done["status"] == "done" and done["scan_id"] == part["scan_id"] and dupefinder.status()["status"] == "idle"


def test_a_scan_left_running_by_a_dead_process_reads_as_interrupted_after_twice_the_budget():
    provider("Dead Scan", "Ann Writer", "mid-ds")
    fileonly("Dead Scan", "Ann Writer")
    dupefinder.scan(max_chunks=0)                                   # leaves a cursor-less partial state
    with db.tx() as conn:                                           # simulate a killed worker: status stuck at 'running'
        conn.execute("UPDATE duplicate_scan_state SET status='running',started_at=?", (db.now() - 10,))
    db.set_setting(dupefinder.HEARTBEAT_KEY, {"at": db.now() - 10, "budget": 900.0})
    assert dupefinder.status()["status"] == "running"             # a fresh heartbeat: really running
    db.set_setting(dupefinder.HEARTBEAT_KEY, {"at": db.now() - 1801, "budget": 900.0})
    with db.tx() as conn:
        conn.execute("UPDATE duplicate_scan_state SET started_at=?", (db.now() - 5000,))
    st = dupefinder.status()
    assert st["status"] == "partial" and st["interrupted"] is True and st["resumable"] is True   # > 2x budget: interrupted
    assert dupefinder.scan()["status"] == "done"                                                   # and Resume works


def test_get_endpoints_never_delete_and_the_scan_does_the_housekeeping(local_mode):
    prov = provider("Vanishing", "Ann Writer", "mid-van")
    fw = fileonly("Vanishing", "Ann Writer")
    scan()
    with db.tx() as conn:
        conn.execute("DELETE FROM media_files WHERE asset_id IN (SELECT id FROM media_assets WHERE work_id=?)", (fw,))
        conn.execute("DELETE FROM media_assets WHERE work_id=?", (fw,))
        conn.execute("DELETE FROM work_contributors WHERE work_id=?", (fw,))
        conn.execute("DELETE FROM works WHERE id=?", (fw,))
    with TestClient(app) as admin:
        r = admin.post("/api/v1/auth/setup", json={"username": "ada", "password": "a-very-long-admin-password"})
        assert admin.get("/api/v1/admin/duplicates").json()["total"] == 0                                 # hidden...
        st = admin.get("/api/v1/admin/duplicates/status").json()
        assert st["counts"]["open"] == 0
    assert db.one("SELECT COUNT(*) n FROM duplicate_groups")["n"] == 1 and prov                             # ...but not deleted by a GET
    dupefinder.scan()
    assert db.one("SELECT COUNT(*) n FROM duplicate_groups")["n"] == 0                                      # the scan prunes
    scheduler.shutdown_pool()
