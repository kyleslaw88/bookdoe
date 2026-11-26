// ui-gate: widths=360,390,1280 order=40
// Player shell smoke: every Player route renders, fits the viewport, logs no console
// error and makes no failed request; the bottom navigation is present on a phone and
// the side rail on wider screens. Width comes from UI_WIDTH (default 390). Run by
// ui-gate.mjs against its seeded instance, or by hand with UI_BASE.
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const WIDTH = Number(process.env.UI_WIDTH) || 390;
const b = await chromium.launch();
const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: WIDTH, height: 760 } });
const p = await ctx.newPage();
const fails = [], consoleErrors = [], failedRequests = [];
p.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
p.on('pageerror', e => consoleErrors.push(String(e)));
p.on('requestfailed', r => failedRequests.push(`${r.method()} ${r.url()} ${r.failure()?.errorText || ''}`));
p.on('response', r => { if (r.status() >= 400 && !r.url().includes('/cover')) failedRequests.push(`${r.status()} ${r.url()}`); });

async function visit(route) {
  await p.goto(`${BASE}/app#${route}`, { waitUntil: 'load' });
  await p.waitForLoadState('networkidle');
  // Same-document hash navigation: the previous page stays until the new one has its
  // data, so wait for a marker of the requested page, then for its skeleton to clear.
  const mark = { home: '.pl-section', library: '#lib-tag', authors: '#au-list', series: '#se-list', discover: '#disc-q', approvals: '#ap-list', settings: '#theme-seg' }[route];
  if (mark) await p.waitForSelector(mark, { timeout: 20000 }).catch(() => {});
  await p.waitForFunction(() => !document.querySelector('#pl-view .pl-skeleton'), null, { timeout: 15000 }).catch(() => {});
  const m = await p.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth, clientW: document.documentElement.clientWidth,
    text: (document.querySelector('#pl-view')?.textContent || '').trim(),
    nav: !!document.querySelector('#pl-nav a[data-nav="home"]'),
    navBox: document.querySelector('#pl-nav')?.getBoundingClientRect().toJSON(),
    skeleton: !!document.querySelector('#pl-view .pl-skeleton'),
  }));
  if (m.scrollW > m.clientW + 2) fails.push(`[${WIDTH}px/${route}] horizontal overflow: ${m.scrollW}px in ${m.clientW}px`);
  if (m.text.length < 20) fails.push(`[${WIDTH}px/${route}] view did not render`);
  if (m.skeleton) fails.push(`[${WIDTH}px/${route}] loading skeleton left on screen`);
  if (!m.nav) fails.push(`[${WIDTH}px/${route}] primary navigation missing`);
  if (m.navBox) {
    const phone = WIDTH < 820;
    if (phone && m.navBox.top < 500) fails.push(`[${WIDTH}px/${route}] bottom navigation is not at the bottom (top=${m.navBox.top})`);
    if (!phone && m.navBox.left > 1) fails.push(`[${WIDTH}px/${route}] side rail is not on the left`);
  }
  return m;
}

for (const route of ['home', 'library', 'authors', 'series', 'discover', 'approvals', 'settings']) await visit(route);
// Open the first book from the library grid and check its page.
await p.goto(`${BASE}/app#library`, { waitUntil: 'load' }); await p.waitForLoadState('networkidle');
// The library page writes itself only after its tag list arrives; wait for its own
// toolbar and either cards or the empty state, not merely for the skeleton to go.
await p.waitForSelector('#lib-tag', { timeout: 15000 }).catch(() => {});
await p.waitForFunction(() => document.querySelector('#lib-body .pl-book, #lib-body .pl-list-item, #lib-body .pl-empty'), null, { timeout: 15000 }).catch(() => {});
const first = await p.$('.pl-book');
if (!first) fails.push(`[${WIDTH}px/library] no book cards rendered`);
else {
  await first.click(); await p.waitForFunction(() => !document.querySelector('#pl-view .pl-skeleton') && document.querySelector('#pl-view h1'), null, { timeout: 15000 }).catch(() => {});
  const h1 = await p.textContent('#pl-view h1');
  if (!h1 || !h1.trim()) fails.push(`[${WIDTH}px/book] book page has no title`);
  const tap = await p.evaluate(() => [...document.querySelectorAll('#pl-view button, #pl-nav a')].map(el => el.getBoundingClientRect()).filter(r => r.width && r.height && r.height < 36).length);
  if (tap) fails.push(`[${WIDTH}px/book] ${tap} tap target(s) under 36px tall`);
}
// Management link appears only for an administrator (auth none = admin here).
await visit('settings');
const mgmt = await p.$('a[href="/manage"]');
if (!mgmt) fails.push(`[${WIDTH}px/settings] administrator sees no Management link`);

await b.close();
if (consoleErrors.length) fails.push(...consoleErrors.map(e => `[${WIDTH}px] console error: ${e}`));
if (failedRequests.length) fails.push(...failedRequests.map(e => `[${WIDTH}px] failed request: ${e}`));
if (fails.length) { console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-player ${WIDTH}px: Player routes render, fit and stay quiet`);
