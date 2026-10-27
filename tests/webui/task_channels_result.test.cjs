const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture(handled,platform='channels',extra={}){
 const calls=[],task={id:'original',version:2,kind:'publish',name:'原任务',state:'action_required',created_at:'2026-10-10T00:00:00Z',summary:{videos:1,accounts:1,targets:1}};
 const d={id:'target',platform,final_started:true,state:'result_unknown',allowed_actions:['reconcile'],effective_fields:{title:'测试片'},...extra};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {version:2,task,deliveries:[d]};if(op==='multi_publish.target.reconcile')return {accepted:true};throw Error(op);},'tasks');
 v.window.p011CheckPublishResult=async delivery=>{calls.push({op:'original.verification',p:{delivery}});return handled;};return {v,calls};
}
for(const platform of ['channels','douyin','kuaishou','bilibili'])for(const handled of [true,false])test(`task result button checks the original delivery without a new publishing action (${platform}, ${handled})`,async()=>{
 const {v,calls}=await fixture(handled,platform);assert.equal(calls.some(c=>c.op==='original.verification'),false);
 await v.click('核对发布结果');assert.equal(calls.filter(c=>c.op==='original.verification').length,1);
 assert.equal(calls.filter(c=>c.op==='multi_publish.target.reconcile').length,handled?0:1);
 assert.equal(calls.some(c=>/execution.start|reprepare|reschedule/.test(c.op)),false);v.dispose();
});
for(const extra of [{final_started:false},{allowed_actions:[]}])test(`未提交或未获准核对时不显示结果核对按钮 ${JSON.stringify(extra)}`,async()=>{
 const {v,calls}=await fixture(false,'douyin',extra);
 assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='核对发布结果'),/View node not found/);
 assert.equal(calls.some(c=>c.op==='multi_publish.target.reconcile'),false);v.dispose();
});
