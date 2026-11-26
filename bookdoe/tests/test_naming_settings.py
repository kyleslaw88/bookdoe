"""Tests for customisable naming and the import transfer-mode setting.

Requested 2026-09-24 after Gate 7: make copy the deliberate default rather than a
silent fallback, and make folder/file naming customisable and previewable.

Also covers two latent defects found while implementing it:
  * `replace_illegal` and `colon_replacement` were stored in naming_templates but
    never read by the renderer - they were dead configuration;
  * the naming UI form omitted both, so every save silently reset them to defaults.
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import importer

client = TestClient(app)


# ---------- transfer mode ----------

def test_default_transfer_mode_is_copy():
    assert importer.DEFAULT_TRANSFER_MODE == "copy"
    assert importer.configured_transfer_mode() == "copy"


def test_transfer_mode_is_configurable_and_persists():
    r = client.put("/api/v1/settings/import", json={"transfer_mode": "hardlink"})
    assert r.status_code == 200, r.text
    assert r.json()["transfer_mode"] == "hardlink"
    assert importer.configured_transfer_mode() == "hardlink"
    assert client.get("/api/v1/settings/import").json()["transfer_mode"] == "hardlink"


def test_invalid_transfer_mode_is_rejected():
    assert client.put("/api/v1/settings/import", json={"transfer_mode": "teleport"}).status_code == 422


def test_corrupt_stored_mode_falls_back_to_copy():
    db.set_setting("import.transfer_mode", "nonsense")
    assert importer.configured_transfer_mode() == "copy"


def test_settings_helpers_round_trip():
    db.set_setting("x.y", {"a": 1})
    assert db.get_setting("x.y") == {"a": 1}
    assert db.get_setting("missing.key", "fallback") == "fallback"


# ---------- naming options actually take effect ----------

def test_colon_replacement_is_honoured():
    ctx = {"Author": "A", "Title": "Dune: Messiah", "Year": "", "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}
    r = lambda c: importer._render("{Title}.{Ext}", ctx, ext="epub", part=None, colon=c)
    assert r("smart") == "Dune - Messiah.epub"
    assert r("dash") == "Dune-Messiah.epub"
    assert r("space") == "Dune Messiah.epub"
    assert r("remove") == "DuneMessiah.epub"
    # A colon never survives into a filename whatever the policy.
    assert all(":" not in r(c) for c in ("smart", "dash", "space", "remove"))


def test_replace_illegal_toggle_is_honoured():
    ctx = {"Author": "A", "Title": 'What?* "Now"', "Year": "", "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}
    stripped = importer._render("{Title}.{Ext}", ctx, ext="epub", part=None, replace_illegal=True)
    kept = importer._render("{Title}.{Ext}", ctx, ext="epub", part=None, replace_illegal=False)
    assert "?" not in stripped and "*" not in stripped
    assert "?" in kept
    # Path separators are never negotiable.
    sep = importer._render("{Title}.{Ext}", {**ctx, "Title": "a/b"}, ext="epub", part=None, replace_illegal=False)
    assert "/" not in sep


def test_saving_naming_preserves_the_two_option_fields():
    r = client.put("/api/v1/naming/audiobook", json={
        "folder_template": "{Author}/{Series}/{Title}",
        "file_template": "{Title} - {Year}.{Ext}",
        "multipart_template": "{Title} - {Part:02}.{Ext}",
        "replace_illegal": False,
        "colon_replacement": "dash",
    })
    assert r.status_code == 200, r.text
    row = db.one("SELECT * FROM naming_templates WHERE media_kind='audiobook'")
    assert row["colon_replacement"] == "dash"
    assert row["replace_illegal"] == 0
    assert row["folder_template"] == "{Author}/{Series}/{Title}"


# ---------- preview ----------

def test_preview_works_without_a_work_id():
    """The settings screen must be able to preview before any work is chosen."""
    r = client.get("/api/v1/naming/audiobook/preview")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["sample"] is True
    assert b["folder"] and b["files"]
    assert "without_optional_fields" in b


def test_preview_shows_the_absent_optional_field_case():
    b = client.get("/api/v1/naming/audiobook/preview").json()
    bare = b["without_optional_fields"]
    assert "Unknown" not in bare["files"][0], "absent tokens must vanish, not read 'Unknown'"


def test_preview_accepts_unsaved_template_edits():
    before = db.one("SELECT folder_template FROM naming_templates WHERE media_kind='ebook'")["folder_template"]
    r = client.get("/api/v1/naming/ebook/preview", params={"folder_template": "{Series}/{Author}", "ext": "epub"})
    assert r.status_code == 200
    assert r.json()["folder"] == "Expeditionary Force/Craig Alanson"
    # Previewing must not persist anything.
    assert db.one("SELECT folder_template FROM naming_templates WHERE media_kind='ebook'")["folder_template"] == before


def test_preview_renders_multipart_when_parts_requested():
    b = client.get("/api/v1/naming/audiobook/preview", params={"parts": 3}).json()
    assert len(b["files"]) == 3
    assert b["files"][0] != b["files"][1]


def test_preview_with_a_real_work_is_not_flagged_as_sample():
    wid, cid, t = db.uid(), db.uid(), db.now()
    with db.tx() as c:
        c.execute("INSERT INTO works(id,title,sort_title,metadata_status,added_at,updated_at) VALUES (?,?,?,'resolved',?,?)", (wid, "Real Book", "Real Book", t, t))
        c.execute("INSERT INTO contributors(id,name,sort_name,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, "Real Author", "Real Author", t, t))
        c.execute("INSERT INTO work_contributors VALUES (?,?, 'author',0)", (wid, cid))
    b = client.get("/api/v1/naming/ebook/preview", params={"work_id": wid, "ext": "epub"}).json()
    assert b["sample"] is False
    assert "Real Book" in b["files"][0]


def test_custom_folder_template_with_series_nesting():
    b = client.get("/api/v1/naming/audiobook/preview", params={"folder_template": "{Author}/{Series}/{SeriesPosition} - {Title}"}).json()
    assert b["folder"] == "Craig Alanson/Expeditionary Force/15 - Dead World"


# ---------- per-component separator cleanup ----------

FULL = {"Author": "Craig Alanson", "Title": "Dead World", "Year": "2024",
        "Series": "Expeditionary Force", "SeriesPosition": "15",
        "SeriesPrefix": "Expeditionary Force 15 - "}
BARE = {**FULL, "Year": "", "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}


def test_series_nesting_collapses_cleanly_when_there_is_no_series():
    """An empty middle component must vanish AND not leave a leading separator."""
    tpl = "{Author}/{Series}/{SeriesPosition} - {Title}"
    assert importer._render(tpl, FULL, ext="", part=None) == "Craig Alanson/Expeditionary Force/15 - Dead World"
    got = importer._render(tpl, BARE, ext="", part=None)
    assert got == "Craig Alanson/Dead World", got
    assert "/-" not in got and not got.split("/")[-1].startswith("-")


def test_no_empty_directory_components_are_produced():
    out = importer._render("{Author}/{Series}/{Title}", BARE, ext="", part=None)
    assert "//" not in out
    assert all(p.strip() for p in out.split("/"))


def test_trailing_separator_before_extension_is_removed_per_component():
    assert importer._render("{Title} - {Year}.{Ext}", BARE, ext="epub", part=None) == "Dead World.epub"


def test_multipart_keeps_its_number_without_series_or_year():
    assert importer._render("{SeriesPrefix}{Title} - {Part:02} - {Year}.{Ext}", BARE, ext="mp3", part=2) == "Dead World - 02.mp3"


def test_path_traversal_tokens_are_dropped():
    ctx = {**BARE, "Title": "..", "Author": "."}
    out = importer._render("{Author}/{Title}", ctx, ext="", part=None)
    assert ".." not in out
