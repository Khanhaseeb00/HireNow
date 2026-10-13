(function(global){
 'use strict';
 var labels={worker_no_show:'Worker did not arrive',incomplete_work:'Work incomplete',payment_issue:'Payment issue',hirer_unavailable:'Hirer unavailable',wrong_address:'Wrong address',cash_not_received:'Cash not received',other:'Other issue'};
 var categories={hirer:['worker_no_show','incomplete_work','payment_issue','other'],worker:['hirer_unavailable','wrong_address','cash_not_received','payment_issue','other']};
 function escape(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function html(){return '<section class="booking-tracking" data-booking-issues><h3>Booking Help</h3><p class="booking-tracking-note">Reports and replies are visible to both parties and admin. Reporting an issue does not itself cancel a booking or issue a refund.</p><div data-issue-content role="status">Loading issues...</div></section>';}
 function mount(root,opts){
  var card=root.querySelector('[data-booking-issues]');if(!card)return;
  var content=card.querySelector('[data-issue-content]'),base=(opts.role==='worker'?'/api/worker/bookings/':'/api/bookings/')+Number(opts.bookingId)+'/issues',busy=false,version=0,replyKeys={};
  function active(){return root.querySelector('[data-booking-issues]')===card&&(!opts.active||opts.active());}
  function editing(){var dirty=Array.from(content.querySelectorAll('textarea')).some(function(input){return !!input.value.trim();});if(dirty)card.dataset.issueEditing='1';else delete card.dataset.issueEditing;}
  function render(rows){
   delete card.dataset.issueEditing;
   content.innerHTML='<button class="btn btn-outline" type="button" data-reload-issues>Refresh Issues</button>'+rows.map(function(issue){
    var id=Number(issue.id);return '<article style="border-top:1px solid var(--line);margin-top:14px;padding-top:12px"><strong>#'+id+' · '+escape(labels[issue.category]||issue.category)+'</strong><p>'+escape(issue.status.replace(/_/g,' '))+' · Reported by '+escape(issue.reporter_role)+'</p><p style="white-space:pre-wrap;overflow-wrap:anywhere">'+escape(issue.description)+'</p>'+
     (issue.resolution?'<p style="white-space:pre-wrap;overflow-wrap:anywhere"><b>'+(issue.status==='resolved'?'Resolution':'Previous resolution')+':</b> '+escape(issue.resolution)+'</p>':'')+
     (issue.events||[]).map(function(event){return '<p style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px"><b>'+escape(event.actor_role)+' · '+escape(event.event_type)+'</b> '+escape(event.body)+'</p>';}).join('')+
     '<form data-issue-reply="'+id+'"><label>Reply'+(issue.status==='resolved'&&issue.reporter_role===opts.role?' (reopens your report)':'')+'<textarea class="ref-notes" data-reply-body required maxlength="2000" style="width:100%;box-sizing:border-box"></textarea></label><button type="submit" class="btn btn-outline">Send Reply</button></form></article>';
   }).join('')+'<form data-report-issue style="margin-top:18px"><h4>Report a booking issue</h4><label>Issue type<select class="loc-select" data-issue-category style="width:100%">'+categories[opts.role].map(function(key){return '<option value="'+key+'">'+labels[key]+'</option>';}).join('')+'</select></label><label>What happened?<textarea class="ref-notes" data-issue-description required minlength="10" maxlength="2000" style="width:100%;box-sizing:border-box" placeholder="Explain the problem and what resolution you need."></textarea></label><button type="submit" class="btn btn-safety">Submit Issue</button></form><p data-issue-feedback role="status" style="white-space:pre-wrap"></p>';
   content.querySelector('[data-reload-issues]').onclick=function(){if(card.dataset.issueEditing==='1'){content.querySelector('[data-issue-feedback]').textContent='Send or clear your draft before refreshing.';return;}load();};
   content.querySelectorAll('textarea').forEach(function(input){input.oninput=editing;});
   content.querySelector('[data-report-issue]').onsubmit=function(event){event.preventDefault();var form=this,description=form.querySelector('[data-issue-description]').value.trim(),category=form.querySelector('[data-issue-category]').value;if(description.length<10)return;send(base,{category:category,description:description},function(result){if(result.replayed&&result.issue.description!==description){var textarea=content.querySelector('[data-issue-reply="'+Number(result.issue.id)+'"] textarea');if(textarea){textarea.value=description;editing();}content.querySelector('[data-issue-feedback]').textContent='An existing issue thread was opened. Your new details are kept as a reply draft.';}});};
   content.querySelectorAll('[data-issue-reply]').forEach(function(form){form.onsubmit=function(event){event.preventDefault();var id=form.dataset.issueReply,body=form.querySelector('textarea').value.trim();if(!body)return;var previous=replyKeys[id];if(!previous||previous.body!==body)replyKeys[id]={body:body,key:crypto.randomUUID()};send(base+'/'+Number(id)+'/reply',{body:body,request_key:replyKeys[id].key},function(){delete replyKeys[id];});};});
  }
  function load(){var token=++version;return opts.api(base).then(function(r){if(!active()||token!==version)return;if(!r.ok||!Array.isArray(r.data))throw Error(r.data.error||'Issues could not be loaded');render(r.data);}).catch(function(error){if(!active()||token!==version)return;content.textContent=error.message;var retry=document.createElement('button');retry.type='button';retry.className='btn btn-outline';retry.textContent='Retry Issues';retry.onclick=load;content.appendChild(retry);});}
  function send(path,payload,after){
   if(busy)return;busy=true;var feedback=content.querySelector('[data-issue-feedback]');feedback.textContent='Saving issue update...';content.querySelectorAll('button').forEach(function(button){button.disabled=true;});
   return opts.api(path,{method:'POST',body:JSON.stringify(payload)}).then(function(r){if(!active())return;if(!r.ok)throw Error(r.data.error||'Update could not be confirmed. Retry with the same details.');return load().then(function(){if(active()&&after)after(r.data);});}).catch(function(error){if(active())feedback.textContent=error.message;}).finally(function(){busy=false;if(active())content.querySelectorAll('button').forEach(function(button){button.disabled=false;});});
  }
  load();return {load:load};
 }
 global.HireNowIssues={html:html,mount:mount,escape:escape};
})(typeof window!=='undefined'?window:globalThis);
