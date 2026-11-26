"""Component tests for the podcast show page helpers in static/app.js (evaluated with node, fixture JSON, no network).

The fixtures match the GET /api/v1/podcasts/{id} response shape from #514."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

ESC = "const e=(v='')=>String(v??'').replace(/[&<>'\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',\"'\":'&#39;','\"':'&quot;'}[c]));"


def _ep(i, title, desc="", played=False, dur=3725):
    return {"id": i, "title": title, "release_date": "2024-01-02", "duration_seconds": dur,
            "description": desc, "played": played, "position_seconds": 0}


SHOW = {
    "id": "sh1", "title": "Alpha <Show>", "cover_url": "/api/v1/podcasts/sh1/cover",
    "seasons": [
        {"id": "x", "number": None, "is_extras": True, "episodes": [_ep("e9", "Bonus")]},
        {"id": "s10", "number": 10, "is_extras": False, "episodes": [_ep("e10", "Ten")]},
        {"id": "s2", "number": 2, "is_extras": False, "episodes": [_ep("e2", "Two", "About two", True), _ep("e3", "Three", "", False)]},
        {"id": "s1", "number": 1, "is_extras": False, "episodes": [_ep("e1", "One")]},
    ],
}


def _run(expr: str, show=SHOW):
    src = APP_JS.read_text()
    block = re.search(r"// <podcast-show-helpers>.*?// </podcast-show-helpers>", src, re.S)
    assert block, "helper block markers are missing from app.js"
    prog = f"{ESC}\n{block.group(0)}\nconst SHOW={json.dumps(show)};\nconsole.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_cover_shown_when_present():
    html = _run("podCoverHtml(SHOW,e)")
    assert '<img class="pod-cover"' in html and 'src="/api/v1/podcasts/sh1/cover"' in html
    assert "pod-cover-ph" not in html


def test_placeholder_when_cover_absent():
    html = _run("podCoverHtml({title:'T',cover_url:null},e)")
    assert "pod-cover-ph" in html and "<img" not in html


def test_season_ordering_numeric_with_extras_last():
    assert _run("podSortSeasons(SHOW.seasons).map(s=>s.id)") == ["s1", "s2", "s10", "x"]


def test_season_selector_labels_in_order_extras_last():
    html = _run("podSelectorHtml(SHOW.seasons,'s1',e)")
    labels = re.findall(r">(Season \d+|Extras)</button>", html)
    assert labels == ["Season 1", "Season 2", "Season 10", "Extras"]
    assert 'data-season="s1" aria-pressed="true"' in html


def test_description_shown_when_present():
    html = _run("podEpisodeHtml(SHOW.seasons[2].episodes[0],e)")
    assert '<p class="pod-desc">About two</p>' in html


def test_description_hidden_when_empty():
    assert "pod-desc" not in _run("podEpisodeHtml(SHOW.seasons[2].episodes[1],e)")
    assert "pod-desc" not in _run("podEpisodeHtml({id:'a',title:'t',description:'   '},e)")


def test_played_state_matches_api_value():
    played = _run("podEpisodeHtml(SHOW.seasons[2].episodes[0],e)")
    unplayed = _run("podEpisodeHtml(SHOW.seasons[2].episodes[1],e)")
    assert 'data-played="true"' in played and ">Played<" in played
    assert 'data-played="false"' in unplayed and ">Unplayed<" in unplayed


def test_episode_shows_title_date_duration_and_escapes():
    html = _run("podEpisodeHtml(SHOW.seasons[2].episodes[0],e)")
    assert "Two" in html and "2024-01-02" in html and "1h 2m" in html
    assert "&lt;Show&gt;" in _run("podShowHtml(SHOW,'s1',e)")


def test_show_page_lists_selected_season_only_and_defaults_to_first():
    html = _run("podShowHtml(SHOW,'s2',e)")
    assert "Two" in html and "Three" in html and ">One<" not in html
    default = _run("podShowHtml(SHOW,undefined,e)")
    assert ">One<" in default and "Bonus" not in default


NUM_SHOW = {
    "id": 7, "title": "Numeric", "cover_url": None,
    "seasons": [
        {"id": 11, "number": 1, "is_extras": False, "episodes": [_ep(1, "First")]},
        {"id": 12, "number": 2, "is_extras": False, "episodes": [_ep(2, "Second")]},
    ],
}


def test_numeric_season_ids_selected_from_string_dataset_value():
    # button click passes dataset.season, always a string
    html = _run("podShowHtml(SHOW,'12',e)", show=NUM_SHOW)
    assert ">Second<" in html and ">First<" not in html
    assert 'data-season="12" aria-pressed="true"' in html
    assert 'data-season="11" aria-pressed="false"' in html
