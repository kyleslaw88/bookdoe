// ui-gate: widths=360,390 order=10
// Fails when any route scrolls horizontally. Width comes from UI_WIDTH (default 390).
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const ROUTES=["dashboard","library","authors","discover","wanted","calendar","queue","requests","review","history","settings"];
const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:664}});
const p=await ctx.newPage();
const fails=[];
for(const route of ROUTES){
  await p.goto(`${BASE}/manage#${route}`,{waitUntil:'load'});
  await p.waitForLoadState('networkidle');
  // A hash change is a same-document navigation, so 'networkidle' can resolve before
  // the page's own fetch has started; wait for the loading skeleton to be replaced.
  await p.waitForFunction(()=>!document.querySelector('#view .skeleton'),null,{timeout:15000}).catch(()=>{});
  const m=await p.evaluate(()=>({scrollW:document.documentElement.scrollWidth,clientW:document.documentElement.clientWidth,
    empty:(document.querySelector('#view')?.textContent||'').trim().length<40}));
  if(m.scrollW>m.clientW+2) fails.push(`[${WIDTH}px/${route}] horizontal overflow: ${m.scrollW}px in ${m.clientW}px`);
  if(m.empty) fails.push(`[${WIDTH}px/${route}] view did not render`);
}
await b.close();
if(fails.length){console.log(fails.join('\n'));process.exit(1);}
console.log(`ui-overflow ${WIDTH}px: ${ROUTES.length} routes fit`);
