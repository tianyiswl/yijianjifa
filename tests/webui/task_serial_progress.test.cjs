const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

function serial(extra={}){return {mode:'per_item_v1',quantity:3,item_index:0,item_number:1,item_title:'第一条冻结文案',stage:'publishing',
 target_count:3,item_received:1,items_received:0,targets_received:1,targets_total:9,waiting_reason:'receipt',needs_action:false,
 waiting_count:2,needs_action_count:0,error_count:0,reason:'等待平台确认接收。',...extra};}
async function open(extra={},taskExtra={},dailyExtra={},executionExtra={}){
 const value=serial(extra),calls=[],routes=[],router={go:route=>routes.push(route)};
 const task={id:'serial-parent',kind:'make',name:'逐条制作批次',state:'waiting_publish',quantity:3,completed:1,succeeded:1,failed:0,
  created_at:'2026-10-26T03:00:00Z',serial:value,...taskExtra};
 const daily={phase:'publishing',serial:value,can_continue:false,publish_task_ids:[],reason:value.reason,...dailyExtra};
 const view=await mount((op,p)=>{calls.push({op,p});
  if(op==='local.checks.status')return {state:'healthy'};
  if(op==='task.center.list')return {items:[task],total:1};
  if(op==='task.center.detail')return {task,items:[],outputs:[],issues:[],controls:task.state==='paused'?['cancel']:['pause','cancel'],daily_publish:daily,execution:{serial:value,...executionExtra}};
  if(op==='task.pause'||op==='task.cancel')return {id:task.id,state:op==='task.pause'?'paused':'cancelled'};
  throw Error(op);
 },'tasks',undefined,{P011Router:router});
 return {view,task,daily,calls,routes,router};
}
const walk=node=>[node,...node.children.flatMap(walk)];
const text=view=>walk(view.getId('detail')).map(n=>n.textContent).join(' ');

test('已完成批次明确区分已接收和投稿上限跳过，不再提示后续等待处理',async()=>{
 const v=await open({stage:'completed',item_index:2,item_number:3,item_received:2,targets_received:6,
  skipped_count:3,error_count:3,waiting_reason:'none',waiting_count:0,reason:'受限账号已跳过。'},
  {state:'completed'}, {phase:'accepted'});
 const rowText=text(v.view)+' '+walk(v.view.getId('rows')).map(n=>n.textContent).join(' ');
 assert.match(rowText,/已跳过 3/);assert.doesNotMatch(rowText,/全部视频已被平台接收|后续等待处理/);
 v.view.dispose();
});
const alerts=view=>walk(view.getId('detail')).filter(n=>n.attrs.role==='alert');

test('任务中心逐条等待显示当前视频与账号及全局数量，不显示内部状态名',async()=>{
 const {view}=await open();const all=text(view)+' '+walk(view.getId('rows')).map(n=>n.textContent).join(' ');
 assert.match(all,/第1\/3条/);assert.match(all,/当前条平台已接收 1\/3/);assert.match(all,/全局 1\/9/);
 assert.doesNotMatch(all,/waiting_publish|需要处理|失败1/);view.dispose();
});

for(const [reason,label]of [['receipt','等待平台确认接收'],['verification','等待完成平台验证'],['result','等待核对原结果']]){
 test(`任务中心${reason}等待保持中性并留住成功数量`,async()=>{
  const {view}=await open({waiting_reason:reason,needs_action:reason!=='receipt',needs_action_count:reason==='receipt'?0:1}, {}, {phase:'needs_attention'});
  assert.match(text(view),new RegExp(label));assert.match(text(view),/全局 1\/9/);
  assert.equal(alerts(view).length,0);view.dispose();
 });
}

test('任务中心真实错误仍明确提示，不能因同时等待而降成正常流程',async()=>{
 const {view}=await open({error_count:1,needs_action:true,reason:'当前条视频工具执行错误。'}, {}, {phase:'needs_attention',reason:'当前条视频工具执行错误。'});
 assert.match(text(view),/错误 1/);assert(alerts(view).length>0);view.dispose();
});

test('等待发布的暂停与取消仅由明确按钮发出，不自动继续或投稿',async()=>{
 const {view,calls}=await open();assert.equal(calls.some(c=>/submit|start|continue|resume|reconcile/.test(c.op)),false);
 await view.click('暂停后续制作');assert.equal(calls.filter(c=>c.op==='task.pause').length,1);
 await view.click('取消后续制作');assert.equal(calls.filter(c=>c.op==='task.cancel').length,0);
 await view.click('确认取消');assert.equal(calls.filter(c=>c.op==='task.cancel').length,1);
 assert.equal(calls.some(c=>/submit|start|continue|resume|reconcile/.test(c.op)),false);view.dispose();
});

test('等待发布的右键菜单可暂停但不伪造继续或删除活跃批次',async()=>{
 const {view}=await open();const row=view.getId('rows').children[0];
 row.oncontextmenu({preventDefault(){},clientX:10,clientY:10});await view.flush();
 const labels=view.getId('ctx').children.map(n=>n.textContent);assert(labels.includes('暂停'));assert.equal(labels.includes('继续'),false);assert.equal(labels.includes('删除任务'),false);view.dispose();
});

test('需要明确继续时只导航原批次，不在任务详情偷偷恢复授权',async()=>{
 const {view,router,routes,calls}=await open({waiting_reason:'authorization',needs_action:true}, {}, {phase:'needs_attention',can_continue:true});
 await view.click('重新检查并继续本批');assert.deepEqual(routes,['mixing']);assert.equal(router.dailyBatchId,'serial-parent');
 assert.equal(calls.some(c=>/submit|start|continue|resume|reconcile/.test(c.op)),false);view.dispose();
});

test('已取消批次不被旧serial等待或继续标记重新唤醒',async()=>{
 const {view}=await open({waiting_reason:'verification',needs_action:true}, {state:'cancelled'}, {phase:'cancelled',can_continue:true});
 const all=text(view);assert.match(all,/已取消/);assert.doesNotMatch(all,/等待完成平台验证|重新检查并继续本批|暂停后续制作|取消后续制作/);view.dispose();
});

test('已暂停逐条批次不走通用继续，明确继续仍回原批次检查授权',async()=>{
 const {view,calls}=await open({stage:'paused',waiting_reason:'authorization',needs_action:true},{state:'paused'},{phase:'needs_attention',can_continue:true});
 view.getId('rows').children[0].oncontextmenu({preventDefault(){},clientX:10,clientY:10});await view.flush();
 assert.equal(view.getId('ctx').children.some(n=>n.textContent==='继续'),false);
 await view.click('重新检查并继续本批');assert.equal(calls.some(c=>c.op==='task.resume'),false);view.dispose();
});

test('平台验证等待并存独立执行错误时保留错误原因和提醒',async()=>{
 const {view}=await open({waiting_reason:'verification',needs_action:true},{},{phase:'needs_attention'}, {reason:'本地成品文件无法读取。'});
 assert.match(text(view),/等待完成平台验证/);assert.match(text(view),/本地成品文件无法读取/);assert(alerts(view).length>0);view.dispose();
});

test('取消后仍能查看真实错误原因和计数，不把历史错误写成验证等待',async()=>{
 const {view}=await open({waiting_reason:'verification',needs_action:true,error_count:1,reason:'平台拒绝当前视频。'}, {state:'cancelled'}, {phase:'cancelled',reason:'平台拒绝当前视频。'});
 assert.match(text(view),/平台拒绝当前视频/);assert.match(text(view),/错误 1/);assert.doesNotMatch(text(view),/等待完成平台验证|重新检查并继续本批/);view.dispose();
});

for(const reason of ['verification_required','等待平台响应超时。']){
 test(`独立记录的正常等待原因${reason}仍是中性提醒`,async()=>{
  const {view}=await open({waiting_reason:'verification',needs_action:true},{},{phase:'needs_attention'}, {reason});
  assert.equal(alerts(view).length,0);assert.match(text(view),/等待完成平台验证/);view.dispose();
 });
}

test('顶层真实错误并存执行验证代码时不能被serial中性状态盖住',async()=>{
 const {view}=await open({waiting_reason:'verification',needs_action:true},{},{phase:'needs_attention',reason:'成品视频文件已丢失。'}, {reason:'verification_required'});
 assert.match(text(view),/成品视频文件已丢失/);assert(alerts(view).length>0);view.dispose();
});
