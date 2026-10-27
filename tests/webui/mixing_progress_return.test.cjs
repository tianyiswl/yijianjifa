const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const account={id:'a',platform:'douyin',nickname:'合成账号',login_state:'valid',is_default:true};
const find=(v,label)=>v.get('daily-progress').querySelectorAll('button').find(b=>b.textContent===label&&!b.classList.contains('hide'));
const phase=v=>v.query('.mixing-execution-phase').textContent;
function fixture(){
 let status={id:'mine',phase:'needs_attention',can_continue:true,publish_task_ids:[],reason:'配音暂不可用',execution:{making:{total:2,completed:0,succeeded:0,failed:0},timing:{active:false}}},cancelled=false;
 const handler=async(op,p)=>{
  if(op==='mix.daily.read')return {settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},active_batch:null,recent_batch:null};
  if(op==='mix.daily.submit')return {id:'mine',phase:'checking_login'};
  if(op==='mix.daily.status'){assert.equal(p.id,'mine');return status;}
  if(op==='mix.submit')return {id:'only-mine',quantity:2,state:'waiting'};
  if(op==='task.poll')return {epoch:'e',revision:1,tasks:[{id:'only-mine',quantity:2,state:cancelled?'cancelled':'running',succeeded:1,failed:0,completed:1,execution:{making:{total:2,completed:1,succeeded:1,failed:0},timing:{active:!cancelled}}}]};
  if(op==='task.cancel'){assert(['mine','only-mine'].includes(p.id));cancelled=true;status={...status,phase:'cancelled',can_continue:false,reason:'',execution:{...status.execution,timing:{active:false}}};return {id:p.id,state:'cancelled'};}
  if(op==='mix.daily.save')return {saved:true};
  if(op==='preset.mix.list'||op==='category.list')return {items:[],total:0};
  if(op==='settings.output.read')return {directory:'/tmp/finished'};
  if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
  if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
  if(op==='settings.mix_preset.read')return {id:''};
  throw Error('Unexpected operation: '+op);
 };
 return {handler,setStatus:value=>status={...status,...value}};
}
test('returning to mixing restores this session paused batch, without auto continuing or blocking a new batch',async()=>{
 const f=fixture(),executionBatches={},router={executionBatches};
 const first=await mountMixing(f.handler,null,router);await first.click(first.get('quick-generate'));first.dispose();
 const v=await mountMixing(f.handler,null,router);
 assert.match(v.get('daily-progress').textContent,/配音暂不可用/);
 assert.match(phase(v),/已暂停/);assert.equal(v.get('quick-generate').disabled,false);
 assert(find(v,'重新检查并继续'));assert.equal(v.calls.filter(c=>/submit|continue|resume/.test(c.operation)).length,0);
 assert(v.calls.some(c=>c.operation==='mix.daily.status'&&c.params.id==='mine'));v.dispose();
});
test('returning restores video-only progress and polls the same task without any publishing call',async()=>{
 const f=fixture(),executionBatches={},router={executionBatches};
 const first=await mountMixing(f.handler,null,router);await first.click(first.get('quick-only-generate'));first.dispose();
 const v=await mountMixing(f.handler,null,router);assert.match(v.query('.mixing-execution-making').textContent,/成功 1\/2 条/);
 assert.equal(v.calls.some(c=>c.operation.startsWith('mix.daily.status')||/submit|continue|resume/.test(c.operation)),false);v.dispose();
});
test('an accepted response arriving after leaving is retained for read-only restoration and never resumed',async()=>{
 const f=fixture(),executionBatches={},router={executionBatches};let finish;
 const v=await mountMixing((op,p)=>op==='mix.daily.submit'?new Promise(resolve=>finish=resolve):f.handler(op,p),null,router);
 await v.click(v.get('quick-generate'));v.dispose();finish({id:'mine',phase:'checking_login'});await v.flush();
 const returned=await mountMixing(f.handler,null,router);assert.match(returned.get('daily-progress').textContent,/配音暂不可用/);
 assert.equal(returned.calls.filter(c=>/submit|continue|resume/.test(c.operation)).length,0);returned.dispose();
});
test('a paused batch can be cancelled with confirmation while retaining progress and completed videos',async()=>{
 const f=fixture(),v=await mountMixing(f.handler);await v.click(v.get('quick-generate'));
 const cancel=find(v,'取消本批');assert(cancel);await v.click(cancel);
 assert.equal(v.calls.some(c=>c.operation==='task.cancel'),false);assert.match(v.get('modal-body').textContent,/已生成的视频.*保留/);
 await v.click(v.get('modal-footer').querySelectorAll('button').find(b=>b.textContent==='确认取消'));
 assert.deepEqual(v.calls.filter(c=>c.operation==='task.cancel').map(c=>c.params),[{id:'mine'}]);
 assert.match(phase(v),/本批已取消/);assert.equal(find(v,'取消本批'),undefined);assert.equal(v.get('quick-generate').disabled,false);
 assert.equal(v.calls.some(c=>/delete|resume|continue/.test(c.operation)),false);v.dispose();
});
test('cancel failure retains the batch and lets the user retry without pretending it stopped',async()=>{
 const f=fixture(),v=await mountMixing((op,p)=>op==='task.cancel'?Promise.reject(Error('取消未确认')):f.handler(op,p));await v.click(v.get('quick-generate'));
 assert(find(v,'取消本批'));await v.click(find(v,'取消本批'));await v.click(v.get('modal-footer').querySelectorAll('button').find(b=>b.textContent==='确认取消'));
 assert.doesNotMatch(phase(v),/已取消/);assert.match(v.get('modal-body').textContent,/取消未确认/);assert.equal(v.get('modal-footer').querySelectorAll('button').find(b=>b.textContent==='确认取消').disabled,false);v.dispose();
});
test('a handed-off publish batch does not offer a making-only cancel that cannot stop its children',async()=>{
 const f=fixture();f.setStatus({phase:'publishing',publish_task_ids:['publish-child'],execution:{timing:{active:true}}});
 const v=await mountMixing(f.handler);await v.click(v.get('quick-generate'));assert.equal(find(v,'取消本批'),undefined);assert(find(v,'查看本批任务'));v.dispose();
});
test('an old delayed receipt never replaces a newer batch accepted after navigating away',async()=>{
 const f=fixture(),router={executionBatches:{},executionAttempts:{}};let finish;
 const first=await mountMixing((op,p)=>op==='mix.daily.submit'?new Promise(resolve=>finish=resolve):f.handler(op,p),null,router);
 await first.click(first.get('quick-generate'));first.dispose();
 const second=await mountMixing((op,p)=>op==='mix.daily.submit'?{id:'newer',phase:'making'}:op==='mix.daily.status'?{id:p.id,phase:'making',publish_task_ids:[]}:f.handler(op,p),null,router);
 await second.click(second.get('quick-generate'));assert.equal(router.executionBatches.mixing.id,'newer');
 finish({id:'older',phase:'making'});await first.flush();assert.equal(router.executionBatches.mixing.id,'newer');second.dispose();
});
test('a cancellation confirmation rechecks handoff before cancelling the parent task',async()=>{
 const f=fixture(),v=await mountMixing(f.handler);await v.click(v.get('quick-generate'));await v.click(find(v,'取消本批'));
 f.setStatus({phase:'publishing',publish_task_ids:['child']});await v.tick();
 await v.click(v.get('modal-footer').querySelectorAll('button').find(b=>b.textContent==='确认取消'));
 assert.equal(v.calls.some(c=>c.operation==='task.cancel'),false);assert.match(v.get('toast').textContent,/已进入发布/);v.dispose();
});
