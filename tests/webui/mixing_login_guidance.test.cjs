const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const account={id:'original-account',platform:'douyin',nickname:'演示账号',login_state:'valid',is_default:true};
const unknown=()=>({id:'current-batch',phase:'needs_attention',can_continue:true,login_accounts:[],login_repair_accounts:[{id:account.id,platform:'douyin',nickname:account.nickname,state:'unavailable',reason:'login_check_failed'}],login_message:'暂时无法确认账号登录，可重新检查或登录账号后继续。',total:1,execution:{timing:{active:false},current:{label:'等待账号处理后继续'}}});
async function fixture(extra=()=>undefined,{historical=false}={}){
 let status=unknown();
 const v=await mountMixing(async(op,p)=>{
  const result=await extra(op,p);if(result!==undefined)return result;
  if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,recent_batch:historical?status:null};
  if(op==='mix.daily.submit')return {id:'current-batch',mix_task_id:'current-batch',phase:'checking_login'};
  if(op==='mix.daily.status')return status;
  if(op==='mix.daily.continue'){status={id:'current-batch',phase:'checking_login'};return status;}
  if(op==='account.login.start')return {id:'login-attempt',state:'completed',account_id:account.id};
  if(op==='account.login.cancel')return {state:'cancelled'};
  if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
  if(op==='settings.output.read')return {directory:'/tmp/finished'};
  if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
  if(op==='preset.mix.list'||op==='category.list')return {items:[],total:0};
  if(op==='settings.mix_preset.read')return {id:''};
  if(op==='mix.daily.save')return {saved:true};throw Error(op);
 });
 return {v,setStatus:s=>status=s};
}
const actions=v=>v.get('daily-progress').querySelectorAll('button');
const action=(v,label,root='daily-progress')=>v.get(root).querySelectorAll('button').find(b=>b.textContent===label);
const count=(v,op)=>v.calls.filter(c=>c.operation===op).length;

test('a newly started batch exposes recovery immediately below its account problem without forcing login',async()=>{
 const {v}=await fixture();await v.click(v.get('quick-generate'));
 const help=v.query('.mixing-execution-account-help');assert(help);assert.equal(help.classList.contains('hide'),false);
 assert.equal(action(v,'重新检查并继续').classList.contains('hide'),false);
 assert.equal(action(v,'登录账号').classList.contains('hide'),false);
 const children=v.get('daily-progress').children;assert(children.indexOf(help)<children.indexOf(v.query('.mixing-execution-events')));
 assert.equal(count(v,'account.login.start'),0);assert.equal(count(v,'mix.daily.continue'),0);
 assert.equal(v.get('quick-generate').disabled,false);v.dispose();
});

test('rechecking an uncertain login never opens a login window or creates another batch',async()=>{
 const {v}=await fixture();await v.click(v.get('quick-generate'));await v.click(action(v,'重新检查并继续'));
 assert.equal(count(v,'account.login.start'),0);assert.equal(count(v,'mix.daily.continue'),1);assert.equal(count(v,'mix.daily.submit'),1);
 assert.deepEqual(v.calls.find(c=>c.operation==='mix.daily.continue').params,{id:'current-batch'});v.dispose();
});

test('optional login binds the original account and only continues the same batch after login completes',async()=>{
 const {v}=await fixture();await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));await v.flush();
 assert.deepEqual(v.calls.find(c=>c.operation==='account.login.start').params,{account_id:account.id,platform:'douyin'});
 assert.deepEqual(v.calls.find(c=>c.operation==='mix.daily.continue').params,{id:'current-batch'});
 assert.equal(count(v,'mix.daily.submit'),1);assert.equal(v.get('daily-progress').dataset.phase,'checking_login');v.dispose();
});

test('a failed optional login retries login rather than silently switching to a bare recheck',async()=>{
 let attempts=0;const {v}=await fixture(op=>op==='account.login.start'?++attempts===1?{id:'failed',state:'failed',message:'登录未完成'}:{id:'successful',state:'completed',account_id:account.id}:undefined);
 await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));assert.equal(count(v,'mix.daily.continue'),0);
 await v.click(action(v,'登录账号','modal-footer'));await v.flush();
 assert.equal(count(v,'account.login.start'),2);assert.equal(count(v,'mix.daily.continue'),1);assert.equal(count(v,'mix.daily.submit'),1);v.dispose();
});

test('cancelling optional login keeps this batch and never continues or starts another batch',async()=>{
 const {v}=await fixture(op=>op==='account.login.start'?{id:'waiting',state:'waiting'}:undefined);
 await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));await v.click(action(v,'取消登录','modal-footer'));await v.loginTick();
 assert.equal(count(v,'account.login.cancel'),1);assert.equal(count(v,'mix.daily.continue'),0);assert.equal(count(v,'mix.daily.submit'),1);
 assert.equal(v.get('daily-progress').dataset.phase,'needs_attention');assert.equal(action(v,'登录账号').disabled,false);v.dispose();
});

test('old historical batches remain view-only and never acquire either recovery action',async()=>{
 const {v}=await fixture(undefined,{historical:true});
 assert.equal(v.query('.mixing-execution-account-help').classList.contains('hide'),true);
 assert.equal(count(v,'account.login.start'),0);assert.equal(count(v,'mix.daily.continue'),0);assert.equal(v.get('quick-generate').disabled,false);v.dispose();
});

test('a genuinely expired account keeps one clear login-and-continue action',async()=>{
 const {v,setStatus}=await fixture();setStatus({...unknown(),phase:'needs_login',login_accounts:[account],login_repair_accounts:[]});
 await v.click(v.get('quick-generate'));
 assert.equal(action(v,'登录并继续').classList.contains('hide'),false);assert.equal(action(v,'登录账号').classList.contains('hide'),true);
 await v.click(action(v,'登录并继续'));assert.equal(count(v,'account.login.start'),1);assert.equal(count(v,'mix.daily.continue'),1);v.dispose();
});

test('an updated account diagnosis removes stale repair actions without changing the batch',async()=>{
 const {v,setStatus}=await fixture();await v.click(v.get('quick-generate'));assert.equal(action(v,'登录账号').classList.contains('hide'),false);
 setStatus({...unknown(),login_repair_accounts:[]});await v.tick();assert.equal(action(v,'登录账号').classList.contains('hide'),true);
 assert.equal(count(v,'mix.daily.submit'),1);assert.equal(count(v,'mix.daily.continue'),0);v.dispose();
});

test('a busy original account offers its own retained verification instead of retrying login or submission',async()=>{
 const {v}=await fixture((op,p)=>{
  if(op==='account.login.start')return {id:'busy',state:'failed',code:'login_account_busy'};
  if(op==='multi_publish.intervention.status')return {request:{account_id:account.id,delivery_id:'original-delivery'}};
 });let opened=null;v.context.window.p011OpenPublishVerification=async id=>{opened=id;return true;};
 await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));await v.flush();
 assert.match(v.get('modal-title').textContent,/待完成的验证/);assert.equal(count(v,'mix.daily.continue'),0);
 assert.deepEqual(v.calls.find(c=>c.operation==='multi_publish.intervention.status').params,{account_id:account.id});
 await v.click(action(v,'处理验证','modal-footer'));await v.flush();assert.equal(opened,'original-delivery');
 assert.equal(count(v,'account.login.start'),1);assert.equal(count(v,'mix.daily.submit'),1);assert.equal(count(v,'mix.daily.continue'),0);v.dispose();
});

test('closing a busy-login explanation preserves this batch without continuing it',async()=>{
 const {v}=await fixture(op=>op==='account.login.start'?{id:'busy',state:'failed',code:'login_account_busy'}:op==='multi_publish.intervention.status'?{request:null}:undefined);
 await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));await v.flush();
 assert.match(v.get('modal-body').textContent,/当前未打开新的登录窗口/);await v.click(action(v,'知道了','modal-footer'));await v.flush();
 assert.equal(v.get('modal-root').classList.contains('hide'),true);assert.equal(action(v,'登录账号').disabled,false);assert.equal(count(v,'mix.daily.continue'),0);v.dispose();
});

test('a cancelled busy lookup cannot replace the next batch when its reply arrives late',async()=>{
 let release,batch=unknown(),submits=0;const waiting=new Promise(resolve=>release=resolve);
 const {v}=await fixture((op)=>{
  if(op==='mix.daily.submit'){if(++submits>1)batch={id:'next-batch',phase:'making'};return batch;}
  if(op==='mix.daily.status')return batch;
  if(op==='account.login.start')return {id:'busy',state:'failed',code:'login_account_busy'};
  if(op==='multi_publish.intervention.status')return waiting;
 });
 await v.click(v.get('quick-generate'));await v.click(action(v,'登录账号'));await v.click(action(v,'取消登录','modal-footer'));
 await v.click(v.get('quick-generate'));assert.equal(v.get('daily-progress').dataset.phase,'making');
 release({request:null});await v.flush();await v.flush();assert.equal(v.get('daily-progress').dataset.phase,'making');
 assert.equal(count(v,'mix.daily.submit'),2);assert.equal(count(v,'mix.daily.continue'),0);assert.equal(v.get('modal-root').classList.contains('hide'),true);v.dispose();
});
