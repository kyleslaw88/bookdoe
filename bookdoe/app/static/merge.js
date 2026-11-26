/* Merging duplicate books and cleaning up redundant files (admin only). ES module, loaded on demand
 * with import() by app.js, which passes its helpers in `ctx`:
 *   { api, $, $$, e, toast, modal, closeModal, fmtBytes, fmtDate, fmtNum }
 * Everything talks to /api/v1/works/merge-candidates, /merge-preview, /merge, /merges (+ /reverse),
 * /works/<id>/redundant-files (+ /remove) and /trash (CSRF is added by ctx.api).
 *
 *   seriesDuplicates(ctx, seriesId, host)   "Possible duplicates" banner on the Series page
 *   mergeDialog(ctx, workIds, opts)         side-by-side merge dialog (library select mode, series, book page)
 *   workDuplicates(ctx, workId, host)       "Find duplicates" on a book page
 *   duplicateFilesPanel(ctx, workId, host)  redundant files of one book, remove into the trash
 *   settingsMerges(ctx, el)                 Recent merges (Undo) and the trash (Restore / Delete now / retention)
 *
 * Nothing here removes a file by itself: merging only moves database records, and files are removed in the
 * separate, confirmed "Duplicate files" step, into a per-library trash folder that can be restored. */

const KIND = { ebook: 'Ebook', audiobook: 'Audiobook' };
const SIGNAL = {
  title_equal: 'same title', title_equal_after_series_suffix: 'same title once the series note is removed', same_authors: 'same author',
  same_position: 'same position', shared_identifier: 'shared identifier', similar_title: 'very similar title',
};
const CODE = { identical: 'Identical', near_identical: 'Near-identical', lower_quality: 'Lower quality' };
const TABLE_LABEL = {
  media_assets: 'asset', media_files: 'file', media_progress: 'reading/listening progress record', bookmarks: 'bookmark', favorites: 'favorite',
  work_tags: 'tag', content_grants: 'access grant', identifiers: 'identifier', series_memberships: 'series membership', ratings: 'rating',
  read_states: 'reading status', editions: 'edition', history: 'history event', work_contributors: 'author/narrator link',
};
const WIDEN_KEYS = { kid_grant_widening: 'kids_widen_grant', tag_grant_widening: 'kids_widen_tag_grant', adult_into_kids_canonical: 'kids_widen_adult', kid_visibility_widening: 'kids_widen_visibility' };
const WIDEN_WHAT = { kid_grant_widening: 'the merged book through the grant on the other record', tag_grant_widening: 'the merged book through a collection granted to them', adult_into_kids_canonical: 'the other record\'s files, now part of a Kids-shelf book', kid_visibility_widening: 'content of the merged book they cannot see today' };
const PRODUCT = document.querySelector('meta[name="alx-product"]')?.content || 'bookdoe';  // the instance's product name, stamped into the page by the server
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const enc = encodeURIComponent;
const dur = s => { s = Math.round(Number(s || 0)); if (!s) return ''; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
const kindLabel = k => KIND[k] || k;
// api() puts a JSON error body in err.payload; a structured detail ({message, ...}) would print as [object Object].
const errText = er => (er && er.payload && er.payload.detail && er.payload.detail.message) || (er && er.message) || 'Request failed';
// Pages listen for this and redraw themselves (app.js wireMergeUi).
const changed = detail => document.dispatchEvent(new CustomEvent('mrg-changed', { detail }));

/* ---------------------------------------------------------------- Series page banner */

export async function seriesDuplicates(ctx, seriesId, host) {
  const { api, e, $$, fmtNum } = ctx;
  if (!host) return;
  let r;
  try { r = await api(`/api/v1/works/merge-candidates?series_id=${enc(seriesId)}`); } catch { host.innerHTML = ''; return; }
  if (!r.groups?.length) { host.innerHTML = ''; return; }
  host.innerHTML = `<div class="panel mrg-banner" style="margin-bottom:16px"><div class="panel-head"><h2>Possible duplicates (${fmtNum(r.groups.length)})</h2><span class="muted small">These look like the same book listed twice</span></div>
    <div class="panel-body mrg-groups">${r.groups.map((g, i) => `<div class="mrg-group" data-group="${i}">
      <div class="mrg-group-titles">${g.cards.map(c => `<div><strong>${e(c.title)}</strong>${c.series.filter(s => s.id === seriesId).map(s => ` <span class="badge">${s.position ? '#' + e(s.position) : 'no position'}</span>`).join('')}${c.owned_assets ? ` <span class="badge good">${plural(c.owned_assets, 'owned asset')}</span>` : ' <span class="badge">not owned</span>'}</div>`).join('')}</div>
      <div class="muted small">${e(g.signals.map(s => SIGNAL[s] || s).join(', '))}</div>
      <div class="mrg-group-actions"><button class="button compact mrg-review" data-group="${i}">Review and merge</button></div></div>`).join('')}</div></div>`;
  $$('.mrg-review', host).forEach(b => b.onclick = () => {
    const g = r.groups[Number(b.dataset.group)];
    mergeDialog(ctx, g.work_ids);
  });
}

/* ---------------------------------------------------------------- Book page */

export async function workDuplicates(ctx, workId, host) {
  const { api, e, $, $$, toast } = ctx;
  if (!host) return;
  host.innerHTML = `<div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Duplicates</h2><button class="button ghost compact" id="mrg-find">Find duplicates</button></div><div class="panel-body" id="mrg-find-out"><span class="muted small">Look for another record of this book (same title, author or identifier) that could be merged into it.</span></div></div>`;
  $('#mrg-find', host).onclick = async ev => {
    const btn = ev.currentTarget; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Looking';
    const out = $('#mrg-find-out', host);
    try {
      const r = await api(`/api/v1/works/merge-candidates?work_id=${enc(workId)}`);
      if (!r.candidates.length) { out.innerHTML = '<div class="muted">No other record looks like this book.</div>'; return; }
      const card = Object.fromEntries(r.cards.map(c => [c.id, c]));
      out.innerHTML = `<div class="mrg-cands">${r.candidates.map(c => `<label class="mrg-cand"><input type="checkbox" class="mrg-cand-pick" value="${e(c.work_id)}" ${c.confidence === 'high' && !c.position_conflict ? 'checked' : ''}>
        <span><strong>${e(c.title)}</strong><span class="muted small"> ${e(c.authors.join(', '))}</span><br><span class="small muted">${e(c.signals.map(s => SIGNAL[s] || s).join(', '))}${c.position_conflict ? ' · different series position' : ''}${card[c.work_id]?.owned_assets ? ` · ${plural(card[c.work_id].owned_assets, 'owned asset')}` : ''}</span></span>
        <span class="badge ${c.confidence === 'high' ? 'good' : ''}">${e(c.confidence)}</span></label>`).join('')}</div>
        <div style="margin-top:12px"><button class="button compact" id="mrg-cand-go">Review and merge selected</button></div>`;
      $('#mrg-cand-go', out).onclick = () => {
        const ids = $$('.mrg-cand-pick', out).filter(c => c.checked).map(c => c.value);
        if (!ids.length) return toast('Tick at least one record to merge with this book', 'warn');
        mergeDialog(ctx, [workId, ...ids]);
      };
    } catch (er) { out.innerHTML = `<div class="muted">${e(errText(er))}</div>`; }
    finally { btn.disabled = false; btn.textContent = 'Find duplicates'; }
  };
}

/* ---------------------------------------------------------------- Merge dialog */

function cardHtml(ctx, c, st) {
  const { e, fmtDate, fmtBytes } = ctx;
  const isKeep = c.id === st.canonical;
  const assets = c.assets.length ? `<table class="mrg-assets"><tbody>${c.assets.map(a => `<tr><td>${e(kindLabel(a.kind))}${a.derived ? ' <span class="badge info">converted</span>' : ''}<div class="muted small">${e(String(a.format || '?').toUpperCase())}</div></td>
      <td><span class="badge ${a.owned ? 'good' : ''}">${a.owned ? 'owned' : 'missing'}</span>${a.monitored ? ' <span class="badge info">monitored</span>' : ''}</td>
      <td class="small">${plural(a.files, 'file')}${a.size_bytes ? ' · ' + e(fmtBytes(a.size_bytes)) : ''}${dur(a.file_duration_seconds) ? ' · ' + dur(a.file_duration_seconds) : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="muted small">No assets</div>';
  const progress = c.progress.length ? c.progress.map(p => `<div class="small">${e(p.username || 'user')}: ${p.percent != null ? Math.round(p.percent) + '%' : '—'} ${e(kindLabel(p.kind))}</div>`).join('') : '<div class="muted small">No progress yet</div>';
  return `<label class="mrg-card ${isKeep ? 'keep' : ''}" data-id="${e(c.id)}">
    <div class="mrg-card-head"><input type="radio" name="mrg-canon" value="${e(c.id)}" ${isKeep ? 'checked' : ''} aria-label="Keep &quot;${e(c.title)}&quot; as the main record">
      <span class="badge ${isKeep ? 'good' : ''}">${isKeep ? 'Keep this record' : 'Merged into the kept one'}</span></div>
    <div class="mrg-card-body"><img class="mrg-cover" loading="lazy" alt="" src="${e(c.cover_url)}" data-img-fallback="hide">
      <div><div class="mrg-title">${e(c.title)}</div><div class="muted small">${e(c.authors.join(', ') || 'No author')}</div>
      <div class="small">${c.series.length ? c.series.map(s => e(s.name) + (s.position ? ' #' + e(s.position) : ' (no position)')).join(' · ') : '<span class="muted">No series</span>'}</div></div></div>
    <div class="mrg-section"><div class="mrg-label">Assets and files</div>${assets}</div>
    <div class="mrg-section"><div class="mrg-label">Progress</div>${progress}</div>
    <div class="mrg-section"><div class="mrg-label">Tags</div><div>${c.kids_shelf ? '<span class="badge good">Kids shelf</span> ' : ''}${c.tags.filter(t => t.name !== 'kids').map(t => `<span class="tag-chip">${e(t.label)}</span>`).join(' ') || (c.kids_shelf ? '' : '<span class="muted small">none</span>')}</div></div>
    <div class="mrg-section small"><div class="mrg-label">Details</div>${c.monitored ? 'Monitored' : 'Not monitored'} · added ${e(fmtDate(c.added_at))}${c.language ? ' · ' + e(c.language) : ''}
      ${c.identifiers.length ? `<div class="muted">${c.identifiers.slice(0, 4).map(i => e(i.namespace + ': ' + i.value)).join('<br>')}</div>` : ''}</div></label>`;
}

function summaryLines(ctx, st) {
  const { fmtBytes } = ctx;
  const lines = [];
  const groups = {};
  for (const c of st.cards) for (const a of c.assets) if (!a.derived) (groups[`${a.kind}|${a.format}`] ||= []).push({ a, own: c.id === st.canonical });
  for (const [key, list] of Object.entries(groups)) {
    const [kind, fmt] = key.split('|');
    const owned = list.filter(x => x.a.owned), files = list.reduce((n, x) => n + x.a.files, 0), bytes = list.reduce((n, x) => n + (x.a.size_bytes || 0), 0);
    if (list.length >= 2) lines.push(`${plural(owned.length, 'owned ' + kind.toLowerCase())} (${String(fmt).toUpperCase()}) will be folded into 1 asset with ${plural(files, 'file')}${bytes ? ' (' + fmtBytes(bytes) + ')' : ''}.`);
    else if (!list[0].own) lines.push(`The ${kindLabel(kind).toLowerCase()} (${String(fmt).toUpperCase()}) with ${plural(files, 'file')} moves onto the kept record.`);
  }
  const moved = (st.plan?.plan?.tables || []).filter(t => t.move > 0 && !['media_assets', 'media_files', 'editions', 'history'].includes(t.table) && TABLE_LABEL[t.table]);
  if (moved.length) lines.push('Moves over: ' + moved.map(t => plural(t.move, TABLE_LABEL[t.table])).join(', ') + '.');
  if (st.plan?.plan?.tag_union?.length) lines.push('Tags added to the kept record: ' + st.plan.plan.tag_union.map(t => t.name === 'kids' ? 'Kids shelf' : t.name).join(', ') + '.');
  lines.push('No files are deleted or moved by a merge. You can undo it afterwards.');
  return lines;
}

export async function mergeDialog(ctx, workIds, opts = {}) {
  const { api, modal, toast, e, $, $$, fmtNum } = ctx;
  let info;
  try { info = await api(`/api/v1/works/merge-candidates?work_ids=${enc(workIds.join(','))}`); }
  catch (er) { toast(errText(er), 'bad'); return; }
  if ((info.cards || []).length < 2) { toast('Pick at least two books to merge', 'warn'); return; }
  const st = { cards: info.cards, canonical: info.suggestion.canonical_work_id, suggestion: info.suggestion, warnings: info.warnings || [], plan: null, perUser: 'canonical',
    kids: false, thirdOk: {}, widenOk: {}, confirm: false, title: null, busy: false, result: null, reversed: null, blockers: [], undoConflicts: null, error: '' };
  const root = modal('Merge duplicate books', '<div id="mrg-body"><div class="empty"><span class="spinner"></span></div></div>', { wide: true,
    footer: '<span id="mrg-foot-note" class="muted small" style="margin-right:auto"></span><button class="button ghost" id="mrg-close">Close</button><button class="button" id="mrg-go" disabled>Merge</button>' });
  $('#mrg-close', root).onclick = () => { root.innerHTML = ''; };

  const cleanTitle = () => {
    const keep = st.cards.find(c => c.id === st.canonical);
    if (keep.clean_title) return keep.title;
    const alt = st.cards.filter(c => c.clean_title).sort((a, b) => a.title.length - b.title.length)[0];
    return alt ? alt.title : keep.title;
  };
  st.title = cleanTitle();

  const sources = () => {
    // The chosen title's record goes last: execute lets the last differing source win a 'take the source value' choice.
    const rest = st.cards.filter(c => c.id !== st.canonical);
    return [...rest.filter(c => c.title !== st.title), ...rest.filter(c => c.title === st.title)].map(c => c.id);
  };
  const resolutions = () => {
    const r = {};
    if (st.kids) { r.kids_tag_union = true; r.kids_tag_union_works = Object.keys(st.thirdOk).filter(id => st.thirdOk[id]); }
    for (const [code, key] of Object.entries(WIDEN_KEYS)) if (st.widenOk[code]) r[key] = true;      // one consent per blocker, each naming its kids
    if (st.confirm) r.confirm_dissimilar = true;
    if (st.perUser !== 'canonical') r.per_user = st.perUser;
    const keep = st.cards.find(c => c.id === st.canonical);
    if (st.title !== keep.title) r.fields = { title: 'source' };
    return r;
  };
  const loadPlan = async () => {
    st.plan = null; st.error = ''; paint();
    try { st.plan = await api(`/api/v1/works/merge-preview?canonical=${enc(st.canonical)}&sources=${enc(sources().join(','))}`); }
    catch (er) { st.error = errText(er); }
    paint();
  };

  function paint() {
    const body = $('#mrg-body', root); if (!body) return;
    const go = $('#mrg-go', root);
    if (st.result) return paintResult(body, go);
    const plan = st.plan;
    const kidsBlock = plan?.blockers?.find(b => b.code === 'kids_tag_mismatch');
    const widen = (plan?.blockers || []).filter(b => ['kid_grant_widening', 'tag_grant_widening', 'adult_into_kids_canonical', 'kid_visibility_widening'].includes(b.code));
    const hard = (plan?.blockers || []).filter(b => b.code !== 'kids_tag_mismatch' && !widen.includes(b));
    const titles = [...new Set(st.cards.map(c => c.title))];
    const conflicts = plan?.conflicts || [];
    const progressN = conflicts.filter(c => ['progress_conflict', 'rating_conflict', 'read_state_conflict'].includes(c.type)).length;
    const fieldConf = conflicts.filter(c => c.type === 'field_conflict' && c.field !== 'title');
    const posConf = conflicts.filter(c => c.type === 'series_position_conflict');
    body.innerHTML = `
      ${st.warnings.length ? `<div class="warning-box" id="mrg-warn"><strong>These may not be the same book.</strong><ul>${st.warnings.map(w => `<li>${e(w.message)}</li>`).join('')}</ul>
        <label><input type="checkbox" id="mrg-confirm" ${st.confirm ? 'checked' : ''}> I know; merge them anyway</label></div>` : ''}
      <p class="small" id="mrg-suggest"><strong>Kept record:</strong> choose which record survives. ${e(st.canonical === st.suggestion.canonical_work_id ? st.suggestion.reason : 'You chose this one; the others fold into it.')}</p>
      <div class="mrg-cards">${st.cards.map(c => cardHtml(ctx, c, st)).join('')}</div>
      ${titles.length > 1 ? `<div class="field" style="margin-top:14px"><label for="mrg-title">Title after the merge</label><select class="select" id="mrg-title">${titles.map(t => `<option ${t === st.title ? 'selected' : ''}>${e(t)}</option>`).join('')}</select></div>` : ''}
      <div class="mrg-plan" id="mrg-plan">${!plan && !st.error ? '<span class="spinner"></span> Working out what would change' : ''}
        ${st.error ? `<div class="warning-box">${e(st.error)}</div>` : ''}
        ${plan ? `<h3>What will happen</h3><ul class="mrg-summary">${summaryLines(ctx, st).map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}
        ${kidsBlock ? `<div class="warning-box" id="mrg-kids"><strong>Kids shelf</strong><div>One of these records is on the Kids shelf and the kept one is not. Merging must not hide it from children, so the kept record would join the Kids shelf.</div>
          <div class="small">These records would end up on the Kids shelf: ${(kidsBlock.gaining || []).map(g => `<strong>${e(g.title || g.id)}</strong>`).join(', ')}.</div>
          <label><input type="checkbox" id="mrg-kids-ok" ${st.kids ? 'checked' : ''}> Put the merged book on the Kids shelf (keep the Kids tag)</label>
          ${(kidsBlock.adult_sources || []).map(id => `<label class="mrg-kw-row"><input type="checkbox" class="mrg-third" data-id="${e(id)}" ${st.thirdOk[id] ? 'checked' : ''}> <span>Also put <strong>${e((kidsBlock.gaining.find(g => g.id === id) || {}).title || id)}</strong> on the Kids shelf (it is not there now)</span></label>`).join('')}</div>` : ''}
        ${widen.length ? `<div class="warning-box" id="mrg-kidswiden"><strong>Kids access would change</strong><div class="small">Nothing is allowed unless you tick it. Each box covers only the accounts named.</div>
          ${widen.map(b => `<label class="mrg-kw-row"><input type="checkbox" class="mrg-kw" data-code="${e(b.code)}" ${st.widenOk[b.code] ? 'checked' : ''}> <span><strong>${e((b.kids || []).join(', ') || 'Kids accounts')}</strong> would see ${e(WIDEN_WHAT[b.code] || 'more')}. ${e(b.message)}</span></label>`).join('')}</div>` : ''}
        ${hard.length ? `<div class="warning-box" id="mrg-blockers"><strong>This cannot be merged yet</strong><ul>${hard.map(b => `<li>${e(b.message || b.code)}</li>`).join('')}</ul></div>` : ''}
        ${st.blockers.length ? `<div class="warning-box" id="mrg-refused"><strong>The server refused</strong><ul>${st.blockers.map(b => `<li>${e(b.message || b.code)}</li>`).join('')}</ul></div>` : ''}
        ${progressN ? `<div class="field"><label for="mrg-peruser">${plural(progressN, 'reader difference')}: progress, rating or reading status exists on both records</label>
          <select class="select" id="mrg-peruser"><option value="canonical" ${st.perUser === 'canonical' ? 'selected' : ''}>Keep the kept record's</option><option value="newest" ${st.perUser === 'newest' ? 'selected' : ''}>Keep whichever was updated most recently</option></select>
          <div class="muted small">The other value is kept in the undo ledger.</div></div>` : ''}
        ${fieldConf.length ? `<div class="muted small">Other differing fields (${e(fieldConf.map(c => c.field).join(', '))}) keep the kept record's value; the other is saved in the book's history.</div>` : ''}
        ${posConf.length ? `<div class="muted small">Series position: ${e(posConf.map(c => (c.canonical_position || '').toString().trim() ? `${c.series} keeps #${c.canonical_position} (the other says ${c.source_position || 'none'})` : `${c.series} takes #${c.source_position} over from the other record`).join('; '))}.</div>` : ''}
      </div>`;
    $$('input[name=mrg-canon]', body).forEach(r => r.onchange = () => { st.canonical = r.value; st.title = cleanTitle(); st.kids = false; st.thirdOk = {}; st.widenOk = {}; loadPlan(); });
    $('#mrg-title', body)?.addEventListener('change', ev => { st.title = ev.target.value; paint(); });
    $('#mrg-confirm', body)?.addEventListener('change', ev => { st.confirm = ev.target.checked; paint(); });
    $$('.mrg-kw', body).forEach(c => c.onchange = () => { st.widenOk[c.dataset.code] = c.checked; paint(); });
    $('#mrg-kids-ok', body)?.addEventListener('change', ev => { st.kids = ev.target.checked; paint(); });
    $$('.mrg-third', body).forEach(c => c.onchange = () => { st.thirdOk[c.dataset.id] = c.checked; paint(); });
    $('#mrg-peruser', body)?.addEventListener('change', ev => { st.perUser = ev.target.value; paint(); });
    const needsConfirm = st.warnings.length && !st.confirm;
    go.disabled = !plan || st.busy || hard.length > 0 || (kidsBlock && (!st.kids || (kidsBlock.adult_sources || []).some(id => !st.thirdOk[id]))) || widen.some(b => !st.widenOk[b.code]) || needsConfirm;
    go.textContent = st.busy ? 'Merging…' : `Merge ${st.cards.length} into 1`;
    go.onclick = doMerge;
    $('#mrg-foot-note', root).textContent = '';
  }

  async function doMerge() {
    st.busy = true; st.blockers = []; paint();
    try {
      st.result = await api('/api/v1/works/merge', { method: 'POST', body: { canonical: st.canonical, sources: sources(), resolutions: resolutions() } });
      toast('Merged. You can undo this.');
      changed(st.result); if (opts.onDone) opts.onDone(st.result);
    } catch (er) {
      st.blockers = er.payload?.detail?.blockers || [{ message: errText(er) }];
      if (st.blockers.some(b => b.code === 'dissimilar_works')) st.warnings = st.warnings.length ? st.warnings : [{ message: 'The server says these do not look like the same book.' }];
    }
    st.busy = false; paint();
  }

  function paintResult(body, go) {
    const keep = st.cards.find(c => c.id === st.canonical);
    go.style.display = 'none';
    if (st.reversed) {
      body.innerHTML = `<div class="empty"><strong>Merge undone</strong><div>${plural(st.reversed.restored_rows, 'record')} restored; the books are separate again.</div></div>`;
      return;
    }
    const unc = st.undoConflicts;
    body.innerHTML = `<div class="mrg-result"><h3>Merged into &ldquo;${e(st.title)}&rdquo;</h3>
      <ul class="mrg-summary">${summaryLines(ctx, st).map(l => `<li>${e(l)}</li>`).join('')}</ul>
      ${st.undoRefused ? `<div class="warning-box" id="mrg-undo-refused"><strong>Cannot undo yet</strong><div>${e(st.undoRefused.message)}</div>${(st.undoRefused.files || []).slice(0, 6).map(f => `<div class="small mrg-path">${e(f.path)}</div>`).join('')}
        ${st.undoRefused.code === 'kid_widening' ? '<button class="button compact danger" id="mrg-undo-kids">Undo anyway and let them see it</button>' : ''}
        ${st.undoRefused.code === 'files_in_trash' ? '<button class="button compact" id="mrg-open-trash">Restore these files first (Settings &gt; System)</button>' : ''}</div>` : ''}
      ${unc ? `<div class="warning-box" id="mrg-undo-conflicts"><strong>Things changed since the merge</strong><div>Undoing now would overwrite:</div><ul>${unc.map(c => `<li>${e(c.table)}: ${e(c.problem)}</li>`).join('')}</ul>
        <button class="button compact danger" id="mrg-undo-force">Undo anyway</button></div>` : ''}
      <div class="mrg-result-actions"><button class="button" id="mrg-open">Open the book</button><button class="button ghost" id="mrg-undo">Undo this merge</button></div>
      <p class="muted small">If the merge left the same file twice, the book page has a &ldquo;Duplicate files&rdquo; panel to review. Nothing is deleted until you confirm it there.</p></div>`;
    $('#mrg-open', body).onclick = () => { root.innerHTML = ''; location.hash = `library/${keep.id}`; };
    const undo = async (force, allow = false) => {
      try {
        st.reversed = await api(`/api/v1/works/merges/${enc(st.result.merge_id)}/reverse`, { method: 'POST', body: { force, allow_kid_widening: allow } });
        toast('Merge undone'); changed(st.reversed); if (opts.onDone) opts.onDone(st.reversed);
      } catch (er) {
        const d = er.payload?.detail;
        if (er.status === 409 && d?.code) st.undoRefused = d;                 // files in the trash / a later merge: even "Undo anyway" cannot override these
        else if (er.status === 409 && d?.conflicts) st.undoConflicts = d.conflicts; else toast(errText(er), 'bad');
      }
      paint();
    };
    $('#mrg-open-trash', body)?.addEventListener('click', () => { root.innerHTML = ''; location.hash = 'settings'; });
    $('#mrg-undo', body).onclick = () => undo(false);
    $('#mrg-undo-force', body)?.addEventListener('click', () => undo(true));
    $('#mrg-undo-kids', body)?.addEventListener('click', () => { st.undoRefused = null; undo(false, true); });
  }

  loadPlan();
}

/* ---------------------------------------------------------------- Duplicate files */

export async function duplicateFilesPanel(ctx, workId, host) {
  const { api, e, $, $$, fmtBytes, toast, modal } = ctx;
  if (!host) return;
  let r;
  host.innerHTML = '<div class="panel" id="mrg-verifying" style="margin-top:16px"><div class="panel-body muted small"><span class="spinner"></span> Verifying files on disk (large audiobooks can take a while)…</div></div>';
  try { r = await api(`/api/v1/works/${enc(workId)}/redundant-files`); } catch { host.innerHTML = ''; return; }
  const ignoredNote = r.ignored?.length ? `<div class="muted small">${plural(r.ignored.length, 'file')} left out because they cannot be judged safely (symlinks, hard links, missing, empty or outside the library).</div>` : '';
  if (!r.groups.length) {
    host.innerHTML = (r.notes.length || r.ignored?.length) ? `<div class="panel" style="margin-top:16px"><div class="panel-head"><h2>Duplicate files</h2></div><div class="panel-body">${r.notes.map(n => `<div class="muted small">${e(n.message)}</div>`).join('')}${ignoredNote}</div></div>` : '';
    return;
  }
  // Nothing is pre-ticked: removing a file is always the administrator's own choice.
  const rows = r.groups.map((g, gi) => `<div class="mrg-dgroup" data-g="${gi}">
      <div>${g.checksum_identical ? '<span class="badge good">Identical (same checksum)</span>' : `<span class="badge warn">${g.code === 'lower_quality' ? 'Lower quality copy' : 'Similar only'} - not byte-identical</span>`}
        <strong>${e(kindLabel(g.kind))} ${e(String(g.format || '').toUpperCase())}</strong> <span class="muted small">${e(g.reason)}</span></div>
      <div class="small ${g.checksum_identical ? 'muted' : 'mrg-warntext'}">${e(g.recommendation)}</div>
      ${g.keeps.map(k => `<div class="mrg-file keep"><span class="badge good">Keep</span> <span class="mrg-path">${e(k.path)}</span> <span class="small">${e(fmtBytes(k.size_bytes))}</span></div>`).join('')}
      ${g.remove.map(f => `<label class="mrg-file remove"><input type="checkbox" class="mrg-rm" data-g="${gi}" ${f.unsupported ? 'disabled data-unsupported="1"' : ''} value="${e(f.id)}" data-bytes="${f.size_bytes}" data-rec="${f.file_will_move ? 0 : 1}" data-dir="${e(f.trash_dir || '')}" data-sim="${g.checksum_identical ? 0 : 1}" data-all="${g.requires_all ? 1 : 0}">
        <span class="badge ${f.unsupported ? '' : 'warn'}">${f.unsupported ? 'Cannot remove here' : 'Remove?'}</span> <span class="mrg-path">${e(f.path)}</span> <span class="small">${e(fmtBytes(f.size_bytes))}</span>
        <span class="muted small mrg-note">${e(f.note || '')}</span></label>`).join('')}</div>`).join('');
  host.innerHTML = `<div class="panel" id="mrg-dfiles" style="margin-top:16px"><div class="panel-head"><h2>Duplicate files</h2><span class="muted small">${plural(r.totals.files, 'candidate')} · ${e(fmtBytes(r.totals.bytes_freed))} could be freed · nothing is selected until you tick it</span></div>
    <div class="panel-body">${r.groups.some(g => g.remove.some(f => f.unsupported)) ? '<div class="warning-box" id="mrg-nolinks"><strong>Automatic removal is not available for some of these files.</strong> This storage does not support the safe move ' + e(PRODUCT) + ' needs (hard links), so they cannot be removed automatically. Remove them by hand.</div>' : ''}${rows}${r.notes.map(n => `<div class="muted small">${e(n.message)}</div>`).join('')}${ignoredNote}
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center"><button class="button compact danger" id="mrg-rm-go" disabled>Remove selected…</button><span class="muted small" id="mrg-rm-sum"></span></div></div></div>`;
  const picked = () => $$('.mrg-rm', host).filter(c => c.checked);
  const sum = () => {
    const p = picked(), moved = p.filter(c => c.dataset.rec === '0').reduce((n, c) => n + Number(c.dataset.bytes), 0), rec = p.filter(c => c.dataset.rec === '1').reduce((n, c) => n + Number(c.dataset.bytes), 0);
    $('#mrg-rm-sum', host).textContent = p.length ? `${plural(p.length, 'file')} selected · ${fmtBytes(moved)} to trash${rec ? ' · ' + fmtBytes(rec) + ' stay on disk (record only)' : ''}` : 'Nothing selected: keeping everything.';
    $('#mrg-rm-go', host).disabled = !p.length;
  };
  $$('.mrg-rm', host).forEach(c => c.onchange = () => {
    if (c.dataset.all === '1') $$(`.mrg-rm[data-g="${c.dataset.g}"]`, host).forEach(o => { o.checked = c.checked; });   // a set of parts is removed whole or not at all
    sum();
  });
  sum();
  $('#mrg-rm-go', host).onclick = () => {
    const p = picked(), ids = p.map(c => c.value);
    const moved = p.filter(c => c.dataset.rec === '0'), rec = p.filter(c => c.dataset.rec === '1'), sim = p.filter(c => c.dataset.sim === '1');
    const movedBytes = moved.reduce((n, c) => n + Number(c.dataset.bytes), 0);
    const dirs = [...new Set(moved.map(c => c.dataset.dir).filter(Boolean))];
    const bulk = p.length >= 10;
    const m = modal('Remove duplicate files?', `<div class="warning-box"><strong>${plural(p.length, 'file')} will be removed from this book.</strong></div>
      <ul class="mrg-summary" id="mrg-rm-summary">${moved.length ? `<li><strong>${plural(moved.length, 'file')}, ${e(fmtBytes(movedBytes))}</strong> move to <code>${dirs.map(d => e(d)).join('</code>, <code>') || '.bookdoe-trash'}</code> and are deleted for good after the retention period. You can restore them until then.</li>` : ''}
      ${rec.length ? `<li>${plural(rec.length, 'file')} sit in an adopted or observe-only library, which ${e(PRODUCT)} never changes: only the record is removed and the file stays where it is. A rescan may add it back.</li>` : ''}
      <li>At least one copy of every format, and of every part, is always kept.</li></ul>
      ${sim.length ? `<div class="warning-box"><strong>These files are only similar, not byte-identical.</strong> They look like another copy of the same recording, but ${e(PRODUCT)} cannot prove it.
        <label><input type="checkbox" id="mrg-rm-sim"> I have checked and want them removed anyway</label></div>` : ''}
      ${bulk ? `<div class="field"><label for="mrg-rm-count">Type ${p.length} to confirm removing ${p.length} files</label><input class="input" id="mrg-rm-count" inputmode="numeric" autocomplete="off"></div>` : ''}`,
      { footer: '<button class="button ghost" id="mrg-rm-cancel">Cancel</button><button class="button danger" id="mrg-rm-confirm" disabled>Remove</button>' });
    const ready = () => { $('#mrg-rm-confirm', m).disabled = (sim.length && !$('#mrg-rm-sim', m).checked) || (bulk && $('#mrg-rm-count', m).value.trim() !== String(p.length)); };
    $('#mrg-rm-sim', m)?.addEventListener('change', ready); $('#mrg-rm-count', m)?.addEventListener('input', ready); ready();
    $('#mrg-rm-cancel', m).onclick = () => { m.innerHTML = ''; };
    $('#mrg-rm-confirm', m).onclick = async ev => {
      ev.currentTarget.disabled = true;
      try {
        const out = await api(`/api/v1/works/${enc(workId)}/redundant-files/remove`, { method: 'POST', body: { file_ids: ids, confirm_similar: !!sim.length, confirm_count: bulk ? p.length : null } });
        m.innerHTML = '';
        toast(`Removed ${plural(out.removed.length, 'file')}: ${fmtBytes(out.bytes_moved)} moved to trash${out.bytes_record_only ? ', ' + fmtBytes(out.bytes_record_only) + ' left in place' : ''}`);
        await duplicateFilesPanel(ctx, workId, host);
      } catch (er) { toast(errText(er), 'bad'); ev.currentTarget.disabled = false; }
    };
  };
}

/* ---------------------------------------------------------------- Settings > System: recent merges and trash */

/* Undo that would let a kid see a book they cannot see now: itemized, default off, retried with allow_kid_widening. */
function kidUndoDialog(ctx, mergeId, detail, done) {
  const { api, modal, toast, e, $ } = ctx;
  const m = modal('Undo would change Kids access', `<div class="warning-box" id="mrg-kidundo"><strong>${e((detail.kids || []).join(', ') || 'Kids accounts')}</strong> would be able to see a book that they cannot see now.<div class="small">${e(detail.message)}</div>
      <label class="mrg-kw-row"><input type="checkbox" id="mrg-kidundo-ok"> <span>Undo anyway and let ${e((detail.kids || []).join(', ') || 'them')} see it</span></label></div>`,
    { footer: '<button class="button ghost" id="mrg-kidundo-cancel">Cancel</button><button class="button danger" id="mrg-kidundo-go" disabled>Undo the merge</button>' });
  $('#mrg-kidundo-ok', m).onchange = ev => { $('#mrg-kidundo-go', m).disabled = !ev.target.checked; };
  $('#mrg-kidundo-cancel', m).onclick = () => { m.innerHTML = ''; };
  $('#mrg-kidundo-go', m).onclick = async () => {
    try { await api(`/api/v1/works/merges/${enc(mergeId)}/reverse`, { method: 'POST', body: { allow_kid_widening: true } }); toast('Merge undone'); changed({}); m.innerHTML = ''; }
    catch (er) { toast(errText(er), 'bad'); }
    done();
  };
}

export async function settingsMerges(ctx, el) {
  const { api, e, $, $$, toast, fmtBytes, fmtDate } = ctx;
  const paint = async () => {
    const [mg, tr] = await Promise.all([api('/api/v1/works/merges?limit=15'), api('/api/v1/trash')]);
    const merges = mg.items;
    el.innerHTML = `<div class="setting-section" style="margin-top:24px"><h3>Recent merges</h3><p>Merged duplicate books can be split apart again.</p></div>
      <div class="panel table-wrap"><table id="mrg-merges"><thead><tr><th>When</th><th>Merged</th><th>Status</th><th></th></tr></thead><tbody>${merges.length ? merges.map(m => `<tr data-id="${e(m.id)}">
        <td class="small">${e(fmtDate(m.created_at))}</td><td><strong>${e(m.canonical_title || '(removed)')}</strong><div class="muted small">${e(m.sources.map(s => s.title || s.id).join('; '))}</div></td>
        <td><span class="badge ${m.status === 'executed' ? 'good' : m.status === 'reversed' ? '' : 'bad'}">${e(m.status === 'executed' ? 'merged' : m.status)}</span></td>
        <td>${m.status !== 'reversed' ? `<button class="button ghost compact mrg-undo-row" data-id="${e(m.id)}">Undo</button>` : ''}</td></tr>`).join('') : '<tr><td colspan="4" class="muted">No merges yet</td></tr>'}</tbody></table></div>
      <div class="setting-section" style="margin-top:24px"><h3>Trash</h3><p>Removed duplicate files wait here ${tr.retention_days} days, then are deleted for good. ${e(fmtBytes(tr.totals.bytes_in_trash))} in trash${tr.totals.bytes_record_only ? ', ' + e(fmtBytes(tr.totals.bytes_record_only)) + ' record-only (files kept in place)' : ''}.</p></div>
      <div class="field-inline" style="margin-bottom:10px"><label for="mrg-retention">Keep for (days)</label><input class="input compact" id="mrg-retention" type="number" min="1" max="3650" value="${tr.retention_days}" style="width:90px"> <button class="button ghost compact" id="mrg-retention-save">Save</button>
        <button class="button ghost compact danger" id="mrg-purge-expired" ${tr.totals.expired ? '' : 'disabled'}>Delete expired now</button></div>
      <div class="panel table-wrap"><table id="mrg-trash"><thead><tr><th>File</th><th>Size</th><th>Expires</th><th></th></tr></thead><tbody>${tr.items.length ? tr.items.map(t => `<tr data-id="${e(t.id)}">
        <td><div class="mrg-path">${e(t.relative_path)}</div><div class="muted small">${e(t.work_title || '')}${t.record_only ? ' · record only, the file stays in its library' : ''}</div></td>
        <td class="small">${e(fmtBytes(t.size_bytes))}</td><td class="small">${t.expired ? 'expired' : t.days_left + ' days'}</td>
        <td><button class="button ghost compact mrg-restore" data-id="${e(t.id)}">Restore</button> <button class="button ghost compact danger mrg-purge" data-id="${e(t.id)}">Delete now</button></td></tr>`).join('') : '<tr><td colspan="4" class="muted">Trash is empty</td></tr>'}</tbody></table></div>`;
    $$('.mrg-undo-row', el).forEach(b => b.onclick = async () => {
      if (!confirm('Undo this merge? The books become separate records again.')) return;
      try { await api(`/api/v1/works/merges/${enc(b.dataset.id)}/reverse`, { method: 'POST', body: {} }); toast('Merge undone'); }
      catch (er) {
        if (er.status === 409 && er.payload?.detail?.code === 'kid_widening') kidUndoDialog(ctx, b.dataset.id, er.payload.detail, paint);
        else if (er.status === 409 && er.payload?.detail?.code) toast(errText(er), 'warn');   // files in the trash / a later merge: nothing to force
        else if (er.status === 409 && er.payload?.detail?.conflicts) {
          const list = er.payload.detail.conflicts.slice(0, 8).map(c => `${c.table}: ${c.problem}`).join('\n');
          if (confirm(`Things changed since the merge:\n${list}\n\nUndo anyway? The earlier values are restored over them.`)) {
            try { await api(`/api/v1/works/merges/${enc(b.dataset.id)}/reverse`, { method: 'POST', body: { force: true } }); toast('Merge undone'); } catch (er2) { toast(errText(er2), 'bad'); }
          }
        } else toast(errText(er), 'bad');
      }
      paint();
    });
    $$('.mrg-restore', el).forEach(b => b.onclick = async () => {
      try { await api(`/api/v1/trash/${enc(b.dataset.id)}/restore`, { method: 'POST', body: {} }); toast('File restored'); } catch (er) { toast(errText(er), 'bad'); }
      paint();
    });
    $$('.mrg-purge', el).forEach(b => b.onclick = async () => {
      if (!confirm('Delete this file for good? It cannot be restored afterwards.')) return;
      try { await api('/api/v1/trash/purge', { method: 'POST', body: { ids: [b.dataset.id] } }); toast('Deleted'); } catch (er) { toast(errText(er), 'bad'); }
      paint();
    });
    $('#mrg-purge-expired', el).onclick = async () => {
      try { const o = await api('/api/v1/trash/purge', { method: 'POST', body: { expired: true } }); toast(`Deleted ${o.purged} expired file(s), ${fmtBytes(o.bytes_freed)} freed`); } catch (er) { toast(errText(er), 'bad'); }
      paint();
    };
    $('#mrg-retention-save', el).onclick = async () => {
      try { await api('/api/v1/trash/settings', { method: 'PUT', body: { retention_days: Number($('#mrg-retention', el).value) } }); toast('Retention saved'); } catch (er) { toast(errText(er), 'bad'); }
      paint();
    };
  };
  await paint();
}
