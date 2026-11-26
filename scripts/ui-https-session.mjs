// Real-browser check over TLS (self-signed certificate, ignoreHTTPSErrors) that the
// __Host- cookies really land: a family-account sign-in with "keep me signed in" leaves a
// working session, a reload stays signed in, trusting the device and losing the session
// cookie still signs in through the __Host- device cookie, and signing out clears both.
// The raw Set-Cookie order is covered by bookdoe/tests/test_remember_and_trust.py; this
// proves the browser accepts what the server sends. Env: UI_BASE (https), UI_SEED.
import { chromium } from 'playwright';
const BASE = process.env.UI_BASE; const SEED = JSON.parse(process.env.UI_SEED || '{}');
const fails = []; const ok = (c, m) => { if (!c) fails.push(m); };
const b = await chromium.launch();
const ctx = await b.newContext({ ignoreHTTPSErrors: true, viewport: { width: 390, height: 760 } });
const p = await ctx.newPage();
let step = 'start';
p.on('response', async r => { if (r.status() === 403) { const t = await r.text().catch(() => ''); if (!t.includes('CSRF')) fails.push(`HTTP 403 ${r.request().method()} ${new URL(r.url()).pathname} ${t}`); } });
const jar = async () => Object.fromEntries((await ctx.cookies()).map(c => [c.name, c]));
try {
  await p.goto(`${BASE}/app`, { waitUntil: 'load' });
  step = 'step0';
  await p.waitForSelector('#login-form', { timeout: 20000 });
  await p.fill('#login-form [name=username]', 'jane'); await p.fill('#login-form [name=password]', SEED.password);
  await p.check('#login-form input[name=remember]'); await p.click('#login-form button');
  step = 'step1';
  await p.waitForSelector('#pl-app:not(.hidden)', { timeout: 20000 });
  let c = await jar();
  const s = c['__Host-bookdoe_session'];
  ok(s && s.secure && s.httpOnly && s.path === '/' && s.expires - Date.now() / 1000 > 80 * 86400, `session cookie missing or wrong over https: ${JSON.stringify(s && { ...s, value: '' })}`);
  ok(!c['bookdoe_session'], 'an unprefixed session cookie exists over https');
  await p.reload({ waitUntil: 'load' });
  await p.waitForSelector('#pl-app:not(.hidden)', { timeout: 20000 });
  ok(!(await p.$('#login-form')), 'reload landed on the sign-in gate');
  // CSRF refresh-and-retry must not repeat an action as a different account: another person signs in
  // on this browser while the page still holds the first person's CSRF token.
  step = 'csrf-other-account';
  await p.goto(`${BASE}/app#library/${SEED.works['Readable Kids Book']}`, { waitUntil: 'load' });
  await p.waitForSelector('#fav', { timeout: 20000 });
  let puts = 0; p.on('request', r => { if (r.method() === 'PUT' && r.url().includes('/favorites/')) puts++; });
  await p.evaluate(async pw => { await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'owen', password: pw }) }); }, SEED.password);
  await p.click('#fav');
  await p.waitForFunction(() => /different account/.test(document.querySelector('#pl-toast')?.textContent || ''), null, { timeout: 10000 });
  ok(puts === 1, `the favorite action was repeated after another account signed in (${puts} PUTs)`);
  await p.evaluate(async pw => { await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'jane', password: pw }) }); }, SEED.password);
  await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
  step = 'step2';
  await p.waitForSelector('#trust-yes', { timeout: 15000 }); await p.click('#trust-yes');
  step = 'step3';
  await p.waitForFunction(() => document.querySelector('#pl-toast')?.textContent.includes('trusted'), null, { timeout: 10000 });
  c = await jar();
  ok(c['__Host-bookdoe_device'] && c['__Host-bookdoe_device'].secure && c['__Host-bookdoe_device'].httpOnly, 'device cookie missing or wrong over https');
  const rest = (await ctx.cookies()).filter(x => !x.name.endsWith('bookdoe_session'));
  await ctx.clearCookies(); await ctx.addCookies(rest);
  await p.goto(`${BASE}/app#home`, { waitUntil: 'load' });
  await p.waitForSelector('#pl-app:not(.hidden)', { timeout: 20000 });
  ok(await p.evaluate(async () => (await fetch('/api/player/v1/home')).status) === 200, 'Player home failed on the device cookie over https');
  step = 'step4';
  await p.goto(`${BASE}/app#settings`, { waitUntil: 'load' });
  await p.waitForSelector('#signout', { timeout: 15000 }); await p.click('#signout');
  step = 'step5';
  await p.waitForSelector('#login-form', { timeout: 20000 });
  c = await jar();
  ok(!c['__Host-bookdoe_session'] && !c['__Host-bookdoe_device'], `sign-out left cookies behind: ${Object.keys(c)}`);
} catch (er) { fails.push(`crashed at ${step}: ${String(er.message || er).split('\n')[0]}`); }
await b.close();
if (fails.length) { console.log(fails.map(f => `[https] ${f}`).join('\n')); process.exit(1); }
console.log('ui-https-session: __Host- session and device cookies land, survive reload, and are cleared on sign-out over TLS');
