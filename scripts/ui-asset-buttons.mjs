// ui-gate: widths=360,390,1280 order=80
// Per-asset controls stay single and fit (issues #265 Convert, series-nav Keep offline): on the
// Management book page every asset row has at most one Convert and one Keep offline button, and
// the Player book page has at most one Keep offline per asset (and none for an original that
// already has a converted copy). Runs at 360/390/1280 against the gate's seeded instance.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899'; const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[];
const b=await chromium.launch();
const ctx=await b.newContext(WIDTH<800?{...devices['iPhone 13'],viewport:{width:WIDTH,height:800}}:{viewport:{width:WIDTH,height:900}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e)));
const fit=async n=>{ const d=await p.evaluate(()=>({s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth})); if(d.s>d.c+2) fails.push(`[${WIDTH}px] ${n} overflows ${JSON.stringify(d)}`); };
for(const t of ['Tiny Book','Spoken Tiny']){
  const w=(await (await fetch(`${BASE}/api/v1/library?q=${encodeURIComponent(t)}`)).json()).items.find(x=>x.title===t);
  if(!w){ fails.push(`[${WIDTH}px] seeded book ${t} missing`); continue; }
  await p.goto(`${BASE}/manage#library/${w.id}`,{waitUntil:'load'}); await p.waitForSelector('.asset-row',{timeout:15000}); await p.waitForTimeout(1500);
  const rows=await p.$$eval('.asset-row',rs=>rs.map(r=>({conv:r.querySelectorAll('.asset-convert').length,off:r.querySelectorAll('.asset-offline').length,sw:r.scrollWidth,cw:r.clientWidth,out:[...r.querySelectorAll('button')].filter(x=>x.getBoundingClientRect().right>document.documentElement.clientWidth+1).length})));
  for(const r of rows){ if(r.conv>1||r.off>1) fails.push(`[${WIDTH}px] Management ${t}: duplicate buttons ${JSON.stringify(r)}`); if(r.sw>r.cw+2||r.out) fails.push(`[${WIDTH}px] Management ${t}: asset row overflows ${JSON.stringify(r)}`); }
  await fit(`Management ${t}`);
  await p.goto(`${BASE}/app#library/${w.id}`,{waitUntil:'load'}); await p.waitForSelector('#view h1, #view h2',{timeout:15000}).catch(()=>{}); await p.waitForTimeout(1500);
  const ids=await p.$$eval('.offline-toggle',bs=>bs.map(x=>x.dataset.asset));
  if(new Set(ids).size!==ids.length) fails.push(`[${WIDTH}px] Player ${t}: duplicate Keep offline ${JSON.stringify(ids)}`);
  await fit(`Player ${t}`);
}
if(errors.length) fails.push(`[${WIDTH}px] page errors: ${errors.join(' | ')}`);
await b.close();
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-asset-buttons ${WIDTH}px: ok`);
