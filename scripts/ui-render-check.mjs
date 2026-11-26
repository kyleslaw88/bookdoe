import { JSDOM } from 'jsdom';

// loopback QA target; override with UI_BASE (use https for anything remote)
const BASE = process.env.UI_BASE || 'http://127.0.0.1:8899'; // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
const fail = [], ok = [];
const check = (name, cond, detail='') => (cond ? ok : fail).push(name + (cond||!detail?'':` — ${detail}`));
const sleep = ms => new Promise(r => setTimeout(r, ms));

const html = await (await fetch(BASE + '/')).text(); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
const dom = new JSDOM(html, { url: BASE + '/', runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true });
const { window } = dom;

// jsdom has no fetch and does not execute type="module" scripts, so the page's
// own app.js is injected as a classic script over a fetch that reaches the real
// server. Everything below is the shipped UI code running against the real API.
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? (input.startsWith('http') ? input : BASE + input) : input.url;
  return fetch(url, init); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
};
window.Event = window.Event || Event;

const appjs = await (await fetch(BASE + '/assets/app.js')).text(); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
const el = window.document.createElement('script');
el.textContent = appjs;
window.document.body.appendChild(el);
await sleep(2000);

const $ = s => window.document.querySelector(s);
const $$ = s => [...window.document.querySelectorAll(s)];

check('app shell is visible (auth ok)', !$('#app-shell').classList.contains('hidden'));

window.location.hash = 'library';
await sleep(2000);

check('library renders one page of cards, not the whole library', $$('.book-card').length === 100, `got ${$$('.book-card').length}`);
check('header reports the real total, not the page size', /Showing 100 of 422 works/.test($('#library-count')?.textContent||''), $('#library-count')?.textContent);
check('a Load more control is offered', !!$('#library-load-more'));

const img = $('.book-card .cover-img');
check('each card requests its cover from bookdoe itself',
  !!img && /^\/api\/v1\/library\/[0-9a-f-]+\/cover$/.test(img.getAttribute('src')), img?.getAttribute('src'));
check('a title tile sits behind the cover as the fallback', !!$('.book-card .cover-fallback'));
check('no card points at a third-party image host',
  $$('.book-card img').every(i => (i.getAttribute('src')||'').startsWith('/api/v1/')));

$('#library-load-more').click();
await sleep(2000);
check('Load more appends the next page', $$('.book-card').length === 200, `got ${$$('.book-card').length}`);

const onPage = $$('.book-title').map(x => x.textContent);
check('precondition: Dead World is past the loaded pages', !onPage.includes('Dead World'));

const filter = $('#library-filter');
filter.value = 'dead world';
filter.dispatchEvent(new window.Event('input'));
await sleep(2200);
const results = $$('.book-title').map(x => x.textContent);
check('searching finds a work past the loaded pages', results.includes('Dead World'), JSON.stringify(results.slice(0,4)));
check('search narrows to just the match', results.length === 1, `got ${results.length}`);
check('search count reflects the filtered total', /1 works matching/.test($('#library-count').textContent), $('#library-count').textContent);

window.location.hash = 'dashboard';
await sleep(1500);
const gs = $('#global-search');
gs.value = 'All Systems Red';
gs.dispatchEvent(new window.Event('input'));
await sleep(2500);
check('global search lands on the library showing the match',
  $$('.book-title').map(x=>x.textContent).includes('All Systems Red'),
  JSON.stringify($$('.book-title').map(x=>x.textContent).slice(0,4)));

window.location.hash = 'authors';
await sleep(2000);
const rows = $$('.author-row');
check('authors page shows one row per author, not one per book', rows.length === 2, `got ${rows.length}`);
const alanson = rows.map(r => [...r.children].map(c => c.textContent.trim())).find(c => c[0] === 'Craig Alanson');
check('author work count is the real number of works', alanson?.[1] === '421', JSON.stringify(alanson));
check('authors page offers a filter', !!$('#author-filter'));

const deadId = (await (await fetch(BASE + '/api/v1/library?q=Dead World')).json()).items[0].id; // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
window.location.hash = 'library/' + deadId;
await sleep(2000);
check('work detail renders the cover from bookdoe',
  $('.detail-cover .cover-img')?.getAttribute('src') === `/api/v1/library/${deadId}/cover`,
  $('.detail-cover')?.innerHTML?.slice(0,120));

$('#refresh-work').click();
await sleep(3000);
const toastText = $$('.toast').map(t => t.textContent).join(' | ');
const modalText = $('.modal')?.textContent || '';
check('refresh no longer answers with the "handled elsewhere" message',
  !/handled by the scheduled OpenReads sync/.test(toastText + modalText), toastText);
check('refresh reports a real outcome', !!(toastText.trim() || modalText.trim()), `toast=${toastText} modal=${modalText.slice(0,80)}`);

console.log('PASS:');
ok.forEach(x => console.log('  ✓ ' + x));
if (fail.length) { console.log('\nFAIL:'); fail.forEach(x => console.log('  ✗ ' + x)); }
console.log(`\n${ok.length} passed, ${fail.length} failed`);
window.close();
process.exit(fail.length ? 1 : 0);
