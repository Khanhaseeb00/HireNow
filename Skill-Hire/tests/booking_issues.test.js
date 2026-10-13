const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 let rows=[],current,replyId=1,keys=0,resolveWrite,writes=[],readFailure=false;
 const feedback={},description={value:''},category={value:'other'},replyBody={value:''},buttons=[{},{}];
 const report={querySelector:selector=>selector==='[data-issue-description]'?description:category};
 const reply={dataset:{issueReply:'1'},querySelector:()=>replyBody};
 const reload={};
 const content={innerHTML:'',textContent:'',appendChild:node=>content.retry=node,querySelector:selector=>selector==='[data-reload-issues]'?reload:selector==='[data-report-issue]'?report:selector==='[data-issue-feedback]'?feedback:replyBody,querySelectorAll:selector=>selector==='button'?buttons:selector==='textarea'?[description,replyBody]:selector==='[data-issue-reply]'&&content.innerHTML.includes('data-issue-reply="1"')?[reply]:[]};
 const card={dataset:{},querySelector:()=>content},root={querySelector:()=>current};current=card;
 const c={crypto:{randomUUID:()=>`12345678-1234-1234-1234-${String(++keys).padStart(12,'0')}`},document:{createElement:()=>({})}};
 vm.runInNewContext(fs.readFileSync('static/booking-issues.js','utf8'),c);
 c.HireNowIssues.mount(root,{role:'hirer',bookingId:7,api:(path,opts)=>{
  if(opts){writes.push({path,payload:JSON.parse(opts.body)});return new Promise(resolve=>resolveWrite=resolve);}
  return Promise.resolve(readFailure?{ok:false,data:{error:'Issues unavailable'}}:{ok:true,data:rows});
 }});
 return {c,content,card,feedback,description,category,replyBody,report,reply,reload,buttons,writes,setRows:value=>rows=value,write:result=>resolveWrite(result),remove:()=>current=null,failReads:()=>readFailure=true};
}
(async()=>{
 const s=setup();await tick();
 s.description.value='Please help resolve this booking problem.';s.description.oninput();assert.equal(s.card.dataset.issueEditing,'1');
 s.reload.onclick();assert.match(s.feedback.textContent,/Send or clear/);
 s.report.onsubmit.call(s.report,{preventDefault(){}});s.report.onsubmit.call(s.report,{preventDefault(){}});assert.equal(s.writes.length,1);assert.equal(s.buttons[0].disabled,true);
 s.write({ok:false,data:{error:'Response timed out'}});await tick();assert.match(s.feedback.textContent,/timed out/);assert.equal(s.description.value,'Please help resolve this booking problem.');assert.equal(s.buttons[0].disabled,false);
 const issue={id:1,status:'open',category:'other',reporter_role:'hirer',description:'<img onerror=bad>',resolution:'<script>bad</script>',events:[{actor_role:'worker',event_type:'reply',body:'<img onerror=bad>'}]};
 s.setRows([issue]);s.report.onsubmit.call(s.report,{preventDefault(){}});s.write({ok:true,data:{issue,replayed:true}});await tick();
 assert.doesNotMatch(s.content.innerHTML,/<img|<script>/);assert.match(s.content.innerHTML,/&lt;img/);assert.match(s.feedback.textContent,/kept as a reply draft/);
 s.replyBody.value='The reported issue still needs help.';s.reply.onsubmit.call(s.reply,{preventDefault(){}});const key=s.writes[2].payload.request_key;
 s.write({ok:false,data:{error:'Response timed out'}});await tick();s.reply.onsubmit.call(s.reply,{preventDefault(){}});assert.equal(s.writes[3].payload.request_key,key);
 s.write({ok:true,data:{issue}});await tick();
 const failed=setup();await tick();failed.failReads();failed.description.value='';failed.replyBody.value='';failed.description.oninput();failed.reload.onclick();await tick();assert(failed.content.retry);assert.equal(failed.content.retry.textContent,'Retry Issues');
 const adminSource=fs.readFileSync('templates/admin.html','utf8'),start=adminSource.indexOf('function renderIssues('),end=adminSource.indexOf('var adminIssueVersion',start);
 const target={};const c={document:{getElementById:()=>target},esc:s.c.HireNowIssues.escape,pill:value=>s.c.HireNowIssues.escape(value),table:(headers,rows)=>rows.join('')};
 vm.runInNewContext(adminSource.slice(start,end),c);c.renderIssues([{id:1,booking_id:7,category:'other',reporter_role:'<img>',hirer_name:'<script>bad</script>',worker_name:'Worker',status:'open',booking_status:'requested'}]);assert.doesNotMatch(target.innerHTML,/<img|<script>/);
 console.log('Booking issue rendering, duplicate submit, draft preservation, reply retry keys and admin escaping tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
