// ui-gate: widths=360,390,1280 order=100
// Merging duplicate books in the real UI: the "Possible duplicates" banner on the Series page (the owner's 39 Clues
// examples), the merge dialog (side-by-side cards, suggested record, plan), merge + Undo, "Merge selected..." from
// the series table and from the library's select mode, "Find duplicates" on a book page, the "Duplicate files"
// panel (remove into the trash, then Restore) and Settings > System (recent merges, trash). Runs against the gate's
// seeded instance (scripts/ui-render-seed.py). It leaves the data as it found it, so it can run at every width.
import { chromium, devices } from 'playwright';
import fs from 'fs';
import path from 'path';
const BASE=process.env.UI_BASE||'http://127.0.0.1:8899';
const WIDTH=Number(process.env.UI_WIDTH)||390;
const SEED_ROOT=process.env.UI_SEED_ROOT||'';
const fails=[], ok=[];
const chk=(n,c,d='')=>(c?ok:fails).push(`[${WIDTH}px] ${n}`+(c||!d?'':` - ${d}`));
const getJson=async u=>(await fetch(`${BASE}${u}`)).json();

const series=(await getJson('/api/v1/series')).items.find(s=>s.name==='The 39 Clues');
chk('the seeded 39 Clues series exists', !!series);
const dune=(await getJson('/api/v1/library?q=Dune%20Twice')).items.find(w=>w.title==='Dune Twice');
chk('the seeded Dune Twice book exists', !!dune);
if(!series||!dune){ console.log(fails.join('\n')); process.exit(1); }
const members=async()=>(await getJson(`/api/v1/series/${series.id}`)).works;

const b=await chromium.launch();
const ctx=await b.newContext({...devices['iPhone 13'],viewport:{width:WIDTH,height:760}});
const p=await ctx.newPage();
const errors=[]; p.on('pageerror',e=>errors.push(String(e))); p.on('dialog',d=>d.accept());
const settle=async()=>{ await p.waitForLoadState('networkidle'); await p.waitForFunction(()=>!document.querySelector('#view .skeleton'),null,{timeout:15000}).catch(()=>{}); };
const fits=async name=>{ const m=await p.evaluate(()=>{const mo=document.querySelector('.modal');return {s:document.documentElement.scrollWidth,c:document.documentElement.clientWidth,ms:mo?mo.scrollWidth:0,mc:mo?mo.clientWidth:0};});
  chk(`${name} fits the viewport`, m.s<=m.c+2 && m.ms<=m.mc+2, JSON.stringify(m)); };
const closeDialog=async()=>{ await p.evaluate(()=>{document.querySelector('#modal-root').innerHTML='';}); };
const wait=(sel,t=15000)=>p.waitForSelector(sel,{timeout:t}).catch(()=>null);
const openSeries=async()=>{ await p.goto(`${BASE}/manage#series/${series.id}`,{waitUntil:'load'}); await settle(); await wait('#series-books'); };

// 1. The banner lists exactly the two duplicated books and never mixes different books.
await openSeries();
chk('the series has its five books', (await members()).length===5);
await wait('#series-dupes .mrg-group');
const groups=await p.$$eval('#series-dupes .mrg-group',gs=>gs.map(g=>g.innerText));
chk('the banner shows two possible-duplicate groups', groups.length===2, JSON.stringify(groups));
chk('Black Circle and Sword Thief are never in one group', groups.every(g=>!(/Black Circle/.test(g)&&/Sword Thief/i.test(g))));
chk('each group names both spellings', /Black Circle \(The 39 Clues, #5\)/.test(groups[0]||'') && /The Black Circle/.test(groups[0]||''));
await fits('series page with the banner');

// 2. Review and merge the first group (Black Circle): suggestion, plan, merge, undo.
await p.click('#series-dupes .mrg-review >> nth=0');
await wait('#mrg-plan .mrg-summary');
chk('the dialog shows both records side by side', (await p.$$('.mrg-card')).length===2);
const suggest=await p.textContent('#mrg-suggest');
chk('it states why a record is suggested', /Suggested because/.test(suggest)&&/owned asset/.test(suggest), suggest);
chk('the suggested record is the one that owns the ebook', await p.$eval('.mrg-card.keep',c=>/#5\)/.test(c.innerText)));
chk('the title after the merge defaults to the clean title', (await p.$eval('#mrg-title',s=>s.value))==='The Black Circle');
chk('the plan says nothing is deleted', /No files are deleted/.test(await p.textContent('#mrg-plan')));
await fits('merge dialog');
await p.click('#mrg-go');
await wait('.mrg-result');
chk('the result names the merged book', /Merged into .The Black Circle./.test(await p.textContent('.mrg-result h3')));
chk('the series now lists four books', (await members()).length===4);
await fits('merge result');
await p.click('#mrg-undo');
await wait('.empty strong');
chk('undo says the merge is undone', /Merge undone/.test(await p.textContent('#mrg-body')));
chk('the series lists five books again', (await members()).length===5);
await closeDialog(); await settle();

// 3. Tick the two Sword Thief rows, "Merge selected...", merge, then undo from Settings > System > Recent merges.
await openSeries();
await wait('#series-books');
const rows=p.locator('#series-books tbody tr',{hasText:/sword thief/i});
chk('two Sword Thief rows are listed', await rows.count()===2);
chk('Merge selected is offered on the series page', !!(await wait('#series-merge')), (await p.evaluate(()=>location.hash+' | '+document.querySelector('#view').innerText.slice(0,200))));
chk('Merge selected is disabled with nothing ticked', !!(await p.$('#series-merge')) && await p.$eval('#series-merge',b=>b.disabled));
for(let i=0;i<2;i++) await rows.nth(i).locator('.series-pick').check();
chk('Merge selected is enabled with two ticked', !!(await wait('#series-merge:not([disabled])')));
await p.click('#series-merge');
await wait('#mrg-plan .mrg-summary');
chk('the dialog opens with the two ticked books and no warning', (await p.$$('.mrg-card')).length===2 && !(await p.$('#mrg-warn')));
await p.click('#mrg-go'); await wait('.mrg-result');
chk('the series lists four books after merging the selection', (await members()).length===4);
await closeDialog();
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await settle();
await p.waitForSelector('#settings-body > *',{timeout:20000}); await p.click('[data-stab="system"]'); await wait('#mrg-merges tbody tr[data-id]');
chk('Recent merges lists the merge with Undo', !!(await p.$('.mrg-undo-row')));
await fits('Settings > System with recent merges');
await p.click('.mrg-undo-row >> nth=0');
await p.waitForFunction(()=>!document.querySelector('.mrg-undo-row'),null,{timeout:15000}).catch(()=>{});
chk('undo from Recent merges restores the five books', (await members()).length===5);

// 4. Different books picked together are flagged and need an explicit confirmation.
const all=await members();
const black=all.find(w=>w.title==='The Black Circle'), sword=all.find(w=>w.title==='The sword thief');
await closeDialog();
await openSeries();
await wait('#series-merge');
await p.locator('#series-books tbody tr',{hasText:'The Black Circle'}).first().locator('.series-pick').check();
await p.locator('#series-books tbody tr',{hasText:'The sword thief'}).first().locator('.series-pick').check();
await p.click('#series-merge'); await wait('#mrg-warn');
chk('two different books show a warning and keep Merge disabled', !!(await p.$('#mrg-warn')) && await p.$eval('#mrg-go',b=>b.disabled));
await fits('merge dialog with a warning');
await closeDialog();

// 5. Library select mode: the bulk bar offers Merge selected.
await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await settle();
await p.fill('#library-filter','Twin Tale'); await p.waitForTimeout(800); await settle();
await p.click('#select-mode'); await wait('#bulk-bar .bulk-actions #bulk-merge');
chk('the bulk bar has Merge selected', !!(await p.$('#bulk-merge')));
const cards=p.locator('.book-card');
chk('the library filter finds both Twin Tale records', await cards.count()===2, String(await cards.count()));
chk('Merge selected is disabled until two are ticked', await p.$eval('#bulk-merge',b=>b.disabled));
for(let i=0;i<await cards.count();i++) await cards.nth(i).click();
await p.waitForTimeout(300);
chk('two ticked books enable Merge selected', !(await p.$eval('#bulk-merge',b=>b.disabled)));
await p.click('#bulk-merge'); await wait('.mrg-card');
chk('Merge selected opens the dialog from the library', (await p.$$('.mrg-card')).length===2);
await wait('#mrg-kidswiden');
chk('a kid who could see only one record makes the dialog ask, itemized, default off', !!(await p.$('#mrg-kidswiden .mrg-kw')) && !(await p.$eval('#mrg-kidswiden .mrg-kw',c=>c.checked)) && /Tiny Reader/.test(await p.textContent('#mrg-kidswiden')) && await p.$eval('#mrg-go',b=>b.disabled));
await p.check('#mrg-kidswiden .mrg-kw'); await p.waitForTimeout(200);
chk('ticking the box for that kid enables Merge', !(await p.$eval('#mrg-go',b=>b.disabled)));
await fits('library merge dialog');
await closeDialog();
await fits('library select mode');

// 5b. A kid who sees the kept record through a grant: the generic visibility blocker has its own itemized box (default off).
const pickLibrary=async (q,n)=>{ await p.goto(`${BASE}/manage#library`,{waitUntil:'load'}); await settle();
  if((await p.textContent('#select-mode')).trim()==='Cancel') await p.click('#select-mode');      // leaves select mode: the earlier ticks are forgotten
  await p.fill('#library-filter',q); await p.waitForTimeout(800); await settle(); await p.click('#select-mode');
  await wait('#bulk-merge'); const cs=p.locator('.book-card'); chk(`the library lists ${n} ${q} records`, await cs.count()===n, String(await cs.count()));
  for(let i=0;i<await cs.count();i++) await cs.nth(i).click(); await p.waitForTimeout(300); chk('Merge selected is enabled for '+q, !!(await wait('#bulk-merge:not([disabled])'))); await p.click('#bulk-merge'); await wait('.mrg-card'); await wait('#mrg-plan .mrg-summary,#mrg-kidswiden,#mrg-kids'); };
await pickLibrary('Grant Tale',2);
await wait('#mrg-kidswiden');
chk('the visibility blocker has its own box, off by default, naming the kid', !!(await p.$('#mrg-kidswiden .mrg-kw[data-code="kid_visibility_widening"]')) && !(await p.$eval('#mrg-kidswiden .mrg-kw',c=>c.checked)) && /Tiny Reader/.test(await p.textContent('#mrg-kidswiden')) && await p.$eval('#mrg-go',b=>b.disabled));
await p.check('#mrg-kidswiden .mrg-kw'); await p.waitForTimeout(200);
chk('ticking it enables Merge', !(await p.$eval('#mrg-go',b=>b.disabled)));
await closeDialog();

// 5c. A three-way merge: the Kids shelf names every record that would gain the tag and each adult one needs its own box.
await pickLibrary('Union Tale',3);
await wait('#mrg-kids .mrg-third');
chk('the Kids shelf block names the adult records that would join it', /Union Tale \(Unions, #1\)/.test(await p.textContent('#mrg-kids')) && !!(await p.$('#mrg-kids .mrg-third')));
await p.check('#mrg-kids-ok'); await p.waitForTimeout(200);
chk('the union box alone does not enable Merge', await p.$eval('#mrg-go',b=>b.disabled));
await p.check('#mrg-kids .mrg-third'); await p.waitForTimeout(200);
chk('with each adult record ticked Merge is enabled', !(await p.$eval('#mrg-go',b=>b.disabled)));
await fits('three-way Kids shelf dialog');
await closeDialog();

// 5d. Undo from Recent merges that would let a kid see a book: itemized confirmation, default off, then it works.
const api=async (m,u,body)=>fetch(`${BASE}${u}`,{method:m,headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
const shelf=(await getJson('/api/v1/library?q=Shelf%20Tale')).items;
const keep=shelf.find(w=>w.title==='Shelf Tale'), other=shelf.find(w=>w.title.startsWith('Shelf Tale ('));
const kidsTag=(await getJson('/api/v1/tags')).items.find(t=>t.name==='kids');
const merged=await (await api('POST','/api/v1/works/merge',{canonical:keep.id,sources:[other.id],resolutions:{}})).json();
chk('the Kids-shelf pair merges through the API', !!merged.merge_id, JSON.stringify(merged).slice(0,200));
await api('DELETE',`/api/v1/library/${keep.id}/tags/${kidsTag.id}`);
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await settle();
await p.waitForSelector('#settings-body > *',{timeout:20000}); await p.click('[data-stab="system"]'); await wait('.mrg-undo-row');
await p.click('.mrg-undo-row >> nth=0');
await wait('#mrg-kidundo');
chk('the history-panel undo asks, itemized with the kid, default off', /Tiny Reader/.test(await p.textContent('#mrg-kidundo')) && !(await p.$eval('#mrg-kidundo-ok',c=>c.checked)) && await p.$eval('#mrg-kidundo-go',b=>b.disabled));
await fits('kid undo confirmation');
await p.check('#mrg-kidundo-ok'); await p.click('#mrg-kidundo-go');
await p.waitForFunction(()=>!document.querySelector('.mrg-undo-row'),null,{timeout:15000}).catch(()=>{});
chk('after consenting the merge is undone', (await getJson('/api/v1/library?q=Shelf%20Tale')).items.length===2);
await api('POST',`/api/v1/library/${keep.id}/tags`,{tag_id:kidsTag.id});      // leave the seed as it was

// 6. Book page: Find duplicates, and the Duplicate files panel (remove into the trash, then Restore).
await p.goto(`${BASE}/manage#library/${black.id}`,{waitUntil:'load'}); await settle(); await wait('#mrg-find');
await p.click('#mrg-find'); await wait('.mrg-cand');
chk('Find duplicates lists the other Black Circle record', (await p.$$('.mrg-cand')).length===1 && /39 Clues, #5/.test(await p.textContent('.mrg-cand')));
await fits('book page with duplicate candidates');
await p.click('#mrg-cand-go'); await wait('.mrg-card'); chk('Review and merge opens the dialog with both', (await p.$$('.mrg-card')).length===2); await closeDialog();

await p.goto(`${BASE}/manage#library/${dune.id}`,{waitUntil:'load'}); await settle(); await wait('#mrg-dfiles');
chk('the Duplicate files panel is listed', !!(await p.$('#mrg-dfiles')));
chk('it recommends one Keep and one Remove with sizes', (await p.$$('#mrg-dfiles .mrg-file.keep')).length===1 && (await p.$$('#mrg-dfiles .mrg-file.remove')).length===1 && /KB|B/.test(await p.textContent('#mrg-dfiles .mrg-file.remove')));
await fits('book page with the Duplicate files panel');
const lib=SEED_ROOT?path.join(SEED_ROOT,'library','Frank Herbert','Dune Twice'):'';
chk('nothing is pre-ticked and Remove is disabled', (await p.$$('#mrg-dfiles .mrg-rm:checked')).length===0 && await p.$eval('#mrg-rm-go',b=>b.disabled));
chk('the group is labelled as checksum-identical', /Identical \(same checksum\)/.test(await p.textContent('#mrg-dfiles')));
await p.check('#mrg-dfiles .mrg-rm');
await p.click('#mrg-rm-go'); await wait('#mrg-rm-confirm');
chk('the summary names the file count, size and destination folder', /1 file/.test(await p.textContent('#mrg-rm-summary')) && /\.bookdoe-trash\/\d{4}-\d\d-\d\d/.test(await p.textContent('#mrg-rm-summary')));
chk('the confirmation explains the trash and retention', /bookdoe-trash/.test(await p.textContent('.modal')));
await fits('remove confirmation');
await p.click('#mrg-rm-confirm');
await p.waitForFunction(()=>!document.querySelector('#mrg-dfiles'),null,{timeout:15000}).catch(()=>{});
chk('the panel disappears once the copy is removed', !(await p.$('#mrg-dfiles')));
if(lib){
  chk('the redundant file moved out of the library folder', !fs.existsSync(path.join(lib,'copy','Dune Twice.epub')));
  chk('the kept file is untouched', fs.existsSync(path.join(lib,'Dune Twice.epub')));
  chk('the file is in the per-library trash folder', fs.existsSync(path.join(SEED_ROOT,'library','.bookdoe-trash')));
}
await p.goto(`${BASE}/manage#settings`,{waitUntil:'load'}); await settle();
await p.waitForSelector('#settings-body > *',{timeout:20000}); await p.click('[data-stab="system"]'); await wait('#mrg-trash .mrg-restore');
chk('the trash lists the file with Restore and Delete now', !!(await p.$('#mrg-trash .mrg-restore')) && !!(await p.$('#mrg-trash .mrg-purge')));
chk('retention defaults to 30 days', (await p.$eval('#mrg-retention',i=>i.value))==='30');
await fits('Settings > System with the trash');
await p.click('#mrg-trash .mrg-restore >> nth=0');
await p.waitForFunction(()=>!document.querySelector('#mrg-trash .mrg-restore'),null,{timeout:15000}).catch(()=>{});
if(lib) chk('Restore puts the file back where it was', fs.existsSync(path.join(lib,'copy','Dune Twice.epub')));
await p.goto(`${BASE}/manage#library/${dune.id}`,{waitUntil:'load'}); await settle(); await wait('#mrg-dfiles');
chk('after the restore the panel offers the copy again', !!(await p.$('#mrg-dfiles')));

chk('no uncaught page errors', errors.length===0, errors.join(' | '));
await b.close();
console.log(ok.map(x=>'ok   '+x).join('\n')); if(fails.length){ console.error(fails.map(x=>'FAIL '+x).join('\n')); process.exit(1); }
