const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),crypto=require('node:crypto').webcrypto;
const source=fs.readFileSync('templates/dashboard.html','utf8'),storage=new Map(),calls=[];
const button={dataset:{},disabled:false,textContent:''},msg={innerHTML:'',textContent:''};let refreshed=0,opened=0,response={ok:false,status:0,data:{error:'Connection failed'}};
const context={crypto,TextEncoder,state:{hirer:{id:12}},localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},stackBody:{querySelector:id=>id==='#refProceed'?button:msg},stackRevision:1,hirerMessageText:String,loadBookings:()=>refreshed++,openBookingDetailsScreen:id=>opened=id,setTimeout:()=>{},api:async(path,options)=>{calls.push({path,options});return response;}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  var pendingBookingKeys='),source.indexOf('  function showPayStep(')),context);
(async()=>{
 const payload={worker_id:1,start_date:'2099-01-02',start_time:'10:00',hours:2,address:'Private job address',payment_method:'cash'};
 const identity=await context.bookingRequestIdentity(payload);context.pendingBookingKeys={};
 const afterRefresh=await context.bookingRequestIdentity({...payload});assert.equal(identity.key,afterRefresh.key);
 assert.ok([...storage.values()].every(value=>!value.includes('Private job address')));
 const changed=await context.bookingRequestIdentity({...payload,hours:3});assert.notEqual(changed.key,identity.key);
 context.state.hirer={id:13};const other=await context.bookingRequestIdentity(payload);assert.notEqual(other.key,identity.key);context.state.hirer={id:12};
 const first=context.createReferenceBooking({id:1},{...payload});const duplicate=context.createReferenceBooking({id:1},{...payload});await Promise.all([first,duplicate]);assert.equal(calls.length,1);assert.equal(button.disabled,false);assert.equal(calls[0].options.headers['Idempotency-Key'],identity.key);
 context.pendingBookingKeys={};response={ok:true,status:200,data:{id:42,replayed:true,status:'requested',payment_method:'cash'}};
 await context.createReferenceBooking({id:1},{...payload});assert.equal(calls.length,2);assert.equal(calls[1].options.headers['Idempotency-Key'],identity.key);assert.equal(storage.has(identity.storageKey),false);assert.equal(refreshed,1);assert.equal(button.disabled,true);
 button.disabled=false;response={ok:true,status:200,data:{id:42,replayed:true,status:'cancelled'}};
 await context.createReferenceBooking({id:1},{...payload});assert.equal(opened,42);assert.match(msg.textContent,/no duplicate booking/);
 console.log('Booking double-click, persistent retry key and replay navigation tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
