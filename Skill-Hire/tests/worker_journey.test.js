const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('templates/worker.html','utf8');
const start=source.indexOf('function doCheckIn('),end=source.indexOf('  // ---------------- floating chat popup',start);
let booking={id:12,status:'confirmed'},gpsCalls=0,writes=[],resolve,refreshes=0;
const note={dataset:{},textContent:'',innerHTML:''};
let accuracy=800;
const c={document:{getElementById:()=>note},findBooking:()=>booking,loadBookings:()=>refreshes++,navigator:{geolocation:{getCurrentPosition:fn=>{gpsCalls++;fn({coords:{latitude:28.6,longitude:77.2,accuracy},timestamp:Date.now()});}}},api:(path,opts)=>{writes.push({path,opts});return new Promise(r=>resolve=r);}};
vm.runInNewContext(source.slice(start,end),c);
(async()=>{
 c.doCheckIn(12);c.doCheckIn(12);assert.equal(gpsCalls,1);assert.equal(writes.length,1);assert.match(writes[0].path,/start-travel$/);
 resolve({ok:true,data:{status:'en_route'}});await new Promise(r=>setImmediate(r));assert.equal(refreshes,1);assert.equal(note.dataset.gpsBusy,undefined);
 booking.status='en_route';c.doCheckIn(12);assert.equal(gpsCalls,2);assert.equal(writes.length,1);assert.match(note.textContent,/800 metres/);assert.match(note.textContent,/precise location/);assert.equal(note.dataset.gpsBusy,undefined);
 accuracy=10;c.doCheckIn(12);assert.equal(writes.length,2);assert.match(writes[1].path,/check-in$/);resolve({ok:true,data:{status:'checked_in'}});await new Promise(r=>setImmediate(r));assert.equal(refreshes,2);
 console.log('Journey records GPS; arrival retains stricter GPS verification and retry guidance');
})().catch(error=>{console.error(error);process.exitCode=1;});
