// ui-gate: widths=360,390,1280 order=130
// The managed library build in the real UI: the entry points the owner looks for (Settings > Library roots banner and the
// per-root "Copy into managed library..." action with plain-words modes and counts, Settings > Jobs link, the Overview
// card, the book page state with "Copy to managed library..." and the Source copies disclosure), the one build panel
// (dry run summary, typed-count confirm, start, live progress to completion, history, Undo build). Runs against the
// gate's seeded instance (scripts/ui-render-seed.py: an adopted root "adopted-books" with three books). It undoes its
// build at the end, so it runs at every width.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[], ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(`[${WIDTH}px] ${n}`+(c||!d?'':` - ${d}`));
const getJson=async u=>(await fetch(`${BASE}${u}`)).json();
const post=async (u,body,method='POST')=>{ const s=await getJson('/api/v1/auth/me'); return fetch(`${BASE}${u}`,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf||''},body:JSON.stringify(body||{})}); };
// Builds may only run while the admin switch is on (default off): every width starts with it off.
await post('/api/v1/managed-build/settings',{run_enabled:false},'PUT');

// A previous width left a finished build? Undo it first (through the API) so this width starts from "not copied yet".
for(const r of (await getJson('/api/v1/managed-build/runs')).items||[]){
  if(r.active_books) await post(`/api/v1/managed-build/runs/${r.run_id}/undo`,{confirm_count:r.active_books});
}
const copyMe=(await getJson('/api/v1/library?q=Copy%20Me')).items.find(w=>w.title==='Copy Me');
chk('the seeded adopted book exists', !!copyMe);
if(!copyMe){ console.log(fails.join('\n')); process.exit(1); }

const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:760}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e))); p.on('dialog',d=>d.accept());
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton'),null,{timeout:15000}).catch(()=>{}); };
const fits=async name=>{ const m=await p.evaluate(()=>{const mo=document.querySelector('.modal');return {s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth,ms:mo?mo.scrollWidth:0,mc:mo?mo.clientWidth:0};});
  chk(`${name} fits the viewport`, m.s<=m.c+2 && m.ms<=m.mc+2, JSON.stringify(m)); };
const wait=(sel,t=15000)=>p.waitForSelector(sel,{timeout:t}).catch(()=>null);
const openRoots=async()=>{ await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await settle(); await p.waitForSelector('#settings-body > *',{timeout:20000}).catch(()=>{}); await p.click('[data-stab="roots"]').catch(()=>{}); await wait('#mb-roots-banner'); };

// 1. Settings > Library roots: banner, plain-words modes, per-root counts, the per-root action.
await openRoots();
const banner=await p.textContent('#mb-roots-banner').catch(()=>'');
chk('the roots tab has the managed-library banner with coverage', /of \d+ books have a verified managed copy/.test(banner), banner);
chk('the banner offers "Build managed library..."', !!(await p.$('#mb-build-open')));
const rootText=await p.$eval('.integration-card[data-root]:has(.mb-root-copy)',c=>c.innerText).catch(()=>'');
chk('the adopted root says what adopt means', /Adopt: read in place, never modified/.test(rootText), rootText);
chk('the adopted root shows its book count and coverage', /3 books/.test(rootText) && /with a managed copy/.test(rootText), rootText);
const managedText=await p.$eval('.integration-card[data-root]:not(:has(.mb-root-copy))',c=>c.innerText).catch(()=>'');
chk('a managed root says what managed means', /Managed: bookdoe stores and organizes the files here/.test(managedText), managedText);
await fits('Settings > Library roots');

// 2. The per-root action opens the one panel, pre-filtered to that root: dry run, summary, typed confirm, start.
await p.click('.mb-root-copy');
await wait('#mb-dry-run');
chk('the panel names the chosen folder', /Only the folder adopted-books/.test(await p.textContent('#mb-panel-host')));
chk('the panel says originals are never changed', /Originals are never changed/.test(await p.textContent('#mb-panel-host')));
await fits('build panel');
await p.click('#mb-dry-run');
await wait('#mb-summary');
const summary=await p.textContent('#mb-summary');
chk('the dry run shows 3 books and what happens to them', /Dry run: 3 book\(s\)/.test(summary) && /Copy \(already EPUB\/M4B\)/.test(summary) && /Copy as it is/.test(summary), summary.slice(0,300));
chk('the dry run shows free space and a time estimate', /free/.test(summary) && /Estimated time/.test(summary));
await wait('#mb-item-list li');
chk('the dry run lists the files', (await p.$$('#mb-item-list li')).length>=3);
await fits('dry run summary');
chk('Confirm stays disabled until the number is typed', await p.$eval('#mb-confirm',x=>x.disabled));
await p.fill('#mb-confirm-count','2');
chk('a wrong number keeps Confirm disabled', await p.$eval('#mb-confirm',x=>x.disabled));
await p.fill('#mb-confirm-count','3');
await p.click('#mb-confirm');
await p.waitForFunction(()=>/Confirmed:/.test(document.querySelector('#mb-summary')?.innerText||''),null,{timeout:15000}).catch(()=>{});
chk('with "Allow builds to run" off, Start stays disabled and says why', await p.$eval('#mb-start',x=>x.disabled).catch(()=>false) && /Allow builds to run/.test(await p.textContent('#mb-summary')));
chk('the switch explains itself in plain words', /Off: builds cannot start or resume/.test(await p.textContent('#mb-run-switch')));
const planNow=await getJson('/api/v1/managed-build/plans/latest');
chk('the API refuses to start while the switch is off (409)', (await post(`/api/v1/managed-build/plans/${planNow.plan_id}/start`,{})).status===409);
await fits('run switch');
await p.click('#mb-run-enabled');                 // the warning dialog is accepted by the dialog handler
await wait('#mb-start:not([disabled])');
chk('switching it on enables Start', !!(await p.$('#mb-start:not([disabled])')));
chk('the switch says turning it off pauses a running build after the current file', /Turning this off pauses a running build after the current file/.test(await p.textContent('#mb-run-switch')));
const runsBefore=((await getJson('/api/v1/managed-build/runs')).items||[]).length;
await p.click('#mb-start');
// the build is small: wait until the new run exists and has finished (through the API), then check the UI shows it
let done=false;
for(let i=0;i<300&&!done;i++){
  const runs=(await getJson('/api/v1/managed-build/runs')).items||[];
  done=runs.length>runsBefore && runs[0].status==='completed';
  if(!done) await p.waitForTimeout(200);
}
chk('the build finishes', done);
await openRoots();
const banner2=await p.textContent('#mb-roots-banner');
chk('the banner now counts the copied books', /3 of \d+ books have a verified managed copy/.test(banner2), banner2);
await p.click('#mb-build-open'); await wait('.mb-run-row');
chk('the Undo build area warns that books with listening/reading history are kept', /listened to or read/.test(await p.textContent('.mb-run-row')));
chk('the history lists the completed build with Undo', /completed/.test(await p.textContent('.mb-run-row')) && !!(await p.$('.mb-undo-btn')));
await fits('build history');

// 3. Settings > Jobs links to the same panel.
await p.waitForSelector('#settings-body > *',{timeout:20000}); await p.click('[data-stab="jobs"]'); await wait('#mb-open-from-jobs');
chk('Settings > Jobs has the link to the build panel', !!(await p.$('#mb-open-from-jobs')));
await p.click('#mb-open-from-jobs'); await wait('#mb-panel-host .mb-intro');
chk('the Jobs link opens the panel on Library roots', !!(await p.$('#mb-panel-host .mb-intro')));

// 4. Overview card.
await p.goto(`${BASE}/manage#dashboard`,{waitUntil:'load'}); await settle(); await wait('#mb-overview .panel-body');
chk('Overview has the Managed library card', /books have a verified managed copy/.test(await p.textContent('#mb-overview').catch(()=>'')));
await fits('Overview');

// 5. Book page: verified state, source copies disclosure, undo for one book, then the one-book copy dialog.
await p.goto(`${BASE}/manage#library/${copyMe.id}`,{waitUntil:'load'}); await settle(); await wait('#mb-book .mb-book-state');
chk('the book page says Managed copy: verified', /Managed copy: verified/.test(await p.textContent('#mb-book')));
chk('the Source copies disclosure lists the original', /Source copies \(1\)/.test(await p.textContent('#mb-book .mb-sources summary').catch(()=>'')));
await fits('book page');
await p.click('#mb-book-undo');
await p.waitForFunction(()=>/Not copied yet/.test(document.querySelector('#mb-book')?.innerText||''),null,{timeout:15000}).catch(()=>{});
chk('after Undo the book page says Not copied yet', /Not copied yet/.test(await p.textContent('#mb-book')));
await p.click('#mb-book-copy'); await wait('#mb-dialog-panel #mb-dry-run');
chk('the one-book dialog is limited to this book', /Only this book/.test(await p.textContent('#mb-dialog-panel')));
await p.click('#mb-dialog-panel #mb-dry-run'); await wait('#mb-dialog-panel #mb-summary');
chk('the one-book dry run plans exactly one book', /Dry run: 1 book\(s\)/.test(await p.textContent('#mb-dialog-panel #mb-summary')));
await fits('one-book dialog');
await p.evaluate(()=>{document.querySelector('#modal-root').innerHTML='';});

// Leave the data as found: undo what is left of the build.
for(const r of (await getJson('/api/v1/managed-build/runs')).items||[]){
  if(r.active_books) await post(`/api/v1/managed-build/runs/${r.run_id}/undo`,{confirm_count:r.active_books});
}
await post('/api/v1/managed-build/settings',{run_enabled:false},'PUT');
chk('everything is undone at the end', (await getJson('/api/v1/managed-build/status')).coverage.books_covered===0);
chk('no uncaught page errors', errors.length===0, errors.join(' | '));
await b.close();
console.log(ok.map(x=>'ok   '+x).join('\n')); if(fails.length){ console.error(fails.map(x=>'FAIL '+x).join('\n')); process.exit(1); }
