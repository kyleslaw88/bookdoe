// ui-gate: widths=360,390,1280 order=120
// The Library State filter (Complete / Partly owned / Nothing owned, counts, "No audiobook" chips) and the Duplicates page:
// scan, filters, groups side by side, select all High, dry run, bulk merge, then undo the whole batch, with no horizontal
// overflow. Runs against the gate's seeded instance (scripts/ui-render-seed.py: three file-only/provider-backed pairs plus the
// State shapes). It leaves the data as it found it (the batch is undone), so it can run at every width.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[], ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(`[${WIDTH}px] ${n}`+(c||!d?'':` - ${d}`));
const getJson=async u=>(await fetch(`${BASE}${u}`)).json();
const postJson=async(u,body)=>(await fetch(`${BASE}${u}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json();

const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:760}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e))); p.on('dialog',d=>d.accept());
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton'),null,{timeout:15000}).catch(()=>{}); };
const wait=(sel,t=20000)=>p.waitForSelector(sel,{timeout:t}).catch(()=>null);
const until=async(fn,t=30000)=>{ const end=Date.now()+t; while(Date.now()<end){ if(await fn()) return true; await new Promise(r=>setTimeout(r,250)); } return false; };
const cardTitles=()=>p.$$eval('.book-card .book-title',t=>t.map(x=>x.textContent));
const overflow=async name=>{ const m=await p.evaluate(()=>{const mo=document.querySelector('.modal');return {s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth,ms:mo?mo.scrollWidth:0,mc:mo?mo.clientWidth:0};});
  chk(`${name} fits the viewport`, m.s<=m.c+2 && m.ms<=m.mc+2, JSON.stringify(m)); };

// ---- 1. State filter labels, counts and chips ----
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await wait('#status-filter'); await settle();
await p.waitForFunction(()=>/\(\d+\)/.test(document.querySelector('#status-filter option:nth-child(2)')?.textContent||''),null,{timeout:10000}).catch(()=>{});
const opts=await p.$$eval('#status-filter option',o=>o.map(x=>x.textContent));
chk('the State filter offers Complete / Partly owned / Nothing owned, with counts', opts.length===4 && /^Any state \(\d+\)$/.test(opts[0]) && /^Complete \(\d+\)$/.test(opts[1]) && /^Partly owned \(\d+\)$/.test(opts[2]) && /^Nothing owned \(\d+\)$/.test(opts[3]), JSON.stringify(opts));
chk('there is no misleading "Missing" option', !opts.some(x=>/Missing/i.test(x)));
const card=title=>p.evaluate(t=>{ const c=[...document.querySelectorAll('.book-card')].find(x=>x.querySelector('.book-title')?.textContent===t); return c?{chips:[...c.querySelectorAll('.asset-badges .badge')].map(x=>x.textContent.trim()),missing:[...c.querySelectorAll('.chip-missing')].map(x=>x.textContent.trim())}:null; },title);
await p.fill('#library-filter','State Shape'); await until(async()=>(await cardTitles()).length===2);
let c1=await card('State Shape Partly'), c2=await card('State Shape Redundant');
chk('an EPUB with a monitored audiobook shows its owned format and a grey "No audiobook" chip', !!c1 && c1.chips.some(x=>/EPUB/.test(x)) && c1.missing.includes('No audiobook'), JSON.stringify(c1));
chk('an owned ebook beside a redundant empty placeholder is not labelled missing', !!c2 && c2.chips.some(x=>/EPUB/.test(x)) && c2.missing.length===0, JSON.stringify(c2));
await p.selectOption('#status-filter','partial'); await until(async()=>(await cardTitles()).length===1);
let titles=await cardTitles();
chk('Partly owned lists the EPUB+wanted-audiobook book, not the redundant-placeholder one', titles.includes('State Shape Partly') && !titles.includes('State Shape Redundant'), JSON.stringify(titles));
chk('the chip reads "State: Partly owned" (no count in it)', (await p.$$eval('#library-chips .filter-chip-text',x=>x.map(y=>y.textContent))).some(x=>x==='State: Partly owned'));
await p.selectOption('#status-filter','complete'); await until(async()=>(await cardTitles()).includes('State Shape Redundant'));
titles=await cardTitles();
chk('Complete lists the redundant-placeholder book', titles.includes('State Shape Redundant') && !titles.includes('State Shape Partly'), JSON.stringify(titles));
await overflow('Library with the State filter');
await p.goto(`${BASE}/manage#library?status=missing`,{waitUntil:'load'}); await wait('#status-filter'); await settle();
chk('an old bookmarked ?status=missing link opens Partly owned', (await p.inputValue('#status-filter'))==='partial');
// the book page lists the wish as "Wanted: Audiobook" with Search / Unmonitor, and hides nothing owned
const shape=(await getJson('/api/v1/library?q=State%20Shape%20Partly')).items[0];
await p.goto(`${BASE}/manage#library/${shape.id}`,{waitUntil:'load'}); await settle(); await wait('.asset-row');
const rows=await p.$$eval('.asset-row',r=>r.map(x=>({kind:x.querySelector('.asset-kind')?.textContent.trim(),btns:[...x.querySelectorAll('.asset-actions button')].map(b=>b.textContent.trim())})));
const wanted=rows.find(r=>/^Wanted: .*Audiobook/.test(r.kind||''));
chk('the book page shows "Wanted: Audiobook" with Search and Unmonitor', !!wanted && wanted.btns.includes('Search') && wanted.btns.includes('Unmonitor'), JSON.stringify(rows));
await overflow('book page');

// ---- 2. Duplicates page: scan ----
await p.goto(`${BASE}/manage#duplicates`,{waitUntil:'load'}); await wait('#dup-scan'); await settle();
chk('the Duplicates page is in the menu and has a Scan button', !!(await p.$('#dup-scan')));
await p.click('#dup-scan');
await p.waitForFunction(()=>/Last scanned/.test(document.querySelector('#dup-sub')?.textContent||''),null,{timeout:60000}).catch(()=>{});
chk('after the scan the page says when it last scanned and how many groups', /Last scanned .* high, \d+ medium, \d+ low/.test(await p.textContent('#dup-sub')), await p.textContent('#dup-sub'));
await wait('.dup-group');
const status=await getJson('/api/v1/admin/duplicates/status');
chk('three high confidence pairs were found', status.counts.high===3, JSON.stringify(status.counts));
const first=await p.$eval('.dup-group',g=>g.innerText);
chk('a group shows the file-only and the provider record side by side with the recommendation', /From file/.test(first) && /Provider record/.test(first) && /Keep this record/.test(first) && /Best metadata/.test(first), first);
chk('a group shows owned files and the metadata quality', /EPUB/.test(first) && /Metadata \d+\/100/.test(first), first);
await overflow('Duplicates page');

// ---- 3. filters and search ----
await p.selectOption('#dup-conf','high'); await until(async()=>(await p.$$('.dup-group')).length===3,10000);
chk('the confidence filter narrows to the high groups', (await p.$$('.dup-group')).length===3);
await p.selectOption('#dup-conf',''); await p.fill('#dup-q','Holes'); await settle();
await p.waitForFunction(()=>document.querySelectorAll('.dup-group').length===1,null,{timeout:8000}).catch(()=>{});
chk('search finds one group by title', (await p.$$('.dup-group')).length===1);
await p.fill('#dup-q',''); await settle();
await p.waitForFunction(()=>document.querySelectorAll('.dup-group').length>=3,null,{timeout:8000}).catch(()=>{});

// ---- 4. dismissal is remembered ----
await p.fill('#dup-q','Hatchet'); await p.waitForFunction(()=>document.querySelectorAll('.dup-group').length===1,null,{timeout:8000}).catch(()=>{});
await p.click('.dup-dismiss'); await settle();
await p.waitForFunction(()=>document.querySelectorAll('.dup-group').length===0,null,{timeout:8000}).catch(()=>{});
chk('"Not the same book" hides the group', (await p.$$('.dup-group')).length===0);
const dismissed=await getJson('/api/v1/admin/duplicates?dismissed=true&light=true');
chk('and the server remembers it', dismissed.total===1);
await postJson('/api/v1/admin/duplicates/'+dismissed.items[0].id+'/dismiss',{dismissed:false});
await p.fill('#dup-q',''); await settle();
await p.waitForFunction(()=>document.querySelectorAll('.dup-group').length>=3,null,{timeout:8000}).catch(()=>{});

// ---- 5. select all High, dry run, bulk merge ----
const worksBefore=(await getJson('/api/v1/library?limit=500')).total;
await p.click('#dup-sel-high'); await p.waitForFunction(()=>/^3 selected/.test(document.querySelector('#dup-bar strong')?.textContent||''),null,{timeout:10000}).catch(()=>{});
chk('one click selects every high confidence group', /^3 selected/.test(await p.textContent('#dup-bar strong')), await p.textContent('#dup-bar strong'));
chk('medium and low groups stay unticked', (await p.$$eval('.dup-pick',c=>c.filter(x=>x.checked).length))<=3);
await p.click('#dup-merge'); await wait('.dup-dry');
const dry=await p.textContent('#bm-body');
chk('the dry run lists each group as Ready and says nothing is deleted', (dry.match(/Ready/g)||[]).length===3 && /No file is deleted/.test(dry), dry.slice(0,300));
await overflow('bulk merge dry run');
await p.click('#bm-go');
await p.waitForFunction(()=>/Done\./.test(document.querySelector('#bm-prog')?.textContent||''),null,{timeout:60000}).catch(()=>{});
const res=await p.textContent('#bm-body');
chk('the results table says 3 merged', (res.match(/Merged/g)||[]).length>=3 && /3 groups? merged/.test(res), res.slice(0,300));
await overflow('bulk merge results');
const merged=await getJson('/api/v1/admin/duplicates/batches');
chk('the batch is recorded with three merges', merged.items[0]?.counts.merged===3, JSON.stringify(merged.items[0]?.counts));
chk('the provider-backed records now hold the files', (await getJson('/api/v1/library?q=Holes')).items.filter(w=>w.metadata_id).every(w=>w.assets.some(a=>a.status==='owned')));
await p.click('#bm-undo');
await until(async()=>(await getJson('/api/v1/admin/duplicates/batches')).items[0]?.status==='undone',60000);
const after=await getJson('/api/v1/admin/duplicates/batches');
chk('one click undid the whole batch', after.items[0]?.status==='undone' && after.items[0].counts.undone===3, JSON.stringify(after.items[0]));
chk('the library is exactly as it was', (await getJson('/api/v1/library?limit=500')).total===worksBefore);
await p.evaluate(()=>{document.querySelector('#modal-root').innerHTML='';});
await settle();
chk('the three groups are open again', (await getJson('/api/v1/admin/duplicates/status')).counts.high===3);
await overflow('Duplicates page after the undo');

chk('no uncaught page errors', errors.length===0, errors.join(' | '));
await b.close();
console.log(ok.map(x=>'ok   '+x).join('\n')); if(fails.length){ console.error(fails.map(x=>'FAIL '+x).join('\n')); process.exit(1); }
