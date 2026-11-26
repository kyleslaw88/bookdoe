"""Owned-media quality evaluation (P5-26, Part XXIX)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import fileverify, quality, scheduler, scanner

client = TestClient(app)

AUDIO_DEFAULT = {"allowed_formats": ["m4b", "mp3"], "preferred_formats": ["m4b", "mp3"], "abridged": "reject"}


def _audio_file(**kw):
    return {"path": "/a/1.m4b", "chapter_count": 12, "has_embedded_cover": 1, "bitrate": 128000, "codec": "aac", "embedded": {}, **kw}


def _ebook_file(drm=None, path="/a/b.epub"):
    return {"path": path, "embedded": {} if drm is None else {"drm": drm}}


# (name, asset, files, profile, siblings, state, reason fragment)
CASES = [
    ("m4b default", {"kind": "audiobook", "format": "m4b", "abridged": 0}, [_audio_file()], AUDIO_DEFAULT, [], "satisfactory", None),
    ("mp3 below m4b cutoff", {"kind": "audiobook", "format": "mp3"}, [_audio_file(), _audio_file(path="/a/2.mp3")],
     {**AUDIO_DEFAULT, "cutoff": "m4b"}, [], "upgrade_desired", "below the profile cutoff m4b"),
    ("mp3 no cutoff is fine", {"kind": "audiobook", "format": "mp3"}, [_audio_file()], AUDIO_DEFAULT, [], "satisfactory", None),
    ("disallowed format", {"kind": "audiobook", "format": "flac"}, [_audio_file()], AUDIO_DEFAULT, [], "upgrade_desired", "not allowed"),
    ("abridged rejected", {"kind": "audiobook", "format": "m4b", "abridged": 1}, [_audio_file()], AUDIO_DEFAULT, [], "upgrade_desired", "Abridged"),
    ("abridged unknown is not abridged", {"kind": "audiobook", "format": "m4b", "abridged": None}, [_audio_file()], AUDIO_DEFAULT, [], "satisfactory", None),
    ("abridged allowed", {"kind": "audiobook", "format": "m4b", "abridged": 1}, [_audio_file()], {**AUDIO_DEFAULT, "abridged": "allow"}, [], "satisfactory", None),
    ("chapters required", {"kind": "audiobook", "format": "m4b"}, [_audio_file(chapter_count=0)], {**AUDIO_DEFAULT, "chapters_required": True}, [], "upgrade_desired", "No chapters"),
    ("chapters not required by default", {"kind": "audiobook", "format": "m4b"}, [_audio_file(chapter_count=0)], AUDIO_DEFAULT, [], "satisfactory", None),
    ("cover preferred", {"kind": "audiobook", "format": "m4b"}, [_audio_file(has_embedded_cover=0)], {**AUDIO_DEFAULT, "embedded_cover_preferred": True}, [], "upgrade_desired", "No embedded cover"),
    ("bitrate floor", {"kind": "audiobook", "format": "m4b"}, [_audio_file(bitrate=48000)], {**AUDIO_DEFAULT, "min_bitrate_kbps": 64}, [], "upgrade_desired", "Bitrate"),
    ("bitrate ok", {"kind": "audiobook", "format": "m4b"}, [_audio_file(bitrate=96000)], {**AUDIO_DEFAULT, "min_bitrate_kbps": 64}, [], "satisfactory", None),
    ("bitrate NULL with floor", {"kind": "audiobook", "format": "m4b"}, [_audio_file(bitrate=None)], {**AUDIO_DEFAULT, "min_bitrate_kbps": 64}, [], "unevaluated", "bitrate"),
    ("codec not preferred", {"kind": "audiobook", "format": "m4b"}, [_audio_file(codec="mp3")], {**AUDIO_DEFAULT, "preferred_codecs": ["aac", "opus"]}, [], "upgrade_desired", "Codec mp3"),
    ("codec preferred", {"kind": "audiobook", "format": "m4b"}, [_audio_file(codec="AAC")], {**AUDIO_DEFAULT, "preferred_codecs": ["aac"]}, [], "satisfactory", None),
    ("NULL stream data", {"kind": "audiobook", "format": "m4b"}, [_audio_file(has_embedded_cover=None, chapter_count=None, codec=None)], AUDIO_DEFAULT, [], "unevaluated", "not been probed"),
    ("no files", {"kind": "audiobook", "format": "m4b"}, [], AUDIO_DEFAULT, [], "unevaluated", "no present files"),
    ("no profile", {"kind": "audiobook", "format": "m4b"}, [_audio_file()], None, [], "unevaluated", "No quality profile"),
    ("epub", {"kind": "ebook", "format": "epub"}, [_ebook_file()], {"cutoff": "epub"}, [], "satisfactory", None),
    ("pdf below cutoff", {"kind": "ebook", "format": "pdf"}, [_ebook_file(path="/a/b.pdf")], {"cutoff": "epub"}, [], "upgrade_desired", "below the profile cutoff"),
    ("pdf with epub sibling", {"kind": "ebook", "format": "pdf"}, [_ebook_file(path="/a/b.pdf")], {"pdf_undesirable_if_epub": True},
     [{"kind": "ebook", "format": "epub"}], "upgrade_desired", "PDF undesirable when EPUB exists"),
    ("pdf alone", {"kind": "ebook", "format": "pdf"}, [_ebook_file(path="/a/b.pdf")], {"pdf_undesirable_if_epub": True}, [], "satisfactory", None),
    ("pdf with epub, key unset (old profile)", {"kind": "ebook", "format": "pdf"}, [_ebook_file(path="/a/b.pdf")], {},
     [{"kind": "ebook", "format": "epub"}], "satisfactory", None),
    ("audiobook sibling is not an epub", {"kind": "ebook", "format": "pdf"}, [_ebook_file(path="/a/b.pdf")], {"pdf_undesirable_if_epub": True},
     [{"kind": "audiobook", "format": "epub"}], "satisfactory", None),
    ("kepub derivative ok", {"kind": "ebook", "format": "kepub"}, [_ebook_file(path="/a/b.kepub.epub")], {"cutoff": "epub", "preferred_formats": ["epub", "pdf"]}, [], "satisfactory", None),
    ("kepub not ok", {"kind": "ebook", "format": "kepub"}, [_ebook_file(path="/a/b.kepub.epub")],
     {"cutoff": "epub", "preferred_formats": ["epub", "pdf"], "kepub_derivative_ok": False}, [], "upgrade_desired", "kepub is below"),
    ("drm flagged but not required", {"kind": "ebook", "format": "epub"}, [_ebook_file(drm=True)], {"cutoff": "epub"}, [], "satisfactory", None),
    ("drm required", {"kind": "ebook", "format": "epub"}, [_ebook_file(drm=True)], {"require_drm_free": True}, [], "upgrade_desired", "DRM-protected"),
    ("drm free required and clean", {"kind": "ebook", "format": "epub"}, [_ebook_file(drm=False)], {"require_drm_free": True}, [], "satisfactory", None),
    ("drm required but undetected", {"kind": "ebook", "format": "epub"}, [_ebook_file()], {"require_drm_free": True}, [], "unevaluated", "DRM"),
]


@pytest.mark.parametrize("name,asset,files,profile,siblings,state,fragment", CASES, ids=[c[0] for c in CASES])
def test_evaluate_table(name, asset, files, profile, siblings, state, fragment):
    out = quality.evaluate(asset, files, profile, siblings)
    assert out["state"] == state, out
    if fragment:
        assert any(fragment in r for r in out["reasons"]), out
    else:
        assert out["reasons"] == []


# ---- database wrapper, real files and routes ----

def _work(title="T"):
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid, title, title, t, t))
    return wid


def _asset(wid, kind, fmt, profile_id=None, abridged=None, status="owned"):
    aid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO media_assets(id,work_id,kind,format,status,profile_id,abridged,added_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (aid, wid, kind, fmt, status, profile_id, abridged, t, t))
    return aid


def _file(aid, path, **kw):
    cols = {"size_bytes": 10, "discovered_at": db.now(), "embedded_json": "{}", **kw}
    with db.tx() as c:
        c.execute(f"INSERT INTO media_files(id,asset_id,path,{','.join(cols)}) VALUES (?,?,?,{','.join('?' * len(cols))})",  # nosec B608
                  (db.uid(), aid, path, *cols.values()))


def _epub(path: Path, encryption: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", "<container/>")
        if encryption:
            z.writestr("META-INF/encryption.xml", encryption)
    return path


ENC = '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:EncryptionMethod Algorithm="{}"/></enc:EncryptedData></encryption>'


def test_m4b_with_chapters_and_cover_is_satisfactory_under_default_profile():
    aid = _asset(_work(), "audiobook", "m4b", "audio-default", abridged=0)
    _file(aid, "/x/a.m4b", chapter_count=10, has_embedded_cover=1, codec="aac", bitrate=64000)
    out = quality.evaluate_asset(aid)
    assert (out["state"], out["reasons"], out["profile_id"]) == ("satisfactory", [], "audio-default")
    assert db.json_load(db.one("SELECT quality_json FROM media_assets WHERE id=?", (aid,))["quality_json"], {})["evaluation"] == out


def test_mp3_multipart_below_m4b_cutoff_is_upgrade_desired():
    pid = "cutoff-m4b"
    with db.tx() as c:
        c.execute("INSERT INTO profiles(id,name,media_kind,config_json,is_default,created_at,updated_at) VALUES (?,?,?,?,0,?,?)",
                  (pid, "m4b cutoff", "audiobook", db.json_dump({"preferred_formats": ["m4b", "mp3"], "cutoff": "m4b"}), db.now(), db.now()))
    aid = _asset(_work(), "audiobook", "mp3", pid)
    for i in (1, 2):
        _file(aid, f"/x/{i}.mp3", part_number=i, chapter_count=0, has_embedded_cover=0, codec="mp3", bitrate=64000)
    out = quality.evaluate_asset(aid)
    assert out["state"] == "upgrade_desired"
    assert any("mp3 is below the profile cutoff m4b" in r for r in out["reasons"])


def test_pdf_with_owned_epub_on_same_work_is_upgrade_desired():
    wid = _work()
    pdf, epub = _asset(wid, "ebook", "pdf", "ebook-default"), _asset(wid, "ebook", "epub", "ebook-default")
    _file(pdf, "/x/b.pdf")
    _file(epub, "/x/b.epub")
    out = quality.evaluate_asset(pdf)
    assert out["state"] == "upgrade_desired" and "PDF undesirable when EPUB exists" in out["reasons"]
    assert quality.evaluate_asset(epub)["state"] == "satisfactory"


def test_missing_epub_file_does_not_count_as_owned_epub():
    wid = _work()
    pdf, epub = _asset(wid, "ebook", "pdf", "ebook-default"), _asset(wid, "ebook", "epub", "ebook-default")
    _file(pdf, "/x/b.pdf")
    _file(epub, "/x/b.epub", missing=1)
    assert "PDF undesirable when EPUB exists" not in quality.evaluate_asset(pdf)["reasons"]


def test_asset_with_null_stream_data_is_unevaluated():
    aid = _asset(_work(), "audiobook", "m4b", "audio-default")
    _file(aid, "/x/a.m4b")
    assert quality.evaluate_asset(aid)["state"] == "unevaluated"


def test_unowned_asset_is_unevaluated():
    aid = _asset(_work(), "audiobook", "m4b", status="missing")
    assert quality.evaluate_asset(aid)["state"] == "unevaluated"


def test_evaluation_writes_only_the_evaluation_key():
    aid = _asset(_work(), "audiobook", "m4b", "audio-default")
    _file(aid, "/x/a.m4b", has_embedded_cover=1)
    with db.tx() as c:
        c.execute("UPDATE media_assets SET quality_json=? WHERE id=?", (db.json_dump({"other": 1}), aid))
    before = db.one("SELECT * FROM media_assets WHERE id=?", (aid,))
    files_before = db.all("SELECT * FROM media_files WHERE asset_id=?", (aid,))
    quality.evaluate_asset(aid)
    after = db.one("SELECT * FROM media_assets WHERE id=?", (aid,))
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"quality_json"}
    assert db.json_load(after["quality_json"], {})["other"] == 1
    assert db.all("SELECT * FROM media_files WHERE asset_id=?", (aid,)) == files_before


def test_drm_epub_is_detected_by_scan_and_rescan_and_flagged(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    drm = _epub(root / "drm.epub", ENC.format("http://www.w3.org/2001/04/xmlenc#aes128-cbc"))
    fonts = _epub(root / "fonts.epub", ENC.format("http://www.idpf.org/2008/embedding"))
    plain = _epub(root / "plain.epub")
    assert scanner.inspect_file(drm, root)["drm"] is True
    assert scanner.inspect_file(fonts, root)["drm"] is False
    assert scanner.inspect_file(plain, root)["drm"] is False

    now = db.now()
    with db.tx() as c:
        c.execute("INSERT INTO root_folders(id,name,path,media_kind,management_mode,monitor_default,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (db.uid(), "R", str(root), "ebook", "managed", 1, 1, now, now))
    aid = _asset(_work(), "ebook", "epub", "ebook-default")
    _file(aid, str(drm))
    fid = db.one("SELECT id FROM media_files WHERE asset_id=?", (aid,))["id"]
    assert fileverify.rescan_file(fid)["rescanned"] is True
    assert db.json_load(db.one("SELECT embedded_json FROM media_files WHERE id=?", (fid,))["embedded_json"], {})["drm"] is True

    out = quality.evaluate_asset(aid)
    assert out["drm"] is True and out["state"] == "satisfactory"
    r = client.put("/api/v1/profiles/ebook-default", json={"require_drm_free": True})
    assert r.status_code == 200, r.text
    out = quality.evaluate_asset(aid)
    assert out["state"] == "upgrade_desired" and any("DRM" in x for x in out["reasons"])


def test_quality_routes_and_library_summary():
    wid = _work()
    aid = _asset(wid, "audiobook", "m4b", "audio-default", abridged=0)
    _file(aid, "/x/a.m4b", has_embedded_cover=1, chapter_count=3)
    assert client.get(f"/api/v1/assets/{aid}/quality").json()["state"] == "unevaluated"
    r = client.post(f"/api/v1/assets/{aid}/quality/evaluate")
    assert r.status_code == 200 and r.json()["state"] == "satisfactory"
    assert client.get(f"/api/v1/assets/{aid}/quality").json() == r.json()
    work = client.get(f"/api/v1/library/{wid}").json()
    assert [a["quality_state"] for a in work["assets"]] == ["satisfactory"]
    listing = client.get("/api/v1/library").json()
    assert [a["quality_state"] for i in listing["items"] if i["id"] == wid for a in i["assets"]] == ["satisfactory"]
    assert client.get("/api/v1/assets/nope/quality").status_code == 404
    assert client.post("/api/v1/assets/nope/quality/evaluate").status_code == 404


def test_work_detail_payload_includes_quality_state_for_owned_assets():
    wid = _work()
    epub = _asset(wid, "ebook", "epub", "ebook-default")
    _file(epub, "/x/b.epub")
    pdf = _asset(wid, "ebook", "pdf", "ebook-default")
    _file(pdf, "/x/b.pdf")
    audio = _asset(wid, "audiobook", "m4b", "audio-default")
    _file(audio, "/x/a.m4b", has_embedded_cover=1, chapter_count=3)

    r = client.get(f"/api/v1/library/{wid}")
    assert r.status_code == 200, r.text
    assert {a["id"]: a["quality_state"] for a in r.json()["assets"]} == {epub: "unevaluated", pdf: "unevaluated", audio: "unevaluated"}

    for aid in (epub, pdf):
        assert client.post(f"/api/v1/assets/{aid}/quality/evaluate").status_code == 200
    states = {a["id"]: a["quality_state"] for a in client.get(f"/api/v1/library/{wid}").json()["assets"]}
    assert states == {epub: "satisfactory", pdf: "upgrade_desired", audio: "unevaluated"}


def test_library_health_job_runs_evaluate_all(tmp_path):
    aid = _asset(_work(), "audiobook", "m4b", "audio-default")
    present = tmp_path / "a.m4b"
    present.write_bytes(b"x")
    _file(aid, str(present), has_embedded_cover=1)
    result = scheduler._library_health()
    assert result["quality"]["evaluated"] >= 1
    assert quality.stored_evaluation(aid) is not None


def test_library_health_leaves_an_asset_with_only_a_missing_file_out_of_evaluation():
    aid = _asset(_work(), "audiobook", "m4b", "audio-default")
    _file(aid, "/nonexistent/a.m4b", has_embedded_cover=1)
    result = scheduler._library_health()
    # Health marks the file missing and the asset stops reading owned, so it is not evaluated.
    assert db.one("SELECT status FROM media_assets WHERE id=?", (aid,))["status"] == "none"
    assert result["quality"]["evaluated"] == 0


def test_evaluate_all_respects_limit_and_prefers_never_evaluated():
    wid = _work()
    ids = []
    for fmt in ("m4b", "mp3"):
        aid = _asset(wid, "audiobook", fmt, "audio-default")
        _file(aid, f"/x/a.{fmt}", has_embedded_cover=1)
        ids.append(aid)
    assert quality.evaluate_all(limit=1)["evaluated"] == 1
    assert quality.evaluate_all(limit=1)["evaluated"] == 1
    assert all(quality.stored_evaluation(i) for i in ids)


def test_profile_put_validates_new_keys_and_rejects_unknown():
    body = {"chapters_required": True, "embedded_cover_preferred": True, "min_bitrate_kbps": 64, "preferred_codecs": ["AAC", "opus"],
            "require_drm_free": True, "kepub_derivative_ok": False, "pdf_undesirable_if_epub": True}
    r = client.put("/api/v1/profiles/audio-default", json=body)
    assert r.status_code == 200, r.text
    assert {k: r.json()["config"][k] for k in body} == {**body, "preferred_codecs": ["aac", "opus"]}
    assert client.put("/api/v1/profiles/audio-default", json={"surprise": 1}).status_code == 422
    assert client.put("/api/v1/profiles/audio-default", json={"min_bitrate_kbps": -1}).status_code == 422
    assert client.put("/api/v1/profiles/audio-default", json={"chapters_required": "maybe"}).status_code == 422
    assert client.put("/api/v1/profiles/audio-default", json={"preferred_codecs": [" "]}).status_code == 400


def test_old_profile_without_new_keys_keeps_old_behaviour():
    with db.tx() as c:
        c.execute("UPDATE profiles SET config_json=? WHERE id='ebook-default'", (db.json_dump({"cutoff": "epub", "preferred_formats": ["epub", "pdf"]}),))
    wid = _work()
    pdf, epub = _asset(wid, "ebook", "pdf", "ebook-default"), _asset(wid, "ebook", "epub", "ebook-default")
    _file(pdf, "/x/b.pdf")
    _file(epub, "/x/b.epub")
    assert quality.evaluate_asset(epub)["reasons"] == []
    assert quality.evaluate_asset(pdf)["reasons"] == ["Format pdf is below the profile cutoff epub"]
