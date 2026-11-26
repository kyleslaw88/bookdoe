"""Continue Listening row helpers in static/player.js (evaluated with node, no browser, no network)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PLAYER_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "player.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Fixture feed shaped like GET /api/player/v1/continue (issue #518): newest first.
FEED = [
    {"kind": "podcast", "episode_id": "ep1", "show_id": "s1", "show_title": "The Show", "title": "Pilot",
     "duration_seconds": 1000, "position_seconds": 250, "updated_at": 2000.0},
    {"kind": "audiobook", "work_id": "w1", "asset_id": "a1", "title": "Dune", "authors": "Frank Herbert",
     "percent": 0.4, "updated_at": 1000.0},
]


def _run(expr: str):
    src = PLAYER_JS.read_text()
    block = re.search(r"// <continue-row-helpers>.*?// </continue-row-helpers>", src, re.S)
    assert block, "helper block markers are missing from player.js"
    prelude = ("const e = v => String(v ?? '');\n"
               "const mk = id => ({id, paused: true, style: {}, classList: {remove() {}}, pause() { this.paused = true; }, play: async function () { this.paused = false; this.plays = (this.plays || 0) + 1; }});\n"
               "const doc = {body: {kids: [], appendChild(n) { this.kids.push(n); }}, getElementById(id) { return this.body.kids.find(k => k.id === id) || null; }, createElement() { return mk(''); }};\n"
               "globalThis.document = doc; const $ = sel => doc.getElementById(sel.slice(1)); const log = [];\n"
               "const api = async (path, opts) => { log.push([opts ? opts.method : 'GET', path]); return {progress: {position_seconds: 250, played: false}, episode: null}; };\n")
    out = subprocess.run(["node", "-e", f"{prelude}{block.group(0)}\nPromise.resolve({expr}).then(v => console.log(JSON.stringify(v)))"],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_row_interleaves_book_and_podcast_in_feed_order_with_episode_and_show():
    html = _run(f"continueListeningRow({json.dumps(FEED)}, b => '<book>' + b.title + '</book>')")
    assert html.index("Pilot") < html.index("<book>Dune</book>")
    assert "The Show" in html and 'data-play-episode="ep1"' in html


def test_row_is_hidden_when_feed_is_empty():
    assert _run("continueListeningRow([], () => 'x')") == ""


def test_tapping_a_podcast_entry_plays_that_episode_via_the_player():
    expr = ("(() => { const calls = []; const t = {closest: sel => sel === '[data-play-episode]' ? {dataset: {playEpisode: 'ep1'}} : null};"
            "const handled = playEpisodeFromTap(t, id => calls.push(id)); return {handled, calls}; })()")
    assert _run(expr) == {"handled": True, "calls": ["ep1"]}


def test_tapping_a_book_does_not_start_an_episode():
    expr = "(() => { const calls = []; const handled = playEpisodeFromTap({closest: () => null}, id => calls.push(id)); return {handled, calls}; })()"
    assert _run(expr) == {"handled": False, "calls": []}


def test_tap_hands_the_episode_to_the_existing_player_which_plays_and_resumes(tmp_path):
    """Tap -> playEpisode (the show page's player path) with a mocked <audio>; the src is the existing /audio route."""
    expr = ("(async () => { const tap = {closest: () => ({dataset: {playEpisode: 'ep1'}})}; let started;"
            "playEpisodeFromTap(tap, id => { started = playEpisode(id, []); }); await started;"
            "const a = doc.getElementById('pod-audio-shared');"
            "return {src: a.src, resumed: a.currentTime, plays: a.plays, log}; })()")
    out = _run(expr)
    assert out["src"] == "/api/player/v1/podcasts/episodes/ep1/audio"
    assert out["resumed"] == 250 and out["plays"] == 1
    assert ["GET", "/api/player/v1/podcasts/episodes/ep1/progress"] in out["log"]


def test_second_tap_reuses_one_body_level_element_and_a_show_page_player_pauses_it():
    expr = ("(async () => { await playEpisode('ep1', []); await playEpisode('ep2', []);"
            "const shared = doc.getElementById('pod-audio-shared'); const count = doc.body.kids.length; const playing = doc.body.kids.filter(k => !k.paused).length;"
            "const own = mk('pod-audio'); doc.body.appendChild(own); const chosen = podAudio(doc);"
            "return {count, src: shared.src, playing, ownChosen: chosen === own, sharedPaused: shared.paused}; })()")
    out = _run(expr)
    assert out["count"] == 1 and out["src"].endswith("/episodes/ep2/audio") and out["playing"] == 1
    assert out["ownChosen"] and out["sharedPaused"]
