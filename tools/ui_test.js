// Headless Chrome UI test + screenshots. Usage: node --experimental-websocket tools/ui_test.js  (needs google-chrome)
const {spawn,execSync}=require('child_process');const fs=require('fs'),os=require('os'),path=require('path');
const ROOT=path.resolve(__dirname,'..'),PORT=18091,CDP=9333,SC=path.join(ROOT,'screens');fs.mkdirSync(SC,{recursive:true});
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'mx'));
const srv=spawn('python3',[path.join(ROOT,'server.py')],{env:{...process.env,PORT,DB_PATH:path.join(tmp,'u.db'),ADMIN_PASSWORD:'testpw',MX_TEST_TODAY:'2026-10-10',HOST:'127.0.0.1'},stdio:'inherit'});
const chrome=spawn('google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port='+CDP,'--user-data-dir='+path.join(tmp,'ch'),'about:blank'],{stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));let pass=0,fail=0;const ok=(n,c,x)=>{c?pass++:fail++;console.log((c?'PASS ':'FAIL ')+n+(c?'':' '+(x||'')))};
(async()=>{
 await sleep(2500);const tabs=await (await fetch(`http://127.0.0.1:${CDP}/json`)).json();const ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise(r=>ws.onopen=r);let id=0;const pend={};const errs=[];
 ws.onmessage=m=>{const d=JSON.parse(m.data);if(d.id&&pend[d.id]){pend[d.id](d);delete pend[d.id]}else if(d.method==='Runtime.exceptionThrown')errs.push(d.params.exceptionDetails.exception&&d.params.exceptionDetails.exception.description||'exception');else if(d.method==='Runtime.consoleAPICalled'&&d.params.type==='error')errs.push('console.error '+JSON.stringify(d.params.args.map(a=>a.value)))};
 const send=(method,params={})=>new Promise(r=>{const i=++id;pend[i]=r;ws.send(JSON.stringify({id:i,method,params}))});
 const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,awaitPromise:true,returnByValue:true});if(r.result.exceptionDetails)errs.push('eval '+e.slice(0,60)+' '+JSON.stringify(r.result.exceptionDetails.exception&&r.result.exceptionDetails.exception.description));return r.result.result&&r.result.result.value};
 const shot=async(n,full)=>{await ev(`document.getElementById('confetti').style.display='none';document.getElementById('toast').className=''`);if(full){const h=await ev('Math.max(document.body.scrollHeight,innerHeight)');await send('Emulation.setDeviceMetricsOverride',{width:430,height:Math.min(h,2600),deviceScaleFactor:1,mobile:true})}const r=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(SC,n+'.png'),Buffer.from(r.result.data,'base64'));await send('Emulation.setDeviceMetricsOverride',{width:430,height:900,deviceScaleFactor:1,mobile:true})};
 await send('Page.enable');await send('Runtime.enable');await send('Emulation.setDeviceMetricsOverride',{width:430,height:900,deviceScaleFactor:1,mobile:true});
 await send('Page.navigate',{url:`http://127.0.0.1:${PORT}/`});await sleep(1200);
 const T=(sel,v)=>ev(`(()=>{const e=document.querySelector('${sel}');e.value=${JSON.stringify(v)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))})()`);
 const C=sel=>ev(`document.querySelector('${sel}').click()`);
 ok('login screen visible',await ev(`!document.getElementById('login').classList.contains('hide')`));
 await shot('1-login');
 await C('#pickNew');await sleep(200);await shot('2-create-account');
 // wrong flows through the UI
 await C('#pickOld'||'#back');
 await T('#name','Nobody');await T('#pin','1234');await C('#go');await sleep(500);
 ok('unknown name shows friendly generic error',(await ev(`document.getElementById('lerr').textContent`)).includes("doesn't match"));
 await C('#back');await C('#pickNew');await T('#name','Mercedes');await T('#pin','1234');await C('#go');await sleep(1200);
 ok('new account -> picker opens automatically',await ev(`!document.getElementById('ov').classList.contains('hide')&&document.querySelector('#ov h2').textContent.includes('Choose')`));
 await shot('3-picker',true);
 // instant substring search in picker
 const vis=async()=>JSON.parse(await ev(`JSON.stringify([...document.querySelectorAll('#pl label b')].map(b=>b.textContent))`));
 await T('#pq','jump');let v=await vis();ok("search 'jump' (name substring, case-insens)",v.includes('Box Jump')&&v.includes('Anterior Jump')&&v.includes('Anterior Triple Jump')&&v.every(n=>/jump/i.test(n)||['Single Leg Hop','Keiser Max Power Rotation Swing','Barbell Landmine Split Switch Clean & Jerk','Barbell Landmine Split Switch Snatch'].includes(n)),JSON.stringify(v));
 await T('#pq','JUMP');v=await vis();ok("search 'JUMP' same result",v.length===7,JSON.stringify(v));
 await T('#pq','erc');v=await vis();ok("mid-word 'erc' matches Zercher Squat",v.includes('Zercher Squat'),JSON.stringify(v));
 await T('#pq','dynamometer');v=await vis();ok('search matches descriptions',v.includes('Grip Strength'),JSON.stringify(v));
 await T('#pq','balance & mob');v=await vis();ok('search matches categories',v.includes('Single Leg Balance'),JSON.stringify(v));
 await T('#pq','');
 for(const n of ['Grip Strength','Gymnazo Mile','Row Machine - 500m Row','Back Squat Strength','Single Leg Balance'])await ev(`[...document.querySelectorAll('#pl label')].find(l=>l.textContent.trim()==${JSON.stringify(n)}).querySelector('input').click()`);
 ok('save button shows count',(await ev(`document.getElementById('ps').textContent`)).includes('5 chosen'));
 await C('#ps');await sleep(900);
 ok('home shows 5 metric cards',await ev(`document.querySelectorAll('#t-home .mc').length`)===5);
 // log result via UI: Grip strength (selector)
 await ev(`openLog(LIB.exercises.find(e=>e.name==='Grip Strength').id)`);await sleep(700);
 await shot('4-log-form',true);
 await C('#lsv');await sleep(500);ok('missing selector -> friendly error',(await ev(`document.getElementById('le').textContent`)).includes('Which hand'));
 await ev(`document.querySelector('.seg[data-sk=hand] button').click()`);await T('#pv','72');await C('#lsv');await sleep(1200);
 ok('after save lands on progress page',await ev(`!!document.getElementById('ml')`));
 // seed more data in-page through the real API for a nice chart
 const L=await ev(`(async()=>{const lib=await (await fetch('/api/exercises')).json();const g=lib.exercises.find(e=>e.name==='Grip Strength').id,m=lib.exercises.find(e=>e.name==='Gymnazo Mile').id,r=lib.exercises.find(e=>e.name==='Row Machine - 500m Row').id;
  const P=async(eid,p,day,sel)=>fetch('/api/results',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({exercise_id:eid,primary:p,day,selectors:sel||{}})});
  for(const [p,d] of [[60,'2026-03-14'],[66,'2026-05-02'],[64,'2026-06-20'],[75,'2026-08-09'],[79,'2026-09-30']])await P(g,p,d,{hand:'Right Hand'});
  for(const [p,d] of [[705,'2026-02-10'],[668,'2026-04-12'],[651,'2026-06-01'],[690,'2026-07-20'],[622,'2026-09-15']])await P(m,p,d);
  for(const [p,d] of [[132,'2026-04-01'],[127,'2026-07-01'],[121,'2026-10-01']])await P(r,p,d,{damper:'5'});return g})()`);
 await ev(`openMetric(${L})`);await sleep(800);await shot('5-progress-chart-grip',true);
 ok('chart svg rendered',await ev(`!!document.querySelector('#ov svg polyline')`));
 ok('improvement message shown',(await ev(`document.querySelector('#ov .msg').textContent`)).includes('improved'),await ev(`document.querySelector('#ov .msg').textContent`));
 const mile=await ev(`LIB.exercises.find(e=>e.name==='Gymnazo Mile').id`);await ev(`openMetric(${mile})`);await sleep(700);
 ok('lower-is-better message says faster',(await ev(`document.querySelector('#ov .msg').textContent`)).includes('faster'),await ev(`document.querySelector('#ov .msg').textContent`));
 await shot('6-progress-chart-mile-lower-is-better',true);
 await ev(`document.getElementById('mb').click()`);await sleep(900);
 await shot('7-my-progress-home',true);
 ok('trend/best displayed on home',(await ev(`document.getElementById('t-home').textContent`)).includes('Best:'));
 await ev(`tab('lib')`);await sleep(300);await T('#lq','row');await sleep(200);ok('library search filters live',await ev(`document.querySelectorAll('#ll .ex').length`)>=1&&await ev(`document.querySelectorAll('#ll .ex').length`)<8);await T('#lq','');await shot('8-exercise-library',false);
 await ev(`tab('badges')`);await sleep(800);await shot('9-badges',true);
 ok('badges earned shown',await ev(`document.querySelectorAll('.bd.got').length`)>=3,await ev(`document.querySelectorAll('.bd.got').length`));
 await ev(`tab('me')`);await sleep(200);await shot('10-me-delete-account');
 // history edit + delete via UI
 await ev(`openLog(${mile},CUR.results?CUR.results[0]:null)`);await sleep(500);
 await ev(`openMetric(${mile})`);await sleep(500);await ev(`document.querySelector('[data-ed]').click()`);await sleep(600);
 ok('edit form prefilled with min/sec',await ev(`document.getElementById('pvm').value!==''`));
 await T('#pvs','5');await C('#lsv');await sleep(900);
 // delete account through UI
 await ev(`closeOv();tab('me')`);await C('#da');await T('#dp','1234');await C('#dy2');await sleep(800);
 ok('delete account returns to login',await ev(`!document.getElementById('login').classList.contains('hide')`));
 const aud=await ev(`(()=>{const bad=[];document.querySelectorAll('button,input,select').forEach(e=>{const r=e.getBoundingClientRect();if(r.width&&r.height&&r.height<44&&e.type!=='checkbox')bad.push(e.id||e.textContent.slice(0,15))});return bad})()`);
 ok('no console/JS errors',errs.length===0,JSON.stringify(errs));
 console.log(`\n${pass} passed, ${fail} failed`);ws.close();chrome.kill();srv.kill();process.exit(fail?1:0);
})().catch(e=>{console.error(e);chrome.kill();srv.kill();process.exit(1)});
