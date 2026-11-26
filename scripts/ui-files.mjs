// ui-gate: widths=360,390,1280 order=90
// Compact file lists and format chips (feat/compact-file-lists). Management book page: a 47-track
// MP3 audiobook is one summary line with a collapsed "Show files" table (sticky header, max-height),
// an ebook holding epub + mobi shows both chips with the primary marked and no Convert to EPUB.
// The Player book page shows chips and "N files" but no file names or paths. 360/390/1280 px.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899'; const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[]; const T=`[${WIDTH}px]`;
const b=await chromium.launch();
const ctx=await b.newContext(WIDTH<800?{...devices['iPhone 13'],viewport:{width:WIDTH,height:800}}:{viewport:{width:WIDTH,height:900}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e)));
const find=async t=>(await (await fetch(`${BASE}/api/v1/library?q=${encodeURIComponent(t)}`)).json()).items.find(x=>x.title===t);
const fit=async n=>{ const d=await p.evaluate(()=>({s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth})); if(d.s>d.c+2) fails.push(`${T} ${n} overflows ${JSON.stringify(d)}`); };
const open=async(id,hash='manage')=>{ await p.goto(`${BASE}/${hash}#library/${id}`,{waitUntil:'load'}); await p.waitForSelector(hash==='manage'?'.asset-row':'.pl-detail',{timeout:15000}); await p.waitForTimeout(1200); };

const many=await find('Many Tracks'), both=await find('Both Formats');
if(!many||!both){ fails.push(`${T} seeded books missing`); }
else{
  await open(many.id);
  const summary=await p.textContent('.files-summary');
  if(!/^47 files · MP3 · 7h 50m · [\d.]+ [KMG]?B$/.test(summary.trim())) fails.push(`${T} summary line wrong: ${summary}`);
  if(await p.$eval('.file-details',d=>d.open)) fails.push(`${T} files disclosure is open by default`);
  if(await p.$eval('.file-table-wrap',el=>el.checkVisibility({checkVisibilityCSS:true,contentVisibilityAuto:true}))) fails.push(`${T} table visible while collapsed`);
  if(await p.$$eval('.asset-row .files > div:not(.files-summary)',n=>n.length)) fails.push(`${T} per-file rows rendered inline for 47 files`);
  await p.click('.file-details summary'); await p.waitForTimeout(200);
  const t=await p.$eval('.file-table-wrap',el=>({h:el.getBoundingClientRect().height,sh:el.scrollHeight,ov:getComputedStyle(el).overflowY,rows:el.querySelectorAll('tbody tr').length,pos:getComputedStyle(el.querySelector('th')).position}));
  if(t.h>322||t.ov!=='auto'||t.rows!==47||t.pos!=='sticky'||t.sh<=t.h) fails.push(`${T} file table not compact/scrollable ${JSON.stringify(t)}`);
  if(await p.$$eval('.file-copy',n=>n.length)!==47) fails.push(`${T} admin copy-path buttons missing`);
  await fit('Many Tracks open');
  await open(both.id);
  const chips=await p.$$eval('.asset-row .fmt-chip',n=>n.map(x=>({t:x.textContent,p:x.classList.contains('primary')})));
  if(JSON.stringify(chips.map(c=>c.t))!=='["EPUB","MOBI"]'||!chips[0].p||chips[1].p) fails.push(`${T} format chips wrong ${JSON.stringify(chips)}`);
  if(await p.$$eval('.asset-convert',n=>n.length)) fails.push(`${T} Convert offered although an EPUB exists`);
  if((await p.textContent('.asset-status')).includes('MOBI')&&/owned\s*·\s*MOBI/.test(await p.textContent('.asset-status'))) fails.push(`${T} old single-format label`);
  if(await p.$$eval('.asset-row .file-line',n=>n.length)!==2) fails.push(`${T} expected two inline compact file rows`);
  await fit('Both Formats');
  // Player: chips and a count, never names or paths
  for(const [w,line] of [[many,/47 files · 7h 50m/],[both,/2 files/]]){
    await open(w.id,'app');
    const html=await p.textContent('#pl-view');
    if(!line.test(html)) fails.push(`${T} Player ${w.title}: summary line missing`);
    if(/Track \d\d|\.mp3|\.mobi|\.epub|\/tmp|bookdoe-uigate/i.test(html)) fails.push(`${T} Player ${w.title}: file name or path shown`);
    if(!(await p.$$eval('.pl-fmt',n=>n.length))) fails.push(`${T} Player ${w.title}: no format chips`);
    await fit(`Player ${w.title}`);
  }
}
if(errors.length) fails.push(`${T} page errors: ${errors.join(' | ')}`);
await b.close();
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-files ${WIDTH}px: ok`);
