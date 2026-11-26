"""Regression tests for naming when optional fields are absent.

Found during the 2026-09-24 Unraid deployment at Gate 7. The first successful
managed import produced "UnknownDead World - Unknown.m4b": _safe_component applied
its `or "Unknown"` fallback to every *field* substitution, not just to a whole empty
path component, so an absent series prefix and an absent publication year each
rendered as the literal word "Unknown". Migrated works routinely have neither.
"""
import pytest

from app.services import importer

BASE = {"Author": "Craig Alanson", "Title": "Dead World", "Year": "",
        "Series": "", "SeriesPosition": "", "SeriesPrefix": ""}
AUDIO = "{SeriesPrefix}{Title} - {Year}.{Ext}"
MULTI = "{SeriesPrefix}{Title} - {Part:02} - {Year}.{Ext}"
EBOOK = "{Title} - {Year}.{Ext}"


def r(tpl, ctx=None, ext="m4b", part=None):
    return importer._render(tpl, {**BASE, **(ctx or {})}, ext=ext, part=part)


def test_absent_optional_fields_do_not_become_the_word_unknown():
    out = r(AUDIO)
    assert out == "Dead World.m4b"
    assert "Unknown" not in out


def test_no_dangling_separator_before_the_extension():
    assert r(AUDIO) == "Dead World.m4b"
    assert " - ." not in r(AUDIO)


def test_series_prefix_keeps_its_separator_spacing():
    out = r(AUDIO, {"Year": "2024", "SeriesPrefix": "Expeditionary Force 15 - "})
    assert out == "Expeditionary Force 15 - Dead World - 2024.m4b"
    assert "-Dead" not in out, "the prefix separator must not fuse onto the title"


def test_year_present_series_absent():
    assert r(AUDIO, {"Year": "2024"}) == "Dead World - 2024.m4b"


def test_series_present_year_absent():
    assert r(AUDIO, {"SeriesPrefix": "ExForce 15 - "}) == "ExForce 15 - Dead World.m4b"


def test_multipart_numbering_survives_absent_year():
    assert r(MULTI, ext="mp3", part=3) == "Dead World - 03.mp3"


def test_hyphenated_titles_are_not_mangled():
    assert r(AUDIO, {"Title": "Winnie-the-Pooh"}) == "Winnie-the-Pooh.m4b"


def test_ebook_template_without_year():
    assert r(EBOOK, ext="epub") == "Dead World.epub"


def test_folder_template_is_unaffected():
    assert r("{Author}/{Title}", ext="") == "Craig Alanson/Dead World"


def test_a_wholly_empty_component_still_falls_back():
    """The fallback is still correct at component level - just not per field."""
    assert importer._safe_component("") == "Unknown"
    assert importer._safe_component("", fallback="") == ""


def test_render_never_returns_an_empty_name():
    assert r("{Series}{Year}", ext="") == "Unknown"


@pytest.mark.parametrize("title", ["A/B", "C:D", 'E"F', "G|H"])
def test_unsafe_characters_are_still_sanitised(title):
    out = r(AUDIO, {"Title": title})
    for ch in '/\\:<>"|?*':
        assert ch not in out.replace(".m4b", "")
