// Real-browser check of the Player's Discover chat against a seeded local-auth instance whose LLM
// and metadata provider are stubbed (scripts/ui-discover-stub.mjs): the interview (ask, a follow-up,
// then cards), library cards before not-in-library cards, Request through the existing flow,
// thumbs, "remember that?", the taste-profile panel, Start over, the plain Search tab, the kid view
// (transparency notice, nothing hidden or adult), an approver reading the kid's chat, and the chat
// disappearing when an administrator turns it off. Env: UI_BASE, UI_WIDTH (360/390/1280).
// Safe to run more than once against the same instance: it tolerates what an earlier run left.
import { chromium, devices } from 'playwright';
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899';
const W = Number(process.env.UI_WIDTH || 390);
const PW = 'ui-e2e-password-long-enough';
const fails = [];
const ok = (cond, msg) => { if (!cond) fails.push(`[${W}] ${msg}`); };
const b = await chromium.launch();
let lastPage = null;

async function session(username) {
  const ctx = await b.newContext({ ...devices['iPhone 13'], viewport: { width: W, height: W >= 1000 ? 900 : 780 }, serviceWorkers: 'block' });
  const p = await ctx.newPage(); lastPage = p;
  p.on('console', m => { if (m.type() === 'error' && !/401|403/.test(m.text())) fails.push(`[${W}/${username}] console error: ${m.text().slice(0, 200)}`); });
  p.on('pageerror', e => fails.push(`[${W}/${username}] page error: ${e}`));
  p.on('dialog', d => d.accept());
  await p.goto(`${BASE}/app`, { waitUntil: 'load' });
  const res = await p.evaluate(async ({ u, pw }) => { const r = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: pw }) }); return r.status; }, { u: username, pw: PW });
  if (res !== 200) throw new Error(`login ${username} -> ${res}`);
  await p.reload({ waitUntil: 'load' });
  return { ctx, p };
}
const overflow = p => p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
const bubbleCount = p => p.locator('#chat-thread .pl-bubble.ai[data-id]:not(.pending)').count();
async function ask(p, text) {
  const before = await bubbleCount(p);
  await p.fill('#chat-q', text); await p.click('#chat-send');
  return before;
}
async function answered(p, before) {
  await p.waitForFunction(n => document.querySelectorAll('#chat-thread .pl-bubble.ai[data-id]:not(.pending)').length > n && !document.querySelector('.pl-bubble.pending'), before, { timeout: 30000 });
}

try {
  // ------------------------------------------------------------ an adult
  const { ctx, p } = await session('jane');
  await p.goto(`${BASE}/app#discover`, { waitUntil: 'load' });
  await p.waitForSelector('#disc-tabs', { timeout: 20000 });
  ok(await p.locator('#disc-tabs button').count() === 2, 'Ask and Search tabs are not both there');
  await p.waitForSelector('#chat-q');
  ok((await p.textContent('#chat-notice')).includes('I\'m an AI'), 'the AI/can-be-wrong notice is missing');
  ok(!(await p.textContent('#chat-notice')).includes('parents'), 'an adult is told parents can see the chat');
  ok(await p.locator('.chat-starter').count() >= 4, 'no starter chips');
  ok(await overflow(p) <= 0, `Ask tab overflows horizontally by ${await overflow(p)}px`);
  // Start over, then a starter chip: the thinking state shows with elapsed time and Cancel, then a follow-up.
  await p.click('#chat-new');
  const b0 = await bubbleCount(p);
  await p.click('.chat-starter >> nth=0');
  await p.waitForSelector('.pl-bubble.pending .chat-secs', { timeout: 10000 });
  ok(await p.locator('.chat-cancel').count() === 1, 'no Cancel button while thinking');
  ok(await p.isDisabled('#chat-send'), 'Send is not disabled while the assistant is thinking');
  await answered(p, b0);
  ok((await p.textContent('.pl-follow')).includes('Ebook or audiobook'), 'the follow-up question did not appear');
  ok(await p.locator('.pl-rec').count() === 0, 'cards appeared before the interview finished');
  // Answer: cards.
  const b1 = await ask(p, 'audiobook please, I liked Pratchett');
  await answered(p, b1);
  const heads = await p.locator('.pl-rec-head').allTextContents();
  ok(heads[0] === 'In your library' && heads[1] === 'Not in your library', `card groups wrong: ${heads}`);
  const titles = await p.locator('.pl-rec strong').allTextContents();
  ok(titles.includes('Plain Novel') && titles.includes('Stub External Tale') && titles.includes('Stub Adult Thriller'), `cards missing: ${titles}`);
  ok(!titles.some(t => /Phantom/.test(t)), 'a hallucinated title was shown');
  ok(titles.indexOf('Plain Novel') < titles.indexOf('Stub External Tale'), 'library card is not first');
  ok(await p.locator('.pl-rec[data-group="library"] .rec-open').count() >= 1, 'library card has no Read/Listen button');
  ok(await p.locator('.pl-rec[data-group="elsewhere"] .rec-request, .pl-rec[data-group="elsewhere"] .rec-status').count() === 2, 'external cards lack Request / status');
  ok(await overflow(p) <= 0, `cards overflow horizontally by ${await overflow(p)}px`);
  for (const t of await p.locator('.rec-fb, .rec-request, .rec-open').all()) { const box = await t.boundingBox(); if (box) ok(box.height >= 36 && box.width >= 36, `tap target too small: ${box.width}x${box.height}`); }
  // Request through the existing dialog.
  const req = p.locator('.pl-rec:has-text("Stub External Tale") .rec-request');
  if (await req.count()) {
    await req.click(); await p.waitForSelector('#rq-go'); await p.click('#rq-go');
    await p.waitForSelector('.pl-rec:has-text("Stub External Tale") .rec-status', { timeout: 10000 });
  }
  ok(await p.locator('.pl-rec:has-text("Stub External Tale") .rec-status').count() === 1, 'the requested card does not show its status');
  // Thumbs.
  const up = p.locator('.pl-rec:has-text("Plain Novel") .rec-fb[data-v="up"]');
  await up.click(); await p.waitForFunction(() => document.querySelector('.rec-fb[data-v="up"].on'), null, { timeout: 5000 });
  ok(await up.getAttribute('aria-pressed') === 'true', 'thumbs up is not pressed');
  // Remember?
  ok(await p.locator('.pl-remember').count() === 1, 'no "shall I remember" prompt');
  await p.click('.chat-remember[data-v="1"]'); await p.waitForFunction(() => !document.querySelector('.pl-remember'), null, { timeout: 5000 });
  // Profile panel.
  await p.click('#chat-profile'); await p.waitForSelector('#prof-done');
  const panel = await p.textContent('.pl-modal-body');
  ok(panel.includes('cozy stories') && panel.includes('Plain Novel'), `profile panel lacks what was remembered: ${panel.slice(0, 200)}`);
  ok(await overflow(p) <= 0, 'profile panel overflows');
  await p.click('.pl-tag-x[data-k="genres"]'); await p.waitForFunction(() => !document.querySelector('.pl-modal-body')?.textContent.includes('cozy stories'), null, { timeout: 5000 });
  await p.click('#prof-clear'); await p.waitForFunction(() => document.querySelectorAll('.pl-tag').length === 0, null, { timeout: 5000 });
  await p.click('#prof-done');
  // The thread survives a reload; Start over empties it.
  await p.reload({ waitUntil: 'load' }); await p.waitForSelector('#chat-q');
  await p.waitForSelector('.pl-rec', { timeout: 10000 }).catch(() => {});
  ok(await p.locator('.pl-rec').count() >= 3, 'the conversation did not come back after a reload');
  await p.click('#chat-new');
  ok(await p.locator('.pl-rec').count() === 0 && await p.locator('.chat-starter').first().isVisible(), 'Start over did not clear the thread');
  // Search tab keeps working.
  await p.click('#disc-tabs button[data-tab="search"]'); await p.waitForSelector('#disc-q');
  await p.fill('#disc-q', 'Plain'); await p.waitForSelector('.pl-result', { timeout: 15000 });
  ok(await p.locator('.pl-result:has-text("Plain Novel")').count() >= 1, 'plain search lost its results');
  // Settings shows the profile entry point.
  await p.goto(`${BASE}/app#settings`, { waitUntil: 'load' }); await p.waitForSelector('#prof-open', { timeout: 15000 });
  await ctx.close();

  // ------------------------------------------------------------ a kid
  const kid = await session('owen');
  const k = kid.p;
  await k.goto(`${BASE}/app#discover`, { waitUntil: 'load' });
  await k.waitForSelector('#chat-q', { timeout: 20000 });
  ok((await k.textContent('#chat-notice')).includes('Your parents can see what you ask here'), 'the kid is not told parents can see the chat');
  await k.click('#chat-new');
  const kb = await ask(k, 'an exciting adventure about animals');
  await answered(k, kb);
  const kt = await k.locator('.pl-rec strong').allTextContents();
  ok(kt.includes('Readable Kids Book') && kt.includes('Stub Kids Adventure'), `kid cards missing: ${kt}`);
  ok(!kt.some(t => /Plain Novel|Adult Thriller|Phantom/.test(t)), `kid saw a hidden, adult or invented title: ${kt}`);
  ok(await overflow(k) <= 0, 'kid chat overflows');
  const kreq = k.locator('.pl-rec:has-text("Stub Kids Adventure") .rec-request');
  if (await kreq.count()) {
    await kreq.click(); await k.waitForSelector('#rq-go'); await k.click('#rq-go');
    await k.waitForSelector('.pl-rec:has-text("Stub Kids Adventure") .rec-status', { timeout: 10000 });
    ok((await k.textContent('.pl-rec:has-text("Stub Kids Adventure") .rec-status')).includes('Awaiting Approval'), 'a kid request is not awaiting approval');
  }
  const kb2 = await ask(k, 'show me erotic stories');
  await answered(k, kb2);
  ok((await k.locator('#chat-thread .pl-bubble.ai').last().textContent()).includes('kid-friendly'), 'an adult-content request from a kid was not redirected');
  await k.goto(`${BASE}/app#settings`, { waitUntil: 'load' }); await k.waitForSelector('#prof-open', { timeout: 15000 });
  ok((await k.textContent('#pl-profile')).includes('Your parents can see this'), 'the kid profile card does not say parents can see it');
  await kid.ctx.close();

  // ------------------------------------------------------------ an administrator reads the kid's chat, then turns the chat off
  const adm = await session('ada');
  const a = adm.p;
  await a.goto(`${BASE}/app#approvals`, { waitUntil: 'load' });
  await a.waitForSelector('.pl-list-item', { timeout: 20000 });
  await a.click('.pl-list-item:has-text("owen")');
  await a.waitForSelector('#ka-chats .pl-request', { timeout: 20000 });
  const chatsText = await a.textContent('#ka-chats');
  ok(/blocked/.test(chatsText) && /dropped/.test(chatsText), `the kid's chats do not show blocked/dropped counts: ${chatsText.slice(0, 200)}`);
  await a.click('#ka-chats .ka-chat >> nth=0'); await a.waitForSelector('.pl-modal .pl-bubble');
  ok((await a.textContent('.pl-modal-body')).length > 20, 'approver chat view is empty');
  ok(await overflow(a) <= 0, 'approver chat view overflows');
  await a.keyboard.press('Escape'); await a.evaluate(() => document.querySelector('.pl-modal-close')?.click());
  const off = await a.evaluate(async () => {
    const me = await (await fetch('/api/v1/auth/me')).json();
    const r = await fetch('/api/v1/settings/discover', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf }, body: JSON.stringify({ enabled: false }) });
    return r.status;
  });
  ok(off === 200, `could not turn the chat off: ${off}`);
  const s2 = await session('jane');
  await s2.p.goto(`${BASE}/app#discover`, { waitUntil: 'load' });
  await s2.p.waitForSelector('#disc-q', { timeout: 20000 });
  ok(await s2.p.locator('#disc-tabs').count() === 0 && await s2.p.locator('#chat-q').count() === 0, 'the chat is still offered after being turned off');
  await s2.ctx.close();
  await a.evaluate(async () => { const me = await (await fetch('/api/v1/auth/me')).json(); await fetch('/api/v1/settings/discover', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf }, body: JSON.stringify({ enabled: true }) }); });
  await adm.ctx.close();
} catch (err) {
  try { fails.push(`[${W}] page text: ${(await lastPage.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ').slice(0, 300)}`); } catch {}
  fails.push(`[${W}] crashed: ${String(err && err.stack || err).split('\n').slice(0, 3).join(' | ')}`);
}
await b.close();
if (fails.length) { console.error(`ui-discover-chat FAILED:\n  ${fails.join('\n  ')}`); process.exit(1); }
console.log(`ui-discover-chat ok at ${W}px`);
