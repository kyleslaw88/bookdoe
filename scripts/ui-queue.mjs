// ui-gate: widths=360,390,1280 order=60
// Queue page on the seeded gate database (scripts/ui-render-seed.py): the new columns,
// filters, the details expander, the saved selection and no horizontal overflow at the
// width the gate runs (UI_WIDTH: 360, 390 or 1280).
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const b=await chromium.launch();
const ctx=await b.newContext(WIDTH<800?{...devices['iPhone 13'],viewport:{width:WIDTH,height:664}}:{viewport:{width:WIDTH,height:900}});
const p=await ctx.newPage();
const fails=[], chk=(n,c,d='')=>{ if(!c) fails.push(`[${WIDTH}px] ${n}${d?' — '+d:''}`); };
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton')&&document.querySelector('#queue-results'),null,{timeout:15000}).catch(()=>{}); };
const overflow=()=>p.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
const visibleTitles=()=>p.$$eval('#queue-results tr.queue-row, #queue-results tbody > tr:not(.queue-detail)',rs=>rs.map(r=>r.querySelector('.q-title strong')?.textContent));
await p.goto(`${BASE}/manage#queue`,{waitUntil:'load'}); await settle();
const heads=await p.$$eval('#queue-results thead th',hs=>hs.filter(h=>getComputedStyle(h).display!=='none').map(h=>h.textContent.trim()));
const need=WIDTH>=1200?['Title','Type','Format','Indexer','Client','Size','Progress','State','Started','Updated','Source']:['Title','Progress','State'];
for(const h of need) chk(`column ${h} is visible`, heads.includes(h), heads.join('|'));
const text=await p.textContent('#queue-results');
chk('active row shows percent and ETA', /42%/.test(text) && /ETA 1m 35s/.test(text), text.slice(0,200));
chk('failed row shows its reason', text.includes('No managed root folder'));
chk('Recently completed lists the imported one', text.includes('Recently completed') && text.includes('Book 004'));
chk('no horizontal overflow', (await overflow())<=2, String(await overflow()));
// counts and filters
const chips=await p.$$eval('.queue-chip',cs=>cs.map(c=>c.textContent.trim()));
chk('state counts in the header', chips.some(c=>/^Failed\s*1$/.test(c)) && chips.some(c=>/^Active\s*1$/.test(c)), chips.join('|'));
await p.selectOption('#queue-kind','ebook');
let t=await p.textContent('#queue-results');
chk('type filter narrows to ebooks', t.includes('Book 002') && !t.includes('Book 001') && !t.includes('Book 003'), t.slice(0,120));
await p.selectOption('#queue-kind','');
await p.selectOption('#queue-indexer','NZBgeek');
t=await p.textContent('#queue-results');
chk('indexer filter works', t.includes('Book 001') && !t.includes('Book 002'));
await p.selectOption('#queue-indexer','');
await p.click('.queue-chip[data-group="failed"]');
t=await p.textContent('#queue-results');
chk('state filter shows only failed', t.includes('Book 003') && !t.includes('Book 001'));
await p.click('.queue-chip[data-group="all"]');
await p.fill('#queue-q','book 002');
chk('text search', (await visibleTitles()).join()==='Book 002', (await visibleTitles()).join());
// the selection survives a reload
await p.reload({waitUntil:'load'}); await settle();
chk('search is remembered', (await p.inputValue('#queue-q'))==='book 002');
await p.fill('#queue-q','');
// details expander
const btn=p.locator('.queue-expand').first();
await btn.click();
const detail=p.locator('tr.queue-detail:not([hidden])').first();
chk('expander opens details', await detail.isVisible());
const dt=await detail.textContent();
chk('details carry release, indexer, client and score', /Release/.test(dt) && /Indexer/.test(dt) && /SABnzbd|qBittorrent/.test(dt) && /91/.test(dt), dt.slice(0,200));
chk('no overflow with details open', (await overflow())<=2, String(await overflow()));
await p.selectOption('#queue-sort','size'); await p.click('#queue-dir');
chk('still no overflow after sort', (await overflow())<=2);
await p.evaluate(()=>localStorage.removeItem('bookdoe-queue-filter'));
await b.close();
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-queue ${WIDTH}px: ok`);
