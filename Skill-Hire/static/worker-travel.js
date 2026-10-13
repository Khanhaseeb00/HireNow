(function(global){
 'use strict';
 var watch=null,timer=null,bookingId=null,api=null,token=0,busy=false,lastSent=0,pending=null,userId=null,lastFixTimestamp=0;
 function banner(message){var el=document.getElementById('workerTravelSharing'),text=document.getElementById('workerTravelMessage');if(el&&text){el.hidden=false;text.textContent=message;}}
 function payload(position){return {latitude:position.coords.latitude,longitude:position.coords.longitude,accuracy:position.coords.accuracy,gps_timestamp:position.timestamp,speed:Number.isFinite(position.coords.speed)&&position.coords.speed>=0&&position.coords.speed<=70?position.coords.speed:null};}
 function stop(){token++;if(watch!=null&&navigator.geolocation)navigator.geolocation.clearWatch(watch);if(timer!=null)clearInterval(timer);watch=null;timer=null;bookingId=null;busy=false;pending=null;lastSent=0;lastFixTimestamp=0;}
 function start(id,client,sharingToken){
  stop();bookingId=Number(id);api=client;var generation=token;
  if(!navigator.geolocation){banner('GPS is unavailable. Live location sharing is paused.');return;}
  banner('Sharing travel GPS with this booking hirer. Keep the worker portal open.');
  function failure(error){if(generation!==token)return;banner('Live GPS paused: '+error.message+'. Allow precise location and use Resume Sharing.');if(error.code===1)stop();}
  function fix(position){
   if(generation!==token)return;
   if(!Number.isFinite(position.coords.accuracy)||position.coords.accuracy>1000){banner('GPS accuracy is too low to share. Enable precise location and retry.');return;}
   if(position.timestamp<lastFixTimestamp)return;
   lastFixTimestamp=position.timestamp;pending=position;
   if(!busy && Date.now()-lastSent>=10000)send();
  }
  function send(){
   if(generation!==token||busy||!pending)return;
   var position=pending;pending=null;busy=true;lastSent=Date.now();
   api('/api/worker/bookings/'+bookingId+'/travel-location',{method:'POST',body:JSON.stringify(Object.assign(payload(position),{sharing_token:sharingToken}))}).then(function(r){
    if(generation!==token)return;
    if(!r.ok){banner(r.data.error||'Live GPS update failed. Last location may be stale.');if(r.status===401||r.status===403||r.status===404||r.status===409)stop();return;}
    banner('Travel GPS shared at '+new Date().toLocaleTimeString()+'. Keep this portal open; phone lock/backgrounding can pause updates.');
   }).catch(function(){if(generation===token)banner('GPS update failed. Checking again shortly.');}).finally(function(){if(generation===token)busy=false;});
  }
  function watchGPS(){
  watch=navigator.geolocation.watchPosition(fix,failure,{enableHighAccuracy:true,timeout:20000,maximumAge:0});
  timer=setInterval(function(){if(generation!==token)return;navigator.geolocation.getCurrentPosition(fix,failure,{enableHighAccuracy:true,timeout:15000,maximumAge:0});if(pending&&!busy&&Date.now()-lastSent>=10000)send();},15000);
  }
  if(sharingToken)watchGPS();else navigator.geolocation.getCurrentPosition(function(position){
    if(generation!==token)return;
    api('/api/worker/bookings/'+bookingId+'/start-travel',{method:'POST',body:JSON.stringify(payload(position))}).then(function(r){
      if(generation!==token)return;
      if(!r.ok){banner(r.data.error||'Sharing could not resume.');return;}
      sharingToken=r.data.sharing_token;watchGPS();fix(position);
    });
  },failure,{enableHighAccuracy:true,timeout:15000,maximumAge:0});
 }
 function sync(bookings,worker,client){
  var id=worker&&worker.id;
  if(userId!==id){stop();userId=id;}
  var current=bookings.find(function(b){return Number(b.id)===bookingId&&b.status==='en_route';});
  if(bookingId&&!current)stop();
  var available=current||bookings.find(function(b){return b.status==='en_route';});
  var el=document.getElementById('workerTravelSharing');if(!el)return;
  el.hidden=!available;if(!available)return;
  if(!bookingId)banner('Live GPS sharing is paused for booking #'+Number(available.id)+'. Resume sharing while travelling.');
  document.getElementById('resumeTravelSharing').onclick=function(){start(available.id,client);};
  document.getElementById('stopTravelSharing').onclick=function(){var target=Number(available.id);stop();banner('Live sharing paused. Removing the last shared travel location...');client('/api/worker/bookings/'+target+'/travel-location',{method:'DELETE'}).then(function(r){banner(r.ok?'Live sharing paused. Resume it while travelling.':'Sharing stopped on this device, but the server still has the last location. Retry Stop Sharing.');});};
 }
 global.HireNowTravel={start:start,stop:stop,sync:sync,payload:payload};
})(typeof window!=='undefined'?window:globalThis);
