const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('templates/dashboard.html','utf8');
const start=source.indexOf('  var SORT_CHIPS =');
const workers=[
 {id:1,name:'A',skill:'Painter',skills_detail:'Wall finishing',city:'Delhi',daily_wage:800,hourly_wage:1,booking_hourly_rate:100,rating:4,jobs_completed:8,is_online:1,verification_status:'verified',distance_km:5},
 {id:2,name:'B',skill:'Painter',city:'Delhi',daily_wage:400,booking_hourly_rate:50,rating:5,is_online:0,verification_status:'unverified',distance_km:null},
 {id:3,name:'C',skill:'Plumber',city:'Mumbai',daily_wage:1200,booking_hourly_rate:150,rating:3,is_online:true,verification_status:'verified',distance_km:2},
 {id:4,name:null,skill:'Painter',city:'Delhi',daily_wage:null,rating:null,is_online:1,verification_status:'verified'}
];
const controls=[{dataset:{workerFilter:'city'},type:'select',value:'Delhi'},{dataset:{workerFilter:'skill'},type:'select',value:'Painter'},{dataset:{workerFilter:'maxHourly'},type:'number',value:''},{dataset:{workerFilter:'online'},type:'checkbox',checked:false},{dataset:{workerFilter:'verified'},type:'checkbox',checked:false}];
const reset={},stack={innerHTML:'',querySelectorAll:selector=>selector==='[data-worker-filter]'?controls:[],querySelector:()=>stack.innerHTML.includes('id="resetWorkerFilters"')?reset:null};
const c={state:{workers,favorites:{}},document:{getElementById:()=>({value:'Delhi'})},stackBody:stack,stackTitle:{},stackSubtitle:{style:{}},stackSearchInput:{value:''},openStack:(_,title,render)=>render(),t:k=>k,colorFor:()=> '#123',initials:name=>String(name||'').slice(0,1),fmtMoney:n=>'₹'+n,hirerMessageText:v=>String(v??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]))};
vm.runInNewContext(source.slice(start,source.indexOf('\n  function loadNotifications()',start)),c);
const defaults={city:'',skill:'',maxHourly:'',online:false,verified:false};
function ids(filters={},query='',sort='All'){return Array.from(c.selectDiscoveryWorkers(workers,{...defaults,...filters},query,sort),w=>w.id);}
assert.deepEqual(ids({city:'Delhi',skill:'Painter',online:true,verified:true,maxHourly:'100'}),[1]);
assert.deepEqual(ids({},'wall finishing'),[1]);assert.deepEqual(ids({},'mumbai'),[3]);
assert.deepEqual(ids({maxHourly:'0'}),[]);assert.deepEqual(ids({maxHourly:'-1'}),[]);assert.deepEqual(ids({maxHourly:'garbage'}),[]);
assert.deepEqual(ids({},'','Price: Low to High'),[2,1,3,4]);assert.deepEqual(ids({},'','Nearby'),[3,1,2,4]);assert.deepEqual(ids({},'','Top Rated'),[2,1,3,4]);
assert.equal(c.workerHourlyPrice(workers[0]),100);assert.equal(c.workerHourlyPrice({daily_wage:801.04}),100.13);assert.equal(c.workerHourlyPrice({daily_wage:null}),null);
assert.equal(workers[0].id,1); // sorting never changes the source array
c.openWorkerListScreen('Painter',{});
assert.match(stack.innerHTML,/₹100<small>\/hour/);assert.doesNotMatch(stack.innerHTML,/reviews/);assert.match(stack.innerHTML,/completed jobs/);assert.match(stack.innerHTML,/Confirm your date and time/);
controls[2].value='60';controls[2].onchange();assert.match(stack.innerHTML,/1 worker found/);assert.doesNotMatch(stack.innerHTML,/data-worker="1"/);
controls[3].checked=true;controls[3].onchange();assert.match(stack.innerHTML,/No workers match/);reset.onclick();assert.match(stack.innerHTML,/3 workers found/);
workers.push({id:5,name:'<img src=x onerror=bad>',skill:'Painter',city:'Delhi',daily_wage:800});c.openWorkerListScreen('',{});assert.doesNotMatch(stack.innerHTML,/<img/);assert.match(stack.innerHTML,/&lt;img/);
console.log('Discovery filters, authoritative hourly prices, safe rendering and reset tests passed');
