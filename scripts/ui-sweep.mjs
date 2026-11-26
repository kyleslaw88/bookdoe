import { chromium, devices } from 'playwright';
import fs from 'fs';
const BASE='http://127.0.0.1:8899', OUT='/tmp/qa/shots';
const ROUTES=['dashboard','library','authors','discover','wanted','calendar','queue','review','history','settings'];
const VIEWPORTS=[
  {name:'mobile',opts:{...devices['iPhone 13']}},
  {name:'tablet',opts:{viewport:{width:820,height:1100},hasTouch:true,isMobile:true}},
  {name:'desktop',opts:{viewport:{width:1440,height:900}}},
];
const findings=[];
const b=await chromium.launch();
for(const vp of VIEWPORTS){
  const ctx=await b.newContext(vp.opts); const page=await ctx.newPage();
  const errs=[]; let notFound=0;
  page.on('console',m=>{if(m.type()==='error')errs.push(m.text())});
  page.on('pageerror',e=>errs.push('pageerror: '+e.message));
  page.on('response',r=>{if(r.status()===404)notFound++});
  for(const route of ROUTES){
    errs.length=0; notFound=0;
    await page.goto(`${BASE}/manage#${route}`,{waitUntil:'load'}); await page.waitForTimeout(1800);
    const m=await page.evaluate(()=>{
      const de=document.documentElement;
      const small=[...document.querySelectorAll('button,a,input,select,.tab')].filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.height<44});
      const sb=document.querySelector('#sidebar').getBoundingClientRect();
      return {scrollW:de.scrollWidth,clientW:de.clientWidth,small:small.length,
        pageH:Math.round(document.body.scrollHeight),sidebarCovering:sb.right>4,
        empty:(document.querySelector('#view')?.textContent||'').trim().length<40};
    });
    await page.screenshot({path:`${OUT}/v2-${vp.name}-${route}.png`});
    if(m.scrollW>m.clientW+2) findings.push(`[${vp.name}/${route}] horizontal scroll ${m.scrollW}px in ${m.clientW}px`);
    if(m.empty) findings.push(`[${vp.name}/${route}] view empty`);
    if(errs.length) findings.push(`[${vp.name}/${route}] console: ${errs.slice(0,2).join(' | ')}`);
    if(notFound) findings.push(`[${vp.name}/${route}] ${notFound} 404 request(s)`);
    if(vp.name!=='desktop'&&m.sidebarCovering) findings.push(`[${vp.name}/${route}] sidebar covering content`);
    if(vp.name==='mobile'&&m.small) findings.push(`[${vp.name}/${route}] ${m.small} tap targets <44px`);
    if(m.pageH>25000) findings.push(`[${vp.name}/${route}] page ${m.pageH}px tall (${Math.round(m.pageH/(vp.opts.viewport?.height||664))} screens)`);
  }
  await ctx.close();
}
await b.close();
fs.writeFileSync('/tmp/qa/findings-pass2.json',JSON.stringify(findings,null,1));
console.log(findings.length?findings.join('\n'):'no findings');
console.log(`\ntotal: ${findings.length}`);
