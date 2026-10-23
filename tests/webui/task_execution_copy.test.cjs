const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
for(const fail of [false,true])test(`copy task logs without expanding or triggering publishing (${fail})`,async()=>{
 const calls=[],task={id:'original',version:2,kind:'publish',name:'原任务',state:'action_required',created_at:'2026-10-10T00:00:00Z',summary:{videos:1,accounts:1,targets:1}};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {version:2,task,deliveries:[]};if(op==='task.execution.copy'){if(fail)throw Error('复制未完成');return {copied:true};}throw Error(op);},'tasks');
 await v.click('复制执行日志');
 assert.deepEqual(calls.find(c=>c.op==='task.execution.copy').p,{id:'original'});
 assert.equal(v.getId('toast').textContent,fail?'复制未完成':'执行日志已复制。');
 assert.equal(calls.some(c=>/execution.start|reprepare|reconcile/.test(c.op)),false);v.dispose();
});

test('mix detail shows failing material and stage with saved logs in a collapsed section',async()=>{
 const calls=[],task={id:'mix',kind:'make',name:'制作批次',state:'completed_with_errors',quantity:2,completed:2,succeeded:0,failed:2,created_at:'2026-10-15T01:30:35Z'};
 const diag={material:'D:\\素材\\河北排水沟.mp4',stage:'读取视频信息',reason:'视频信息读取失败',error_type:'MediaProbeError'};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {task,items:[{index:0,state:'failed',diagnostic:diag,execution_log:[{time:'2026-10-15T01:30:35Z',step:'mix.failed',...diag}]},{index:1,state:'failed',error:'旧错误'}],issues:[{index:0,title:'本地制作失败',object:'第1条视频',...diag}],outputs:[]};if(op==='task.execution.copy')return {copied:true};throw Error(op);},'tasks');
 assert.ok(v.find(n=>n.textContent==='失败素材：'+diag.material));
 assert.ok(v.find(n=>n.textContent==='失败步骤：'+diag.stage));
 assert.ok(v.find(n=>n.textContent==='原因：'+diag.reason));
 const box=v.find(n=>n.tag==='details'&&n.children.some(c=>c.tag==='summary'&&c.textContent==='查看执行日志'));
 assert.notEqual(box.open,true);
 assert.ok(box.children.some(n=>n.textContent.includes('mix.failed')&&n.textContent.includes(diag.material)));
 assert.ok(v.find(n=>n.textContent.includes('未记录执行步骤')&&n.textContent.includes('未补造')));
 await v.click('复制执行日志');
 assert.deepEqual(calls.find(c=>c.op==='task.execution.copy').p,{id:'mix'});
 assert.equal(calls.some(c=>/execution.start|reprepare|resume/.test(c.op)),false);
 v.dispose();
});

test('daily making detail opens its existing publish task without creating or resuming work',async()=>{
 const calls=[],task={id:'mix-daily',kind:'make',name:'制作批次',state:'completed',quantity:1,completed:1,succeeded:1,failed:0,created_at:'2026-10-15T01:30:35Z'};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return p.id==='mix-daily'?{task,daily_publish:{phase:'needs_attention',reason:'账号需要重新登录',publish_task_ids:['publish-child']},items:[],outputs:[]}:{task:{...task,id:'publish-child',kind:'publish',name:'本批发布',version:2},version:2,deliveries:[]};throw Error(op);},'tasks');
 assert.ok(v.find(n=>n.textContent==='原因：账号需要重新登录'));
 await v.click('查看发布任务');
 assert.equal(calls.filter(c=>c.op==='task.center.detail').at(-1).p.id,'publish-child');
 assert.equal(calls.some(c=>/execution.start|reprepare|resume|mix.daily.submit/.test(c.op)),false);
 v.dispose();
});

test('daily task login action returns to the same mixing batch without starting work in task history',async()=>{
 const calls=[],routes=[],task={id:'mix-login',kind:'make',name:'制作批次',state:'paused',quantity:10,completed:0,succeeded:0,failed:0,created_at:'2026-10-15T01:30:35Z'};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {task,daily_publish:{phase:'needs_login',can_continue:true,reason:'主号需要登录',publish_task_ids:[]},items:[],outputs:[]};throw Error(op);},'tasks');
 v.runtime.P011Router.go=view=>routes.push(view);assert.ok(v.find(n=>n.textContent==='状态：需要登录'));
 await v.click('登录并继续本批');assert.deepEqual(routes,['mixing']);assert.equal(v.runtime.P011Router.dailyBatchId,'mix-login');
 assert.equal(calls.some(c=>/account.login|mix.daily.continue|mix.daily.submit|task.resume/.test(c.op)),false);v.dispose();
});

const nodeText=node=>[node.textContent,...node.children.map(nodeText)].join('');
async function dailyDetail(publishing){const calls=[],task={id:'mix-results',kind:'make',name:'本批制作',state:'completed',quantity:2,completed:2,succeeded:2,failed:0,created_at:'2026-10-15T01:30:35Z'};const view=await mount(async(op,p)=>{calls.push({op,p});if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {task,daily_publish:{phase:'needs_attention',publish_task_ids:['child']},...(publishing?{execution:{publishing}}:{}),items:[],outputs:[]};throw Error(op);},'tasks');return {view,calls};}

test('daily parent task details show each affected account, video and exact reason without another publishing action',async()=>{
 const {view:v,calls}=await dailyDetail({targets:[{id:'failed',account_id:'a',platform:'bilibili',account_label:'B站 · 同名账号',title:'<img src=x onerror=bad()>',state:'pre_submit_failed',state_label:'提交前未完成',reason:'提交前处理超时，尚未提交平台。'},{id:'unknown',account_id:'b',platform:'bilibili',account_label:'B站 · 同名账号',title:'第二条视频',state:'result_unknown',state_label:'结果待核对',reason:'请核对原结果，不要重复提交。'},{id:'accepted',account_id:'c',platform:'channels',account_label:'视频号 · 接收账号',title:'接收视频',state:'platform_scheduled',state_label:'平台已接收定时安排',reason:''},{id:'pending',account_id:'d',platform:'douyin',account_label:'抖音 · 等待账号',title:'等待视频',state:'waiting',state_label:'等待处理',reason:''}],complete:true,omitted:0});
 const issues=v.getId('detail').querySelector('.daily-publish-issues');assert.ok(issues);assert.equal(issues.querySelectorAll('.daily-publish-issue').length,2);assert.match(nodeText(issues),/B站 · 同名账号.*<img src=x onerror=bad\(\)>.*提交前未完成.*提交前处理超时，尚未提交平台。/);assert.match(nodeText(issues),/第二条视频.*结果待核对.*不要重复提交/);assert.doesNotMatch(nodeText(issues),/接收账号|等待账号/);assert.equal(issues.querySelectorAll('img').length,0);assert.equal(calls.some(c=>/execution.start|reprepare|resume|mix.daily.submit/.test(c.op)),false);v.dispose();
});

for(const state of ['waiting_adapter','waiting_configuration','missed_schedule','unknown'])test(`daily parent task details retain the ${state} target and its account reason`,async()=>{
 const {view:v}=await dailyDetail({targets:[{id:'target',account_id:'a',platform:'douyin',account_label:'抖音 · 待处理账号',title:'原视频',state,state_label:'状态需要核对',reason:'请先处理本账号的原操作。'}],complete:state!=='unknown',omitted:0});const issues=v.getId('detail').querySelector('.daily-publish-issues');assert.ok(issues);assert.match(nodeText(issues),/抖音 · 待处理账号.*原视频.*状态需要核对.*请先处理本账号/);v.dispose();
});

test('daily parent details preserve partial result notices and do not fabricate legacy failure lists',async()=>{
 const {view:v}=await dailyDetail({targets:[],complete:false,omitted:2});assert.match(nodeText(v.getId('detail')),/部分记录暂不可读取.*另2项/);assert.equal(v.getId('detail').querySelectorAll('.daily-publish-issue').length,0);v.dispose();const {view:old}=await dailyDetail();assert.equal(old.getId('detail').querySelector('.daily-publish-issues'),null);old.dispose();
});
