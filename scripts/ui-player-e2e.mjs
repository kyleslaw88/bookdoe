// Real-browser checks of the Player against a seeded local-auth instance
// (scripts/ui-player-seed.py): covers really render (naturalWidth > 0 AND the pixel
// at the cover's centre belongs to the <img>, not the title fallback) for admin, adult
// and kid with the service worker controlling the page; the tag filter narrows the
// library and survives navigation and reload; the EPUB reader's A+/A- visibly change
// the rendered text and persist across a reload; the audiobook controls are measured in
// both shells and both themes (contrast, targets, focus, time label); and a kid's
// listening client is taken through an admin removing the book from the kids shelf and
// putting it back (request volume stays tiny, the place is kept and replayed, playback
// resumes, the worker re-stores covers and says "Download again"). Env: UI_BASE, UI_SEED
// (the JSON line the seed script printed).
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const SEED = JSON.parse(process.env.UI_SEED || '{}');
const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(msg); };
const b = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });

async function session(username) {
  const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: 390, height: 760 }, serviceWorkers: 'allow' });
  const p = await ctx.newPage();
  p.on('pageerror', e => fails.push(`[${username}] page error: ${e}`));
  p.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/auth/me') && !p.__expectError) fails.push(`[${username}] HTTP ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
  await p.goto(`${BASE}/app`, { waitUntil: 'load' });
  await p.evaluate(async ({ u, pw }) => { const r = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: pw }) }); if (!r.ok) throw new Error('login failed ' + r.status); }, { u: username, pw: SEED.password });
  await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
  // A same-document hash change does not re-run sign-in; reload so the new session is picked up
  // (this used to happen by itself when the first service worker took over and the page reloaded).
  // Let the first-ever install finish BEFORE reloading. A reload that starts while the worker is still
  // installing is uncontrolled, and clients.claim() at activation then runs in the gap before the new page
  // exists, so that page is never controlled (reproduced on a loaded 4-vCPU CI runner: worker 'activated',
  // page.controller null for the whole 15 s wait). On an idle machine the install takes ~80 ms and wins the race.
  await p.evaluate(() => navigator.serviceWorker.ready.then(() => true));
  await p.reload({ waitUntil: 'load' });
  await settle(p, MARK.home);
  // Wait for the worker to control the page, then reload so every request goes through it.
  await p.waitForFunction(() => navigator.serviceWorker && navigator.serviceWorker.controller, null, { timeout: 15000 }).catch(() => fails.push(`[${username}] service worker never took control`));
  await p.reload({ waitUntil: 'load' }); await settle(p, MARK.home);
  return { ctx, p };
}
// Management's book page. A hash change between two books is a same-document navigation: goto() returns
// at once and the previous book's page (with its own #tags-row) is still on screen until route() swaps
// it, so a bare wait for #tags-row passes on stale DOM and the click then lands on a node that is
// replaced under it. Wait for the page of the work we asked for (its Studio link names the id);
// renderWork() wires every handler in the same synchronous step that draws #tags-row.
async function openManage(p, url) {
  const id = decodeURIComponent(new URL(url).hash.split('/')[1]);
  await p.goto(url, { waitUntil: 'load' });
  await p.waitForFunction(w => document.querySelector(`#open-studio[data-route="library/${w}/studio"]`) && document.querySelector('#tags-row'), id, { timeout: 20000 });
}
// The book page re-renders after a tag change: renderWork() first swaps the page for a loading
// skeleton (so #tags-row is gone, and a bare "the chip is not there" wait passes on the skeleton), then
// fetches and draws it again. Navigating away before that draw lets the late render land on the next
// page, so wait for the redrawn tag row without the chip.
const tagGone = (p, name) => p.waitForFunction(n => document.querySelector('#tags-row') && !document.querySelector(`#tags-row .tag-chip[data-tag-name="${n}"]`), name, { timeout: 20000 });
// Settings draws its first tab (Library roots) after the tab strip: clicking a tab before that
// finishes starts a second render while the first is still writing, and the first one's late
// handler wiring then throws on the replaced DOM (a page error) or the second tab never settles.
async function openSettingsTab(p, tab, marker) {
  await p.goto(`${BASE}/manage#settings`, { waitUntil: 'load' });
  await p.waitForSelector(`[data-stab="${tab}"]`, { timeout: 20000 });
  // (The page is not reloaded between visits, so the tab last used may already be the one asked for.)
  await p.waitForSelector('#settings-body > *', { timeout: 20000 });
  if (!(await p.$(`[data-stab="${tab}"].active`))) await p.click(`[data-stab="${tab}"]`);
  await p.waitForSelector(marker, { timeout: 20000 });
}
// Hash routes render asynchronously and the previous page stays on screen until the new
// one is ready, so wait for a marker of the page we asked for, not just for quiet.
const MARK = { home: '.pl-section', library: '#lib-tag', book: '.pl-detail' };
async function settle(p, mark) {
  await p.waitForLoadState('networkidle');
  if (mark) await p.waitForSelector(mark, { timeout: 20000 }).catch(() => {});
  await p.waitForFunction(() => !document.querySelector('#pl-view .pl-skeleton'), null, { timeout: 15000 }).catch(() => {});
}

async function coverReport(p) {
  await p.evaluate(() => Promise.all([...document.querySelectorAll('.pl-cover img')].map(i => i.complete ? null : new Promise(r => { i.onload = i.onerror = r; }))));
  return p.evaluate(() => [...document.querySelectorAll('.pl-cover img')].map(img => {
    const r = img.getBoundingClientRect(); const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { src: img.getAttribute('src'), natural: img.naturalWidth, onTop: el === img, inView: r.top >= 0 && r.bottom <= innerHeight };
  }));
}

for (const [who, expectTitles] of [['ada', 5], ['jane', 5], ['owen', 2]]) {
  const { ctx, p } = await session(who);
  const sw = await p.evaluate(() => !!(navigator.serviceWorker && navigator.serviceWorker.controller));
  ok(sw, `[${who}] service worker is not controlling the page`);
  const shellName = await p.evaluate(async () => (await caches.keys()).filter(n => n.startsWith('bookdoe-shell-')));
  ok(shellName.length === 1 && shellName[0] === 'bookdoe-shell-' + await p.evaluate(() => window.__ALX_BUILD__.sha), `[${who}] shell cache name must follow the build (got ${shellName})`);
  for (const route of ['home', 'library']) {
    await p.goto(`${BASE}/app#${route}`, { waitUntil: 'load' }); await settle(p, MARK[route]);
    const covers = await coverReport(p);
    ok(covers.length >= 1, `[${who}/${route}] no cover images rendered`);
    for (const c of covers) {
      ok(c.natural > 0, `[${who}/${route}] cover ${c.src} did not load (naturalWidth 0)`);
      if (c.inView) ok(c.onTop, `[${who}/${route}] cover ${c.src} loaded but is hidden behind the title fallback`);
    }
    if (route === 'library') ok(covers.length === expectTitles, `[${who}/library] expected ${expectTitles} covers, saw ${covers.length}`);
  }
  // Book page cover.
  await p.goto(`${BASE}/app#library/${SEED.works['Readable Kids Book']}`, { waitUntil: 'load' }); await settle(p, MARK.book);
  const detail = await coverReport(p);
  ok(detail.length === 1 && detail[0].natural > 0 && detail[0].onTop, `[${who}/book] detail cover not visible: ${JSON.stringify(detail)}`);
  // Covers were served through the worker (cache-first content-cover): second visit offline still shows them.
  await ctx.setOffline(true);
  // Home is cached (content-json) and so are covers (content-cover); the paged library
  // list is network-only by design, so the offline check uses Home.
  await p.goto(`${BASE}/app#home`, { waitUntil: 'load' }).catch(() => {}); await settle(p, MARK.home).catch(() => {}); await p.waitForTimeout(800);
  const offlineCovers = await coverReport(p).catch(() => []);
  ok(offlineCovers.some(c => c.natural > 0 && c.onTop), `[${who}] covers do not come from the worker cache offline: ${JSON.stringify(offlineCovers)}`);
  await ctx.setOffline(false);

  // Tag filter.
  try {
  await p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await settle(p, MARK.library);
  const tagOptions = await p.$$eval('#lib-tag option', os => os.map(o => o.textContent.trim()));
  if (who === 'owen') ok(!tagOptions.some(t => t.startsWith('classics')), `[owen] kid sees a tag that is only on hidden books: ${tagOptions}`);
  else {
    ok(tagOptions.some(t => t.startsWith('classics')), `[${who}] tag filter lacks 'classics': ${tagOptions}`);
    await p.selectOption('#lib-tag', { label: tagOptions.find(t => t.startsWith('classics')) }); await p.waitForTimeout(400); await settle(p, MARK.library);
    let titles = await p.$$eval('.pl-book-title', els => els.map(e => e.textContent.trim()));
    ok(titles.length === 1 && titles[0] === 'Grown-up Classic', `[${who}] tag filter did not narrow: ${titles}`);
    // Combine with a format chip, navigate away and back, reload: the filter must hold.
    await p.click('[data-kind="ebook"]'); await p.waitForTimeout(400); await settle(p, MARK.library);
    await p.goto(`${BASE}/app#home`, { waitUntil: 'load' }); await settle(p, MARK.home);
    await p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await settle(p, MARK.library);
    titles = await p.$$eval('.pl-book-title', els => els.map(e => e.textContent.trim()));
    ok(titles.length === 1 && (await p.$eval('#lib-tag', s => s.selectedOptions[0].textContent)).startsWith('classics'), `[${who}] tag filter lost after navigation: ${titles}`);
    await p.reload({ waitUntil: 'load' }); await settle(p, MARK.library);
    titles = await p.$$eval('.pl-book-title', els => els.map(e => e.textContent.trim()));
    ok(titles.length === 1, `[${who}] tag filter lost after reload: ${titles}`);
    await p.selectOption('#lib-tag', ''); await p.click('[data-kind=""]'); await settle(p);
  }
  } catch (er) { fails.push(`[${who}] tag filter check crashed: ${String(er.message || er).split('\n')[0]}`); }

  // Management Inbox (admin): filter to kids suggestions, select all, bulk approve with
  // the confirmation dialog, and check the metadata task was left alone.
  if (who === 'ada') try {
    const dialogs = [];
    p.on('dialog', d => { dialogs.push(d.message()); d.accept().catch(() => {}); });
    await p.goto(`${BASE}/manage#inbox`, { waitUntil: 'load' }); await p.waitForSelector('#review-kind', { timeout: 20000 });
    const kinds = await p.$$eval('#review-kind option', os => os.map(o => o.textContent.trim()));
    ok(kinds.some(k => k.startsWith('Kids shelf suggestions (3)')) && kinds.some(k => k.startsWith('Unresolved books (1)')), `[inbox] kind counts wrong: ${kinds}`);
    await p.selectOption('#review-kind', 'kids_tag'); await p.waitForFunction(() => document.querySelectorAll('.review-pick').length === 3, null, { timeout: 15000 });
    await p.click('#review-select-all'); await p.waitForFunction(() => document.querySelector('#review-selected')?.textContent.startsWith('3'), null, { timeout: 5000 });
    // Untick one: select-all with an exclusion.
    await p.click('.review-pick'); await p.waitForFunction(() => document.querySelector('#review-selected')?.textContent.startsWith('2'), null, { timeout: 5000 });
    await p.click('[data-review-bulk="approve"]');
    await p.waitForFunction(() => document.querySelectorAll('.review-pick').length === 1, null, { timeout: 20000 });
    ok(dialogs.length === 1 && /Approve 2 kids shelf suggestions/.test(dialogs[0]), `[inbox] no confirmation naming kind and count: ${dialogs}`);
    const tagged = await p.evaluate(async ids => { const out = []; for (const id of ids) { const w = await (await fetch(`/api/v1/library/${id}`)).json(); out.push((w.tags || []).some(t => t.name === 'kids')); } return out; }, SEED.works.proposals);
    ok(tagged.filter(Boolean).length === 2, `[inbox] expected 2 of 3 proposals tagged after bulk approve: ${tagged}`);
    const meta = await p.evaluate(async () => (await (await fetch('/api/v1/review?kind=metadata_match')).json()).total);
    ok(meta === 1, `[inbox] metadata task was touched by a kids bulk action (${meta} left)`);
    // Filter persists across a reload.
    await p.reload({ waitUntil: 'load' }); await p.waitForSelector('#review-kind', { timeout: 20000 });
    ok((await p.$eval('#review-kind', s => s.value)) === 'kids_tag', '[inbox] kind filter was not remembered after reload');
    p.removeAllListeners('dialog');
  } catch (er) { fails.push(`[inbox] check crashed: ${String(er.message || er).split('\n')[0]}`); }


  // Re-check of existing auto-approvals (admin): the Settings > Tags button shows a dry-run count first,
  // queuing puts the failures in the Inbox group "Auto-tagged without audience evidence" without removing
  // any tag, a book page says why it fails the gate, and bulk keep / remove record manual decisions.
  if (who === 'ada') try {
    const dialogs = [];
    const onDialog = d => { dialogs.push(d.message()); d.accept().catch(() => {}); };
    p.on('dialog', onDialog);
    const R = SEED.works.recheck;
    const state = async id => p.evaluate(async id => { const w = await (await fetch(`/api/v1/library/${id}`)).json(); return { on: (w.tags || []).some(t => t.name === 'kids'), via: w.kids_decision && w.kids_decision.via, state: w.kids_decision && w.kids_decision.state, gate: w.kids_evidence && w.kids_evidence.gate }; }, id);
    await p.goto(`${BASE}/manage#library/${R['Recheck Adult 1']}`, { waitUntil: 'load' }); await p.waitForSelector('#kids-why', { timeout: 20000 });
    const why = (await p.textContent('#kids-why')).replace(/\s+/g, ' ');
    ok(/Fails the audience-evidence gate/.test(why) && /No strong audience signal/.test(why), `[recheck] book page does not say why it fails the gate: ${why}`);
    const passWhy = await (async () => { await p.goto(`${BASE}/manage#library/${R['Recheck Pass']}`, { waitUntil: 'load' }); await p.waitForSelector('#kids-why', { timeout: 20000 }); return (await p.textContent('#kids-why')).replace(/\s+/g, ' '); })();
    ok(/Passes the audience-evidence gate/.test(passWhy) && /Scholastic/.test(passWhy), `[recheck] passing book does not show its signal: ${passWhy}`);
    await openSettingsTab(p, 'tags', '#kids-recheck');
    ok(await p.$eval('#kids-auto-strong', c => c.checked), '[recheck] "Require strong audience evidence" is not on by default');
    ok((await p.$eval('#kids-auto-th', i => i.value)) === '0.95', '[recheck] default auto threshold is not 0.95');
    const dry = await p.evaluate(async () => (await fetch('/api/v1/tags/kids-auto/recheck')).json());
    ok(dry.pass === 1 && dry.fail >= 3 && dry.dry_run === true, `[recheck] dry-run counts: ${JSON.stringify(dry)}`);
    await p.click('#kids-recheck'); await p.waitForSelector('#kr-summary', { timeout: 15000 });
    const sum = (await p.textContent('#kr-summary')).replace(/\s+/g, ' ');
    ok(sum.includes(`${dry.pass} of ${dry.checked}`) && sum.includes(`${dry.fail} do not`), `[recheck] dry-run dialog does not show the counts: ${sum}`);
    ok(/Nothing is removed/.test(await p.textContent('#modal-root')), '[recheck] dialog does not promise nothing is removed');
    const beforeQueue = await p.evaluate(async () => (await (await fetch('/api/v1/review?kind=kids_recheck')).json()).total);
    ok(beforeQueue === 0, `[recheck] dry-run queued tasks (${beforeQueue})`);
    await p.click('#kr-go'); await p.waitForSelector('#review-kind', { timeout: 20000 });
    await p.selectOption('#review-kind', 'kids_recheck'); await p.waitForFunction(n => document.querySelectorAll('.review-pick').length === n, dry.fail, { timeout: 15000 });
    const kinds = await p.$$eval('#review-kind option', os => os.map(o => o.textContent.trim()));
    ok(kinds.some(k => k.startsWith(`Auto-tagged without audience evidence (${dry.fail})`)), `[recheck] Inbox group label/count: ${kinds}`);
    ok(/No strong audience signal/.test(await p.textContent('#inbox-review-list')), '[recheck] Inbox cards do not show the reasons');
    for (const t of Object.keys(R)) ok((await state(R[t])).on, `[recheck] ${t} lost its tag just by being queued`);
    // Keep two (manual_tag), remove one (manual_untag), through the bulk bar.
    const taskIds = await p.evaluate(async () => Object.fromEntries((await (await fetch('/api/v1/review?kind=kids_recheck&limit=100')).json()).items.map(t => [t.title, t.id])));
    const pick = async title => p.click(`.review-pick[data-id="${taskIds[title]}"]`);
    await pick('Recheck Adult 1'); await pick('Recheck Adult 2');
    await p.waitForFunction(() => document.querySelector('#review-selected')?.textContent.startsWith('2'), null, { timeout: 5000 });
    await p.click('[data-review-bulk="keep"]');
    await p.waitForFunction(n => document.querySelectorAll('.review-pick').length === n, dry.fail - 2, { timeout: 20000 });
    ok(dialogs.some(d => /Keep on shelf 2 auto-tagged without audience evidence/.test(d)), `[recheck] keep confirmation: ${dialogs}`);
    await pick('Recheck Adult 3');
    await p.waitForFunction(() => document.querySelector('#review-selected')?.textContent.startsWith('1'), null, { timeout: 5000 });
    await p.click('[data-review-bulk="remove"]');
    await p.waitForFunction(n => document.querySelectorAll('.review-pick').length === n, dry.fail - 3, { timeout: 20000 });
    const kept1 = await state(R['Recheck Adult 1']), kept2 = await state(R['Recheck Adult 2']), gone = await state(R['Recheck Adult 3']), passed = await state(R['Recheck Pass']);
    ok(kept1.on && kept1.via === 'manual_tag' && kept2.on && kept2.via === 'manual_tag', `[recheck] bulk keep not recorded as manual_tag: ${JSON.stringify([kept1, kept2])}`);
    ok(!gone.on && gone.via === 'manual_untag' && gone.state === 'off', `[recheck] bulk remove not recorded as manual_untag: ${JSON.stringify(gone)}`);
    ok(passed.on && passed.via === 'auto_approved', `[recheck] the passing book was touched: ${JSON.stringify(passed)}`);
    p.off('dialog', onDialog);
  } catch (er) { fails.push(`[recheck] check crashed: ${String(er.message || er).split("\n").slice(0, 3).join(" | ")}`); }

  // Kids-shelf cleanup (admin): a wrongly auto-approved book can be taken off the shelf from
  // its Management page (labelled remove control on the tag chip, confirmation naming the
  // effect), added back, and several at once from the Library tag view. A kid's session
  // loses the book at once, including the copy its service worker stored.
  if (who === 'ada') try {
    const C = SEED.works.cleanup, star = C['Backyard Starship'];
    const dialogs = [];
    p.on('dialog', d => { dialogs.push(d.message()); d.accept().catch(() => {}); });
    const kid = await session('owen');
    const kidTitles = async () => { await kid.p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await kid.p.reload({ waitUntil: 'load' }); await settle(kid.p, MARK.library); await kid.p.waitForTimeout(400); return kid.p.$$eval('.pl-book-title', els => els.map(e => e.textContent.trim())); };
    ok((await kidTitles()).includes('Backyard Starship'), '[kids-cleanup] kid does not see the kids-shelf book to begin with');
    await kid.p.goto(`${BASE}/app#library/${star}`, { waitUntil: 'load' }); await settle(kid.p, MARK.book);
    // Kid book page: no tag editing, no Management link.
    ok(!(await kid.p.$('.tag-remove, .pl-tag-remove, [aria-label^="Remove tag"], a[href^="/manage"], #add-tag')), '[kids-cleanup] kid book page offers tag editing or a Management link');

    const mgr = `${BASE}/manage#library/${star}`;
    await openManage(p, mgr);
    const chips = await p.$$eval('#tags-row .tag-chip', cs => cs.map(c => ({ text: c.textContent.replace(/\s+/g, ' ').trim(), btn: !!c.querySelector('button[aria-label^="Remove tag"]') })));
    ok(chips.some(c => /^Kids shelf/.test(c.text) && c.btn), `[kids-cleanup] Management book page has no labelled remove control on the Kids shelf tag: ${JSON.stringify(chips)}`);
    ok(!chips.some(c => /^kids\b/i.test(c.text) && !/^Kids shelf/.test(c.text)), `[kids-cleanup] Management shows the tag as plain "kids": ${JSON.stringify(chips)}`);
    dialogs.length = 0;
    await p.click('#tags-row .tag-chip[data-tag-name="kids"] button[aria-label^="Remove tag"]');
    await tagGone(p, 'kids');
    ok(dialogs.length === 1 && /kid accounts? (will )?lose access/i.test(dialogs[0]) && /classifier will not/i.test(dialogs[0]), `[kids-cleanup] removal confirmation does not state the effect: ${JSON.stringify(dialogs)}`);
    const dec = await p.evaluate(async id => (await (await fetch(`/api/v1/library/${id}`)).json()).kids_decision, star);
    ok(dec && dec.state === 'off' && dec.via === 'manual_untag' && dec.by === 'ada' && dec.at, `[kids-cleanup] manual_untag not recorded with who/when: ${JSON.stringify(dec)}`);
    ok(/excluded by hand/i.test(await p.textContent('.detail-meta')), '[kids-cleanup] page does not say the book was excluded by hand');
    // The kid loses it immediately: list, detail, and the worker's stored copy.
    ok(!(await kidTitles()).includes('Backyard Starship'), '[kids-cleanup] kid still lists the removed book');
    kid.p.__expectError = true;
    const kidDetail = await kid.p.evaluate(async id => (await fetch(`/api/v1/library/${id}`)).status, star);
    kid.p.__expectError = false;
    ok(kidDetail === 404, `[kids-cleanup] kid can still open the removed book (${kidDetail})`);
    const kidCached = await kid.p.evaluate(async id => { for (const n of await caches.keys()) { if (!n.includes('content')) continue; const c = await caches.open(n); if (await c.match(`/api/v1/library/${id}`)) return true; } return false; }, star);
    ok(!kidCached, '[kids-cleanup] kid service worker still holds the removed book');
    // Add it back, then the page shows it as manual.
    await p.click('#add-kids-shelf'); await p.waitForSelector('#tags-row .tag-chip[data-tag-name="kids"]', { timeout: 15000 });
    ok(/Kids shelf · manual/.test((await p.textContent('.detail-meta')).replace(/\s+/g, ' ')), '[kids-cleanup] re-added book is not shown as manually on the shelf');
    ok((await kidTitles()).includes('Backyard Starship'), '[kids-cleanup] kid does not see the re-added book');
    // Take it off for good (the actual fix the owner asked for).
    await openManage(p, mgr);
    await p.click('#tags-row .tag-chip[data-tag-name="kids"] button[aria-label^="Remove tag"]');
    await tagGone(p, 'kids');
    // An ordinary tag has a remove control too, with no kids-specific warning.
    // (classics sits on Grown-up Classic.)
    dialogs.length = 0;
    await openManage(p, `${BASE}/manage#library/${SEED.works['Grown-up Classic']}`);
    await p.click('#tags-row .tag-chip[data-tag-name="classics"] button[aria-label^="Remove tag"]');
    await tagGone(p, 'classics');
    ok(!dialogs.some(d => /kid/i.test(d)), `[kids-cleanup] ordinary tag removal showed a kids warning: ${dialogs}`);
    await p.evaluate(async ([id, tag]) => { const csrf = (await (await fetch('/api/v1/auth/me')).json()).csrf; await fetch(`/api/v1/library/${id}/tags`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ tag_id: tag }) }); },
      [SEED.works['Grown-up Classic'], await p.evaluate(async () => (await (await fetch('/api/v1/tags')).json()).items.find(t => t.name === 'classics').id)]);

    // Bulk: Library, filter to the Kids shelf tag, select, drop two of the three.
    dialogs.length = 0;
    await p.goto(`${BASE}/manage#library`, { waitUntil: 'load' }); await p.waitForSelector('#tag-filter', { timeout: 20000 });
    const mOpts = await p.$$eval('#tag-filter option', os => os.map(o => o.textContent.trim()));
    ok(mOpts.some(o => /^Kids shelf/.test(o)) && !mOpts.some(o => /^kids$/i.test(o)), `[kids-cleanup] Management tag filter label: ${mOpts}`);
    await p.selectOption('#tag-filter', { label: mOpts.find(o => /^Kids shelf/.test(o)) });
    await p.waitForFunction(() => document.querySelectorAll('.book-card').length >= 4, null, { timeout: 15000 });
    await p.click('#select-mode');
    await p.click('#select-all-shown'); 
    await p.waitForFunction(() => /selected/.test(document.querySelector('#bulk-bar')?.textContent || ''), null, { timeout: 5000 });
    const picked = Object.entries(C).filter(([t]) => t.startsWith('Bulk Kid'));
    // Un-select Bulk Kid 3 so it must stay on the shelf.
    await p.click(`.book-card[data-id="${C['Bulk Kid 3']}"]`);
    // Also unselect everything that is not one of our fixtures, so the rest of the shelf stays.
    for (const id of await p.$$eval('.book-card.selected', cs => cs.map(c => c.dataset.id))) if (![C['Backyard Starship'], C['Bulk Kid 1'], C['Bulk Kid 2']].includes(id)) await p.click(`.book-card[data-id="${id}"]`);
    await p.click('#bulk-remove-tag');
    await p.waitForSelector('#tag-remove-pick', { timeout: 10000 });
    ok((await p.$eval('#tag-remove-pick', s => s.selectedOptions[0].textContent)).startsWith('Kids shelf'), '[kids-cleanup] bulk Remove tag does not preselect the filtered tag');
    ok(/kid accounts lose access/i.test(await p.textContent('#modal-root')) && /classifier will not/i.test(await p.textContent('#modal-root')), '[kids-cleanup] bulk dialog does not state the effect for the kids tag');
    await p.click('#tag-remove-go');
    await p.waitForFunction(() => !document.querySelector('#tag-remove-go'), null, { timeout: 20000 });
    const after = await p.evaluate(async ids => { const out = {}; for (const [k, id] of Object.entries(ids)) { const w = await (await fetch(`/api/v1/library/${id}`)).json(); out[k] = { on: (w.tags || []).some(t => t.name === 'kids'), via: w.kids_decision && w.kids_decision.via }; } return out; }, C);
    ok(!after['Backyard Starship'].on && after['Backyard Starship'].via === 'manual_untag' && !after['Bulk Kid 1'].on && !after['Bulk Kid 2'].on && after['Bulk Kid 1'].via === 'manual_untag', `[kids-cleanup] bulk removal not applied/recorded: ${JSON.stringify(after)}`);
    ok(after['Bulk Kid 3'].on, '[kids-cleanup] bulk removal touched an unselected book');
    const kt = await kidTitles();
    ok(!kt.includes('Backyard Starship') && !kt.includes('Bulk Kid 1') && !kt.includes('Bulk Kid 2') && kt.includes('Bulk Kid 3'), `[kids-cleanup] kid library after bulk removal: ${kt}`);
    // Bulk ADD: select the three removed books in the Library, "Add to kids shelf", confirm,
    // read the result summary; the kid now sees them; the generic picker lists Kids shelf.
    dialogs.length = 0;
    await p.goto(`${BASE}/manage#library`, { waitUntil: 'load' }); await p.waitForSelector('#select-mode', { timeout: 20000 });
    await p.selectOption('#tag-filter', ''); /* the hash change keeps the Kids shelf filter from the bulk removal above */
    await p.waitForFunction(() => document.querySelectorAll('.book-card').length >= 4, null, { timeout: 15000 });
    await p.click('#select-mode');
    const addIds = [C['Backyard Starship'], C['Bulk Kid 1'], C['Bulk Kid 2']];
    // The list is paged and sorted, so find each fixture through the search box (selection survives filtering).
    for (const [q, ids] of [['Backyard Starship', [C['Backyard Starship']]], ['Bulk Kid', [C['Bulk Kid 1'], C['Bulk Kid 2']]]]) {
      await p.fill('#library-filter', q);
      for (const id of ids) { await p.waitForSelector(`.book-card[data-id="${id}"]`, { timeout: 15000 }); await p.click(`.book-card[data-id="${id}"]`); }
    }
    await p.waitForFunction(() => /3 selected/.test(document.querySelector('#bulk-bar')?.textContent || ''), null, { timeout: 5000 });
    ok(/^Add to kids shelf$/.test((await p.textContent('#bulk-kids-shelf')).trim()) && !!(await p.$('#bulk-remove-tag')), '[kids-bulk-add] select toolbar lacks "Add to kids shelf" / "Remove tag..."');
    await p.click('#bulk-tag'); await p.waitForSelector('#tag-pick', { timeout: 10000 });
    const pickOpts = await p.$$eval('#tag-pick option', os => os.map(o => o.textContent.trim()));
    ok(pickOpts.some(o => /^Kids shelf/.test(o)), `[kids-bulk-add] Add tag picker does not list Kids shelf: ${pickOpts}`);
    await p.click('#modal-root .modal-cancel');
    await p.click('#bulk-kids-shelf'); await p.waitForSelector('#kids-add-go', { timeout: 10000 });
    const conf = await p.textContent('#modal-root');
    ok(/3 books/.test(conf) && /kid accounts will be able to see/i.test(conf) && /classifier will not/i.test(conf), `[kids-bulk-add] confirmation does not state count and effect: ${conf}`);
    await p.click('#kids-add-go'); await p.waitForSelector('#bulk-result', { timeout: 20000 });
    ok(/3\s*added/.test(await p.textContent('#bulk-result')), `[kids-bulk-add] result summary: ${await p.textContent('#bulk-result')}`);
    await p.click('#bulk-result-ok');
    const added = await p.evaluate(async ids => { const out = []; for (const id of ids) { const w = await (await fetch(`/api/v1/library/${id}`)).json(); out.push({ on: (w.tags || []).some(t => t.name === 'kids'), via: w.kids_decision && w.kids_decision.via, by: w.kids_decision && w.kids_decision.by }); } return out; }, addIds);
    ok(added.every(a => a.on && a.via === 'manual_tag' && a.by === 'ada'), `[kids-bulk-add] not recorded as manual_tag: ${JSON.stringify(added)}`);
    const kt2 = await kidTitles();
    ok(kt2.includes('Backyard Starship') && kt2.includes('Bulk Kid 1') && kt2.includes('Bulk Kid 2'), `[kids-bulk-add] kid library after bulk add: ${kt2}`);
    // Settings > Tags uses the same label.
    await openSettingsTab(p, 'tags', '.tag-view');
    const tagRows = await p.$$eval('#settings-body .integration-card strong', els => els.map(e => e.textContent.trim()));
    ok(tagRows.includes('Kids shelf') && !tagRows.includes('kids'), `[kids-cleanup] Settings > Tags label: ${tagRows}`);
    p.removeAllListeners('dialog');

    // Player: admin gets a Manage link on the book page and the same label in the tag filter;
    // an adult gets neither the link nor any editing control.
    await p.goto(`${BASE}/app#library/${star}`, { waitUntil: 'load' }); await settle(p, MARK.book);
    const href = await p.$eval('a.pl-manage-link', a => a.getAttribute('href')).catch(() => null);
    ok(href === `/manage#library/${star}`, `[kids-cleanup] admin Player book page lacks the Manage link (${href})`);
    await p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await settle(p, MARK.library);
    const pOpts = await p.$$eval('#lib-tag option', os => os.map(o => o.textContent.trim()));
    ok(pOpts.some(o => /^Kids shelf/.test(o)) && !pOpts.some(o => /^kids\b/.test(o)), `[kids-cleanup] Player tag filter label: ${pOpts}`);
    const jane = await session('jane');
    await jane.p.goto(`${BASE}/app#library/${star}`, { waitUntil: 'load' }); await settle(jane.p, MARK.book);
    ok(!(await jane.p.$('a.pl-manage-link, .tag-remove, [aria-label^="Remove tag"]')), '[kids-cleanup] adult sees Management actions on a Player book page');
    await jane.ctx.close();
    await kid.ctx.close();
  } catch (er) { fails.push(`[kids-cleanup] check crashed: ${String(er.message || er).split("\n").slice(0, 3).join(" | ")}`); }



  // Removal and re-add while a kid is listening (admin removes the book from the kids shelf
  // mid-listen and puts it back ~30 minutes later). Live incident: the kid's client sent
  // ~140 PUT /progress a minute, all 404, for the whole 30 minutes. Now: the client keeps
  // the place on the device, shows one calm notice, probes at most every few minutes, and
  // when the book is back the saved place is replayed and playback resumes without a
  // refresh. The kid's clock is faked (Playwright page.clock) so 30 minutes take seconds,
  // and the test drives timeupdate/seeked events every 3 simulated seconds to make a
  // runaway caller out of it. The kid's worker is checked on the way (#321): the dropped
  // copies come back normally, and a dropped offline file says "Download again".
  if (who === 'ada') try {
    const AB = SEED.works.audio['Bedtime Audiobook'];
    const dialogs = [];
    p.removeAllListeners('dialog');
    p.on('dialog', d => { dialogs.push(d.message()); d.accept().catch(() => {}); });
    const kid = await session('owen'); const kp = kid.p;
    let sim = 0; const puts = [], fileGets = [];
    kp.on('request', r => {
      const u = new URL(r.url());
      if (r.method() === 'PUT' && /\/progress(\/|$)/.test(u.pathname)) puts.push({ sim, url: u.pathname });
      if (/\/files\//.test(u.pathname)) fileGets.push({ sim, status: null });
    });
    const T0 = Date.now();
    await kp.clock.install({ time: T0 }); await kp.clock.pauseAt(T0 + 1000);
    const tick = async ms => { await kp.clock.runFor(ms); sim += ms; };
    // Playwright's own waits poll with requestAnimationFrame, which the faked clock freezes: poll from Node.
    const until = async (fn, arg, timeout = 15000) => { const end = Date.now() + timeout; for (;;) { if (await kp.evaluate(fn, arg)) return true; if (Date.now() > end) throw new Error(`timed out waiting for ${fn.toString().replace(/\s+/g, ' ').slice(0, 90)}`); await new Promise(r => setTimeout(r, 150)); } };
    await kp.evaluate(h => { location.hash = h; }, `library/${AB.work}/read/${AB.asset}`);
    await until(() => !!document.querySelector('#au-play'));
    const man = await kp.evaluate(async ([w, a]) => (await fetch(`/api/v1/library/${w}/read/${a}`)).json(), [AB.work, AB.asset]);
    const fileUrl = man.files[0].url, coverUrl = `/api/v1/library/${AB.work}/cover`, jsonUrl = `/api/v1/library/${AB.work}`;
    // The kid saves the book for offline and the worker stores its cover and work JSON on the way.
    const cached = u => kp.evaluate(async u => { for (const n of await caches.keys()) { if (!n.includes('content')) continue; if (await (await caches.open(n)).match(u, { ignoreVary: true })) return true; } return false; }, u);
    // Listening: play, let a few saves through.
    await kp.waitForTimeout(500);
    await kp.click('#au-play');
    await until(() => { const a = document.getElementById('alx-audio'); return a && a.currentTime > 0.6 && !a.paused; }, null, 15000)
      .catch(async er => { throw new Error(`${er.message}; audio=${JSON.stringify(await kp.evaluate(() => { const a = document.getElementById('alx-audio'); return a && { t: a.currentTime, paused: a.paused, err: a.error && a.error.code, rs: a.readyState, src: a.currentSrc }; }))}`); });
    // Once it is playing (and buffered), the kid keeps the book offline; the worker also stores its
    // cover and work JSON. (Starting playback from a copy stored mid-stream is a separate matter.)
    await kp.evaluate(async ([c, j]) => { await fetch(c); await fetch(j); }, [coverUrl, jsonUrl]);
    const saved = await kp.evaluate(u => window.bookdoePwa.cacheContent(u), fileUrl);
    ok(saved && saved.ok, `[lifecycle] kid could not save the audiobook offline: ${JSON.stringify(saved)}`);
    await until(async ([c, j]) => { for (const n of await caches.keys()) { if (!n.includes('content')) continue; const ch = await caches.open(n); if (await ch.match(c) && await ch.match(j)) return true; } return false; }, [coverUrl, jsonUrl], 10000).catch(() => {});
    ok(await cached(fileUrl) && await cached(coverUrl) && await cached(jsonUrl), '[lifecycle] the worker did not store the kid\'s cover, work JSON and offline file before removal');
    await kp.evaluate(() => { document.getElementById('alx-audio').currentTime = 61; });
    await until(() => document.getElementById('alx-audio').currentTime >= 61, null, 10000);
    for (let i = 0; i < 4; i++) { await kp.evaluate(() => document.getElementById('alx-audio').dispatchEvent(new Event('timeupdate'))); await tick(11000); await kp.waitForTimeout(150); }
    const serverPos = () => kp.evaluate(async ([w, a]) => { const r = await fetch(`/api/v1/library/${w}/progress/${a}`); return r.ok ? (await r.json()).progress : { status: r.status }; }, [AB.work, AB.asset]);
    const before = await serverPos();
    ok(before && before.position >= 60, `[lifecycle] the kid's place was not saved while listening: ${JSON.stringify(before)}`);
    ok(puts.length >= 1 && puts.length <= 8, `[lifecycle] ${puts.length} saves while listening for ~45 s (expected a handful)`);

    // Admin takes the book off the kids shelf (Management, labelled remove control + confirmation).
    await openManage(p, `${BASE}/manage#library/${AB.work}`);
    await p.click('#tags-row .tag-chip[data-tag-name="kids"] button[aria-label^="Remove tag"]');
    await tagGone(p, 'kids');
    kp.__expectError = true;
    // The worker still has the stored copies; the first use of the file serves it and checks the server.
    await kp.evaluate(async u => (await fetch(u, { headers: { Range: 'bytes=0-9' } })).status, fileUrl);
    const dropDeadline = Date.now() + 10000; let dropped = false;
    while (Date.now() < dropDeadline && !dropped) { dropped = !(await cached(fileUrl)); if (!dropped) await kp.waitForTimeout(250); }
    ok(dropped, '[lifecycle/worker] the offline file was not dropped after the server revoked access');
    const jsonStatus = await kp.evaluate(async u => (await fetch(u)).status, jsonUrl);
    ok(jsonStatus === 404 && !(await cached(jsonUrl)), `[lifecycle/worker] kid work JSON after removal: ${jsonStatus}, cached=${await cached(jsonUrl)}`);
    const coverStatus = await kp.evaluate(async u => (await fetch(u)).status, coverUrl);
    ok(coverStatus === 404 || coverStatus === 200, `[lifecycle/worker] cover answer ${coverStatus}`);
    await kp.waitForTimeout(500);
    const revoked = await kp.evaluate(() => window.bookdoePwa.listRevoked());
    ok(revoked.includes(fileUrl), `[lifecycle/worker] the dropped offline file was not remembered as revoked: ${JSON.stringify(revoked)}`);

    // The listener keeps going. Stream fails (the element's next request is a 404), saves 404.
    const putsBefore = puts.length, filesBefore = fileGets.length, simBefore = sim;
    // The element's next request for the file is a 404 now (a new URL keeps Chromium's in-memory
    // media cache from answering for it, which a bare load() lets it do).
    await kp.evaluate(() => { const a = document.getElementById('alx-audio'); a.src = a.currentSrc.split('?')[0] + '?next=' + Math.random(); });
    // The clock is frozen, so the app's save request (which flips the player to 'unavailable') only goes out when time
    // advances. Advance in small steps, with a real pause per step for the 404 to come back, until the notice shows.
    // Jumping 3 s at a time let the app's 5 s reload timer fire before a loaded runner's response arrived: the book
    // reloaded, played again and the 'audio kept playing' assertion failed (3 of 5 runs under CPU contention).
    for (let k = 0; k < 16; k++) {
      if (await kp.evaluate(() => !!document.querySelector('#au-notice:not([hidden])'))) break;
      await tick(250); await kp.waitForTimeout(100);
    }
    const heldPos = Number(await kp.evaluate(() => document.getElementById('alx-audio').currentTime)) || 0;
    for (let i = 0; i < 600; i++) {            // 600 x 3 s = 30 simulated minutes
      await kp.evaluate(() => { const a = document.getElementById('alx-audio'); a.dispatchEvent(new Event('timeupdate')); a.dispatchEvent(new Event('seeked')); a.dispatchEvent(new Event('pause')); });
      await tick(3000); await kp.waitForTimeout(8);
    }
    const hiddenPuts = puts.slice(putsBefore);
    const perMinute = {}; for (const r of hiddenPuts) { const m = Math.floor((r.sim - simBefore) / 60000); perMinute[m] = (perMinute[m] || 0) + 1; }
    process.stdout.write(`[lifecycle] ${hiddenPuts.length} progress requests in 30 simulated minutes off the shelf (max ${Math.max(0, ...Object.values(perMinute))} per minute); file requests ${fileGets.length - filesBefore}\n`);
    ok(hiddenPuts.length <= 12, `[lifecycle] ${hiddenPuts.length} PUT /progress in 30 minutes off the shelf (the incident was ~4200); expected a handful`);
    ok(Math.max(0, ...Object.values(perMinute)) <= 2, `[lifecycle] more than 2 progress requests in one minute: ${JSON.stringify(perMinute)}`);
    ok(fileGets.length - filesBefore <= 6, `[lifecycle] ${fileGets.length - filesBefore} file requests while the book was away (the element must not loop on errors)`);
    const notice = await kp.$eval('#au-notice', n => ({ hidden: n.hidden, text: n.textContent })).catch(() => null);
    ok(notice && !notice.hidden && /isn.t available right now/i.test(notice.text), `[lifecycle] no calm notice while the book is away: ${JSON.stringify(notice)}`);
    const toasts = await kp.$$eval('.pl-toast', ts => ts.map(t => t.textContent));
    ok(!toasts.some(t => /error|media|decode|supported|not found|request failed|http/i.test(t)), `[lifecycle] raw error shown to the kid: ${JSON.stringify(toasts)}`);
    const held = await kp.evaluate(a => { const all = JSON.parse(localStorage.getItem('alx-pending-progress') || '{}'); return all[a] || null; }, AB.asset);
    ok(held && held.position >= 60 && held.asset_id === AB.asset, `[lifecycle] the listener's place was not kept on the device: ${JSON.stringify(held)}`);
    ok((await kp.evaluate(() => document.getElementById('alx-audio').paused)), '[lifecycle] audio kept playing/looping on a failed stream');

    // Admin puts it back.
    await p.click('#add-kids-shelf'); await p.waitForSelector('#tags-row .tag-chip[data-tag-name="kids"]', { timeout: 15000 });
    ok(dialogs.some(d => /kids shelf/i.test(d)), '[lifecycle] no confirmation when re-adding');
    const putsAtReadd = puts.length, simAtReadd = sim;
    let back = false;
    for (let i = 0; i < 16 && !back; i++) { await tick(30000); await kp.waitForTimeout(250); back = await kp.$eval('#au-notice', n => n.hidden).catch(() => false); }
    ok(back, '[lifecycle] the notice never cleared after the book was re-added (no recovery without a refresh)');
    process.stdout.write(`[lifecycle] recovered ${Math.round((sim - simAtReadd) / 1000)} simulated seconds after re-add with ${puts.length - putsAtReadd} request(s)\n`);
    ok(puts.length - putsAtReadd <= 3, `[lifecycle] ${puts.length - putsAtReadd} progress requests to recover`);
    ok(sim - simAtReadd <= 7 * 60 * 1000, `[lifecycle] recovery took ${(sim - simAtReadd) / 1000} s of simulated time (> 7 min probe cadence)`);
    await until(() => { const a = document.getElementById('alx-audio'); return a && !a.error && a.currentTime >= 59 && !a.paused; }, null, 15000)
      .catch(async () => fails.push(`[lifecycle] playback did not resume at the saved place: ${JSON.stringify(await kp.evaluate(() => { const a = document.getElementById('alx-audio'); return { t: a.currentTime, paused: a.paused, err: a.error && a.error.code, src: a.currentSrc }; }))}`));
    const after = await serverPos();
    ok(after && after.position >= held.position - 1, `[lifecycle] the held place was not replayed to the server: ${JSON.stringify(after)} vs ${held && held.position}`);
    ok(await kp.evaluate(() => localStorage.getItem('alx-pending-progress')) === null, '[lifecycle] pending positions still held after the replay');
    ok((await kp.$$eval('.pl-toast', ts => ts.map(t => t.textContent))).some(t => /back/i.test(t)), '[lifecycle] no "your book is back" toast');
    kp.__expectError = false;

    // The worker serves and re-stores normally after the re-add (no sticky "revoked" state).
    const statuses = await kp.evaluate(async ([c, j, f]) => [(await fetch(c)).status, (await fetch(j)).status, (await fetch(f, { headers: { Range: 'bytes=0-9' } })).status], [coverUrl, jsonUrl, fileUrl]);
    ok(statuses[0] === 200 && statuses[1] === 200 && (statuses[2] === 206 || statuses[2] === 200), `[lifecycle/worker] after re-add cover/json/file answered ${statuses}`);
    await kp.waitForTimeout(400);
    ok(await cached(coverUrl) && await cached(jsonUrl), '[lifecycle/worker] cover and work JSON were not stored again after the re-add');
    ok(!(await cached(fileUrl)), '[lifecycle/worker] the dropped offline file reappeared without being downloaded again');
    // The book page says what happened and offers a fresh download.
    await kp.evaluate(h => { location.hash = h; }, `library/${AB.work}`); await until(() => !!document.querySelector('.offline-toggle'));
    await until(() => { const b = document.querySelector('.offline-toggle'); return b && /Download again|Keep offline/.test(b.textContent); }, null, 10000);
    const btn = await kp.$eval('.offline-toggle', b => ({ text: b.textContent.trim(), revoked: b.dataset.revoked, title: b.title }));
    ok(/Download again/.test(btn.text) && btn.revoked === '1' && /off your shelf/.test(btn.title), `[lifecycle/worker] offline button after re-add: ${JSON.stringify(btn)}`);
    await kp.click('.offline-toggle');
    await until(() => /Kept offline/.test(document.querySelector('.offline-toggle')?.textContent || ''), null, 15000).catch(() => fails.push('[lifecycle/worker] Download again did not store the file'));
    ok(await cached(fileUrl) && !(await kp.evaluate(() => window.bookdoePwa.listRevoked())).includes(fileUrl), '[lifecycle/worker] after downloading again the file is not stored or still marked revoked');
    p.removeAllListeners('dialog');
    await kid.ctx.close();
  } catch (er) { fails.push(`[lifecycle] check crashed: ${String(er.stack || er).split('\n').slice(0, 6).join(' | ')}`); }

  // Audiobook controls (admin; the module is shared, so one session covers it): in both
  // shells and both themes every control must be readable (text and glyphs >= 4.5:1,
  // control edges >= 3:1, a disabled control dimmed but >= 3:1), >= 44 px, show a focus
  // ring, and the time line must read "0:04 / 7:30:33 · 7:30:29 left" style. Root cause of
  // the report: Management never loaded any .pl-* rule, so the buttons were browser-default
  // light-grey faces with styles.css's `button { color: inherit }` light text on top.
  if (who === 'ada') try {
    const AU = SEED.works.audio['Grown-up Audiobook'];
    const measure = () => p.evaluate(() => {
      const parse = c => {
        let m = c.match(/^rgba?\(([^)]+)\)$/);
        if (m) { const [r, g, bl, a = 1] = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [r, g, bl, a]; }
        m = c.match(/^color\(srgb ([^)]+)\)$/);
        if (m) { const v = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [v[0] * 255, v[1] * 255, v[2] * 255, v[3] ?? 1]; }
        return [255, 255, 255, 1];
      };
      const over = (top, under) => { const a = top[3]; return [top[0] * a + under[0] * (1 - a), top[1] * a + under[1] * (1 - a), top[2] * a + under[2] * (1 - a), 1]; };
      const bgOf = el => { const chain = []; for (let n = el; n; n = n.parentElement) chain.unshift(parse(getComputedStyle(n).backgroundColor)); return chain.reduce((acc, l) => l[3] === 0 ? acc : over(l, acc), [255, 255, 255, 1]); };
      const lum = ([r, g, bl]) => { const f = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl); };
      const ratio = (a, c) => { const [x, y] = [lum(a), lum(c)].sort((m, n) => n - m); return Math.round(((x + 0.05) / (y + 0.05)) * 100) / 100; };
      const out = [];
      const ids = ['#au-back', '#au-prev', '#au-back15', '#au-play', '#au-fwd30', '#au-next', '#au-speed', '#au-sleep', '.au-tr'];
      for (const sel of ids) {
        const el = document.querySelector(sel); if (!el) { out.push({ sel, missing: true }); continue; }
        const cs = getComputedStyle(el), r = el.getBoundingClientRect();
        const bg = bgOf(el), parentBg = bgOf(el.parentElement);
        const fg = over(parse(cs.color), bg);
        const border = parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none' ? over(parse(cs.borderTopColor), parentBg) : null;
        out.push({ sel, disabled: el.disabled, w: Math.round(r.width), h: Math.round(r.height), text: ratio(fg, bg),
          edge: border ? ratio(border, parentBg) : null, face: ratio(bg, parentBg), borderStyle: cs.borderTopStyle, bgAlpha: parse(cs.backgroundColor)[3] });
      }
      const label = document.querySelector('#au-times'); const lc = getComputedStyle(label);
      out.push({ sel: '#au-times', text: ratio(over(parse(lc.color), bgOf(label)), bgOf(label)), label: label.textContent });
      for (const sel of ['.pl-chapter-now', '#au-total']) { const el = document.querySelector(sel); if (el) out.push({ sel, text: ratio(over(parse(getComputedStyle(el).color), bgOf(el)), bgOf(el)), empty: !el.textContent.trim() }); }
      const seek = document.querySelector('#au-seek').getBoundingClientRect();
      out.push({ sel: '#au-seek', w: Math.round(seek.width), h: Math.round(seek.height) });
      out.push({ sel: 'overflow', over: document.documentElement.scrollWidth > innerWidth });
      return out;
    });
    const focusRings = () => p.evaluate(() => {
      const parse = c => { let m = c.match(/^rgba?\(([^)]+)\)$/); if (m) { const v = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [v[0], v[1], v[2], v[3] ?? 1]; } m = c.match(/^color\(srgb ([^)]+)\)$/); if (m) { const v = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [v[0] * 255, v[1] * 255, v[2] * 255, v[3] ?? 1]; } return [255, 255, 255, 1]; };
      const over = (top, under) => { const a = top[3]; return [top[0] * a + under[0] * (1 - a), top[1] * a + under[1] * (1 - a), top[2] * a + under[2] * (1 - a), 1]; };
      const bgOf = el => { const chain = []; for (let n = el; n; n = n.parentElement) chain.unshift(parse(getComputedStyle(n).backgroundColor)); return chain.reduce((acc, l) => l[3] === 0 ? acc : over(l, acc), [255, 255, 255, 1]); };
      const lum = ([r, g, bl]) => { const f = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl); };
      const ratio = (a, c) => { const [x, y] = [lum(a), lum(c)].sort((m, n) => n - m); return Math.round(((x + 0.05) / (y + 0.05)) * 100) / 100; };
      const el = document.activeElement; if (!el || el === document.body || !el.closest('.pl-consume')) return null;
      const cs = getComputedStyle(el); const bg = bgOf(el.parentElement || el);
      return { id: el.id || el.className, style: cs.outlineStyle, width: parseFloat(cs.outlineWidth), ratio: ratio(over(parse(cs.outlineColor), bg), bg) };
    });
    for (const shell of ['app', 'manage']) for (const theme of ['dark', 'light']) {
      const tag = `[audio-controls ${shell}/${theme}]`;
      await p.goto('about:blank');
      await p.addInitScript(([t]) => { try { localStorage.setItem('bookdoe-theme', t); localStorage.setItem('alx-pl-theme', t); } catch {} }, [theme]);
      await p.setViewportSize({ width: 390, height: 844 });
      await p.goto(`${BASE}/${shell}#library/${AU.work}/read/${AU.asset}`, { waitUntil: 'load' });
      await p.waitForSelector('#au-play', { timeout: 30000 });
      await p.waitForFunction(() => /\d+:\d\d \/ \d+:\d\d/.test(document.querySelector('#au-times')?.textContent || ''), null, { timeout: 15000 }).catch(() => {});
      ok((await p.getAttribute('html', 'data-theme')) === theme, `${tag} theme did not apply`);
      if (process.env.UI_SHOTS) { await import('fs').then(fs => fs.mkdirSync(process.env.UI_SHOTS, { recursive: true })); await p.screenshot({ path: `${process.env.UI_SHOTS}/audio-${shell}-${theme}.png` }); }
      const rows = await measure();
      for (const r of rows) {
        if (r.missing) { ok(false, `${tag} ${r.sel} is missing`); continue; }
        if (r.sel === 'overflow') { ok(!r.over, `${tag} the page scrolls sideways`); continue; }
        if (r.sel === '#au-seek') { ok(r.h >= 44, `${tag} seek slider is ${r.h}px tall (< 44)`); continue; }
        if (r.sel !== '#au-times' && r.sel !== '.pl-chapter-now' && r.sel !== '#au-total') { ok(r.w >= 44 && r.h >= 44, `${tag} ${r.sel} touch target ${r.w}x${r.h} (< 44px)`); }
        if (r.empty) continue;
        if (r.disabled) {
          ok(r.text >= 3, `${tag} disabled ${r.sel} text is ${r.text}:1 (< 3, unreadable)`);
          ok(r.text < 7 && (r.borderStyle === 'dashed' || r.bgAlpha === 0), `${tag} disabled ${r.sel} does not look different from an enabled control`);
        } else ok(r.text >= 4.5, `${tag} ${r.sel} text/glyph contrast ${r.text}:1 (< 4.5)`);
        if (typeof r.edge === 'number' && !r.disabled) ok(r.edge >= 3 || r.face >= 3, `${tag} ${r.sel} boundary ${r.edge}:1 (< 3) and face ${r.face}:1`);
        if (r.sel === '#au-play') ok(r.face >= 3, `${tag} play button face ${r.face}:1 against the page (< 3)`);
      }
      ok(rows.some(r => r.disabled), `${tag} no disabled control to compare (Next should be disabled on a one-file book)`);
      await p.evaluate(() => { document.getElementById('alx-audio').currentTime = 0; });
      await p.waitForFunction(() => /^0:0\d \/ 3:20/.test(document.querySelector('#au-times')?.textContent || ''), null, { timeout: 10000 }).catch(() => {});
      const label = (await measure()).find(r => r.sel === '#au-times').label;
      ok(/^\d+:\d{2}(:\d{2})? \/ (\d+:\d{2}(:\d{2})?|--:--)( · \d+:\d{2}(:\d{2})? left)?$/.test(label), `${tag} malformed time label "${label}"`);
      ok(label === '0:00 / 3:20 · 3:20 left', `${tag} expected "0:00 / 3:20 · 3:20 left", saw "${label}"`);
      // Seek to 100 s: the label follows, with the same separators.
      await p.evaluate(() => { const a = document.getElementById('alx-audio'); a.currentTime = 100; });
      await p.waitForFunction(() => /^1:4\d \/ 3:20/.test(document.querySelector('#au-times')?.textContent || ''), null, { timeout: 10000 }).catch(() => {});
      ok(/^1:4\d \/ 3:20 · 1:\d\d left$/.test(await p.textContent('#au-times')), `${tag} label after seeking: "${await p.textContent('#au-times')}"`);
      // A long book reads like the report's example (formatter, in the real page).
      const long = await p.evaluate(async () => { const m = await import('/assets/consume.js'); return [m.formatAudioTimes({ cur: 4, dur: 27033 }).label, m.formatAudioTimes({ cur: 4, dur: NaN }).label]; });
      ok(long[0] === '0:04 / 7:30:33 · 7:30:29 left' && long[1] === '0:04 / --:--', `${tag} formatter in the page: ${JSON.stringify(long)}`);
      // Keyboard focus: every focusable control gets a visible ring (>= 2 px, >= 3:1).
      await p.evaluate(() => document.querySelector('#au-back').focus());
      const seen = new Set();
      for (let i = 0; i < 12; i++) {
        await p.keyboard.press('Tab'); const f = await focusRings(); if (!f) continue;
        seen.add(f.id);
        ok(f.style !== 'none' && f.width >= 2 && f.ratio >= 3, `${tag} focus ring on ${f.id}: ${f.style} ${f.width}px ${f.ratio}:1`);
      }
      for (const id of ['au-seek', 'au-prev', 'au-back15', 'au-play', 'au-fwd30', 'au-speed', 'au-sleep']) ok(seen.has(id), `${tag} Tab never reached #${id} (saw ${[...seen]})`);
      await p.evaluate(() => { const a = document.getElementById('alx-audio'); a.pause(); });
    }
    await p.addInitScript(() => { try { localStorage.removeItem('bookdoe-theme'); localStorage.removeItem('alx-pl-theme'); } catch {} }); await p.setViewportSize({ width: 390, height: 760 }); await p.goto(`${BASE}/app#home`, { waitUntil: 'load' }); await settle(p, MARK.home);
  } catch (er) { fails.push(`[audio-controls] check crashed: ${String(er.message || er).split('\n').slice(0, 3).join(' | ')}`); }

  // Send to Kindle (kid, who has no address yet): button reads "Add Kindle email" and
  // leads to Settings; after saving an address the book page offers "Send to Kindle";
  // one click sends one message to the e2e SMTP sink; a second click is blocked.
  if (who === 'owen' && process.env.UI_SMTP_OUT) try {
    const fs = await import('fs');
    const book = `${BASE}/app#library/${SEED.works['Readable Kids Book']}`;
    await p.goto(book, { waitUntil: 'load' }); await settle(p, MARK.book);
    await p.waitForSelector('#kindle-add', { timeout: 15000 });
    ok(!(await p.$('#kindle-send')), '[kindle] send button shown without a Kindle address');
    await p.click('#kindle-add'); await p.waitForSelector('#kindle-email', { timeout: 15000 });
    ok((await p.textContent('#pl-kindle')).includes('bookdoe@e2e.test'), '[kindle] settings do not show the sender address to approve');
    await p.fill('#kindle-email', 'not-an-email'); await p.click('#kindle-form button[type=submit], #kindle-form .pl-button.small'); await p.waitForTimeout(600);
    ok((await p.evaluate(async () => (await (await fetch('/api/player/v1/kindle')).json()).kindle_email)) === null, '[kindle] an invalid address was saved');
    await p.fill('#kindle-email', 'owen_e2e@kindle.com'); await p.click('#kindle-form .pl-button.small');
    await p.waitForFunction(() => document.querySelector('#kindle-clear'), null, { timeout: 15000 });
    await p.goto(book, { waitUntil: 'load' }); await settle(p, MARK.book);
    await p.waitForSelector('#kindle-send', { timeout: 15000 });
    const before = fs.readFileSync(process.env.UI_SMTP_OUT, 'utf8').trim().split('\n').filter(Boolean).length;
    await p.click('#kindle-send');
    await p.waitForFunction(() => document.querySelector('#kindle-send')?.textContent.includes('Sent'), null, { timeout: 30000 });
    ok(await p.$eval('#kindle-send', b => b.disabled), '[kindle] send button re-enabled after success (double submit possible)');
    const lines = fs.readFileSync(process.env.UI_SMTP_OUT, 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l));
    ok(lines.length === before + 1, `[kindle] expected one message at the sink, saw ${lines.length - before}`);
    const m = lines[lines.length - 1] || {};
    ok(m.to && m.to[0] === 'owen_e2e@kindle.com' && m.from === 'bookdoe@e2e.test' && /\.epub$/.test(m.attachment || ''), `[kindle] wrong message: ${JSON.stringify(m)}`);
    // A hidden book cannot be sent even by id (kid visibility): this 404 is the point.
    p.__expectError = true;
    const code = await p.evaluate(async id => (await fetch(`/api/player/v1/kindle/send/${id}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': (await (await fetch('/api/v1/auth/me')).json()).csrf }, body: '{}' })).status, SEED.works['Grown-up Classic']);
    p.__expectError = false;
    ok(code === 404, `[kindle] kid could target a hidden book (${code})`);
  } catch (er) { fails.push(`[kindle] check crashed: ${String(er.message || er).split('\n')[0]}`); }

  // Relay failure (admin): point the relay at a closed port, send, and the Player
  // shows the server's plain reason (424 with detail), then restore the sink.
  if (who === 'ada' && process.env.UI_SMTP_OUT) try {
    const csrf = async () => (await (await fetch('/api/v1/auth/me')).json()).csrf;
    const cfg = await p.evaluate(async () => (await (await fetch('/api/v1/settings/devices')).json()));
    const put = async body => p.evaluate(async ({ body, csrf }) => (await fetch('/api/v1/settings/devices', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(body) })).status, { body, csrf: await p.evaluate(csrf) });
    ok((await put({ ...cfg, port: 1, password: null })) === 200, '[relay] could not reconfigure the relay for the failure case');
    await p.evaluate(async c => fetch('/api/player/v1/kindle', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': c }, body: JSON.stringify({ kindle_email: 'ada_e2e@kindle.com' }) }), await p.evaluate(csrf));
    await p.goto("about:blank"); /* fresh document: the Player keeps the Kindle state it loaded */ await p.goto(`${BASE}/app#library/${SEED.works['Readable Kids Book']}`, { waitUntil: 'load' }); await settle(p, MARK.book);
    await p.waitForSelector('#kindle-send', { timeout: 15000 });
    p.__expectError = true;
    await p.click('#kindle-send');
    await p.waitForFunction(() => [...document.querySelectorAll('.pl-toast')].some(t => /Could not reach the mail server/.test(t.textContent)), null, { timeout: 30000 }).catch(() => {});
    p.__expectError = false;
    const toasts = await p.$$eval('.pl-toast', ts => ts.map(t => t.textContent));
    ok(toasts.some(t => /Could not reach the mail server 127\.0\.0\.1:1: connection refused/.test(t)), `[relay] Player did not show the relay reason: ${JSON.stringify(toasts)}`);
    ok(!(await p.$eval('#kindle-send', b => b.disabled)), '[relay] send button stayed disabled after a failure');
    // The Management test button reports the same reason, then success once restored.
    await openSettingsTab(p, 'devices', '#smtp-test');
    p.__expectError = true; await p.click('#smtp-test');
    await p.waitForFunction(() => /connection refused|Sent a test message|no email/.test(document.querySelector('#smtp-test-result')?.textContent || ''), null, { timeout: 40000 }).catch(() => {});
    p.__expectError = false;
    const res1 = await p.textContent('#smtp-test-result');
    ok(/Could not reach the mail server 127\.0\.0\.1:1: connection refused/.test(res1) || /no email/.test(res1), `[relay] test button did not show the reason: ${res1}`);
    ok((await put({ ...cfg, password: null })) === 200, '[relay] could not restore the relay');
  } catch (er) { fails.push(`[relay] check crashed: ${String(er.message || er).split('\n')[0]}`); }

  // Reader font size (admin only: once is enough, the module is shared).
  if (who === 'ada') try {
    await p.goto(`${BASE}/app#library/${SEED.works['Readable Kids Book']}/read/${SEED.works.asset}`, { waitUntil: 'load' });
    const frame = async () => { await p.waitForSelector('#rd-view iframe', { timeout: 40000 }); const f = p.frames().find(f => f.parentFrame() && f.url() !== ''); return f || p.frames()[1]; };
    const measure = async () => { const f = await frame(); await f.waitForSelector('p', { timeout: 20000 }); return f.evaluate(() => ({ body: parseFloat(getComputedStyle(document.body).fontSize), p: parseFloat(getComputedStyle(document.querySelector('p')).fontSize), text: document.querySelector('p').textContent.slice(0, 20) })); };
    const before = await measure();
    ok(before.text.startsWith('Paragraph'), `[reader] text did not render: ${JSON.stringify(before)}`);
    await p.click('#rd-aa'); await p.waitForSelector('#rd-size-up');
    await p.click('#rd-size-up'); await p.click('#rd-size-up'); await p.waitForTimeout(600);
    const after = await measure();
    ok(after.p > before.p, `[reader] A+ did not grow the rendered text: ${before.p}px -> ${after.p}px`);
    await p.click('#rd-size-down'); await p.waitForTimeout(600);
    const down = await measure();
    ok(down.p < after.p && down.p > before.p, `[reader] A- did not shrink: ${after.p}px -> ${down.p}px`);
    await p.reload({ waitUntil: 'load' });
    const reloaded = await measure();
    ok(Math.abs(reloaded.p - down.p) < 0.5, `[reader] size did not persist across reload: ${down.p}px -> ${reloaded.p}px`);
  } catch (er) { fails.push(`[reader] check crashed: ${String(er.message || er).split('\n')[0]}`); }
  await ctx.close();
}

// Series navigation and series-level actions: the admin opens a book that is in a series
// from Management, follows the "Series:" button, puts the whole series on the kids shelf
// through the confirmation, sees the per-book chips change; the kid then sees those books
// (and their series page) while a book of another series stays hidden; the Player book page
// links to its series for admin and kid.
try {
  const S = SEED.works.series;
  const { ctx, p } = await session('ada');
  await p.goto(`${BASE}/manage#library/${S['Series Kid 2']}`, { waitUntil: 'load' });
  await p.waitForSelector('#series-links a', { timeout: 20000 });
  const link = await p.$eval('#series-links a', a => { const r = a.getBoundingClientRect(); return { text: a.textContent.trim(), href: a.getAttribute('href'), inView: r.top >= 0 && r.bottom <= innerHeight }; });
  ok(/^Series: E2E Daniel X #2/.test(link.text) && link.href === `#series/${S.series_id}`, `[series] management series button wrong: ${JSON.stringify(link)}`);
  ok(link.inView, '[series] management series button is not visible without scrolling on a phone');
  await p.click('#series-links a'); await p.waitForSelector('#series-books', { timeout: 20000 });
  ok((await p.$$eval('#series-books .series-pick', cs => cs.length)) === 3, '[series] series table should list 3 books');
  ok((await p.$$eval('.series-kids-chip[data-kids="no"]', cs => cs.length)) === 3, '[series] every book should start off the kids shelf');
  // Subset selection narrows the action label; clearing it goes back to the whole series.
  await p.check('.series-pick >> nth=0');
  ok(/Add 1 selected to Kids shelf/.test(await p.textContent('#series-kids-add')), '[series] subset selection did not change the action label');
  await p.uncheck('.series-pick >> nth=0');
  ok(/Add series to Kids shelf/.test(await p.textContent('#series-kids-add')), '[series] whole-series label missing');
  await p.click('#series-kids-add'); await p.waitForSelector('#kids-add-confirm', { timeout: 10000 });
  const confirmText = await p.textContent('#kids-add-confirm');
  ok(/3 books/.test(confirmText) && /Kid accounts will be able to see and open these books/.test(confirmText), `[series] confirmation lacks count or kid warning: ${confirmText}`);
  await p.click('#kids-add-go'); await p.waitForSelector('#bulk-result', { timeout: 20000 });
  ok(/3\s*added/.test(await p.textContent('#bulk-result')), '[series] result summary does not say 3 added');
  await p.click('#bulk-result-ok');
  await p.waitForFunction(() => document.querySelectorAll('.series-kids-chip[data-kids="yes"]').length === 3, null, { timeout: 15000 }).catch(() => {});
  ok((await p.$$eval('.series-kids-chip[data-kids="yes"]', cs => cs.length)) === 3, '[series] per-book chips did not update to Kids shelf: yes');
  await ctx.close();

  for (const who of ['ada', 'owen']) {
    const { ctx: c2, p: q } = await session(who);
    await q.goto(`${BASE}/app#library/${S['Series Kid 2']}`, { waitUntil: 'load' }); await settle(q, MARK.book);
    await q.waitForSelector('.pl-series-link', { timeout: 15000 });
    const t = (await q.textContent('.pl-series-link')).trim();
    ok(/^Series: E2E Daniel X #2/.test(t), `[series/${who}] Player book page series link wrong: ${t}`);
    await q.click('.pl-series-link'); await q.waitForFunction(() => /E2E Daniel X/.test(document.querySelector('#pl-view h1')?.textContent || '') && !document.querySelector('#pl-view .pl-skeleton') && document.querySelector('#pl-view .pl-grid .pl-book'), null, { timeout: 15000 }).catch(() => {});
    ok(q.url().endsWith(`#series/${S.series_id}`), `[series/${who}] link did not open the series view: ${q.url()}`);
    const titles = await q.$$eval('.pl-grid .pl-book', bs => bs.map(x => x.getAttribute('aria-label')));
    ok(['Series Kid 1', 'Series Kid 2', 'Series Kid 4'].every(x => titles.includes(x)), `[series/${who}] Player series view is missing books: ${JSON.stringify(titles)}`);
    ok(!/Series Hidden/.test(await q.content()), `[series/${who}] hidden book of another series appeared`);
    if (who === 'owen') {
      q.__expectError = true;
      const codes = await q.evaluate(async ({ id, sid }) => [(await fetch(`/api/v1/library/${id}`)).status, (await fetch(`/api/v1/series/${sid}`)).status], { id: S['Series Hidden 1'], sid: S.other_series_id });
      q.__expectError = false;
      ok(codes[0] === 404 && codes[1] === 404, `[series/owen] hidden book or its series reachable: ${codes}`);
      const d = await q.evaluate(async sid => (await fetch(`/api/v1/series/${sid}`)).json(), S.series_id);
      ok(Array.isArray(d.gaps) && d.gaps.length === 0, `[series/owen] kid series payload leaks numbering gaps: ${JSON.stringify(d.gaps)}`);
    }
    await c2.close();
  }
} catch (er) { fails.push(`[series] check crashed: ${String(er.message || er).split('\n')[0]}`); }

// One "Keep offline" per asset, never per file: a book whose audiobook has 12 tracks and
// whose ebook has 2 files shows exactly one control per asset in Management and the Player,
// and one click stores every file of that asset.
try {
  const M = SEED.works.multi;
  const { ctx, p } = await session('ada');
  await p.goto(`${BASE}/manage#library/${M.work}`, { waitUntil: 'load' });
  await p.waitForSelector('.asset-row', { timeout: 20000 });
  await p.waitForSelector('.asset-offline', { timeout: 15000 }).catch(() => fails.push('[offline] no Keep offline control in Management'));
  const mrows = await p.$$eval('.asset-row', rs => rs.map(r => ({ kind: r.querySelector('.asset-kind')?.textContent.trim().split(/\s/)[1], offline: r.querySelectorAll('.asset-offline').length, kindle: r.querySelectorAll('.asset-kindle').length, files: Number((r.querySelector('.files-summary')?.textContent.match(/^(\d+) files/) || [])[1]) || r.querySelectorAll('.files .file-line').length })));
  ok(mrows.length === 2 && mrows.every(r => r.offline === 1), `[offline] Management should show one Keep offline per asset: ${JSON.stringify(mrows)}`);
  ok(mrows.some(r => r.files === 12) && mrows.some(r => r.files === 2), `[offline] fixture file counts unexpected: ${JSON.stringify(mrows)}`);
  ok(mrows.filter(r => r.kindle).every(r => r.kindle === 1), `[offline] Send to Kindle duplicated: ${JSON.stringify(mrows)}`);
  const overflowM = await p.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  ok(overflowM <= 1, `[offline] Management book page overflows horizontally by ${overflowM}px`);
  const storedCount = urls => p.evaluate(async us => { let n = 0; for (const u of us) if (await window.bookdoePwa.isCached(u)) n++; return n; }, urls);
  const audioBtn = p.locator('.asset-row', { hasText: 'Audiobook' }).locator('.asset-offline');
  const urls = JSON.parse(await audioBtn.getAttribute('data-urls'));
  ok(urls.length === 12, `[offline] audiobook control should cover 12 files, covers ${urls.length}`);
  await audioBtn.click();
  await p.waitForFunction(() => /Kept offline/.test(document.querySelector('.asset-offline')?.textContent || ''), null, { timeout: 60000 }).catch(() => {});
  ok((await storedCount(urls)) === 12, `[offline] one click did not store all 12 tracks (stored ${await storedCount(urls)})`);
  await audioBtn.click();
  await p.waitForFunction(() => /^Keep offline/.test(document.querySelector('.asset-row .asset-offline')?.textContent || ''), null, { timeout: 30000 }).catch(() => {});
  ok((await storedCount(urls)) === 0, '[offline] second click did not remove every track');
  await p.goto(`${BASE}/app#library/${M.work}`, { waitUntil: 'load' }); await settle(p, MARK.book);
  await p.waitForSelector('.offline-toggle', { timeout: 15000 });
  const prow = await p.$$eval('.offline-toggle', bs => bs.map(x => ({ text: x.textContent.trim(), n: JSON.parse(x.dataset.urls).length })));
  ok(prow.length === 2 && prow.map(x => x.n).sort((a, b) => a - b).join() === '2,12', `[offline] Player should show one control per asset (12 and 2 files): ${JSON.stringify(prow)}`);
  ok((await p.$$('#kindle-send')).length <= 1, '[offline] Player shows Send to Kindle more than once');
  const overflowP = await p.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  ok(overflowP <= 1, `[offline] Player book page overflows horizontally by ${overflowP}px`);
  const big = p.locator('.offline-toggle', { hasText: 'audiobook' });
  const purls = JSON.parse(await big.getAttribute('data-urls'));
  await big.click();
  await p.waitForFunction(() => [...document.querySelectorAll('.offline-toggle')].some(x => /Kept offline/.test(x.textContent) && JSON.parse(x.dataset.urls).length === 12), null, { timeout: 60000 }).catch(() => {});
  ok((await storedCount(purls)) === 12, `[offline] Player click did not store all 12 tracks (stored ${await storedCount(purls)})`);
  await big.click();
  await p.waitForFunction(() => [...document.querySelectorAll('.offline-toggle')].every(x => /^Keep offline/.test(x.textContent)), null, { timeout: 30000 }).catch(() => {});
  ok((await storedCount(purls)) === 0, '[offline] Player second click did not remove all tracks');
  await ctx.close();
} catch (er) { fails.push(`[offline] check crashed: ${String(er.message || er).split('\n')[0]}`); }
// ---- Staying signed in: "keep me signed in" and "trust this device" -----------------------
// Sign in with the checkbox (90 day session cookie), trust the browser from the Home offer,
// then lose the session cookie (what 24 h later looks like): a reload must still be signed
// in through the HttpOnly device cookie, the Player must work, Management must not open for
// it, and revoking it from another session must sign the browser out on its next request.
for (const [who, isAdmin] of [['ada', true], ['owen', false]]) {
  const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: 390, height: 760 }, serviceWorkers: 'allow' });
  const p = await ctx.newPage();
  p.on('pageerror', e => fails.push(`[trust/${who}] page error: ${e}`));
  const cookie = async name => (await ctx.cookies()).find(c => c.name === name);
  try {
    await p.goto(`${BASE}/app`, { waitUntil: 'load' });
    await p.waitForSelector('#login-form', { timeout: 15000 });
    ok(await p.$('#login-form input[name=remember]'), `[trust/${who}] gate has no "Keep me signed in" checkbox`);
    ok(/90 days/.test(await p.textContent('#login-form')), `[trust/${who}] gate does not say 90 days`);
    ok(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `[trust/${who}] gate overflows horizontally`);
    await p.fill('#login-form [name=username]', who); await p.fill('#login-form [name=password]', SEED.password);
    await p.check('#login-form input[name=remember]');
    await p.click('#login-form button');
    await p.waitForSelector('#pl-app:not(.hidden)', { timeout: 20000 });
    const sess = await cookie('bookdoe_session');
    ok(sess && sess.expires - Date.now() / 1000 > 80 * 86400, `[trust/${who}] remembered session cookie is not ~90 days (expires ${sess && Math.round((sess.expires - Date.now() / 1000) / 86400)}d)`);
    ok(!(await cookie('bookdoe_device')), `[trust/${who}] a device cookie exists before trusting`);

    // The quiet offer on Home.
    await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
    await p.waitForSelector('#trust-yes', { timeout: 15000 });
    ok(/revoke it/.test(await p.textContent('#trust-prompt')), `[trust/${who}] offer lacks the consequence wording`);
    await p.click('#trust-yes');
    await p.waitForFunction(() => document.querySelector('#pl-toast')?.textContent.includes('trusted'), null, { timeout: 10000 });
    const dev = await cookie('bookdoe_device');
    // Chromium caps cookie lifetimes at 400 days whatever Max-Age says; rotation re-sets it every 30 days of use.
    ok(dev && dev.httpOnly && dev.sameSite === 'Lax' && dev.expires - Date.now() / 1000 > 300 * 86400, `[trust/${who}] device cookie attributes wrong: ${JSON.stringify(dev && { ...dev, value: '' })}`);
    ok(!(await p.evaluate(() => document.cookie)).includes('bookdoe_device'), `[trust/${who}] script can read the device cookie`);

    // Settings lists it, marked as this device.
    await p.goto(`${BASE}/app#settings`, { waitUntil: 'load' });
    await p.waitForSelector('#forget-here', { timeout: 15000 });
    ok(/this device/i.test(await p.textContent('#pl-trust')), `[trust/${who}] Settings does not mark the current device`);

    // Lose the session cookie; reload: the device cookie keeps the Player signed in.
    const rest = (await ctx.cookies()).filter(c => c.name !== 'bookdoe_session');
    await ctx.clearCookies(); await ctx.addCookies(rest);
    await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
    await p.waitForSelector('#pl-app:not(.hidden)', { timeout: 20000 });
    ok(!(await cookie('bookdoe_session')), `[trust/${who}] a session cookie came back from the device cookie`);
    const home = await p.evaluate(async () => (await fetch('/api/player/v1/home')).status);
    ok(home === 200, `[trust/${who}] Player home not reachable with the device cookie (${home})`);
    await p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await settle(p, MARK.library);
    ok((await p.$$('.pl-card, .pl-book, .pl-cover')).length > 0, `[trust/${who}] library did not render under the device cookie`);
    const write = await p.evaluate(async () => { const me = await (await fetch('/api/player/v1/me')).json(); return { csrf: !!me.csrf, noHeader: (await fetch('/api/player/v1/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status }; });
    ok(write.csrf && write.noHeader === 403, `[trust/${who}] device-cookie write without CSRF was not refused: ${JSON.stringify(write)}`);

    // Management stays closed to it (the admin gets the sign-in, the kid the 403 page).
    await p.goto(`${BASE}/manage`, { waitUntil: 'load' });
    if (isAdmin) {
      await p.waitForSelector('#auth-shell:not(.hidden)', { timeout: 15000 });
      ok(await p.$('#app-shell.hidden'), `[trust/${who}] Management opened on a device cookie`);
    } else ok(/for administrators/.test(await p.textContent('body')), `[trust/${who}] a kid's Management page is not the 403 page`);
    const adminApi = await p.evaluate(async () => (await fetch('/api/v1/users')).status);
    ok(adminApi === 403, `[trust/${who}] /api/v1/users answered ${adminApi} on a device cookie`);

    // Revoke from another signed-in session; the next request is signed out.
    const ctx2 = await b.newContext(); const p2 = await ctx2.newPage();
    await p2.goto(`${BASE}/app`, { waitUntil: 'load' });
    const gone = await p2.evaluate(async ({ u, pw }) => {
      const l = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: pw }) });
      const csrf = (await l.json()).csrf; const list = await (await fetch('/api/player/v1/devices')).json();
      const d = list.items.find(x => x.kind === 'browser');
      return (await fetch(`/api/player/v1/devices/${d.id}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } })).status;
    }, { u: who, pw: SEED.password });
    ok(gone === 200, `[trust/${who}] revoking from another session answered ${gone}`);
    await ctx2.close();
    await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
    await p.waitForSelector('#pl-auth:not(.hidden)', { timeout: 15000 });
    ok(!(await cookie('bookdoe_device')), `[trust/${who}] the dead device cookie was not cleared`);
  } catch (er) { fails.push(`[trust/${who}] check crashed: ${String(er.message || er).split('\n')[0]}`); }
  await ctx.close();
}
await b.close();
if (fails.length) { console.log(fails.join('\n')); process.exit(1); }
console.log('ui-player-e2e: covers, tag filter, reader text size, Inbox bulk approve, auto-approval re-check (dry-run, queue, bulk keep/remove), kids-shelf tag removal (single and bulk) and Send to Kindle verified for admin, adult and kid; remember-me and trusted-device sign-in (device cookie, Management refused, revocation) verified for admin and kid; series navigation and whole-series kids-shelf action verified');
