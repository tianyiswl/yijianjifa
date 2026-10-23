const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

const original=()=>({id:'sms-original',delivery_id:'dy-original',account_name:'合成账号',platform:'douyin',kind:'sms',final_started:true,message:'请完成原提交验证'});
const overlay=v=>v.find(n=>(n.className||'').includes('p011-verification-overlay'));
const hidden=v=>assert.throws(()=>overlay(v));
async function fixture({reject=false}={}){
 let request=original(),poll=null;
 const calls=[],v=await mount(async(op,p)=>{
  calls.push({op,p});
  if(op==='publish.intervention.status')return {request:null};
  if(op==='multi_publish.intervention.status'){
   if(poll){const wait=poll;poll=null;return wait;}
   return {request:p.delivery_id&&p.delivery_id!==request?.delivery_id?null:request,pending:request?1:0};
  }
  if(op==='multi_publish.intervention.defer'){if(reject)throw Error('暂时无法收起');return {deferred:true,delivery_id:p.delivery_id};}
  if(op==='multi_publish.intervention.respond')return {accepted:true};
  throw Error(op);
 },'globals',['publish_intervention']);
 return {v,calls,setRequest:r=>request=r,holdPoll:promise=>poll=promise};
}

test('defer closes immediately and a stale repeated status cannot reopen the original SMS dialog',async()=>{
 const {v,calls}=await fixture();
 await v.click('关闭');hidden(v);
 for(let i=0;i<3;i++){await v.tick();hidden(v);}
 assert.deepEqual(calls.filter(c=>c.op==='multi_publish.intervention.defer').map(c=>c.p),[{id:'sms-original',delivery_id:'dy-original'}]);
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'),false);v.dispose();
});

test('a rejected defer retains the original verification and explains the failure',async()=>{
 const {v}=await fixture({reject:true});await v.click('关闭');assert(overlay(v));
 assert(v.find(n=>n.textContent.includes('暂时无法收起')));v.dispose();
});

test('closing the prompt never hides a distinct later verification request',async()=>{
 const {v,setRequest}=await fixture();await v.click('关闭');hidden(v);
 setRequest({...original(),id:'next-sms',delivery_id:'another-target',account_name:'另一合成账号'});
 await v.tick();assert(overlay(v));assert(v.find(n=>n.tag==='h3'&&n.textContent.includes('另一合成账号')));v.dispose();
});

test('an in-flight status response cannot reopen the prompt after defer succeeds',async()=>{
 const {v,holdPoll}=await fixture();let resolve;
 holdPoll(new Promise(done=>resolve=done));const pending=v.tick();await v.flush();
 await v.click('关闭');hidden(v);resolve({request:original(),pending:1});await pending;hidden(v);v.dispose();
});

test('task-requested opening reads only the original target and does not resume or submit',async()=>{
 const {v,calls}=await fixture();await v.click('关闭');hidden(v);
 assert.equal(await v.window.p011OpenPublishVerification('dy-original'),true);assert(overlay(v));
 for(let i=0;i<2;i++){await v.tick();assert(overlay(v));}
 assert.deepEqual(calls.filter(c=>c.op==='multi_publish.intervention.status'&&c.p.delivery_id).map(c=>c.p),Array(3).fill({delivery_id:'dy-original'}));
 assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'),false);
 await v.click('验证遇到问题？');await v.click('检查验证状态');assert.deepEqual(calls.filter(c=>c.op==='multi_publish.intervention.respond').map(c=>c.p),[{id:'sms-original',delivery_id:'dy-original',action:'done'}]);
 assert.equal(calls.some(c=>/execution.start|reprepare|reschedule|publish.confirm/.test(c.op)),false);v.dispose();
});

test('missing or mismatched original verification is not displayed and disposal removes the opener',async()=>{
 const {v,setRequest}=await fixture();await v.click('关闭');hidden(v);
 assert.equal(await v.window.p011OpenPublishVerification('wrong-target'),false);hidden(v);
 setRequest(null);assert.equal(await v.window.p011OpenPublishVerification('dy-original'),false);hidden(v);
 v.dispose();assert.equal(v.window.p011OpenPublishVerification,undefined);
});

test('an explicitly opened external Channels verification stays open while automatic duplicate prompts stay hidden',async()=>{
 const {v,calls,setRequest}=await fixture();
 setRequest({...original(),platform:'channels',kind:'complex',external_browser:true});await v.tick();hidden(v);
 assert.equal(await v.window.p011OpenPublishVerification('dy-original'),true);assert(overlay(v));
 await v.tick();assert(overlay(v));assert.equal(calls.some(c=>c.op==='multi_publish.intervention.respond'),false);v.dispose();
});

test('the existing result button can query a deferred original request without relying on automatic visibility',async()=>{
 const calls=[],request={...original(),platform:'channels',kind:'complex',external_browser:true};
 const v=await mount((op,p)=>{
  calls.push({op,p});if(op==='publish.intervention.status')return {request:null};
  if(op==='multi_publish.intervention.status')return {request:p.delivery_id==='dy-original'?request:null,pending:1};
  if(op==='multi_publish.intervention.respond')return {accepted:true};throw Error(op);
 },'globals',['publish_intervention']);
 hidden(v);assert.equal(await v.window.p011CheckPublishResult('dy-original'),true);
 assert.deepEqual(calls.filter(c=>c.op==='multi_publish.intervention.respond').map(c=>c.p),[{id:'sms-original',delivery_id:'dy-original',action:'done'}]);
 hidden(v);v.dispose();
});

test('task details reopen only the requested pending delivery and keep accepted deliveries free of verification actions',async()=>{
 const calls=[],task={id:'original-task',version:2,kind:'publish',name:'原定时批次',state:'action_required',summary:{videos:2,accounts:2,targets:2}};
 const v=await mount((op,p)=>{
  calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[task],total:1};
  if(op==='task.center.detail')return {version:2,task,deliveries:[
   {id:'dy-original',platform:'douyin',state:'result_unknown',final_started:true,allowed_actions:['reconcile'],effective_fields:{title:'待验证原片'}},
   {id:'dy-accepted',platform:'douyin',state:'platform_scheduled',final_started:true,allowed_actions:[],effective_fields:{title:'已接收原片'}}]};
  throw Error(op);
 },'tasks');
 v.window.p011OpenPublishVerification=async id=>{calls.push({op:'open-original',p:{id}});return true;};
 const buttons=v.getId('detail').querySelectorAll('button').filter(n=>n.textContent==='处理验证');assert.equal(buttons.length,1);
 await v.click('处理验证');assert.deepEqual(calls.filter(c=>c.op==='open-original'),[{op:'open-original',p:{id:'dy-original'}}]);
 assert.equal(calls.some(c=>/execution.start|target.reprepare|target.reconcile|reschedule/.test(c.op)),false);v.dispose();
});
