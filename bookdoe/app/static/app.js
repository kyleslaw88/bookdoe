const TAGLINE = document.querySelector('meta[name="alx-product"]')?.dataset.tagline || 'Every Story. One Library.';
const PRODUCT = document.querySelector('meta[name="alx-product"]')?.content || 'bookdoe';  // the instance's product name (config app_name), stamped by the server
const state = { me: null, csrf: null, route: 'dashboard', settingsTab: 'roots', dashboard: null };
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const e = (v='') => String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmtNum = v => new Intl.NumberFormat().format(Number(v||0));
const fmtDate = v => v ? new Date(Number(v) > 2e9 ? Number(v) : Number(v)*1000).toLocaleString() : '—';
const fmtBytes = n => { n=Number(n||0); if(!n)return '—'; const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++} return `${n.toFixed(i>1?1:0)} ${u[i]}`; };
const escAttr = e;

const brandLogo = (hCls, mCls) => `<img class="brand-h on-light ${hCls}" src="/assets/brand/bookdoe-horizontal-light.svg" alt="${e(PRODUCT)}" /><img class="brand-h on-dark ${hCls}" src="/assets/brand/bookdoe-horizontal-dark.svg" alt="${e(PRODUCT)}" /><img class="brand-mark-img ${mCls}" src="/assets/brand/bookdoe-mark.svg" alt="" width="44" height="44" />`;
async function api(path, opts={}) {
  const headers = {'Accept':'application/json', ...(opts.headers||{})};
  if (opts.body && typeof opts.body !== 'string') { headers['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
  if (!['GET','HEAD'].includes((opts.method||'GET').toUpperCase()) && state.csrf) headers['X-CSRF-Token']=state.csrf;
  const r = await fetch(path, {...opts, headers});
  let data=null; const ct=r.headers.get('content-type')||'';
  if(ct.includes('json')) data=await r.json(); else data=await r.text();
  if(!r.ok) {
    if(r.status===401 && !path.includes('/auth/')) { state.me=null; await initAuth(); }
    const msg = data?.detail || data?.message || (typeof data==='string'?data:`Request failed (${r.status})`);
    // Keep the status and parsed body on the error. Callers sometimes need the
    // payload of a non-2xx response - /api/v1/auth/me answers 401 when nobody is
    // signed in, and its body is what tells us which auth mode to render.
    const err = new Error(msg);
    err.status = r.status; err.retryAfter = Number(r.headers.get('Retry-After')) || 0;
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(message, kind='good', ms=4200) {
  const stack=$('#toast-stack'); const el=document.createElement('div'); el.className=`toast ${kind}`;
  el.innerHTML=`<span>${kind==='bad'?'⚠':kind==='warn'?'!':'●'}</span><div>${e(message)}</div>`; stack.appendChild(el);
  setTimeout(()=>el.remove(),ms);
}

function modal(title, body, {wide=false, footer=''}={}) {
  const root=$('#modal-root');
  root.innerHTML=`<div class="modal-backdrop"><div class="modal ${wide?'wide':''}"><div class="modal-head"><h2>${e(title)}</h2><button class="icon-button modal-close">×</button></div><div class="modal-body">${body}</div>${footer?`<div class="modal-foot">${footer}</div>`:''}</div></div>`;
  $('.modal-close',root).onclick=()=>root.innerHTML='';
  $('.modal-backdrop',root).addEventListener('click', ev=>{ if(ev.target.classList.contains('modal-backdrop')) root.innerHTML=''; });
  return root;
}

function setLoading(title='Loading') { $('#view').innerHTML=`<div class="page-head"><div><h1>${e(title)}</h1><p>Gathering the latest state…</p></div></div><div class="panel"><div class="panel-body"><div class="skeleton" style="width:42%;margin-bottom:12px"></div><div class="skeleton" style="width:82%;margin-bottom:12px"></div><div class="skeleton" style="width:66%"></div></div></div>`; }

// <acquisition-gate>
// The server's acquisition flag, read from /api/v1/auth/me (acquisition_enabled). Off means
// the Management UI shows no trace of acquiring: no Wanted/Queue/Requests/Calendar pages, no
// indexer or download-client settings, no Search/Monitor controls, no wanted counts.
const ACQ_ROUTES = new Set(['wanted', 'queue', 'requests', 'calendar']);
const ACQ_SETTINGS_TABS = new Set(['indexers', 'clients']);
const ACQ_JOB_KINDS = new Set(['download-poll', 'wanted-search', 'upgrade-search', 'author-discovery']);
function jobsFor(items, on){ return on ? items : (items||[]).filter(j => !ACQ_JOB_KINDS.has(j.kind)); }
function acquisitionOn(me){ return !!(me && me.acquisition_enabled); }
function routeVisible(route, on){ return on || !ACQ_ROUTES.has(route); }
function settingsTabsFor(tabs, on){ return on ? tabs : tabs.filter(([id]) => !ACQ_SETTINGS_TABS.has(id)); }
// Search/Monitor on one asset row (full-access accounts only).
function assetAcqButtonsHtml(a, on){
  return on ? `<button class="button secondary compact asset-search" data-kind="${a.kind}">Search</button> <button class="button ghost compact asset-monitor" data-asset="${a.id}" data-next="${a.monitored?0:1}">${a.monitored?'Unmonitor':'Monitor'}</button>` : '';
}
function monitoredBadgeHtml(w, on){ return on ? (w.monitored ? '<span class="badge info">Monitored</span>' : '<span class="badge">Unmonitored</span>') : ''; }
function wantedLabel(a, on){ return on && a.status!=='owned' && a.monitored && !a.derived_from_asset_id; }
function dashboardAcqParts(on, c, d, stat, queueMini){
  if(!on) return {actions:'', stats:'', queue:''};
  return {
    actions: '<button class="button secondary" id="poll-now">Poll downloads</button>',
    stats: `${stat('Wanted audio',c.wanted_audiobooks,'explicitly wanted')}${stat('Wanted ebooks',c.wanted_ebooks,'explicitly wanted')}`,
    queue: `<div class="panel"><div class="panel-head"><h2>Active queue</h2><button class="button ghost compact" data-route="queue">Open queue</button></div><div class="panel-body">${queueMini(d.queue)}</div></div>`,
  };
}
// </acquisition-gate>

const nav = [
  ['LIBRARY', null, null],
  ['dashboard','Overview','⌂'], ['library','Library','▦'], ['authors','Authors','♙'], ['series','Series','❧'], ['duplicates','Duplicates','⧉'], ['discover','Discover','⌕'], ['wanted','Wanted','◎'], ['calendar','Calendar','□'],
  ['ACTIVITY', null, null],
  ['queue','Queue','⇣'], ['requests','Requests','✓'], ['inbox','Inbox','◇'], ['history','History','↺'],
  ['SYSTEM', null, null],
  ['settings','Settings','⚙']
];

// A kids-scope account gets a deliberately small surface: its own shelf, the
// authors on it, and series it belongs to. Everything acquisition/settings-shaped
// is not merely hidden here - the server 403s all of it independently (see
// auth.require_full_access); this list keeps a kid from ever being shown a link to
// a page that would just error.
const KIDS_ROUTES = new Set(['library', 'authors', 'narrators', 'series', 'settings']);

function isKidsScope(){ return state.me?.access_scope === 'kids'; }

function renderNav() {
  const n=$('#nav'); n.innerHTML='';
  const kids = isKidsScope();
  // Group into sections first so an entirely-hidden section (e.g. every route under
  // ACTIVITY on a kids account) does not leave a bare, link-less heading behind.
  const sections = [];
  for(const item of nav){
    if(item[1]===null) sections.push({label:item[0], routes:[]});
    else sections[sections.length-1].routes.push(item);
  }
  for(const section of sections){
    const admin = !!state.me?.is_admin; const acqOn = acquisitionOn(state.me);
    const visible = (kids ? section.routes.filter(([route])=>KIDS_ROUTES.has(route)) : section.routes).filter(([route])=>route!=='duplicates'||admin).filter(([route])=>routeVisible(route,acqOn));
    if(!visible.length) continue;
    n.insertAdjacentHTML('beforeend',`<div class="nav-section">${section.label}</div>`);
    for(const [route,label,icon] of visible){
      n.insertAdjacentHTML('beforeend',`<a href="#${route}" data-route="${route}" class="${state.route===route?'active':''}"><span class="nav-icon">${icon}</span><span>${label}</span><span class="nav-badge hidden" id="badge-${route}"></span></a>`);
    }
  }
  if(state.dashboard){
    const map={inbox:state.dashboard.counts.review}; if(acquisitionOn(state.me)){ map.wanted=(state.dashboard.counts.wanted_ebooks||0)+(state.dashboard.counts.wanted_audiobooks||0); map.queue=state.dashboard.counts.active_downloads; }
    for(const [k,v] of Object.entries(map)){ const b=$(`#badge-${k}`); if(b&&v){b.textContent=v;b.classList.remove('hidden');} }
  }
}

async function initAuth(){
  let me;
  try {
    me=await api('/api/v1/auth/me');
  } catch(err) {
    // A 401 here is the normal "not signed in" answer and its body carries
    // auth_mode. Defaulting to 'local' would hide the OIDC sign-in button and
    // show a username/password form that the server refuses.
    me = (err && err.payload && typeof err.payload === 'object' && err.payload.auth_mode)
      ? err.payload
      : {authenticated:false, auth_mode:'local', setup_required:false};
  }
  // A trusted browser (device cookie) is Player-only, even for an administrator:
  // Management treats it as signed out and asks for the normal sign-in.
  if(me.authenticated && me.user?.auth_method==='device' && me.user?.is_admin){
    me={authenticated:false, auth_mode:me.auth_mode, setup_required:false, remember_days:me.signin?.remember_days||0, device_notice:true};
  }
  if(me.authenticated && !me.user?.management){
    // This shell is Management. The server already refuses the shell and every
    // management API to a non-admin; a non-admin who signed in here (local form) is
    // simply sent to the Player rather than shown a page of 403s.
    location.replace('/app'); return;
  }
  if(me.authenticated){
    state.me={...me.user, acquisition_enabled:!!me.acquisition_enabled}; state.csrf=me.csrf;
    // Tell the service worker whose offline content cache to use (pwa.js/sw.js). Not
    // awaited: offline bookkeeping must never delay sign-in.
    if(window.bookdoePwa && me.user?.id) window.bookdoePwa.setUser(me.user.id).catch(()=>{});
    $('#auth-shell').classList.add('hidden'); $('#app-shell').classList.remove('hidden');
    $('#user-button').textContent=(me.user.display_name||me.user.username||'U').slice(0,2).toUpperCase();
    // A kids account has no use for the dashboard's acquisition-shaped widgets - land
    // it straight on the shelf instead. currentHash() always defaults to 'dashboard',
    // so the raw hash (empty on first load, or literally '#dashboard') is what needs
    // checking here.
    if(isKidsScope() && (!location.hash || location.hash==='#dashboard')) location.hash='library';
    // The topbar's "Add books" is a persistent, always-in-the-chrome control, not
    // something route()'s per-page gating touches - it needs its own check here.
    if(isKidsScope()) $('#add-books-topbar').classList.add('hidden');
    renderNav(); await route(); if(!isKidsScope()) refreshHealth();
    loadBuild();
    return;
  }
  state.me=null; state.csrf=null; $('#app-shell').classList.add('hidden'); $('#auth-shell').classList.remove('hidden'); renderAuth(me);
}

const rememberBox=days=>days>0?`<label class="check-line" style="display:flex;gap:8px;align-items:center;margin:2px 0 12px"><input type="checkbox" name="remember" value="1"><span>Keep me signed in for ${days} days</span></label>`:'';
const loginBody=form=>{const o=Object.fromEntries(new FormData(form));o.remember=!!o.remember;return o;};
function renderAuth(info){
  const el=$('#auth-shell'); const rd=Number(info.remember_days)||0;
  const note=info.device_notice?'<p class="help">Management still asks you to sign in, even on a trusted device.</p>':'';
  if(info.setup_required){
    el.innerHTML=`<form class="auth-card" id="setup-form">${brandLogo('auth-logo-img', 'auth-logo-img')}<h1>Build your library</h1><p>Create the first administrator. ${e(PRODUCT)} will keep existing media in place unless you explicitly choose to reorganize it.</p><div class="field"><label>Username</label><input class="input" name="username" value="admin" required></div><div class="field"><label>Password</label><input class="input" name="password" type="password" minlength="12" required><div class="help">At least 12 characters.</div></div><div class="field"><label>Email (optional)</label><input class="input" name="email" type="email"></div><button class="button">Create administrator</button></form>`;
    $('#setup-form').onsubmit=async ev=>{ev.preventDefault();const f=new FormData(ev.target);try{const x=await api('/api/v1/auth/setup',{method:'POST',body:Object.fromEntries(f)});state.csrf=x.csrf;await initAuth();}catch(er){toast(er.message,'bad')}};
  } else if(info.auth_mode==='oidc') {
    // OIDC is the household admin's SSO path; a kids account has no Authentik
    // identity of its own, so a plain username/password stays available as a
    // secondary path the server deliberately still accepts in this mode.
    el.innerHTML=`<div class="auth-card">${brandLogo('auth-logo-img', 'auth-logo-img')}<h1>${e(PRODUCT)} Management</h1><p>Sign in through your configured identity provider.</p>${note}${rememberBox(rd).replace('name="remember"','id="oidc-remember" name="oidc-remember"')}<a class="button" id="oidc-link" style="display:block;text-align:center" href="/auth/login?next=%2Fmanage">Sign in with Authentik / OIDC</a>
      <button type="button" class="button ghost" id="family-toggle" style="display:block;width:100%;margin-top:14px">Sign in with a family account</button>
      <form id="family-login-form" class="hidden" style="margin-top:14px"><div class="field"><label>Username</label><input class="input" name="username" autocomplete="username" required></div><div class="field"><label>Password</label><input class="input" name="password" type="password" autocomplete="current-password" required></div>${rememberBox(rd)}<button class="button">Sign in</button></form></div>`;
    const rb=$('#oidc-remember'); if(rb) rb.onchange=()=>{ $('#oidc-link').href='/auth/login?next=%2Fmanage'+(rb.checked?'&remember=1':''); };
    $('#family-toggle').onclick=()=>{ $('#family-toggle').classList.add('hidden'); $('#family-login-form').classList.remove('hidden'); $('#family-login-form input[name=username]').focus(); };
    $('#family-login-form').onsubmit=async ev=>{ev.preventDefault();try{const x=await api('/api/v1/auth/login',{method:'POST',body:loginBody(ev.target)});state.csrf=x.csrf;await initAuth();}catch(er){toast(er.message,'bad')}};
  } else if(info.auth_mode==='proxy') {
    el.innerHTML=`<div class="auth-card">${brandLogo('auth-logo-img', 'auth-logo-img')}<h1>Authentication required</h1><p>The reverse proxy did not provide a trusted user header. Verify the Authentik proxy-provider configuration.</p></div>`;
  } else {
    el.innerHTML=`<form class="auth-card" id="login-form">${brandLogo('auth-logo-img', 'auth-logo-img')}<h1>Welcome back</h1><p>Sign in to manage your unified ebook and audiobook library.</p><div class="field"><label>Username</label><input class="input" name="username" autocomplete="username" required autofocus></div><div class="field"><label>Password</label><input class="input" name="password" type="password" autocomplete="current-password" required></div>${rememberBox(rd)}<button class="button">Sign in</button></form>`;
    $('#login-form').onsubmit=async ev=>{ev.preventDefault();try{const x=await api('/api/v1/auth/login',{method:'POST',body:loginBody(ev.target)});state.csrf=x.csrf;await initAuth();}catch(er){toast(er.message,'bad')}};
  }
}

function currentHash(){ const h=location.hash.slice(1)||'dashboard'; return h; }
async function route(){
  if(!state.me)return;
  closeNav();
  const {path:raw,filters:hashFilters}=parseLibraryHash(currentHash()); const [r,id,sub,tab]=raw.split('/'); state.route=r; renderNav();
  // Library filters live only while you are in the Library. Filters carried by the link (#library?tag=..) apply once.
  if(r!=='wanted') wantedState.notFound=false;
  if(r!=='library') resetLibraryFilters();
  else if(!id && hashFilters){ resetLibraryFilters(); Object.assign(libraryState,hashFilters); }
  // Defense in depth, not the boundary itself: every one of these routes' endpoints
  // already 403s a kids-scope account server-side (see auth.require_full_access).
  // This just avoids letting a kid land on a page that can only ever show an error.
  if(isKidsScope() && !KIDS_ROUTES.has(r)){ location.hash='library'; return; }
  // Acquisition off: those pages do not exist (the server 404s their API too).
  if(!routeVisible(r,acquisitionOn(state.me))){ location.hash='dashboard'; return; }
  // The reader/player pages hold a renderer or a playing <audio>; leaving them must
  // flush the last position and stop playback, whatever page comes next.
  if(state.consumeCleanup){ try{ state.consumeCleanup(); }catch{} state.consumeCleanup=null; }
  try {
    if(r==='dashboard') return await renderDashboard();
    if(r==='library' && id && sub==='read' && tab) return await renderConsume(id,tab);
    if(r==='library' && id && sub==='studio') return await (isKidsScope() ? renderWork(id) : renderStudio(id,tab));
    if(r==='library' && id) return await renderWork(id);
    if(r==='library') return await renderLibrary();
    if(r==='authors' && id) return await renderAuthor(id);
    if(r==='authors') return await renderAuthors();
    if(r==='narrators' && id) return await renderNarrator(id);
    if(r==='podcasts' && id && sub==='play' && tab) return await renderPodcastPlayer(id,tab);
    if(r==='podcasts' && id) return await renderPodcastShow(id);
    if(r==='series' && id) return await renderSeriesDetail(id);
    if(r==='series') return await renderSeries();
    if(r==='duplicates') return await renderDuplicates();
    if(r==='discover') return await renderDiscover();
    if(r==='wanted') return await renderWanted();
    if(r==='calendar') return await renderCalendar();
    if(r==='queue') return await renderQueue();
    if(r==='requests') return await renderRequests();
    if(r==='inbox') return await renderInbox();
    if(r==='review') return await renderInbox();
    if(r==='history') return await renderHistory();
    if(r==='failures') return await renderFailures();
    if(r==='settings') return await renderSettings();
    location.hash= isKidsScope() ? 'library' : 'dashboard';
  } catch(err){ $('#view').innerHTML=`<div class="page-head"><div><h1>Something went wrong</h1><p>${e(err.message)}</p></div></div><div class="warning-box"><strong>Request failed.</strong> The application state has not been changed.</div>`; }
}
window.addEventListener('hashchange',route);

async function renderDashboard(){
  setLoading('Overview'); const d=await api('/api/v1/dashboard'); state.dashboard=d; renderNav();
  const c=d.counts; const f=d.failures||{failed_imports:0,failed_jobs:0,total:0};
  const attention = f.total
    ? `<div class="attention-card"><div><strong>${fmtNum(f.total)} thing${f.total===1?' needs':'s need'} attention</strong><div class="muted">${fmtNum(f.failed_imports)} failed import${f.failed_imports===1?'':'s'} · ${fmtNum(f.failed_jobs)} failed job${f.failed_jobs===1?'':'s'}</div></div><button class="button" data-route="failures">Review</button></div>`
    : `<div class="attention-card calm"><div><strong>Nothing needs attention</strong><div class="muted">Imports and background jobs are all healthy.</div></div></div>`;
  const finished = d.recently_finished?.length
    ? `<div class="activity-list">${d.recently_finished.map(x=>`<div class="activity"><i class="activity-dot" style="background:var(--good)"></i><div><strong><a href="#library/${escAttr(x.work_id)}">${e(x.title)}</a></strong><span>Finished</span></div><span>${fmtDate(x.finished_at)}</span></div>`).join('')}</div>`
    : `<div class="empty"><strong>Nothing finished yet</strong>Mark a book as finished from its page to see it here.</div>`;
  const acq=dashboardAcqParts(acquisitionOn(state.me),c,d,stat,queueMini);
  $('#view').innerHTML=`<div class="page-head"><div><h1>Overview</h1><p>${acq.queue?'Your library, acquisition state, and service health in one place.':'Your library and service health in one place.'}</p></div><div class="page-actions">${acq.actions}<button class="button" data-route="discover">Add books</button></div></div>
  ${attention}
  <div class="grid stats-grid">
    ${stat('Library',c.works,'canonical works')}${stat('Audiobooks',c.owned_audiobooks,'owned')}${stat('Ebooks',c.owned_ebooks,'owned')}${acq.stats}${stat('Review',c.review,'needs attention')}
  </div>
  <div class="columns"><div class="panel"><div class="panel-head"><h2>Recent activity</h2><button class="button ghost compact" data-route="history">All history</button></div><div class="panel-body">${activityHtml(d.recent)}</div></div>
  ${acq.queue}</div>
  <div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Recently finished</h2></div><div class="panel-body">${finished}</div></div>`;
  wireRoutes(); const pollNow=$('#poll-now'); if(pollNow) pollNow.onclick=async()=>{try{const x=await api('/api/v1/queue/poll',{method:'POST',body:{}});toast(`Updated ${x.updated} download(s)`);renderDashboard()}catch(er){toast(er.message,'bad')}};
}

async function renderFailures(){
  setLoading('Failures');
  const d=await api('/api/v1/failures');
  $('#view').innerHTML=`<div class="page-head"><div><h1>Failures</h1><p>Everything that needs a decision, in one place.</p></div></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Failed imports</h2></div>${d.imports.length?`<div class="table-wrap"><table><thead><tr><th>Book</th><th>Reason</th><th>When</th><th></th></tr></thead><tbody>${d.imports.map(x=>`<tr><td><strong>${e(x.work_title||x.source_title||'Unknown')}</strong></td><td class="small">${e(x.error||'—')}</td><td>${fmtDate(x.updated_at)}</td><td><button class="button compact retry-import" data-id="${escAttr(x.id)}">Retry</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><strong>No failed imports</strong></div>'}</div>
  <div class="panel"><div class="panel-head"><h2>Failed jobs</h2></div>${d.jobs.length?`<div class="table-wrap"><table><thead><tr><th>Job</th><th>Error</th><th>When</th><th></th></tr></thead><tbody>${d.jobs.map(x=>`<tr><td><strong>${e(x.name)}</strong></td><td class="small">${e(x.error||'—')}</td><td>${fmtDate(x.started_at)}</td><td><button class="button compact retry-job" data-id="${escAttr(x.job_id)}">Run again</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><strong>No failed jobs</strong></div>'}</div>`;
  $$('.retry-import').forEach(b=>b.onclick=async()=>{ b.disabled=true; b.innerHTML='<span class="spinner"></span>'; try{ const x=await api(`/api/v1/downloads/${b.dataset.id}/import`,{method:'POST',body:{}}); toast(`Imported ${x.files.length} file(s)`); renderFailures(); }catch(er){ toast(er.message,'bad'); b.disabled=false; b.textContent='Retry'; } });
  $$('.retry-job').forEach(b=>b.onclick=async()=>{ b.disabled=true; b.innerHTML='<span class="spinner"></span>'; try{ const x=await api(`/api/v1/jobs/${b.dataset.id}/run`,{method:'POST',body:{}}); toast(`Job ${x.status}`,x.status==='success'?'good':'bad'); renderFailures(); }catch(er){ toast(er.message,'bad'); b.disabled=false; b.textContent='Run again'; } });
}
function stat(label,value,foot){return `<div class="stat-card"><div class="stat-label">${e(label)}</div><div class="stat-value">${fmtNum(value)}</div><div class="stat-foot">${e(foot)}</div></div>`}
function activityHtml(rows){ if(!rows?.length)return `<div class="empty"><strong>No activity yet</strong>Your history will appear here.</div>`; return `<div class="activity-list">${rows.map(x=>`<div class="activity"><i class="activity-dot"></i><div><strong>${e(x.message)}</strong><span>${e(x.event_type)}</span></div><span>${fmtDate(x.created_at)}</span></div>`).join('')}</div>`; }
function queueMini(rows){if(!rows?.length)return `<div class="empty"><strong>Queue is clear</strong>No active downloads.</div>`;return rows.map(x=>`<div class="activity"><i class="activity-dot"></i><div><strong>${e(x.title||x.source_title||'Download')}</strong><span>${e(x.state)}</span><div class="progress"><i style="width:${Math.round((x.progress||0)*100)}%"></i></div></div><span>${Math.round((x.progress||0)*100)}%</span></div>`).join('')}

// Library paging state. The page holds a window onto the library, never the whole
// of it: this library is twelve thousand works and the grid used to load a fixed
// first 300 then filter those 300 in the browser, so anything alphabetically later
// than the cut simply did not exist as far as the UI was concerned.
const LIBRARY_PAGE = 100;
const libraryState = { q: '', kind: '', status: '', seriesId: '', tagId: '', readStatus: '', offset: 0, total: 0, items: [], table: false, selectMode: false, selected: new Set() };

// <lib-filter-helpers> (pure; scripts/ and tests/test_library_filters_js.py evaluate this block)
const LIB_FILTER_FIELDS=[['q','Search'],['kind','Media'],['status','State'],['seriesId','Series'],['tagId','Tag'],['readStatus','Reading']];
const LIB_HASH_KEYS={q:'q',kind:'kind',status:'status',series:'seriesId',tag:'tagId',read:'readStatus'};
function libFilterDefaults(){ return {q:'',kind:'',status:'',seriesId:'',tagId:'',readStatus:''}; }
// The active filters as [{key,label}] for the removable chips. An empty value is not a filter.
function libActiveFilters(s,labelOf){
  return LIB_FILTER_FIELDS.filter(([k])=>s[k]).map(([k,name])=>({key:k,label:k==='q'?`${name}: \u201c${s[k]}\u201d`:`${name}: ${(labelOf&&labelOf(k,s[k]))||s[k]}`}));
}
// '#library?q=x&tag=ID' -> {path:'library',filters:{q:'x',tagId:'ID'}}; filters is null when the hash carries none.
function parseLibraryHash(raw){
  const i=raw.indexOf('?'); if(i<0) return {path:raw,filters:null};
  const p=new URLSearchParams(raw.slice(i+1)), f={};
  for(const [param,key] of Object.entries(LIB_HASH_KEYS)){ const v=(p.get(param)||'').trim(); if(v) f[key]=v; }
  if(f.kind&&!['audiobook','ebook'].includes(f.kind)) delete f.kind;
  if(f.status&&!['owned','missing','complete','partial','nothing'].includes(f.status)) delete f.status;
  return {path:raw.slice(0,i),filters:Object.keys(f).length?f:null};
}
// </lib-filter-helpers>
// <podcast-show-helpers> (pure; tests/test_podcast_show_js.py evaluates this block)
// Numbered seasons ascending, Extras always last.
function podSortSeasons(seasons){ return [...(seasons||[])].sort((a,b)=>(a.is_extras?1:0)-(b.is_extras?1:0)||(Number(a.number)||0)-(Number(b.number)||0)); }
// Ids may be numbers (API) or strings (dataset attributes): always compare as strings.
function podSameId(a,b){ return a!=null&&b!=null&&String(a)===String(b); }
// Route of the player page for one episode (the hash the show page's Play buttons open).
function podPlayRoute(showId,episodeId){ return `podcasts/${encodeURIComponent(showId)}/play/${encodeURIComponent(episodeId)}`; }
function podSeasonLabel(s){ return s.is_extras?'Extras':`Season ${s.number}`; }
function podFmtDuration(sec){ sec=Math.round(Number(sec)); if(!(sec>0)) return ''; const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60); return h?`${h}h ${m}m`:`${m}m`; }
function podCoverHtml(show,esc){ return show.cover_url?`<img class="pod-cover" src="${esc(show.cover_url)}" alt="Cover of ${esc(show.title)}">`:'<div class="pod-cover pod-cover-ph" aria-label="No cover">\u266a</div>'; }
function podSelectorHtml(seasons,selectedId,esc){ return podSortSeasons(seasons).map(s=>{ const on=podSameId(s.id,selectedId); return `<button class="button compact pod-season${on?'':' ghost'}" data-season="${esc(s.id)}" aria-pressed="${on}">${esc(podSeasonLabel(s))}</button>`; }).join(''); }
function podEpisodeHtml(ep,esc){
  const dur=podFmtDuration(ep.duration_seconds), desc=String(ep.description||'').trim();
  return `<li class="pod-episode" data-episode="${esc(ep.id)}" data-played="${ep.played?'true':'false'}"><div class="pod-ep-head"><strong>${esc(ep.title)}</strong>${ep.missing?'<span class="badge bad pod-missing">Missing</span>':''}<span class="pod-played muted small">${ep.played?'Played':'Unplayed'}</span><button class="button compact pod-play" data-play="${esc(ep.id)}" aria-label="Play ${esc(ep.title)}">\u25b6 Play</button></div><div class="muted small pod-ep-meta">${esc(ep.release_date||'')}${dur?` \u00b7 ${esc(dur)}`:''}</div>${desc?`<p class="pod-desc">${esc(desc)}</p>`:''}</li>`;
}
function podEpisodesHtml(season,esc){ const eps=season?.episodes||[]; return eps.length?`<ul class="pod-episodes">${eps.map(x=>podEpisodeHtml(x,esc)).join('')}</ul>`:'<div class="empty"><strong>No episodes</strong></div>'; }
function podShowHtml(show,selectedId,esc){
  const seasons=podSortSeasons(show.seasons), sel=seasons.find(s=>podSameId(s.id,selectedId))||seasons[0];
  return `<div class="page-head pod-head">${podCoverHtml(show,esc)}<div><h1>${esc(show.title)}</h1></div></div><div class="pod-seasons">${podSelectorHtml(seasons,sel?.id,esc)}</div><div id="pod-episodes">${podEpisodesHtml(sel,esc)}</div>`;
}
// </podcast-show-helpers>
async function renderPodcastShow(id){
  setLoading('Podcast');
  const show=await api(`/api/v1/podcasts/${encodeURIComponent(id)}`);
  const draw=sel=>{
    $('#view').innerHTML=podShowHtml(show,sel,e);
    $$('.pod-season').forEach(b=>b.onclick=()=>draw(b.dataset.season));
    $$('.pod-play').forEach(b=>b.onclick=()=>{ location.hash=podPlayRoute(show.id,b.dataset.play); });
    const img=$('img.pod-cover'); if(img) img.addEventListener('error',()=>{ img.outerHTML=podCoverHtml({title:show.title,cover_url:''},e); });
  };
  draw(podSortSeasons(show.seasons)[0]?.id);
}
// Episode playback (#podcasts/<show>/play/<episode>): the shared audiobook player in consume.js, opened on the episode.
// When one ends the player starts the next unplayed episode (across seasons) and the hash follows it without a re-render.
async function renderPodcastPlayer(showId, episodeId){
  setLoading('Opening');
  const [mod,show]=await Promise.all([import('/assets/consume.js'), api(`/api/v1/podcasts/${encodeURIComponent(showId)}`)]);
  state.consumeCleanup=await mod.openPodcast({show, episodeId, host:$('#view'), api, toast, modal, closeModal, chrome:'management', autoplay:true,
    onBack:()=>{ location.hash=`podcasts/${showId}`; },
    onEpisode:ep=>{ history.replaceState(null,'','#'+podPlayRoute(showId,ep.id)); }});
}
// Leaving the Library (or using the menu) drops its filters; sort/view mode stay. The header box is
// cleared with them so the two inputs can never disagree.
function resetLibraryFilters(){
  clearTimeout(searchTimer); clearTimeout(libraryTypeTimer);   // a pending keystroke must not resurrect the query
  Object.assign(libraryState,libFilterDefaults(),{offset:0});
  const g=$('#global-search'); if(g) g.value='';
}
// Bookmarked links keep working: State "Missing" is now "Partly owned", "Owned" is the plain library (any state).
function normalizeLibraryState(){
  const L=libraryState;
  if(L.status==='owned') L.status='';
  else if(L.status==='missing') L.status='partial';
}
const LIB_STATE_OPTIONS=[['','Any state'],['complete','Complete'],['partial','Partly owned'],['nothing','Nothing owned']];
const LIB_STATE_TITLES={complete:'Every format you monitor is owned',partial:'You own something, but a monitored format is still missing',nothing:'Wanted, but nothing owned yet'};
function libraryStateSelectHtml(){
  return `<select class="select" id="status-filter" aria-label="State">${LIB_STATE_OPTIONS.map(([v,l])=>`<option value="${v}" data-label="${escAttr(l)}"${LIB_STATE_TITLES[v]?` title="${escAttr(LIB_STATE_TITLES[v])}"`:''}${libraryState.status===v?' selected':''}>${l}</option>`).join('')}</select>`;
}
// Counts in the State dropdown: works per state under the other filters (q, series, tag, reading status).
let libraryCountsSeq=0;
async function paintLibraryStateCounts(){
  const sel=$('#status-filter'); if(!sel) return;
  const seq=++libraryCountsSeq, L=libraryState, p=new URLSearchParams();
  if(L.q) p.set('q',L.q); if(L.seriesId) p.set('series_id',L.seriesId); if(L.tagId) p.set('tag_id',L.tagId); if(L.readStatus) p.set('read_status',L.readStatus);
  try{
    const c=await api(`/api/v1/library-state-counts?${p}`);
    if(seq!==libraryCountsSeq) return;
    const n={'':c.owned,complete:c.complete,partial:c.partial,nothing:c.nothing};
    $$('#status-filter option').forEach(o=>{ const l=o.dataset.label||o.textContent; o.textContent=`${l} (${fmtNum(n[o.value]??0)})`; });
    paintLibraryChips();
  }catch{}
}
function libraryQuery(offset){
  const p = new URLSearchParams({limit: String(LIBRARY_PAGE), offset: String(offset)});
  if(libraryState.q) p.set('q', libraryState.q);
  if(libraryState.kind) p.set('kind', libraryState.kind);
  if(libraryState.status) p.set('status', libraryState.status);
  if(libraryState.seriesId) p.set('series_id', libraryState.seriesId);
  if(libraryState.tagId) p.set('tag_id', libraryState.tagId);
  if(libraryState.readStatus) p.set('read_status', libraryState.readStatus);
  return `/api/v1/library?${p}`;
}

// Typing must not re-render the page. renderLibrary() builds the shell once when you
// arrive; every subsequent search or filter change only refetches and repaints the
// results. Rebuilding #view on each keystroke destroyed and recreated the very input
// being typed into, which stole focus and read as a full page reload.
let librarySeq = 0;
async function loadLibrary(){
  const seq = ++librarySeq;
  const container=$('#library-container');
  container?.classList.add('busy');
  try{
    const data=await api(libraryQuery(0));
    if(seq!==librarySeq) return;   // a later keystroke already superseded this one
    libraryState.total=data.total ?? data.items.length;
    libraryState.items=data.items;
    paintLibrary();
    paintLibraryStateCounts();
  } finally { if(seq===librarySeq) $('#library-container')?.classList.remove('busy'); }
}

// One place changes the library filters: the header box, the page input, the dropdowns, the
// chips and "Clear filters" all call this, so state, controls and results cannot drift apart.
function syncLibraryControls(){
  const L=libraryState, set=(id,v)=>{ const el=$(id); if(el && el.value!==v) el.value=v; };
  const inp=$('#library-filter'); if(inp && inp.value.trim()!==L.q) inp.value=L.q;
  const g=$('#global-search'); if(g && g.value.trim()!==L.q) g.value=L.q;
  set('#asset-filter',L.kind); set('#status-filter',L.status); set('#series-filter',L.seriesId); set('#tag-filter',L.tagId); set('#read-filter',L.readStatus);
}
function paintLibraryChips(){
  const box=$('#library-chips'); if(!box) return;
  const labelOf=(k,v)=>{ const sel={kind:'#asset-filter',status:'#status-filter',seriesId:'#series-filter',tagId:'#tag-filter',readStatus:'#read-filter'}[k]; const o=sel&&[...($(sel)?.options||[])].find(x=>x.value===v); return o?(o.dataset.label||o.textContent):undefined; };
  const chips=libActiveFilters(libraryState,labelOf);
  box.hidden=!chips.length;
  box.innerHTML=chips.map(c=>`<button type="button" class="chip filter-chip" data-clear="${escAttr(c.key)}" aria-label="Remove filter ${escAttr(c.label)}"><span class="filter-chip-text">${e(c.label)}</span> <span aria-hidden="true">\u00d7</span></button>`).join('')+(chips.length?'<button type="button" class="button ghost compact" id="library-clear-filters">Clear filters</button>':'');
  $$('[data-clear]',box).forEach(b=>b.onclick=()=>applyLibraryFilters({[b.dataset.clear]:''}));
  $('#library-clear-filters')?.addEventListener('click',()=>applyLibraryFilters(libFilterDefaults()));
}
function applyLibraryFilters(patch){
  const L=libraryState;
  if(!Object.keys(patch).some(k=>patch[k]!==L[k])) return;   // already so: no second request
  Object.assign(L,patch,{offset:0});
  // The explicit filters of a link (#library?tag=..) apply once; changing anything makes the page the source of truth.
  if(location.hash.includes('?')) history.replaceState(null,'','#library');
  normalizeLibraryState(); syncLibraryControls(); paintLibraryChips();
  loadLibrary().catch(er=>toast(er.message,'bad'));
}
// Typing waits for a pause; clearing (the x of a search box, Escape, deleting the text) must not.
let libraryTypeTimer;
function libraryTyped(value,immediate){
  clearTimeout(libraryTypeTimer);
  const q=value.trim(), g=$('#global-search'); if(g && g.value.trim()!==q) g.value=q;
  if(immediate||!q) applyLibraryFilters({q}); else libraryTypeTimer=setTimeout(()=>applyLibraryFilters({q}),300);
}

async function renderLibrary(q){
  setLoading('Library');
  if(q!==undefined) libraryState.q=q;
  normalizeLibraryState();
  libraryState.offset=0;
  const full=!isKidsScope();
  const [data, seriesList, tagList] = await Promise.all([
    api(libraryQuery(0)),
    full ? api('/api/v1/series').catch(()=>({items:[]})) : Promise.resolve({items:[]}),
    api('/api/v1/tags').catch(()=>({items:[]})),
  ]);
  libraryState.total=data.total ?? data.items.length;
  libraryState.items=data.items;
  const seriesOptions=seriesList.items.map(sv=>`<option value="${escAttr(sv.id)}" ${libraryState.seriesId===sv.id?'selected':''}>${e(sv.name)}</option>`).join('');
  const tagOptions=tagList.items.filter(t=>full||t.name!=='kids').map(t=>`<option value="${escAttr(t.id)}" ${libraryState.tagId===t.id?'selected':''}>${e(tagLabel(t))}</option>`).join('');
  $('#view').innerHTML=`<div class="page-head"><div><h1>Library</h1><p id="library-count"></p></div><div class="page-actions">${full?`<button class="button ghost secondary" id="select-mode">${libraryState.selectMode?'Cancel':'Select'}</button><button class="button secondary" id="list-view">${libraryState.table?'Covers':'Table'}</button><button class="button" data-route="discover">Add</button>`:''}</div></div>
    <div id="bulk-bar" class="hidden"></div>
    <div class="toolbar"><input class="input grow" id="library-filter" type="search" enterkeyhint="search" autocomplete="off" placeholder="Search title or author…" value="${escAttr(libraryState.q)}"><select class="select" id="asset-filter"><option value="">All media</option><option value="audiobook"${libraryState.kind==='audiobook'?' selected':''}>Audiobooks</option><option value="ebook"${libraryState.kind==='ebook'?' selected':''}>Ebooks</option></select>${libraryStateSelectHtml()}</div>
    <div class="toolbar" style="margin-top:8px">${full?`<select class="select" id="series-filter"><option value="">All series</option>${seriesOptions}</select>`:''}<select class="select" id="tag-filter"><option value="">All tags</option>${tagOptions}</select><select class="select" id="read-filter"><option value="">Any reading status</option>${READ_STATES.filter(([v])=>v).map(([v,l])=>`<option value="${v}" ${libraryState.readStatus===v?'selected':''}>${l}</option>`).join('')}</select></div>
    <div id="library-chips" class="filter-chips" hidden></div>
    <div id="library-container" class="library-grid"></div>
    <div id="library-more" style="margin-top:18px;text-align:center"></div>`;
  wireRoutes();
  syncLibraryControls(); paintLibraryChips();
  paintLibrary();
  // Searching and filtering re-query the server - filtering the loaded page in the
  // browser would only ever search whatever slice happened to be on screen - but they
  // repaint the results only, leaving the toolbar (and the caret) alone.
  const lf=$('#library-filter');
  lf.oninput=ev=>libraryTyped(ev.target.value,false);
  lf.addEventListener('search',ev=>libraryTyped(ev.target.value,true));   // the x / Enter of type=search
  lf.addEventListener('keydown',ev=>{ if(ev.key==='Escape' && lf.value){ ev.preventDefault(); lf.value=''; libraryTyped('',true); } });
  paintLibraryStateCounts();
  $('#asset-filter').onchange=ev=>applyLibraryFilters({kind:ev.target.value});
  $('#status-filter').onchange=ev=>applyLibraryFilters({status:ev.target.value});
  $('#series-filter')?.addEventListener('change',ev=>applyLibraryFilters({seriesId:ev.target.value}));
  $('#tag-filter')?.addEventListener('change',ev=>applyLibraryFilters({tagId:ev.target.value}));
  $('#read-filter')?.addEventListener('change',ev=>applyLibraryFilters({readStatus:ev.target.value}));
  $('#list-view')?.addEventListener('click',()=>{libraryState.table=!libraryState.table;$('#list-view').textContent=libraryState.table?'Covers':'Table';paintLibrary()});
  $('#select-mode')?.addEventListener('click',()=>{
    libraryState.selectMode=!libraryState.selectMode;
    if(!libraryState.selectMode) libraryState.selected.clear();
    else if(libraryState.table){ libraryState.table=false; $('#list-view').textContent='Table'; }   // only covers can be ticked
    $('#select-mode').textContent=libraryState.selectMode?'Cancel':'Select';
    paintLibrary();
  });
}
function paintBulkBar(){
  const bar=$('#bulk-bar'); if(!bar) return;
  const n=libraryState.selected.size;
  if(!libraryState.selectMode){ bar.classList.add('hidden'); bar.innerHTML=''; return; }
  bar.classList.remove('hidden');
  const tagged=libraryState.tagId?` tagged with the filter above`:'';
  bar.innerHTML=`<div class="attention-card calm bulk-bar-card"><div><strong>${n} selected</strong><div class="muted small">Tick books, or select every one of the ${fmtNum(libraryState.total)} listed${tagged}.</div></div><div class="bulk-actions"><button class="button ghost compact" id="select-all-shown">Select all ${fmtNum(libraryState.total)}</button>${n?'<button class="button ghost compact" id="select-none">Clear</button>':''}<button class="button compact" id="bulk-kids-shelf" ${n?'':'disabled'} title="Kid accounts will be able to see and open these books">Add to kids shelf</button><button class="button compact" id="bulk-tag" ${n?'':'disabled'}>Add tag…</button><button class="button compact danger" id="bulk-remove-tag" ${n?'':'disabled'}>Remove tag…</button></div></div>`;
  $('#bulk-kids-shelf').onclick=()=>bulkKidsShelfDialog([...libraryState.selected]);
  $('#bulk-tag').onclick=()=>bulkTagDialog([...libraryState.selected]);
  $('#bulk-remove-tag').onclick=()=>bulkRemoveTagDialog([...libraryState.selected]);
  $('#select-none')?.addEventListener('click',()=>{ libraryState.selected.clear(); paintLibrary(); });
  $('#select-all-shown').onclick=async ev=>{
    ev.target.disabled=true; ev.target.innerHTML='<span class="spinner"></span> Selecting';
    try{
      // The list is paged: fetch the rest of this filter so "all" means all, not one page.
      while(libraryState.items.length<libraryState.total && libraryState.items.length<2000){
        const next=await api(libraryQuery(libraryState.items.length));
        if(!next.items.length) break;
        libraryState.items=libraryState.items.concat(next.items);
      }
      libraryState.items.forEach(w=>libraryState.selected.add(w.id));
      paintLibrary();
    }catch(er){ toast(er.message,'bad'); paintBulkBar(); }
  };
}
// Bulk 'Remove tag': one server call per tag, per-book results, admin only and audited
// (POST /api/v1/tags/<id>/remove-works). The kids tag goes through the same path as the
// single-book control, so each removal is a recorded manual untag the classifier respects.
function libraryBulkDone(){ libraryState.selectMode=false; libraryState.selected.clear(); renderLibrary(); }
function bulkRemoveTagDialog(workIds,opts={}){
  const ctx=opts.ctx, onDone=opts.onDone||libraryBulkDone;
  api('/api/v1/tags').then(d=>{
    const options=d.items.map(t=>`<option value="${escAttr(t.id)}" data-name="${escAttr(t.name)}" ${!opts.ctx&&libraryState.tagId===t.id?'selected':''}>${e(tagLabel(t))}</option>`).join('');
    const noun=`${workIds.length} book${workIds.length===1?'':'s'}`;
    const m=modal(`Remove a tag from ${noun}`,`<div class="field"><label for="tag-remove-pick">Tag to remove</label><select class="select" id="tag-remove-pick"><option value="">Choose…</option>${options}</select></div><p class="muted small" id="tag-remove-effect"></p>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button danger" id="tag-remove-go">Remove tag</button>'});
    const pick=$('#tag-remove-pick',m), effect=$('#tag-remove-effect',m);
    const sync=()=>{ const kids=pick.selectedOptions[0]?.dataset.name==='kids'; effect.textContent=kids?`This takes ${noun} off the kids shelf. ${KIDS_REMOVE_EFFECT}`:(pick.value?`Removes the tag from ${noun}. Books that do not have it are left alone.`:''); };
    pick.onchange=sync; sync();
    $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
    $('#tag-remove-go',m).onclick=async()=>{
      if(!pick.value) return toast('Pick a tag first','bad');
      const btn=$('#tag-remove-go',m); btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
      try{
        let removed=0, skipped=0;
        for(let i=0;i<workIds.length;i+=500){
          const r=await api(`/api/v1/tags/${pick.value}/remove-works`,{method:'POST',body:{work_ids:workIds.slice(i,i+500),...(ctx||{})}});
          removed+=r.removed; skipped+=r.results.filter(x=>x.status!=='removed').length;
        }
        $('#modal-root').innerHTML='';
        toast(`Removed from ${removed} book${removed===1?'':'s'}${skipped?` (${skipped} did not have it)`:''}`);
        onDone();
      }catch(er){ toast(er.message,'bad'); btn.disabled=false; btn.textContent='Remove tag'; }
    };
  });
}
// Bulk add: one server call per 500 books (POST /api/v1/tags/<id>/add-works), per-book
// results. The kids tag goes through the same path as the single-book control, so each
// add is a recorded manual tag the classifier never overrides.
const KIDS_ADD_EFFECT='Kid accounts will be able to see and open these books. The classifier will not change them; only you can take them off again.';
async function bulkAddToTag(tagId,workIds,ctx){
  const tot={applied:0,already:0,skipped:0,failed:0,problems:[]};
  for(let i=0;i<workIds.length;i+=500){
    const r=await api(`/api/v1/tags/${tagId}/add-works`,{method:'POST',body:{work_ids:workIds.slice(i,i+500),...(ctx||{})}});
    for(const k of ['applied','already','skipped','failed']) tot[k]+=r[k];
    r.results.filter(x=>x.status==='failed'||x.status==='skipped').forEach(x=>tot.problems.push(x));
  }
  return tot;
}
function bulkResultDialog(title,tot,onDone=libraryBulkDone){
  const parts=[`<li><strong>${tot.applied}</strong> added</li>`,`<li><strong>${tot.already}</strong> already had it</li>`];
  if(tot.skipped) parts.push(`<li><strong>${tot.skipped}</strong> skipped</li>`);
  if(tot.failed) parts.push(`<li><strong>${tot.failed}</strong> failed</li>`);
  const why=tot.problems.slice(0,5).map(x=>`<li class="muted small">${e(x.status)}: ${e(x.reason||'')}</li>`).join('');
  const m=modal(title,`<ul id="bulk-result">${parts.join('')}</ul>${why?`<ul>${why}</ul>`:''}`,{footer:'<button class="button" id="bulk-result-ok">Done</button>'});
  $('#bulk-result-ok',m).onclick=()=>{ $('#modal-root').innerHTML=''; onDone(); };
}
function bulkKidsShelfDialog(workIds,opts={}){
  const noun=`${workIds.length} book${workIds.length===1?'':'s'}`;
  const m=modal('Add to kids shelf',`<p id="kids-add-confirm">Put <strong>${noun}</strong> on the kids shelf? ${KIDS_ADD_EFFECT}</p>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="kids-add-go">Add to kids shelf</button>'});
  $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
  $('#kids-add-go',m).onclick=async()=>{
    const btn=$('#kids-add-go',m); btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
    try{
      const kidsTag=(await api('/api/v1/tags')).items.find(t=>t.name==='kids');
      if(!kidsTag) throw new Error('Kids shelf tag not found');
      bulkResultDialog('Kids shelf updated',await bulkAddToTag(kidsTag.id,workIds,opts.ctx),opts.onDone);
    }catch(er){ toast(er.message,'bad'); btn.disabled=false; btn.textContent='Add to kids shelf'; }
  };
}
function bulkKidsRemoveDialog(workIds,opts={}){
  const noun=`${workIds.length} book${workIds.length===1?'':'s'}`;
  const m=modal('Remove from kids shelf',`<p id="kids-remove-confirm">Take <strong>${noun}</strong> off the kids shelf? ${KIDS_REMOVE_EFFECT}</p>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button danger" id="kids-remove-go">Remove from kids shelf</button>'});
  $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
  $('#kids-remove-go',m).onclick=async()=>{
    const btn=$('#kids-remove-go',m); btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
    try{
      const kidsTag=(await api('/api/v1/tags')).items.find(t=>t.name==='kids');
      if(!kidsTag) throw new Error('Kids shelf tag not found');
      let removed=0, skipped=0;
      for(let i=0;i<workIds.length;i+=500){
        const r=await api(`/api/v1/tags/${kidsTag.id}/remove-works`,{method:'POST',body:{work_ids:workIds.slice(i,i+500),...(opts.ctx||{})}});
        removed+=r.removed; skipped+=r.results.filter(x=>x.status!=='removed').length;
      }
      $('#modal-root').innerHTML='';
      toast(`Removed ${removed} book${removed===1?'':'s'} from the kids shelf${skipped?` (${skipped} were not on it)`:''}`);
      (opts.onDone||libraryBulkDone)();
    }catch(er){ toast(er.message,'bad'); btn.disabled=false; btn.textContent='Remove from kids shelf'; }
  };
}
function bulkTagDialog(workIds,opts={}){
  api('/api/v1/tags').then(d=>{
    const options=d.items.map(t=>`<option value="${escAttr(t.id)}" data-name="${escAttr(t.name)}">${e(tagLabel(t))}</option>`).join('');
    const noun=`${workIds.length} book${workIds.length===1?'':'s'}`;
    const m=modal(`Tag ${noun}`,`<div class="field"><label for="tag-pick">Existing tag</label><select class="select" id="tag-pick"><option value="">Choose…</option>${options}</select></div><div class="field"><label>Or create a new one</label><input class="input" id="tag-new" placeholder="e.g. cozy mystery"></div><p class="muted small" id="tag-add-effect"></p>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="tag-save">Apply</button>'});
    const pick=$('#tag-pick',m), effect=$('#tag-add-effect',m);
    pick.onchange=()=>{ effect.textContent=pick.selectedOptions[0]?.dataset.name==='kids'?`This puts ${noun} on the kids shelf. ${KIDS_ADD_EFFECT}`:''; };
    $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
    $('#tag-save',m).onclick=async()=>{
      const btn=$('#tag-save',m); btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
      try{
        let tagId=pick.value;
        const newName=$('#tag-new',m).value.trim();
        if(!tagId && newName) tagId=(await api('/api/v1/tags',{method:'POST',body:{name:newName}})).id;
        if(!tagId){ toast('Pick or create a tag first','bad'); btn.disabled=false; btn.textContent='Apply'; return; }
        if(pick.selectedOptions[0]?.dataset.name==='kids' && !confirm(`Put ${noun} on the kids shelf?\n\n${KIDS_ADD_EFFECT}`)){ btn.disabled=false; btn.textContent='Apply'; return; }
        bulkResultDialog('Tag applied',await bulkAddToTag(tagId,workIds,opts.ctx),opts.onDone);
      }catch(er){ toast(er.message,'bad'); btn.disabled=false; btn.textContent='Apply'; }
    };
  });
}

function paintLibrary(){
  const container=$('#library-container'); if(!container) return;
  const shown=libraryState.items.length;
  const filtered=libActiveFilters(libraryState).length>0;
  paintLibraryChips();
  const noun=n=>`${fmtNum(n)} work${n===1?'':'s'}`;
  $('#library-count').textContent=shown>=libraryState.total
    ? (filtered ? `${noun(libraryState.total)} matching.` : `${noun(libraryState.total)} — ebooks and audiobooks unified by bibliographic work.`)
    : `Showing ${fmtNum(shown)} of ${noun(libraryState.total)}${filtered?' matching':''}.`;
  if(!libraryState.items.length){
    container.className='panel';
    container.innerHTML=`<div class="empty"><strong>No matching works</strong>${libraryState.q?`Nothing in the library matches “${e(libraryState.q)}”.`:filtered?'No work matches the filters you have set.':'Add books from Discover, or import an existing library.'}${filtered?'<div style="margin-top:12px"><button type="button" class="button secondary" id="library-empty-clear">Clear filters</button></div>':''}</div>`;
    $('#library-empty-clear')?.addEventListener('click',()=>applyLibraryFilters(libFilterDefaults()));
  } else if(libraryState.table){
    container.className='panel table-wrap';
    container.innerHTML=`<table><thead><tr><th>Title</th><th>Author</th><th>Ebook</th><th>Audio</th><th>Metadata</th></tr></thead><tbody>${libraryState.items.map(w=>`<tr class="clickable" data-id="${escAttr(w.id)}"><td><strong>${e(w.title)}</strong></td><td>${e(w.authors.map(a=>a.name).join(', '))}</td><td>${assetStatus(w,'ebook')}</td><td>${assetStatus(w,'audiobook')}</td><td>${metadataBadge(w)}</td></tr>`).join('')}</tbody></table>`;
    $$('tr[data-id]',container).forEach(x=>x.onclick=()=>location.hash=`library/${x.dataset.id}`);
  } else {
    container.className='library-grid';
    container.innerHTML=libraryState.items.map(w=>bookCard(w,libraryState.selectMode)).join('');
    wireBookCards();
  }
  paintBulkBar();
  const more=$('#library-more');
  if(shown<libraryState.total){
    more.innerHTML=`<button class="button secondary" id="library-load-more">Load ${fmtNum(Math.min(LIBRARY_PAGE,libraryState.total-shown))} more</button>`;
    $('#library-load-more').onclick=async ev=>{
      ev.target.disabled=true; ev.target.innerHTML='<span class="spinner"></span> Loading';
      try{
        const next=await api(libraryQuery(shown));
        libraryState.items=libraryState.items.concat(next.items);
        libraryState.total=next.total ?? libraryState.total;
        paintLibrary();
      }catch(er){toast(er.message,'bad');ev.target.disabled=false;ev.target.textContent='Load more'}
    };
  } else more.innerHTML='';
}

// Covers come from bookdoe, never straight from a metadata provider: works
// migrated from Readarr have no cover_url at all, and pointing <img> at a remote
// host would leak the library's contents and break when that host is down. The
// endpoint 404s when there is no art, which onerror turns back into a title tile.
// A deterministic hue from the title+author, not a random one: the same book must
// always render the same color across reloads and across the grid/list/detail views,
// or it would look like the fallback tile itself was broken.
function hashHue(str){
  let h=0; for(let i=0;i<str.length;i++){ h=(h*31+str.charCodeAt(i))|0; }
  return Math.abs(h)%360;
}
function coverHtml(w,{size='normal'}={}){
  // Only request art we know exists. Rendering an <img> for every work meant a 404
  // per coverless book on each page of the grid - noise in the console, wasted
  // round-trips, and a visible broken-image flash before onerror cleaned it up.
  const img = w.has_cover===false ? '' : `<img loading="lazy" class="cover-img" src="/api/v1/library/${encodeURIComponent(w.id)}/cover" alt="" data-img-fallback="remove">`;
  // The fallback is real design, not a placeholder: at any given moment roughly two
  // thirds of this library has no cover art, so this tile IS what most of the grid
  // looks like. A hue derived from the book's own identity plus its initial letter
  // reads as "this is book X" rather than "art failed to load".
  const author=(w.authors||[]).map(a=>a.name).join(', ');
  const hue=hashHue(w.title+author);
  const initial=(w.title||'?').trim().charAt(0).toUpperCase();
  const fallback=`<div class="cover-fallback" style="--hue:${hue}"><span class="cover-fallback-mark">${e(initial)}</span><span class="cover-fallback-title">${e(w.title)}</span>${author?`<span class="cover-fallback-author">${e(author)}</span>`:''}</div>`;
  return `${img}${fallback}`;
}
// Small grey chips for monitored formats the book does not have yet ("No audiobook"), next to the owned-format badges,
// so a card with an EPUB and a monitored audiobook is not mislabeled as missing. The server decides (ownership.summarize).
function missingFormatChips(w){
  return (w.missing_formats||[]).map(k=>`<span class="badge chip-missing" title="Monitored, not owned yet">No ${k==='audiobook'?'audiobook':'ebook'}</span>`).join('');
}
function bookCard(w,selectMode=false){
  const owned=w.assets.filter(a=>a.status==='owned');
  const badges=owned.map(a=>`<span class="badge good">${a.kind==='audiobook'?'♫':'▤'} ${e((a.format||a.kind).toUpperCase())}</span>`).join('')+missingFormatChips(w);
  const author=w.authors.map(a=>a.name).join(', ');
  const corner=[];
  if(w.rating) corner.push(`<span class="corner-badge" title="Your rating">★ ${w.rating}</span>`);
  if(w.read_status==='finished') corner.push(`<span class="corner-badge good" title="Finished">✓</span>`);
  else if(w.read_status==='reading') corner.push(`<span class="corner-badge info" title="Reading">…</span>`);
  const checkbox=selectMode?'<div class="book-card-check"></div>':'';
  return `<article class="book-card" data-id="${escAttr(w.id)}"><div class="cover">${coverHtml(w)}<div class="asset-badges">${badges}</div>${corner.length?`<div class="corner-badges">${corner.join('')}</div>`:''}${checkbox}</div><div class="book-title">${e(w.title)}</div><div class="book-author">${e(author||'Unknown author')}</div></article>`}
function wireBookCards(){
  $$('.book-card').forEach(x=>{
    if(libraryState.selectMode){
      x.classList.toggle('selected', libraryState.selected.has(x.dataset.id));
      x.onclick=()=>{
        if(libraryState.selected.has(x.dataset.id)) libraryState.selected.delete(x.dataset.id);
        else libraryState.selected.add(x.dataset.id);
        x.classList.toggle('selected');
        paintBulkBar();
      };
    } else {
      x.onclick=()=>location.hash=`library/${x.dataset.id}`;
    }
  });
}
const ASSET_STATUS_LABEL={owned:'owned',missing:'wanted',downloading:'downloading',none:'not tracked'};
function assetStatus(w,k){const a=w.assets.find(x=>x.kind===k&&x.status==='owned')||w.assets.find(x=>x.kind===k);if(!a)return '—';const label=ASSET_STATUS_LABEL[a.status]||a.status;return `<span class="badge ${a.status==='owned'?'good':a.status==='none'?'':a.monitored?'warn':''}">${e(label)}${a.format?' · '+e(a.format.toUpperCase()):''}</span>${qualityBadge(a)}${policyChip(a)}`}

// Owned-media quality (P5-26) and the opt-in upgrade policy (P5-27). Only owned assets are
// evaluated, so a wanted or untracked slot gets no quality badge. Everyone sees the state
// (read-only); the policy chip and selector are for full-access accounts only.
const QUALITY_BADGE={satisfactory:['good','Satisfactory'],upgrade_desired:['warn','Upgrade desired']};
const UPGRADE_POLICY_LABEL={none:'Upgrades: off',flag_only:'Upgrades: flag only',search:'Upgrades: search'};
function qualityBadge(a){
  if(a.status!=='owned') return '';
  const [cls,label]=QUALITY_BADGE[a.quality_state]||['','Unevaluated'];
  return ` <span class="badge quality-badge ${cls}" data-quality="${escAttr(a.quality_state||'unevaluated')}">${label}</span>`;
}
function policyChip(a){
  if(isKidsScope() || a.status!=='owned') return '';
  const p=a.upgrade_policy||'none';
  return ` <span class="badge info policy-chip" data-policy="${escAttr(p)}">${e(UPGRADE_POLICY_LABEL[p]||p)}</span>`;
}
function policySelector(a){
  const p=a.upgrade_policy||'none';
  return `<label class="small muted">Upgrades <select class="asset-policy" data-asset="${escAttr(a.id)}" title="Whether ${e(PRODUCT)} may search for a better release of this owned file. It never downloads or replaces anything by itself.">${Object.entries(UPGRADE_POLICY_LABEL).map(([v,l])=>`<option value="${v}"${v===p?' selected':''}>${e(l.replace('Upgrades: ',''))}</option>`).join('')}</select></label>`;
}

// Resolved (a provider vouches for this), Local (real data pulled from the file
// itself), and Manual (an operator corrected it by hand) are all legitimate, settled
// states - only a genuine null metadata_source is a warning. The old version painted
// every non-provider-linked work amber, including ones with a perfectly good cover,
// series and year sourced from the file - which read as "something is wrong here"
// on a book that was actually fine.
function metadataBadge(w){
  if(w.metadata_source==='provider') return `<span class="badge good">Resolved</span>`;
  if(w.metadata_source==='local') return `<span class="badge info">From file</span>`;
  if(w.metadata_source==='manual') return `<span class="badge info">Manual</span>`;
  return `<span class="badge warn">Unresolved</span>`;
}
function starWidget(workId,rating,{interactive=true}={}){
  const stars=[1,2,3,4,5].map(n=>`<span class="star ${n<=(rating||0)?'filled':''}" data-n="${n}">★</span>`).join('');
  return `<div class="star-widget ${interactive?'':'readonly'}" data-work="${escAttr(workId)}">${stars}</div>`;
}
function wireStarWidget(el,onSet){
  $$('.star',el).forEach(star=>{
    star.onclick=()=>{ const n=Number(star.dataset.n); const current=$$('.star',el).filter(s=>s.classList.contains('filled')).length; onSet(current===n?null:n); };
  });
}
const READ_STATES=[['','Not tracked'],['want_to_read','Want to read'],['reading','Reading'],['finished','Finished'],['dnf','Did not finish']];

// One user-facing name for the kids system tag, in Management and the Player alike. The
// server sends it as `label`; the stored name stays 'kids' (the access boundary keys on it).
const KIDS_LABEL='Kids shelf';
function tagLabel(t){ return t.label || (t.name==='kids'?KIDS_LABEL:t.name); }
// What removing the kids tag does, said the same way wherever it can be done.
const KIDS_REMOVE_EFFECT='Kid accounts lose access to it right away (unless it is also allowed to a kid individually or through another tag). The classifier will not tag it again; only you can put it back.';
// Why a book is on the shelf (admin only): what the model cited, the signals the deterministic
// gate sees in the metadata now, and for an auto-approved book whether it passes today's gate.
// Provider categories / subjects / maturity stored for a book (services/audience.py), each with who said it.
const AUDIENCE_KIND_LABEL={category:'Category',subject:'Subject',maturity:'Maturity rating',age_range:'Age range',audience:'Audience'};
const AUDIENCE_NORM_LABEL={juvenile:'children\'s',mature:'mature',adult_audience:'adult',adult_genre:'adult genre',young_adult:'young adult'};
function audienceMetaHtml(items){
  const shown=items.filter(x=>AUDIENCE_KIND_LABEL[x.kind]);
  if(!shown.length) return '<span class="muted"> none stored yet; the audience backfill fills this in (Settings, Tags)</span>';
  return `<ul class="kids-why-list">${shown.map(x=>`<li>${e(AUDIENCE_KIND_LABEL[x.kind])}: ${e(x.value)} <span class="muted small">(${e(x.provider.replace('_',' '))}${AUDIENCE_NORM_LABEL[x.norm]?`, ${e(AUDIENCE_NORM_LABEL[x.norm])}`:''}, ${e(fmtDate(x.fetched_at))})</span></li>`).join('')}</ul>`;
}
function audienceCoverageText(c){
  if(!c) return '';
  return `${fmtNum(c.with_audience)} of ${fmtNum(c.total_works)} works have audience metadata`+(c.pending?` · ${fmtNum(c.pending)} still to check`:'')+(c.running?' · backfill running':'');
}
function kidsEvidenceHtml(w){
  const k=w.kids_evidence; if(!k||isKidsScope()) return '';
  const list=(items,cls)=>items.length?`<ul class="kids-why-list ${cls}">${items.map(x=>`<li>${e(x)}</li>`).join('')}</ul>`:'<span class="muted">none</span>';
  const strong=(k.strong_signals||[]).map(x=>x.detail), hard=(k.hard_adult_signals||[]).map(x=>x.detail);
  const gate=k.auto_approved?(k.gate.passed?'<span class="badge good">Passes the audience-evidence gate</span>':'<span class="badge warn">Fails the audience-evidence gate</span>'):(k.gate.passed?'<span class="badge good">Would pass the gate</span>':'<span class="badge">Would not be auto-approved</span>');
  return `<details class="kids-why" id="kids-why" ${k.auto_approved&&!k.gate.passed?'open':''}><summary>Why it is on the shelf ${gate}</summary>
    <div class="small"><p>${k.verdict?`Model verdict <strong>${e(k.verdict.replace('_',' '))}</strong>${k.confidence!=null?` at confidence ${e(String(k.confidence))}`:''}${k.model?` (${e(k.model)})`:''}${k.source==='deterministic'?' - decided by adult signals in the metadata, no model call':''}. ${e(k.reason||'')}`:'No classifier verdict on record (placed by a person).'}</p>
    <div><strong>Strong audience signals in the metadata</strong>${list(strong,'good')}</div>
    <div><strong>Cited by the model</strong>${k.has_cited_evidence?list(k.audience_evidence||[],''):'<span class="muted"> verdict recorded before the model cited evidence</span>'}</div>
    <div><strong>Adult signals</strong>${list([...hard,...(k.llm_adult_signals||[])],'bad')}</div>
    ${(k.provider_conflict||[]).length?`<div><strong>Providers disagree</strong><span class="muted small"> a provider marks it mature, so these do not count as audience evidence</span>${list(k.provider_conflict,'bad')}</div>`:''}
    <div id="kids-audience-meta"><strong>Stored audience metadata</strong>${audienceMetaHtml(k.audience_metadata||[])}</div>
    ${k.gate.passed?'':`<div><strong>Why the gate would not auto-approve</strong>${list(k.gate.reasons||[],'bad')}</div>`}
    ${k.evidence_changed?'<p class="muted">The metadata has changed since the model last looked.</p>':''}</div></details>`;
}
function kidsDecisionTitle(w){const d=w.kids_decision;if(!d)return 'Not decided by a person; the classifier may propose it.';if(d.via==='auto_approved')return `Auto-approved by the classifier at confidence ${d.confidence??'?'} (${d.model||'model unknown'}), ${fmtDate(d.at)}. Removing the tag by hand locks it off.`;return `${d.state==='on'?'On':'Off'} the kids shelf by ${d.by||'an administrator'} (${d.via.replace(/_/g,' ')}), ${fmtDate(d.at)}. The classifier will not change this.`}
async function renderWork(id){
  setLoading('Book');
  const w=await api(`/api/v1/library/${encodeURIComponent(id)}`);
  const author=w.authors.map(a=>a.name).join(', '); const series=w.series.map(s=>`${s.name}${s.position?' #'+s.position:''}`).join(' · ');
  const seriesLinks=w.series.map(s=>`<a class="button secondary compact series-link" href="#series/${escAttr(s.id)}" data-series="${escAttr(s.id)}" title="Open the series page">Series: ${e(s.name)}${s.position?' #'+e(s.position):''} ›</a>`).join(' ');
  const full = !isKidsScope();
  // Every tag gets its chip and, for an administrator, a remove control. The kids tag used to be
  // filtered out of the chips and shown as a read-only badge, which is why it could not be removed.
  const shownTags=(w.tags||[]).filter(t=>full||t.name!=='kids');
  const isKidsTagged=(w.tags||[]).some(t=>t.name==='kids');

  $('#view').innerHTML=`<div class="page-head"><div><button class="button ghost compact" data-route="library">← Library</button></div><div class="page-actions">${full?`<button class="button ghost secondary" id="open-studio" data-route="library/${escAttr(w.id)}/studio">Metadata Studio</button><button class="button secondary" id="refresh-work">Refresh metadata</button>`:''}</div></div>
  <div class="detail-hero"><div class="detail-cover">${coverHtml(w)}</div><div class="detail-meta">${seriesLinks?`<div class="series-links" id="series-links" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">${seriesLinks}</div>`:'<div class="muted small">Standalone</div>'}<h1>${e(w.title)}</h1><div class="byline">${e(author||'Unknown author')} ${w.first_publish_date?' · '+e(w.first_publish_date):''}</div>${(w.narrators||[]).length?`<div class="byline">Narrated by ${w.narrators.map(n=>`<a class="narrator-link" href="#narrators/${escAttr(n.id)}">${e(n.name)}</a>`).join(', ')}</div>`:''}
    <div style="margin-top:11px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">${metadataBadge(w)} ${monitoredBadgeHtml(w,acquisitionOn(state.me))}${isKidsTagged&&full?`<span class="badge good" title="${escAttr(kidsDecisionTitle(w))}">✓ Kids shelf${w.kids_decision?' · '+(w.kids_decision.via==='auto_approved'?`auto-approved (${w.kids_decision.confidence??'?'}, ${e(w.kids_decision.model||'model unknown')})`:w.kids_decision.via==='proposal_approved'?'classifier, approved':'manual'):''}</span>`:w.kids_decision?.state==='off'?`<span class="badge" title="${escAttr(kidsDecisionTitle(w))}">Kids shelf: excluded by hand</span>`:''}</div>
    ${kidsEvidenceHtml(w)}
    <div class="description">${e(w.description||'No description available.')}</div>
    <div class="detail-actions">
      <div class="field-inline"><label>Your rating</label><span id="rating-widget">${starWidget(w.id,w.rating)}</span></div>
      <div class="field-inline"><label>Reading status</label><select class="select compact" id="read-status">${READ_STATES.map(([v,l])=>`<option value="${v}" ${w.read_status===v||(!w.read_status&&!v)?'selected':''}>${l}</option>`).join('')}</select></div>
    </div>
    <div class="tags-row" id="tags-row">${shownTags.map(t=>`<span class="tag-chip ${t.kind==='system'?'system':''}" data-tag="${escAttr(t.id)}" data-tag-name="${escAttr(t.name)}" ${t.name==='kids'?`title="${escAttr(kidsDecisionTitle(w))}"`:''}>${e(tagLabel(t))}${full?` <button class="tag-remove" data-tag="${escAttr(t.id)}" data-name="${escAttr(t.name)}" aria-label="Remove tag ${escAttr(tagLabel(t))} from this book" title="Remove ${escAttr(tagLabel(t))}">×<span class="tag-remove-text"> Remove</span></button>`:''}</span>`).join('')}${full&&!isKidsTagged?'<button class="button ghost compact" id="add-kids-shelf" title="Kid accounts will be able to see and open this book">Add to kids shelf</button>':''}${full?'<button class="button ghost compact" id="add-tag">+ Tag</button>':''}</div>
  </div></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Media assets</h2><span class="muted small">One work, independent media states</span></div>${assetGroupsHtml(w,full)}</div>
  <div class="columns"><div class="panel"><div class="panel-head"><h2>Editions</h2></div><div class="table-wrap"><table><thead><tr><th>Edition</th><th>Publisher</th><th>Released</th><th>Language</th></tr></thead><tbody>${w.editions.length?w.editions.map(x=>`<tr><td>${e(x.title||w.title)}</td><td>${e(x.publisher||'—')}</td><td>${e(x.release_date||'—')}</td><td>${e(x.language||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="muted">No editions cached.</td></tr>'}</tbody></table></div></div><div class="panel"><div class="panel-head"><h2>${full?'Recent activity':'Recommendations'}</h2></div><div class="panel-body" id="side-panel-body">${full?activityHtml(w.history):'<div class="empty"><span class="spinner"></span></div>'}</div></div></div>
  ${full?`<div class="panel" style="margin-top:16px"><div class="panel-head"><h2>If you liked this…</h2></div><div class="panel-body" id="recs-body"><div class="empty"><span class="spinner"></span></div></div></div>`:''}`;

  wireRoutes();
  wireFileCopy();
  $$('.asset-search').forEach(b=>b.onclick=()=>manualSearch(id,b.dataset.kind,w.title));
  $$('.asset-policy').forEach(sel=>sel.onchange=async()=>{try{await api(`/api/v1/assets/${sel.dataset.asset}`,{method:'PATCH',body:{upgrade_policy:sel.value}});toast('Upgrade policy updated');renderWork(id)}catch(er){toast(er.message,'bad')}});
  $$('.asset-monitor').forEach(b=>b.onclick=async()=>{try{await api(`/api/v1/assets/${b.dataset.asset}`,{method:'PATCH',body:{monitored:b.dataset.next==='1'}});toast('Monitoring updated');renderWork(id)}catch(er){toast(er.message,'bad')}});
  $$('.asset-kindle').forEach(b=>b.onclick=()=>sendToKindle(id,b.dataset.asset,b));
  $$('.asset-kobo').forEach(b=>b.onclick=()=>downloadKepub(id,b.dataset.asset,b));
  $$('.asset-clear-progress').forEach(b=>b.onclick=()=>openClearProgress(id,b.dataset.asset,b.dataset.kind,()=>renderWork(id)));
  wireOfflineButtons();
  if(full){
    $('#refresh-work').onclick=()=>refreshWorkMetadata(id);
  }

  wireStarWidget($('#rating-widget .star-widget'), async n=>{
    try{ await api(`/api/v1/library/${id}/rating`,{method:'PUT',body:{rating:n}}); renderWork(id); }
    catch(er){ toast(er.message,'bad'); }
  });
  $('#read-status').onchange=async ev=>{
    try{ await api(`/api/v1/library/${id}/read-state`,{method:'PUT',body:{status:ev.target.value||null}}); toast('Reading status updated'); }
    catch(er){ toast(er.message,'bad'); ev.target.value=w.read_status||''; }
  };
  $$('.tag-remove').forEach(b=>b.onclick=async ev=>{
    ev.stopPropagation();
    if(b.dataset.name==='kids' && !confirm(`Take "${w.title}" off the kids shelf?\n\n${KIDS_REMOVE_EFFECT}`)) return;
    try{ await api(`/api/v1/library/${id}/tags/${b.dataset.tag}`,{method:'DELETE'}); toast(b.dataset.name==='kids'?'Removed from the kids shelf':'Tag removed'); renderWork(id); }catch(er){ toast(er.message,'bad'); }
  });
  $('#add-kids-shelf')?.addEventListener('click',async()=>{
    if(!confirm(`Put "${w.title}" on the kids shelf? Every kids account will be able to see and open it.`)) return;
    try{
      const kidsTag=(await api('/api/v1/tags')).items.find(t=>t.name==='kids');
      await api(`/api/v1/library/${id}/tags`,{method:'POST',body:{tag_id:kidsTag.id}}); toast('Added to the kids shelf'); renderWork(id);
    }catch(er){ toast(er.message,'bad'); }
  });
  $('#add-tag')?.addEventListener('click',()=>addTagDialog(id));

  if(full){
    api(`/api/v1/library/${id}/recommendations`).then(r=>{ const el=$('#recs-body'); if(el) el.innerHTML=recommendationsHtml(r); }).catch(()=>{ const el=$('#recs-body'); if(el) el.innerHTML='<div class="muted small">Recommendations unavailable right now.</div>'; });
  } else {
    api(`/api/v1/library/${id}/recommendations`).then(r=>{ const el=$('#side-panel-body'); if(el) el.innerHTML=recommendationsHtml(r); }).catch(()=>{});
  }
}

// Metadata Studio (Part XXI): a routed workspace (#library/<id>/studio[/<tab>]), not a
// modal. The Work tab edits each field; Edition and File list theirs read-only.
const STUDIO_TABS=[
  ['work','Work','Select a field to edit.'],
  ['edition','Edition','Edition metadata (read-only)'],
  ['file','File','File metadata (read-only)'],
];
// Part XXI's Work metadata list. `api` is the P4-05 field endpoint for the fields P4-04
// records observations for; the others have no observation trail, so they are edited
// through PATCH /library/<id>, or shown read-only where nothing can write them yet.
const STUDIO_WORK_FIELDS=[
  {key:'title',label:'Title',api:'title'},
  {key:'subtitle',label:'Subtitle',api:'subtitle'},
  {key:'authors',label:'Authors'},
  {key:'contributors',label:'Contributors',readonly:'No edit endpoint for non-author contributors yet.'},
  {key:'description',label:'Description',api:'description',multiline:true},
  {key:'first_publish_date',label:'Original publication date',api:'first_publish_date'},
  {key:'primary_language',label:'Language',api:'primary_language'},
  {key:'subjects',label:'Genres / subjects',readonly:PRODUCT+' does not store genres or subjects yet.'},
  {key:'series',label:'Series'},
  {key:'series_position',label:'Series position'},
];
const studioTab=tab=>STUDIO_TABS.some(t=>t[0]===tab)?tab:'work';
const hasEvidence=data=>!!data?.observations?.length;

function studioFieldValue(w,f,data){
  if(data) return data.value??'';
  if(f.key==='authors') return (w.authors||[]).map(a=>a.name).join(', ');
  if(f.key==='series') return w.series[0]?.name||'';
  if(f.key==='series_position') return w.series[0]?.position??'';
  return '';
}

// Render contributors as a structured, role-labeled list grouped by role.
// Driven by P4-07 role data from work_contributors; falls back gracefully
// when authors or narrators arrays are absent.
function studioContributorsHtml(w){
  // Combine authors and narrators from the work-detail payload into a unified
  // role-labeled list.  w.contributors may be set by P4-07; otherwise fall back
  // to the separate authors / narrators arrays that _library_summary returns.
  const all=w.contributors
    ? w.contributors.map(c=>({name:c.name,role:c.role||'author'}))
    : [
        ...(w.authors||[]).map(a=>({name:a.name,role:'author'})),
        ...(w.narrators||[]).map(n=>({name:n.name,role:'narrator'})),
      ];
  if(!all.length) return '<div class="muted">\u2014</div>';
  // Group by role, preserving insertion order of roles
  const groups=[];
  const seen=new Set();
  for(const c of all){
    if(!seen.has(c.role)){ seen.add(c.role); groups.push(c.role); }
  }
  const items=groups.map(role=>{
    const members=all.filter(c=>c.role===role).map(c=>`<li>${e(c.name)} <span class="muted small">(${e(role)})</span></li>`).join('');
    return `<div class="studio-contributor-group"><strong>${e(role)}</strong><ul>${members}</ul></div>`;
  });
  return `<div class="studio-contributors">${items.join('')}</div>`;
}

// Lock/unlock, manual and restore go to the card's own buttons; one "Choose" per
// competing observation follows, rendered in the observation list below the card.
function studioFieldActions(w,f,data){
  const base=`/api/v1/library/${encodeURIComponent(w.id)}/metadata/${f.api}`;
  const run=(path,body,msg)=>async()=>{ await post(path,body); toast(msg); return renderStudio(w.id,'work'); };
  return [
    data.locked?{label:'Unlock',handler:run(`${base}/unlock`,{},`${f.label} unlocked`)}:{label:'Lock',handler:run(`${base}/lock`,{},`${f.label} locked`)},
    {label:'Manual value',handler:()=>$(`[data-studio-manual="${f.key}"]`)?.classList.toggle('hidden')},
    {label:'Restore automatic',kind:'ghost',handler:run(`${base}/restore-automatic`,{},`${f.label} restored`)},
    ...data.observations.filter(o=>!o.is_current).map(o=>({label:'Choose',handler:run(`${base}/choose`,{observation_id:o.id},`${f.label} updated`)})),
  ];
}

function studioFieldHtml(w,f,data){
  const value=studioFieldValue(w,f,data);
  const label=`<div class="studio-field-label">${e(f.label)}</div>`;
  const input=f.multiline?`<textarea class="textarea" name="value">${e(value)}</textarea>`:`<input class="input" name="value" value="${escAttr(value)}">`;
  if(hasEvidence(data)){
    const current=data.observations.find(o=>o.is_current), competing=data.observations.filter(o=>!o.is_current);
    const actions=studioFieldActions(w,f,data), card=actions.slice(0,3);
    const inferred={label:'Current value',title:value===''?'(empty)':value};
    const evidence={source:`Source: ${data.source||'unknown'}`,explanation:data.locked?`Locked${data.lock?.locked_by?' by '+data.lock.locked_by:''}`:'Unlocked',confidence:current?.confidence};
    const list=competing.length?competing.map((o,i)=>`<li class="studio-observation"><span>${e(o.value===null||o.value===''?'(empty)':o.value)} <span class="muted small">— ${e(o.source||'unknown')} · ${fmtDate(o.observed_at)}</span></span><button class="button compact secondary" data-action-index="${card.length+i}">Choose</button></li>`).join(''):'<li class="muted small">No competing observations.</li>';
    return `<div data-studio-field="${f.key}" class="studio-field">${label}${evidenceCard(inferred,evidence,card)}<div class="evidence-label muted small">Competing observations</div><ul class="studio-observations">${list}</ul>
      <form data-studio-manual="${f.key}" class="studio-manual hidden">${input}<div class="muted small">A manual value is locked so provider refreshes cannot overwrite it.</div><button class="button compact">Save manual value</button></form></div>`;
  }
  if(f.key==='contributors'){
    // Contributors render as a structured, role-labeled list (not a flat string).
    // Uses P4-07 role data from w.contributors; falls back gracefully when absent.
    return `<div data-studio-field="${f.key}" class="studio-field studio-readonly">${label}${studioContributorsHtml(w)}<div class="muted small">${e(f.readonly)}</div></div>`;
  }
  if(f.readonly) return `<div data-studio-field="${f.key}" class="studio-field studio-readonly">${label}<div>${e(value||'—')}</div><div class="muted small">${e(f.readonly)}</div></div>`;
  const help=f.api?'No observations recorded yet. Saving records a manual value and locks the field.':'This field has no observation trail yet. Saving edits it directly.';
  return `<form data-studio-field="${f.key}" class="studio-field studio-fallback">${label}${input}<div class="muted small">${help}</div><button class="button compact secondary">Save</button></form>`;
}

async function saveStudioField(w,f,value){
  const id=encodeURIComponent(w.id);
  if(f.api) await post(`/api/v1/library/${id}/metadata/${f.api}/manual`,{value});
  else if(f.key==='authors') await api(`/api/v1/library/${id}`,{method:'PATCH',body:{authors:value.split(',').map(s=>s.trim()).filter(Boolean)}});
  else {
    // Series and position edit the first membership; PATCH replaces the whole list, so
    // any further memberships are sent back unchanged.
    const series=w.series.map(s=>({name:s.name,position:s.position}));
    const first=series[0]||{name:'',position:null};
    if(f.key==='series') first.name=value.trim();
    else if(!first.name) throw new Error('Set a series before its position');
    else first.position=value.trim()||null;
    await api(`/api/v1/library/${id}`,{method:'PATCH',body:{series:first.name?[first,...series.slice(1)]:series.slice(1)}});
  }
  toast(`${f.label} saved`);
  return renderStudio(w.id,'work');
}

// Edition and File tabs list Part XXI's EDITION / FILE METADATA fields read-only, from the
// Work detail payload. Fields the payload does not carry yet render as "Not recorded"
// rather than being dropped, so the gap stays visible.
const NOT_RECORDED=Symbol('not recorded');
const fmtDuration=s=>{ s=Number(s||0); if(!s)return ''; const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return h?`${h}h ${m}m`:`${m}m ${s%60}s`; };
function studioFields(rows){
  const val=v=>v===NOT_RECORDED?'<span class="muted">Not recorded</span>':(v===null||v===undefined||v==='')?'<span class="muted">—</span>':e(v);
  return `<div class="table-wrap"><table><tbody>${rows.map(([label,v])=>`<tr data-studio-field="${escAttr(label)}"><th scope="row">${e(label)}</th><td>${val(v)}</td></tr>`).join('')}</tbody></table></div>`;
}
function studioIdentifiers(w,match){ return (w.identifiers||[]).filter(i=>match(String(i.namespace).toLowerCase())).map(i=>i.value).join(', '); }
// Note: evidence-card treatment for edition and file fields is follow-up work (not yet implemented).
function studioEditionHtml(w){
  if(!w.editions?.length) return '<div class="empty"><strong>No editions cached</strong>Refresh metadata to pull editions for this work.</div>';
  const preferred={[w.preferred_ebook_edition?.id]:'Preferred ebook',[w.preferred_audiobook_edition?.id]:'Preferred audiobook'};
  // The payload carries ISBN/ASIN on the work, not per edition; label as work-level to avoid
  // misleading the user into thinking these are per-edition identifiers.
  const isbn=studioIdentifiers(w,ns=>ns.startsWith('isbn')), asin=studioIdentifiers(w,ns=>ns==='asin');
  return w.editions.map(x=>{
    const bound=(w.assets||[]).filter(a=>a.edition_id===x.id).flatMap(a=>a.narrators||[]);
    const narrators=bound.length?bound:(x.duration_seconds?(w.narrators||[]).map(n=>n.name):[]);
    const badge=preferred[x.id]?` <span class="badge info">${preferred[x.id]}</span>`:'';
    return `<div class="panel-head"><h2>${e(x.title||w.title)}${badge}</h2></div>${studioFields([
      ['ISBN (work-level)',isbn],['ASIN (work-level)',asin],['Publisher',x.publisher],['Publication date',x.release_date],['Language',x.language],
      ['Page count',x.pages],['Narrator',narrators.join(', ')],['Audiobook duration',fmtDuration(x.duration_seconds)],
      ['Abridged/unabridged',x.abridged==null?'':x.abridged?'Abridged':'Unabridged'],['Edition cover',x.cover_url],
    ])}`;
  }).join('');
}
function studioFileHtml(w){
  const files=(w.assets||[]).flatMap(a=>(a.files||[]).map(f=>[a,f]));
  if(!files.length) return '<div class="empty"><strong>No files</strong>No file is associated with this work yet.</div>';
  return files.map(([a,f])=>`<div class="panel-head"><h2>${a.kind==='audiobook'?'Audiobook':'Ebook'} · ${e(String(f.path||f.name||'').split('/').pop())}</h2>${f.missing?'<span class="badge bad">Missing</span>':''}</div>${studioFields([
    ['Format',(f.container||a.format||'').toUpperCase()],['Codec',f.codec],['Bitrate',f.bitrate?`${Math.round(f.bitrate/1000)} kbps`:''],
    ['Sample rate',NOT_RECORDED],['Channels',NOT_RECORDED],['Duration',fmtDuration(f.duration_seconds)],['Chapter count',NOT_RECORDED],
    ['Path',f.path||''],['File size',f.size_bytes?fmtBytes(f.size_bytes):''],['Embedded metadata state',NOT_RECORDED],
  ])}`).join('');
}
function studioHtml(w,tab,fields){
  const active=studioTab(tab);
  const base=`library/${escAttr(w.id)}/studio`;
  const intro=e(STUDIO_TABS.find(t=>t[0]===active)[2]);
  const coverSection=active==='work'&&fields
    ?`<div class="studio-cover" data-studio-field="cover"><div class="studio-field-label">Cover</div><div class="cover-edit-row"><div class="cover-edit-thumb" id="studio-cover-thumb">${coverHtml(w)}</div><div class="cover-edit-controls"><div class="field-inline"><input class="input" id="studio-cover-url-input" placeholder="https://..."><button type="button" class="button secondary compact" id="studio-cover-fetch">Fetch</button></div><div class="field-inline"><input type="file" accept="image/*" id="studio-cover-file-input"><button type="button" class="button ghost compact" id="studio-cover-remove">Remove</button></div><div class="help">Paste a link to an image, or upload one. This replaces the cover shown here and is never overwritten by an automatic refresh.</div></div></div></div>`
    :'';
  const body=active==='work'&&fields
    ?`${coverSection}<p class="muted small">${intro}</p><div class="studio-fields">${STUDIO_WORK_FIELDS.map(f=>studioFieldHtml(w,f,fields[f.key])).join('')}</div>`:intro;
  const extra=active==='edition'?studioEditionHtml(w):active==='file'?studioFileHtml(w):'';
  return `<div class="page-head"><div><button class="button ghost compact" data-route="library/${escAttr(w.id)}">← ${e(w.title)}</button><h1>Metadata Studio</h1><p>${e(w.title)}</p></div></div>
  <div class="tabs studio-tabs" role="tablist">${STUDIO_TABS.map(([id,label])=>`<button class="tab${id===active?' active':''}" role="tab" aria-selected="${id===active}" data-studio-tab="${id}" data-route="${base}/${id}">${label}</button>`).join('')}</div>
  <div class="panel studio-panel" role="tabpanel" data-studio-panel="${active}"><div class="panel-body">${body}</div>${extra}</div>`;
}
async function loadStudioFields(id){
  const fields={};
  await Promise.all(STUDIO_WORK_FIELDS.filter(f=>f.api).map(async f=>{ fields[f.key]=await api(`/api/v1/library/${encodeURIComponent(id)}/metadata/${f.api}`); }));
  return fields;
}
function wireStudioWork(w,fields){
  const coverEl=$('[data-studio-field="cover"]');
  if(coverEl){
    // The cover endpoint caches for a day and the URL does not otherwise change
    // when its bytes do, so a cache-busting query param is the only way the
    // browser (and this Studio, without a full page reload) sees the new art.
    const refreshThumb=()=>{
      $('#studio-cover-thumb',coverEl).innerHTML=`<img class="cover-img" src="/api/v1/library/${encodeURIComponent(w.id)}/cover?t=${Date.now()}" alt="" data-img-fallback="remove">`;
    };
    const fetchBtn=$('#studio-cover-fetch',coverEl);
    if(fetchBtn){
      fetchBtn.onclick=async()=>{
        const url=$('#studio-cover-url-input',coverEl).value.trim();
        if(!url) return;
        fetchBtn.disabled=true;
        try{ await api(`/api/v1/library/${w.id}/cover`,{method:'PUT',body:{url}}); refreshThumb(); toast('Cover updated'); }
        catch(er){ toast(er.message,'bad'); }
        finally{ fetchBtn.disabled=false; }
      };
    }
    const fileInput=$('#studio-cover-file-input',coverEl);
    if(fileInput){
      fileInput.onchange=async ev=>{
        const file=ev.target.files[0];
        if(!file) return;
        try{
          const bytes=await file.arrayBuffer();
          await fetch(`/api/v1/library/${encodeURIComponent(w.id)}/cover/upload`,{
            method:'PUT', credentials:'same-origin', body:bytes,
            headers:{'Content-Type':file.type||'application/octet-stream','X-CSRF-Token':state.csrf},
          }).then(async r=>{ if(!r.ok) throw new Error((await r.json().catch(()=>({}))).detail||'Upload failed'); });
          refreshThumb(); toast('Cover updated');
        } catch(er){ toast(er.message,'bad'); }
      };
    }
    const removeBtn=$('#studio-cover-remove',coverEl);
    if(removeBtn){
      removeBtn.onclick=async()=>{
        try{ await api(`/api/v1/library/${w.id}/cover`,{method:'DELETE'}); refreshThumb(); toast('Cover removed'); }
        catch(er){ toast(er.message,'bad'); }
      };
    }
  }
  for(const f of STUDIO_WORK_FIELDS){
    const el=$(`[data-studio-field="${f.key}"]`);
    if(!el) continue;
    const observed=hasEvidence(fields[f.key]);
    if(observed) bindEvidenceActions(el,studioFieldActions(w,f,fields[f.key]));
    const form=observed?$(`[data-studio-manual="${f.key}"]`):f.readonly?null:el;
    if(form) form.onsubmit=ev=>{ ev.preventDefault(); return saveStudioField(w,f,$('[name=value]',form).value).catch(er=>toast(er.message,'bad')); };
  }
}
async function renderStudio(id,tab){
  setLoading('Metadata Studio');
  const w=await api(`/api/v1/library/${encodeURIComponent(id)}`);
  const fields=studioTab(tab)==='work'?await loadStudioFields(w.id):null;
  $('#view').innerHTML=studioHtml(w,tab,fields);
  wireRoutes();
  if(fields) wireStudioWork(w,fields);
}

function recommendationsHtml(r){
  if(!r.items?.length) return '<div class="muted small">Nothing similar found yet.</div>';
  return `<div class="rec-grid">${r.items.map(x=>{
    const inner=`<div class="rec-title">${e(x.title)}</div><div class="rec-author">${e((x.authors||[]).join(', ')||'Unknown author')}</div>${x.reason?`<div class="rec-reason">${e(x.reason)}</div>`:''}${!x.in_library?'<div class="rec-badge">Not in your library</div>':''}`;
    return x.in_library ? `<a class="rec-card" href="#library/${escAttr(x.work_id)}">${inner}</a>` : `<div class="rec-card muted">${inner}</div>`;
  }).join('')}</div>${r.algorithm==='llm'?'<div class="muted small" style="margin-top:8px">AI-suggested, based on this book</div>':''}`;
}

function addTagDialog(workId){
  api('/api/v1/tags').then(d=>{
    const options=d.items.filter(t=>t.kind!=='system').map(t=>`<option value="${escAttr(t.id)}">${e(t.name)}</option>`).join('');
    const m=modal('Add a tag',`<div class="field"><label>Existing tag</label><select class="select" id="tag-pick"><option value="">Choose…</option>${options}</select></div><div class="field"><label>Or create a new one</label><input class="input" id="tag-new" placeholder="e.g. cozy mystery"></div>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="tag-save">Add</button>'});
    $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
    $('#tag-save',m).onclick=async()=>{
      try{
        let tagId=$('#tag-pick',m).value;
        const newName=$('#tag-new',m).value.trim();
        if(!tagId && newName) tagId=(await api('/api/v1/tags',{method:'POST',body:{name:newName}})).id;
        if(!tagId) return toast('Pick or create a tag first','bad');
        await api(`/api/v1/library/${workId}/tags`,{method:'POST',body:{tag_id:tagId}});
        $('#modal-root').innerHTML=''; renderWork(workId);
      }catch(er){ toast(er.message,'bad'); }
    };
  });
}

async function sendToKindle(workId,assetId,button){
  if(!state.me.kindle_email){
    toast('Add a Kindle email address in your account settings first','warn');
    return;
  }
  button.disabled=true; const old=button.textContent; button.innerHTML='<span class="spinner"></span>';
  try{ const r=await api(`/api/v1/library/${workId}/send/kindle`,{method:'POST',body:{asset_id:assetId}}); toast(`Sent ${r.filename} to Kindle`); }
  catch(er){ toast(er.message,'bad'); }
  finally{ button.disabled=false; button.textContent=old; }
}
function downloadKepub(workId,assetId,button){
  const a=document.createElement('a');
  a.href=`/api/v1/library/${workId}/kobo/${assetId}`; a.download='';
  document.body.appendChild(a); a.click(); a.remove();
}

// "Refresh metadata" used to only explain that the feature lived somewhere else,
// and the global job it pointed at could not help: it re-synced works that already
// had an OpenReads id, which none of the imported library did. This actually
// resolves the work, and hands ambiguity back to the operator instead of guessing.
async function refreshWorkMetadata(id, metadataId){
  const button=$('#refresh-work');
  if(button){ button.disabled=true; button.innerHTML='<span class="spinner"></span> Refreshing'; }
  try {
    const r=await api(`/api/v1/library/${encodeURIComponent(id)}/refresh`,{method:'POST',body:{metadata_id:metadataId||null}});
    if(r.status==='linked'){
      toast(r.cover&&r.cover.status==='cached'?'Metadata and cover updated':'Metadata updated');
      $('#modal-root').innerHTML='';
      return renderWork(id);
    }
    if(r.status==='review') return metadataCandidateDialog(id,r);
    // Not a dead end: the same dialog lets you search for it yourself. Sending people
    // to Discover was wrong advice - Add there creates a *second* work rather than
    // linking this one, which would strand the files already attached to it.
    metadataCandidateDialog(id,{...r,candidates:[]});
  } catch(er){ toast(er.message,'bad'); }
  finally { if(button&&document.body.contains(button)){ button.disabled=false; button.textContent='Refresh metadata'; } }
}

function candidateRows(list){
  if(!list.length) return '<div class="empty" style="padding:24px"><strong>No matches</strong>Try a different wording, the series name, or an identifier.</div>';
  return `<div class="table-wrap"><table><tbody>${list.map(c=>{
    const authors=(c.authors||(c.contributors||[]).filter(x=>!x.role||x.role==='author').map(x=>x.name)||[]).filter(Boolean);
    const series=(c.series||[]).map(x=>x.name+(x.position?' #'+x.position:'')).join(', ');
    return `<tr><td><strong>${e(c.title||'Untitled')}</strong><div class="small muted">${e(authors.join(', ')||'Unknown author')}${c.first_publish_date?' · '+e(c.first_publish_date):''}${series?' · '+e(series):''}</div></td><td><button class="button compact pick-meta" data-id="${escAttr(c.id)}">Use this</button></td></tr>`;
  }).join('')}</tbody></table></div>`;
}

// The picker used to be a dead end: five suggestions, none of them right, and no way
// to look for anything else or to say "none of these".
function metadataCandidateDialog(id,result){
  const suggested=result.candidates||[];
  const seed=[result.title,(result.authors||[]).join(' ')].filter(Boolean).join(' ').trim();
  const ids=(result.identifiers||[]).map(i=>`${i.namespace}: ${i.value}`).join(' · ');
  const intro=suggested.length
    ? `OpenReads suggested ${suggested.length} match${suggested.length===1?'':'es'} but none confidently enough to pick automatically${result.confidence?` (confidence ${Math.round(result.confidence*100)}%)`:''}.`
    : `OpenReads found nothing matching this title automatically.`;
  const m=modal('Which book is this?',`
    <p class="muted small">${e(intro)} Search for it yourself below, or leave it unmatched.</p>
    ${ids?`<p class="muted small mono">${e(ids)}</p>`:''}
    <form id="meta-search-form" class="toolbar" style="margin:0 0 12px">
      <input id="meta-search-q" class="input grow" value="${escAttr(seed)}" placeholder="Title, author, series, ISBN…" autocomplete="off">
      <button class="button" type="submit">Search</button>
    </form>
    <div id="meta-results">${candidateRows(suggested)}</div>
    <div id="meta-more" style="margin-top:12px;text-align:center"></div>`,
    {wide:true, footer:'<button class="button ghost" id="meta-none">None of these — leave unmatched</button>'});

  const wire=()=>$$('.pick-meta',m).forEach(b=>b.onclick=()=>refreshWorkMetadata(id,b.dataset.id));
  wire();
  $('#meta-none',m).onclick=()=>{ $('#modal-root').innerHTML=''; toast('Left unmatched. It stays in the Review queue.','warn'); };

  let shown=[], lastQuery='';
  const runSearch=async(grow=false)=>{
    const q=grow?lastQuery:$('#meta-search-q',m).value.trim();
    if(!q) return;
    lastQuery=q;
    const out=$('#meta-results',m);
    if(!grow){ shown=[]; out.innerHTML='<div class="empty" style="padding:24px"><span class="spinner"></span><strong style="margin-top:10px">Searching OpenReads</strong></div>'; }
    try{
      const want=Math.min(shown.length+20,50);
      const d=await api(`/api/v1/discover?q=${encodeURIComponent(q)}&limit=${want}`);
      const items=d.items||[];
      const exhausted=items.length<=shown.length||items.length>=50;
      shown=items;
      out.innerHTML=candidateRows(items);
      wire();
      $('#meta-more',m).innerHTML = (items.length&&!exhausted) ? '<button class="button secondary compact" id="meta-load-more">More results</button>' : '';
      const b=$('#meta-load-more',m);
      if(b) b.onclick=()=>{ b.disabled=true; b.innerHTML='<span class="spinner"></span>'; runSearch(true); };
    }catch(er){ out.innerHTML=`<div class="warning-box"><strong>Search failed.</strong> ${e(er.message)}</div>`; }
  };
  $('#meta-search-form',m).onsubmit=ev=>{ ev.preventDefault(); runSearch(false); };
}
// ---- Book-page file/asset rendering: format chips, a one-line files summary and a compact table.
const AUDIO_TRACK_THRESHOLD=4;   // more files than this (or any audiobook) is shown as a summary, not rows
function fmtTotalTime(sec){
  sec=Math.round(Number(sec||0)); if(!sec) return '';
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;
  return h?`${h}h ${String(m).padStart(2,'0')}m`:m?`${m}m${s&&m<10?` ${String(s).padStart(2,'0')}s`:''}`:`${s}s`;
}
function fileBase(path){ return String(path||'').split('/').pop(); }
// Non-admins get no path: the server sends a display name ('Track 3') instead.
const fileLabel=f=>f.path?fileBase(f.path):(f.name||'File');
function assetFormats(a){
  if(a.formats&&a.formats.length) return a.formats;
  return a.format?[String(a.format).toLowerCase()]:[];
}
// Every distinct format present; the primary one (what Read/Listen opens first) is marked.
function formatChipsHtml(a){
  const fm=assetFormats(a); if(!fm.length) return '';
  const primary=a.primary_format||fm[0];
  return `<span class="fmt-chips" aria-label="Formats">${fm.map(f=>f===primary&&fm.length>1
    ?`<span class="fmt-chip primary" data-format="${escAttr(f)}" title="Primary: this is the format Read or Listen opens first">${e(f.toUpperCase())}</span>`
    :`<span class="fmt-chip${f===primary?' primary':''}" data-format="${escAttr(f)}">${e(f.toUpperCase())}</span>`).join('')}</span>`;
}
function filesSummaryText(a,files){
  const present=files.filter(f=>!f.missing);
  const bytes=present.reduce((n,f)=>n+Number(f.size_bytes||0),0);
  const dur=present.reduce((n,f)=>n+Number(f.duration_seconds||0),0);
  const parts=[`${present.length} file${present.length===1?'':'s'}`, assetFormats(a).map(x=>x.toUpperCase()).join(' + ')];
  if(dur) parts.push(fmtTotalTime(dur));
  if(bytes) parts.push(fmtBytes(bytes));
  return parts.filter(Boolean).join(' · ');
}
const copyButton=(f,full)=>full&&f.path?`<button class="button ghost compact file-copy" data-path="${escAttr(f.path)}" title="Copy the file path" aria-label="Copy path of ${escAttr(fileLabel(f))}">Copy path</button>`:'';
function filesHtml(a,files,full){
  if(!files.length) return 'No file associated yet.';
  const missing=files.filter(f=>f.missing).length;
  const warn=missing?` <span class="small" style="color:var(--bad)">${missing} missing</span>`:'';
  if(a.kind!=='audiobook' && files.length<=AUDIO_TRACK_THRESHOLD){
    return files.map(f=>`<div class="file-line" title="${full&&f.path?escAttr(f.path):escAttr(fileLabel(f))}">${f.missing?'⚠ ':''}<span class="file-name">${e(fileLabel(f))}</span> <span class="file-size">${fmtBytes(f.size_bytes)}</span>${copyButton(f,full)}</div>`).join('');
  }
  const hasDur=files.some(f=>f.duration_seconds);
  const rows=files.map((f,i)=>`<tr${f.missing?' class="file-missing"':''}><td class="num">${f.part_number!=null&&f.part_number!==''?e(f.part_number):i+1}</td><td class="file-name" title="${full&&f.path?escAttr(f.path):escAttr(fileLabel(f))}">${f.missing?'⚠ ':''}${e(fileLabel(f))}</td>${hasDur?`<td class="num">${e(fmtTotalTime(f.duration_seconds)||'—')}</td>`:''}<td class="num">${fmtBytes(f.size_bytes)}</td>${full?`<td>${copyButton(f,full)}</td>`:''}</tr>`).join('');
  return `<div class="files-summary" data-asset="${escAttr(a.id)}">${e(filesSummaryText(a,files))}${warn}</div>
    <details class="file-details"><summary><span class="show-label">Show files</span><span class="hide-label">Hide files</span></summary>
    <div class="file-table-wrap"><table class="file-table"><thead><tr><th class="num">#</th><th>Name</th>${hasDur?'<th class="num">Time</th>':''}<th class="num">Size</th>${full?'<th></th>':''}</tr></thead><tbody>${rows}</tbody></table></div></details>`;
}
// A derived copy sits under the same heading as its original instead of looking like a stray extra asset.
// An empty format-less slot beside an owned asset of the same kind is bookkeeping, not a second ebook: not listed.
function redundantPlaceholder(a,assets){ return a.status!=='owned'&&!a.format&&!a.derived_from_asset_id&&!(a.files||[]).length&&assets.some(o=>o!==a&&o.kind===a.kind&&o.status==='owned'); }
function assetGroupsHtml(w,full){
  const groups=[]; const byKind={};
  for(const a of w.assets.filter(x=>!redundantPlaceholder(x,w.assets))){ (byKind[a.kind]=byKind[a.kind]||(groups[groups.push({kind:a.kind,items:[]})-1])).items.push(a); }
  return groups.map(g=>g.items.length>1
    ?`<div class="asset-group" data-kind="${escAttr(g.kind)}"><div class="asset-group-head">${g.kind==='audiobook'?'♫ Audiobook':'▤ Ebook'} <span class="muted small">original and converted copy</span></div>${g.items.map(a=>assetRow(a,w,full)).join('')}</div>`
    :g.items.map(a=>assetRow(a,w,full)).join('')).join('');
}
function wireFileCopy(){
  $$('.file-copy').forEach(b=>b.onclick=async()=>{ try{ await navigator.clipboard.writeText(b.dataset.path); toast('Path copied'); }catch{ toast('Copy failed','bad'); } });
}
function assetRow(a,w,full=true){
  const files=a.files||[];
  // narrator_ids is index-aligned with narrators (null where the name has no
  // contributor row yet); pair them before dropping blanks so a link never
  // lands on the wrong person.
  const narrators=(a.narrators||[]).map((name,i)=>({name,id:(a.narrator_ids||[])[i]||null})).filter(n=>n.name);
  const owned=a.status==='owned' && files.some(f=>!f.missing);
  // Kindle accepts EPUB directly today; a Kobo device reads plain EPUB too, but only
  // a .kepub.epub-suffixed file gets Kobo's per-paragraph reading-position tracking -
  // see kepub.py. Both are delivery of a book already on the shelf, so a kids
  // account gets them too, unlike Search/Monitor which mutate acquisition state.
  const deviceButtons = owned && a.kind==='ebook' ? `
    <button class="button ghost compact asset-kindle" data-asset="${a.id}">Send to Kindle</button>
    ${(a.primary_format||a.format||'').toLowerCase()==='epub'?`<button class="button ghost compact asset-kobo" data-asset="${a.id}">Download for Kobo</button>`:''}` : '';
  // In-browser reading/listening: EPUB and PDF render in the page, audio streams to an
  // <audio> element. The button reads Resume when this account already has a position.
  const prog=(w.progress||[]).find(p=>p.asset_id===a.id);
  const readable = a.kind==='ebook' ? files.some(f=>!f.missing && f.readable) : files.some(f=>!f.missing && f.readable);
  const consumeButton = owned && readable ? `<button class="button compact asset-consume" data-route="library/${escAttr(w.id)}/read/${escAttr(a.id)}">${a.kind==='ebook'?(prog?'▶ Resume reading':'Read'):(prog?'▶ Resume listening':'Listen')}</button>` : '';
  // Only offered while there is a position to clear; it confirms first (openClearProgress)
  // because a misclick here would throw away hours of listening position.
  const clearButton = prog ? `<button class="button ghost compact asset-clear-progress" data-asset="${escAttr(a.id)}" data-kind="${a.kind}" title="Forget where you are in this ${a.kind==='ebook'?'book':'audiobook'}">Clear progress</button>` : '';
  const fullButtons = full ? assetAcqButtonsHtml(a, acquisitionOn(state.me)) : '';
  // "Keep offline" stores the file bytes the reader/player streams (the same
  // /api/v1/library/<work>/files/<id> URL renderConsume uses) in the installed app's
  // cache (sw.js), so Read/Listen works with no network. Only offered when a service
  // worker is running; the button text is resolved after render by wireOfflineButtons.
  // ONE control per asset (an audiobook can have dozens of track files): it keeps or drops all
  // of the asset's files together. Per-file detail stays in the file list.
  const keepFiles=files.filter(f=>!f.missing);
  const offlineButtons = owned && readable && window.bookdoePwa?.supported && keepFiles.length
    ? `<button class="button ghost compact asset-offline" data-asset="${escAttr(a.id)}" data-files="${keepFiles.length}" data-urls="${escAttr(JSON.stringify(keepFiles.map(f=>`/api/v1/library/${w.id}/files/${f.id}`)))}" data-name="${escAttr(w.title)}" title="${keepFiles.length>1?`Stores all ${keepFiles.length} files on this device`:'Stores the file on this device'}">Keep offline</button>`
    : '';
  const wantedNow=wantedLabel(a,acquisitionOn(state.me));
  return `<div class="asset-row${wantedNow?' asset-wanted':''}"><div class="asset-kind">${wantedNow?'Wanted: ':''}${a.kind==='audiobook'?'♫ Audiobook':'▤ Ebook'}${narrators.length?`<div class="small muted">Read by ${narrators.map(n=>n.id?`<a class="narrator-link" href="#narrators/${escAttr(n.id)}">${e(n.name)}</a>`:e(n.name)).join(', ')}</div>`:''}${prog&&prog.percent!=null?`<div class="small muted">${Math.round(prog.percent*100)}% ${a.kind==='ebook'?'read':'listened'}</div>`:''}</div><div class="asset-status"><span class="badge ${a.status==='owned'?'good':a.monitored?'warn':''}">${e(a.status)}</span>${formatChipsHtml(a)}${qualityBadge(a)}${derivedBadgeHtml(a)}${policyChip(a)}${full&&a.status==='owned'?`<div>${policySelector(a)}</div>`:''}</div><div class="files">${filesHtml(a,files,full)}</div><div class="asset-actions">${consumeButton}${clearButton}${fullButtons}${full?convertButtonHtml(a):''}${deviceButtons}${offlineButtons}</div></div>`}

// Clearing a reading/listening position is destructive and one tap away from Resume, so
// it goes through the same confirm-modal shape as cancelling a download. The server
// also drops a 'reading' status the reader set automatically (never one picked by hand).
function openClearProgress(workId, assetId, kind, onDone){
  const noun = kind==='ebook' ? 'reading' : 'listening';
  const m=modal('Clear progress',`<div class="warning-box"><strong>This forgets where you are in this ${kind==='ebook'?'book':'audiobook'}.</strong> The next time you open it you start from the beginning. The file itself is not touched.</div>`,
    {footer:`<button class="button ghost modal-cancel">Keep my place</button><button class="button danger" id="clear-progress-confirm">Clear ${noun} progress</button>`});
  $('.modal-cancel',m).onclick=closeModal;
  $('#clear-progress-confirm',m).onclick=async()=>{
    try{ await clearProgress(workId, assetId); toast(`${kind==='ebook'?'Reading':'Listening'} progress cleared`); closeModal(); if(onDone) onDone(); }
    catch(er){ toast(er.message,'bad'); }
  };
}
async function clearProgress(workId, assetId){
  return api(`/api/v1/library/${encodeURIComponent(workId)}/progress/${encodeURIComponent(assetId)}`,{method:'DELETE'});
}

// Resolves each "Keep offline" button to its real state (stored or not) and toggles the
// file in the service worker's per-user content cache on click.
function wireOfflineButtons(){
  const pwa=window.bookdoePwa; if(!pwa||!pwa.supported) return;
  $$('.asset-offline').forEach(async b=>{
    const urls=JSON.parse(b.dataset.urls||'[]'); const name=b.dataset.name||'this book';
    const paint=cached=>{ b.dataset.cached=cached?'1':'0'; b.textContent=cached?'✓ Kept offline':'Keep offline'; b.classList.toggle('secondary',cached); };
    const allCached=async()=>{ for(const u of urls) if(!(await pwa.isCached(u))) return false; return true; };
    try{ paint(await allCached()); }catch{ paint(false); }
    b.onclick=async()=>{
      b.disabled=true;
      try{
        if(b.dataset.cached==='1'){ b.textContent='Removing…'; for(const u of urls) await pwa.forgetContent(u); paint(false); toast(`Removed offline copy of ${name}`); }
        else {
          for(let i=0;i<urls.length;i++){
            b.textContent=urls.length>1?`Saving ${i+1}/${urls.length}…`:'Saving…';
            if(await pwa.isCached(urls[i]).catch(()=>false)) continue;
            const r=await pwa.cacheContent(urls[i]); if(!r?.ok) throw new Error(r?.error||'Could not store the file');
          }
          paint(true); toast(`${name} is available offline`);
        }
      }catch(er){ paint(await allCached().catch(()=>false)); toast(er.message,'bad'); }
      finally{ b.disabled=false; }
    };
  });
}

async function manualSearch(workId,kind,title){
  const m=modal(`Search ${kind}: ${title}`,`<div class="empty"><span class="spinner"></span><strong style="margin-top:10px">Querying indexers</strong>${e(PRODUCT)} is running identifier and title/author query plans.</div>`,{wide:true});
  try{const data=await api(`/api/v1/library/${workId}/search/${kind}`,{method:'POST',body:{}}); $('.modal-body',m).innerHTML=releaseTable(data); $$('.release-grab',m).forEach(b=>b.onclick=()=>grabRelease(b.dataset.id,false)); $$('.release-force',m).forEach(b=>b.onclick=()=>grabRelease(b.dataset.id,true)); $$('.score-toggle',m).forEach(b=>b.onclick=()=>{const x=$(`#score-${CSS.escape(b.dataset.id)}`,m);x.classList.toggle('hidden')});}catch(er){$('.modal-body',m).innerHTML=`<div class="warning-box"><strong>Search failed.</strong> ${e(er.message)}</div>`;}
}
function releaseTable(data){if(!data.items.length)return `<div class="empty"><strong>No releases found</strong>${data.errors.length?e(data.errors.map(x=>x.indexer+': '+x.error).join('; ')):'Try a different indexer/category configuration.'}</div>`;return `${data.errors.length?`<div class="warning-box" style="margin-bottom:12px"><strong>Some indexers failed.</strong> ${e(data.errors.map(x=>x.indexer).join(', '))}</div>`:''}<div class="table-wrap"><table><thead><tr><th>Release</th><th>Indexer</th><th>Format</th><th>Size</th><th>Peers</th><th>Score</th><th></th></tr></thead><tbody>${data.items.map(r=>`<tr class="release-row ${r.rejected?'rejected':''}"><td><strong>${e(r.title)}</strong>${r.rejections?.length?`<div class="small" style="color:var(--bad)">${e(r.rejections.join(' · '))}</div>`:''}<div id="score-${e(r.id)}" class="score-pop hidden">${r.score_components.map(c=>`<div class="score-line"><span>${e(c.label)}${c.evidence?' · '+e(c.evidence):''}</span><b>${c.score>0?'+':''}${c.score}</b></div>`).join('')}</div></td><td>${e(r.indexer_name||'')}</td><td>${e((r.parsed?.format||'—').toUpperCase())}</td><td>${fmtBytes(r.size)}</td><td>${r.seeders??'—'}</td><td><button class="button ghost compact score-toggle" data-id="${r.id}"><span class="score ${r.score>=80?'good':r.score>=40?'warn':'bad'}">${r.score}</span></button></td><td>${r.rejected?`<button class="button ghost compact release-force" data-id="${r.id}">Force</button>`:`<button class="button compact release-grab" data-id="${r.id}">Grab</button>`}</td></tr>`).join('')}</tbody></table></div>`}
async function grabRelease(id,force){try{const x=await api(`/api/v1/releases/${id}/grab`,{method:'POST',body:{force}});toast(`Sent to ${x.client}`);$('#modal-root').innerHTML='';location.hash='queue';}catch(er){toast(er.message,'bad')}}


const AUTHORS_PAGE=200;
function authorRows(items){return items.map(a=>`<tr class="clickable author-row" data-id="${a.id}"><td><strong>${e(a.name)}</strong></td><td>${a.work_count||0}</td><td title="Books with at least one owned file (a book with an ebook and an audiobook counts once)">${a.owned_works||0}</td><td>${a.owned_audio||0}</td><td>${a.owned_ebooks||0}</td><td>${a.policy?`<span class="badge info">${e(a.policy)}</span> ${a.monitor_audiobook?'<span class="badge good">audio</span>':''} ${a.monitor_ebook?'<span class="badge good">ebook</span>':''}`:'<span class="badge">not monitored</span>'}</td><td>${fmtDate(a.last_refresh_at)}</td></tr>`).join('')}
async function renderAuthors(){
  setLoading('Authors');
  const st={q:'',items:[],total:0,seq:0};
  const load=async(append)=>{
    const seq=++st.seq;
    const d=await api(`/api/v1/authors?limit=${AUTHORS_PAGE}&offset=${append?st.items.length:0}${st.q?'&q='+encodeURIComponent(st.q):''}`);
    if(seq!==st.seq) return; // a newer keystroke superseded this response
    st.items=append?st.items.concat(d.items):d.items; st.total=d.total??st.items.length;
    paint();
  };
  const paint=()=>{
    $('#authors-count').textContent=`${fmtNum(st.total)} author${st.total===1?'':'s'} — monitor an author once; choose which media types ${e(PRODUCT)} should pursue.`;
    $('#authors-body').innerHTML=st.items.length?authorRows(st.items):`<tr><td colspan="7"><div class="empty"><strong>No authors found</strong>${st.q?`Nothing matches \u201c${e(st.q)}\u201d. <button type="button" class="button secondary compact" id="authors-clear">Clear filter</button>`:'Add works or import your Readarr libraries.'}</div></td></tr>`;
    $('#authors-clear')?.addEventListener('click',()=>{ const i=$('#author-filter'); i.value=''; st.q=''; load(false); });
    $('#authors-more').classList.toggle('hidden',st.items.length>=st.total);
    $$('.author-row').forEach(x=>x.onclick=()=>location.hash=`authors/${x.dataset.id}`);
  };
  $('#view').innerHTML=`<div class="page-head"><div><h1>Authors</h1><p id="authors-count"></p></div></div><div class="toolbar"><input class="input grow" id="author-filter" type="search" placeholder="Filter authors…"></div><div class="panel table-wrap"><table><thead><tr><th>Author</th><th>Works</th><th>Owned books</th><th>Owned audio</th><th>Owned ebooks</th><th>Monitoring</th><th>Last refresh</th></tr></thead><tbody id="authors-body"></tbody></table></div><div style="margin-top:12px"><button class="button secondary hidden" id="authors-more">Load more</button></div>`;
  await load(false);
  // Filtering is server-side now: the list is paged, so a browser-side filter would
  // only search the pages already loaded.
  let timer; $('#author-filter').oninput=ev=>{clearTimeout(timer);const v=ev.target.value.trim();timer=setTimeout(()=>{st.q=v;load(false)},250)};
  $('#authors-more').onclick=()=>load(true);
}
async function renderAuthor(id){
  setLoading('Author'); const a=await api(`/api/v1/authors/${id}`);
  $('#view').innerHTML=`<div class="page-head"><div><button class="button ghost compact" data-route="authors">← Authors</button><h1 style="margin-top:12px">${e(a.name)}</h1><p>${a.works.length} known works${a.last_refresh_at?' · refreshed '+fmtDate(a.last_refresh_at):''}</p></div><div class="page-actions">${acquisitionOn(state.me)?'<button class="button secondary" id="author-monitor">Monitoring</button>':''}${a.policy&&a.policy!=='none'?'<button class="button" id="author-refresh">Refresh bibliography</button>':''}</div></div>${a.last_error?`<div class="warning-box" style="margin-bottom:14px"><strong>Last refresh failed.</strong> ${e(a.last_error)}</div>`:''}<div class="library-grid">${a.works.map(w=>{const ww={...w,authors:[{name:a.name}],series:[],assets:w.assets||[]};return bookCard(ww)}).join('')}</div>`;
  wireRoutes();wireBookCards();const am=$('#author-monitor'); if(am) am.onclick=()=>authorMonitorDialog(a);const r=$('#author-refresh');if(r)r.onclick=async()=>{r.disabled=true;r.innerHTML='<span class="spinner"></span> Refreshing';try{const x=await api(`/api/v1/authors/${id}/refresh`,{method:'POST',body:{}});toast(`Discovered ${x.discovered}; added ${x.added}`);renderAuthor(id)}catch(er){toast(er.message,'bad');r.disabled=false;r.textContent='Refresh bibliography'}};
}
async function renderNarrator(id){
  setLoading('Narrator'); const a=await api(`/api/v1/authors/${encodeURIComponent(id)}?role=narrator`);
  $('#view').innerHTML=`<div class="page-head"><div><button class="button ghost compact" data-route="library">← Library</button><h1 style="margin-top:12px">${e(a.name)}</h1><p>Narrated ${a.works.length} ${a.works.length===1?'book':'books'}</p></div></div><div class="library-grid">${a.works.map(w=>bookCard({...w,authors:[],series:[],assets:w.assets||[]})).join('')}</div>`;
  wireRoutes();wireBookCards();
}

function authorMonitorDialog(a){const m=modal(`Monitor ${a.name}`,`<form id="author-monitor-form" class="form-grid"><div class="field full"><label>Policy</label><select class="select" name="policy"><option value="future" ${a.policy==='future'?'selected':''}>Future / newly discovered works</option><option value="all" ${a.policy==='all'?'selected':''}>All known works</option><option value="latest" ${a.policy==='latest'?'selected':''}>Recent works</option><option value="none" ${a.policy==='none'?'selected':''}>None</option></select><div class="help">Future is conservative on first setup, then monitors new works discovered on later refreshes.</div></div><div class="field"><label><input type="checkbox" name="audiobook" ${a.monitor_audiobook!==0?'checked':''}> Audiobooks</label></div><div class="field"><label><input type="checkbox" name="ebook" ${a.monitor_ebook?'checked':''}> Ebooks</label></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="author-monitor-save">Save</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#author-monitor-save',m).onclick=async()=>{const f=new FormData($('#author-monitor-form'));try{await api(`/api/v1/authors/${a.id}/monitor`,{method:'PUT',body:{policy:f.get('policy'),audiobook:f.has('audiobook'),ebook:f.has('ebook')}});toast('Author monitoring updated');$('#modal-root').innerHTML='';renderAuthor(a.id)}catch(er){toast(er.message,'bad')}}}
const SERIES_PAGE=100;
async function renderSeries(){
  setLoading('Series');
  const full=!isKidsScope();
  const st={q:'',uncertain:false,items:[],total:0,seq:0};
  const card=sv=>{
    const pct = sv.work_count ? Math.round(100*sv.owned_count/sv.work_count) : 0;
    return `<div class="series-card" data-id="${escAttr(sv.id)}"><h3>${e(sv.name)}</h3><div class="muted small">${fmtNum(sv.work_count)} book${sv.work_count===1?'':'s'} · ${fmtNum(sv.owned_count)} owned${sv.partial_count?` · ${fmtNum(sv.partial_count)} partly`:''}</div><div class="series-progress"><i style="width:${pct}%"></i></div>${sv.gap_count?`<div class="small series-gap-badge">⚠ ${sv.gap_count} gap${sv.gap_count===1?'':'s'} in the numbering</div>`:''}${sv.policy&&sv.policy!=='none'?'<span class="badge info" style="margin-top:6px">Monitored</span>':''}</div>`;
  };
  const load=async(append)=>{
    const seq=++st.seq;
    const d=await api(`/api/v1/series?limit=${SERIES_PAGE}&offset=${append?st.items.length:0}${st.uncertain?'&include_uncertain=true':''}${st.q?'&q='+encodeURIComponent(st.q):''}`);
    if(seq!==st.seq) return;
    st.items=append?st.items.concat(d.items):d.items; st.total=d.total??st.items.length;
    $('#series-count').textContent=`${fmtNum(st.total)} series across your library.`;
    $('#series-results').innerHTML=st.items.length?`<div class="series-grid">${st.items.map(card).join('')}</div>`:'<div class="panel"><div class="empty"><strong>No series found</strong>Series appear here once a book in your library is linked to one. Single-book series with no numbering are hidden unless you tick "Show uncertain".'+(st.q||st.uncertain?' <div style="margin-top:12px"><button type="button" class="button secondary compact" id="series-clear">Clear filters</button></div>':'')+'</div></div>';
    $('#series-clear')?.addEventListener('click',()=>{ const i=$('#series-search'), u=$('#series-uncertain'); i.value=''; st.q=''; if(u) u.checked=false; st.uncertain=false; load(false); });
    $('#series-more').classList.toggle('hidden',st.items.length>=st.total);
    $$('.series-card').forEach(c=>c.onclick=()=>location.hash=`series/${c.dataset.id}`);
  };
  $('#view').innerHTML=`<div class="page-head"><div><h1>Series</h1><p id="series-count"></p></div>${full&&state.me?.is_admin?'<div class="page-actions"><button class="button secondary" id="series-cleanup-open">Clean up series</button></div>':''}</div><div id="series-cleanup-panel"></div><div class="toolbar"><input class="input grow" id="series-search" type="search" placeholder="Filter series…">${full?'<label class="small"><input type="checkbox" id="series-uncertain"> Show uncertain</label>':''}</div><div id="series-results"></div><div style="margin-top:12px"><button class="button secondary hidden" id="series-more">Load more</button></div>`;
  await load(false);
  let timer; $('#series-search').oninput=ev=>{clearTimeout(timer);const v=ev.target.value.trim();timer=setTimeout(()=>{st.q=v;load(false)},250)};
  $('#series-uncertain')?.addEventListener('change',ev=>{st.uncertain=ev.target.checked;load(false)});
  $('#series-more').onclick=()=>load(true);
  $('#series-cleanup-open')?.addEventListener('click',()=>seriesCleanupPanel($('#series-cleanup-panel'),()=>load(false)));
}

const SERIES_CLEANUP_LABELS={rename:'Series renamed',merge:'Books moved into another series',split:'Books split into several series',drop_junk:'Books dropped (not a series)',clear_position:'Bad numbers cleared or fixed',fill_position:'Numbers filled in',delete_empty_series:'Empty series removed',monitor_repointed:'Followed series re-pointed',skipped_manual:'Left alone (edited by hand)'};
function seriesCleanupSample(x){const b=x.before||{},a=x.after;const side=o=>!o?'<em>removed</em>':Array.isArray(o.series)?o.series.map(e).join(' + '):`${e(o.series)}${o.position!=null&&o.position!==''?` #${e(o.position)}`:''}${o.books?` (${o.books} books)`:''}`;return `<li>${b.work?`<span class="muted">${e(b.work)}: </span>`:''}${side(b)} &rarr; ${side(a)}</li>`}
function seriesCleanupPlanHtml(plan){const t=plan.totals||{};return `<p class="small">${fmtNum(t.series_before)} series and ${fmtNum(t.memberships_before)} memberships now; about ${fmtNum(t.series_after)} series and ${fmtNum(t.memberships_after)} memberships after. No book or file is ever deleted.</p>${Object.entries(SERIES_CLEANUP_LABELS).map(([k,label])=>{const c=plan.categories?.[k];if(!c||!c.count)return '';return `<details class="small" style="margin:6px 0"><summary><strong>${fmtNum(c.count)}</strong> ${e(label)}</summary><ul>${c.samples.map(seriesCleanupSample).join('')}</ul>${c.count>c.samples.length?`<div class="muted">…and ${fmtNum(c.count-c.samples.length)} more</div>`:''}</details>`}).join('')||'<p class="muted small">Nothing to clean up.</p>'}`}
async function seriesCleanupPanel(el,onDone){
  el.innerHTML='<div class="panel" style="margin-bottom:12px"><div class="panel-head"><h2>Clean up series</h2></div><div class="panel-body" id="sc-body">Checking…</div></div>';
  const body=$('#sc-body',el);
  let cfg=await api('/api/v1/admin/series/settings');
  const draw=async(d)=>{
    const changes=d&&d.plan&&d.plan.categories?Object.values(d.plan.categories).reduce((n,c)=>n+c.count,0):0;
    body.innerHTML=`<p class="muted small">Re-reads every series name with the current rules: splits strings that hold several series, trims stray brackets, drops publisher lines and catalog numbers. Preview first; nothing changes until you apply, and the last cleanup can be undone.</p><div id="sc-plan">${seriesCleanupPlanHtml(d.plan)}</div><div class="toolbar"><button class="button" id="sc-apply" ${changes?'':'disabled'}>Apply cleanup…</button><button class="button ghost secondary" id="sc-refresh">Preview again</button>${d.last?`<button class="button ghost secondary" id="sc-undo">Undo last cleanup</button>`:''}<span id="sc-msg" class="small muted"></span></div>
    <h3 style="margin-top:18px">Ignored names</h3><p class="muted small">One per line. A name here is never treated as a series (add <code>*</code> at the end to match a prefix). Publisher lines such as Knaur, Goldmann, rororo, Mills &amp; Boon and Harlequin are already built in.</p><textarea class="input" id="sc-ignored" rows="5" style="width:100%">${e((cfg.ignored_names||[]).join('\n'))}</textarea><div class="toolbar"><label class="small">Largest believable book number <input class="input" id="sc-max" type="number" min="1" max="999" value="${escAttr(cfg.max_position)}" style="width:90px"></label><button class="button secondary" id="sc-save">Save</button></div>`;
    $('#sc-refresh').onclick=async()=>{$('#sc-msg').textContent='Checking…';try{await draw(await api('/api/v1/admin/series/cleanup'))}catch(er){toast(er.message,'bad')}};
    $('#sc-apply').onclick=async()=>{if(!confirm('Apply this series cleanup? It changes series names and numbers (never books or files) and can be undone afterwards.'))return;$('#sc-apply').disabled=true;try{const r=await api('/api/v1/admin/series/cleanup',{method:'POST',body:{confirm:true}});toast('Series cleanup applied');await draw(await api('/api/v1/admin/series/cleanup'));onDone&&onDone()}catch(er){toast(er.message,'bad');$('#sc-apply').disabled=false}};
    $('#sc-undo')?.addEventListener('click',async()=>{if(!confirm('Undo the last series cleanup? Series names, numbers and followed series go back as they were.'))return;try{await api('/api/v1/admin/series/cleanup/undo',{method:'POST',body:{}});toast('Cleanup undone');await draw(await api('/api/v1/admin/series/cleanup'));onDone&&onDone()}catch(er){toast(er.message,'bad')}});
    $('#sc-save').onclick=async()=>{try{cfg=await api('/api/v1/admin/series/settings',{method:'PUT',body:{ignored_names:$('#sc-ignored').value.split('\n').map(x=>x.trim()).filter(Boolean),max_position:Number($('#sc-max').value)||200}});toast('Saved');await draw(await api('/api/v1/admin/series/cleanup'))}catch(er){toast(er.message,'bad')}};
  };
  try{await draw(await api('/api/v1/admin/series/cleanup'))}catch(er){body.textContent=er.message}
}

async function renderSeriesDetail(id){
  setLoading('Series');
  const s=await api(`/api/v1/series/${encodeURIComponent(id)}`);
  const full=!isKidsScope();
  const posCap=s.max_position||200; // catalog numbers (Knaur 60291) are not places in the series
  const positions=s.works.map(w=>Number(w.position)).filter(n=>!isNaN(n)&&Number.isInteger(n)&&n>=0&&n<=posCap);
  const maxPos = positions.length ? Math.max(...positions) : 0;
  const haveAt = new Set(positions);
  const slots = maxPos && full ? Array.from({length:maxPos},(_,i)=>i+1).map(n=>`<div class="series-slot ${haveAt.has(n)?'owned':(n<=maxPos?'gap':'')}">${n}</div>`).join('') : '';
  $('#view').innerHTML=`<div class="page-head"><div><button class="button ghost compact" data-route="series">← Series</button><h1 style="margin-top:12px">${e(s.name)}</h1><p>${fmtNum(s.works.length)} book${s.works.length===1?'':'s'} · ${fmtNum(s.owned_count||0)} owned${full&&s.gaps.length?` · ${s.gaps.length} possible gap${s.gaps.length===1?'':'s'} in the numbering`:''}</p></div>${full?`<div class="page-actions"><button class="button ghost secondary" id="series-discover">Check for more</button>${acquisitionOn(state.me)?'<button class="button secondary" id="series-monitor">Monitoring</button>':''}</div>`:''}</div>
  ${slots?`<div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Numbering</h2></div><div class="panel-body"><div class="series-position-list">${slots}</div><p class="muted small" style="margin-top:10px">This only reflects what your library and its linked metadata already know about - it cannot see a book neither has ever mentioned.</p></div></div>`:''}
  ${full?`<div id="series-bulk-bar"></div><div class="panel table-wrap" style="margin-bottom:16px"><table id="series-books"><thead><tr><th style="width:36px"><input type="checkbox" id="series-select-all" aria-label="Select all books in this series"></th><th>#</th><th>Book</th><th>Kids shelf</th><th>Tags</th></tr></thead><tbody>${s.works.map(seriesRow).join('')}</tbody></table></div>`:`<div class="library-grid">${s.works.map(bookCard).join('')}</div>`}
  <div id="series-discover-results"></div>`;
  wireRoutes(); wireBookCards();
  if(full){
    wireSeriesBulk(s);
    const sm=$('#series-monitor'); if(sm) sm.onclick=()=>seriesMonitorDialog(s);
    $('#series-discover').onclick=async()=>{
      const btn=$('#series-discover'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Checking';
      try{
        const r=await api(`/api/v1/series/${id}/discover-more`,{method:'POST',body:{}});
        const box=$('#series-discover-results');
        if(!r.candidates?.length){ box.innerHTML='<div class="panel" style="margin-top:16px"><div class="empty"><strong>Nothing new found</strong>Nothing matching this series turned up that you don\'t already have.</div></div>'; }
        else {
          box.innerHTML=`<div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Possible additions</h2><span class="muted small">Not confident matches - review before adding</span></div><div class="table-wrap"><table><thead><tr><th>Work</th><th>Author</th><th></th></tr></thead><tbody>${r.candidates.map(c=>`<tr><td><strong>${e(c.title)}</strong></td><td>${e((c.contributors||[]).filter(x=>x.role==='author').map(x=>x.name).join(', '))}</td><td><button class="button compact series-add-candidate" data-id="${escAttr(c.id)}">Add</button></td></tr>`).join('')}</tbody></table></div></div>`;
          $$('.series-add-candidate',box).forEach(b=>b.onclick=async()=>{ try{ const w=await api('/api/v1/library',{method:'POST',body:{metadata_id:b.dataset.id,monitored:acquisitionOn(state.me)}}); toast(`Added ${w.title}`); location.hash=`library/${w.id}`; }catch(er){ toast(er.message,'bad'); } });
        }
      }catch(er){ toast(er.message,'bad'); }
      finally{ const btn=$('#series-discover'); if(btn){ btn.disabled=false; btn.textContent='Check for more'; } }
    };
  }
}

// One row of the admin series table: selectable, with the shelf and tag state a bulk
// action changes, so its effect is visible without leaving the page.
function seriesRow(w){
  const tags=(w.tags||[]).filter(t=>t.name!=='kids');
  return `<tr data-id="${escAttr(w.id)}"><td><input type="checkbox" class="series-pick" value="${escAttr(w.id)}" aria-label="Select ${escAttr(w.title)}"></td><td>${e(w.position||'')}</td><td><a href="#library/${escAttr(w.id)}"><strong>${e(w.title)}</strong></a><div class="small muted">${e((w.authors||[]).map(a=>a.name).join(', '))}</div></td><td><span class="badge ${w.on_kids_shelf?'good':''} series-kids-chip" data-kids="${w.on_kids_shelf?'yes':'no'}">${w.on_kids_shelf?'Kids shelf: yes':'Kids shelf: no'}</span></td><td class="series-tags">${tags.map(t=>`<span class="tag-chip ${t.kind==='system'?'system':''}">${e(tagLabel(t))}</span>`).join(' ')||'<span class="muted small">none</span>'}</td></tr>`;
}
// Admin actions for a whole series (or the ticked books in it). They use the library's bulk
// dialogs and endpoints, so the kids shelf still records a manual decision per book; the
// series id and whether it was "all" or a selection go along for the audit trail.
function wireSeriesBulk(s){
  const bar=$('#series-bulk-bar'); if(!bar) return;
  const all=s.works.map(w=>w.id);
  const picked=()=>$$('.series-pick').filter(c=>c.checked).map(c=>c.value);
  const paint=()=>{
    const sel=picked(), subset=sel.length>0&&sel.length<all.length;
    const ids=sel.length?sel:all, what=subset?`${sel.length} selected`:'series';
    const ctx={series_id:s.id,scope:subset?'selected':'all'};
    const done=()=>renderSeriesDetail(s.id);
    $('#series-select-all').checked=all.length>0&&sel.length===all.length;
    bar.innerHTML=`<div class="attention-card calm bulk-bar-card" style="margin-bottom:12px"><div><strong>${subset?`${sel.length} of ${all.length} selected`:`Whole series (${all.length} book${all.length===1?'':'s'})`}</strong><div class="muted small">Tick books to act on part of the series; otherwise these apply to every book in it.</div></div><div class="bulk-actions"><button class="button compact" id="series-kids-add" ${all.length?'':'disabled'}>Add ${what} to Kids shelf</button><button class="button compact danger" id="series-kids-remove" ${all.length?'':'disabled'}>Remove ${what} from Kids shelf</button><button class="button compact" id="series-tag-add" ${all.length?'':'disabled'}>Add tag…</button><button class="button compact danger" id="series-tag-remove" ${all.length?'':'disabled'}>Remove tag…</button></div></div>`;
    $('#series-kids-add').onclick=()=>bulkKidsShelfDialog(ids,{ctx,onDone:done});
    $('#series-kids-remove').onclick=()=>bulkKidsRemoveDialog(ids,{ctx,onDone:done});
    $('#series-tag-add').onclick=()=>bulkTagDialog(ids,{ctx,onDone:done});
    $('#series-tag-remove').onclick=()=>bulkRemoveTagDialog(ids,{ctx,onDone:done});
  };
  $$('.series-pick').forEach(c=>c.onchange=paint);
  $('#series-select-all').onchange=ev=>{ $$('.series-pick').forEach(c=>c.checked=ev.target.checked); paint(); };
  paint();
}
function seriesMonitorDialog(s){
  const m=modal(`Monitor ${s.name}`,`<form id="series-monitor-form" class="form-grid"><div class="field full"><label>Policy</label><select class="select" name="policy"><option value="future" ${s.policy==='future'?'selected':''}>Future / newly discovered entries</option><option value="all" ${s.policy==='all'?'selected':''}>All known entries</option><option value="none" ${!s.policy||s.policy==='none'?'selected':''}>None</option></select></div><div class="field"><label><input type="checkbox" name="audiobook" ${s.monitor_audiobook!==0?'checked':''}> Audiobooks</label></div><div class="field"><label><input type="checkbox" name="ebook" ${s.monitor_ebook?'checked':''}> Ebooks</label></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="series-monitor-save">Save</button>'});
  $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
  $('#series-monitor-save',m).onclick=async()=>{
    const f=new FormData($('#series-monitor-form'));
    try{ await api(`/api/v1/series/${s.id}/monitor`,{method:'PUT',body:{policy:f.get('policy'),audiobook:f.has('audiobook'),ebook:f.has('ebook')}}); toast('Series monitoring updated'); $('#modal-root').innerHTML=''; renderSeriesDetail(s.id); }
    catch(er){ toast(er.message,'bad'); }
  };
}

// Every monitored work with any resolved publish date used to load at once - once
// metadata linking actually started succeeding at scale, that became 9,000 rows
// (a 524,000px page). A release calendar's job is "what's coming up", not "the full
// publication history of every backlist title you own" - defaulting to a window
// around today, with Prev/Next to move it, keeps the normal case fast and the full
// history one click away rather than the default.
const calendarState = { monthsBack: 3, monthsForward: 12, allTime: false };
function calendarBounds(){
  const now=new Date();
  const start=new Date(now.getFullYear(),now.getMonth()-calendarState.monthsBack,1);
  const end=new Date(now.getFullYear(),now.getMonth()+calendarState.monthsForward+1,0);
  const fmt=d=>d.toISOString().slice(0,10);
  return {start:fmt(start),end:fmt(end)};
}
async function renderCalendar(){
  setLoading('Calendar');
  const {start,end}=calendarBounds();
  const q = calendarState.allTime ? '' : `?start=${start}&end=${end}`;
  const d=await api(`/api/v1/calendar${q}`);
  const grouped={};
  for(const x of d.items){
    const raw=String(x.date||'Unknown');
    const key=raw.slice(0,7)||'Unknown';
    (grouped[key]??=[]).push(x);
  }
  const entries=Object.entries(grouped);
  const body=entries.length
    ? entries.map(([month,items])=>{
        const rows=items.map(x=>`<tr class="clickable cal-row" data-id="${escAttr(x.work_id)}"><td style="width:130px">${e(x.date)}${x.approximate?' <span class="badge warn">approx.</span>':''}</td><td><strong>${e(x.title)}</strong></td><td>${e((x.media_kinds||'').replaceAll(',',', '))}</td></tr>`).join('');
        return `<div class="panel" style="margin-bottom:12px"><div class="panel-head"><h2>${e(month)}</h2></div><div class="table-wrap"><table><tbody>${rows}</tbody></table></div></div>`;
      }).join('')
    : `<div class="panel"><div class="empty"><strong>Nothing dated in this window</strong>${calendarState.allTime?'Publication dates will appear as metadata is resolved.':'Try widening the range, or view all history.'}</div></div>`;
  const rangeLabel = calendarState.allTime ? 'All history' : `${calendarState.monthsBack} months back to ${calendarState.monthsForward} months ahead`;
  $('#view').innerHTML=`<div class="page-head"><div><h1>Calendar</h1><p>Publication dates from metadata. ${e(rangeLabel)}.</p></div><div class="page-actions">${calendarState.allTime?'<button class="button ghost secondary" id="cal-windowed">Just show upcoming</button>':'<button class="button ghost secondary" id="cal-all">Show all history</button>'}</div></div>${body}`;
  $$('.cal-row').forEach(x=>x.onclick=()=>location.hash=`library/${x.dataset.id}`);
  $('#cal-all')?.addEventListener('click',()=>{calendarState.allTime=true;renderCalendar()});
  $('#cal-windowed')?.addEventListener('click',()=>{calendarState.allTime=false;renderCalendar()});
}

// Discover paging. Both this and the metadata picker used a fixed limit with no way
// to see anything beyond it, so a search that put the right book at position 31 was
// indistinguishable from one that found nothing.
const DISCOVER_PAGE = 30;
const discoverState = { q: '', items: [], exhausted: false };

async function renderDiscover(){
  $('#view').innerHTML=`<div class="page-head"><div><h1>Discover</h1><p>Search OpenReads' canonical metadata graph, then monitor every medium from one work.</p></div></div>
    <div class="panel"><div class="panel-body"><form id="discover-form" class="toolbar" style="margin:0"><input id="discover-q" class="input grow" placeholder="Title, author, ISBN…" autofocus value="${escAttr(discoverState.q)}"><button class="button">Search</button></form></div></div>
    <div id="discover-results" style="margin-top:16px"></div>
    <div id="discover-more" style="margin-top:16px;text-align:center"></div>`;
  $('#discover-form').onsubmit=ev=>{ ev.preventDefault(); discoverSearch($('#discover-q').value.trim(), {reset:true}); };
  if(discoverState.items.length) paintDiscover();
}

async function discoverSearch(q,{reset=false}={}){
  if(!q) return;
  if(reset){ discoverState.q=q; discoverState.items=[]; discoverState.exhausted=false; }
  const out=$('#discover-results');
  if(reset) out.innerHTML=`<div class="panel"><div class="empty"><span class="spinner"></span><strong style="margin-top:10px">Searching metadata</strong></div></div>`;
  try{
    // The provider has no offset parameter, so a wider limit is asked for and the
    // results already shown are sliced off. Wasteful but honest; the alternative is
    // pretending page one is everything.
    const want=discoverState.items.length+DISCOVER_PAGE;
    const d=await api(`/api/v1/discover?q=${encodeURIComponent(discoverState.q)}&limit=${Math.min(want,50)}`);
    const items=d.items||[];
    discoverState.exhausted = items.length<=discoverState.items.length || items.length>=50;
    discoverState.items=items;
    paintDiscover();
  }catch(er){ out.innerHTML=`<div class="warning-box"><strong>Metadata search failed.</strong> ${e(er.message)}</div>`; }
}

function paintDiscover(){
  const items=discoverState.items;
  const out=$('#discover-results');
  out.innerHTML = items.length
    ? `<div class="panel table-wrap"><table><thead><tr><th>Work</th><th>Author</th><th>Series</th><th>Identifiers</th><th></th></tr></thead><tbody>${items.map(x=>`<tr><td><strong>${e(x.title)}</strong><div class="small muted">${e(x.first_publish_date||'')}</div></td><td>${e((x.contributors||[]).filter(c=>c.role==='author').map(c=>c.name).join(', '))}</td><td>${e((x.series||[]).map(s=>s.name+(s.position?' #'+s.position:'')).join(', '))}</td><td>${e((x.identifiers||[]).map(i=>`${i.namespace}: ${i.value}`).slice(0,2).join(' · '))}</td><td><button class="button compact add-meta" data-id="${escAttr(x.id)}">Add</button></td></tr>`).join('')}</tbody></table></div>`
    : `<div class="panel"><div class="empty"><strong>No results</strong>Nothing matched “${e(discoverState.q)}”.</div></div>`;
  $$('.add-meta').forEach(b=>b.onclick=async()=>{
    b.disabled=true; b.textContent='Adding…';
    try{ const w=await api('/api/v1/library',{method:'POST',body:{metadata_id:b.dataset.id,monitored:acquisitionOn(state.me)}}); toast(`Added ${w.title}`); location.hash=`library/${w.id}`; }
    catch(er){ toast(er.message,'bad'); b.disabled=false; b.textContent='Add'; }
  });
  const more=$('#discover-more');
  more.innerHTML = (items.length && !discoverState.exhausted)
    ? `<button class="button secondary" id="discover-load-more">More results</button>`
    : (items.length ? `<span class="muted small">${fmtNum(items.length)} result${items.length===1?'':'s'} — that is everything the provider returned.</span>` : '');
  const btn=$('#discover-load-more');
  if(btn) btn.onclick=async()=>{ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Loading'; await discoverSearch(discoverState.q); };
}


// Wanted is fetched per media kind with server-side totals. Splitting one capped
// response in the browser would make the tab counts describe the page rather than
// the library - this library has eight thousand wanted assets.
const WANTED_PAGE = 300;
const wantedState = { kind: 'audiobook', notFound: false };
// "Searched automatically 3 h ago" / "cooling down until ...": what the wanted-search job knows about a row.
function wantedAutoStatus(x){
  if(!x.last_auto_search_at) return '<span class="muted small">Not searched yet</span>';
  const base=`Last auto search ${fmtDate(x.last_auto_search_at)}`;
  if(x.cooling_down) return `<span class="small">${base}<br><span class="badge">cooling down until ${fmtDate(x.next_eligible_at)}</span></span>`;
  return `<span class="small">${base}${x.auto_misses?` <span class="badge">not found x${x.auto_misses}</span>`:''}</span>`;
}
async function renderWanted(){
  setLoading('Wanted');
  const [audio, ebooks] = await Promise.all([
    api(`/api/v1/wanted?kind=audiobook&limit=${WANTED_PAGE}${wantedState.notFound?'&not_found=1':''}`),
    api(`/api/v1/wanted?kind=ebook&limit=${WANTED_PAGE}${wantedState.notFound?'&not_found=1':''}`),
  ]);
  const sets={audiobook:audio, ebook:ebooks};
  $('#view').innerHTML=`<div class="page-head"><div><h1>Wanted</h1><p>Monitored media assets that are not currently owned.</p></div><div class="page-actions"><label class="field-inline"><input type="checkbox" id="wanted-notfound"${wantedState.notFound?' checked':''}> Not found yet</label>${state.me?.is_admin?'<button class="button secondary" id="wanted-run-now">Run wanted search now</button>':''}</div></div><div class="tabs"><button class="tab${wantedState.kind==='audiobook'?' active':''}" data-wtab="audiobook">Audiobooks (${fmtNum(audio.total)})</button><button class="tab${wantedState.kind==='ebook'?' active':''}" data-wtab="ebook">Ebooks (${fmtNum(ebooks.total)})</button></div><div id="wanted-table"></div>`;
  const draw=kind=>{
    wantedState.kind=kind;
    const d=sets[kind], items=d.items;
    const note=items.length<d.total?`<div class="panel-body muted small">Showing the first ${fmtNum(items.length)} of ${fmtNum(d.total)}.</div>`:'';
    // Grouped by author: a flat table of up to 300 rows, each with its own Search
    // button, was the entire page - this turns it into something you can actually
    // scan, and a group can be searched in one action instead of one row at a time.
    const groups=new Map();
    for(const x of items){ const key=x.author||'Unknown author'; if(!groups.has(key)) groups.set(key,[]); groups.get(key).push(x); }
    const sorted=[...groups.entries()].sort((a,b)=>b[1].length-a[1].length || a[0].localeCompare(b[0]));
    $('#wanted-table').innerHTML = items.length
      ? `${note}${sorted.map(([author,rows],gi)=>`
        <div class="wanted-group" data-gi="${gi}">
          <div class="wanted-group-head"><div><span class="wanted-group-caret">▸</span> <strong>${e(author)}</strong> <span class="muted small">${rows.length} book${rows.length===1?'':'s'}</span></div><button class="button ghost compact wanted-search-group" data-gi="${gi}">Search all</button></div>
          <div class="wanted-group-body table-wrap"><table><tbody>${rows.map(x=>`<tr><td><strong>${e(x.title)}</strong></td><td>${e((x.format||'Any').toUpperCase())}</td><td class="wanted-auto">${wantedAutoStatus(x)}</td><td><button class="button compact wanted-search" data-work="${escAttr(x.work_id)}" data-kind="${escAttr(x.kind)}" data-title="${escAttr(x.title)}">Search</button></td></tr>`).join('')}</tbody></table></div>
        </div>`).join('')}`
      : `<div class="panel"><div class="empty"><strong>Nothing wanted here</strong>${wantedState.notFound?'<div style="margin-top:12px"><button type="button" class="button secondary compact" id="wanted-clear">Clear filters</button></div>':''}</div></div>`;
    $('#wanted-clear')?.addEventListener('click',()=>{ wantedState.notFound=false; renderWanted(); });
    $$('.wanted-group-head').forEach(h=>h.onclick=ev=>{ if(ev.target.closest('.wanted-search-group')) return; h.closest('.wanted-group').classList.toggle('open'); });
    $$('.wanted-search').forEach(b=>b.onclick=ev=>{ ev.stopPropagation(); manualSearch(b.dataset.work,b.dataset.kind,b.dataset.title); });
    $$('.wanted-search-group').forEach(b=>b.onclick=ev=>{ ev.stopPropagation(); wantedSearchGroup(sorted[Number(b.dataset.gi)][1],b); });
  };
  draw(wantedState.kind);
  $('#wanted-notfound').onchange=ev=>{ wantedState.notFound=ev.target.checked; renderWanted(); };
  const runNow=$('#wanted-run-now'); if(runNow) runNow.onclick=async()=>{ runNow.disabled=true; try{ await api('/api/v1/wanted/search-now',{method:'POST',body:{}}); toast('Wanted search started; results appear under Settings > Jobs','good'); }catch(er){ toast(er.message,'bad'); } runNow.disabled=false; };
  $$('[data-wtab]').forEach(b=>b.onclick=()=>{$$('[data-wtab]').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw(b.dataset.wtab)});
}

// Searches every book in an author group and grabs the top release only when it
// clears the same 80-point confidence floor the automatic wanted-search job uses -
// see scheduler.py _wanted_search(). Sequential, not parallel: this is one indexer
// query per book, and a group can be dozens of books.
async function wantedSearchGroup(rows,button){
  button.disabled=true; const old=button.textContent;
  let grabbed=0, none=0, failed=0;
  for(const [i,x] of rows.entries()){
    button.innerHTML=`<span class="spinner"></span> ${i+1}/${rows.length}`;
    try{
      const data=await api(`/api/v1/library/${x.work_id}/search/${x.kind}`,{method:'POST',body:{}});
      const best=(data.items||[]).filter(r=>!r.rejected).sort((a,b)=>b.score-a.score)[0];
      if(best && best.score>=80){ await api(`/api/v1/releases/${best.id}/grab`,{method:'POST',body:{}}); grabbed++; }
      else none++;
    }catch(er){ failed++; }
  }
  toast(`${grabbed} grabbed, ${none} with no confident match${failed?`, ${failed} failed`:''}`, grabbed?'good':'warn');
  button.disabled=false; button.textContent=old;
  if(grabbed) renderWanted();
}

// ---------- Queue page ----------
// One fetch (open downloads + the last few finished ones), filtered and sorted in the
// browser: the queue is tens of rows, not thousands. The shell is built once; filter and
// search changes repaint only #queue-results so typing never loses focus.
const QUEUE_FILTER_KEY = 'bookdoe-queue-filter';
const QUEUE_HISTORY_N = 25;
const QUEUE_CLOSED = ['imported', 'failed', 'cancelled'];
const QUEUE_GROUP_LABELS = { all: 'Open', active: 'Active', queued: 'Queued', completed: 'Completed', failed: 'Failed' };
const queueState = (() => {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(QUEUE_FILTER_KEY) || '{}') || {}; } catch { saved = {}; }
  return { group: 'all', kind: '', indexer: '', client: '', q: '', sort: 'started', dir: 'desc', items: [], counts: {}, expanded: new Set(), timer: null, ...saved };
})();
function rememberQueueFilter() {
  try { const { group, kind, indexer, client, q, sort, dir } = queueState; localStorage.setItem(QUEUE_FILTER_KEY, JSON.stringify({ group, kind, indexer, client, q, sort, dir })); } catch {}
}
const fmtSecs = s => { s = Math.round(Number(s)); if (!(s >= 0)) return ''; const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${String(m).padStart(2, '0')}m` : m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`; };
const fmtAgo = ts => { if (!ts) return ''; const s = Math.max(0, Date.now() / 1000 - Number(ts)); return s < 60 ? 'just now' : `${fmtSecs(s).replace(/ \d+s$/, '')} ago`; };
const queueWhen = ts => ts ? `<span title="${escAttr(fmtAgo(ts))}">${e(fmtDate(ts))}</span>` : '—';
function queueIsFiltered() { const s = queueState; return !!(s.group !== 'all' || s.kind || s.indexer || s.client || s.q.trim()); }
function clearQueueFilters() { Object.assign(queueState, { group: 'all', kind: '', indexer: '', client: '', q: '' }); rememberQueueFilter(); paintQueue(true); }
function queueFiltered() {
  const s = queueState, q = s.q.trim().toLowerCase();
  const rows = s.items.filter(x => (!s.kind || x.media_kind === s.kind) && (!s.indexer || (x.indexer || '') === s.indexer) && (!s.client || (x.client || '') === s.client)
    && (s.group === 'all' || x.state_group === s.group)
    && (!q || [x.work_title, x.title, x.release_title, x.indexer, x.client, ...(x.authors || [])].some(v => String(v || '').toLowerCase().includes(q))));
  const key = { started: x => x.started_at || 0, progress: x => x.progress_percent || 0, size: x => x.size_bytes || 0, title: x => (x.work_title || x.release_title || '').toLowerCase() }[s.sort] || (x => x.started_at || 0);
  const sign = s.dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => { const ka = key(a), kb = key(b); return (ka > kb ? 1 : ka < kb ? -1 : 0) * sign; });
  return rows;
}
function queueRowHtml(x) {
  const isClosed = QUEUE_CLOSED.includes(x.state), pct = Math.round(x.progress_percent || 0);
  const tone = x.state === 'completed' || x.state === 'imported' ? 'good' : /failed|cancelled/.test(x.state) ? 'bad' : 'info';
  const title = x.work_title || x.title || x.release_title || 'Download';
  const titleHtml = x.work_id ? `<a href="#library/${escAttr(x.work_id)}"><strong>${e(title)}</strong></a>` : `<strong>${e(title)}</strong>`;
  const authors = (x.authors || []).join(', '), series = x.series ? `${x.series.name}${x.series.position ? ' #' + x.series.position : ''}` : '';
  const kindBadge = x.media_kind ? `<span class="badge">${x.media_kind === 'audiobook' ? '♫ audiobook' : '▤ ebook'}</span>` : '<span class="muted">—</span>';
  const speed = x.speed_bps ? `${fmtBytes(x.speed_bps)}/s` : '', eta = x.eta_seconds != null && x.state === 'downloading' ? `ETA ${fmtSecs(x.eta_seconds)}` : '';
  const sizeTxt = x.size_bytes ? (x.downloaded_bytes != null && !isClosed ? `${fmtBytes(x.downloaded_bytes)} / ${fmtBytes(x.size_bytes)}` : fmtBytes(x.size_bytes)) : '—';
  const open = queueState.expanded.has(x.id);
  const src = x.grab_source === 'automatic' ? 'Automatic' : x.grab_source === 'manual' ? 'Manual' : '—';
  const actions = `${['completed', 'import_pending', 'import_failed'].includes(x.state) ? `<button class="button compact import-download" data-id="${escAttr(x.id)}">Import</button> ` : ''}${['grabbed', 'downloading'].includes(x.state) ? `<button class="button compact secondary cancel-download" data-id="${escAttr(x.id)}" data-active="1">Cancel</button>` : ['completed', 'import_pending', 'import_failed', 'importing'].includes(x.state) ? `<button class="button compact secondary cancel-download" data-id="${escAttr(x.id)}">Remove</button>` : ''}`;
  const kv = (k, v) => v === null || v === undefined || v === '' ? '' : `<div class="queue-kv"><span class="muted small">${e(k)}</span><span>${v}</span></div>`;
  const hist = (x.history || []).map(h => `<li><span class="muted small">${e(fmtDate(h.created_at))}</span> ${e(h.message)}</li>`).join('');
  const detail = `<tr class="queue-detail" data-detail="${escAttr(x.id)}" ${open ? '' : 'hidden'}><td colspan="12"><div class="queue-detail-grid">
    ${kv('Release', `<span class="mono small">${e(x.release_title || '')}</span>`)}${kv('Score', x.release_score != null ? `<span class="score ${x.release_score >= 80 ? 'good' : x.release_score >= 40 ? 'warn' : 'bad'}">${e(x.release_score)}</span>` : '')}
    ${kv('Type / format', e([x.media_kind, x.format ? String(x.format).toUpperCase() : ''].filter(Boolean).join(' · ')))}${kv('Indexer', e(x.indexer || ''))}
    ${kv('Client', e([x.client, x.protocol].filter(Boolean).join(' · ')))}${kv('Size', x.size_bytes ? e(fmtBytes(x.size_bytes)) : '')}
    ${kv('Speed / ETA', e([speed, eta].filter(Boolean).join(' · ')))}${kv('Status', e(x.state_label))}
    ${kv('Grabbed', e(x.grab_source === 'automatic' ? 'Automatically (wanted search)' : x.grab_source === 'manual' ? 'Manually' + (x.grabbed_by ? ' by ' + x.grabbed_by : '') : x.grabbed_by ? 'By ' + x.grabbed_by : ''))}
    ${kv('Started', queueWhen(x.started_at))}${kv('Last updated', queueWhen(x.updated_at))}
    ${kv(isClosed ? 'Finished' : 'Downloaded', queueWhen(x.finished_at || x.completed_at))}${kv('Time in queue', e(fmtSecs(x.duration_seconds)))}
    ${kv('Error', x.failure_reason ? `<span class="queue-error">${e(x.failure_reason)}</span>` : '')}
    ${kv('Download path', x.download_path ? `<span class="mono small">${e(x.download_path)}</span>` : '')}${kv('Destination', x.destination ? `<span class="mono small">${e(x.destination)}</span>` : '')}
    ${hist ? `<div class="queue-kv queue-history"><span class="muted small">History</span><ul>${hist}</ul></div>` : ''}</div></td></tr>`;
  return `<tr><td class="q-title" data-state="${escAttr(x.state)}"><button class="button ghost compact queue-expand" data-id="${escAttr(x.id)}" aria-expanded="${open}" aria-label="Details">${open ? '▾' : '▸'}</button>${titleHtml}
      <div class="small muted">${e([authors, series].filter(Boolean).join(' · '))}</div><div class="small muted q-inline">${e([x.media_kind, x.format ? String(x.format).toUpperCase() : '', x.indexer].filter(Boolean).join(' · '))}</div>
      ${x.error && (x.state === 'import_failed' || x.state === 'failed') ? `<div class="small queue-error">${e(x.error)}</div>` : ''}</td>
    <td class="q-type">${kindBadge}</td><td class="q-sec q-format">${x.format ? e(String(x.format).toUpperCase()) : '—'}</td><td class="q-sec q-indexer">${e(x.indexer || '—')}</td>
    <td class="q-sec q-client">${e(x.client || '—')}<div class="small muted">${e(x.protocol || '')}</div></td><td class="q-sec q-size">${e(sizeTxt)}</td>
    <td class="q-progress"><div class="progress"><i style="width:${pct}%"></i></div><span class="small muted">${pct}%${speed || eta ? ' · ' + e([speed, eta].filter(Boolean).join(' · ')) : ''}</span></td>
    <td class="q-state"><span class="badge ${tone}">${e(x.state_label || x.state)}</span></td>
    <td class="q-sec q-started">${queueWhen(x.started_at)}</td><td class="q-sec q-updated">${queueWhen(x.updated_at)}</td><td class="q-sec q-source">${e(src)}</td>
    <td class="q-actions">${actions}</td></tr>${detail}`;
}
function queueTableHtml(rows, emptyTitle, emptyText, clearBtn) {
  if (!rows.length) return `<div class="empty"><strong>${e(emptyTitle)}</strong>${e(emptyText)}${clearBtn ? '<div style="margin-top:12px"><button type="button" class="button secondary compact queue-clear-empty">Clear filters</button></div>' : ''}</div>`;
  return `<div class="table-wrap"><table class="queue-table"><thead><tr><th>Title</th><th class="q-type">Type</th><th class="q-sec">Format</th><th class="q-sec">Indexer</th><th class="q-sec">Client</th><th class="q-sec">Size</th><th>Progress</th><th>State</th><th class="q-sec">Started</th><th class="q-sec">Updated</th><th class="q-sec">Source</th><th></th></tr></thead><tbody>${rows.map(queueRowHtml).join('')}</tbody></table></div>`;
}
function queueResultsHtml() {
  const rows = queueFiltered(), open = rows.filter(x => !QUEUE_CLOSED.includes(x.state)), done = rows.filter(x => QUEUE_CLOSED.includes(x.state));
  const filtered = queueIsFiltered();
  const openPanel = `<div class="panel">${queueTableHtml(open, filtered && queueState.items.length ? 'No downloads match' : 'Queue is clear', filtered && queueState.items.length ? 'Change or clear the filters.' : 'No active acquisitions.', filtered && queueState.items.length)}</div>`;
  const donePanel = done.length ? `<div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Recently completed</h2><span class="muted small">last ${QUEUE_HISTORY_N} finished, failed or cancelled</span></div>${queueTableHtml(done, '', '')}</div>` : '';
  return openPanel + donePanel;
}
function queueControlsHtml() {
  const s = queueState, uniq = k => [...new Set(s.items.map(x => x[k]).filter(Boolean))].sort();
  const opts = (list, cur, any) => `<option value="">${any}</option>` + list.map(v => `<option value="${escAttr(v)}" ${v === cur ? 'selected' : ''}>${e(v)}</option>`).join('');
  const chips = Object.keys(QUEUE_GROUP_LABELS).map(g => {
    const n = g === 'all' ? s.items.filter(x => !QUEUE_CLOSED.includes(x.state)).length : (s.counts[g] ?? 0);
    return `<button class="chip queue-chip ${s.group === g ? 'active' : ''}" data-group="${g}" aria-pressed="${s.group === g}">${QUEUE_GROUP_LABELS[g]} <b>${fmtNum(n)}</b></button>`;
  }).join('');
  return `<div class="queue-chips">${chips}</div><div class="queue-filters">
    <input class="input" id="queue-q" type="search" placeholder="Search title, author, indexer" value="${escAttr(s.q)}">
    <select class="select" id="queue-kind"><option value="">All types</option><option value="ebook" ${s.kind === 'ebook' ? 'selected' : ''}>Ebooks</option><option value="audiobook" ${s.kind === 'audiobook' ? 'selected' : ''}>Audiobooks</option></select>
    <select class="select" id="queue-indexer">${opts(uniq('indexer'), s.indexer, 'All indexers')}</select>
    <select class="select" id="queue-client">${opts(uniq('client'), s.client, 'All clients')}</select>
    <select class="select" id="queue-sort">${[['started', 'Sort: started'], ['progress', 'Sort: progress'], ['size', 'Sort: size'], ['title', 'Sort: title']].map(([v, l]) => `<option value="${v}" ${s.sort === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <button class="button secondary compact" id="queue-dir" title="Reverse order">${s.dir === 'asc' ? '↑ Asc' : '↓ Desc'}</button>${queueIsFiltered() ? '<button class="button ghost compact" id="queue-clear">Clear filters</button>' : ''}</div>`;
}
function wireQueueResults() {
  $$('.queue-clear-empty').forEach(b => b.onclick = clearQueueFilters);
  $$('.import-download').forEach(b => b.onclick = () => openImport(b.dataset.id));
  $$('.cancel-download').forEach(b => b.onclick = () => openCancelDownload(b.dataset.id, !!b.dataset.active));
  $$('.queue-expand').forEach(b => b.onclick = () => {
    const id = b.dataset.id, row = document.querySelector(`tr[data-detail="${CSS.escape(id)}"]`);
    const show = !queueState.expanded.has(id); show ? queueState.expanded.add(id) : queueState.expanded.delete(id);
    if (row) row.hidden = !show; b.setAttribute('aria-expanded', String(show)); b.textContent = show ? '▾' : '▸';
  });
}
function paintQueue(controls) {
  const res = $('#queue-results'); if (res) res.innerHTML = queueResultsHtml();
  const ctl = $('#queue-controls'); if (ctl && controls) ctl.innerHTML = queueControlsHtml();
  wireQueueControls(); wireQueueResults();
}
function wireQueueControls() {
  const s = queueState, on = (sel, fn) => { const el = $(sel); if (el) el.onchange = fn; };
  $('#queue-clear')?.addEventListener('click', clearQueueFilters);
  $$('.queue-chip').forEach(b => b.onclick = () => { s.group = b.dataset.group; rememberQueueFilter(); paintQueue(true); });
  const q = $('#queue-q'); if (q) q.oninput = () => { s.q = q.value; rememberQueueFilter(); paintQueue(false); };
  on('#queue-kind', ev => { s.kind = ev.target.value; rememberQueueFilter(); paintQueue(false); });
  on('#queue-indexer', ev => { s.indexer = ev.target.value; rememberQueueFilter(); paintQueue(false); });
  on('#queue-client', ev => { s.client = ev.target.value; rememberQueueFilter(); paintQueue(false); });
  on('#queue-sort', ev => { s.sort = ev.target.value; rememberQueueFilter(); paintQueue(false); });
  const d = $('#queue-dir'); if (d) d.onclick = () => { s.dir = s.dir === 'asc' ? 'desc' : 'asc'; d.textContent = s.dir === 'asc' ? '↑ Asc' : '↓ Desc'; rememberQueueFilter(); paintQueue(false); };
}
async function loadQueue() {
  const d = await api(`/api/v1/queue?scope=all&history_limit=${QUEUE_HISTORY_N}`);
  queueState.items = d.items; queueState.counts = d.counts || {};
  return d;
}
// Refresh while something is moving. Stops by itself when the user leaves the page or
// hides the tab, and restarts from renderQueue when they come back.
function scheduleQueueRefresh() {
  if (typeof setInterval !== 'function') return;
  clearInterval(queueState.timer); queueState.timer = null;
  if (!queueState.items.some(x => ['grabbed', 'downloading'].includes(x.state))) return;
  queueState.timer = setInterval(async () => {
    if (location.hash !== '#queue' || !$('#queue-results')) { clearInterval(queueState.timer); queueState.timer = null; return; }
    if (document.hidden) return;
    try {
      await loadQueue(); paintQueue(true);
      if (!queueState.items.some(x => ['grabbed', 'downloading'].includes(x.state))) { clearInterval(queueState.timer); queueState.timer = null; }
    } catch {}
  }, 8000);
}
async function renderQueue() {
  setLoading('Queue'); await loadQueue();
  $('#view').innerHTML = `<div class="page-head"><div><h1>Queue</h1><p>Durable acquisition state across Usenet and torrent clients.</p></div><div class="page-actions"><button class="button secondary" id="queue-poll">Poll now</button></div></div>
    <div id="queue-controls">${queueControlsHtml()}</div><div id="queue-results">${queueResultsHtml()}</div>`;
  $('#queue-poll').onclick = async () => { try { const x = await api('/api/v1/queue/poll', { method: 'POST', body: {} }); toast(`Updated ${x.updated} item(s)`); renderQueue(); } catch (er) { toast(er.message, 'bad'); } };
  wireQueueControls(); wireQueueResults(); scheduleQueueRefresh();
}
function openCancelDownload(id,active){const verb=active?'Cancel':'Remove';const m=modal(`${verb} download`,`<div class="warning-box"><strong>${active?'The download client will stop this job and delete its data.':'This item leaves the queue. You will be asked whether to also delete its files in the download client.'}</strong> Files already in your library are never touched.</div>`,{footer:`<button class="button ghost modal-cancel">Keep</button><button class="button danger" id="cancel-confirm">${verb}</button>`});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#cancel-confirm',m).onclick=async()=>{try{const rm=active||(typeof confirm==='function'&&confirm('Also delete this download\'s files in the download client?'));await api(`/api/v1/downloads/${encodeURIComponent(id)}?remove_from_client=${rm?'true':'false'}`,{method:'DELETE'});toast(active?'Download cancelled':'Download removed');$('#modal-root').innerHTML='';renderQueue()}catch(er){toast(er.message,'bad')}}}
function openImport(id){const m=modal('Import completed download',`<div class="warning-box"><strong>Managed import only.</strong> ${e(PRODUCT)} will never rename adopted library files. This operation is for a completed download and requires a managed destination root.</div><div class="field" style="margin-top:16px"><label>Transfer mode</label><select class="select" id="import-mode"><option value="hardlink">Hardlink (fall back to copy across filesystems)</option><option value="copy">Copy</option><option value="move">Move</option></select></div>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="import-confirm">Import</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#import-confirm',m).onclick=async()=>{try{const x=await api(`/api/v1/downloads/${id}/import`,{method:'POST',body:{mode:$('#import-mode').value}});toast(`Imported ${x.files.length} file(s)`);$('#modal-root').innerHTML='';renderQueue()}catch(er){toast(er.message,'bad')}}}

// ---------- P4-02: Inbox (Part XX matching UI) over scan_candidates and search_candidates ----------

// Reusable evidence card, generic so P4-06 (Metadata Studio) can render any record with
// it. Returns HTML; call bindEvidenceActions(cardElement, actions) after inserting it.
//   inferred - {title, subtitle, authors, series, seriesPosition, identifiers, mediaKind, publicationDate, language, label?}
//              (leave authors undefined for a record that has none, e.g. a single Studio field)
//   evidence - {source, explanation, reasons, confidence}
//   actions  - [{label, icon?, handler, kind?: 'primary'|'secondary'|'ghost'}]
function normIdentifiers(v) {
  if (Array.isArray(v)) return v.filter(i => i && i.value).map(i => ({ namespace: i.namespace || i.type || 'id', value: i.value }));
  return v && typeof v === 'object' ? Object.entries(v).filter(([, x]) => x).map(([namespace, value]) => ({ namespace, value })) : [];
}

function evidenceCard(inferred, evidence, actions) {
  const authors = (inferred.authors || []).filter(Boolean), series = (inferred.series || []).filter(Boolean);
  const ids = normIdentifiers(inferred.identifiers);
  const meta = [
    authors.length ? e(authors.join(', ')) : inferred.authors ? '<span class="muted">Unknown author</span>' : '',
    series.length ? e(series.join(', ')) + (inferred.seriesPosition ? ' #' + e(inferred.seriesPosition) : '') : '',
    e(inferred.mediaKind), e(inferred.publicationDate), e(inferred.language),
  ].filter(Boolean).join(' · ');
  const conf = evidence.confidence != null ? Math.round(evidence.confidence * 100) : null;
  const why = [evidence.source, evidence.explanation, ...(evidence.reasons || [])].filter(Boolean);
  const buttons = (actions || []).map((a, i) =>
    `<button class="button compact ${a.kind === 'primary' ? '' : a.kind === 'ghost' ? 'ghost' : 'secondary'}" data-action-index="${i}">${a.icon ? e(a.icon) + ' ' : ''}${e(a.label)}</button>`).join('');
  return `<div class="evidence-card"><div class="evidence-card-body">
      <div class="evidence-label muted small">${e(inferred.label || PRODUCT+' inferred')}</div>
      <div class="evidence-title">${e(inferred.title || 'Untitled')}${inferred.subtitle ? ` <span class="muted small">- ${e(inferred.subtitle)}</span>` : ''}</div>
      <div class="evidence-meta">${meta}</div>
      ${ids.length ? `<div class="evidence-ids mono small muted">${ids.map(i => `${e(i.namespace)}: ${e(i.value)}`).join(' · ')}</div>` : ''}
      ${why.length || conf != null ? `<div class="evidence-explain"><span class="muted small">Why: ${why.map(e).join(' · ') || 'no evidence recorded'}</span> ${conf != null ? `<span class="badge ${conf >= 90 ? 'good' : conf >= 60 ? 'warn' : 'bad'}">${conf}% match</span>` : ''}</div>` : ''}
    </div>
    ${buttons ? `<div class="evidence-label muted small">What you can do</div><div class="evidence-actions">${buttons}</div>` : ''}
  </div>`;
}

function bindEvidenceActions(root, actions) {
  $$('[data-action-index]', root).forEach(b => {
    b.onclick = () => Promise.resolve(actions[Number(b.dataset.actionIndex)].handler()).catch(er => toast(er.message, 'bad'));
  });
}

const closeModal = () => { $('#modal-root').innerHTML = ''; };
const post = (path, body = {}) => api(path, { method: 'POST', body });

function scanCandidateModel(c) {
  const d = c.detected || {};
  return {
    inferred: { title: d.title, subtitle: d.subtitle, authors: Array.isArray(d.authors) ? d.authors : [], series: d.series ? [d.series] : [], seriesPosition: d.series_position,
      identifiers: d.identifiers, mediaKind: c.media_kind, publicationDate: d.publication_date, language: d.language },
    evidence: { source: `Scanned file (${c.status})`, reasons: c.reasons || [], confidence: c.confidence },
  };
}

function searchCandidateModel(c) {
  return {
    inferred: { title: c.title, subtitle: c.subtitle, authors: Array.isArray(c.authors) ? c.authors : [], series: (c.series || []).map(s => typeof s === 'string' ? s : s.name || ''),
      seriesPosition: c.series_position, identifiers: c.identifiers, publicationDate: c.publication_date, language: c.language },
    evidence: { source: `Search "${c.query || ''}" via ${(c.providers || []).join(', ') || 'providers'}`, explanation: c.match_explanation, confidence: c.match_score },
  };
}

// Legacy review_tasks are still written by the metadata resolver and the Readarr migration
// and no candidate table covers them (they hang off an existing Work), so they stay here.
function reviewTaskModel(t) {
  const p = t.payload || {}, src = p.source || {};
  return {
    inferred: { title: t.title, authors: src.author ? [src.author] : [], identifiers: src.identifiers },
    evidence: { source: `Unresolved book (${String(t.kind || 'review').replace(/_/g, ' ')})`, confidence: p.confidence,
      explanation: p.candidates?.length ? `${p.candidates.length} provider suggestion(s), none confident enough` : 'Not matched against OpenReads yet' },
  };
}

function reviewTaskActions(t) {
  return [
    ...(t.entity_id ? [
      { label: 'Find a match', icon: '⌕', kind: 'primary', handler: () => refreshWorkMetadata(t.entity_id) },
      { label: 'Open book', handler: () => { location.hash = `library/${t.entity_id}`; } },
      { label: 'Edit manually', icon: '✎', handler: () => { location.hash = `library/${t.entity_id}/studio/work`; } },
    ] : []),
    { label: 'Ignore', icon: '✕', kind: 'ghost', handler: () => confirmPost('Ignore this item? It will stop being offered for review.', `/api/v1/review/${t.id}/ignore`, 'Item ignored') },
  ];
}

// A kids_tag review task (services/tagging.py): the classifier's verdict on text metadata
// already on record. Approving is the ONLY way the verdict becomes the 'kids' tag, so the
// approve action confirms first; rejecting changes nothing about the book.
function kidsSuggestionModel(t) {
  const p = t.payload || {}, src = p.source || {};
  return {
    inferred: { label: 'Suggested for the kids shelf', title: t.title, authors: src.authors || (src.author ? [src.author] : []), subtitle: src.description ? src.description.slice(0, 160) + (src.description.length > 160 ? '…' : '') : '' },
    evidence: { source: `Classifier verdict: ${String(p.verdict || 'kids').replace(/_/g, ' ')}${p.model ? ` (${p.model})` : ''}`, explanation: p.reason || 'No reason given', confidence: p.confidence,
      reasons: [
        (p.audience_evidence || []).length ? `Model cited: ${p.audience_evidence.join('; ')}` : '',
        ((p.gate || {}).strong_signals || []).length ? `Strong signals: ${p.gate.strong_signals.map(x => x.detail).join('; ')}` : '',
        p.not_auto_approved || '',
      ].filter(Boolean) },
  };
}

// A kids_recheck task: a book the classifier auto-approved before the evidence gate existed
// that does not pass it. Nothing has been removed; an administrator keeps or removes it.
function kidsRecheckModel(t) {
  const p = t.payload || {}, src = p.source || {};
  return {
    inferred: { label: 'Auto-tagged without audience evidence', title: t.title, authors: src.authors || [], subtitle: src.description ? src.description.slice(0, 160) + (src.description.length > 160 ? '…' : '') : '' },
    evidence: { source: `Auto-approved${p.model ? ` by ${p.model}` : ''}; still on the kids shelf until you decide`, explanation: (p.reasons || []).join('; ') || 'Fails the audience-evidence gate', confidence: p.confidence },
  };
}
function kidsRecheckActions(t) {
  return [
    { label: 'Keep on kids shelf', icon: '✓', kind: 'primary', handler: () => confirmPost(`Keep "${t.title}" on the kids shelf? This records your decision and the classifier will never change it.`, `/api/v1/review/${t.id}/kids-recheck/keep`, 'Kept on the kids shelf') },
    ...(t.entity_id ? [{ label: 'Open book', handler: () => { location.hash = `library/${t.entity_id}`; } }] : []),
    { label: 'Remove from kids shelf', icon: '✕', kind: 'ghost', handler: () => confirmPost(`Take "${t.title}" off the kids shelf? Kid accounts will no longer see it, and the classifier will never add it back.`, `/api/v1/review/${t.id}/kids-recheck/remove`, 'Removed from the kids shelf') },
  ];
}

function kidsSuggestionActions(t) {
  return [
    { label: 'Add to kids shelf', icon: '✓', kind: 'primary', handler: () => confirmPost(`Put "${t.title}" on the kids shelf? Every kids account will be able to see and open it.`, `/api/v1/review/${t.id}/kids-tag/approve`, 'Added to the kids shelf') },
    ...(t.entity_id ? [{ label: 'Open book', handler: () => { location.hash = `library/${t.entity_id}`; } }] : []),
    { label: 'Not for kids', icon: '✕', kind: 'ghost', handler: () => confirmPost('Decline this suggestion? The book stays as it is and is not suggested again.', `/api/v1/review/${t.id}/kids-tag/reject`, 'Suggestion declined') },
  ];
}

async function runKidsScan(limit = 20) {
  const r = await post('/api/v1/tags/classify-library', { limit });
  toast(r.pending ? `Checking up to ${limit} of ${fmtNum(r.pending)} unchecked books in the background. Suggestions appear here as they arrive.` : 'Every owned book has already been checked.', 'good', 7000);
}


// Seed for "Search by ISBN / ASIN / author": the strongest key the record has.
function identifierSeed(identifiers, authors) {
  const ids = normIdentifiers(identifiers), pick = ns => ids.find(i => i.namespace.toLowerCase().includes(ns));
  return pick('isbn') ? { mode: 'isbn', value: pick('isbn').value } : pick('asin') ? { mode: 'asin', value: pick('asin').value } : { mode: 'author', value: (authors || [])[0] || '' };
}

// A Work that is the same book as a search result: a shared identifier, or the same title
// with an author in common.  Signature is (identifier, title, author) so callers can
// pass raw fields without constructing a full item object.
async function findExistingWork(identifier, title, author) {
  const norm = s => String(s ?? '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  const idKey = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const ids = identifier ? normIdentifiers(identifier) : [];
  const keys = new Set(ids.map(i => idKey(i.value)));
  const authors = new Set((author || []).map(norm));
  const same = w => w.type === 'work' && (normIdentifiers(w.identifiers).some(i => keys.has(idKey(i.value)))
    || (norm(w.title) === norm(title) && (w.authors || []).some(a => authors.has(norm(a)))));
  for (const q of [...ids.map(i => i.value), title].filter(Boolean)) {
    const hit = ((await api(`/api/v1/search?q=${encodeURIComponent(q)}&limit=20`)).results || []).find(same);
    if (hit) return hit;
  }
  return null;
}

// A search result picked for a scanned file. Adopt needs an admin and nothing can delete
// a Work afterwards, so that check comes before any write (promote-then-fail would leave
// an empty Work). A book already in the library gets the file instead of a duplicate.
// When adopt fails after promote, the promoted Work becomes an orphan (no asset linked).
async function useSearchMatchForScan(scanId, item) {
  if (!state.me?.is_admin) throw new Error('Only an admin can add a file to the library');
  const existing = await findExistingWork(item.identifiers, item.title, item.authors);
  if (existing) {
    await post(`/api/v1/scan-candidates/${scanId}/adopt`, { work_id: existing.id });
    await post(`/api/v1/search-candidates/${item.id}/dismiss`);
    return afterInboxChange(`File added to ${existing.title}, already in your library`);
  }
  const work = await post(`/api/v1/search-candidates/${item.id}/promote`);
  await adoptScanCandidate(scanId, work.id);
}

// "Search again" on a search match: the pick becomes a local book, the old match is dismissed.
// Admin check before any write (promote-then-fail would leave an empty Work). A book already
// in the library gets joined instead of a duplicate; other results from the same search are
// dismissed when we join an existing Work.
async function replaceSearchCandidate(oldId, item) {
  if (!state.me?.is_admin) throw new Error('Only an admin can add a book to the library');
  const existing = await findExistingWork(item.identifiers, item.title, item.authors);
  if (existing) {
    await post(`/api/v1/search-candidates/${item.id}/promote`, { work_id: existing.id });
    await post(`/api/v1/search-candidates/${oldId}/dismiss`);
    return afterInboxChange(`Added to ${existing.title}, already in your library`);
  }
  await post(`/api/v1/search-candidates/${item.id}/promote`);
  await post(`/api/v1/search-candidates/${oldId}/dismiss`);
  await afterInboxChange('Added to the library');
}

function scanCandidateActions(c) {
  const d = c.detected || {}, find = seed => matchSearchDialog({ seed, pickLabel: 'Use this match', onPick: item => useSearchMatchForScan(c.id, item) });
  return [
    ...(c.match_work_id ? [{ label: 'Adopt match', icon: '✓', kind: 'primary', handler: () => adoptScanCandidate(c.id, c.match_work_id) }] : []),
    { label: 'Find a match', icon: '⌕', kind: c.match_work_id ? 'secondary' : 'primary', handler: () => find({ mode: 'text', value: [d.title, ...(d.authors || [])].filter(Boolean).join(' ') }) },
    { label: 'Search all providers', icon: '⌕', handler: () => find({ mode: 'text', value: [d.title, ...(d.authors || [])].filter(Boolean).join(' '), providers: 'all' }) },
    { label: 'Search by ISBN / ASIN / author', icon: '#', handler: () => find(identifierSeed(d.identifiers, d.authors)) },
    { label: 'Edit parsed details', icon: '✎', handler: () => editScanCandidate(c) },
    { label: 'Link to existing book', icon: '🔗', handler: () => workPickerDialog(workId => adoptScanCandidate(c.id, workId)) },
    { label: 'Ignore file', icon: '✕', kind: 'ghost', handler: () => dismissScanCandidate(c.id) },
  ];
}

// "Create local book" is POST .../search-candidates/{id}/promote without a work_id: a Work
// from this record's own fields (metadata_source 'candidate'). POST /api/v1/library needs
// an OpenReads metadata_id, so it cannot serve here. No edit action: there is no PATCH for
// search candidates yet (P4-01 follow-up, tracked by test_inbox_ui.py).
function searchCandidateActions(c) {
  const again = seed => matchSearchDialog({ seed, pickLabel: 'Use this instead', onPick: item => replaceSearchCandidate(c.id, item) });
  return [
    { label: 'Create local book', icon: '＋', kind: 'primary', handler: () => promoteSearchCandidate(c.id, null) },
    { label: 'Link to existing book', icon: '🔗', handler: () => workPickerDialog(workId => promoteSearchCandidate(c.id, workId)) },
    { label: 'Search again', icon: '↻', handler: () => again({ mode: 'text', value: c.query || [c.title, ...(c.authors || [])].join(' ') }) },
    { label: 'Search all providers', icon: '⌕', handler: () => again({ mode: 'text', value: c.query || [c.title, ...(c.authors || [])].join(' '), providers: 'all' }) },
    { label: 'Search by ISBN / ASIN / author', icon: '#', handler: () => again(identifierSeed(c.identifiers, c.authors)) },
    { label: 'Leave unmatched', icon: '✕', kind: 'ghost', handler: () => confirmPost('Dismiss this match? It will no longer appear in the Inbox.', `/api/v1/search-candidates/${c.id}/dismiss`, 'Match dismissed') },
  ];
}

async function afterInboxChange(message) { toast(message); closeModal(); await renderInbox(); }

async function confirmPost(question, path, message) {
  if (!confirm(question)) return;
  await post(path);
  await afterInboxChange(message);
}

async function adoptScanCandidate(cid, workId) {
  await post(`/api/v1/scan-candidates/${cid}/adopt`, workId ? { work_id: workId } : {});
  await afterInboxChange('File adopted into the library');
}

async function dismissScanCandidate(cid) {
  await post(`/api/v1/scan-candidates/${cid}/ignore`);
  await afterInboxChange('File ignored');
}

async function promoteSearchCandidate(cid, workId) {
  await post(`/api/v1/search-candidates/${cid}/promote`, workId ? { work_id: workId } : {});
  await afterInboxChange(workId ? 'Linked to existing book' : 'Added to the library');
}

function editScanCandidate(c) {
  const d = c.detected || {};
  const inputs = [['title', 'Title', d.title, 'full'], ['authors', 'Author(s)', (d.authors || []).join(', '), 'full'], ['series', 'Series', d.series, ''], ['series_position', 'Series position', d.series_position, '']];
  const m = modal('Edit parsed details', `<form id="edit-scan-form" class="form-grid">
    ${inputs.map(([n, l, v, w]) => `<div class="field ${w}"><label>${l}</label><input class="input" name="${n}" value="${escAttr(v || '')}"></div>`).join('')}
    <div class="field full"><label>Identifiers</label><textarea class="input" name="identifiers" rows="3" placeholder="isbn:9780000000000">${e(normIdentifiers(d.identifiers).map(i => `${i.namespace}:${i.value}`).join('\n'))}</textarea><div class="help">One per line as namespace:value, e.g. asin:B000000000.</div></div>
  </form>`, { footer: '<button class="button ghost modal-cancel">Cancel</button><button class="button" id="edit-scan-save">Save</button>' });
  $('.modal-cancel', m).onclick = closeModal;
  $('#edit-scan-save', m).onclick = async () => {
    try { await saveScanEdit(c, Object.fromEntries(new FormData($('#edit-scan-form', m)))); }
    catch (er) { toast(er.message, er.status ? 'bad' : 'warn'); }
  };
}

// The PATCH body for the edit form: only the fields the user changed, since the endpoint
// writes whatever it is sent and an untouched empty box must not erase what the scan found.
function scanEditBody(detected, f) {
  const d = detected || {}, body = {};
  const title = String(f.title || '').trim();
  if (title !== String(d.title || '').trim()) {
    if (!title) throw new Error('A title is required');
    body.title = title;
  }
  const authors = String(f.authors || '').split(',').map(a => a.trim()).filter(Boolean);
  if (authors.join('\n') !== (Array.isArray(d.authors) ? d.authors : []).join('\n')) body.authors = authors;
  for (const k of ['series', 'series_position']) {
    const v = String(f[k] || '').trim();
    if (v !== String(d[k] ?? '').trim()) body[k] = v;
  }
  const identifiers = {};
  for (const line of String(f.identifiers || '').split('\n').map(l => l.trim()).filter(Boolean)) {
    const at = line.indexOf(':');
    if (at < 1 || at === line.length - 1) throw new Error(`Identifier "${line}" must look like namespace:value`);
    identifiers[line.slice(0, at).trim().toLowerCase()] = line.slice(at + 1).trim();
  }
  const key = o => Object.entries(o).map(([k, v]) => `${k}:${v}`).sort().join('\n');
  const before = Object.fromEntries(normIdentifiers(d.identifiers).map(i => [i.namespace.toLowerCase(), String(i.value)]));
  if (key(identifiers) !== key(before)) body.identifiers = identifiers;
  return body;
}

async function saveScanEdit(c, form) {
  const body = scanEditBody(c.detected, form);
  if (!Object.keys(body).length) { closeModal(); return toast('Nothing changed', 'warn'); }
  await api(`/api/v1/scan-candidates/${c.id}`, { method: 'PATCH', body });
  await afterInboxChange('Parsed details updated');
}

// Pick a library book (same rows as the Work page's picker), then call onChoose(workId).
function workPickerDialog(onChoose) {
  const m = modal('Link to existing book', `<div class="toolbar"><input class="input grow" id="wp-q" placeholder="Search your library by title or author…"><button class="button" id="wp-go">Search</button></div><div id="wp-results" class="muted small" style="margin-top:12px">Type a title or author to find the book.</div>`, { wide: true, footer: '<button class="button ghost modal-cancel">Cancel</button>' });
  $('.modal-cancel', m).onclick = closeModal;
  const run = async () => {
    const q = $('#wp-q', m).value.trim(), out = $('#wp-results', m);
    if (!q) return;
    out.innerHTML = '<span class="spinner"></span>';
    try {
      const d = await api(`/api/v1/library?q=${encodeURIComponent(q)}&limit=25`);
      out.innerHTML = candidateRows(d.items.map(w => ({ ...w, authors: (w.authors || []).map(a => a.name) })));
      $$('.pick-meta', out).forEach(b => b.onclick = () => onChoose(b.dataset.id).catch(er => toast(er.message, 'bad')));
    } catch (er) { out.innerHTML = `<div class="warning-box">${e(er.message)}</div>`; }
  };
  $('#wp-go', m).onclick = run;
  $('#wp-q', m).addEventListener('keydown', ev => { if (ev.key === 'Enter') run(); });
}

// Federated search from a card: text, ISBN, ASIN or author, against the default providers or
// all of them. Every mode is the same call with q set; ISBNs lose spaces and dashes.
const ALL_PROVIDERS = 'local,openlibrary,hardcover,audiobookdb,googlebooks';

function matchSearchUrl({ mode, q, providers }) {
  const v = mode === 'isbn' ? String(q || '').replace(/[\s-]/g, '') : String(q || '').trim();
  const p = providers === 'all' ? ALL_PROVIDERS : providers;
  return `/api/v1/search/federated?q=${encodeURIComponent(v)}&limit=25${p ? `&providers=${p}` : ''}`;
}

// The federated endpoint stores every result as a pending search candidate, which the Inbox
// lists under "Search matches". The dialog dismisses the unpicked results it showed (`shown`)
// on pick, close (button, X or backdrop) or the next search, so a card search does not leave a page of new items behind.
async function discardSearchResults(shown, keepId) {
  const ids = [...shown].filter(id => id !== keepId);
  ids.forEach(id => shown.delete(id));
  await Promise.all(ids.map(id => post(`/api/v1/search-candidates/${id}/dismiss`).catch(() => null)));
}

async function pickSearchResult(shown, item, onPick) {
  await discardSearchResults(shown, item.id);
  await onPick(item);
  shown.delete(item.id);
}

function matchSearchDialog({ seed, pickLabel, onPick }) {
  const modes = [['text', 'Title / author'], ['isbn', 'ISBN'], ['asin', 'ASIN'], ['author', 'Author']];
  const m = modal('Find a match', `<div class="toolbar">
      <select class="select" id="ms-mode">${modes.map(([v, l]) => `<option value="${v}"${v === seed.mode ? ' selected' : ''}>${l}</option>`).join('')}</select>
      <input class="input grow" id="ms-q" value="${escAttr(seed.value || '')}">
      <select class="select" id="ms-providers"><option value="">Default providers</option><option value="all"${seed.providers === 'all' ? ' selected' : ''}>All providers</option></select>
      <button class="button" id="ms-go">Search</button></div>
    <div id="ms-results" class="muted small" style="margin-top:12px"></div>`, { wide: true, footer: '<button class="button ghost modal-cancel">Close</button>' });
  const shown = new Set(), close = () => { closeModal(); return discardSearchResults(shown); };
  $('.modal-cancel', m).onclick = close;
  const x = $('.modal-close', m), backdrop = $('.modal-backdrop', m);
  if (x) x.onclick = close;
  if (backdrop) backdrop.addEventListener('click', ev => { if (ev.target.classList.contains('modal-backdrop')) return close(); });
  const run = async () => {
    const q = $('#ms-q', m).value.trim(), out = $('#ms-results', m);
    if (!q) return toast('Enter something to search for', 'warn');
    out.innerHTML = '<span class="spinner"></span>';
    try {
      await discardSearchResults(shown);  // search again: the previous results are stale, so they leave the Inbox
      const items = (await api(matchSearchUrl({ mode: $('#ms-mode', m).value, q, providers: $('#ms-providers', m).value }))).items || [];
      items.forEach(i => shown.add(i.id));
      out.innerHTML = items.length ? items.map(i => { const x = searchCandidateModel(i); return `<div class="inbox-item" data-result="${escAttr(i.id)}">${evidenceCard(x.inferred, x.evidence, [{ label: pickLabel, kind: 'primary' }])}</div>`; }).join('')
        : 'No results. Try another provider or a different search.';
      $$('[data-result]', out).forEach(card => bindEvidenceActions(card, [{ handler: () => pickSearchResult(shown, items.find(i => i.id === card.dataset.result), onPick) }]));
    } catch (er) { out.innerHTML = `<div class="warning-box"><strong>Search failed.</strong> ${e(er.message)}</div>`; }
  };
  $('#ms-go', m).onclick = run;
  $('#ms-q', m).addEventListener('keydown', ev => { if (ev.key === 'Enter') run(); });
  if (seed.value) run();
}

const INBOX_PAGE = 50;
// Review tasks of every kind share one section with filters (kind, state, text) and
// bulk selection; each card keeps the per-item actions of its kind.
const REVIEW_KIND_LABEL = { kids_tag: 'Kids shelf suggestions', kids_recheck: 'Auto-tagged without audience evidence', metadata_match: 'Unresolved books' };
const reviewKindLabel = k => REVIEW_KIND_LABEL[k] || String(k || 'review').replace(/_/g, ' ');
function reviewModel(t) { return t.kind === 'kids_tag' ? kidsSuggestionModel(t) : t.kind === 'kids_recheck' ? kidsRecheckModel(t) : reviewTaskModel(t); }
function reviewActions(t) { return t.kind === 'kids_tag' ? kidsSuggestionActions(t) : t.kind === 'kids_recheck' ? kidsRecheckActions(t) : reviewTaskActions(t); }
const INBOX = {
  scan: { model: scanCandidateModel, actions: scanCandidateActions, url: '/api/v1/scan-candidates?', title: 'Scanned files', blurb: 'Files on disk not matched to a book yet', noun: 'scanned files', empty: 'All scanned files are matched.' },
  search: { model: searchCandidateModel, actions: searchCandidateActions, url: '/api/v1/search-candidates?status=pending&', title: 'Search matches', blurb: 'Provider results awaiting a decision', noun: 'matches', empty: 'No search results are waiting for a decision.' },
  review: { model: reviewModel, actions: reviewActions, url: '/api/v1/review?', title: 'Review tasks', blurb: 'Kids shelf suggestions, unresolved books and anything else the library asks a person about', noun: 'tasks', empty: 'Nothing matches this filter.' },
};
// The review filter survives navigation and reloads (localStorage); selection does not.
const REVIEW_FILTER_KEY = 'alx-inbox-review-filter';
const savedReviewFilter = (() => { try { return JSON.parse(localStorage.getItem(REVIEW_FILTER_KEY) || '{}'); } catch { return {}; } })();
const inboxState = {
  scan: { items: [], total: 0 }, search: { items: [], total: 0 },
  review: { items: [], total: 0, categories: null, kinds: {}, bulkActions: {}, defaultBulk: ['ignore'],
    kind: savedReviewFilter.kind || '', status: savedReviewFilter.status || 'open', q: savedReviewFilter.q || '', category: savedReviewFilter.category || '',
    picked: new Set(), excluded: new Set(), selectAll: false, lastPick: null },
};
function rememberReviewFilter() { const s = inboxState.review; localStorage.setItem(REVIEW_FILTER_KEY, JSON.stringify({ kind: s.kind, status: s.status, q: s.q, category: s.category })); }
// Unresolved books are triaged by category (server-side, see services/review_triage.py): a wall of
// thousands of identical rows cannot be worked, a few labelled groups with bulk actions can.
const REVIEW_CATEGORIES = [
  ['identifier-exact', 'Exact identifier match', 'A suggestion shares an ISBN/ASIN/ID with the book'],
  ['has-owned-files', 'Has files', 'You own a file for it; needs a person'],
  ['hopeless', 'No identifiers, no files', 'Nothing to match on'],
  ['other', 'Other', 'Needs a look'],
];

function reviewQuery(s, offset) {
  const q = new URLSearchParams({ limit: String(INBOX_PAGE), offset: String(offset), status: s.status || 'open' });
  if (s.kind) q.set('kind', s.kind);
  if (s.q) q.set('q', s.q);
  if (s.kind === 'metadata_match' && s.category) q.set('category', s.category);
  return q.toString();
}
function isSelected(s, id) { return s.selectAll ? !s.excluded.has(id) : s.picked.has(id); }
function selectedCount(s) { return s.selectAll ? Math.max(0, s.total - s.excluded.size) : s.picked.size; }
function clearSelection(s) { s.picked = new Set(); s.excluded = new Set(); s.selectAll = false; s.lastPick = null; }

function inboxCard(type, c) {
  const m = INBOX[type].model(c);
  const s = inboxState.review;
  const check = type === 'review' && state.me?.is_admin && s.status === 'open'
    ? `<label class="inbox-pick"><input type="checkbox" class="review-pick" data-id="${escAttr(c.id)}" ${isSelected(s, c.id) ? 'checked' : ''} aria-label="Select ${escAttr(c.title || 'task')}"> <span class="badge">${e(reviewKindLabel(c.kind))}</span></label>` : '';
  return `<div class="inbox-item ${isSelected(s, c.id) && type === 'review' ? 'selected' : ''}" data-candidate-id="${escAttr(c.id)}">${type === 'scan' ? `<div class="inbox-path mono small muted">${e(c.path)}</div>` : ''}${check}${evidenceCard(m.inferred, m.evidence, INBOX[type].actions(c))}</div>`;
}

async function loadInbox(type, append) {
  const s = inboxState[type];
  const query = type === 'review' ? reviewQuery(s, append ? s.items.length : 0) : `limit=${INBOX_PAGE}&offset=${append ? s.items.length : 0}`;
  const d = await api(`${INBOX[type].url}${query}`);
  s.items = append ? s.items.concat(d.items) : d.items;
  s.total = d.total ?? s.items.length;
  if (type === 'review') { if (d.categories) s.categories = d.categories; s.kinds = d.kinds || {}; s.bulkActions = d.bulk_actions || {}; s.defaultBulk = d.default_bulk_actions || ['ignore']; if (!append) clearSelection(s); }
}

async function runReviewTriage(action) {
  const q = action === 'auto_resolve'
    ? 'Link up to 100 books to the suggestion that shares an exact identifier with them and close their tasks?'
    : 'Ignore up to 500 books that have no identifiers, no files and no suggestions? They stop being offered for review; nothing about the books changes.';
  if (!confirm(q)) return;
  const r = await post('/api/v1/review/triage', { action, limit: action === 'auto_resolve' ? 100 : 500 });
  toast(action === 'auto_resolve' ? `Linked ${r.linked} book(s)${r.errors?.length ? `, ${r.errors.length} failed` : ''}` : `Ignored ${r.ignored} item(s)`);
  await loadInbox('review');
  paintInbox();
}

// Bulk actions go through POST /api/v1/review/bulk, which runs the same per-item code
// as the card buttons (kids approve/reject record kids_decisions; ignore is the ignore
// route). A filter selection sends the filter plus the unticked ids; an explicit
// selection sends ids. The kind is always sent so nothing leaks across kinds.
const BULK_LABEL = { approve: 'Approve', reject: 'Reject', ignore: 'Ignore', keep: 'Keep on shelf', remove: 'Remove from shelf' };
const BULK_DONE = { approve: 'Approved', reject: 'Rejected', ignore: 'Ignored', keep: 'Kept', remove: 'Removed' };
async function runReviewBulk(action) {
  const s = inboxState.review, n = selectedCount(s);
  if (!s.kind) return toast('Pick one kind of task first; bulk actions never mix kinds.', 'bad');
  if (!n) return toast('Nothing selected', 'bad');
  const what = `${BULK_LABEL[action]} ${fmtNum(n)} ${reviewKindLabel(s.kind).toLowerCase()}${n === 1 ? '' : ''}`;
  const detail = s.kind === 'kids_tag' && action === 'approve' ? ' Every kids account will be able to see and open these books.'
    : s.kind === 'kids_tag' && action === 'reject' ? ' The books stay as they are and are not suggested again.'
    : s.kind === 'kids_recheck' && action === 'keep' ? ' Each stays on the kids shelf as your decision; the classifier will never change it.'
    : s.kind === 'kids_recheck' && action === 'remove' ? ' Kid accounts will no longer see these books, and the classifier will never add them back.'
    : ' They stop being offered for review; nothing about the books changes.';
  if (!confirm(`${what}?${detail}`)) return;
  const body = s.selectAll
    ? { action, kind: s.kind, filter: { q: s.q || null, category: s.kind === 'metadata_match' && s.category ? s.category : null }, exclude: [...s.excluded] }
    : { action, kind: s.kind, ids: [...s.picked] };
  const r = await post('/api/v1/review/bulk', body);
  toast(`${BULK_DONE[action]} ${fmtNum(r.applied)} of ${fmtNum(r.selected)}${r.skipped ? `, ${fmtNum(r.skipped)} skipped` : ''}${r.failed ? `, ${fmtNum(r.failed)} failed` : ''}`, r.failed ? 'warn' : 'good', 7000);
  await loadInbox('review');
  paintInbox();
  return r;
}

const reviewFiltered = s => !!(s.kind || s.q || s.category || s.status !== 'open');
async function clearReviewFilters() { Object.assign(inboxState.review, { kind: '', status: 'open', q: '', category: '' }); rememberReviewFilter(); await loadInbox('review'); paintInbox(); }
function paintReviewBar() {
  const bar = $('#inbox-review-bar'), s = inboxState.review, cats = s.categories;
  if (!bar) return;
  const admin = !!state.me?.is_admin;
  const kinds = Object.keys({ ...s.kinds, ...(s.kind ? { [s.kind]: s.kinds[s.kind] || 0 } : {}) }).sort();
  const openTotal = Object.values(s.kinds).reduce((a, b) => a + b, 0);
  const kindOpts = `<option value="">All kinds (${fmtNum(openTotal)})</option>` + kinds.map(k => `<option value="${escAttr(k)}" ${s.kind === k ? 'selected' : ''}>${e(reviewKindLabel(k))} (${fmtNum(s.kinds[k] || 0)})</option>`).join('');
  const bulk = s.kind ? (s.bulkActions[s.kind] || s.defaultBulk) : [];
  const n = selectedCount(s);
  bar.innerHTML = `<div class="inbox-filters"><select class="select compact" id="review-kind" aria-label="Kind">${kindOpts}</select><select class="select compact" id="review-status" aria-label="State">${['open', 'resolved', 'ignored'].map(v => `<option value="${v}" ${s.status === v ? 'selected' : ''}>${v}</option>`).join('')}</select><input class="input compact" id="review-q" type="search" placeholder="Search tasks" value="${escAttr(s.q)}" aria-label="Search tasks">${reviewFiltered(s) ? '<button type="button" class="button ghost compact" id="review-clear">Clear filters</button>' : ''}</div>`
    + (s.kind === 'metadata_match' && cats ? `<div class="inbox-categories"><button class="button compact ${s.category ? 'ghost' : 'secondary'}" data-cat="">All</button>${REVIEW_CATEGORIES.map(([k, label, hint]) =>
      `<button class="button compact ${s.category === k ? 'secondary' : 'ghost'}" data-cat="${k}" title="${escAttr(hint)}">${e(label)} (${fmtNum(cats[k] || 0)})</button>`).join('')}</div>`
      + (admin ? `<div class="inbox-bulk"><button class="button compact" data-bulk="auto_resolve"${cats['identifier-exact'] ? '' : ' disabled'}>Auto-resolve exact matches</button><button class="button compact danger" data-bulk="ignore_hopeless"${cats.hopeless ? '' : ' disabled'}>Ignore hopeless</button></div>` : '') : '')
    + (s.kind === 'kids_tag' && admin ? `<div class="inbox-bulk"><button class="button compact" id="kids-scan">Scan 20 unchecked books</button></div>` : '')
    + (admin && s.status === 'open' ? `<div class="inbox-bulk inbox-selection"><button class="button ghost compact" id="review-select-all" ${s.kind ? '' : 'disabled title="Pick a kind first"'}>${s.selectAll && !s.excluded.size ? '✓ All' : 'Select all'} ${fmtNum(s.total)} matching</button><button class="button ghost compact" id="review-select-none" ${n ? '' : 'disabled'}>Clear</button><span class="muted small" id="review-selected">${fmtNum(n)} selected</span> ${bulk.map(a => `<button class="button compact ${a === 'approve' || a === 'keep' ? '' : a === 'reject' || a === 'remove' ? 'danger' : 'secondary'}" data-review-bulk="${a}" ${n ? '' : 'disabled'}>${BULK_LABEL[a]} selected</button>`).join('')}${s.kind ? '' : '<span class="muted small">Bulk actions need one kind selected.</span>'}</div>` : '');
  const reload = async () => { rememberReviewFilter(); await loadInbox('review'); paintInbox(); };
  $('#review-kind', bar).onchange = ev => { s.kind = ev.target.value; s.category = ''; reload(); };
  $('#review-status', bar).onchange = ev => { s.status = ev.target.value; reload(); };
  let t; $('#review-q', bar).oninput = ev => { clearTimeout(t); const v = ev.target.value.trim(); if (!v) { s.q = ''; reload(); return; } t = setTimeout(() => { s.q = v; reload(); }, 300); };
  $('#review-clear', bar)?.addEventListener('click', () => clearReviewFilters());
  $$('[data-cat]', bar).forEach(b => b.onclick = async () => { s.category = b.dataset.cat; reload(); });
  $$('[data-bulk]', bar).forEach(b => b.onclick = () => runReviewTriage(b.dataset.bulk).catch(er => toast(er.message)));
  const scan = $('#kids-scan', bar);
  if (scan) scan.onclick = () => { scan.disabled = true; runKidsScan(20).catch(er => toast(er.message, 'bad')).finally(() => { scan.disabled = false; }); };
  $('#review-select-all', bar)?.addEventListener('click', () => { s.selectAll = true; s.excluded = new Set(); s.picked = new Set(); paintInbox(); });
  $('#review-select-none', bar)?.addEventListener('click', () => { clearSelection(s); paintInbox(); });
  $$('[data-review-bulk]', bar).forEach(b => b.onclick = () => runReviewBulk(b.dataset.reviewBulk).catch(er => toast(er.message, 'bad')));
}

// Individual ticks, with shift-click selecting the range since the last tick.
function wireReviewPicks(list) {
  const s = inboxState.review;
  const boxes = $$('.review-pick', list);
  boxes.forEach((box, idx) => box.onclick = ev => {
    const ids = [box.dataset.id];
    if (ev.shiftKey && s.lastPick != null) { const [a, b] = [s.lastPick, idx].sort((x, y) => x - y); for (let i = a; i <= b; i++) ids.push(boxes[i].dataset.id); }
    const on = box.checked;
    for (const id of ids) { if (s.selectAll) { if (on) s.excluded.delete(id); else s.excluded.add(id); } else { if (on) s.picked.add(id); else s.picked.delete(id); } }
    s.lastPick = idx;
    paintInbox();
  });
}

async function renderInbox() {
  setLoading('Inbox');
  $('#view').innerHTML = '<div class="page-head"><div><h1>Inbox</h1><p id="inbox-count"></p></div></div>' + Object.entries(INBOX).map(([t, s]) =>
    `<div class="inbox-section"><div class="inbox-section-head"><h2>${s.title}</h2><span class="muted small">${s.blurb}</span></div><div id="inbox-${t}-bar"></div><div id="inbox-${t}-list"></div><div id="inbox-${t}-more" class="inbox-more"></div></div>`).join('');
  await Promise.all(Object.keys(INBOX).map(t => loadInbox(t)));
  paintInbox();
}

function paintInbox() {
  // The review list may be filtered; the headline count and the nav badge stay the whole open backlog.
  const review = { total: Object.values(inboxState.review.kinds || {}).reduce((a, b) => a + b, 0) };
  const { scan, search } = inboxState, total = scan.total + search.total + review.total, badge = $('#badge-inbox');
  if (badge) { badge.textContent = total || ''; badge.classList.toggle('hidden', !total); }
  const n = (s, one, many) => `${fmtNum(s.total)} ${s.total === 1 ? one : many}`;
  const kids = inboxState.review.kinds?.kids_tag || 0, recheck = inboxState.review.kinds?.kids_recheck || 0, meta = inboxState.review.kinds?.metadata_match || 0;
  $('#inbox-count').textContent = total ? `${n(scan, 'scanned file', 'scanned files')} · ${n(search, 'search match', 'search matches')} · ${fmtNum(meta)} unresolved book${meta === 1 ? '' : 's'} · ${fmtNum(kids)} kids shelf suggestion${kids === 1 ? '' : 's'}${recheck ? ` · ${fmtNum(recheck)} auto-tagged without audience evidence` : ''}` : 'Everything is matched - nothing needs attention.';
  paintReviewBar();
  for (const [type, src] of Object.entries(INBOX)) {
    const s = inboxState[type], list = $(`#inbox-${type}-list`), more = $(`#inbox-${type}-more`);
    list.innerHTML = s.items.length ? s.items.map(c => inboxCard(type, c)).join('') : `<div class="panel"><div class="empty"><strong>Nothing here</strong>${e(src.empty)}${type === 'review' && reviewFiltered(inboxState.review) ? '<div style="margin-top:12px"><button type="button" class="button secondary compact" id="review-empty-clear">Clear filters</button></div>' : ''}</div></div>`;
    $('#review-empty-clear', list)?.addEventListener('click', () => clearReviewFilters());
    $$('.inbox-item', list).forEach(card => bindEvidenceActions(card, src.actions(s.items.find(x => x.id === card.dataset.candidateId))));
    if (type === 'review') wireReviewPicks(list);
    more.innerHTML = s.items.length < s.total ? `<button class="button secondary compact">Show more ${src.noun}</button>` : '';
    if (more.firstChild) more.firstChild.onclick = async () => { await loadInbox(type, true); paintInbox(); };
  }
}

const REQUEST_SEARCH_TEXT={searching:'Searching',grabbed:'Found, downloading',no_match:'Not found yet',no_result:'Not found yet',skipped_cooldown:'Checked recently, will try again later',skipped_cap:'Search limit reached for today',skipped_active:'Already downloading or in the library',busy:'Search already running',throttled:'Search paused briefly, will retry',error:'Search failed, will retry'};
const REQUEST_TONE={good:'good',warn:'warn',bad:'bad',info:'info','':''};
async function renderRequests(status=''){
  setLoading('Requests');
  const [d,cfg]=await Promise.all([api(`/api/v1/requests${status?`?status=${status}`:''}`),api('/api/v1/settings/requests')]);
  $('#view').innerHTML=`<div class="page-head"><div><h1>Requests</h1><p>What the household asked for. Approving records acquisition intent on the book, exactly like Monitor; the wanted search, download and import pipeline does the rest.</p></div><div class="page-actions"><select class="select compact" id="req-filter">${[['','All'],['pending','Awaiting approval'],['approved','Approved'],['denied','Denied'],['cancelled','Cancelled']].map(([v,l])=>`<option value="${v}" ${status===v?'selected':''}>${l}</option>`).join('')}</select></div></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-body" style="display:flex;gap:18px;flex-wrap:wrap;align-items:center"><label class="field-inline"><span>Adult requests</span><select class="select compact" id="req-adult-policy"><option value="auto" ${cfg.adult_policy==='auto'?'selected':''}>Go straight to acquisition</option><option value="approve" ${cfg.adult_policy==='approve'?'selected':''}>Need approval</option></select></label><label class="field-inline"><input type="checkbox" id="req-kid-ext" ${cfg.kid_external_search?'checked':''}> Kids may request books outside the library</label><label class="field-inline"><input type="checkbox" id="req-search" ${cfg.search_on_approval?'checked':''}> Search once when a request is approved</label><span class="muted small">Kid requests always need approval. Approvers: Settings → Accounts.</span></div></div>
  <div class="panel table-wrap"><table><thead><tr><th>When</th><th>Who</th><th>Book</th><th>Formats</th><th>Status</th><th></th></tr></thead><tbody>${d.items.length?d.items.map(r=>`<tr><td>${fmtDate(r.created_at)}</td><td><strong>${e(r.requester_name||r.requester)}</strong><div class="small muted">${e(r.requester_type)}</div></td><td>${r.work_id?`<a href="#library/${escAttr(r.work_id)}">${e(r.title)}</a>`:e(r.title)}<div class="small muted">${e(r.author||'')}</div></td><td>${r.kinds.join(' + ')}</td><td><span class="badge ${REQUEST_TONE[r.tone]||''}">${e(r.friendly_status)}</span>${r.search&&REQUEST_SEARCH_TEXT[r.search.state]?`<div class="small muted">${e(REQUEST_SEARCH_TEXT[r.search.state])}</div>`:''}${r.decided_by?`<div class="small muted">by ${e(r.decided_by)}${r.decision_note?' · '+e(r.decision_note):''}</div>`:''}</td><td>${r.status==='pending'?`<button class="button compact req-approve" data-id="${r.id}">Approve</button> <button class="button ghost compact req-deny" data-id="${r.id}">Deny</button>`:''}</td></tr>`).join(''):`<tr><td colspan="6" class="muted">No requests${status?' with this status. <button type="button" class="button secondary compact" id="req-clear">Clear filters</button>':'.'}</td></tr>`}</tbody></table></div>`;
  $('#req-filter').onchange=ev=>renderRequests(ev.target.value);
  $('#req-clear')?.addEventListener('click',()=>renderRequests(''));
  const savePolicy=async()=>{try{await api('/api/v1/settings/requests',{method:'PUT',body:{adult_policy:$('#req-adult-policy').value,kid_external_search:$('#req-kid-ext').checked,search_on_approval:$('#req-search').checked}});toast('Request policy saved');}catch(er){toast(er.message,'bad')}};
  $('#req-adult-policy').onchange=savePolicy; $('#req-kid-ext').onchange=savePolicy; $('#req-search').onchange=savePolicy;
  $$('.req-approve').forEach(b=>b.onclick=async()=>{try{await api(`/api/v1/requests/${b.dataset.id}/approve`,{method:'POST',body:{}});toast('Approved');renderRequests(status)}catch(er){toast(er.message,'bad')}});
  $$('.req-deny').forEach(b=>b.onclick=async()=>{try{await api(`/api/v1/requests/${b.dataset.id}/deny`,{method:'POST',body:{}});toast('Denied');renderRequests(status)}catch(er){toast(er.message,'bad')}});
}

// Approvers and grants for one account (Settings → Accounts → Family).
async function familyDialog(u, all){
  const fam=await api(`/api/v1/users/${u.id}/family`);
  const adults=all.filter(x=>x.account_type==='adult'&&!fam.approvers.some(a=>a.id===x.id));
  const tags=(await api('/api/v1/tags')).items.filter(t=>t.name!=='kids'&&!fam.tag_grants.some(g=>g.tag_id===t.id));
  const isKid=fam.account_type==='kid';
  const m=modal(`${e(u.display_name||u.username)}: family`,`
    ${isKid?`<div class="setting-section"><h3>Approvers</h3><p>Adults who decide this kid's requests and may allow individual books. Administrators always can.</p>${fam.approvers.map(a=>`<div class="integration-card"><div><strong>${e(a.display_name||a.username)}</strong><span>since ${fmtDate(a.created_at)}</span></div><button class="button ghost compact fam-unassign" data-id="${a.id}">Remove</button></div>`).join('')||'<p class="muted small">None yet.</p>'}${adults.length?`<div class="field-inline" style="margin-top:8px"><select class="select compact" id="fam-adult">${adults.map(a=>`<option value="${a.id}">${e(a.display_name||a.username)}</option>`).join('')}</select><button class="button compact" id="fam-assign">Add approver</button></div>`:''}</div>
    <div class="setting-section"><h3>Allowed books</h3><p>Beyond the kids shelf. Approvers can manage these from the Player too.</p>${fam.grants.map(g=>`<div class="integration-card"><div><strong>${e(g.title)}</strong><span>${g.note?e(g.note)+' · ':''}by ${e(g.granted_by||'?')}</span></div><button class="button ghost compact fam-ungrant" data-id="${g.work_id}">Remove</button></div>`).join('')||'<p class="muted small">None.</p>'}<div class="field-inline" style="margin-top:8px"><input class="input compact" id="fam-work" placeholder="Search a title to allow…"><div id="fam-work-results"></div></div></div>
    <div class="setting-section"><h3>Allowed collections</h3><p>Every book carrying one of these tags, now and later. Administrators only.</p>${fam.tag_grants.map(g=>`<div class="integration-card"><div><strong>${e(g.name)}</strong></div><button class="button ghost compact fam-untag" data-id="${g.tag_id}">Remove</button></div>`).join('')||'<p class="muted small">None.</p>'}${tags.length?`<div class="field-inline" style="margin-top:8px"><select class="select compact" id="fam-tag">${tags.map(t=>`<option value="${t.id}">${e(t.name)} (${t.work_count})</option>`).join('')}</select><button class="button compact" id="fam-tag-add">Allow collection</button></div>`:''}</div>`
    :`<div class="setting-section"><h3>Approves for</h3><p>Kids whose requests this adult decides. Assign from the kid's own Family dialog.</p>${fam.approves_for.map(k=>`<div class="integration-card"><div><strong>${e(k.display_name||k.username)}</strong><span>${k.grants} allowed book${k.grants===1?'':'s'}</span></div></div>`).join('')||'<p class="muted small">Nobody yet.</p>'}</div>`}`,{footer:'<button class="button ghost modal-cancel">Close</button>'});
  $('.modal-cancel',m).onclick=closeModal;
  const act=async(fn)=>{try{await fn();closeModal();familyDialog(u,all)}catch(er){toast(er.message,'bad')}};
  $('#fam-assign',m)?.addEventListener('click',()=>act(()=>api(`/api/v1/users/${u.id}/approvers/${$('#fam-adult',m).value}`,{method:'PUT',body:{}})));
  $$('.fam-unassign',m).forEach(b=>b.onclick=()=>act(()=>api(`/api/v1/users/${u.id}/approvers/${b.dataset.id}`,{method:'DELETE'})));
  $$('.fam-ungrant',m).forEach(b=>b.onclick=()=>act(()=>api(`/api/v1/users/${u.id}/grants/${b.dataset.id}`,{method:'DELETE'})));
  $$('.fam-untag',m).forEach(b=>b.onclick=()=>act(()=>api(`/api/v1/users/${u.id}/tag-grants/${b.dataset.id}`,{method:'DELETE'})));
  $('#fam-tag-add',m)?.addEventListener('click',()=>act(()=>api(`/api/v1/users/${u.id}/tag-grants/${$('#fam-tag',m).value}`,{method:'PUT',body:{}})));
  let t; $('#fam-work',m)?.addEventListener('input',ev=>{clearTimeout(t);const q=ev.target.value.trim();t=setTimeout(async()=>{const el=$('#fam-work-results',m);if(!q){el.innerHTML='';return}const d=await api(`/api/v1/library?q=${encodeURIComponent(q)}&limit=8`);el.innerHTML=d.items.map(w=>`<button class="button ghost compact fam-grant" data-id="${w.id}" style="margin:4px 4px 0 0">${e(w.title)}</button>`).join('')||'<span class="muted small">No owned book matches.</span>';$$('.fam-grant',el).forEach(b=>b.onclick=()=>act(()=>api(`/api/v1/users/${u.id}/grants/${b.dataset.id}`,{method:'PUT',body:{}})))},260)});
}

async function renderHistory(){setLoading('History');const d=await api('/api/v1/history?limit=500');$('#view').innerHTML=`<div class="page-head"><div><h1>History</h1><p>Auditable lifecycle events across metadata, acquisition, and library management.</p></div></div><div class="panel table-wrap"><table><thead><tr><th>Time</th><th>Event</th><th>Book</th><th>Message</th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${fmtDate(x.created_at)}</td><td><span class="badge">${e(x.event_type)}</span></td><td>${e(x.title||'—')}</td><td>${e(x.message)}</td></tr>`).join('')}</tbody></table></div>`}

async function renderSettings(){
  setLoading('Settings'); const tab=state.settingsTab;
  const kids=isKidsScope();
  const tabs = settingsTabsFor(kids
    ? [['users','Accounts'],['about','About']]
    : [['roots','Library roots'],['indexers','Indexers'],['clients','Download clients'],['naming','Files & naming'],['migration','Migration'],['users','Accounts'],['apikeys','API keys'],['tags','Tags'],['devices','Devices'],['jobs','Jobs'],['conversion','Conversion'],['discover','Discover chat'],['system','System'],['about','About']], acquisitionOn(state.me));
  const activeTab = tabs.some(([id])=>id===tab) ? tab : tabs[0][0];
  state.settingsTab = activeTab;
  $('#view').innerHTML=`<div class="page-head"><div><h1>Settings</h1><p>${kids?'Your account.':'Integrations, storage behavior, migrations, naming, and system state.'}</p></div></div><div class="tabs" id="settings-tabs">${tabs.map(([id,l])=>`<button class="tab ${activeTab===id?'active':''}" data-stab="${id}">${l}</button>`).join('')}</div><div id="settings-body"></div>`;
  $$('[data-stab]').forEach(b=>b.onclick=()=>{state.settingsTab=b.dataset.stab;renderSettings()}); await renderSettingsTab(activeTab);
}
async function renderSettingsTab(tab){const el=$('#settings-body');if(tab==='roots')return settingsRoots(el);if(tab==='indexers')return settingsIndexers(el);if(tab==='clients')return settingsClients(el);if(tab==='naming')return settingsNaming(el);if(tab==='migration')return settingsMigration(el);if(tab==='users')return settingsUsers(el);if(tab==='apikeys')return settingsApiKeys(el);if(tab==='tags')return settingsTags(el);if(tab==='devices')return settingsDevices(el);if(tab==='jobs')return settingsJobs(el);if(tab==='conversion')return settingsConversionTab(el);if(tab==='discover')return settingsDiscover(el);if(tab==='system')return settingsSystem(el);if(tab==='about')return settingsAbout(el);}

async function settingsApiKeys(el){
  const admin=!!state.me?.is_admin;
  const d=await api('/api/v1/auth/api-keys'+(admin?'?all=true':''));
  const scopeLabel={read:'Read-only',write:'Read & write',admin:'Admin'};
  el.innerHTML=`<div class="setting-section"><h3>API keys</h3><p>Let scripts, agents and OPDS readers use ${e(PRODUCT)} without signing in through Authentik. A key acts as the account that created it, capped to the access level you pick. Send it as <span class="mono">Authorization: Bearer alex_…</span>, or as the password for an OPDS reader (any username).</p></div>
  <div class="panel"><div class="panel-head"><h2>Keys</h2><button class="button compact" id="key-add">New key</button></div><div class="panel-body">${d.keys.length?d.keys.map(k=>`<div class="integration-card"><div><strong>${e(k.name)}</strong><span><span class="mono">${e(k.key_prefix||'alex_…')}…</span> · ${e(scopeLabel[k.scope]||k.scope)}${admin?` · ${e(k.owner)}`:''} · created ${e(fmtDate(k.created_at))} · ${k.last_used_at?`last used ${e(fmtDate(k.last_used_at))}`:'never used'}${k.expires_at?` · ${k.expired?'expired':'expires'} ${e(fmtDate(k.expires_at))}`:''}</span></div><button class="button ghost compact key-revoke" data-id="${escAttr(k.id)}" data-name="${escAttr(k.name)}">Revoke</button></div>`).join(''):'<p class="muted">No API keys yet.</p>'}</div></div>`;
  $('#key-add').onclick=()=>{
    const m=modal('New API key',`<form id="key-form" class="form-grid"><div class="field full"><label>Name</label><input class="input" name="name" placeholder="e.g. an external client infrastructure agent" required></div>
      <div class="field"><label>Access</label><select class="select" name="scope"><option value="read">Read-only</option><option value="write">Read &amp; write</option>${admin?'<option value="admin">Admin</option>':''}</select></div>
      <div class="field"><label>Expires</label><select class="select" name="expires_days"><option value="">Never</option><option value="30">In 30 days</option><option value="90">In 90 days</option><option value="365">In a year</option></select></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="key-save">Create key</button>'});
    $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
    $('#key-save',m).onclick=async()=>{
      const f=Object.fromEntries(new FormData($('#key-form')));
      if(!f.name.trim()) return toast('Give the key a name','warn');
      try{
        const r=await api('/api/v1/auth/api-keys',{method:'POST',body:{name:f.name.trim(),scopes:[f.scope],expires_days:f.expires_days?Number(f.expires_days):null}});
        const m2=modal('Copy your key now',`<p>This is the only time the key is shown. Store it somewhere safe - if it is lost, revoke it and make a new one.</p><input class="input mono" id="key-once" value="${escAttr(r.key)}" readonly style="width:100%">`,{footer:'<button class="button ghost" id="key-copy">Copy</button><button class="button" id="key-done">Done</button>'});
        $('#key-once',m2).select();
        $('#key-copy',m2).onclick=async()=>{try{await navigator.clipboard.writeText(r.key);toast('Copied')}catch(_){$('#key-once',m2).select();toast('Press Ctrl+C to copy','warn')}};
        $('#key-done',m2).onclick=()=>{$('#modal-root').innerHTML='';renderSettings()};
      }catch(er){toast(er.message,'bad')}
    };
  };
  $$('.key-revoke').forEach(b=>b.onclick=async()=>{if(!confirm(`Revoke "${b.dataset.name}"? Anything using it stops working immediately.`))return;try{await api(`/api/v1/auth/api-keys/${b.dataset.id}`,{method:'DELETE'});toast('Key revoked');renderSettings()}catch(er){toast(er.message,'bad')}});
}
async function settingsUsers(el){
  const kids=isKidsScope();
  if(kids){
    el.innerHTML=`<div class="setting-section"><h3>Your account</h3><p>Where "Send to Kindle" delivers your books.</p></div><div class="panel"><div class="panel-body"><form id="self-form" class="form-grid"><div class="field full"><label>Kindle email</label><input class="input" name="kindle_email" value="${escAttr(state.me.kindle_email||'')}" placeholder="you@kindle.com"></div><button class="button" style="width:max-content">Save</button></form></div></div>`;
    $('#self-form').onsubmit=async ev=>{ev.preventDefault();const f=new FormData(ev.target);try{await api('/api/v1/auth/me',{method:'PATCH',body:Object.fromEntries(f)});state.me.kindle_email=f.get('kindle_email');toast('Saved');}catch(er){toast(er.message,'bad')}};
    return;
  }
  const d=await api('/api/v1/users');
  el.innerHTML=`<div class="setting-section"><h3>Accounts</h3><p>Four account types. <strong>Administrator</strong>: Management and Player. <strong>Adult</strong>: the Player, every book, requests go straight to acquisition. <strong>Kid</strong>: the Player over the kids shelf, grants and approved requests only. <strong>Pending</strong>: an Authentik identity nobody has placed yet; it can only sign in and out until you pick a type here. Authentik-linked accounts can also be placed by group (see docs/PLAYER_MANAGEMENT.md).</p></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Staying signed in</h2></div><div class="panel-body" id="signin-settings"><p class="muted small">Loading…</p></div></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Your account</h2></div><div class="panel-body"><form id="self-form" class="form-grid"><div class="field full"><label>Your Kindle email</label><input class="input" name="kindle_email" value="${escAttr(state.me.kindle_email||'')}" placeholder="you@kindle.com"></div><button class="button" style="width:max-content">Save</button></form></div></div>
  <div class="panel"><div class="panel-head"><h2>Accounts</h2><button class="button compact" id="user-add">Add account</button></div><div class="panel-body">${d.items.map(u=>`<div class="integration-card"><div><strong>${e(u.display_name||u.username)}</strong><span>${e(u.username)} · ${ACCOUNT_TYPE_LABEL[u.account_type]||e(u.account_type)} · ${e(u.auth_source)}${u.linked_identity?' · Authentik-linked':''}${u.kindle_email?' · Kindle: '+e(u.kindle_email):''}</span></div><div class="integration-actions">${u.account_type==='kid'||u.account_type==='adult'?`<button class="button secondary compact user-family" data-id="${u.id}">Family</button>`:''}<button class="button ghost compact user-edit" data-id="${u.id}">Edit</button><button class="button danger compact user-delete" data-id="${u.id}">Remove</button></div></div>`).join('')}</div></div>`;
  $('#self-form').onsubmit=async ev=>{ev.preventDefault();const f=new FormData(ev.target);try{await api('/api/v1/auth/me',{method:'PATCH',body:Object.fromEntries(f)});state.me.kindle_email=f.get('kindle_email');toast('Saved');}catch(er){toast(er.message,'bad')}};
  signinSettingsPanel();
  $('#user-add').onclick=()=>userEditDialog(null);
  $$('.user-edit').forEach(b=>b.onclick=()=>userEditDialog(d.items.find(u=>u.id===b.dataset.id)));
  $$('.user-family').forEach(b=>b.onclick=()=>familyDialog(d.items.find(u=>u.id===b.dataset.id),d.items).catch(er=>toast(er.message,'bad')));
  $$('.user-delete').forEach(b=>b.onclick=async()=>{ if(!confirm('Remove this account? This also deletes its trusted devices and sessions. Removing someone from Authentik does NOT sign them out of '+PRODUCT+': remembered sessions and trusted devices keep working until you remove the account or revoke them here.')) return; try{ await api(`/api/v1/users/${b.dataset.id}`,{method:'DELETE'}); renderSettings(); }catch(er){ toast(er.message,'bad'); } });
}

const SIGNIN_TYPES=[['admin','Administrators'],['adult','Adults'],['kid','Kids']];
async function signinSettingsPanel(){
  const box=$('#signin-settings'); if(!box) return;
  const c=await api('/api/v1/settings/signin');
  const types=(key,name)=>SIGNIN_TYPES.map(([v,l])=>`<label class="check-line" style="display:inline-flex;gap:6px;margin-right:12px"><input type="checkbox" data-grp="${name}" value="${v}" ${c[key].includes(v)?'checked':''}> ${l}</label>`).join('');
  box.innerHTML=`<p class="help">Optional ways to avoid signing in every day. They do not change Authentik. Management always needs the normal sign-in; a trusted device only opens the Player.</p>
  <form id="signin-form" class="form-grid">
    <div class="field"><label>Remember me (days, 0 = off)</label><input class="input" type="number" min="0" max="365" name="remember_days" value="${c['auth.remember_days']}"></div>
    <div class="field"><label>Longest a remembered sign-in can last (days)</label><input class="input" type="number" min="1" max="730" name="remember_absolute_days" value="${c['auth.remember_absolute_days']}"></div>
    <div class="field full"><label>Remember me allowed for</label><div>${types('auth.remember_allowed_types','rem')}</div></div>
    <div class="field full"><label>Trust this device allowed for</label><div>${types('auth.trust_device_allowed_types','trust')}</div></div>
    <div class="field"><label>Trusted devices per person</label><input class="input" type="number" min="1" max="50" name="trusted_devices_max" value="${c['auth.trusted_devices_max']}"></div>
    <div class="field"><label>Revoke trusted devices unused for (days, 0 = never)</label><input class="input" type="number" min="0" max="3650" name="trusted_device_idle_days" value="${c['auth.trusted_device_idle_days']}"></div>
    <button class="button" style="width:max-content">Save</button></form>`;
  $('#signin-form').onsubmit=async ev=>{ev.preventDefault();const f=new FormData(ev.target);const grp=n=>$$(`[data-grp=${n}]:checked`,ev.target).map(i=>i.value);
    try{await api('/api/v1/settings/signin',{method:'PUT',body:{remember_days:Number(f.get('remember_days')),remember_absolute_days:Number(f.get('remember_absolute_days')),remember_allowed_types:grp('rem'),trust_device_allowed_types:grp('trust'),trusted_devices_max:Number(f.get('trusted_devices_max')),trusted_device_idle_days:Number(f.get('trusted_device_idle_days'))}});toast('Saved');}catch(er){toast(er.message,'bad')}};
}
const ACCOUNT_TYPE_LABEL={admin:'Administrator',adult:'Adult',kid:'Kid',pending:'Pending (no access yet)'};
function userEditDialog(u){
  const isNew=!u;
  const m=modal(isNew?'Add household account':`Edit ${u.username}`,`<form id="user-form" class="form-grid">
    <div class="field"><label>Username</label><input class="input" name="username" value="${escAttr(u?.username||'')}" ${isNew?'required':'disabled'}></div>
    ${isNew?`<div class="field"><label>Password</label><input class="input" name="password" type="password" minlength="12" required><div class="help">At least 12 characters.</div></div>`:''}
    <div class="field"><label>Display name</label><input class="input" name="display_name" value="${escAttr(u?.display_name||'')}"></div>
    <div class="field"><label>Account type</label><select class="select" name="account_type">${Object.entries(ACCOUNT_TYPE_LABEL).map(([v,l])=>`<option value="${v}" ${(u?u.account_type:'adult')===v?'selected':''}>${l}</option>`).join('')}</select><div class="help">Pending keeps the person signed in but with no access until you choose.</div></div>
    <div class="field full"><label>Kindle email</label><input class="input" name="kindle_email" value="${escAttr(u?.kindle_email||'')}" placeholder="name@kindle.com"></div>
    ${isNew?'':`<div class="field full"><label class="check-line" style="display:flex;gap:8px"><input type="checkbox" name="revoke_devices" value="1"> <span>Also revoke this person's trusted devices</span></label><div class="help">Trusted devices stay signed in through a role change unless you tick this. Moving someone to Pending (disabling them) always revokes them. Removing a person from Authentik does not end their remembered sessions or trusted devices; revoke them here.</div></div>`}
  </form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="user-save">Save</button>'});
  $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
  // Disabling someone (Pending) revokes their trusted devices, so the box starts ticked and says why.
  const typeSel=$('[name=account_type]',m), rv=$('[name=revoke_devices]',m);
  if(typeSel&&rv){ typeSel.onchange=()=>{ if(typeSel.value==='pending') rv.checked=true; }; }
  $('#user-save',m).onclick=async()=>{
    const f=new FormData($('#user-form',m));
    const body={display_name:f.get('display_name')||null, account_type:f.get('account_type'), kindle_email:f.get('kindle_email')||null, revoke_devices:!!f.get('revoke_devices')};
    try{
      if(isNew){ await api('/api/v1/users',{method:'POST',body:{username:f.get('username'),password:f.get('password'),display_name:body.display_name,account_type:body.account_type}}); }
      else { await api(`/api/v1/users/${u.id}`,{method:'PATCH',body}); }
      $('#modal-root').innerHTML=''; toast('Saved'); renderSettings();
    }catch(er){ toast(er.message,'bad'); }
  };
}

async function kidsAutoDialog(){
  const d=await api('/api/v1/tags/kids-auto?limit=500');
  const m=modal('Auto-approved kids books',`<p class="muted small">${d.total} book(s) the classifier put on the kids shelf. <strong>Remove and lock off</strong> takes them off the shelf and records a manual decision the classifier will never revisit. <strong>Remove and re-evaluate</strong> takes them off and forgets the verdict, so the next scan asks the model again (and may auto-approve again while the setting is on).</p>
    <div style="max-height:50vh;overflow:auto">${d.items.map(x=>`<label class="integration-card" style="cursor:pointer"><input type="checkbox" class="ka-pick" value="${escAttr(x.work_id)}" checked> <div><strong>${e(x.title)}</strong><span>confidence ${x.confidence??'?'} · ${e(x.model||'model unknown')} · ${fmtDate(x.decided_at)}</span></div></label>`).join('')||'<p class="muted">None.</p>'}</div>`,
    {footer:'<button class="button ghost modal-cancel">Close</button><button class="button secondary" id="ka-reeval">Remove and re-evaluate</button><button class="button danger" id="ka-lock">Remove and lock off</button>'});
  $('.modal-cancel',m).onclick=closeModal;
  const go=async mode=>{const ids=$$('.ka-pick:checked',m).map(i=>i.value);if(!ids.length)return toast('Nothing selected','bad');if(!confirm(`Remove ${ids.length} book(s) from the kids shelf (${mode==='lock_off'?'locked off':'re-evaluate later'})?`))return;try{const r=await api('/api/v1/tags/kids-auto/revert',{method:'POST',body:{work_ids:ids,mode}});toast(`Reverted ${r.reverted}`);closeModal();renderSettings()}catch(er){toast(er.message,'bad')}};
  $('#ka-lock',m).onclick=()=>go('lock_off'); $('#ka-reeval',m).onclick=()=>go('reevaluate');
}
// Dry-run first: the count of auto-approved books that fail the evidence gate. Queuing them writes
// Inbox tasks only; nobody is taken off the shelf until an administrator keeps or removes them.
async function kidsRecheckDialog(){
  const d=await api('/api/v1/tags/kids-auto/recheck');
  const why=Object.entries(d.by_blocker||{}).map(([k,n])=>`<li>${fmtNum(n)} - ${e(({llm_not_kids:'model did not say kids',below_threshold:'confidence below the threshold',llm_reported_adult_signals:'model reported adult signals',hard_adult_signals:'adult signals in the metadata',no_strong_audience_signal:'no strong audience signal',description_too_short:'no usable description'})[k]||k)}</li>`).join('');
  const m=modal('Re-check auto-approved books',`<p id="kr-summary"><strong>${fmtNum(d.pass)}</strong> of ${fmtNum(d.checked)} auto-approved book(s) pass the audience-evidence gate and stay as they are. <strong>${fmtNum(d.fail)}</strong> do not.</p>${why?`<ul class="small">${why}</ul>`:''}${d.audience_coverage?`<p class="muted small">${fmtNum(d.with_audience_metadata||0)} of these have provider categories or subjects on record. ${e(audienceCoverageText(d.audience_coverage))}.</p>`:''}<p class="muted small">Queuing puts the ${fmtNum(d.fail)} in the Inbox under "Auto-tagged without audience evidence" with their reasons. <strong>Nothing is removed</strong>: they stay on the kids shelf until you keep or remove them there. No model is called. Safe to run again.</p>`,
    {footer:`<button class="button ghost modal-cancel">Close</button><button class="button" id="kr-go" ${d.fail?'':'disabled'}>Queue ${fmtNum(d.fail)} for review</button>`});
  $('.modal-cancel',m).onclick=closeModal;
  $('#kr-go',m).onclick=async()=>{try{const r=await api('/api/v1/tags/kids-auto/recheck',{method:'POST',body:{dry_run:false}});closeModal();toast(`${fmtNum(r.fail)} book(s) are waiting in the Inbox`);location.hash='inbox'}catch(er){toast(er.message,'bad')}};
}
async function settingsTags(el){
  const d=await api('/api/v1/tags');
  el.innerHTML=`<div class="setting-section"><h3>Tags</h3><p>The <strong>Kids shelf</strong> tag is what a restricted account is allowed to see - everything else is a plain organizational label. Its name is fixed (it is a protected system tag); it can be added to and removed from books, one at a time on a book's page or many at once from the Library.</p></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Kids shelf classifier</h2></div><div class="panel-body"><p class="muted small">When a local AI model is configured, a scan reads each owned book's title, author and description (never the file or cover) and proposes the <strong>Kids shelf</strong> tag for the ones it is confident about. With <strong>auto-approve</strong> on, a proposal at or above the threshold is applied at once (recorded as "auto-approved" with its confidence and model, auditable in History); below it, or when the model abstains, it waits in the <a href="#inbox">Inbox</a>. A tag you add or remove by hand always wins and is never touched by the classifier.</p>
    <div id="kids-auto" class="muted small">Loading…</div><div style="margin-top:10px"><button class="button secondary" id="classify-all">Scan 20 unchecked books</button></div></div></div>
  <div class="panel"><div class="panel-head"><h2>All tags</h2><button class="button compact" id="tag-add">New tag</button></div><div class="panel-body">${d.items.map(t=>`<div class="integration-card"><div><strong>${e(tagLabel(t))}</strong><span>${t.work_count} book${t.work_count===1?'':'s'}${t.kind==='system'?' · system':''}</span></div><div class="integration-actions">${t.work_count?`<button class="button ghost compact tag-view" data-id="${escAttr(t.id)}">View books</button>`:''}${t.kind==='system'?'<span class="badge info">Protected</span>':`<button class="button danger compact tag-delete" data-id="${t.id}">Delete</button>`}</div></div>`).join('')}</div></div>`;
  $('#tag-add').onclick=()=>{
    const m=modal('New tag','<div class="field"><label>Name</label><input class="input" id="new-tag-name" required></div>',{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="new-tag-save">Create</button>'});
    $('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';
    $('#new-tag-save',m).onclick=async()=>{ const name=$('#new-tag-name',m).value.trim(); if(!name) return; try{ await api('/api/v1/tags',{method:'POST',body:{name}}); $('#modal-root').innerHTML=''; renderSettings(); }catch(er){ toast(er.message,'bad'); } };
  };
  $$('.tag-view').forEach(b=>b.onclick=()=>{ Object.assign(libraryState,{tagId:b.dataset.id,q:'',kind:'',status:'',seriesId:'',readStatus:'',selectMode:false}); libraryState.selected.clear(); location.hash='library'; });
  $$('.tag-delete').forEach(b=>b.onclick=async()=>{ if(!confirm('Delete this tag? It will be removed from every book that has it.')) return; try{ await api(`/api/v1/tags/${b.dataset.id}`,{method:'DELETE'}); renderSettings(); }catch(er){ toast(er.message,'bad'); } });
  $('#classify-all').onclick=async()=>{
    const btn=$('#classify-all'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Starting';
    try{ await runKidsScan(20); }
    catch(er){ toast(er.message,'bad'); }
    finally{ btn.disabled=false; btn.textContent='Scan 20 unchecked books'; }
  };
  api('/api/v1/settings/kids').then(cfg=>{const box=$('#kids-auto');if(!box)return;const paint=c=>{box.innerHTML=`<label class="field-inline"><input type="checkbox" id="kids-auto-on" ${c.kids_auto_approve?'checked':''}> Auto-approve confident proposals</label> <label class="field-inline">at confidence ≥ <input class="input compact" id="kids-auto-th" type="number" min="${c.propose_threshold}" max="1" step="0.01" value="${c.kids_auto_threshold}" style="width:84px"></label> <label class="field-inline" title="Without this, a confident verdict alone can tag a book. With it, the metadata must also show a children's category, a children's publisher or an age range up to 12."><input type="checkbox" id="kids-auto-strong" ${c.kids_require_strong_evidence?'checked':''}> Require strong audience evidence</label> <button class="button compact" id="kids-auto-save">Save</button> <span class="muted">${fmtNum(c.auto_approved_count)} auto-approved · ${fmtNum(c.pending_proposals)} waiting in the Inbox</span><div class="small" style="margin-top:8px" id="audience-cov"><span class="muted">${e(audienceCoverageText(c.audience_coverage))}</span> <button class="button secondary compact" id="audience-backfill-now" ${c.audience_coverage&&c.audience_coverage.running?'disabled':''} title="Looks up categories, subjects and maturity ratings (Open Library, Google Books) for works that lack them, a few at a time">Run backfill now</button></div>${c.auto_approved_count?` <button class="button ghost compact" id="kids-auto-list">Review auto-approved</button> <button class="button secondary compact" id="kids-recheck">Re-check auto-approved books</button>`:''}`;
    $('#kids-auto-save',box).onclick=async()=>{try{const r=await api('/api/v1/settings/kids',{method:'PUT',body:{kids_auto_approve:$('#kids-auto-on',box).checked,kids_auto_threshold:Number($('#kids-auto-th',box).value),kids_require_strong_evidence:$('#kids-auto-strong',box).checked}});toast(r.sweep&&r.sweep.approved?`Saved; ${r.sweep.approved} waiting proposal(s) approved`:'Saved');paint(r)}catch(er){toast(er.message,'bad')}};
    $('#audience-backfill-now',box)?.addEventListener('click',async ev=>{const b=ev.currentTarget;b.disabled=true;try{const r=await api('/api/v1/tags/audience-backfill',{method:'POST',body:{}});toast('Audience backfill started');c.audience_coverage=r.coverage;paint(c)}catch(er){toast(er.message,'bad');b.disabled=false}});
    $('#kids-auto-list',box)?.addEventListener('click',kidsAutoDialog);
    $('#kids-recheck',box)?.addEventListener('click',kidsRecheckDialog);};paint(cfg)}).catch(()=>{});
}

async function settingsDevices(el){
  const d=await api('/api/v1/settings/devices');
  el.innerHTML=`<div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Paired reading devices</h2><span class="muted small">Player API credentials; revoking signs the device out</span></div><div class="panel-body" id="paired-devices"><span class="muted small">Loading…</span></div></div><div class="setting-section"><h3>Devices</h3><p>Deliver a book already on the shelf straight to a reader.</p></div>
  <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Send to Kindle</h2></div><div class="panel-body"><p class="muted small">Uses Amazon's Send-to-Kindle email address, which accepts EPUB directly. Each account sets its own destination address under Settings → Household.</p><form id="smtp-form" class="form-grid">
    <div class="field"><label>SMTP host</label><input class="input" name="host" value="${escAttr(d.host)}" required></div>
    <div class="field"><label>Port</label><input class="input" name="port" type="number" value="${d.port}"></div>
    <div class="field"><label>Username</label><input class="input" name="username" value="${escAttr(d.username)}"></div>
    <div class="field"><label>Password</label><input class="input" name="password" type="password" placeholder="${d.configured?'Unchanged':''}"></div>
    <div class="field full"><label>From address</label><input class="input" name="from_email" type="email" value="${escAttr(d.from_email)}" required></div>
    <div class="field"><label>TLS</label><select class="select" name="tls_mode">${[['starttls','STARTTLS (usually port 587)'],['ssl','Implicit TLS (port 465)'],['none','None (plain, e.g. a LAN relay on 25)']].map(([v,l])=>`<option value="${v}" ${(d.tls_mode||'starttls')===v?'selected':''}>${l}</option>`).join('')}</select></div>
    <div class="field-inline"><button class="button">Save</button> <button type="button" class="button secondary" id="smtp-test" ${d.configured?'':'disabled'}>Send test email</button></div>
    <div id="smtp-test-result" class="small muted" style="grid-column:1/-1"></div>
  </form></div></div>
  <div class="panel"><div class="panel-head"><h2>Kobo</h2></div><div class="panel-body"><p class="muted small">Kobo has no equivalent wireless "send" pipeline the way Kindle does. Instead, every owned epub offers a <strong>Download for Kobo</strong> button that converts it to a <span class="mono">.kepub.epub</span> on the fly - the format Kobo's own software needs to track per-paragraph reading position and highlights, not just plain EPUB. Transfer the download to the device over USB or your usual sync.</p></div></div>`;
  $('#smtp-form').onsubmit=async ev=>{
    ev.preventDefault(); const f=new FormData(ev.target);
    const body={host:f.get('host'),port:Number(f.get('port')||587),username:f.get('username')||'',password:f.get('password')||null,from_email:f.get('from_email'),tls_mode:f.get('tls_mode')||'starttls',use_tls:f.get('tls_mode')==='starttls'};
    try{ await api('/api/v1/settings/devices',{method:'PUT',body}); toast('Saved'); renderSettings(); }catch(er){ toast(er.message,'bad'); }
  };
  // "Send test email": the relay's exact answer, success or failure, shown in place
  // (the server answers 200 with ok:false on failure so no proxy can swallow the text).
  const tb=$('#smtp-test'); if(tb) tb.onclick=async()=>{ const out=$('#smtp-test-result'); tb.disabled=true; out.textContent='Sending a test message to your own address…'; out.style.color='';
    try{ const r=await api('/api/v1/settings/devices/test',{method:'POST',body:{}}); out.textContent=r.message; out.style.color=r.ok?'var(--good)':'var(--bad)'; }
    catch(er){ out.textContent=er.message; out.style.color='var(--bad)'; }
    finally{ tb.disabled=false; } };
  api('/api/v1/devices?include_revoked=true').then(d0=>{const box=$('#paired-devices');if(!box)return;const alerts=d0.items.filter(x=>x.reuse_detected_at).map(x=>`<div class="warning-box"><strong>Possible stolen sign-in:</strong> ${e(x.owner)}'s trusted browser "${e(x.name)}" presented an old token on ${fmtDate(x.reuse_detected_at)}; it was revoked.</div>`).join('');const d={items:d0.items.filter(x=>!x.revoked_at)};box.innerHTML=alerts+(d.items.length?d.items.map(x=>`<div class="integration-card"><div><strong>${e(x.name)}</strong><span>${e(x.owner)} · ${x.kind==='browser'?'trusted browser · '+e(x.ua_summary||'browser')+(x.last_ip_prefix?' · '+e(x.last_ip_prefix):''):e(x.platform||'device')+' · '+e(x.token_prefix||'')+'…'} · last seen ${x.last_seen_at?fmtDate(x.last_seen_at):'never'}</span></div><div class="integration-actions"><button class="button danger compact dev-revoke" data-id="${x.id}">Revoke</button></div></div>`).join(''):'<div class="empty"><strong>No devices paired</strong>People pair readers from the Player (Settings → Pair).</div>');$$('.dev-revoke',box).forEach(b=>b.onclick=async()=>{if(!confirm('Revoke this device? It signs out immediately.'))return;try{await api(`/api/v1/devices/${b.dataset.id}`,{method:'DELETE'});toast('Device revoked');settingsDevices(el)}catch(er){toast(er.message,'bad')}})}).catch(()=>{});
}

async function settingsRoots(el){const d=await api('/api/v1/roots');el.innerHTML=`<div class="setting-section"><h3>Library roots</h3><p>Adopt mode catalogs existing files in place. Managed mode is required before ${e(PRODUCT)} will organize newly completed downloads.</p><div class="warning-box"><strong>Safety invariant:</strong> scanning a root never renames or moves an existing file.</div></div><div class="panel"><div class="panel-head"><h2>Roots</h2><button class="button compact" id="root-add">Add root</button></div><div class="panel-body">${d.items.length?d.items.map(x=>`<div class="integration-card" data-root="${escAttr(x.id)}"><div><strong>${e(x.name)}</strong><span>${e(x.path)} · ${e(x.media_kind)} · ${e(x.management_mode)}${x.last_scan_at?' · scanned '+fmtDate(x.last_scan_at):''}</span></div><div class="integration-actions"><button class="button secondary compact root-scan" data-id="${x.id}">Scan</button></div></div>`).join(''):'<div class="empty"><strong>No roots configured</strong>Add your current ebook and audiobook paths in adopt mode.</div>'}</div></div><div id="mb-panel-host"></div>`;$('#root-add').onclick=openAddRoot;managedBuildRootsHook(el,d.items);$$('.root-scan').forEach(b=>b.onclick=async()=>{b.disabled=true;b.innerHTML='<span class="spinner"></span>';try{const x=await api(`/api/v1/roots/${b.dataset.id}/scan`,{method:'POST',body:{auto_adopt_exact:true}});toast(`Scan: ${x.files_seen} files, ${x.matched} matched, ${x.ambiguous} ambiguous`);settingsRoots(el)}catch(er){toast(er.message,'bad');b.disabled=false;b.textContent='Scan'}})}
// 'Podcasts' is a library type, not a media kind: the server stores it as a read-only audiobook-kind root.
function rootPayload(f){ return f.media_kind==='podcasts'?{...f,media_kind:'audiobook',library_type:'podcasts'}:f; }
function openAddRoot(){const m=modal('Add library root',`<form id="root-form" class="form-grid"><div class="field"><label>Name</label><input class="input" name="name" required placeholder="Audiobooks"></div><div class="field"><label>Media</label><select class="select" name="media_kind"><option value="audiobook">Audiobook</option><option value="ebook">Ebook</option><option value="podcasts">Podcasts</option><option value="mixed">Mixed</option></select></div><div class="field full"><label>Container path</label><input class="input mono" name="path" required placeholder="/audiobooks"><div class="help">Use the path visible inside the ${e(PRODUCT)} container.</div></div><div class="field"><label>Management mode</label><select class="select" name="management_mode"><option value="adopt">Adopt in place</option><option value="observe">Observe only</option><option value="managed">Fully managed</option></select></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="root-save">Save root</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#root-save',m).onclick=async()=>{const f=new FormData($('#root-form'));try{await api('/api/v1/roots',{method:'POST',body:rootPayload(Object.fromEntries(f))});toast('Root added');$('#modal-root').innerHTML='';renderSettings()}catch(er){toast(er.message,'bad')}}}

async function settingsIndexers(el){const d=await api('/api/v1/indexers');el.innerHTML=`<div class="setting-section"><h3>Indexer fabric</h3><p>Prowlarr is first-class; direct Newznab and Torznab remain supported protocol adapters.</p></div><div class="panel"><div class="panel-head"><h2>Indexers</h2><button class="button compact" id="indexer-add">Add indexer</button></div><div class="panel-body">${d.items.length?d.items.map(x=>integrationCard(x,'indexer')):'<div class="empty"><strong>No indexers</strong>Add Prowlarr or a Newznab/Torznab endpoint.</div>'}</div></div>`;$('#indexer-add').onclick=openAddIndexer;$$('.test-indexer').forEach(b=>b.onclick=()=>testIntegration('indexers',b.dataset.id,b));}
function integrationCard(x,type){return `<div class="integration-card"><div><strong>${e(x.name)}</strong><span>${e(x.kind)} · ${e(x.base_url)}${x.last_ok_at?' · healthy '+fmtDate(x.last_ok_at):x.last_error?' · '+e(x.last_error):''}</span></div><div class="integration-actions"><span class="badge ${x.last_error?'bad':x.last_ok_at?'good':''}">${x.enabled?'enabled':'disabled'}</span><button class="button secondary compact test-${type}" data-id="${x.id}">Test</button></div></div>`}
function openAddIndexer(){const m=modal('Add indexer',`<form id="ix-form" class="form-grid"><div class="field"><label>Name</label><input class="input" name="name" required></div><div class="field"><label>Type</label><select class="select" name="kind"><option value="prowlarr">Prowlarr</option><option value="newznab">Newznab</option><option value="torznab">Torznab</option></select></div><div class="field full"><label>Base URL</label><input class="input" name="base_url" placeholder="http://prowlarr:9696" required></div><div class="field full"><label>API key</label><input class="input mono" name="api_key" type="password"></div><div class="field"><label>Categories</label><input class="input" name="categories" placeholder="7000, 3030"></div><div class="field"><label>Priority</label><input class="input" name="priority" type="number" value="50"></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="ix-save">Save</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#ix-save',m).onclick=async()=>{const f=Object.fromEntries(new FormData($('#ix-form')));f.priority=Number(f.priority||50);f.categories=String(f.categories||'').split(',').map(x=>Number(x.trim())).filter(Boolean);try{await api('/api/v1/indexers',{method:'POST',body:f});toast('Indexer added');$('#modal-root').innerHTML='';renderSettings()}catch(er){toast(er.message,'bad')}}}
async function testIntegration(kind,id,button){const old=button.textContent;button.disabled=true;button.innerHTML='<span class="spinner"></span>';try{const x=await api(`/api/v1/${kind}/${id}/test`,{method:'POST',body:{}});toast(x.message,x.ok?'good':'bad');renderSettings()}catch(er){toast(er.message,'bad');button.disabled=false;button.textContent=old}}

async function settingsClients(el){const d=await api('/api/v1/download-clients');const maps=await api('/api/v1/path-mappings');el.innerHTML=`<div class="setting-section"><h3>Download clients</h3><p>SABnzbd/NZBGet handle Usenet; qBittorrent/Transmission handle torrents. Remote path mappings bridge container path differences.</p></div><div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Clients</h2><button class="button compact" id="client-add">Add client</button></div><div class="panel-body">${d.items.length?d.items.map(x=>integrationCard(x,'client')):'<div class="empty"><strong>No clients</strong>Add the downloaders already running on your Unraid host.</div>'}</div></div><div class="panel"><div class="panel-head"><h2>Remote path mappings</h2><button class="button compact" id="mapping-add">Add mapping</button></div><div class="panel-body">${maps.items.length?maps.items.map(x=>`<div class="integration-card"><div><strong class="mono">${e(x.remote_path)}</strong><span>→ ${e(x.local_path)}</span></div></div>`).join(''):'<div class="empty"><strong>No mappings</strong>Only needed when downloader and ${e(PRODUCT)} paths differ.</div>'}</div></div>`;$('#client-add').onclick=openAddClient;$('#mapping-add').onclick=()=>openAddMapping(d.items);$$('.test-client').forEach(b=>b.onclick=()=>testIntegration('download-clients',b.dataset.id,b));}
function openAddClient(){const m=modal('Add download client',`<form id="client-form" class="form-grid"><div class="field"><label>Name</label><input class="input" name="name" required></div><div class="field"><label>Type</label><select class="select" name="kind"><option value="sabnzbd">SABnzbd</option><option value="qbittorrent">qBittorrent</option><option value="nzbget">NZBGet</option><option value="transmission">Transmission</option></select></div><div class="field full"><label>Base URL</label><input class="input" name="base_url" placeholder="http://sabnzbd:8080" required></div><div class="field"><label>Username</label><input class="input" name="username"></div><div class="field"><label>Password</label><input class="input" type="password" name="password"></div><div class="field"><label>API key</label><input class="input" type="password" name="api_key"></div><div class="field"><label>Category</label><input class="input" name="category" placeholder="books"></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="client-save">Save</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#client-save',m).onclick=async()=>{const f=Object.fromEntries(new FormData($('#client-form')));try{await api('/api/v1/download-clients',{method:'POST',body:f});toast('Download client added');$('#modal-root').innerHTML='';renderSettings()}catch(er){toast(er.message,'bad')}}}
function openAddMapping(clients){if(!clients.length)return toast('Add a download client first','bad');const m=modal('Add remote path mapping',`<form id="map-form" class="form-grid"><div class="field full"><label>Client</label><select class="select" name="client_id">${clients.map(x=>`<option value="${x.id}">${e(x.name)}</option>`).join('')}</select></div><div class="field"><label>Remote path</label><input class="input mono" name="remote_path" placeholder="/downloads" required></div><div class="field"><label>${e(PRODUCT)} path</label><input class="input mono" name="local_path" placeholder="/downloads" required></div></form>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="map-save">Save</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#map-save',m).onclick=async()=>{try{await api('/api/v1/path-mappings',{method:'POST',body:Object.fromEntries(new FormData($('#map-form')))});toast('Mapping added');$('#modal-root').innerHTML='';renderSettings()}catch(er){toast(er.message,'bad')}}}

async function settingsNaming(el){
  const [d,imp]=await Promise.all([api('/api/v1/naming'),api('/api/v1/settings/import')]);
  const modeOpt=m=>`<option value="${m}"${imp.transfer_mode===m?' selected':''}>${m}</option>`;
  el.innerHTML=`<div class="setting-section"><h3>Library file handling</h3><p>How ${e(PRODUCT)} places files it acquires, and how it names them. These rules apply to <strong>managed imports only</strong>.</p><div class="warning-box"><strong>Existing library safety:</strong> changing any of this cannot rename, move or touch an adopted file.</div></div>
  <div class="panel" style="margin-bottom:14px"><div class="panel-head"><h2>Transfer mode</h2></div><div class="panel-body">
    <form class="form-grid" id="import-form">
      <div class="field"><label>Mode</label><select class="select" name="transfer_mode">${imp.modes.map(modeOpt).join('')}</select></div>
      <div class="field"><label><input type="checkbox" name="automatic" ${imp.automatic?'checked':''}> Import completed downloads automatically</label><div class="help">On by default. A download that cannot be imported — no managed root for that media kind, or a size that does not match the release — stays in the queue as failed, with the reason, for you to handle.</div></div>
      <div class="field full"><div class="help"><strong>copy</strong> — duplicates the file into the library and leaves the download in place to keep seeding. Works everywhere. <strong>hardlink</strong> — no extra disk use, but requires the download and the library to be inside the same mount; if they are not, it silently falls back to copy. <strong>move</strong> — removes the source, which will break seeding on a private tracker.</div></div>
      <div><button class="button compact">Save</button></div>
    </form></div></div>
  ${d.items.map(x=>`<div class="panel" style="margin-bottom:14px"><div class="panel-head"><h2>${x.media_kind==='audiobook'?'Audiobook':'Ebook'} naming</h2></div><div class="panel-body">
    <form class="form-grid naming-form" data-kind="${x.media_kind}">
      <div class="field full"><label>Folder template</label><input class="input mono" name="folder_template" value="${escAttr(x.folder_template)}"></div>
      <div class="field full"><label>Filename template</label><input class="input mono" name="file_template" value="${escAttr(x.file_template)}"></div>
      ${x.media_kind==='audiobook'?`<div class="field full"><label>Multipart filename template</label><input class="input mono" name="multipart_template" value="${escAttr(x.multipart_template||'')}"><div class="help">Used when one book arrives as several files.</div></div>`:''}
      <div class="field"><label>Colon replacement</label><select class="select" name="colon_replacement">${['smart','dash','space','remove'].map(c=>`<option value="${c}"${(x.colon_replacement||'smart')===c?' selected':''}>${c}${c==='smart'?' ( -)':''}</option>`).join('')}</select></div>
      <div class="field"><label>Strip illegal characters</label><select class="select" name="replace_illegal"><option value="1"${x.replace_illegal?' selected':''}>Yes</option><option value="0"${!x.replace_illegal?' selected':''}>No</option></select></div>
      <div class="field full"><div class="help">Tokens: <span class="mono">{Author} {Title} {Year} {Series} {SeriesPosition} {SeriesPrefix} {Part:02} {Ext}</span> · use <span class="mono">/</span> in the folder template for nesting. A token with no value disappears; it never becomes the word "Unknown".</div></div>
      <div class="field full"><label>Preview</label><div class="mono naming-preview" data-kind="${x.media_kind}" style="white-space:pre-wrap;opacity:.85">…</div></div>
      <div><button class="button compact">Save</button></div>
    </form></div></div>`).join('')}`;

  $('#import-form').onsubmit=async ev=>{ev.preventDefault();
    try{const f=new FormData(ev.target);const r=await api('/api/v1/settings/import',{method:'PUT',body:{transfer_mode:f.get('transfer_mode'),automatic:f.has('automatic')}});toast(`Transfer mode: ${r.transfer_mode} · automatic import ${r.automatic?'on':'off'}`)}
    catch(er){toast(er.message,'bad')}};

  const previewFor=async f=>{
    const kind=f.dataset.kind, b=Object.fromEntries(new FormData(f));
    const q=new URLSearchParams({ext:kind==='audiobook'?'m4b':'epub',parts:kind==='audiobook'?'3':'1',
      folder_template:b.folder_template||'',file_template:b.file_template||'',
      replace_illegal:b.replace_illegal==='1'?'true':'false',colon_replacement:b.colon_replacement||'smart'});
    if(b.multipart_template)q.set('multipart_template',b.multipart_template);
    const box=$(`.naming-preview[data-kind="${kind}"]`);
    try{const p=await api(`/api/v1/naming/${kind}/preview?${q}`);
      const show=o=>`  ${o.folder}/\n`+o.files.map(n=>`    ${n}`).join('\n');
      box.textContent=`with series + year:\n${show(p)}`+(p.without_optional_fields?`\n\nno series, no year:\n${show(p.without_optional_fields)}`:'');
    }catch(er){box.textContent=`preview unavailable: ${er.message}`}};

  $$('.naming-form').forEach(f=>{
    let t; const bump=()=>{clearTimeout(t);t=setTimeout(()=>previewFor(f),250)};
    f.querySelectorAll('input,select').forEach(i=>{i.oninput=bump;i.onchange=bump});
    previewFor(f);
    f.onsubmit=async ev=>{ev.preventDefault();
      const b=Object.fromEntries(new FormData(f));
      b.replace_illegal=b.replace_illegal==='1';
      try{await api(`/api/v1/naming/${f.dataset.kind}`,{method:'PUT',body:b});toast(`${f.dataset.kind} naming saved`);previewFor(f)}
      catch(er){toast(er.message,'bad')}};
  });
}
async function settingsMigration(el){const d=await api('/api/v1/migrations');el.innerHTML=`<div class="setting-section"><h3>Readarr migration</h3><p>Analyze each of your existing instances independently. ${e(PRODUCT)} reconciles both into one Work graph and leaves files exactly where they are.</p><div class="warning-box"><strong>Read-only source:</strong> migration opens Readarr SQLite databases with SQLite <span class="mono">mode=ro</span>. It never writes to them and never renames source media.</div></div><div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2>Analyze instance</h2></div><div class="panel-body"><form id="migration-form" class="form-grid"><div class="field"><label>Instance</label><select class="select" name="media_kind"><option value="audiobook">Audiobook Readarr</option><option value="ebook">Ebook Readarr</option></select></div><div class="field"><label>Label</label><input class="input" name="source_name" placeholder="Readarr Audio"></div><div class="field full"><label>Mounted database path</label><input class="input mono" name="path" placeholder="/migrations/readarr-audio/readarr.db" required><div class="help">Mount the Readarr config/database into ${e(PRODUCT)} read-only.</div></div><div><button class="button">Analyze</button></div></form></div></div><div class="panel"><div class="panel-head"><h2>Migration runs</h2></div><div class="panel-body" id="migration-runs">${d.items.length?d.items.map(migrationRun).join(''):'<div class="empty"><strong>No migration runs</strong>Analyze your audiobook instance first, then ebook.</div>'}</div></div>`;$('#migration-form').onsubmit=async ev=>{ev.preventDefault();const b=Object.fromEntries(new FormData(ev.target));try{const x=await api('/api/v1/migrations/readarr/analyze',{method:'POST',body:b});toast(`Analyzed ${x.summary.books} books / ${x.summary.files} files`);renderSettings()}catch(er){toast(er.message,'bad')}};$$('.mig-reconcile').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const x=await api(`/api/v1/migrations/${b.dataset.id}/reconcile`,{method:'POST',body:{use_network:true}});toast(`Resolved ${x.resolved}; ${x.probable+x.unresolved} need review`);renderSettings()}catch(er){toast(er.message,'bad');b.disabled=false}});$$('.mig-execute').forEach(b=>b.onclick=()=>confirmMigration(b.dataset.id));}
function migrationRun(x){const a=x.analysis||{},r=x.result||{};return `<div class="integration-card"><div><strong>${e(x.source_name)} · ${e(x.media_kind)}</strong><span>${e(x.status)} · ${e(x.source_path)}${a.books?` · ${a.books.length} books`:''}</span></div><div class="integration-actions">${x.status==='analyzed'?`<button class="button secondary compact mig-reconcile" data-id="${x.id}">Reconcile</button>`:''}${x.status==='reconciled'?`<button class="button compact mig-execute" data-id="${x.id}">Execute</button>`:''}${x.status==='complete'?'<span class="badge good">Complete</span>':''}</div></div>`}
function confirmMigration(id){const m=modal('Execute migration',`<div class="warning-box"><strong>This imports database state, not files.</strong> Existing paths are recorded in place. If a metadata record remains unresolved, ${e(PRODUCT)} can create a local stub and send it to Review rather than dropping it.</div>`,{footer:'<button class="button ghost modal-cancel">Cancel</button><button class="button" id="mig-confirm">Import state</button>'});$('.modal-cancel',m).onclick=()=>$('#modal-root').innerHTML='';$('#mig-confirm',m).onclick=async()=>{try{const x=await api(`/api/v1/migrations/${id}/execute`,{method:'POST',body:{import_unresolved_stubs:true}});toast(`Imported ${x.works} mappings and ${x.files} file records`);$('#modal-root').innerHTML='';renderSettings()}catch(er){toast(er.message,'bad')}}}

async function settingsJobs(el){const d=await api('/api/v1/jobs');const acqOn=acquisitionOn(state.me);d.items=jobsFor(d.items,acqOn);d.runs=(d.runs||[]).filter(r=>acqOn||!ACQ_JOB_KINDS.has(r.kind));el.innerHTML=`<div class="setting-section"><h3>Background jobs</h3><p>Persistent schedules keep metadata, wanted search, downloads, and file health moving without external cron.</p></div><div class="panel table-wrap"><table><thead><tr><th>Job</th><th>Cadence</th><th>Last run</th><th>Status</th><th>Next</th><th></th></tr></thead><tbody>${d.items.map(x=>`<tr><td><strong>${e(x.name)}</strong><div class="small muted">${e(x.kind)}</div>${x.kind==='managed-library-build'?'<div class="small muted">Manual only: dry run, confirm and run from Settings &gt; Library roots</div>':''}</td><td>${x.schedule_seconds?Math.round(x.schedule_seconds/60)+' min':'manual'}</td><td>${fmtDate(x.last_run_at)}</td><td><span class="badge ${x.last_status==='success'?'good':x.last_status==='failed'?'bad':''}">${e(x.last_status||'never')}</span></td><td>${fmtDate(x.next_run_at)}</td><td>${x.kind==='managed-library-build'?'<button class="button secondary compact" id="mb-open-from-jobs" type="button">Open build panel</button>':`<button class="button secondary compact run-job" data-id="${x.id}">Run</button>`}</td></tr>`).join('')}</tbody></table></div>${acqOn?await wantedSearchPanel(d):''}<div class="setting-section"><h3>Recent runs</h3></div><div class="panel table-wrap" id="job-runs"><table><thead><tr><th>Job</th><th>Trigger</th><th>Started</th><th>Duration</th><th>Outcome</th><th>Attempt</th><th>Details</th></tr></thead><tbody>${(d.runs||[]).length?d.runs.map(r=>`<tr><td><strong>${e((d.items.find(j=>j.id===r.job_id)||{}).name||r.kind)}</strong></td><td>${e(r.trigger_source||'schedule')}</td><td>${fmtDate(r.started_at)}</td><td>${r.finished_at?Math.round(r.finished_at-r.started_at)+' s':'\u2014'}</td><td><span class="badge ${r.status==='success'?'good':r.status==='failed'?'bad':''}">${e(r.status)}</span></td><td>${fmtNum(r.attempt||1)}</td><td class="small">${e(r.error||'')}</td></tr>`).join(''):'<tr><td colspan="7" class="muted">No runs yet</td></tr>'}</tbody></table></div>`;wireWantedSearchPanel();$$('.run-job').forEach(b=>b.onclick=async()=>{b.disabled=true;b.innerHTML='<span class="spinner"></span>';try{const x=await api(`/api/v1/jobs/${b.dataset.id}/run`,{method:'POST',body:{}});toast(`Job ${x.status}`,x.status==='success'?'good':'bad');renderSettings()}catch(er){toast(er.message,'bad');b.disabled=false;b.textContent='Run'}});const mbOpen=$('#mb-open-from-jobs');if(mbOpen)mbOpen.onclick=()=>openManagedBuildPanel({})}


// Automatic wanted search: its settings, last result and a run-now button. The run is started in
// the background (a drain can take 20 minutes); Recent runs below shows it as it progresses.
const WS_FIELDS=[['wanted_search_batch','Batch size','searches per batch'],['wanted_search_budget_minutes','Run budget','minutes per run'],['wanted_search_daily_cap','Daily cap','automatic searches per 24 h'],['wanted_search_cooldown_hours','Base cooldown','hours after a miss, doubles per miss, max 7 days'],['wanted_search_delay_seconds','Pause','seconds between searches'],['auto_grab_min_score','Grab score floor','minimum score to auto-grab']];
async function wantedSearchPanel(d){
  const job=(d.items||[]).find(j=>j.kind==='wanted-search'); if(!job) return '';
  const cfg=await api('/api/v1/settings/acquisition');
  const last=(d.runs||[]).find(r=>r.kind==='wanted-search'&&r.status!=='running'), lr=last&&last.result_json?JSON.parse(last.result_json):null;
  const summary=lr?`Last run: ${fmtNum(lr.searched)} searched, ${fmtNum(lr.grabbed)} grabbed, ${fmtNum(lr.no_result)} not found, ${fmtNum(lr.skipped_on_cooldown)} cooling down, ${fmtNum(lr.batches)} batch(es), stopped: <strong>${e(lr.stopped_reason||'?')}</strong> after ${e(String(lr.elapsed))} s.`:'No automatic run yet.';
  return `<div class="setting-section" id="wanted-search-panel"><h3>Wanted search</h3><p>${job.enabled?'Enabled':'<strong>Disabled</strong>'}: searches wanted books in batches until the backlog is drained or the budget, daily cap or indexers say stop. ${summary}</p></div><div class="panel"><div class="panel-body" style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end">${WS_FIELDS.map(([k,l,h])=>`<label class="field-inline"><span>${l}</span><input type="number" step="any" min="0" data-ws="${k}" value="${escAttr(String(cfg[k]))}" style="width:90px"><span class="small muted">${h}</span></label>`).join('')}<button class="button" id="ws-save">Save</button><button class="button secondary" id="ws-run">Run wanted search now</button></div></div>`;
}
function wireWantedSearchPanel(){
  const save=$('#ws-save'), run=$('#ws-run'); if(!save) return;
  save.onclick=async()=>{ const body={}; $$('[data-ws]').forEach(i=>{ body[i.dataset.ws]=Number(i.value); }); try{ await api('/api/v1/settings/acquisition',{method:'PUT',body}); toast('Saved','good'); }catch(er){ toast(er.message,'bad'); } };
  run.onclick=async()=>{ run.disabled=true; try{ await api('/api/v1/wanted/search-now',{method:'POST',body:{}}); toast('Wanted search started','good'); }catch(er){ toast(er.message,'bad'); } run.disabled=false; };
}
async function settingsSystem(el){const h=await api('/api/v1/system/health');el.innerHTML=`<div class="setting-section"><h3>System health</h3><p>${e(PRODUCT)} is designed to remain useful even when metadata or an acquisition dependency is offline.</p></div><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr))"><div class="stat-card"><div class="stat-label">${e(PRODUCT)}</div><div class="stat-value" style="font-size:18px;color:var(--good)">Healthy</div><div class="stat-foot">v${e(h.application.version)}</div></div><div class="stat-card"><div class="stat-label">OpenReads</div><div class="stat-value" style="font-size:18px;color:${h.openreads.ok?'var(--good)':'var(--bad)'}">${h.openreads.ok?'Connected':'Unavailable'}</div><div class="stat-foot">${e(h.openreads.error||'Canonical metadata v2')}</div></div><div class="stat-card"><div class="stat-label">Indexers</div><div class="stat-value">${h.indexers.length}</div><div class="stat-foot">configured</div></div><div class="stat-card"><div class="stat-label">Download clients</div><div class="stat-value">${h.download_clients.length}</div><div class="stat-foot">configured</div></div><div class="stat-card"><div class="stat-label">Scheduler</div><div class="stat-value" style="font-size:18px;color:${!h.scheduler?.enabled?'var(--muted)':h.scheduler.last_tick_age_seconds!=null&&h.scheduler.last_tick_age_seconds<=300?'var(--good)':'var(--warn)'}">${!h.scheduler?.enabled?'Off':h.scheduler.last_tick_age_seconds==null?'Starting':h.scheduler.last_tick_age_seconds<=300?'Ticking':'Stalled'}</div><div class="stat-foot">${h.scheduler?.last_tick_age_seconds!=null?`last tick ${h.scheduler.last_tick_age_seconds}s ago`:''}${(h.scheduler?.running||[]).length?` · running: ${h.scheduler.running.map(r=>`${e(r.kind)} (${r.age_seconds}s)`).join(', ')}`:' · idle'}</div></div>${h.audience_backfill?`<div class="stat-card"><div class="stat-label">Audience metadata</div><div class="stat-value" style="font-size:18px">${fmtNum(h.audience_backfill.with_audience)} / ${fmtNum(h.audience_backfill.total_works)}</div><div class="stat-foot">${fmtNum(h.audience_backfill.pending)} to check${h.audience_backfill.running?' · running':h.audience_backfill.last_status?` · last run ${e(h.audience_backfill.last_status)}`:''}</div></div>`:''}</div><div class="panel" style="margin-top:16px"><div class="panel-head"><h2>OPDS endpoints</h2></div><div class="panel-body"><div class="code">${e(location.origin)}/opds/v2/catalog.json\n${e(location.origin)}/opds/v1/catalog.xml</div><p class="muted small">OPDS supports the application session/API authorization and local HTTP Basic credentials for compatible readers.</p></div></div><div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Series cleanup</h2></div><div class="panel-body"><p class="muted small">Drops series rows that are really identifiers, page counts or publisher imprints, and merges duplicate spellings. Safe to re-run.</p><button class="button secondary" id="series-cleanup">Run series cleanup</button> <span id="series-cleanup-result" class="small muted"></span></div></div>`;const sc=$('#series-cleanup');if(sc)sc.onclick=async()=>{sc.disabled=true;try{const x=await api('/api/v1/admin/series/dedupe',{method:'POST',body:{}});$('#series-cleanup-result').textContent=`Dropped ${x.dropped}, merged ${x.merged}.`;toast(`Series cleanup: dropped ${x.dropped}, merged ${x.merged}`)}catch(er){toast(er.message,'bad')}finally{sc.disabled=false}}}

// ---------- Reader / player (#library/<id>/read/<assetId>) ----------
// The reader and audio player live in consume.js, shared with the Player shell, so
// both experiences render books identically. Loaded on demand (dynamic import keeps
// this file a plain script for the node harness in tests/test_frontend_route_errors.py).
async function renderConsume(workId, assetId){
  setLoading('Opening');
  const mod=await import('/assets/consume.js');
  state.consumeCleanup=await mod.open({workId, assetId, host:$('#view'), api, toast, modal, closeModal, chrome:'management',
    onBack:()=>{ location.hash=`library/${workId}`; }});
}

function wireRoutes(root=document){ $$('[data-route]',root).forEach(x=>x.onclick=ev=>{ev.preventDefault();location.hash=x.dataset.route}); }
/* ---------- build / version ----------
 * state.build = {client, server, details, mismatch, waiting}. The server sha is
 * /api/v1/version (anyone); an administrator also gets /version/details (database step,
 * process start). The client sha is the one this page was served with (pwa.js). */
const fmtBuiltAt=v=>{if(!v)return 'unknown';const d=new Date(v);return isNaN(d)?String(v):d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})};
function buildLine(b,esc=true){
  const x=esc?e:(v=>String(v));
  const s=b.details||b.server||{}; const parts=[`${x(PRODUCT)} ${x(s.sha||'?')}`,`built ${x(fmtBuiltAt(s.built_at))}`];
  if(b.details&&b.details.started_at) parts.push(`up since ${x(new Date(b.details.started_at*1000).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}))}`);
  let t=parts.join(' · '); if(b.details&&b.details.migration_step!=null) t+=`; database step ${x(b.details.migration_step)}`;
  return t;
}
async function copyBuild(){const text=state.buildPlain||'';try{await navigator.clipboard.writeText(text);toast('Build info copied')}catch{const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');toast('Build info copied')}catch{toast(text)}ta.remove()}}
function paintBuild(){
  const b=state.build; if(!b||!b.server) return;
  const plain=buildLine(b,false); state.buildPlain=plain+`; web app ${b.client.sha}`;
  const chip=$('#build-chip'); if(chip){
    $('#build-chip-text').textContent=`${PRODUCT} ${b.server.sha} · built ${fmtBuiltAt(b.server.built_at)}`;
    chip.title=plain; chip.onclick=copyBuild; const up=$('#build-update'); if(up) up.onclick=()=>window.bookdoePwa?window.bookdoePwa.applyUpdate():location.reload(); chip.classList.toggle('stale',!!(b.mismatch||b.waiting));
    $('#build-update')?.classList.toggle('hidden',!(b.mismatch||b.waiting));
  }
  const line=$('#build-line'); if(line){
    line.innerHTML=`<div id="build-line-text" class="mono">${buildLine(b)}</div><div class="small muted" id="build-client">Web app (this page) ${e(b.client.sha)} ${b.mismatch||b.waiting?`- differs from the server`:`- matches the server &#10003;`}</div>${b.mismatch||b.waiting?'<div class="warning-box" id="build-stale"><strong>Update available.</strong> This page is older than the server. <button class="button compact" id="build-reload">Reload now</button></div>':''}<div><button class="button secondary compact" id="build-copy">Copy</button></div>`;
    $('#build-copy').onclick=copyBuild; const rl=$('#build-reload'); if(rl) rl.onclick=()=>window.bookdoePwa?window.bookdoePwa.applyUpdate():location.reload();
  }
}
async function loadBuild(){
  const pwa=window.bookdoePwa; const client=(pwa&&pwa.build)||window.__ALX_BUILD__||{sha:'dev'};
  let server=null,details=null; try{server=await api('/api/v1/version')}catch{}
  if(!isKidsScope()){try{details=await api('/api/v1/version/details')}catch{}}
  state.build={client,server,details,mismatch:!!(server&&server.sha!==client.sha),waiting:false}; paintBuild();
  if(pwa&&pwa.watchVersion) pwa.watchVersion(v=>{ if(!state.build) return; state.build.server=v.server||state.build.server; state.build.mismatch=v.mismatch; state.build.waiting=v.waiting; paintBuild(); });
}
async function settingsAbout(el){
  el.innerHTML=`<div class="setting-section">${brandLogo('about-logo', 'about-logo')}<h3 id="about-name">${e(PRODUCT)}</h3><p id="about-tagline">${e(TAGLINE)}</p><p>The build this server is running, and the web app version this page loaded.</p></div><div class="panel"><div class="panel-body stack" id="build-line"><span class="muted">Loading…</span></div></div>`;
  if(!state.build) await loadBuild(); paintBuild();
}
async function refreshHealth(){try{const h=await api('/api/v1/system/health');const p=$('#health-pill');p.className=`health-pill ${h.openreads.ok?'ok':'bad'}`;p.querySelector('span').textContent=h.openreads.ok?'Systems nominal':'Metadata degraded';}catch{}}

document.addEventListener('click',ev=>{const r=ev.target.closest('[data-route]');if(r){ev.preventDefault();location.hash=r.dataset.route}});
$('#theme-toggle').onclick=()=>{const next=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;localStorage.setItem('bookdoe-theme',next)};
document.documentElement.dataset.theme=localStorage.getItem('bookdoe-theme')||'dark';
// The mobile drawer sits above the top bar, so the hamburger that opens it ends up
// underneath it. With nothing else wired up, opening the menu on a phone covered the
// page and the only way out was reloading. It now closes on a backdrop tap, on
// Escape, on its own close button, and - the one that actually matters - whenever
// you navigate somewhere.
function setNavOpen(open){
  const sidebar=$('#sidebar'); if(!sidebar) return;
  sidebar.classList.toggle('open', open);
  $('#nav-backdrop').hidden = !open;
  document.body.style.overflow = open ? 'hidden' : '';
  // iOS Safari ignores body overflow for touch scrolling; overscroll-behavior:contain on
  // .nav (styles.css) stops a swipe at the end of the list from moving the page behind.
  // The html element is deliberately left alone: making it overflow:hidden resets the
  // page's scroll position to the top, which loses the reader's place.
  document.documentElement.classList.toggle('nav-open', open);
  $('#menu-toggle')?.setAttribute('aria-expanded', String(open));
}
function closeNav(){ setNavOpen(false); }
$('#menu-toggle').onclick=()=>setNavOpen(!$('#sidebar').classList.contains('open'));
$('#nav-close').onclick=closeNav;
$('#nav-backdrop').onclick=closeNav;
// Tapping the entry you are already on does not change the hash, so route() never
// runs and the drawer would stay open on exactly the tap most likely to be a mistake.
$('#nav').addEventListener('click',ev=>{ if(ev.target.closest('a')) closeNav(); });
document.addEventListener('keydown',ev=>{
  if(ev.key!=='Escape') return;
  if($('#modal-root').innerHTML) { $('#modal-root').innerHTML=''; return; }
  closeNav();
});
$('#user-button').onclick=()=>{const m=modal('Account',`<div><strong>${e(state.me?.display_name||state.me?.username)}</strong><div class="muted small">${e(state.me?.email||'')}</div><div class="muted small" style="margin-top:6px">Administrator · Management</div></div>`,{footer:'<a class="button secondary" href="/app">Open the Player</a><button class="button danger" id="logout">Sign out</button>'});$('#logout',m).onclick=async()=>{try{await api('/api/v1/auth/logout',{method:'POST',body:{}});}catch{}try{if(window.bookdoePwa)await window.bookdoePwa.clearUser();}catch{}location.reload()}};
// The global search queries the whole library server-side. It used to load the
// first page and filter that in the browser, which meant a book the server could
// find perfectly well reported "no results".
let searchTimer;
$('#global-search').oninput=ev=>{
  clearTimeout(searchTimer);
  const q=ev.target.value.trim();
  const run=()=>{
    const onList=!!$('#library-filter') && currentHash().split('?')[0]==='library';
    // An empty box is "no query", never a search for nothing; off the Library there is nothing to clear.
    if(onList){ libraryTyped(q,true); return; }
    if(!q) { libraryState.q=''; return; }
    libraryState.q=q;
    // Changing the hash makes route() do the render; calling renderLibrary here too would run the query twice.
    location.hash='library';
  };
  if(q) searchTimer=setTimeout(run,280); else run();
};
$('#global-search').addEventListener('keydown',ev=>{ if(ev.key==='Escape' && ev.target.value){ ev.target.value=''; ev.target.dispatchEvent(new Event('input')); } });
$('#global-search').addEventListener('search',ev=>{ if(!ev.target.value) ev.target.dispatchEvent(new Event('input')); });
// The menu always means "start fresh": a Library entry tapped while already in the Library
// (where the hash does not change) clears the filters and reloads the list.
$('#nav').addEventListener('click',ev=>{
  const a=ev.target.closest('a[data-route="library"]'); if(!a) return;
  resetLibraryFilters();
  if(currentHash()==='library') route();
});

// ---- Format conversion (issue #265). All UI lives in convert.js, loaded on demand; the glue
// below is self-contained so the book page, the bulk bar and the settings tab need no edits.
const convertCtx=()=>({api,$,$$,e,toast,modal,closeModal,fmtBytes,fmtDate,fmtNum});
const convertModule=()=>import('/assets/convert.js');
// "Converted from MP3" (derived assets only): the copy keeps a visible link to its original.
function derivedBadgeHtml(a){
  if(!a.derived_from_asset_id) return '';
  return `<div><span class="badge info asset-derived" title="Made by ${e(PRODUCT)} from another file of this book; the original is kept">${e(a.derived_label||'Converted')}</span></div>`;
}
// Per-asset "Convert to M4B/EPUB" control, ALWAYS drawn for a convertible asset: the button when it is available (with
// a note when the copy will be saved in the managed library because the book lives in a read-only root), a status line
// while queued / running / done, and a DISABLED button with the reason (tooltip plus visible text) when it is blocked.
// It is left out only where conversion can never apply: converted copies, other kinds, a book that already IS the
// preferred format, a book that is not owned yet.
function convertButtonHtml(a){
  const c=a.convert; if(!c||a.derived_from_asset_id) return '';
  if(['already_preferred','derived','not_owned','unsupported_kind'].includes(c.code)) return '';
  // Never offer a target the asset's own files already include (an EPUB beside the MOBI, an M4B beside MP3s).
  if((a.formats||[]).includes(String(c.target||'').toLowerCase())) return '';
  const label=(c.target||'').toUpperCase();
  const note=t=>`<div class="small muted asset-convert-note">${e(t)}</div>`;
  const box=inner=>`<div class="asset-convert-box" data-convert-state="${escAttr(c.state||'')}" data-convert-code="${escAttr(c.code||'')}">${inner}</div>`;
  const run=c.conversion&&['queued','running'].includes(c.conversion.status);
  if(run) return box(`<span class="badge info asset-convert-state" data-conversion="${escAttr(c.conversion.id)}">${c.conversion.status==='running'?`Converting to ${e(label)} ${Math.round(c.conversion.percent||0)}%`:`Queued for ${e(label)}`}</span>${c.cross_root&&c.note?note(c.note):''}`);
  if(c.state==='available'||c.state==='stale'){
    const failed=c.conversion&&c.conversion.status==='failed'?`<div class="small asset-convert-failed" style="color:var(--bad)" title="${escAttr(c.conversion.error||'')}">Last attempt failed${c.conversion.error?`: ${e(String(c.conversion.error).slice(0,140))}`:''}</div>`:'';
    return box(`<button class="button ghost compact asset-convert" data-asset="${escAttr(a.id)}" data-target="${escAttr(c.target)}" title="Make a ${e(label)} copy. The original is kept.">${c.state==='stale'?'Re-convert':'Convert'} to ${e(label)}</button>${c.state==='stale'&&c.reason?note(c.reason):''}${c.cross_root&&c.note?note(c.note):''}${failed}`);
  }
  if(c.state==='converted') return box(`<div class="small muted asset-convert-done" title="${escAttr(c.reason||'')}">Converted to ${e(label)}${c.cross_root?' (saved in your managed library)':''}</div>`);
  const why=c.reason||'Conversion is not available for this book';
  return box(`<button class="button ghost compact asset-convert-off" type="button" disabled aria-disabled="true" title="${escAttr(why)}">Convert to ${e(label)}</button>${note(why)}`);
}
function wireConversionUi(){
  if(typeof MutationObserver==='undefined'||typeof document.addEventListener!=='function') return;   // test harnesses stub a minimal DOM
  document.addEventListener('click',ev=>{
    const b=ev.target.closest&&ev.target.closest('.asset-convert'); if(!b) return;
    b.disabled=true;
    convertModule().then(m=>m.convertAsset(convertCtx(),b.dataset.asset,b.dataset.target,()=>{ const h=currentHash().split('/'); if(h[0]==='library'&&h[1]) renderWork(h[1]); })).catch(er=>{ toast(er.message,'bad'); b.disabled=false; });
  });
  // The bulk bar is drawn by paintBulkBar(); this adds one button to it whenever it is drawn.
  let queued=false;
  new MutationObserver(()=>{
    if(queued) return; queued=true;
    requestAnimationFrame(()=>{
      queued=false;
      watchConversion();
      const actions=$('#bulk-bar .bulk-actions'); if(!actions||$('#bulk-convert',actions)) return;
      const btn=document.createElement('button');
      btn.className='button compact'; btn.id='bulk-convert'; btn.disabled=!libraryState.selected.size;
      btn.title='Make M4B / EPUB copies of the selected books. Originals are kept.';
      btn.textContent='Convert selected to preferred formats';
      btn.onclick=()=>convertModule().then(m=>m.bulkConvertDialog(convertCtx(),[...libraryState.selected],()=>{ libraryState.selectMode=false; libraryState.selected.clear(); renderLibrary(); })).catch(er=>toast(er.message,'bad'));
      actions.appendChild(btn);
    });
  }).observe(document.body,{childList:true,subtree:true});
}
// A book page showing "Queued/Converting ..." refreshes itself once the conversion has finished
// (it polls the active list every 3 s only while such a badge is on screen).
let conversionWatch=null;
function watchConversion(){
  const badge=$('.asset-convert-state');
  if(!badge){ if(conversionWatch){ clearInterval(conversionWatch.timer); conversionWatch=null; } return; }
  if(conversionWatch) return;
  const hash=currentHash();
  conversionWatch={timer:setInterval(async()=>{
    const h=currentHash().split('/');
    if(currentHash()!==hash||h[0]!=='library'||!h[1]){ clearInterval(conversionWatch.timer); conversionWatch=null; return; }
    try{
      const d=await api('/api/v1/conversions?group=active&limit=1');
      if(d.total===0){ clearInterval(conversionWatch.timer); conversionWatch=null; renderWork(h[1]); }
    }catch{ /* the next tick tries again */ }
  },3000)};
}
async function settingsDiscover(el){
  const d=await api('/api/v1/settings/discover');
  const num=(id,label,val,hint,min,max)=>`<div class="field"><label for="${id}">${label}</label><input class="input" id="${id}" type="number" min="${min}" max="${max}" value="${escAttr(val)}"><span class="muted small">${hint}</span></div>`;
  el.innerHTML=`<div class="setting-section"><h3>Discover chat</h3><p>The Player's AI book-recommendation chat. Every title it suggests is checked against the library and the metadata provider before anyone sees it, and kid accounts get extra server-side filtering. Turn it off here and the Player keeps plain search.</p></div>
  <div class="panel"><div class="panel-body"><form id="dc-form" class="form-grid">
    <div class="field full"><label class="field-inline"><input type="checkbox" id="dc-enabled" ${d.enabled?'checked':''}> Discover chat is on</label><span class="muted small">${d.llm_configured?'Model: <span class="mono">'+e(d.model||'')+'</span> (read-only, set by BOOKDOE_LLM_MODEL). Right now the chat is '+(d.available?'available':'<strong>not available</strong> (model unreachable)')+'.':'No LLM is configured (BOOKDOE_LLM_ENABLED), so the chat is hidden from the Player.'}</span></div>
    ${num('dc-timeout','Time limit per answer (seconds)',d.llm_timeout_seconds,'Hard limit for one turn, model plus checking. The deployed reasoning model can take tens of seconds.',10,900)}
    ${num('dc-tokens','Answer size limit (tokens)',d.max_tokens,'A reasoning model spends most of this thinking; too small and it never writes an answer.',500,16000)}
    ${num('dc-turns','Messages per conversation',d.max_turns,'Then the person is asked to start over.',2,200)}
    ${num('dc-cap','Messages per person per day',d.daily_cap,'Rolling 24 hours, per account.',1,1000)}
    ${num('dc-ret','Keep conversations (days)',d.retention_days,'Older conversations are deleted by the housekeeping job. Taste profiles are kept until the person clears them.',1,3650)}
    <div class="field full"><label class="field-inline"><input type="checkbox" id="dc-kidvis" ${d.kid_chats_visible_to_approvers?'checked':''}> Approvers and administrators can read a kid's chats and taste profile</label><span class="muted small">On by default. The kid is told plainly in the app. Adults' chats and profiles are never readable by anyone else, administrators included. Open a kid under Player &rarr; Approvals to read their chats.</span></div>
    <div class="field full"><label class="field-inline"><input type="checkbox" id="dc-lib" ${d.send_library_titles?'checked':''}> Tell the model which titles the library holds (adults only)</label><span class="muted small">A capped list of titles and authors, nothing else. A kid's chat never gets it.</span></div>
    <div class="field full"><label for="dc-unv">Kid suggestions with no children's-book evidence</label><select class="select" id="dc-unv"><option value="drop" ${d.kid_unverified==='drop'?'selected':''}>Drop them (safest)</option><option value="ask_parent" ${d.kid_unverified==='ask_parent'?'selected':''}>Show title only, as "ask a parent" (needs approval anyway)</option></select><span class="muted small">Anything with an adult signal in its metadata is always dropped.</span></div>
    <div class="full"><button class="button" id="dc-save">Save</button></div></form></div></div>`;
  $('#dc-form').onsubmit=async ev=>{ev.preventDefault();
    const n=id=>Number($('#'+id).value);
    try{await api('/api/v1/settings/discover',{method:'PUT',body:{enabled:$('#dc-enabled').checked,llm_timeout_seconds:n('dc-timeout'),max_tokens:n('dc-tokens'),max_turns:n('dc-turns'),daily_cap:n('dc-cap'),retention_days:n('dc-ret'),kid_chats_visible_to_approvers:$('#dc-kidvis').checked,send_library_titles:$('#dc-lib').checked,kid_unverified:$('#dc-unv').value}});toast('Saved');settingsDiscover(el)}catch(er){toast(er.message,'bad')}};
}
async function settingsConversionTab(el){ const m=await convertModule(); await m.settingsConversion(convertCtx(),el); }
wireConversionUi();

// ---- Library-wide duplicate finder and bulk merge (admin only). All UI lives in dupes.js, loaded on demand.
async function renderDuplicates(){
  setLoading('Duplicates');
  if(!state.me?.is_admin){ $('#view').innerHTML='<div class="page-head"><div><h1>Duplicates</h1><p>Only an administrator can find and merge duplicate books.</p></div></div>'; return; }
  const m=await import('/assets/dupes.js');
  await m.duplicatesPage({api,$,$$,e,toast,modal,closeModal,fmtBytes,fmtDate,fmtNum,escAttr},$('#view'));
}

// ---- Merging duplicate works and cleaning up redundant files (admin only). All UI lives in merge.js,
// loaded on demand. This glue only injects its entry points as pages draw (a series page banner, the
// "Merge selected" buttons, a book page's Duplicates and Duplicate files panels, Settings > System), so
// the page renderers above need no edits.
const mergeCtx=()=>({api,$,$$,e,toast,modal,closeModal,fmtBytes,fmtDate,fmtNum});
const mergeModule=()=>import('/assets/merge.js');
function wireMergeUi(){
  if(typeof MutationObserver==='undefined'||typeof document.addEventListener!=='function') return;   // test harnesses stub a minimal DOM
  const admin=()=>!!state.me?.is_admin&&!isKidsScope();
  const addButton=(actions,id,label,title,getIds)=>{
    if(!actions||$('#'+id,actions)) return;
    const btn=document.createElement('button');
    btn.className='button compact'; btn.id=id; btn.textContent=label; btn.title=title;
    btn.disabled=getIds().length<2;
    btn.onclick=()=>{ const ids=getIds(); if(ids.length<2) return toast('Tick at least two books to merge','warn'); mergeModule().then(m=>m.mergeDialog(mergeCtx(),ids)).catch(er=>toast(er.message,'bad')); };
    actions.appendChild(btn);
  };
  const seriesPicked=()=>$$('.series-pick').filter(c=>c.checked).map(c=>c.value);
  let queued=false;
  const inject=()=>{
    queued=false;
    if(!admin()) return;
    const h=currentHash().split('/');
    // Library select mode: "Merge selected..." joins the bulk actions.
    addButton($('#bulk-bar .bulk-actions'),'bulk-merge','Merge selected…','Merge the ticked books into one record. Nothing is deleted; you can undo it.',()=>[...libraryState.selected]);
    const bulk=$('#bulk-merge'); if(bulk) bulk.disabled=libraryState.selected.size<2;
    if(h[0]==='series'&&h[1]&&$('#series-books')){
      const bar=$('#series-bulk-bar');
      if(bar&&!$('#series-dupes')){
        const host=document.createElement('div'); host.id='series-dupes'; bar.before(host);
        mergeModule().then(m=>m.seriesDuplicates(mergeCtx(),h[1],host)).catch(()=>{ host.innerHTML=''; });
      }
      addButton($('#series-bulk-bar .bulk-actions'),'series-merge','Merge selected…','Merge the ticked books of this series into one record.',seriesPicked);
      const sm=$('#series-merge'); if(sm) sm.disabled=seriesPicked().length<2;
    }
    if(h[0]==='library'&&h[1]&&!h[2]&&h[1]!=='read'&&$('.detail-hero')&&!$('#work-dupes')){
      const anchor=$$('#view .panel').find(p=>/Media assets/.test($('h2',p)?.textContent||''));
      if(anchor){
        const host=document.createElement('div'); host.id='work-dupes';
        host.innerHTML='<div id="work-dupes-find"></div><div id="work-dupes-files"></div>';
        anchor.after(host);
        mergeModule().then(m=>{ m.workDuplicates(mergeCtx(),h[1],$('#work-dupes-find',host)); m.duplicateFilesPanel(mergeCtx(),h[1],$('#work-dupes-files',host)); }).catch(()=>{});
      }
    }
    if(h[0]==='settings'){
      const body=$('#settings-body');
      if(body&&$('.setting-section h3',body)?.textContent==='System health'&&!$('#merge-admin',body)){
        const host=document.createElement('div'); host.id='merge-admin'; body.appendChild(host);
        mergeModule().then(m=>m.settingsMerges(mergeCtx(),host)).catch(er=>{ host.innerHTML=''; });
      }
    }
  };
  new MutationObserver(()=>{ if(queued) return; queued=true; requestAnimationFrame(inject); }).observe(document.body,{childList:true,subtree:true});
  document.addEventListener('change',ev=>{ if(ev.target.closest&&ev.target.closest('#series-books, #series-bulk-bar')) requestAnimationFrame(inject); });
  // After a merge or an undo, redraw whatever page is behind the dialog.
  document.addEventListener('mrg-changed',ev=>{
    const h=currentHash().split('/'), r=ev.detail||{};
    if(h[0]==='series'&&h[1]) renderSeriesDetail(h[1]);
    else if(h[0]==='library'&&h[1]&&!h[2]){
      if((r.source_work_ids||[]).includes(h[1])&&r.canonical_work_id) location.hash=`library/${r.canonical_work_id}`;   // this book was merged away
      else renderWork(h[1]);
    }
    else if(h[0]==='library'&&!h[1]) { libraryState.selectMode=false; libraryState.selected.clear(); renderLibrary(); }
    else if(h[0]==='settings'){ const host=$('#merge-admin'); if(host) mergeModule().then(m=>m.settingsMerges(mergeCtx(),host)); }
  });
}
wireMergeUi();

// ---- Managed library build (#219). All UI lives in managedbuild.js, loaded on demand. One build panel, reached from
// Settings > Library roots (banner and a per-root action), Settings > Jobs, the Overview card and a book page.
const managedBuildModule=()=>import('/assets/managedbuild.js');
const managedBuildCtx=()=>({api,$,$$,e,toast,modal,closeModal,fmtBytes,fmtDate,fmtNum,openPanel:openManagedBuildPanel});
function openManagedBuildPanel(preset={},opts={}){
  if(opts.dialog){
    const m=modal('Copy to managed library','<div id="mb-dialog-panel"></div>',{wide:true});
    const host=$('#mb-dialog-panel',m);
    const close=$('.modal-close',m); if(close&&opts.onClose){ const prev=close.onclick; close.onclick=()=>{ prev&&prev(); opts.onClose(); }; }
    managedBuildModule().then(mod=>mod.buildPanel(managedBuildCtx(),host,preset)).catch(er=>toast(er.message,'bad'));
    return;
  }
  state.settingsTab='roots'; state.mbPanelPreset=preset;
  if(currentHash()==='settings') renderSettings(); else location.hash='settings';
}
function managedBuildRootsHook(el,roots){
  if(!state.me?.is_admin||isKidsScope()) return;
  managedBuildModule().then(async mod=>{
    await mod.rootsEnhance(managedBuildCtx(),el,roots);
    const host=$('#mb-panel-host',el); if(!host) return;
    const preset=state.mbPanelPreset; state.mbPanelPreset=null;
    if(preset){ await mod.buildPanel(managedBuildCtx(),host,preset); host.scrollIntoView({block:'start'}); return; }
    // A build in progress is always on show here, so its progress and controls are where the owner looks.
    try{ const st=await api('/api/v1/managed-build/status'); if(st.active_run) await mod.buildPanel(managedBuildCtx(),host,{}); }catch{ /* the banner still works */ }
  }).catch(()=>{});
}
function wireManagedBuildUi(){
  if(typeof MutationObserver==='undefined'||typeof document.addEventListener!=='function') return;   // test harnesses stub a minimal DOM
  let queued=false;
  const inject=()=>{
    queued=false;
    if(!state.me?.is_admin||isKidsScope()) return;
    const h=currentHash().split('/');
    if(h[0]==='dashboard'&&$('#view .stats-grid')&&!$('#mb-overview')){
      const host=document.createElement('div'); host.id='mb-overview'; $('#view .stats-grid').after(host);
      managedBuildModule().then(m=>m.overviewCard(managedBuildCtx(),host)).catch(()=>host.remove());
    }
    if(h[0]==='library'&&h[1]&&!h[2]&&h[1]!=='read'&&$('.detail-hero')&&!$('#mb-book')){
      const anchor=$$('#view .panel').find(p=>/Media assets/.test($('h2',p)?.textContent||''));
      if(anchor){
        const host=document.createElement('div'); host.id='mb-book'; anchor.after(host);
        managedBuildModule().then(m=>m.bookState(managedBuildCtx(),h[1],host)).catch(()=>host.remove());
      }
    }
  };
  new MutationObserver(()=>{ if(queued) return; queued=true; requestAnimationFrame(inject); }).observe(document.body,{childList:true,subtree:true});
}
wireManagedBuildUi();

initAuth();
