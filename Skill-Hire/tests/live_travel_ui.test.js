const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function worker(){
 let now=1000000,watchCb,intervalCb,cleared=0,currentPositionCalls=0,resolvers=[],writes=[];
 const elements={workerTravelSharing:{},workerTravelMessage:{},resumeTravelSharing:{},stopTravelSharing:{}};
 const pos=()=>({coords:{latitude:28.59,longitude:77.2,accuracy:20,speed:5},timestamp:now});
 const c={document:{getElementById:id=>elements[id]},Date:class extends Date{static now(){return now;}},navigator:{geolocation:{watchPosition:fn=>{watchCb=fn;return 9;},clearWatch:()=>cleared++,getCurrentPosition:fn=>{currentPositionCalls++;fn(pos());}}},setInterval:fn=>{intervalCb=fn;return 1;},clearInterval:()=>{}};
 vm.runInNewContext(fs.readFileSync('static/worker-travel.js','utf8'),c);
 const api=(path,options)=>{writes.push({path,method:options.method,body:options.body&&JSON.parse(options.body)});return new Promise(resolve=>resolvers.push(resolve));};
 c.HireNowTravel.sync([{id:7,status:'en_route'}],{id:2},api);c.HireNowTravel.start(7,api,'initial-token');watchCb(pos());
 assert.equal(writes.length,1);assert.equal(writes[0].body.sharing_token,'initial-token');assert.match(writes[0].path,/travel-location$/);
 resolvers.shift()({ok:true,data:{}});await tick();assert.match(elements.workerTravelMessage.textContent,/portal open/);
 watchCb({...pos(),timestamp:now-1000});assert.equal(writes.length,1);
 now+=15000;intervalCb();assert.equal(currentPositionCalls,1);assert.equal(writes.length,2);
 elements.stopTravelSharing.onclick();assert.equal(writes[2].method,'DELETE');assert(cleared>0);
 resolvers.shift()({ok:true,data:{}});resolvers.shift()({ok:true,data:{sharing:false}});await tick();assert.match(elements.workerTravelMessage.textContent,/paused/);
 elements.resumeTravelSharing.onclick();assert.match(writes[3].path,/start-travel$/);
 resolvers.shift()({ok:true,data:{sharing_token:'resumed-token'}});await tick();assert.equal(writes[4].body.sharing_token,'resumed-token');resolvers.shift()({ok:true,data:{}});await tick();
 c.HireNowTravel.sync([{id:7,status:'checked_in'}],{id:2},api);assert.equal(elements.workerTravelSharing.hidden,true);
 console.log('Worker live GPS lifecycle, throttling, pause/resume tokens and arrival cleanup passed');
}
async function hirer(){
 let data={sharing:true,fresh:true,age_seconds:10,location:{latitude:28.59,longitude:77.2,accuracy:20},destination:{latitude:28.6,longitude:77.2},distance_km:1.1,eta_minutes:4,eta_basis:'straight_line_current_speed',message:'Live updates'},interval,removed=0,reads=0,active=true;
 const location={},eta={},detail={},button={},mapElement={};
 const card={querySelector:selector=>({'[data-travel-location]':location,'[data-travel-eta]':eta,'[data-travel-detail]':detail,'[data-refresh-travel]':button,'[data-travel-map]':mapElement}[selector])};
 const root={querySelector:()=>card};
 const layer=()=>({addTo(){return this;},bindTooltip(){return this;},setLatLng(){return this;},setRadius(){return this;}});
 const c={document:{hidden:false},setInterval:fn=>{interval=fn;return 1;},clearInterval:()=>{},L:{map:()=>({remove:()=>removed++,fitBounds:()=>{},setView:()=>{}}),tileLayer:layer,marker:layer,circle:layer}};
 vm.runInNewContext(fs.readFileSync('static/hirer-travel.js','utf8'),c);
 const mounted=c.HireNowLiveTravel.mount(root,{id:7},{active:()=>active,api:async(path,options)=>{reads++;assert.equal(options.cache,'no-store');return {ok:true,data};}});await tick();
 assert.match(location.textContent,/Updated 10/);assert.match(eta.textContent,/Rough travel time: 4 min/);assert.match(detail.textContent,/traffic are not included/);
 data={...data,fresh:false,age_seconds:180,eta_minutes:null,message:'Location is stale'};await mounted.refresh();assert.match(location.textContent,/stale/);assert.equal(eta.textContent,'ETA unavailable');
 data={sharing:false,location:null};await mounted.refresh();assert.equal(removed,1);assert.equal(eta.textContent,'ETA unavailable');
 active=false;const before=reads;interval();await mounted.refresh();assert.equal(reads,before);
 assert.equal(c.HireNowLiveTravel.html({status:'completed'}),'');
 console.log('Hirer map polling, ETA labels, stale location, paused sharing and view cleanup passed');
}
(async()=>{await worker();await hirer();})().catch(error=>{console.error(error);process.exitCode=1;});
