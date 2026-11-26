/* Library-wide duplicate finder and bulk merge (admin only). ES module, loaded on demand with import() by app.js
 * (renderDuplicates), which passes its helpers in `ctx`:
 *   { api, $, $$, e, toast, modal, closeModal, fmtBytes, fmtDate, fmtNum, escAttr }
 * Talks to /api/v1/admin/duplicates (+ /status, /scan, /dismiss, /dry-run, /bulk-merge, /batches...) and
 * /api/v1/admin/placeholder-assets/cleanup (CSRF is added by ctx.api).
 *
 *   duplicatesPage(ctx, host)   the Duplicates page: scan status, filters, groups side by side, bulk merge, batches
 *
 * Nothing here deletes a file: a merge moves database records; redundant copies are removed later in the separate,
 * confirmed "Duplicate files" step on the book page. Groups the dry run cannot merge safely are listed as "needs review"
 * with a link to the normal merge dialog (merge.js). */

const enc = encodeURIComponent;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const errText = er => (er && er.payload && er.payload.detail && er.payload.detail.message) || (er && er.message) || 'Request failed';
const CHUNK_DELAY_MS = 1000, MAX_CHUNKS = 150;      // pause between requests, and a hard cap on one browser loop
const sleep = ms => new Promise(r => setTimeout(r, ms));
/* Drive a batch chunk by chunk. Stops (and says why) when a chunk made no progress, when the batch left 'running', or at
 * the hard cap; waits at least CHUNK_DELAY_MS between requests. Returns { b, note }. */
async function driveBatch(ctx, b, onProgress) {
  const { api } = ctx;
  for (let i = 0; i < MAX_CHUNKS; i++) {
    if (onProgress) onProgress(b);
    if (b.status !== 'running') return { b, note: '' };
    if (b.rate_limited) { await sleep(Math.min(65, b.rate_limited.retry_after + 1) * 1000); }
    else if (b.processed === 0) return { b, note: 'No progress: another worker may still be merging, or a merge got stuck. Wait a minute, then press Continue / Recover.' };
    else await sleep(CHUNK_DELAY_MS);
    b = await api(`/api/v1/admin/duplicates/batches/${enc(b.id)}/continue`, { method: 'POST', body: {} });
  }
  return { b, note: `Paused after ${MAX_CHUNKS} requests. Press Continue to carry on.` };
}
const CONF = { high: ['good', 'High'], medium: ['warn', 'Medium'], low: ['', 'Low'] };
const REASON = {
  refresh_conflict: 'Refreshing its metadata failed because the other record already holds the provider record',
  file_only_vs_provider: 'One record is from files only, the other has the provider metadata',
  same_authors: 'same author', title_equal: 'same title', title_equal_after_series_suffix: 'same title once the series note is removed',
  same_position: 'same series position', shared_identifier: 'shared identifier (ISBN, ASIN or provider id)',
  titles_differ: 'titles differ: review it in the merge dialog', authors_unknown: 'an author is missing on one side',
};
const BLOCKER = {
  kid_grant_widening: 'A child could see more after this merge', tag_grant_widening: 'A child could see more after this merge',
  adult_into_kids_canonical: 'Adult files would join a Kids-shelf book', kid_visibility_widening: 'A child could see more after this merge',
  kids_tag_mismatch: 'Only one record is on the Kids shelf', dissimilar_works: 'The titles differ',
  progress_conflict: 'Both have reading or listening progress for the same person', rating_conflict: 'Both carry a different rating from the same person',
  read_state_conflict: 'Both carry a different reading status', series_position_conflict: 'They name different series positions',
  overlap: 'Shares a record with another selected group', dismissed: 'Marked as not the same book', stale_group: 'Changed since the scan: scan again',
  unhandled_tables: 'The merge does not handle every table yet', source_already_merged: 'Already merged', not_found: 'No longer exists',
  too_many: 'Too many records for a bulk run', stopped_after_an_error: 'Not run: stopped after an error',
};
const blockerText = r => BLOCKER[r.code] || r.message || r.code;
const when = (ctx, t) => (t ? ctx.fmtDate(t) : 'never');

export async function duplicatesPage(ctx, host) {
  const { api, $, $$, e, toast, fmtNum } = ctx;
  const st = { f: { confidence: '', kind: '', has_owned: '', series_id: '', series_name: '', q: '', dismissed: false }, offset: 0, total: 0, items: [], selected: new Set(), canon: {}, status: null, seq: 0, poll: null, loading: false };
  const PAGE = 25;

  host.innerHTML = `<div class="page-head"><div><h1>Duplicates</h1><p id="dup-sub">Looking for books that are in the library twice…</p></div>
      <div class="page-actions"><button class="button" id="dup-scan">Scan library</button><button class="button secondary" id="dup-more">More…</button></div></div>
    <div id="dup-progress"></div>
    <div class="panel dup-filters"><div class="panel-body">
      <div class="toolbar"><input class="input grow" id="dup-q" type="search" enterkeyhint="search" autocomplete="off" placeholder="Search title or author…">
        <select class="select" id="dup-conf" aria-label="Confidence"><option value="">All confidence</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select>
        <select class="select" id="dup-kind" aria-label="Kind"><option value="">Any kind</option><option value="refresh_conflict">Refresh conflict</option><option value="file_vs_provider">File vs provider record</option><option value="identifier">Shared identifier</option><option value="title_author">Same title and author</option></select>
        <select class="select" id="dup-owned" aria-label="Owned files"><option value="">Owned or not</option><option value="true">Has owned files</option><option value="false">No owned files</option></select></div>
      <div id="dup-chips" class="filter-chips" hidden></div></div></div>
    <div id="dup-bar" class="dup-bar"></div>
    <div id="dup-list" class="dup-list"></div>
    <div style="margin-top:12px"><button class="button secondary hidden" id="dup-load-more">Load more</button></div>
    <div id="dup-batches" style="margin-top:20px"></div>`;

  const query = off => {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(off) });
    const f = st.f;
    if (f.confidence) p.set('confidence', f.confidence);
    if (f.kind) p.set('kind', f.kind);
    if (f.has_owned) p.set('has_owned', f.has_owned);
    if (f.series_id) p.set('series_id', f.series_id);
    if (f.q) p.set('q', f.q);
    if (f.dismissed) p.set('dismissed', 'true');
    return p;
  };

  /* ------------------------------------------------------------ status and scan */
  const paintStatus = () => {
    const s = st.status; if (!s) return;
    const c = s.counts || {};
    const running = s.status === 'running' || (s.running || []).some(r => r.kind === 'duplicate-scan');
    $('#dup-sub').textContent = running ? 'Scanning the library…'
      : s.status === 'partial' ? `A scan was ${s.interrupted ? 'interrupted' : 'paused'} at ${fmtNum(s.blocks_done)} of ${fmtNum(s.blocks_total)} title groups. Resume it to carry on where it stopped.`
      : s.last_complete_at ? `Last scanned ${when(ctx, s.last_complete_at)} · ${fmtNum(c.high || 0)} high, ${fmtNum(c.medium || 0)} medium, ${fmtNum(c.low || 0)} low confidence${c.dismissed ? ` · ${fmtNum(c.dismissed)} dismissed` : ''}`
        : 'Not scanned yet. A scan reads the whole library once; later scans only look at books that changed.';
    const paused = !running && s.status === 'partial';
    $('#dup-scan').disabled = running;
    $('#dup-scan').innerHTML = running ? '<span class="spinner"></span> Scanning' : (paused ? 'Resume scan' : (s.last_complete_at ? 'Scan again' : 'Scan library'));
    const pct = s.blocks_total ? Math.min(100, Math.round(100 * s.blocks_done / s.blocks_total)) : 0;
    $('#dup-progress').innerHTML = running
      ? `<div class="panel" style="margin-bottom:12px"><div class="panel-body"><strong>${fmtNum(s.blocks_done)} of ${fmtNum(s.blocks_total)}</strong> title groups checked · ${fmtNum(s.groups_found)} candidate groups so far<div class="series-progress" style="margin-top:8px"><i style="width:${pct}%"></i></div><div class="muted small">A scan can be interrupted and picks up where it stopped.</div></div></div>`
      : (s.status === 'partial' ? `<div class="muted small" style="margin-bottom:8px">Groups found so far are listed below.</div>` : s.open_conflicts ? `<div class="muted small" style="margin-bottom:8px">${plural(s.open_conflicts, 'book')} could not refresh their metadata because another record holds it; they rank first below.</div>` : '');
  };
  const refreshStatus = async () => {
    try { st.status = await api('/api/v1/admin/duplicates/status'); } catch (er) { toast(errText(er), 'bad'); return; }
    paintStatus();
    const running = st.status.status === 'running' || (st.status.running || []).some(r => r.kind === 'duplicate-scan');
    clearTimeout(st.poll);
    if (running && document.body.contains(host)) st.poll = setTimeout(async () => { await refreshStatus(); if (!(st.status.status === 'running')) load(false); }, 2000);
  };
  $('#dup-scan').onclick = async () => {
    try { await api('/api/v1/admin/duplicates/scan', { method: 'POST', body: { full: false } }); toast(st.status && st.status.status === 'partial' ? 'Scan resumed' : 'Scan started'); } catch (er) { toast(errText(er), 'bad'); }
    refreshStatus();
  };
  $('#dup-more').onclick = () => moreDialog();

  /* ------------------------------------------------------------ list */
  const chip = (k, label) => `<button type="button" class="chip filter-chip" data-clear="${e(k)}"><span class="filter-chip-text">${e(label)}</span> <span aria-hidden="true">×</span></button>`;
  const paintChips = () => {
    const f = st.f, c = [];
    if (f.series_id) c.push(chip('series_id', 'Series: ' + (f.series_name || f.series_id)));
    if (f.dismissed) c.push(chip('dismissed', 'Showing dismissed'));
    const box = $('#dup-chips'); box.hidden = !c.length; box.innerHTML = c.join('');
    $$('[data-clear]', box).forEach(b => b.onclick = () => { if (b.dataset.clear === 'dismissed') st.f.dismissed = false; else { st.f.series_id = ''; st.f.series_name = ''; } load(false); });
  };

  const memberHtml = (g, m) => {
    const keep = (st.canon[g.id] || g.canonical_work_id) === m.id;
    const assets = m.assets.filter(a => a.owned || a.files).map(a => `<div class="small"><span class="badge good">${e(String(a.format || a.kind).toUpperCase())}</span> ${plural(a.files, 'file')}${a.size_bytes ? ' · ' + e(ctx.fmtBytes(a.size_bytes)) : ''}</div>`).join('')
      || '<div class="muted small">No owned files</div>';
    const q = m.quality;
    return `<label class="dup-member ${keep ? 'keep' : ''}" data-id="${e(m.id)}">
      <div class="dup-member-head"><input type="radio" name="dup-canon-${e(g.id)}" value="${e(m.id)}" ${keep ? 'checked' : ''} aria-label="Keep &quot;${e(m.title)}&quot;">
        <span class="badge ${keep ? 'good' : ''}">${keep ? 'Keep this record' : 'Merged into the kept one'}</span><span class="badge ${m.provider_linked ? 'good' : 'info'}">${e(m.source_label)}</span></div>
      <div class="dup-member-body">${m.has_cover ? `<img class="dup-cover" loading="lazy" alt="" src="${e(m.cover_url)}" data-img-fallback="hide">` : '<div class="dup-cover dup-cover-none" aria-hidden="true"></div>'}
        <div class="dup-member-text"><a class="dup-title" href="#library/${e(m.id)}">${e(m.title)}</a>
          <div class="muted small">${e(m.authors.join(', ') || 'No author')}</div>
          <div class="small">${m.series.length ? m.series.map(s => e(s.name) + (s.position ? ' #' + e(s.position) : '')).join(' · ') : '<span class="muted">No series</span>'}</div></div></div>
      <div class="dup-member-assets">${assets}</div>
      <div class="dup-quality small"><span class="muted">Metadata ${q.score}/100:</span> ${q.has.map(h => `<span class="badge good">${e(h)}</span>`).join(' ')} ${q.lacks.map(h => `<span class="badge chip-missing">no ${e(h)}</span>`).join(' ')}</div></label>`;
  };

  const groupHtml = g => {
    const [cls, label] = CONF[g.confidence] || ['', g.confidence];
    const reasons = g.reasons.filter(r => REASON[r]).map(r => REASON[r]);
    const ids = g.reasons.filter(r => r.startsWith('identifier:')).map(r => r.slice(11));
    return `<article class="dup-group" data-id="${e(g.id)}">
      <div class="dup-group-head"><label class="dup-check"><input type="checkbox" class="dup-pick" value="${e(g.id)}" ${st.selected.has(g.id) ? 'checked' : ''} aria-label="Select ${e(g.title)}"></label>
        <span class="badge ${cls}">${e(label)}</span><span class="badge">${e(g.kind_label)}</span>${g.has_owned ? '<span class="badge info">has owned files</span>' : ''}
        ${g.series.map(s => `<button type="button" class="chip dup-series" data-series="${e(s.id)}" data-name="${e(s.name)}" title="Show only this series">${e(s.name)}</button>`).join('')}</div>
      <div class="dup-members">${g.members.map(m => memberHtml(g, m)).join('')}</div>
      <div class="muted small dup-why">${e(reasons.join(' · '))}${ids.length ? ' · ' + e(ids.join(', ')) : ''}</div>
      <div class="small dup-reco">${e(g.canonical_reason || '')}</div>
      <div class="dup-actions"><button class="button compact dup-review" data-id="${e(g.id)}">Review in merge dialog</button>
        <button class="button ghost compact dup-dismiss" data-id="${e(g.id)}">${g.dismissed ? 'Undo: not the same book' : 'Not the same book'}</button></div></article>`;
  };

  const paintBar = () => {
    const n = st.selected.size;
    $('#dup-bar').innerHTML = `<div class="attention-card calm bulk-bar-card"><div><strong>${fmtNum(n)} selected</strong><div class="muted small">${fmtNum(st.total)} group${st.total === 1 ? '' : 's'} match${st.total === 1 ? 'es' : ''} the filters. Medium and low confidence groups are only merged when you tick them yourself.</div></div>
      <div class="bulk-actions"><button class="button ghost compact" id="dup-sel-high">Select all high confidence</button>${n ? '<button class="button ghost compact" id="dup-sel-none">Clear</button>' : ''}<button class="button compact" id="dup-merge" ${n ? '' : 'disabled'}>Merge selected…</button></div></div>`;
    $('#dup-sel-high').onclick = selectAllHigh;
    $('#dup-sel-none')?.addEventListener('click', () => { st.selected.clear(); paintList(); });
    $('#dup-merge').onclick = () => bulkDialog();
  };

  const wireList = () => {
    $$('.dup-pick').forEach(c => c.onchange = () => { c.checked ? st.selected.add(c.value) : st.selected.delete(c.value); paintBar(); });
    $$('.dup-series').forEach(b => b.onclick = () => { st.f.series_id = b.dataset.series; st.f.series_name = b.dataset.name; load(false); });
    $$('.dup-group input[type=radio]').forEach(r => r.onchange = () => {
      const gid = r.name.replace('dup-canon-', ''); st.canon[gid] = r.value;
      $$(`.dup-group[data-id="${gid}"] .dup-member`).forEach(m => { m.classList.toggle('keep', m.dataset.id === r.value); const b = $('.badge', m); b.className = 'badge' + (m.dataset.id === r.value ? ' good' : ''); b.textContent = m.dataset.id === r.value ? 'Keep this record' : 'Merged into the kept one'; });
    });
    $$('.dup-review').forEach(b => b.onclick = async () => {
      const g = st.items.find(x => x.id === b.dataset.id);
      const m = await import('/assets/merge.js'); m.mergeDialog(ctx, g.work_ids);
    });
    $$('.dup-dismiss').forEach(b => b.onclick = async () => {
      const g = st.items.find(x => x.id === b.dataset.id);
      try { await api(`/api/v1/admin/duplicates/${enc(g.id)}/dismiss`, { method: 'POST', body: { dismissed: !g.dismissed } }); st.selected.delete(g.id); toast(g.dismissed ? 'Back in the list' : 'Remembered: not the same book'); await load(false); refreshStatus(); } catch (er) { toast(errText(er), 'bad'); }
    });
  };

  const paintList = () => {
    $('#dup-list').innerHTML = st.items.length ? st.items.map(groupHtml).join('')
      : `<div class="panel"><div class="empty"><strong>${st.status && !st.status.last_complete_at ? 'No scan yet' : 'No duplicates found'}</strong>${st.status && !st.status.last_complete_at ? 'Press Scan library to look for books that are in the library twice.' : 'Nothing matches these filters.'}</div></div>`;
    $('#dup-load-more').classList.toggle('hidden', st.items.length >= st.total);
    paintChips(); paintBar(); wireList();
  };

  async function load(append) {
    const seq = ++st.seq;
    st.loading = true;
    try {
      const off = append ? st.items.length : 0;
      const d = await api(`/api/v1/admin/duplicates?${query(off)}`);
      if (seq !== st.seq) return;
      st.items = append ? st.items.concat(d.items) : d.items; st.total = d.total;
      paintList();
    } catch (er) { $('#dup-list').innerHTML = `<div class="warning-box"><strong>Could not load the groups.</strong> ${e(errText(er))}</div>`; }
    finally { st.loading = false; }
  }
  $('#dup-load-more').onclick = () => load(true);
  let timer;
  $('#dup-q').oninput = ev => { clearTimeout(timer); const v = ev.target.value.trim(); timer = setTimeout(() => { st.f.q = v; load(false); }, 300); };
  $('#dup-conf').onchange = ev => { st.f.confidence = ev.target.value; load(false); };
  $('#dup-kind').onchange = ev => { st.f.kind = ev.target.value; load(false); };
  $('#dup-owned').onchange = ev => { st.f.has_owned = ev.target.value; load(false); };

  /* "Select all high confidence": every high group in the library, not only the page on screen. */
  async function selectAllHigh() {
    const btn = $('#dup-sel-high'); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
    try {
      let off = 0, n = 0;
      for (;;) {
        const d = await api(`/api/v1/admin/duplicates?confidence=high&limit=200&offset=${off}&light=true`);
        d.items.forEach(g => { st.selected.add(g.id); n++; });
        off += d.items.length;
        if (!d.items.length || off >= d.total) break;
      }
      toast(`${plural(n, 'high confidence group')} selected`);
    } catch (er) { toast(errText(er), 'bad'); }
    paintList();
  }

  /* ------------------------------------------------------------ bulk merge */
  async function bulkDialog() {
    const ids = [...st.selected];
    const body = { group_ids: ids, canonicals: Object.fromEntries(Object.entries(st.canon).filter(([g]) => st.selected.has(g))) };
    const root = ctx.modal('Merge selected', '<div id="bm-body"><div class="empty"><span class="spinner"></span> Checking each group…</div></div>', { wide: true,
      footer: '<button class="button ghost" id="bm-close">Close</button><button class="button" id="bm-go" disabled>Merge</button>' });
    $('#bm-close', root).onclick = () => { root.innerHTML = ''; };
    let dry;
    try { dry = await api('/api/v1/admin/duplicates/dry-run', { method: 'POST', body }); } catch (er) { $('#bm-body', root).innerHTML = `<div class="warning-box">${e(errText(er))}</div>`; return; }
    const ready = dry.groups.filter(g => g.status === 'ready'), review = dry.groups.filter(g => g.status !== 'ready');
    const moves = ready.reduce((a, g) => ({ files: a.files + (g.moves.files || 0), assets: a.assets + (g.moves.folded_assets || 0) }), { files: 0, assets: 0 });
    const row = g => `<tr><td><strong>${e(g.title || '?')}</strong><div class="muted small">${g.status === 'ready' ? `keeps ${e(g.canonical_title || '')}` : ''}</div></td>
      <td>${g.status === 'ready' ? `<span class="badge good">Ready</span>` : `<span class="badge warn">Needs review</span>`}</td>
      <td class="small">${g.status === 'ready' ? `${plural(g.moves.files || 0, 'file')} move${(g.moves.files || 0) === 1 ? 's' : ''}${g.moves.folded_assets ? ', ' + plural(g.moves.folded_assets, 'asset') + ' folded' : ''}${g.moves.field_fills?.length ? '; fills ' + e(g.moves.field_fills.join(', ')) : ''}` : e((g.reasons || []).map(blockerText).join('; '))}</td></tr>`;
    $('#bm-body', root).innerHTML = `<p>${ready.length ? `<strong>${plural(ready.length, 'group')}</strong> can be merged now.` : '<strong>Nothing can be merged in bulk.</strong>'} Each one is its own merge with its own Undo, and one click can undo the whole batch afterwards. No file is deleted.</p>
      ${review.length ? `<p class="small">${plural(review.length, 'group')} need${review.length === 1 ? 's' : ''} a person: they are left out of this run. Open them one by one with "Review in merge dialog".</p>` : ''}
      <div class="table-wrap"><table class="dup-dry"><thead><tr><th>Book</th><th>Status</th><th>What happens</th></tr></thead><tbody>${dry.groups.map(row).join('')}</tbody></table></div>
      <div class="dup-options"><label class="check"><input type="checkbox" id="bm-stop"> Stop at the first error</label><label class="check"><input type="checkbox" id="bm-refresh" checked> Refresh the metadata of each kept book afterwards (queued, rate limited)</label></div>
      <p class="muted small">${plural(moves.files, 'file')} move${moves.files === 1 ? 's' : ''} onto the kept records; redundant duplicate files are handled later in the separate Duplicate files step.</p>`;
    const go = $('#bm-go', root); go.disabled = !ready.length; go.textContent = ready.length ? `Merge ${plural(ready.length, 'group')}` : 'Merge';
    go.onclick = async () => {
      go.disabled = true; $('#bm-close', root).disabled = true;
      const run = { ...body, stop_on_error: $('#bm-stop', root).checked, refresh: $('#bm-refresh', root).checked };
      $('#bm-body', root).innerHTML = '<div id="bm-prog"></div><div id="bm-res"></div>';
      let b, note = '';
      try {
        b = await api('/api/v1/admin/duplicates/bulk-merge', { method: 'POST', body: run });
        const paint = x => {
          const pr = x.progress; $('#bm-prog', root).innerHTML = `<div><strong>${fmtNum(pr.done)} of ${fmtNum(pr.total)}</strong> groups done · ${fmtNum(x.counts.merged)} merged · ${fmtNum(x.counts.skipped)} skipped · ${fmtNum(x.counts.failed)} failed</div><div class="series-progress" style="margin:8px 0"><i style="width:${pr.total ? Math.round(100 * pr.done / pr.total) : 0}%"></i></div>`;
          if (x.rate_limited) { const n = document.createElement('div'); n.className = 'muted small'; n.textContent = `Pausing ${x.rate_limited.retry_after}s (write limit)…`; $('#bm-prog', root).append(n); }
        };
        ({ b, note } = await driveBatch(ctx, b, paint));
      } catch (er) { toast(errText(er), 'bad'); if (!b) { $('#bm-body', root).innerHTML = `<div class="warning-box">${e(errText(er))}</div>`; $('#bm-close', root).disabled = false; return; } }
      resultsHtml(root, b, note);
      $('#bm-close', root).disabled = false; go.classList.add('hidden');
      st.selected.clear(); await load(false); refreshStatus(); loadBatches();
    };
  }

  // The hold on a batch's refresh: a date while it is in the future, then "due" (the release happens when the refresh queue runs).
  const heldNote = b => {
    if (!b.held_refreshes) return '';
    if (b.refresh_held_until && b.refresh_held_until * 1000 > Date.now()) return `Refresh held until ${e(when(ctx, b.refresh_held_until))}; Undo stays one-click until then.`;
    return 'Refresh held; due for release the next time the refresh queue runs.';
  };

  const resultsHtml = (root, b, note = '') => {
    const label = { merged: ['good', 'Merged'], skipped: ['warn', 'Skipped'], failed: ['bad', 'Failed'], undone: ['info', 'Undone'], pending: ['', 'Waiting'], running: ['info', 'Merging'] };
    const lab = s => label[s] || ['', String(s)];
    $('#bm-prog', root).innerHTML = `<div><strong>${b.status === 'stopped' ? 'Stopped after an error.' : b.status === 'running' ? 'Not finished.' : 'Done.'}</strong> ${plural(b.counts.merged, 'group')} merged, ${b.counts.skipped} skipped, ${b.counts.failed} failed.</div>${note ? `<div class="warning-box small" style="margin-top:6px">${e(note)}</div>` : ''}`;
    $('#bm-res', root).innerHTML = `<div class="table-wrap"><table class="dup-dry"><thead><tr><th>Book</th><th>Result</th><th>Why</th></tr></thead><tbody>${b.items.map(i => `<tr><td><strong>${e(i.title || '?')}</strong></td><td><span class="badge ${lab(i.status)[0]}">${lab(i.status)[1]}</span></td><td class="small">${i.status === 'merged' ? 'Undo is available' : e((i.detail?.reasons || []).map(blockerText).join('; ') || i.reason || '')}</td></tr>`).join('')}</tbody></table></div>
      ${b.counts.merged ? `<div class="dup-options">${b.status === 'running' ? '<span class="muted small">Still merging. Undo and Finish are available once every group is done.</span>'
        : `<button class="button danger compact" id="bm-undo">Undo this whole batch</button> <button class="button secondary compact" id="bm-finish">Finish batch / refresh metadata</button>`}</div>
        <p class="muted small">Metadata is not refreshed yet, so the whole batch can still be undone in one click. "Finish batch" queues the refresh of the kept books; after that, undoing the batch is no longer one click. ${heldNote(b) ? heldNote(b) + ' ' : ''}After ${b.grace_days ?? st.status?.refresh_grace_days ?? 7} days the batch is finished the next time the metadata refresh queue runs (a manual job unless you schedule it under Jobs); "Run the queue now" is in Duplicate tools.</p>` : ''}`;
    $('#bm-undo', root)?.addEventListener('click', () => undoBatch(b.id, root));
    $('#bm-finish', root)?.addEventListener('click', () => finishBatch(b.id, root));
  };

  async function finishBatch(id, root) {
    if (!confirm('Finish this batch and refresh the metadata of its kept books? After this, undoing the batch is no longer one click.')) return;
    try {
      const f = await api(`/api/v1/admin/duplicates/batches/${enc(id)}/finalize`, { method: 'POST', body: {} });
      toast(f.queued_refreshes ? `Refreshing ${plural(f.queued_refreshes, 'kept book')} in the background` : 'Batch finished');
      if (root) root.innerHTML = '';
      refreshStatus(); loadBatches();
    } catch (er) { toast(errText(er), 'bad'); }
  }

  async function undoBatch(id, root) {
    if (!confirm('Undo every merge of this batch, newest first? It stops at the first one that changed since, and nothing is half-done.')) return;
    try {
      const u = await api(`/api/v1/admin/duplicates/batches/${enc(id)}/undo`, { method: 'POST', body: {} });
      if (u.stopped) toast(`Undid ${u.undone}; stopped: ${u.stopped.message}`, 'warn', 9000); else toast(`Undid ${plural(u.undone, 'merge')}`);
      if (root) root.innerHTML = '';
      await load(false); refreshStatus(); loadBatches();
    } catch (er) { toast(errText(er), 'bad'); }
  }

  /* ------------------------------------------------------------ batches, more actions */
  async function loadBatches() {
    const box = $('#dup-batches'); if (!box) return;
    let d; try { d = await api('/api/v1/admin/duplicates/batches?limit=10'); } catch { box.innerHTML = ''; return; }
    if (!d.items.length) { box.innerHTML = ''; return; }
    const refreshNote = b => {
      if (!b.counts.merged) return '';
      if (b.status === 'undoing') return '';
      if (b.finalized_at) return `<div class="small warn-text">Finished ${e(when(ctx, b.finalized_at))}${b.finalized_by === 'grace' ? ' automatically' : ''}: metadata refresh is queued, so Undo may stop at the first refreshed item.</div>${b.held_refreshes ? `<div class="muted small">${heldNote(b)}</div>` : ''}`;
      if (b.held_refreshes) return `<div class="muted small">${heldNote(b)}</div>`;
      return '';
    };
    const actions = b => {
      if (b.status === 'running') {
        if (b.counts.pending) return `<button class="button compact dup-batch-go" data-id="${e(b.id)}">Continue</button>`;
        return b.counts.running ? `<button class="button compact dup-batch-go" data-id="${e(b.id)}">${b.stale_claims ? 'Recover' : 'Check again'}</button><span class="muted small"> Merging…</span>` : `<button class="button compact dup-batch-go" data-id="${e(b.id)}">Recover</button>`;
      }
      if (!b.counts.merged) return '';
      const undo = b.status === 'undoing' && !b.undo_stuck ? '<span class="muted small">Undoing…</span>' : `<button class="button ghost compact dup-batch-undo" data-id="${e(b.id)}">${b.undo_stuck ? 'Resume undo' : 'Undo batch'}</button>`;
      return `<span>${b.held_refreshes ? `<button class="button secondary compact dup-batch-finish" data-id="${e(b.id)}">Finish batch / refresh metadata</button> ` : ''}${undo}</span>`;
    };
    box.innerHTML = `<div class="panel"><div class="panel-head"><h2>Recent bulk merges</h2></div><div class="panel-body dup-batches">${d.items.map(b => `<div class="dup-batch"><div><strong>${e(when(ctx, b.created_at))}</strong><div class="muted small">${plural(b.counts.merged, 'merged')} · ${b.counts.skipped} skipped · ${b.counts.failed} failed${b.counts.undone ? ' · ' + b.counts.undone + ' undone' : ''} · <span class="badge">${e(b.status.replace('_', ' '))}</span></div>${refreshNote(b)}</div>
      ${actions(b)}</div>`).join('')}</div></div>`;
    $$('.dup-batch-undo', box).forEach(x => x.onclick = () => undoBatch(x.dataset.id, null));
    $$('.dup-batch-finish', box).forEach(x => x.onclick = () => finishBatch(x.dataset.id, null));
    $$('.dup-batch-go', box).forEach(x => x.onclick = async () => {
      x.disabled = true;
      try {
        const first = await api(`/api/v1/admin/duplicates/batches/${enc(x.dataset.id)}/continue`, { method: 'POST', body: {} });
        const { b, note } = await driveBatch(ctx, first, null);
        toast(note || (b.status === 'running' ? 'Batch still running' : 'Batch continued'), note ? 'warn' : undefined, note ? 9000 : undefined); load(false); loadBatches();
      } catch (er) { toast(errText(er), 'bad'); x.disabled = false; }
    });
  }

  function moreDialog() {
    const root = ctx.modal('Duplicate tools', `<div class="dup-tools">
      <section><h3>Full rescan</h3><p class="muted small">The normal scan only re-reads books that changed. A full rescan re-reads everything (identifier-only edits are picked up here).</p><button class="button secondary compact" id="dt-full">Full rescan</button></section>
      <section><h3>Find refresh conflicts</h3><p class="muted small">Looks up which provider record up to 25 books without one would match (one provider call a second; it stops if the provider throttles). Nothing is linked or changed; each "another work already has this record" answer becomes a top-ranked candidate here.</p><button class="button secondary compact" id="dt-harvest">Retry 25 books</button></section>
      <section><h3>Refresh queue</h3><p class="muted small">${plural(st.status?.refresh_queue || 0, 'kept book')} queued for a metadata refresh; ${plural(st.status?.refresh_held || 0, 'more book')} wait${(st.status?.refresh_held || 0) === 1 ? 's' : ''} for their batch to be finished (or ${st.status?.refresh_grace_days ?? 7} days) so the batch stays one-click undoable. After the grace period the batch is finished the next time this queue runs; the job is manual unless you schedule it under Jobs.</p><button class="button secondary compact" id="dt-refresh">Run the queue now</button></section>
      <section><h3>Clean placeholder assets</h3><p class="muted small" id="dt-ph">Empty placeholder slots that sit beside an owned asset of the same kind can be removed. Checking only reads the database.</p><button class="button secondary compact" id="dt-ph-check">Check</button> <button class="button secondary compact" id="dt-ph-apply" disabled>Remove them</button></section></div>`, { wide: false, footer: '<button class="button ghost" id="dt-close">Close</button>' });
    $('#dt-close', root).onclick = () => { root.innerHTML = ''; };
    const run = (id, url, body, msg) => { $(id, root).onclick = async () => { try { await api(url, { method: 'POST', body }); toast(msg); root.innerHTML = ''; refreshStatus(); } catch (er) { toast(errText(er), 'bad'); } }; };
    run('#dt-full', '/api/v1/admin/duplicates/scan', { full: true }, 'Full rescan started');
    run('#dt-harvest', '/api/v1/admin/duplicates/harvest', { limit: 25 }, 'Looking up provider records in the background');
    run('#dt-refresh', '/api/v1/admin/duplicates/refresh-queue/run', {}, 'Refreshing merged books in the background');
    $('#dt-ph-check', root).onclick = async () => {
      const chk = $('#dt-ph-check', root); chk.disabled = true; $('#dt-ph', root).textContent = 'Checking…';
      try {
        const d = await api('/api/v1/admin/placeholder-assets/cleanup', { method: 'POST', body: {} });
        $('#dt-ph', root).textContent = d.found ? `${plural(d.found, 'empty placeholder')} sit${d.found === 1 ? 's' : ''} beside an owned asset of the same kind (${plural(d.works, 'book')}). Removing them never touches files, downloads, progress or bookmarks, and the monitoring is kept.` : 'No redundant placeholder assets.';
        const b = $('#dt-ph-apply', root); b.disabled = !d.found;
        b.onclick = async () => { try { const r = await api('/api/v1/admin/placeholder-assets/cleanup', { method: 'POST', body: { apply: true } }); toast(`${plural(r.removed, 'placeholder')} removed`); root.innerHTML = ''; } catch (er) { toast(errText(er), 'bad'); } };
      } catch (er) { $('#dt-ph', root).textContent = errText(er); }
      chk.disabled = false;
    };
  }

  paintChips(); paintBar();
  await Promise.all([refreshStatus(), load(false), loadBatches()]);
}
