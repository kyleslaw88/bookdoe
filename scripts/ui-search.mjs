import { chromium, devices } from 'playwright';
// loopback QA target; override with UI_BASE (use https for anything remote)
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899', // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
      OUT='/tmp/qa/shots';
const fails=[],ok=[]; const chk=(n,c,d='')=>(c?ok:fails).push(n+(c||!d?'':` — ${d}`));
const b=await chromium.launch(); const ctx=await b.newContext({...devices['iPhone 13']}); const p=await ctx.newPage();
const errs=[]; p.on('pageerror',e=>errs.push(e.message));

// ---------- library search must not re-render the page ----------
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await p.waitForTimeout(2500); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
const before=await p.evaluate(()=>{const i=document.querySelector('#library-filter'); i.dataset.stamp='original'; return i.dataset.stamp;});
await p.click('#library-filter');
await p.type('#library-filter','dead world',{delay:90});
const duringFocus=await p.evaluate(()=>document.activeElement?.id);
await p.waitForTimeout(2200);
const after=await p.evaluate(()=>{const i=document.querySelector('#library-filter');
  return {stamp:i.dataset.stamp, value:i.value, focused:document.activeElement?.id, caret:i.selectionStart,
          results:[...document.querySelectorAll('.book-title')].map(x=>x.textContent)};});
chk('the input survives typing (page is not rebuilt)', after.stamp==='original', JSON.stringify({before,after:after.stamp}));
chk('every keystroke is kept', after.value==='dead world', JSON.stringify(after.value));
chk('focus stays in the search box', after.focused==='library-filter', JSON.stringify({duringFocus,after:after.focused}));
chk('the caret stays at the end', after.caret===10, `caret at ${after.caret}`);
chk('and it actually searched the server', after.results.includes('Dead World'), JSON.stringify(after.results.slice(0,3)));
await p.screenshot({path:`${OUT}/search-typed.png`});

// clearing it restores the full library, still without a rebuild
await p.fill('#library-filter',''); await p.waitForTimeout(2200);
const cleared=await p.evaluate(()=>({stamp:document.querySelector('#library-filter').dataset.stamp,
  count:document.querySelectorAll('.book-card').length, label:document.querySelector('#library-count')?.textContent}));
chk('clearing restores the library without a rebuild', cleared.stamp==='original' && cleared.count===100, JSON.stringify(cleared));

// ---------- pluralisation ----------
await p.fill('#library-filter','dead world'); await p.waitForTimeout(2200);
const label=await p.evaluate(()=>document.querySelector('#library-count')?.textContent);
chk('one result reads "1 work", not "1 works"', /\b1 work\b/.test(label||'') && !/1 works/.test(label||''), JSON.stringify(label));

// ---------- discover: more results ----------
await p.goto(`${BASE}/manage#discover`,{waitUntil:'load'}); await p.waitForTimeout(1800); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
await p.fill('#discover-q','dune'); await p.press('#discover-q','Enter'); await p.waitForTimeout(7000);
const d1=await p.evaluate(()=>({rows:document.querySelectorAll('#discover-results tbody tr').length, more:!!document.querySelector('#discover-load-more')}));
chk('discover returns a first page', d1.rows>0, JSON.stringify(d1));
chk('discover offers More results', d1.more, JSON.stringify(d1));
if(d1.more){
  await p.click('#discover-load-more'); await p.waitForTimeout(8000);
  const d2=await p.evaluate(()=>document.querySelectorAll('#discover-results tbody tr').length);
  chk('More results actually adds results', d2>d1.rows, `${d1.rows} -> ${d2}`);
}
await p.screenshot({path:`${OUT}/search-discover.png`});

// ---------- picker: more results ----------
const id=(await (await fetch(`${BASE}/api/v1/library?q=Dead World`)).json()).items[0].id; // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
await p.goto(`${BASE}/manage#library/${id}`,{waitUntil:'load'}); await p.waitForTimeout(2500); // nosemgrep: typescript.react.security.react-insecure-request.react-insecure-request
await p.click('#refresh-work'); await p.waitForTimeout(8000);
await p.fill('#meta-search-q','dune'); await p.click('#meta-search-form button[type=submit]'); await p.waitForTimeout(7000);
const m1=await p.evaluate(()=>({rows:document.querySelectorAll('.pick-meta').length, more:!!document.querySelector('#meta-load-more')}));
chk('picker search returns results', m1.rows>0, JSON.stringify(m1));
chk('picker offers More results', m1.more, JSON.stringify(m1));
if(m1.more){
  await p.click('#meta-load-more'); await p.waitForTimeout(8000);
  const m2=await p.evaluate(()=>document.querySelectorAll('.pick-meta').length);
  chk('picker More results adds results', m2>m1.rows, `${m1.rows} -> ${m2}`);
}
await p.screenshot({path:`${OUT}/search-picker.png`});

console.log('PASS:'); ok.forEach(x=>console.log('  ✓ '+x));
if(fails.length){console.log('\nFAIL:');fails.forEach(x=>console.log('  ✗ '+x));}
console.log(`\npage errors: ${errs.length?errs.slice(0,2).join(' | '):'none'}`);
console.log(`${ok.length} passed, ${fails.length} failed`);
await b.close(); process.exit(fails.length?1:0);
