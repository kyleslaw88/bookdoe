import { chromium, devices } from 'playwright';
const BASE='http://127.0.0.1:8899', OUT='/tmp/qa/shots';
const fails=[], ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(n+(c||!d?'':` — ${d}`));

const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13']});
const p=await ctx.newPage();
const errs=[]; p.on('pageerror',e=>errs.push(e.message));

// ---------- 1. find a book and open it ----------
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await p.waitForTimeout(2000);
await p.fill('#library-filter','dead world'); await p.waitForTimeout(2200);
const titles=await p.$$eval('.book-title',n=>n.map(x=>x.textContent));
chk('search finds Dead World on mobile', titles.includes('Dead World'), JSON.stringify(titles.slice(0,3)));
await p.click('.book-card'); await p.waitForTimeout(2200);
const detail=await p.evaluate(()=>({title:document.querySelector('.detail-meta h1')?.textContent?.trim(),
  assets:document.querySelectorAll('.asset-row').length, files:document.querySelectorAll('.files div').length,
  overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+2}));
chk('book detail opens with both formats', detail.assets===2, JSON.stringify(detail));
chk('owned files are listed', detail.files>=2, `${detail.files} file rows`);
chk('detail page does not scroll sideways', !detail.overflow);
await p.screenshot({path:`${OUT}/flow-detail.png`});

// ---------- 2. refresh metadata -> candidate picker ----------
await p.click('#refresh-work'); await p.waitForTimeout(6000);
const modal=await p.evaluate(()=>{const m=document.querySelector('.modal');
  return m?{present:true,picks:m.querySelectorAll('.pick-meta').length,w:Math.round(m.getBoundingClientRect().width),
    overflows:m.getBoundingClientRect().right>window.innerWidth+2}:{present:false,toast:[...document.querySelectorAll('.toast')].map(t=>t.textContent).join('|')};});
chk('refresh metadata returns a usable result', modal.present||!!modal.toast, JSON.stringify(modal));
if(modal.present){ chk('candidate picker offers choices', modal.picks>0, JSON.stringify(modal));
  chk('picker fits the phone screen', !modal.overflows, JSON.stringify(modal));
  await p.screenshot({path:`${OUT}/flow-candidates.png`});
  await p.click('.modal-close'); await p.waitForTimeout(600); }

// ---------- 3. load more ----------
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await p.waitForTimeout(2200);
await p.fill('#library-filter',''); await p.waitForTimeout(2500);
const before=await p.$$eval('.book-card',n=>n.length);
await p.click('#library-load-more'); await p.waitForTimeout(2500);
const after=await p.$$eval('.book-card',n=>n.length);
chk('load more appends a page', after===before*2, `${before} -> ${after}`);

// ---------- 4. authors ----------
await p.goto(`${BASE}/manage#authors`,{waitUntil:'load'}); await p.waitForTimeout(2500);
await p.fill('#author-filter','alanson'); await p.waitForTimeout(600);
const visible=await p.$$eval('.author-row',rows=>rows.filter(r=>r.style.display!=='none').length);
chk('author filter narrows the list', visible===1, `${visible} rows visible`);
await p.click('.author-row:not([style*="none"])'); await p.waitForTimeout(2500);
const auth=await p.evaluate(()=>({h:document.querySelector('.page-head h1')?.textContent?.trim(),
  works:document.querySelectorAll('.book-card').length}));
chk('author page lists that author\'s works', auth.works>0, JSON.stringify(auth));
await p.screenshot({path:`${OUT}/flow-author.png`});

// ---------- 5. discover ----------
await p.goto(`${BASE}/manage#discover`,{waitUntil:'load'}); await p.waitForTimeout(1800);
await p.fill('#discover-q','project hail mary');
await p.press('#discover-q','Enter'); await p.waitForTimeout(6000);
const disc=await p.evaluate(()=>({rows:document.querySelectorAll('#discover-results tbody tr').length,
  err:document.querySelector('#discover-results .warning-box')?.textContent?.trim()?.slice(0,80),
  overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+2}));
chk('discover returns results', disc.rows>0, JSON.stringify(disc));
chk('discover results table does not widen the page', !disc.overflow, JSON.stringify(disc));
await p.screenshot({path:`${OUT}/flow-discover.png`});

// ---------- 6. review, and act on an item ----------
await p.goto(`${BASE}/manage#review`,{waitUntil:'load'}); await p.waitForTimeout(2800);
const rev=await p.evaluate(()=>{const cards=document.querySelectorAll('.review-card');
  return {cards:cards.length, finds:document.querySelectorAll('.review-find').length,
    bulk:!!document.querySelector('#review-find-all'), h:Math.round(document.body.scrollHeight),
    perCard: cards.length?Math.round(document.body.scrollHeight/cards.length):0,
    rawVisible:[...document.querySelectorAll('.review-card .code')].some(c=>!c.classList.contains('hidden')),
    showsAuthor:!!document.querySelector('.review-source')};});
chk('review is paged, not the whole queue', rev.cards<=50, `${rev.cards} cards`);
chk('raw JSON is hidden behind a toggle', !rev.rawVisible, JSON.stringify(rev));
chk('each item shows its author/identifiers readably', rev.showsAuthor);
chk('cards are a sane height', rev.perCard<320, `${rev.perCard}px per card`);
chk('every item offers a direct action', rev.finds===rev.cards, JSON.stringify(rev));
chk('a bulk action is offered for the loaded page', rev.bulk);
await p.screenshot({path:`${OUT}/flow-review.png`});

// actually drive it
// Check before the toast auto-dismisses (4.2s).
await p.click('.review-find'); await p.waitForTimeout(2500);
const acted=await p.evaluate(()=>({picks:document.querySelectorAll('.review-pick').length,
  toast:[...document.querySelectorAll('.toast')].map(t=>t.textContent).join('|'),
  cards:document.querySelectorAll('.review-card').length}));
chk('Find match produces candidates or an honest answer',
  acted.picks>0 || /no match|linked|matched/i.test(acted.toast), JSON.stringify(acted));
const warnStyled=await p.evaluate(()=>{const t=document.querySelector('.toast.warn');
  if(!t) return 'no warn toast shown';
  const cs=getComputedStyle(t); return cs.borderColor;});
chk('a "no match" toast is visually distinct from success', warnStyled!=='no warn toast shown', String(warnStyled));
await p.screenshot({path:`${OUT}/flow-review-acted.png`});

// ---------- 7. settings: reach a far tab and change something ----------
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await p.waitForTimeout(2200);
const tabNames=await p.$$eval('.tab',t=>t.map(x=>x.textContent.trim()));
const jobsIdx=tabNames.findIndex(t=>/job/i.test(t));
chk('settings exposes a Jobs tab', jobsIdx>=0, JSON.stringify(tabNames));
if(jobsIdx>=0){
  await p.$$eval('.tab',(t,i)=>t[i].scrollIntoView({block:'nearest',inline:'center'}),jobsIdx);
  await p.waitForTimeout(400);
  await p.$$eval('.tab',(t,i)=>t[i].click(),jobsIdx);
  await p.waitForTimeout(2500);
  const jobs=await p.evaluate(()=>({rows:document.querySelectorAll('tbody tr').length,
    overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+2}));
  chk('Jobs tab is reachable and renders', jobs.rows>0, JSON.stringify(jobs));
  chk('Jobs tab does not widen the page', !jobs.overflow, JSON.stringify(jobs));
  await p.screenshot({path:`${OUT}/flow-settings-jobs.png`});
}

console.log('PASS:'); ok.forEach(x=>console.log('  ✓ '+x));
if(fails.length){console.log('\nFAIL:');fails.forEach(x=>console.log('  ✗ '+x));}
console.log(`\npage errors: ${errs.length?errs.slice(0,3).join(' | '):'none'}`);
console.log(`${ok.length} passed, ${fails.length} failed`);
await b.close();
