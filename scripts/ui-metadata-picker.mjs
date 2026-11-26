import { chromium, devices } from 'playwright';
// loopback QA target; override with UI_BASE (use https for anything remote)
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899', // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
      OUT='/tmp/qa/shots';
const fails=[],ok=[]; const chk=(n,c,d='')=>(c?ok:fails).push(n+(c||!d?'':` — ${d}`));
const b=await chromium.launch(); const ctx=await b.newContext({...devices['iPhone 13']}); const p=await ctx.newPage();
const errs=[]; p.on('pageerror',e=>errs.push(e.message));

const id=(await (await fetch(`${BASE}/api/v1/library?q=Dead World`)).json()).items[0].id; // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
await p.goto(`${BASE}/manage#library/${id}`,{waitUntil:'load'}); await p.waitForTimeout(2500); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request

// the metadata the files supplied
const meta=await p.evaluate(()=>({
  series:document.querySelector('.detail-meta .muted.small')?.textContent?.trim(),
  byline:document.querySelector('.byline')?.textContent?.replace(/\s+/g,' ').trim(),
  narrator:[...document.querySelectorAll('.asset-kind')].map(x=>x.textContent.trim()).join(' | '),
}));
chk('series from the file is shown', /Convergence/.test(meta.series||''), JSON.stringify(meta.series));
chk('year from the file is shown', /2026/.test(meta.byline||''), JSON.stringify(meta.byline));
chk('narrator from the file is shown', /R\.C\. Bray/.test(meta.narrator||''), JSON.stringify(meta.narrator));
await p.screenshot({path:`${OUT}/picker-detail.png`});

await p.click('#refresh-work'); await p.waitForTimeout(7000);
const m=await p.evaluate(()=>{const el=document.querySelector('.modal'); if(!el) return null;
  return {searchBox:!!el.querySelector('#meta-search-q'), seeded:el.querySelector('#meta-search-q')?.value,
    none:!!el.querySelector('#meta-none'), rows:el.querySelectorAll('.pick-meta').length,
    showsIds:/asin/i.test(el.textContent), overflows:el.getBoundingClientRect().right>window.innerWidth+2};});
chk('picker opens', !!m, 'no modal');
chk('it has a search box', m?.searchBox, JSON.stringify(m));
chk('seeded with the title and author', /Dead World/.test(m?.seeded||'') && /Alanson/.test(m?.seeded||''), JSON.stringify(m?.seeded));
chk('it shows the identifiers we hold', m?.showsIds, JSON.stringify(m));
chk('it offers a "none of these" way out', m?.none);
chk('it fits the phone screen', !m?.overflows);
await p.screenshot({path:`${OUT}/picker-open.png`});

// refine the search the way a person would
await p.fill('#meta-search-q','Convergence Craig Alanson');
await p.click('#meta-search-form button[type=submit]'); await p.waitForTimeout(6000);
const refined=await p.evaluate(()=>{const el=document.querySelector('.modal');
  return {rows:el.querySelectorAll('.pick-meta').length, text:el.querySelector('#meta-results')?.textContent?.replace(/\s+/g,' ').slice(0,120)};});
chk('refining the search returns different results', refined.rows>0, JSON.stringify(refined));
chk('the refined result is the right author', /Alanson/.test(refined.text||''), JSON.stringify(refined));
await p.screenshot({path:`${OUT}/picker-refined.png`});

// a deliberately hopeless search should say so, not break
await p.fill('#meta-search-q','zzzzqqq nonexistent book title');
await p.click('#meta-search-form button[type=submit]'); await p.waitForTimeout(6000);
const none=await p.evaluate(()=>document.querySelector('#meta-results')?.textContent?.trim().slice(0,60));
chk('an empty search is reported, not left blank', /no matches/i.test(none||''), JSON.stringify(none));

// and the escape hatch works
await p.click('#meta-none'); await p.waitForTimeout(800);
chk('"none of these" closes the picker', !(await p.$('.modal')));
console.log('PASS:'); ok.forEach(x=>console.log('  ✓ '+x));
if(fails.length){console.log('\nFAIL:');fails.forEach(x=>console.log('  ✗ '+x));}
console.log(`\npage errors: ${errs.length?errs.slice(0,2).join(' | '):'none'}`);
console.log(`${ok.length} passed, ${fails.length} failed`);
await b.close(); process.exit(fails.length?1:0);
