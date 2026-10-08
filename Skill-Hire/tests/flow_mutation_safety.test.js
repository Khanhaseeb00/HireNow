const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function adminChecks(){
 const source=fs.readFileSync('templates/admin.html','utf8');
 const start=source.indexOf('var adminMutations=');
 let resolveWrite,resolveRefresh,writes=0,reads=0;
 const status={};
 const c={adminAuthEpoch:1,document:{getElementById:()=>status},api:()=>{writes++;return new Promise(resolve=>resolveWrite=resolve);},loadAll:()=>{reads++;return new Promise(resolve=>resolveRefresh=resolve);}};
 vm.runInNewContext(source.slice(start,source.indexOf('\nfunction pill',start)),c);
 const first=c.adminMutation('/api/admin/finance/7/settle',{method:'POST'});
 const duplicate=await c.adminMutation('/api/admin/finance/7/settle',{method:'POST'});
 assert.equal(writes,1);assert.equal(duplicate.status,409);
 resolveWrite({ok:false,status:0,data:{error:'Timed out'}});await tick();assert.equal(reads,1);
 assert.equal((await c.adminMutation('/api/admin/finance/7/settle',{})).status,409);
 resolveRefresh();assert.equal((await first).ok,false);assert.equal(status.textContent,'');
 const stale=c.adminMutation('/api/admin/finance/7/settle',{});c.adminAuthEpoch++;
 resolveWrite({ok:true,data:{}});assert.equal((await stale).ok,false);assert.equal(reads,1);
 console.log('Admin write guard and refresh-before-retry checks passed');
}
async function hirerChecks(){
 const source=fs.readFileSync('templates/dashboard.html','utf8');
 const start=source.indexOf('  var hirerBookingsRequest=');
 let pending=[],renders=0;
 const status={};
 const c={state:{hirer:{id:1},bookings:[{id:9}]},document:{getElementById:()=>status},api:()=>new Promise(resolve=>pending.push(resolve)),renderHomeBookingSummary:()=>{},renderBookings:()=>renders++,renderMessages:()=>{},renderProfile:()=>{},showHirerLoadError:(_,message,retry)=>{status.message=message;status.retry=retry;}};
 vm.runInNewContext(source.slice(start,source.indexOf('\n  function bookingStatusClass',start)),c);
 const old=c.loadBookings(),latest=c.loadBookings();pending[1]({ok:true,data:[{id:2}]});await latest;pending[0]({ok:true,data:[{id:3}]});assert.equal(await old,false);assert.equal(c.state.bookings[0].id,2);
 pending=[];const failure=c.loadBookings();pending[0]({ok:false,data:{error:'Unavailable'}});await failure;
 assert.match(status.message,/outdated/);assert.equal(c.state.bookings[0].id,2);
 pending=[];status.retry();pending[0]({ok:true,data:[]});await tick();assert.equal(c.state.bookings.length,0);
 pending=[];const stale=c.loadBookings();c.hirerBookingsSession++;pending[0]({ok:true,data:[{id:5}]});assert.equal(await stale,false);assert.equal(c.state.bookings.length,0);
 // Actual cancellation listener: two clicks send only one cancellation request.
 const cancelStart=source.indexOf('        var cancel=stackBody.querySelector("#refCancelBooking")');
 let click,resolveCancel,writes=0,refreshes=0;
 const button={disabled:false,addEventListener:(_,fn)=>click=fn};
 const cc={stackBody:{querySelector:()=>button},b:{id:7},window:{confirm:()=>true,alert:()=>{}},api:()=>{writes++;return new Promise(resolve=>resolveCancel=resolve);},loadBookings:()=>refreshes++,openBookingDetailsScreen:()=>{},goBack:()=>{}};
 vm.runInNewContext(source.slice(cancelStart,source.indexOf('        var syncPayment=',cancelStart)),cc);
 click();click();assert.equal(writes,1);assert.equal(button.disabled,true);
 resolveCancel({ok:false,data:{error:'Timed out'}});await tick();assert.equal(refreshes,1);assert.equal(button.disabled,false);
 console.log('Hirer booking retry, stale response and cancellation safeguards passed');
}
async function workerChecks(){
 const source=fs.readFileSync('templates/worker.html','utf8');
 const start=source.indexOf('  document.getElementById("savePayoutBtn").addEventListener');
 let click,resolveSave,writes=0,refreshes=0;
 const nodes={};
 for(const id of ['payoutMessage','payoutHolder','payoutAccount','payoutIfsc','payoutBank','payoutUpi'])nodes[id]={value:'test'};
 const button={disabled:false,textContent:'Save',addEventListener:(_,fn)=>click=fn};nodes.savePayoutBtn=button;
 const c={document:{getElementById:id=>nodes[id]},state:{worker:{id:1}},workerJobsSession:1,api:()=>{writes++;return new Promise(resolve=>resolveSave=resolve);},loadPayout:async()=>{refreshes++;}};
 vm.runInNewContext(source.slice(start,source.indexOf('\n  var availabilityState',start)),c);
 click.call(button);click.call(button);assert.equal(writes,1);
 resolveSave({ok:true,data:{}});await tick();assert.equal(nodes.payoutAccount.value,'');assert.equal(refreshes,1);assert.equal(button.disabled,false);
 nodes.payoutAccount.value='secret account';click.call(button);c.workerJobsSession++;nodes.payoutMessage.textContent='New session';
 resolveSave({ok:true,data:{}});await tick();assert.equal(refreshes,1);assert.equal(nodes.payoutMessage.textContent,'New session');
 const earningStart=source.indexOf('  function renderEarningsScreen(');
 let resolveEarnings,painted=0;
 for(const id of ['earningsBreakdown','todayEarning','earningsTotal'])nodes[id]={};
 Object.assign(c,{earningsRequest:0,earningsData:null,api:()=>new Promise(resolve=>resolveEarnings=resolve),wfui:x=>x,earningsText:x=>x,fmtMoney:x=>String(x),paintEarnings:()=>painted++});
 vm.runInNewContext(source.slice(earningStart,source.indexOf('\n  function workerConversationRows',earningStart)),c);
 c.renderEarningsScreen();c.workerJobsSession++;resolveEarnings({ok:true,data:{items:[],today_net:999}});await tick();assert.equal(painted,0);assert.equal(c.earningsData,null);
 let payoutResolvers=[];
 nodes.payoutSaved={};
 c.api=()=>new Promise(resolve=>payoutResolvers.push(resolve));
 const payoutStart=source.indexOf('  var payoutLoadVersion=');
 vm.runInNewContext(source.slice(payoutStart,source.indexOf('  document.getElementById("openPayoutBtn")',payoutStart)),c);
 const older=c.loadPayout(),latest=c.loadPayout();
 payoutResolvers[1]({ok:true,data:{account_holder_name:'Latest holder',account_number_last4:'1234',verification_status:'verified'}});await latest;
 payoutResolvers[0]({ok:true,data:{account_holder_name:'Stale holder'}});await older;
 assert.equal(nodes.payoutHolder.value,'Latest holder');assert.equal(nodes.payoutAccount.value,'');
 const oldAccount=c.loadPayout();c.workerJobsSession++;payoutResolvers[2]({ok:true,data:{account_holder_name:'Previous account'}});await oldAccount;
 assert.equal(nodes.payoutHolder.value,'');
 console.log('Worker payout double-click, sensitive input clearing and earnings session guards passed');
}
async function logoutChecks(){
 for(const side of ['dashboard','worker']){
  const source=fs.readFileSync('templates/'+side+'.html','utf8');
  const start=source.indexOf('  document.getElementById("logoutBtn").addEventListener("click", function () {');
  const end=side==='worker'?source.indexOf('  function startApp',start):source.indexOf('  var profileBookingsBtn',start);
  let click,resolveLogout,writes=0,reloads=0;
  const button={disabled:false,addEventListener:(_,fn)=>click=fn},nodes={logoutBtn:button};
  const c={document:{getElementById:id=>nodes[id]||(nodes[id]={value:'private',textContent:'private'})},state:{worker:{id:1},hirer:{id:1},bookings:[{id:1}]},workerJobsSession:0,hirerBookingsSession:0,earningsRequest:0,earningsData:{},detailBookingId:1,workerNotificationTimer:null,appScreen:{style:{display:'block'},classList:{remove:()=>{}}},authScreen:{style:{display:'none'}},window:{alert:()=>{},location:{reload:()=>reloads++}},api:()=>{writes++;return new Promise(resolve=>resolveLogout=resolve);}};
  vm.runInNewContext(source.slice(start,end),c);
  click.call(button);click.call(button);assert.equal(writes,1);
  resolveLogout({ok:false,data:{error:'Logout timed out'}});await tick();
  assert.equal(button.disabled,false);assert.equal(reloads,0);assert(c.state.worker);assert.equal(c.appScreen.style.display,'block');
  click.call(button);resolveLogout({ok:true,data:{}});await tick();
  if(side==='worker'){assert.equal(c.state.worker,null);assert.equal(c.appScreen.style.display,'none');assert.equal(c.authScreen.style.display,'block');assert.equal(nodes.payoutAccount.value,'');}
  else {assert.equal(c.state.hirer,null);assert.equal(reloads,1);}
 }
 console.log('Worker and hirer logout only succeeds after server confirmation');
}
(async()=>{await adminChecks();await hirerChecks();await workerChecks();await logoutChecks();})().catch(error=>{console.error(error);process.exitCode=1;});
