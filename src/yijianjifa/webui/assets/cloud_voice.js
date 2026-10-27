/* Cloud account controls. Secrets live in the native host, never browser storage. */
const P011CloudVoice=(()=>{
 const format=n=>Number(n||0).toLocaleString('en-US');
 function words(text){return Array.from(String(text||'')).length;}

 function mount({$,api,el,button,modal,hideModal,notice,changed,isBusy=()=>false,selectedVoice=()=>null,useSystem,selectionUpdated=()=>{}}){
  let alive=true,state=null,busy=false;
  function input(label,type='text'){
   const field=el('input');field.type=type;field.setAttribute('aria-label',label);field.autocomplete=type==='password'?'current-password':'username';
   const wrap=el('label',label);wrap.append(field);return {field,wrap};
  }
  function controls(){
   if(!alive)return;
   if($('freeUsageNotice'))$('freeUsageNotice').textContent='软件与系统配音免费。云配音可选，按成功字数收费。';
   if($('cloudPolicyNotice')){
    $('cloudPolicyNotice').hidden=!(state?.authenticated&&state?.policy_notice_required);
    $('cloudPolicyText').textContent=state?.policy_notice||'';
   }
   $('cloudTestNotice').hidden=!state?.local_test;
   $('cloudTestNotice').textContent=state?.local_test?'本地测试：系统声音模拟云配音，不产生真实费用。测试字数和兑换码不适用于正式服务。':'';
   $('cloudStatus').textContent=state?.mode==='own'&&selectedVoice()? '原配音方式已停用，请重新选择':!state?.enabled?'云配音暂未开放，可使用系统配音':!state.authenticated?'云配音 · 登录后可充值字数':state.balance_current===false?'云配音 · 字数暂未更新，请检查网络':`云配音 · 剩余 ${format(state.available_words)} 字${state.reserved_words?`（预留 ${format(state.reserved_words)} 字）`:''}`;
   $('cloudAccount').textContent=state?.authenticated?state.account_label||'配音账号':state?.local_test?'登录测试账号':'登录云配音';
   $('cloudAccount').disabled=busy||!state?.enabled;$('cloudRedeem').disabled=busy||!state?.enabled;
   $('cloudUsage').disabled=busy||!state?.enabled;selectionUpdated();
  }
  async function refresh(){try{const next=await api('voice.cloud.read',{});if(alive){state=next;controls();}return next;}catch(e){if(alive){$('cloudStatus').textContent='云配音状态暂不可用';}throw e;}}
  async function updated(){await refresh();if(alive)await changed();}
  function guarded(fn){return async(...args)=>{if(busy||isBusy())return;busy=true;controls();try{await fn(...args);}catch(e){notice(e.message);}finally{busy=false;controls();}};}
  function auth(register=false){
   if(!state?.enabled){notice('云配音暂未开放，请先使用免费的系统配音。');return;}
   const username=input('配音账号'),password=input('配音密码','password');password.field.autocomplete=register?'new-password':'current-password';
   const form=el('div',undefined,'form-grid');form.append(username.wrap,password.wrap);
   const rule=el('small','6—128位，可使用字母、数字或符号。','muted'),problem=el('small',undefined,'cloud-password-error');
   rule.id='cloud-password-rule';problem.id='cloud-password-problem';problem.setAttribute('role','status');problem.setAttribute('aria-live','polite');
   function validPassword(){const n=words(password.field.value);return n>=6&&n<=128;}
   function validate(){
    if(!register)return;
    const n=words(password.field.value);problem.textContent=!n?'':n<6?`至少6位，还差${6-n}位。`:n>128?'密码最多128位。':'';
    problem.hidden=!problem.textContent;password.field.setAttribute('aria-invalid',String(n>0&&!validPassword()));
    submit.disabled=!validPassword()||!username.field.value.trim();
   }
   const submit=button(register?'注册并登录':'登录',guarded(async()=>{
    if(register&&!validPassword()){validate();return;}
    const value=password.field.value;password.field.value='';
    validate();
    await api(register?'voice.cloud.register':'voice.cloud.login',{username:username.field.value.trim(),password:value});hideModal();await updated();notice('配音账号已登录。');
   }),'btn primary');
   if(register){password.field.minLength=6;password.field.maxLength=256;password.field.setAttribute('aria-describedby','cloud-password-rule cloud-password-problem');password.wrap.append(rule,problem);password.field.oninput=validate;username.field.oninput=validate;validate();}
   modal(register?'注册配音账号':'登录配音账号',[el('p',state?.local_test?'请创建本地测试账号，不要使用真实账号密码。测试字数仅在这台电脑的测试服务中有效。':'软件和系统配音免费，无需登录配音账号。仅使用云配音时需要注册，登录后用兑换码充值。配音字数跟随账号，换电脑也能使用。'),form,el('p','账号使用4—32位字母、数字或下划线。配音音频长期保存在本机；复用和重新下载不扣费，原音频无法恢复时自动重新生成，并按成功字数扣费。字数不足时提示充值。','muted'),el('p',register?'暂不提供密码找回服务，请务必记住账号和密码。登录后，下次打开软件会自动登录。':'登录后，下次打开软件会自动登录；主动退出或修改密码后需重新登录。','muted')],[button('取消',hideModal),button(register?'已有账号，去登录':'还没有账号？注册',()=>auth(!register)),submit]);
  }
  function passwordAction(title,action,{newPassword=false,help='',submitText='确定'}={}){
   const password=input(newPassword?'新配音密码':'配音密码','password');if(newPassword)password.field.autocomplete='new-password';
   modal(title,[...(help?[el('p',help)]:[]),password.wrap],[button('取消',hideModal),button(submitText,guarded(async()=>{const value=password.field.value;password.field.value='';const r=await action(value);if(r?.cancelled)return;hideModal();await updated();notice('操作已完成。');}),'btn primary')]);
  }
  function account(){
   if(!state?.authenticated){auth();return;}
   const overview=el('div',undefined,'cloud-account-overview');
   overview.append(el('span','当前账号','muted'),el('strong',state.account_label||'已登录','cloud-account-name'),el('p',state.balance_current===false?'字数暂未更新，请检查网络。':`剩余 ${format(state.available_words)} 字`,'cloud-account-balance'));
   const settings=el('details',undefined,'cloud-account-settings');settings.open=false;settings.append(el('summary','账号设置'));
   function setting(title,help,action){const row=el('div',undefined,'cloud-account-action'),text=el('div');text.append(el('strong',title),el('p',help,'muted'));row.append(text,action);settings.append(row);}
   setting('账号密码','修改后，所有设备都需要重新登录。',button('修改密码',changePassword));
   setting('在所有设备退出','包括当前电脑。所有设备都需要重新登录，剩余字数会保留。',button('退出所有设备',()=>passwordAction('退出所有设备',password=>api('voice.cloud.logout_all',{password}),{help:'包括当前电脑，所有设备都需要重新登录。账号和剩余字数会保留。请输入密码确认。',submitText:'确认退出所有设备'}),'btn danger'));
   modal('配音账号',[overview,el('p',state.policy_notice||'本地音频长期保留；复用和重新下载免费，无法恢复时自动重新生成并按成功字数扣费。','muted'),el('p',state.local_test?'这是本地测试账号，测试字数仅在这台电脑的测试服务中有效。':'换电脑时，用这个账号登录即可继续使用剩余字数。','muted'),settings],[
    button('退出登录',guarded(async()=>{await api('voice.cloud.logout',{});hideModal();await updated();notice('已在这台电脑退出登录，剩余字数保留。');})),
    button('完成',hideModal,'btn primary')
   ]);
  }
  function changePassword(){
   const old=input('原配音密码','password'),next=input('新配音密码','password');next.field.autocomplete='new-password';
   next.wrap.append(el('small','6—128位，可使用字母、数字或符号。','muted'));
   const form=el('div',undefined,'form-grid');form.append(old.wrap,next.wrap);
   modal('修改配音密码',[el('p','修改后，所有设备都需要用新密码重新登录。'),form],[button('取消',hideModal),button('保存新密码',guarded(async()=>{
    const oldValue=old.field.value,newValue=next.field.value;old.field.value=next.field.value='';
    await api('voice.cloud.password.change',{old:oldValue,new:newValue});hideModal();await updated();notice('密码已修改，请重新登录。');
   }),'btn primary')]);
  }
  function redeem(){
   if(!state?.authenticated){auth();return;}
   const code=input('兑换码');code.wrap.className='cloud-redeem-field';code.field.autocomplete='off';code.field.placeholder=state?.local_test?'粘贴本地测试兑换码':'粘贴购买后收到的兑换码';
   const purchaseHelp=state?.local_test?[]:[
    el('p','① 联系购买：微信 sj1337622，或淘宝店铺「逆浪风」。'),
    el('p','② 付款后领码：联系原购买渠道，提供淘宝订单号或转账凭证，核实收款后人工领取兑换码。付款不会自动增加软件字数。'),
    el('p','③ 登录要充值的配音账号，在下方粘贴兑换码并点击“兑换”。每码只能兑换一次；未收到码或兑换失败，请联系上述购买渠道。')
   ];
   modal(state?.local_test?'测试兑换码充值':'兑换码充值',[el('p',state?.local_test?'本地测试：每码增加 1 万测试字，无需付款，不产生正式额度。':'19.9 元／1 万字，人工付款后领取兑换码。'),...purchaseHelp,code.wrap,el('p','充值后点击“继续本批”可继续暂停的制作，已完成内容保留。','muted')],[button('取消',hideModal),button('兑换',guarded(async()=>{
    const value=code.field.value.trim();if(!value)throw Error('请填写兑换码。');const result=await api('voice.cloud.redeem',{code:value});code.field.value='';hideModal();await updated();
    notice((result.already_redeemed||result.replayed)?'此兑换已入账，未重复增加字数。':`已增加 ${format(result.credited_words||result.added_words||10000)} 字。请保管好账号和密码，暂不提供密码找回服务。`);
   }),'btn primary')]);
  }
  async function usage(page=0){
   if(!state?.authenticated){auth();return;}
   const data=await api('voice.cloud.usage',{page});if(!alive)return;
   const list=el('div',undefined,'cloud-usage');
   for(const item of data.items||[]){const time=typeof item.created_at==='number'?new Date(item.created_at*1000):new Date(item.created_at||item.time);
    const delta=Number(item.delta??item.words??0);const row=el('p');row.append(el('strong',`${delta>0?'+':''}${format(delta)} 字 `),el('span',`${item.kind==='redemption'?'兑换充值':(item.purpose==='audition'||String(item.purpose||'').includes('试听'))?'文字试听':'配音生成'} · ${Number.isNaN(time.getTime())?'时间未记录':time.toLocaleString('zh-CN',{hour12:false})}${Number.isInteger(item.available_after)?` · 余 ${format(item.available_after)} 字`:''}`));list.append(row);}
   if(!list.children.length)list.append(el('p','暂无用量记录。'));
   const previous=button('上一页',()=>usage(page-1)),next=button('下一页',()=>usage(page+1));previous.disabled=page===0;next.disabled=Number.isInteger(data.total)?(page+1)*(data.page_size||30)>=data.total:data.has_more===false||(data.items||[]).length<(data.page_size||30);
   modal('配音字数记录',[el('p',`剩余 ${format(state.available_words)} 字`),list],[previous,next,button('关闭',hideModal)]);
  }
  function canChoose(voice){
   return Boolean(state?.enabled&&!busy&&!isBusy()&&(state.mode==='own'||!state.authenticated||voice?.available!==false));
  }
  const chooseVoice=guarded(async select=>{
   if(!state?.enabled){notice('云配音暂未开放，请先使用系统配音。');return;}
   if(!state.authenticated){auth();return;}
   if(state.mode!=='hosted'){
    await useSystem();
    await api('voice.cloud.mode.save',{mode:'hosted'});await updated();
   }
   const current=await refresh();
   if(!current?.enabled){notice('云配音暂未开放，请先使用系统配音。');return;}
   if(!current.authenticated){notice('请先登录配音账号，再设置默认配音。');auth();return;}
   if(current.balance_current!==true||!Number.isInteger(current.available_words)||current.available_words<0){notice('配音字数暂未更新，请检查网络后重试。默认配音未更改。');return;}
   if(current.available_words===0){notice('配音字数不足，请充值后再设为默认配音。');return;}
   await select();
  });
  if($('cloudPolicySeen'))$('cloudPolicySeen').onclick=guarded(async()=>{await api('voice.cloud.policy.seen',{});await refresh();});
  $('cloudAccount').onclick=account;$('cloudRedeem').onclick=redeem;$('cloudUsage').onclick=()=>usage().catch(e=>notice(e.message));
  refresh().catch(()=>{});
  return {refresh,canChoose,chooseVoice,isBusy:()=>busy,selectionChanged:controls,mode:()=>state?.mode,refreshAfterGeneration:()=>refresh().catch(()=>{}),dispose(){alive=false;},auth};
 }
 return {mount,words};
})();
if(typeof module!=='undefined')module.exports=P011CloudVoice;
