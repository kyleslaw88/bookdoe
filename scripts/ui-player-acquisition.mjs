// ui-gate: widths=390 order=45
// Player acquisition flag: /api/v1/auth/me is intercepted to return acquisition_enabled false, then true.
// Off: no request button, "not yet in the library" state, Elsewhere results, My requests, pending-request
// workflow or approvals badge renders. On: the same screens show them. Run by ui-gate.mjs, or by hand with UI_BASE.
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const b = await chromium.launch();
const fails = [];
const SEARCH = {
  in_library: [{ id: 'w-hidden', title: 'Hidden Tale', authors: ['A. Writer'], owned_kinds: [], visible: false, has_cover: false }],
  external: [{ title: 'Faraway Book', authors: ['B. Author'], year: '2020' }],
};
const PENDING = { total: 1, items: [{ id: 'rq1', title: 'Wanted Title', author: 'C. Person', kinds: ['ebook'], requester: 'kid', requester_name: 'Kid', status: 'pending' }] };

const KIDS = { items: [{ id: 'k1', username: 'kid', display_name: 'Kid', grants: 2, pending_requests: 3 }] };

async function run(flag) {
  const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: 390, height: 760 } });
  const p = await ctx.newPage();
  await p.route('**/api/v1/auth/me', async r => {
    const res = await r.fetch(); const j = await res.json();
    await r.fulfill({ response: res, json: { ...j, acquisition_enabled: flag } });
  });
  await p.route('**/api/player/v1/requests/search*', r => r.fulfill({ json: SEARCH }));
  await p.route('**/api/player/v1/requests?*', r => r.fulfill({ json: { items: [{ id: 'r9', title: 'Mine Requested', kinds: ['ebook'], status: 'pending', friendly_status: 'Waiting' }] } }));
  await p.route('**/api/player/v1/approvals?status=pending*', r => r.fulfill({ json: PENDING }));
  let release; const held = new Promise(res => { release = res; });
  await p.route('**/api/player/v1/approvals/kids', async r => { await held; await r.fulfill({ json: KIDS }); });
  const has = async sel => (await p.locator(sel).count()) > 0;
  const text = async sel => (await p.locator(sel).first().textContent().catch(() => '')) || '';
  const check = (cond, msg) => { if (!cond) fails.push(`[acquisition_enabled=${flag}] ${msg}`); };
  const go = async (route, mark) => { await p.goto(`${BASE}/app#${route}`, { waitUntil: 'load' }); await p.waitForSelector(mark, { timeout: 20000 }); };

  await go('home', '.pl-section'); await p.waitForFunction(() => !document.querySelector('#pl-view .pl-skeleton'));
  check((await text('#pl-view')).includes('request a book') === flag, 'home "request a book" prompt');
  await p.waitForSelector('#pl-nav a[data-nav="approvals"]');
  await p.waitForTimeout(500);
  check((await has('#pl-nav .pl-badge')) === flag, 'approvals nav badge');

  await go('discover', '#disc-q');
  await p.fill('#disc-q', 'tale'); await p.waitForSelector('#disc-body .pl-result', { timeout: 10000 }).catch(() => {});
  await p.waitForTimeout(400);
  check((await has('.disc-req')) === flag, 'Request buttons');
  check((await has('#disc-mine')) === flag, 'My requests section');
  const body = await text('#disc-body');
  check(body.includes('Faraway Book') === flag, 'Elsewhere (external) results');
  check(body.includes('not yet in the library') === flag, '"not yet in the library" state');
  check(body.includes('Hidden Tale') === flag, 'results that can only be requested');

  await p.goto(`${BASE}/app#approvals`, { waitUntil: 'load' });
  await p.waitForSelector('#pl-view .pl-sub', { timeout: 20000 });
  check(!(await text('#pl-view .pl-sub')).includes('undefined'), 'approvals loading subtitle shows "undefined"');
  release();
  await p.waitForSelector('#ap-list, #pl-view .pl-list', { timeout: 20000 });
  await p.waitForFunction(() => !document.querySelector('#pl-view .pl-skeleton'));
  check((await has('#ap-list')) === flag, 'pending requests list');
  check((await text('#pl-view')).includes('3 waiting') === flag, 'kids row pending-request count');
  check((await has('.ap-approve')) === flag, 'request approve buttons');
  check((await has('#ap-done')) === flag, 'recently decided requests');
  check(((await text('#pl-view')).includes('Wanted Title')) === flag, 'pending request entry');
  await ctx.close();
}
await run(false);
await run(true);
await b.close();
if (fails.length) { console.error(fails.join('\n')); process.exit(1); }
console.log('ui-player-acquisition: ok (flag off hides, flag on shows)');
