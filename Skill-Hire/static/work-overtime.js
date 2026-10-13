(function(global){
  'use strict';
  function escape(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function html(b,role){
    if(b.status!=='in_progress'&&!(b.status==='completed'&&b.billable_minutes!=null))return '';
    if(b.approved_work_minutes==null)return '<section class="booking-tracking"><h3>Work billing</h3><p>This timer started under the previous hourly agreement. Its billing remains unchanged.</p></section>';
    var rate=Number(b.agreed_hourly_rate),limit=Number(b.approved_work_minutes),pending=b.overtime_state==='pending',extra=Number(b.overtime_requested_minutes)||0;
    var maximum=rate*limit/60+(Number(b.diagnosis_fee)||0),cost=rate*extra/60;
    var header='<section class="booking-tracking" data-overtime><h3>Approved work time</h3><p>'+limit+' minutes · Maximum job charge ₹'+escape(maximum.toFixed(2))+' before whole-rupee rounding.</p><p>The clock records total elapsed work. Extra minutes beyond the approved limit are not billed unless you approve more time. Extra time uses the same agreed rate of ₹'+escape(rate)+'/hour.</p>';
    if(b.status==='completed')return header+'<p>Recorded work: '+Number(b.actual_minutes)+' minutes · Billed work: '+Number(b.billable_minutes)+' minutes.</p></section>';
    if(pending)header+='<p><strong>Extra time requested: '+extra+' minutes · maximum additional work charge ₹'+escape(cost.toFixed(2))+'</strong></p><p style="white-space:pre-wrap;overflow-wrap:anywhere">'+escape(b.overtime_reason)+'</p><p>Approval raises the total billing limit to '+(limit+extra)+' minutes and can cover work already recorded beyond the current limit. Only actual recorded minutes are charged.</p>'+(role==='hirer'?'<button class="btn btn-safety" data-overtime-decision="approve">Approve extra time</button> <button class="btn btn-outline" data-overtime-decision="decline">Decline</button>':'<p>Waiting for hirer approval. Request extra time before continuing beyond the limit.</p>');
    else if(role==='worker')header+='<form data-overtime-form><label>Extra minutes <select name="minutes"><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option><option value="120">120 minutes</option></select></label><label style="display:block;margin-top:8px">Reason <textarea name="reason" required maxlength="500" placeholder="Explain why more time is needed"></textarea></label><button type="submit" class="btn btn-outline">Request extra time</button></form>';
    if(!pending&&b.overtime_state)header+='<p>Last extra-time request: '+escape(b.overtime_state)+'.</p>';
    return header+'<p data-overtime-feedback role="status" aria-live="polite"></p></section>';
  }
  function mount(root,b,options){
    var card=root.querySelector('[data-overtime]');if(!card)return;
    var busy=false,feedback=card.querySelector('[data-overtime-feedback]');
    function active(){return root.isConnected!==false&&root.querySelector('[data-overtime]')===card&&(!options.active||options.active());}
    function send(data){
      if(busy||!active())return Promise.resolve();busy=true;
      var buttons=card.querySelectorAll('button');buttons.forEach(function(btn){btn.disabled=true;});feedback.textContent='Saving extra-time request...';
      return Promise.resolve().then(function(){return options.api('/api/'+(options.role==='worker'?'worker/':'')+'bookings/'+Number(b.id)+'/overtime',{method:'POST',body:JSON.stringify(data)});}).then(function(r){
        if(!active())return;if(!r.ok)throw Error(r.data.error||'Extra-time action could not be confirmed.');
        feedback.textContent='Saved. Refreshing the approved time...';if(options.changed)options.changed();
      }).catch(function(err){if(active())feedback.textContent=(err.message||'Connection failed.')+' Refresh Status before retrying.';}).finally(function(){busy=false;if(active())buttons.forEach(function(btn){btn.disabled=false;});});
    }
    var form=card.querySelector('[data-overtime-form]');if(form)form.onsubmit=function(event){event.preventDefault();send({minutes:Number(form.elements.minutes.value),reason:form.elements.reason.value});};
    card.querySelectorAll('[data-overtime-decision]').forEach(function(btn){btn.onclick=function(){send({decision:btn.dataset.overtimeDecision,request_id:b.overtime_request_id});};});
    return {send:send};
  }
  global.HireNowOvertime={html:html,mount:mount};
})(typeof window!=='undefined'?window:globalThis);
