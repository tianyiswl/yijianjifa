const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const row={id:'one',display_name:'待发视频',title:'批准的标题',body:'批准的正文',tags:'#自动剪辑',duration_ms:3000};
function text(node){return node.textContent+(node.children||[]).map(text).join('');}
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}
async function fixture(extra={},router={}){
 const calls=[],timers=new Map(),routes=[];let timerId=0,state='waiting';
 const handler=async(op,p)=>{calls.push({op,p:structuredClone(p)});if(extra[op])return extra[op](p);
  if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'逆浪风',login_state:'valid',is_default:true}],total:1};
  if(op==='publish.candidates')return {items:[row],total:1,default_account_id:'a'};
  if(op==='publish.execution.availability')return {available:true};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='publish.prepare')return {token:'approved-preparation',items:[{...row,video_id:'one',account_id:'a',time:p.start,execution_mode:'platform_schedule',blockers:[],required_choice:[]}],issues:[]};
  if(op==='publish.confirm'){assert.deepEqual(p,{token:'approved-preparation'});return {id:'current',quantity:1,state:'paused',submitted:false};}
  if(op==='publish.execution.check'){assert.deepEqual(p,{id:'current'});return {id:'current',available:true,digest:'frozen-digest'};}
  if(op==='publish.execution.start'){assert.deepEqual(p,{id:'current',digest:'frozen-digest',consent:true});return {id:'current',accepted:true,submitted:false};}
  if(op==='task.center.detail'){assert.equal(p.id,'current');return {task:{id:'current',kind:'publish',state:'running',quantity:1,account:'逆浪风'},settings:{kind:'normal_publish',platform:'douyin'},items:[{index:0,state,account_id:'a'}]};}
  if(op==='publish.progress.status')return {task:{id:'old-unfinished',stage:'正在上传并填写发布资料',title:'旧任务绝不展示',index:1,running:true,updated_at:1792886400}};
  if(/task\.center\.list|\.resume|reconcile|multi_publish/.test(op))throw Error('Must not discover or resume another task: '+op);
  return baseData(op);
 };
 const v=await mount(handler,'publish',undefined,{setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),P011Router:Object.assign(router,{go:route=>routes.push(route)})});await v.flush();
 const progress=()=>v.find(n=>n.id==='publish-batch-progress'),node=name=>name==='counts'?v.find(n=>n.id==='publish-batch-progress').querySelector('.batch-progress-publishing')?.querySelector('p'):name==='phase'?progress().querySelector('.batch-progress-phase'):name==='actions'?progress():progress().querySelector('.publish-progress-error');
 async function choose(){const check=v.find(n=>n.attrs['aria-label']==='选择 待发视频');check.checked=true;check.onchange();await v.flush();}
 async function tick(){const found=[...timers].find(([,timer])=>timer.delay===1200);if(found){timers.delete(found[0]);await found[1].fn();}await flush();}
 return {v,calls,routes,timers,progress,node,choose,tick,setState:value=>state=value};
}
const publishing=calls=>calls.filter(c=>['publish.prepare','publish.confirm','publish.execution.check','publish.execution.start'].includes(c.op));

test('ordinary current-batch progress retains prepare-confirm-start boundaries and never reads old unfinished tasks',async()=>{
 const confirmation=deferred(),check=deferred(),f=await fixture({'publish.confirm':()=>confirmation.promise,'publish.execution.check':()=>check.promise});
 assert.equal(f.calls.some(c=>['task.center.detail','publish.progress.status'].includes(c.op)),false,'Opening the page must not adopt an old task');
 await f.choose();await f.v.click('开始发布');
 assert.deepEqual(publishing(f.calls).map(c=>c.op),['publish.prepare','publish.confirm']);assert.equal(f.calls.find(c=>c.op==='publish.confirm').p.token,'approved-preparation');assert.equal(f.node('phase').textContent,'等待开始');
 confirmation.resolve({id:'current',quantity:1,state:'paused',submitted:false});await f.v.flush();
 assert.equal(f.progress().dataset.taskId,'current');assert.match(f.node('phase').textContent,/尚未开始|等待|运行|读取/);assert.equal(f.calls.some(c=>c.op==='publish.execution.start'),false);assert.equal(f.calls.some(c=>c.op==='task.center.detail'),false,'Only the confirmed batch becomes pending; no execution before its check');
 check.resolve({id:'current',available:true,digest:'frozen-digest'});await f.v.flush();await f.v.flush();
 assert.deepEqual(publishing(f.calls).map(c=>c.op),['publish.prepare','publish.confirm','publish.execution.check','publish.execution.start']);
 assert.deepEqual(f.calls.find(c=>c.op==='publish.execution.start').p,{id:'current',digest:'frozen-digest',consent:true});
 assert.equal(f.node('counts').textContent,'发布：平台已接收 0/1 项');assert(f.v.find(n=>n.id==='publish-batch-progress').querySelector('.batch-progress-publishing-bar'));assert.equal(f.progress().querySelector('.batch-progress-making').classList.contains('hide'),true);assert.equal(f.v.getId('overlay').hidden,true,'The old started modal must not open');assert.doesNotMatch(text(f.v.getId('overlay')),/定时提交已开始|右上角持续显示/);assert.doesNotMatch(text(f.progress()),/旧任务绝不展示/);
 f.setState('platform_scheduled');await f.tick();assert.equal(f.node('counts').textContent,'发布：平台已接收 1/1 项');assert.match(f.node('phase').textContent,/已全部提交平台/);
 assert(f.calls.filter(c=>c.op==='task.center.detail').every(c=>c.p.id==='current'));assert.equal(f.calls.some(c=>/task\.center\.list|\.resume|reconcile|multi_publish/.test(c.op)),false);
 const action=f.node('actions').querySelector('button');action.onclick({isTrusted:true});await f.v.flush();assert.deepEqual(f.routes,['tasks']);assert.equal(f.v.runtime.P011Router.pendingTaskId,'current');assert.equal(publishing(f.calls).length,4);f.v.dispose();
});

test('ordinary disposal ignores a late progress read and clears its recurring poll',async()=>{
 const detail=deferred(),f=await fixture({'task.center.detail':()=>detail.promise});await f.choose();await f.v.click('开始发布');await f.v.flush();assert(f.calls.some(c=>c.op==='publish.execution.start'));assert(f.calls.some(c=>c.op==='task.center.detail'));
 const snapshot=text(f.progress()),calls=f.calls.length;f.v.dispose();detail.resolve({task:{id:'current',state:'completed',quantity:1},settings:{platform:'douyin'},items:[{index:0,state:'published',account_id:'a'}]});await f.v.flush();await f.tick();assert.equal(text(f.progress()),snapshot);assert.equal(f.calls.length,calls);assert.equal([...f.timers.values()].some(t=>t.delay===1200),false);
});

test('leaving while the execution check is pending cannot start publication afterwards',async()=>{
 const check=deferred(),f=await fixture({'publish.execution.check':()=>check.promise});await f.choose();await f.v.click('开始发布');await f.v.flush();assert(f.calls.some(c=>c.op==='publish.confirm'));assert(f.calls.some(c=>c.op==='publish.execution.check'));f.v.dispose();check.resolve({available:true,digest:'frozen-digest'});await f.v.flush();assert.equal(f.calls.some(c=>c.op==='publish.execution.start'),false);assert.equal(f.calls.some(c=>c.op==='task.center.detail'),false);
});

test('a lost start response observes the confirmed batch without starting it again',async()=>{
 const f=await fixture({'publish.execution.start':()=>{throw Error('响应暂未取得');}});f.setState('platform_scheduled');await f.choose();await f.v.click('开始发布');await f.v.flush();await f.v.flush();
 assert.equal(f.calls.filter(c=>c.op==='publish.confirm').length,1);assert.equal(f.calls.filter(c=>c.op==='publish.execution.start').length,1);assert(f.calls.some(c=>c.op==='task.center.detail'&&c.p.id==='current'),'A confirmed local task remains observable even when its start response is lost');assert.equal(f.node('counts').textContent,'发布：平台已接收 1/1 项');assert.match(f.node('phase').textContent,/已全部提交平台/);assert.equal(f.v.getId('overlay').hidden,false,'The necessary uncertain-execution error stays visible');f.v.dispose();
});

test('failed confirmation has no task identity to poll and cannot adopt global old progress',async()=>{
 const f=await fixture({'publish.confirm':()=>{throw Error('保存未确认');}});await f.choose();await f.v.click('开始发布');await f.v.flush();assert.equal(f.calls.some(c=>['publish.execution.check','publish.execution.start','task.center.detail','publish.progress.status'].includes(c.op)),false);assert.equal(f.node('phase').textContent,'等待开始');assert.equal(f.v.getId('overlay').hidden,false);f.v.dispose();
});

async function multiFixture(extra={},router={}){
 const calls=[],timers=new Map();let timerId=0;const summary={videos:1,accounts:1,targets:1,ready:1,blocked:0,excluded:0};
 const handler=async(op,p)=>{calls.push({op,p:structuredClone(p)});if(extra[op])return extra[op](p);
  if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'逆浪风',login_state:'valid',is_default:true}],total:1};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='multi_publish.candidates')return {items:[row],total:1};
  if(op==='multi_publish.catalog')return {items:[{platform:'douyin',label:'抖音',login_ready:true,ordinary_ready:true,schedule_ready:true,publish_now_ready:true,field_schema:{}}]};
  if(op==='multi_publish.settings.read')return {settings:{schema_version:2,targets:[{account_id:'a',platform:'douyin'}],distribution_by_platform:{},start_policy:{kind:'relative_tomorrow_noon'},schedule:{mode:'daily',interval:'3',per_day:'1',days:'1',clock_mode:'interval',times:['12:00']},schedule_by_platform:{},common_fields:{},fields_by_platform:{}},issues:[],has_saved_settings:true};
  if(op==='multi_publish.settings.save')return {settings:p.settings,saved:true};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='multi_publish.prepare')return {prepared_id:'a'.repeat(48),quantity:1,targets:[],summary,requires_partial_consent:false};
  if(op==='multi_publish.confirm')return {id:'multi-current',version:2,quantity:1,state:'waiting',submitted:false};
  if(op==='multi_publish.detail')return {id:'multi-current',version:2,quantity:1,summary,deliveries:[{id:'delivery',delivery_id:'delivery',platform:'douyin',account_id:'a',account_label:'逆浪风',state:'waiting',accepted:true,worker_running:false,can_submit:true,allowed_actions:['start'],execution_log:[]}]};
  if(op==='multi_publish.execution.check')return {id:'multi-current',available:true,delivery_ids:['delivery'],digest:'frozen-multi',requires_partial_consent:false};
  if(op==='multi_publish.execution.start')return {id:'multi-current',accepted:true,submitted:false};
  return baseData(op);
 };
 const v=await mount(handler,'multi_publish',undefined,{setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),P011Router:router});await v.flush();
 async function submit(){const check=v.find(n=>n.attrs['aria-label']==='选择 待发视频');check.checked=true;check.onchange();await v.click('开始发布');await v.flush();}
 async function tick(){const entry=[...timers].find(([,timer])=>timer.delay===1000);if(entry){timers.delete(entry[0]);await entry[1].fn();}await v.flush();}
 return {v,calls,timers,submit,tick};
}

test('multi existing batch watcher pauses hidden-page reads and resumes the same batch only',async()=>{
 const f=await multiFixture();await f.submit();assert.equal(f.calls.filter(c=>c.op==='multi_publish.confirm').length,1);assert.equal(f.calls.filter(c=>c.op==='multi_publish.execution.start').length,1);const before=f.calls.filter(c=>c.op==='multi_publish.detail').length;assert(before>0);f.v.runtime.document.hidden=true;f.v.runtime.document.visibilityState='hidden';await f.tick();assert.equal(f.calls.filter(c=>c.op==='multi_publish.detail').length,before);f.v.runtime.document.hidden=false;f.v.runtime.document.visibilityState='visible';f.v.runtime.document.emit('visibilitychange');await f.tick();assert.equal(f.calls.filter(c=>c.op==='multi_publish.detail').length,before+1);assert(f.calls.filter(c=>c.op==='multi_publish.detail').every(c=>c.p.id==='multi-current'));assert.equal(f.calls.filter(c=>c.op==='multi_publish.execution.start').length,1);f.v.dispose();assert.equal([...f.timers.values()].some(t=>t.delay===1000),false);
});

test('multi disposal during the first confirmed detail read does not request another read or start',async()=>{
 const detail=deferred(),f=await multiFixture({'multi_publish.detail':()=>detail.promise});await f.submit();assert.equal(f.calls.filter(c=>c.op==='multi_publish.confirm').length,1);assert.equal(f.calls.filter(c=>c.op==='multi_publish.detail').length,1);f.v.dispose();detail.resolve({id:'multi-current',quantity:1,summary:{videos:1,accounts:1,targets:1,ready:1,blocked:0,excluded:0},deliveries:[]});await f.v.flush();assert.equal(f.calls.filter(c=>c.op==='multi_publish.detail').length,1);assert.equal(f.calls.some(c=>c.op==='multi_publish.execution.check'||c.op==='multi_publish.execution.start'),false);
});


test('ordinary accepted progress survives leaving and remounting the same client session without re-submission',async()=>{
 const router={executionBatches:{image_publish:{id:'image-other',mode:'multi'},multi_publish:{id:'multi-other',mode:'multi'}}},first=await fixture({},router);
 await first.choose();await first.v.click('开始发布');await first.v.flush();first.v.dispose();
 const returned=await fixture({},router);assert.equal(returned.progress().dataset.taskId,'current');assert.equal(returned.node('counts').textContent,'发布：平台已接收 0/1 项');assert(returned.calls.some(c=>c.op==='task.center.detail'&&c.p.id==='current'));assert.equal(publishing(returned.calls).length,0);assert.equal(returned.calls.some(c=>/task\.center\.list|resume|reconcile|multi_publish\.detail/.test(c.op)),false);
 returned.v.dispose();const ended=await fixture({'task.center.detail':()=>({task:{id:'current',kind:'publish',state:'completed',quantity:1,account:'逆浪风'},settings:{platform:'douyin'},items:[{index:0,state:'platform_scheduled',account_id:'a'}]})},router);
 assert.equal(ended.node('counts').textContent,'发布：平台已接收 1/1 项');await ended.choose();assert.equal(ended.v.find(n=>n.tag==='button'&&n.textContent==='开始发布').disabled,false);assert.equal(publishing(ended.calls).length,0);ended.v.dispose();
});

test('ordinary confirmation arriving after disposal preserves only its receipt for a later read',async()=>{
 const router={},confirmation=deferred(),first=await fixture({'publish.confirm':()=>confirmation.promise},router);await first.choose();await first.v.click('开始发布');first.v.dispose();const before=first.calls.length;
 confirmation.resolve({id:'current',quantity:1,state:'paused',submitted:false});await first.v.flush();assert.equal(first.calls.length,before);assert.equal(first.calls.some(c=>/execution\.check|execution\.start|task\.center\.detail/.test(c.op)),false);
 const returned=await fixture({},router);assert.equal(returned.progress().dataset.taskId,'current');assert.equal(publishing(returned.calls).length,0);returned.v.dispose();
});

test('multi accepted batch restores only detail watching and releases controls when its worker has ended',async()=>{
 const router={executionBatches:{publish:{id:'ordinary-other',mode:'ordinary'},image_publish:{id:'image-other',mode:'multi'}}},first=await multiFixture({},router);await first.submit();first.v.dispose();
 const returned=await multiFixture({'multi_publish.detail':()=>({id:'multi-current',version:2,quantity:1,summary:{videos:1,accounts:1,targets:1,ready:0,blocked:0,excluded:0},deliveries:[{id:'delivery',delivery_id:'delivery',platform:'douyin',account_id:'a',account_label:'逆浪风',state:'platform_scheduled',accepted:true,worker_running:false,can_submit:false,allowed_actions:[],execution_log:[]}]})},router);await returned.tick();
 assert.equal(returned.v.find(n=>n.id==='publish-batch-progress').dataset.taskId,'multi-current');assert.match(returned.v.find(n=>n.id==='publish-batch-progress').querySelector('.batch-progress-publishing').querySelector('p').textContent,/1\/1/);assert.equal(returned.v.find(n=>n.tag==='button'&&n.textContent==='开始发布').disabled,false);assert.equal(returned.calls.some(c=>/multi_publish\.(prepare|confirm|execution\.check|execution\.start|receipt)$/.test(c.op)),false);assert(returned.calls.filter(c=>c.op==='multi_publish.detail').every(c=>c.p.id==='multi-current'));returned.v.dispose();assert.equal([...returned.timers.values()].some(t=>t.delay===1000),false);
});

test('multi confirmation after disposal remains observable but does not read or start until returning',async()=>{
 const router={},confirmation=deferred(),first=await multiFixture({'multi_publish.confirm':()=>confirmation.promise},router);await first.submit();first.v.dispose();const before=first.calls.length;confirmation.resolve({id:'multi-current',quantity:1,state:'waiting',submitted:false});await first.v.flush();assert.equal(first.calls.length,before);assert.equal(first.calls.some(c=>/multi_publish\.(detail|execution\.check|execution\.start)$/.test(c.op)),false);
 const returned=await multiFixture({},router);await returned.tick();assert.equal(returned.v.find(n=>n.id==='publish-batch-progress').dataset.taskId,'multi-current');assert(returned.calls.some(c=>c.op==='multi_publish.detail'&&c.p.id==='multi-current'));assert.equal(returned.calls.some(c=>/multi_publish\.(confirm|execution\.check|execution\.start)$/.test(c.op)),false);returned.v.dispose();
});


test('a disposed ordinary submission receipt cannot replace a more recently accepted batch',async()=>{
 const router={},late=deferred(),older=await fixture({'publish.confirm':()=>late.promise},router);await older.choose();await older.v.click('开始发布');older.v.dispose();const newer=await fixture({},router);await newer.choose();await newer.v.click('开始发布');await newer.v.flush();newer.v.dispose();late.resolve({id:'late-old',quantity:1,state:'paused',submitted:false});await older.v.flush();
 const returned=await fixture({},router);assert.equal(returned.progress().dataset.taskId,'current');assert(returned.calls.filter(c=>c.op==='task.center.detail').every(c=>c.p.id==='current'));assert.equal(publishing(returned.calls).length,0);returned.v.dispose();
});

test('a disposed multi-platform confirm cannot replace a newer accepted batch in the same session',async()=>{
 const router={},late=deferred(),older=await multiFixture({'multi_publish.confirm':()=>late.promise},router);await older.submit();older.v.dispose();const newer=await multiFixture({},router);await newer.submit();newer.v.dispose();late.resolve({id:'late-old-multi',quantity:1,state:'waiting',submitted:false});await older.v.flush();
 const returned=await multiFixture({},router);await returned.tick();assert.equal(returned.v.find(n=>n.id==='publish-batch-progress').dataset.taskId,'multi-current');assert(returned.calls.filter(c=>c.op==='multi_publish.detail').every(c=>c.p.id==='multi-current'));assert.equal(returned.calls.some(c=>/multi_publish\.(confirm|execution\.start)$/.test(c.op)),false);returned.v.dispose();
});
