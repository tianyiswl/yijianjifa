const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');

function fixture(status){return async(op,p)=>{
 if(op==='mix.daily.read')return {settings:null,accounts:[{id:'a',platform:'douyin',nickname:'测试主号',login_state:'valid',is_default:true}],capabilities:{douyin:['platform_schedule']},has_saved_settings:false,active_batch:null};
 if(op==='mix.daily.save')return {saved:true};
 if(op==='mix.daily.submit')return {id:'serial',quantity:3,phase:'checking_login'};
 if(op==='mix.daily.status')return status;
 if(op==='task.publish_report.copy')return {copied:true};
 if(op==='task.poll')return {epoch:'e',revision:1,tasks:[]};
 if(op==='preset.mix.list')return {items:[],total:0};
 if(op==='settings.output.read')return {directory:'/tmp/output'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
 if(op==='settings.mix_preset.read')return {id:''};
 throw Error(op);
};}

function sample(overrides={}){
 const serial={mode:'per_item_v1',quantity:3,item_index:0,item_number:1,item_title:'第一条冻结文案',
  stage:'publishing',target_count:3,item_received:1,items_received:0,targets_received:1,targets_total:9,
  waiting_reason:'receipt',needs_action:false,waiting_count:2,needs_action_count:0,error_count:0,...overrides};
 return {id:'serial',phase:serial.stage==='needs_attention'?'needs_attention':serial.stage==='making'?'making':'publishing',
  total:9,accepted:serial.targets_received,pending:9-serial.targets_received,failed:serial.error_count,serial,
  execution:{serial,making:{completed:1,succeeded:1,failed:0,total:3},timing:{active:true},
   current:{index:1,total:3,title:serial.item_title},events:[],publishing:{targets:[],complete:true,omitted:0}}};
}
async function open(status){const view=await mountMixing(fixture(status));await view.click(view.get('quick-generate'));return view;}
const node=(view,name)=>view.query('.mixing-execution-'+name);

test('数量只在底部进度条显示，当前步骤只在右侧显示一次',async()=>{
 const view=await open(sample());
 assert.equal(node(view,'making').classList.contains('hide'),true);
 assert.equal(node(view,'publishing').classList.contains('hide'),true);
 const summary=view.get('mixing-batch-progress');
 assert.equal(summary.querySelector('.batch-progress-phase').classList.contains('hide'),true);
 assert.equal(summary.querySelector('.batch-progress-counts').classList.contains('hide'),false);
 assert.match(summary.querySelector('.batch-progress-publishing').textContent,/平台已接收 1\/9 项/);
 assert.doesNotMatch(summary.querySelector('.batch-progress-making').textContent,/失败 0/);
 assert.equal(node(view,'phase').classList.contains('hide'),false);
 await view.tick();
 assert.equal(node(view,'making').classList.contains('hide'),true);
 assert.equal(node(view,'publishing').classList.contains('hide'),true);
 view.dispose();
});

test('之前有失败项也不能把当前正常等待重复显示成红色错误',async()=>{
 const status=sample({error_count:1,reason:'等待本条平台确认接收。'});
 status.reason=status.serial.reason;
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/等待平台确认接收/);
 assert.equal(node(view,'reason').classList.contains('hide'),true);
 assert.equal(node(view,'reason').attrs.role,'status');
 assert.equal(view.get('daily-progress').dataset.runState,'waiting');
 assert.match(view.get('mixing-batch-progress').textContent,/错误 1 项/);
 view.dispose();
});

test('正常等待说明不能遮住独立的真实错误',async()=>{
 const status=sample({error_count:1,reason:'等待本条平台确认接收。'});
 status.reason=status.serial.reason;
 status.execution.reason='快手 · 测试号：视频文件无法读取。';
 const view=await open(status);
 assert.match(node(view,'reason').textContent,/快手.*视频文件无法读取/);
 assert.equal(node(view,'reason').classList.contains('hide'),false);
 assert.equal(node(view,'reason').attrs.role,'alert');
 view.dispose();
});

test('批次结束只保留总结果和报告，不重复显示最后一条的汇总和序号',async()=>{
 const status=sample({stage:'completed',waiting_reason:'none',item_index:2,item_number:3,error_count:1,
  targets_received:8,item_received:2,needs_action:false,reason:'本条发布尝试已结束：接收2项，失败1项；未完成项保留待处理。'});
 status.phase='completed';status.reason=status.serial.reason;status.execution.timing.active=false;
 status.execution.report={ready:true,missed:[{filename:'1027_191800_03.mp4'}],unmade:0,text:'B站 · 测试账号\n1027_191800_03.mp4\n封面保存尚未确认，尚未提交。'};
 const view=await open(status);
 assert.equal(view.get('mixing-batch-progress').querySelector('.batch-progress-phase').classList.contains('hide'),true);
 assert.match(node(view,'phase').textContent,/本批处理完成.*失败 1/);
 assert.equal(node(view,'reason').classList.contains('hide'),true);
 assert.equal(node(view,'current').classList.contains('hide'),true);
 assert.equal(node(view,'report').classList.contains('hide'),false);
 assert.match(node(view,'report').textContent,/B站.*测试账号[\s\S]*1027_191800_03.mp4/);
 view.dispose();
});

test('已结束批次的原回执报告不被清理后的空任务列表误报为等待',async()=>{
 const status={phase:'accepted',accepted:8,total:9,pending:0,failed:1,execution:{closed_report:true,
  timing:{active:false},making:{succeeded:3,failed:0,completed:3,total:3},events:[],current:{},serial:null,
  publishing:{targets:[],complete:false,omitted:0},report:{ready:true,accepted:8,total:9,unmade:0,missed:[{filename:'03.mp4'}],text:'原回执报告：8项接收，1项尚未提交'}}};
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/本批处理完成.*1项待处理/);
 assert.doesNotMatch(node(view,'phase').textContent,/等待|当前未运行|全部.*接收/);
 assert.match(view.get('mixing-batch-progress').querySelector('.batch-progress-publishing').textContent,/8\/9/);
 assert.equal(node(view,'report').classList.contains('hide'),false);
 assert.equal(view.calls.some(c=>/continue|reconcile|publish\.start/.test(c.operation)),false);
 view.dispose();
});

test('已有失败不把后续制作误报暂停，结束后列出失败报告和真实文件名',async()=>{
 const running=sample({item_index:1,item_number:2,stage:'making',waiting_reason:'none',error_count:1,targets_received:2});
 const v=await open(running);
 assert.match(node(v,'phase').textContent,/正在制作/);
 assert.doesNotMatch(node(v,'phase').textContent,/后续等待|已暂停|遇到错误/);
 v.dispose();
 const done=sample({item_index:2,item_number:3,stage:'completed',waiting_reason:'none',error_count:1,targets_received:8,items_received:2});
 done.execution.timing.active=false;
 done.execution.report={ready:true,missed:[{filename:'1027_180500_003.mp4'}],text:'发布报告\nB站 · 逆浪风\n1027_180500_003.mp4\n封面未确认，尚未提交'};
 const finished=await open(done);
 assert.match(node(finished,'phase').textContent,/处理完成.*失败 1/);
 assert.match(node(finished,'report').textContent,/1027_180500_003.mp4/);
 assert.equal(node(finished,'report').classList.contains('hide'),false);
 const copy=finished.get('daily-progress').querySelectorAll('button').find(n=>n.textContent==='复制发布报告');await finished.click(copy);
 assert.deepEqual(finished.calls.find(c=>c.operation==='task.publish_report.copy').params,{id:'serial'});
 finished.dispose();
});

test('提前停止的报告不把未生成内容写成全部已接收',async()=>{
 const data=sample({stage:'paused',waiting_reason:'none'});data.phase='cancelled';data.execution.timing.active=false;
 data.accepted=0;data.execution.report={ready:true,accepted:3,total:9,unmade:2,missed:[],text:'已制作视频均已接收，另2条尚未生成'};
 const v=await open(data);
 assert.match(node(v,'report').textContent,/2条未生成/);
 assert.doesNotMatch(node(v,'report').textContent,/全部已接收/);
 assert.match(node(v,'publishing').textContent,/3\/9/);
 v.dispose();
});

test('账号检查只显示一次当前步骤，进度只显示整批数量',async()=>{
 const status=sample({quantity:9,target_count:4,targets_total:36,item_received:0,targets_received:0,items_received:0});
 status.phase='checking_login';status.login_message='正在检查账号登录，检查通过后继续本批。';
 status.execution.making={completed:1,succeeded:1,failed:0,total:9};
 status.execution.current={label:'正在检查第2/4个账号',account_label:'视频号 · 逆浪风',total:4};
 const view=await open(status);
 assert.equal(node(view,'phase').textContent,'正在检查第2/4个账号');
 assert.equal(node(view,'hint').classList.contains('hide'),true);
 assert.match(node(view,'making').textContent,/成功 1\/9 条/);
 assert.doesNotMatch(node(view,'making').textContent,/失败 0/);
 assert.equal(node(view,'publishing').textContent,'发布：平台已接收 0/36 项');
 assert.equal(node(view,'current').textContent,'第1/9条 · 视频号 · 逆浪风');
 assert.equal(node(view,'account-help').classList.contains('is-controls-only'),true);
 assert.doesNotMatch(node(view,'current').textContent,/正在检查|账号：/);
 assert.equal(view.calls.some(c=>c.operation==='task.cancel'),false);
 view.dispose();
});

test('混剪执行记录按视频编号显示，不展示用户不认识的文案标题',async()=>{
 const status=sample({item_index:1,item_number:2,item_title:'用户不认识的标题'});
 status.execution.events=[{time:'2026-10-27T09:10:05Z',step:'douyin.upload',message:'上传视频',title:'第2个视频',account_label:'抖音 · 测试号'}];
 const view=await open(status);
 assert.match(node(view,'events').textContent,/上传视频 · 第2个视频 · 抖音 · 测试号/);
 assert.match(node(view,'current').textContent,/第2\/3条/);
 assert.doesNotMatch(node(view,'current').textContent,/用户不认识的标题|当前内容/);
 view.dispose();
});

test('投稿上限已跳过时继续显示其他账号进度，结束时不冒称全部接收',async()=>{
 const status=sample({skipped_count:1,error_count:1,targets_received:1});
 status.execution.publishing.targets=[{id:'limited',account_id:'a',platform:'douyin',account_label:'抖音 · 测试号',state:'platform_rejected',skipped:true,reason:'今天投稿次数已达到上限'}];
 const view=await open(status);
 assert.match(node(view,'publishing').textContent,/已跳过 1 项/);
 assert.doesNotMatch(node(view,'phase').textContent,/自动流程已暂停|后续等待处理/);
 view.dispose();
 const done=sample({stage:'completed',item_index:2,item_number:3,item_received:2,targets_received:6,items_received:0,skipped_count:3,error_count:3,waiting_reason:'none'});
 done.execution.timing.active=false;const ended=await open(done);
 assert.match(node(ended,'phase').textContent,/本批处理完成 · 已跳过 3 项/);
 assert.doesNotMatch(node(ended,'phase').textContent,/全部.*接收|自动流程已暂停/);ended.dispose();
});

test('抖音明确投稿上限在主进度和侧边处理卡显示平台原原因',async()=>{
 const status=sample({stage:'needs_attention',error_count:1,waiting_reason:'configuration',needs_action:true});
 status.execution.timing.active=false;
 status.execution.publishing.targets=[{id:'limited',account_id:'a',platform:'douyin',account_label:'抖音 · 测试号',state:'platform_rejected',reason:'抖音提示：今天投稿次数已达到上限，请明天再试。'}];
 const view=await open(status);
 assert.match(view.get('mixing-batch-progress').textContent,/今天投稿次数已达到上限，请明天再试/);
 assert.match(node(view,'account-help').textContent,/今天投稿次数已达到上限，请明天再试/);
 assert.match(node(view,'phase').textContent,/自动流程已暂停/);
 assert.ok(view.get('daily-progress').querySelectorAll('button').some(n=>n.textContent==='查看受限任务'));
 assert.equal(view.calls.some(c=>/reconcile|resume|publish\.start/.test(c.operation)),false);view.dispose();
});

test('逐条进度同时显示当前视频和当前账号，未来目标没有记录不误报资料缺失',async()=>{
 const view=await open(sample());
 assert.match(node(view,'current').textContent,/第1\/3条/);
 assert.match(view.get('mixing-batch-progress').querySelector('.batch-progress-publishing').textContent,/平台已接收 1\/9 项/);
 assert.equal(node(view,'publishing').textContent,'发布：平台已接收 1/9 项');
 assert.doesNotMatch(view.get('daily-progress').textContent,/部分.*不可读取|进度.*缺失/);
 view.dispose();
});

for(const [reason,label] of [['dispatch','等待提交当前视频'],['account','等待账号空闲'],['receipt','等待平台确认接收'],['schedule','等待定时时间']]){
 test(`正常${reason}等待使用中性提示而非失败`,async()=>{
  const view=await open(sample({waiting_reason:reason,stage:reason==='schedule'?'waiting_schedule':'publishing'}));
  assert.match(node(view,'phase').textContent,new RegExp(label));
  assert.equal(view.get('daily-progress').dataset.runState,'waiting');
  assert.doesNotMatch(node(view,'phase').textContent,/失败|已停止|需处理/);
  assert.notEqual(node(view,'reason').attrs.role,'alert');
  view.dispose();
 });
}

for(const [reason,label] of [['verification','等待完成平台验证'],['result','等待核对原结果'],['authorization','等待确认后继续']]){
 test(`需要行动的${reason}仍明确等待，保留已有成功进度`,async()=>{
  const view=await open(sample({stage:'needs_attention',waiting_reason:reason,needs_action:true,
   item_received:2,targets_received:2,waiting_count:1,needs_action_count:1}));
  assert.match(node(view,'phase').textContent,new RegExp(label));
  assert.equal(view.get('daily-progress').dataset.runState,'waiting');
  assert.match(node(view,'publishing').textContent,/2\/9/);assert.match(view.get('mixing-batch-progress').querySelector('.batch-progress-publishing').textContent,/2\/9/);
  assert.doesNotMatch(node(view,'publishing').textContent,/失败/);
  assert.notEqual(node(view,'reason').attrs.role,'alert');
  assert.ok(view.get('daily-progress').querySelectorAll('button').some(b=>b.textContent==='查看本批任务'));
  view.dispose();
 });
}

test('已有发布记录后制作下一条仍显示当前制作，不显示整批已结束',async()=>{
 const status=sample({stage:'making',waiting_reason:'none',item_index:1,item_number:2,item_title:'第二条冻结文案',
  item_received:0,items_received:1,targets_received:3,waiting_count:0});
 status.execution.current={index:2,total:3,title:'第二条冻结文案'};
 const view=await open(status);
 assert.equal(node(view,'phase').textContent,'正在制作');assert.match(node(view,'current').textContent,/第2\/3条/);
 assert.equal(view.get('daily-progress').dataset.runState,'running');
 assert.match(node(view,'publishing').textContent,/3\/9/);
 view.dispose();
});

test('正常制作阶段的中性说明不使用失败告警样式',async()=>{
 const status=sample({stage:'making',waiting_reason:'none',item_index:1,item_number:2,
  reason:'等待派发本条发布任务。'});status.reason=status.serial.reason;
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/正在制作/);
 assert.equal(view.get('daily-progress').dataset.runState,'running');
 assert.equal(node(view,'reason').attrs.role,'status');view.dispose();
});

test('真实错误保留错误提示，不把它涂成正常等待',async()=>{
 const status=sample({stage:'needs_attention',waiting_reason:'none',error_count:1,needs_action:true});
 status.reason='视频工具执行失败，当前条尚未提交。';
 const view=await open(status);
 assert.equal(view.get('daily-progress').dataset.runState,'attention');
 assert.match(node(view,'reason').textContent,/视频工具执行失败/);
 assert.equal(node(view,'reason').attrs.role,'alert');
 view.dispose();
});

for(const waiting_reason of ['verification','account','receipt'])test(`真实错误与${waiting_reason}等待并存时仍显示错误`,async()=>{
 const status=sample({stage:'needs_attention',waiting_reason,error_count:1,needs_action:true});
 status.reason='当前条有一个账号提交前失败。';
 const view=await open(status);
 assert.equal(view.get('daily-progress').dataset.runState,'attention');
 assert.match(node(view,'phase').textContent,/错误/);
 assert.match(node(view,'reason').textContent,/提交前失败/);
 assert.equal(node(view,'reason').attrs.role,'alert');view.dispose();
});

test('逐条等待也不能掩盖独立的执行错误',async()=>{
 const status=sample({waiting_reason:'verification',stage:'needs_attention'});
 status.reason='verification_required';status.execution.reason='当前视频文件无法读取。';
 const view=await open(status);
 assert.equal(view.get('daily-progress').dataset.runState,'attention');
 assert.match(node(view,'reason').textContent,/当前视频文件无法读取/);
 assert.equal(node(view,'reason').attrs.role,'alert');view.dispose();
});

test('主动暂停后的继续等待使用中性说明',async()=>{
 const status=sample({stage:'paused',waiting_reason:'authorization',reason:'已暂停，等待继续。'});
 status.phase='needs_attention';status.reason='已暂停，等待继续。';
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/已暂停.*等待继续/);
 assert.equal(node(view,'reason').attrs.role,'status');view.dispose();
});

test('内部异常停止制作时显示错误原因，不当作普通暂停等待',async()=>{
 const status=sample({stage:'paused',waiting_reason:'configuration',error_count:1,reason:'逐条流程检查异常（ValueError），已停止后续制作。'});
 status.phase='needs_attention';status.reason=status.serial.reason;
 const view=await open(status);
 assert.equal(view.get('daily-progress').dataset.runState,'attention');
 assert.match(node(view,'phase').textContent,/错误/);
 assert.match(node(view,'reason').textContent,/ValueError/);
 assert.equal(node(view,'reason').attrs.role,'alert');view.dispose();
});

test('同账号暂缓与独立制作错误并存时不掩盖错误',async()=>{
 const status={id:'serial',phase:'needs_attention',reason:'视频文件无法读取。',total:2,accepted:0,pending:1,failed:1,
  execution:{making:{completed:2,succeeded:1,failed:1,total:2},timing:{active:false},events:[],
   publishing:{complete:true,omitted:0,targets:[{id:'wait',account_id:'a',platform:'douyin',state:'waiting',blocked_by:{state:'result_unknown'}}]}}};
 const view=await open(status);
 assert.notEqual(view.get('daily-progress').dataset.runState,'waiting');
 assert.match(node(view,'reason').textContent,/视频文件无法读取/);
 assert.equal(node(view,'reason').attrs.role,'alert');view.dispose();
});

test('最后一条已制作但只有部分账号接收时明确等待，不提前显示本批完成',async()=>{
 const status=sample({item_index:2,item_number:3,item_received:2,items_received:2,targets_received:8,
  waiting_count:1});status.execution.making={completed:3,succeeded:3,failed:0,total:3};
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/等待平台确认接收/);
 assert.doesNotMatch(node(view,'phase').textContent,/本批.*完成|全部.*接收/);
 assert.match(node(view,'publishing').textContent,/8\/9/);
 view.dispose();
});

test('逐条批次已有发布记录后仍能取消后续制作，保留原发布处理入口',async()=>{
 const status=sample();status.publish_task_ids=['child-one'];
 const view=await open(status);
 const cancel=view.get('daily-progress').querySelectorAll('button').find(b=>b.textContent==='取消本批');
 assert.ok(cancel&&!cancel.classList.contains('hide'));
 await view.click(cancel);
 assert.match(view.get('modal-body').textContent,/已提交平台的部分.*任务记录/);
 view.dispose();
});

test('全部接收才显示本批完成，并保留平台接收与公开的区别',async()=>{
 const status=sample({stage:'completed',waiting_reason:'none',item_index:2,item_number:3,item_received:3,
  items_received:3,targets_received:9,waiting_count:0});status.phase='accepted';
 status.execution.making={completed:3,succeeded:3,failed:0,total:3};status.execution.timing.active=false;
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/本批.*全部.*平台.*接收|本批.*平台.*全部.*接收/);
 assert.equal(view.get('daily-progress').dataset.runState,'completed');
 assert.doesNotMatch(node(view,'phase').textContent,/全部.*已公开|全部.*已发布/);
 view.dispose();
});

for(const [state,label] of [['result_unknown','等待核对原结果'],['paused_auth','等待完成平台验证']]){
 test(`旧批次${state}也按等待呈现，不沿用失败式反馈`,async()=>{
  const status={id:'serial',phase:'needs_attention',total:2,accepted:1,pending:1,failed:0,
   execution:{making:{completed:2,succeeded:2,failed:0,total:2},timing:{active:false},events:[],
    publishing:{complete:true,omitted:0,targets:[{id:'done',account_id:'a',platform:'douyin',state:'platform_scheduled'},
      {id:'wait',account_id:'b',platform:'douyin',state,needs_verification:state==='paused_auth',reason:'等待原操作确认'}]}}};
  const view=await open(status);
  assert.match(node(view,'phase').textContent,new RegExp(label));
  assert.ok(['waiting','verification'].includes(view.get('daily-progress').dataset.runState));
  assert.doesNotMatch(node(view,'publishing').textContent,/未完成|失败/);
  assert.notEqual(node(view,'reason').attrs.role,'alert');
  view.dispose();
 });
}

test('旧批次等待账号空闲写正常等待，不称为暂停或失败',async()=>{
 const status={id:'serial',phase:'publishing',total:1,accepted:0,pending:1,failed:0,
  execution:{making:{completed:1,succeeded:1,failed:0,total:1},timing:{active:false},events:[],
   publishing:{complete:true,omitted:0,targets:[{id:'wait',account_id:'a',platform:'douyin',state:'waiting',blocked_by:{delivery_id:'other',state:'running'}}]}}};
 const view=await open(status);
 assert.match(node(view,'phase').textContent,/等待账号空闲/);
 assert.equal(view.get('daily-progress').dataset.runState,'waiting');
 assert.doesNotMatch(node(view,'phase').textContent,/已暂停|失败/);
 view.dispose();
});

test('进度读取暂不可用仅等待重读，保留制作与接收数量',async()=>{
 let unavailable=false;const status=sample();const base=fixture(status);
 const view=await mountMixing(async(op,p)=>{if(op==='mix.daily.status'&&unavailable)throw Error('网络暂不可用');return base(op,p);});
 await view.click(view.get('quick-generate'));unavailable=true;await view.tick();
 assert.match(node(view,'reason').textContent,/等待重新读取进度/);
 assert.notEqual(node(view,'reason').attrs.role,'alert');
 assert.match(node(view,'making').textContent,/成功 1\/3/);
 assert.match(node(view,'publishing').textContent,/1\/9/);
 view.dispose();
});


test('全部平台接收的说明使用成功样式与状态语义',async()=>{
 const status=sample({stage:'completed',waiting_reason:'none',item_index:2,item_number:3,item_received:3,items_received:3,targets_received:9,waiting_count:0,reason:'本条全部所选账号平台已接收。'});
 status.phase='accepted';status.reason=status.serial.reason;status.execution.timing.active=false;
 const view=await open(status);assert.equal(node(view,'reason').classList.contains('is-success'),true);
 assert.equal(node(view,'reason').attrs.role,'status');view.dispose();
});


test('单条全部接收而整批尚未结束时也使用成功样式',async()=>{
 const status=sample({stage:'publishing',waiting_reason:'none',item_received:3,items_received:1,targets_received:3,waiting_count:0,reason:'本条全部所选账号平台已接收。'});
 status.reason=status.serial.reason;
 const view=await open(status);assert.equal(node(view,'reason').classList.contains('is-success'),true);
 assert.equal(node(view,'reason').attrs.role,'status');assert.notEqual(view.get('daily-progress').dataset.runState,'completed');view.dispose();
});
