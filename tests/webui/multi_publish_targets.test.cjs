const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const summary={videos:2,accounts:2,targets:3,ready:1,blocked:2,excluded:0};
function detail(){const deliveries=[{id:'d1',delivery_id:'d1',video_id:'v1',title:'第一条',platform:'douyin',account_id:'a1',account_label:'抖音甲',state:'result_unknown',stage:'提交结果待核对',submission_status:'uncertain',allowed_actions:['reconcile','cancel','hide'],execution_mode:'platform_schedule',scheduled_at:'2026-10-07T12:00:00+08:00',effective_fields:{title:'第一条'}},{id:'d2',delivery_id:'d2',video_id:'v1',title:'第一条',platform:'bilibili',account_id:'a2',account_label:'B站乙',state:'waiting_adapter',stage:'发布待接入',submission_status:'not_submitted',allowed_actions:['reprepare','reschedule','cancel','hide'],execution_mode:'platform_schedule',scheduled_at:'2026-10-07T12:00:00+08:00',effective_fields:{title:'第一条'}},{id:'d3',delivery_id:'d3',video_id:'v2',title:'第二条',platform:'douyin',account_id:'a1',account_label:'抖音甲',state:'platform_scheduled',stage:'平台已接收定时安排',submission_status:'submitted',allowed_actions:['reconcile','hide'],execution_mode:'platform_schedule',scheduled_at:'2026-10-07T15:00:00+08:00',effective_fields:{title:'第二条'}}];return {id:'batch',version:2,task:{id:'batch',kind:'publish',name:'本批',version:2,state:'waiting',summary,quantity:3,completed:1,succeeded:0,failed:0,created_at:'2026-10-06T00:00:00Z'},summary,counts:{result_unknown:1,waiting_adapter:1,platform_scheduled:1},accepted_count:1,published_count:0,controls:['pause'],deliveries,items:deliveries,settings:{},issues:[],outputs:[]};}
for(const state of ['result_unknown','pre_submit_failed','published'])test(`task detail is read-only for ${state}`,async()=>{
 const calls=[],data=detail();data.deliveries[0].state=state;data.deliveries[0].reason_message='测试失败原因';
 const v=await mount((op,p)=>{calls.push(op);if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[data.task],total:1};if(op==='task.center.detail')return data;throw Error(op);},'tasks');
 assert.ok(v.find(n=>n.textContent==='原因：测试失败原因'));
 assert.ok(v.find(n=>n.textContent==='抖音 · 抖音甲'));
 assert.throws(()=>v.find(n=>n.textContent==='修改本目标资料'));
 assert.throws(()=>v.find(n=>n.tag==='pre'));
 await v.tick();assert.equal(calls.some(op=>op.startsWith('multi_publish.')),false);v.dispose();
});

test('a queued account wait is displayed without starting another submission',async()=>{
 const calls=[],data=detail();data.deliveries[0].state='waiting';data.deliveries[0].execution_log=[{time:'2026-10-20T09:00:00Z',step:'account.wait',checks:{busy:true}}];
 const v=await mount((op,p)=>{calls.push(op);if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[data.task],total:1};if(op==='task.center.detail')return data;throw Error(op);},'tasks');
 assert.ok(v.find(n=>n.textContent.includes('等待账号空闲，空闲后自动继续 [account.wait]')));
 assert.equal(calls.some(op=>op.startsWith('multi_publish.')),false);v.dispose();
});
