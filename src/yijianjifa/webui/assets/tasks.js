P011Views.tasks=()=>{'use strict';
 const $=id=>document.getElementById(id),names={action_required:'需要处理',waiting_configuration:'资料待处理',waiting_adapter:'发布待接入',succeeded:'处理完成',partial:'部分完成',skipped:'已跳过',missed_waiting_user:'错过时间，等待处理',waiting:'等待中',running:'制作中',paused:'已暂停',interrupted:'等待继续',cancelling:'正在取消',cancelled:'已取消',completed:'制作完成',completed_with_errors:'制作完成（有失败）',publishing:'发布执行中',pre_submit_failed:'提交前失败',failed:'失败',paused_auth:'需要登录',result_unknown:'结果待确认',platform_scheduled:'平台已接收定时',published:'实际已发布'};
 function countsLabel(t,sep=' · '){if(t.version===2)return P011MultiTargets.counts(t);return ['成功'+t.succeeded,...(t.kind==='download'&&t.partial?['部分完成'+t.partial]:[]),'失败'+t.failed].join(sep);}
 function stateLabel(t){if(typeof t.status_label==='string'&&t.status_label)return t.status_label;return t.kind==='download'?({waiting:'等待下载',running:'下载处理中',paused:'等待处理',paused_auth:'等待下载验证',completed:'下载处理完成',completed_with_errors:'部分完成（有失败）'}[t.state]||names[t.state]||t.state):t.kind==='publish'&&t.state==='running'?'发布执行中':names[t.state]||t.state;}
 function appendPublishingIssues(row,data){
  const publishing=data.execution?.publishing;if(!publishing||!Array.isArray(publishing.targets))return;
  const attention=new Set(['pre_submit_failed','platform_rejected','failed','action_required','missed_waiting_user','paused_auth','waiting_adapter','waiting_configuration','missed_schedule','cancelled','result_unknown','interrupted','unknown']),targets=publishing.targets.slice(0,400).filter(target=>target&&attention.has(target.state));
  if(targets.length){const issues=el('div',undefined,'daily-publish-issues');issues.append(el('h4','发布需处理'));
   for(const target of targets){const issue=el('div',undefined,'daily-publish-issue');issue.append(el('b',target.account_label||target.platform_label||'所选账号'),el('p',target.title||'未记录标题'),el('p',target.state_label||names[target.state]||'状态待核对'));if(typeof target.reason==='string'&&target.reason)issue.append(el('p',target.reason,'daily-publish-issue-reason'));issues.append(issue);}row.append(issues);
  }
  const omitted=Number.isInteger(publishing.omitted)&&publishing.omitted>0?publishing.omitted:0,message=[publishing.complete===false?'部分记录暂不可读取':'',omitted?`另${omitted}项请查看发布任务`:''].filter(Boolean).join('；');if(message)row.append(el('p',message,'safe-note'));
 }
 function canDelete(t){if(t.version===2)return true;return t.kind==='download'?!['waiting','queued','running','cancelling'].includes(t.state):t.reviewed_removed||!['running','publishing','paused_auth','cancelling'].includes(t.state);}

 let alive=true,tab='all',page=0,total=0,rows=[],active=P011Router.pendingTaskId||'',selection=new Set(),timer,noticeTimer,busy=false,loading=false,detailKey='',renderKey='',generation=0,editor=null;delete P011Router.pendingTaskId;
 const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const id=()=>[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');
 async function api(operation,params,request_id=id()){const r=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id,operation,params})));if(!r.ok)throw Error(r.error.message);return r.data;}
 const intent=new P011WriteIntent(api,id);
 const multiIntent=new P011MultiClient(api,id,()=>{});
 const recoverIntent=button('核对原操作',async()=>{if(busy)return;busy=true;notice('正在核对原操作，请勿重复创建…');try{const receipt=await multiIntent.recover();if(!alive)return;if(receipt){if(editor){editor.remove();editor=null;}active=receipt.id||active;detailKey='';notice('原操作已找到持久回执；平台结果请查看目标状态。');}else notice('未找到持久受理回执，结果仍待核对，请勿重复创建。');}finally{busy=false;recoverIntent.hidden=!multiIntent.pending;await load();}});recoverIntent.hidden=!multiIntent.pending;$('rows').closest('section').prepend(recoverIntent);
 function notice(text){if(alive){clearTimeout(noticeTimer);$('toast').textContent=text;$('toast').style.display='block';noticeTimer=setTimeout(()=>{if(alive)$('toast').style.display='none';},3000);}}
 function button(text,fn,cls='btn'){const b=el('button',text,cls);b.addEventListener('click',e=>{if(e.isTrusted)Promise.resolve(fn()).catch(e=>notice(e.message));});return b;}
 function closeContext(){ $('ctx').style.display='none'; }
 function outsideContext(e){if(!e.target?.closest?.('#ctx'))closeContext();}
 function contextKey(e){if(e.key==='Escape')closeContext();}
 document.addEventListener('pointerdown',outsideContext);document.addEventListener('keydown',contextKey);document.addEventListener('scroll',closeContext,true);
 function discardEditor(){if(editor)editor.remove();editor=null;$('batchModal').hidden=true;detailKey='';}
 function navigate(fn){closeContext();if(busy){notice('正在更新任务，请稍候。');return;}discardEditor();return fn();}
 window.p011Navigate=navigate;window.p011BeforeClose=()=>{closeContext();discardEditor();return api('app.exit',{}).catch(e=>notice(e.message));};
 document.querySelector('.nav').replaceChildren(...['资料库','混剪制作','成品库','普通发布','团购带货','账号管理','任务记录','常用方案与设置'].map((name,i)=>{const b=button(name,()=>navigate(()=>P011Router.go(i===3?'publish':i===4?'commerce':i===5?'accounts':i===7?'presets':['library','mixing','finished'][i])),i===6?'on':'');b.disabled=i>2&&i!==3&&i!==4&&i!==5&&i!==7;return b;}));
 const pager=el('div',undefined,'toolbar'),prev=button('上一页',()=>{page--;load();}),next=button('下一页',()=>{page++;load();}),pageLabel=el('span');pager.append(prev,pageLabel,next);$('rows').closest('section').append(pager);
 const checkStatus=el('p',undefined,'notice');checkStatus.setAttribute('role','status');checkStatus.hidden=true;$('rows').closest('section').prepend(checkStatus);
 async function refreshCheckStatus(){let state='unavailable';try{state=(await api('local.checks.status',{})).state;}catch(e){}if(!alive)return;const messages={failed:'本地定时检查失败，正在重试；错过时间的标记可能延迟。不会自动补发，请暂勿根据列表判断是否已到点。若持续出现，请重启程序后检查任务记录。',stopped:'本地定时检查未运行，不能及时标记错过的安排；不会自动补发。请重启程序后检查任务记录。',starting:'本地定时检查正在启动，请稍候确认任务状态。'};checkStatus.hidden=state==='healthy';checkStatus.textContent=state==='healthy'?'':messages[state]||'暂时无法读取本地定时检查状态，请稍后重试；不能据此认定检查正常。';}
 const filters=()=>({query:$('q').value,tab,status:$('status').value});
 async function load(){if(loading||!alive)return;loading=true;try{await refreshCheckStatus();if(!alive||editor)return;const data=await api('task.center.list',{...filters(),offset:page*20,limit:20});if(!alive)return;total=data.total;rows=data.items;if(page>0&&!rows.length){page=Math.max(0,Math.ceil(total/20)-1);loading=false;return load();}if(!active)active=rows[0]?.id||'';render();await detail();}catch(e){notice(e.message);}finally{loading=false;}}
 function selectTask(pid){active=pid;detailKey='';render();detail().catch(e=>notice(e.message));}
 function render(){if(!alive)return;const key=JSON.stringify([rows,active,[...selection],page,total]);if(key===renderKey)return;renderKey=key;$('rows').replaceChildren(...rows.map(t=>{const tr=el('tr',undefined,'row'+(active===t.id?' active':'')),check=el('input');tr.setAttribute('aria-selected',active===t.id?'true':'false');check.type='checkbox';check.checked=selection.has(t.id);check.setAttribute('aria-label','选择任务 '+t.name);check.addEventListener('change',()=>{check.checked?selection.add(t.id):selection.delete(t.id);render();});const td=el('td');td.append(check);tr.append(td,el('td',t.name),el('td',t.kind==='download'?'下载':t.kind==='make'?'制作':'发布'),el('td',t.manual_review_label||stateLabel(t)),el('td',`${t.completed}/${t.quantity} · ${countsLabel(t)}`),el('td',new Date(/[zZ]|[+-]\d\d:\d\d$/.test(t.created_at)?t.created_at:t.created_at+'Z').toLocaleString()));tr.addEventListener('click',e=>{if(e.target===check)return;selectTask(t.id);});tr.addEventListener('dblclick',e=>{if(e.target===check)return;selectTask(t.id);});tr.addEventListener('contextmenu',e=>{e.preventDefault();selectTask(t.id);const ctx=$('ctx');ctx.replaceChildren(button('查看详情',()=>{ctx.style.display='none';detailKey='';detail();}));if(t.kind==='publish'&&t.version===2&&['action_required','pre_submit_failed','completed_with_errors'].includes(t.state))ctx.append(button('重试未提交项',()=>{closeContext();retryUnsubmitted(t.id);}));if(t.kind==='make'){if(['waiting','running'].includes(t.state))ctx.append(button('暂停',()=>{ctx.style.display='none';control(t.id,'pause');}));if(['paused','interrupted'].includes(t.state))ctx.append(button('继续',()=>{ctx.style.display='none';control(t.id,'resume');}));}if(canDelete(t))ctx.append(button('删除任务',()=>{closeContext();deleteTask(t.id);}));ctx.style.left=Math.max(8,Math.min(e.clientX,(window.innerWidth||1200)-200))+'px';ctx.style.top=Math.max(8,Math.min(e.clientY,(window.innerHeight||800)-180))+'px';ctx.style.display='block';});return tr;}));$('deleteSelected').disabled=!selection.size||busy;selectionTools.update(selection.size,busy);$('all').checked=rows.length>0&&rows.every(t=>selection.has(t.id));P011Pager.update(pager,total,page,20,n=>{page=n;return load();});pageLabel.textContent=`共${total}项 · ${page+1}/${Math.max(1,Math.ceil(total/20))}`;prev.disabled=page===0;next.disabled=(page+1)*20>=total;const move=selection.size===1&&rows.some(t=>selection.has(t.id)&&t.kind==='make'&&t.state==='waiting');$('up').disabled=$('down').disabled=!move;}
 function retryable(d){return d.state==='pre_submit_failed'&&d.submission_status==='not_submitted'&&d.final_started===false&&d.worker_running===false&&!d.cancel_requested&&d.execution_mode==='platform_schedule'&&(d.allowed_actions||[]).includes('reprepare');}
 function retryFingerprint(d){return JSON.stringify([d.delivery_id||d.id,d.attempt,d.execution_mode,d.scheduled_at,d.effective_fields]);}
 async function retryUnsubmitted(task){
  if(busy||editor||multiIntent.pending)return;busy=true;
  try{
   const data=await multiIntent.send('detail',{id:task});if(!alive)return;
   const targets=(data.deliveries||[]).filter(retryable);
   if(!targets.length){notice('没有可重试的未提交定时项。');return;}
   const card=el('section',undefined,'card');editor=card;
   card.append(el('h3','重试原任务'),el('p','保持原视频、账号和定时时间，只重试尚未提交的项目。'));
   for(const d of targets)card.append(el('p',`${d.account_label} · ${d.effective_fields?.title||'未命名视频'} · 定时 ${new Date(d.scheduled_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`));
   const close=()=>{editor=null;card.remove();detailKey='';};
   const cancel=button('取消',()=>{if(!busy){close();load();}});
   const start=button('按原时间重试',async()=>{
    if(busy)return;busy=true;start.disabled=true;cancel.disabled=true;
    try{
     const latest=await multiIntent.send('detail',{id:task});
     for(const d of targets){const fresh=latest.deliveries?.find(v=>(v.delivery_id||v.id)===(d.delivery_id||d.id));if(!fresh||!retryable(fresh)||retryFingerprint(fresh)!==retryFingerprint(d))throw Error('原任务已变化，请重新查看；本次未开始发布。');}
     const ids=[];
     for(const d of targets){
      const did=d.delivery_id||d.id;
      if(!Number.isFinite(Date.parse(d.scheduled_at))||Date.parse(d.scheduled_at)<=Date.now())throw Error('原定时时间已过，请先调整安排。');
      const prepared=await multiIntent.write('target.reprepare',{delivery_id:did,fields:{},reset_fields:[]},{batch_id:task});
      if(prepared.execution_mode!==d.execution_mode||Date.parse(prepared.scheduled_at)!==Date.parse(d.scheduled_at))throw Error('定时安排不一致，本次未开始发布。');
      ids.push(did);
     }
     const check=await multiIntent.send('execution.check',{id:task,delivery_ids:ids});
     if(!check.available||check.delivery_ids?.length!==ids.length||ids.some(v=>!check.delivery_ids.includes(v)))throw Error('原任务暂不能继续，请查看登录状态和失败原因。');
     await multiIntent.write('execution.start',{id:task,delivery_ids:ids,digest:check.digest,consent:true,consent_partial:check.requires_partial_consent},{batch_id:task});
     notice('原任务已开始重试，定时时间保持不变。');
    }finally{busy=false;close();await load();}
   },'btn primary');card.append(cancel,start);$('detail').append(card);
  }finally{busy=false;}
 }
 async function control(task,action){if(busy||editor)return;busy=true;try{if(rows.some(t=>t.id===task&&t.kind==='download')){await intent.run('download.'+action,{task_id:task,...(action==='resume'?{show_browser:false}:{})});detailKey='';}else await intent.run(action==='cancel'&&rows.some(t=>t.id===task&&t.kind==='publish')?'publish.cancel':action==='up'||action==='down'?'task.move':'task.'+action,action==='up'||action==='down'?{id:task,delta:action==='up'?-1:1}:{id:task});detailKey='';}finally{busy=false;}await load();}
 async function detail(){if(!active||!alive||editor)return;const pid=active,ticket=++generation;const data=await api('task.center.detail',{id:pid});if(!alive||editor||pid!==active||ticket!==generation)return;const key=JSON.stringify(data);if(key===detailKey)return;detailKey=key;const target=$('detail');target.querySelectorAll('video').forEach(v=>v.pause());target.replaceChildren();$('detailTitle').textContent='任务信息';
 const date=value=>value?new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value)?value:value+'Z').toLocaleString():'未记录';
 const text=(label,value)=>{if(value!==undefined&&value!==null&&value!=='')target.append(el('p',label+'：'+value));};
 target.append(el('h3',data.task.name));text('创建时间',date(data.task.created_at));text('状态',stateLabel(data.task));text('进度',countsLabel(data.version===2?{...data.task,...data}:data.task));
 target.append(button('复制执行日志',async()=>{const result=await api('task.execution.copy',{id:pid});if(result.copied)notice('执行日志已复制。');else notice('复制未完成，请重试。');}));
 if(canDelete({...data.task,version:data.version||data.task.version}))target.append(button('删除任务',()=>deleteTask(pid)));
 if(data.daily_publish){const daily=data.daily_publish,row=el('section',undefined,'card'),phase={checking_login:'正在检查账号',needs_login:'需要登录',making:'制作中',publishing:'发布中',accepted:'平台已接收',needs_attention:'需要处理',completed:'已完成',cancelled:'已取消'};row.append(el('b','制作后发布'),el('p','状态：'+(phase[daily.phase]||'等待处理')));if(daily.reason)row.append(el('p','原因：'+daily.reason));if(daily.can_continue)row.append(button(daily.phase==='needs_login'?'登录并继续本批':'重新检查并继续本批',()=>navigate(()=>{P011Router.dailyBatchId=pid;P011Router.go('mixing');})));appendPublishingIssues(row,data);for(const child of daily.publish_task_ids||[])row.append(button('查看发布任务',()=>selectTask(child)));target.append(row);}
 const platforms={douyin:'抖音',kuaishou:'快手',channels:'视频号',bilibili:'B站'};
 if(data.version===2||data.task.version===2){
  P011MultiBatches.add(pid);
  for(const d of data.deliveries||[]){if(d.hidden)continue;
   const row=el('section',undefined,'card');const fields=d.effective_fields||{};
   row.append(el('b',fields.title||d.title||(d.content_type==='image_post'||data.task.content_type==='image_post'?'未命名图文':'未命名视频')),el('p',(platforms[d.platform]||d.platform)+' · '+(d.account_label||'未记录账号')),el('p','状态：'+(d.stage||names[d.state]||d.state)),el('p','发布时间：'+(d.execution_mode==='publish_now'?'立即发布':date(d.scheduled_at))));
   if(d.reason_message)row.append(el('p','原因：'+d.reason_message));

   if(['paused_auth','result_unknown'].includes(d.state))row.append(button('处理验证',async()=>{
    if(busy)return;busy=true;
    try{const opened=await window.p011OpenPublishVerification?.(d.delivery_id||d.id);if(!opened)notice('原验证页面暂不可用，请先核对原任务结果；不要重复发布。');}finally{busy=false;}
   }));

   if((d.platform==='channels'||d.content_type==='image_post')&&d.final_started&&(d.allowed_actions||[]).includes('reconcile'))row.append(button('核对发布结果',async()=>{
    if(busy)return;busy=true;
    try{const checked=await window.p011CheckPublishResult?.(d.delivery_id||d.id);if(!checked)await multiIntent.write('target.reconcile',{delivery_id:d.delivery_id||d.id},{batch_id:pid});detailKey='';notice('正在核对发布结果。');}finally{busy=false;await load();}
   }));
   const fieldsBox=el('details');fieldsBox.append(el('summary','查看发布文案'));
   for(const [key,label]of [['title','标题'],['body','正文'],['tags','标签']])if(fields[key])fieldsBox.append(el('p',label+'：'+(Array.isArray(fields[key])?fields[key].join(' '):fields[key])));
   row.append(fieldsBox);
   const logBox=el('details');logBox.append(el('summary','查看执行日志'));
   const logNames={'sms.cooldown':'等待短信验证码60秒间隔，结束后自动继续','account.wait':'等待账号空闲，空闲后自动继续','execution.start':'开始执行','execution.error':'执行异常','execution.result':'执行结束','submission.final_check':'提交前检查','submission.authorized':'提交检查通过','bilibili.baseline':'读取账号与已有稿件','bilibili.prepare':'准备上传与发布资料','bilibili.result':'读取平台提交结果','bilibili.form_readback':'核对发布表单','channels.login_check':'检查视频号登录状态','channels.login_valid':'视频号登录有效','channels.login_expired':'视频号登录已失效','channels.baseline':'检查视频号账号与已有作品','channels.prepare':'准备视频号发布资料','channels.form_readback':'核对视频号发布表单','channels.result':'读取视频号提交结果','channels.original_reminder_continue':'按不声明原创继续提交','channels.original_reminder_dismiss':'关闭原创收益提醒','channels.submit_request':'核对视频号提交请求','channels.submit_response':'识别视频号成功回执','browser.close':'关闭发布页面','image.upload_start':'开始上传图文图片','image.upload_ready':'图文图片上传完成','image.order_verified':'核对图文图片顺序','image.form_verified':'核对图文发布表单','image.receipt_received':'收到图文发布回执','image.confirm_submit':'确认图文提交'};
   for(const entry of d.execution_log||[]){const line=el('p');line.textContent=date(entry.time)+' · '+(logNames[entry.step]||entry.step)+' ['+entry.step+'] · '+Object.entries(entry.checks||{}).map(([k,v])=>k+'='+v).join('，');logBox.append(line);}
   if(!(d.execution_log||[]).length)logBox.append(el('p','此任务尚未记录执行日志；新版开始的任务将自动记录。'));
   row.append(logBox);target.append(row);
  }return;
 }
 for(const issue of data.issues||[]){if(data.task.kind==='make'&&(data.items||[]).some(i=>i.index===issue.index&&(i.diagnostic?.reason||i.error)))continue;const card=el('section',undefined,'card');card.append(el('b',issue.title||'失败原因'));for(const [key,label]of [['object','对象'],['material','失败素材'],['stage','失败阶段'],['reason','原因']])if(issue[key])card.append(el('p',label+'：'+issue[key]));target.append(card);}
 for(const item of data.items||[]){const row=el('section',undefined,'card');row.append(el('b',item.title||'第'+(item.index+1)+'项'),el('p','状态：'+(names[item.state]||item.state||'未记录')));if(item.time)row.append(el('p',(data.task.kind==='make'?'记录时间：':'发布时间：')+date(item.time)));
  for(const [stage,value]of Object.entries(item.stages||{}))row.append(el('p',({video:'视频',audio:'音频',silent:'静音视频',asr:'语音字幕',ocr:'画面字幕'}[stage]||stage)+'：'+(names[value.state]||value.state)+(value.message?' · '+value.message:'')));
  const diagnostic=item.diagnostic||{};
  for(const [key,label]of [['material','失败素材'],['stage','失败步骤']])if(diagnostic[key])row.append(el('p',label+'：'+diagnostic[key]));
  if(!diagnostic.material&&diagnostic.materials?.length)row.append(el('p','参与素材：'+diagnostic.materials.join('；')));
  if(diagnostic.reason||item.message||item.error)row.append(el('p','原因：'+(diagnostic.reason||item.message||item.error)));
  if(data.task.kind==='make'){
   const logBox=el('details');logBox.append(el('summary','查看执行日志'));
   for(const entry of item.execution_log||[]){const fields=[date(entry.time),entry.step];for(const [key,label]of [['stage','步骤'],['material','素材'],['reason','原因'],['error_type','错误类型']])if(entry[key])fields.push(label+'：'+entry[key]);logBox.append(el('p',fields.join(' · ')));}
   if(!(item.execution_log||[]).length)logBox.append(el('p','此条未记录执行步骤；未补造历史日志。'));
   row.append(logBox);
  }
  target.append(row);
 }
 for(const output of data.outputs||[]){const row=el('div',undefined,'line');row.append(el('span',output.name),button('打开文件夹',()=>api('media.reveal',{kind:'finished',id:output.id,original:false})));target.append(row);}
 }

 function allowRearrange(pid,data){
  if(editor||busy)return;const card=el('section',undefined,'card');editor=card;
  const consent=el('input'),label=el('label');consent.type='checkbox';consent.setAttribute('aria-label','确认已撤销并允许新安排');
  label.append(consent,el('span','我确认对应作品已在后台撤销或删除，允许为此账号和视频新建一次安排。'));
  card.append(el('h3','允许重新安排一次'),el('p','作品：'+(data.items[0]?.title||'未填写标题')),el('p','只解除这条历史对一次新安排的限制，不执行旧任务，也不会现在上传或发布。请到成品库重新选择账号、视频和时间后提交。'),label);
  const close=button('暂不重新安排',()=>{if(busy)return;editor=null;card.remove();detailKey='';load();});
  const save=button('确认允许新安排',async()=>{if(busy)return;if(!consent.checked){notice('请先确认对应作品已撤销或删除。');return;}busy=true;save.disabled=close.disabled=consent.disabled=true;
   try{await intent.run('publish.review.allow_rearrange',{id:pid,digest:data.manual_review.digest,consent:true});editor=null;card.remove();detailKey='';notice('已允许重新安排一次，请到成品库选择视频；尚未上传或发布。');}
   finally{busy=false;save.disabled=close.disabled=consent.disabled=false;}await load();
  },'btn primary');card.append(close,save);$('detail').prepend(card);
 }
 function reviewResult(pid,data){
  if(editor||busy)return;const card=el('section',undefined,'card');editor=card;
  const mode=el('select'),note=el('textarea'),consent=el('input'),label=el('label');
  mode.setAttribute('aria-label','后台核对结果');mode.append(new Option('请选择核对结果',''),new Option('后台已接收定时安排','scheduled'),new Option('后台已公开发布','published'),new Option('已在后台撤销排期或删除作品','removed'),new Option('后台暂未找到，仍需继续核对','not_found'));mode.value='';
  note.setAttribute('aria-label','核对依据');note.placeholder='填写核对时间、作品标题、后台状态及处理结果';note.maxLength=1000;
  consent.type='checkbox';consent.setAttribute('aria-label','已核对账号和作品');label.append(consent,el('span','我已核对对应账号、作品和安排；此处只记录结果，不替我撤销或发布。'));
  card.append(el('h3','记录后台核对结果'),el('p','账号：'+(data.task.account||'请查看子项目账号')+' · 作品：'+(data.items[0]?.title||'未填写标题')),el('p','请先在账号管理中打开对应后台核对。暂未找到不等于未提交，请勿重新发布。'),mode,note,label);
  const close=button('取消记录',()=>{if(busy)return;editor=null;card.remove();detailKey='';load();});
  const save=button('保存核对记录',async()=>{if(busy)return;if(!mode.value||!note.value.trim()||!consent.checked){notice('请选择结果、填写核对依据，并确认已核对账号和作品。');return;}busy=true;save.disabled=close.disabled=mode.disabled=note.disabled=consent.disabled=true;
   try{await intent.run('publish.review.save',{id:pid,digest:data.manual_review.digest,outcome:mode.value,note:note.value.trim(),consent:true});editor=null;card.remove();detailKey='';notice('人工核对记录已保存，未执行平台操作。');}
   finally{busy=false;save.disabled=close.disabled=mode.disabled=note.disabled=consent.disabled=false;}await load();
  },'btn primary');card.append(close,save);$('detail').prepend(card);
 }
 async function removeRecords(ids){
  if(busy||!ids.length)return;closeContext();discardEditor();busy=true;renderKey='';render();
  let removed=0;const errors=[];
  try{for(const pid of ids){try{await intent.run('task.delete',{id:pid});selection.delete(pid);removed++;if(active===pid){active='';$('detail').replaceChildren();}}catch(e){errors.push((rows.find(r=>r.id===pid)?.name||'所选任务')+'：'+e.message);}}}
  finally{busy=false;detailKey='';renderKey='';await load();}
  notice(errors.length?`已删除 ${removed} 个，${errors.length} 个未删除。`+errors.join('；'):'任务记录已删除，视频文件保留。');
 }
 function deleteSelected(){return removeRecords([...selection]);}
 $('deleteSelected').addEventListener('click',()=>deleteSelected().catch(e=>notice(e.message)));
 function deleteTask(pid){return removeRecords([pid]);}
 function cancelTask(pid,kind){
  if(editor||busy)return;const card=el('section',undefined,'notice');editor=card;
  card.append(el('p',kind==='download'?'取消尚未完成的下载或提取？已经成功的文件和字幕保留。':kind==='publish'?'取消尚未执行的本地发布安排？已提交或结果待确认的记录、任务副本保留。':'取消尚未完成的制作？已经成功的成品保留。'));
  const keep=button('继续保留',()=>{if(busy)return;editor=null;card.remove();detailKey='';load();});
  const confirm=button('确认取消',async()=>{
   if(busy)return;busy=true;keep.disabled=confirm.disabled=true;
   try{await intent.run(kind==='download'?'download.cancel':kind==='publish'?'publish.cancel':'task.cancel',kind==='download'?{task_id:pid}:{id:pid});editor=null;card.remove();detailKey='';}
   finally{busy=false;keep.disabled=confirm.disabled=false;}await load();
  },'btn primary');card.append(keep,confirm);$('detail').prepend(card);
 }
 function cancelAccount(pid,account){
  if(editor||busy)return;const card=el('section',undefined,'card');editor=card;
  card.append(el('p','仅取消账号「'+account.label+'」尚未执行的本地安排。其他账号不变；已提交、结果待确认的记录及任务副本保留。'));
  const keep=button('继续保留此账号',()=>{if(busy)return;editor=null;card.remove();detailKey='';load();});
  const confirm=button('确认取消此账号',async()=>{
   if(busy)return;busy=true;keep.disabled=confirm.disabled=true;
   try{const result=await intent.run('publish.cancel',{id:pid,account_id:account.account_id});editor=null;card.remove();detailKey='';notice('已取消 '+result.cancelled+' 项本地安排；其他账号保持原状。');}
   finally{busy=false;keep.disabled=confirm.disabled=false;}await load();
  },'btn primary');card.append(keep,confirm);$('detail').prepend(card);
 }
 function reschedule(pid,item){if(editor||busy)return;const card=el('section',undefined,'card'),mode=el('select'),when=el('input'),consent=el('input'),risk=el('label'),actions=el('div',undefined,'line');editor=card;mode.setAttribute('aria-label','重新安排方式');mode.append(new Option('平台定时','platform_schedule'),new Option('软件定时','software_schedule'),new Option('立即发布','publish_now'));when.type='datetime-local';when.setAttribute('aria-label','新的发布时间');const future=new Date();future.setDate(future.getDate()+1);future.setHours(12,0,0,0);when.value=new Date(future.getTime()-future.getTimezoneOffset()*60000).toISOString().slice(0,16);consent.type='checkbox';risk.append(consent,el('span','我明确同意软件定时：保持程序运行、电脑开机联网不睡眠；到点操作不保证公开显示时间，错过不自动补发。'));risk.hidden=true;mode.onchange=()=>{when.disabled=mode.value==='publish_now';risk.hidden=mode.value!=='software_schedule';consent.checked=false;};card.append(el('h3','重新安排这条发布'),el('p',`${item.title||item.video_id} · 账号 ${item.account_id}`),el('p','原确认记录保留。本次只修改此项，保存后仍暂停，不提交平台。定时时间须在当前时间2.5小时后至13天后之间；立即发布不受此限制。'),mode,when,risk);const close=()=>{if(busy)return;card.remove();editor=null;detailKey='';load();};actions.append(button('取消修改',close),button('确认保存新安排',async()=>{if(busy)return;const date=new Date(when.value);if(mode.value!=='publish_now'&&!Number.isFinite(date.getTime())){notice('请输入有效发布时间。');return;}if(mode.value==='software_schedule'&&!consent.checked){notice('请明确同意软件定时运行条件。');return;}const params={id:pid,item_key:item.account_id+':'+item.video_id,mode:mode.value,when:mode.value==='publish_now'?null:date.toISOString(),software_consent:mode.value==='software_schedule'&&consent.checked};busy=true;card.querySelectorAll('button,input,select').forEach(n=>n.disabled=true);try{await intent.run('publish.reschedule',params);editor=null;card.remove();active=pid;detailKey='';notice('新安排已保存，任务仍暂停，未提交平台。');}finally{busy=false;if(editor){card.querySelectorAll('button,input,select').forEach(n=>n.disabled=false);when.disabled=mode.value==='publish_now';}}await load();},'btn primary'));card.append(actions);$('detail').prepend(card);}
 $('all').hidden=true;
 const selectionTools=P011Selection.mount({host:$('selAll').parentElement,all:$('selAll'),clear:$('clear'),remove:$('deleteSelected'),count:$('count')});
 async function selectFiltered(enabled){let offset=0;do{const p=await api('task.center.list',{...filters(),offset,limit:100});if(!alive)return;for(const t of p.items)enabled?selection.add(t.id):selection.delete(t.id);offset+=p.items.length;if(offset>=p.total||!p.items.length)break;}while(alive);render();}
 $('selAll').addEventListener('click',()=>selectFiltered(true).catch(e=>notice(e.message)));$('all').addEventListener('change',e=>selectFiltered(e.target.checked).catch(e=>notice(e.message)));$('clear').addEventListener('click',()=>{selection.clear();render();});for(const [key,action] of [['up','up'],['down','down']])$(key).addEventListener('click',()=>{if(selection.size===1)control([...selection][0],action).catch(e=>notice(e.message));});
 for(const key of ['q','status'])$(key).addEventListener(key==='q'?'input':'change',()=>{page=0;load();});document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{tab=b.dataset.tab;page=0;document.querySelectorAll('.tab').forEach(n=>n.classList.toggle('on',n===b));load();}));$('restore').addEventListener('click',()=>load());
 async function tick(){await load();if(alive)timer=setTimeout(tick,1500);}tick();return ()=>{$('batchModal').hidden=true;closeContext();document.removeEventListener('pointerdown',outsideContext);document.removeEventListener('keydown',contextKey);document.removeEventListener('scroll',closeContext,true);alive=false;generation++;clearTimeout(timer);clearTimeout(noticeTimer);$('toast').style.display='none';$('detail')?.querySelectorAll('video').forEach(v=>v.pause());};
};
