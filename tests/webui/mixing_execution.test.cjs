const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
function fixture(extra=()=>undefined){return async(op,p)=>{const result=await extra(op,p);if(result!==undefined)return result;
 if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null};
 if(op==='mix.daily.save')return {saved:true};if(op==='mix.daily.submit')return {id:'batch',quantity:10,phase:'checking_login'};
 if(op==='mix.daily.status')return {id:'batch',phase:'checking_login',accepted:0,pending:10,failed:0,total:10};
 if(op==='mix.submit')return {id:'only',quantity:10,state:'waiting'};if(op==='task.poll')return {epoch:'e',revision:1,tasks:[]};
 if(op==='preset.mix.list')return {items:[],total:0};if(op==='settings.output.read')return {directory:'/tmp/output'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
};}
const node=(view,name)=>view.query('.mixing-execution-'+name);
const logEvent=(message='正在制作第一条')=>({time:'2026-10-16T04:03:12Z',step:'mix.started',message,title:'真实内容'});
const action=(view,text)=>view.get('daily-progress').querySelectorAll('button').find(b=>b.textContent===text);

test('the idle execution panel stays visible without querying or starting a batch',async()=>{
 const view=await mountMixing(fixture());assert.equal(view.get('daily-progress').classList.contains('hide'),false);assert.match(view.get('daily-progress').textContent,/开始后，这里显示账号检查、视频制作和平台提交进度。/);
 assert.equal(view.get('daily-progress').attrs['aria-live'],'off');assert.equal(view.calls.some(c=>['mix.daily.status','task.poll','mix.daily.submit','mix.submit'].includes(c.operation)),false);view.dispose();
});

test('daily progress shows true making counts, current content and account, timed records and precise failure reasons as text',async()=>{
 let status={phase:'making',accepted:0,pending:10,failed:0,total:10,execution:{making:{succeeded:2,failed:1,completed:3,total:10},current:{index:4,total:10,title:'<img src=x onerror=bad()>',account_label:'原账号'},events:[logEvent()]}};
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?status:undefined));await view.click(view.get('quick-generate'));assert.match(node(view,'making').textContent,/成功 2\/10 条.*失败 1 条/);assert.match(node(view,'current').textContent,/第4\/10条.*<img src=x onerror=bad\(\)>.*原账号/);assert.equal(node(view,'current').querySelectorAll('img').length,0);
 assert.match(node(view,'events').textContent,/10月16日12:03:12.*正在制作第一条/);status={...status,phase:'needs_attention',reason:'指定素材无法读取，第二条制作失败。'};await view.tick();assert.match(node(view,'reason').textContent,/指定素材无法读取，第二条制作失败。/);assert.equal(node(view,'reason').attrs.role,'alert');view.dispose();
});

test('platform acceptance is shown separately from public publication and unknown making counts are not invented',async()=>{
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'accepted',accepted:8,pending:0,failed:2,total:10,execution:{current:{label:'平台已接收本批安排'},events:[]}}:undefined));await view.click(view.get('quick-generate'));
 assert.equal(node(view,'publishing').textContent,'发布：平台已接收 8/10 项 · 未完成2项');assert.match(node(view,'phase').textContent,/已提交平台/);assert.doesNotMatch(view.get('daily-progress').textContent,/实际已发布|全部已发布/);assert.match(node(view,'making').textContent,/正在读取制作进度/);assert.doesNotMatch(node(view,'events').textContent,/\d\d:\d\d:\d\d/);view.dispose();
});

test('video-only batches use the existing poll execution without extra reads or fabricated timestamps',async()=>{
 const view=await mountMixing(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state:'running',quantity:10,succeeded:3,failed:0,completed:3,execution:{making:{succeeded:3,failed:0,completed:3,total:10},current:{index:4,total:10,title:'只生成内容'},events:[logEvent('制作第三条完成')]}}]}:undefined));await view.click(view.get('quick-only-generate'));
 assert.match(node(view,'phase').textContent,/正在制作/);assert.match(node(view,'making').textContent,/成功 3\/10 条/);assert.equal(node(view,'publishing').classList.contains('hide'),true);assert.match(node(view,'events').textContent,/制作第三条完成/);assert.equal(view.calls.some(c=>['task.detail','mix.daily.status','mix.daily.submit'].includes(c.operation)),false);assert.equal(view.calls.filter(c=>c.operation==='task.poll').length,1);view.dispose();
});

test('unchanged polls retain DOM and log scroll position; appended records do not steal focus or scroll',async()=>{
 let events=[logEvent()];const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'making',accepted:0,pending:10,failed:0,total:10,execution:{making:{succeeded:0,failed:0,completed:0,total:10},events}}:undefined));await view.click(view.get('quick-generate'));
 const logs=node(view,'events'),row=logs.querySelector('li'),phase=node(view,'phase');logs.scrollTop=42;let focused=0;logs.focus=()=>focused++;await view.tick();assert.equal(logs.querySelector('li'),row);assert.equal(node(view,'phase'),phase);assert.equal(logs.scrollTop,42);
 events=[...events,{...logEvent('正在提交平台'),time:'2026-10-16T04:04:00Z',step:'submission.authorized'}];await view.tick();assert.equal(logs.querySelectorAll('li').length,2);assert.equal(logs.scrollTop,42);assert.equal(focused,0);view.dispose();
});

test('resume and task-detail actions live in the execution panel and keep the existing batch identity',async()=>{
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_login',can_continue:true,login_message:'主号需要登录',login_accounts:[{id:'a',platform:'douyin',nickname:'主号'}],total:10}:undefined),undefined,{dailyBatchId:'batch'});
 assert.ok(node(view,'account-help').contains(action(view,'登录并继续')));assert.ok(node(view,'actions').contains(action(view,'查看本批任务')));assert.equal(view.get('quick-generate').disabled,false);await view.click(action(view,'查看本批任务'));assert.deepEqual(view.routes.at(-1),['tasks']);assert.equal(view.context.P011Router.pendingTaskId,'batch');assert.equal(view.calls.filter(c=>c.operation==='mix.daily.submit').length,0);view.dispose();
});

test('checking shows only the native batch label, and missing or invalid timestamps do not become historical records',async()=>{
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'checking_login',total:10,execution:{current:{label:'正在检查本批5个账号',total:5},events:[{step:'login.check',message:'没有持久时间'},{time:'invalid',step:'login.check',message:'无效时间'}]}}:undefined));await view.click(view.get('quick-generate'));assert.equal(node(view,'current').textContent,'正在检查本批5个账号');assert.doesNotMatch(node(view,'current').textContent,/账号：|第5/);assert.equal(node(view,'events').querySelectorAll('li').length,0);assert.match(node(view,'events').textContent,/暂无执行记录/);view.dispose();
});

test('a poll failure is visible without replacing known counts or inventing a log, and recovery clears it',async()=>{
 let failed=false;const view=await mountMixing(fixture(op=>{if(op==='mix.daily.status'){if(failed)throw Error('网络暂不可用');return {phase:'making',total:10,execution:{making:{succeeded:3,failed:0,completed:3,total:10},events:[logEvent()]}};}}));await view.click(view.get('quick-generate'));const toast=view.get('toast').textContent;failed=true;await view.tick();assert.match(node(view,'reason').textContent,/进度读取失败：网络暂不可用/);assert.match(node(view,'making').textContent,/成功 3\/10 条/);assert.equal(node(view,'events').querySelectorAll('li').length,1);assert.equal(view.get('toast').textContent,toast);failed=false;await view.tick();assert.equal(node(view,'reason').textContent,'');view.dispose();
});

test('logs initially show the latest entry and follow new entries only while the user stays near the bottom',async()=>{
 let events=[logEvent('第一条'),logEvent('第二条')];const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'making',total:10,execution:{events}}:undefined));const logs=node(view,'events');Object.defineProperty(logs,'scrollHeight',{get:()=>logs.querySelectorAll('li').length*100});Object.defineProperty(logs,'clientHeight',{value:100});logs.scrollTop=0;
 await view.click(view.get('quick-generate'));assert.equal(logs.scrollTop,100);events=[...events,logEvent('第三条')];await view.tick();assert.equal(logs.scrollTop,200);
 logs.scrollTop=20;events=[...events,logEvent('第四条')];await view.tick();assert.equal(logs.scrollTop,20);const row=logs.querySelector('li');await view.tick();assert.equal(logs.scrollTop,20);assert.equal(logs.querySelector('li'),row);view.dispose();
});

test('video-only execution exposes its genuine failure reason and clears it when a later response has no reason',async()=>{
 let withReason=true;const view=await mountMixing(fixture(op=>op==='task.poll'?{epoch:'e',revision:withReason?1:2,tasks:[{id:'only',state:'running',quantity:10,succeeded:withReason?0:1,failed:1,completed:withReason?1:2,execution:{making:{succeeded:withReason?0:1,failed:1,completed:withReason?1:2,total:10},...(withReason?{reason:'第二条素材无法读取，请检查原视频。'}:{})}}]}:undefined));await view.click(view.get('quick-only-generate'));assert.match(node(view,'reason').textContent,/第二条素材无法读取，请检查原视频/);assert.equal(node(view,'reason').classList.contains('hide'),false);
 withReason=false;await view.tick();assert.equal(node(view,'reason').textContent,'');assert.equal(node(view,'reason').classList.contains('hide'),true);view.dispose();
});

test('copying execution information is unavailable before a real batch exists',async()=>{
 const view=await mountMixing(fixture());const copy=action(view,'复制执行信息');
 assert.ok(copy,'The idle panel exposes its copy action');assert.equal(copy.disabled,true);assert.equal(node(view,'actions').classList.contains('hide'),false);
 await view.click(copy);assert.equal(view.calls.some(c=>c.operation==='task.execution.copy'),false);assert.equal(view.calls.some(c=>c.operation==='mix.daily.submit'||c.operation==='mix.submit'),false);view.dispose();
});

test('copy uses the current daily batch and the existing native fresh, redacted execution-log bridge',async()=>{
 const view=await mountMixing(fixture(op=>op==='task.execution.copy'?{copied:true}:undefined));await view.click(view.get('quick-generate'));
 const copy=action(view,'复制执行信息');assert.ok(copy);assert.equal(copy.disabled,false);const reads=view.calls.filter(c=>c.operation==='mix.daily.status').length;
 await view.click(copy);const calls=view.calls.filter(c=>c.operation==='task.execution.copy');assert.equal(calls.length,1);assert.deepEqual(calls[0].params,{id:'batch'});
 assert.equal(view.calls.filter(c=>c.operation==='mix.daily.status').length,reads,'Copy does not create another progress poll');assert.equal(view.calls.filter(c=>c.operation==='mix.daily.submit').length,1);assert.match(view.get('toast').textContent,/执行信息已复制/);assert.ok(action(view,'查看本批任务'));view.dispose();
});

test('copy uses the video-only task and reports clipboard failure without blocking retry',async()=>{
 let attempts=0;const view=await mountMixing(fixture(op=>{if(op==='task.execution.copy'){if(++attempts===1)throw Error('剪贴板不可用');return {copied:true};}}));await view.click(view.get('quick-only-generate'));
 const copy=action(view,'复制执行信息');assert.ok(copy);await view.click(copy);assert.match(view.get('toast').textContent,/剪贴板不可用/);assert.equal(copy.disabled,false);
 await view.click(copy);assert.deepEqual(view.calls.filter(c=>c.operation==='task.execution.copy').map(c=>c.params),[{id:'only'},{id:'only'}]);assert.match(view.get('toast').textContent,/执行信息已复制/);assert.equal(view.calls.filter(c=>c.operation==='mix.submit').length,1);assert.equal(view.calls.some(c=>c.operation==='mix.daily.submit'),false);view.dispose();
});

test('an in-flight execution copy is not duplicated and cannot update a departed page',async()=>{
 let complete;const pending=new Promise(resolve=>{complete=resolve;});const view=await mountMixing(fixture(op=>op==='task.execution.copy'?pending:undefined));await view.click(view.get('quick-generate'));
 const copy=action(view,'复制执行信息');assert.ok(copy);await view.click(copy);assert.equal(copy.disabled,true);await view.click(copy);assert.equal(view.calls.filter(c=>c.operation==='task.execution.copy').length,1);
 const toast=view.get('toast').textContent;view.dispose();complete({copied:true});await view.flush();assert.equal(view.get('toast').textContent,toast);
});

for(const phase of ['needs_attention','completed'])test(`a recent ${phase} batch restores execution and copy without restoring its old date or starting work`,async()=>{
 const recent={id:'previous',phase,can_continue:false},view=await mountMixing(fixture((op,p)=>{
  if(op==='mix.daily.read')return {settings:{mix:{quantity:6,total_ms:15000,segment_ms:3000},publishing:{targets:[{account_id:'a',platform:'douyin'}],mode:'platform_schedule',start_date:'2026-10-28',time:'11:05',interval_hours:2.5,days:2,per_day:3}},accounts:[{id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:true,active_batch:null,recent_batch:recent};
  if(op==='mix.daily.status')return {...recent,accepted:phase==='completed'?6:0,failed:phase==='completed'?0:6,total:6,reason:phase==='needs_attention'?'视频号表单未加载':'',execution:{making:{succeeded:6,failed:0,completed:6,total:6},events:[logEvent('本批实际记录')]}};
  if(op==='task.execution.copy')return {copied:true};
 }),null,{},{now:'2026-10-16T04:00:00Z'});
 assert.equal(view.get('daily-progress').dataset.phase,phase);assert.equal(node(view,'hint').textContent,'最近一批执行结果');assert.match(node(view,'events').textContent,/本批实际记录/);assert.equal(view.get('daily-publish-day').value,'2026-10-17');assert.equal(view.get('daily-publish-time').value,'11:05');assert.equal(view.get('quick-generate').disabled,false);
 assert.equal(node(view,'actions').querySelectorAll('button').some(b=>/继续/.test(b.textContent)&&!b.classList.contains('hide')),false);const copy=action(view,'复制执行信息');assert.equal(copy.disabled,false);await view.click(copy);
 assert.deepEqual(view.calls.find(c=>c.operation==='task.execution.copy').params,{id:'previous'});assert.equal(view.calls.some(c=>['mix.submit','mix.daily.submit','mix.daily.continue','account.login.start'].includes(c.operation)),false);view.dispose();
});

test('a display-only recent batch cannot turn a later auth status into a continuation or block a new task',async()=>{
 let auth=false;const view=await mountMixing(fixture(op=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null,recent_batch:{id:'previous',phase:'completed',can_continue:false}};
  if(op==='mix.daily.status')return {id:'previous',phase:auth?'needs_login':'completed',can_continue:auth,login_message:auth?'请先处理原账号':'',login_accounts:auth?[{id:'a',platform:'douyin',nickname:'主号'}]:[]};
 }));assert.equal(view.get('daily-progress').dataset.phase,'completed');assert.equal(node(view,'hint').textContent,'最近一批执行结果');assert.ok(action(view,'复制执行信息'));auth=true;await view.tick();assert.equal(view.get('daily-progress').dataset.phase,'needs_login');assert.equal(node(view,'hint').textContent,'请先处理原账号');assert.equal(view.get('quick-generate').disabled,false);assert.equal(node(view,'actions').querySelectorAll('button').some(b=>/继续/.test(b.textContent)&&!b.classList.contains('hide')),false);
 assert.equal(view.calls.some(c=>['mix.submit','mix.daily.submit','mix.daily.continue','account.login.start'].includes(c.operation)),false);view.dispose();
});

test('an explicit new daily click replaces a recent display with a new batch and its copy target',async()=>{
 const view=await mountMixing(fixture((op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null,recent_batch:{id:'previous',phase:'completed',can_continue:false}};
  if(op==='mix.daily.status'&&p.id==='previous')return {id:'previous',phase:'completed',can_continue:false};
  if(op==='task.execution.copy')return {copied:true};
 }));assert.equal(view.get('daily-progress').dataset.phase,'completed');assert.equal(view.calls.some(c=>c.operation==='mix.daily.submit'),false);await view.click(view.get('quick-generate'));assert.equal(view.get('daily-progress').dataset.phase,'checking_login');
 await view.click(action(view,'复制执行信息'));assert.deepEqual(view.calls.find(c=>c.operation==='task.execution.copy').params,{id:'batch'});assert.equal(view.calls.filter(c=>c.operation==='mix.daily.submit').length,1);assert.equal(view.calls.some(c=>['mix.submit','mix.daily.continue'].includes(c.operation)),false);view.dispose();
});

test('a late recent-batch poll cannot replace an explicitly started video-only execution or its copy target',async()=>{
 let reads=0,finish;const view=await mountMixing(fixture(op=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null,recent_batch:{id:'previous',phase:'completed',can_continue:false}};
  if(op==='mix.daily.status')return ++reads===1?{id:'previous',phase:'completed',can_continue:false}:new Promise(resolve=>{finish=resolve;});
  if(op==='task.poll')return {epoch:'e',revision:1,tasks:[{id:'only',state:'running',quantity:10,succeeded:1,failed:0,completed:1}]};
  if(op==='task.execution.copy')return {copied:true};
 }));const pending=view.tick();await view.flush();await view.click(view.get('quick-only-generate'));assert.equal(view.get('daily-progress').dataset.phase,'running');finish({id:'previous',phase:'completed',can_continue:false});await pending;
 assert.equal(view.get('daily-progress').dataset.phase,'running');await view.click(action(view,'复制执行信息'));assert.deepEqual(view.calls.find(c=>c.operation==='task.execution.copy').params,{id:'only'});assert.equal(view.calls.filter(c=>c.operation==='mix.submit').length,1);assert.equal(view.calls.some(c=>c.operation==='mix.daily.continue'),false);view.dispose();
});

function delivery(id,state,extra={}){return {id,account_id:'account-a',platform:'douyin',platform_label:'抖音',account_label:'抖音 · 主号',title:'视频'+id,state,state_label:({platform_scheduled:'平台已接收定时安排',published:'已公开',reviewing:'平台已接收，审核中',waiting:'等待处理',result_unknown:'结果待核对',pre_submit_failed:'提交前未完成'})[state]||'处理中',reason:'',scheduled_at:'2026-10-17T04:00:00Z',...extra};}

test('the compact platform line aggregates five accounts into the four actual platforms without claiming public publication',async()=>{
 const targets=[delivery('b1','platform_scheduled',{platform:'bilibili',platform_label:'B站',account_id:'b1'}),delivery('b2','pre_submit_failed',{platform:'bilibili',platform_label:'B站',account_id:'b2',reason:'提交前超时'}),...['c1','c2'].map(id=>delivery(id,'platform_scheduled',{platform:'channels',platform_label:'视频号',account_id:'c'})),...['d1','d2'].map(id=>delivery(id,'pre_submit_failed',{reason:'提交前超时'})),...['k1','k2'].map(id=>delivery(id,'platform_scheduled',{platform:'kuaishou',platform_label:'快手',account_id:'k'}))];
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',accepted:5,pending:0,failed:3,total:8,execution:{publishing:{targets,complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));
 const results=node(view,'results');assert.equal(results.classList.contains('hide'),false);assert.deepEqual(results.querySelectorAll('.mixing-execution-platform').map(item=>item.textContent),['B站 已接收 1/2','视频号 已接收 2/2','抖音 已接收 0/2','快手 已接收 2/2']);assert.equal(results.querySelectorAll('details').length,0);assert.equal(results.querySelectorAll('li').length,0);assert.doesNotMatch(results.textContent,/视频b1|提交前超时|已公开|第五/);assert.equal(view.get('daily-progress').querySelector('.mixing-execution-results'),null);view.dispose();
});

test('compact account rows separate same-name accounts by platform and account ID and count only accepted targets',async()=>{
 const targets=[delivery('1','platform_scheduled',{account_id:'a',account_label:'抖音 · 同名'}),delivery('2','result_unknown',{account_id:'a',account_label:'抖音 · 同名'}),delivery('3','pre_submit_failed',{account_id:'b',account_label:'抖音 · 同名'}),delivery('4','reviewing',{platform:'channels',platform_label:'视频号',account_id:'a',account_label:'视频号 · 同名'})];
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',execution:{publishing:{targets,complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));const results=node(view,'results');assert.deepEqual(results.querySelectorAll('.mixing-execution-account').map(row=>row.textContent),['同名 已接收 1/2','同名 已接收 0/1','同名 已接收 1/1']);const rows=results.querySelectorAll('.mixing-execution-platform-row');assert.equal(rows.length,2);assert.equal(rows[0].querySelector('.mixing-execution-platform').textContent,'抖音 已接收 1/3');assert.equal(rows[0].querySelectorAll('.mixing-execution-account').length,2);assert.equal(rows[1].querySelector('.mixing-execution-platform').textContent,'视频号 已接收 1/1');assert.equal(rows[1].querySelectorAll('.mixing-execution-account').length,1);assert.equal(view.get('daily-progress').querySelectorAll('.mixing-execution-account').length,0);view.dispose();
});

test('account refresh reuses rows, removes stale accounts and renders names as safe text',async()=>{
 let targets=[delivery('1','waiting',{account_label:'抖音 · <img src=x onerror=bad()>'}),delivery('2','platform_scheduled',{account_id:'other',account_label:'抖音 · 旧号'})];const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'publishing',execution:{publishing:{targets,complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));const results=node(view,'results'),row=results.querySelector('.mixing-execution-account');assert.equal(results.querySelectorAll('img').length,0);assert.match(row.textContent,/<img src=x onerror=bad\(\)> 已接收 0\/1/);targets=[{...targets[0],state:'platform_scheduled'}];await view.tick();assert.equal(results.querySelector('.mixing-execution-account'),row);assert.match(row.textContent,/已接收 1\/1/);assert.equal(results.querySelectorAll('.mixing-execution-account').length,1);assert.doesNotMatch(results.textContent,/旧号/);view.dispose();
});

test('only actual received states enter platform progress while pending, unknown and failed targets remain in the denominator',async()=>{
 const targets=['platform_scheduled','published','platform_accepted','processing','reviewing','waiting','unknown','pre_submit_failed'].map((state,index)=>delivery(String(index),state));const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',total:8,execution:{publishing:{targets,complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));assert.equal(node(view,'results').querySelector('.mixing-execution-platform').textContent,'抖音 已接收 5/8');assert.doesNotMatch(node(view,'results').textContent,/已公开|全部成功/);view.dispose();
});

test('platform progress refresh updates counts without replacing unchanged display or changing log scroll',async()=>{
 let targets=[delivery('one','platform_scheduled'),delivery('two','waiting')];const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'publishing',total:2,execution:{publishing:{targets,complete:true,omitted:0},events:[logEvent()]}}:undefined));await view.click(view.get('quick-generate'));const result=node(view,'results'),platform=result.querySelector('.mixing-execution-platform'),logs=node(view,'events');logs.scrollTop=42;await view.tick();assert.equal(result.querySelector('.mixing-execution-platform'),platform);assert.equal(logs.scrollTop,42);targets=[targets[0],delivery('two','platform_scheduled')];await view.tick();assert.equal(result.querySelector('.mixing-execution-platform').textContent,'抖音 已接收 2/2');assert.equal(logs.scrollTop,42);view.dispose();
});

test('partial platform progress is explicit and legacy or video-only responses hide the compact line',async()=>{
 let publishing={targets:[delivery('one','platform_scheduled')],complete:false,omitted:2};const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',total:8,execution:{...(publishing?{publishing}:{})}}:undefined));await view.click(view.get('quick-generate'));const results=node(view,'results');assert.match(results.textContent,/部分记录暂不可读取/);assert.match(results.textContent,/另2项请查看本批任务/);assert.match(results.textContent,/已接收 1\/1/);assert.doesNotMatch(results.textContent,/已接收 8|全部已发布/);publishing=null;await view.tick();assert.equal(results.classList.contains('hide'),true);view.dispose();
 const only=await mountMixing(fixture(op=>op==='task.poll'?{epoch:'e',revision:1,tasks:[{id:'only',state:'running',quantity:10,completed:0,succeeded:0,failed:0,execution:{publishing:{targets:[delivery('unexpected','published')],complete:true,omitted:0}}}]}:undefined));await only.click(only.get('quick-only-generate'));assert.equal(node(only,'results').classList.contains('hide'),true);only.dispose();
});

test('settled platform data suppresses the duplicate generic timeout but keeps independent errors and the task detail action',async()=>{
 let reason='本次处理超时，请按当前目标状态核对原操作。';const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',reason,total:1,execution:{current:{label:'发布处理结束'},publishing:{targets:[delivery('one','pre_submit_failed',{reason:'提交前处理超时，尚未提交平台。'})],complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));assert.equal(node(view,'reason').classList.contains('hide'),true);assert.equal(node(view,'current').classList.contains('hide'),true);await view.click(action(view,'查看本批任务'));assert.equal(view.context.P011Router.pendingTaskId,'batch');assert.deepEqual(view.routes.at(-1),['tasks']);reason='本批发布安排已过期，请核对任务记录。';await view.tick();assert.equal(node(view,'reason').textContent,reason);view.dispose();
});

test('compact platform progress follows batch timing and never duplicates in the execution log panel',async()=>{
 const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'needs_attention',total:1,execution:{publishing:{targets:[delivery('one','pre_submit_failed')],complete:true,omitted:0}}}:undefined));await view.click(view.get('quick-generate'));const results=node(view,'results'),summary=view.get('mixing-batch-progress'),time=summary.querySelector('.batch-progress-time');assert.equal(summary.contains(results),true);assert.equal(view.get('daily-progress').contains(results),false);assert.ok(summary.children.indexOf(results)>summary.children.indexOf(time));assert.equal(view.document.querySelectorAll('.mixing-execution-results').length,1);view.dispose();
});

test('a standalone execution component without a progress host keeps the compact platform line available',async()=>{
 const view=await mountMixing(fixture()),host=view.document.createElement('div'),el=(tag,text,cls)=>{const node=view.document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;},button=(text,fn)=>{const node=el('button',text);node.onclick=fn;return node;};const panel=view.context.P011MixExecution({host,el,button,onDetails(){},onContinue(){},async onCopy(){}});panel.render({batch:{id:'standalone'},status:{phase:'accepted',total:1,execution:{publishing:{targets:[delivery('one','platform_scheduled')],complete:true,omitted:0}}}});const results=host.querySelector('.mixing-execution-results');assert.equal(results.classList.contains('hide'),false);assert.match(results.textContent,/已接收 1\/1/);panel.dispose();view.dispose();
});

 test('clearing execution records preserves the batch, ignores old polls and shows new records',async()=>{
 let events=[logEvent('旧记录')];const view=await mountMixing(fixture(op=>op==='mix.daily.status'?{phase:'making',total:10,execution:{events}}:undefined));
 assert.equal(action(view,'清空记录').disabled,true);await view.click(view.get('quick-generate'));
 const calls=view.calls.length;await view.click(action(view,'清空记录'));assert.equal(view.calls.length,calls);assert.equal(node(view,'events').querySelectorAll('li').length,0);assert.equal(action(view,'清空记录').disabled,true);
 await view.tick();assert.equal(node(view,'events').querySelectorAll('li').length,0);assert.match(node(view,'phase').textContent,/正在制作/);
 events=[...events,logEvent('新记录')];await view.tick();assert.equal(node(view,'events').querySelectorAll('li').length,1);assert.match(node(view,'events').textContent,/新记录/);assert.doesNotMatch(node(view,'events').textContent,/旧记录/);
 await view.click(action(view,'清空记录'));await view.tick();assert.equal(node(view,'events').querySelectorAll('li').length,0);
 events=[...events,logEvent('新记录')];await view.tick();assert.equal(node(view,'events').querySelectorAll('li').length,1);view.dispose();
 });
