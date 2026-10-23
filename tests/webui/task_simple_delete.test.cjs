const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture(){
 let rows=['failed','accepted'].map((id,i)=>({id,version:2,kind:'publish',name:id,state:i?'completed':'action_required',created_at:'2026-10-10T00:00:00Z',summary:{videos:1,accounts:1,targets:1}}));const calls=[];
 const view=await mount((op,p)=>{
  calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:rows,total:rows.length};
  const child={id:'target',delivery_id:'target',attempt:1,state:'pre_submit_failed',submission_status:'not_submitted',final_started:false,worker_running:false,execution_mode:'platform_schedule',scheduled_at:'2026-10-11T04:00:00Z',effective_fields:{title:'测试片'},allowed_actions:['reprepare']};
  if(op==='task.center.detail'||op==='multi_publish.detail')return {version:2,task:rows[0],deliveries:[child]};
  if(op==='task.delete'){rows=rows.filter(t=>t.id!==p.id);return {deleted:true};}
  if(op==='app.exit')return {exiting:true};throw Error(op);
 },'tasks');return {view,calls};
}
test('selected task records delete with one click even when an unconfirmed retry card is open',async()=>{
 const {view:v,calls}=await fixture();const row=v.getId('rows').children[0];row.oncontextmenu({preventDefault(){},clientX:20,clientY:20});await v.flush();await v.click('重试未提交项');
 await v.getId('selAll').onclick({isTrusted:true});await v.flush();await v.getId('deleteSelected').onclick({isTrusted:true});await v.flush();
 assert.deepEqual(calls.filter(c=>c.op==='task.delete').map(c=>c.p.id),['failed','accepted']);
 assert.equal(calls.some(c=>/reprepare|execution.start/.test(c.op)),false);
 assert.equal(v.getId('batchModal').hidden,true);assert.equal(v.getId('rows').children.length,0);assert.equal(v.getId('detail').children.length,0);
 await v.window.p011BeforeClose();assert.equal(calls.filter(c=>c.op==='app.exit').length,1);v.dispose();
});
test('single record delete has no second dialog and exposes active v2 cancellation',async()=>{
 const {view:v,calls}=await fixture();const row=v.getId('rows').children[0];row.oncontextmenu({preventDefault(){},clientX:20,clientY:20});await v.flush();await v.click('删除任务');
 assert.deepEqual(calls.filter(c=>c.op==='task.delete').map(c=>c.p.id),['failed']);assert.equal(v.getId('batchModal').hidden,true);v.dispose();
});
test('close from an unconfirmed retry card does not require cancelling that card first',async()=>{
 const {view:v,calls}=await fixture();const row=v.getId('rows').children[0];row.oncontextmenu({preventDefault(){},clientX:20,clientY:20});await v.flush();await v.click('重试未提交项');await v.window.p011BeforeClose();
 assert.equal(calls.filter(c=>c.op==='app.exit').length,1);assert.equal(calls.some(c=>/reprepare|execution.start|task.delete/.test(c.op)),false);v.dispose();
});
