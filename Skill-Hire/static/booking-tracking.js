(function(global){
  'use strict';
  var stages=['requested','confirmed','en_route','checked_in','in_progress','completed'];
  var labels=['Request sent','Accepted','En route','Arrived','Work started','Completed'];
  var interval=null;
  function escape(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function utcTime(value){
    if(typeof value!=='string'||!value)return NaN;
    return Date.parse(/[zZ]$|[+-]\d\d:\d\d$/.test(value)?value:value+'Z');
  }
  function snapshot(booking,now){
    var running=booking.status==='in_progress'&&!booking.work_ended_at;
    var start=utcTime(booking.work_started_at),end=booking.work_ended_at?utcTime(booking.work_ended_at):running?now:NaN;
    var valid=Number.isFinite(start)&&Number.isFinite(end)&&end>=start;
    var seconds=valid?Math.floor((end-start)/1000):null;
    var rate=booking.agreed_hourly_rate==null?NaN:Number(booking.agreed_hourly_rate);
    var minutes=seconds==null?null:Math.max(1,Math.floor(seconds/60));
    var amount=minutes==null||!Number.isFinite(rate)||rate<0?null:rate*minutes/60;
    if(amount!=null){var whole=Math.floor(amount);amount=amount-whole===0.5?whole+whole%2:Math.round(amount);amount+=Number(booking.diagnosis_fee)||0;}
    if(!running)amount=booking.status==='completed'&&booking.total_amount!=null&&Number.isFinite(Number(booking.total_amount))?Number(booking.total_amount):null;
    return {running:running,seconds:seconds,amount:amount};
  }
  function html(booking){
    var index=stages.indexOf(booking.status),terminal=['cancelled','rejected'].indexOf(booking.status)>=0;
    var progress=index<0?'<p>'+escape(booking.request_expired_at?'Request expired':terminal?booking.status:'Status unavailable')+'</p>':'<ol class="booking-tracking-stages" aria-label="Job progress">'+stages.map(function(stage,i){return '<li class="'+(i<index?'done':i===index?'current':'')+'"'+(i===index?' aria-current="step"':'')+'><span aria-hidden="true">'+(i<index?'✓':i+1)+'</span>'+labels[i]+'</li>';}).join('')+'</ol>';
    var hasTimer=!terminal&&(booking.status==='in_progress'||booking.status==='completed')&&booking.work_started_at;
    return '<section class="booking-tracking" data-booking-track="'+Number(booking.id)+'"><h3>Job progress</h3>'+progress+
      (hasTimer?'<div class="booking-tracking-values"><div><span>Work duration</span><strong data-work-clock>—</strong></div><div><span>'+(booking.status==='completed'?'Final job bill':'Estimated job bill')+'</span><strong data-work-estimate>—</strong></div></div><p class="booking-tracking-note">'+(booking.status==='completed'?'Payment and worker settlement are shown separately below.':'Estimate includes the diagnosis fee where applicable. Final billing uses recorded whole work minutes; worker commission is separate.')+'</p>':'')+
      '<p class="booking-tracking-note" data-track-feedback>Latest recorded status. This is not live GPS tracking.</p><button type="button" class="btn btn-outline" data-refresh-tracking>Refresh Status</button></section>';
  }
  function fingerprint(b){return JSON.stringify([b.status,b.payment_status,b.paid_amount,b.total_amount,b.work_started_at,b.work_ended_at,b.work_approved_at,b.work_declined_at,b.diagnosis_notes]);}
  function stop(){if(interval!=null){clearInterval(interval);interval=null;}}
  function mount(root,booking,options){
    stop();options=options||{};
    var card=root.querySelector('[data-booking-track]');if(!card)return;
    var clock=card.querySelector('[data-work-clock]'),amount=card.querySelector('[data-work-estimate]'),feedback=card.querySelector('[data-track-feedback]'),button=card.querySelector('[data-refresh-tracking]');
    var busy=false,ticks=0;
    function active(){return root.isConnected!==false && root.querySelector('[data-booking-track]')===card && (!options.active||options.active());}
    function paint(){
      var value=snapshot(booking,Date.now());
      if(clock){var seconds=value.seconds;clock.textContent=seconds==null?'Time unavailable':String(Math.floor(seconds/3600)).padStart(2,'0')+':'+String(Math.floor(seconds%3600/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');}
      if(amount)amount.textContent=value.amount==null?'Rate needs review':options.money?options.money(value.amount):'₹'+value.amount;
    }
    function refresh(){
      if(busy||!active()||!options.fetchLatest)return Promise.resolve();
      if(options.canRefresh&&!options.canRefresh())return Promise.resolve();
      busy=true;button.disabled=true;feedback.textContent='Checking latest status...';
      return Promise.resolve().then(options.fetchLatest).then(function(latest){
        if(!active())return;
        if(!latest||Number(latest.id)!==Number(booking.id))throw Error('Status could not be loaded');
        feedback.textContent='Status checked. Latest recorded update is shown.';
        if(fingerprint(latest)!==fingerprint(booking)){if(options.changed)options.changed(latest);}
        else {booking=latest;paint();}
      }).catch(function(){if(active())feedback.textContent='Status could not be refreshed. Last loaded status is shown. Use Refresh Status to retry.';}).finally(function(){busy=false;if(active())button.disabled=false;});
    }
    button.onclick=refresh;paint();
    if(['requested','confirmed','en_route','checked_in','in_progress'].indexOf(booking.status)>=0){
      interval=setInterval(function(){if(!active()){stop();return;}paint();ticks++;if(ticks%15===0&&!(global.document&&global.document.hidden))refresh();},1000);
    }
    return {refresh:refresh,stop:stop};
  }
  global.HireNowTracking={html:html,mount:mount,snapshot:snapshot,utcTime:utcTime,stop:stop};
})(typeof window!=='undefined'?window:globalThis);
