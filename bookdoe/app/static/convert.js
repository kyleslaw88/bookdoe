/* Format conversion UI for Management (issue #265): the per-asset Convert button, the bulk
 * action's confirmation dialog, Settings > Conversion and the Conversions list. ES module,
 * loaded on demand with import() by app.js, which passes its helpers in `ctx`:
 *   { api, $, $$, e, toast, modal, closeModal, fmtBytes, fmtDate, fmtNum }
 * Everything here talks to /api/v1/assets/<id>/convert, /api/v1/conversions* and
 * /api/v1/settings/conversion (admin only, CSRF added by ctx.api). Originals are never
 * touched by a conversion; every dialog says so. */

const STATUS_BADGE = { queued: '', running: 'info', done: 'good', failed: 'bad', cancelled: '', skipped: 'warn', superseded: '' };
const TARGET_LABEL = { m4b: 'M4B', epub: 'EPUB' };

export async function convertAsset(ctx, assetId, target, onDone) {
  const { api, toast } = ctx;
  try {
    const r = await api(`/api/v1/assets/${encodeURIComponent(assetId)}/convert`, { method: 'POST', body: {} });
    toast(r.created ? `Queued conversion to ${TARGET_LABEL[target] || target}. The original is kept.` : 'Already queued');
    if (onDone) onDone();
  } catch (er) { toast(er.message, 'bad'); }
}

export async function bulkConvertDialog(ctx, workIds, onDone) {
  const { api, modal, closeModal, toast, e, fmtBytes, fmtNum } = ctx;
  let plan;
  try { plan = await api('/api/v1/conversions/bulk', { method: 'POST', body: { work_ids: workIds, dry_run: true } }); }
  catch (er) { toast(er.message, 'bad'); return; }
  const cat = plan.categories || {};
  const blocked = Object.entries(plan.blocked || {}).map(([code, v]) => `<li data-blocked="${e(code)}">${fmtNum(v.count)} blocked: ${e(v.reason || code)}</li>`).join('');
  const queued = cat.already_queued ? `<li>${fmtNum(cat.already_queued)} already queued</li>` : '';
  const dests = (plan.destinations || []).map(d => `<li class="conv-dest" data-fits="${d.fits ? 1 : 0}"><strong>${e(d.name)}</strong>: ${fmtNum(d.count)} file(s), about ${fmtBytes(d.estimated_bytes)} of ${d.free_bytes != null ? fmtBytes(d.free_bytes) : 'unknown'} free${d.cross_root ? ' (managed library; originals stay where they are)' : ' (next to the originals)'}${d.fits ? '' : ' <span style="color:var(--bad)">not enough room</span>'}</li>`).join('');
  const body = `
    <div class="warning-box"><strong>Originals are kept.</strong> Nothing is replaced or deleted${plan.into_managed_library ? `. ${fmtNum(plan.into_managed_library)} book(s) live in read-only library roots: their converted files are saved in your managed library and the originals are not touched` : '; each copy is added next to the original'}.</div>
    <p style="margin-top:12px"><strong id="bulk-convert-count">${fmtNum(plan.eligible)}</strong> conversion(s) would be queued: ${fmtNum(plan.audio)} audiobook(s) to M4B, ${fmtNum(plan.ebook)} ebook(s) to EPUB, from ${fmtNum(plan.works)} selected book(s).</p>
    <ul class="small" id="bulk-convert-categories">
      <li data-cat="will_convert">${fmtNum(cat.will_convert || 0)} will convert</li>
      <li data-cat="already_preferred">${fmtNum(cat.already_preferred || 0)} already have the preferred format</li>
      ${queued}${blocked}
    </ul>
    <p class="muted">Estimated disk use: <strong>${fmtBytes(plan.estimated_bytes)}</strong>; a ${fmtBytes(plan.margin_bytes)} margin is always kept free.</p>
    ${dests ? `<ul class="small" id="bulk-convert-destinations">${dests}</ul>` : ''}
    ${plan.fits === false ? `<div class="warning-box" id="bulk-convert-nospace" style="color:var(--bad)">Not enough free space for this selection, so it will not start. ${e((plan.shortfall || []).join('; '))} Select fewer books or free space.</div>` : ''}`;
  const m = modal('Convert to preferred formats', body, {
    footer: `<button class="button ghost modal-cancel">Cancel</button><button class="button" id="bulk-convert-confirm" ${plan.eligible && plan.fits !== false ? '' : 'disabled'}>Convert ${fmtNum(plan.eligible)} (originals kept)</button>`,
  });
  ctx.$('.modal-cancel', m).onclick = closeModal;
  ctx.$('#bulk-convert-confirm', m).onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const r = await api('/api/v1/conversions/bulk', { method: 'POST', body: { work_ids: workIds, dry_run: false, confirm: true } });
      toast(`Queued ${fmtNum(r.queued)} conversion(s). Follow them in Settings > Conversion.`);
      closeModal();
      if (onDone) onDone();
    } catch (er) { toast(er.message, 'bad'); ev.target.disabled = false; }
  };
}

const SETTING_FIELDS = [
  ['enabled_audio', 'Convert audiobooks to M4B', 'bool'],
  ['enabled_ebook', 'Convert MOBI/AZW ebooks to EPUB', 'bool'],
  ['audio_bitrate_mono_kbps', 'Audio bitrate, mono (kbps)', 'int'],
  ['audio_bitrate_stereo_kbps', 'Audio bitrate, stereo (kbps)', 'int'],
  ['audio_concurrency', 'Audiobook conversions at a time', 'int'],
  ['ebook_concurrency', 'Ebook conversions at a time', 'int'],
  ['auto_on_import', 'Convert automatically on import', 'bool'],
  ['only_if_no_preferred', 'Automatic and bulk conversion: only if no preferred-format copy exists yet', 'bool'],
  ['max_attempts', 'Attempts before a conversion is parked as failed', 'int'],
  ['retry_backoff_minutes', 'First retry delay (minutes, doubles each attempt)', 'int'],
  ['disk_margin_mb', 'Keep this much disk free in the destination (MB); a conversion or bulk run that would eat into it is refused', 'int'],
  ['audiobook_output_root', 'Audiobooks in read-only roots: save converted M4B files in', 'root:audiobook'],
  ['ebook_output_root', 'Ebooks in read-only roots: save converted EPUB files in', 'root:ebook'],
  ['queue_limit', 'Largest waiting queue', 'int'],
];

export async function settingsConversion(ctx, el) {
  const { api, e, toast, fmtBytes } = ctx;
  const cfg = await api('/api/v1/settings/conversion');
  el.innerHTML = `<div class="setting-section"><h3>Conversion</h3>
    <p>Make a copy of a book in the preferred format: audiobooks become one chaptered <strong>M4B</strong>, MOBI/AZW/AZW3 ebooks become <strong>EPUB</strong>. The original is always kept; the copy is listed as "Converted from ..." and never counts as a second owned copy. A book in a managed library root gets its copy next to the original. A book in a read-only (adopted or observed) root is never touched: its copy is saved in the managed library as <span class="mono">Author/Title/Title.m4b</span> (or .epub), in the destination chosen below. DRM-protected files are skipped.</p>
    ${cfg.ffmpeg_available ? '' : '<div class="warning-box"><strong>ffmpeg is not installed on this server.</strong> Audiobook conversion will fail until it is.</div>'}
    <div class="panel"><div class="panel-body" id="conv-settings">${SETTING_FIELDS.map(([k, label, type]) => {
      if (type === 'bool') return `<label class="field-inline"><input type="checkbox" data-conv="${k}" ${cfg[k] ? 'checked' : ''}> ${e(label)}</label>`;
      if (type.startsWith('root:')) {
        const info = (cfg.output_roots || {})[type.slice(5)] || { roots: [], effective: null };
        const auto = info.effective && !cfg[k] ? ` (now: ${info.effective.name})` : '';
        const opts = [`<option value="">Automatic${e(auto)}</option>`, ...info.roots.map(r => `<option value="${e(r.id)}" ${cfg[k] === r.id ? 'selected' : ''}>${e(r.name)}</option>`)].join('');
        return `<div class="field-inline"><label for="conv-${k}">${e(label)}</label><select class="input compact" id="conv-${k}" data-conv="${k}">${opts}</select>${info.problem ? `<div class="small" style="color:var(--bad)">${e(info.problem)}</div>` : info.effective && info.effective.free_bytes != null ? `<div class="small muted">${fmtBytes(info.effective.free_bytes)} free</div>` : ''}</div>`;
      }
      return `<div class="field-inline"><label for="conv-${k}">${e(label)}</label><input class="input compact" id="conv-${k}" type="number" data-conv="${k}" value="${Number(cfg[k])}"></div>`;
    }).join('')}
      <div style="margin-top:12px"><button class="button" id="conv-save">Save</button></div></div></div></div>
    <div class="setting-section"><h3>Conversions</h3><div id="conv-list"></div></div>`;
  ctx.$('#conv-save', el).onclick = async () => {
    const body = {};
    ctx.$$('[data-conv]', el).forEach(i => { body[i.dataset.conv] = i.type === 'checkbox' ? i.checked : i.tagName === 'SELECT' ? i.value : Number(i.value); });
    try { await api('/api/v1/settings/conversion', { method: 'PUT', body }); toast('Conversion settings saved'); }
    catch (er) { toast(er.message, 'bad'); }
  };
  await conversionsList(ctx, ctx.$('#conv-list', el));
}

function statusCell(ctx, c) {
  const { e, fmtDate } = ctx;
  const pct = c.status === 'running' ? `<div class="progress"><i style="width:${Math.round(c.percent || 0)}%"></i></div><span class="small muted">${Math.round(c.percent || 0)}%</span>` : '';
  const retry = c.status === 'queued' && c.next_attempt_at ? `<div class="small muted">retry after ${e(fmtDate(c.next_attempt_at))}</div>` : '';
  return `<span class="badge ${STATUS_BADGE[c.status] ?? ''}">${e(c.status)}</span>${pct}${retry}`;
}

export async function conversionsList(ctx, host) {
  const { api, e, toast, fmtBytes, fmtDate } = ctx;
  let timer = null;
  const paint = async () => {
    if (!host.isConnected) { clearTimeout(timer); return; }
    let d;
    try { d = await api('/api/v1/conversions?limit=100'); } catch (er) { host.innerHTML = `<div class="warning-box">${e(er.message)}</div>`; return; }
    const rows = d.items.map(c => `<tr data-id="${e(c.id)}">
      <td><strong>${e(c.work_title || '')}</strong><div class="small muted">${e((c.source_format || '').toUpperCase())} to ${e(TARGET_LABEL[c.target_format] || c.target_format)} · ${e(c.trigger_source)}</div></td>
      <td>${statusCell(ctx, c)}</td>
      <td class="small">${c.status === 'done' ? `${e(c.output_name || '')}<div class="muted">${fmtBytes((c.result || {}).size_bytes)}</div>` : e(c.error || '')}</td>
      <td class="small">${e(fmtDate(c.finished_at || c.created_at))}</td>
      <td>${['queued', 'running'].includes(c.status) ? `<button class="button compact secondary conv-cancel" data-id="${e(c.id)}">Cancel</button>` : ''}${['failed', 'cancelled', 'skipped'].includes(c.status) ? `<button class="button compact conv-retry" data-id="${e(c.id)}">Retry</button>` : ''}</td></tr>`).join('');
    host.innerHTML = `<div class="small muted" id="conv-counts">${Object.entries(d.counts).map(([k, v]) => `${e(k)}: ${v}`).join(' · ') || 'Nothing converted yet'}</div>
      <div class="panel table-wrap"><table><thead><tr><th>Book</th><th>Status</th><th>Result</th><th>When</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="muted">No conversions yet</td></tr>'}</tbody></table></div>`;
    ctx.$$('.conv-cancel', host).forEach(b => b.onclick = async () => { try { await api(`/api/v1/conversions/${encodeURIComponent(b.dataset.id)}/cancel`, { method: 'POST', body: {} }); toast('Cancel requested'); paint(); } catch (er) { toast(er.message, 'bad'); } });
    ctx.$$('.conv-retry', host).forEach(b => b.onclick = async () => { try { await api(`/api/v1/conversions/${encodeURIComponent(b.dataset.id)}/retry`, { method: 'POST', body: {} }); toast('Queued again'); paint(); } catch (er) { toast(er.message, 'bad'); } });
    // Poll while something is queued or running so percentages move; stop when the list leaves the page.
    if (d.items.some(c => ['queued', 'running'].includes(c.status))) timer = setTimeout(paint, 3000);
  };
  await paint();
}
