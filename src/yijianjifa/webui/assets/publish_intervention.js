function P011PublishIntervention(){
 let alive=true,timer,current=null,root=null,sending=false,consent=null,feedback=null,latestViewRevision=0,openedDelivery=null,visibilityEpoch=0;
 const deferredRequests=new Set();
 const el=(tag,text)=>{const n=document.createElement(tag);if(text)n.textContent=text;return n;};
 async function api(operation,params){const request_id=[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');const r=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id,operation,params})));if(!r.ok)throw Error(r.error.message);return r.data;}
 const sameHandle=(a,b)=>!!a&&!!b&&a.id===b.id&&a.delivery_id===b.delivery_id&&a.inspection_id===b.inspection_id;
 const reasons=new Set(['identity_unavailable','identity_mismatch','session_changed','login_required','sms_required','challenge_required']);
 const reasonOf=r=>reasons.has(r?.reason)?r.reason:({login:'login_required',sms:'sms_required',complex:'challenge_required'}[r?.kind]||'');
 const accountCheck=r=>['identity_unavailable','identity_mismatch','session_changed'].includes(reasonOf(r));
 const platformName=r=>({bilibili:'B站',channels:'视频号',douyin:'抖音',kuaishou:'快手'}[r.platform]||'平台');
 const browserLogin=r=>r?.browser_login===true&&!r.inspection_id&&!r.final_started;
 const sameRequest=(a,b)=>sameHandle(a,b)&&a.kind===b.kind&&reasonOf(a)===reasonOf(b)&&!!a.final_started===!!b.final_started&&browserLogin(a)===browserLogin(b);
 const simpleSms=r=>!!r.delivery_id&&!r.inspection_id&&r.kind==='sms'&&!accountCheck(r);
 function doneLabel(r){if(browserLogin(r))return '已登录，继续';if(r.inspection_id||!r.delivery_id)return '已完成验证';if(simpleSms(r))return '检查验证状态';if(accountCheck(r)&&r.final_started)return '检查发布结果';return {identity_unavailable:'重新核对原账号',identity_mismatch:'重新核对原账号',session_changed:'确认原账号并继续',login_required:'已登录，继续',sms_required:'检查验证状态',challenge_required:'验证完成'}[reasonOf(r)]||'检查发布结果';}
 function titleText(r){if(r.inspection_id)return '仅检查，不发表 · '+r.account_name;if(!r.delivery_id)return '发布需要验证 · '+r.account_name;const label=simpleSms(r)?'短信验证':reasonOf(r)==='identity_mismatch'?'所选账号不一致':({identity_unavailable:'原账号待核对',session_changed:'账号需确认',login_required:'需要登录',sms_required:'需要短信验证码',challenge_required:'需要平台验证'}[reasonOf(r)]||'发布需要处理');return platformName(r)+label+' · '+r.account_name;}
 const backgroundVerification=r=>!r.inspection_id&&r.external_browser===false&&!accountCheck(r)&&['sms','complex','login'].includes(r.kind);
 function messageText(r){
  if(browserLogin(r))return '请在打开的浏览器中登录原'+platformName(r)+'账号“'+r.account_name+'”，完成后点击“已登录，继续”。';
  if(simpleSms(r))return '输入短信验证码，点击“确认验证码”。验证通过后，软件会自动继续。';
  if(backgroundVerification(r)&&r.view_mode!=='page'&&!r.delivery_id)return '发布已暂停，请切换到显示浏览器模式，完成'+(r.kind==='login'?'登录':'验证码验证')+'后继续。浏览器原页面会在客户端中显示，已上传视频和填写内容保留。';
  if(backgroundVerification(r)&&r.view_mode!=='page')return '请按页面提示完成'+(r.kind==='login'?'登录':'验证')+'，然后点击“'+doneLabel(r)+'”。如看不到页面，可在“验证遇到问题？”中打开。';
  if(!r.delivery_id||r.inspection_id||typeof r.message==='string'&&r.message.trim())return r.message;
  const name=platformName(r),account='“'+r.account_name+'”';
  if(reasonOf(r)==='identity_unavailable')return '程序尚未确认当前页面属于所选'+name+'账号'+account+'。'+(r.final_started?'请重新核对原账号后读取原提交结果。':'发布暂停在账号核对，请点击“重新核对原账号”继续检查。');
  if(reasonOf(r)==='identity_mismatch')return '当前页面账号与这条原任务所选'+name+'账号'+account+'不一致。请核对所选原账号。';
  if(reasonOf(r)==='session_changed')return '所选'+name+'账号的登录资料已变化。请确认仍使用'+account+'原账号，再点击“'+doneLabel(r)+'”。';
  if(reasonOf(r)==='login_required')return '原任务的'+name+'页面需要重新登录。请按下方页面提示登录'+account+'原账号。';
  return r.message;
 }
 function acceptedText(r,action){
  if(action==='cancel')return r.final_started?'已收到停止请求；客户端将停止本地核对，原提交结果请到任务记录查看。':'已收到取消请求；客户端将停止此本地目标。';
  if(action==='code')return '客户端已收到验证码处理请求；将交给原平台页面检查。';
  if(action==='resend_code')return '再次获取请求已排队，正在交给原平台页面。';
  if(action==='done'){
   if(r.final_started&&!r.inspection_id)return '已收到核对请求；客户端将读取原提交结果，不会再次发布。';
   if(accountCheck(r))return '已收到重新核对请求；客户端将检查原账号，通过后继续这条原任务。';
   if(reasonOf(r)==='login_required')return '已收到继续请求；客户端将检查原账号登录状态，通过后继续原任务。';
   return '已收到继续请求；客户端将检查验证是否完成，通过后继续'+(r.inspection_id?'原检查。':'原任务。');
  }
  return '客户端已收到操作请求，等待继续处理。';
 }
 const viewRevision=r=>Number.isSafeInteger(r?.view_revision)&&r.view_revision>0?r.view_revision:0;
 const viewActions=new Set(['visible','refresh','scroll','pointer','resend_code']);
 function close(resetOpened=true){root?.remove();root=null;current=null;consent=null;feedback=null;latestViewRevision=0;if(resetOpened)openedDelivery=null;}
 async function defer(){
  if(sending||!current)return;const request=current;sending=true;paintFeedback();
  try{
   const result=await api('multi_publish.intervention.defer',{id:request.id,delivery_id:request.delivery_id});
   if(result?.deferred!==true||result.delivery_id!==request.delivery_id)throw Error('暂未确认收起，请重试。');
   deferredRequests.add(request.id);visibilityEpoch++;
   if(sameRequest(current,request))close();
  }catch(error){if(sameRequest(current,request))setFeedback('未能收起：'+error.message,'error');}
  finally{sending=false;paintFeedback();}
 }
 function paintFeedback(){
  if(!root)return;const node=root.querySelector('[data-feedback]');if(node){node.textContent=feedback?.text||'';node.hidden=!feedback?.text;node.className='p011-verification-feedback'+(feedback?.tone==='error'?' is-error':'');}
  const remaining=Math.max(0,Number(current?.sms_retry_after)||0);
  for(const b of root.querySelectorAll('button')){if(b.dataset.action==='resend_code')b.textContent=remaining?'再次获取（'+remaining+'秒）':'再次获取验证码';b.disabled=b.dataset.action==='resend_code'&&remaining>0||sending||!!feedback?.pending&&(viewActions.has(b.dataset.action)||b.dataset.action==='code'||feedback.action==='code'&&b.dataset.action==='done');}
 }
 function setFeedback(text,tone='progress',pending=false,action='',revision=null){feedback={text,tone,pending,action,revision};paintFeedback();}
 function viewFeedback(r){
  const revision=Number.isSafeInteger(r.view_revision)&&r.view_revision>0?r.view_revision:null;
  // Account management can explicitly show this same browser from another
  // control. Its newer accepted revision replaces even an older queued view;
  // a local action without its response revision still waits for that reply.
  if(browserLogin(r)&&r.view_action==='visible'&&feedback&&Number.isSafeInteger(feedback.revision)&&revision>feedback.revision)feedback=null;
  if(r.view_action==='resend_code'&&r.view_state!=='failed'&&(!feedback||feedback.action==='resend_code'&&feedback.revision===revision)&&['queued','sent','failed','waiting'].includes(r.sms_state)){setFeedback(r.sms_message||'正在获取短信验证码。',r.sms_state==='failed'?'error':'progress',r.sms_state==='queued','resend_code',revision);return;}
  if((feedback?.action==='code'&&revision===feedback.revision||!feedback&&r.view_action==='code'&&revision!==null)&&['queued','processing','submitted','failed'].includes(r.code_state)){
   const messages={queued:'验证码请求已排队，正在交给原平台页面。',processing:'正在填写原平台验证码并提交验证。',submitted:'验证码已填入并提交原平台，正在等待验证结果。',failed:'验证码未通过或未能提交，请查看原页面提示后重新输入或获取。'};
   setFeedback(typeof r.code_message==='string'?r.code_message:messages[r.code_state],r.code_state==='failed'?'error':'progress',['queued','processing'].includes(r.code_state),'code',revision);return;
  }
  // A previous poll may finish after a new action starts. Only the accepted
  // action's revision can finish its feedback; before acceptance keep waiting.
  if(feedback&&(!feedback.pending||feedback.revision===null||revision!==feedback.revision))return;
  if(revision===null)return;
  const action=feedback?.action||'visible';
  if(r.view_state==='failed'){setFeedback((browserLogin(r)?'登录页面未能打开：':'画面处理失败：')+(r.view_error||(browserLogin(r)?'请点击“打开登录页面”重试。':'无法读取原会话画面，请再次展开。')),'error',false,action,revision);return;}
  if(r.view_state==='queued'){if(!feedback)setFeedback(browserLogin(r)?'正在打开登录页面…':'正在处理原会话画面…','progress',true,action,revision);return;}
  if(browserLogin(r)&&r.view_state==='ready'&&r.view_mode==='browser'&&r.external_browser===true){setFeedback('登录页面已打开，请在浏览器中完成原账号登录。','success',false,action,revision);return;}
  if(r.view_state==='ready'&&r.view_mode==='page'&&r.image){const message={refresh:'原任务画面已更新。',scroll:'原任务画面已滚动。',pointer:'画面操作已完成，请核对平台提示。'}[action]||'原任务页面已显示，可查看下方画面。';setFeedback(message,'success',false,action,revision);}
 }
 async function respond(action,code=''){
  if(sending||!current)return false;
  const inspection=!!current.inspection_id,v2=!!current.delivery_id||inspection,nativeView=v2||browserLogin(current),finalStarted=!inspection&&current.final_started;
  const needsConsent=v2&&!finalStarted&&['code','pointer','done'].includes(action);
  if(needsConsent&&!consent?.checked){setFeedback(inspection?'请确认仍使用此账号继续检查；本次不会发表。':'请勾选确认仍使用此目标原账号继续。','error');return false;}
  if(nativeView&&(viewActions.has(action)||action==='code')&&feedback?.pending)return false;
  if(action==='resend_code'&&Number(current.sms_retry_after)>0)return false;
  const request=current,params=v2?{id:current.id,...(inspection?{inspection_id:current.inspection_id}:{delivery_id:current.delivery_id}),action,...(['code','pointer','scroll'].includes(action)?{code}:{}),...(needsConsent?{consent_same_identity:true}:{})}:{id:current.id,action,code};
  sending=true;
  const message=browserLogin(current)&&action==='visible'?'正在打开登录页面…':v2?{visible:'正在读取原任务页面…',refresh:'正在更新原任务画面…',scroll:'正在滚动原任务画面…',pointer:'正在处理画面操作…',resend_code:'正在请求新的短信验证码…',code:'正在发送验证码处理请求…',done:finalStarted?'正在请求核对原提交结果…':accountCheck(current)?'正在请求重新核对原账号…':'正在请求检查原账号验证状态…',cancel:'正在请求取消本地目标…'}[action]:'正在处理，请稍候…';setFeedback(message||'正在处理，请稍候…','progress',nativeView&&(viewActions.has(action)||action==='code'),action);
  try{const data=await api(inspection?'multi_publish.inspection.respond':(v2?'multi_publish.':'publish.')+'intervention.respond',params);if(!sameRequest(current,request))return false;if(data?.accepted!==true)throw Error(data?.message||'操作未受理，请重试。');if(nativeView&&(viewActions.has(action)||action==='code'&&viewRevision(data))){if(!viewRevision(data))throw Error('画面操作未返回核对标识，请再次查看。');feedback.revision=data.view_revision;latestViewRevision=Math.max(latestViewRevision,data.view_revision);if(action==='code')setFeedback(acceptedText(request,action),'progress',true,'code',data.view_revision);}else setFeedback(acceptedText(request,action));if(data.request&&sameRequest(data.request,request))show(data.request);else if(nativeView&&(viewActions.has(action)||action==='code'))viewFeedback(current);return true;}
  catch(e){if(sameRequest(current,request))setFeedback('操作未完成：'+e.message,'error');return false;}
  finally{sending=false;paintFeedback();}
 }
 function updateView(r){
  const pageMode=r.view_mode==='page',img=root.querySelector('img'),frame=root.querySelector('[data-frame]');
  const assistance=root.querySelector('.p011-verification-assistance');
  root.querySelector('.p011-verification-dialog').className='p011-verification-dialog'+(simpleSms(r)?' p011-verification-sms':'')+(pageMode&&(!simpleSms(r)||!assistance?.hidden)?' p011-verification-page':'');
  if(img){img.hidden=backgroundVerification(r)&&!pageMode||!(pageMode||r.kind==='complex'||r.kind==='login');frame.hidden=img.hidden;if(r.image){const src='data:image/png;base64,'+r.image;if(img.src!==src)img.src=src;}}
  const viewer=root.querySelector('[data-viewer]');if(viewer)viewer.hidden=!pageMode;
  const controls=root.querySelector('[data-frame-controls]');
  if(controls){controls.hidden=!!img?.hidden;for(const button of controls.querySelectorAll('[data-scroll]'))button.hidden=!pageMode;}
 }
 function show(r){
  const revision=viewRevision(r),previousRevision=sameHandle(current,r)?latestViewRevision:0;
  if(sameHandle(current,r)&&revision<latestViewRevision)return;
  if(sameRequest(current,r)){latestViewRevision=Math.max(latestViewRevision,revision);current=r;root.querySelector('[data-message]').textContent=messageText(r);updateView(r);viewFeedback(r);paintFeedback();return;}
  const inspection=!!r.inspection_id,v2=!!r.delivery_id||inspection,finalStarted=!inspection&&r.final_started,simple=v2&&!inspection;
  close(false);current=r;latestViewRevision=Math.max(previousRevision,revision);root=el('div');root.className='p011-verification-overlay';root.setAttribute('role','dialog');root.setAttribute('aria-modal','true');root.setAttribute('aria-label',inspection?'检查需要验证':v2?titleText(r):'发布需要验证');
  const box=el('div'),title=el('h3',titleText(r)),message=el('p',messageText(r));box.className='p011-verification-dialog';message.dataset.message='true';box.append(title,message);
  const operationStatus=el('p');operationStatus.dataset.feedback='true';operationStatus.hidden=true;operationStatus.setAttribute('role','status');operationStatus.setAttribute('aria-live','polite');box.append(operationStatus);
  if(inspection)box.append(el('p','完成验证后仅继续上传和资料检查，停在正式发表前；不会发表或定时发表，也不主动保存草稿。'));
  if(v2&&finalStarted)box.append(el('p','这条视频已经尝试提交。验证后会检查它的发布结果，避免重复发布。'));
  if(v2&&!finalStarted){
   consent=el('input');consent.type='checkbox';consent.setAttribute('aria-label',inspection?'确认同一账号继续检查':'确认同一账号继续此目标');
   const label=el('label');label.append(consent,el('span',inspection?'我确认仍使用 '+r.account_name+' 原账号继续检查，不切换账号。':'我确认继续使用'+platformName(r)+'账号“'+r.account_name+'”处理这条原任务。'));box.append(label);
  }
  if(v2&&!inspection&&reasonOf(r)==='identity_mismatch')box.append(el('p',browserLogin(r)?'请登录本次选择的'+platformName(r)+'账号；登录其他账号不能继续这条任务。':'账号身份（UID）必须与这条原任务所选账号一致；'+(finalStarted?'核对操作':'勾选确认')+'不会授权使用其他账号。'));
  const assistance=simple&&!browserLogin(r)?el('div'):null;
  // SMS code processing already advances the original session on success.
  // Keep manual page/check tools optional; cancellation belongs in task records.
  if(assistance){
   assistance.className='p011-verification-assistance';assistance.hidden=true;assistance.id='p011-verification-assistance';
   assistance.append(el('p',simpleSms(r)?'收不到短信时，请等待倒计时结束后再次获取。也可以打开平台页面完成验证，再检查验证状态。':'看不到验证页面时，请点击下方按钮打开。'));
   const tools=el('div');tools.className='p011-verification-help-actions';
   for(const [action,label] of [['visible','打开验证页面'],...(simpleSms(r)?[['done',doneLabel(r)]]:[])]){const b=el('button',label);b.dataset.action=action;b.onclick=e=>{if(e.isTrusted)respond(action);};tools.append(b);}
   assistance.append(tools,el('p','需要停止这条任务，可到“任务记录”中删除。'));
  }
  let codeConfirm=null;
  if(r.kind==='sms'){
   const label=el('label','请输入平台发送的验证码'),input=el('input');input.autocomplete='one-time-code';input.maxLength=16;input.setAttribute('aria-label','验证码');
   const submitCode=()=>{const value=input.value;respond('code',value).then(accepted=>{if(accepted&&root?.contains(input)&&input.value===value)input.value='';});};
   const confirm=el('button','确认验证码');confirm.className='primary';confirm.dataset.action='code';confirm.onclick=e=>{if(e.isTrusted)submitCode();};input.onkeydown=e=>{if(e.isTrusted&&e.key==='Enter'){e.preventDefault();submitCode();}};const smsActions=el('div');smsActions.className='p011-verification-sms-actions';
   if(simpleSms(r)){codeConfirm=confirm;input.id='p011-verification-code';input.inputMode='numeric';input.placeholder='输入短信验证码';label.setAttribute('for',input.id);smsActions.className+=' p011-verification-code-row';smsActions.append(input);box.append(label,smsActions);}
   else{label.append(input);box.append(label);smsActions.append(confirm);box.append(smsActions);}
   if(v2&&r.platform==='douyin'&&!inspection){const resend=el('button','再次获取验证码');resend.dataset.action='resend_code';resend.onclick=e=>{if(e.isTrusted)respond('resend_code');};smsActions.append(resend);}
  }
  if(!browserLogin(r)&&(r.kind==='complex'||r.kind==='login'||v2)){
   if(r.kind==='login'&&!accountCheck(r)){
    const done=doneLabel(r);
    box.append(el('p',v2&&r.platform==='channels'?'请在下方页面完成此视频号登录。若出现“微信快捷登录”，先点击它，再按微信提示完成登录；完成后点击“'+done+'”。':v2?'请按下方'+platformName(r)+'页面提示登录原账号；如页面显示二维码，再用手机扫码。完成后点击“'+done+'”。':'请用手机扫描下方平台二维码，登录 '+r.account_name+' 原账号；完成后点击“'+done+'”。'));
   }else if(r.kind==='complex')box.append(el('p','请在下方平台画面完成验证，可点击或拖动。'));
   const frame=el('div'),img=el('img');frame.className='p011-verification-frame';frame.dataset.frame='true';img.alt=accountCheck(r)?'原任务当前账号页面':r.kind==='login'?'当前平台登录页面':'当前平台验证页面';img.draggable=false;
   if(r.image)img.src='data:image/png;base64,'+r.image;
   let down=null;
   const pointerAllowed=()=>!!current&&!accountCheck(current)&&(current.kind==='complex'||current.kind==='login'||(v2&&current.view_mode==='page'))&&!img.hidden;
   const point=e=>{
    const rect=img.getBoundingClientRect();
    if(!(rect.width>0&&rect.height>0&&img.naturalWidth>0&&img.naturalHeight>0))return null;
    return [Math.max(0,Math.min(img.naturalWidth,Math.round((e.clientX-rect.left)*img.naturalWidth/rect.width))),Math.max(0,Math.min(img.naturalHeight,Math.round((e.clientY-rect.top)*img.naturalHeight/rect.height)))];
   };
   if(v2||r.kind==='complex'||r.kind==='login'){
    img.onpointerdown=e=>{if(e.isTrusted&&pointerAllowed()){down=point(e);if(down){img.setPointerCapture?.(e.pointerId);e.preventDefault();}}};
    img.onpointerup=e=>{if(e.isTrusted&&down&&pointerAllowed()){const end=point(e),start=down;down=null;if(end)respond('pointer',current.kind==='login'&&current.view_mode!=='page'||Math.hypot(end[0]-start[0],end[1]-start[1])<5?end:[...start,...end]);}};
    img.onpointercancel=e=>{if(e.isTrusted)down=null;};
   }
   const pageContainer=simpleSms(r)?assistance:box;
   frame.append(img);pageContainer.append(frame);
   if(v2){
    const viewer=el('p',inspection?'当前显示的是原检查会话。请只完成登录或验证码操作；完成后点击“已完成验证”继续检查。':accountCheck(r)?'请核对页面中的账号，再点击“'+doneLabel(r)+'”。':'请只在登录或验证码区域操作；完成后点击“'+doneLabel(r)+'”。');viewer.className='p011-verification-hint';viewer.dataset.viewer='true';pageContainer.append(viewer);
    const controls=el('div');controls.className='p011-verification-frame-controls';controls.dataset.frameControls='true';
    for(const [action,label,code] of [['scroll','向上滚动',[0,-450]],['scroll','向下滚动',[0,450]],['refresh','更新画面',undefined]]){
     const b=el('button',label);b.dataset.action=action;if(action==='scroll')b.dataset.scroll='true';b.onclick=e=>{if(e.isTrusted)respond(action,code);};controls.append(b);
    }
    pageContainer.append(controls);
   }
  }
  const foot=el('div');foot.className='p011-verification-actions';
  if(simple){
   if(assistance){box.append(assistance);
    const help=el('button','验证遇到问题？');help.className='p011-verification-help-toggle';help.setAttribute('aria-expanded','false');help.setAttribute('aria-controls',assistance.id);help.onclick=e=>{if(!e.isTrusted)return;assistance.hidden=!assistance.hidden;help.textContent=assistance.hidden?'验证遇到问题？':'收起帮助';help.setAttribute('aria-expanded',String(!assistance.hidden));updateView(current);};foot.append(help);
   }else if(browserLogin(r)){const open=el('button','打开登录页面');open.dataset.action='visible';open.onclick=e=>{if(e.isTrusted)respond('visible');};foot.append(open);}
   const later=el('button','关闭');later.title='任务保留在“任务记录”中，可稍后继续处理';later.dataset.action='defer';later.onclick=e=>{if(e.isTrusted)defer();};foot.append(later);
   if(codeConfirm)foot.append(codeConfirm);
   const hint=el('p','关闭后可从“任务记录”继续处理。');hint.className='p011-verification-hint';box.append(hint);
  }
  const actions=inspection?[['visible','在客户端展开页面'],['cancel','取消检查'],['done','已完成验证']]:v2?(codeConfirm?[]:[['done',doneLabel(r)]]):browserLogin(r)?[['visible','打开登录页面'],['cancel','稍后处理'],['done',doneLabel(r)]]:[['cancel','稍后处理'],['visible',backgroundVerification(r)?'显示浏览器验证':'显示浏览器处理'],['done','已完成验证']];
  for(const [action,label] of actions){const b=el('button',label);b.dataset.action=action;if(action==='done')b.className='primary';b.onclick=e=>{if(e.isTrusted)respond(action);};foot.append(b);}
  box.append(foot);root.append(box);document.body.append(root);updateView(r);viewFeedback(r);paintFeedback();
 }
 // A visible native Channels browser already provides the real verification
 // surface. Post-submit checking stays in task details instead of mirroring it.
 const externalResult=r=>r?.platform==='channels'&&r.final_started===true&&r.external_browser===true&&r.kind==='complex';
 async function checkResult(delivery){
  const data=await api('multi_publish.intervention.status',{delivery_id:delivery}),r=data.request;
  if(!r||r.platform!=='channels'||r.delivery_id!==delivery||r.final_started!==true)return false;
  const result=await api('multi_publish.intervention.respond',{id:r.id,delivery_id:delivery,action:'done'});
  if(result?.accepted!==true)throw Error(result?.message||'操作未受理，请重试。');
  return true;
 }
 window.p011CheckPublishResult=checkResult;
 async function openVerification(delivery){
  const legacy=delivery&&typeof delivery==='object'&&Object.keys(delivery).sort().join(',')==='account_id,request_id'&&typeof delivery.request_id==='string'&&/^[A-Za-z0-9_-]{1,96}$/.test(delivery.request_id)&&typeof delivery.account_id==='string'&&/^[A-Za-z0-9_-]{1,64}$/.test(delivery.account_id);
  if(!alive||sending||!legacy&&(typeof delivery!=='string'||!delivery))return false;const epoch=++visibilityEpoch;
  const data=await api(legacy?'publish.intervention.status':'multi_publish.intervention.status',legacy?{}:{delivery_id:delivery}),r=data.request;
  if(!alive||epoch!==visibilityEpoch||!r||(legacy?r.id!==delivery.request_id||r.account_id!==delivery.account_id||!!r.delivery_id||!browserLogin(r):r.delivery_id!==delivery))return false;
  deferredRequests.delete(r.id);openedDelivery=legacy?null:delivery;show(r);return true;
 }
 window.p011OpenPublishVerification=openVerification;
 async function tick(){const epoch=visibilityEpoch,delivery=openedDelivery;try{let data=await api('publish.intervention.status',{});try{const v2=await api('multi_publish.intervention.status',delivery?{delivery_id:delivery}:{});if(v2.request||delivery)data=v2;}catch{}if(alive&&epoch===visibilityEpoch){if(data.request&&!deferredRequests.has(data.request.id)&&(!externalResult(data.request)||delivery===data.request.delivery_id))show(data.request);else close();}}catch{}finally{if(alive)timer=setTimeout(tick,1000);}}
 tick();return ()=>{alive=false;visibilityEpoch++;clearTimeout(timer);close();if(window.p011CheckPublishResult===checkResult)delete window.p011CheckPublishResult;if(window.p011OpenPublishVerification===openVerification)delete window.p011OpenPublishVerification;};
}
