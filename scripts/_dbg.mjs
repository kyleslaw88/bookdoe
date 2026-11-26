import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE; const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:390,height:560},hasTouch:true}); const p=await ctx.newPage();
await p.goto(`${BASE}/manage#dashboard`,{waitUntil:'load'}); await p.waitForSelector('#nav a');
await p.click('#menu-toggle'); await p.waitForTimeout(400);
console.log(JSON.stringify(await p.evaluate(()=>{const nav=document.querySelector('#nav'),sb=document.querySelector('#sidebar');const cs=getComputedStyle(nav);const last=[...nav.querySelectorAll('a')].pop();
 nav.scrollTop=nav.scrollHeight; const r=last.getBoundingClientRect();
 return {navOverflow:cs.overflowY,navH:nav.clientHeight,navSH:nav.scrollHeight,navScrollTop:nav.scrollTop,navRect:nav.getBoundingClientRect().toJSON(),sbRect:sb.getBoundingClientRect().toJSON(),sbH:getComputedStyle(sb).height,lastRect:r.toJSON(),inner:innerHeight,sbDisplay:getComputedStyle(sb).display,sbOverflow:getComputedStyle(sb).overflowY,bottom:document.querySelector('.sidebar-bottom').getBoundingClientRect().toJSON()}}),null,1));
await p.screenshot({path:'/tmp/alx-drawer.png'}); await b.close();
