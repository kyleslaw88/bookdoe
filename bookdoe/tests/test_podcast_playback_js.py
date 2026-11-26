"""Web playback of podcast episodes (issue #519): consume.js openPodcast() driving the shared audiobook player, run
under node with a fake DOM, a fake <audio>, a mocked API and a virtual clock (no network, no real sleeps).

The show page's Play wiring is checked against the helper block in app.js, as in test_podcast_show_js.py."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r"""
const store = new Map();
globalThis.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
globalThis.crypto ??= { randomUUID: () => '00000000-test-device' };
// Virtual clock: the saver's timers and Date.now follow it, so nothing here really waits.
let T = 1_000_000_000_000, TID = 0; const timers = new Map();
globalThis.setTimeout = (fn, ms) => { const k = ++TID; timers.set(k, { at: T + Math.max(0, ms || 0), fn }); return k; };
globalThis.clearTimeout = k => timers.delete(k);
Date.now = () => T;
const tick = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
async function advance(ms) {
  const end = T + ms;
  for (;;) {
    let next = null; for (const [k, v] of timers) if (v.at <= end && (!next || v.at < next[1].at)) next = [k, v];
    if (!next) break;
    timers.delete(next[0]); T = Math.max(T, next[1].at); next[1].fn(); await tick();
  }
  T = end; await tick();
}
class FakeAudio extends EventTarget {
  constructor() { super(); Object.assign(this, { src: '', currentTime: 0, duration: NaN, readyState: 0, paused: true, playbackRate: 1, error: null, style: {}, plays: 0 }); }
  load() { this.readyState = 0; }
  play() { this.paused = false; this.plays++; this.dispatchEvent(new Event('play')); return Promise.resolve(); }
  pause() { if (!this.paused) { this.paused = true; this.dispatchEvent(new Event('pause')); } }
  removeAttribute(n) { if (n === 'src') this.src = ''; }
  // What the browser does once the file's headers are in.
  metadata(duration) { this.duration = duration; this.readyState = 4; this.dispatchEvent(new Event('loadedmetadata')); }
}
const stub = () => ({ dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, addEventListener() {}, removeEventListener() {}, value: '0', textContent: '', innerHTML: '', hidden: false, disabled: false });
function makeHost() {
  const els = new Map();
  return { isConnected: true, _html: '', set innerHTML(v) { this._html = v; els.clear(); }, get innerHTML() { return this._html; },
    querySelector(sel) { if (!els.has(sel)) els.set(sel, stub()); return els.get(sel); }, querySelectorAll() { return []; }, el: sel => { if (!els.has(sel)) els.set(sel, stub()); return els.get(sel); } };
}
const audios = [];
globalThis.document = {
  createElement: t => { if (t === 'audio') { const a = new FakeAudio(); audios.push(a); return a; } return { dataset: {}, set onload(f) { this._l = f; }, get onload() { return this._l; } }; },
  head: { appendChild(n) { queueMicrotask(() => n.onload && n.onload()); } }, body: { appendChild() {} },
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
};
globalThis.window = { addEventListener() {} };
Object.defineProperty(globalThis, "navigator", { value: {}, configurable: true, writable: true });
globalThis.location = { hash: '' };
const mod = await import(process.argv[2]);

const ep = (id, title, dur = 600, played = false) => ({ id, title, release_date: '2024-01-01', duration_seconds: dur, description: '', played, position_seconds: 0 });
const SHOW = { id: 'sh1', title: 'Alpha Show', cover_url: '/api/v1/podcasts/sh1/cover', seasons: [
  { id: 's1', number: 1, is_extras: false, episodes: [ep('e1', 'One: Pilot'), ep('e1b', 'One: Finale')] },
  { id: 's2', number: 2, is_extras: false, episodes: [ep('e2', 'Two: Return')] } ] };
// Mocked API. `saved` maps episode id -> the GET .../progress body; `next` maps episode id -> the next episode (or null).
function mockApi({ saved = {}, next = {} } = {}) {
  const calls = [];
  const api = async (path, opts = {}) => {
    const method = opts.method || 'GET'; calls.push({ method, path, body: opts.body });
    let m;
    if ((m = path.match(/^\/api\/player\/v1\/podcasts\/episodes\/(\w+)\/progress$/))) {
      if (method === 'GET') return saved[m[1]] ?? { episode_id: m[1], progress: null };
      return { episode_id: m[1], applied: true, progress: { position_seconds: opts.body.position_seconds, played: false } };
    }
    if ((m = path.match(/^\/api\/player\/v1\/podcasts\/episodes\/(\w+)\/played$/))) return { played: true };
    if ((m = path.match(/^\/api\/player\/v1\/podcasts\/episodes\/(\w+)\/next$/))) return { episode: next[m[1]] ?? null };
    throw Object.assign(new Error('unexpected ' + path), { status: 404 });
  };
  api.calls = calls; return api;
}
const toasts = [];
const toast = m => toasts.push(m);
async function start(episodeId, apiOpts, extra = {}) {
  const api = mockApi(apiOpts), host = makeHost(), seen = [];
  const cleanup = await mod.openPodcast({ show: SHOW, episodeId, api, host, toast, chrome: 'management', onBack() {}, onEpisode: e => seen.push(e.id), ...extra });
  return { api, host, cleanup, seen, audio: audios[0] };
}
const out = {};
"""


def run(body: str):
    src = (STATIC / "consume.js").read_text()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "consume.mjs").write_text(src)
        (Path(tmp) / "t.mjs").write_text(PRELUDE + body + "\nconsole.log(JSON.stringify(out));")
        r = subprocess.run([NODE, str(Path(tmp) / "t.mjs"), str(Path(tmp) / "consume.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_play_action_loads_episode_audio_and_title_into_the_player():
    out = run("""
const s = await start('e1');
out.src = s.audio.src; out.html = s.host.innerHTML; out.seen = s.seen;
out.title = mod.current().m.title; out.show = mod.current().m.authors;
""")
    assert out["src"] == "/api/player/v1/podcasts/episodes/e1/audio"
    assert "<h1>One: Pilot</h1>" in out["html"] and "Alpha Show" in out["html"]
    assert out["title"] == "One: Pilot" and out["show"] == "Alpha Show"
    assert out["seen"] == ["e1"]


def test_unknown_episode_is_an_error_not_a_blank_player():
    out = run("""
try { await start('nope'); out.err = null; } catch (er) { out.err = er.message; }
out.audios = audios.length;
""")
    assert out["err"] == "Episode not found"


def test_resume_applies_the_saved_position_on_load():
    out = run("""
const saved = { e1: { episode_id: 'e1', progress: { position_seconds: 123.5, client_updated_at: 1700000000, played: false } } };
const s = await start('e1', { saved });
out.beforeMeta = s.audio.currentTime;
s.audio.metadata(600);
out.afterMeta = s.audio.currentTime;
out.progressGets = s.api.calls.filter(c => c.method === 'GET' && c.path.endsWith('/e1/progress')).length;
""")
    assert out["beforeMeta"] == 0 and out["afterMeta"] == 123.5 and out["progressGets"] == 1


def test_no_saved_position_and_played_episodes_start_at_zero():
    out = run("""
const a = await start('e1'); a.audio.metadata(600); out.fresh = a.audio.currentTime;
await mod.stopPlayback();
const saved = { e1b: { episode_id: 'e1b', progress: { position_seconds: 590, client_updated_at: 1700000000, played: true } } };
const b = await start('e1b', { saved }); b.audio.metadata(600); out.played = b.audio.currentTime;
""")
    assert out["fresh"] == 0 and out["played"] == 0


def test_position_is_saved_on_pause_and_at_a_fixed_interval_keyed_by_episode():
    out = run("""
const s = await start('e1'); const a = s.audio; a.metadata(600);
const puts = () => s.api.calls.filter(c => c.method === 'PUT').map(c => ({ path: c.path, pos: c.body.position_seconds, stamped: c.body.client_updated_at > 0, keys: Object.keys(c.body).sort() }));
a.currentTime = 40; a.dispatchEvent(new Event('timeupdate')); await advance(0);     // first tick writes at once
out.afterTick = puts().length;
a.currentTime = 45; a.dispatchEvent(new Event('timeupdate')); await advance(0);     // inside the 10 s window: no write
out.insideWindow = puts().length;
await advance(11000); a.currentTime = 52; a.dispatchEvent(new Event('timeupdate')); await advance(0);
out.afterInterval = puts().length;
a.currentTime = 70; a.pause(); a.play(); a.pause(); await advance(0);              // pause writes (play first so pause fires)
out.all = puts();
""")
    assert out["afterTick"] == 1 and out["insideWindow"] == 1 and out["afterInterval"] == 2
    assert len(out["all"]) >= 3
    for put in out["all"]:
        assert put["path"] == "/api/player/v1/podcasts/episodes/e1/progress"
        assert put["keys"] == ["client_updated_at", "position_seconds"] and put["stamped"]
    assert [p["pos"] for p in out["all"]][:2] == [40, 52] and out["all"][-1]["pos"] == 70


def test_end_marks_played_and_plays_the_next_episode_across_a_season_boundary():
    out = run("""
const next = { e1b: { id: 'e2', show_id: 'sh1', title: 'Two: Return', season_number: 2, duration_seconds: 600 } };
const s = await start('e1b', { next }); const a = s.audio; a.metadata(600);
a.currentTime = 600; a.paused = false;
a.dispatchEvent(new Event('ended')); await tick();
out.srcAfterEnd = a.src;
out.callsPaths = s.api.calls.map(c => `${c.method} ${c.path}`);
out.endPut = s.api.calls.filter(c => c.method === 'PUT').map(c => c.body.position_seconds);
a.metadata(600); await tick();
out.autoplayed = a.plays; out.title = mod.current().m.title; out.seen = s.seen; out.timers = timers.size;
""")
    assert out["srcAfterEnd"] == "/api/player/v1/podcasts/episodes/e2/audio"
    paths = out["callsPaths"]
    base = "/api/player/v1/podcasts/episodes/e1b"
    assert f"PUT {base}/progress" in paths and f"POST {base}/played" in paths and f"GET {base}/next" in paths
    assert paths.index(f"GET {base}/next") > paths.index(f"POST {base}/played")
    assert out["endPut"] == [600]
    assert out["autoplayed"] >= 1 and out["title"] == "Two: Return" and out["seen"] == ["e1b", "e2"]


def test_end_of_the_last_episode_stops_cleanly():
    out = run("""
const s = await start('e2'); const a = s.audio; a.metadata(600); a.currentTime = 600;
a.dispatchEvent(new Event('ended')); await tick();
out.src = a.src; out.toasts = toasts; out.nexts = s.api.calls.filter(c => c.path.endsWith('/next')).length;
""")
    assert out["src"].endswith("/e2/audio") and out["toasts"] == ["Finished"] and out["nexts"] == 1


def test_end_of_episode_sleep_timer_stops_instead_of_advancing():
    out = run("""
const next = { e1: { id: 'e1b', title: 'One: Finale', duration_seconds: 600 } };
const s = await start('e1', { next }); const a = s.audio; a.metadata(600);
out.label = /<option value="-1">End of episode<\\/option>/.test(s.host.innerHTML);
s.host.el('#au-sleep').onchange({ target: { value: '-1' } });
a.currentTime = 600; a.dispatchEvent(new Event('ended')); await tick();
out.src = a.src; out.toasts = toasts; out.nexts = s.api.calls.filter(c => c.path.endsWith('/next')).length;
""")
    assert out["label"] and out["src"].endswith("/e1/audio") and out["nexts"] == 0
    assert "Sleep timer: end of episode" in out["toasts"]


def test_speed_control_drives_the_episode_audio_and_is_remembered():
    out = run("""
const s = await start('e1'); const a = s.audio;
s.host.el('#au-speed').onchange({ target: { value: '1.5' } });
out.rate = a.playbackRate; a.dispatchEvent(new Event('ratechange')); out.stored = localStorage.getItem('bookdoe-speed');
await mod.stopPlayback();
const t = await start('e1b'); out.nextRate = audios[0].playbackRate;
""")
    assert out["rate"] == 1.5 and out["stored"] == "1.5" and out["nextRate"] == 1.5


def test_leaving_the_page_flushes_the_last_position():
    out = run("""
const s = await start('e1'); const a = s.audio; a.metadata(600);
a.currentTime = 99; a.dispatchEvent(new Event('timeupdate')); await advance(0);
a.currentTime = 130; await s.cleanup(); await tick();
out.puts = s.api.calls.filter(c => c.method === 'PUT').map(c => c.body.position_seconds);
""")
    assert out["puts"][-1] == 130


def test_episode_ending_after_the_page_was_left_continues_in_the_background_without_rendering():
    out = run("""
const next = { e1: { id: 'e1b', show_id: 'sh1', title: 'One: Finale', duration_seconds: 600 } };
const mini = Object.assign(stub(), { querySelector: () => stub() });   // a DOM-like mini-player host: paintMini needs classList, dataset, querySelector
const s = await start('e1', { next }, { chrome: 'player', mini });
const a = s.audio; a.metadata(600);
const html = s.host.innerHTML; const seenBefore = s.seen.slice();
await s.cleanup(); await tick();                      // the listener navigated away; playback goes on
a.currentTime = 600; a.dispatchEvent(new Event('ended')); await tick();
out.nexts = s.api.calls.filter(c => c.path.endsWith('/e1/next')).length;
out.src = a.src; out.htmlSame = s.host.innerHTML === html; out.seen = s.seen; out.seenBefore = seenBefore;
""")
    assert out["nexts"] == 1 and out["src"] == "/api/player/v1/podcasts/episodes/e1b/audio"
    assert out["htmlSame"] and out["seen"] == out["seenBefore"] == ["e1"]


def test_reopening_the_playing_episode_hands_play_next_to_the_new_page():
    out = run("""
const next = { e1: { id: 'e1b', show_id: 'sh1', title: 'One: Finale', duration_seconds: 600 } };
const s = await start('e1', { next }, { chrome: 'player' });
const a = s.audio; a.metadata(600);
await s.cleanup(); await tick();                      // the listener left the page; e1 keeps playing
const host2 = makeHost(), seen2 = [];
await mod.openPodcast({ show: SHOW, episodeId: 'e1', api: s.api, host: host2, toast, chrome: 'player', onBack() {}, onEpisode: e => seen2.push(e.id) });
a.currentTime = 600; a.dispatchEvent(new Event('ended')); await tick();
out.src = a.src; out.html2 = host2.innerHTML; out.seen2 = seen2; out.seen1 = s.seen;
""")
    assert out["src"] == "/api/player/v1/podcasts/episodes/e1b/audio"
    assert "One: Finale" in out["html2"]
    assert out["seen2"] == ["e1", "e1b"] and out["seen1"] == ["e1"]


# ---- show page wiring (app.js helper block) ----
def _helpers(expr: str):
    src = (STATIC / "app.js").read_text()
    block = re.search(r"// <podcast-show-helpers>.*?// </podcast-show-helpers>", src, re.S).group(0)
    esc = "const e=(v='')=>String(v??'').replace(/[&<>'\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',\"'\":'&#39;','\"':'&quot;'}[c]));"
    prog = f"{esc}\n{block}\nconsole.log(JSON.stringify({expr}))"
    return json.loads(subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_every_episode_row_has_a_play_button_for_that_episode():
    html = _helpers("podEpisodeHtml({id:'e7',title:'Seven <b>',description:''},e)")
    assert 'class="button compact pod-play" data-play="e7"' in html and 'aria-label="Play Seven &lt;b&gt;"' in html


def test_play_route_targets_the_player_page_for_the_show_and_episode():
    assert _helpers("podPlayRoute('sh 1','e/7')") == "podcasts/sh%201/play/e%2F7"


def test_app_routes_the_play_hash_to_the_shared_player_and_follows_play_next():
    src = (STATIC / "app.js").read_text()
    assert "r==='podcasts' && id && sub==='play' && tab" in src
    assert src.index("sub==='play' && tab") < src.index("if(r==='podcasts' && id) return await renderPodcastShow")
    body = src[src.index("async function renderPodcastPlayer"):]
    body = body[:body.index("\n}\n")]
    assert "mod.openPodcast(" in body and "history.replaceState" in body and "chrome:'management'" in body
