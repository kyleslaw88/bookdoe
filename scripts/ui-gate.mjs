// `make uigate`: seed a throwaway database, start the app on a free port, run the
// headless mobile checks at 360 and 390 px, tear everything down. Exit code is the
// gate result. See scripts/README-ui-qa.md.
import { spawn, spawnSync } from 'child_process';
import fs from 'fs';
import net from 'net';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

const REPO=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const APP=path.join(REPO,'bookdoe');
const PY=process.env.PYTHON||'python';
// Checks run on the shared seeded instance are found by file name: every scripts/ui-*.mjs that
// carries a header line `// ui-gate: widths=360,390,1280 order=140` runs at those widths. A new check is
// one new file, so feature branches no longer edit a shared list. `order` is the run position on the
// shared instance (state-changing checks must stay after the read-only ones); checks are sorted by
// order, then file name, and one without `order` runs last.
// Scripts that need their own instance (run further down) or are not part of the gate are listed
// here, one per line; a ui-*.mjs with neither a header nor an entry fails the gate, so a new check
// is never silently skipped.
const OWN_INSTANCE=[
  'ui-player-e2e.mjs',
  'ui-discover-chat.mjs',
  'ui-discover-stub.mjs',
  'ui-https-session.mjs',
];
const NOT_GATED=[
  'ui-flows.mjs',
  'ui-metadata-picker.mjs',
  'ui-render-check.mjs',
  'ui-search.mjs',
  'ui-sweep.mjs',
];
function discoverPlan(dir){
  const byWidth=new Map(), unclassified=[], found=[];
  for(const f of fs.readdirSync(dir).filter(n=>/^ui-.*\.mjs$/.test(n)&&n!=='ui-gate.mjs').sort()){
    const m=fs.readFileSync(path.join(dir,f),'utf8').split('\n',10).join('\n').match(/^\/\/\s*ui-gate:\s*widths=([\d,]+)(?:\s+order=(\d+))?\s*$/m);
    if(!m){ if(!OWN_INSTANCE.includes(f)&&!NOT_GATED.includes(f)) unclassified.push(f); continue; }
    found.push({f,order:m[2]===undefined?Infinity:parseInt(m[2],10),widths:m[1].split(',').map(x=>parseInt(x,10)).filter(Number.isFinite)});
  }
  found.sort((a,b)=>(a.order===b.order?0:a.order<b.order?-1:1)||(a.f<b.f?-1:a.f>b.f?1:0));
  for(const {f,widths} of found) for(const w of widths){
    if(!byWidth.has(w)) byWidth.set(w,[]);
    byWidth.get(w).push(f);
  }
  return {plan:[...byWidth.entries()].sort((a,b)=>a[0]-b[0]),unclassified};
}
const SCRIPTS=path.join(REPO,'scripts');
const {plan:PLAN,unclassified}=discoverPlan(process.argv[2]==='--list'&&process.argv[3]?path.resolve(process.argv[3]):SCRIPTS);
if(unclassified.length){
  console.error(`ui-gate: no "// ui-gate: widths=..." header and not listed in OWN_INSTANCE/NOT_GATED: ${unclassified.join(', ')}`);
  process.exit(2);
}
const WIDTHS=PLAN.map(x=>x[0]); const CHECKS=[...new Set(PLAN.flatMap(x=>x[1]))];
if(process.argv[2]==='--list'){
  for(const [w,cs] of PLAN) console.log(`${w}: ${cs.join(' ')}`);
  process.exit(0);
}

try{ await import('playwright'); }catch{
  console.error('ui-gate: playwright is not installed. Run: npm install && npx playwright install chromium');
  process.exit(2);
}

const root=fs.mkdtempSync(path.join(os.tmpdir(),'bookdoe-uigate-'));
const env={...process.env,
  BOOKDOE_DB_PATH:`${root}/bookdoe.db`,BOOKDOE_DATA_DIR:root,BOOKDOE_BACKUP_DIR:`${root}/backups`,
  BOOKDOE_COVER_DIR:`${root}/covers`,BOOKDOE_AUTH_MODE:'none',BOOKDOE_SCHEDULER_ENABLED:'0',BOOKDOE_ACQUISITION_ENABLED:'1',
  BOOKDOE_SECRET_KEY:'uigate-secret-key-long-enough',PYTHONPATH:'.',UI_SEED_ROOT:root,UI_SHOTS:`${root}/shots`};

let server=null, code=1;
const cleanup=()=>{ if(server&&server.exitCode===null) server.kill('SIGTERM'); fs.rmSync(root,{recursive:true,force:true}); };
process.on('SIGINT',()=>{cleanup();process.exit(130);});
process.on('SIGTERM',()=>{cleanup();process.exit(143);});

const freePort=()=>new Promise((res,rej)=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const {port}=s.address();s.close(()=>res(port));});s.on('error',rej);});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

try{
  const seed=spawnSync(PY,[path.join(REPO,'scripts/ui-render-seed.py')],{cwd:APP,env,stdio:'inherit'});
  if(seed.status!==0) throw new Error('seeding the temp database failed');

  const port=await freePort(), base=`http://127.0.0.1:${port}`;
  server=spawn(PY,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:APP,env,stdio:['ignore','ignore','inherit']});
  let up=false;
  for(let i=0;i<150&&!up&&server.exitCode===null;i++){
    up=await fetch(`${base}/`).then(r=>r.ok,()=>false);
    if(!up) await sleep(200);
  }
  if(!up) throw new Error('app did not start');

  const failed=[];
  for(const [width,checks] of PLAN) for(const check of checks){
    const r=spawnSync('node',[path.join(REPO,'scripts',check)],{env:{...env,UI_BASE:base,UI_WIDTH:String(width)},stdio:'inherit'});
    if(r.status!==0) failed.push(`${check} @${width}px`);
  }
  // Second instance: local auth with admin/adult/kid, covers and a real EPUB, driven in a
  // real browser with the service worker on (scripts/ui-player-e2e.mjs).
  if(server&&server.exitCode===null) server.kill('SIGTERM');
  const root2=fs.mkdtempSync(path.join(os.tmpdir(),'bookdoe-uigate-e2e-'));
  const smtpPort=await freePort(), smtpOut=`${root2}/mail.jsonl`;
  const env2={...env,BOOKDOE_DB_PATH:`${root2}/bookdoe.db`,BOOKDOE_DATA_DIR:root2,BOOKDOE_BACKUP_DIR:`${root2}/backups`,
    BOOKDOE_COVER_DIR:`${root2}/covers`,BOOKDOE_AUTH_MODE:'local',UI_SEED_ROOT:root2,BOOKDOE_PUBLIC_URL:'http://127.0.0.1',
    UI_SMTP_PORT:String(smtpPort),UI_SMTP_OUT:smtpOut};
  // A local SMTP sink stands in for the mail relay so Send-to-Kindle is exercised for real
  // up to the wire (scripts/ui-smtp-sink.py); nothing leaves the machine.
  let sink=spawn(PY,[path.join(REPO,'scripts/ui-smtp-sink.py'),'--port',String(smtpPort),'--out',smtpOut],{stdio:['ignore','ignore','inherit']});
  try{
    const seed2=spawnSync(PY,[path.join(REPO,'scripts/ui-player-seed.py')],{cwd:APP,env:env2,encoding:'utf8'});
    if(seed2.status!==0){ console.error(seed2.stdout, seed2.stderr); throw new Error('seeding the e2e database failed'); }
    const seedJson=seed2.stdout.split('\n').find(l=>l.startsWith('E2E '))?.slice(4);
    const port2=await freePort(), base2=`http://127.0.0.1:${port2}`;
    server=spawn(PY,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port2)],{cwd:APP,env:env2,stdio:['ignore','ignore','inherit']});
    let up2=false;
    for(let i=0;i<150&&!up2&&server.exitCode===null;i++){ up2=await fetch(`${base2}/health`).then(r=>r.ok,()=>false); if(!up2) await sleep(200); }
    if(!up2) throw new Error('e2e app did not start');
    const r=spawnSync('node',[path.join(REPO,'scripts/ui-player-e2e.mjs')],{env:{...env2,UI_BASE:base2,UI_SEED:seedJson},stdio:'inherit'});
    if(r.status!==0) failed.push('ui-player-e2e.mjs');
  } finally { if(sink&&sink.exitCode===null) sink.kill('SIGTERM'); fs.rmSync(root2,{recursive:true,force:true}); }
  // Discover chat: its own local-auth instance with the model and the metadata provider stubbed
  // (scripts/ui-discover-stub.mjs), driven at 360, 390 and 1280 px (scripts/ui-discover-chat.mjs).
  {
    const root4=fs.mkdtempSync(path.join(os.tmpdir(),'bookdoe-uigate-chat-'));
    let stub=null;
    try{
      const stubPort=await freePort();
      stub=spawn('node',[path.join(REPO,'scripts/ui-discover-stub.mjs'),String(stubPort)],{stdio:['ignore','ignore','inherit']});
      for(let i=0;i<50;i++){ if(await new Promise(r=>{const s=net.connect(stubPort,'127.0.0.1',()=>{s.destroy();r(true)});s.on('error',()=>r(false));})) break; await sleep(100); }
      const env4={...env,BOOKDOE_DB_PATH:`${root4}/bookdoe.db`,BOOKDOE_DATA_DIR:root4,BOOKDOE_BACKUP_DIR:`${root4}/backups`,
        BOOKDOE_COVER_DIR:`${root4}/covers`,BOOKDOE_AUTH_MODE:'local',UI_SEED_ROOT:root4,BOOKDOE_PUBLIC_URL:'http://127.0.0.1',UI_SMTP_PORT:'1',
        BOOKDOE_LLM_ENABLED:'1',BOOKDOE_LLM_BASE_URL:`http://127.0.0.1:${stubPort}`,BOOKDOE_LLM_MODEL:'stub-model',BOOKDOE_LLM_TIMEOUT:'20',
        OPENREADS_URL:`http://127.0.0.1:${stubPort}`};
      const seed4=spawnSync(PY,[path.join(REPO,'scripts/ui-player-seed.py')],{cwd:APP,env:env4,encoding:'utf8'});
      if(seed4.status!==0){ console.error(seed4.stdout, seed4.stderr); throw new Error('seeding the Discover chat database failed'); }
      // "Plain Novel" is a library book hidden from kids; give it a file so it counts as in the library for adults.
      const fix4=spawnSync(PY,['-c',"import sys;sys.path.insert(0,'.')\nfrom app import db\nw=db.one(\"SELECT id FROM works WHERE title='Plain Novel'\")\na=db.one('SELECT id FROM media_assets WHERE work_id=? AND status=\\'owned\\'',(w['id'],))\nwith db.tx() as c: c.execute(\"INSERT INTO media_files(id,asset_id,path,size_bytes,embedded_json,adopted,managed,missing,discovered_at) VALUES ('pn1',?,'/books/Plain_Novel.epub',10,'{}',1,0,0,?)\",(a['id'],db.now()))"],{cwd:APP,env:env4,encoding:'utf8'});
      if(fix4.status!==0){ console.error(fix4.stderr); throw new Error('adding the Plain Novel file failed'); }
      if(server&&server.exitCode===null) server.kill('SIGTERM');
      const port4=await freePort(), base4=`http://127.0.0.1:${port4}`;
      server=spawn(PY,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port4)],{cwd:APP,env:env4,stdio:['ignore','ignore','inherit']});
      let up4=false;
      for(let i=0;i<150&&!up4&&server.exitCode===null;i++){ up4=await fetch(`${base4}/health`).then(r=>r.ok,()=>false); if(!up4) await sleep(200); }
      if(!up4) throw new Error('Discover chat app did not start');
      for(const width of WIDTHS){
        const r4=spawnSync('node',[path.join(REPO,'scripts/ui-discover-chat.mjs')],{env:{...env4,UI_BASE:base4,UI_WIDTH:String(width)},stdio:'inherit'});
        if(r4.status!==0) failed.push(`ui-discover-chat.mjs @${width}px`);
      }
    } finally { if(stub&&stub.exitCode===null) stub.kill('SIGTERM'); fs.rmSync(root4,{recursive:true,force:true}); }
  }
  // Third instance over TLS (throwaway self-signed certificate): the __Host- cookies must
  // really land in a browser (scripts/ui-https-session.mjs).
  {
    const root3=fs.mkdtempSync(path.join(os.tmpdir(),'bookdoe-uigate-tls-'));
    try{
      const key=`${root3}/key.pem`, cert=`${root3}/cert.pem`;
      const ssl=spawnSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,'-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1'],{stdio:'ignore'});
      if(ssl.status!==0) throw new Error('openssl could not make the throwaway certificate');
      const port3=await freePort(), base3=`https://127.0.0.1:${port3}`;
      const env3={...env,BOOKDOE_DB_PATH:`${root3}/bookdoe.db`,BOOKDOE_DATA_DIR:root3,BOOKDOE_BACKUP_DIR:`${root3}/backups`,
        BOOKDOE_COVER_DIR:`${root3}/covers`,BOOKDOE_AUTH_MODE:'local',UI_SEED_ROOT:root3,BOOKDOE_PUBLIC_URL:base3,UI_SMTP_PORT:'1'};
      const seed3=spawnSync(PY,[path.join(REPO,'scripts/ui-player-seed.py')],{cwd:APP,env:env3,encoding:'utf8'});
      if(seed3.status!==0){ console.error(seed3.stdout, seed3.stderr); throw new Error('seeding the TLS database failed'); }
      const seedJson3=seed3.stdout.split('\n').find(l=>l.startsWith('E2E '))?.slice(4);
      if(server&&server.exitCode===null) server.kill('SIGTERM');
      server=spawn(PY,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port3),'--ssl-keyfile',key,'--ssl-certfile',cert],{cwd:APP,env:env3,stdio:['ignore','ignore','inherit']});
      let up3=false;
      for(let i=0;i<150&&!up3&&server.exitCode===null;i++){ up3=await fetch(`${base3}/health`,{dispatcher:undefined}).then(r=>r.ok,()=>false); if(!up3) await sleep(200); }
      // fetch rejects the self-signed certificate; poll the port instead.
      const wait=async()=>{ for(let i=0;i<150;i++){ if(await new Promise(r=>{const s=net.connect(port3,'127.0.0.1',()=>{s.destroy();r(true)});s.on('error',()=>r(false));})) return true; await sleep(200);} return false; };
      if(!up3 && !(await wait())) throw new Error('TLS app did not start');
      await sleep(1500);
      const r3=spawnSync('node',[path.join(REPO,'scripts/ui-https-session.mjs')],{env:{...env3,UI_BASE:base3,UI_SEED:seedJson3},stdio:'inherit'});
      if(r3.status!==0) failed.push('ui-https-session.mjs');
    } finally { fs.rmSync(root3,{recursive:true,force:true}); }
  }
  if(failed.length) console.error(`\nui-gate FAILED:\n  ${failed.join('\n  ')}`);
  else console.log(`\nui-gate passed: ${CHECKS.join(', ')} at ${WIDTHS.join('/')}px, plus ui-player-e2e.mjs, ui-discover-chat.mjs (stubbed model) and ui-https-session.mjs (TLS)`);
  code=failed.length?1:0;
}catch(e){ console.error('ui-gate: '+e.message); }
finally{ cleanup(); }
process.exit(code);
