const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const c={};vm.runInNewContext(fs.readFileSync('static/work-overtime.js','utf8'),c);const ui=c.HireNowOvertime;
const b={id:7,status:'in_progress',approved_work_minutes:120,agreed_hourly_rate:100,overtime_state:'pending',overtime_requested_minutes:30,overtime_request_id:'current',overtime_reason:'<img onerror=x>',diagnosis_fee:0};
assert.match(ui.html(b,'hirer'),/maximum additional work charge ₹50.00/);assert.match(ui.html(b,'hirer'),/data-overtime-decision="approve"/);assert.doesNotMatch(ui.html(b,'worker'),/data-overtime-decision/);assert.doesNotMatch(ui.html(b,'hirer'),/<img/);assert.match(ui.html(b,'hirer'),/work already recorded/);
assert.match(ui.html({...b,approved_work_minutes:null},'worker'),/previous hourly agreement/);
assert.match(ui.html({...b,status:'completed',actual_minutes:155,billable_minutes:120},'hirer'),/Billed work: 120/);assert.equal(ui.html({...b,status:'cancelled'},'hirer'),'');
const feedback={},button={dataset:{overtimeDecision:'approve'}},card={querySelector:s=>s==='[data-overtime-feedback]'?feedback:null,querySelectorAll:()=>[button]};let current=card;
const root={isConnected:true,querySelector:()=>current};
(async()=>{
 let resolve,reads=0,changed=0;
 const mounted=ui.mount(root,b,{role:'hirer',api:(url,options)=>{reads++;assert.equal(url,'/api/bookings/7/overtime');assert.deepEqual(JSON.parse(options.body),{decision:'approve',request_id:'current'});return new Promise(r=>resolve=r);},changed:()=>changed++});
 const first=mounted.send({decision:'approve',request_id:'current'});mounted.send({decision:'approve',request_id:'current'});await new Promise(r=>setImmediate(r));assert.equal(reads,1);assert.equal(button.disabled,true);resolve({ok:true});await first;assert.equal(changed,1);
 const stale=mounted.send({decision:'approve',request_id:'current'});await new Promise(r=>setImmediate(r));current=null;resolve({ok:true});await stale;assert.equal(changed,1);
 current=card;const failure=mounted.send({decision:'approve',request_id:'current'});await new Promise(r=>setImmediate(r));resolve({ok:false,data:{error:'Request changed'}});await failure;assert.match(feedback.textContent,/Refresh Status/);assert.equal(button.disabled,false);
 console.log('Overtime cost disclosure, role controls, safe reason rendering, duplicate guard and stale-view handling passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
