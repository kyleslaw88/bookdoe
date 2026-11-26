/* Managed library build UI (#219, #222-#224). ES module, loaded on demand with import() by app.js, which passes its
 * helpers in `ctx`: { api, $, $$, e, toast, modal, closeModal, fmtBytes, fmtDate, fmtNum, openPanel }.
 * Everything talks to /api/v1/managed-build/* and /api/v1/works/<id>/managed-copy (admin only; CSRF added by ctx.api).
 *
 *   buildPanel(ctx, host, preset)     THE build panel (one component): options, pilot filter, Dry run -> summary,
 *                                     typed-count Confirm, Start, live progress with Pause/Resume/Cancel and
 *                                     Retry/Skip, run history with Undo build. Used inline on Settings > Library
 *                                     roots and in a dialog from a book page.
 *   rootsEnhance(ctx, el, roots)      Settings > Library roots: coverage banner, plain-words mode, per-root counts
 *                                     and coverage, "Copy into managed library..." per adopted/observe root.
 *   overviewCard(ctx, host)           the "Managed library" card on Overview.
 *   bookState(ctx, workId, host)      a book page: "Managed copy: verified / Not copied yet / Copying n%", the
 *                                     one-book "Copy to managed library" and the admin "Source copies" disclosure.
 * Originals are never changed: every surface says so. Nothing starts without the typed confirmation. */

const PRODUCT = document.querySelector('meta[name="alx-product"]')?.content || 'bookdoe';  // the instance's product name, stamped into the page by the server
const MODE_TEXT = {
  managed: `Managed: ${PRODUCT} stores and organizes the files here.`,
  adopt: 'Adopt: read in place, never modified.',
  observe: 'Observe: read in place, hidden from management.',
};
const ACTION_LABEL = { copy: 'Copy (already EPUB/M4B)', convert: 'Convert to EPUB/M4B', copy_as_is: 'Copy as it is', skip: 'Skip' };
const STATE_BADGE = { running: 'info', paused: 'warn', interrupted: 'warn', stopped: 'warn', cancelled: '', completed: 'good' };
const BOOK_STATE = {
  verified: ['good', 'Managed copy: verified'], converted: ['good', 'Managed copy: converted'], managed: ['good', 'Stored in the managed library'],
  partial: ['warn', 'Managed copy: partly copied'], not_copied: ['', 'Not copied yet'], copying: ['info', 'Copying'],
};
const KIND_LABEL = { ebook: 'Ebook', audiobook: 'Audiobook' };
const UNDO_KEEPS = 'Converted books that someone has listened to or read (saved progress or bookmarks), or that existed before the build, are kept with their converted file and listed as "kept"; nothing of their history is deleted.';
const RUN_SWITCH_WARNING = 'Allow managed library builds to run? A running build copies (and converts) books into the managed library folders and switches the library to use those copies. Your original files are never changed, moved or deleted, and Undo build puts everything back. Switch it off again when you are done.';
const undoResult = r => {
  const kept = (r.problems || []).filter(p => String(p.code || '').startsWith('kept')).length;
  const refused = (r.problems || []).length - kept;
  return `Undone: ${r.undone_books} book(s), ${r.undone_files} file(s) moved to the managed trash${kept ? `; ${kept} kept (listening/reading history or older than the build)` : ''}${refused ? `; ${refused} refused: ${(r.problems.find(p => !String(p.code || '').startsWith('kept')) || {}).error || ''}` : ''}`;
};

const pct = (a, b) => (b ? Math.min(100, Math.round((100 * a) / b)) : 0);
const dur = s => { if (s == null) return '—'; s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return h ? `${h} h ${m} min` : `${m || (s ? '<1' : 0)} min`; };
const bar = p => `<div class="mb-bar" role="progressbar" aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100"><i style="width:${p}%"></i></div>`;

function coverageLine(ctx, c) {
  const { fmtNum, fmtBytes } = ctx;
  return `${fmtNum(c.books_covered)} of ${fmtNum(c.books_total)} books have a verified managed copy (${fmtBytes(c.bytes_covered) === '—' ? '0 B' : fmtBytes(c.bytes_covered)} of ${fmtBytes(c.bytes_total) === '—' ? '0 B' : fmtBytes(c.bytes_total)})`;
}

// ---- Settings > Library roots ------------------------------------------------------------------------

export async function rootsEnhance(ctx, el, roots) {
  const { api, $, $$, e, fmtNum, fmtBytes } = ctx;
  let st;
  try { st = await api('/api/v1/managed-build/status'); } catch { return; }
  const cov = st.coverage || { roots: [] };
  const byId = Object.fromEntries((cov.roots || []).map(r => [r.id, r]));
  const run = st.active_run;
  if (!$('#mb-roots-banner', el)) {
    const banner = document.createElement('div');
    banner.id = 'mb-roots-banner';
    banner.className = 'mb-banner';
    banner.innerHTML = `<div class="mb-banner-text"><strong>Managed library</strong><span id="mb-coverage-line">${e(coverageLine(ctx, cov))}</span>
      ${bar(pct(cov.bytes_covered, cov.bytes_total))}
      ${run ? `<span class="small">Build ${e(run.status)}: ${fmtNum(run.progress?.files_done || 0)} of ${fmtNum(run.progress?.files_total || 0)} files</span>` : '<span class="small muted">Copies books from adopted folders into the managed library in EPUB/M4B. Originals are never changed.</span>'}</div>
      <button class="button" id="mb-build-open" type="button">Build managed library…</button>`;
    const head = $('.setting-section', el);
    (head || el.firstChild) ? (head || el.firstChild).after(banner) : el.prepend(banner);
    $('#mb-build-open', banner).onclick = () => ctx.openPanel({});
  }
  for (const card of $$('.integration-card[data-root]', el)) {
    const id = card.dataset.root, r = byId[id], root = (roots || []).find(x => x.id === id) || {};
    if (!r || $('.mb-root-meta', card)) continue;
    const meta = document.createElement('div');
    meta.className = 'mb-root-meta';
    const counts = `${fmtNum(r.books)} book${r.books === 1 ? '' : 's'} · ${fmtBytes(r.bytes) === '—' ? '0 B' : fmtBytes(r.bytes)}`;
    const covered = r.mode === 'managed' ? '' : ` · ${fmtNum(r.covered_books || 0)} with a managed copy (${pct(r.covered_bytes || 0, r.bytes)}%)`;
    meta.innerHTML = `<span class="small mb-mode">${e(MODE_TEXT[r.mode] || r.mode)}</span><span class="small muted">${counts}${covered}</span>`;
    const text = card.firstElementChild;
    (text || card).appendChild(meta);
    if ((r.mode === 'adopt' || r.mode === 'observe') && root.enabled !== 0 && root.enabled !== false) {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'button secondary compact mb-root-copy'; btn.dataset.root = id;
      btn.textContent = 'Copy into managed library…';
      btn.title = 'Opens the build panel for this folder only. Nothing is copied before you confirm.';
      btn.onclick = () => ctx.openPanel({ root_ids: [id], label: r.name });
      ($('.integration-actions', card) || card).prepend(btn);
    }
  }
}

// ---- Overview card ----------------------------------------------------------------------------------

export async function overviewCard(ctx, host) {
  const { api, e, fmtNum } = ctx;
  let st;
  try { st = await api('/api/v1/managed-build/status'); } catch { host.remove(); return; }
  const c = st.coverage || {}, run = st.active_run;
  host.className = 'panel mb-overview';
  host.innerHTML = `<div class="panel-head"><h2>Managed library</h2><button class="button ghost compact" id="mb-overview-open" type="button">Open</button></div>
    <div class="panel-body"><div id="mb-overview-line">${e(coverageLine(ctx, c))}</div>${bar(pct(c.bytes_covered, c.bytes_total))}
    <div class="small muted">${run ? `Build ${e(run.status)}: ${fmtNum(run.progress?.files_done || 0)} of ${fmtNum(run.progress?.files_total || 0)} files${run.progress?.current?.length ? ` · ${e(run.progress.current.join(', '))}` : ''}` : 'Originals stay where they are; the managed copies are verified byte for byte.'}</div></div>`;
  host.querySelector('#mb-overview-open').onclick = () => ctx.openPanel({});
}

// ---- Book page --------------------------------------------------------------------------------------

export async function bookState(ctx, workId, host) {
  const { api, e, fmtBytes, fmtDate, toast } = ctx;
  let st;
  try { st = await api(`/api/v1/works/${encodeURIComponent(workId)}/managed-copy`); } catch { host.innerHTML = ''; return; }
  const assets = st.assets || [];
  if (!assets.length) { host.innerHTML = ''; return; }
  const lines = assets.map(a => {
    const [cls, label] = BOOK_STATE[a.state] || ['', a.state];
    const extra = a.state === 'copying' && a.percent != null ? ` ${a.percent}%` : '';
    return `<div class="mb-book-line"><span class="small">${e(KIND_LABEL[a.kind] || a.kind)}${a.format ? ` · ${e(String(a.format).toUpperCase())}` : ''}</span> <span class="badge ${cls} mb-book-state" data-state="${e(a.state)}">${e(label + extra)}</span></div>`;
  }).join('');
  const sources = assets.flatMap(a => a.sources || []);
  const canCopy = assets.some(a => a.state === 'not_copied' || a.state === 'partial');
  const canUndo = assets.some(a => (a.sources || []).length);
  host.className = 'panel mb-book';
  host.innerHTML = `<div class="panel-head"><h2>Managed library</h2></div><div class="panel-body">${lines}
    <div class="mb-actions">${canCopy ? '<button class="button secondary compact" id="mb-book-copy" type="button">Copy to managed library…</button>' : ''}
    ${canUndo ? '<button class="button ghost compact" id="mb-book-undo" type="button">Undo managed copy</button>' : ''}</div>
    ${sources.length ? `<details class="mb-sources"><summary>Source copies (${sources.length})</summary><p class="small muted">The original files. They are kept exactly where they are, never edited, counted or offered for removal.</p>
      <ul>${sources.map(s => `<li class="small"><span class="mono">${e(s.path)}</span> · ${fmtBytes(s.size_bytes)} · ${s.kind === 'convert' ? 'converted' : `verified (${e(s.verified || '')})`} ${fmtDate(s.copied_at)}</li>`).join('')}</ul></details>` : ''}</div>`;
  const copy = host.querySelector('#mb-book-copy');
  if (copy) copy.onclick = () => ctx.openPanel({ work_ids: [workId], label: 'this book' }, { dialog: true, onClose: () => bookState(ctx, workId, host) });
  const undo = host.querySelector('#mb-book-undo');
  if (undo) undo.onclick = async () => {
    if (!confirm(`Move the managed copies of this book to the managed trash and use the originals again? The originals are not touched.\n\n${UNDO_KEEPS}`)) return;
    undo.disabled = true;
    try { const r = await api(`/api/v1/works/${encodeURIComponent(workId)}/managed-copy/undo`, { method: 'POST', body: {} });
      toast(r.problems?.length && !r.undone_books ? r.problems[0].error : undoResult(r), r.problems?.length ? 'warn' : 'good'); }
    catch (er) { toast(er.message, 'bad'); }
    bookState(ctx, workId, host);
  };
}

// ---- The build panel --------------------------------------------------------------------------------

export async function buildPanel(ctx, host, preset = {}) {
  const { api, e, toast, fmtNum, fmtBytes, fmtDate } = ctx;
  const S = { preset: { ...preset }, plan: null, timer: null, filter: '', offset: 0, runEnabled: false };
  host.classList.add('mb-panel');
  const stop = () => { if (S.timer) { clearInterval(S.timer); S.timer = null; } };
  const alive = () => document.body.contains(host);

  async function load() {
    let st;
    try { st = await api('/api/v1/managed-build/status'); } catch (er) { host.innerHTML = `<div class="warning-box">${e(er.message)}</div>`; return; }
    const runs = await api('/api/v1/managed-build/runs?limit=10').then(r => r.items).catch(() => []);
    const run = st.active_run;
    S.runEnabled = !!st.run_enabled;
    host.innerHTML = `
      <div class="mb-intro"><p><strong>Copy your books into the managed library.</strong> Books already in EPUB or M4B are copied; MOBI/AZW and MP3 sets are converted to EPUB/M4B; anything else is copied as it is (or skipped). Every copy is verified before it is used. <strong>Originals are never changed, moved or deleted.</strong></p>
      <p class="small muted" id="mb-coverage">${e(coverageLine(ctx, st.coverage || {}))}</p></div>
      <div class="mb-switch" id="mb-run-switch"><label class="field mb-check"><input type="checkbox" id="mb-run-enabled" ${S.runEnabled ? 'checked' : ''}> <span><strong>Allow builds to run</strong></span></label>
      <p class="small ${S.runEnabled ? 'muted' : 'mb-switch-off'}" id="mb-run-switch-text">${S.runEnabled ? 'On: a confirmed build can start or resume. Turning this off pauses a running build after the current file; switch it off when you are done.' : 'Off: builds cannot start or resume (a build that was running pauses after the current file). Dry runs and Undo still work. Switch it on to copy books into the managed library (your originals are never changed).'}</p></div>
      <div id="mb-run">${run ? runHtml(run) : ''}</div>
      <div id="mb-setup">${run ? '' : setupHtml(st)}</div>
      <div id="mb-plan"></div>
      <div id="mb-history">${historyHtml(runs)}</div>`;
    wireSwitch();
    wireRun(run);
    wireSetup(st);
    wireHistory(runs);
    if (!run && S.plan) showPlan(S.plan);
    stop();
    if (run && run.status === 'running') S.timer = setInterval(poll, 2000);
  }

  function wireSwitch() {
    const box = host.querySelector('#mb-run-enabled');
    if (!box) return;
    box.onchange = async () => {
      const want = box.checked;
      if (want && !confirm(RUN_SWITCH_WARNING)) { box.checked = false; return; }
      box.disabled = true;
      try { await api('/api/v1/managed-build/settings', { method: 'PUT', body: { run_enabled: want } }); toast(want ? 'Builds may run' : 'Builds switched off; a running build pauses after the current file'); }
      catch (er) { toast(er.message, 'bad'); }
      await load();
    };
  }

  async function poll() {
    if (!alive()) return stop();
    try {
      const st = await api('/api/v1/managed-build/status');
      const run = st.active_run;
      if (!run || run.status !== 'running') { stop(); return load(); }
      const box = host.querySelector('#mb-run');
      if (box) { box.innerHTML = runHtml(run); wireRun(run); }
      const c = host.querySelector('#mb-coverage'); if (c) c.textContent = coverageLine(ctx, st.coverage || {});
    } catch { /* next tick */ }
  }

  function presetLabel() {
    const p = S.preset;
    if (p.work_ids) return `Only ${p.label || 'the chosen book'}`;
    if (p.root_ids) return `Only the folder ${p.label || ''}`;
    return '';
  }

  function setupHtml(st) {
    const roots = (st.coverage?.roots || []).filter(r => r.mode === 'adopt' || r.mode === 'observe');
    const chosenRoot = (S.preset.root_ids || [])[0] || '';
    return `<div class="mb-options">
      ${presetLabel() ? `<div class="mb-chip">${e(presetLabel())} <button type="button" class="button ghost compact" id="mb-clear-preset">Whole library</button></div>` : ''}
      <div class="mb-grid">
        <label class="field"><span>What to copy</span><select class="select" id="mb-mode"><option value="preferred">Preferred formats (EPUB, M4B)</option><option value="as_is">Exactly as they are</option></select></label>
        <label class="field"><span>Books that cannot be converted</span><select class="select" id="mb-nonconv"><option value="copy">Copy them as they are</option><option value="skip">Skip them</option></select></label>
        <label class="field"><span>Verify each copy</span><select class="select" id="mb-verify"><option value="full">Full: re-read and compare checksums</option><option value="size">Size only</option><option value="none">None (not recommended)</option></select></label>
        <label class="field"><span>Speed limit (MB/s, 0 = none)</span><input class="input" id="mb-throttle" type="number" min="0" max="5000" value="50"></label>
        <label class="field"><span>Books at a time</span><select class="select" id="mb-conc"><option value="1">1</option><option value="2">2</option></select></label>
        <label class="field mb-check"><input type="checkbox" id="mb-stop"> <span>Stop at the first error</span></label>
      </div>
      ${S.preset.work_ids ? '' : `<div class="mb-grid mb-pilot"><div class="small muted mb-pilot-note">Pilot: try a few books first.</div>
        <label class="field"><span>Kind</span><select class="select" id="mb-kind"><option value="">Ebooks and audiobooks</option><option value="ebook">Ebooks</option><option value="audiobook">Audiobooks</option></select></label>
        <label class="field"><span>Folder</span><select class="select" id="mb-root"><option value="">All adopted folders</option>${roots.map(r => `<option value="${e(r.id)}" ${r.id === chosenRoot ? 'selected' : ''}>${e(r.name)} (${e(r.mode)})</option>`).join('')}</select></label>
        <label class="field"><span>First N books (empty = all)</span><input class="input" id="mb-limit" type="number" min="1" placeholder="all"></label></div>`}
      <div class="mb-actions"><button class="button" id="mb-dry-run" type="button">Dry run</button><span class="small muted">A dry run only reads: it shows what would happen.</span></div>
    </div>`;
  }

  function options() {
    const v = id => host.querySelector(id)?.value;
    const inc = {};
    if (S.preset.work_ids) inc.work_ids = S.preset.work_ids;
    const root = v('#mb-root') || (S.preset.root_ids || [])[0];
    if (root) inc.root_ids = [root];
    if (v('#mb-kind')) inc.kinds = [v('#mb-kind')];
    const lim = parseInt(v('#mb-limit') || '', 10);
    if (lim > 0) inc.limit_books = lim;
    return { mode: v('#mb-mode'), non_convertible: v('#mb-nonconv'), verify: v('#mb-verify'), throttle_mb_s: Math.max(0, parseInt(v('#mb-throttle') || '0', 10) || 0),
             concurrency: parseInt(v('#mb-conc') || '1', 10), stop_on_error: !!host.querySelector('#mb-stop')?.checked, include: inc };
  }

  function wireSetup() {
    const clear = host.querySelector('#mb-clear-preset');
    if (clear) clear.onclick = () => { S.preset = {}; S.plan = null; load(); };
    const dry = host.querySelector('#mb-dry-run');
    if (!dry) return;
    dry.onclick = async () => {
      dry.disabled = true; dry.textContent = 'Working…';
      try { S.plan = await api('/api/v1/managed-build/plans', { method: 'POST', body: options() }); S.filter = ''; S.offset = 0; showPlan(S.plan); }
      catch (er) { toast(er.message, 'bad'); }
      dry.disabled = false; dry.textContent = 'Dry run';
    };
  }

  function showPlan(plan) {
    const box = host.querySelector('#mb-plan');
    if (!box) return;
    const s = plan.summary || {}, ba = s.by_action || {};
    const rows = ['copy', 'convert', 'copy_as_is', 'skip'].filter(a => ba[a]).map(a =>
      `<tr><td>${e(ACTION_LABEL[a])}</td><td>${fmtNum(ba[a].assets)}</td><td>${fmtNum(ba[a].files)}</td><td>${a === 'skip' ? '—' : fmtBytes(ba[a].est_bytes)}</td></tr>`).join('');
    const dests = (s.destinations || []).map(d => `<li class="${d.fits ? '' : 'mb-bad'}"><strong>${e(d.name || '')}</strong>: needs ${fmtBytes(d.required_bytes)} + ${fmtBytes(d.margin_bytes)} margin, ${d.free_bytes == null ? 'free space unknown' : fmtBytes(d.free_bytes) + ' free'}${d.fits ? '' : ' (not enough)'}</li>`).join('');
    const skips = Object.entries(s.skips || {}).map(([code, x]) => `<li>${fmtNum(x.books || x.count)} book(s): ${e(x.reason || code)}</li>`).join('');
    const warns = (s.warnings || []).map(w => `<div class="warning-box small">${e(w)}</div>`).join('');
    const confirmBox = plan.executed ? '<p class="small muted">This plan has been run.</p>' : plan.confirmed
      ? `<div class="mb-actions"><button class="button" id="mb-start" type="button" ${S.runEnabled ? '' : 'disabled'}>Start build</button><span class="small">Confirmed: ${fmtNum(plan.book_count)} book(s).${S.runEnabled ? '' : ' Switch on "Allow builds to run" above to start.'}</span></div>`
      : plan.book_count ? `<div class="mb-confirm"><label class="field"><span>Type the number of books (${fmtNum(plan.book_count)}) to confirm</span><input class="input" id="mb-confirm-count" inputmode="numeric" autocomplete="off"></label>
          <div class="mb-actions"><button class="button" id="mb-confirm" type="button" disabled>Confirm</button><button class="button" id="mb-start" type="button" disabled>Start build</button></div></div>`
        : '<p class="small muted">Nothing to copy with these options.</p>';
    box.innerHTML = `<div class="mb-summary" id="mb-summary"><h3>Dry run: ${fmtNum(plan.book_count || 0)} book(s), ${fmtNum(s.files || 0)} file(s), ${fmtBytes(s.bytes) === '—' ? '0 B' : fmtBytes(s.bytes)}</h3>
      <div class="table-wrap"><table class="mb-table"><thead><tr><th>What happens</th><th>Books</th><th>Files</th><th>Size</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">Nothing found</td></tr>'}</tbody></table></div>
      <p class="small">Estimated time: about ${dur(s.estimated_seconds)} (at ${fmtBytes(s.rate_bytes_per_second)}/s${(s.by_action || {}).convert ? '; conversions are a rough guess' : ''}).</p>
      ${dests ? `<p class="small"><strong>Destinations</strong></p><ul class="small mb-list">${dests}</ul>` : ''}
      ${skips ? `<details class="small"><summary>Skipped (${Object.values(s.skips).reduce((n, x) => n + (x.books || 0), 0)} book(s))</summary><ul class="mb-list">${skips}</ul></details>` : ''}
      ${s.collisions && Object.keys(s.collisions).length ? `<p class="small">${fmtNum(Object.values(s.collisions).reduce((a, b) => a + b, 0))} name(s) already taken: those files get a numbered name, an identical file is reused. Nothing is ever overwritten.</p>` : ''}
      ${warns}
      <div class="mb-items"><div class="mb-actions"><select class="select compact" id="mb-item-filter"><option value="">All files</option>${Object.keys(ACTION_LABEL).map(a => `<option value="${a}" ${S.filter === a ? 'selected' : ''}>${e(ACTION_LABEL[a])}</option>`).join('')}<option value="collision" ${S.filter === 'collision' ? 'selected' : ''}>Name already taken</option></select></div><div id="mb-item-list" class="small"></div></div>
      ${confirmBox}</div>`;
    loadItems();
    host.querySelector('#mb-item-filter').onchange = ev => { S.filter = ev.target.value; S.offset = 0; loadItems(); };
    const input = host.querySelector('#mb-confirm-count'), conf = host.querySelector('#mb-confirm'), start = host.querySelector('#mb-start');
    if (input) input.oninput = () => { conf.disabled = String(parseInt(input.value, 10)) !== String(plan.book_count); };
    if (conf) conf.onclick = async () => {
      conf.disabled = true;
      try { S.plan = await api(`/api/v1/managed-build/plans/${plan.plan_id}/confirm`, { method: 'POST', body: { book_count: parseInt(input.value, 10) } }); showPlan(S.plan); toast('Plan confirmed'); }
      catch (er) { toast(er.message, 'bad'); conf.disabled = false; }
    };
    if (start && (plan.confirmed && !plan.executed) && S.runEnabled) start.onclick = async () => {
      start.disabled = true; start.textContent = 'Starting…';
      try { await api(`/api/v1/managed-build/plans/${plan.plan_id}/start`, { method: 'POST', body: {} }); S.plan = null; toast('Build started'); load(); }
      catch (er) { toast(er.message, 'bad'); start.disabled = false; start.textContent = 'Start build'; }
    };
  }

  async function loadItems() {
    const list = host.querySelector('#mb-item-list');
    if (!list || !S.plan) return;
    const q = S.filter === 'collision' ? '&collision=true' : S.filter ? `&action=${S.filter}` : '';
    try {
      const d = await api(`/api/v1/managed-build/plans/${S.plan.plan_id}/items?limit=20&offset=${S.offset}${q}`);
      list.innerHTML = `<ul class="mb-list">${d.items.map(i => `<li><strong>${e(i.title || '')}</strong> · ${e(ACTION_LABEL[i.action] || i.action)}${i.skip_reason ? `: ${e(i.skip_reason)}` : ''}<div class="mono mb-path">${e(i.source_path || '')}${i.dest_path && i.action !== 'skip' ? ` → ${e(i.dest_path)}` : ''}</div></li>`).join('') || '<li class="muted">None</li>'}</ul>
        <div class="mb-actions">${S.offset ? '<button class="button ghost compact" id="mb-prev" type="button">Previous</button>' : ''}<span class="muted">${fmtNum(Math.min(d.total, S.offset + 1))}–${fmtNum(Math.min(d.total, S.offset + d.items.length))} of ${fmtNum(d.total)}</span>${S.offset + d.items.length < d.total ? '<button class="button ghost compact" id="mb-next" type="button">Next</button>' : ''}</div>`;
      const n = list.querySelector('#mb-next'), p = list.querySelector('#mb-prev');
      if (n) n.onclick = () => { S.offset += 20; loadItems(); };
      if (p) p.onclick = () => { S.offset = Math.max(0, S.offset - 20); loadItems(); };
    } catch (er) { list.textContent = er.message; }
  }

  function runHtml(run) {
    const p = run.progress || {};
    const fails = (run.failures || []).map(f => `<li><strong>${e(f.title || '')}</strong>: ${e(f.error || '')}<div class="mb-actions"><button class="button ghost compact mb-retry" data-item="${f.id}" type="button">Retry</button><button class="button ghost compact mb-skip" data-item="${f.id}" type="button">Skip</button></div></li>`).join('');
    const running = run.status === 'running';
    return `<div class="mb-runbox" id="mb-runbox" data-status="${e(run.status)}"><div class="mb-run-head"><strong>Build</strong> <span class="badge ${STATE_BADGE[run.status] || ''}" id="mb-run-status">${e(run.status)}${run.pause_requested && running ? ' (pausing…)' : ''}${run.cancel_requested && running ? ' (cancelling…)' : ''}</span></div>
      ${bar(pct(p.bytes_done || 0, p.bytes_total || 0))}
      <div class="small">${fmtNum(p.files_done || 0)} of ${fmtNum(p.files_total || 0)} files · ${fmtBytes(p.bytes_done) === '—' ? '0 B' : fmtBytes(p.bytes_done)} of ${fmtBytes(p.bytes_total)}${running && p.bytes_per_second ? ` · ${fmtBytes(p.bytes_per_second)}/s · about ${dur(p.eta_seconds)} left` : ''}</div>
      ${running && p.current?.length ? `<div class="small">Now: ${e(p.current.join(', '))}</div>` : ''}
      ${run.stop_reason ? `<div class="warning-box small">${e(run.stop_reason)}</div>` : ''}
      <div class="mb-actions">${running ? '<button class="button secondary compact" id="mb-pause" type="button">Pause</button>' : ''}${['paused', 'interrupted', 'stopped'].includes(run.status) ? `<button class="button compact" id="mb-resume" type="button" ${S.runEnabled ? '' : 'disabled title="Switch on Allow builds to run first"'}>Resume</button>` : ''}<button class="button ghost compact" id="mb-cancel" type="button">Cancel</button></div>
      ${fails ? `<details open class="small"><summary>${(run.failures || []).length} failed</summary><ul class="mb-list">${fails}</ul></details>` : ''}</div>`;
  }

  function wireRun(run) {
    if (!run) return;
    const act = async (path, body, msg) => { try { await api(`/api/v1/managed-build/runs/${run.run_id}/${path}`, { method: 'POST', body: body || {} }); if (msg) toast(msg); } catch (er) { toast(er.message, 'bad'); } load(); };
    const b = id => host.querySelector(id);
    if (b('#mb-pause')) b('#mb-pause').onclick = () => act('pause', null, 'Pausing after the current file');
    if (b('#mb-resume')) b('#mb-resume').onclick = () => act('resume', null, 'Resumed');
    if (b('#mb-cancel')) b('#mb-cancel').onclick = () => { if (confirm('Cancel the build? Books already copied stay (use Undo build to remove them); the rest is not copied.')) act('cancel', null, 'Cancelling'); };
    host.querySelectorAll('.mb-retry').forEach(x => x.onclick = () => act(`items/${x.dataset.item}/retry`, null, 'Queued again'));
    host.querySelectorAll('.mb-skip').forEach(x => x.onclick = () => act(`items/${x.dataset.item}/skip`, null, 'Skipped'));
  }

  function historyHtml(runs) {
    if (!runs.length) return '';
    return `<h3 class="mb-h">Build history</h3><ul class="mb-list mb-runs">${runs.map(r => `<li class="mb-run-row" data-run="${e(r.run_id)}"><div><span class="badge ${STATE_BADGE[r.status] || ''}">${e(r.status)}</span> ${fmtDate(r.started_at)} · ${fmtNum(r.counts?.done || 0)} file(s) done${r.counts?.failed ? `, ${fmtNum(r.counts.failed)} failed` : ''} · ${fmtNum(r.active_books || 0)} book(s) with copies</div>
      ${r.active_books && r.status !== 'running' ? `<p class="small muted mb-undo-note">${e(UNDO_KEEPS)}</p><div class="mb-undo"><input class="input compact mb-undo-count" inputmode="numeric" placeholder="type ${fmtNum(r.active_books)}" aria-label="Type the number of books to undo"><button class="button ghost compact mb-undo-btn" type="button" data-run="${e(r.run_id)}" data-books="${r.active_books}">Undo build</button></div>` : ''}
      <button class="button ghost compact mb-journal-btn" type="button" data-run="${e(r.run_id)}">Journal</button><div class="mb-journal small"></div></li>`).join('')}</ul>`;
  }

  function wireHistory() {
    host.querySelectorAll('.mb-undo-btn').forEach(btn => btn.onclick = async () => {
      const input = btn.parentElement.querySelector('.mb-undo-count');
      const n = parseInt(input.value, 10);
      if (n !== parseInt(btn.dataset.books, 10)) { toast(`Type ${btn.dataset.books} to undo this build`, 'warn'); input.focus(); return; }
      btn.disabled = true;
      try { const r = await api(`/api/v1/managed-build/runs/${btn.dataset.run}/undo`, { method: 'POST', body: { confirm_count: n } });
        toast(undoResult(r), r.problems?.length ? 'warn' : 'good'); }
      catch (er) { toast(er.message, 'bad'); }
      load();
    });
    host.querySelectorAll('.mb-journal-btn').forEach(btn => btn.onclick = async () => {
      const box = btn.nextElementSibling;
      if (box.innerHTML) { box.innerHTML = ''; return; }
      try { const j = await api(`/api/v1/managed-build/runs/${btn.dataset.run}/journal?limit=50`);
        box.innerHTML = `<ul class="mb-list">${j.items.map(x => `<li>${e(x.title || '')} · ${e(x.status)}${x.undo_state ? ` (${e(x.undo_state)})` : ''}<div class="mono mb-path">${e(x.source_path)} → ${e(x.managed_path)}</div></li>`).join('') || '<li class="muted">Nothing copied</li>'}</ul>${j.total > j.items.length ? `<div class="muted">${fmtNum(j.total - j.items.length)} more</div>` : ''}`; }
      catch (er) { box.textContent = er.message; }
    });
  }

  await load();
  return { reload: load, stop };
}
