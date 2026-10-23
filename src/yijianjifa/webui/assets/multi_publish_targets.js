/* Exact V2 target controls. Actions come only from host allowed_actions. */
(function(root){'use strict';
const PLATFORMS={douyin:'抖音',kuaishou:'快手',channels:'视频号',bilibili:'B站'};
function counts(data){const s=data.summary,images=data.content_type==='image_post'||data.task?.content_type==='image_post'||Number(s.image_posts)>0;return `${images?(s.image_posts||0)+'篇图文':s.videos+'个视频'} · ${s.accounts}个账号 · ${s.targets}项发布 · 平台已接收${data.accepted_count||0}项 · 已公开${data.published_count||0}项`;}
function render(data,h){const {el,button,target}=h;target.append(el('h3',data.task.name),el('p',counts(data)),el('p','平台已接收、审核与公开是不同状态；本地受理不代表已上传。'));const batchActions=el('div',undefined,'line');for(const action of data.controls||[])batchActions.append(button(action==='pause'?'暂停本批已授权目标':'继续本批已授权目标',()=>h.run('multi_publish.batch.'+action,{id:data.id})));target.append(batchActions);
 const groups=new Map();for(const delivery of data.deliveries){if(delivery.hidden)continue;if(!groups.has(delivery.video_id))groups.set(delivery.video_id,[]);groups.get(delivery.video_id).push(delivery);}for(const [video,children]of groups){const group=el('details',undefined,'multi-video-targets');group.open=true;group.append(el('summary',(children[0].effective_fields?.title||children[0].title||video)+`（${children.length}项目标）`));for(const d of children){const line=el('section',undefined,'multi-target-row');line.dataset.deliveryId=d.delivery_id;line.append(el('b',(PLATFORMS[d.platform]||d.platform)+' · '+d.account_label),el('p',d.stage||d.state),el('p',d.execution_mode==='publish_now'?'立即方式':(d.execution_mode==='software_schedule'?'本机排期':'平台定时')+' · '+(d.scheduled_at?new Date(d.scheduled_at).toLocaleString():'未安排')),el('p',d.submission_status==='uncertain'?'提交结果未确认，请核对原结果，勿重复投稿。':d.submission_status==='submitted'?'已记录平台提交；公开状态以目标回执为准。':'本地目标尚未提交'),el('p',d.reason_message||({waiting_adapter:'发布通道尚未接入，请等待接入完成。',waiting_configuration:'发布资料或接入条件待处理。',result_unknown:'提交结果尚未确认，请核对原结果。'}[d.state]||'')));if(d.receipt_summary)line.append(el('p','回执：'+JSON.stringify(d.receipt_summary)));const actions=el('div',undefined,'line');for(const action of d.allowed_actions||[]){const labels={start:'开始本目标',pause:'暂停本目标',resume:'继续同账号目标',reprepare:'修改本目标资料',reschedule:'调整本目标时间',reconcile:'核对原结果',cancel:'取消本地目标',hide:'移出目标列表'},b=button(labels[action],()=>perform(action,d));b.setAttribute('aria-label',(action==='reconcile'?'核对原结果':action==='reprepare'?'修改本目标资料':labels[action])+' '+d.delivery_id);actions.append(b);}line.append(actions);const fields=el('details');fields.append(el('summary','冻结发布资料'),el('pre',JSON.stringify(d.effective_fields||{},null,2)));line.append(fields);group.append(line);}target.append(group);}
 function editor(title,d){const card=el('section',undefined,'card');card.append(el('h3',title),el('p',(PLATFORMS[d.platform]||d.platform)+' · '+d.account_label+' · '+(d.effective_fields?.title||d.title||d.video_id)));if(!h.begin(card))return null;target.prepend(card);return card;}
 function confirm(title,d,message,fn,consentRequired=false){const card=editor(title,d);if(!card)return;const consent=el('input'),label=el('label');consent.type='checkbox';consent.setAttribute('aria-label','确认本目标操作');label.append(consent,el('span',message));card.append(consentRequired?label:el('p',message));card.append(button('暂不处理',()=>h.end(card)),button('确认本目标操作',async()=>{if(consentRequired&&!consent.checked){h.notice('请明确确认本目标与同一账号。');return;}await fn();h.end(card);await h.refresh();},'btn primary'));}
 async function perform(action,d){if(action==='reconcile'||action==='pause'){await h.run('multi_publish.target.'+action,{delivery_id:d.delivery_id});return;}
 if(action==='cancel'||action==='hide'){confirm(action==='cancel'?'取消本地目标':'移出目标列表',d,action==='cancel'?'仅取消此目标本地意图；已提交或结果未确认的证据保留，不撤回平台作品。':'保留本目标历史证据，其他目标继续显示。',()=>h.run('multi_publish.target.'+action,{delivery_id:d.delivery_id}));return;}
 if(action==='resume'){confirm('继续同账号目标',d,'我确认仍使用此目标原账号继续验证和处理，不切换为另一账号。',()=>h.run('multi_publish.target.resume',{delivery_id:d.delivery_id,consent_same_identity:true}),true);return;}
 if(action==='start'){confirm('开始本目标',d,'仅允许这个明确目标执行，其余目标保持原安排。',async()=>{const check=await h.api('multi_publish.execution.check',{id:data.id,delivery_ids:[d.delivery_id]});if(!check.available)throw Error('目标暂不可执行，请重新核对状态。');await h.run('multi_publish.execution.start',{id:data.id,delivery_ids:check.delivery_ids,digest:check.digest,consent:true,consent_partial:check.requires_partial_consent});},true);return;}
 if(action==='reprepare'){
  const card=editor('修改本目标资料',d);if(!card)return;
  const fields={};let reset=[];const schema=d.field_schema||{},native=P011MultiPlatformFields.supported(schema),labels={title:'标题',body:'正文',tags:'标签',category_id:'分区',copyright:'原创 / 转载',source:'转载来源'};
  const keys=['title','body','tags',...(native?P011MultiPlatformFields.keysFor(schema):['category_id','copyright','source'].filter(k=>schema[k]))];
  function changed(key,value){fields[key]=value;reset=reset.filter(k=>k!==key);}
  function inherit(key){delete fields[key];if(!reset.includes(key))reset.push(key);}
  for(const key of native?['title','body','tags']:keys){
   const spec=schema[key]||{},wrap=el('label',labels[key]);let input;
   if(key==='category_id'){
    input=el('select');input.append(new Option('保持当前分区',''));
    const choices=(spec.choices||[]).filter(c=>Number.isInteger(c.id)&&c.id>0);
    for(const choice of choices)input.append(new Option(String(choice.label||choice.name||choice.id),String(choice.id)));
    if(!choices.length){input.disabled=true;card.append(el('p','分区选项尚未配置，需接入维护者提供当前官方分区；其他修改可先保存在原目标。'));}
   }else if(key==='copyright'){input=el('select');input.append(new Option('保持当前选择',''),new Option('原创','original'),new Option('转载','repost'));}
   else input=el(key==='title'?'input':'textarea');
   input.setAttribute('aria-label','目标'+labels[key]+' '+d.delivery_id);
   const existing=d.effective_fields?.[key];input.value=key==='copyright'?({1:'original',2:'repost'}[existing]||existing||''):String(existing??'');
   if(spec.max_length)input.maxLength=spec.max_length;
   input.oninput=input.onchange=()=>{if(key==='category_id'||key==='copyright'){if(!input.value){delete fields[key];return;}changed(key,key==='category_id'?Number(input.value):input.value);}else changed(key,input.value);};
   wrap.append(input);card.append(wrap);if(spec.help||spec.description)card.append(el('p',String(spec.help||spec.description)));
  }
  const platformFields=native?P011MultiPlatformFields.create({el,button,host:card,api:h.api,platform:d.platform,account_id:d.account_id,schema,values:fields,effective:d.effective_fields||{},inherit:true,prefix:'目标'+(PLATFORMS[d.platform]||d.platform)+' '+d.delivery_id+' ',isAlive:()=>target.contains(card)&&card.isConnected!==false,onInherit:key=>inherit(key),onChange:()=>{reset=reset.filter(k=>!Object.hasOwn(fields,k));}}):null;
  const end=()=>{platformFields?.dispose();h.end(card);};
  const cover=el('select');cover.setAttribute('aria-label','目标封面 '+d.delivery_id);cover.append(new Option('保持当前冻结封面','keep'),new Option('不选择封面','clear'),new Option('恢复冻结公共封面','inherit'));cover.value='keep';cover.onchange=()=>{if(cover.value==='clear')changed('cover',null);else if(cover.value==='inherit')inherit('cover');else{delete fields.cover;reset=reset.filter(k=>k!=='cover');}};card.append(el('label','封面'),cover);
  card.append(button('恢复冻结公共资料',()=>{for(const key of Object.keys(fields))if(key!=='cover')delete fields[key];reset=[...keys,...(reset.includes('cover')?['cover']:[])];platformFields?.reset?.();card.append(el('p','将删除本目标文字及平台专用覆盖，恢复冻结公共资料；保留当前封面选择，封面可单独选择恢复。'));}),button('取消修改',end),button('保存本目标资料',async()=>{platformFields?.materialize();reset=reset.filter(k=>!Object.hasOwn(fields,k));await h.run('multi_publish.target.reprepare',{delivery_id:d.delivery_id,fields,reset_fields:reset});end();await h.refresh();},'btn primary'));return;
 }
 if(action==='reschedule'){
  const card=editor('调整本目标时间',d);if(!card)return;
  const mode=el('select'),date=el('input'),error=el('p',undefined,'notice');let saving=false;
  mode.setAttribute('aria-label','目标排期方式 '+d.delivery_id);mode.append(new Option('平台定时','platform_schedule'),new Option('立即方式','publish_now'));mode.value=d.execution_mode==='publish_now'?'publish_now':'platform_schedule';
  date.type='datetime-local';date.setAttribute('aria-label','目标发布时间 '+d.delivery_id);const value=new Date();value.setDate(value.getDate()+1);value.setHours(12,0,0,0);const initial=d.scheduled_at?new Date(d.scheduled_at):value;
  date.value=new Date(initial.getTime()-initial.getTimezoneOffset()*60000).toISOString().slice(0,16);mode.onchange=()=>date.disabled=mode.value==='publish_now';mode.onchange();
  error.setAttribute('aria-label','目标时间保存状态 '+d.delivery_id);error.setAttribute('role','status');error.hidden=true;
  const cancel=button('取消调整',()=>{if(!saving)h.end(card);}),save=button('保存本目标时间',async()=>{
   if(saving)return;error.textContent='';error.hidden=true;
   const when=new Date(date.value);if(mode.value!=='publish_now'&&!Number.isFinite(when.getTime())){error.textContent='请输入有效时间。';error.hidden=false;return;}
   saving=true;save.disabled=cancel.disabled=mode.disabled=date.disabled=true;
   try{const result=await h.run('multi_publish.target.reschedule',{delivery_id:d.delivery_id,execution_mode:mode.value,scheduled_at:mode.value==='publish_now'?null:when.toISOString()});if(!target.contains(card))return;if(result?.id!==d.delivery_id)throw Error('未能确认本目标的新时间，原安排仍保留，请稍后重试。');h.end(card);await h.refresh();}
   catch(e){if(target.contains(card)){error.textContent=e.message;error.hidden=false;}}
   finally{saving=false;if(target.contains(card)){save.disabled=cancel.disabled=mode.disabled=false;date.disabled=mode.value==='publish_now';}}
  },'btn primary');
  card.append(el('p','只重新冻结本目标时间；平台限制由后端检查，不会自动换日或改为本机排期。'),mode,date,error,cancel,save);
 }
 }
}
root.P011MultiTargets={render,counts};
})(globalThis);
