"""Installable PWA shell: manifest, iOS meta tags, service worker.

The HTTP half drives the real FastAPI app (manifest and worker routes, content types,
cache headers, icons). The worker half loads the real sw.js into node with a fake
ServiceWorkerGlobalScope and an in-memory Cache Storage, then pushes synthetic fetch
and message events through its listeners: classification, per-user content caching,
sign-out wiping the cache, and Range slicing for <audio> seeks served from cache.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

STATIC = Path(__file__).parent.parent / "app" / "static"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---------- manifest ----------

def test_manifest_is_served_as_valid_json_with_manifest_content_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert r.headers["cache-control"] == "no-cache"
    m = json.loads(r.text)
    assert m["name"] == "bookdoe"
    assert m["display"] == "standalone"
    assert m["start_url"].startswith("/")
    assert m["scope"] == "/"
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", m["theme_color"])
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", m["background_color"])
    assert m["theme_color"] == "#0b0f17"  # --bg in styles.css dark theme


def test_manifest_start_url_resolves_to_the_app_shell(client):
    m = client.get("/manifest.webmanifest").json()
    r = client.get(m["start_url"].split("#", 1)[0])
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert '<link rel="manifest" href="/manifest.webmanifest" />' in r.text


def test_manifest_icons_exist_and_match_declared_sizes(client):
    m = client.get("/manifest.webmanifest").json()
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"]), "Android adaptive icon needs a maskable entry"
    for icon in m["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.headers["content-type"].startswith(icon["type"]), icon["src"]
        if icon["type"] == "image/png":
            # PNG IHDR: 8-byte signature, 4 length, 4 'IHDR', then width/height big-endian.
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = struct.unpack(">II", r.content[16:24])
            assert f"{w}x{h}" == icon["sizes"], icon["src"]


# ---------- index.html: iOS needs more than the manifest ----------

def test_index_declares_ios_standalone_meta_and_touch_icon(client):
    html = client.get("/").text
    for tag in (
        '<meta name="apple-mobile-web-app-capable" content="yes" />',
        '<meta name="mobile-web-app-capable" content="yes" />',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />',
        '<meta name="apple-mobile-web-app-title" content="bookdoe" />',
        '<link rel="apple-touch-icon" sizes="180x180" href="/assets/icons/apple-touch-icon.png" />',
        '<meta name="theme-color" content="#0b0f17" media="(prefers-color-scheme: dark)" />',
        '<script src="/assets/pwa.js?v=dev"></script>',
    ):
        assert tag in html, tag
    r = client.get("/assets/icons/apple-touch-icon.png")
    assert r.status_code == 200
    w, h = struct.unpack(">II", r.content[16:24])
    assert (w, h) == (180, 180)
    # iOS composites the icon on an opaque tile; a PNG with an alpha channel renders
    # black corners. Colour type byte 6 = RGBA, 2 = RGB.
    assert r.content[25] == 2, "apple-touch-icon must be opaque RGB"


# ---------- service worker route ----------

def test_service_worker_is_served_from_origin_root_as_javascript(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert "addEventListener('fetch'" in r.text
    # Registered with scope '/', so it must not be an /assets/ URL.
    assert "navigator.serviceWorker.register('/sw.js', { scope: '/' })" in (STATIC / "pwa.js").read_text()


def test_pwa_routes_do_not_shadow_api_404s(client):
    # The catch-all SPA route still refuses to turn API typos into HTML.
    assert client.get("/api/v1/sw.js").status_code == 404
    assert client.get("/api/v1/manifest.webmanifest").status_code == 404


def test_app_js_tells_worker_who_signed_in_and_clears_on_logout():
    js = (STATIC / "app.js").read_text()
    assert "window.bookdoePwa.setUser(me.user.id)" in js
    assert "window.bookdoePwa.clearUser()" in js
    assert "wireOfflineButtons()" in js


# ---------- service worker behaviour (node harness) ----------

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [swJs, body] = process.argv.slice(1);

// In-memory Cache Storage. Keys are absolute URLs, as in the browser.
const store = new Map();
function norm(req) { return typeof req === 'string' ? new URL(req, 'https://bookdoe.test').href : req.url; }
class FakeCache {
  constructor() { this.m = new Map(); }
  async match(req) { const r = this.m.get(norm(req)); return r ? r.clone() : undefined; }
  async put(req, res) { this.m.set(norm(req), res.clone()); }
  async delete(req) { return this.m.delete(norm(req)); }
  async keys() { return [...this.m.keys()].map(u => new Request(u)); }
}
const caches = {
  async open(n) { if (!store.has(n)) store.set(n, new FakeCache()); return store.get(n); },
  async keys() { return [...store.keys()]; },
  async delete(n) { return store.delete(n); },
  async has(n) { return store.has(n); },
};
const listeners = {};
const network = [];     // every URL the worker fetched from the "network"
let networkBody = {};   // url -> {status, body, headers}
const ctx = {
  console, URL, Request, Response, Headers, caches, setTimeout, TextEncoder, TextDecoder, AbortController,
  fetch: async (req) => {
    const url = typeof req === 'string' ? norm(req) : req.url;
    network.push(new URL(url).pathname);
    const spec = networkBody[new URL(url).pathname] || networkBody['*'];
    if (!spec) throw new TypeError('Failed to fetch (offline)');
    return new Response(spec.body, { status: spec.status || 200, headers: spec.headers || {} });
  },
};
ctx.self = Object.assign(ctx, {
  location: new URL('https://bookdoe.test/sw.js'),
  addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
  skipWaiting: async () => {}, clients: { claim: async () => {}, matchAll: async () => ctx.__pages || [] },
});
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(swJs, 'utf8'), ctx, { filename: 'sw.js' });

// Drive a fetch event through the worker; resolves to the Response, or null when
// the worker declined to handle it (no respondWith => browser goes to network).
async function dispatch(path, init = {}) {
  const req = new Request('https://bookdoe.test' + path, init);
  let out = null;
  const ev = { request: req, respondWith(p) { out = Promise.resolve(p); }, waitUntil() {} };
  for (const fn of listeners.fetch) fn(ev);
  return out ? await out : null;
}
async function message(data) {
  let reply;
  const done = new Promise(r => { reply = r; });
  const ev = { data, ports: [{ postMessage: reply }], waitUntil: p => p };
  for (const fn of listeners.message) fn(ev);
  return done;
}
const api = { dispatch, message, network, store, caches, setNetwork: m => { networkBody = m; }, setPages: p => { ctx.__pages = p; },
  sw: ctx.self.__bookdoeSw, listeners };
vm.runInContext(`(async (h) => { ${body} })`, ctx, { filename: 'test-body.js' })(api)
  .then(r => { console.log(JSON.stringify(r ?? null)); })
  .catch(er => { console.error(er && er.stack || String(er)); process.exit(1); });
"""


def run_sw(body: str):
    proc = subprocess.run([NODE, "-e", HARNESS, "--", str(STATIC / "sw.js"), body], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_sw_parses_and_registers_lifecycle_listeners():
    out = run_sw("return Object.keys(h.listeners).sort();")
    assert out == ["activate", "fetch", "install", "message"]


@needs_node
def test_sw_classifies_requests():
    out = run_sw("""
      const c = (p, m='GET') => h.sw.classify(new URL('https://bookdoe.test' + p), m);
      const x = (u, m='GET') => h.sw.classify(new URL(u), m);
      return {
        root: c('/'), css: c('/assets/styles.css'), manifest: c('/manifest.webmanifest'),
        self: c('/sw.js'),
        stream: c('/api/v1/library/w1/files/f1'), file: c('/opds/files/abc'), kobo: c('/api/v1/library/w1/kobo/a1'),
        cover: c('/api/v1/library/w1/cover'), work: c('/api/v1/library/w1'),
        readerManifest: c('/api/v1/library/w1/read/a1'),
        playerWork: c('/api/player/v1/library/w1'), playerEbook: c('/api/player/v1/ebook/w1/a1'), playerAudio: c('/api/player/v1/audio/w1/a1'),
        playerHome: c('/api/player/v1/home'), playerCover: c('/api/player/v1/library/w1/cover'), playerList: c('/api/player/v1/library?limit=5'),
        playerProgress: c('/api/player/v1/progress'), playerSync: c('/api/player/v1/sync'), playerDevices: c('/api/player/v1/devices'),
        progress: c('/api/v1/library/w1/progress/a1'), progressPut: c('/api/v1/library/w1/progress/a1', 'PUT'),
        workQuery: c('/api/v1/library?limit=5'), list: c('/api/v1/library'),
        me: c('/api/v1/auth/me'), jobs: c('/api/v1/jobs'), opds: c('/opds/v2/catalog.json'),
        oidc: c('/auth/login'), post: c('/opds/files/abc', 'POST'), spa: c('/library/w1'),
        epubjs: x('https://cdn.jsdelivr.net/npm/epubjs@0.3.93/dist/epub.min.js'),
        jszip: x('https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js'),
        otherCdn: x('https://cdn.jsdelivr.net/npm/lodash@4/lodash.min.js'),
        otherHost: x('https://evil.example/npm/epubjs@0.3.93/dist/epub.min.js'),
      };
    """)
    assert out == {
        # '/' is a navigation now (the Player shell answers it, see shellKeyFor), not a
        # precached asset; the two shells are cached under /app and /manage.
        "root": "navigation", "css": "shell", "manifest": "shell", "self": "bypass",
        # The reader/player stream (#250) and the two download forms are the book bytes.
        "stream": "content-bytes", "file": "content-bytes", "kobo": "content-bytes",
        "cover": "content-cover",
        "work": "content-json", "readerManifest": "content-json",
        # The Player API's per-work JSON, manifests and Home are the offline reading set;
        # lists, progress, sync and credentials never enter a cache.
        "playerWork": "content-json", "playerEbook": "content-json", "playerAudio": "content-json", "playerHome": "content-json",
        "playerCover": "content-cover", "playerList": "bypass", "playerProgress": "bypass", "playerSync": "bypass", "playerDevices": "bypass",
        # Progress is carried inside the reader manifest; the standalone endpoint and
        # every write go straight to the network.
        "progress": "bypass", "progressPut": "bypass",
        # /api/v1/library is the paged list, not a work; it must never be cached.
        "workQuery": "bypass", "list": "bypass",
        "me": "bypass", "jobs": "bypass", "opds": "bypass", "oidc": "bypass",
        "post": "bypass", "spa": "navigation",
        # Only the exact pinned EPUB libraries from the host app.js loads them from.
        "epubjs": "vendor", "jszip": "vendor", "otherCdn": "bypass", "otherHost": "bypass",
    }


@needs_node
def test_sw_vendor_libraries_pin_matches_consume_js_and_are_cached_cross_origin():
    # The reader module is shared by both shells; it is the only place that loads them.
    js = (STATIC / "consume.js").read_text()
    urls = re.findall(r"'(https://cdn\.jsdelivr\.net/npm/[^']+)'", js)
    assert len(urls) == 2, "consume.js should pin exactly jszip and epubjs"
    for shell in ("app.js", "player.js"):
        assert "cdn.jsdelivr.net" not in (STATIC / shell).read_text(), f"{shell} must not load vendor code itself"
    out = run_sw(f"""
      const urls = {json.dumps(urls)};
      h.setNetwork({{'*': {{body: 'lib', headers: {{'Content-Type': 'text/javascript'}}}}}});
      for (const u of urls) h.sw.classify(new URL(u), 'GET') === 'vendor' || (() => {{ throw new Error('not vendor: ' + u); }})();
      // Dispatch through the fetch listener with absolute cross-origin URLs.
      const dispatchAbs = async u => {{ let out = null; const ev = {{request: new Request(u), respondWith(p) {{ out = p; }}, waitUntil() {{}}}}; for (const fn of h.listeners.fetch) fn(ev); return out ? await out : null; }};
      const online = await Promise.all(urls.map(dispatchAbs));
      h.setNetwork({{}});
      const offline = await Promise.all(urls.map(dispatchAbs));
      return {{online: online.map(r => r && r.status), offline: await Promise.all(offline.map(r => r && r.text())), names: await h.caches.keys()}};
    """)
    assert out["online"] == [200, 200]
    assert out["offline"] == ["lib", "lib"], "EPUB libraries must come from cache offline"
    assert out["names"] == ["bookdoe-vendor-v3"]


@needs_node
def test_sw_never_caches_general_api_traffic():
    out = run_sw("""
      h.setNetwork({'*': {body: '{}'}});
      await h.message({type: 'set-user', uid: 'u1'});
      const r = await h.dispatch('/api/v1/jobs');
      const names = await h.caches.keys();
      const sizes = {}; for (const n of names) sizes[n] = (await (await h.caches.open(n)).keys()).length;
      return {handled: r !== null, sizes};
    """)
    assert out["handled"] is False
    assert all(n.startswith("bookdoe-") for n in out["sizes"])
    assert sum(v for n, v in out["sizes"].items() if "content" in n) == 0


@needs_node
def test_sw_content_is_cached_per_user_and_served_offline():
    out = run_sw("""
      h.setNetwork({'/opds/files/f1': {body: 'EPUBBYTES', headers: {'Content-Type': 'application/epub+zip'}}});
      // No user recorded: the worker just proxies to the network and stores nothing.
      const anon = await h.dispatch('/opds/files/f1');
      const anonStored = (await h.caches.keys()).filter(n => n.includes('content')).length;
      await h.message({type: 'set-user', uid: 'alice'});
      const first = await (await h.dispatch('/opds/files/f1')).text();
      h.setNetwork({});   // go offline
      const second = await (await h.dispatch('/opds/files/f1')).text();
      const isCached = await h.message({type: 'is-cached', url: '/opds/files/f1'});
      const listed = await h.message({type: 'list-content'});
      // Bob signs in on the same device: Alice's cache is dropped, and Bob sees nothing.
      await h.message({type: 'set-user', uid: 'bob'});
      let bob; try { bob = (await h.dispatch('/opds/files/f1')).status; } catch (e) { bob = 'network-error'; }
      const names = await h.caches.keys();
      return {anonStatus: anon.status, anonStored, first, second, isCached, listed, bob, names, network: h.network};
    """)
    assert out["anonStatus"] == 200 and out["anonStored"] == 0
    assert out["first"] == "EPUBBYTES" and out["second"] == "EPUBBYTES"
    assert out["isCached"] == {"ok": True, "cached": True}
    assert out["listed"] == {"ok": True, "urls": ["/opds/files/f1"]}
    assert out["bob"] == "network-error"
    assert "bookdoe-content-v3-alice" not in out["names"]
    # Four network fetches: anonymous, Alice's first load, the background access check
    # behind her cached copy (it fails offline and changes nothing), Bob's (failed) attempt.
    assert out["network"] == ["/opds/files/f1"] * 4


@needs_node
def test_sw_clear_user_on_logout_wipes_that_users_content():
    out = run_sw("""
      h.setNetwork({'/opds/files/f9': {body: 'MP3'}});
      await h.message({type: 'set-user', uid: 'alice'});
      await h.message({type: 'cache-content', url: '/opds/files/f9'});
      const before = await h.caches.keys();
      await h.message({type: 'clear-user'});
      const after = await h.caches.keys();
      h.setNetwork({});
      let afterStatus; try { afterStatus = (await h.dispatch('/opds/files/f9')).status; } catch (e) { afterStatus = 'network-error'; }
      return {before, after, afterStatus};
    """)
    assert "bookdoe-content-v3-alice" in out["before"]
    assert "bookdoe-content-v3-alice" not in out["after"]
    assert out["afterStatus"] == "network-error"


@needs_node
def test_sw_cache_content_refuses_non_content_urls():
    out = run_sw("""
      h.setNetwork({'*': {body: '{}'}});
      const anon = await h.message({type: 'cache-content', url: '/opds/files/f1'});
      await h.message({type: 'set-user', uid: 'u'});
      const api = await h.message({type: 'cache-content', url: '/api/v1/auth/me'});
      const ok = await h.message({type: 'cache-content', url: '/opds/files/f1'});
      return {anon, api, ok, network: h.network};
    """)
    assert out["anon"]["ok"] is False
    assert out["api"]["ok"] is False and "/api/v1/auth/me" not in out["network"]
    assert out["ok"] == {"ok": True}


@needs_node
def test_sw_serves_byte_ranges_from_cached_audio_for_seeking():
    out = run_sw("""
      const bytes = 'ABCDEFGHIJ';
      h.setNetwork({'/opds/files/a1': {body: bytes, headers: {'Content-Type': 'audio/mpeg'}}});
      await h.message({type: 'set-user', uid: 'u'});
      await h.message({type: 'cache-content', url: '/opds/files/a1'});
      h.setNetwork({});
      const r1 = await h.dispatch('/opds/files/a1', {headers: {Range: 'bytes=2-4'}});
      const r2 = await h.dispatch('/opds/files/a1', {headers: {Range: 'bytes=7-'}});
      const r3 = await h.dispatch('/opds/files/a1', {headers: {Range: 'bytes=-3'}});
      const r4 = await h.dispatch('/opds/files/a1', {headers: {Range: 'bytes=50-60'}});
      const full = await h.dispatch('/opds/files/a1');
      return {
        r1: [r1.status, await r1.text(), r1.headers.get('content-range'), r1.headers.get('content-length')],
        r2: [r2.status, await r2.text(), r2.headers.get('content-range')],
        r3: [r3.status, await r3.text(), r3.headers.get('content-range')],
        r4: [r4.status, r4.headers.get('content-range')],
        full: [full.status, await full.text(), full.headers.get('content-type')],
        parse: [h.sw.parseRange('bytes=0-0', 10), h.sw.parseRange('garbage', 10), h.sw.parseRange('bytes=0-99', 10)],
      };
    """)
    assert out["r1"] == [206, "CDE", "bytes 2-4/10", "3"]
    assert out["r2"] == [206, "HIJ", "bytes 7-9/10"]
    assert out["r3"] == [206, "HIJ", "bytes 7-9/10"]
    assert out["r4"] == [416, "bytes */10"]
    assert out["full"] == [200, "ABCDEFGHIJ", "audio/mpeg"]
    assert out["parse"] == [{"start": 0, "end": 0}, None, {"start": 0, "end": 9}]


@needs_node
def test_sw_does_not_store_a_partial_network_response_as_the_whole_file():
    out = run_sw("""
      h.setNetwork({'/opds/files/a2': {status: 206, body: 'CDE', headers: {'Content-Range': 'bytes 2-4/10'}}});
      await h.message({type: 'set-user', uid: 'u'});
      const r = await h.dispatch('/opds/files/a2', {headers: {Range: 'bytes=2-4'}});
      const cached = await h.message({type: 'is-cached', url: '/opds/files/a2'});
      return {status: r.status, cached};
    """)
    assert out["status"] == 206
    assert out["cached"] == {"ok": True, "cached": False}


@needs_node
@pytest.mark.parametrize("path", ["/api/v1/library/w1", "/api/v1/library/w1/read/a1"])
def test_sw_work_json_is_network_first_with_offline_fallback(path):
    # Work detail and the reader/player manifest (#250): fresh when online, last seen
    # copy when offline, so an opened book's page and its Read/Listen view still render.
    out = run_sw(f"""
      const p = {json.dumps(path)};
      await h.message({{type: 'set-user', uid: 'u'}});
      h.setNetwork({{[p]: {{body: '{{"title":"v1"}}', headers: {{'Content-Type': 'application/json'}}}}}});
      const a = await (await h.dispatch(p)).json();
      h.setNetwork({{[p]: {{body: '{{"title":"v2"}}', headers: {{'Content-Type': 'application/json'}}}}}});
      const b = await (await h.dispatch(p)).json();
      h.setNetwork({{}});
      const c = await (await h.dispatch(p)).json();
      const other = await h.dispatch('/api/v1/library/w2');
      return {{a, b, c, otherStatus: other.status, otherBody: await other.json()}};
    """)
    assert out["a"] == {"title": "v1"}
    assert out["b"] == {"title": "v2"}, "online: always the fresh copy"
    assert out["c"] == {"title": "v2"}, "offline: last seen copy"
    assert out["otherStatus"] == 503 and out["otherBody"] == {"detail": "offline"}


def test_keep_offline_button_targets_the_reader_stream_url():
    # The button must store the exact URL renderConsume/renderPlayer fetch, or the
    # offline copy is never hit. reader.py builds that URL; keep the three in step.
    js = (STATIC / "app.js").read_text()
    assert '`/api/v1/library/${w.id}/files/${f.id}`' in js and 'data-urls=' in js
    reader_py = (Path(__file__).parent.parent / "app" / "services" / "reader.py").read_text()
    assert '"url": f"/api/v1/library/{work_id}/files/{f[\'id\']}"' in reader_py


@needs_node
def test_sw_navigation_falls_back_to_cached_shell_offline():
    out = run_sw("""
      h.setNetwork({'/': {body: '<!doctype html><title>bookdoe</title>', headers: {'Content-Type': 'text/html'}},
                    '/manage': {body: '<!doctype html><title>Management</title>', headers: {'Content-Type': 'text/html'}}});
      await h.dispatch('/', {headers: {Accept: 'text/html'}});
      await h.dispatch('/manage', {headers: {Accept: 'text/html'}});
      h.setNetwork({});
      const r = await h.dispatch('/app/library/w1', {headers: {Accept: 'text/html'}});   // any Player path
      const m = await h.dispatch('/manage/settings', {headers: {Accept: 'text/html'}});  // any Management path
      const shell = await (await h.caches.open('bookdoe-shell-dev')).keys();
      return {status: r.status, body: await r.text(), mbody: await m.text(), shell: shell.map(x => new URL(x.url).pathname).sort(),
              keys: ['/', '/app', '/app/x', '/manage', '/manage/x', '/managed'].map(h.sw.shellKeyFor)};
    """)
    assert out["status"] == 200
    assert "<title>bookdoe</title>" in out["body"]
    assert "<title>Management</title>" in out["mbody"], "offline /manage must get the Management shell, never the Player's"
    assert out["shell"] == ["/app", "/manage"]
    assert out["keys"] == ["/app", "/app", "/app", "/manage", "/manage", "/app"]


@needs_node
def test_sw_activate_tells_open_pages_a_new_version_took_over():
    out = run_sw("""
      const got = [];
      h.sw && (globalThis.__pages = [{ postMessage: m => got.push(m) }, { postMessage: m => got.push(m) }]);
      let p; h.listeners.activate[0]({ waitUntil: x => { p = x; } }); await p;
      return got;
    """)
    assert out == [{"type": "sw-updated", "version": "dev"}] * 2


def test_pwa_js_exposes_the_update_hook_and_player_defers_the_update_while_in_a_book():
    pwa = (STATIC / "pwa.js").read_text()
    assert "onUpdate: cb =>" in pwa and "ev.data.type === 'sw-updated'" in pwa
    player = (STATIC / "player.js").read_text()
    assert "watchVersion(" in player and "applyUpdate" in player
    assert "state.reloadWhenLeaving = true" in player, "an update must never interrupt reading or listening"
    assert "replayPendingProgress(api)" in player, "offline positions are replayed when back online"


def test_player_offline_buttons_target_the_reader_stream_url():
    player = (STATIC / "player.js").read_text()
    assert '`/api/v1/library/${w.id}/files/${f.id}`' in player and 'data-urls=' in player
    assert 'data-url="' not in player.split("function offlineButtons")[1].split("async function wireOfflineButtons")[0], "one control per asset, not per file"
    consume = (STATIC / "consume.js").read_text()
    # A failed save keeps the listener's place on the device (network failure AND a 404 for a
    # book that is off the shelf for now); only a refusal that can never succeed (400/422)
    # is dropped. tests/test_audio_client.py exercises the behaviour.
    assert "queuePendingProgress(assetId, body)" in consume
    assert "if (kind === 'drop')" in consume and "classifyProgressError" in consume


@needs_node
def test_sw_install_precaches_shell_and_activate_drops_old_versions():
    out = run_sw("""
      h.setNetwork({'*': {body: 'x'}});
      await h.caches.open('bookdoe-shell-v0');
      await h.caches.open('bookdoe-content-v0-someone');
      let p; h.listeners.install[0]({ waitUntil: x => { p = x; } }); await p;
      h.listeners.activate[0]({ waitUntil: x => { p = x; } }); await p;
      const shell = (await (await h.caches.open('bookdoe-shell-dev')).keys()).map(x => new URL(x.url).pathname).sort();
      return {shell, names: (await h.caches.keys()).sort(), expected: [...h.sw.SHELL_URLS].sort()};
    """)
    assert out["shell"] == out["expected"]
    assert out["names"] == ["bookdoe-shell-dev"]


def test_shell_precache_list_matches_files_that_exist(client):
    # Every URL the worker precaches must be served by this app, or install silently
    # skips it and the shell is incomplete offline.
    sw = (STATIC / "sw.js").read_text()
    block = re.search(r"const SHELL_URLS = \[(.*?)\];", sw, re.S).group(1)
    urls = re.findall(r"'([^']+)'", block)
    assert "/app" in urls and "/manage" in urls and "/assets/app.js" in urls and "/assets/player.js" in urls and "/assets/pwa.js" in urls
    for u in urls:
        assert client.get(u).status_code == 200, u


@needs_node
@pytest.mark.parametrize("path,body", [("/api/v1/library/w1/cover", "PNG"), ("/opds/files/f1", "EPUB"), ("/api/v1/library/w1", '{"t":1}')])
def test_sw_drops_a_stored_copy_once_the_server_revokes_access(path, body):
    # An admin removed the kids tag from this book: the kid's stored cover, bytes and
    # work JSON must not keep being served once the server answers 404 for them.
    out = run_sw(f"""
      const p = {json.dumps(path)};
      await h.message({{type: 'set-user', uid: 'kid'}});
      h.setNetwork({{[p]: {{body: {json.dumps(body)}}}}});
      await (await h.dispatch(p)).text();
      h.setNetwork({{[p]: {{status: 404, body: '{{"detail":"Work not found"}}'}}}});
      const online = await h.dispatch(p);          // cache-first kinds answer from the copy, then check
      const first = online.status;
      await new Promise(r => setTimeout(r, 50));
      const stored = (await h.message({{type: 'is-cached', url: p}})).cached;
      h.setNetwork({{}});                           // and offline there is nothing left to show
      let offline; try {{ offline = (await h.dispatch(p)).status; }} catch (e) {{ offline = 'network-error'; }}
      return {{first, stored, offline}};
    """)
    assert out["stored"] is False, "the stale copy survived a 404 from the server"
    assert out["offline"] in (503, "network-error")


@needs_node
def test_sw_remembers_a_dropped_offline_file_and_serves_normally_once_the_book_is_back():
    """#321 drops a kid's stored copies when the server says 403/404. The offline file must
    not vanish silently (the page says "Download again"), and nothing about it may stick:
    when the book returns the worker serves and stores it like any other."""
    out = run_sw("""
      const f = '/api/v1/library/w1/files/f1', cover = '/api/v1/library/w1/cover';
      const told = []; h.setPages([{ postMessage: m => told.push(m) }]);
      await h.message({type: 'set-user', uid: 'kid'});
      h.setNetwork({[f]: {body: 'AUDIO-BYTES'}, [cover]: {body: 'PNG'}});
      await h.message({type: 'cache-content', url: f});
      await (await h.dispatch(cover)).text();
      const saved = (await h.message({type: 'is-cached', url: f})).cached;
      // The admin takes the book off the kids shelf: both answers become 404.
      h.setNetwork({[f]: {status: 404, body: '{"detail":"Not found"}'}, [cover]: {status: 404, body: '{}'}});
      await (await h.dispatch(f)).text();       // served from the copy, then the server is asked
      await (await h.dispatch(cover)).text();
      await new Promise(r => setTimeout(r, 80));
      const dropped = {file: !(await h.message({type: 'is-cached', url: f})).cached, cover: !(await h.message({type: 'is-cached', url: cover})).cached};
      const revoked = (await h.message({type: 'list-revoked'})).urls;
      // Back on the shelf: served and stored again, no sticky state.
      h.setNetwork({[f]: {body: 'AUDIO-BYTES'}, [cover]: {body: 'PNG'}});
      const coverBack = await h.dispatch(cover); const coverBody = await coverBack.text();
      const fileBack = await h.dispatch(f, {headers: {Range: 'bytes=0-4'}}); const fileBody = await fileBack.text();
      await new Promise(r => setTimeout(r, 20));
      const coverStored = (await h.message({type: 'is-cached', url: cover})).cached;
      const stillRemembered = (await h.message({type: 'list-revoked'})).urls;
      // Downloading again (or forgetting on purpose) clears the memo.
      await h.message({type: 'cache-content', url: f});
      const afterDownload = (await h.message({type: 'list-revoked'})).urls;
      // Another user at the same device never sees this memo.
      await h.message({type: 'set-user', uid: 'someone-else'});
      const other = (await h.message({type: 'list-revoked'})).urls;
      return {saved, dropped, revoked, told, coverBody, coverStatus: coverBack.status, fileBody, fileStatus: fileBack.status, coverStored, stillRemembered, afterDownload, other};
    """)
    assert out["saved"] is True
    assert out["dropped"] == {"file": True, "cover": True}
    assert out["revoked"] == ["/api/v1/library/w1/files/f1"], "only the offline file is remembered; covers are not something the user saved"
    assert out["told"] == [{"type": "content-revoked", "urls": ["/api/v1/library/w1/files/f1"]}]
    assert (out["coverStatus"], out["coverBody"], out["coverStored"]) == (200, "PNG", True)
    assert out["fileStatus"] in (200, 206) and out["fileBody"].startswith("AUDIO")
    assert out["stillRemembered"] == ["/api/v1/library/w1/files/f1"], "the memo stays until the user downloads again, so the page can offer it"
    assert out["afterDownload"] == [] and out["other"] == []


# ---------- brand artwork ----------

def test_web_icons_are_the_brand_artwork_in_every_size(client):
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-512-maskable.png", 512), ("apple-touch-icon.png", 180)):
        r = client.get(f"/assets/icons/{name}")
        assert r.status_code == 200, name
        w, h = struct.unpack(">II", r.content[16:24])
        assert (w, h) == (size, size), name
        assert r.content[25] == 2, f"{name} must be opaque RGB"   # 6 would be RGBA


def test_pages_show_the_logo_image_not_a_letter_on_a_gradient():
    import pathlib
    static = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
    for name in ("index.html", "player.html", "player.js"):
        text = (static / name).read_text(encoding="utf-8")
        assert ">A</div>" not in text and ">A</span>" not in text, f"{name} still draws the letter A as the logo"
        assert "/assets/brand/bookdoe-" in text, f"{name} must show the vendored bookdoe logo"


def test_wide_layout_keeps_the_content_below_the_top_bar_and_the_safe_area():
    """The tablet layout once used a fixed 86px top padding, so on an iPad window (a top safe area of about 28px, which the
    fixed top bar also adds) the top bar covered the first row of every page, e.g. the Back button."""
    import pathlib, re
    css = (pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "player.css").read_text(encoding="utf-8")
    wide = css[css.index("@media (min-width: 820px)"):]
    view = re.search(r"\.pl-view \{[^}]*\}", wide).group(0)
    assert "padding: calc(86px + env(safe-area-inset-top))" in view, view
    rail = re.search(r"\.pl-nav \{[^}]*\}", wide).group(0)
    assert "calc(20px + env(safe-area-inset-top))" in rail, rail


def test_sw_does_not_run_regular_expressions_on_request_paths():
    """Paths are user-supplied; classify() matches them segment by segment (regex DoS finding, #698)."""
    import pathlib, re
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w)])/[^/\n*][^\n]*/[a-z]*\.test\(|\brx\.test\(", text), "regex test() on a path"
