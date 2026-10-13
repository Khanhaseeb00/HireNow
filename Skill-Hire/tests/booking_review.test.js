const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('templates/dashboard.html','utf8');
const start=source.indexOf('  function bookingEstimateAmount('),end=source.indexOf('  function openBookingMapPicker(',start);
let sent=0,payload;
const inputs={refAddress:{value:'Main Street <unsafe>'},refNotes:{value:'Call before arrival'},endTime:{value:'11:30'},refBookingMsg:{},refProceed:{dataset:{},disabled:false},bookDate:{value:'2099-01-01'},editBookingReview:{}};
const timeButton={dataset:{start:'10:00 AM'}};
const stack={innerHTML:'',querySelector:selector=>inputs[selector.slice(1)]||null,querySelectorAll:selector=>selector==='[data-start]'?[timeButton]:[]};
const escape=v=>String(v??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const worker={id:1,name:'Worker <test>',skill:'Painter',city:'Delhi',daily_wage:800,booking_hourly_rate:100};
const c={state:{workers:[worker],location:{address:'Saved address'}},stackBody:stack,stackScreen:{},stackRevision:0,openStack:(_,title,render)=>{c.stackRevision++;render();},api:async path=>path.includes('available-slots')?{ok:true,data:{slots:['10:00 AM']}}:{ok:true,data:{slabs:[]}},serviceDate:offset=>offset?'2099-01-02':'2099-01-01',workerHourlyPrice:()=>100,hirerMessageText:escape,colorFor:()=> '#123',initials:()=> 'W',fmtMoney:n=>'₹'+n,createReferenceBooking:(w,p)=>{sent++;payload=p;}};
vm.runInNewContext(source.slice(start,end),c);
assert.equal(c.bookingEstimateAmount(112.5,1),112);assert.equal(c.bookingEstimateAmount(125,1.5),188);
const draft={start_date:'2099-01-01',start_time:'10:00 AM',end_time:'11:30',booking_type:'regular',payment_method:'cash',address:'<img onerror=bad>',special_instructions:'Line 1\nLine 2'};
const regular=c.bookingReviewHtml(worker,draft,1.5,null);
assert.match(regular,/₹150/);assert.match(regular,/Cash after work/);assert.match(regular,/actual timer duration/);assert.match(regular,/refund is not instant/);assert.doesNotMatch(regular,/<img/);assert.match(regular,/&lt;img/);
const diagnosis=c.bookingReviewHtml(worker,{...draft,booking_type:'diagnosis',payment_method:'online'},1.5,75);
assert.match(diagnosis,/₹75/);assert.match(diagnosis,/Repair work is charged separately/);assert.match(diagnosis,/Online payment after worker acceptance/);
(async()=>{
 c.openBookingScreen(1);await new Promise(resolve=>setImmediate(resolve));
 timeButton.onclick();inputs.endTime.onchange.call(inputs.endTime);
 inputs.refProceed.onclick();assert.equal(sent,0);assert.match(stack.innerHTML,/Review your booking request/);assert.match(stack.innerHTML,/Main Street &lt;unsafe&gt;/);
 inputs.editBookingReview.onclick();assert.match(stack.innerHTML,/Review Booking/);assert.match(stack.innerHTML,/Call before arrival/);
 inputs.refProceed.onclick();inputs.refProceed.onclick();assert.equal(sent,1);assert.equal(payload.address,'Main Street <unsafe>');assert.equal(payload.end_time,'11:30');assert.equal(payload.expected_hourly_rate,100);
 // A background refresh cannot recreate an enabled confirmation while a request is sending.
 inputs.refProceed.dataset.bookingBusy='1';const before=stack.innerHTML;
 timeButton.onclick();assert.equal(stack.innerHTML,before);
 console.log('Booking review, edit preservation, explicit confirmation, estimate and safe rendering tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
