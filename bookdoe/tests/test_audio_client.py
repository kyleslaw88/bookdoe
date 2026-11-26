"""consume.js audiobook client logic that runs without a browser: the time label, how a
failed progress save is classified, the backoff, and the saver state machine driven by a
fake clock. The browser lifecycle (admin removes the book, kid backs off, admin re-adds,
playback resumes) is scripts/ui-player-e2e.mjs.

The incident behind these: a kid's client sent ~140 PUT /progress a minute for 30 minutes
at a book that had left the kids shelf. Whatever the cause, a failing save must not be
able to do that again.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

PRELUDE = r"""
import fs from 'fs';
const store = new Map();
globalThis.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
globalThis.crypto ??= { randomUUID: () => '00000000-test-device' };
const mod = await import(process.argv[2]);
const out = {};
// A virtual clock: timers run in order as time is advanced, so 30 minutes take no time.
function clock() {
  let t = 1_000_000_000_000, id = 0; const timers = new Map();
  return {
    now: () => t,
    setTimeout: (fn, ms) => { const k = ++id; timers.set(k, { at: t + Math.max(0, ms), fn }); return k; },
    clearTimeout: k => timers.delete(k),
    async advance(ms) {
      const end = t + ms;
      for (;;) {
        let next = null; for (const [k, v] of timers) if (v.at <= end && (!next || v.at < next[1].at)) next = [k, v];
        if (!next) break;
        timers.delete(next[0]); t = Math.max(t, next[1].at); next[1].fn();
        for (let i = 0; i < 5; i++) await Promise.resolve();
      }
      t = end; for (let i = 0; i < 5; i++) await Promise.resolve();
    },
  };
}
const err = (status, extra = {}) => Object.assign(new Error(status ? `HTTP ${status}` : 'network'), { status, ...extra });
"""


def run(body: str):
    src = (STATIC / "consume.js").read_text()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "consume.mjs").write_text(src)
        (Path(tmp) / "t.mjs").write_text(PRELUDE + body + "\nconsole.log(JSON.stringify(out));")
        r = subprocess.run([NODE, str(Path(tmp) / "t.mjs"), str(Path(tmp) / "consume.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_time_label_reads_elapsed_total_and_remaining():
    out = run("""
      const f = mod.formatAudioTimes;
      out.cases = {
        phone: f({ cur: 4.4, dur: 27033.2, rate: 1 }).label,          // the screenshot: 0:04 / 7:30:33
        start: f({ cur: 0, dur: 59.9 }).label,
        minutes: f({ cur: 61, dur: 3725 }).label,
        padding: f({ cur: 3605, dur: 36000 }).label,
        end: f({ cur: 99, dur: 90 }).label,                            // elapsed never beats the total
        fast: f({ cur: 0, dur: 3600, rate: 2 }).label,                  // remaining is listening time
        weirdRate: f({ cur: 0, dur: 600, rate: 0 }).label,
        nan: f({ cur: 3.2, dur: NaN }).label,                           // before metadata loads
        inf: f({ cur: 3, dur: Infinity }).label,
        zero: f({ cur: 0, dur: 0 }).label,
        neg: f({ cur: -5, dur: 100 }).label,
        none: f().label,
        str: f({ cur: '12', dur: '120' }).label,
      };
      out.parts = f({ cur: 4, dur: 27033 });
      out.clock = [0, 59, 60, 3599, 3600, 3661, 36000, 360000, NaN, Infinity, -1, '7'].map(mod.fmtClock);
    """)
    assert out["cases"] == {
        "phone": "0:04 / 7:30:33 · 7:30:29 left",
        "start": "0:00 / 0:59 · 0:59 left",
        "minutes": "1:01 / 1:02:05 · 1:01:04 left",
        "padding": "1:00:05 / 10:00:00 · 8:59:55 left",
        "end": "1:30 / 1:30 · 0:00 left",
        "fast": "0:00 / 1:00:00 · 30:00 left",
        "weirdRate": "0:00 / 10:00 · 10:00 left",
        "nan": "0:03 / --:--",
        "inf": "0:03 / --:--",
        "zero": "0:00 / --:--",
        "neg": "0:00 / 1:40 · 1:40 left",
        "none": "0:00 / --:--",
        "str": "0:12 / 2:00 · 1:48 left",
    }
    assert out["parts"] == {"elapsed": "0:04", "total": "7:30:33", "remaining": "7:30:29", "label": "0:04 / 7:30:33 · 7:30:29 left"}
    assert out["clock"] == ["0:00", "0:59", "1:00", "59:59", "1:00:00", "1:01:01", "10:00:00", "100:00:00", "0:00", "0:00", "0:00", "0:07"]


def test_failures_are_classified_and_backoff_is_capped_and_jittered():
    out = run("""
      const c = mod.classifyProgressError;
      out.kinds = Object.fromEntries([404, 403, 410, 500, 502, 503, 429, 408, 401, 400, 422, 0].map(s => [s, c(s ? err(s) : err(0))]));
      out.nullErr = c(null);
      const P = mod.PROGRESS_POLICY;
      const lo = [1, 2, 3, 4, 5, 6, 7, 20].map(n => mod.backoffDelay(n, { random: () => 0 }));
      const hi = [1, 2, 3, 4, 5, 6, 7, 20].map(n => mod.backoffDelay(n, { random: () => 1 }));
      out.lo = lo; out.hi = hi; out.max = P.maxMs; out.min = P.minGapMs;
      out.retryAfter = mod.backoffDelay(1, { random: () => 0.5, retryAfterMs: 90000 });
      out.probes = [0, 1, 2, 3, 10].map(k => mod.probeDelay(k, { random: () => 0.5 }));
    """)
    assert out["kinds"] == {"404": "unavailable", "403": "unavailable", "410": "unavailable", "500": "retry", "502": "retry", "503": "retry",
                            "429": "retry", "408": "retry", "401": "retry", "400": "drop", "422": "drop", "0": "retry"}
    assert out["nullErr"] == "retry"
    assert out["lo"] == sorted(out["lo"]) and out["hi"] == sorted(out["hi"])
    assert all(v >= out["min"] for v in out["lo"]), "never faster than one request per 5 s"
    assert max(out["hi"]) <= out["max"] * 1.25 + 1 and out["lo"][-1] >= out["max"] * 0.75 - 1, "capped near 5 minutes, jittered both ways"
    assert out["retryAfter"] == 90000, "a server Retry-After wins over a shorter backoff"
    assert out["probes"] == [60000, 120000, 240000, 300000, 300000]


def test_a_book_taken_off_the_shelf_is_probed_gently_and_recovers_with_the_saved_place():
    """The incident, replayed on a fake clock: 404 for 30 minutes while the player keeps
    producing positions (a tick every 250 ms, a save every 10 s). Then the admin re-adds it."""
    out = run("""
      const c = clock(); const log = []; const states = []; let live = false;
      const api = async (url, o) => { log.push({ t: c.now(), pos: o.body.position }); if (!live) throw err(404); return { applied: true }; };
      const saver = mod.progressSaver(api, 'w1', 'a1', { now: c.now, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout, random: () => 0.5, onState: (s, p) => states.push([s, p]) });
      let pos = 0;
      for (let s = 0; s < 30 * 60; s++) {                       // 30 minutes, one second at a time
        pos += 1; if (s % 10 === 0) saver.queue({ file_id: 'f1', position: pos }, 0);
        if (s % 7 === 0) saver.queue({ file_id: 'f1', position: pos }, 800);  // seeks and pauses on top
        await c.advance(1000);
      }
      out.requestsWhileHidden = log.length;
      out.firstGap = log.length > 1 ? log[1].t - log[0].t : null;
      out.minGap = Math.min(...log.slice(1).map((x, i) => x.t - log[i].t));
      out.perMinuteMax = Math.max(...Array.from({ length: 30 }, (_, m) => log.filter(x => x.t >= log[0].t + m * 60000 && x.t < log[0].t + (m + 1) * 60000).length));
      out.state = saver.state;
      out.held = JSON.parse(store.get('alx-pending-progress'))['a1'].position;
      out.lastPos = pos;
      // admin re-adds the book
      live = true;
      const before = log.length;
      await c.advance(6 * 60 * 1000);
      out.requestsAfterReadd = log.length - before;
      out.recoveredWith = log[log.length - 1].pos;
      out.stateAfter = saver.state;
      out.pendingAfter = store.get('alx-pending-progress') ?? null;
      out.transitions = states;
    """)
    assert out["state"] == "unavailable"
    assert out["requestsWhileHidden"] <= 12, f"30 minutes of failures made {out['requestsWhileHidden']} requests (the incident: ~4200)"
    assert out["minGap"] >= 5000 and out["perMinuteMax"] <= 2
    assert out["held"] >= out["lastPos"] - 10, "the listener's place is kept on the device, not discarded"
    assert out["requestsAfterReadd"] <= 2
    assert out["recoveredWith"] >= out["lastPos"] - 10, "the saved position is what gets replayed"
    assert out["stateAfter"] == "ok" and out["pendingAfter"] is None
    assert out["transitions"] == [["unavailable", "ok"], ["ok", "unavailable"]]


def test_retryable_failures_back_off_and_the_hard_ceiling_holds_even_with_zero_delays():
    out = run("""
      // (a) a server outage: 503s back off 5 s, 10 s, 20 s ... then sit on the 5 minute cadence
      let c = clock(); let log = []; let up = false;
      let api = async () => { log.push(c.now()); if (!up) throw err(503); return {}; };
      let s = mod.progressSaver(api, 'w', 'a', { now: c.now, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout, random: () => 0.5 });
      s.queue({ position: 1 }, 0);
      for (let i = 0; i < 3600; i++) { if (i % 10 === 0) s.queue({ position: i }, 0); await c.advance(1000); }
      out.outage = { n: log.length, gaps: log.slice(1).map((x, i) => (x - log[i]) / 1000) };
      up = true; await c.advance(6 * 60 * 1000); out.outageRecovered = s.state;
      // (b) 429 with Retry-After is honoured
      c = clock(); log = [];
      api = async () => { log.push(c.now()); if (log.length === 1) throw err(429, { retryAfter: 45 }); return {}; };
      s = mod.progressSaver(api, 'w', 'b', { now: c.now, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout, random: () => 0.5 });
      s.queue({ position: 1 }, 0); await c.advance(60000);
      out.retryAfterGap = log.length > 1 ? (log[1] - log[0]) / 1000 : null;
      // (c) the ceiling: delays configured to zero (a bug) still cannot exceed windowMax a minute
      c = clock(); log = [];
      api = async () => { log.push(c.now()); throw err(500); };
      s = mod.progressSaver(api, 'w', 'c', { now: c.now, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout, random: () => 0.5,
        policy: { minGapMs: 0, baseMs: 0, maxMs: 0, jitter: 0, probeBaseMs: 0, probeMaxMs: 0 } });
      for (let i = 0; i < 600; i++) { s.queue({ position: i }, 0); await c.advance(100); }   // 60 s of a runaway caller
      out.ceiling = log.filter(x => x < log[0] + 60000).length; out.ceilingTotal = log.length;
      // (d) 400 can never succeed: dropped, not kept, not retried
      c = clock(); log = []; store.clear();
      api = async () => { log.push(c.now()); throw err(400); };
      s = mod.progressSaver(api, 'w', 'd', { now: c.now, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout });
      s.queue({ position: 1 }, 0); await c.advance(10 * 60000);
      out.dropped = { n: log.length, pending: store.get('alx-pending-progress') ?? null, state: s.state };
    """)
    gaps = out["outage"]["gaps"]
    assert out["outage"]["n"] <= 20 and min(gaps) >= 5, out["outage"]
    assert gaps[:4] == [5.0, 10.0, 20.0, 40.0], "exponential, with the jitter centred"
    assert max(gaps) <= 300 * 1.25
    assert out["outageRecovered"] == "ok"
    assert out["retryAfterGap"] >= 45
    assert out["ceiling"] <= 10, "windowMax is a hard limit per book per minute whatever the delays say"
    assert out["ceilingTotal"] == 20, "and the next minute's budget opens only when the first minute has passed"
    assert out["dropped"] == {"n": 1, "pending": None, "state": "ok"}


def test_replay_keeps_positions_for_books_that_are_off_the_shelf_and_gates_itself():
    out = run("""
      store.set('alx-pending-progress', JSON.stringify({
        gone: { asset_id: 'gone', file_id: 'f', position: 99, client_updated_at: Date.now() / 1000 },
        ok:   { asset_id: 'ok', file_id: 'f', position: 5, client_updated_at: Date.now() / 1000 },
        old:  { asset_id: 'old', file_id: 'f', position: 1, client_updated_at: 1 },        // older than the 30 day TTL
        newer:{ asset_id: 'newer', file_id: 'f', position: 2, client_updated_at: Date.now() / 1000 },
      }));
      let calls = 0; let sent = null;
      const api = async (u, o) => { calls++; sent = o.body.items.map(i => i.asset_id);
        return { items: [{ asset_id: 'gone', applied: false, error: 'not_found' }, { asset_id: 'ok', applied: true }, { asset_id: 'newer', applied: false }] }; };
      mod.resetReplayGate();
      out.first = await mod.replayPendingProgress(api);
      out.sent = sent;
      out.left = Object.keys(JSON.parse(store.get('alx-pending-progress')));
      out.second = await mod.replayPendingProgress(api);          // immediately again: gated, no request
      out.calls = calls;
      // network failure keeps everything and backs off
      mod.resetReplayGate();
      const down = async () => { throw err(0); };
      out.down = await mod.replayPendingProgress(down);
      out.leftAfterDown = Object.keys(JSON.parse(store.get('alx-pending-progress')));
    """)
    assert out["sent"] == ["gone", "ok", "newer"], "entries past the TTL are let go before sending"
    assert out["first"] == {"replayed": 1, "stale": 1, "kept": 1}
    assert out["left"] == ["gone"], "only the book that is off the shelf is kept"
    assert out["second"] == {"replayed": 0, "deferred": True} and out["calls"] == 1
    assert out["down"]["replayed"] == 0 and out["down"]["kept"] == 1 and out["leftAfterDown"] == ["gone"]
