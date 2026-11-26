// ui-gate: widths=360,390 order=30
// The Management drawer on a short phone viewport: the nav list must scroll so the last
// entries (Settings, under SYSTEM) are reachable and clickable; the header and the
// bottom row stay pinned; the page behind must not scroll while the drawer is open.
// Also: a long dialog fits the viewport and scrolls inside itself. Width from UI_WIDTH;
// the height is deliberately short (UI_HEIGHT, default 560) to force overflow, standing
// in for Safari's toolbar eating the bottom of 100vh.
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const WIDTH = Number(process.env.UI_WIDTH) || 390, HEIGHT = Number(process.env.UI_HEIGHT) || 560;
const b = await chromium.launch();
const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: WIDTH, height: HEIGHT }, hasTouch: true });
const p = await ctx.newPage();
const fails = []; const ok = (c, m) => { if (!c) fails.push(`[${WIDTH}x${HEIGHT}] ${m}`); };

await p.goto(`${BASE}/manage#dashboard`, { waitUntil: 'load' }); await p.waitForSelector('#nav a', { timeout: 20000 });
// Make the page itself scrollable so bleed would be observable.
await p.evaluate(() => { const v = document.querySelector('#view'); if (v) v.style.minHeight = '3000px'; window.scrollTo(0, 300); });
const y0 = await p.evaluate(() => window.scrollY);
await p.click('#menu-toggle'); await p.waitForTimeout(400);

const geo = await p.evaluate(() => {
  const nav = document.querySelector('#nav'), sb = document.querySelector('#sidebar'), last = [...document.querySelectorAll('#nav a')].pop();
  const cs = getComputedStyle(nav), r = last.getBoundingClientRect();
  return { overflows: nav.scrollHeight > nav.clientHeight, overflowY: cs.overflowY, sidebarBottom: sb.getBoundingClientRect().bottom, lastBottomBefore: r.bottom, lastRoute: last.dataset.route, innerHeight };
});
ok(geo.overflows, `nav does not overflow at this height (scrollHeight <= clientHeight), the check proves nothing: ${JSON.stringify(geo)}`);
ok(geo.overflowY === 'auto' || geo.overflowY === 'scroll', `nav is not scrollable (overflow-y: ${geo.overflowY})`);
ok(Math.abs(geo.sidebarBottom - geo.innerHeight) < 2, `drawer does not end at the viewport bottom (${geo.sidebarBottom} vs ${geo.innerHeight})`);
ok(geo.lastBottomBefore > geo.innerHeight, 'the last entry is already visible, nothing to scroll');

// Scroll the list the way a thumb would: wheel/touch over the nav, then check.
await p.hover('#nav a[data-route="library"]');
for (let i = 0; i < 12; i++) await p.mouse.wheel(0, 200);
await p.waitForTimeout(300);
const after = await p.evaluate(() => {
  const nav = document.querySelector('#nav'), last = [...document.querySelectorAll('#nav a')].pop(), r = last.getBoundingClientRect();
  const mid = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const bottom = document.querySelector('.sidebar-bottom').getBoundingClientRect();
  return { scrollTop: nav.scrollTop, lastTop: r.top, lastBottom: r.bottom, innerHeight, onTop: mid === last || last.contains(mid), bottomRowVisible: bottom.bottom <= innerHeight + 1 && bottom.height > 0, pageY: window.scrollY, brandTop: document.querySelector('.brand').getBoundingClientRect().top };
});
ok(after.scrollTop > 0, 'wheel over the nav did not scroll the list');
ok(after.lastTop >= 0 && after.lastBottom <= after.innerHeight, `last entry (${geo.lastRoute}) not fully visible after scrolling: top ${after.lastTop} bottom ${after.lastBottom} of ${after.innerHeight}`);
ok(after.onTop, 'last entry is covered by something else (not clickable)');
ok(after.bottomRowVisible, 'the pinned bottom row (health/theme) is off screen');
ok(after.brandTop >= 0 && after.brandTop < 40, `header did not stay pinned (top ${after.brandTop})`);
ok(after.pageY === y0, `page behind the drawer scrolled (${y0} -> ${after.pageY})`);
// Scrolling past the end must not move the page behind (overscroll containment).
for (let i = 0; i < 6; i++) await p.mouse.wheel(0, 400);
await p.waitForTimeout(200);
ok((await p.evaluate(() => window.scrollY)) === y0, 'over-scrolling the list moved the page behind the drawer');
// Click the last entry: route changes and the drawer closes.
await p.evaluate(() => { const nav = document.querySelector('#nav'); nav.scrollTop = nav.scrollHeight; });
await p.waitForTimeout(200);
// A real pointer click at the entry's on-screen position (not Playwright's scroll-into-
// view helper, which does not understand a fixed drawer with its own scroll box).
const lastBox = await p.$eval(`#nav a[data-route="${geo.lastRoute}"]`, a => a.getBoundingClientRect().toJSON());
ok(lastBox.top >= 0 && lastBox.bottom <= HEIGHT, `last entry off screen before the click: ${JSON.stringify(lastBox)}`);
await p.mouse.click(lastBox.x + lastBox.width / 2, lastBox.y + lastBox.height / 2); await p.waitForTimeout(800);
const routed = await p.evaluate(() => ({ hash: location.hash, open: document.querySelector('#sidebar').classList.contains('open'), navOpenClass: document.documentElement.classList.contains('nav-open') }));
ok(routed.hash === `#${geo.lastRoute}`, `clicking the last entry did not navigate (${routed.hash})`);
ok(!routed.open && !routed.navOpenClass, 'drawer stayed open after navigating');

// A long dialog: Settings -> Accounts -> Add account at this short height.
await p.goto(`${BASE}/manage#settings`, { waitUntil: 'load' }); await p.waitForSelector('[data-stab="users"]', { timeout: 20000 }).catch(() => {});
if (await p.$('[data-stab="users"]')) { await p.waitForSelector('#settings-body > *', { timeout: 20000 }).catch(() => {}); await p.click('[data-stab="users"]'); await p.waitForSelector('#user-add', { timeout: 20000 }).catch(() => {}); }
if (await p.$('#user-add')) {
  // Settings re-renders once its tab data arrives; click until the dialog is really open.
  for (let i = 0; i < 5 && !(await p.$('.modal')); i++) { await p.waitForTimeout(400); await p.click('#user-add').catch(() => {}); await p.waitForTimeout(300); }
  await p.waitForSelector('.modal', { timeout: 10000 });
  const m = await p.evaluate(() => { const m = document.querySelector('.modal'), r = m.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, innerHeight, overflow: getComputedStyle(m).overflowY, scrolls: m.scrollHeight > m.clientHeight, maxH: getComputedStyle(m).maxHeight }; });
  ok(m.top >= 0 && m.bottom <= m.innerHeight + 1, `dialog does not fit the viewport (top ${m.top}, bottom ${m.bottom} of ${m.innerHeight}, max-height ${m.maxH})`);
  ok(m.overflow === 'auto' || m.overflow === 'scroll', `dialog is not scrollable (overflow-y ${m.overflow})`);
} else fails.push(`[${WIDTH}x${HEIGHT}] could not open the Accounts settings to test a dialog`);

await b.close();
if (fails.length) { console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-drawer-scroll ${WIDTH}x${HEIGHT}: drawer scrolls to the last entry, page stays put, dialog fits`);
