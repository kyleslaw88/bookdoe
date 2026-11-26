/* The service worker.
 *
 * Served at /sw.js (see main.py) so its scope is the whole origin: the UI is a
 * single page whose routes live in the hash (#library/<id>), so a worker scoped to
 * /assets/ could never answer the navigation that boots the app offline.
 *
 * Three caches, three policies. Only the shell cache name follows the build: the server
 * bakes the git sha into BUILD below when it serves /sw.js, so every deploy installs a
 * new worker with a new shell cache and the old one is deleted on activation. The other
 * caches carry a fixed schema tag (CACHE_SCHEMA) and survive every update, because they
 * hold what the person saved (offline books, the signed-in user) and not code.
 *
 *   shell   (bookdoe-shell-<build>) index.html, styles.css, consume.css, app.js, pwa.js, icons,
 *                                      manifest. Stale-while-revalidate: the files are
 *                                      unversioned (the server sends Cache-Control:
 *                                      no-cache for the same reason), so we answer from
 *                                      cache for speed and refresh the copy in the
 *                                      background; a deploy shows up on the next load.
 *   content (bookdoe-content-v3-<uid>)  book and audiobook bytes (what the reader and
 *                                      player stream: /api/v1/library/<work>/files/<id>;
 *                                      also /opds/files/<id> and kobo downloads), cover
 *                                      art, and the JSON of works the user has opened
 *                                      (work detail, reader manifest). Cache-first for
 *                                      bytes and covers, network-first for the JSON.
 *   vendor  (bookdoe-vendor-v3)     the pinned epub.js and JSZip builds app.js loads
 *                                      from jsdelivr for EPUB rendering (CORS + SRI, so
 *                                      the cached copy is a real, checkable response).
 *                                      Cache-first; public code, not per user.
 *   everything else under /api/        network only, never stored. Reading-progress
 *                                      writes go straight to the network and the reader
 *                                      already tolerates them failing offline.
 *
 * Multi-user safety. Sessions are an HttpOnly cookie, which a worker cannot read, and
 * the Cache Storage is shared by everyone who uses this browser profile. So content
 * is keyed by user: the page tells the worker who is signed in after /api/v1/auth/me
 * succeeds (pwa.js, message {type:'set-user'}) and the worker records it in a tiny
 * meta cache (worker globals do not survive termination). Only the recorded user's
 * cache is ever read, and signing out deletes that user's content cache outright, so
 * the next person at a shared device finds nothing. With no recorded user, no content
 * is served from cache at all.
 */
'use strict';

// The server replaces the literal below with the build sha (main.py pwa_service_worker);
// served raw (tests, no server) it stays 'dev'.
const BUILD = 'dev';
const VERSION = BUILD;
// Bump only when the layout of the meta/content/vendor caches changes incompatibly.
const CACHE_SCHEMA = 'v3';
const SHELL_CACHE = `bookdoe-shell-${BUILD}`;
const META_CACHE = `bookdoe-meta-${CACHE_SCHEMA}`;
const VENDOR_CACHE = `bookdoe-vendor-${CACHE_SCHEMA}`;
const CONTENT_PREFIX = `bookdoe-content-${CACHE_SCHEMA}-`;
const PWA_NS = '/__bookdoe_pwa/';
const SESSION_KEY = PWA_NS + 'user';
const REVOKED_PREFIX = PWA_NS + 'revoked/';

// Two shells: the Player (/app, also served at /) and Management (/manage). Both are
// precached so either opens offline; a navigation is answered with the shell its path
// belongs to (shellKeyFor), never the other one.
const SHELL_URLS = [
  '/app',
  '/manage',
  '/assets/styles.css',
  '/assets/app.js',
  '/assets/player.css',
  '/assets/player.js',
  '/assets/consume.js',
  '/assets/consume.css',
  '/assets/pwa.js',
  '/manifest.webmanifest',
  '/assets/icons/icon-192.png',
  '/assets/icons/icon-512.png',
  '/assets/icons/icon-512-maskable.png',
  '/assets/icons/apple-touch-icon.png',
  '/assets/brand/bookdoe-mark.svg',
  '/assets/brand/bookdoe-horizontal-light.svg',
  '/assets/brand/bookdoe-horizontal-dark.svg',
];

// Path shapes are matched segment by segment (no regular expressions on request paths, so no
// backtracking). A segment is a literal, '*' (any one non-empty segment), 'name@*' (a literal
// prefix, then anything without '/'), or 'a|b' (either literal).
const CONTENT_BYTES = [
  ['api', 'v1', 'library', '*', 'files', '*'],   // reader/player stream (Range-capable)
  ['opds', 'files', '*'],
  ['api', 'v1', 'library', '*', 'kobo', '*'],
];
const CONTENT_COVER = [['api', 'v1', 'library', '*', 'cover'], ['api', 'player', 'v1', 'library', '*', 'cover']];
const CONTENT_JSON = [
  ['api', 'v1', 'library', '*'],                 // work detail (the shelf page)
  ['api', 'v1', 'library', '*', 'read', '*'],    // reader/player manifest: tracks, chapters, position
  ['api', 'player', 'v1', 'library', '*'],        // Player API work detail
  ['api', 'player', 'v1', 'ebook|audio', '*', '*'], // Player API manifests
  ['api', 'player', 'v1', 'home'],                 // Home screen: last known state offline
];
// Pinned by app.js (loadScript) with an integrity hash; only these exact files.
const VENDOR_HOSTS = new Set(['cdn.jsdelivr.net']);
const VENDOR_PATHS = [['npm', 'jszip@*', 'dist', 'jszip.min.js'], ['npm', 'epubjs@*', 'dist', 'epub.min.js']];

function segmentMatches(want, got) {
  if (got === '') return false;
  if (want === '*') return true;
  if (want.endsWith('*')) return got.startsWith(want.slice(0, -1)) && got.length > want.length - 1;
  return want.split('|').includes(got);
}

function pathMatches(shape, path) {
  if (!path.startsWith('/')) return false;
  const parts = path.slice(1).split('/');
  return parts.length === shape.length && shape.every((want, i) => segmentMatches(want, parts[i]));
}

/* ---------- classification (pure; exercised directly by tests/test_pwa.py) ---------- */

function classify(url, method) {
  if (method !== 'GET') return 'bypass';
  const p = url.pathname;
  if (url.origin !== self.location.origin) {
    return VENDOR_HOSTS.has(url.hostname) && VENDOR_PATHS.some(sh => pathMatches(sh, p)) ? 'vendor' : 'bypass';
  }
  if (p === '/sw.js' || p.startsWith('/__bookdoe_pwa/')) return 'bypass';
  if (p === '/manifest.webmanifest' || p.startsWith('/assets/')) return 'shell';
  if (CONTENT_BYTES.some(sh => pathMatches(sh, p))) return 'content-bytes';
  if (CONTENT_COVER.some(sh => pathMatches(sh, p))) return 'content-cover';
  if (!url.search && CONTENT_JSON.some(sh => pathMatches(sh, p))) return 'content-json';
  if (p.startsWith('/api/') || p.startsWith('/opds/') || p.startsWith('/auth/')) return 'bypass';
  // Any other path is the SPA fallback (main.py serves index.html for it).
  return 'navigation';
}

/* ---------- user bookkeeping ---------- */

async function currentUser() {
  const meta = await caches.open(META_CACHE);
  const hit = await meta.match(SESSION_KEY);
  if (!hit) return null;
  const uid = (await hit.text()).trim();
  return uid || null;
}

async function setUser(uid) {
  const meta = await caches.open(META_CACHE);
  if (!uid) { await meta.delete(SESSION_KEY); return; }
  await meta.put(SESSION_KEY, new Response(String(uid), { headers: { 'Content-Type': 'text/plain' } }));
}

function contentCacheName(uid) {
  // Cache names are plain strings; keep the id URL-safe so an odd id can't collide.
  return CONTENT_PREFIX + encodeURIComponent(uid);
}

async function dropUserContent(uid) {
  if (uid) await caches.delete(contentCacheName(uid));
}

/* Offline copies the server took away. A stored file is dropped when the server says
 * 403/404 for it (see dropIfRevoked). That is correct but it must not be silent: the
 * path is remembered here (per user, in the small meta cache, so it survives the worker
 * being stopped) and open pages are told, so the book page can say "Download again"
 * instead of looking as if nothing was ever saved. It is only a memo. Nothing about it
 * blocks serving: once the server answers 200 again the file is fetched and stored like
 * any other, and a successful download or "Remove from device" clears the memo. */
async function revokedList(uid) {
  const meta = await caches.open(META_CACHE);
  const hit = await meta.match(REVOKED_PREFIX + encodeURIComponent(uid));
  if (!hit) return [];
  try { const v = JSON.parse(await hit.text()); return Array.isArray(v) ? v : []; } catch (_) { return []; }
}
async function writeRevoked(uid, urls) {
  const meta = await caches.open(META_CACHE);
  const key = REVOKED_PREFIX + encodeURIComponent(uid);
  if (!urls.length) { await meta.delete(key); return; }
  await meta.put(key, new Response(JSON.stringify(urls.slice(-200)), { headers: { 'Content-Type': 'application/json' } }));
}
async function noteRevoked(uid, href) {
  const path = new URL(href).pathname;
  const list = await revokedList(uid);
  if (!list.includes(path)) await writeRevoked(uid, [...list, path]);
  if (self.clients && self.clients.matchAll) {
    const pages = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const page of pages) { try { page.postMessage({ type: 'content-revoked', urls: [path] }); } catch (_) {} }
  }
}
async function clearRevoked(uid, href) {
  const path = new URL(href).pathname;
  const list = await revokedList(uid);
  if (list.includes(path)) await writeRevoked(uid, list.filter(u => u !== path));
}

/* meta/vendor/content caches of a different CACHE_SCHEMA belong to code that no longer exists. */
function isStaleSchemaCache(name) {
  const m = /^bookdoe-(meta|vendor|content)-(v\d+)(?:-|$)/.exec(name);
  return !!m && m[2] !== CACHE_SCHEMA;
}

/* ---------- install / activate ---------- */

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // addAll is atomic; one missing file would fail the whole install, and the icons
    // are not worth that. Fetch individually and keep what succeeded.
    // The pages reference /assets/*.js|css as ?v=<build> (main.py _shell), so store them under
    // that exact URL; offline the page finds what it asks for.
    await Promise.all(SHELL_URLS.map(async u => {
      const key = u.startsWith('/assets/') && (u.endsWith('.js') || u.endsWith('.css')) ? `${u}?v=${encodeURIComponent(BUILD)}` : u;
      try {
        const r = await fetch(key, { cache: 'no-cache' });
        if (r.ok) await cache.put(key, r);
      } catch (_) { /* offline during install: the fetch handler fills it later */ }
    }));
    // No skipWaiting here: an installed worker waits until the person taps "Update now"
    // (message 'skip-waiting'), so a page never swaps code under an open book. The very
    // first install has nothing to wait for and activates by itself.
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    // Only old shell caches go. Content (offline books), meta (who is signed in) and vendor
    // caches are the person's, not the build's.
    const names = await caches.keys();
    await Promise.all(names
      .filter(n => (n.startsWith('bookdoe-shell-') && n !== SHELL_CACHE) || isStaleSchemaCache(n))
      .map(n => caches.delete(n)));
    await self.clients.claim();
    // Tell open pages a new worker took over, so the Player can reload when it is not
    // in the middle of a book (player.js listens through pwa.js). The shell files are
    // revalidated per request, so this is about not leaving a stale bundle running
    // against a freshly deployed backend.
    if (self.clients.matchAll) {
      const pages = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const page of pages) { try { page.postMessage({ type: 'sw-updated', version: BUILD }); } catch (_) {} }
    }
  })());
});

/* ---------- messages from the page (pwa.js) ---------- */

self.addEventListener('message', event => {
  const msg = event.data || {};
  const reply = result => { if (event.ports && event.ports[0]) event.ports[0].postMessage(result); };
  event.waitUntil((async () => {
    try {
      if (msg.type === 'skip-waiting') {
        reply({ ok: true, build: BUILD });
        await self.skipWaiting();
      } else if (msg.type === 'get-build') {
        reply({ ok: true, build: BUILD });
      } else if (msg.type === 'set-user') {
        const prev = await currentUser();
        if (prev && prev !== String(msg.uid)) await dropUserContent(prev);
        await setUser(msg.uid);
        reply({ ok: true });
      } else if (msg.type === 'clear-user') {
        const prev = await currentUser();
        await dropUserContent(prev);
        await setUser(null);
        reply({ ok: true });
      } else if (msg.type === 'cache-content') {
        // The reader/player (or the "Keep offline" button) asks for a whole file to be
        // stored so it plays and seeks offline. Fetch without a Range header so the
        // cached copy is the complete 200 body (see rangeFromCache).
        const uid = await currentUser();
        if (!uid) { reply({ ok: false, error: 'not signed in' }); return; }
        const url = new URL(msg.url, self.location.origin);
        const kind = classify(url, 'GET');
        if (!kind.startsWith('content-')) { reply({ ok: false, error: 'not cacheable content' }); return; }
        const r = await fetch(new Request(url.href, { credentials: 'same-origin' }));
        if (!r.ok || r.status !== 200) { reply({ ok: false, error: `HTTP ${r.status}` }); return; }
        const cache = await caches.open(contentCacheName(uid));
        await cache.put(url.href, r);
        await clearRevoked(uid, url.href);
        reply({ ok: true });
      } else if (msg.type === 'is-cached') {
        const uid = await currentUser();
        if (!uid) { reply({ ok: true, cached: false }); return; }
        const cache = await caches.open(contentCacheName(uid));
        const hit = await cache.match(new URL(msg.url, self.location.origin).href);
        reply({ ok: true, cached: !!hit });
      } else if (msg.type === 'forget-content') {
        const uid = await currentUser();
        if (uid) {
          const cache = await caches.open(contentCacheName(uid));
          await cache.delete(new URL(msg.url, self.location.origin).href);
          await clearRevoked(uid, new URL(msg.url, self.location.origin).href);
        }
        reply({ ok: true });
      } else if (msg.type === 'list-revoked') {
        const uid = await currentUser();
        reply({ ok: true, urls: uid ? await revokedList(uid) : [] });
      } else if (msg.type === 'list-content') {
        const uid = await currentUser();
        let urls = [];
        if (uid) {
          const cache = await caches.open(contentCacheName(uid));
          urls = (await cache.keys()).map(r => new URL(r.url).pathname);
        }
        reply({ ok: true, urls });
      } else {
        reply({ ok: false, error: 'unknown message' });
      }
    } catch (err) {
      reply({ ok: false, error: String(err && err.message || err) });
    }
  })());
});

/* ---------- fetch strategies ---------- */

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  // Keyed by the full URL: /assets/app.js?v=<new build> must not be answered with the copy
  // stored for the previous build.
  const cached = await cache.match(request.url);
  const refresh = fetch(request).then(r => {
    if (r && r.ok) cache.put(request.url, r.clone()).catch(() => {});
    return r;
  }).catch(() => null);
  if (cached) { refresh.catch(() => {}); return cached; }
  const fresh = await refresh;
  if (fresh) return fresh;
  // Offline and this exact URL was never stored (e.g. a page cached by an older build):
  // any stored copy of the same file beats an error page.
  return (await cache.match(request.url, { ignoreSearch: true })) || offlineResponse(request);
}

/* Which precached shell answers a navigation: /manage and below is Management, every
 * other path (/, /app, /app/anything, old deep links) is the Player. */
function shellKeyFor(pathname) {
  return pathname === '/manage' || pathname.startsWith('/manage/') ? '/manage' : '/app';
}

async function navigationResponse(request) {
  const cache = await caches.open(SHELL_CACHE);
  const key = shellKeyFor(new URL(request.url).pathname);
  try {
    const r = await fetch(request);
    // A 403 from /manage (non-admin) or a redirect must not overwrite the stored shell.
    if (r && r.ok) cache.put(key, r.clone()).catch(() => {});
    return r;
  } catch (_) {
    const cached = await cache.match(key);
    return cached || offlineResponse(request);
  }
}

/* Parse "bytes=a-b" against a total length; null when unsatisfiable or absent. */
function parseRange(header, total) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header || '');
  if (!m) return null;
  let start = m[1] === '' ? null : Number(m[1]);
  let end = m[2] === '' ? null : Number(m[2]);
  if (start === null && end === null) return null;
  if (start === null) { start = Math.max(0, total - end); end = total - 1; }       // suffix: -500
  else if (end === null || end >= total) end = total - 1;
  if (start > end || start >= total) return null;
  return { start, end };
}

/* <audio>/<video> and PDF viewers ask for byte ranges. Cache Storage stores the full
 * 200 body; slice it here so seeking works offline, and never store a 206 as if it
 * were the whole file. */
async function rangeFromCache(cached, request) {
  const rangeHeader = request.headers.get('range');
  if (!rangeHeader) return cached;
  const buf = await cached.arrayBuffer();
  const range = parseRange(rangeHeader, buf.byteLength);
  if (!range) {
    return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${buf.byteLength}` } });
  }
  const headers = new Headers(cached.headers);
  headers.set('Content-Range', `bytes ${range.start}-${range.end}/${buf.byteLength}`);
  headers.set('Content-Length', String(range.end - range.start + 1));
  headers.set('Accept-Ranges', 'bytes');
  return new Response(buf.slice(range.start, range.end + 1), { status: 206, headers });
}

/* Access can be taken away after a copy was stored (an admin removes the kids tag from a
 * book): a cache-first answer would keep serving it to that kid. So whenever a stored
 * copy is used and the network is reachable, ask the server in the background with a
 * one-byte Range request (headers only; the body is aborted) and drop the copy when the
 * server now says 403/404. Offline, the copy is still served: that is what it is for.
 * At most one check per URL per minute. */
const _lastCheck = new Map();
function dropIfRevoked(url, cacheName, uid, remember) {
  const now = Date.now();
  if (now - (_lastCheck.get(url) || 0) < 60000) return;
  _lastCheck.set(url, now);
  if (_lastCheck.size > 500) _lastCheck.clear();
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  fetch(url, { headers: { Range: 'bytes=0-0' }, credentials: 'same-origin', signal: ctl && ctl.signal }).then(async r => {
    if (ctl) ctl.abort();
    if (r.status === 403 || r.status === 404) {
      const c = await caches.open(cacheName); await c.delete(url, { ignoreVary: true });
      if (remember && uid) await noteRevoked(uid, url);
    }
  }).catch(() => {});
}

async function contentBytes(request, uid) {
  const cache = await caches.open(contentCacheName(uid));
  const cached = await cache.match(request.url, { ignoreVary: true });
  if (cached) { dropIfRevoked(request.url, contentCacheName(uid), uid, true); return rangeFromCache(cached, request); }
  const r = await fetch(request);
  // Only a complete, successful body is worth keeping; a Range reply (206) is a slice.
  if (r && r.status === 200 && !request.headers.get('range')) cache.put(request.url, r.clone()).then(() => clearRevoked(uid, request.url)).catch(() => {});
  return r;
}

async function cacheFirst(request, uid) {
  const cache = await caches.open(contentCacheName(uid));
  const cached = await cache.match(request.url);
  if (cached) { dropIfRevoked(request.url, contentCacheName(uid), uid, false); return cached; }
  const r = await fetch(request);
  if (r && r.ok) cache.put(request.url, r.clone()).catch(() => {});
  return r;
}

async function networkFirst(request, uid) {
  const cache = await caches.open(contentCacheName(uid));
  try {
    const r = await fetch(request);
    if (r && r.ok) cache.put(request.url, r.clone()).catch(() => {});
    // The server answered and refused: the stored copy must not outlive that.
    else if (r && (r.status === 403 || r.status === 404)) cache.delete(request.url).catch(() => {});
    return r;
  } catch (_) {
    const cached = await cache.match(request.url);
    return cached || offlineResponse(request);
  }
}

function offlineResponse(request) {
  const accept = request.headers.get('accept') || '';
  if (accept.includes('text/html')) {
    return new Response(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<title>{{app_name}} is offline</title><body style="font-family:system-ui;background:#0b0f17;color:#f2f5f9;display:grid;place-items:center;height:100vh;margin:0">' +
      '<div style="text-align:center"><h1 style="font-size:20px">You are offline</h1><p style="color:#92a0b5">Open {{app_name}} once while online so the app shell is saved for offline use.</p></div>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }
  return new Response(JSON.stringify({ detail: 'offline' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
}

async function vendorCacheFirst(request) {
  const cache = await caches.open(VENDOR_CACHE);
  const cached = await cache.match(request.url);
  if (cached) return cached;
  const r = await fetch(request);
  // Only a CORS-readable 200 is stored: an opaque response could not be checked
  // against the integrity hash and would fail the <script> anyway.
  if (r && r.ok && r.type !== 'opaque') cache.put(request.url, r.clone()).catch(() => {});
  return r;
}

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  const kind = classify(url, request.method);
  if (kind === 'bypass') return;

  if (kind === 'shell') { event.respondWith(staleWhileRevalidate(request, SHELL_CACHE)); return; }
  if (kind === 'navigation') { event.respondWith(navigationResponse(request)); return; }
  if (kind === 'vendor') { event.respondWith(vendorCacheFirst(request)); return; }

  event.respondWith((async () => {
    const uid = await currentUser();
    // Nobody recorded as signed in: behave like no worker at all.
    if (!uid) return fetch(request);
    if (kind === 'content-bytes') return contentBytes(request, uid);
    if (kind === 'content-cover') return cacheFirst(request, uid);
    return networkFirst(request, uid);   // content-json
  })());
});

// Exposed for the node test harness (tests/test_pwa.py); harmless in a browser.
if (typeof self !== 'undefined') {
  self.__bookdoeSw = { classify, parseRange, rangeFromCache, contentCacheName, shellKeyFor, SHELL_URLS, VERSION, BUILD, SHELL_CACHE, META_CACHE, CONTENT_PREFIX };
}
