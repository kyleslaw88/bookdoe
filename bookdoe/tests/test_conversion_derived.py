"""Derived assets (issue #265): a converted copy is its own asset, linked to its source, and is
never a second owned copy. Covers the migration, ownership / acquisition semantics, duplicate
detection, the work merge, the Management label, kid visibility and the Player's capability
negotiation (default behaviour unchanged).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, migrations
from app import main as main_module
from app.services import catalogdupes, derived, importer, monitoring, quality, tagging, upgrades, wanted_search, workmerge

import conv_fixtures as cf
import helpers
# Fixtures and helpers of the Player API suite: a local-auth world with an admin, an adult and a kid.
from test_player_api import _book, _login, local_mode, pair, world  # noqa: F401
from test_import_idempotent import prepare_import

client = TestClient(main_module.app)


def _mp3_book(tmp_path, title="Watch the Skies", author="Eric Flint", n=2, mode="managed"):
    rt = cf.root(tmp_path, mode, "audiobook")
    files = [cf.make_mp3(rt["path"] / author / title / f"{i:02d}.mp3", seconds=1, title=f"Part {i}", track=i) if cf.HAVE_FFMPEG
             else _stub(rt["path"] / author / title / f"{i:02d}.mp3") for i in range(1, n + 1)]
    work_id, asset_id = cf.add_book(rt, title, author, "audiobook", files)
    return rt, work_id, asset_id, files


def _stub(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + b"\x00" * 300)
    return path


# ---------------------------------------------------------------------------
# Migration 34
# ---------------------------------------------------------------------------

def test_migration_33_adds_columns_table_and_active_index():
    with db.session() as conn:
        cols = migrations._columns(conn, "media_assets")
        assert {"derived_from_asset_id", "derived_by", "derived_at"} <= cols
        assert migrations._table_exists(conn, "conversions")
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(conversions)")}
        assert "idx_conversions_active" in idx
        assert conn.execute("SELECT name FROM migration_steps WHERE version=34").fetchone()["name"] == "media_conversions"
        # every pre-existing and new ordinary asset is "not derived"
        assert conn.execute("SELECT COUNT(*) FROM media_assets WHERE derived_from_asset_id IS NOT NULL").fetchone()[0] == 0


def test_migration_33_is_idempotent_and_keeps_derived_rows(tmp_path):
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    derived_id = cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    with db.tx() as conn:
        conn.execute("DELETE FROM migration_steps WHERE version=34")
    results = migrations.run_pending_migrations()
    assert [r["version"] for r in results] == [34]
    assert results[0]["row_counts"] == {"columns_added": 0, "tables_created": 0, "already_applied": 1}
    row = db.one("SELECT derived_from_asset_id,derived_by FROM media_assets WHERE id=?", (derived_id,))
    assert row == {"derived_from_asset_id": asset_id, "derived_by": "test 1"}
    assert migrations.run_pending_migrations() == []


def test_one_active_conversion_per_source_and_target_is_enforced_by_the_database(tmp_path):
    _, work_id, asset_id, _ = _mp3_book(tmp_path)

    def insert(status):
        with db.tx() as conn:
            conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,created_at) VALUES (?,?,?,?,?,?,?)",
                         (db.uid(), "audio", work_id, asset_id, "m4b", status, db.now()))

    insert("queued")
    with pytest.raises(Exception, match="UNIQUE"):
        insert("running")
    insert("failed")      # history rows are not limited


# ---------------------------------------------------------------------------
# A derived asset is not a second owned copy
# ---------------------------------------------------------------------------

def test_derived_asset_never_counts_as_a_second_owned_copy(tmp_path):
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    before = catalogdupes._load_works()[work_id]
    cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    after = catalogdupes._load_works()[work_id]
    assert (after["owned"], after["assets"]) == (before["owned"], before["assets"]) == (1, 1)
    assert after["kinds"] == {"audiobook"}


def test_monitoring_and_series_policies_leave_a_derived_asset_unmonitored(tmp_path):
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    d = cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    monitoring._apply_asset_monitoring(work_id, {"ebook": False, "audiobook": True}, True)
    row = db.one("SELECT monitored,has_acquisition_intent,status FROM media_assets WHERE id=?", (d,))
    assert row == {"monitored": 0, "has_acquisition_intent": 0, "status": "owned"}
    assert db.one("SELECT monitored FROM media_assets WHERE id=?", (asset_id,))["monitored"] == 1
    # and the explicit toggle refuses it: acquisition settings belong to the original
    r = client.patch(f"/api/v1/assets/{d}", json={"monitored": True})
    assert r.status_code == 409 and "converted copy" in r.json()["detail"]
    assert db.one("SELECT monitored FROM media_assets WHERE id=?", (d,))["monitored"] == 0


def test_derived_asset_is_never_wanted_searched_or_upgrade_searched(tmp_path, monkeypatch):
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    d = cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    # Even an (invalid) derived row that claims wanted state is not eligible for anything
    # the automatic paths do: wanted search needs status 'missing', which a derivative never has.
    with db.tx() as conn:
        conn.execute("UPDATE media_assets SET upgrade_policy='search', quality_json=? WHERE id IN (?,?)",
                     (json.dumps({"evaluation": {"state": "upgrade_desired"}}), d, asset_id))
    assert {a["id"] for a in upgrades.eligible_assets()} == {asset_id}
    calls = []
    monkeypatch.setattr("app.services.releases.search_releases", lambda *a, **k: calls.append(a) or {"items": []})
    wanted_search.run(None)
    assert calls == []
    assert d not in {r["id"] for r in db.all("SELECT id FROM media_assets WHERE id IN (SELECT id FROM media_assets ORDER BY id LIMIT 50)"
                                              " AND status='missing'")}
    # quality evaluation skips it as well
    assert quality.evaluate_all()["evaluated"] == 1


def test_a_real_import_of_the_same_format_supersedes_the_derived_copy(tmp_path):
    """The slot UNIQUE(work, kind, format) is shared: an m4b that arrives through the importer
    takes it, and the redundant converted file we made goes (it would otherwise be re-adopted)."""
    from test_download_cancel import _download

    did, work_id, wanted_asset = _download()          # a wanted audiobook slot with a download
    rt = cf.root(tmp_path, "managed", "audiobook", name="library")
    src = cf.add_book(rt, "Other", "Other", "audiobook", [_stub(rt["path"] / "x.mp3")])
    mp3 = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','mp3','owned',0,?,?)",
                     (mp3, work_id, db.now(), db.now()))
    derived_file = rt["path"] / "X" / "Some Book" / "Some Book.m4b"
    d = cf.add_derived(mp3, "m4b", derived_file)
    assert derived_file.exists()
    prepare_import(tmp_path, did, ["real.m4b"])
    importer.import_download(did)
    assert db.one("SELECT 1 FROM media_assets WHERE id=?", (d,)) is None
    assert not derived_file.exists()
    asset = db.one("SELECT format,status,derived_from_asset_id FROM media_assets WHERE id=?", (wanted_asset,))
    assert asset == {"format": "m4b", "status": "owned", "derived_from_asset_id": None}
    assert db.one("SELECT status FROM media_assets WHERE id=?", (mp3,))["status"] == "owned"   # the original is untouched


def test_scans_and_reconcile_never_adopt_a_file_bookdoe_generated(tmp_path):
    from app.services import repair, scanner

    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    out = rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b"
    d = cf.add_derived(asset_id, "m4b", out)
    with db.tx() as conn:   # the conversion row remembers the output even after its asset is gone
        conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,output_path,created_at) VALUES (?,?,?,?,?,'done',?,?)",
                     (db.uid(), "audio", work_id, asset_id, "m4b", str(out), db.now()))
        conn.execute("DELETE FROM media_files WHERE asset_id=?", (d,))
    assert derived.is_generated_path(str(out))
    assert repair.reconcile_managed_files()["registered"] == 0
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE path=?", (str(out),))["n"] == 0


# ---------------------------------------------------------------------------
# Work merge
# ---------------------------------------------------------------------------

def test_merge_handles_every_new_foreign_key():
    with db.session() as conn:
        inventory = workmerge.table_inventory(conn)
    handled = {(d["table"], d["column"]) for d in inventory["handled"]}
    assert {("media_assets", "derived_from_asset_id"), ("conversions", "work_id"), ("conversions", "source_asset_id"),
            ("conversions", "output_asset_id")} <= handled
    assert inventory["unhandled"] == []


def _two_works(tmp_path, *, dup_title="Watch the Skies"):
    rt = cf.root(tmp_path, "managed", "audiobook")
    a = [_stub(rt["path"] / "A" / "a1.mp3")]
    b = [_stub(rt["path"] / "B" / "b1.mp3")]
    canon, canon_asset = cf.add_book(rt, dup_title, "Eric Flint", "audiobook", a)
    src, src_asset = cf.add_book(rt, dup_title + " (dup)", "Eric Flint", "audiobook", b)
    return rt, canon, canon_asset, src, src_asset


def test_merge_keeps_the_source_link_of_a_derived_asset_when_its_source_folds(tmp_path):
    rt, canon, canon_asset, src, src_asset = _two_works(tmp_path)
    d = cf.add_derived(src_asset, "m4b", rt["path"] / "B" / "b.m4b")
    preview = workmerge.preview(canon, [src])
    assert preview["blockers"] == []
    # converted copies are listed in the plan, not as conflicts to resolve
    assert [n["action"] for n in preview["plan"]["derived_assets"]] == ["moved"]
    assert not [c for c in preview["conflicts"] if c["type"] == "asset_collision" and c["format"] == "m4b"]
    workmerge.execute(canon, [src], confirm_dissimilar=True)
    row = db.one("SELECT work_id,derived_from_asset_id,status FROM media_assets WHERE id=?", (d,))
    # the mp3 assets (same kind, same format) folded; the link follows the surviving asset
    assert row == {"work_id": canon, "derived_from_asset_id": canon_asset, "status": "owned"}
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE work_id=? AND derived_from_asset_id IS NULL", (canon,))["n"] == 1


def test_merge_drops_a_derived_copy_whose_slot_is_taken_and_keeps_its_file(tmp_path):
    rt, canon, canon_asset, src, src_asset = _two_works(tmp_path)
    real = rt["path"] / "A" / "real.m4b"
    real.write_bytes(b"real m4b" * 20)
    real_asset = db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',0,?,?)",
                     (real_asset, canon, db.now(), db.now()))
    d_file = rt["path"] / "B" / "b.m4b"
    d = cf.add_derived(src_asset, "m4b", d_file)
    plan = workmerge.preview(canon, [src])["plan"]["derived_assets"]
    assert [(n["action"], n["asset_id"]) for n in plan] == [("dropped", d)]
    workmerge.execute(canon, [src], confirm_dissimilar=True)
    assert db.one("SELECT 1 FROM media_assets WHERE id=?", (d,)) is None
    assert d_file.exists()                                           # files on disk are never touched by a merge
    assert db.one("SELECT COUNT(*) n FROM media_files WHERE path=?", (str(d_file),))["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM media_assets WHERE work_id=? AND format='m4b'", (canon,))["n"] == 1


def test_merge_retires_a_canonical_derived_copy_for_a_real_asset_and_moves_progress(tmp_path):
    rt, canon, canon_asset, src, src_asset = _two_works(tmp_path)
    d = cf.add_derived(canon_asset, "m4b", rt["path"] / "A" / "a.m4b")
    real_file = rt["path"] / "B" / "real.m4b"
    real_file.write_bytes(b"real" * 50)
    real, user = db.uid(), db.uid()
    with db.tx() as conn:
        conn.execute("INSERT INTO media_assets(id,work_id,kind,format,status,monitored,added_at,updated_at) VALUES (?,?,'audiobook','m4b','owned',0,?,?)",
                     (real, src, db.now(), db.now()))
        conn.execute("INSERT INTO media_files(id,asset_id,path,size_bytes,container,managed,adopted,missing,discovered_at) VALUES (?,?,?,?,?,1,0,0,?)",
                     (db.uid(), real, str(real_file), 200, "m4b", db.now()))
        conn.execute("INSERT INTO users(id,username,auth_source,is_admin,access_scope,created_at,updated_at) VALUES (?,?,'local',0,'full',?,?)",
                     (user, "listener", db.now(), db.now()))
        conn.execute("INSERT INTO media_progress(asset_id,user_id,work_id,kind,position,percent,updated_at) VALUES (?,?,?,'audiobook',120,0.1,?)",
                     (d, user, canon, db.now()))
    plan = workmerge.preview(canon, [src])["plan"]["derived_assets"]
    assert [(n["action"], n["asset_id"], n["replaced_by"]) for n in plan] == [("retired", d, real)]
    workmerge.execute(canon, [src], confirm_dissimilar=True)
    assert db.one("SELECT 1 FROM media_assets WHERE id=?", (d,)) is None
    assert db.one("SELECT work_id FROM media_assets WHERE id=?", (real,))["work_id"] == canon
    # the real file is now the work's m4b and the listener's place moved to it; the retired copy's file stays on disk
    assert db.one("SELECT asset_id FROM media_files WHERE path=?", (str(real_file),))["asset_id"] == real
    assert db.one("SELECT position FROM media_progress WHERE asset_id=? AND user_id=?", (real, user))["position"] == 120
    assert (rt["path"] / "A" / "a.m4b").exists()


def test_a_derived_asset_adds_no_ownership_to_a_merge(tmp_path):
    rt, canon, canon_asset, src, src_asset = _two_works(tmp_path)
    cf.add_derived(src_asset, "m4b", rt["path"] / "B" / "b.m4b")
    acq = workmerge.preview(canon, [src])["plan"]["acquisition"]
    assert acq["owned_assets"] == {"canonical": 1, "sources": 1}      # the mp3 only, not the m4b


# ---------------------------------------------------------------------------
# Management label, kid visibility, Player negotiation
# ---------------------------------------------------------------------------

def test_management_work_detail_labels_the_copy_and_links_the_source(tmp_path):
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    d = cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    assets = {a["id"]: a for a in client.get(f"/api/v1/library/{work_id}").json()["assets"]}
    assert assets[d]["derived_label"] == "Converted from MP3"
    assert assets[d]["derived_from_asset_id"] == asset_id and assets[d]["derived_from_format"] == "mp3"
    assert assets[d]["files"][0]["size_bytes"] > 0
    assert "derived_label" not in assets[asset_id]
    assert assets[asset_id]["files"], "the original stays visible with its files"


def test_originals_are_listed_before_their_copies(tmp_path):
    """'m4b' sorts before 'mp3', but the library table shows the first asset of a kind: it must be the original."""
    rt, work_id, asset_id, _ = _mp3_book(tmp_path)
    d = cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "Watch the Skies.m4b")
    assert [a["id"] for a in client.get(f"/api/v1/library/{work_id}").json()["assets"]] == [asset_id, d]
    listed = next(w for w in client.get("/api/v1/library?limit=50").json()["items"] if w["id"] == work_id)
    assert [a["id"] for a in listed["assets"]] == [asset_id, d]


def test_player_api_default_is_unchanged_and_a_declared_format_adds_the_copy(world, tmp_path):
    jane, jh = world["jane"]
    work_id, asset_id, _ = world["audio"]
    d = cf.add_derived(asset_id, "m4b", tmp_path / "Spoken Book.m4b")
    dev, _, _ = pair(jane, jh)

    def item(url):
        return next(w for w in dev.get(url).json()["items"] if w["id"] == work_id)

    plain = item("/api/player/v1/library")
    assert [a["id"] for a in plain["assets"]] == [asset_id] and "preferred" not in plain
    assert all("derived_from" not in a for a in plain["assets"])
    assert dev.get(f"/api/player/v1/library/{work_id}").json()["assets"] == plain["assets"]

    negotiated = item("/api/player/v1/library?formats=m4b,epub")
    assert {a["id"] for a in negotiated["assets"]} == {asset_id, d}
    assert next(a for a in negotiated["assets"] if a["id"] == d)["derived_from"] == asset_id
    assert negotiated["preferred"] == {"audiobook": d}
    # the header is the same declaration; unsupported / non-preferred formats declare nothing
    via_header = dev.get(f"/api/player/v1/library/{work_id}", headers={"X-bookdoe-Formats": "M4B"}).json()
    assert via_header["preferred"] == {"audiobook": d}
    nothing = dev.get(f"/api/player/v1/library/{work_id}?formats=mp3,flac").json()
    assert nothing["assets"] == plain["assets"] and "preferred" not in nothing
    assert "converted_assets" in dev.get("/api/player/v1/capabilities").json()["features"]


def test_a_derived_copy_follows_the_kid_rules_of_its_work(world, tmp_path):
    owen, oh = world["owen"]
    kid_work, kid_asset, _ = _book(tmp_path, "Kids Mobi", "ebook", "mobi")
    tagging.add_tag(kid_work, tagging.kids_tag_id())
    adult_work, adult_asset, _ = _book(tmp_path, "Adult Mobi", "ebook", "mobi")
    d_kid = cf.add_derived(kid_asset, "epub", tmp_path / "Kids Mobi.epub")
    d_adult = cf.add_derived(adult_asset, "epub", tmp_path / "Adult Mobi.epub")
    odev, _, _ = pair(owen, oh, name="Owen's reader")
    items = {w["id"]: w for w in odev.get("/api/player/v1/library?formats=epub").json()["items"]}
    assert set(items) == {kid_work, world["kids"][0]}                      # the kid shelf, nothing else
    assert d_kid in {a["id"] for a in items[kid_work]["assets"]}
    assert items[kid_work]["preferred"] == {"ebook": d_kid}
    # the adult book and its converted copy do not exist for the kid, by listing or by id
    assert odev.get(f"/api/player/v1/library/{adult_work}?formats=epub").status_code == 404
    assert odev.get(f"/api/player/v1/ebook/{adult_work}/{d_adult}").status_code == 404
    assert odev.get(f"/api/v1/library/{adult_work}/files/{db.one('SELECT id FROM media_files WHERE asset_id=?', (d_adult,))['id']}").status_code == 404
    # the kid's own book: the converted copy is readable (the MOBI original is not, in a browser) and
    # its bytes are served under the same visibility rule as any file of that work
    assert odev.get(f"/api/player/v1/ebook/{kid_work}/{d_kid}").status_code == 200
    kid_file = db.one("SELECT id FROM media_files WHERE asset_id=?", (d_kid,))["id"]
    assert odev.get(f"/api/v1/library/{kid_work}/files/{kid_file}").status_code == 200
