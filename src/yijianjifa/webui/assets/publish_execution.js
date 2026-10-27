/* Read-only progress for the batch explicitly accepted on this page.
 * Local execution authorization is not evidence of platform acceptance. */
var P011PublishExecution=(()=>{'use strict';
 const received=new Set(['published','platform_scheduled','platform_accepted','processing','reviewing']);
 const attention=new Set(['result_unknown','interrupted','unknown','pre_submit_failed','platform_rejected','failed','action_required','missed_waiting_user','paused_auth','waiting_adapter','waiting_configuration','missed_schedule','cancelled']);
 const platforms={douyin:'抖音',kuaishou:'快手',channels:'视频号',bilibili:'B站'};
 const stateLabels={waiting:'等待处理',running:'正在发布',paused:'已暂停',paused_auth:'等待平台验证',result_unknown:'发布结果待核对',interrupted:'发布结果待核对',unknown:'发布结果待核对',platform_scheduled:'平台已接收定时安排',platform_accepted:'平台已接收',processing:'平台已接收，处理中',reviewing:'平台已接收，审核中',published:'作品已公开',pre_submit_failed:'提交前未完成',platform_rejected:'平台未接收',failed:'本条需要处理',action_required:'等待处理',missed_waiting_user:'等待处理',waiting_adapter:'发布通道需要处理',waiting_configuration:'发布设置需要处理',missed_schedule:'发布时间需要调整',cancelled:'本条已取消'};
 const steps={
  'sms.cooldown':'等待短信验证码60秒间隔，结束后自动继续','account.wait':'等待账号空闲，空闲后自动继续','execution.start':'开始处理发布目标','submission.final_check':'正在核对最终发布资料','submission.authorized':'本次提交授权已核对','execution.error':'发布处理遇到问题','browser.close':'发布窗口清理结果已记录',
  driver_start:'启动发布组件',browser_launch:'打开发布窗口',session_restore:'恢复账号登录状态',navigation:'打开平台页面',identity:'核对发布账号',upload_input:'检查视频上传入口',upload:'上传视频',baseline_read:'读取已有作品与安排',baseline_readback:'核对已有作品与安排',form_read:'读取发布表单',form_readback:'核对发布表单',result_read:'读取平台处理结果',result_readback:'核对平台处理结果',final_click:'执行平台提交',baseline:'检查账号与已有作品',prepare:'准备上传与发布资料',result:'读取平台处理结果',upload_observation:'检查视频上传状态',login_check:'检查账号登录状态',login_valid:'账号登录有效',login_expired:'账号登录已失效',submit_request:'核对平台提交请求',submit_response:'识别平台提交回执',timeout:'发布处理超时',preparation_timeout:'发布准备超时',cancelled:'发布处理已取消',original_reminder_check:'检查原创提示',original_reminder_guard:'核对原创提示状态',original_reminder_form:'核对原创提示表单',original_reminder_dismiss:'关闭原创收益提醒',original_reminder_continue:'按不声明原创继续提交'
 };
 const ordinaryStages=new Set(['任务已开始','等待账号空闲','正在打开发布浏览器','等待账号验证','正在核对提交结果','正在核对账号和已有安排','正在上传并填写发布资料','正在提交平台定时安排']);
 const integer=value=>Number.isInteger(value)&&value>=0?value:null;
 const clean=value=>typeof value==='string'?value.slice(0,300):'';
 function stepGroup(step){
  if(['execution.start','account.wait','sms.cooldown'].includes(step))return step;
  if(['execution.result','execution.error'].includes(step))return 'result';
  if(step==='submission.authorized')return 'submit';
  const match=typeof step==='string'&&step.match(/^(douyin|kuaishou|channels|bilibili)\.([a-z_]+)$/);
  return match&&({upload:'upload',final_click:'submit',timeout:'result',preparation_timeout:'result',login_expired:'login',cancelled:'result'})[match[2]]||'';
 }
 function stepLabel(step,state){if(!stepGroup(step))return '';if(step==='execution.result')return stateLabels[state]||'发布结果已记录';if(stepGroup(step)==='submit')return '提交发布';if(Object.hasOwn(steps,step))return steps[step];const match=typeof step==='string'&&step.match(/^(douyin|kuaishou|channels|bilibili)\.([a-z_]+)$/);return match&&Object.hasOwn(steps,match[2])?steps[match[2]]:'';}
 function stageLabel(value){return ordinaryStages.has(value)?value:stepLabel(value,'')||'';}
 function create({el,button,host,summaryHost,api,onTask,notice=()=>{},compact=false}){
  let alive=true,mode='idle',taskId=null,epoch=0,timer=null,inFlight=false,known=null,complete=false;
  const records=new Map(),content=el('div',undefined,'publish-execution-content');
  host.replaceChildren(el('h3','执行进度'),content);
  const shared=P011MixExecution({host:content,summaryHost,el,button,publishOnly:true,
   onDetails:batch=>{if(alive&&batch.id===taskId)onTask?.(batch.id);},
   onCopy:async batch=>{const version=epoch;try{const result=await api('task.execution.copy',{id:batch.id});if(alive&&version===epoch&&batch.id===taskId)notice(result.copied?'执行信息已复制。':'复制未完成，请重试。');}catch{if(alive&&version===epoch&&batch.id===taskId)notice('复制未完成，请重试。');}}
  });
  const visibilityHost=compact&&summaryHost?summaryHost:host;
  const compactReason=compact&&summaryHost?el('span',undefined,'publish-progress-error'):null;
  if(compactReason){compactReason.setAttribute('role','status');summaryHost.classList.add('is-inline');const details=[...content.querySelectorAll('button')].find(n=>n.textContent==='查看本批任务');summaryHost.append(compactReason,details);}
  function instant(value){if(typeof value!=='string'||!value)return null;const time=new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value)?value:value+'Z').getTime();return Number.isFinite(time)?time:null;}
  function textStamp(value){return instant(value)!==null?value:null;}
  function liveStamp(live){if(typeof live?.updated_at!=='number')return null;const date=new Date(live.updated_at*1000);return Number.isFinite(date.getTime())?date.toISOString():null;}
  function stop(){if(timer!==null)clearTimeout(timer);timer=null;}
  function visible(){return typeof document==='undefined'||document.hidden!==true&&document.visibilityState!=='hidden'&&visibilityHost.isConnected!==false&&!visibilityHost.hidden;}
  function syncHost(){if(compact&&summaryHost){summaryHost.dataset.taskId=taskId||'';summaryHost.dataset.mode=mode;}host.dataset.runState=content.dataset.runState||'idle';host.dataset.phase=content.dataset.phase||'idle';if(compactReason){if(taskId&&!known&&mode!=='idle')summaryHost.querySelector('.batch-progress-phase').textContent='正在读取进度';const reason=content.querySelector('.mixing-execution-reason');compactReason.textContent=reason?.textContent||'';compactReason.hidden=!compactReason.textContent||reason.classList.contains('hide');compactReason.title=compactReason.textContent;}}
  function reset(nextMode,id){stop();epoch++;mode=nextMode;taskId=id;inFlight=false;known=null;complete=false;records.clear();host.dataset.taskId=id||'';host.dataset.mode=nextMode;shared.idle();syncHost();}
  function bind(nextMode,id){if(!alive||typeof id!=='string'||!id)return false;if(mode!==nextMode||taskId!==id)reset(nextMode,id);return true;}
  function record(key,event){const old=records.get(key);if(!old||old.message!==event.message||old.title!==event.title||old.account_label!==event.account_label)records.set(key,event);while(records.size>100)records.delete(records.keys().next().value);}
  function render(data){
   if(!alive)return;known=data;
   const targets=data.targets,accepted=targets.filter(t=>received.has(t.state)).length,failed=targets.filter(t=>attention.has(t.state)).length,total=data.total,consistent=total!==null&&total>=targets.length,full=consistent&&data.integrity&&targets.length===total;
   const allReceived=full&&total>0&&accepted===total;
    const isActive=t=>typeof t.execution_active==='boolean'?t.execution_active:t.state==='running'||t.worker_running===true&&!['result_unknown','interrupted','unknown'].includes(t.state);
    const unknown=targets.some(t=>['result_unknown','unknown','interrupted'].includes(t.state)),verify=targets.some(t=>t.state==='paused_auth'),active=data.live?.running===true||targets.some(isActive);
   complete=allReceived&&!active;
   const phase=allReceived?'accepted':unknown||verify||failed?'needs_attention':active?'publishing':data.state==='paused'?'paused':'waiting';
   const validEvents=[];
   for(const target of targets)for(const event of target.events){const message=stepLabel(event.step,event.checks?.state||target.state);if(message&&textStamp(event.time)){validEvents.push(event);record(target.id+':'+stepGroup(event.step),{time:event.time,step:event.step,message,title:target.title,account_label:target.account_label});}}
   const liveStage=stageLabel(data.live?.stage),updatedAt=liveStamp(data.live);
   if(liveStage&&updatedAt)record('ordinary:'+String(data.live.index||0)+':'+liveStage,{time:updatedAt,step:'ordinary.stage',message:liveStage,title:clean(data.live.title),account_label:targets.find(t=>t.index===data.live.index-1)?.account_label||targets[0]?.account_label||''});
   const events=[...records.values()].sort((a,b)=>instant(a.time)-instant(b.time));
   const starts=validEvents.filter(event=>event.step==='execution.start'),first=starts.length?starts:validEvents;
   const startedAt=first.length?first.reduce((a,b)=>instant(a.time)<=instant(b.time)?a:b).time:textStamp(data.createdAt);
   const activity=[...validEvents.map(e=>e.time),...(updatedAt?[updatedAt]:[])],last=activity.length?activity.reduce((a,b)=>instant(a)>=instant(b)?a:b):null;
    const currentTarget=targets.find(t=>data.live?.index>0&&t.index===data.live.index-1)||targets.find(isActive)||targets.find(t=>attention.has(t.state));
   const current={label:liveStage||currentTarget?.lastStage||'',...(data.live?.index>0?{index:data.live.index,...(total>0?{total}:{})}:{}),title:clean(data.live?.title)||currentTarget?.title||'',account_label:currentTarget?.account_label||''};
   const omitted=consistent?Math.max(0,total-targets.length):1,partial=!full,problem=targets.find(t=>attention.has(t.state)&&t.reason);
   const status={phase,...(consistent?{total}:{}),...(data.integrity&&consistent?{accepted}:{}),failed,pending:consistent?Math.max(0,total-accepted-failed):undefined,
    reason:partial?'部分进度暂不可读取，请到本批任务核对。':problem?problem.account_label+'：'+problem.reason:'',execution:{current,events,publishing:{targets,complete:full,omitted:!data.integrity?Math.max(1,omitted):omitted},timing:{...(startedAt?{started_at:startedAt}:{}),...(last?{last_activity_at:last}:{}),active}}};
   shared.render({batch:{id:taskId},status,canContinue:false,canRepair:false});syncHost();
  }
  function target(raw,id,platform,accountLabel){
   const events=Array.isArray(raw.execution_log)?raw.execution_log.filter(e=>e&&typeof e==='object').slice(-500):[];let lastStage='';for(const event of events){const message=stepLabel(event.step,raw.state);if(message&&textStamp(event.time)&&event.step!=='execution.result')lastStage=message;}
   const state=clean(raw.state),account_id=clean(raw.account_id),chosenPlatform=clean(raw.platform)||platform;
   const platformName=platforms[chosenPlatform]||'发布平台',rawLabel=clean(raw.account_label)||accountLabel||'所选账号';
   accountLabel=rawLabel.startsWith(platformName+' · ')?rawLabel:platformName+' · '+rawLabel;
    const blocked=raw.blocked_by,blocked_by=blocked&&typeof blocked.delivery_id==='string'&&['running','paused_auth','result_unknown','interrupted'].includes(blocked.state)?{delivery_id:blocked.delivery_id,state:blocked.state}:null;
    return {id,state,state_label:stateLabels[state]||'结果需要核对',platform:chosenPlatform,platform_label:platforms[chosenPlatform]||'发布平台',account_id,account_label:accountLabel,worker_running:raw.worker_running===true,...(typeof raw.execution_active==='boolean'?{execution_active:raw.execution_active}:{}),...(blocked_by?{blocked_by}:{}),skipped:raw.posting_limited===true,reason:clean(raw.reason_message),events,lastStage,title:clean(raw.effective_fields?.title)||clean(raw.title),needs_verification:raw.needs_verification===true||state==='paused_auth'||raw.error_code==='verification_required'&&['result_unknown','interrupted'].includes(state),...(integer(raw.index)!==null?{index:raw.index}:{})};
  }
  function ordinaryData(detail,live){
   if(!detail||detail.task?.id!==taskId||!Array.isArray(detail.items))return null;const seen=new Set();let integrity=true;const platform=clean(detail.settings?.platform)||'douyin',accountLabel=clean(detail.task.account)||'已选账号',targets=[];
   for(const raw of detail.items){if(!raw||typeof raw!=='object'||integer(raw.index)===null||seen.has(raw.index)||!clean(raw.account_id)){integrity=false;continue;}seen.add(raw.index);targets.push(target(raw,'item:'+raw.index,platform,accountLabel));}
   return {targets,total:integer(detail.task.quantity),integrity,state:detail.task.state,createdAt:detail.task.created_at,live:live?.id===taskId?live:null};
  }
  function multiData(detail){const seen=new Set(),targets=[];let integrity=true;for(const raw of detail.deliveries){const id=raw?.id||raw?.delivery_id;if(!raw||typeof raw!=='object'||typeof id!=='string'||!id||seen.has(id)||!clean(raw.account_id)||!Object.hasOwn(platforms,raw.platform)){integrity=false;continue;}seen.add(id);targets.push(target(raw,id,'','已选账号'));}return {targets,total:integer(detail.quantity),integrity,state:detail.state,createdAt:detail.created_at,live:null};}
  function readError(){shared.error('进度暂时无法读取，保留上次结果，稍后自动重试。');syncHost();}
  function schedule(){stop();if(alive&&mode==='ordinary'&&!complete&&visible())timer=setTimeout(()=>{timer=null;return poll();},1200);}
  async function poll(){if(!alive||mode!=='ordinary'||inFlight||!visible())return;const version=epoch,id=taskId;inFlight=true;try{const [detail,live]=await Promise.allSettled([api('task.center.detail',{id}),api('publish.progress.status',{})]);if(!alive||epoch!==version||mode!=='ordinary')return;const data=detail.status==='fulfilled'?ordinaryData(detail.value,live.status==='fulfilled'?live.value?.task:null):null;if(data)render(data);else readError();}catch{if(alive&&epoch===version)readError();}finally{if(alive&&epoch===version){inFlight=false;schedule();}}}
  function onVisibility(){if(!alive||mode!=='ordinary')return;stop();if(visible())return poll();}
  if(typeof document!=='undefined')document.addEventListener?.('visibilitychange',onVisibility);
  function idle(){if(alive)reset('idle',null);}
  idle();return {
   ordinary(id){if(!bind('ordinary',id))return Promise.resolve();if(!known){shared.render({batch:{id},status:{phase:'waiting',execution:{current:{label:'正在读取本批进度'},events:[],publishing:{targets:[],complete:false,omitted:0}}},canContinue:false,canRepair:false});syncHost();}stop();return poll();},
   multi(detail){if(!detail||!Array.isArray(detail.deliveries)||taskId&&detail.id!==taskId||!bind('multi',detail.id))return;render(multiData(detail));},
   pending(id,message){if(!bind('pending',id))return;shared.render({batch:{id},status:{phase:'waiting',execution:{current:{label:clean(message)||'正在读取本批进度'},events:[],publishing:{targets:[],complete:false,omitted:0}}},canContinue:false,canRepair:false});syncHost();},
   idle,
   dispose(){if(!alive)return;alive=false;stop();epoch++;taskId=null;shared.dispose();if(typeof document!=='undefined')document.removeEventListener?.('visibilitychange',onVisibility);}
  };
 }
 return {create};
})();
