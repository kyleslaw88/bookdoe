"""Conversion API and settings: admin-only, CSRF-protected, audited; bulk dry run; auto-on-import."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.services import conversion, importer

import conv_fixtures as cf
from test_player_api import _login, local_mode, world  # noqa: F401
from test_import_idempotent import prepare_import


@pytest.fixture(autouse=True)
def no_background_kick(monkeypatch):
    monkeypatch.setattr(conversion, "kick", lambda kind: None)


def mp3_book(tmp_path, title="Watch the Skies", mode="managed"):
    rt = cf.root(tmp_path, mode, "audiobook", name=f"{mode}-{title}")
    files = []
    for i in (1, 2):
        p = rt["path"] / "Eric Flint" / title / f"{i:02d}.mp3"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3" + bytes([i]) * 4000)
        files.append(p)
    work_id, asset_id = cf.add_book(rt, title, "Eric Flint", "audiobook", files)
    return rt, work_id, asset_id


ENDPOINTS = [
    ("get", "/api/v1/settings/conversion", None),
    ("put", "/api/v1/settings/conversion", {"audio_concurrency": 2}),
    ("post", "/api/v1/assets/nope/convert", {}),
    ("post", "/api/v1/conversions/bulk", {"work_ids": ["x"]}),
    ("get", "/api/v1/conversions", None),
    ("post", "/api/v1/conversions/nope/cancel", {}),
    ("post", "/api/v1/conversions/nope/retry", {}),
]


def call(c, method, url, body=None, headers=None):
    return getattr(c, method)(url, headers=headers, **({} if body is None else {"json": body}))


@pytest.mark.parametrize("who", ["jane", "owen"])
def test_only_an_administrator_reaches_any_conversion_endpoint(world, who):
    person, headers = world[who]
    for method, url, body in ENDPOINTS:
        r = call(person, method, url, body, headers)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0


def test_anonymous_requests_are_refused(world):
    anon = TestClient(main_module.app)
    for method, url, body in ENDPOINTS:
        assert call(anon, method, url, body).status_code in (401, 403)


def test_a_state_changing_request_without_the_csrf_token_is_refused(world, tmp_path):
    admin, ah = world["admin"]
    rt, work_id, asset_id = mp3_book(tmp_path)
    for method, url, body in [("put", "/api/v1/settings/conversion", {"audio_concurrency": 2}),
                              ("post", f"/api/v1/assets/{asset_id}/convert", {}),
                              ("post", "/api/v1/conversions/bulk", {"work_ids": [work_id], "dry_run": False, "confirm": True})]:
        assert call(admin, method, url, body).status_code == 403
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
    assert conversion.get_settings()["audio_concurrency"] == 1


def test_settings_defaults_validation_and_audit(world):
    admin, ah = world["admin"]
    s = admin.get("/api/v1/settings/conversion").json()
    assert s["enabled_audio"] is True and s["enabled_ebook"] is True
    assert (s["audio_bitrate_mono_kbps"], s["audio_bitrate_stereo_kbps"]) == (64, 96)
    assert (s["audio_concurrency"], s["ebook_concurrency"]) == (1, 1)
    assert s["auto_on_import"] is False and s["only_if_no_preferred"] is True
    assert s["targets"] == {"audio": "m4b", "ebook": "epub"} and isinstance(s["ffmpeg_available"], bool)

    r = admin.put("/api/v1/settings/conversion", json={"audio_bitrate_mono_kbps": 48, "auto_on_import": True, "ebook_concurrency": 2}, headers=ah)
    assert r.status_code == 200
    assert (r.json()["audio_bitrate_mono_kbps"], r.json()["auto_on_import"], r.json()["ebook_concurrency"]) == (48, True, 2)
    assert admin.get("/api/v1/settings/conversion").json()["audio_bitrate_mono_kbps"] == 48
    event = db.one("SELECT message FROM history WHERE event_type='settings.conversion'")
    assert "audio_bitrate_mono_kbps 64 -> 48" in event["message"] and "auto_on_import False -> True" in event["message"] and "ada" in event["message"]

    for bad in ({"audio_bitrate_mono_kbps": 5000}, {"audio_concurrency": 0}, {"audio_concurrency": 9}, {"disk_margin_mb": -1},
                {"auto_on_import": "yes"}, {"audio_concurrency": True}, {"audio_concurrency": 1.5}, {"surprise": 1}):
        r = admin.put("/api/v1/settings/conversion", json=bad, headers=ah)
        assert r.status_code == 422, (bad, r.status_code)
    assert conversion.get_settings()["audio_bitrate_mono_kbps"] == 48           # nothing half-applied


def test_convert_cancel_retry_and_list_through_the_api(world, tmp_path):
    admin, ah = world["admin"]
    rt, work_id, asset_id = mp3_book(tmp_path)

    detail = admin.get(f"/api/v1/library/{work_id}").json()
    opt = next(a for a in detail["assets"] if a["id"] == asset_id)["convert"]
    assert opt["state"] == "available" and opt["target"] == "m4b" and opt["estimated_bytes"] > 0 and opt["conversion"] is None
    assert "files" not in opt and "root" not in opt, "no paths leak into the option"

    r = admin.post(f"/api/v1/assets/{asset_id}/convert", json={}, headers=ah)
    assert r.status_code == 202 and r.json()["created"] and r.json()["conversion"]["status"] == "queued"
    conv_id = r.json()["conversion"]["id"]
    again = admin.post(f"/api/v1/assets/{asset_id}/convert", json={}, headers=ah)
    assert again.status_code == 202 and again.json()["created"] is False and again.json()["conversion"]["id"] == conv_id
    assert "tmp_path" not in r.json()["conversion"] and "output_path" not in r.json()["conversion"]

    listed = admin.get("/api/v1/conversions?group=active").json()
    assert listed["total"] == 1 and listed["items"][0]["work_title"] == "Watch the Skies" and listed["items"][0]["source_format"] == "mp3"
    assert listed["counts"] == {"queued": 1}
    assert admin.get("/api/v1/conversions?group=history").json()["total"] == 0
    assert next(a for a in admin.get(f"/api/v1/library/{work_id}").json()["assets"] if a["id"] == asset_id)["convert"]["conversion"]["status"] == "queued"

    assert admin.post(f"/api/v1/conversions/{conv_id}/cancel", json={}, headers=ah).json()["status"] == "cancelled"
    assert admin.post(f"/api/v1/conversions/{conv_id}/cancel", json={}, headers=ah).status_code == 409
    assert admin.post(f"/api/v1/conversions/{conv_id}/retry", json={}, headers=ah).json()["status"] == "queued"
    assert admin.post("/api/v1/conversions/nope/cancel", json={}, headers=ah).status_code == 404
    assert admin.post("/api/v1/assets/nope/convert", json={}, headers=ah).status_code == 404
    events = {h["event_type"] for h in db.all("SELECT event_type FROM history WHERE work_id=?", (work_id,))}
    assert {"conversion.queued", "conversion.cancel", "conversion.retry"} <= events
    queued = db.one("SELECT message FROM history WHERE event_type='conversion.queued'")["message"]
    assert "ada" in queued, "audited with who asked"


def test_the_convert_offer_is_for_administrators_only(world, tmp_path):
    jane, jh = world["jane"]
    rt, work_id, asset_id = mp3_book(tmp_path)
    for c in (jane, world["admin"][0]):
        detail = c.get(f"/api/v1/library/{work_id}").json()
        asset = next(a for a in detail["assets"] if a["id"] == asset_id)
        assert ("convert" in asset) == (c is not jane)


def test_bulk_dry_run_counts_and_estimates_without_writing(world, tmp_path):
    admin, ah = world["admin"]
    _, w1, a1 = mp3_book(tmp_path, "One")
    _, w2, a2 = mp3_book(tmp_path, "Two")
    _, w3, a3 = mp3_book(tmp_path, "Read only", mode="adopt")      # converts into the managed audiobook root
    rt = cf.root(tmp_path, "managed", "ebook")
    pdf = rt["path"] / "x" / "Book.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF" * 30)
    w4, _ = cf.add_book(rt, "A PDF", "X", "ebook", [pdf])
    mobi = cf.make_mobi(rt["path"] / "m" / "Tiny.mobi")
    w5, a5 = cf.add_book(rt, "Tiny", "Anne", "ebook", [mobi])
    wanted_only = cf.add_book  # a work with nothing owned counts as skipped too
    from app.services import migration
    w6 = migration._find_or_create_stub({"title": "Not owned", "author": "Z", "identifiers": {}})

    body = {"work_ids": [w1, w2, w3, w4, w5, w6]}
    r = admin.post("/api/v1/conversions/bulk", json=body, headers=ah)
    assert r.status_code == 200
    plan = r.json()
    assert plan["dry_run"] is True and plan["originals_kept"] is True
    assert (plan["works"], plan["eligible"], plan["audio"], plan["ebook"]) == (6, 4, 3, 1)
    assert plan["estimated_bytes"] > 0 and plan["free_bytes"] and plan["estimated_bytes"] == sum(i["estimated_bytes"] for i in plan["items"])
    assert plan["skipped"] == {"unsupported": 1, "no_owned_files": 1}
    assert plan["into_managed_library"] == 1
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0, "a dry run writes nothing"

    # confirming is explicit
    r = admin.post("/api/v1/conversions/bulk", json={**body, "dry_run": False}, headers=ah)
    assert r.status_code == 422 and "originals are kept" in r.json()["detail"]
    r = admin.post("/api/v1/conversions/bulk", json={**body, "dry_run": False, "confirm": True}, headers=ah)
    assert r.status_code == 200 and r.json()["queued"] == 4 and r.json()["errors"] == []
    assert sorted(x["kind"] for x in db.all("SELECT kind FROM conversions")) == ["audio", "audio", "audio", "ebook"]
    # and asking twice queues nothing new
    again = admin.post("/api/v1/conversions/bulk", json={**body, "dry_run": False, "confirm": True}, headers=ah).json()
    assert again["queued"] == 0 and again["eligible"] == 0 and again["skipped"].get("already_queued") == 4
    assert db.one("SELECT 1 FROM history WHERE event_type='conversion.bulk'")
    assert admin.post("/api/v1/conversions/bulk", json={"work_ids": []}, headers=ah).status_code == 422


def test_only_if_no_preferred_decides_whether_bulk_reconverts_a_changed_source(world, tmp_path):
    admin, ah = world["admin"]
    rt, work_id, asset_id = mp3_book(tmp_path)
    cf.add_derived(asset_id, "m4b", rt["path"] / "Eric Flint" / "Watch the Skies" / "x.m4b")
    done = db.uid()
    with db.tx() as conn:   # a finished conversion whose source signature no longer matches the files
        out = db.one("SELECT id FROM media_assets WHERE derived_from_asset_id=?", (asset_id,))["id"]
        conn.execute("INSERT INTO conversions(id,kind,work_id,source_asset_id,target_format,status,source_signature,output_asset_id,created_at,finished_at)"
                     " VALUES (?,?,?,?,?,'done','old-signature',?,?,?)", (done, "audio", work_id, asset_id, "m4b", out, db.now(), db.now()))
    body = {"work_ids": [work_id]}
    assert admin.post("/api/v1/conversions/bulk", json=body, headers=ah).json()["eligible"] == 0           # default: leave converted books alone
    conversion.save_settings({"only_if_no_preferred": False})
    plan = admin.post("/api/v1/conversions/bulk", json=body, headers=ah).json()
    assert plan["eligible"] == 1 and plan["items"][0]["state"] == "stale"


# ---------------------------------------------------------------------------
# Convert automatically on import
# ---------------------------------------------------------------------------

def _import_mp3(tmp_path):
    from test_download_cancel import _download

    did, work_id, asset_id = _download()
    prepare_import(tmp_path, did, ["a.mp3", "b.mp3"])
    importer.import_download(did)
    return work_id, asset_id


def test_import_does_not_convert_by_default(tmp_path):
    work_id, asset_id = _import_mp3(tmp_path)
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0


def test_import_queues_a_conversion_when_the_policy_is_on(tmp_path):
    conversion.save_settings({"auto_on_import": True})
    work_id, asset_id = _import_mp3(tmp_path)
    row = db.one("SELECT * FROM conversions")
    assert (row["source_asset_id"], row["trigger_source"], row["status"], row["kind"]) == (asset_id, "auto", "queued", "audio")


def test_a_failure_to_queue_never_fails_the_import(tmp_path, monkeypatch):
    conversion.save_settings({"auto_on_import": True})
    monkeypatch.setattr(conversion, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    work_id, asset_id = _import_mp3(tmp_path)
    assert db.one("SELECT status FROM media_assets WHERE id=?", (asset_id,))["status"] == "owned"


def test_auto_conversion_respects_the_kind_switch_and_read_only_roots(tmp_path):
    conversion.save_settings({"auto_on_import": True, "enabled_audio": False})
    _import_mp3(tmp_path)
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
    rt, work_id, asset_id = mp3_book(tmp_path, "Adopted", mode="adopt")
    conversion.save_settings({"enabled_audio": True})
    # the import above left a managed audiobook root, so the adopted book is converted INTO it
    assert conversion.maybe_auto_enqueue(asset_id)["created"] is True
    with db.tx() as conn:
        conn.execute("DELETE FROM conversions")
        conn.execute("DELETE FROM root_folders WHERE management_mode='managed'")
    assert conversion.maybe_auto_enqueue(asset_id) is None            # no managed root: nothing queued, nothing raised
    assert db.one("SELECT COUNT(*) n FROM conversions")["n"] == 0
