// ui-gate: widths=360,390 order=20
import { chromium, devices } from 'playwright';
import fs from 'fs';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899', OUT=process.env.UI_SHOTS||'/tmp/qa/shots';
const WIDTH=Number(process.env.UI_WIDTH)||390;  // the gate runs 360 and 390
fs.mkdirSync(OUT,{recursive:true});
const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:664}}); const p=await ctx.newPage();
const fails=[]; const ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(n+(c||!d?'':` — ${d}`));
const st=()=>p.evaluate(()=>{const sb=document.querySelector('#sidebar');const r=sb.getBoundingClientRect();
  return {open:sb.classList.contains('open'),covering:r.right>4,backdrop:!document.querySelector('#nav-backdrop').hidden};});

await p.goto(`${BASE}/manage#dashboard`,{waitUntil:'load'}); await p.waitForTimeout(1600);
chk('drawer starts closed', !(await st()).covering);
// Spec Part XXXIV: search stays available on a phone; it must not be hidden for width.
const search=await p.evaluate(()=>{const g=document.querySelector('.global-search');if(!g)return null;const r=g.getBoundingClientRect();const cs=getComputedStyle(g);return {display:cs.display,width:r.width,inViewport:r.left>=0&&r.right<=innerWidth};});
chk('global search is visible on mobile', !!search && search.display!=='none' && search.width>80 && search.inViewport, JSON.stringify(search));

await p.click('#menu-toggle'); await p.waitForTimeout(400);
let s=await st(); chk('hamburger opens the drawer', s.covering); chk('a backdrop appears behind it', s.backdrop);
await p.screenshot({path:`${OUT}/fixed-nav-open.png`});

// 1. navigating closes it
await p.click('.nav a[data-route="library"]'); await p.waitForTimeout(1800);
s=await st(); chk('navigating closes the drawer', !s.covering, JSON.stringify(s));
chk('and the route actually changed', (await p.evaluate(()=>location.hash))==='#library');
await p.screenshot({path:`${OUT}/fixed-nav-after-navigate.png`});

// 2. tapping the entry you are already on
await p.click('#menu-toggle'); await p.waitForTimeout(400);
await p.click('.nav a[data-route="library"]'); await p.waitForTimeout(800);
chk('tapping the current page also closes it', !(await st()).covering);

// 3. backdrop
await p.click('#menu-toggle'); await p.waitForTimeout(400);
await p.click('#nav-backdrop',{position:{x:WIDTH-20,y:500}}); await p.waitForTimeout(500);
chk('tapping outside closes it', !(await st()).covering);

// 4. escape
await p.click('#menu-toggle'); await p.waitForTimeout(400);
await p.keyboard.press('Escape'); await p.waitForTimeout(400);
chk('Escape closes it', !(await st()).covering);

// 5. explicit close button, and it must be on top
await p.click('#menu-toggle'); await p.waitForTimeout(400);
const reachable=await p.evaluate(()=>{const t=document.querySelector('#nav-close');const r=t.getBoundingClientRect();
  const hit=document.elementFromPoint(Math.round(r.left+r.width/2),Math.round(r.top+r.height/2));
  return {visible:!!t.offsetParent,topmost:hit===t||t.contains(hit)};});
chk('the close button is visible and tappable', reachable.visible&&reachable.topmost, JSON.stringify(reachable));
await p.click('#nav-close'); await p.waitForTimeout(400);
chk('close button closes it', !(await st()).covering);

// tap targets
await p.click('#menu-toggle'); await p.waitForTimeout(400);
const small=await p.evaluate(()=>[...document.querySelectorAll('.nav a')].filter(a=>a.getBoundingClientRect().height<44).length);
chk('nav rows meet the 44px touch floor', small===0, `${small} still under 44px`);
await p.click('#nav-close'); await p.waitForTimeout(300);

// settings tabs
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await p.waitForTimeout(2000);
const t=await p.evaluate(()=>{const de=document.documentElement;const strip=document.querySelector('.tabs');
  return {docScroll:de.scrollWidth,clientW:de.clientWidth,stripScrolls:strip.scrollWidth>strip.clientWidth,
          stripW:Math.round(strip.getBoundingClientRect().width)};});
chk('settings no longer widens the page', t.docScroll<=t.clientW+2, JSON.stringify(t));
chk('the tab strip scrolls within itself instead', t.stripScrolls);
await p.screenshot({path:`${OUT}/fixed-mobile-settings.png`});

console.log('PASS:'); ok.forEach(x=>console.log('  ✓ '+x));
if(fails.length){console.log('\nFAIL:');fails.forEach(x=>console.log('  ✗ '+x));}
console.log(`\n${ok.length} passed, ${fails.length} failed`);
await b.close(); process.exit(fails.length?1:0);
