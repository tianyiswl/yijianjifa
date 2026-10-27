const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const {mountMixing}=require('./support/mixing_view.cjs');

test('任务详情只读打开原制作批次进度，不执行账号检查或投稿',async()=>{
 const calls=[],routes=[],router={go:route=>routes.push(route)},task={id:'original',kind:'make',name:'制作批次',state:'completed',quantity:24,completed:24,succeeded:24,failed:0,created_at:'2026-10-26T03:00:00Z'};
 const v=await mount((op,p)=>{calls.push(op);if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {task,items:[],outputs:[],issues:[],daily_publish:{phase:'needs_attention',can_continue:false,publish_task_ids:['child']}};throw Error(op);},'tasks',undefined,{P011Router:router});
 await v.click('查看本批进度');
 assert.deepEqual(routes,['mixing']);assert.equal(router.pendingDailyDisplayBatchId,'original');
 assert.equal(router.dailyBatchId,undefined);assert.equal(router.pendingDailyBatchId,undefined);
 assert.equal(calls.some(op=>/submit|start|continue|resume|reconcile|login/.test(op)),false);v.dispose();
});

test('只读进度导航仅恢复指定批次，不沿用其他批次或恢复继续权限',async()=>{
 const router={pendingDailyDisplayBatchId:'original',executionBatches:{mixing:{id:'another',mode:'daily'}}};
 const status={id:'original',phase:'needs_attention',can_continue:true,publish_task_ids:['child'],accepted:7,total:24,failed:1,execution:{making:{completed:24,total:24,succeeded:24,failed:0},current:{label:'等待核对发布结果，16项暂缓'},timing:{active:false},publishing:{complete:true,targets:[{id:'unknown',state:'result_unknown',account_id:'a',platform:'douyin'},{id:'waiting',state:'waiting',account_id:'a',platform:'douyin',blocked_by:{delivery_id:'unknown',state:'result_unknown'}}]}}};
 const v=await mountMixing((op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[],capabilities:{},active_batch:{id:'another'},recent_batch:null};
  if(op==='mix.daily.status'){assert.equal(p.id,'original');return status;}
  if(op==='category.list'||op==='preset.mix.list')return {items:[],total:0};
  if(op==='settings.output.read')return {directory:'/tmp/acceptance'};
  if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
  if(op==='settings.mix_preset.read')return {id:''};
  if(op==='library.list')return {items:[],total:0};
  throw Error('Unexpected operation: '+op);
 },null,router);
 assert.match(v.get('daily-progress').textContent,/16项暂缓/);
 const buttons=v.get('daily-progress').querySelectorAll('button').filter(b=>!b.classList.contains('hide')).map(b=>b.textContent);
 assert(buttons.includes('查看本批任务'));assert.equal(buttons.some(label=>/继续|登录|取消本批/.test(label)),false);
 assert.equal(v.context.P011Router.pendingDailyDisplayBatchId,undefined);assert.equal(router.executionBatches.mixing.id,'another');
 assert.equal(v.calls.some(c=>/submit|start|continue|resume|reconcile|login/.test(c.operation)),false);v.dispose();
});

test('制作任务详情明确列出同账号结果未知造成的16项暂缓',async()=>{
 const task={id:'parent',kind:'make',name:'制作批次',state:'completed',quantity:24,completed:24,succeeded:24,failed:0,created_at:'2026-10-26T03:00:00Z'};
 const targets=[{id:'unknown',state:'result_unknown',title:'合成待核对内容',reason:'原发布结果待核对'},...Array.from({length:16},(_,i)=>({id:'waiting-'+i,state:'waiting',title:'合成暂缓内容'+i,blocked_by:{delivery_id:'unknown',state:'result_unknown'}}))];
 const v=await mount(op=>{if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[task],total:1};if(op==='task.center.detail')return {task,items:[],outputs:[],issues:[],daily_publish:{phase:'needs_attention',can_continue:false,publish_task_ids:['child']},execution:{publishing:{complete:true,targets}}};throw Error(op);},'tasks');
 assert(v.find(n=>n.tag==='p'&&n.textContent==='同一账号有发布结果待核对，16项暂缓。'));
 assert(v.find(n=>n.tag==='p'&&n.textContent==='合成待核对内容'));
 v.dispose();
});
