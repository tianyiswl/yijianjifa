const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
test('unavailable receipt storage does not show the unwanted warning',async()=>{
 const view=await mount(op=>{if(op==='local.checks.status')return {state:'healthy'};if(op==='task.center.list')return {items:[],total:0};throw Error(op);},'tasks',undefined,{localStorage:{getItem(){throw Error('SecurityError');}}});
 assert.throws(()=>view.find(n=>n.attrs['aria-label']==='操作核对提示'));
 assert.notEqual(view.getId('toast').style.display,'block');view.dispose();
});
test('task operation toast expires and its timer is cleaned up when leaving',async()=>{
 const timers=new Map(),cleared=[];let seq=0;
 const view=await mount(op=>{
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')throw Error('列表读取失败');
  throw Error(op);
 },'tasks',undefined,{setTimeout(fn,ms){const key=++seq;timers.set(key,{fn,ms});return key;},clearTimeout(key){cleared.push(key);timers.delete(key);}});
 assert.equal(view.getId('toast').style.display,'block');
 const expiry=[...timers].find(([,t])=>t.ms===3000);assert.ok(expiry);
 expiry[1].fn();assert.equal(view.getId('toast').style.display,'none');
 view.dispose();assert.ok(cleared.includes(expiry[0]));
});
test('download task list and detail explain partial work separately from failed and successful items',async()=>{
 const task={id:'download-one',kind:'download',name:'下载测试',state:'completed_with_errors',quantity:1,completed:1,succeeded:0,failed:0,partial:1,created_at:'2026-10-05T00:00:00'};
 const view=await mount(op=>{
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[task],total:1};
  if(op==='task.center.detail')return {task,controls:[],settings:{kind:'download'},items:[],issues:[],outputs:[],download_task_id:task.id};
  throw Error(op);
 },'tasks');
 assert.ok(view.find(n=>n.tag==='td'&&/部分完成1/.test(n.textContent)));
 assert.ok(view.find(n=>n.tag==='p'&&/部分完成1/.test(n.textContent)));
 view.dispose();
});
test('task page shows failed checks even when task list fails and clears warning only after recovery',async()=>{
 let state='failed',listFails=true;
 const view=await mount((op)=>{
  if(op==='local.checks.status')return {state,last_success_at:null,last_failure_at:'2026-09-25T10:00:00+00:00'};
  if(op==='task.center.list'){if(listFails)throw Error('模拟列表读取失败');return {items:[],total:0};}
  throw Error(op);
 },'tasks');
 const banner=view.find(n=>n.attrs['role']==='status');assert.equal(banner.hidden,false);
 assert.match(banner.textContent,/检查失败/);assert.match(banner.textContent,/不会自动补发/);
 listFails=false;await view.tick();assert.equal(banner.hidden,false);
 state='healthy';await view.tick();assert.equal(banner.hidden,true);
 state='stopped';await view.tick();assert.equal(banner.hidden,false);assert.match(banner.textContent,/未运行/);
 view.dispose();
});
test('task page cannot present an unavailable check status as healthy',async()=>{
 let unreachable=false;
 const view=await mount((op)=>{
  if(op==='local.checks.status'){if(unreachable)throw Error('status unavailable');return {state:'healthy'};}
  if(op==='task.center.list')return {items:[],total:0};throw Error(op);
 },'tasks');
 const banner=view.find(n=>n.attrs['role']==='status');assert.equal(banner.hidden,true);
 unreachable=true;await view.tick();assert.equal(banner.hidden,false);assert.match(banner.textContent,/无法读取/);
 view.dispose();
});
for(const kind of ['make','publish','download'])test(`${kind} detail is read-only and exposes the failure reason`,async()=>{
 const calls=[],task={id:'batch',kind,name:'验收批次',state:'failed',quantity:1,completed:1,succeeded:0,failed:1,created_at:'2026-10-10T00:00:00Z'};
 const view=await mount((op,p)=>{
  calls.push(op);
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[task],total:1};
  if(op==='task.center.detail')return {task,controls:['cancel','resume'],settings:{private_fixture:'must-not-render'},items:[{index:0,title:'测试视频',state:'failed'}],issues:[{title:'处理失败',stage:'上传',reason:'网络超时'}],outputs:[],manual_review:{available:true,entries:[]}};
  throw Error(op);
 },'tasks');
 assert.ok(view.find(n=>n.textContent==='原因：网络超时'));
 assert.ok(view.find(n=>n.textContent==='测试视频'));
 const detail=view.getId('detail');const texts=detail.children.map(n=>n.textContent).join(' ');
 assert.equal(/private_fixture|冻结|核对记录/.test(texts),false);
 assert.deepEqual(detail.children.filter(n=>n.tag==='button').map(n=>n.textContent),['复制执行日志','删除任务']);
 await view.tick();assert.equal(calls.some(op=>/cancel|resume|review.save|execution.start/.test(op)),false);
 view.dispose();
});

test('batch delete removes selected records directly and reports only unsuccessful ones',async()=>{
 let rows=['a','b'].map(id=>({id,kind:'publish',name:id,state:'paused',quantity:1,completed:0,succeeded:0,failed:0,created_at:'2026-09-29T00:00:00'}));const calls=[];
 const view=await mount((op,p)=>{
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:rows,total:rows.length};
  if(op==='task.center.detail')return {task:rows.find(t=>t.id===p.id),controls:[],settings:{},items:[],issues:[],outputs:[]};
  if(op==='task.delete'){calls.push(p.id);if(p.id==='b')throw Error('结果待确认');rows=rows.filter(t=>t.id!==p.id);return {deleted:true};}throw Error(op);
 },'tasks');
 await view.getId('selAll').onclick({isTrusted:true});await view.flush();await view.getId('deleteSelected').onclick({isTrusted:true});await view.flush();
 assert.deepEqual(calls,['a','b']);assert.equal(view.getId('batchModal').hidden,true);
 assert.match(view.getId('toast').textContent,/已删除 1 个/);assert.match(view.getId('toast').textContent,/b：结果待确认/);view.dispose();
});

test('single delete removes the task without a second dialog',async()=>{
 const task={id:'one',kind:'publish',name:'单条测试任务',state:'cancelled',quantity:1,completed:0,succeeded:0,failed:0,created_at:'2026-09-29T00:00:00'};let deleted=0;
 const view=await mount((op)=>{
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:deleted?[]:[task],total:deleted?0:1};
  if(op==='task.center.detail')return {task,controls:[],settings:{},items:[],issues:[],outputs:[]};
  if(op==='task.delete'){deleted++;return {deleted:true};}throw Error(op);
 },'tasks');
 await view.getId('rows').children[0].oncontextmenu({preventDefault(){},clientX:20,clientY:20});await view.flush();await view.click('删除任务');
 assert.equal(view.getId('batchModal').hidden,true);assert.equal(deleted,1);assert.equal(view.getId('rows').children.length,0);view.dispose();
});
test('multi-platform detail shows target data and reason without editing or raw receipts',async()=>{
 const calls=[],task={id:'batch',version:2,kind:'publish',name:'多平台发布',state:'action_required',created_at:'2026-10-10T00:00:00Z'};
 const view=await mount(op=>{
  calls.push(op);
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[{...task,summary:{videos:1,accounts:1,targets:1}}],total:1};
  if(op==='task.center.detail')return {id:'batch',version:2,task,summary:{videos:1,accounts:1,targets:1},controls:['pause'],deliveries:[{delivery_id:'d',platform:'bilibili',account_label:'测试账号',state:'pre_submit_failed',execution_mode:'platform_schedule',scheduled_at:'2026-10-11T04:00:00Z',reason_message:'封面处理失败',allowed_actions:['reprepare','start','cancel'],receipt_summary:{private_fixture:'must-not-render'},effective_fields:{title:'测试视频',body:'正文',tags:['功能测试']}}]};
  throw Error(op);
 },'tasks');
 assert.ok(view.find(n=>n.textContent==='B站 · 测试账号'));
 assert.ok(view.find(n=>n.textContent==='原因：封面处理失败'));
 assert.ok(view.find(n=>n.textContent==='正文：正文'));
 assert.ok(view.find(n=>n.textContent==='标签：功能测试'));
 assert.throws(()=>view.find(n=>n.tag==='pre'));
 assert.throws(()=>view.find(n=>n.textContent==='修改本目标资料'));
 assert.throws(()=>view.find(n=>n.textContent==='开始本目标'));
 assert.equal(calls.some(op=>op.startsWith('multi_publish.')),false);
 view.dispose();
});
