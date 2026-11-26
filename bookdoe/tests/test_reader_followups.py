"""Follow-ups from the first days of the reader/player in use.

1. The Library's media-kind filter means "I own this kind", and composes with the
   owned/missing state filter as a description of ONE asset.
2. Starting a book in the reader marks it 'reading' so the reading-status filter finds
   it - without overriding a status the person chose by hand (precedence in reader.py).
3. "Clear progress" in the UI confirms first, then forgets the position and unwinds the
   automatic 'reading' state (never a manual one).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app import db
from app.main import app
from app.services import migration, reader

import helpers
from test_inbox_ui import live, needs_node, run_ui  # noqa: F401  (live is a fixture)
from test_reader import _epub_book, _asset

APP_JS = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text()


def _work(title):
    return migration._find_or_create_stub({"title": title, "author": "Author", "identifiers": {}})


def _titles(c, **params):
    r = c.get("/api/v1/library", params=params)
    assert r.status_code == 200, r.text
    return sorted(w["title"] for w in r.json()["items"])


# ---------- 1. kind / status filters ----------
@pytest.fixture
def mixed_shelf():
    """ebook_only: owns the ebook, WANTS the audiobook. audio_only: owns the audiobook.
    both: owns both. wanted_only: nothing owned (never in the Library)."""
    ebook_only, audio_only, both, wanted_only = (_work(t) for t in ("Ebook Only", "Audio Only", "Both", "Wanted Only"))
    helpers.own(ebook_only, "ebook")
    helpers.want(ebook_only, "audiobook")
    helpers.own(audio_only, "audiobook", "m4b")
    helpers.own(both, "ebook")
    helpers.own(both, "audiobook", "m4b")
    helpers.want(wanted_only, "audiobook")
    return {"ebook_only": ebook_only, "audio_only": audio_only, "both": both}


def test_kind_filter_means_owned_in_that_kind(mixed_shelf):
    with TestClient(app) as c:
        # Before: "Ebook Only" matched kind=audiobook because its WANTED audiobook asset existed.
        assert _titles(c, kind="audiobook") == ["Audio Only", "Both"]
        assert _titles(c, kind="ebook") == ["Both", "Ebook Only"]
        assert _titles(c) == ["Audio Only", "Both", "Ebook Only"]


def test_kind_and_status_describe_the_same_asset(mixed_shelf):
    with TestClient(app) as c:
        # "which of my books is still missing its audiobook" - must still work after the fix
        assert _titles(c, kind="audiobook", status="missing") == ["Ebook Only"]
        assert _titles(c, kind="audiobook", status="owned") == ["Audio Only", "Both"]
        assert _titles(c, kind="ebook", status="missing") == []
        # status alone is unchanged: any asset of the work in that state
        assert _titles(c, status="missing") == ["Ebook Only"]
        assert _titles(c, status="owned") == ["Audio Only", "Both", "Ebook Only"]


def test_total_follows_the_tightened_kind_filter(mixed_shelf):
    with TestClient(app) as c:
        r = c.get("/api/v1/library", params={"kind": "audiobook", "limit": 1}).json()
        assert r["total"] == 2 and len(r["items"]) == 1


# ---------- 2. progress -> read state precedence ----------
def _state(work_id, user_id):
    return db.one("SELECT status,source,started_at,finished_at FROM read_states WHERE work_id=? AND user_id=?", (work_id, user_id))


def _set_manual(c, work_id, status):
    assert c.put(f"/api/v1/library/{work_id}/read-state", json={"status": status}).status_code == 200


def _save(c, work_id, aid, percent=0.1):
    r = c.put(f"/api/v1/library/{work_id}/progress/{aid}", json={"location": f"epubcfi(/6/2!/4/1:{int(percent * 100)})", "percent": percent})
    assert r.status_code == 200, r.text


def test_first_progress_marks_an_untracked_book_reading(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        assert _titles(c, read_status="reading") == []
        _save(c, work_id, aid)
        st = _state(work_id, "anonymous")
        assert st["status"] == "reading" and st["source"] == "auto" and st["started_at"] is not None
        # This is the complaint: the dropdown filter now finds a book that has progress.
        assert _titles(c, read_status="reading") == ["Readable Book"]
        assert c.get(f"/api/v1/library/{work_id}").json()["read_status"] == "reading"


def test_want_to_read_becomes_reading_when_opened(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _set_manual(c, work_id, "want_to_read")
        _save(c, work_id, aid)
        assert _state(work_id, "anonymous")["status"] == "reading"
        assert _titles(c, read_status="reading") == ["Readable Book"]
        assert _titles(c, read_status="want_to_read") == []


@pytest.mark.parametrize("manual", ["finished", "dnf", "reading"])
def test_a_hand_picked_status_is_never_overridden_by_opening_the_book(tmp_path, manual):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _set_manual(c, work_id, manual)
        before = _state(work_id, "anonymous")
        _save(c, work_id, aid)
        _save(c, work_id, aid, 0.5)  # and later saves do not either
        after = _state(work_id, "anonymous")
        assert after["status"] == manual and after["source"] == "manual"
        assert after["finished_at"] == before["finished_at"]


def test_a_manual_change_after_auto_marking_sticks(tmp_path):
    """Reader marks it reading, person marks it finished, reader keeps saving: stays finished."""
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _save(c, work_id, aid)
        _set_manual(c, work_id, "finished")
        _save(c, work_id, aid, 0.99)
        st = _state(work_id, "anonymous")
        assert st["status"] == "finished" and st["source"] == "manual"
        # Clearing progress leaves the manual decision alone too.
        assert c.delete(f"/api/v1/library/{work_id}/progress/{aid}").status_code == 200
        assert _state(work_id, "anonymous")["status"] == "finished"


def test_the_dropdown_writes_manual_even_for_reading(tmp_path):
    work_id, _, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _set_manual(c, work_id, "reading")
        assert _state(work_id, "anonymous")["source"] == "manual"


def test_auto_marking_is_per_account(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    a = auth_module.create_local_user("alice", "a-very-long-password-1", is_admin=False)
    b = auth_module.create_local_user("bob", "a-very-long-password-2", is_admin=False)
    reader.set_progress(work_id, aid, a, file_id=None, position=None, location="epubcfi(/6/2!/4/1:0)", percent=0.1)
    assert _state(work_id, a)["status"] == "reading"
    assert _state(work_id, b) is None


# ---------- 3. clearing progress ----------
def test_clearing_progress_unwinds_an_automatic_reading_state(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _save(c, work_id, aid)
        assert _state(work_id, "anonymous")["source"] == "auto"
        r = c.delete(f"/api/v1/library/{work_id}/progress/{aid}")
        assert r.status_code == 200 and r.json()["progress"] is None
        assert _state(work_id, "anonymous") is None
        assert _titles(c, read_status="reading") == []
        assert c.get(f"/api/v1/library/{work_id}").json()["read_status"] is None


def test_clearing_progress_leaves_a_hand_picked_reading_alone(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _set_manual(c, work_id, "reading")
        _save(c, work_id, aid)
        c.delete(f"/api/v1/library/{work_id}/progress/{aid}")
        st = _state(work_id, "anonymous")
        assert st["status"] == "reading" and st["source"] == "manual"


def test_clearing_one_asset_keeps_reading_while_the_other_asset_has_a_position(tmp_path):
    """Halfway through the audiobook, clear the ebook's bookmark: still reading the work."""
    work_id, ebook_aid, _, _ = _epub_book(tmp_path)
    audio_aid = _asset(work_id, "audiobook", "m4b")
    with TestClient(app) as c:
        _save(c, work_id, ebook_aid)
        assert c.put(f"/api/v1/library/{work_id}/progress/{audio_aid}", json={"position": 120.0, "percent": 0.3}).status_code == 200
        c.delete(f"/api/v1/library/{work_id}/progress/{ebook_aid}")
        assert _state(work_id, "anonymous")["status"] == "reading"
        c.delete(f"/api/v1/library/{work_id}/progress/{audio_aid}")
        assert _state(work_id, "anonymous") is None


def test_clearing_progress_that_does_not_exist_is_harmless(tmp_path):
    work_id, aid, _, _ = _epub_book(tmp_path)
    with TestClient(app) as c:
        _set_manual(c, work_id, "reading")
        assert c.delete(f"/api/v1/library/{work_id}/progress/{aid}").status_code == 200
        assert _state(work_id, "anonymous")["status"] == "reading"


# ---------- 3b. the UI ----------
def test_asset_row_offers_clear_progress_only_when_there_is_progress():
    row = APP_JS[APP_JS.index("function assetRow("):APP_JS.index("function openClearProgress(")]
    assert "const clearButton = prog ? `<button class=\"button ghost compact asset-clear-progress\"" in row
    assert "${consumeButton}${clearButton}" in row, "Clear progress sits right next to Resume"


def test_clear_progress_button_confirms_before_calling_the_delete_route():
    fn = APP_JS[APP_JS.index("function openClearProgress("):APP_JS.index("async function clearProgress(")]
    assert "modal('Clear progress'" in fn
    assert 'id="clear-progress-confirm"' in fn and "modal-cancel" in fn
    assert "await clearProgress(workId, assetId)" in fn, "the DELETE only runs from the confirm button's handler"
    # ... and the detail page wires the button to the confirming opener, then re-renders the row.
    assert "$$('.asset-clear-progress').forEach(b=>b.onclick=()=>openClearProgress(id,b.dataset.asset,b.dataset.kind,()=>renderWork(id)))" in APP_JS


@needs_node
def test_clear_progress_handler_deletes_the_position_over_real_http(tmp_path, live):
    work_id, aid, _, _ = _epub_book(tmp_path)
    auth_module.ensure_anonymous_user()
    reader.set_progress(work_id, aid, "anonymous", file_id=None, position=None, location="epubcfi(/6/2!/4/1:0)", percent=0.25)
    assert _state(work_id, "anonymous")["status"] == "reading"
    out = run_ui(f"return await clearProgress({work_id!r}, {aid!r});", live)
    assert out == {"asset_id": aid, "progress": None}
    assert reader.get_progress(aid, "anonymous") is None
    assert _state(work_id, "anonymous") is None
