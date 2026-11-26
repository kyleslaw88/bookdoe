"""GET /api/v1/scan-candidates - read-only visibility into scan candidates.

Candidates already carry `confidence` and `reasons_json` once a scan runs, but
there was no way to read them back short of adopting blind. These tests cover
the list endpoint (filterable by status/scan_run_id, paginated like
/api/v1/library and /api/v1/wanted) and the detail endpoint (decoded JSON).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db
from app.main import app


def _seed_run() -> str:
    rid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO scan_runs(id,status,started_at,finished_at) VALUES (?,?,?,?)",
            (rid, "done", t, t),
        )
    return rid


def _seed_candidate(scan_run_id: str, status: str, path: str, confidence: float = 0.5) -> str:
    cid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            """INSERT INTO scan_candidates(id,scan_run_id,path,media_kind,detected_json,confidence,status,reasons_json,created_at)
               VALUES (?,?,?,'ebook',?,?,?,?,?)""",
            (cid, scan_run_id, path, '{"title":"Dead World","authors":["Craig Alanson"]}', confidence, status, '["title exact match"]', t),
        )
    return cid


def test_list_filters_by_status_and_reports_paging_envelope():
    rid = _seed_run()
    ambiguous_id = _seed_candidate(rid, "ambiguous", "/books/a.epub")
    _seed_candidate(rid, "matched", "/books/b.epub")
    _seed_candidate(rid, "unmatched", "/books/c.epub")
    with TestClient(app) as c:
        body = c.get("/api/v1/scan-candidates?status=ambiguous").json()
    assert body["total"] == 1
    assert [x["id"] for x in body["items"]] == [ambiguous_id]
    assert body["limit"] == 100 and body["offset"] == 0


def test_list_accepts_each_valid_status():
    rid = _seed_run()
    for status in ("matched", "probable", "ambiguous", "unmatched", "ignored"):
        _seed_candidate(rid, status, f"/books/{status}.epub")
    with TestClient(app) as c:
        for status in ("matched", "probable", "ambiguous", "unmatched", "ignored"):
            body = c.get(f"/api/v1/scan-candidates?status={status}").json()
            assert body["total"] == 1
            assert body["items"][0]["status"] == status


def test_list_rejects_unknown_status_with_400():
    rid = _seed_run()
    _seed_candidate(rid, "matched", "/books/a.epub")
    with TestClient(app) as c:
        resp = c.get("/api/v1/scan-candidates?status=bogus")
    assert resp.status_code == 400


def test_list_filters_by_scan_run_id():
    rid_a = _seed_run()
    rid_b = _seed_run()
    _seed_candidate(rid_a, "ambiguous", "/books/a.epub")
    _seed_candidate(rid_b, "ambiguous", "/books/b.epub")
    with TestClient(app) as c:
        body = c.get(f"/api/v1/scan-candidates?scan_run_id={rid_a}").json()
    assert body["total"] == 1
    assert body["items"][0]["scan_run_id"] == rid_a


def test_list_is_paginated_like_library_and_wanted():
    rid = _seed_run()
    for i in range(5):
        _seed_candidate(rid, "unmatched", f"/books/{i:02d}.epub")
    with TestClient(app) as c:
        body = c.get("/api/v1/scan-candidates?limit=2").json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 0


def test_detail_decodes_reasons_and_detected_as_real_json():
    rid = _seed_run()
    cid = _seed_candidate(rid, "probable", "/books/a.epub")
    with TestClient(app) as c:
        body = c.get(f"/api/v1/scan-candidates/{cid}").json()
    assert body["reasons"] == ["title exact match"]
    assert body["detected"] == {"title": "Dead World", "authors": ["Craig Alanson"]}
    assert "reasons_json" not in body and "detected_json" not in body


def test_detail_unknown_id_is_404():
    with TestClient(app) as c:
        resp = c.get("/api/v1/scan-candidates/does-not-exist")
    assert resp.status_code == 404


def test_scan_result_filters_by_status():
    rid = _seed_run()
    ambiguous_id = _seed_candidate(rid, "ambiguous", "/books/a.epub")
    _seed_candidate(rid, "matched", "/books/b.epub")
    with TestClient(app) as c:
        body = c.get(f"/api/v1/scans/{rid}?status=ambiguous").json()
    assert [x["id"] for x in body["items"]] == [ambiguous_id]


def test_scan_result_rejects_unknown_status_with_400():
    rid = _seed_run()
    _seed_candidate(rid, "matched", "/books/a.epub")
    with TestClient(app) as c:
        resp = c.get(f"/api/v1/scans/{rid}?status=bogus")
    assert resp.status_code == 400


def test_adopt_endpoint_behavior_is_unchanged():
    """Guards against the new read routes accidentally shadowing /adopt's route."""
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)",
            (wid, "Test Book", "Test Book", t, t),
        )
    rid = _seed_run()
    cid = _seed_candidate(rid, "unmatched", "/books/a.epub")
    with TestClient(app) as c:
        resp = c.post(f"/api/v1/scan-candidates/{cid}/adopt", json={"work_id": wid})
    assert resp.status_code == 200
    assert "asset_id" in resp.json()


# ---------- P4-01: edit / ignore ----------

def test_edit_then_adopt_uses_the_edited_values_not_the_original_detected_ones():
    wid, t = db.uid(), db.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)",
            (wid, "Test Book", "Test Book", t, t),
        )
    rid = _seed_run()
    cid = _seed_candidate(rid, "unmatched", "/books/a.epub")
    with TestClient(app) as c:
        edit_resp = c.patch(
            f"/api/v1/scan-candidates/{cid}",
            json={"title": "Corrected Title", "authors": ["Corrected Author"], "series": "Corrected Series"},
        )
        assert edit_resp.status_code == 200
        assert edit_resp.json()["detected"]["title"] == "Corrected Title"
        assert edit_resp.json()["detected"]["authors"] == ["Corrected Author"]
        assert edit_resp.json()["detected"]["series"] == "Corrected Series"

        adopt_resp = c.post(f"/api/v1/scan-candidates/{cid}/adopt", json={"work_id": wid})
    assert adopt_resp.status_code == 200

    detail = db.one("SELECT detected_json FROM scan_candidates WHERE id=?", (cid,))
    detected = db.json_load(detail["detected_json"], {})
    assert detected["title"] == "Corrected Title"
    assert detected["authors"] == ["Corrected Author"]
    assert detected["series"] == "Corrected Series"
    # The original detected field this replaced must be gone, not just shadowed.
    assert detected.get("authors") != ["Craig Alanson"]


def test_edit_is_a_partial_merge_not_a_full_replace():
    rid = _seed_run()
    cid = _seed_candidate(rid, "unmatched", "/books/a.epub")
    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/scan-candidates/{cid}", json={"series_position": "3"})
    assert resp.status_code == 200
    detected = resp.json()["detected"]
    assert detected["series_position"] == "3"
    assert detected["title"] == "Dead World"  # untouched, still the original seed value
    assert detected["authors"] == ["Craig Alanson"]  # untouched


def test_edit_endpoint_does_not_expose_non_editable_fields_as_writable():
    """The route-level ScanCandidateEditRequest model only declares the five
    allow-listed fields, so an unrecognized key (e.g. match_work_id, confidence)
    is silently dropped by Pydantic before it ever reaches the service layer --
    confirm it never lands in detected_json either way. The service layer's own
    explicit allow-list guard (unreachable from here, since the Pydantic model
    already only offers the allowed keys) is covered directly in test_scanner.py.
    """
    rid = _seed_run()
    cid = _seed_candidate(rid, "unmatched", "/books/a.epub")
    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/scan-candidates/{cid}", json={"match_work_id": "sneaky", "confidence": 0.99})
    assert resp.status_code == 200
    detail = db.one("SELECT detected_json FROM scan_candidates WHERE id=?", (cid,))
    decoded = db.json_load(detail["detected_json"], {})
    assert "match_work_id" not in decoded and "confidence" not in decoded


def test_edit_unknown_id_is_400():
    with TestClient(app) as c:
        resp = c.patch("/api/v1/scan-candidates/does-not-exist", json={"title": "X"})
    assert resp.status_code == 400


def test_ignore_sets_status_and_is_excluded_from_default_list_and_scan_result():
    rid = _seed_run()
    ignored_id = _seed_candidate(rid, "ambiguous", "/books/a.epub")
    visible_id = _seed_candidate(rid, "unmatched", "/books/b.epub")
    with TestClient(app) as c:
        ignore_resp = c.post(f"/api/v1/scan-candidates/{ignored_id}/ignore")
        assert ignore_resp.status_code == 200
        assert ignore_resp.json() == {"ok": True}

        list_body = c.get("/api/v1/scan-candidates").json()
        assert [x["id"] for x in list_body["items"]] == [visible_id]
        assert list_body["total"] == 1

        scan_body = c.get(f"/api/v1/scans/{rid}").json()
        assert [x["id"] for x in scan_body["items"]] == [visible_id]

        # Still reachable by explicit status filter and by direct id.
        explicit_body = c.get("/api/v1/scan-candidates?status=ignored").json()
        assert [x["id"] for x in explicit_body["items"]] == [ignored_id]
        detail = c.get(f"/api/v1/scan-candidates/{ignored_id}").json()
        assert detail["status"] == "ignored"


def test_ignore_unknown_id_is_400():
    with TestClient(app) as c:
        resp = c.post("/api/v1/scan-candidates/does-not-exist/ignore")
    assert resp.status_code == 400
