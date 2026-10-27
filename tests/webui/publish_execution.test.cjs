const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
// This contract harness tests the shipped module, not browser layout.
class Node{
 constructor(tag,text='',cls=''){this.tagName=tag.toUpperCase();this.children=[];this.parentElement=null;this.className=cls;this._text=String(text??'');this.attrs={};this.dataset={};this.hidden=false;this.isConnected=true;this.scrollTop=0;this.classList={contains:c=>this.className.split(/\s+/).includes(c),add:c=>{if(!this.classList.contains(c))this.className+=' '+c;},remove:c=>{this.className=this.className.split(/\s+/).filter(x=>x!==c).join(' ');},toggle:(c,yes)=>{yes= yes===undefined?!this.classList.contains(c):yes;this.classList[yes?'add':'remove'](c);return yes;}};}
 get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}set textContent(v){this._text=String(v??'');this.replaceChildren();}
 append(...nodes){for(const n of nodes){n.remove();n.parentElement=this;this.children.push(n);}}replaceChildren(...nodes){this.children.forEach(n=>n.parentElement=null);this.children=[];this.append(...nodes);}remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(n=>n!==this);this.parentElement=null;}
 setAttribute(k,v){this.attrs[k]=String(v);}removeAttribute(k){delete this.attrs[k];}querySelectorAll(s){return this.children.flatMap(n=>[...((s.startsWith('.')?n.classList.contains(s.slice(1)):n.tagName===s.toUpperCase())?[n]:[]),...n.querySelectorAll(s)]);}querySelector(s){return this.querySelectorAll(s)[0]||null;}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function mount(api=async()=>{throw Error('No API call expected');},options={}){const timers=new Map(),events=new Map(),calls=[],routes=[],notices=[],host=new Node('section'),summaryHost=new Node('section'),renders=[],mixOptions=[];let next=0;const document={hidden:false,visibilityState:'visible',addEventListener:(kind,fn)=>{(events.get(kind)||events.set(kind,new Set()).get(kind)).add(fn);},removeEventListener:(kind,fn)=>events.get(kind)?.delete(fn)};
 const context={document,Date,Intl,setTimeout:(fn,delay)=>{timers.set(++next,{fn,delay});return next;},clearTimeout:id=>timers.delete(id)};
 const assets=path.resolve(__dirname,'../../src/yijianjifa/webui/assets');vm.runInNewContext(fs.readFileSync(path.join(assets,'mixing_execution.js'),'utf8'),context);const shared=context.P011MixExecution;context.P011MixExecution=options=>{mixOptions.push(options);const component=shared(options);return {...component,render(value){renders.push(value);return component.render(value);}};};vm.runInNewContext(fs.readFileSync(path.join(assets,'publish_execution.js'),'utf8'),context);
 const el=(tag,text,cls)=>{const n=new Node(tag,text,cls);if(options.nodeList&&cls==='publish-execution-content'){const query=n.querySelectorAll.bind(n);n.querySelectorAll=selector=>{const nodes=query(selector);return Object.assign({length:nodes.length,[Symbol.iterator]:()=>nodes[Symbol.iterator]()},nodes);};}return n;},button=(text,fn,cls)=>{const n=el('button',text,cls);n.onclick=fn;return n;};
 const progress=context.P011PublishExecution.create({el,button,host,summaryHost,...options,api:async(op,params)=>{calls.push({op,params});return api(op,params);},onTask:id=>routes.push(id),notice:message=>notices.push(message)});
 const names={phase:'mixing-execution-phase',counts:'batch-progress-publishing',meter:'batch-progress-publishing-bar',current:'mixing-execution-current',reason:'mixing-execution-reason',groups:'mixing-execution-results',events:'mixing-execution-events',actions:'mixing-execution-actions'};
 return {progress,host,summaryHost,calls,routes,notices,timers,document,renders,mixOptions,node:name=>(name==='counts'?summaryHost.querySelector('.'+names[name])?.querySelector('p'):(host.querySelector('.'+names[name])||summaryHost.querySelector('.'+names[name]))),async tick(){const entry=[...timers].find(([,t])=>t.delay===1200);if(entry){timers.delete(entry[0]);await entry[1].fn();}await flush();},async visibility(hidden){document.hidden=hidden;document.visibilityState=hidden?'hidden':'visible';for(const fn of events.get('visibilitychange')||[])await fn();await flush();},listenerCount:()=>events.get('visibilitychange')?.size||0};}

function target(id,state,extra={}){return {id,platform:'channels',account_id:'a',account_label:'逆浪风',state,accepted:true,worker_running:false,execution_log:[],...extra};}
function multi(deliveries,extra={}){return {id:'batch',quantity:deliveries.length,deliveries,...extra};}
function ordinary(items,extra={}){return {task:{id:'normal',kind:'publish',state:'running',quantity:items.length,account:'主号',...extra},settings:{platform:'douyin',kind:'normal_publish'},items:items.map((item,index)=>({index,account_id:'a',...item}))};}

test('visible publishing logs keep business stages once and omit internal browser checks',()=>{
 const steps=['execution.start','douyin.driver_start','douyin.browser_launch','douyin.identity',
  'douyin.upload','douyin.form_read','submission.final_check','submission.authorized','douyin.final_click',
  'douyin.result_read','execution.result','browser.close'];
 const events=steps.map(step=>({time:'2026-10-25T01:00:00Z',step,checks:{state:'platform_scheduled'}}));
 const v=mount();v.progress.multi(multi([target('1','platform_scheduled',{execution_log:events})]));
 const messages=v.renders.at(-1).status.execution.events.map(e=>e.message);
 assert.equal(messages.length,4);
 assert.equal(messages.filter(m=>/提交发布/.test(m)).length,1);
 assert.doesNotMatch(messages.join(' '),/组件|浏览器|入口|表单|授权|核对发布账号|读取平台/);
 v.progress.dispose();
});

test('failure summary identifies the platform and exact account even when nicknames are shared',async()=>{
 const v=mount();
 v.progress.multi(multi([target('bad','platform_rejected',{platform:'bilibili',account_label:'逆浪风',reason_message:'今日投稿次数已达到上限'})]));
 assert.match(v.renders.at(-1).status.reason,/B站 · 逆浪风：今日投稿次数已达到上限/);
 assert.doesNotMatch(v.renders.at(-1).status.reason,/视频号/);
 assert(v.calls.every(c=>c.op==='multi_publish.task.detail'));v.progress.dispose();
});
const event=(step,time='2026-10-25T01:00:00Z',checks={})=>({step,time,checks});

test('idle and pending never discover or resume global old tasks, and pending does not invent totals',()=>{const v=mount();assert.match(v.host.textContent,/执行进度/);assert.match(v.node('phase').textContent,/尚未开始/);assert.equal(v.calls.length,0);v.progress.pending('fresh','本次安排已保存');assert.match(v.node('current').textContent,/本次安排已保存/);assert.equal(v.node('meter').classList.contains('hide')||v.node('meter').parentElement.classList.contains('hide'),true);assert.doesNotMatch(v.host.textContent,/0\/0|全部完成/);assert.equal(v.calls.length,0);v.progress.dispose();});

test('multi counts only shared received states and ignores local accepted grants',()=>{const states=['published','platform_scheduled','platform_accepted','processing','reviewing','waiting','result_unknown','interrupted','pre_submit_failed'];const v=mount();v.progress.multi(multi(states.map((s,i)=>target(String(i),s))));assert.equal(v.node('counts').textContent,'发布：平台已接收 5/9 项 · 错误 1 项 · 等待核对 2 项');assert.equal(v.node('meter').attrs.value,'5');assert.equal(v.node('meter').attrs.max,'9');assert.match(v.node('phase').textContent,/待核对|需处理/);assert.equal(v.calls.length,0);assert.equal(v.timers.size,0);assert.doesNotMatch(v.host.textContent,/全部已发布|实际已发布/);v.progress.dispose();});

test('all received can finish local submission without claiming public publication',()=>{const v=mount();v.progress.multi(multi([target('1','published'),target('2','reviewing')]));assert.match(v.node('phase').textContent,/已全部提交平台/);assert.equal(v.node('meter').attrs.value,'2');assert.doesNotMatch(v.host.textContent,/全部已发布|发布成功/);v.progress.dispose();});

test('unknown, duplicate, malformed and partial records cannot make a full meter',()=>{const v=mount();v.progress.multi(multi([target('1','platform_accepted')],{quantity:2}));assert.equal(v.node('meter').classList.contains('hide'),false);assert.equal(v.node('meter').attrs.max,'2');assert.equal(v.node('meter').attrs.value,'1');assert.match(v.node('reason').textContent,/部分|读取/);v.progress.multi(multi([target('1','platform_accepted'),target('1','published')]));assert.notEqual(v.node('meter').attrs.value,v.node('meter').attrs.max);assert.doesNotMatch(v.node('phase').textContent,/已全部提交平台/);v.progress.multi(multi([target('1','unrecognized',{accepted:true})]));assert.equal(v.node('meter').attrs.value,'0');v.progress.dispose();});

test('platform and account rows separate same names safely and retain nodes through unchanged updates',()=>{const targets=[target('1','reviewing',{account_label:'<img src=x onerror=bad()>'}),target('2','waiting',{account_id:'b',account_label:'<img src=x onerror=bad()>'}),target('3','platform_scheduled',{platform:'bilibili',account_label:'逆浪风'})],v=mount();v.progress.multi(multi(targets));const rows=v.host.querySelectorAll('.mixing-execution-account');assert.equal(rows.length,3);assert.equal(v.host.querySelectorAll('img').length,0);assert.match(rows[0].textContent,/<img src=x onerror=bad\(\)> 已接收 1\/1/);assert.deepEqual(v.host.querySelectorAll('.mixing-execution-platform').map(n=>n.textContent),['视频号 已接收 1/2','B站 已接收 1/1']);v.progress.multi(multi(targets));assert.equal(v.host.querySelector('.mixing-execution-account'),rows[0]);v.progress.dispose();});

test('execution events are once per actual delivery step and unchanged polls retain rows and scroll',()=>{const v=mount(),one=target('1','running',{worker_running:true,execution_log:[event('channels.upload'),event('channels.upload','2026-10-25T01:00:01Z'),event('channels.form_read'),event('unknown.secret'),event('channels.result','bad')]});v.progress.multi(multi([one]));const events=v.node('events'),rows=events.querySelectorAll('li');assert.equal(rows.length,1);assert.match(events.textContent,/上传视频/);assert.doesNotMatch(events.textContent,/读取发布表单/);assert.doesNotMatch(events.textContent,/secret|bad/);events.scrollTop=37;v.progress.multi(multi([one]));assert.equal(events.querySelector('li'),rows[0]);assert.equal(events.scrollTop,37);v.progress.multi(multi([{...one,state:'reviewing',execution_log:[...one.execution_log,event('execution.result','2026-10-25T01:02:00Z',{state:'reviewing'}),event('execution.result','2026-10-25T01:02:01Z',{state:'reviewing'})]}]));assert.equal(events.querySelectorAll('li').length,2);v.progress.dispose();});

test('ordinary polls only exact current detail and matching transient stage, never stale global task',async()=>{let live={id:'other',stage:'正在上传并填写发布资料',running:true,updated_at:1792886400};const v=mount(async(op,params)=>{if(op==='task.center.detail'){assert.equal(params.id,'normal');return ordinary([{state:'reviewing'},{state:'waiting'}]);}if(op==='publish.progress.status')return {task:live};throw Error(op);});await v.progress.ordinary('normal');assert.equal(v.node('counts').textContent,'发布：平台已接收 1/2 项');assert.doesNotMatch(v.node('current').textContent,/正在上传/);live={id:'normal',stage:'正在上传并填写发布资料',index:2,title:'<img src=x>',running:true,updated_at:1792886400};await v.tick();assert.match(v.node('current').textContent,/正在上传并填写发布资料/);assert.match(v.node('current').textContent,/第2(?:\/2)?条/);assert.equal(v.host.querySelectorAll('img').length,0);assert.equal(v.node('events').querySelectorAll('li').length,1);live.updated_at++;await v.tick();assert.equal(v.node('events').querySelectorAll('li').length,1);assert(v.calls.every(c=>['task.center.detail','publish.progress.status'].includes(c.op)));v.progress.dispose();});

test('ordinary failed reads preserve previous evidence and recover without toasts or fabricated completion',async()=>{let failed=false;const v=mount(async op=>{if(op==='publish.progress.status')return {task:null};if(failed)throw Error('secret token');return ordinary([{state:'platform_scheduled'},{state:'result_unknown'}]);});await v.progress.ordinary('normal');assert.equal(v.node('meter').attrs.value,'1');failed=true;await v.tick();assert.equal(v.node('meter').attrs.value,'1');assert.match(v.node('reason').textContent,/读取|保留/);assert.doesNotMatch(v.host.textContent,/secret token/);failed=false;await v.tick();assert.doesNotMatch(v.node('reason').textContent,/暂时无法读取/);v.progress.dispose();});

test('ordinary rejects mismatched detail identity and holds paused local task without auto execution',async()=>{let mismatch=false;const v=mount(async op=>op==='publish.progress.status'?{task:null}:ordinary([{state:'paused'}],{id:mismatch?'other':'normal',state:'paused'}));await v.progress.ordinary('normal');assert.match(v.node('phase').textContent,/尚未开始|已暂停/);mismatch=true;await v.tick();assert.match(v.node('reason').textContent,/读取/);assert.equal(v.node('meter').attrs.value,'0');assert(v.calls.every(c=>['task.center.detail','publish.progress.status'].includes(c.op)));v.progress.dispose();});

test('hidden or detached page pauses API reads, visibility resumes only bound ordinary task',async()=>{const v=mount(async op=>op==='publish.progress.status'?{task:null}:ordinary([{state:'waiting'}]));await v.progress.ordinary('normal');const count=v.calls.length;await v.visibility(true);await v.tick();assert.equal(v.calls.length,count);await v.visibility(false);assert.equal(v.calls.length,count+2);v.host.isConnected=false;await v.tick();assert.equal(v.calls.length,count+2);v.progress.dispose();assert.equal(v.listenerCount(),0);});

test('switching batches or disposing ignores late reads and stops all polling',async()=>{let release;const v=mount(async op=>op==='publish.progress.status'?{task:null}:new Promise(resolve=>release=resolve));const pending=v.progress.ordinary('normal');await flush();v.progress.pending('new','已受理');v.progress.multi(multi([target('1','reviewing')],{id:'new'}));release(ordinary([{state:'waiting'}]));await pending;assert.equal(v.host.dataset.taskId,'new');assert.equal(v.node('counts').textContent,'发布：平台已接收 1/1 项');assert.equal(v.timers.size,0);v.progress.dispose();v.progress.multi(multi([target('1','waiting')]));assert.equal(v.host.dataset.taskId,'new');assert.equal(v.timers.size,0);});

test('task action only navigates to bound task and idle cancels stale tracking',async()=>{const v=mount();v.progress.multi(multi([target('1','result_unknown')]));const buttons=v.host.querySelectorAll('button'),details=buttons.find(n=>n.textContent==='查看本批任务');assert.deepEqual(buttons.filter(n=>!n.classList.contains('hide')).map(n=>n.textContent),['查看待核对任务','查看本批任务','复制执行信息','清空记录']);assert.equal(details.textContent,'查看本批任务');await details.onclick();assert.deepEqual(v.routes,['batch']);v.progress.idle();assert.equal(details.classList.contains('hide'),true);await details.onclick();assert.deepEqual(v.routes,['batch']);assert.equal(v.calls.length,0);v.progress.dispose();});

test('a stale multi detail cannot replace a newer explicitly accepted batch',()=>{const v=mount();v.progress.pending('new','已受理');v.progress.multi(multi([target('1','reviewing')],{id:'new'}));v.progress.multi(multi([target('old','waiting')],{id:'old'}));assert.equal(v.host.dataset.taskId,'new');assert.equal(v.node('counts').textContent,'发布：平台已接收 1/1 项');v.progress.pending('next','已受理');v.progress.multi(multi([target('2','waiting')],{id:'next'}));assert.equal(v.host.dataset.taskId,'next');assert.equal(v.node('counts').textContent,'发布：平台已接收 0/1 项');v.progress.dispose();});

test('ambiguous duplicate records at the declared total cannot display a full meter',()=>{const v=mount();v.progress.multi(multi([target('1','reviewing'),target('1','published')],{quantity:1}));assert.equal(v.node('meter').classList.contains('hide')||v.node('meter').parentElement.classList.contains('hide'),true);assert.doesNotMatch(v.node('phase').textContent,/已全部提交平台/);assert.match(v.node('reason').textContent,/部分/);v.progress.dispose();});

test('the received meter follows the backend shared completion set including public works',()=>{const source=fs.readFileSync(path.resolve(__dirname,'../../src/yijianjifa/features/publish/outcomes.py'),'utf8'),set=source.match(/COMPLETED_DELIVERY_STATES\s*=\s*frozenset\(\{([\s\S]*?)\}\)/)[1],states=[...set.matchAll(/'([a-z_]+)'/g)].map(m=>m[1]),v=mount();v.progress.multi(multi(states.map((state,i)=>target(String(i),state))));assert.equal(v.node('meter').attrs.value,String(states.length));assert.match(v.node('phase').textContent,/已全部提交平台/);v.progress.dispose();});

test('disposing during a read ignores its result and leaves no timers or handlers',async()=>{let release;const v=mount(async op=>op==='publish.progress.status'?{task:null}:new Promise(resolve=>release=resolve));const pending=v.progress.ordinary('normal');await flush();const text=v.host.textContent;v.progress.dispose();release(ordinary([{state:'published'}]));await pending;assert.equal(v.host.textContent,text);assert.equal(v.timers.size,0);assert.equal(v.listenerCount(),0);});

test('publisher is a publish-only adapter of the actual shared mixing execution renderer',()=>{const v=mount();assert.equal(v.mixOptions.length,1);assert.equal(v.mixOptions[0].publishOnly,true);assert.equal(v.mixOptions[0].summaryHost,v.summaryHost);v.progress.multi(multi([target('1','running',{worker_running:true})]));const value=v.renders.at(-1);assert(value);assert.equal(Object.hasOwn(value.status.execution,'making'),false);assert.equal(value.canContinue,false);assert.equal(value.canRepair,false);assert.equal(v.host.querySelector('.mixing-execution-making').classList.contains('hide'),true);assert.equal(v.summaryHost.querySelector('.batch-progress-making').classList.contains('hide'),true);assert.equal(v.host.querySelector('progress'),null);assert(v.summaryHost.querySelector('.batch-progress-publishing-bar'));v.progress.dispose();});

test('shared copy reads the exact batch native log, prevents duplicates and reports success',async()=>{let release;const v=mount(async(op,params)=>{assert.equal(op,'task.execution.copy');assert.equal(params.id,'batch');return new Promise(resolve=>release=resolve);});v.progress.multi(multi([target('1','result_unknown')]));const copy=v.host.querySelectorAll('button').find(n=>n.textContent==='复制执行信息'),pending=copy.onclick();await flush();copy.onclick();assert.equal(v.calls.length,1);release({copied:true});await pending;assert.match(v.notices.at(-1),/执行信息已复制/);assert.equal(v.calls.some(c=>/start|submit|resume|reconcile/.test(c.op)),false);v.progress.dispose();});

test('shared clear only clears displayed events and never changes a task or re-adds old steps',()=>{const v=mount(),one=target('1','running',{execution_log:[event('channels.upload')]});v.progress.multi(multi([one]));assert.equal(v.node('events').querySelectorAll('li').length,1);const clear=v.host.querySelectorAll('button').find(n=>n.textContent==='清空记录');clear.onclick();assert.equal(v.node('events').querySelectorAll('li').length,0);v.progress.multi(multi([one]));assert.equal(v.node('events').querySelectorAll('li').length,0);v.progress.multi(multi([{...one,execution_log:[...one.execution_log,event('submission.authorized','2026-10-25T01:02:00Z')]}]));assert.equal(v.node('events').querySelectorAll('li').length,1);assert.match(v.node('events').textContent,/提交发布/);assert.equal(v.calls.length,0);v.progress.dispose();});

test('bottom summary uses only recorded start/activity times and never invents making totals',()=>{const v=mount();v.progress.multi(multi([target('1','reviewing',{execution_log:[event('execution.start','2026-10-25T01:00:00Z'),event('execution.result','2026-10-25T01:01:08Z',{state:'reviewing'})]})],{created_at:'2026-10-25T00:59:00Z'}));assert.match(v.summaryHost.querySelector('.batch-progress-started').textContent,/开始时间：10月25日09:00:00/);assert.match(v.summaryHost.querySelector('.batch-progress-elapsed').textContent,/00:01:08/);assert.equal(v.summaryHost.querySelector('.batch-progress-making').classList.contains('hide'),true);assert.doesNotMatch(v.host.textContent,/制作：成功 0\/0/);v.progress.dispose();});

test('ordinary keeps reading while a fully received batch native worker is still closing',async()=>{let running=true;const v=mount(async op=>op==='publish.progress.status'?{task:{id:'normal',stage:'正在核对提交结果',index:1,running,updated_at:1792886400}}:ordinary([{state:'platform_accepted'}]));await v.progress.ordinary('normal');assert.equal(v.node('meter').attrs.value,'1');assert.match(v.node('phase').textContent,/运行中/);assert.equal([...v.timers.values()].some(t=>t.delay===1200),true,'Received works do not prove that the native worker has stopped');running=false;await v.tick();assert.match(v.node('phase').textContent,/已全部提交平台/);assert.equal([...v.timers.values()].some(t=>t.delay===1200),false);v.progress.dispose();});


test('compact progress tracks the visible action row, keeps errors visible and opens the bound task',async()=>{
 let failed=false;const v=mount(async op=>{if(op==='publish.progress.status')return {task:null};if(failed)throw Error('private');return ordinary([{state:'waiting'}]);},{compact:true,nodeList:true});
 v.host.isConnected=false;await v.progress.ordinary('normal');assert.equal(v.calls.length,2);
 const details=v.summaryHost.querySelectorAll('button').find(n=>n.textContent==='查看本批任务');assert(details);await details.onclick();assert.deepEqual(v.routes,['normal']);
 failed=true;await v.tick();assert.match(v.summaryHost.textContent,/暂时无法读取/);assert.doesNotMatch(v.summaryHost.textContent,/private/);
 failed=false;await v.tick();const error=v.summaryHost.querySelector('.publish-progress-error');assert(error.hidden||!error.textContent);
 const count=v.calls.length;v.summaryHost.isConnected=false;await v.tick();assert.equal(v.calls.length,count);v.progress.dispose();
});


test('compact first read failure preserves the acknowledged task action without claiming it has not started',async()=>{
 const v=mount(async()=>{throw Error('offline');},{compact:true,nodeList:true});await v.progress.ordinary('acknowledged');
 const details=v.summaryHost.querySelectorAll('button').find(n=>n.textContent==='查看本批任务');assert.equal(details.classList.contains('hide'),false);
 assert.match(v.summaryHost.textContent,/暂时无法读取/);assert.doesNotMatch(v.summaryHost.querySelector('.batch-progress-phase').textContent,/等待开始|尚未开始/);await details.onclick();assert.deepEqual(v.routes,['acknowledged']);v.progress.dispose();
});

test('finished unknown result and same account queue show pending verification and deferred count',()=>{
 const v=mount(),blocked={delivery_id:'unknown',state:'result_unknown'};
 v.progress.multi(multi([target('accepted','platform_scheduled'),target('unknown','result_unknown',{worker_running:true,execution_active:false}),...Array.from({length:16},(_,i)=>target('wait'+i,'waiting',{blocked_by:blocked,reason_message:'同一账号有发布结果尚未确认，本目标暂缓。'}))]));
 assert.match(v.node('phase').textContent,/等待核对原结果.*16项暂缓/);
 assert.doesNotMatch(v.node('phase').textContent,/运行中|其余目标继续/);
 assert.equal(v.renders.at(-1).status.execution.timing.active,false);
 assert.equal(v.calls.length,0);v.progress.dispose();
});

test('another account actually running keeps its own scope while unresolved account queue is deferred',()=>{
 const v=mount();v.progress.multi(multi([target('unknown','result_unknown',{worker_running:true,execution_active:false}),target('wait','waiting',{blocked_by:{delivery_id:'unknown',state:'result_unknown'}}),target('other','running',{account_id:'b',account_label:'另一个账号',execution_active:true,worker_running:true})]));
 assert.match(v.node('phase').textContent,/运行中.*其余账号正在处理.*1项暂缓待核对/);
 assert.match(v.node('current').textContent,/另一个账号/);
 assert.equal(v.renders.at(-1).status.execution.timing.active,true);v.progress.dispose();
});

test('最终提交仍在读取结果时不会误称其他账号正在处理',()=>{
 const v=mount();v.progress.multi(multi([target('submitting','result_unknown',{execution_active:true,worker_running:true}),target('wait','waiting',{blocked_by:{delivery_id:'submitting',state:'result_unknown'}})]));
 assert.match(v.node('phase').textContent,/运行中.*正在核对提交结果.*1项等待同一账号/);
 assert.doesNotMatch(v.node('phase').textContent,/其余账号/);v.progress.dispose();
});
