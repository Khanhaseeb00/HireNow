(function(global){
 'use strict';
 var timer=null,map=null;
 function stop(){if(timer!=null)clearInterval(timer);timer=null;if(map){map.remove();map=null;}}
 function html(b){return b.status==='en_route'?'<section class="booking-tracking" data-live-travel><h3>Worker live location</h3><div data-travel-map style="height:240px;border-radius:12px;overflow:hidden"></div><p data-travel-location role="status">Loading worker location...</p><strong data-travel-eta>ETA unavailable</strong><p class="booking-tracking-note" data-travel-detail>Live updates require the worker portal to remain open.</p><button class="btn btn-outline" type="button" data-refresh-travel>Refresh Location</button></section>':'';}
 function mount(root,b,opts){
  stop();var card=root.querySelector('[data-live-travel]');if(!card)return;
  var location=card.querySelector('[data-travel-location]'),eta=card.querySelector('[data-travel-eta]'),detail=card.querySelector('[data-travel-detail]'),button=card.querySelector('[data-refresh-travel]'),workerMarker=null,jobMarker=null,circle=null,busy=false,centred=false;
  function active(){return root.querySelector('[data-live-travel]')===card&&(!opts.active||opts.active());}
  function refresh(){
   if(busy||!active())return Promise.resolve();busy=true;button.disabled=true;
   return opts.api('/api/bookings/'+Number(b.id)+'/travel-location',{cache:'no-store'}).then(function(r){
    if(!active())return;if(!r.ok)throw Error(r.data.error||'Location unavailable');var data=r.data;
    if(!data.sharing||!data.location){location.textContent=data.message||'Live location is not being shared for this booking.';eta.textContent='ETA unavailable';detail.textContent='Live location is shared only while the worker is travelling and sharing is enabled.';if(map){map.remove();map=null;workerMarker=null;jobMarker=null;circle=null;centred=false;}return;}
    location.textContent=(data.fresh?'Updated ':'Last known position · stale · ')+data.age_seconds+' seconds ago · GPS accuracy ±'+Math.round(data.location.accuracy)+' m';
    eta.textContent=data.eta_minutes==null?'ETA unavailable':(data.eta_basis==='traffic_route'?'Estimated arrival (driving): ':'Rough travel time: ')+data.eta_minutes+' min';
    detail.textContent=data.eta_basis==='straight_line_current_speed'?'Straight-line distance/current GPS speed estimate; roads and traffic are not included.':data.message;
    if(data.distance_km!=null)detail.textContent+=' Straight-line distance to job pin: '+data.distance_km+' km.';
    if(!global.L){detail.textContent+=' Map could not load.';return;}
    if(!map){map=global.L.map(card.querySelector('[data-travel-map]'));global.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);}
    var point=[data.location.latitude,data.location.longitude];
    if(!workerMarker)workerMarker=global.L.marker(point).addTo(map).bindTooltip('Worker');else workerMarker.setLatLng(point);
    if(!circle)circle=global.L.circle(point,{radius:data.location.accuracy}).addTo(map);else circle.setLatLng(point).setRadius(data.location.accuracy);
    if(data.destination.latitude!=null&&data.destination.longitude!=null&&!jobMarker)jobMarker=global.L.marker([data.destination.latitude,data.destination.longitude]).addTo(map).bindTooltip('Job location');
    if(!centred){if(jobMarker)map.fitBounds([point,[data.destination.latitude,data.destination.longitude]],{padding:[20,20],maxZoom:15});else map.setView(point,15);centred=true;}
   }).catch(function(error){if(active()){location.textContent=error.message+'. Last visible position may be outdated.';eta.textContent='ETA unavailable';}}).finally(function(){busy=false;if(active())button.disabled=false;});
  }
  button.onclick=refresh;refresh();timer=setInterval(function(){if(!active()){stop();return;}if(!(global.document&&global.document.hidden))refresh();},10000);
  return {refresh:refresh,stop:stop};
 }
 global.HireNowLiveTravel={html:html,mount:mount,stop:stop};
})(typeof window!=='undefined'?window:globalThis);
