const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const account={id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true};
const loginStatus={id:'batch-1',phase:'needs_login',login_accounts:[{id:'a',platform:'douyin',nickname:'主号',reason:'登录已过期'}],login_message:'主号需要登录',can_continue:true,total:10};
function fixture(extra=()=>undefined){return async(op,p,request)=>{const result=await extra(op,p,request);if(result!==undefined)return result;
 if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null};
 if(op==='mix.daily.submit')return {id:'batch-1',mix_task_id:'batch-1',phase:'checking_login',quantity:p.mix.quantity};
 if(op==='mix.daily.status')return {id:'batch-1',phase:'checking_login',login_accounts:[],login_message:'正在检查发布账号',can_continue:false,total:10};
 if(op==='mix.daily.save')return {saved:true};if(op==='mix.submit')return {id:'only-1'};
 if(op==='task.poll')return {epoch:'e',revision:1,tasks:[{id:'only-1',state:'waiting',quantity:10}]};
 if(op==='preset.mix.list')return {items:[],total:0};if(op==='settings.output.read')return {directory:'/tmp/finished'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
};}
const action=(view,text,root='daily-progress')=>view.get(root).querySelectorAll('button').find(node=>node.textContent===text);
const count=(view,op)=>view.calls.filter(call=>call.operation===op).length;

test('valid accounts enter checking before making; duplicate daily starts are blocked while only make still works',async()=>{
 const view=await mountMixing(fixture());await view.click(view.get('quick-generate'));
 assert.equal(view.get('daily-progress').dataset.phase,'checking_login');assert.match(view.get('daily-progress').textContent,/正在检查发布账号/);
 assert.doesNotMatch(view.get('toast').textContent,/已开始本批制作/);assert.equal(view.get('quick-generate').disabled,true);
 assert.equal(view.get('quick-only-generate').disabled,false);await view.click(view.get('quick-generate'));assert.equal(count(view,'mix.daily.submit'),1);
 await view.click(view.get('quick-only-generate'));assert.equal(count(view,'mix.submit'),1);assert.equal(view.get('quick-generate').disabled,true);
 await view.click(view.get('quick-generate'));assert.equal(count(view,'mix.daily.submit'),1);assert.equal(count(view,'account.login.start'),0);view.dispose();
});

// Resuming now requires an explicit selection from the task list.
const mountFromTasks=(handler,incoming,router={})=>mountMixing(handler,incoming,{dailyBatchId:'batch-1',...router});

test('expired local accounts remain selectable, then login and continue the original batch without resubmitting',async()=>{
 let phase='needs_login';const view=await mountFromTasks(fixture((op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[{...account,login_state:'expired'}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false};
  if(op==='mix.daily.status')return phase==='needs_login'?loginStatus:{phase:'making',total:10};
  if(op==='account.login.start'){assert.deepEqual(p,{account_id:'a',platform:'douyin'});return {id:'login-1',state:'completed',account_id:'a'};}
  if(op==='mix.daily.continue'){assert.deepEqual(p,{id:'batch-1'});phase='making';return {id:'batch-1',phase:'checking_login'};}
 }));
 const selected=view.query('[data-account="a"]');assert.equal(selected.disabled,false);await view.change(selected,true);assert.equal(view.get('quick-generate').disabled,false);
 const resume=action(view,'登录并继续');assert.ok(resume);await view.click(resume);await view.flush();
 assert.equal(count(view,'account.login.start'),1);assert.equal(count(view,'mix.daily.continue'),1);assert.equal(count(view,'mix.daily.submit'),0);
 assert.equal(view.get('daily-progress').dataset.phase,'making');assert.equal(view.get('modal-root').classList.contains('hide'),true);view.dispose();
});

test('cancelling login preserves the same batch for retry without making or uploading',async()=>{
 const view=await mountFromTasks(fixture(op=>op==='mix.daily.status'?loginStatus:op==='account.login.start'?{id:'login-1',state:'waiting'}:op==='account.login.cancel'?{state:'cancelled'}:undefined));
 await view.click(action(view,'登录并继续'));assert.equal(view.get('modal-root').classList.contains('hide'),false);
 await view.click(action(view,'取消登录','modal-footer'));await view.loginTick();
 assert.equal(count(view,'account.login.cancel'),1);assert.equal(count(view,'mix.daily.continue'),0);assert.equal(count(view,'mix.daily.submit'),0);
 assert.equal(view.get('daily-progress').dataset.phase,'needs_login');assert.ok(action(view,'登录并继续'));assert.equal(view.get('quick-only-generate').disabled,false);view.dispose();
});

test('failed login can retry the same batch and only a successful attempt continues it',async()=>{
 let attempts=0,phase='needs_login';const view=await mountFromTasks(fixture(op=>{
  if(op==='mix.daily.status')return phase==='needs_login'?loginStatus:{phase:'making',total:10};
  if(op==='account.login.start')return ++attempts===1?{id:'login-1',state:'failed',message:'验证码未完成'}:{id:'login-2',state:'completed',account_id:'a'};
  if(op==='mix.daily.continue'){phase='making';return {id:'batch-1',phase:'checking_login'};}
 }));await view.click(action(view,'登录并继续'));assert.equal(count(view,'mix.daily.continue'),0);
 assert.match(view.get('modal-body').textContent,/验证码未完成/);await view.click(action(view,'登录并继续','modal-footer'));await view.flush();
 assert.equal(count(view,'mix.daily.continue'),1);assert.equal(count(view,'mix.daily.submit'),0);view.dispose();
});

test('returning to the page restores the active checking batch and then its login action',async()=>{
 let phase='checking_login';const view=await mountFromTasks(fixture(op=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:{id:'batch-1',phase:'checking_login'}};
  if(op==='mix.daily.status')return phase==='checking_login'?{id:'batch-1',phase,total:10}:{...loginStatus,id:'batch-1'};
 }));assert.equal(view.get('daily-progress').dataset.phase,'checking_login');assert.equal(view.get('quick-generate').disabled,true);assert.equal(count(view,'mix.daily.submit'),0);
 phase='needs_login';await view.tick();assert.ok(action(view,'登录并继续'));assert.deepEqual(view.calls.filter(c=>c.operation==='mix.daily.status').at(-1).params,{id:'batch-1'});view.dispose();
});

test('leaving during login cancels the login session and a late success cannot continue a batch',async()=>{
 let complete;const status=new Promise(resolve=>{complete=resolve;});const view=await mountFromTasks(fixture(op=>op==='mix.daily.status'?loginStatus:op==='account.login.start'?status:op==='account.login.cancel'?{state:'cancelled'}:undefined));
 await view.click(action(view,'登录并继续'));view.dispose();complete({id:'login-late',state:'completed',account_id:'a'});await view.flush();await view.flush();
 assert.equal(count(view,'account.login.cancel'),1);assert.equal(count(view,'mix.daily.continue'),0);assert.equal(count(view,'mix.daily.submit'),0);
});

test('an active needs-login batch restores its original account list and continues with its own id',async()=>{
 let phase='needs_login';const view=await mountFromTasks(fixture((op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:{...loginStatus,id:'batch-1'}};
  if(op==='mix.daily.status')return phase==='needs_login'?{...loginStatus,id:'batch-1'}:{id:'batch-1',phase:'making'};
  if(op==='account.login.start')return {id:'login-r',state:'completed',account_id:'a'};
  if(op==='mix.daily.continue'){assert.deepEqual(p,{id:'batch-1'});phase='making';return {id:'batch-1',phase:'checking_login'};}
 }));await view.click(action(view,'登录并继续'));await view.flush();assert.equal(count(view,'mix.daily.submit'),0);assert.equal(count(view,'mix.daily.continue'),1);view.dispose();
});

test('a recoverable checking error rechecks the same batch without opening login or creating a new batch',async()=>{
 let phase='needs_attention';const view=await mountFromTasks(fixture((op,p)=>{
  if(op==='mix.daily.status')return {phase,can_continue:phase==='needs_attention',login_accounts:[],login_message:'账号检查未完成，请重试',total:10};
  if(op==='mix.daily.continue'){assert.deepEqual(p,{id:'batch-1'});phase='checking_login';return {id:'batch-1',phase};}
 }));assert.equal(view.get('quick-generate').disabled,false);
 await view.click(action(view,'重新检查并继续'));assert.equal(count(view,'account.login.start'),0);assert.equal(count(view,'mix.daily.continue'),1);assert.equal(count(view,'mix.daily.submit'),0);view.dispose();
});

test('task navigation restores its requested daily batch instead of opening old task details or the latest batch',async()=>{
 const view=await mountFromTasks(fixture((op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:{id:'latest',phase:'making'}};
  if(op==='mix.daily.status')return {...loginStatus,id:p.id};
 }),undefined,{dailyBatchId:'requested'});assert.deepEqual(view.calls.filter(c=>c.operation==='mix.daily.status').at(-1).params,{id:'requested'});assert.ok(action(view,'登录并继续'));
 assert.equal(count(view,'task.detail'),0);assert.equal(view.context.P011Router.dailyBatchId,undefined);view.dispose();
});

test('native login errors remain visible and can retry the original batch',async()=>{
 let tries=0;const view=await mountFromTasks(fixture(op=>{
  if(op==='mix.daily.status')return loginStatus;if(op==='account.login.start'){if(++tries===1)throw Error('登录窗口暂不可用');return {id:'login-2',state:'completed',account_id:'a'};}
  if(op==='mix.daily.continue')return {id:'batch-1',phase:'checking_login'};
 }));await view.click(action(view,'登录并继续'));assert.match(view.get('modal-body').textContent,/登录窗口暂不可用/);
 await view.click(action(view,'登录并继续','modal-footer'));assert.equal(count(view,'mix.daily.continue'),1);assert.equal(count(view,'mix.daily.submit'),0);view.dispose();
});

test('multiple login accounts complete in order and repeated resume clicks open only one flow',async()=>{
 let finish;const first=new Promise(resolve=>{finish=resolve;});const view=await mountFromTasks(fixture((op,p)=>{
  if(op==='mix.daily.status')return {...loginStatus,login_accounts:[...loginStatus.login_accounts,{id:'b',platform:'bilibili',nickname:'B站'}]};
  if(op==='account.login.start')return p.account_id==='a'?first:{id:'login-b',state:'completed',account_id:'b'};
  if(op==='mix.daily.continue')return {id:'batch-1',phase:'checking_login'};
 }));const resume=action(view,'登录并继续');await view.click(resume);await view.click(resume);assert.equal(count(view,'account.login.start'),1);
 finish({id:'login-a',state:'completed',account_id:'a'});await view.flush();await view.flush();assert.deepEqual(view.calls.filter(c=>c.operation==='account.login.start').map(c=>c.params.account_id),['a','b']);assert.equal(count(view,'mix.daily.continue'),1);view.dispose();
});

test('closing the login modal cancels the native session and permits retry without waiting for its late response',async()=>{
 let finish;const pending=new Promise(resolve=>{finish=resolve;});let attempts=0;const view=await mountFromTasks(fixture(op=>{
  if(op==='mix.daily.status')return loginStatus;if(op==='account.login.start')return ++attempts===1?{id:'login-1',state:'waiting'}:{id:'login-2',state:'completed',account_id:'a'};
  if(op==='account.login.status')return pending;if(op==='account.login.cancel')return {state:'cancelled'};
  if(op==='mix.daily.continue')return {id:'batch-1',phase:'checking_login'};
 }));await view.click(action(view,'登录并继续'));const waiting=view.loginTick();await view.flush();
 await view.click(view.query('[data-action="close-modal"]'));assert.equal(view.get('modal-root').classList.contains('hide'),true);await view.click(action(view,'登录并继续'));assert.equal(count(view,'mix.daily.continue'),1);
 finish({id:'login-1',state:'completed',account_id:'a'});await waiting;assert.equal(count(view,'mix.daily.continue'),1);assert.equal(count(view,'account.login.cancel'),1);view.dispose();
});

test('after login succeeds the resume request is shown as checking, without a misleading cancel-login action',async()=>{
 let finish;const pending=new Promise(resolve=>{finish=resolve;});const view=await mountFromTasks(fixture(op=>op==='mix.daily.status'?loginStatus:op==='account.login.start'?{id:'login-1',state:'completed',account_id:'a'}:op==='mix.daily.continue'?pending:undefined));
 await view.click(action(view,'登录并继续'));assert.equal(view.get('daily-progress').dataset.phase,'checking_login');
 assert.equal(action(view,'取消登录','modal-footer'),undefined);assert.equal(view.get('modal-root').classList.contains('hide'),true);
 finish({id:'batch-1',phase:'checking_login'});await view.flush();view.dispose();
});

test('a late login cancellation acknowledgement cannot touch the next page after disposal',async()=>{
 let finish;const pending=new Promise(resolve=>{finish=resolve;});const view=await mountFromTasks(fixture(op=>op==='mix.daily.status'?loginStatus:op==='account.login.start'?{id:'login-1',state:'waiting'}:op==='account.login.cancel'?pending:undefined));
 await view.click(action(view,'登录并继续'));await view.click(view.query('[data-action="close-modal"]'));
 view.dispose();view.document.body.replaceChildren();finish({state:'cancelled'});await view.flush();await view.loginTick();assert.equal(count(view,'mix.daily.continue'),0);
});


test('登录暂停只在右侧显示账号和原批次继续入口，重复轮询不恢复底部重复提示',async()=>{
 let waiting=true;
 const view=await mountFromTasks(fixture(op=>{
  if(op==='mix.daily.status')return waiting?loginStatus:{id:'batch-1',phase:'making',total:10};
  if(op==='account.login.start')return {id:'login-p',state:'completed',account_id:'a'};
  if(op==='mix.daily.continue'){waiting=false;return {id:'batch-1',phase:'checking_login'};}
 }));
 const notice=view.query('.batch-progress-login');assert.ok(notice);
 assert.equal(notice.classList.contains('hide'),true);
 const help=view.query('.mixing-execution-account-help');
 assert.equal(help.classList.contains('hide'),false);
 assert.match(help.textContent,/已暂停.*等待账号登录/);
 assert.match(help.textContent,/抖音.*主号/);
 assert.match(help.textContent,/进度不会推进/);
 const button=help.querySelectorAll('button').find(b=>b.textContent==='登录并继续');assert.ok(button);assert.equal(button.disabled,false);
 await view.tick();assert.equal(notice.classList.contains('hide'),true);assert.equal(help.classList.contains('hide'),false);
 await view.click(button);await view.flush();await view.tick();
 assert.equal(count(view,'mix.daily.submit'),0);assert.equal(count(view,'mix.daily.continue'),1);
 assert.equal(notice.classList.contains('hide'),true);assert.equal(button.classList.contains('hide'),true);view.dispose();
});
