// ui-gate: widths=360,390,1280 order=70
// Format conversion in the real UI (issue #265): the per-asset Convert button on the book page,
// the bulk action's confirmation dialog in select mode, and Settings > Conversion with the
// Conversions list. Runs against the gate's seeded instance (scripts/ui-render-seed.py puts a
// MOBI "Tiny Book" and an MP3 "Spoken Tiny" in a managed root). The first width converts the
// MOBI to EPUB through the real queue; later widths see the finished state.
import { chromium, devices } from 'playwright';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const fails=[], ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(`[${WIDTH}px] ${n}`+(c||!d?'':` - ${d}`));

const find=async q=>(await (await fetch(`${BASE}/api/v1/library?q=${encodeURIComponent(q)}`)).json()).items.find(w=>w.title===q);
const tiny=await find('Tiny Book'); chk('the seeded MOBI book exists', !!tiny);
if(!tiny){ console.log(fails.join('\n')); process.exit(1); }

const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:700}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e)));
const fits=async name=>{ const m=await p.evaluate(()=>({s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth}));
  chk(`${name} fits the viewport`, m.s<=m.c+2, JSON.stringify(m)); };
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton'),null,{timeout:15000}).catch(()=>{}); };
const openBook=async()=>{ await p.goto(`${BASE}/manage#library/${tiny.id}`,{waitUntil:'load'}); await settle(); await p.waitForSelector('.asset-row',{timeout:15000}); };

// 1. Book page: the Convert button, then the converted copy with its label.
await openBook();
const first=await p.$('.asset-convert');
if(first){
  const label=(await first.textContent()).trim();
  chk('the button names the target format', /Convert to EPUB/.test(label), label);
  await fits('book page with the button');
  await first.click();
  await p.waitForSelector('.toast', {timeout:10000});
  chk('a toast says the original is kept', /original is kept/i.test(await p.textContent('#toast-stack')));
  // the page watches the queue and redraws itself when the conversion has finished
  chk('the page refreshes itself when the conversion finishes', !!(await p.waitForSelector('.asset-derived',{timeout:15000}).catch(()=>null)));
}
// the queue runs in a worker; wait for the derived asset to appear (reload: the hash alone does not refetch)
let labelled=null;
for(let i=0;i<60&&!labelled;i++){ await p.waitForTimeout(500); await p.reload({waitUntil:'load'}); await settle(); await p.waitForSelector('.asset-row',{timeout:15000}); labelled=await p.$('.asset-derived'); }
chk('the converted copy is labelled "Converted from MOBI"', !!labelled && /Converted from MOBI/.test(await labelled.textContent()));
chk('the original asset row is still listed', (await p.$$('.asset-row')).length>=2);
chk('no Convert button is offered once an EPUB copy exists', !(await p.$('.asset-convert')));
await fits('book page with the converted copy');

// 1b. A book in a READ-ONLY root: the control is visible with the note; a DRM book shows it disabled with the reason.
const ro=await find('Read Only Tiny'); chk('the seeded read-only-root audiobook exists', !!ro);
if(ro){
  await p.goto(`${BASE}/manage#library/${ro.id}`,{waitUntil:'load'}); await settle(); await p.waitForSelector('.asset-row',{timeout:15000});
  const btn=await p.$('.asset-convert');
  chk('Convert to M4B is offered for a book in a read-only root', !!btn && /Convert to M4B/.test(await btn.textContent()));
  chk('with the managed-library note', /Saved in your managed library; the original stays where it is/.test(await p.textContent('.asset-convert-box')));
  await fits('read-only-root book page');
  await p.screenshot({path:`${process.env.UI_SHOTS||'/tmp/qa/shots'}/convert-readonly-${WIDTH}.png`}).catch(()=>{});
}
const locked=await find('Locked Tiny'); chk('the seeded DRM book exists', !!locked);
if(locked){
  await p.goto(`${BASE}/manage#library/${locked.id}`,{waitUntil:'load'}); await settle(); await p.waitForSelector('.asset-row',{timeout:15000});
  const off=await p.$('.asset-convert-off');
  chk('a blocked book still shows a Convert button', !!off && /Convert to EPUB/.test(await off.textContent()));
  chk('it is disabled and carries the reason as a tooltip', !!off && (await off.isDisabled()) && /DRM/.test((await off.getAttribute('title'))||''));
  chk('the reason is also visible text', /DRM/.test(await p.textContent('.asset-convert-note').catch(()=>'')));
  chk('and no live Convert button is offered', !(await p.$('.asset-convert')));
  await fits('blocked book page');
}

// 2. Settings > Conversion: the form and the list.
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await settle();
await p.waitForSelector('#settings-body > *',{timeout:20000}); await p.click('[data-stab="conversion"]'); await p.waitForSelector('#conv-settings',{timeout:10000});
chk('the destination roots are selectable (Automatic by default)', (await p.$eval('[data-conv="audiobook_output_root"]',i=>i.value))==='' && !!(await p.$('[data-conv="ebook_output_root"]')));
chk('auto-convert on import is off by default', (await p.$eval('[data-conv="auto_on_import"]',i=>i.checked))===false);
chk('the audio bitrate defaults are 64 and 96', (await p.$eval('[data-conv="audio_bitrate_mono_kbps"]',i=>i.value))==='64' && (await p.$eval('[data-conv="audio_bitrate_stereo_kbps"]',i=>i.value))==='96');
await p.waitForSelector('#conv-list table', {timeout:10000});
const row=await p.textContent('#conv-list');
chk('the Conversions list shows the finished conversion', /Tiny Book/.test(row) && /done/.test(row), row.slice(0,200));
await fits('Settings > Conversion');
await p.screenshot({path:`${process.env.UI_SHOTS||'/tmp/qa/shots'}/convert-settings-${WIDTH}.png`}).catch(()=>{});

// 3. Library select mode: the bulk action opens a dialog that says originals are kept.
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await settle();
await p.click('#select-mode'); await p.waitForSelector('#bulk-bar .bulk-actions',{timeout:10000});
await p.waitForSelector('#bulk-convert',{timeout:5000}).catch(()=>{});
chk('select mode offers "Convert selected to preferred formats"', !!(await p.$('#bulk-convert')));
chk('and it is disabled until something is selected', (await p.$eval('#bulk-convert',b=>b.disabled).catch(()=>null))===true);
await p.click('#select-all-shown'); await p.waitForFunction(()=>!document.querySelector('#bulk-convert')?.disabled,null,{timeout:20000}).catch(()=>{});
await p.click('#bulk-convert'); await p.waitForSelector('#bulk-convert-count',{timeout:20000});
const dialog=await p.textContent('#modal-root');
chk('the dialog says originals are kept and shows a disk estimate', /Originals are kept/.test(dialog) && /Estimated disk use/.test(dialog), dialog.slice(0,200));
chk('the dry run counted the seeded audiobooks, the read-only-root one included (Spoken Tiny, Many Tracks, Read Only Tiny)', /3 audiobook\(s\) to M4B/.test(dialog), dialog.slice(0,300));
chk('the dry run shows will-convert / already-preferred / blocked counts', await p.$('#bulk-convert-categories [data-cat="will_convert"]') && await p.$('#bulk-convert-categories [data-cat="already_preferred"]') && await p.$('#bulk-convert-categories [data-blocked="drm"]'), dialog.slice(0,400));
chk('it names the destination root with its free space and the read-only note', /library/.test(await p.textContent('#bulk-convert-destinations').catch(()=>'')) && /free/.test(await p.textContent('#bulk-convert-destinations').catch(()=>'')) && /read-only library roots/.test(dialog), dialog.slice(0,500));
chk('the margin is stated and the Convert button is enabled (it fits)', /margin/.test(dialog) && (await p.$eval('#bulk-convert-confirm',b=>!b.disabled)), dialog.slice(0,300));
await fits('bulk dialog');
await p.click('#modal-root .modal-cancel');
chk('cancelling queues nothing', (await (await fetch(`${BASE}/api/v1/conversions?group=active`)).json()).total===0);

chk('no script errors', errors.length===0, errors.join(' | '));
await b.close();
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log(`ui-convert ${WIDTH}px: ${ok.length} checks passed`);
