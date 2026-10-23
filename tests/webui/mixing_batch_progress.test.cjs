const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const account={id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true};
const baseRead={settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null};
const timing={started_at:'2026-10-16T04:00:00Z',last_activity_at:'2026-10-16T04:00:40Z',active:true};
function fixture(extra=()=>undefined){return async(op,p)=>{const result=await extra(op,p);if(result!==undefined)return result;
 if(op==='mix.daily.read')return baseRead;if(op==='mix.daily.save')return {saved:true};if(op==='mix.daily.submit')return {id:'daily',phase:'checking_login'};
 if(op==='mix.daily.status')return {phase:'making',execution:{making:{total:10,completed:3,succeeded:2,failed:1},timing}};
 if(op==='mix.submit')return {id:'only',quantity:5};if(op==='task.poll')return {epoch:'e',revision:1,tasks:[]};
 if(op==='preset.mix.list')return {items:[],total:0};if(op==='settings.output.read')return {directory:'/tmp/finished'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
};}
const node=(view,name)=>view.query('.batch-progress-'+name);
const mount=handler=>mountMixing(handler,null,{},{now:'2026-10-16T04:01:15Z'});

test('the left idle progress waits for a real batch without inventing counts, time or queries',async()=>{
 const v=await mount(fixture());assert.ok(v.get('mixing-batch-progress'));assert.match(v.get('mixing-batch-progress').textContent,/等待开始/);assert.equal(node(v,'making').classList.contains('hide'),true);assert.equal(node(v,'publishing').classList.contains('hide'),true);assert.equal(node(v,'time').classList.contains('hide'),true);assert.equal(v.clockTimers(),0);
 assert.equal(v.calls.some(c=>['mix.daily.status','task.poll','mix.daily.submit','mix.submit'].includes(c.operation)),false);v.dispose();
});

function target(id,state){return {id,account_id:'a',platform:'douyin',platform_label:'抖音',account_label:'抖音 · 主号',state};}
function settledStatus(extra={}){return {phase:'needs_attention',total:2,accepted:1,pending:0,failed:1,execution:{making:{total:2,completed:2,succeeded:2,failed:0},timing:{...timing,active:false},publishing:{targets:[target('one','platform_scheduled'),target('two','pre_submit_failed')],complete:true,omitted:0}},...extra};}

test('a settled failed batch clearly says its run ended in both existing status locations',async()=>{
 const v=await mount(fixture(op=>op==='mix.daily.status'?settledStatus():undefined));await v.click(v.get('quick-generate'));
 assert.equal(v.get('daily-progress').dataset.runState,'attention');assert.equal(v.get('mixing-batch-progress').dataset.runState,'attention');
 assert.equal(v.query('.mixing-execution-phase').textContent,'本批运行已结束 · 1项需处理');assert.equal(node(v,'phase').textContent,'本批运行已结束 · 1项需处理');assert.equal(v.clockTimers(),0);v.dispose();
});

test('attention does not announce completion while another target is still active',async()=>{
 const status=settledStatus();status.pending=1;status.execution.timing.active=true;status.execution.publishing.targets[0].state='running';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));
 assert.equal(v.get('daily-progress').dataset.runState,'running');assert.equal(v.query('.mixing-execution-phase').textContent,'运行中 · 其余目标继续处理');assert.doesNotMatch(node(v,'phase').textContent,/结束|已完成/);assert.equal(v.clockTimers(),1);v.dispose();
});

test('all receipts end the local run without claiming public publication',async()=>{
 const status=settledStatus();Object.assign(status,{phase:'accepted',accepted:2,failed:0});status.execution.publishing.targets[1].state='reviewing';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));
 assert.equal(node(v,'phase').textContent,'本批运行已结束 · 已全部提交平台');assert.equal(v.get('daily-progress').dataset.runState,'completed');assert.doesNotMatch(node(v,'phase').textContent,/已发布|已公开/);v.dispose();
});

test('unexplained failed counts cannot turn accepted target snapshots into a green result',async()=>{
 const status=settledStatus({accepted:2,failed:1});status.execution.publishing.targets[1].state='platform_scheduled';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.notEqual(v.get('daily-progress').dataset.runState,'completed');assert.doesNotMatch(node(v,'phase').textContent,/已全部提交平台|运行已结束/);assert.match(node(v,'publishing').textContent,/需处理 1 项/);v.dispose();
});

test('a reconciled excluded target remains visibly unresolved after other targets settle',async()=>{
 const status=settledStatus({total:3,accepted:2,failed:1,excluded_count:1});status.execution.making={total:1,completed:1,succeeded:1,failed:0};status.execution.publishing.targets[1].state='platform_scheduled';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.equal(v.get('daily-progress').dataset.runState,'attention');assert.equal(node(v,'phase').textContent,'本批运行已结束 · 1项需处理');assert.doesNotMatch(node(v,'phase').textContent,/已全部提交平台/);v.dispose();
});

for(const phase of ['paused','interrupted'])test(`an explicit ${phase} batch wins over old accepted target snapshots`,async()=>{
 const status=settledStatus({phase,accepted:2,failed:0});status.execution.publishing.targets[1].state='platform_scheduled';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.equal(v.get('daily-progress').dataset.runState,'paused');assert.equal(node(v,'phase').textContent,'已暂停 · 等待继续处理');assert.doesNotMatch(node(v,'phase').textContent,/已全部提交平台|运行已结束/);v.dispose();
});

test('an inconsistent completed making snapshot cannot claim successful completion',async()=>{
 const v=await mount(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state:'completed',quantity:2,completed:2,succeeded:0,failed:0,execution:{timing:{...timing,active:false}}}]}:undefined));await v.click(v.get('quick-only-generate'));assert.notEqual(v.get('daily-progress').dataset.runState,'completed');assert.doesNotMatch(node(v,'phase').textContent,/制作已完成|制作已结束/);v.dispose();
});

for(const [phase,can_continue,expected] of [['needs_login',true,'已暂停 · 等待登录后继续'],['needs_attention',true,'已暂停 · 等待账号处理'],['publishing',false,'已暂停 · 等待继续提交平台']])test(`a stopped ${phase} wait is explicit without claiming it ended`,async()=>{
 const status=settledStatus({phase,can_continue,pending:1});status.execution.publishing.targets[0].state='waiting';
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));
 assert.equal(v.get('daily-progress').dataset.runState,'paused');assert.equal(node(v,'phase').textContent,expected);assert.doesNotMatch(node(v,'phase').textContent,/运行中|已结束/);assert.equal(v.clockTimers(),0);v.dispose();
});

for(const changed of ['missing-active','partial','omitted','pending','unread-target','contradictory-receipts'])test(`incomplete ${changed} evidence never declares a settled run ended`,async()=>{
 const status=settledStatus();if(changed==='missing-active')delete status.execution.timing.active;if(changed==='partial')status.execution.publishing.complete=false;if(changed==='omitted')status.execution.publishing.omitted=1;if(changed==='pending')status.pending=1;if(changed==='unread-target')status.execution.publishing.targets[0].state='unknown';if(changed==='contradictory-receipts')status.accepted=0;
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.doesNotMatch(node(v,'phase').textContent,/已结束|全部提交平台/);v.dispose();
});

for(const makingFailed of [1,2])test(`a batch with ${makingFailed} failed video(s) ends only after all remaining targets settle`,async()=>{
 const status=settledStatus({total:4,accepted:makingFailed===1?1:0,failed:makingFailed===1?3:4});status.execution.making={total:2,completed:2,succeeded:2-makingFailed,failed:makingFailed};if(makingFailed===2)status.execution.publishing.targets=[];
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.equal(node(v,'phase').textContent,makingFailed===1?'本批运行已结束 · 1条制作失败，1项发布需处理':'本批运行已结束 · 2条制作失败');assert.equal(v.get('daily-progress').dataset.runState,'attention');assert.equal(v.clockTimers(),0);v.dispose();
});

test('a transition to a settled result updates status even if counts and log records do not change',async()=>{
 const status=settledStatus();status.execution.timing.active=true;
 const v=await mount(fixture(op=>op==='mix.daily.status'?status:undefined));await v.click(v.get('quick-generate'));assert.equal(v.get('daily-progress').dataset.runState,'running');status.execution.timing.active=false;await v.tick();assert.equal(v.query('.mixing-execution-phase').textContent,'本批运行已结束 · 1项需处理');assert.equal(v.clockTimers(),0);const toast=v.get('toast').textContent;await v.tick();assert.equal(v.get('toast').textContent,toast);v.dispose();
});

for(const [state,failed,expected] of [['completed',0,'视频制作已完成'],['completed_with_errors',1,'视频制作已结束 · 1条失败']])test(`only-make ${state} has a clear final result`,async()=>{
 const v=await mount(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state,quantity:2,completed:2,succeeded:2-failed,failed,execution:{timing:{...timing,active:false}}}]}:undefined));await v.click(v.get('quick-only-generate'));assert.equal(node(v,'phase').textContent,expected);assert.equal(v.query('.mixing-execution-phase').textContent,expected);assert.equal(v.clockTimers(),0);v.dispose();
});

test('queued making is visibly waiting rather than falsely running or finished',async()=>{
 const v=await mount(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state:'waiting',quantity:2,completed:0,succeeded:0,failed:0,execution:{timing}}]}:undefined));await v.click(v.get('quick-only-generate'));assert.equal(node(v,'phase').textContent,'等待开始 · 视频制作已排队');assert.equal(v.get('daily-progress').dataset.runState,'waiting');assert.doesNotMatch(node(v,'phase').textContent,/运行中|已结束/);v.dispose();
});

test('daily bars use making completion and platform acceptance without claiming public publication',async()=>{
 const v=await mount(fixture(op=>op==='mix.daily.status'?{phase:'publishing',total:8,pending:3,accepted:4,failed:1,execution:{making:{total:10,completed:3,succeeded:2,failed:1},timing}}:undefined));await v.click(v.get('quick-generate'));
 assert.match(node(v,'making').textContent,/已处理 3\/10 条.*成功 2.*失败 1/);assert.deepEqual([node(v,'making-bar').attrs.value,node(v,'making-bar').attrs.max],['3','10']);
 assert.equal(node(v,'publishing').textContent,'发布：平台已接收 4/8 项 · 需处理 1 项');assert.deepEqual([node(v,'publishing-bar').attrs.value,node(v,'publishing-bar').attrs.max],['4','8']);assert.doesNotMatch(v.get('mixing-batch-progress').textContent,/已发布/);assert.match(node(v,'phase').textContent,/正在提交平台/);v.dispose();
});

for(const [accepted,failed] of [[0,0],[0,8],[5,3],[8,0]])test(`a zero runnable queue cannot fill the acceptance bar when receipts are ${accepted}/8`,async()=>{
 const v=await mount(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',total:8,pending:0,accepted,failed,execution:{making:{total:2,completed:2,succeeded:2,failed:0},timing:{...timing,active:false}}}:undefined));await v.click(v.get('quick-generate'));assert.deepEqual([node(v,'publishing-bar').attrs.value,node(v,'publishing-bar').attrs.max],[String(accepted),'8']);assert.equal(node(v,'publishing').textContent,`发布：平台已接收 ${accepted}/8 项`+(failed?` · 需处理 ${failed} 项`:''));assert.doesNotMatch(node(v,'publishing').textContent,/已处理|未完成 0|已发布/);assert.equal(node(v,'publishing-bar').attrs['aria-label'],'平台接收进度');v.dispose();
});

test('an active batch updates elapsed wall time locally each second without extra polls or restarting its timer on identical replies',async()=>{
 const v=await mount(fixture());await v.click(v.get('quick-generate'));assert.match(node(v,'started').textContent,/10月16日12:00:00/);assert.match(node(v,'elapsed').textContent,/00:01:15/);assert.equal(v.clockTimers(),1);
 const calls=v.calls.length;v.setNow('2026-10-16T04:01:16Z');await v.clockTick();assert.match(node(v,'elapsed').textContent,/00:01:16/);assert.equal(v.calls.length,calls);await v.tick();assert.equal(v.clockTimers(),1);v.dispose();assert.equal(v.clockTimers(),0);
});

test('stopping uses only the last durable activity and marks its limit even when the rest of the response is identical',async()=>{
 let active=true;const v=await mount(fixture(op=>op==='mix.daily.status'?{phase:'making',execution:{making:{total:10,completed:3,succeeded:2,failed:1},timing:{...timing,active}}}:undefined));await v.click(v.get('quick-generate'));active=false;await v.tick();assert.match(node(v,'elapsed').textContent,/00:00:40.*截至最后记录/);assert.equal(v.clockTimers(),0);
 v.setNow('2026-10-16T05:00:00Z');await v.clockTick();assert.match(node(v,'elapsed').textContent,/00:00:40/);v.dispose();
});

test('only-make replaces the old clock and hides publication progress',async()=>{
 const v=await mount(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state:'running',quantity:5,completed:2,succeeded:2,failed:0,execution:{timing:{started_at:'2026-10-16T04:01:10Z',last_activity_at:null,active:true}}}]}:undefined));await v.click(v.get('quick-generate'));await v.click(v.get('quick-only-generate'));
 assert.match(node(v,'making').textContent,/已处理 2\/5 条/);assert.match(node(v,'elapsed').textContent,/00:00:05/);assert.equal(node(v,'publishing').classList.contains('hide'),true);assert.equal(v.clockTimers(),1);v.dispose();assert.equal(v.clockTimers(),0);const before=node(v,'elapsed').textContent;v.setNow('2026-10-16T06:00:00Z');await v.clockTick();assert.equal(node(v,'elapsed').textContent,before);
});

test('a recent stopped result restores genuine time while legacy missing time is explicitly unrecorded',async()=>{
 let withTime=true;const v=await mount(fixture(op=>{
  if(op==='mix.daily.read')return {...baseRead,recent_batch:{id:'old',phase:'needs_attention',can_continue:false}};
  if(op==='mix.daily.status')return {phase:'needs_attention',can_continue:false,total:8,pending:0,accepted:3,failed:5,execution:{making:{total:2,completed:2,succeeded:2,failed:0},...(withTime?{timing:{...timing,active:false}}:{})}};
 }));assert.match(node(v,'phase').textContent,/最近一批/);assert.match(node(v,'elapsed').textContent,/00:00:40.*截至最后记录/);assert.equal(v.clockTimers(),0);withTime=false;await v.tick();assert.match(node(v,'started').textContent,/未记录/);assert.match(node(v,'elapsed').textContent,/未记录/);assert.doesNotMatch(node(v,'elapsed').textContent,/00:00:00/);assert.equal(v.clockTimers(),0);v.dispose();
});

test('a late publishing-daily response cannot replace the selected only-make summary, clock or copy target',async()=>{
 let reads=0,finish;const v=await mount(fixture(op=>{
  if(op==='mix.daily.status')return ++reads===1?{phase:'publishing',total:8,pending:2,accepted:6,failed:0,execution:{making:{total:10,completed:10,succeeded:10,failed:0},timing}}:new Promise(resolve=>{finish=resolve;});
  if(op==='task.poll')return {epoch:'e',revision:1,tasks:[{id:'only',state:'running',quantity:5,completed:2,succeeded:2,failed:0,execution:{timing:{started_at:'2026-10-16T04:01:10Z',last_activity_at:null,active:true}}}]};
  if(op==='task.execution.copy')return {copied:true};
 }));await v.click(v.get('quick-generate'));const pending=v.tick();await v.flush();await v.click(v.get('quick-only-generate'));
 assert.equal(v.get('daily-progress').dataset.phase,'running');assert.match(node(v,'elapsed').textContent,/00:00:05/);finish({phase:'completed',accepted:8,pending:0,failed:0,total:8,execution:{timing:{...timing,active:false}}});await pending;
 assert.equal(v.get('daily-progress').dataset.phase,'running');assert.match(node(v,'phase').textContent,/正在制作/);assert.match(node(v,'making').textContent,/已处理 2\/5 条/);assert.equal(node(v,'publishing').classList.contains('hide'),true);assert.match(node(v,'elapsed').textContent,/00:00:05/);assert.equal(v.clockTimers(),1);
 v.setNow('2026-10-16T04:01:16Z');await v.clockTick();assert.match(node(v,'elapsed').textContent,/00:00:06/);await v.click(v.get('daily-progress').querySelectorAll('button').find(b=>b.textContent==='复制执行信息'));assert.deepEqual(v.calls.find(c=>c.operation==='task.execution.copy').params,{id:'only'});v.dispose();
});

test('an old only-make poll cannot roll back completed progress, restart timing or overwrite the poll cursor',async()=>{
 let reads=0,finish;const v=await mount(fixture(op=>{
  if(op==='task.poll'){
   if(++reads===1)return new Promise(resolve=>{finish=resolve;});
   return {epoch:'current-epoch',revision:2,tasks:reads===2?[{id:'only',state:'completed',quantity:5,completed:5,succeeded:5,failed:0,execution:{timing:{started_at:'2026-10-16T04:01:10Z',last_activity_at:'2026-10-16T04:01:14Z',active:false}}}]:[]};
  }
 }));await v.click(v.get('quick-only-generate'));await v.tick();assert.equal(v.get('daily-progress').dataset.phase,'completed');assert.equal(v.clockTimers(),0);
 finish({epoch:'old-epoch',revision:1,tasks:[{id:'only',state:'running',quantity:5,completed:1,succeeded:1,failed:0,execution:{timing:{started_at:'2026-10-16T04:01:10Z',last_activity_at:null,active:true}}}]});await v.flush();await v.flush();
 assert.equal(v.get('daily-progress').dataset.phase,'completed');assert.match(node(v,'making').textContent,/已处理 5\/5 条/);assert.match(node(v,'elapsed').textContent,/00:00:04.*截至最后记录/);assert.equal(v.clockTimers(),0);await v.tick();assert.deepEqual(v.calls.filter(c=>c.operation==='task.poll').at(-1).params,{epoch:'current-epoch',after:2});v.dispose();
});

for(const previous of ['daily','only'])test(`a stale ${previous} poll rejection cannot replace the current only-make reason or toast`,async()=>{
 let reads=0,reject;const v=await mount(fixture(op=>{
  if(op==='mix.daily.status')return ++reads===1?{phase:'publishing',execution:{timing}}:new Promise((resolve,fail)=>{reject=fail;});
  if(op==='task.poll'){
   if(previous==='only'&&++reads===1)return new Promise((resolve,fail)=>{reject=fail;});
   return {epoch:'current-epoch',revision:2,tasks:[{id:'only',state:'completed',quantity:5,completed:5,succeeded:5,failed:0,execution:{timing:{...timing,active:false}}}]};
  }
 }));let pending;if(previous==='daily'){await v.click(v.get('quick-generate'));pending=v.tick();await v.flush();await v.click(v.get('quick-only-generate'));}else{await v.click(v.get('quick-only-generate'));await v.tick();}
 const toast=v.get('toast').textContent;reject(Error('旧请求网络故障'));if(pending)await pending;await v.flush();await v.flush();assert.equal(v.get('daily-progress').dataset.phase,'completed');assert.equal(v.query('.mixing-execution-reason').textContent,'');assert.equal(v.get('toast').textContent,toast);assert.equal(v.clockTimers(),0);v.dispose();
});
