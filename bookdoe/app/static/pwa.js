/* PWA glue for the page: registers the service worker (app-wide, from index.html),
 * tells it who is signed in, and gives the UI and the reader/player a small API for
 * keeping books and audiobooks available offline.
 *
 *   bookdoePwa.setUser(uid)        after /api/v1/auth/me succeeds (app.js initAuth)
 *   bookdoePwa.clearUser()         on sign-out; also deletes that user's content cache
 *   bookdoePwa.cacheContent(url)   store /opds/files/<id> (or a kobo URL) for offline
 *   bookdoePwa.isCached(url)       -> boolean
 *   bookdoePwa.forgetContent(url)  drop one stored file
 *   bookdoePwa.listContent()       -> [pathname, ...] of stored files for this user
 *   bookdoePwa.listRevoked()       -> [pathname, ...] of stored files the server took away
 *                                        (the book left this account's shelf); cleared by a
 *                                        new download or by forgetContent
 *   bookdoePwa.onRevoked(cb)       cb({urls}) when the worker drops such a file
 *   bookdoePwa.installPrompt()     -> true if a Chromium install prompt was shown
 *   bookdoePwa.isStandalone()      running as an installed app (iOS or manifest)
 *   bookdoePwa.onUpdate(cb)        cb({version}) when a new worker has taken over
 *   bookdoePwa.checkForUpdate()    ask the browser to re-fetch sw.js now
 *   bookdoePwa.build               {sha, built} this page was served with (also window.__ALX_BUILD__)
 *   bookdoePwa.serverVersion()     -> {sha, built_at} from GET /api/v1/version, or null offline
 *   bookdoePwa.watchVersion(cb)    cb({client, server, mismatch, waiting}) on start, on focus and
 *                                     on visibility, at most every 5 minutes, and when a worker waits
 *   bookdoePwa.hasWaiting()        a new worker is installed and waiting for "Update now"
 *   bookdoePwa.applyUpdate()       skipWaiting + reload (or a plain reload when nothing waits)
 *
 * Every call resolves to {ok:false} rather than throwing when there is no worker
 * (private browsing, http:// on a LAN host, old Safari), so callers can ignore it.
 */
(function () {
  'use strict';
  const supported = 'serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1');
  let registration = null;
  let deferredInstall = null;

  async function ready() {
    if (!supported) return null;
    if (registration) return registration;
    try { registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' }); }
    catch (err) { console.warn('bookdoe: service worker registration failed', err); return null; }
    return registration;
  }

  async function send(message) {
    const reg = await ready();
    const worker = reg && (reg.active || reg.waiting || reg.installing);
    if (!worker) return { ok: false, error: 'no service worker' };
    return new Promise(resolve => {
      const channel = new MessageChannel();
      const timer = setTimeout(() => resolve({ ok: false, error: 'timeout' }), 30000);
      channel.port1.onmessage = ev => { clearTimeout(timer); resolve(ev.data); };
      worker.postMessage(message, [channel.port2]);
    });
  }

  window.addEventListener('beforeinstallprompt', ev => { ev.preventDefault(); deferredInstall = ev; });
  const updateListeners = [];
  const revokedListeners = [];
  if (supported) {
    navigator.serviceWorker.addEventListener('message', ev => {
      if (ev.data && ev.data.type === 'content-revoked') revokedListeners.forEach(cb => { try { cb(ev.data); } catch (_) {} });
      if (ev.data && ev.data.type === 'sw-updated') updateListeners.forEach(cb => { try { cb(ev.data); } catch (_) {} });
    });
  }

  // The sha this page was served with: the shell stamps <meta name="alx-build"> (main.py _shell),
  // the same sha that is on its script/style URLs. window.__ALX_BUILD__ is the page's own id.
  const meta = document.querySelector('meta[name="alx-build"]');
  const build = { sha: (meta && meta.content) || 'dev', built: (meta && meta.dataset.built) || '' };
  window.__ALX_BUILD__ = build;

  async function serverVersion() {
    try {
      const r = await fetch('/api/v1/version', { cache: 'no-store', headers: { Accept: 'application/json' } });
      return r.ok ? await r.json() : null;
    } catch (_) { return null; }
  }
  const hasWaiting = () => !!(registration && registration.waiting && navigator.serviceWorker.controller);

  const watchers = [];
  let lastCheck = 0, checking = false;
  async function runWatch(force) {
    if (checking || !watchers.length) return;
    if (!force && Date.now() - lastCheck < 5 * 60 * 1000) return;
    checking = true; lastCheck = Date.now();
    try {
      const reg = await ready();
      if (reg) { try { await reg.update(); } catch (_) {} }
      const server = await serverVersion();
      const state = { client: build, server, mismatch: !!(server && server.sha !== build.sha), waiting: hasWaiting() };
      watchers.forEach(cb => { try { cb(state); } catch (_) {} });
    } finally { checking = false; }
  }
  function watchVersion(cb) {
    watchers.push(cb);
    runWatch(true);
    if (watchers.length === 1) {
      window.addEventListener('focus', () => runWatch(false));
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') runWatch(false); });
    }
  }
  // A worker that finishes installing while the page runs: tell the watchers right away.
  // Another tab applied an update: this page still runs the old bundle, so say so.
  if (supported && navigator.serviceWorker.controller) navigator.serviceWorker.addEventListener('controllerchange', () => runWatch(true));
  function trackWaiting(reg) {
    const announce = () => { if (hasWaiting()) runWatch(true); };
    reg.addEventListener('updatefound', () => {
      const w = reg.installing;
      if (w) w.addEventListener('statechange', () => { if (w.state === 'installed') announce(); });
    });
    announce();
  }

  async function applyUpdate() {
    const reg = await ready();
    if (reg) {
      // Not installed yet (the server is ahead of the worker): fetch it first, but do not wait long.
      if (!reg.waiting) {
        try { await reg.update(); } catch (_) {}
        for (let i = 0; i < 20 && !reg.waiting && reg.installing; i++) await new Promise(res => setTimeout(res, 250));
      }
      if (reg.waiting) {
        await new Promise(resolve => {
          const done = () => resolve();
          navigator.serviceWorker.addEventListener('controllerchange', done, { once: true });
          setTimeout(done, 3000);
          try { reg.waiting.postMessage({ type: 'skip-waiting' }); } catch (_) { done(); }
        });
      }
    }
    location.reload();
  }

  window.bookdoePwa = {
    supported,
    build,
    serverVersion,
    watchVersion,
    hasWaiting,
    applyUpdate,
    ready,
    ready,
    setUser: uid => send({ type: 'set-user', uid: String(uid) }),
    clearUser: () => send({ type: 'clear-user' }),
    cacheContent: url => send({ type: 'cache-content', url }),
    isCached: async url => { const r = await send({ type: 'is-cached', url }); return !!(r && r.cached); },
    forgetContent: url => send({ type: 'forget-content', url }),
    listRevoked: async () => { const r = await send({ type: 'list-revoked' }); return (r && r.urls) || []; },
    onRevoked: cb => { revokedListeners.push(cb); },
    listContent: async () => { const r = await send({ type: 'list-content' }); return (r && r.urls) || []; },
    isStandalone: () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true,
    onUpdate: cb => { updateListeners.push(cb); },
    checkForUpdate: async () => { const reg = await ready(); if (reg) { try { await reg.update(); } catch (_) {} } },
    installPrompt: async () => {
      if (!deferredInstall) return false;
      const ev = deferredInstall; deferredInstall = null;
      ev.prompt(); await ev.userChoice; return true;
    },
  };

  if (supported) window.addEventListener('load', () => { ready().then(reg => { if (reg) trackWaiting(reg); }); });
})();

// Image fallbacks without inline handlers (CSP forbids onerror="..."). The error
// event does not bubble, so listen in the capture phase on the document.
document.addEventListener('error', (ev) => {
  const el = ev.target;
  if (!el || el.tagName !== 'IMG' || !el.dataset) return;
  if (el.dataset.imgFallback === 'remove') el.remove();
  else if (el.dataset.imgFallback === 'hide') el.style.display = 'none';
}, true);
