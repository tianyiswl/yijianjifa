const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture({final=false,changed=false,wrongMode=false,wrongTargets=false}={}){
 const calls=[],task={id:'original',version:2,kind:'publish',name:'原任务',state:'action_required',created_at:'2026-10-10T00:00:00Z',summary:{videos:1,accounts:1,targets:1}};
 const future=new Date(Date.now()+48*60*60*1000);future.setUTCHours(4,0,0,0);const scheduled=future.toISOString(),changedScheduled=new Date(future.getTime()+60*60*1000).toISOString();
 const child={id:'target',delivery_id:'target',attempt:1,state:'pre_submit_failed',platform:'channels',account_label:'测试账号',submission_status:'not_submitted',final_started:final,worker_running:false,execution_mode:'platform_schedule',scheduled_at:scheduled,effective_fields:{title:'测试片'},allowed_actions:['reprepare']};
 let reads=0;
 const v=await mount((op,p)=>{calls.push({op,p});
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[task],total:1};
  if(op==='task.center.detail')return {version:2,task,deliveries:[child]};
  if(op==='multi_publish.detail'){reads++;return {version:2,task,deliveries:[changed&&reads>1?{...child,scheduled_at:changedScheduled}:child]};}
  if(op==='multi_publish.target.reprepare')return {id:'target',state:'waiting',attempt:2,execution_mode:wrongMode?'publish_now':child.execution_mode,scheduled_at:child.scheduled_at};
  if(op==='multi_publish.execution.check')return {available:true,delivery_ids:wrongTargets?['other']:['target'],digest:'check',requires_partial_consent:false};
  if(op==='multi_publish.execution.start')return {accepted:true};
  throw Error(op);
 },'tasks');
 const row=v.getId('rows').children[0];row.oncontextmenu({isTrusted:true,target:row.children[1],clientX:100,clientY:100,preventDefault(){}});await v.flush();
 return {v,calls};
}
test('retry prepares and starts only failed unsubmitted original targets after showing original time',async()=>{
 const {v,calls}=await fixture();await v.click('重试未提交项');
 assert.equal(calls.some(c=>/reprepare|execution.start/.test(c.op)),false);
 assert.ok(v.find(n=>/测试片/.test(n.textContent)&&/定时/.test(n.textContent)));
 await v.click('按原时间重试');
 const prepared=calls.filter(c=>c.op==='multi_publish.target.reprepare');assert.equal(prepared.length,1);assert.equal(prepared[0].p.delivery_id,'target');
 const starts=calls.filter(c=>c.op==='multi_publish.execution.start');assert.equal(starts.length,1);assert.equal(starts[0].p.id,'original');assert.deepEqual(Array.from(starts[0].p.delivery_ids),['target']);
 assert.equal(calls.some(c=>/multi_publish\.(prepare|confirm)$/.test(c.op)),false);v.dispose();
});
test('a final marker prevents retry even if old row says failed',async()=>{
 const {v,calls}=await fixture({final:true});await v.click('重试未提交项');
 assert.equal(calls.some(c=>/reprepare|execution.start/.test(c.op)),false);assert.throws(()=>v.find(n=>n.textContent==='按原时间重试'));v.dispose();
});
test('changed original schedule stops continuation without writing',async()=>{
 const {v,calls}=await fixture({changed:true});await v.click('重试未提交项');await v.click('按原时间重试');
 assert.equal(calls.some(c=>/reprepare|execution.start/.test(c.op)),false);v.dispose();
});

for(const option of ['wrongMode','wrongTargets'])test(`${option} cannot start the original scheduled retry`,async()=>{
 const {v,calls}=await fixture({[option]:true});await v.click('重试未提交项');await v.click('按原时间重试');
 assert.equal(calls.some(c=>c.op==='multi_publish.execution.start'),false);v.dispose();
});
