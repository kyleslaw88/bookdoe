"""Tests for preferred-language scoring.

Gap found during the 2026-09-24 Unraid deployment (Gate 6): release titles were
parsed for language but nothing consumed it, so a Dutch and an Italian "Project Hail
Mary" M4B scored 150 -- identical to the English releases. Harmless while a human
picks the release; a real risk once automatic Wanted search grabs the top result.
"""
import pytest

from app import db
from app.services import releases


def _work(title="Project Hail Mary", author="Andy Weir"):
    wid, cid, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid, title, title, t, t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, author, author, t, t))
        c.execute("INSERT INTO work_contributors VALUES (?,?, 'author',0)", (wid, cid))
    return wid


def _score(wid, title, profile, kind="audiobook", size=900 * 1024 * 1024):
    return releases.score_release(wid, kind, {"title": title, "size": size, "protocol": "torrent", "seeders": 50}, profile)


PREFER_ENG = {"allowed_formats": ["m4b", "mp3"], "preferred_formats": ["m4b", "mp3"], "languages": ["eng"], "language_policy": "prefer"}
REJECT_ENG = {**PREFER_ENG, "language_policy": "reject"}
IGNORE = {**PREFER_ENG, "language_policy": "ignore"}


@pytest.mark.parametrize("tag,code", [
    ("[ENG / M4B]", "eng"), ("[DUT / M4B]", "dut"), ("[ITA / M4B]", "ita"),
    ("[TUR / M4B]", "tur"), ("[FIN / M4B]", "fin"), ("[GER / M4B]", "ger"),
    ("(Spanish) M4B", "spa"), ("Japanese M4B", "jpn"),
])
def test_language_is_detected_from_release_titles(tag, code):
    p = releases.parse_release(f"Project Hail Mary by Andy Weir {tag}", "audiobook")
    assert p["language"] == code


def test_untagged_release_reports_no_language():
    p = releases.parse_release("The Martian by Andy Weir 2014 Retail EPUB eBook-BitBook", "ebook")
    assert p["language"] is None


def test_preferred_language_outranks_a_foreign_release():
    wid = _work()
    eng, _, eng_rej, _ = _score(wid, "Project Hail Mary by Andy Weir [ENG / M4B]", PREFER_ENG)
    dut, _, dut_rej, _ = _score(wid, "Project Hail Mary by Andy Weir [DUT / M4B]", PREFER_ENG)
    assert not eng_rej and not dut_rej, "prefer policy must not reject"
    assert eng > dut, f"English ({eng}) must outrank Dutch ({dut})"
    # The exact regression: they used to tie.
    assert eng - dut >= 50


def test_untagged_release_is_not_penalised():
    """Most English releases carry no language tag; they must not be punished."""
    wid = _work()
    untagged, comps, rej, _ = _score(wid, "Project Hail Mary by Andy Weir M4B", PREFER_ENG)
    foreign, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [ITA / M4B]", PREFER_ENG)
    assert not rej
    assert untagged > foreign
    assert not any(c["score"] < 0 and "language" in c["label"].lower() for c in comps)


def test_reject_policy_hard_rejects_foreign_releases():
    wid = _work()
    _, _, eng_rej, _ = _score(wid, "Project Hail Mary by Andy Weir [ENG / M4B]", REJECT_ENG)
    _, _, dut_rej, _ = _score(wid, "Project Hail Mary by Andy Weir [DUT / M4B]", REJECT_ENG)
    assert eng_rej == []
    assert any("dut" in r for r in dut_rej), dut_rej


def test_reject_policy_still_allows_untagged_releases():
    wid = _work()
    _, _, rej, _ = _score(wid, "Project Hail Mary by Andy Weir M4B", REJECT_ENG)
    assert rej == [], "an untagged release must not be rejected as foreign"


def test_ignore_policy_restores_the_old_tie():
    wid = _work()
    eng, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [ENG / M4B]", IGNORE)
    dut, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [DUT / M4B]", IGNORE)
    assert eng == dut


def test_multiple_preferred_languages_are_honoured():
    wid = _work()
    cfg = {**PREFER_ENG, "languages": ["eng", "ger"]}
    ger, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [GER / M4B]", cfg)
    ita, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [ITA / M4B]", cfg)
    assert ger > ita


def test_language_names_are_normalised_to_codes():
    assert releases.normalize_language("English") == "eng"
    assert releases.normalize_language("nederlands") == "dut"
    assert releases.normalize_language("Deutsch") == "ger"
    assert releases.normalize_language("eng") == "eng"
    assert releases.normalize_language("klingon") is None


def test_short_codes_do_not_false_positive_inside_words():
    """'ita' must not match inside 'capital', etc."""
    assert releases.detect_language("the capital hospital ebook") is None
    assert releases.detect_language("a german tragedy") == "ger"


# ---- the selector itself ----
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _default(kind="audiobook"):
    return db.one("SELECT * FROM profiles WHERE media_kind=? AND is_default=1", (kind,))


def test_profiles_are_listable_with_known_languages():
    r = client.get("/api/v1/profiles")
    assert r.status_code == 200
    body = r.json()
    assert body["items"], "default profiles should exist"
    assert "eng" in body["known_languages"] and "ger" in body["known_languages"]
    cfg = body["items"][0]["config"]
    assert "languages" in cfg


def test_preferred_language_is_selectable_and_persists():
    pid = _default()["id"]
    r = client.put(f"/api/v1/profiles/{pid}", json={"languages": ["English", "ger"], "language_policy": "reject"})
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["languages"] == ["eng", "ger"], "names must normalise to codes"
    assert cfg["language_policy"] == "reject"
    stored = db.json_load(db.one("SELECT config_json FROM profiles WHERE id=?", (pid,))["config_json"], {})
    assert stored["languages"] == ["eng", "ger"]


def test_selected_language_actually_changes_scoring():
    pid = _default()["id"]
    wid = _work()
    client.put(f"/api/v1/profiles/{pid}", json={"languages": ["ger"], "language_policy": "prefer"})
    cfg = db.json_load(db.one("SELECT config_json FROM profiles WHERE id=?", (pid,))["config_json"], {})
    ger, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [GER / M4B]", cfg)
    eng, _, _, _ = _score(wid, "Project Hail Mary by Andy Weir [ENG / M4B]", cfg)
    assert ger > eng, "after selecting German, German must outrank English"


def test_unknown_language_is_rejected_by_the_api():
    pid = _default()["id"]
    r = client.put(f"/api/v1/profiles/{pid}", json={"languages": ["klingon"]})
    assert r.status_code == 400


def test_unknown_profile_returns_404():
    assert client.put("/api/v1/profiles/nope", json={"languages": ["eng"]}).status_code == 404
