/* bookdoe Player (/app). Home, Library, Discover, Settings; reading and listening
 * are entered from a book. Everything here talks to the versioned Player API where it
 * exists and to the personal /api/v1 surfaces otherwise; nothing here can reach
 * Management, and the server enforces that independently of this file.
 *
 * Phase 1 (identity and routing): sign-in for every auth mode, the pending-approval
 * screen, the four-tab shell, a plain library grid and a book page with Read/Listen
 * that opens the shared reader/player module. Later phases extend the pages. */
const PRODUCT = document.querySelector('meta[name="alx-product"]')?.content || 'bookdoe';  // the instance's product name (config app_name), stamped by the server
const TAGLINE = document.querySelector('meta[name="alx-product"]')?.dataset.tagline || 'Every Story. One Library.';
const state = { me: null, csrf: null, signin: null, route: 'home', online: navigator.onLine, lib: { q: '', kind: '', tag: '', view: 'grid', items: [], total: 0, offset: 0 } };
// Library filter state survives navigation (it lives in `state`) and reloads (localStorage).
try { Object.assign(state.lib, JSON.parse(localStorage.getItem('alx-pl-lib') || '{}')); } catch {}
function rememberLib() { const L = state.lib; localStorage.setItem('alx-pl-lib', JSON.stringify({ kind: L.kind, tag: L.tag, view: L.view })); }
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const e = (v = '') => String(v ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));

async function api(path, opts = {}) {
  const startedAs = opts._startedAs ?? (state.me ? state.me.id : null);
  const headers = { Accept: 'application/json', ...(opts.headers || {}) };
  if (opts.body && typeof opts.body !== 'string') { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body); }
  if (!['GET', 'HEAD'].includes((opts.method || 'GET').toUpperCase()) && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const r = await fetch(path, { ...opts, headers });
  let data = null; const ct = r.headers.get('content-type') || '';
  data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) {
    // The credential behind the page can change under it (the 24 h session lapsed and the trusted-device
    // cookie took over): the CSRF token held by the page is then stale. Fetch the current one and retry once.
    if (r.status === 403 && data?.detail === 'CSRF validation failed' && !opts._csrfRetried) {
      // Retry only if the same person is still signed in; otherwise the action must not run as someone else.
      let m = null; try { m = await (await fetch('/api/v1/auth/me', { headers: { Accept: 'application/json' } })).json(); } catch {}
      if (m && m.csrf) {
        if (startedAs && m.user && m.user.id !== startedAs) { const er = new Error('You were signed in as a different account. Reload to continue.'); er.status = 403; throw er; }
        state.csrf = m.csrf; return api(path, { ...opts, headers: { ...(opts.headers || {}), ...(typeof opts.body === 'string' ? { 'Content-Type': 'application/json' } : {}) }, _csrfRetried: true, _startedAs: startedAs });
      }
    }
    if (r.status === 401 && !path.includes('/auth/')) { state.me = null; await initAuth(); }
    const err = new Error(data?.detail || data?.message || (typeof data === 'string' && data ? data : `Request failed (${r.status})`));
    err.status = r.status; err.retryAfter = Number(r.headers.get('Retry-After')) || 0; err.payload = data; throw err;
  }
  return data;
}

function toast(message, kind = 'good', ms = 3800) {
  const el = document.createElement('div'); el.className = `pl-toast ${kind}`; el.textContent = message;
  $('#pl-toast').appendChild(el); setTimeout(() => el.remove(), ms);
}
function modal(title, body, { footer = '' } = {}) {
  const root = $('#pl-modal');
  root.innerHTML = `<div class="pl-modal-backdrop"><div class="pl-modal" role="dialog" aria-modal="true" aria-label="${e(title)}"><div class="pl-modal-head"><h2>${e(title)}</h2><button class="pl-icon pl-modal-close" aria-label="Close">×</button></div><div class="pl-modal-body">${body}</div>${footer ? `<div class="pl-modal-foot">${footer}</div>` : ''}</div></div>`;
  $('.pl-modal-close', root).onclick = closeModal;
  $('.pl-modal-backdrop', root).addEventListener('click', ev => { if (ev.target.classList.contains('pl-modal-backdrop')) closeModal(); });
  return root;
}
function closeModal() { $('#pl-modal').innerHTML = ''; }

/* ---------- identity ---------- */
async function initAuth() {
  let me;
  try { me = await api('/api/v1/auth/me'); }
  catch (err) { me = (err?.payload && typeof err.payload === 'object' && err.payload.auth_mode) ? err.payload : { authenticated: false, auth_mode: 'local', setup_required: false }; }
  if (me.authenticated) {
    state.me = me.user; state.csrf = me.csrf; state.signin = me.signin || null; state.acquisition = me.acquisition_enabled !== false;
    if (window.bookdoePwa && me.user?.id) window.bookdoePwa.setUser(me.user.id).catch(() => {});
    $('#pl-auth').classList.add('hidden');
    if (me.user.account_type === 'pending') { renderPending(); return; }
    $('#pl-app').classList.remove('hidden');
    $('#pl-account').textContent = (me.user.display_name || me.user.username || 'U').slice(0, 2).toUpperCase();
    renderNav(); await route(); refreshApprovalBadge(); replayOffline();
    return;
  }
  state.me = null; state.csrf = null; $('#pl-app').classList.add('hidden'); $('#pl-auth').classList.remove('hidden'); renderAuth(me);
}

// "Keep me signed in" on every sign-in gate. The wish travels in the request body (local
// form) or inside the server-signed OIDC state cookie (the link below), never as a flag
// the callback trusts from its own URL.
const rememberBox = days => days > 0 ? `<label class="pl-check" style="margin:2px 0 12px"><input type="checkbox" name="remember" value="1"><span>Keep me signed in for ${days} days</span></label>` : '';
const loginBody = form => { const o = Object.fromEntries(new FormData(form)); o.remember = !!o.remember; return o; };
const brandLogo = (hCls, mCls) => `<img class="brand-h on-light ${hCls}" src="/assets/brand/bookdoe-horizontal-light.svg" alt="${e(PRODUCT)}" /><img class="brand-h on-dark ${hCls}" src="/assets/brand/bookdoe-horizontal-dark.svg" alt="${e(PRODUCT)}" /><img class="brand-mark-img ${mCls}" src="/assets/brand/bookdoe-mark.svg" alt="" width="44" height="44" />`;
function renderAuth(info) {
  const el = $('#pl-auth'); const rd = Number(info.remember_days) || 0;
  const form = (id, title, lead, submitLabel, extra = '') => `<form class="pl-gate-card" id="${id}">${brandLogo('pl-logo-h', 'pl-logo')}<h1>${title}</h1><p>${lead}</p>${extra}<div class="pl-field"><label>Username</label><input class="pl-input" name="username" autocomplete="username" required></div><div class="pl-field"><label>Password</label><input class="pl-input" name="password" type="password" autocomplete="current-password" required></div>${rememberBox(rd)}<button class="pl-button wide">${submitLabel}</button></form>`;
  if (info.setup_required) {
    el.innerHTML = `<form class="pl-gate-card" id="setup-form">${brandLogo('pl-logo-h', 'pl-logo')}<h1>Set up ${e(PRODUCT)}</h1><p>Create the first administrator account.</p><div class="pl-field"><label>Username</label><input class="pl-input" name="username" value="admin" required></div><div class="pl-field"><label>Password</label><input class="pl-input" name="password" type="password" minlength="12" required></div><button class="pl-button wide">Create administrator</button></form>`;
    $('#setup-form').onsubmit = async ev => { ev.preventDefault(); try { const x = await api('/api/v1/auth/setup', { method: 'POST', body: Object.fromEntries(new FormData(ev.target)) }); state.csrf = x.csrf; await initAuth(); } catch (er) { toast(er.message, 'bad'); } };
  } else if (info.auth_mode === 'oidc') {
    el.innerHTML = `<div class="pl-gate-card">${brandLogo('pl-logo-h', 'pl-logo')}<h1>${e(PRODUCT)}</h1><p>${e(TAGLINE)}</p>${rememberBox(rd).replace('name="remember"', 'id="oidc-remember" name="oidc-remember"')}<a class="pl-button wide" id="oidc-link" href="/auth/login?next=%2Fapp">Sign in</a><button type="button" class="pl-button ghost wide" id="family-toggle" style="margin-top:12px">Sign in with a family account</button><form id="family-form" class="hidden" style="margin-top:14px"><div class="pl-field"><label>Username</label><input class="pl-input" name="username" autocomplete="username" required></div><div class="pl-field"><label>Password</label><input class="pl-input" name="password" type="password" autocomplete="current-password" required></div>${rememberBox(rd)}<button class="pl-button wide">Sign in</button></form></div>`;
    const rb = $('#oidc-remember'); if (rb) rb.onchange = () => { $('#oidc-link').href = '/auth/login?next=%2Fapp' + (rb.checked ? '&remember=1' : ''); };
    $('#family-toggle').onclick = () => { $('#family-toggle').classList.add('hidden'); $('#family-form').classList.remove('hidden'); $('#family-form input').focus(); };
    $('#family-form').onsubmit = async ev => { ev.preventDefault(); try { const x = await api('/api/v1/auth/login', { method: 'POST', body: loginBody(ev.target) }); state.csrf = x.csrf; await initAuth(); } catch (er) { toast(er.message, 'bad'); } };
  } else if (info.auth_mode === 'proxy') {
    el.innerHTML = `<div class="pl-gate-card">${brandLogo('pl-logo-h', 'pl-logo')}<h1>Sign-in required</h1><p>The reverse proxy did not pass a trusted identity. Open ${e(PRODUCT)} through its configured address.</p></div>`;
  } else {
    el.innerHTML = form('login-form', e(PRODUCT), e(TAGLINE), 'Sign in');
    $('#login-form').onsubmit = async ev => { ev.preventDefault(); try { const x = await api('/api/v1/auth/login', { method: 'POST', body: loginBody(ev.target) }); state.csrf = x.csrf; await initAuth(); } catch (er) { toast(er.message, 'bad'); } };
  }
}

function renderPending() {
  const el = $('#pl-auth'); el.classList.remove('hidden');
  el.innerHTML = `<div class="pl-gate-card"><img class="pl-logo" src="/assets/icons/icon-192.png" alt="" width="44" height="44" /><h1>Almost there</h1><p>You are signed in as <strong>${e(state.me.display_name || state.me.username)}</strong>, but an administrator has not placed your account yet. Ask them to open Management and choose Adult or Kid for you; this page updates once they do.</p><button class="pl-button ghost wide" id="pending-refresh">Check again</button><button class="pl-button quiet wide" id="pending-logout" style="margin-top:10px">Sign out</button></div>`;
  $('#pending-refresh').onclick = () => initAuth();
  $('#pending-logout').onclick = signOut;
}

async function signOut() {
  try { await api('/api/v1/auth/logout', { method: 'POST', body: {} }); } catch {}
  try { if (window.bookdoePwa) await window.bookdoePwa.clearUser(); } catch {}
  location.reload();
}

/* ---------- shell ---------- */
const NAV = [['home', 'Home', '⌂'], ['library', 'Library', '▦'], ['podcasts', 'Podcasts', '♪'], ['discover', 'Discover', '⌕'], ['approvals', 'Approvals', '✓'], ['settings', 'Settings', '⚙']];
// The household can switch acquisition off (auth/me carries the flag); then nothing that asks for a book to be fetched is drawn.
function acquisitionOn() { return state.acquisition !== false; }
function isApprover() { return !!(state.me && state.me.auth_method !== 'device' && (state.me.approver_for > 0 || state.me.account_type === 'admin')); }
function renderNav() {
  const n = $('#pl-nav');
  n.innerHTML = NAV.filter(([r]) => r !== 'approvals' || isApprover()).map(([r, label, icon]) => `<a href="#${r}" data-nav="${r}" class="${state.route === r ? 'active' : ''}"><span class="ic" aria-hidden="true">${icon}</span><span>${label}</span>${r === 'approvals' && state.pendingApprovals && acquisitionOn() ? `<span class="pl-badge">${state.pendingApprovals}</span>` : ''}</a>`).join('');
}
async function refreshApprovalBadge() {
  if (!isApprover() || !acquisitionOn()) return;
  try { const d = await api('/api/player/v1/approvals?status=pending&limit=1'); state.pendingApprovals = d.total; renderNav(); } catch {}
}
function currentHash() { return location.hash.slice(1) || 'home'; }
async function route() {
  if (!state.me) return;
  const raw = currentHash(); const [r, id, sub, tab] = raw.split('/');
  // A typed search never outlives the Library; the kind and tag filters are remembered by design (rememberLib).
  if (r !== 'library') state.lib.q = '';
  state.route = (r === 'authors' || r === 'series') ? 'library' : (r === 'pair' || r === 'about') ? 'settings' : r; renderNav(); closeModal();
  if (state.consumeCleanup && !(r === 'library' && sub === 'read')) { try { await state.consumeCleanup(); } catch {} state.consumeCleanup = null; if (state.reloadWhenLeaving) { state.reloadWhenLeaving = false; applyAppUpdate(); return; } }
  const view = $('#pl-view'); view.scrollTop = 0;
  try {
    if (r === 'home') return await renderHome();
    if (r === 'podcasts' && id === 'episode' && sub) return await renderEpisode(sub);
    if (r === 'library' && id && sub === 'read' && tab) return await renderConsume(id, tab);
    if (r === 'library' && id) return await renderBook(id);
    if (r === 'library') return await renderLibrary();
    if (r === 'authors' && id) return await renderAuthor(id);
    if (r === 'authors') return await renderAuthors();
    if (r === 'series' && id) return await renderSeriesDetail(id);
    if (r === 'series') return await renderSeries();
    if (r === 'podcasts' && id) return await renderPodcastShow(id);
    if (r === 'podcasts') return await renderPodcasts();
    if (r === 'discover') return await renderDiscover();
    if (r === 'approvals' && isApprover()) return await renderApprovals(id, sub);
    if (r === 'pair') return await renderPair(id);
    if (r === 'settings') return await renderSettings();
    if (r === 'about') return await renderAbout();
    location.hash = 'home';
  } catch (err) {
    view.innerHTML = `<h1 class="pl-h1">Something went wrong</h1><div class="pl-error">${e(err.message)}</div>`;
  }
}
window.addEventListener('hashchange', route);
document.addEventListener('click', ev => { if (playEpisodeFromTap(ev.target, id => { location.hash = `podcasts/episode/${id}`; })) return; const a = ev.target.closest('[data-go]'); if (a) { ev.preventDefault(); location.hash = a.dataset.go; } });

/* ---------- helpers ---------- */
function hue(str) { let h = 0; for (const c of String(str || '')) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function authorsOf(w) { return (w.authors || []).map(a => a.name).join(', '); }
function kindsOf(w) { return [...new Set((w.assets || []).filter(a => a.status === 'owned').map(a => a.kind))]; }
// Converted copies (issue #265): when a book has an M4B/EPUB bookdoe made from another file, that copy is the
// primary way in and the original stays one tap away. A work with no copy is untouched by all of this.
const hasCopy = (w, a) => !a.derived_from_asset_id && (w.assets || []).some(x => x.derived_from_asset_id && x.kind === a.kind && x.status === 'owned' && (x.files || []).some(f => !f.missing));
function coverHtml(w, extra = '') {
  const kinds = kindsOf(w);
  const badges = kinds.length ? `<div class="pl-kinds">${kinds.map(k => `<span class="pl-kind">${k === 'audiobook' ? 'AUDIO' : 'EBOOK'}</span>`).join('')}</div>` : '';
  const img = w.has_cover ? `<img src="/api/v1/library/${e(w.id)}/cover" alt="" loading="lazy" decoding="async" data-img-fallback="remove">` : '';
  return `<div class="pl-cover" style="--hue:${hue(w.title)}"><div class="pl-fallback"><strong>${e(w.title)}</strong><span>${e(authorsOf(w))}</span></div>${img}${badges}${extra}</div>`;
}
// Format chips and a "N files · 8h 12m" line. Never a path or a per-file list.
function fmtSpan(sec) { sec = Math.round(Number(sec) || 0); if (!sec) return ''; const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60); return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`; }
function assetFormatList(a) { return (a.formats && a.formats.length) ? a.formats : (a.format ? [String(a.format).toLowerCase()] : []); }
function formatChips(a) {
  const fm = assetFormatList(a); const primary = a.primary_format || fm[0];
  return fm.map(f => `<span class="pl-fmt${f === primary && fm.length > 1 ? ' primary' : ''}" data-format="${e(f)}">${e(f.toUpperCase())}</span>`).join('');
}
function assetSummaryLine(a) {
  const present = (a.files || []).filter(f => !f.missing);
  const n = a.file_count != null ? a.file_count : present.length;
  const dur = a.total_duration_seconds || present.reduce((t, f) => t + Number(f.duration_seconds || 0), 0) || a.duration_seconds;
  if (!n) return '';
  return `${n} file${n === 1 ? '' : 's'}${fmtSpan(dur) ? ' · ' + fmtSpan(dur) : ''}`;
}
function cardFormats(w) { const f = [...new Set((w.assets || []).filter(a => a.status === 'owned' || a.status === undefined).map(a => a.primary_format || (a.format ? String(a.format).toLowerCase() : '')).filter(Boolean))]; return f.length ? ' · ' + f.map(x => x.toUpperCase()).join(' + ') : ''; }
function bookCard(w) {
  return `<button class="pl-book" data-go="library/${e(w.id)}" aria-label="${e(w.title)}">${coverHtml(w)}<div class="pl-book-title">${e(w.title)}</div><div class="pl-book-author">${e(authorsOf(w) || 'Unknown author')}</div></button>`;
}
function listItem(w) {
  return `<button class="pl-list-item" data-go="library/${e(w.id)}">${coverHtml(w)}<div><strong>${e(w.title)}</strong><span>${e(authorsOf(w) || 'Unknown author')}${kindsOf(w).length ? ' · ' + kindsOf(w).map(k => k === 'audiobook' ? 'Audiobook' : 'Ebook').join(' + ') + cardFormats(w) : ''}</span></div><span class="pl-muted">›</span></button>`;
}
function skeleton(n = 6) { return `<div class="pl-grid">${Array.from({ length: n }, () => `<div><div class="pl-cover"></div><div class="pl-skeleton" style="margin-top:8px"></div></div>`).join('')}</div>`; }

/* ---------- pages ---------- */
// <continue-row-helpers>
// Pure pieces of the Continue Listening row so they can be tested under node (tests/test_continue_row_js.py).
function podcastCard(it) {
  const pct = it.duration_seconds && it.position_seconds != null ? Math.min(100, Math.round(it.position_seconds / it.duration_seconds * 100)) : null;
  return `<div class="pl-continue-card" data-episode="${e(it.episode_id)}"><div><h3>${e(it.title)}</h3><div class="pl-meta">${e(it.show_title)} · Podcast</div>${pct != null ? `<div class="pl-bar"><i style="width:${pct}%"></i></div>` : ''}<div class="pl-row"><button class="pl-button small" data-play-episode="${e(it.episode_id)}">▶ Play</button></div></div></div>`;
}
// The feed arrives interleaved by last played time already; render it in that order.
function continueListeningRow(items, bookCard) {
  if (!items.length) return '';
  return `<section class="pl-section"><div class="pl-section-head"><h2>Continue listening</h2></div><div class="pl-continue">${items.map(it => it.kind === 'podcast' ? podcastCard(it) : bookCard(it)).join('')}</div></section>`;
}
// A tap on a podcast entry opens #podcasts/episode/<id>; `play` is the router's entry point.
function playEpisodeFromTap(target, play) {
  const b = target && target.closest && target.closest('[data-play-episode]');
  if (!b) return false;
  play(b.dataset.playEpisode);
  return true;
}
// The show page's own #pod-audio when it is on screen; otherwise one body-level element made once and reused by every
// tap, so a route change (which rewrites #pl-view) cannot detach it while it plays and two taps never play at once.
function podAudio(doc) {
  const own = doc.getElementById('pod-audio');
  let shared = doc.getElementById('pod-audio-shared');
  if (own) { if (shared) shared.pause(); return own; }
  if (!shared) {
    shared = doc.createElement('audio'); shared.id = 'pod-audio-shared'; shared.controls = true;
    shared.style.cssText = 'position:fixed;left:0;right:0;bottom:0;width:100%;z-index:50';
    doc.body.appendChild(shared);
  }
  return shared;
}
// Play one episode and save its position every 15 s and on pause, under the account's own id (server-side).
async function playEpisode(episodeId, seasons) {
  const audio = podAudio(document); audio.classList.remove('hidden');
  audio.src = `/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/audio`;
  try {
    const p = (await api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/progress`)).progress;
    if (p && !p.played && p.position_seconds > 0) audio.currentTime = p.position_seconds;
  } catch {}
  const save = () => api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/progress`, { method: 'PUT', body: { position_seconds: audio.currentTime, client_updated_at: Date.now() / 1000 } }).catch(() => {});
  let last = 0;
  audio.ontimeupdate = () => { if (audio.currentTime - last >= 15 || audio.currentTime < last) { last = audio.currentTime; save(); } };
  audio.onpause = save;
  audio.onended = async () => {
    await api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/played`, { method: 'POST', body: {} }).catch(() => {});
    try { const n = (await api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(episodeId)}/next`)).episode; if (n) playEpisode(n.id, seasons); } catch {}
  };
  audio.play().catch(() => {});
}
// </continue-row-helpers>
function continueCard(it) {
  const pct = it.percent != null ? Math.round(it.percent * 100) : null;
  const w = { id: it.work_id, title: it.title, has_cover: it.has_cover, authors: it.authors ? [{ name: it.authors }] : [], assets: [{ kind: it.kind, status: 'owned' }] };
  const meta = it.kind === 'audiobook'
    ? (it.duration && it.position != null ? `${fmtClock(Math.max(0, it.duration - it.position))} left in this part` : pct != null ? `${pct}% listened` : 'Listening')
    : (pct != null ? `${pct}% read` : 'Reading');
  return `<div class="pl-continue-card">${coverHtml(w)}<div><h3>${e(it.title)}</h3><div class="pl-meta">${e(it.authors || '')}${it.authors ? ' · ' : ''}${meta}</div>${pct != null ? `<div class="pl-bar"><i style="width:${pct}%"></i></div>` : ''}<div class="pl-row"><button class="pl-button small" data-go="library/${e(it.work_id)}/read/${e(it.asset_id)}">${it.kind === 'audiobook' ? '▶ Resume' : '▤ Resume'}</button><button class="pl-button ghost small" data-go="library/${e(it.work_id)}">Details</button></div></div></div>`;
}
const fmtClock = s => { s = Math.max(0, Math.floor(Number(s) || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(x).padStart(2, '0')}`; };

// One quiet, dismissible offer after a normal sign-in; the answer is remembered on this device.
function trustPrompt() {
  const s = state.signin;
  if (!s || !s.trust_allowed || s.trusted_here || state.me.auth_method !== 'session' || localStorage.getItem('alx-trust-dismissed')) return '';
  return `<div class="pl-card pl-card-body" id="trust-prompt" style="margin:0 0 14px"><strong>Stay signed in on this device?</strong><div class="pl-muted pl-small" style="margin:4px 0 10px">This device stays signed in until you revoke it. Use only on your own devices.</div><div class="pl-row"><button class="pl-button small" id="trust-yes">Trust this device</button><button class="pl-button ghost small" id="trust-no">Not now</button></div></div>`;
}
document.addEventListener('click', async ev => {
  if (ev.target.id === 'trust-no') { localStorage.setItem('alx-trust-dismissed', '1'); $('#trust-prompt')?.remove(); }
  if (ev.target.id === 'trust-yes') { try { await trustThisDevice(); localStorage.setItem('alx-trust-dismissed', '1'); $('#trust-prompt')?.remove(); } catch (er) { toast(er.message, 'bad'); } }
});
async function trustThisDevice() {
  await api('/api/player/v1/devices/trust', { method: 'POST', body: {} });
  toast('This device is trusted'); state.signin = { ...(state.signin || {}), trusted_here: true };
  if ($('#pl-trust')) trustSection();
}
async function renderHome() {
  const view = $('#pl-view'); const name = state.me.display_name || state.me.username;
  view.innerHTML = `<h1 class="pl-h1">Hello, ${e(name)}</h1><p class="pl-sub">Pick up where you left off.</p>${skeleton(6)}`;
  const h = await api('/api/player/v1/home');
  const shelf = (title, items, link) => `<section class="pl-section"><div class="pl-section-head"><h2>${title}</h2>${link || ''}</div>${items}</section>`;
  const nothing = h.continue_reading.length + h.continue_listening.length === 0;
  view.innerHTML = `<h1 class="pl-h1">Hello, ${e(name)}</h1>${trustPrompt()}<p class="pl-sub">${nothing ? 'Open a book and it will wait for you here.' : 'Pick up where you left off.'}</p>
    ${h.continue_reading.length ? shelf('Continue reading', `<div class="pl-continue">${h.continue_reading.map(continueCard).join('')}</div>`) : ''}
    ${continueListeningRow(h.continue_listening, continueCard)}
    ${shelf('Recently added', h.recently_added.length ? `<div class="pl-grid">${h.recently_added.map(bookCard).join('')}</div>` : '<div class="pl-empty"><strong>Nothing here yet</strong>Books appear as soon as they are added to your shelf.</div>', '<a href="#library">My Library</a>')}
    <section class="pl-section"><div class="pl-card pl-card-body pl-row" style="justify-content:space-between"><div><strong>Looking for something new?</strong><div class="pl-muted pl-small">${acquisitionOn() ? 'Search the library or request a book.' : 'Search the library.'}</div></div><a class="pl-button quiet" href="#discover">Discover</a></div></section>`;
}

const LIB_FILTERS = [['', 'All'], ['ebook', 'Ebooks'], ['audiobook', 'Audiobooks'], ['favorites', 'Favorites'], ['reading', 'In progress'], ['finished', 'Finished'], ['offline', 'Offline']];
async function renderLibrary() {
  const view = $('#pl-view'); const L = state.lib;
  // Tags come from /api/v1/tags, which for a kid lists only tags carried by works that
  // kid can see (tagging.list_tags). The kids system tag is redundant on a kid account.
  const tags = (await api('/api/v1/tags').catch(() => ({ items: [] }))).items.filter(t => !(t.name === 'kids' && state.me.account_type === 'kid'));
  if (L.tag && !tags.some(t => t.id === L.tag)) { L.tag = ''; rememberLib(); }
  view.innerHTML = `<h1 class="pl-h1">My Library</h1><div class="pl-search"><span aria-hidden="true">⌕</span><input id="lib-q" placeholder="Search titles and authors" value="${e(L.q)}" autocomplete="off" type="search" enterkeyhint="search" aria-label="Search the library"></div>
    <div class="pl-toolbar">${LIB_FILTERS.map(([v, l]) => `<button class="pl-chip ${L.kind === v ? 'active' : ''}" data-kind="${v}">${l}</button>`).join('')}<select class="pl-select" id="lib-tag" aria-label="Filter by tag"><option value="">Any tag</option>${tags.map(t => `<option value="${e(t.id)}" ${L.tag === t.id ? 'selected' : ''}>${e(t.label || (t.name === 'kids' ? 'Kids shelf' : t.name))} (${t.work_count})</option>`).join('')}</select><span style="flex:1"></span><a class="pl-chip" href="#authors">Authors</a><a class="pl-chip" href="#series">Series</a><div class="pl-segment" aria-label="View"><button data-view="grid" class="${L.view === 'grid' ? 'on' : ''}" aria-label="Grid">▦</button><button data-view="list" class="${L.view === 'list' ? 'on' : ''}" aria-label="List">☰</button></div></div>
    <div id="lib-active" class="pl-row" style="margin:0 0 12px"></div>
    <div id="lib-body">${skeleton(9)}</div>`;
  const reload = () => loadLibrary(true).catch(er => toast(er.message, 'bad'));
  let t;
  const typed = (v, now) => { clearTimeout(t); const q = v.trim(); if (now || !q) { if (q !== L.q) { L.q = q; reload(); } } else t = setTimeout(() => { L.q = q; reload(); }, 260); };
  $('#lib-q').oninput = ev => typed(ev.target.value, false);
  $('#lib-q').addEventListener('search', ev => typed(ev.target.value, true));
  $('#lib-q').addEventListener('keydown', ev => { if (ev.key === 'Escape' && ev.target.value) { ev.target.value = ''; typed('', true); } });
  $$('[data-kind]').forEach(b => b.onclick = () => { L.kind = b.dataset.kind; rememberLib(); $$('[data-kind]').forEach(x => x.classList.toggle('active', x === b)); loadLibrary(true).catch(er => toast(er.message, 'bad')); });
  $('#lib-tag').onchange = ev => { L.tag = ev.target.value; rememberLib(); loadLibrary(true).catch(er => toast(er.message, 'bad')); };
  $$('[data-view]').forEach(b => b.onclick = () => { L.view = b.dataset.view; rememberLib(); $$('[data-view]').forEach(x => x.classList.toggle('on', x === b)); paintLibrary(); });
  await loadLibrary(true);
}
const libFiltered = () => !!(state.lib.q || state.lib.kind || state.lib.tag);
// Reset every filter (text, kind, tag); the grid/list choice stays.
function clearLibFilters() { const L = state.lib; L.q = ''; L.kind = ''; L.tag = ''; rememberLib(); renderLibrary().catch(er => toast(er.message, 'bad')); }
function paintLibActive() {
  const el = $('#lib-active'); if (!el) return;
  el.style.display = libFiltered() ? '' : 'none';
  el.innerHTML = libFiltered() ? '<button type="button" class="pl-button ghost small" id="lib-clear">Clear filters</button>' : '';
  $('#lib-clear')?.addEventListener('click', clearLibFilters);
}
async function loadLibrary(reset) {
  const L = state.lib; if (reset) { L.offset = 0; L.items = []; }
  let d;
  if (L.kind === 'favorites') d = await api(`/api/player/v1/favorites?limit=60&offset=${L.offset}`);
  else if (L.kind === 'offline') d = await offlineLibrary();
  else {
    const q = new URLSearchParams({ limit: '60', offset: String(L.offset) }); if (L.q) q.set('q', L.q);
    if (L.kind === 'ebook' || L.kind === 'audiobook') q.set('kind', L.kind);
    if (L.kind === 'reading' || L.kind === 'finished') q.set('read_status', L.kind);
    if (L.tag) q.set('tag_id', L.tag);
    d = await api(`/api/v1/library?${q}`);
  }
  // Favorites and Offline are built from other endpoints: apply the tag filter here.
  if (L.tag && (L.kind === 'favorites' || L.kind === 'offline')) { d = { ...d, items: d.items.filter(w => (w.tags || []).some(t => t.id === L.tag)) }; d.total = d.items.length; }
  L.items = L.items.concat(d.items); L.total = d.total; L.offset += d.items.length; paintLibrary();
}
function paintLibrary() {
  const L = state.lib; const el = $('#lib-body'); if (!el) return;
  paintLibActive();
  if (!L.items.length) {
    el.innerHTML = `<div class="pl-empty"><strong>${L.q ? 'No matches' : L.kind === 'favorites' ? 'No favorites yet' : 'Nothing here yet'}</strong>${L.q ? 'Try another title or author.' : L.kind === 'favorites' ? 'Tap the heart on a book to keep it close.' : 'Books you can read or listen to appear here.'}${libFiltered() ? '<div style="margin-top:12px"><button type="button" class="pl-button" id="lib-empty-clear">Clear filters</button></div>' : ''}</div>`;
    $('#lib-empty-clear')?.addEventListener('click', clearLibFilters);
    return;
  }
  el.innerHTML = (L.view === 'list' ? `<div class="pl-list">${L.items.map(listItem).join('')}</div>` : `<div class="pl-grid">${L.items.map(bookCard).join('')}</div>`) +
    (L.offset < L.total ? `<div style="text-align:center;margin:18px 0"><button class="pl-button ghost" id="lib-more">Show more (${L.total - L.offset} left)</button></div>` : `<p class="pl-muted pl-small" style="text-align:center;margin:16px 0">${L.total} book${L.total === 1 ? '' : 's'}</p>`);
  const more = $('#lib-more'); if (more) more.onclick = () => { more.disabled = true; loadLibrary(false).catch(er => toast(er.message, 'bad')); };
}

async function renderAuthors() {
  const view = $('#pl-view'); view.innerHTML = `<h1 class="pl-h1">Authors</h1>${skeleton(3)}`;
  const d = await api('/api/v1/authors?limit=1000');
  const items = d.items.filter(a => (a.owned_audio || 0) + (a.owned_ebooks || 0) > 0 || a.work_count);
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="library">‹ Library</button></div><h1 class="pl-h1">Authors</h1><div class="pl-search" style="margin-bottom:12px"><span aria-hidden="true">⌕</span><input id="au-q" placeholder="Find an author" aria-label="Find an author"></div><div class="pl-list" id="au-list">${items.map(a => `<button class="pl-list-item" data-go="authors/${e(a.id)}" data-name="${e(a.name.toLowerCase())}"><div class="pl-cover" style="--hue:${hue(a.name)};aspect-ratio:1;border-radius:50%"><div class="pl-fallback" style="justify-content:center;align-items:center;padding:0"><strong style="font-size:16px">${e(a.name.split(' ').map(x => x[0]).join('').slice(0, 2).toUpperCase())}</strong></div></div><div><strong>${e(a.name)}</strong><span>${a.owned_ebooks || 0} ebook${a.owned_ebooks === 1 ? '' : 's'} · ${a.owned_audio || 0} audiobook${a.owned_audio === 1 ? '' : 's'}</span></div><span class="pl-muted">›</span></button>`).join('') || '<div class="pl-empty"><strong>No authors yet</strong></div>'}</div>`;
  $('#au-q').oninput = ev => { const q = ev.target.value.trim().toLowerCase(); $$('#au-list [data-name]').forEach(b => b.classList.toggle('hidden', !!q && !b.dataset.name.includes(q))); };
}
async function renderAuthor(id) {
  const view = $('#pl-view'); view.innerHTML = skeleton(6);
  const [a, d] = await Promise.all([api(`/api/v1/authors/${encodeURIComponent(id)}`), api(`/api/v1/library?author_id=${encodeURIComponent(id)}&limit=200`)]);
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="authors">‹ Authors</button></div><h1 class="pl-h1">${e(a.name)}</h1><p class="pl-sub">${d.total} book${d.total === 1 ? '' : 's'} on your shelf</p>${d.items.length ? `<div class="pl-grid">${d.items.map(bookCard).join('')}</div>` : '<div class="pl-empty"><strong>Nothing available yet</strong></div>'}`;
}
async function renderSeries() {
  const view = $('#pl-view'); view.innerHTML = `<h1 class="pl-h1">Series</h1>${skeleton(3)}`;
  const d = await api('/api/v1/series?limit=1000');
  const items = d.items.filter(s => s.owned_count > 0);
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="library">‹ Library</button></div><h1 class="pl-h1">Series</h1><div class="pl-search" style="margin-bottom:12px"><span aria-hidden="true">⌕</span><input id="se-q" placeholder="Find a series" aria-label="Find a series"></div><div class="pl-list" id="se-list">${items.map(s => `<button class="pl-list-item" data-go="series/${e(s.id)}" data-name="${e(s.name.toLowerCase())}"><div class="pl-cover" style="--hue:${hue(s.name)};aspect-ratio:1;border-radius:12px"><div class="pl-fallback" style="justify-content:center;align-items:center;padding:0"><strong style="font-size:15px">${e(String(s.owned_count))}</strong></div></div><div><strong>${e(s.name)}</strong><span>${s.owned_count} of ${s.work_count} on your shelf</span></div><span class="pl-muted">›</span></button>`).join('') || '<div class="pl-empty"><strong>No series yet</strong></div>'}</div>`;
  $('#se-q').oninput = ev => { const q = ev.target.value.trim().toLowerCase(); $$('#se-list [data-name]').forEach(b => b.classList.toggle('hidden', !!q && !b.dataset.name.includes(q))); };
}
async function renderSeriesDetail(id) {
  const view = $('#pl-view'); view.innerHTML = skeleton(6);
  const [s, d] = await Promise.all([api(`/api/v1/series/${encodeURIComponent(id)}`), api(`/api/v1/library?series_id=${encodeURIComponent(id)}&limit=200`)]);
  const pos = Object.fromEntries((s.works || []).map(w => [w.id, w.position]));
  const items = d.items.slice().sort((a, b) => (parseFloat(pos[a.id]) || 1e9) - (parseFloat(pos[b.id]) || 1e9));
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="series">‹ Series</button></div><h1 class="pl-h1">${e(s.name)}</h1><p class="pl-sub">${items.length} on your shelf, in series order</p>${items.length ? `<div class="pl-grid">${items.map(w => bookCard(w).replace('<div class="pl-book-title">', `<div class="pl-book-title">${pos[w.id] ? `<span class="pl-muted">#${e(pos[w.id])} </span>` : ''}`)).join('')}</div>` : '<div class="pl-empty"><strong>Nothing available yet</strong></div>'}`;
}

// Books kept on this device: the service worker's per-user content cache holds the
// file bytes under /api/v1/library/<work>/files/<file>; the work JSON is served from
// the same cache when offline, so this list works with no network at all.
async function offlineLibrary() {
  const pwa = window.bookdoePwa; if (!pwa || !pwa.supported) return { items: [], total: 0 };
  const urls = await pwa.listContent();
  const ids = [...new Set(urls.map(u => (u.match(/^\/api\/v1\/library\/([^/]+)\/files\//) || [])[1]).filter(Boolean))];
  const items = (await Promise.all(ids.map(id => api(`/api/v1/library/${id}`).catch(() => null)))).filter(Boolean);
  return { items, total: items.length };
}
function offlineButtons(w) {
  const pwa = window.bookdoePwa; if (!pwa || !pwa.supported) return '';
  // ONE control per owned asset, acting on all of its files (an audiobook can have dozens of
  // tracks); never one button per file.
  const assets = (w.assets || []).filter(a => a.status === 'owned' && !hasCopy(w, a)).map(a => ({ kind: a.kind, id: a.id, files: (a.files || []).filter(f => !f.missing) })).filter(a => a.files.length);
  if (!assets.length) return '';
  return `<div class="pl-row" style="margin-top:14px" id="offline-row">${assets.map(a => `<button class="pl-button ghost small offline-toggle" data-asset="${e(a.id)}" data-files="${a.files.length}" data-urls="${e(JSON.stringify(a.files.map(f => `/api/v1/library/${w.id}/files/${f.id}`)))}" data-name="${e(w.title || 'this book')}" data-suffix="${assets.length > 1 ? e(a.kind) : ''}">Keep offline${assets.length > 1 ? ` (${a.kind})` : ''}</button>`).join('')}</div>`;
}
async function wireOfflineButtons() {
  const pwa = window.bookdoePwa; if (!pwa || !pwa.supported) return;
  // A copy the server took away (the book left the shelf) is not silently forgotten:
  // once the book is back the button says "Download again" and explains why.
  const revoked = new Set(pwa.listRevoked ? await pwa.listRevoked().catch(() => []) : []);
  $$('.offline-toggle').forEach(async b => {
    const urls = JSON.parse(b.dataset.urls || '[]'); const sfx = b.dataset.suffix ? ` (${b.dataset.suffix})` : '';
    const gone = urls.some(u => revoked.has(u));
    const paint = cached => {
      b.dataset.cached = cached ? '1' : '0';
      b.textContent = (cached ? '✓ Kept offline' : gone ? '↻ Download again' : 'Keep offline') + sfx;
      b.classList.toggle('quiet', cached);
      b.title = !cached && gone ? 'This book was off your shelf, so its saved copy was removed from this device. Download it again to listen offline.' : '';
      b.dataset.revoked = !cached && gone ? '1' : '0';
    };
    const allCached = async () => { for (const u of urls) if (!(await pwa.isCached(u))) return false; return true; };
    try { paint(await allCached()); } catch { paint(false); }
    b.onclick = async () => {
      b.disabled = true;
      try {
        if (b.dataset.cached === '1') { b.textContent = 'Removing…'; for (const u of urls) await pwa.forgetContent(u); paint(false); toast('Removed from this device'); }
        else {
          for (let i = 0; i < urls.length; i++) {
            b.textContent = urls.length > 1 ? `Saving ${i + 1}/${urls.length}…` : 'Saving…';
            if (await pwa.isCached(urls[i]).catch(() => false)) continue;
            const r = await pwa.cacheContent(urls[i]); if (!r?.ok) throw new Error(r?.error || 'Could not store the file');
          }
          paint(true); toast(`${b.dataset.name} is available offline`);
        }
      } catch (er) { paint(await allCached().catch(() => false)); toast(er.message, 'bad'); }
      finally { b.disabled = false; }
    };
  });
}

// Send to Kindle. The server decides what is sendable (EPUB first, then the other
// Kindle-accepted formats) and whether mail is set up; the button is hidden when
// either is false, reads "Add Kindle email" (leading to Settings) when the account has
// no address, and sends once per click (disabled while in flight).
async function kindleInfo(force) {
  if (!force && state.kindle) return state.kindle;
  try { state.kindle = await api('/api/player/v1/kindle'); } catch { state.kindle = { configured: false }; }
  return state.kindle;
}
async function kindleButton(w) {
  const slot = $('#kindle-slot'); if (!slot) return;
  const info = await kindleInfo();
  if (!info.configured) return;
  const opts = await api(`/api/player/v1/kindle/${encodeURIComponent(w.id)}`).catch(() => ({ items: [] }));
  if (!opts.items.length) return;
  const best = opts.items[0];
  if (!info.kindle_email) { slot.innerHTML = `<a class="pl-button ghost" id="kindle-add" href="#settings">✉ Add Kindle email</a>`; return; }
  slot.innerHTML = `<button class="pl-button ghost" id="kindle-send" title="Sends the ${e(best.format.toUpperCase())} to ${e(info.kindle_email)}">✉ Send to Kindle</button>`;
  const b = $('#kindle-send');
  b.onclick = async () => {
    if (b.disabled) return;
    b.disabled = true; const label = b.textContent; b.textContent = 'Sending…';
    try { const r = await api(`/api/player/v1/kindle/send/${encodeURIComponent(w.id)}`, { method: 'POST', body: {} }); toast(`Sent ${r.filename} to ${r.to}. It arrives on the Kindle in a few minutes.`, 'good', 7000); b.textContent = '✓ Sent'; }
    catch (er) { toast(er.message, 'bad', 7000); b.textContent = label; b.disabled = false; }
  };
}

async function renderBook(id) {
  const view = $('#pl-view'); view.innerHTML = skeleton(1);
  const [w, fav] = await Promise.all([api(`/api/v1/library/${encodeURIComponent(id)}`), api('/api/player/v1/favorites?limit=500').catch(() => ({ items: [] }))]);
  let isFav = fav.items.some(x => x.id === w.id);
  const owned = (w.assets || []).filter(a => a.status === 'owned' && (a.files || []).some(f => !f.missing))
    .sort((a, b) => Number(!!b.derived_from_asset_id) - Number(!!a.derived_from_asset_id));
  const prog = kind => (w.progress || []).find(p => p.kind === kind);
  const action = a => {
    // positions are per asset: with a converted copy present, never show the other version's place
    const p = (w.progress || []).find(x => x.asset_id === a.id) || (hasCopy(w, a) || a.derived_from_asset_id ? null : prog(a.kind)); const readable = a.kind === 'ebook' ? a.files.some(f => !f.missing && f.readable) : a.files.some(f => !f.missing && f.readable);
    if (!readable) return '';
    const label = a.kind === 'ebook' ? (p ? `Resume reading${p.percent != null ? ` · ${Math.round(p.percent * 100)}%` : ''}` : 'Read') : (p ? `Resume listening${p.percent != null ? ` · ${Math.round(p.percent * 100)}%` : ''}` : 'Listen');
    if (hasCopy(w, a)) return `<button class="pl-button ghost small" data-go="library/${e(w.id)}/read/${e(a.id)}">${a.kind === 'ebook' ? '▤' : '♫'} Original${a.format ? ' (' + e(String(a.format).toUpperCase()) + ')' : ''}${p ? ' · resume' : ''}</button>`;
    return `<button class="pl-button ${a.kind === 'ebook' ? '' : 'quiet'}" data-go="library/${e(w.id)}/read/${e(a.id)}">${a.kind === 'ebook' ? '▤' : '♫'} ${label}</button>`;
  };
  const series = (w.series || []).map(s => `<a class="pl-series-link" href="#series/${e(s.id)}">Series: ${e(s.name)}${s.position ? ' #' + e(s.position) : ''} ›</a>`).join(' · ');
  const authors = (w.authors || []).map(a => `<a href="#authors/${e(a.id)}">${e(a.name)}</a>`).join(', ');
  view.innerHTML = `<div class="pl-row" style="margin-bottom:14px;justify-content:space-between"><button class="pl-button ghost small" data-go="library">‹ Library</button>${state.me && state.me.management ? `<a class="pl-button quiet small pl-manage-link" href="/manage#library/${e(w.id)}" title="Edit tags and metadata in Management">Manage</a>` : ''}<button class="pl-button ghost small" id="fav" aria-pressed="${isFav}" aria-label="Favorite">${isFav ? '♥ Favorite' : '♡ Favorite'}</button></div>
    <div class="pl-detail">${coverHtml(w)}<div>${series ? `<div class="pl-muted pl-small">${series}</div>` : ''}<h1>${e(w.title)}</h1><div class="pl-by">${authors || 'Unknown author'}${(w.narrators || []).length ? ` · read by ${e(w.narrators.map(n => n.name).join(', '))}` : ''}</div>
      <div class="pl-formats" id="pl-formats">${owned.map(a => `<div class="pl-asset-line" data-kind="${e(a.kind)}"><span class="pl-muted pl-small">${a.kind === 'ebook' ? 'Ebook' : 'Audiobook'}</span> ${formatChips(a)} <span class="pl-muted pl-small">${e(assetSummaryLine(a))}</span></div>`).join('')}</div>
      <div class="pl-actions">${owned.map(action).join('')}${owned.length ? '' : '<span class="pl-muted">No file available yet.</span>'}<span id="kindle-slot"></span></div>
      <div class="pl-desc clamp" id="desc">${e(w.description || 'No description available.')}</div>${(w.description || '').length > 500 ? '<button class="pl-button ghost small" id="desc-more" style="margin-top:8px">Read more</button>' : ''}
      <div style="margin:18px 0 8px" class="pl-row"><span class="pl-muted pl-small">Your rating</span><span class="pl-stars" id="stars">${[1, 2, 3, 4, 5].map(n => `<button data-n="${n}" class="${w.rating >= n ? 'on' : ''}" aria-label="${n} star${n > 1 ? 's' : ''}">★</button>`).join('')}</span></div>
      <div class="pl-row"><span class="pl-muted pl-small">Status</span><select class="pl-select" id="read-state" style="width:auto;min-height:40px">${[['', 'Not tracked'], ['want_to_read', 'Want to read'], ['reading', 'In progress'], ['finished', 'Finished'], ['dnf', 'Did not finish']].map(([v, l]) => `<option value="${v}" ${(w.read_status || '') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      ${(w.progress || []).length ? `<div class="pl-row" style="margin-top:12px">${w.progress.map(p => `<button class="pl-button ghost small clear-progress" data-asset="${e(p.asset_id)}" data-kind="${p.kind}">Clear ${p.kind === 'ebook' ? 'reading' : 'listening'} progress</button>`).join('')}</div>` : ''}
      ${offlineButtons(w)}
    </div></div>`;
  wireOfflineButtons();
  $('#desc-more')?.addEventListener('click', ev => { $('#desc').classList.toggle('clamp'); ev.target.textContent = $('#desc').classList.contains('clamp') ? 'Read more' : 'Show less'; });
  kindleButton(w);
  $('#fav').onclick = async () => { try { await api(`/api/player/v1/favorites/${w.id}`, { method: isFav ? 'DELETE' : 'PUT' }); isFav = !isFav; $('#fav').textContent = isFav ? '♥ Favorite' : '♡ Favorite'; $('#fav').setAttribute('aria-pressed', String(isFav)); } catch (er) { toast(er.message, 'bad'); } };
  $$('#stars button').forEach(b => b.onclick = async () => { const n = Number(b.dataset.n); try { await api(`/api/v1/library/${w.id}/rating`, { method: 'PUT', body: { rating: w.rating === n ? null : n } }); renderBook(id); } catch (er) { toast(er.message, 'bad'); } });
  $('#read-state').onchange = async ev => { try { await api(`/api/v1/library/${w.id}/read-state`, { method: 'PUT', body: { status: ev.target.value || null } }); toast('Status updated'); } catch (er) { toast(er.message, 'bad'); } };
  $$('.clear-progress').forEach(b => b.onclick = () => {
    const noun = b.dataset.kind === 'ebook' ? 'reading' : 'listening';
    modal('Clear progress', `<p>This forgets where you are in this ${b.dataset.kind === 'ebook' ? 'book' : 'audiobook'}. Next time it starts from the beginning.</p>`, { footer: `<button class="pl-button ghost" id="cp-keep">Keep my place</button><button class="pl-button danger" id="cp-go">Clear ${noun} progress</button>` });
    $('#cp-keep').onclick = closeModal;
    $('#cp-go').onclick = async () => { try { await api(`/api/v1/library/${w.id}/progress/${b.dataset.asset}`, { method: 'DELETE' }); closeModal(); toast('Progress cleared'); renderBook(id); } catch (er) { toast(er.message, 'bad'); } };
  });
}

async function renderConsume(workId, assetId) {
  const mod = await import('/assets/consume.js');
  state.consumeCleanup = await mod.open({ workId, assetId, host: $('#pl-view'), api, toast, modal, closeModal, chrome: 'player', mini: $('#pl-mini'), onBack: () => { location.hash = `library/${workId}`; } });
}

/* ---------- podcasts ---------- */
const podSeasonLabel = s => s.is_extras ? 'Extras' : `Season ${s.number}`;
const podSortSeasons = seasons => [...(seasons || [])].sort((a, b) => (a.is_extras ? 1 : 0) - (b.is_extras ? 1 : 0) || (Number(a.number) || 0) - (Number(b.number) || 0));
async function renderPodcasts() {
  const view = $('#pl-view'); view.innerHTML = skeleton(1);
  const d = await api('/api/player/v1/podcasts');
  view.innerHTML = `<h1 class="pl-h1">Podcasts</h1>${d.shows.length ? `<div class="pl-pod-shows">${d.shows.map(s => `<a class="pl-card pl-card-body" href="#podcasts/${encodeURIComponent(s.id)}"><img class="pl-pod-cover" src="/api/player/v1/podcasts/shows/${encodeURIComponent(s.id)}/cover" alt="" width="64" height="64" data-img-fallback="remove"> <strong>${e(s.title)}</strong></a>`).join('')}</div>` : '<p class="pl-muted">No podcasts yet.</p>'}`;
}
async function renderPodcastShow(id) {
  const view = $('#pl-view'); view.innerHTML = skeleton(1);
  const show = await api(`/api/player/v1/podcasts/shows/${encodeURIComponent(id)}`);
  const seasons = podSortSeasons(show.seasons);
  let sel = seasons[0]?.id;
  const draw = () => {
    const season = seasons.find(s => String(s.id) === String(sel));
    view.innerHTML = `<h1 class="pl-h1">${e(show.title)}</h1><img class="pl-pod-cover" src="/api/player/v1/podcasts/shows/${encodeURIComponent(show.id)}/cover" alt="Cover of ${e(show.title)}" width="160" height="160" data-img-fallback="remove">
      <div class="pl-pod-seasons">${seasons.map(s => `<button class="pl-button${String(s.id) === String(sel) ? '' : ' ghost'} small pod-season" data-season="${e(s.id)}">${e(podSeasonLabel(s))}</button>`).join('')}</div>
      <ul class="pl-pod-episodes">${(season?.episodes || []).map(x => `<li class="pod-episode" data-episode="${e(x.id)}"><strong>${e(x.title)}</strong> ${x.missing ? '<span class="pl-muted pod-missing">Missing</span>' : `<button class="pl-button small pod-play" data-episode="${e(x.id)}">${x.played ? 'Play again' : 'Play'}</button>`}<div class="pl-muted">${e(x.release_date || '')}${x.played ? ' · Played' : ''}</div></li>`).join('') || '<li class="pl-muted">No episodes</li>'}</ul>
      <audio id="pod-audio" controls preload="none" class="hidden"></audio>`;
    $$('.pod-season').forEach(b => b.onclick = () => { sel = b.dataset.season; draw(); });
    $$('.pod-play').forEach(b => b.onclick = () => playEpisode(b.dataset.episode, seasons));
  };
  draw();
}



async function renderEpisode(id) {
  document.getElementById('alx-audio')?.pause();   // a book that is playing yields to the episode
  const m = await api(`/api/player/v1/podcasts/episodes/${encodeURIComponent(id)}`).catch(err => { viewError(err); return null; });
  if (!m) return;
  $('#pl-view').innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="home">‹ Home</button></div><h1 class="pl-h1">${e(m.title)}</h1><p class="pl-sub">${e(m.show_title)} · Podcast</p><p class="pl-muted pl-small">Playing in the player at the bottom of the screen. It keeps going while you browse.</p>`;
  playEpisode(id, []);
}
function viewError(err) { $('#pl-view').innerHTML = `<h1 class="pl-h1">Could not play this episode</h1><div class="pl-error">${e(err.message)}</div>`; }
const KIND_LABEL = { ebook: 'Ebook', audiobook: 'Audiobook' };
const SEARCH_TEXT = { searching:'Searching',grabbed:'Found, downloading',no_match:'Not found yet',no_result:'Not found yet',skipped_cooldown:'Checked recently, will try again later',skipped_cap:'Search limit reached for today',skipped_active:'Already downloading or in the library',busy:'Search already running',throttled:'Search paused briefly, will retry',error:'Search failed, will retry' };
function statusChip(r) { return `<span class="pl-status ${e(r.tone || '')}">${e(r.friendly_status)}</span>`; }
// Where the automatic search for an approved request stands; nothing once the book is available.
function searchNote(r) { return r.search && SEARCH_TEXT[r.search.state] && r.friendly_status !== 'Available' ? ` · ${e(SEARCH_TEXT[r.search.state])}` : ''; }
function requestRow(r, { cancel = false } = {}) {
  return `<div class="pl-request"><div><strong>${r.work_id ? `<a href="#library/${e(r.work_id)}">${e(r.title)}</a>` : e(r.title)}</strong><span>${e(r.author || '')}${r.author ? ' · ' : ''}${r.kinds.map(k => KIND_LABEL[k]).join(' + ')}${r.requester ? ` · for ${e(r.requester_name || r.requester)}` : ''}${r.decision_note ? ` · "${e(r.decision_note)}"` : ''}${searchNote(r)}</span></div><div class="pl-row" style="gap:6px">${statusChip(r)}${cancel && r.status === 'pending' ? `<button class="pl-button ghost small req-cancel" data-id="${e(r.id)}" aria-label="Withdraw request">×</button>` : ''}</div></div>`;
}
function requestDialog(seed, onDone) {
  const kinds = seed.owned_kinds || [];
  const canEbook = !kinds.includes('ebook'), canAudio = !kinds.includes('audiobook');
  modal(`Request ${seed.title}`, `<p class="pl-muted pl-small" style="margin:0 0 12px">${e((seed.authors || []).join(', ') || '')}</p>
    ${seed.visible === false ? '<p>This book is in the household library. Your request asks for permission to read it.</p>' : ''}
    <div class="pl-row" style="flex-direction:column;align-items:stretch;gap:4px">
      <label class="pl-check"><input type="checkbox" name="kind" value="ebook" ${canEbook ? (seed.kind_hint === 'audiobook' && canAudio ? '' : 'checked') : 'disabled'}> Ebook${canEbook ? '' : ' (already in the library)'}</label>
      <label class="pl-check"><input type="checkbox" name="kind" value="audiobook" ${canAudio && (!canEbook || seed.kind_hint === 'audiobook') ? 'checked' : ''} ${canAudio ? '' : 'disabled'}> Audiobook${canAudio ? '' : ' (already in the library)'}</label>
    </div>`, { footer: '<button class="pl-button ghost" id="rq-cancel">Cancel</button><button class="pl-button" id="rq-go">Send request</button>' });
  $('#rq-cancel').onclick = closeModal;
  $('#rq-go').onclick = async () => {
    let chosen = $$('#pl-modal input[name=kind]:checked').map(i => i.value);
    if (seed.visible === false && !chosen.length) chosen = kinds.length ? kinds : ['ebook'];
    if (!chosen.length) { toast('Pick a format', 'bad'); return; }
    const body = seed.id ? { work_id: seed.id, kinds: chosen } : { metadata_id: seed.metadata_id, title: seed.title, author: (seed.authors || []).join(', ') || null, kinds: chosen };
    try { const r = await api('/api/player/v1/requests', { method: 'POST', body }); closeModal(); toast(r.status === 'approved' ? 'Requested. It will appear once it arrives.' : 'Request sent for approval'); onDone && onDone(); }
    catch (er) { toast(er.message, 'bad'); }
  };
}

async function discoverCaps() {
  if (state.caps && Date.now() - state.caps.at < 60000) return state.caps.value;
  let value = null;
  try { value = await api('/api/player/v1/capabilities'); } catch {}
  state.caps = { at: Date.now(), value };
  return value;
}

async function renderDiscover() {
  const view = $('#pl-view');
  const caps = await discoverCaps();
  const chat = !!(caps && caps.discover_chat);
  const tab = chat && state.discTab !== 'search' ? 'ask' : 'search';
  view.innerHTML = `<h1 class="pl-h1">Discover</h1><p class="pl-sub">${chat ? 'Tell the assistant what you are in the mood for, or search the library.' : (acquisitionOn() ? 'Search the library, or ask for a book that is not on your shelf.' : 'Search the library.')}</p>
    ${chat ? `<div class="pl-segment" id="disc-tabs" role="tablist" style="margin-bottom:14px">${[['ask', 'Ask'], ['search', 'Search']].map(([v, l]) => `<button role="tab" data-tab="${v}" aria-selected="${tab === v}" class="${tab === v ? 'on' : ''}">${l}</button>`).join('')}</div>` : ''}
    <div id="disc-pane"></div>`;
  $$('#disc-tabs button').forEach(b => b.onclick = () => { state.discTab = b.dataset.tab; stopChatPolling(); renderDiscover(); });
  if (tab === 'ask') return renderDiscoverChat($('#disc-pane'));
  return renderDiscoverSearch($('#disc-pane'));
}

async function renderDiscoverSearch(pane) {
  pane.innerHTML = `<div class="pl-search"><span aria-hidden="true">⌕</span><input id="disc-q" placeholder="Title or author" autocomplete="off" aria-label="Search"></div><div id="disc-body" style="margin-top:16px"></div>
    ${acquisitionOn() ? '<section class="pl-section" id="disc-mine"><div class="pl-section-head"><h2>My requests</h2></div><div class="pl-card" id="disc-reqs"><div class="pl-card-body pl-muted pl-small">Loading…</div></div></section>' : ''}`;
  const paintMine = async () => {
    if (!acquisitionOn()) return;
    const d = await api('/api/player/v1/requests?limit=50').catch(() => ({ items: [] }));
    const el = $('#disc-reqs'); if (!el) return;
    el.innerHTML = d.items.length ? d.items.map(r => requestRow(r, { cancel: true })).join('') : '<div class="pl-card-body pl-muted pl-small">Nothing requested yet.</div>';
    $$('.req-cancel', el).forEach(b => b.onclick = async () => { try { await api(`/api/player/v1/requests/${b.dataset.id}`, { method: 'DELETE' }); toast('Request withdrawn'); paintMine(); } catch (er) { toast(er.message, 'bad'); } });
  };
  paintMine();
  let t;
  $('#disc-q').oninput = ev => {
    clearTimeout(t); const q = ev.target.value.trim();
    t = setTimeout(async () => {
      const body = $('#disc-body'); if (!body) return;
      if (!q) { body.innerHTML = ''; return; }
      body.innerHTML = '<div class="pl-skeleton"></div>';
      try {
        const d = await api(`/api/player/v1/requests/search?q=${encodeURIComponent(q)}`);
        const acq = acquisitionOn();
        const lib = d.in_library.filter(x => acq || x.visible).map(x => `<div class="pl-result"><div class="pl-cover" style="--hue:${hue(x.title)}"><div class="pl-fallback"><strong style="font-size:9px">${e(x.title)}</strong></div>${x.has_cover ? `<img src="/api/v1/library/${e(x.id)}/cover" alt="" loading="lazy" data-img-fallback="remove">` : ''}</div><div><strong>${e(x.title)}</strong><span>${e(x.authors.join(', '))}${x.owned_kinds.length ? ' · ' + x.owned_kinds.map(k => KIND_LABEL[k]).join(' + ') + ' in the library' : acq ? ' · not yet in the library' : ''}</span></div>${x.visible ? `<a class="pl-button quiet small" href="#library/${e(x.id)}">Open</a>` : `<button class="pl-button small disc-req" data-i="${d.in_library.indexOf(x)}" data-src="lib">Request</button>`}</div>`).join('');
        const ext = !acq ? '' : d.external.map((x, i) => `<div class="pl-result"><div class="pl-cover" style="--hue:${hue(x.title)}"><div class="pl-fallback"><strong style="font-size:9px">${e(x.title)}</strong></div>${x.cover_url ? `<img src="${e(x.cover_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-img-fallback="remove">` : ''}</div><div><strong>${e(x.title)}</strong><span>${e((x.authors || []).join(', '))}${x.year ? ' · ' + e(String(x.year).slice(0, 4)) : ''}</span></div><button class="pl-button small disc-req" data-i="${i}" data-src="ext">Request</button></div>`).join('');
        body.innerHTML = `${lib ? `<h2 style="font-size:15px;margin:0 0 8px">In the library</h2>${lib}` : ''}${ext ? `<h2 style="font-size:15px;margin:14px 0 8px">Elsewhere</h2>${ext}` : ''}${acq && d.external_error ? `<p class="pl-muted pl-small">${e(d.external_error)}</p>` : ''}${!lib && !ext ? '<div class="pl-empty"><strong>Nothing found</strong>Try a different spelling.</div>' : ''}`;
        $$('.disc-req', body).forEach(b => b.onclick = () => requestDialog(b.dataset.src === 'lib' ? d.in_library[Number(b.dataset.i)] : d.external[Number(b.dataset.i)], paintMine));
      } catch (er) { body.innerHTML = `<div class="pl-error">${e(er.message)}</div>`; }
    }, 300);
  };
  $('#disc-q').focus();
}

/* ---------- Discover chat (the "Ask" tab) ----------
 * The assistant interviews, then returns cards: books already in the library first (existing Read /
 * Listen actions), then books that are not (the existing request flow). The server does the grounding and
 * the kid filtering; this code only draws what it is given. User text always goes through e()/textContent. */
const CHAT = { conv: null, gen: 0, timer: null, tick: null, status: null, msgs: [] };
const CHAT_STARTERS = ['Something cozy and funny', 'A page-turning mystery', 'A big fantasy series', 'An audiobook for a long drive', 'Surprise me'];
const CHAT_STARTERS_KID = ['Funny animal stories', 'An exciting adventure', 'A mystery for kids', 'Something with dragons', 'Surprise me'];
function stopChatPolling() { CHAT.gen++; clearTimeout(CHAT.timer); clearInterval(CHAT.tick); CHAT.timer = CHAT.tick = null; }

function recCard(c, msgId, idx) {
  const lib = c.group === 'library';
  const cover = lib ? c.cover_url : c.cover_url;
  const img = cover ? `<img src="${e(cover)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-img-fallback="remove">` : '';
  const authors = e(c.author || '');
  let action = '';
  if (lib) {
    action = (c.actions || []).map(a => `<button class="pl-button${a.kind === 'ebook' ? '' : ' quiet'} small rec-open" data-go="library/${e(c.work_id)}/read/${e(a.asset_id)}">${a.kind === 'ebook' ? '▤' : '♫'} ${e(a.label)}${a.percent != null ? ` · ${Math.round(a.percent * 100)}%` : ''}</button>`).join('')
      + `<a class="pl-button ghost small" href="#library/${e(c.work_id)}">Details</a>`;
  } else if (c.request) {
    action = `<span class="pl-status ${e(c.request.tone || '')} rec-status">${e(c.request.friendly_status)}</span>`;
  } else {
    action = `<button class="pl-button small rec-request" data-i="${idx}">${c.unverified ? 'Ask a parent' : 'Request'}</button>`;
  }
  const thumbs = `<span class="rec-thumbs"><button class="pl-icon rec-fb${c.feedback === 'up' ? ' on' : ''}" data-i="${idx}" data-v="up" aria-pressed="${c.feedback === 'up'}" aria-label="More like this" title="More like this">+</button><button class="pl-icon rec-fb${c.feedback === 'down' ? ' on' : ''}" data-i="${idx}" data-v="down" aria-pressed="${c.feedback === 'down'}" aria-label="Not for me" title="Not for me">−</button></span>`;
  return `<div class="pl-rec" data-group="${lib ? 'library' : 'elsewhere'}"><div class="pl-cover" style="--hue:${hue(c.title)}"><div class="pl-fallback"><strong style="font-size:9px">${e(c.title)}</strong></div>${img}</div>
    <div class="pl-rec-main"><strong>${e(c.title)}</strong><span>${authors}${c.year ? ` · ${e(c.year)}` : ''}</span>${c.why ? `<p class="pl-rec-why">${e(c.why)}</p>` : ''}${c.unverified ? '<p class="pl-rec-why pl-muted">I could not check this one for your age, so a parent has to say yes.</p>' : ''}
    <div class="pl-rec-actions">${action}${thumbs}</div></div></div>`;
}

function chatMessageHtml(m) {
  if (m.role === 'user') return `<div class="pl-bubble me"><p>${e(m.text)}</p></div>`;
  if (m.status === 'pending') return `<div class="pl-bubble ai pending" data-id="${e(m.id)}"><p><span class="pl-dots" aria-hidden="true"><i></i><i></i><i></i></span> <span class="chat-think">Thinking… <span class="chat-secs">0</span>s</span></p><p class="pl-muted pl-small chat-slow hidden">This model can take a minute. You can wait or cancel.</p><button class="pl-button ghost small chat-cancel" data-id="${e(m.id)}">Cancel</button></div>`;
  if (m.status === 'failed' || m.status === 'cancelled') return `<div class="pl-bubble ai err"><p>${e(m.error_message || 'That did not work.')}</p>${m.status === 'failed' ? '<button class="pl-button ghost small chat-retry">Try again</button>' : ''}</div>`;
  const cards = m.cards || [];
  const lib = cards.map((c, i) => [c, i]).filter(([c]) => c.group === 'library');
  const ext = acquisitionOn() ? cards.map((c, i) => [c, i]).filter(([c]) => c.group !== 'library') : [];
  const prop = m.profile_proposal && m.profile_proposal_state === 'open'
    ? `<div class="pl-remember" data-id="${e(m.id)}"><span>${e(m.profile_question || 'Shall I remember that?')}</span><span class="pl-row" style="gap:6px"><button class="pl-button small chat-remember" data-v="1">Yes, remember</button><button class="pl-button ghost small chat-remember" data-v="0">No</button></span></div>` : '';
  return `<div class="pl-bubble ai" data-id="${e(m.id)}"><p>${e(m.text)}</p>${m.follow_up ? `<p class="pl-follow"><strong>${e(m.follow_up)}</strong></p>` : ''}
    ${lib.length ? `<h3 class="pl-rec-head">In your library</h3>${lib.map(([c, i]) => recCard(c, m.id, i)).join('')}` : ''}
    ${ext.length ? `<h3 class="pl-rec-head">Not in your library</h3>${ext.map(([c, i]) => recCard(c, m.id, i)).join('')}` : ''}${prop}</div>`;
}

function chatNoticeHtml(st) {
  const kid = st.account_type === 'kid';
  return `${e(st.notice)}${kid ? (st.parents_can_see ? ' <strong>Your parents can see what you ask here.</strong>' : ' Your chats stay private to you.') : ''}`;
}

async function renderDiscoverChat(pane) {
  pane.innerHTML = '<div class="pl-skeleton" style="height:120px"></div>';
  let st;
  try { st = await api('/api/player/v1/discover/status'); } catch (er) { pane.innerHTML = `<div class="pl-error">${e(er.message)}</div>`; return; }
  CHAT.status = st;
  const kid = st.account_type === 'kid';
  pane.innerHTML = `<p class="pl-notice" id="chat-notice">${chatNoticeHtml(st)}</p>
    <div class="pl-row" style="justify-content:space-between;margin:8px 0 10px"><button class="pl-button ghost small" id="chat-new">Start over</button><button class="pl-button quiet small" id="chat-profile">Show my taste profile</button></div>
    <div id="chat-thread" class="pl-thread" aria-live="polite" aria-label="Conversation"></div>
    <div id="chat-starters" class="pl-row" style="gap:8px;margin:10px 0">${(kid ? CHAT_STARTERS_KID : CHAT_STARTERS).map(t => `<button class="pl-chip chat-starter">${e(t)}</button>`).join('')}</div>
    <form id="chat-form" class="pl-composer"><textarea id="chat-q" rows="1" maxlength="${st.limits.message_chars}" placeholder="What are you in the mood for?" aria-label="What are you in the mood for?"></textarea><button class="pl-button" id="chat-send" type="submit">Ask</button></form>
    <p class="pl-muted pl-small" id="chat-left">${st.limits.remaining_today} messages left today.</p>`;
  stopChatPolling();
  CHAT.msgs = []; CHAT.conv = null; CHAT.fresh = false;
  // Wire the controls before the first await: the screen is usable (and clickable) as soon as it is drawn.
  $('#chat-new').onclick = async () => { stopChatPolling(); CHAT.fresh = true; CHAT.conv = null; CHAT.msgs = []; paintThread(); $('#chat-q').focus(); };
  $('#chat-profile').onclick = () => profileModal();
  $('#chat-form').onsubmit = ev => { ev.preventDefault(); sendChat($('#chat-q').value); };
  $('#chat-q').addEventListener('keydown', ev => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendChat($('#chat-q').value); } });
  $$('.chat-starter').forEach(b => b.onclick = () => sendChat(b.textContent));
  try {
    const list = await api('/api/player/v1/discover/chat/conversations');
    if (list.items.length) { const full = await api(`/api/player/v1/discover/chat/conversations/${list.items[0].id}`); if (!CHAT.fresh && !CHAT.conv && !CHAT.msgs.length) { CHAT.conv = full.id; CHAT.msgs = full.messages; } }
  } catch {}
  if (!pane.isConnected || !pane.querySelector('#chat-form')) return;   // the person left Discover while the chat loaded
  paintThread();
  const pending = CHAT.msgs.find(m => m.role === 'assistant' && m.status === 'pending');
  if (pending) pollMessage(pending.id);
}

function paintThread() {
  const el = $('#chat-thread'); if (!el) return;
  el.innerHTML = CHAT.msgs.length ? CHAT.msgs.map(chatMessageHtml).join('') : `<div class="pl-bubble ai"><p>Hi! Tell me what kind of book you are looking for and I will find some, in your library and beyond. I may ask a question or two first.</p></div>`;
  const starters = $('#chat-starters'); if (starters) starters.classList.toggle('hidden', CHAT.msgs.length > 0);
  const busy = CHAT.msgs.some(m => m.role === 'assistant' && m.status === 'pending');
  const send = $('#chat-send'); if (send) send.disabled = busy;
  wireThread(el);
  el.scrollTop = el.scrollHeight;
  const view = $('#pl-view'); if (view && CHAT.msgs.length) $('#chat-form')?.scrollIntoView({ block: 'end' });
}

function wireThread(el) {
  $$('.chat-cancel', el).forEach(b => b.onclick = async () => {
    try { await api(`/api/player/v1/discover/chat/conversations/${CHAT.conv}/messages/${b.dataset.id}/cancel`, { method: 'POST', body: {} }); } catch (er) { toast(er.message, 'bad'); }
    stopChatPolling(); await refreshChatMessage(b.dataset.id);
  });
  $$('.chat-retry', el).forEach(b => b.onclick = () => { const lastUser = [...CHAT.msgs].reverse().find(m => m.role === 'user'); if (lastUser) sendChat(lastUser.text); });
  $$('.chat-remember', el).forEach(b => b.onclick = async () => {
    const id = b.closest('.pl-remember').dataset.id;
    try { await api(`/api/player/v1/discover/chat/messages/${id}/profile`, { method: 'POST', body: { accept: b.dataset.v === '1' } }); toast(b.dataset.v === '1' ? 'Remembered. See it under Show my taste profile.' : 'Okay, not remembering that.'); await refreshChatMessage(id); } catch (er) { toast(er.message, 'bad'); }
  });
  $$('.rec-request', el).forEach(b => b.onclick = () => {
    const bubble = b.closest('.pl-bubble'); const m = CHAT.msgs.find(x => x.id === bubble.dataset.id); const c = m.cards[Number(b.dataset.i)];
    requestDialog({ id: c.metadata_id ? null : c.work_id, metadata_id: c.metadata_id, title: c.title, authors: c.author ? [c.author] : [], owned_kinds: [], kind_hint: c.kind_hint }, () => refreshChatMessage(m.id));
  });
  $$('.rec-fb', el).forEach(b => b.onclick = async () => {
    const bubble = b.closest('.pl-bubble'); const m = CHAT.msgs.find(x => x.id === bubble.dataset.id);
    try {
      const r = await api(`/api/player/v1/discover/chat/messages/${m.id}/feedback`, { method: 'POST', body: { index: Number(b.dataset.i), verdict: b.dataset.v } });
      m.cards[Number(b.dataset.i)].feedback = r.feedback;
      toast(r.feedback === 'up' ? 'Noted: more like this.' : r.feedback === 'down' ? 'Noted: not for you.' : 'Feedback removed.');
      const row = b.closest('.rec-thumbs'); $$('.rec-fb', row).forEach(x => { const on = r.feedback === x.dataset.v; x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on)); });
    } catch (er) { toast(er.message, 'bad'); }
  });
}

async function refreshChatMessage(id) {
  try {
    const m = await api(`/api/player/v1/discover/chat/conversations/${CHAT.conv}/messages/${id}`);
    const i = CHAT.msgs.findIndex(x => x.id === id); if (i >= 0) CHAT.msgs[i] = m;
    paintThread();
  } catch (er) { toast(er.message, 'bad'); }
}

async function sendChat(raw, retried = false) {
  const text = String(raw || '').replace(/\s+/g, ' ').trim();
  if (!text) return;
  if (CHAT.msgs.some(m => m.role === 'assistant' && m.status === 'pending')) { toast('Still working on your last message.', 'warn'); return; }
  const q = $('#chat-q');
  try {
    if (!CHAT.conv) { const c = await api('/api/player/v1/discover/chat/conversations', { method: 'POST', body: {} }); CHAT.conv = c.id; CHAT.msgs = []; }
    const r = await api(`/api/player/v1/discover/chat/conversations/${CHAT.conv}/messages`, { method: 'POST', body: { text } });
    if (q) q.value = '';
    CHAT.msgs.push({ id: r.user_message_id, role: 'user', text, status: 'done' });
    CHAT.msgs.push(r.message);
    paintThread();
    if (r.message.status === 'pending') pollMessage(r.message.id);
    refreshLeft();
  } catch (er) {
    // The account type changed since this chat began (an adult made a kid account, or the reverse): the
    // server will not continue the old chat. Begin a fresh one and send the same message there, once.
    if (er.status === 409 && /start a new chat/i.test(er.message) && !retried && CHAT.conv) {
      stopChatPolling(); CHAT.conv = null; CHAT.msgs = []; paintThread();
      // The account is now a different type: take its status first so a kid sees the "parents can see" notice
      // (and the right limits) before, not after, the message is sent again.
      await refreshLeft();
      return sendChat(text, true);
    }
    toast(er.message, 'bad');
  }
}

async function refreshLeft() {
  try {
    const st = await api('/api/player/v1/discover/status'); CHAT.status = st;
    const el = $('#chat-left'); if (el) el.textContent = `${st.limits.remaining_today} messages left today.`;
    const note = $('#chat-notice'); if (note) note.innerHTML = chatNoticeHtml(st);
  } catch {}
}

function pollMessage(id) {
  stopChatPolling();
  const gen = CHAT.gen; const started = Date.now();
  const timeoutS = (CHAT.status && CHAT.status.limits.timeout_seconds) || 120;
  CHAT.tick = setInterval(() => {
    const secs = Math.round((Date.now() - started) / 1000);
    const s = $('.chat-secs'); if (s) s.textContent = String(secs);
    if (secs >= 25) $('.chat-slow')?.classList.remove('hidden');
  }, 500);
  const step = async () => {
    if (gen !== CHAT.gen) return;
    try {
      const m = await api(`/api/player/v1/discover/chat/conversations/${CHAT.conv}/messages/${id}`);
      if (gen !== CHAT.gen) return;
      if (m.status !== 'pending') {
        clearInterval(CHAT.tick);
        const i = CHAT.msgs.findIndex(x => x.id === id); if (i >= 0) CHAT.msgs[i] = m;
        paintThread(); return;
      }
    } catch (er) { if (er.status === 401 || er.status === 403) return; }
    if ((Date.now() - started) / 1000 > timeoutS + 15) {
      clearInterval(CHAT.tick);
      const i = CHAT.msgs.findIndex(x => x.id === id); if (i >= 0) CHAT.msgs[i] = { ...CHAT.msgs[i], status: 'failed', error: 'timeout', error_message: 'That took longer than I am allowed to wait. Please try again, or use Search.' };
      paintThread(); return;
    }
    CHAT.timer = setTimeout(step, (Date.now() - started) < 20000 ? 1200 : 2500);
  };
  CHAT.timer = setTimeout(step, 600);
}

/* ---------- the taste profile (chat panel and Settings) ---------- */
const PROFILE_LABELS = { genres: 'Genres', themes: 'Themes', authors: 'Authors', dislikes: 'Avoid', notes: 'Notes' };
async function profileModal(onClose) {
  let data;
  try { data = await api('/api/player/v1/discover/profile'); } catch (er) { toast(er.message, 'bad'); return; }
  const kid = state.me?.account_type === 'kid';
  const paint = () => {
    const p = data.profile;
    const lists = Object.keys(PROFILE_LABELS).map(k => `<div class="pl-prof-group"><h3>${PROFILE_LABELS[k]}</h3><div class="pl-row" style="gap:6px">${p[k].map((v, i) => `<span class="pl-tag">${e(v)}<button class="pl-tag-x" data-k="${k}" data-i="${i}" aria-label="Remove ${e(v)}">×</button></span>`).join('') || '<span class="pl-muted pl-small">Nothing yet</span>'}</div>
      <div class="pl-row" style="gap:6px;margin-top:6px"><input class="pl-input prof-add" data-k="${k}" placeholder="Add" maxlength="${k === 'notes' ? 200 : 60}" aria-label="Add to ${PROFILE_LABELS[k]}" style="min-height:38px"><button class="pl-button quiet small prof-add-btn" data-k="${k}">Add</button></div></div>`).join('');
    const titles = (k, label) => `<div class="pl-prof-group"><h3>${label}</h3><div class="pl-row" style="gap:6px">${p[k].map((t, i) => `<span class="pl-tag">${e(t.title)}${t.author ? ' · ' + e(t.author) : ''}<button class="pl-tag-x" data-k="${k}" data-i="${i}" aria-label="Remove ${e(t.title)}">×</button></span>`).join('') || '<span class="pl-muted pl-small">Nothing yet</span>'}</div></div>`;
    const body = `${kid && CHAT.status?.parents_can_see !== false ? '<p class="pl-notice">Your parents can see this.</p>' : ''}<p class="pl-muted pl-small" style="margin-top:0">This is what the assistant remembers about your taste. Only you${kid ? ' and your parents' : ''} can see it. Edit or delete anything.</p>
      ${lists}${titles('liked_titles', 'More like these')}${titles('avoid_titles', 'Not for me')}
      <div class="pl-prof-group"><h3>Formats</h3><div class="pl-row">${['ebook', 'audiobook'].map(f => `<label class="pl-check"><input type="checkbox" class="prof-format" value="${f}" ${p.formats.includes(f) ? 'checked' : ''}> ${KIND_LABEL[f]}</label>`).join('')}</div></div>
      <div class="pl-prof-group pl-row"><label class="pl-small pl-muted">Length <select class="pl-select prof-length" style="min-height:38px">${[['', 'No preference'], ['short', 'Short'], ['medium', 'Medium'], ['long', 'Long'], ['any', 'Any']].map(([v, l]) => `<option value="${v}" ${p.length === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <label class="pl-small pl-muted">Reading level <input class="pl-input prof-level" value="${e(p.reading_level)}" maxlength="60" style="min-height:38px"></label></div>`;
    modal('Your reading profile', body, { footer: '<button class="pl-button danger" id="prof-clear">Clear everything</button><button class="pl-button" id="prof-done">Done</button>' });
    const save = async () => { try { data = await api('/api/player/v1/discover/profile', { method: 'PUT', body: data.profile }); } catch (er) { toast(er.message, 'bad'); } paint(); };
    $$('.pl-tag-x').forEach(b => b.onclick = () => { const k = b.dataset.k; data.profile[k].splice(Number(b.dataset.i), 1); save(); });
    const add = k => { const inp = $(`.prof-add[data-k="${k}"]`); const v = inp.value.trim(); if (v) { data.profile[k].push(v); save(); } };
    $$('.prof-add-btn').forEach(b => b.onclick = () => add(b.dataset.k));
    $$('.prof-add').forEach(i => i.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); add(i.dataset.k); } }));
    $$('.prof-format').forEach(c => c.onchange = () => { data.profile.formats = $$('.prof-format').filter(x => x.checked).map(x => x.value); save(); });
    $('.prof-length').onchange = ev => { data.profile.length = ev.target.value; save(); };
    $('.prof-level').onchange = ev => { data.profile.reading_level = ev.target.value; save(); };
    $('#prof-clear').onclick = async () => { if (!confirm('Forget everything the assistant remembers about your taste?')) return; try { data = await api('/api/player/v1/discover/profile', { method: 'DELETE' }); toast('Profile cleared'); } catch (er) { toast(er.message, 'bad'); } paint(); };
    $('#prof-done').onclick = () => { closeModal(); onClose && onClose(); };
  };
  paint();
}

async function profileSection() {
  const el = $('#pl-profile'); if (!el) return;
  try {
    const d = await api('/api/player/v1/discover/profile');
    const p = d.profile; const n = ['genres', 'themes', 'authors', 'dislikes', 'notes'].reduce((a, k) => a + p[k].length, 0) + p.liked_titles.length + p.avoid_titles.length;
    const kid = state.me?.account_type === 'kid';
    el.innerHTML = `<div class="pl-setting"><div><strong>Your reading profile</strong><span>${d.empty ? 'Nothing saved yet. The Discover assistant can offer to remember what you like.' : `${n} thing${n === 1 ? '' : 's'} remembered: ${e([...p.genres, ...p.authors].slice(0, 4).join(', ') || 'see the list')}.`}${kid ? ' Your parents can see this.' : ''}</span></div><div class="pl-row" style="gap:6px"><button class="pl-button quiet small" id="prof-open">View and edit</button></div></div>`;
    $('#prof-open').onclick = () => profileModal(profileSection);
  } catch { el.innerHTML = ''; }
}

/* ---------- a kid's chats, for the people who approve for them ---------- */
async function kidChatsSection(kidId) {
  const el = $('#ka-chats'); if (!el) return;
  try {
    const d = await api(`/api/player/v1/discover/kids/${kidId}/chats`);
    el.innerHTML = `<div class="pl-section-head"><h2>Discover chats (${d.items.length})</h2><button class="pl-button quiet small" id="ka-profile">Taste profile</button></div>
      <p class="pl-muted pl-small" style="margin-top:0">What this account asked the book assistant. They are told you can see this. ${d.totals.blocked || d.totals.dropped ? `Filtered so far: ${d.totals.blocked} blocked, ${d.totals.dropped} suggestions dropped.` : ''}</p>
      <div class="pl-card">${d.items.length ? d.items.map(c => `<div class="pl-request"><div><strong>${e(c.title)}</strong><span>${c.turns} message${c.turns === 1 ? '' : 's'} · ${e(new Date(c.updated_at * 1000).toLocaleDateString())}${c.blocked ? ` · ${c.blocked} blocked` : ''}${c.dropped ? ` · ${c.dropped} dropped` : ''}${c.deleted_by_owner ? ' · deleted by them' : ''}</span></div><div class="pl-row" style="gap:6px"><button class="pl-button quiet small ka-chat" data-id="${e(c.id)}">Read</button><button class="pl-button ghost small ka-chat-del" data-id="${e(c.id)}" aria-label="Delete this chat">×</button></div></div>`).join('') : '<div class="pl-card-body pl-muted pl-small">No chats yet.</div>'}</div>`;
    $$('.ka-chat').forEach(b => b.onclick = async () => {
      try {
        const c = await api(`/api/player/v1/discover/kids/${kidId}/chats/${b.dataset.id}`);
        modal(c.title, c.messages.map(m => m.role === 'user' ? `<div class="pl-bubble me"><p>${e(m.text)}</p></div>` : `<div class="pl-bubble ai"><p>${e(m.text)}</p>${(m.cards || []).map(x => `<div class="pl-request" style="padding:6px 0"><div><strong>${e(x.title)}</strong><span>${e(x.author || '')} · ${x.group === 'library' ? 'in the library' : 'not in the library'}${x.unverified ? ' · unverified' : ''}${x.request ? ' · ' + e(x.request.friendly_status) : ''}</span></div></div>`).join('')}${m.blocked ? '<p class="pl-muted pl-small">Redirected by the safety filter.</p>' : ''}${m.counts && (m.counts.dropped || m.counts.blocked) ? `<p class="pl-muted pl-small">${m.counts.blocked} blocked, ${m.counts.dropped} dropped.</p>` : ''}</div>`).join(''));
      } catch (er) { toast(er.message, 'bad'); }
    });
    $$('.ka-chat-del').forEach(b => b.onclick = async () => { if (!confirm('Delete this chat for everyone?')) return; try { await api(`/api/player/v1/discover/kids/${kidId}/chats/${b.dataset.id}`, { method: 'DELETE' }); kidChatsSection(kidId); } catch (er) { toast(er.message, 'bad'); } });
    $('#ka-profile').onclick = async () => {
      try {
        const p = (await api(`/api/player/v1/discover/kids/${kidId}/profile`)).profile;
        const rows = Object.keys(PROFILE_LABELS).filter(k => p[k].length).map(k => `<p><strong>${PROFILE_LABELS[k]}:</strong> ${e(p[k].join(', '))}</p>`).join('');
        modal('Taste profile', rows || '<p class="pl-muted">Nothing saved yet.</p>');
      } catch (er) { toast(er.message, 'bad'); }
    };
  } catch (er) {
    el.innerHTML = er.status === 403 ? '<p class="pl-muted pl-small">Discover chats are private to this account (turned off in Management).</p>' : '';
  }
}

async function renderApprovals(kidId) {
  const view = $('#pl-view');
  if (kidId) return renderKidAccess(kidId);
  const acq = acquisitionOn();
  const lead = acq ? 'Requests waiting on you, and the kids you look after.' : 'The kids you look after.';
  view.innerHTML = `<h1 class="pl-h1">Approvals</h1><p class="pl-sub">${lead}</p>${skeleton(2)}`;
  const [pending, kids] = await Promise.all([acq ? api('/api/player/v1/approvals?status=pending') : { total: 0, items: [] }, api('/api/player/v1/approvals/kids')]);
  state.pendingApprovals = pending.total; renderNav();
  view.innerHTML = `<h1 class="pl-h1">Approvals</h1><p class="pl-sub">${lead}</p>
    ${acq ? `<section class="pl-section"><div class="pl-section-head"><h2>Waiting (${pending.total})</h2></div><div class="pl-card" id="ap-list">${pending.items.length ? pending.items.map(r => `<div class="pl-request" data-id="${e(r.id)}"><div><strong>${e(r.title)}</strong><span>${e(r.author || '')}${r.author ? ' · ' : ''}${r.kinds.map(k => KIND_LABEL[k]).join(' + ')} · for <b>${e(r.requester_name || r.requester)}</b>${r.work_id ? ' · in the library' : ' · new to the library'}</span></div><div class="pl-row" style="gap:6px"><button class="pl-button small ap-approve" data-id="${e(r.id)}">Approve</button><button class="pl-button ghost small ap-deny" data-id="${e(r.id)}">Deny</button></div></div>`).join('') : '<div class="pl-card-body pl-muted pl-small">Nothing waiting.</div>'}</div></section>` : ''}
    <section class="pl-section"><div class="pl-section-head"><h2>Kids</h2></div><div class="pl-list">${kids.items.map(k => `<button class="pl-list-item" data-go="approvals/${e(k.id)}"><div class="pl-cover" style="--hue:${hue(k.username)};aspect-ratio:1;border-radius:50%"><div class="pl-fallback" style="justify-content:center;align-items:center;padding:0"><strong style="font-size:16px">${e((k.display_name || k.username).slice(0, 2).toUpperCase())}</strong></div></div><div><strong>${e(k.display_name || k.username)}</strong><span>${k.grants} book${k.grants === 1 ? '' : 's'} granted${acq && k.pending_requests ? ` · ${k.pending_requests} waiting` : ''}</span></div><span class="pl-muted">›</span></button>`).join('') || '<div class="pl-empty"><strong>No kid accounts</strong></div>'}</div></section>
    ${acq ? '<section class="pl-section"><div class="pl-section-head"><h2>Recently decided</h2></div><div class="pl-card" id="ap-done"><div class="pl-card-body pl-muted pl-small">Loading…</div></div></section>' : ''}`;
  const decide = async (id, verb) => {
    const note = verb === 'deny' ? (prompt('Reason (optional, shown to the requester)') || null) : null;
    try { await api(`/api/player/v1/approvals/${id}/${verb}`, { method: 'POST', body: { note } }); toast(verb === 'approve' ? 'Approved' : 'Denied'); renderApprovals(); } catch (er) { toast(er.message, 'bad'); }
  };
  $$('.ap-approve').forEach(b => b.onclick = () => decide(b.dataset.id, 'approve'));
  $$('.ap-deny').forEach(b => b.onclick = () => decide(b.dataset.id, 'deny'));
  if (acq) Promise.all(['approved', 'denied'].map(st => api(`/api/player/v1/approvals?status=${st}&limit=10`))).then(([a, d]) => {
    const rows = [...a.items, ...d.items].sort((x, y) => (y.decided_at || 0) - (x.decided_at || 0)).slice(0, 12);
    const el = $('#ap-done'); if (el) el.innerHTML = rows.length ? rows.map(r => requestRow(r)).join('') : '<div class="pl-card-body pl-muted pl-small">No decisions yet.</div>';
  }).catch(() => {});
}

async function renderKidAccess(kidId) {
  const view = $('#pl-view'); view.innerHTML = skeleton(2);
  const kids = await api('/api/player/v1/approvals/kids');
  const kid = kids.items.find(k => k.id === kidId);
  if (!kid) { view.innerHTML = '<div class="pl-error">You do not approve for this account.</div>'; return; }
  const grants = await api(`/api/player/v1/approvals/kids/${kidId}/grants`);
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="approvals">‹ Approvals</button></div><h1 class="pl-h1">${e(kid.display_name || kid.username)}</h1><p class="pl-sub">Books this account may read beyond the kids shelf.</p>
    <div class="pl-search"><span aria-hidden="true">⌕</span><input id="ka-q" placeholder="Find a library book to allow" autocomplete="off" aria-label="Find a book"></div><div id="ka-results" style="margin-top:12px"></div>
    <section class="pl-section"><div class="pl-section-head"><h2>Allowed books (${grants.items.length})</h2></div><div class="pl-card">${grants.items.length ? grants.items.map(g => `<div class="pl-request"><div><strong><a href="#library/${e(g.work_id)}">${e(g.title)}</a></strong><span>${g.note ? e(g.note) + ' · ' : ''}by ${e(g.granted_by || 'an administrator')}</span></div><button class="pl-button ghost small ka-revoke" data-work="${e(g.work_id)}">Remove</button></div>`).join('') : '<div class="pl-card-body pl-muted pl-small">Only the kids shelf so far.</div>'}</div></section>
    <section class="pl-section" id="ka-chats"></section>`;
  kidChatsSection(kidId);
  $$('.ka-revoke').forEach(b => b.onclick = async () => { try { await api(`/api/player/v1/approvals/kids/${kidId}/grants/${b.dataset.work}`, { method: 'DELETE' }); toast('Access removed'); renderKidAccess(kidId); } catch (er) { toast(er.message, 'bad'); } });
  let t;
  $('#ka-q').oninput = ev => { clearTimeout(t); const q = ev.target.value.trim(); t = setTimeout(async () => {
    const el = $('#ka-results'); if (!el) return; if (!q) { el.innerHTML = ''; return; }
    try {
      const d = await api(`/api/player/v1/approvals/kids/${kidId}/search?q=${encodeURIComponent(q)}`);
      el.innerHTML = d.items.length ? d.items.map(w => `<div class="pl-result">${coverHtml(w)}<div><strong>${e(w.title)}</strong><span>${e(authorsOf(w))}${w.visible_to_kid ? ' · already visible' : ''}</span></div>${w.granted ? `<button class="pl-button ghost small ka-revoke2" data-work="${e(w.id)}">Remove</button>` : w.visible_to_kid ? '<span class="pl-status good">On shelf</span>' : `<button class="pl-button small ka-grant" data-work="${e(w.id)}">Allow</button>`}</div>`).join('') : '<div class="pl-empty"><strong>No matches</strong></div>';
      $$('.ka-grant', el).forEach(b => b.onclick = async () => { try { await api(`/api/player/v1/approvals/kids/${kidId}/grants/${b.dataset.work}`, { method: 'PUT', body: {} }); toast('Allowed'); renderKidAccess(kidId); } catch (er) { toast(er.message, 'bad'); } });
      $$('.ka-revoke2', el).forEach(b => b.onclick = async () => { try { await api(`/api/player/v1/approvals/kids/${kidId}/grants/${b.dataset.work}`, { method: 'DELETE' }); toast('Access removed'); renderKidAccess(kidId); } catch (er) { toast(er.message, 'bad'); } });
    } catch (er) { el.innerHTML = `<div class="pl-error">${e(er.message)}</div>`; }
  }, 300); };
}


async function renderPair(code) {
  const view = $('#pl-view');
  view.innerHTML = `<div class="pl-row" style="margin-bottom:8px"><button class="pl-button ghost small" data-go="settings">‹ Settings</button></div><h1 class="pl-h1">Pair a device</h1><p class="pl-sub">Type the code shown on your reader or player. The device then signs in as <strong>${e(state.me.display_name || state.me.username)}</strong> with this account's access and nothing more; you can revoke it any time from Settings.</p>
    <form id="pair-form" class="pl-card pl-card-body" style="display:grid;gap:12px;max-width:440px"><div class="pl-field"><label for="pair-code">Code on the device</label><input id="pair-code" class="pl-input" name="user_code" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="XXXX-XXXX" maxlength="9" inputmode="text" value="${e(code || '')}" style="font-family:ui-monospace,monospace;letter-spacing:2px;text-transform:uppercase;font-size:20px" required></div><div class="pl-field"><label for="pair-name">Name this device (optional)</label><input id="pair-name" class="pl-input" name="name" placeholder="Kitchen e-reader"></div><div class="pl-row"><button class="pl-button">Approve device</button><button type="button" class="pl-button ghost" id="pair-deny">Deny</button></div></form>`;
  const form = $('#pair-form');
  form.onsubmit = async ev => { ev.preventDefault(); const f = new FormData(form); try { const r = await api('/api/player/v1/device/approve', { method: 'POST', body: { user_code: f.get('user_code'), name: f.get('name') || null } }); toast(`Approved: ${r.name}. The device will connect in a moment.`); location.hash = 'settings'; } catch (er) { toast(er.message, 'bad'); } };
  $('#pair-deny').onclick = async () => { try { await api('/api/player/v1/device/deny', { method: 'POST', body: { user_code: $('#pair-code').value } }); toast('Denied'); location.hash = 'settings'; } catch (er) { toast(er.message, 'bad'); } };
  // Accept the code however it is typed or pasted: drop dashes, spaces and case, and put the dash back after four characters.
  const pc = $('#pair-code');
  const fmt = v => { const c = (v || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8); return c.length > 4 ? c.slice(0, 4) + '-' + c.slice(4) : c; };
  pc.value = fmt(pc.value);
  pc.addEventListener('input', () => { pc.value = fmt(pc.value); });
  pc.addEventListener('paste', ev => { const t = (ev.clipboardData || window.clipboardData)?.getData('text'); if (t) { ev.preventDefault(); pc.value = fmt(t); } });
  pc.focus();
}

const ago = t => t ? new Date(t * 1000).toLocaleString() : 'never';
async function trustSection() {
  const el = $('#pl-trust'); if (!el) return;
  let d; try { d = await api('/api/player/v1/devices'); } catch (er) { el.innerHTML = `<div class="pl-setting pl-muted pl-small">${e(er.message)}</div>`; return; }
  const mine = d.items.filter(x => x.kind === 'browser'); const allowed = d.trust && d.trust.allowed;
  const here = d.current_device_id;
  const onSession = state.me.auth_method === 'session';
  const alerts = (d.alerts || []).map(a => `<div class="pl-setting"><div><strong>Possible stolen sign-in: ${e(a.name)}</strong><span>An old copy of this device's sign-in was used on ${e(ago(a.detected_at))}, so the device was signed out and revoked. Trust it again from a normal sign-in, and change your password if you did not expect this.</span></div></div>`).join('');
  el.innerHTML = alerts + `<div class="pl-setting" style="flex-direction:column;align-items:stretch;gap:10px"><div><strong>Trusted devices</strong><span>${allowed ? 'This device stays signed in until you revoke it. Use only on your own devices. Management still asks you to sign in.' : 'Trusting a device is turned off for this kind of account.'}</span></div>
    ${allowed && onSession && !here ? '<div><button class="pl-button small" id="trust-here">Trust this device</button></div>' : ''}</div>` +
    mine.map(x => `<div class="pl-setting"><div><strong>${e(x.name)}${x.id === here ? ' (this device)' : ''}</strong><span>${e(x.ua_summary || 'browser')} · added ${e(ago(x.created_at))} · last used ${e(ago(x.last_seen_at))}</span></div><div class="pl-row" style="gap:6px">${x.id === here ? '<button class="pl-button danger small" id="forget-here">Sign out and forget this device</button>' : `<button class="pl-button danger small trust-revoke" data-id="${e(x.id)}">Revoke</button>`}</div></div>`).join('');
  $('#trust-here')?.addEventListener('click', async () => { try { await trustThisDevice(); } catch (er) { toast(er.message, 'bad'); } });
  $('#forget-here')?.addEventListener('click', signOut);
  $$('.trust-revoke', el).forEach(b => b.onclick = async () => { try { await api(`/api/player/v1/devices/${b.dataset.id}`, { method: 'DELETE' }); toast('Device revoked'); trustSection(); } catch (er) { toast(er.message, 'bad'); } });
}

async function devicesSection() {
  const el = $('#pl-devices'); if (!el) return;
  try {
    const d = await api('/api/player/v1/devices');
    const paired = d.items.filter(x => x.kind !== 'browser');
    el.innerHTML = (paired.length ? paired.map(x => `<div class="pl-setting"><div><strong>${e(x.name)}</strong><span>${e(x.platform || 'device')}${x.client_version ? ' ' + e(x.client_version) : ''} · ${e(x.token_prefix || '')}… · last seen ${x.last_seen_at ? new Date(x.last_seen_at * 1000).toLocaleString() : 'never'}</span></div><div class="pl-row" style="gap:6px"><button class="pl-button ghost small dev-rename" data-id="${e(x.id)}" data-name="${e(x.name)}">Rename</button><button class="pl-button danger small dev-revoke" data-id="${e(x.id)}">Revoke</button></div></div>`).join('') : '<div class="pl-setting"><div><strong>No devices paired</strong><span>Readers and players you approve appear here.</span></div></div>') +
      `<div class="pl-setting"><div><strong>Pair a new device</strong><span>Enter the code a device shows you.</span></div><a class="pl-button quiet small" href="#pair">Pair</a></div>`;
    $$('.dev-revoke', el).forEach(b => b.onclick = () => { modal('Revoke device', '<p>The device signs out immediately and must be paired again to read or listen.</p>', { footer: '<button class="pl-button ghost" id="dv-keep">Keep</button><button class="pl-button danger" id="dv-go">Revoke</button>' }); $('#dv-keep').onclick = closeModal; $('#dv-go').onclick = async () => { try { await api(`/api/player/v1/devices/${b.dataset.id}`, { method: 'DELETE' }); closeModal(); toast('Device revoked'); devicesSection(); } catch (er) { toast(er.message, 'bad'); } }; });
    $$('.dev-rename', el).forEach(b => b.onclick = async () => { const name = prompt('Device name', b.dataset.name); if (!name) return; try { await api(`/api/player/v1/devices/${b.dataset.id}`, { method: 'PATCH', body: { name } }); devicesSection(); } catch (er) { toast(er.message, 'bad'); } });
  } catch (er) { el.innerHTML = `<div class="pl-setting pl-muted pl-small">${e(er.message)}</div>`; }
}

async function renderSettings() {
  const me = state.me; const theme = document.documentElement.dataset.theme;
  $('#pl-view').innerHTML = `<h1 class="pl-h1">Settings</h1><div class="pl-settings">
    <div class="pl-card"><div class="pl-setting"><div><strong>${e(me.display_name || me.username)}</strong><span>${e(me.email || '')}${me.email ? ' · ' : ''}${e(me.account_type)} account</span></div>${me.management ? '<a class="pl-button quiet small" href="/manage">Management</a>' : ''}</div>
      <div class="pl-setting"><div><strong>Appearance</strong><span>Applies to the app; the reader has its own.</span></div><div class="pl-segment" id="theme-seg">${[['dark', 'Dark'], ['light', 'Light'], ['sepia', 'Sepia']].map(([v, l]) => `<button data-theme="${v}" class="${theme === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>
    </div>
    <div class="pl-card" id="pl-profile"></div>
    <div class="pl-card" id="pl-build"></div>
    <div class="pl-card"><div class="pl-setting"><div><strong>About ${e(PRODUCT)}</strong><span>${e(TAGLINE)}</span></div><a class="pl-button quiet small" href="#about">About</a></div></div>
    <div class="pl-card" id="pl-kindle"><div class="pl-setting pl-muted pl-small">Loading Kindle settings…</div></div>
    <div class="pl-card" id="pl-trust"><div class="pl-setting pl-muted pl-small">Loading…</div></div>
    <div class="pl-card" id="pl-devices"><div class="pl-setting pl-muted pl-small">Loading devices…</div></div>
    <div class="pl-card"><div class="pl-setting"><div><strong>Sign out</strong><span>Also clears books kept offline on this device.</span></div><button class="pl-button danger small" id="signout">Sign out</button></div>
      ${me.auth_method === 'session' ? '<div class="pl-setting"><div><strong>Sign out everywhere</strong><span>Ends every browser sign-in. Trusted devices stay trusted unless you tick the box.</span><label class="pl-check"><input type="checkbox" id="so-all-dev"><span>Also revoke my trusted devices</span></label></div><button class="pl-button danger small" id="signout-all">Sign out everywhere</button></div>' : ''}</div>
  </div>`;
  $$('#theme-seg button').forEach(b => b.onclick = () => setTheme(b.dataset.theme));
  $('#signout').onclick = signOut;
  $('#signout-all')?.addEventListener('click', async () => { try { await api('/api/v1/auth/logout-all', { method: 'POST', body: { revoke_trusted_devices: $('#so-all-dev').checked } }); } catch (er) { return toast(er.message, 'bad'); } try { if (window.bookdoePwa) await window.bookdoePwa.clearUser(); } catch {} location.reload(); });
  paintBuild();
  trustSection();
  devicesSection();
  kindleSection();
  profileSection();
}

async function renderAbout() {
  $('#pl-view').innerHTML = `<h1 class="pl-h1">About</h1><div class="pl-card"><div class="pl-setting" style="flex-direction:column;align-items:flex-start;gap:6px">${brandLogo('pl-about-logo', 'pl-logo')}<strong id="about-name" style="font-size:20px">${e(PRODUCT)}</strong><span id="about-tagline">${e(TAGLINE)}</span><span class="pl-muted pl-small" id="about-build">Build ${e((window.__ALX_BUILD__ || {}).sha || 'dev')}</span></div></div><p><a href="#settings">Back to Settings</a></p>`;
}

async function kindleSection() {
  const el = $('#pl-kindle'); if (!el) return;
  const info = await kindleInfo(true);
  if (!el.isConnected) return;   // the person left Settings while this loaded; the form it would wire is gone
  const note = info.configured
    ? `Books are emailed from <strong>${e(info.sender_address)}</strong>. Add that address to your Kindle's approved senders (Amazon → Manage Your Content and Devices → Preferences → Personal Document Settings) or deliveries are silently dropped.`
    : 'Send-to-Kindle is not set up on this server yet; an administrator can add it under Management → Settings → Devices. Your address is kept for when it is.';
  el.innerHTML = `<div class="pl-setting" style="flex-direction:column;align-items:stretch;gap:10px"><div><strong>Send to Kindle</strong><span>${note}</span></div>
    <form id="kindle-form" class="pl-row" style="align-items:stretch"><input class="pl-input" id="kindle-email" type="email" name="kindle_email" placeholder="name@kindle.com" value="${e(info.kindle_email || '')}" autocomplete="off" style="flex:1;min-width:200px" aria-label="Kindle email"><button class="pl-button small">Save</button>${info.kindle_email ? '<button type="button" class="pl-button ghost small" id="kindle-clear">Remove</button>' : ''}</form></div>`;
  $('#kindle-form').onsubmit = async ev => { ev.preventDefault(); const v = $('#kindle-email').value.trim(); try { await api('/api/player/v1/kindle', { method: 'PUT', body: { kindle_email: v || null } }); toast(v ? 'Kindle email saved' : 'Kindle email removed'); await kindleInfo(true); kindleSection(); } catch (er) { toast(er.message, 'bad'); } };
  $('#kindle-clear')?.addEventListener('click', async () => { try { await api('/api/player/v1/kindle', { method: 'PUT', body: { kindle_email: null } }); toast('Kindle email removed'); await kindleInfo(true); kindleSection(); } catch (er) { toast(er.message, 'bad'); } });
}

function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.setItem('alx-pl-theme', t); $$('#theme-seg button').forEach(b => b.classList.toggle('on', b.dataset.theme === t)); }
document.documentElement.dataset.theme = localStorage.getItem('alx-pl-theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');

$('#pl-theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('#pl-account').onclick = () => { location.hash = 'settings'; };
function paintOnline() { state.online = navigator.onLine; $('#pl-offline').classList.toggle('hidden', state.online); }
async function replayOffline() {
  if (!navigator.onLine || !state.me) return;
  try { const mod = await import('/assets/consume.js'); const r = await mod.replayPendingProgress(api); if (r.replayed) toast(`Synced ${r.replayed} saved position${r.replayed === 1 ? '' : 's'}${r.stale ? ` (${r.stale} newer elsewhere)` : ''}`); } catch {}
}
window.addEventListener('online', () => { paintOnline(); replayOffline(); }); window.addEventListener('offline', paintOnline); paintOnline();
// A new service worker took over (a deploy): reload when not inside a book so the
// bundle never lags the backend; otherwise reload on the next navigation.
let revokedToastAt = 0;
if (window.bookdoePwa && window.bookdoePwa.onRevoked) window.bookdoePwa.onRevoked(() => {
  if (Date.now() - revokedToastAt < 60000) return;
  revokedToastAt = Date.now();
  toast('A book saved on this device is not on your shelf right now, so its offline copy was removed. You can download it again if it comes back.', 'good', 8000);
});
/* ---------- build / update ----------
 * state.build = {client, server, mismatch, waiting} from bookdoePwa.watchVersion (on start,
 * on focus/visibility at most every 5 minutes, and when a new worker finishes installing).
 * An update is offered, never forced: the tap applies it (skipWaiting + reload), and not
 * while a book is open or audio is playing; then it waits for the reader to close or pause. */
const shortSha = v => String(v || '?').slice(0, 12);
function updatePending() { const b = state.build; return !!(b && (b.mismatch || b.waiting)); }
function paintBuild() {
  const b = state.build; if (!b) return;
  const equal = !updatePending() && b.server && b.server.sha === b.client.sha;
  const bar = $('#pl-update');
  if (bar) {
    bar.classList.toggle('hidden', !updatePending());
    bar.innerHTML = updatePending() ? `<span>Update available: ${e(shortSha(b.client.sha))} -&gt; ${e(shortSha(b.server?.sha))}</span><button class="pl-button small" id="pl-update-bar-now">Update now</button>` : '';
    $('#pl-update-bar-now')?.addEventListener('click', applyAppUpdate);
  }
  const card = $('#pl-build'); if (!card) return;
  card.innerHTML = `<div class="pl-setting"><div><strong>App version</strong><span id="pl-build-version">${e(shortSha(b.client.sha))} (server ${e(b.server ? shortSha(b.server.sha) : 'unknown')})</span></div>${equal ? '<span class="pl-build-ok" title="Up to date" aria-label="Up to date">&#10003;</span>' : ''}</div>` +
    (updatePending() ? `<div class="pl-setting" id="pl-update-row"><div><strong>Update available</strong><span>${e(shortSha(b.client.sha))} -&gt; ${e(shortSha(b.server?.sha))}</span></div><button class="pl-button small" id="pl-update-now">Update now</button></div>` : '');
  $('#pl-update-now')?.addEventListener('click', applyAppUpdate);
}
function playerBusy() {
  const a = document.getElementById('alx-audio');
  return currentHash().includes('/read/') || !!(a && !a.paused && !a.ended);
}
async function applyAppUpdate() {
  if (playerBusy()) {
    state.reloadWhenLeaving = true;
    const a = document.getElementById('alx-audio');
    if (a && !a.paused) a.addEventListener('pause', () => { if (!playerBusy()) { state.reloadWhenLeaving = false; applyAppUpdate(); } }, { once: true });
    toast('The update applies when you close the book or pause the audio.', 'good', 5000);
    return;
  }
  if (window.bookdoePwa) await window.bookdoePwa.applyUpdate(); else location.reload();
}
if (window.bookdoePwa) {
  const pwa = window.bookdoePwa;
  state.build = { client: pwa.build, server: null, mismatch: false, waiting: false };
  pwa.watchVersion(v => { state.build = v; paintBuild(); });
}

initAuth();
