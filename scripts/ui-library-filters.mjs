// ui-gate: widths=360,390,1280 order=110
// Library search and filters must never strand you (the owner: "I cannot get out of the search"). Drives the real
// Management UI and the Player library on the gate's seeded instance (scripts/ui-render-seed.py):
// search in the header, clear in the header box, clear in the page input (x / input event, Escape), nav away
// and back (the query is gone), removable chips + Clear filters, the empty-state Clear filters button, and
// link filters (#library?q=..) that apply once. Width from UI_WIDTH (360, 390, 1280); exit 1 on any failure.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[], chk=(n,c,d='')=>{ if(!c) fails.push(`[${WIDTH}px] ${n}${d?' - '+d:''}`); };
const b=await chromium.launch();
const ctx=await b.newContext(WIDTH<800?{...devices['iPhone 13'],viewport:{width:WIDTH,height:800}}:{viewport:{width:WIDTH,height:900}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e)));
p.setDefaultTimeout(8000);
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton')&&!document.querySelector('#library-container.busy'),null,{timeout:15000}).catch(()=>{}); await p.waitForTimeout(450); };
const snap=()=>p.evaluate(()=>({
  page:document.querySelector('#library-filter')?.value??null, header:document.querySelector('#global-search')?.value??null,
  cards:document.querySelectorAll('.book-card').length, empty:!!document.querySelector('#library-container .empty'),
  chips:[...document.querySelectorAll('#library-chips .filter-chip')].map(x=>x.textContent.trim()),
  clearBtn:!!document.querySelector('#library-clear-filters'), emptyClear:!!document.querySelector('#library-empty-clear'),
  count:document.querySelector('#library-count')?.textContent||'', hash:location.hash,
  overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth }));
const navTo=async route=>{ await p.evaluate(r=>document.querySelector(`#nav a[data-route="${r}"]`).click(),route); await settle(); };
let FULL=0;   // the unfiltered list, measured on arrival

try{
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await p.waitForSelector('#library-filter'); await settle();
let s=await snap(); FULL=s.cards;
chk('library lists several works', FULL>=5 && !s.empty, JSON.stringify(s));
chk('no filters: no chips, no Clear filters', s.chips.length===0 && !s.clearBtn);
chk('input is a search box (x clear control)', await p.evaluate(()=>document.querySelector('#library-filter').type==='search'));

// 1. search from the header box, then clear it there
await p.fill('#global-search','dead world'); await p.waitForFunction(()=>document.querySelector('#library-filter')?.value==='dead world'); await settle();
s=await snap();
chk('header search fills the page input', s.page==='dead world' && s.cards===1, JSON.stringify(s));
chk('active query is a chip with Clear filters', s.chips.length===1 && /dead world/.test(s.chips[0]) && s.clearBtn, JSON.stringify(s.chips));
await p.fill('#global-search',''); await settle();
s=await snap();
chk('clearing the header box clears the page input', s.page==='' && s.header==='', JSON.stringify(s));
chk('...and re-runs the unfiltered list (not "No matching works")', s.cards===FULL && !s.empty && s.chips.length===0, JSON.stringify(s));

// 2. clear in the page input (input event with empty value = the x of type=search)
await p.fill('#library-filter','dead world'); await settle();
s=await snap(); chk('page input searches and mirrors into the header box', s.cards===1 && s.header==='dead world', JSON.stringify(s));
await p.fill('#library-filter',''); await settle();
s=await snap(); chk('clearing the page input clears the header box and lists everything', s.header==='' && s.cards===FULL && !s.empty, JSON.stringify(s));
await p.evaluate(()=>{ const i=document.querySelector('#library-filter'); i.value='dead world'; i.dispatchEvent(new Event('input',{bubbles:true})); }); await settle();
await p.evaluate(()=>{ const i=document.querySelector('#library-filter'); i.value=''; i.dispatchEvent(new Event('search',{bubbles:true})); }); await settle();
s=await snap(); chk('the search event with an empty value (iOS x) clears too', s.cards===FULL && s.header==='', JSON.stringify(s));

// 3. Escape
await p.fill('#library-filter','dead world'); await settle();
await p.press('#library-filter','Escape'); await settle();
s=await snap(); chk('Escape in the page input clears everything', s.page==='' && s.header==='' && s.cards===FULL, JSON.stringify(s));
await p.fill('#global-search','dead world'); await settle();
await p.press('#global-search','Escape'); await settle();
s=await snap(); chk('Escape in the header box clears everything', s.page==='' && s.header==='' && s.cards===FULL, JSON.stringify(s));

// 4. empty state is never a dead end
await p.fill('#library-filter','zzzz no such book'); await settle();
s=await snap(); chk('no match shows the empty state with a Clear filters button', s.empty && s.emptyClear && s.clearBtn, JSON.stringify(s));
chk('the empty-state page fits the viewport', s.overflow<=2, String(s.overflow));
await p.click('#library-empty-clear'); await settle();
s=await snap(); chk('empty-state Clear filters restores the list', s.cards===FULL && s.page==='' && s.header==='' && s.chips.length===0, JSON.stringify(s));

// 5. chips for each filter, removable one by one, and Clear filters
await p.fill('#library-filter','book'); await settle();
await p.selectOption('#asset-filter','ebook'); await settle();
await p.selectOption('#status-filter','partial'); await settle();
s=await snap(); chk('query + media + state show three chips', s.chips.length===3, JSON.stringify(s.chips));
chk('chips fit the viewport', s.overflow<=2, String(s.overflow));
await p.click('#library-chips [data-clear="kind"]'); await settle();
s=await snap(); chk('removing the media chip resets that dropdown only', s.chips.length===2 && await p.inputValue('#asset-filter')==='' && s.page==='book', JSON.stringify(s));
await p.click('#library-clear-filters'); await settle();
s=await snap(); chk('Clear filters resets every control', s.chips.length===0 && s.page==='' && s.header==='' && await p.inputValue('#status-filter')==='' && s.cards===FULL && !s.clearBtn, JSON.stringify(s));
const seriesOpt=await p.$$eval('#series-filter option',o=>o.filter(x=>x.value).map(x=>({v:x.value,t:x.textContent})));
if(seriesOpt.length){
  await p.selectOption('#series-filter',seriesOpt[0].v); await settle();
  s=await snap(); chk('series filter shows a chip with the series name', s.chips.some(c=>c.includes(seriesOpt[0].t)), JSON.stringify(s.chips));
  await p.click('#library-clear-filters'); await settle();
}

// 6. a stale query does not survive navigation (menu, then back to Library)
await p.fill('#global-search','dead world'); await settle();
await navTo('authors');
chk('header box is emptied when leaving the Library', (await p.inputValue('#global-search'))==='');
await navTo('library');
s=await snap(); chk('Authors then Library: the query is gone', s.page==='' && s.header==='' && s.cards===FULL && s.chips.length===0, JSON.stringify(s));
await p.fill('#library-filter','zzzz no such book'); await settle();
await navTo('authors'); await navTo('library');
s=await snap(); chk('a no-match search does not stick across navigation', !s.empty && s.cards===FULL, JSON.stringify(s));
await p.fill('#library-filter','dead world'); await settle();
await navTo('library');
s=await snap(); chk('tapping Library in the menu while filtered starts fresh', s.page==='' && s.cards===FULL, JSON.stringify(s));
await p.fill('#library-filter','dead world'); await settle();
await p.evaluate(()=>{ location.hash='settings'; }); await settle();
await p.evaluate(()=>{ location.hash='library'; }); await settle();
s=await snap(); chk('leaving by URL also drops the query', s.page==='' && s.cards===FULL, JSON.stringify(s));
// sort/view mode may persist
await p.click('#list-view'); await navTo('authors'); await navTo('library');
chk('table/cover mode is kept', await p.evaluate(()=>!!document.querySelector('#library-container table')));
await p.click('#list-view'); await settle();

// 7. link filters apply once, reloads keep only the URL's explicit filters
await p.goto(`${BASE}/manage#library?q=dead%20world`,{waitUntil:'load'}); await p.waitForSelector('#library-filter'); await settle();
s=await snap(); chk('a link filter (#library?q=..) is honored', s.page==='dead world' && s.header==='dead world' && s.cards===1 && s.chips.length===1, JSON.stringify(s));
await p.reload({waitUntil:'load'}); await p.waitForSelector('#library-filter'); await settle();
s=await snap(); chk('reload keeps the URL filter', s.page==='dead world' && s.cards===1, JSON.stringify(s));
await p.fill('#library-filter',''); await settle();
s=await snap(); chk('clearing a link filter drops it from the URL', !s.hash.includes('?') && s.cards===FULL, JSON.stringify(s));
await p.reload({waitUntil:'load'}); await p.waitForSelector('#library-filter'); await settle();
s=await snap(); chk('reload after clearing shows the whole library', s.page==='' && s.cards===FULL, JSON.stringify(s));
await p.goto(`${BASE}/manage#library?status=missing&kind=ebook`,{waitUntil:'load'}); await p.waitForSelector('#library-filter'); await settle();
s=await snap(); chk('dropdown filters from the link are honored (two chips)', s.chips.length===2 && await p.inputValue('#status-filter')==='partial' && await p.inputValue('#asset-filter')==='ebook', JSON.stringify(s));
await navTo('authors'); await navTo('library');
s=await snap(); chk('link filters apply once: menu navigation clears them', s.chips.length===0 && await p.inputValue('#status-filter')==='' && s.cards===FULL, JSON.stringify(s));

// 8. other list pages: Authors, Series, Queue, Wanted keep an escape hatch
await navTo('authors'); await p.waitForSelector('#author-filter');
await p.fill('#author-filter','zzzz no such author'); await p.waitForSelector('#authors-clear',{timeout:5000}).catch(()=>{});
chk('Authors: no-match state has Clear filter', !!(await p.$('#authors-clear')));
if(await p.$('#authors-clear')){ await p.click('#authors-clear'); await p.waitForSelector('.author-row',{timeout:5000}).catch(()=>{}); chk('Authors: Clear filter restores rows', (await p.$$('.author-row')).length>0 && (await p.inputValue('#author-filter'))===''); }
await navTo('series'); await p.waitForSelector('#series-search');
await p.fill('#series-search','zzzz no such series'); await p.waitForSelector('#series-clear',{timeout:5000}).catch(()=>{});
chk('Series: no-match state has Clear filters', !!(await p.$('#series-clear')));
if(await p.$('#series-clear')){ await p.click('#series-clear'); await p.waitForSelector('.series-card',{timeout:5000}).catch(()=>{}); chk('Series: Clear filters restores cards', (await p.$$('.series-card')).length>0); }
await p.evaluate(()=>localStorage.setItem('bookdoe-queue-filter',JSON.stringify({q:'zzzz no such download',group:'all'})));
await p.goto(`${BASE}/manage#queue`,{waitUntil:'load'}); await p.reload({waitUntil:'load'}); await p.waitForSelector('#queue-results'); await settle();
chk('Queue: a saved filter shows Clear filters in the toolbar and the empty state', !!(await p.$('#queue-clear')) && !!(await p.$('.queue-clear-empty')));
if(await p.$('#queue-clear')){ await p.click('#queue-clear'); chk('Queue: Clear filters empties the search and lists rows', (await p.inputValue('#queue-q'))==='' && !(await p.$('#queue-clear')) && (await p.$$('#queue-results tbody tr')).length>0); }
await p.evaluate(()=>localStorage.removeItem('bookdoe-queue-filter'));

// 9. the Player library: visible Clear filters, no dead end
await p.goto(`${BASE}/app#library`,{waitUntil:'load'}); await p.waitForSelector('#lib-q'); await p.waitForFunction(()=>!document.querySelector('#pl-view .pl-skeleton')); 
const pl=()=>p.evaluate(()=>({q:document.querySelector('#lib-q').value,books:document.querySelectorAll('#lib-body .pl-cover').length,clear:!!document.querySelector('#lib-clear'),emptyClear:!!document.querySelector('#lib-empty-clear'),empty:!!document.querySelector('#lib-body .pl-empty'),overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth}));
let q=await pl(); chk('Player: no filter, no Clear filters', !q.clear && q.books>0, JSON.stringify(q));
await p.fill('#lib-q','zzzz no such book'); await p.waitForSelector('#lib-empty-clear',{timeout:5000}).catch(()=>{});
q=await pl(); chk('Player: no-match state offers Clear filters', q.empty && q.emptyClear && q.clear, JSON.stringify(q));
chk('Player: empty state fits the viewport', q.overflow<=2, String(q.overflow));
await p.click('#lib-empty-clear'); await p.waitForFunction(()=>document.querySelectorAll('#lib-body .pl-cover').length>0,null,{timeout:5000}).catch(()=>{});
q=await pl(); chk('Player: Clear filters restores the list and the input', q.q==='' && q.books>0 && !q.clear, JSON.stringify(q));
await p.fill('#lib-q','dead world'); await p.waitForTimeout(500);
await p.press('#lib-q','Escape'); await p.waitForFunction(()=>document.querySelectorAll('#lib-body .pl-cover').length>1,null,{timeout:5000}).catch(()=>{});
q=await pl(); chk('Player: Escape clears the search', q.q==='' && q.books>1, JSON.stringify(q));
await p.fill('#lib-q','dead world'); await p.waitForTimeout(500);
await p.evaluate(()=>{ location.hash='authors'; }); await p.waitForSelector('#au-list'); await p.evaluate(()=>{ location.hash='library'; }); await p.waitForSelector('#lib-q');
chk('Player: a typed search does not survive navigation', (await p.inputValue('#lib-q'))==='');

}catch(err){ fails.push(`[${WIDTH}px] aborted: ${String(err.message).split('\n')[0]}`); }
chk('no page errors', errors.length===0, errors.join(' | '));
await b.close();
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-library-filters ${WIDTH}px: ok`);
