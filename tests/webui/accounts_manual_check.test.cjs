const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {mount,Node}=require('./support/commerce_view.cjs');
const account=(id,platform)=>({id,platform,nickname:'账号'+id,remark:'原备注',group_name:'',login_state:'valid',is_default:false,capabilities:{ordinary:'pending',commerce:'pending',location_scope:'pending'},login_check:null});
const text=node=>[node.textContent,...node.children.map(text)].join('');
function publishSettings(){const sandbox={Option:function(label,value){const n=new Node('option');n.textContent=label;n.value=value;return n;}};for(const file of ['multi_publish_platform_fields.js','account_publish_settings.js'])vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/yijianjifa/webui/assets',file),'utf8'),sandbox);return sandbox.P011AccountPublishSettings;}
async function fixture({rows=['douyin','kuaishou','channels','bilibili'].map((p,i)=>account(String(i),p)),start,status,cancel,availability,backend,defaults,extra={}}={}){
 const timers=new Map(),calls=[];let serial=0;
 const view=await mount(async(op,p,request)=>{
  calls.push({op,p,request});
  if(op==='account.login.availability')return availability?availability():{available:true,platforms:['douyin','kuaishou','channels','bilibili']};
  if(op==='multi_publish.catalog')return {items:['douyin','kuaishou','channels','bilibili'].map(platform=>({platform,login_ready:true,ordinary_ready:true}))};
  if(op==='account.list')return {items:structuredClone(rows.slice(p.offset,p.offset+p.limit)),total:rows.length,groups:[]};
  if(op==='account.get')return structuredClone(rows.find(r=>r.id===p.id));
  if(op==='account.login.check.start')return start?start(p):{id:'c'.repeat(48),state:'running',completed:0,total:p.account_ids.length};
  if(op==='account.login.check.status')return status?status(p):{id:p.id,state:'running',completed:0,total:rows.length};
  if(op==='account.login.check.cancel')return cancel?cancel(p):{id:p.id,state:'cancelled',completed:0,total:rows.length};
  if(op==='account.backend.start'&&backend)return backend(p);
  if(op==='account.publish_settings.read')return {account_id:p.account_id,platform:rows.find(r=>r.id===p.account_id).platform,fields:{},schema:{},...defaults};
  if(op==='account.publish_settings.save')return {saved:true};
  throw Error('Unexpected operation '+op);
 },'accounts',null,{setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),...extra});
 const select=async(...ids)=>{for(const id of ids){const checkbox=view.find(n=>n.attrs['aria-label']==='选择账号 账号'+id);checkbox.checked=true;checkbox.onchange();}await view.flush();};
 const tick=async()=>{const entry=[...timers].find(([,t])=>t.delay<3000);assert.ok(entry,'manual check should schedule a progress read');timers.delete(entry[0]);await entry[1].fn();await view.flush();};
 const clickCheck=async()=>{view.getId('checkSelected').onclick({isTrusted:true});await view.flush();};
 return {...view,rows,calls,timers,select,tick,clickCheck};
}

test('selected accounts across all four platforms run read-only login checks without a capability upload',async()=>{
 const f=await fixture({start:p=>({id:'c'.repeat(48),state:'completed',completed:p.account_ids.length,total:p.account_ids.length})});
 await f.select('0','1','2','3');assert.equal(f.getId('checkSelected').disabled,false);await f.clickCheck();
 const request=f.calls.find(c=>c.op==='account.login.check.start');assert.deepEqual(request?.p,{account_ids:['0','1','2','3']});assert.match(request.request.request_id,/^[a-f0-9]{32}$/);
 assert.equal(f.calls.some(c=>c.op.startsWith('account.capabilities.')||c.op.startsWith('publish.')),false);assert.equal(f.getId('checkSelected').textContent,'检查所选');
 assert.equal(f.getId('selectedCount').textContent,'已选 4 项');f.dispose();
});

test('the real global feature pass after account mounting keeps readonly login checks visible with commerce disabled',async()=>{
 const f=await fixture({start:p=>({id:'c'.repeat(48),state:'completed',completed:p.account_ids.length,total:p.account_ids.length})});
 try{
  f.runtime.P011ApplyFeatures();
  assert.equal(f.getId('commerce-btn').hidden,true,'The commerce handoff remains disabled');
  assert.equal(f.getId('checkSelected').hidden,false,'The global feature pass must not hide readonly login checks');
  await f.select('0','1','2','3');assert.equal(f.getId('checkSelected').disabled,false);await f.clickCheck();
  assert.deepEqual(f.calls.find(c=>c.op==='account.login.check.start').p.account_ids,['0','1','2','3']);
  f.runtime.P011ApplyFeatures();assert.equal(f.getId('checkSelected').hidden,false);
  assert.equal(f.calls.some(c=>c.op.startsWith('account.capabilities.')||c.op.startsWith('publish.')),false);
 }finally{f.dispose();}
});

test('only an active checking observation is displayed as checking',async()=>{
 const f=await fixture({rows:[{...account('0','channels'),login_check:{state:'checking',checked_at:'2026-10-25T01:00:00Z'}},{...account('1','douyin'),login_check:{state:'unavailable',checked_at:'2026-10-24T01:00:00Z',reason:'timeout'}}]});
 assert.match(text(f.getId('rows').children[0]),/正在检查.*检查开始：.*2026/);assert.doesNotMatch(text(f.getId('rows').children[0]),/登录状态：已登录|需要重新登录/);
 assert.match(text(f.getId('rows').children[1]),/已保存登录.*最近检查：检查超时.*2026/);assert.doesNotMatch(text(f.getId('rows').children[1]),/正在检查|需要重新登录/);f.dispose();
});

for(const [reason,label] of [['network_unavailable','网络暂不可用'],['timeout','检查超时'],['verification_required','需要完成平台验证'],['identity_unavailable','账号资料尚未读取完成'],['session_unavailable','登录页面尚未准备好']])test('uncertain '+reason+' explains the result without declaring the account logged in or expired',async()=>{
 const f=await fixture({rows:[{...account('0','channels'),login_check:{state:'unavailable',reason,checked_at:'2026-10-25T01:00:00Z',diagnostic:{stage:'identity_second',elapsed_ms:24000}}}]});
 const row=f.getId('rows').children[0];assert.match(text(row),new RegExp(label));assert.doesNotMatch(text(row),/已登录|需要重新登录|identity_second|24000/);
 row.onclick({target:{}});await f.flush();
 assert.match(text(f.getId('detail')),new RegExp('登录状态：'+(reason==='verification_required'?label:'已保存登录')));
 assert.match(text(f.getId('detail')),new RegExp('最近检查：'+label));f.dispose();
});

test('manual check progress uses the job id and completion refreshes saved results without replacing editors',async()=>{
 let reads=0;const f=await fixture({status:p=>{reads++;if(reads===1)return {id:p.id,state:'running',completed:1,total:2};f.rows[0].login_check={state:'valid',checked_at:'2026-10-25T01:02:00Z'};return {id:p.id,state:'completed',completed:2,total:2};}});
 f.getId('rows').children[0].onclick({target:{}});await f.flush();const remark=f.find(n=>n.id==='account-remark');
 await f.select('0','1');await f.clickCheck();assert.equal(f.getId('checkSelected').textContent,'取消检查');
 await f.tick();assert.ok(f.getId('checkSelected').parentElement.children.some(n=>!n.hidden&&n.textContent==='已检查 1 / 2 个账号'));await f.tick();
 assert.deepEqual(f.calls.filter(c=>c.op==='account.login.check.status').map(c=>c.p),[{id:'c'.repeat(48)},{id:'c'.repeat(48)}]);
 assert.match(text(f.getId('rows').children[0]),/已登录.*2026/);assert.match(text(f.getId('detail')),/最近检查：已登录.*2026/);assert.equal(f.find(n=>n.id==='account-remark'),remark);assert.equal(f.getId('checkSelected').textContent,'检查所选');f.dispose();
});

test('cancelling the manual check preserves its last completed login observation',async()=>{
 const previous={state:'unavailable',reason:'network_unavailable',checked_at:'2026-10-24T01:00:00Z'};
 const f=await fixture({rows:[{...account('0','channels'),login_check:previous}]});await f.select('0');await f.clickCheck();await f.clickCheck();
 assert.deepEqual(f.calls.find(c=>c.op==='account.login.check.cancel')?.p,{id:'c'.repeat(48)});assert.equal(f.calls.filter(c=>c.op==='account.login.check.cancel').length,1);
 assert.match(text(f.getId('rows')),/网络暂不可用/);assert.doesNotMatch(text(f.getId('rows')),/已登录|正在检查/);assert.deepEqual(f.rows[0].login_check,previous);assert.equal(f.getId('checkSelected').textContent,'检查所选');f.dispose();
});

test('leaving the account page cancels only its own manual login check',async()=>{
 const f=await fixture();await f.select('0');await f.clickCheck();let navigated=false;await f.window.p011Navigate(()=>{navigated=true;});
 assert.equal(navigated,true);assert.equal(f.calls.filter(c=>c.op==='account.login.check.cancel').length,1);assert.equal(f.calls.some(c=>c.op==='account.login.cancel'||c.op.startsWith('account.capabilities.')),false);
 const before=f.calls.length;f.dispose();for(const [,timer]of f.timers)await timer.fn();await f.flush();assert.equal(f.calls.length,before);
});

test('closing the view during check startup cancels the returned job without polling after disposal',async()=>{
 let release;const f=await fixture({start:()=>new Promise(resolve=>release=resolve)});await f.select('0');await f.clickCheck();f.dispose();release({id:'d'.repeat(48),state:'running',completed:0,total:1});await f.flush();
 assert.deepEqual(f.calls.find(c=>c.op==='account.login.check.cancel')?.p,{id:'d'.repeat(48)});assert.equal(f.calls.some(c=>c.op==='account.login.check.status'),false);
});

test('a late status response cannot restart a check that was already cancelled',async()=>{
 let release;const f=await fixture({status:()=>new Promise(resolve=>release=resolve)});await f.select('0');await f.clickCheck();const pending=f.tick();await f.flush();await f.clickCheck();
 release({id:'c'.repeat(48),state:'running',completed:0,total:1});await pending;
 assert.equal(f.calls.filter(c=>c.op==='account.login.check.cancel').length,1);assert.equal(f.getId('checkSelected').textContent,'检查所选');assert.equal([...f.timers.values()].some(t=>t.delay<3000),false);f.dispose();
});

test('accepted cancellation waits for the job without repeatedly sending cancel',async()=>{
 let reads=0;const f=await fixture({cancel:p=>({id:p.id,state:'running',completed:0,total:1}),status:p=>({id:p.id,state:++reads===1?'running':'cancelled',completed:0,total:1})});
 await f.select('0');await f.clickCheck();await f.clickCheck();assert.equal(f.getId('checkSelected').disabled,true);await f.tick();await f.tick();
 assert.equal(f.calls.filter(c=>c.op==='account.login.check.cancel').length,1);assert.equal(f.getId('checkSelected').textContent,'检查所选');f.dispose();
});

test('a failed cancel can be retried with the same write intent without discarding saved results',async()=>{
 let attempts=0;const f=await fixture({cancel:p=>{if(++attempts===1)throw Error('暂未取消');return {id:p.id,state:'cancelled',completed:0,total:1};}});
 await f.select('0');await f.clickCheck();await f.clickCheck();assert.equal(f.getId('checkSelected').disabled,false);await f.clickCheck();
 const calls=f.calls.filter(c=>c.op==='account.login.check.cancel');assert.equal(calls.length,2);assert.equal(calls[0].request.request_id,calls[1].request.request_id);assert.equal(f.getId('checkSelected').textContent,'检查所选');f.dispose();
});

test('selection across list pages still checks every selected account',async()=>{
 const rows=Array.from({length:22},(_,i)=>account(String(i),['douyin','kuaishou','channels','bilibili'][i%4]));
 const f=await fixture({rows,start:p=>({id:'c'.repeat(48),state:'completed',completed:p.account_ids.length,total:p.account_ids.length})});
 await f.select('0');await f.click('下一页');await f.select('20');await f.clickCheck();assert.deepEqual(f.calls.find(c=>c.op==='account.login.check.start').p.account_ids,['0','20']);f.dispose();
});

test('the mounted publish settings editor blocks leaving until cancelled and is replaced on account change',async()=>{
 const f=await fixture({rows:[account('0','channels'),account('1','bilibili')],extra:{P011AccountPublishSettings:publishSettings()}});
 f.getId('rows').children[0].onclick({target:{}});await f.flush();await f.click('发布设置');let navigated=false;await f.window.p011Navigate(()=>{navigated=true;});
 assert.equal(navigated,false);assert.match(f.getId('toast').textContent,/请先保存或取消发布设置/);await f.click('取消');
 f.getId('rows').children[1].onclick({target:{}});await f.flush();assert.equal(f.find(n=>n.tag==='button'&&n.textContent==='发布设置').attrs['aria-label'],'发布设置 1');await f.window.p011Navigate(()=>{navigated=true;});assert.equal(navigated,true);
 f.dispose();assert.equal(f.getId('detail').querySelectorAll('.account-publish-settings').length,0);
});

test('late login availability cannot rebuild or close an open account publish settings editor',async()=>{
 let release;const f=await fixture({rows:[account('0','channels')],availability:()=>new Promise(resolve=>release=resolve),extra:{P011AccountPublishSettings:publishSettings()}});
 f.getId('rows').children[0].onclick({target:{}});await f.flush();await f.click('发布设置');const dialogTitle=f.getId('dialog').children[0];
 release({available:true,platforms:['channels']});await f.flush();assert.equal(f.getId('modal').style.display,'grid');assert.equal(f.getId('dialog').children[0],dialogTitle);
 await f.click('取消');assert.equal(f.getId('modal').style.display,'none');f.dispose();
});

test('saved status polling respects the real settings editor ownership rather than only the shared modal style',async()=>{
 const f=await fixture({rows:[account('0','channels')],extra:{P011AccountPublishSettings:publishSettings()}});
 f.getId('rows').children[0].onclick({target:{}});await f.flush();await f.click('发布设置');const before=f.calls.filter(c=>c.op==='account.list').length;
 f.getId('modal').style.display='none';const entry=[...f.timers].find(([,timer])=>timer.delay===5000);assert.ok(entry);f.timers.delete(entry[0]);await entry[1].fn();await f.flush();
 assert.equal(f.calls.filter(c=>c.op==='account.list').length,before);await f.click('取消');f.dispose();
});

for(const completion of ['completed','cancelled','failed'])test('backend '+completion+' preserves an edited publish settings draft and restores its backend button',async()=>{
 let finish;const row=account('0','channels'),f=await fixture({rows:[row],backend:()=>new Promise(resolve=>finish=resolve),defaults:{fields:{content_declaration_choice:'none'},schema:{content_declaration_choice:{choices:[{choice:'none',name:'不添加'},{choice:'ai_generated',name:'AI生成'}]}}}});
 f.getId('rows').children[0].onclick({target:{}});await f.flush();await f.click('打开后台');await f.click('发布设置');
 const input=f.find(n=>n.attrs['aria-label']==='默认内容声明');input.value='ai_generated';input.onchange();
 if(completion==='completed')row.login_check={state:'valid',checked_at:'2026-10-25T10:00:00+00:00'};
 finish({id:'synthetic-backend',state:completion,account_id:row.id,platform:row.platform,...(completion==='failed'?{code:'login_account_busy'}:{})});await f.flush();
 assert.equal(f.getId('modal').style.display,'grid','A backend result cannot close the settings editor');
 assert.equal(f.find(n=>n.attrs['aria-label']==='默认内容声明'),input);assert.equal(input.value,'ai_generated');
 assert.equal(f.find(n=>n.tag==='button'&&n.textContent==='打开后台').disabled,false);
 if(completion==='completed')assert.match(text(f.getId('detail')),/最近检查：已登录/);
 await f.click('保存');assert.equal(f.getId('modal').style.display,'none');
 assert.equal(f.calls.find(c=>c.op==='account.publish_settings.save').p.fields.content_declaration_choice,'ai_generated');
 assert.equal(f.calls.some(c=>c.op==='multi_publish.intervention.status'),false);f.dispose();
});
