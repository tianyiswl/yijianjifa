const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const timing={started_at:'2026-10-25T04:00:00Z',last_activity_at:'2026-10-25T04:00:40Z',active:false};
async function fixture(){
 const v=await mountMixing(async(op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[],capabilities:{},has_saved_settings:false,active_batch:null};
  if(op==='mix.daily.save')return {saved:true};if(op==='preset.mix.list')return {items:[],total:0};
  if(op==='settings.output.read')return {directory:'/tmp/finished'};if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
  if(op==='category.list')return {items:[],total:0};if(op==='library.list')return {items:[],total:0};if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
 },null,{},{now:'2026-10-25T04:01:00Z'});
 const el=(tag,text,cls)=>{const n=v.document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;},button=(text,fn)=>{const n=el('button',text);n.onclick=fn;return n;},host=el('section'),summary=el('section'),actions=[];
 const panel=v.context.P011MixExecution({host,summaryHost:summary,el,button,publishOnly:true,onDetails:b=>actions.push(['details',b.id]),onContinue:b=>actions.push(['continue',b.id]),onRepairLogin:b=>actions.push(['repair',b.id]),onCopy:async b=>actions.push(['copy',b.id])});
 return {v,host,summary,panel,actions,node:name=>host.querySelector('.mixing-execution-'+name),summaryNode:name=>summary.querySelector('.batch-progress-'+name),render:(status,extra={})=>panel.render({batch:{id:'publish'},status,...extra}),dispose(){panel.dispose();v.dispose();}};
}
const target=(state,id='one',extra={})=>({id,platform:'douyin',platform_label:'抖音',account_id:'a',account_label:'主号',state,...extra});
function status(targets,extra={}){const accepted=targets.filter(t=>['published','platform_scheduled','platform_accepted','processing','reviewing'].includes(t.state)).length;return {phase:'completed',total:targets.length,accepted,pending:0,failed:targets.length-accepted,execution:{publishing:{targets,complete:true,omitted:0},timing:{...timing}},...extra};}
function hidden(node){return node.classList.contains('hide');}

test('publication-only idle and running reuse the shared panel without making text, bars or continuation actions',async()=>{
 const f=await fixture();assert.doesNotMatch(f.host.textContent,/制作/);assert.match(f.node('hint').textContent,/平台提交/);
 const s=status([target('running')],{phase:'publishing',pending:1,failed:0});s.execution.timing.active=true;f.render(s,{canContinue:true,canRepair:true});
 assert.equal(f.host.dataset.runState,'running');assert.match(f.node('phase').textContent,/正在提交平台/);assert.equal(hidden(f.node('making')),true);assert.equal(hidden(f.summaryNode('making')),true);assert.doesNotMatch(f.summary.textContent,/制作/);assert.equal(hidden(f.node('account-help')),true);
 assert(f.host.querySelectorAll('button').filter(n=>/登录并继续|登录账号/.test(n.textContent)).every(hidden));assert.deepEqual(f.actions,[]);f.dispose();
});

test('complete publication evidence ends the shared run without fabricating making results or public success',async()=>{
 const f=await fixture(),targets=['published','platform_scheduled','platform_accepted','processing','reviewing'].map((s,i)=>target(s,String(i)));f.render(status(targets));
 assert.equal(f.host.dataset.runState,'completed');assert.equal(f.summary.dataset.runState,'completed');assert.equal(f.node('phase').textContent,'本批运行已结束 · 已全部提交平台');assert.match(f.summaryNode('publishing').textContent,/平台已接收 5\/5/);assert.deepEqual([f.summaryNode('publishing-bar').attrs.value,f.summaryNode('publishing-bar').attrs.max],['5','5']);assert.doesNotMatch(f.host.textContent+f.summary.textContent,/制作|全部已发布|已公开/);assert.match(f.summaryNode('started').textContent,/10月25日12:00:00/);assert.match(f.summaryNode('elapsed').textContent,/00:00:40.*截至最后记录/);assert.equal(f.summary.querySelector('.mixing-execution-results'),null);assert.equal(f.host.contains(f.node('results')),true);f.dispose();
});

test('publication-only failed and unknown targets remain distinct from completed receipts',async()=>{
 const f=await fixture();f.render(status([target('platform_accepted'),target('pre_submit_failed','two')]));assert.equal(f.host.dataset.runState,'attention');assert.equal(f.node('phase').textContent,'本批运行已结束 · 1项需处理');
 f.render(status([target('result_unknown')]));assert.equal(f.host.dataset.runState,'waiting');assert.match(f.node('phase').textContent,/等待核对原结果/);assert.doesNotMatch(f.node('phase').textContent,/已全部提交/);f.dispose();
});

test('publication-only verification remains neutral without requiring fictitious making counts',async()=>{
 const f=await fixture(),s=status([target('platform_accepted'),target('paused_auth','two',{needs_verification:true})],{phase:'needs_attention',reason:'verification_required'});f.render(s,{canContinue:true,canRepair:true});assert.equal(f.host.dataset.runState,'verification');assert.equal(f.node('phase').textContent,'等待完成平台验证');assert.equal(f.node('reason').attrs.role,'status');assert.equal(hidden(f.node('reason')),true);assert.match(f.summaryNode('publishing').textContent,/平台已接收 1\/2 项 · 等待验证 1 项/);assert.equal(hidden(f.node('account-help')),true);assert.doesNotMatch(f.host.textContent+f.summary.textContent,/制作/);f.dispose();
});

for(const issue of ['missing-targets','partial','omitted','mismatched-total','mismatched-receipts','mismatched-failures','duplicate-targets','malformed-target','missing-active','pending','zero','active'])test(`publication-only ${issue} cannot claim completion`,async()=>{
 const f=await fixture(),s=status([target('platform_accepted')]);
 if(issue==='missing-targets')delete s.execution.publishing;
 if(issue==='partial')s.execution.publishing.complete=false;
 if(issue==='omitted')s.execution.publishing.omitted=1;
 if(issue==='mismatched-total')s.total=2;
 if(issue==='mismatched-receipts')s.accepted=0;
 if(issue==='mismatched-failures')s.failed=1;
 if(issue==='duplicate-targets'){s.execution.publishing.targets.push({...s.execution.publishing.targets[0]});s.total=2;s.accepted=2;}
 if(issue==='malformed-target')delete s.execution.publishing.targets[0].account_id;
 if(issue==='missing-active')delete s.execution.timing.active;
 if(issue==='pending')s.pending=1;
 if(issue==='zero'){Object.assign(s,{total:0,accepted:0,failed:0});s.execution.publishing.targets=[];}
 if(issue==='active')s.execution.timing.active=true;
 f.render(s);assert.notEqual(f.host.dataset.runState,'completed');assert.doesNotMatch(f.node('phase').textContent,/已全部提交平台|运行已结束/);assert.doesNotMatch(f.host.textContent+f.summary.textContent,/制作/);f.dispose();
});

test('publication-only pending reads and absent timestamps do not invent zero counts, making data or time',async()=>{
 const f=await fixture();f.render({phase:'waiting',execution:{current:{label:'已保存，正在读取本批进度'},publishing:{targets:[],complete:false,omitted:0}}});assert.equal(f.node('phase').textContent,'等待开始');assert.match(f.node('current').textContent,/正在读取本批进度/);assert.doesNotMatch(f.node('publishing').textContent,/0\/0/);assert.match(f.summaryNode('started').textContent,/未记录/);assert.match(f.summaryNode('elapsed').textContent,/未记录/);assert.equal(f.v.clockTimers(),0);
 const s=status([target('running')],{phase:'checking_login',pending:1,failed:0});s.execution.timing.active=true;s.execution.making={total:999,completed:999,succeeded:999,failed:0};f.render(s);assert.match(f.node('phase').textContent,/正在检查发布账号/);assert.doesNotMatch(f.host.textContent+f.summary.textContent,/制作|999/);f.dispose();
});

test('a newly accepted publication batch waiting for its worker is queued rather than paused',async()=>{
 const f=await fixture(),s=status([target('waiting')],{phase:'waiting',pending:1,failed:0});f.render(s);assert.equal(f.host.dataset.runState,'waiting');assert.equal(f.node('phase').textContent,'等待开始 · 本批已排队');assert.doesNotMatch(f.host.textContent+f.summary.textContent,/已暂停|等待继续提交/);assert.match(f.summaryNode('publishing').textContent,/平台已接收 0\/1/);assert.deepEqual(f.actions,[]);f.dispose();
});

test('publication-only cancelled and paused states cannot be overridden by earlier accepted target snapshots',async()=>{
 const f=await fixture();for(const phase of ['cancelled','paused','interrupted']){f.render(status([target('platform_accepted')],{phase}));assert.equal(f.host.dataset.runState,'paused');assert.doesNotMatch(f.node('phase').textContent,/已全部提交/);assert.doesNotMatch(f.node('phase').textContent,/制作/);}f.dispose();
});

test('publication-only events, clear, task details and copy still use the shared component behavior',async()=>{
 const f=await fixture(),s=status([target('platform_scheduled')]);s.execution.events=[{time:'2026-10-25T04:00:40Z',message:'平台接收结果已记录',account_label:'主号'}];f.render(s);assert.equal(f.node('events').querySelectorAll('li').length,1);
 await f.host.querySelectorAll('button').find(n=>n.textContent==='复制执行信息').onclick();f.host.querySelectorAll('button').find(n=>n.textContent==='查看本批任务').onclick();f.host.querySelectorAll('button').find(n=>n.textContent==='清空记录').onclick();assert.equal(f.node('events').querySelectorAll('li').length,0);assert.deepEqual(f.actions,[['copy','publish'],['details','publish']]);assert.equal(f.host.dataset.runState,'completed');f.dispose();
});
