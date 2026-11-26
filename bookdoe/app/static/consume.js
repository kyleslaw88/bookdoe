/* Shared reader and audiobook player, used by both shells (Player at /app, Management
 * at /manage). ES module; loaded on demand with import().
 *
 *   const cleanup = await open({workId, assetId, host, api, toast, modal, closeModal, onBack, chrome})
 *
 * `host` is the element the page renders into; `api` is the shell's fetch wrapper (it
 * adds the CSRF header); `chrome` is 'player' or 'management' (the Player keeps audio
 * playing behind a mini-player when the page is left; Management stops it).
 *
 * EPUB is rendered by epub.js (BSD-2-Clause) with JSZip (MIT), pinned and
 * integrity-checked from jsdelivr (sw.js caches exactly these two files); nothing here
 * parses an EPUB by hand. PDF uses the browser's viewer in an iframe. Audio is an
 * HTML5 <audio> over the Range-capable /files route. Positions are saved per account
 * per asset through PUT /progress: debounced while moving, flushed when the page is
 * left, and never saved as "0" over a real position. Reading preferences (theme, font,
 * size, spacing, margins) and bookmarks are per account, kept through the preferences
 * and bookmarks endpoints when present and in localStorage as the offline fallback. */

const VENDOR_SCRIPTS = {
  jszip: ['https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js', 'sha384-+mbV2IY1Zk/X1p/nWllGySJSUN8uMs+gUAN10Or95UBH0fpj6GfKgPmgC5EXieXG'],
  epubjs: ['https://cdn.jsdelivr.net/npm/epubjs@0.3.93/dist/epub.min.js', 'sha384-ZqmkDa/1AGF4SKdSmXgEG+3G+Gsb6yNqUSq2Mqp0jDf1GWk+UY5eNbEd7hgwiywI'],
};
const loaded = {};
function loadScript(name) {
  if (loaded[name]) return loaded[name];
  const [src, integrity] = VENDOR_SCRIPTS[name];
  loaded[name] = new Promise((resolve, reject) => {
    const s = document.createElement('script'); s.src = src; s.integrity = integrity; s.crossOrigin = 'anonymous';
    s.onload = () => resolve(); s.onerror = () => { delete loaded[name]; reject(new Error(`Could not load ${name} from the CDN`)); };
    document.head.appendChild(s);
  });
  return loaded[name];
}

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (v = '') => String(v ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
export const fmtClock = s => { s = Number.isFinite(Number(s)) ? Math.max(0, Math.floor(Number(s))) : 0; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(x).padStart(2, '0')}`; };

// <reader-relocate>
// epub.js runs 'relocated' listeners inside its display queue: a listener that throws (e.g.
// percentageFromCfi on an odd CFI) leaves the queue wedged and every later next()/prev() does
// nothing. Listeners are wrapped so a failure here can never stop page turns (#712).
function readerPercent(book, cfi) {
  try {
    const n = book?.locations?.length?.();
    if (!n) return null;
    const p = book.locations.percentageFromCfi(cfi);
    return typeof p === 'number' && isFinite(p) ? p : null;
  } catch { return null; }
}
function safeRelocated(fn) {
  return loc => { if (!loc?.start?.cfi) return; try { fn(loc); } catch (e) { console.warn('relocated handler failed', e); } };
}
// </reader-relocate>

/* ---------- progress saver ---------- */
// One saver per page: coalesces rapid position changes; flush() is what the cleanup
// hook awaits so the last position survives navigating away. Each write carries the
// moment the position was captured and a stable per-browser device id so the server can
// order updates from several devices (see reader.set_progress's conflict rule).
export function deviceId() {
  let id = localStorage.getItem('alx-device-id');
  if (!id) { id = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('')); localStorage.setItem('alx-device-id', id); }
  return id;
}

// Failure policy. A failed save is never retried in a tight loop: whatever the cause
// (the book left the shelf, the server is down, a bug in this file) every book is held
// to PROGRESS_POLICY.windowMax requests a minute and, after any failure, one request per
// minGapMs. Live incident: a kid's client sent ~140 PUTs a minute for 30 minutes at a
// book an admin had taken off the kids shelf.
export const PROGRESS_POLICY = Object.freeze({
  minGapMs: 5000,            // after a failure: at most one request per book per 5 s
  baseMs: 5000, maxMs: 300000, factor: 2, jitter: 0.25,   // retryable failures: 5 s, 10 s, 20 s ... capped at 5 min, +-25%
  maxRapidAttempts: 6,       // retryable failures in a row before only the slow cadence (maxMs) is used
  probeBaseMs: 60000, probeMaxMs: 300000,                 // a book that is gone: look again after 1, 2, 4, then every 5 min
  windowMs: 60000, windowMax: 10,                         // hard ceiling per book, success or not
  pendingTtlMs: 30 * 24 * 3600 * 1000,                    // a saved position nobody could deliver for 30 days is let go
});
// 404/403/410: the server understood and the book is not available to this account now
// ("unavailable": keep the place, look again later). Network errors, 5xx, 429 and
// auth hiccups are "retry" (back off). Anything else (400/422) can never succeed as
// sent: "drop".
export function classifyProgressError(er) {
  const s = er && er.status;
  if (s === 404 || s === 403 || s === 410) return 'unavailable';
  if (!s || s >= 500 || s === 429 || s === 408 || s === 401) return 'retry';
  return 'drop';
}
export function backoffDelay(attempt, { policy = PROGRESS_POLICY, random = Math.random, retryAfterMs = 0 } = {}) {
  const raw = Math.min(policy.maxMs, policy.baseMs * policy.factor ** Math.max(0, attempt - 1));
  return Math.max(policy.minGapMs, retryAfterMs || 0, Math.round(raw * (1 + policy.jitter * (random() * 2 - 1))));
}
export function probeDelay(k, { policy = PROGRESS_POLICY, random = Math.random } = {}) {
  const raw = Math.min(policy.probeMaxMs, policy.probeBaseMs * 2 ** Math.max(0, k));
  return Math.max(policy.minGapMs, Math.round(raw * (1 + policy.jitter * (random() * 2 - 1))));
}
const retryAfterMs = er => { const s = Number(er && er.retryAfter); return Number.isFinite(s) && s > 0 ? Math.min(s, 600) * 1000 : 0; };

// Positions the server has not accepted (offline, or the book is off the shelf for now)
// wait in localStorage, one per asset, stamped with the clock of the moment they were
// made. replayPendingProgress() sends them as one batch when the page comes back online
// or the book comes back; the server's conflict rule decides, so a stale offline
// position never overwrites a newer one. Nothing is thrown away because of a 404.
const PENDING_KEY = 'alx-pending-progress';
function readPending() { try { return JSON.parse(localStorage.getItem(PENDING_KEY) || '{}'); } catch { return {}; } }
function writePending(all) { try { if (Object.keys(all).length) localStorage.setItem(PENDING_KEY, JSON.stringify(all)); else localStorage.removeItem(PENDING_KEY); } catch {} }
export function queuePendingProgress(assetId, body) { const all = readPending(); all[assetId] = { ...body, asset_id: assetId }; writePending(all); }
export function pendingProgressFor(assetId) { return readPending()[assetId] || null; }
export function dropPendingProgress(assetId) { const all = readPending(); if (assetId in all) { delete all[assetId]; writePending(all); } }
const replayGate = { nextAt: 0, misses: 0 };
export function resetReplayGate() { replayGate.nextAt = 0; replayGate.misses = 0; }
export async function replayPendingProgress(api, { now = Date.now, random = Math.random, policy = PROGRESS_POLICY } = {}) {
  const t = now();
  if (t < replayGate.nextAt) return { replayed: 0, deferred: true };
  const all = readPending();
  for (const [id, body] of Object.entries(all)) if (t - (Number(body.client_updated_at) * 1000 || t) > policy.pendingTtlMs) delete all[id];
  writePending(all);
  const items = Object.values(all);
  if (!items.length) { resetReplayGate(); return { replayed: 0 }; }
  replayGate.nextAt = t + policy.minGapMs;      // one batch per 5 s whatever calls this
  let r;
  try { r = await api('/api/player/v1/progress', { method: 'PUT', body: { items } }); }
  catch (er) {
    const kind = classifyProgressError(er);
    if (kind === 'drop') {
      // The server refused the batch as malformed. One bad entry must not cost the others
      // their place: try them one at a time (a handful at most) and let go only of those refused.
      let dropped = 0, replayed = 0, stale = 0;
      const left = readPending();
      for (const body of items.slice(0, 10)) {
        try {
          const one = await api('/api/player/v1/progress', { method: 'PUT', body: { items: [body] } });
          const res = (one.items || [])[0] || {};
          if (res.applied) { replayed++; delete left[body.asset_id]; } else if (!res.error) { stale++; delete left[body.asset_id]; }
        } catch (e2) { if (classifyProgressError(e2) === 'drop') { dropped++; delete left[body.asset_id]; } else break; }
      }
      writePending(left);
      return { replayed, stale, dropped, kept: Object.keys(left).length, error: er.message };
    }
    replayGate.misses++;
    replayGate.nextAt = t + backoffDelay(replayGate.misses, { policy, random, retryAfterMs: retryAfterMs(er) });
    return { replayed: 0, kept: items.length, error: er.message };
  }
  let replayed = 0, stale = 0, kept = 0;
  const left = readPending();
  const byAsset = new Map((r.items || []).map(i => [i.asset_id, i]));
  for (const body of items) {
    const res = byAsset.get(body.asset_id);
    if (!res) { kept++; continue; }
    if (res.applied) { replayed++; delete left[body.asset_id]; }
    else if (res.error === 'not_found' || res.error === 'rate_limited') kept++;   // the book is off the shelf for now: keep the place
    else { if (!res.error) stale++; delete left[body.asset_id]; }                  // newer elsewhere, or can never be accepted
  }
  writePending(left);
  if (kept) { replayGate.misses++; replayGate.nextAt = t + probeDelay(replayGate.misses - 1, { policy, random }); }
  else resetReplayGate();
  return { replayed, stale, kept };
}

// The saver is a small state machine: 'ok' -> 'retrying' (network/5xx/429: backoff) or
// 'unavailable' (404/403/410: the book is not on this account's shelf right now). While
// not 'ok' the latest position is kept in localStorage and a gentle probe (the same PUT,
// with the real position) runs on a growing delay; the first success replays the place
// and reports state 'ok', which is the page's cue to recover playback.
export function progressSaver(api, workId, assetId, opts = {}) {
  const P = { ...PROGRESS_POLICY, ...(opts.policy || {}) };
  const now = opts.now || Date.now, setT = opts.setTimeout || setTimeout, clearT = opts.clearTimeout || clearTimeout, random = opts.random || Math.random;
  const onState = opts.onState || (() => {});
  // Podcast episodes reuse this saver with their own request (opts.send) and keep no offline queue
  // (opts.pending === false): the shared queue replays through the book-only batch endpoint.
  const url = `/api/v1/library/${encodeURIComponent(workId)}/progress/${encodeURIComponent(assetId)}`;
  const queuePending = opts.pending === false ? () => {} : queuePendingProgress;
  const dropPending = opts.pending === false ? () => {} : dropPendingProgress;
  let latest = null, state = 'ok', failures = 0, probes = 0, nextAt = 0, timer = null, timerAt = 0, busy = null, disposed = false, lastKey = '';
  const sent = [];
  const setState = s => { if (s === state) return; const prev = state; state = s; try { onState(s, prev); } catch (er) { console.warn('progress state hook failed', er); } };
  const ceilingAt = () => { const t = now(); while (sent.length && t - sent[0] >= P.windowMs) sent.shift(); return sent.length >= P.windowMax ? sent[0] + P.windowMs : 0; };
  const schedule = delay => {
    if (disposed) return;
    const at = Math.max(now() + Math.max(0, delay), nextAt, ceilingAt());
    if (timer && timerAt <= at) return;          // the pending timer fires first and re-checks every limit
    if (timer) clearT(timer);
    timerAt = at; timer = setT(() => { timer = null; attempt(); }, Math.max(0, at - now()));
  };
  const attempt = async () => {
    if (disposed || busy || !latest) return busy;
    const t = now();
    if (t < nextAt || t < ceilingAt()) { schedule(0); return; }
    const body = latest; sent.push(t);
    busy = (async () => {
      try {
        await (opts.send ? opts.send(body) : api(url, { method: 'PUT', body }));
        lastKey = JSON.stringify({ ...body, client_updated_at: 0 });
        if (latest === body) latest = null;
        failures = 0; probes = 0; nextAt = 0; dropPending(assetId);
        setState('ok');
        if (latest) schedule(P.minGapMs);
      } catch (er) {
        const kind = classifyProgressError(er);
        console.warn('progress not saved', kind, er && er.message);
        if (kind === 'drop') { if (latest === body) latest = null; return; }
        queuePending(assetId, body);      // the listener's place stays on this device
        let delay;
        if (kind === 'unavailable') { delay = probeDelay(probes++, { policy: P, random }); setState('unavailable'); }
        else { failures++; delay = failures > P.maxRapidAttempts ? P.maxMs : backoffDelay(failures, { policy: P, random, retryAfterMs: retryAfterMs(er) }); setState('retrying'); }
        nextAt = now() + delay;
        schedule(delay);
      } finally { busy = null; }
    })();
    return busy;
  };
  return {
    // `delay` is how long to coalesce; the limits above always win over it.
    queue(body, delay = 1500) {
      const stamped = { ...body, device_id: deviceId(), client_updated_at: now() / 1000 };
      if (state === 'ok' && !latest && JSON.stringify({ ...stamped, client_updated_at: 0 }) === lastKey) return;
      latest = stamped;
      if (state !== 'ok') queuePending(assetId, { asset_id: assetId, ...stamped });
      schedule(delay);
    },
    // Page leaving: send now when the book is in good standing, otherwise just make sure
    // the place is stored (a flush must never turn into a request burst).
    flush() {
      clearT(timer); timer = null;
      if (!latest) return busy || Promise.resolve();
      if (state !== 'ok' || failures || ceilingAt() > now()) { queuePending(assetId, { asset_id: assetId, ...latest }); return busy || Promise.resolve(); }
      return attempt();
    },
    dispose() { if (latest) queuePending(assetId, { asset_id: assetId, ...latest }); disposed = true; clearT(timer); timer = null; },
    get state() { return state; },
    get hasUnsent() { return !!latest; },
  };
}

/* ---------- per-account preferences (reader) ---------- */
const READER_DEFAULTS = { theme: 'dark', font: 'serif', size: 3, spacing: 1.6, margin: 1, flow: 'paginated' };
const FONT_SIZES = [80, 90, 100, 112, 125, 140, 160, 180];
const FONTS = { serif: 'Georgia, "Iowan Old Style", "Palatino Linotype", serif', sans: 'Inter, system-ui, -apple-system, "Segoe UI", sans-serif', dyslexic: '"Atkinson Hyperlegible", Verdana, sans-serif' };
const THEMES = { dark: { fg: '#ecf0f5', bg: '#12161d', link: '#7fb3ff' }, light: { fg: '#1c2230', bg: '#ffffff', link: '#2c6fd6' }, sepia: { fg: '#3a2f22', bg: '#f6ecd9', link: '#8a5a2b' } };
function loadPrefs(api) {
  const local = (() => { try { return JSON.parse(localStorage.getItem('alx-reader-prefs') || '{}'); } catch { return {}; } })();
  const prefs = { ...READER_DEFAULTS, ...local };
  // The server copy wins when reachable (it follows the account across devices); the
  // local copy is what a device reads offline.
  const remote = api('/api/player/v1/preferences').then(p => { if (p && p.reader) { Object.assign(prefs, p.reader); localStorage.setItem('alx-reader-prefs', JSON.stringify(prefs)); } return prefs; }).catch(() => prefs);
  return { prefs, remote };
}
function savePrefs(api, prefs) {
  localStorage.setItem('alx-reader-prefs', JSON.stringify(prefs));
  api('/api/player/v1/preferences', { method: 'PUT', body: { reader: prefs } }).catch(() => {});
}

/* ---------- bookmarks ---------- */
function bookmarkStore(api, assetId) {
  const key = `alx-bookmarks-${assetId}`;
  const local = () => { try { return JSON.parse(localStorage.getItem(key) || '[]'); } catch { return []; } };
  return {
    async list() {
      try { const r = await api(`/api/player/v1/bookmarks?asset_id=${encodeURIComponent(assetId)}`); localStorage.setItem(key, JSON.stringify(r.items)); return r.items; }
      catch { return local(); }
    },
    async add(bm) {
      const item = { id: 'local-' + Date.now(), created_at: Date.now() / 1000, ...bm };
      try { const r = await api('/api/player/v1/bookmarks', { method: 'POST', body: { asset_id: assetId, ...bm } }); Object.assign(item, r); }
      catch { localStorage.setItem(key, JSON.stringify([...local(), item])); }
      return item;
    },
    async remove(id) {
      if (!String(id).startsWith('local-')) { try { await api(`/api/player/v1/bookmarks/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch {} }
      localStorage.setItem(key, JSON.stringify(local().filter(b => b.id !== id)));
    },
  };
}

/* ---------- entry ---------- */
// consume.css carries every rule this module's markup needs; loading it here (rather
// than from each shell's HTML) is what keeps Management from rendering it unstyled.
let stylesReady = null;
function ensureStyles() {
  if (stylesReady) return stylesReady;
  const existing = document.querySelector('link[data-consume-css]');
  if (existing) return (stylesReady = Promise.resolve());
  stylesReady = new Promise(resolve => {
    const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/assets/consume.css'; l.dataset.consumeCss = '1';
    l.onload = l.onerror = () => resolve();     // never block the book on a stylesheet
    document.head.appendChild(l);
  });
  return stylesReady;
}

export async function open(opts) {
  const { workId, assetId, api } = opts;
  await ensureStyles();
  const m = await api(`/api/v1/library/${encodeURIComponent(workId)}/read/${encodeURIComponent(assetId)}`);
  if (m.kind === 'audiobook') return openAudio(m, opts);
  if (m.format === 'pdf') return openPdf(m, opts);
  return openEpub(m, opts);
}

function barButton(id, label, title, extra = '') { return `<button class="pl-button ghost small" id="${id}" title="${esc(title || label)}" aria-label="${esc(title || label)}" ${extra}>${label}</button>`; }

/* ---------- EPUB ---------- */
async function openEpub(m, { api, host, toast, onBack }) {
  const file = m.files[0]; const saver = progressSaver(api, m.work_id, m.asset_id);
  const { prefs, remote } = loadPrefs(api); const bookmarks = bookmarkStore(api, m.asset_id);
  const root = document.createElement('div'); root.className = 'pl-reader'; root.dataset.readerTheme = prefs.theme;
  root.innerHTML = `
    <div class="pl-reader-bar">${barButton('rd-back', '‹ Back', 'Back to the book')}<div class="pl-reader-title">${esc(m.title || 'Book')}</div>
      <div class="pl-row" style="gap:6px">${barButton('rd-bm', '☆', 'Bookmark this page')}${barButton('rd-toc', '≡', 'Contents and bookmarks')}${barButton('rd-aa', 'Aa', 'Reading settings')}${barButton('rd-full', '⛶', 'Full screen')}</div></div>
    <div class="pl-reader-stage"><div id="rd-view" style="height:100%"><div class="pl-empty" style="margin:40px auto;max-width:360px"><strong>Opening</strong>Loading the book…</div></div>
      <div class="pl-reader-tap left" id="rd-tap-prev" aria-hidden="true"></div><div class="pl-reader-tap right" id="rd-tap-next" aria-hidden="true"></div>
      <aside class="pl-reader-panel hidden" id="rd-panel"></aside></div>
    <div class="pl-reader-bar bottom">${barButton('rd-prev', '‹ Previous', 'Previous page')}<span class="pl-reader-pos" id="rd-pos">${m.progress?.percent != null ? `${Math.round(m.progress.percent * 100)}%` : ''}</span>${barButton('rd-next', 'Next ›', 'Next page')}</div>`;
  host.innerHTML = ''; host.appendChild(root);
  document.body.style.overflow = 'hidden';
  const applyShellTheme = () => { const t = THEMES[prefs.theme] || THEMES.dark; root.style.setProperty('--rd-bg', t.bg); root.style.setProperty('--rd-fg', t.fg); };
  applyShellTheme();

  let rendition = null, book = null, immersive = false, panel = null;
  const onKey = ev => { if (!rendition || ev.target.matches('input,select,textarea')) return; if (ev.key === 'ArrowRight' || ev.key === ' ' || ev.key === 'PageDown') { ev.preventDefault(); Promise.resolve(rendition.next()).catch(() => {}); } else if (ev.key === 'ArrowLeft' || ev.key === 'PageUp') { ev.preventDefault(); Promise.resolve(rendition.prev()).catch(() => {}); } else if (ev.key === 'Escape') { if (panel) togglePanel(null); else if (document.fullscreenElement) document.exitFullscreen(); } };
  let done = false;
  const cleanup = async () => {
    if (done) return; done = true;
    document.removeEventListener('keydown', onKey); window.removeEventListener('pagehide', flushNow);
    document.body.style.overflow = '';
    try { if (document.fullscreenElement) await document.exitFullscreen(); } catch {}
    try { rendition?.destroy(); book?.destroy(); } catch {}
    root.remove();
    await saver.flush();
  };
  const flushNow = () => saver.flush();
  window.addEventListener('pagehide', flushNow, { once: true });
  $('#rd-back', root).onclick = async () => { await cleanup(); onBack(); };
  $('#rd-full', root).onclick = () => { if (document.fullscreenElement) document.exitFullscreen(); else root.requestFullscreen?.().catch(() => toast('Full screen is not available here', 'bad')); };

  const togglePanel = (kind) => {
    const el = $('#rd-panel', root);
    if (!kind || panel === kind) { panel = null; el.classList.add('hidden'); el.innerHTML = ''; return; }
    panel = kind; el.classList.remove('hidden');
    if (kind === 'toc') paintToc(el); else paintSettings(el);
  };
  $('#rd-toc', root).onclick = () => togglePanel('toc');
  $('#rd-aa', root).onclick = () => togglePanel('settings');

  async function paintToc(el) {
    const bms = await bookmarks.list();
    el.innerHTML = `<h3>Bookmarks</h3><div id="rd-bms">${bms.length ? bms.map(b => `<div class="pl-bm" data-id="${esc(b.id)}" data-loc="${esc(b.location)}"><span>${esc(b.title || 'Bookmark')}<br><small>${b.percent != null ? Math.round(b.percent * 100) + '%' : ''}</small></span><button class="pl-button ghost small rd-bm-del" aria-label="Remove bookmark">×</button></div>`).join('') : '<div class="pl-muted pl-small">No bookmarks yet. Tap ☆ while reading.</div>'}</div><h3 style="margin-top:18px">Contents</h3><div class="pl-toc" id="rd-toc-list"><div class="pl-muted pl-small">Loading…</div></div>`;
    $$('.pl-bm', el).forEach(row => { row.onclick = ev => { if (ev.target.closest('.rd-bm-del')) return; rendition?.display(row.dataset.loc); togglePanel(null); }; });
    $$('.rd-bm-del', el).forEach(b => b.onclick = async ev => { ev.stopPropagation(); const row = b.closest('.pl-bm'); await bookmarks.remove(row.dataset.id); row.remove(); });
    const nav = await book?.loaded?.navigation; const list = $('#rd-toc-list', el); if (!list) return;
    const items = []; const walk = (arr, depth) => arr.forEach(it => { items.push({ href: it.href, label: (it.label || '').trim() || 'Untitled', depth }); if (it.subitems?.length) walk(it.subitems, depth + 1); });
    walk(nav?.toc || [], 0);
    list.innerHTML = items.length ? items.map(it => `<a class="depth-${Math.min(it.depth, 1)}" data-href="${esc(it.href)}">${esc(it.label)}</a>`).join('') : '<div class="pl-muted pl-small">This book has no table of contents.</div>';
    $$('a', list).forEach(a => a.onclick = () => { rendition?.display(a.dataset.href); togglePanel(null); });
  }
  function paintSettings(el) {
    const seg = (name, options) => `<div class="pl-segment" data-pref="${name}">${options.map(([v, l]) => `<button data-v="${v}" class="${String(prefs[name]) === String(v) ? 'on' : ''}">${l}</button>`).join('')}</div>`;
    el.innerHTML = `<div class="pl-reader-settings"><h3>Reading settings</h3>
      <div class="pl-setting"><div><strong>Theme</strong></div>${seg('theme', [['light', 'Light'], ['sepia', 'Sepia'], ['dark', 'Dark']])}</div>
      <div class="pl-setting"><div><strong>Font</strong></div>${seg('font', [['serif', 'Serif'], ['sans', 'Sans'], ['dyslexic', 'Legible']])}</div>
      <div class="pl-setting"><div><strong>Text size</strong></div><div class="pl-segment"><button id="rd-size-down" aria-label="Smaller text">A−</button><button id="rd-size-up" aria-label="Larger text">A+</button></div></div>
      <div class="pl-setting"><div><strong>Line spacing</strong></div>${seg('spacing', [['1.35', 'Tight'], ['1.6', 'Normal'], ['1.9', 'Loose']])}</div>
      <div class="pl-setting"><div><strong>Margins</strong></div>${seg('margin', [['0', 'None'], ['1', 'Normal'], ['2', 'Wide']])}</div>
      <div class="pl-setting"><div><strong>Layout</strong></div>${seg('flow', [['paginated', 'Pages'], ['scrolled', 'Scroll']])}</div></div>`;
    $$('[data-pref] button', el).forEach(b => b.onclick = () => { const name = b.closest('[data-pref]').dataset.pref; const v = b.dataset.v; prefs[name] = name === 'spacing' ? Number(v) : name === 'margin' ? Number(v) : v; $$('button', b.parentElement).forEach(x => x.classList.toggle('on', x === b)); applyPrefs(name === 'flow'); });
    $('#rd-size-down', el).onclick = () => { prefs.size = Math.max(0, Number(prefs.size) - 1); applyPrefs(); };
    $('#rd-size-up', el).onclick = () => { prefs.size = Math.min(FONT_SIZES.length - 1, Number(prefs.size) + 1); applyPrefs(); };
  }
  let lastCfi = m.progress?.location || null;
  function applyPrefs(reflow = false) {
    savePrefs(api, prefs); applyShellTheme(); root.dataset.readerTheme = prefs.theme;
    if (!rendition) return;
    const t = THEMES[prefs.theme] || THEMES.dark; const pad = [0, 8, 28][Number(prefs.margin)] ?? 8;
    const size = `${FONT_SIZES[Math.max(0, Math.min(FONT_SIZES.length - 1, Number(prefs.size)))]}%`;
    // Text size is set on html AND body with !important, and body text elements are made
    // to inherit it. epub.js's own fontSize() only writes an inline size on <body>, which
    // a publisher stylesheet with absolute sizes (`p { font-size: 12pt }`, common in
    // Calibre output) silently defeats, so A+/A- changed nothing on those books.
    // Headings, small, sup/sub keep their own relative sizes.
    rendition.themes.default({
      html: { 'font-size': `${size} !important` },
      body: { 'font-size': '1em !important', color: `${t.fg} !important`, background: `${t.bg} !important`, 'font-family': `${FONTS[prefs.font] || FONTS.serif} !important`, 'line-height': `${prefs.spacing} !important`, padding: `0 ${pad}px !important` },
      'p, div, li, blockquote, dd, dt, td, th, figcaption, pre': { 'font-size': 'inherit !important', 'line-height': `${prefs.spacing} !important` },
      'a, a:visited': { color: `${t.link} !important` },
    });
    rendition.themes.fontSize(size);
    if (reflow) { rendition.flow(prefs.flow); if (lastCfi) rendition.display(lastCfi); }
  }

  try {
    await loadScript('jszip'); await loadScript('epubjs');
    const r = await fetch(file.url, { headers: { Accept: 'application/epub+zip' } });
    if (!r.ok) throw new Error(`Could not fetch the book (${r.status})`);
    const buf = await r.arrayBuffer();
    await remote;
    book = window.ePub(buf);
    const el = $('#rd-view', root); el.innerHTML = '';
    rendition = book.renderTo(el, { width: '100%', height: '100%', flow: prefs.flow, spread: 'none', allowScriptedContent: false });
    applyPrefs();
    await rendition.display(m.progress?.location || undefined);
    // Locations give a stable percentage; generating them is the slow part, so the
    // page is readable first and the percentage appears when it is ready.
    book.ready.then(() => book.locations.generate(1200)).then(() => { const loc = rendition.currentLocation(); if (loc?.start) paintPos(loc); }).catch(() => {});
    const paintPos = loc => { const pct = readerPercent(book, loc.start.cfi); const posEl = $('#rd-pos', root); if (posEl) posEl.textContent = pct != null ? `${Math.round(pct * 100)}%` : ''; return pct; };
    rendition.on('relocated', safeRelocated(loc => { lastCfi = loc.start.cfi; const pct = paintPos(loc); saver.queue({ location: loc.start.cfi, percent: pct == null ? undefined : Math.round(pct * 1000) / 1000 }); }));
    const next = () => { Promise.resolve(rendition.next()).catch(() => {}); }, prev = () => { Promise.resolve(rendition.prev()).catch(() => {}); };
    $('#rd-prev', root).onclick = prev; $('#rd-next', root).onclick = next;
    $('#rd-tap-prev', root).onclick = prev; $('#rd-tap-next', root).onclick = next;
    // Swipe inside the rendered page (the iframe swallows document events), and a tap
    // in the middle toggles the bars for an uncluttered page.
    let touch = null;
    rendition.on('touchstart', ev => { const t = ev.changedTouches?.[0]; touch = t ? { x: t.screenX, y: t.screenY, at: Date.now() } : null; });
    rendition.on('touchend', ev => { const t = ev.changedTouches?.[0]; if (!touch || !t) return; const dx = t.screenX - touch.x, dy = t.screenY - touch.y; if (Math.abs(dx) > 60 && Math.abs(dy) < 80 && Date.now() - touch.at < 800) { dx < 0 ? next() : prev(); } else if (Math.abs(dx) < 8 && Math.abs(dy) < 8) { immersive = !immersive; root.classList.toggle('immersive', immersive); } touch = null; });
    rendition.on('keydown', onKey); document.addEventListener('keydown', onKey);
    $('#rd-bm', root).onclick = async () => { if (!lastCfi) return; const pct = book.locations?.length() ? book.locations.percentageFromCfi(lastCfi) : null; let title = 'Bookmark'; try { const spine = book.spine.get(lastCfi); const navItem = spine && book.navigation?.get(spine.href); if (navItem?.label) title = navItem.label.trim(); } catch {} await bookmarks.add({ location: lastCfi, percent: pct, title }); toast('Bookmarked'); if (panel === 'toc') paintToc($('#rd-panel', root)); };
  } catch (er) {
    const el = $('#rd-view', root); if (el) el.innerHTML = `<div class="pl-empty" style="margin:40px auto;max-width:420px"><strong>Could not open this book</strong>${esc(er.message)}<div style="margin-top:12px"><a class="pl-button ghost small" href="${esc(file.url)}" download>Download the file instead</a></div></div>`;
  }
  return cleanup;
}

/* ---------- PDF ---------- */
function openPdf(m, { api, host, toast, onBack }) {
  const file = m.files[0]; const saver = progressSaver(api, m.work_id, m.asset_id);
  const page = Math.max(1, parseInt(m.progress?.location || '1', 10) || 1);
  const root = document.createElement('div'); root.className = 'pl-reader';
  // The browser's viewer does not report its page back, so the position is the page
  // the reader types in - a bookmark, kept per account like everything else here.
  root.innerHTML = `<div class="pl-reader-bar">${barButton('rd-back', '‹ Back', 'Back to the book')}<div class="pl-reader-title">${esc(m.title || 'PDF')}</div><div class="pl-row" style="gap:6px"><label class="pl-muted pl-small" for="rd-page">Page</label><input id="rd-page" class="pl-input" type="number" min="1" value="${page}" style="width:84px;min-height:38px;padding:4px 8px">${barButton('rd-page-save', 'Save', 'Save my page')}</div></div>
    <div class="pl-reader-stage"><iframe title="${esc(m.title || 'PDF')}" src="${esc(file.url)}#page=${page}"></iframe></div>`;
  host.innerHTML = ''; host.appendChild(root); document.body.style.overflow = 'hidden';
  let done = false;
  const cleanup = async () => { if (done) return; done = true; document.body.style.overflow = ''; root.remove(); await saver.flush(); };
  $('#rd-back', root).onclick = async () => { await cleanup(); onBack(); };
  $('#rd-page-save', root).onclick = () => { const p = Math.max(1, parseInt($('#rd-page', root).value, 10) || 1); saver.queue({ location: String(p) }, 0); toast(`Saved page ${p}`); };
  return cleanup;
}

/* ---------- audio ---------- */
// One <audio> for the whole page lifetime so playback survives navigation in the
// Player shell; the page UI attaches to it and the mini player shows while it plays.
const PLAYBACK_SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2, 2.5];
const SLEEP_OPTIONS = [[0, 'Off'], [15, '15 min'], [30, '30 min'], [45, '45 min'], [60, '60 min'], [-1, 'End of chapter']];
// Where the listener really is. A reset element (load(), a failed stream) reports 0, which
// must never replace a real place: only a element holding data counts.
function trackPlace() { const a = player.audio; if (a && !a.error && a.readyState >= 2) player.lastPos = a.currentTime || 0; }
const player = { audio: null, m: null, tracks: [], idx: 0, saver: null, sleepAt: null, sleepChapter: false, listeners: [], mini: null, api: null,
  lastPos: 0, wantPlay: false, ignorePause: false, notice: null, mediaFailures: 0, mediaTimer: null, metaHandler: null, repaint: null, toast: null };
// "0:04 / 7:30:33 · 7:30:29 left". Total and remaining are left out until the duration is
// known (NaN/Infinity/0 before metadata loads); remaining is in listening time, so it
// follows the playback speed. Elapsed never exceeds the total.
export function formatAudioTimes({ cur, dur, rate = 1 } = {}) {
  const c = Number.isFinite(Number(cur)) && Number(cur) > 0 ? Math.floor(Number(cur)) : 0;
  if (!(Number.isFinite(Number(dur)) && Number(dur) >= 1)) return { elapsed: fmtClock(c), total: null, remaining: null, label: `${fmtClock(c)} / --:--` };
  const d = Math.floor(Number(dur)), e = Math.min(c, d), r = Number.isFinite(Number(rate)) && Number(rate) > 0 ? Number(rate) : 1;
  const elapsed = fmtClock(e), total = fmtClock(d), remaining = fmtClock(Math.max(0, Math.round((d - e) / r)));
  return { elapsed, total, remaining, label: `${elapsed} / ${total} · ${remaining} left` };
}
// Glyphs are inline SVG in currentColor, not emoji: ⏮ ⏭ ▶ draw as colour emoji on iOS
// and Android, which ignore the text colour and can vanish on a button face.
const svgIcon = d => `<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" focusable="false" fill="currentColor"><path d="${d}"/></svg>`;
const ICON = {
  prev: svgIcon('M6 6h2v12H6zM9.5 12 18 6v12z'), next: svgIcon('M16 6h2v12h-2zM14.5 12 6 6v12z'),
  play: svgIcon('M8 5v14l11-7z'), pause: svgIcon('M7 5h4v14H7zM13 5h4v14h-4z'),
};
const NOTICE_TEXT = {
  unavailable: "This book isn't available right now. Your place is saved, and it will pick up here when the book is back.",
  trouble: 'Playback hit a problem. Your place is saved. Tap play to try again.',
};
export function current() { return player.m ? { m: player.m, audio: player.audio, idx: player.idx, place: player.lastPos, notice: player.notice } : null; }
export function stopPlayback() {
  if (!player.audio) return;
  try { player.saver?.queue(snapshot(), 0); } catch {}
  clearTimeout(player.mediaTimer); player.mediaTimer = null;
  player.audio.pause(); player.audio.removeAttribute('src'); player.audio.load();
  for (const [t, fn] of player.listeners) player.audio.removeEventListener(t, fn);
  player.listeners = []; player.m = null; player.tracks = []; player.sleepAt = null; player.notice = null; player.mediaFailures = 0; player.wantPlay = false;
  paintMini(); if ('mediaSession' in navigator) { try { navigator.mediaSession.playbackState = 'none'; } catch {} }
  const saver = player.saver; player.saver = null;
  // Whatever is still unsent is written to this device by flush()/dispose(); a later
  // visit or the online event replays it.
  return saver && Promise.resolve(saver.flush()).finally(() => saver.dispose());
}
function ensureAudio() {
  if (!player.audio) { player.audio = document.createElement('audio'); player.audio.preload = 'metadata'; player.audio.id = 'alx-audio'; player.audio.style.display = 'none'; document.body.appendChild(player.audio); }
  return player.audio;
}
function before(i) { return player.tracks.slice(0, i).reduce((s, t) => s + (Number(t.duration_seconds) || 0), 0); }
function total() { return player.tracks.reduce((s, t) => s + (Number(t.duration_seconds) || 0), 0); }
function snapshot() {
  // After a media error the element reports 0; saving that would wipe the listener's place.
  const a = player.audio, pos = (a.error || player.notice || a.readyState < 2) ? player.lastPos : (a.currentTime || 0), tot = total();
  const dur = a.duration || Number(player.tracks[player.idx]?.duration_seconds) || 0;
  const pct = tot ? Math.min(1, (before(player.idx) + pos) / tot) : (dur ? Math.min(1, pos / dur) : undefined);
  return { file_id: player.tracks[player.idx]?.id, position: Math.round(pos * 10) / 10, percent: pct == null ? undefined : Math.round(pct * 1000) / 1000, duration: dur ? Math.round(dur) : undefined };
}
function chapterAt(t) { let cur = null; for (const c of player.m?.chapters || []) if (t >= c.start) cur = c; return cur; }
function loadTrack(i, at = 0, autoplay = false) {
  const a = player.audio; player.idx = i; a.src = player.tracks[i].url;
  if (player.metaHandler) a.removeEventListener('loadedmetadata', player.metaHandler);   // a failed load never fires it; do not stack them
  player.metaHandler = () => { player.metaHandler = null; if (at) a.currentTime = Math.min(at, Math.max(0, (a.duration || at) - 1)); if (autoplay) a.play().catch(() => {}); };
  a.addEventListener('loadedmetadata', player.metaHandler, { once: true });
  a.load(); updateSession();
}
function updateSession() {
  if (!('mediaSession' in navigator) || !player.m) return;
  const ms = navigator.mediaSession; const ch = chapterAt(player.audio.currentTime || 0);
  try {
    ms.metadata = new MediaMetadata({ title: player.m.title || 'Audiobook', artist: player.m.authors || '', album: ch ? ch.title : (player.tracks.length > 1 ? player.tracks[player.idx].title : ''), artwork: player.m.cover_url ? [{ src: player.m.cover_url, sizes: '512x512', type: 'image/jpeg' }] : [] });
    ms.setActionHandler('play', () => player.audio.play()); ms.setActionHandler('pause', () => player.audio.pause());
    ms.setActionHandler('seekbackward', d => { player.audio.currentTime = Math.max(0, player.audio.currentTime - (d.seekOffset || 15)); });
    ms.setActionHandler('seekforward', d => { player.audio.currentTime = Math.min(player.audio.duration || Infinity, player.audio.currentTime + (d.seekOffset || 30)); });
    ms.setActionHandler('seekto', d => { if (d.seekTime != null) player.audio.currentTime = d.seekTime; });
    ms.setActionHandler('previoustrack', () => jumpChapter(-1)); ms.setActionHandler('nexttrack', () => jumpChapter(1));
  } catch {}
}
function jumpChapter(dir) {
  const a = player.audio, chapters = player.m?.chapters || [];
  if (chapters.length) { const cur = chapterAt(a.currentTime || 0); const i = cur ? chapters.indexOf(cur) : -1; const target = chapters[Math.max(0, Math.min(chapters.length - 1, i + dir))]; if (target) a.currentTime = target.start; return; }
  const n = player.idx + dir; if (n >= 0 && n < player.tracks.length) loadTrack(n, 0, !a.paused);
}
function paintMini() {
  const host = player.mini; if (!host) return;
  // The mini player is for when the page is left; on the book's own page it would only sit on top of the controls.
  if (!player.m || player.repaint) { host.classList.add('hidden'); if (!player.m) { host.innerHTML = ''; delete host.dataset.bound; } document.querySelector('.pl-view')?.classList.remove('has-mini'); return; }
  const a = player.audio; const ch = chapterAt(a.currentTime || 0);
  const tot = total(); const pct = tot ? Math.min(1, (before(player.idx) + (a.currentTime || 0)) / tot) : (a.duration ? (a.currentTime || 0) / a.duration : 0);
  if (!host.dataset.bound) {
    host.innerHTML = `<div class="pl-cover" id="mini-cover"></div><div class="pl-mini-text" id="mini-text"><strong></strong><span></span></div><div class="pl-mini-ctl"><button id="mini-back" aria-label="Back 15 seconds">↺</button><button id="mini-play" aria-label="Play or pause">▶</button><button id="mini-stop" aria-label="Stop">×</button></div><div class="pl-bar"><i id="mini-bar"></i></div>`;
    host.dataset.bound = '1';
    $('#mini-text', host).onclick = () => { if (player.m) location.hash = player.m.podcast ? `podcasts/${player.m.podcast.show_id}/play/${player.m.podcast.episode_id}` : `library/${player.m.work_id}/read/${player.m.asset_id}`; };
    $('#mini-play', host).onclick = () => a.paused || player.audioFailed ? playNow(a) : a.pause();
    $('#mini-back', host).onclick = () => { a.currentTime = Math.max(0, a.currentTime - 15); };
    $('#mini-stop', host).onclick = () => stopPlayback();
  }
  host.classList.remove('hidden'); document.querySelector('.pl-view')?.classList.add('has-mini');
  $('#mini-cover', host).innerHTML = player.m.cover_url ? `<img src="${esc(player.m.cover_url)}" alt="">` : '';
  $('#mini-text strong', host).textContent = player.m.title || 'Audiobook';
  $('#mini-text span', host).textContent = ch ? ch.title : (player.tracks.length > 1 ? player.tracks[player.idx].title : (player.m.authors || ''));
  $('#mini-play', host).textContent = a.paused ? '▶' : '❚❚';
  $('#mini-bar', host).style.width = `${Math.round(pct * 100)}%`;
}

// Recovery. The server said the book is off this account's shelf (404/403/410) or the
// media element failed. One failure produces one calm notice, never a retry loop: the
// saver probes the server on a slow, growing delay, and when it gets through again the
// element is reloaded at the saved place (and resumes playing if it had been).
function setNotice(kind) { player.notice = kind; if (player.repaint) player.repaint(); paintMini(); }
function reloadTrackAtPlace(autoplay) {
  if (!player.m || !player.tracks.length) return;
  clearTimeout(player.mediaTimer); player.mediaTimer = null;
  const a = player.audio; a.pause();
  loadTrack(player.idx, player.lastPos, autoplay);
}
function onSaveState(state, prev) {
  if (!player.m) return;
  if (state === 'unavailable') { clearTimeout(player.mediaTimer); player.mediaTimer = null; setNotice('unavailable'); }
  else if (state === 'ok' && prev === 'unavailable') {
    const wasBroken = player.audio.error || player.audioFailed;
    player.audioFailed = false; player.mediaFailures = 0;
    setNotice(null);
    if (player.toast) player.toast('Your book is back. Picking up where you left off.', 'good', 5000);
    if (wasBroken) reloadTrackAtPlace(player.wantPlay);
  }
}
function onMediaError() {
  const a = player.audio; if (!player.m || !player.saver || !a.error) return;
  player.audioFailed = true; player.mediaFailures++;
  if (!a.paused) player.ignorePause = true;
  a.pause();
  clearTimeout(player.mediaTimer); player.mediaTimer = null;
  // Ask the server once (through the saver's limits) whether the book is still ours;
  // 404 flips the saver to 'unavailable' and the notice with it.
  try { player.saver.queue(snapshot(), 0); } catch {}
  if (player.saver.state === 'unavailable') return;
  if (player.mediaFailures <= 3) {
    // Transient (network blip): reload at most three times, 5 s / 20 s / 60 s apart.
    const wait = [5000, 20000, 60000][player.mediaFailures - 1];
    player.mediaTimer = setTimeout(() => { player.mediaTimer = null; if (player.saver && player.saver.state === 'unavailable') return; reloadTrackAtPlace(player.wantPlay); }, wait);
  } else setNotice('trouble');
}
function playNow(a) {
  if (player.audioFailed || a.error) { player.mediaFailures = 0; player.audioFailed = false; if (player.notice === 'trouble') setNotice(null); reloadTrackAtPlace(true); return Promise.resolve(); }
  return a.play().catch(er => {
    if (er && er.name === 'NotAllowedError') return;
    if (er && er.name === 'AbortError') return;
    setNotice(player.notice || 'trouble');
  });
}

/* ---------- podcast episodes ---------- */
// A podcast episode plays through the audiobook player above: openPodcast() turns an episode into the same
// shape a one-file audiobook has (m.files, m.progress) and calls openAudio(), so speed, sleep timer, the mini
// player and the media session are the book code. Only three things differ, all keyed on m.podcast: positions go
// to /api/player/v1/podcasts/episodes/{id}/progress, resume comes from the episode's own saved position, and the
// end of an episode asks the next-episode endpoint and plays the answer.
const enc = encodeURIComponent;
export const podcastAudioUrl = id => `/api/player/v1/podcasts/episodes/${enc(id)}/audio`;
export function findPodcastEpisode(show, episodeId) {
  for (const s of show?.seasons || []) for (const ep of s.episodes || []) if (String(ep.id) === String(episodeId)) return ep;
  return null;
}
// `saved` is the GET .../progress body ({progress: {position_seconds, client_updated_at, played} | null}). A played
// episode starts again from the top; otherwise playback resumes at the saved position.
export function podcastSource(show, ep, saved, { autoplay = false } = {}) {
  const p = saved && saved.progress;
  const at = p && !p.played ? Number(p.position_seconds) || 0 : 0;
  return {
    kind: 'audiobook', podcast: { show_id: show.id, episode_id: ep.id }, asset_id: `podcast:${ep.id}`, work_id: null,
    title: ep.title, authors: show.title || '', cover_url: show.cover_url || null, chapters: [], autoplay,
    files: [{ id: ep.id, url: podcastAudioUrl(ep.id), title: ep.title, duration_seconds: ep.duration_seconds || 0 }],
    progress: at > 0 ? { file_id: ep.id, position: at, client_updated_at: Number(p.client_updated_at) || 0 } : null,
  };
}
export async function openPodcast(opts) {
  const { episodeId, api, onEpisode } = opts;
  await ensureStyles();
  const show = opts.show;     // the shell fetched it (GET /api/v1/podcasts/{id}); this module only calls the Player API
  if (!show) throw new Error('Show not found');
  let pageCleanup = null;
  let pageGone = false;     // the cleanup returned below has run: later episodes play in the background only
  const play = async (ep, autoplay) => {
    const saved = await api(`/api/player/v1/podcasts/episodes/${enc(ep.id)}/progress`).catch(() => null);
    const m = podcastSource(show, ep, saved, { autoplay });
    m.podcast.switchTo = switchTo;
    pageCleanup = await openAudio(m, pageGone ? { ...opts, background: true } : opts);
    // Reopening the episode that is already playing keeps the old player.m (and its ended listener): hand it to this page.
    if (player.m?.podcast) player.m.podcast.switchTo = switchTo;
    if (onEpisode && !pageGone) onEpisode(ep);
  };
  const switchTo = async next => {
    const ep = findPodcastEpisode(show, next.id) || next;   // a rescan can add episodes the page has not loaded
    const old = pageCleanup; pageCleanup = null;
    if (old) await old();
    await play(ep, true);
  };
  const first = findPodcastEpisode(show, episodeId);
  if (!first) throw new Error('Episode not found');
  await play(first, opts.autoplay === true);
  return async () => { pageGone = true; if (pageCleanup) await pageCleanup(); };
}
// The episode ended: store the end position, mark it played (the server's 95% / last 30 s rule needs a known
// duration; this does not), then play the next unplayed episode, across season boundaries. Nothing here waits on a
// timer.
async function podcastEnded(m, a, api, toast) {
  const id = m.podcast.episode_id;
  try { player.saver?.queue(snapshot(), 0); await player.saver?.flush(); } catch {}
  try { await api(`/api/player/v1/podcasts/episodes/${enc(id)}/played`, { method: 'POST', body: {} }); } catch {}
  if (player.sleepChapter) { player.sleepChapter = false; toast('Sleep timer: end of episode'); return; }
  let next;
  try { next = (await api(`/api/player/v1/podcasts/episodes/${enc(id)}/next`)).episode; }
  catch { toast('Could not load the next episode'); return; }
  if (!next) { toast('Finished'); return; }
  if (player.m !== m) return;     // the listener already moved on to something else
  await m.podcast.switchTo(next);
}

async function openAudio(m, { api, host, toast, onBack, chrome, mini, background }) {
  const a = ensureAudio(); player.api = api; player.mini = mini || player.mini; player.toast = toast;
  const sameBook = player.m && player.m.asset_id === m.asset_id;
  m.authors = m.authors || ''; m.cover_url = m.cover_url || (m.has_cover === false ? null : `/api/v1/library/${m.work_id}/cover`);
  if (!sameBook) {
    await stopPlayback();
    player.m = m; player.tracks = m.files; player.notice = null; player.audioFailed = false; player.mediaFailures = 0;
    player.saver = m.podcast
      ? progressSaver(api, null, m.asset_id, { onState: onSaveState, pending: false,
          send: b => api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(m.podcast.episode_id)}/progress`, { method: 'PUT', body: { position_seconds: b.position, client_updated_at: b.client_updated_at } }) })
      : progressSaver(api, m.work_id, m.asset_id, { onState: onSaveState });
    // A position this device could not deliver (offline, or the book was off the shelf)
    // is newer than the server's copy and is where the listener actually is.
    const held = pendingProgressFor(m.asset_id);
    const place = held && held.file_id && Number(held.client_updated_at || 0) > Number(m.progress?.client_updated_at || 0) ? held : m.progress;
    const idx = Math.max(0, player.tracks.findIndex(t => t.id === place?.file_id));
    const resumeAt = place?.file_id === player.tracks[idx]?.id ? Number(place.position) || 0 : 0;
    player.lastPos = resumeAt;
    a.playbackRate = Number(localStorage.getItem('bookdoe-speed')) || 1;
    loadTrack(idx, resumeAt, m.autoplay === true);
    let lastTick = 0;
    const on = (t, fn) => { a.addEventListener(t, fn); player.listeners.push([t, fn]); };
    on('timeupdate', () => { trackPlace(); const now = Date.now(); if (now - lastTick > 10000) { lastTick = now; player.saver.queue(snapshot(), 0); } if (player.sleepAt && now >= player.sleepAt) { a.pause(); player.sleepAt = null; toast('Sleep timer: paused'); } if (player.sleepChapter) { const ch = chapterAt(a.currentTime); if (player.sleepChapterStart && ch && ch.start !== player.sleepChapterStart) { a.pause(); player.sleepChapter = false; toast('Sleep timer: end of chapter'); } } });
    on('error', onMediaError);
    on('playing', () => { player.audioFailed = false; player.mediaFailures = 0; if (player.notice === 'trouble') setNotice(null); });
    on('pause', () => { if (player.ignorePause) player.ignorePause = false; else if (a.readyState >= 1 && !a.error) player.wantPlay = false; /* a pause the element does to itself (reset, failed stream) is not the listener stopping */ player.saver.queue(snapshot(), 0); if ('mediaSession' in navigator) try { navigator.mediaSession.playbackState = 'paused'; } catch {} });
    on('play', () => { player.wantPlay = true; if ('mediaSession' in navigator) try { navigator.mediaSession.playbackState = 'playing'; } catch {} updateSession(); });
    on('seeked', () => { trackPlace(); player.saver.queue(snapshot(), 800); });
    on('ratechange', () => localStorage.setItem('bookdoe-speed', String(a.playbackRate)));
    on('ended', () => { if (m.podcast) { podcastEnded(m, a, api, toast); return; } if (player.idx + 1 < player.tracks.length) { loadTrack(player.idx + 1, 0, true); player.saver.queue({ file_id: player.tracks[player.idx].id, position: 0, percent: total() ? Math.round(before(player.idx) / total() * 1000) / 1000 : undefined }, 0); } else { player.saver.queue({ file_id: player.tracks[player.idx].id, position: Math.round(a.duration || 0), percent: 1 }, 0); toast('Finished'); } });
    window.addEventListener('pagehide', () => player.saver?.flush(), { once: true });
    replayPendingProgress(api).catch(() => {});
  }

  // Background start (a podcast episode that follows after the listener left the player page): audio and saver
  // are set up above; nothing is rendered into `host` and no page handlers are bound.
  if (background) { paintMini(); return async () => {}; }
  const chapters = m.chapters?.length ? m.chapters : null;
  host.innerHTML = `<div class="pl-consume"><div class="pl-row" style="margin-bottom:12px"><button class="pl-button ghost small" id="au-back">‹ Back</button></div>
  <div class="pl-player">
    <div class="pl-cover" style="--hue:200">${m.cover_url ? `<img src="${esc(m.cover_url)}" alt="" data-img-fallback="remove">` : ''}<div class="pl-fallback"><strong>${esc(m.title)}</strong></div></div>
    <div>
      <h1>${esc(m.title || 'Audiobook')}</h1><div class="pl-by pl-muted">${esc(m.authors || '')}</div>
      <div class="pl-chapter-now" id="au-chapter"></div>
      <div class="pl-notice" id="au-notice" role="status" aria-live="polite" hidden></div>
      <div class="pl-scrub"><input type="range" id="au-seek" min="0" max="1000" value="0" aria-label="Seek" aria-valuetext="0:00"><div class="pl-times" id="au-times">0:00 / --:--</div></div>
      <div class="pl-transport"><button class="pl-button quiet" id="au-prev" aria-label="Previous chapter">${ICON.prev}</button><button class="pl-button quiet" id="au-back15" aria-label="Back 15 seconds">↺15</button><button class="pl-button play" id="au-play" aria-label="Play">${ICON.play}</button><button class="pl-button quiet" id="au-fwd30" aria-label="Forward 30 seconds">30↻</button><button class="pl-button quiet" id="au-next" aria-label="Next chapter">${ICON.next}</button></div>
      <div class="pl-player-extras"><label class="pl-row pl-small pl-muted">Speed <select class="pl-select" id="au-speed" style="width:auto">${PLAYBACK_SPEEDS.map(s => `<option value="${s}" ${s === a.playbackRate ? 'selected' : ''}>${s}×</option>`).join('')}</select></label><label class="pl-row pl-small pl-muted">Sleep <select class="pl-select" id="au-sleep" style="width:auto">${SLEEP_OPTIONS.map(([v, l]) => `<option value="${v}">${v === -1 && m.podcast ? 'End of episode' : l}</option>`).join('')}</select></label><span class="pl-small pl-muted" id="au-total">${total() ? fmtClock(total()) + ' total' : ''}</span></div>
      <div class="pl-section"><div class="pl-section-head"><h2>${chapters ? 'Chapters' : (player.tracks.length > 1 ? 'Parts' : 'File')}</h2><span class="pl-muted pl-small">${chapters ? chapters.length + ' chapters' : player.tracks.length + (player.tracks.length > 1 ? ' files' : ' file')}</span></div>
      <div class="pl-tracks">${chapters ? chapters.map(c => `<button class="pl-track au-ch" data-start="${c.start}"><span>${esc(c.title)}</span><small>${fmtClock(c.start)}</small></button>`).join('') : player.tracks.map((t, i) => `<button class="pl-track au-tr ${i === player.idx ? 'active' : ''}" data-index="${i}"><span>${t.part_number != null ? `<span class="pl-muted">${t.part_number}.</span> ` : ''}${esc(t.title)}</span><small>${t.duration_seconds ? fmtClock(t.duration_seconds) : ''}</small></button>`).join('')}</div></div>
    </div></div></div>`;

  const seek = $('#au-seek', host); let scrubbing = false;
  const paint = () => {
    if (!host.isConnected) return;
    const dur = Number.isFinite(a.duration) && a.duration > 0 ? a.duration : (Number(player.tracks[player.idx]?.duration_seconds) || 0); const cur = a.currentTime || 0;
    if (!scrubbing) seek.value = dur ? String(Math.round(cur / dur * 1000)) : '0';
    const times = formatAudioTimes({ cur, dur, rate: a.playbackRate });
    $('#au-times', host).textContent = times.label; seek.setAttribute('aria-valuetext', times.total ? `${times.elapsed} of ${times.total}` : times.elapsed);
    const note = $('#au-notice', host); note.hidden = !player.notice; note.textContent = player.notice ? NOTICE_TEXT[player.notice] : '';
    const playBtn = $('#au-play', host); const playState = a.paused ? 'paused' : 'playing'; if (playBtn.dataset.state !== playState) { playBtn.dataset.state = playState; playBtn.innerHTML = a.paused ? ICON.play : ICON.pause; } $('#au-play', host).setAttribute('aria-label', a.paused ? 'Play' : 'Pause');
    const ch = chapterAt(cur);
    // Controls that cannot do anything yet are disabled (dimmed, still legible), not hidden.
    seek.disabled = !dur; $('#au-back15', host).disabled = !dur; $('#au-fwd30', host).disabled = !dur;
    const chs = player.m?.chapters || []; $('#au-next', host).disabled = chs.length ? (!!ch && chs.indexOf(ch) === chs.length - 1) : player.idx >= player.tracks.length - 1;
    $('#au-chapter', host).textContent = ch ? ch.title : (player.tracks.length > 1 ? player.tracks[player.idx].title : '');
    $$('.au-ch', host).forEach(b => b.classList.toggle('active', !!ch && Number(b.dataset.start) === ch.start));
    $$('.au-tr', host).forEach(b => b.classList.toggle('active', Number(b.dataset.index) === player.idx));
    paintMini();
  };
  const pageListeners = [['timeupdate', paint], ['play', paint], ['pause', paint], ['loadedmetadata', paint], ['durationchange', paint], ['ratechange', paint], ['error', paint]];
  pageListeners.forEach(([t, fn]) => a.addEventListener(t, fn)); player.repaint = paint; paint();
  seek.oninput = () => { scrubbing = true; const dur = a.duration || Number(player.tracks[player.idx]?.duration_seconds) || 0; $('#au-times', host).textContent = formatAudioTimes({ cur: dur * Number(seek.value) / 1000, dur, rate: a.playbackRate }).label; };
  seek.onchange = () => { const dur = a.duration || 0; a.currentTime = dur * Number(seek.value) / 1000; scrubbing = false; };
  $('#au-play', host).onclick = () => a.paused || player.audioFailed ? playNow(a) : a.pause();
  $('#au-back15', host).onclick = () => { a.currentTime = Math.max(0, a.currentTime - 15); };
  $('#au-fwd30', host).onclick = () => { a.currentTime = Math.min(a.duration || Infinity, a.currentTime + 30); };
  $('#au-prev', host).onclick = () => jumpChapter(-1); $('#au-next', host).onclick = () => jumpChapter(1);
  $('#au-speed', host).onchange = ev => { a.playbackRate = Number(ev.target.value); };
  $('#au-sleep', host).onchange = ev => { const v = Number(ev.target.value); player.sleepChapter = v === -1; player.sleepChapterStart = v === -1 ? (chapterAt(a.currentTime)?.start ?? 0) : null; player.sleepAt = v > 0 ? Date.now() + v * 60000 : null; toast(v ? `Sleep timer set (${v === -1 && m.podcast ? 'End of episode' : SLEEP_OPTIONS.find(o => o[0] === v)[1]})` : 'Sleep timer off'); };
  $$('.au-ch', host).forEach(b => b.onclick = () => { a.currentTime = Number(b.dataset.start); a.play().catch(() => {}); });
  $$('.au-tr', host).forEach(b => b.onclick = () => loadTrack(Number(b.dataset.index), 0, true));
  const keys = ev => { if (ev.target.matches('input,select,textarea')) return; if (ev.key === ' ') { ev.preventDefault(); $('#au-play', host)?.click(); } else if (ev.key === 'ArrowLeft') $('#au-back15', host)?.click(); else if (ev.key === 'ArrowRight') $('#au-fwd30', host)?.click(); };
  document.addEventListener('keydown', keys);
  $('#au-back', host).onclick = () => onBack();

  // Leaving the page: the Player keeps playing behind the mini player; Management
  // (no mini player) stops and saves, as it always did.
  let done = false;
  return async () => {
    if (done) return; done = true;
    document.removeEventListener('keydown', keys);
    pageListeners.forEach(([t, fn]) => a.removeEventListener(t, fn));
    if (player.repaint === paint) player.repaint = null;
    if (chrome !== 'player' || !player.mini) await stopPlayback();
    else { player.saver?.queue(snapshot(), 0); paintMini(); await player.saver?.flush(); }
  };
}
