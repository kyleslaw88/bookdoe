// ui-gate: widths=360,390,1280 order=50
// Build indicator: Management shows the sha in the nav drawer footer and in Settings > About;
// the Player's Settings shows "App version <sha> (server <sha>)" with a check when equal, and a
// visible "Update available" row (with the Update now button) when the server's sha differs
// from the page's own. Nothing may overflow. Width from UI_WIDTH (default 390).
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const WIDTH = Number(process.env.UI_WIDTH) || 390;
const b = await chromium.launch();
const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: WIDTH, height: 760 }, serviceWorkers: 'block' });
const p = await ctx.newPage();
const fails = [];
const ok = (c, m) => { if (!c) fails.push(`[${WIDTH}px] ${m}`); };
p.on('pageerror', e => fails.push(`[${WIDTH}px] page error: ${e}`));
const fit = async what => {
  const m = await p.evaluate(() => ({ s: document.documentElement.scrollWidth, c: document.documentElement.clientWidth }));
  ok(m.s <= m.c + 2, `${what}: horizontal overflow ${m.s} > ${m.c}`);
};
const inView = async (sel, what) => {
  const r = await p.locator(sel).first().boundingBox({ timeout: 5000 }).catch(() => null);
  ok(r && r.x >= -1 && r.x + r.width <= WIDTH + 1, `${what} is not inside the ${WIDTH}px viewport (${JSON.stringify(r)})`);
};

// ---- Management ----
await p.goto(`${BASE}/manage#settings`, { waitUntil: 'load' });
await p.waitForSelector('[data-stab]', { timeout: 20000 });
if (WIDTH < 900) { await p.click('#menu-toggle'); await p.waitForTimeout(400); }
// The chip exists with a placeholder ("bookdoe …") until /api/v1/version answers.
await p.waitForFunction(() => /built /.test(document.querySelector('#build-chip-text')?.textContent || ''), null, { timeout: 15000 }).catch(() => {});
const chip = (await p.locator('#build-chip-text').textContent({ timeout: 15000 })) || '';
ok(/^bookdoe dev · built /.test(chip), `drawer footer shows the build (got "${chip}")`);
await inView('#build-chip', 'drawer build chip');
if (WIDTH < 900) { await p.click('#nav-close').catch(() => {}); await p.waitForTimeout(300); }
await p.waitForSelector('#settings-body > *', { timeout: 20000 });
await p.click('[data-stab="about"]');
await p.waitForSelector('#build-line-text', { timeout: 15000 });
const line = (await p.locator('#build-line-text').textContent()) || '';
ok(/^bookdoe dev · built .+ · up since .+; database step \d+$/.test(line), `About line (got "${line}")`);
ok(/matches the server/.test((await p.locator('#build-client').textContent()) || ''), 'About says the web app matches the server');
ok(await p.locator('#build-copy').isVisible(), 'About has a Copy button');
await inView('#build-copy', 'About copy button');
await fit('management about');

// ---- Player: matching build ----
await p.goto(`${BASE}/app#settings`, { waitUntil: 'load' });
await p.waitForSelector('#pl-build-version', { timeout: 20000 });
await p.waitForFunction(() => /\(server dev\)/.test(document.querySelector('#pl-build-version')?.textContent || ''), null, { timeout: 15000 }).catch(() => {});
const ver = (await p.locator('#pl-build-version').textContent()) || '';
ok(/^dev \(server dev\)$/.test(ver), `Player version row (got "${ver}")`);
ok(await p.locator('.pl-build-ok').isVisible(), 'green check when client and server match');
ok(await p.locator('#pl-update-row').count() === 0, 'no update row when the builds match');
await fit('player settings (match)');

// ---- Player: the server moved on ----
await p.route('**/api/v1/version', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sha: 'zzzzzzzzzzzz', built_at: '2026-10-14T10:00Z' }) }));
await p.reload({ waitUntil: 'load' });   // same URL: goto() would be a same-document hash navigation
await p.waitForSelector('#pl-update-row', { timeout: 20000 }).catch(() => fails.push(`[${WIDTH}px] mismatch did not show the Update available row`));
const row = (await p.locator('#pl-update-row').textContent().catch(() => '')) || '';
ok(/Update available/.test(row) && /dev -> zzzzzzzzzzzz/.test(row), `update row text (got "${row.trim()}")`);
ok(await p.locator('#pl-update-now').isVisible(), 'Update now button visible');
ok(await p.locator('.pl-build-ok').count() === 0, 'no green check on a mismatch');
ok(await p.locator('#pl-update').isVisible(), 'floating update notice visible');
await inView('#pl-update-now', 'Update now button');
await inView('#pl-update-bar-now', 'notice Update now button');
await fit('player settings (mismatch)');
await b.close();
if (fails.length) { console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-build ${WIDTH}px: about, version row and update row fit`);
