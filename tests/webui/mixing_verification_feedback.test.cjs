const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');
const target=(state='result_unknown',extra={})=>({id:'delivery',account_id:'a',platform:'douyin',platform_label:'抖音',account_label:'抖音 · 测试账号',state,reason:'平台需要验证，请完成后继续。',needs_verification:true,...extra});
const status=(targets=[target()],extra={})=>({id:'batch',phase:'needs_attention',reason_code:'verification_required',reason:'verification_required',can_continue:false,total:targets.length,accepted:0,failed:targets.filter(t=>t.state!=='running').length,pending:targets.filter(t=>t.state==='running').length,execution:{making:{total:1,completed:1,succeeded:1,failed:0},timing:{active:false},current:{label:'发布结果待核对'},publishing:{targets,complete:true,omitted:0}},...extra});
async function mount(value){let current=value;const v=await mountMixing(async(op,p)=>{
 if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'测试账号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false};
 if(op==='mix.daily.submit')return {id:'batch',phase:'checking_login'};
 if(op==='mix.daily.status'){if(current instanceof Error)throw current;return current;}
 if(op==='mix.daily.save')return {saved:true};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
 if(op==='settings.output.read')return {directory:'/tmp/finished'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='preset.mix.list'||op==='category.list')return {items:[],total:0};
 if(op==='settings.mix_preset.read')return {id:''};throw Error(op);
 });await v.click(v.get('quick-generate'));return {v,set:value=>current=value};}
const node=(v,name)=>v.query('.mixing-execution-'+name);

test('a verified platform handoff is a neutral verification reminder rather than a stopped failure',async()=>{
 const {v}=await mount(status());assert.equal(node(v,'phase').textContent,'待完成平台验证');
 assert.equal(v.get('daily-progress').dataset.runState,'verification');assert.equal(v.get('mixing-batch-progress').dataset.runState,'verification');
 assert.match(node(v,'publishing').textContent,/平台已接收 0\/1 项 · 待验证 1 项/);
 assert.match(node(v,'hint').textContent,/验证窗口.*任务记录/);assert.equal(node(v,'reason').classList.contains('hide'),true);
 assert.doesNotMatch(v.get('daily-progress').textContent,/verification_required|已停止|未完成1项/);
 assert.equal(v.calls.filter(c=>c.operation==='mix.daily.submit').length,1);v.dispose();
});

test('verification and another running target keep the batch running with a separate pending verification count',async()=>{
 const s=status([target(),target('running',{id:'other',needs_verification:false,reason:''})]);s.execution.timing.active=true;
 const {v}=await mount(s);assert.equal(v.get('daily-progress').dataset.runState,'running');assert.match(node(v,'phase').textContent,/运行中/);
 assert.match(node(v,'publishing').textContent,/待验证 1 项/);assert.doesNotMatch(node(v,'phase').textContent,/停止|结束/);v.dispose();
});

test('a real failure remains visible alongside a verification handoff',async()=>{
 const {v}=await mount(status([target(),target('pre_submit_failed',{id:'failed',needs_verification:false,reason:'视频文件无法读取。'})]));
 assert.notEqual(v.get('daily-progress').dataset.runState,'verification');assert.match(node(v,'publishing').textContent,/待验证 1 项.*其他需处理 1 项/);
 assert.match(node(v,'reason').textContent,/视频文件无法读取/);assert.equal(node(v,'reason').attrs.role,'alert');assert.equal(node(v,'reason').classList.contains('hide'),false);v.dispose();
});

test('a timeout unknown without exact handoff evidence stays a result that needs checking',async()=>{
 const {v}=await mount(status([target('result_unknown',{needs_verification:false,reason:'提交结果尚未确认。'})],{reason_code:'timeout',reason:'等待平台响应超时。'}));
 assert.match(node(v,'phase').textContent,/发布结果待核对/);assert.notEqual(v.get('daily-progress').dataset.runState,'verification');assert.match(node(v,'reason').textContent,/超时/);v.dispose();
});

test('an explicit cancellation wins over stale verification evidence',async()=>{
 const {v}=await mount(status(undefined,{phase:'cancelled',reason_code:'daily_batch_cancelled',reason:'本批已取消'}));
 assert.equal(node(v,'phase').textContent,'本批已取消');assert.doesNotMatch(node(v,'publishing').textContent,/待验证/);v.dispose();
});

test('verification completion removes its reminder and counts only the real platform receipt',async()=>{
 const {v,set}=await mount(status());const done=target('platform_scheduled',{needs_verification:false,reason:''});
 set(status([done],{phase:'accepted',accepted:1,failed:0,pending:0,reason_code:'',reason:''}));await v.tick();
 assert.equal(v.get('daily-progress').dataset.runState,'completed');assert.match(node(v,'publishing').textContent,/平台已接收 1\/1 项/);
 assert.doesNotMatch(v.get('daily-progress').textContent,/待完成平台验证|待验证 1|verification_required|已公开/);v.dispose();
});

test('incomplete evidence and a truthy string cannot turn an unknown result into a verification-only batch',async()=>{
 for(const changes of [{needs_verification:'true'},{needs_verification:false}]){const {v}=await mount(status([target('result_unknown',changes)]));
  assert.notEqual(v.get('daily-progress').dataset.runState,'verification');assert.doesNotMatch(v.get('daily-progress').textContent,/verification_required/);v.dispose();}
 const s=status();s.execution.publishing.complete=false;const {v}=await mount(s);assert.notEqual(v.get('daily-progress').dataset.runState,'verification');v.dispose();
});

test('a stale flag on an accepted target does not reintroduce a verification reminder',async()=>{
 const {v}=await mount(status([target('platform_scheduled')],{phase:'accepted',accepted:1,failed:0,pending:0,reason_code:'',reason:''}));
 assert.equal(v.get('daily-progress').dataset.runState,'completed');assert.doesNotMatch(node(v,'publishing').textContent,/待验证/);v.dispose();
});


test('a progress read failure after verification restores real alert semantics',async()=>{
 const {v,set}=await mount(status());set(new Error('网络不可用'));await v.tick();
 assert.match(node(v,'reason').textContent,/进度读取失败/);assert.equal(node(v,'reason').attrs.role,'alert');assert.equal(node(v,'reason').classList.contains('is-verification'),false);v.dispose();
});

test('a stale verification code cannot hide a newer independent error message',async()=>{
 const {v}=await mount(status(undefined,{reason:'原视频文件无法读取。'}));
 assert.match(node(v,'reason').textContent,/原视频文件无法读取/);assert.equal(node(v,'reason').attrs.role,'alert');assert.equal(node(v,'reason').classList.contains('hide'),false);assert.notEqual(v.get('daily-progress').dataset.runState,'verification');v.dispose();
});

test('explicit cancellation remains cancelled even if an old active timing flag remains',async()=>{
 const s=status(undefined,{phase:'cancelled',reason_code:'daily_batch_cancelled',reason:'本批已取消'});s.execution.timing.active=true;
 const {v}=await mount(s);assert.equal(node(v,'phase').textContent,'本批已取消');assert.doesNotMatch(node(v,'phase').textContent,/运行中/);v.dispose();
});

test('a second problem without a saved reason still remains visible beside verification',async()=>{
 const {v}=await mount(status([target(),target('pre_submit_failed',{id:'failed',needs_verification:false,reason:''})]));
 assert.match(node(v,'reason').textContent,/另有任务需要处理/);assert.equal(node(v,'reason').attrs.role,'alert');assert.equal(node(v,'reason').classList.contains('hide'),false);v.dispose();
});

test('an empty top-level reason still lets a real execution error prevent a verification-only title',async()=>{
 const s=status(undefined,{reason:''});s.execution.reason='本地成品文件无法读取。';const {v}=await mount(s);
 assert.notEqual(v.get('daily-progress').dataset.runState,'verification');assert.match(node(v,'reason').textContent,/本地成品文件无法读取/);assert.equal(node(v,'reason').attrs.role,'alert');v.dispose();
});
