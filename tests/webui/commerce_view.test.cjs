const test=require('node:test'),assert=require('node:assert/strict');
const {mount:mountBase,baseData}=require('./support/commerce_view.cjs');
// Explicit initial V1 compatibility intent for existing Douyin-only behaviors.
const mount=(handler,name='commerce',incoming,extra)=>mountBase(handler,name,name==='publish'?{...incoming,compatibility:true}:incoming,extra);
test('a new check hides an earlier successful preview even when revalidation fails',async()=>{
 let checks=0,reject;
 const view=await mount(op=>op==='commerce.prepare'&&++checks>1?new Promise((_,r)=>reject=r):baseData(op));
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 await view.click('检查发布安排');
 const preview=view.find(n=>n.tag==='section'&&n.children.some(c=>c.tag==='h3'&&c.textContent==='本次团购安排检查'));
 assert.equal(preview.hidden,false);
 await view.click('检查发布安排');
 assert.equal(preview.hidden,true);
 reject(Error('查询已失效'));await view.flush();
 assert.equal(preview.hidden,true);assert.equal(view.ids.get('toast').textContent,'查询已失效');view.dispose();
});

test('ordinary publishing revokes the old preparation while rechecking and after failure',async()=>{
 let checks=0,reject;
 const view=await mount(op=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号',group_name:''}],total:1};
  if(op==='publish.prepare'){if(++checks>1)return new Promise((_,r)=>reject=r);return {token:'old',items:[],issues:[]};}
  throw Error(op);
 },'publish');
 view.find(n=>n.id==='publish-per-day').value='1';view.find(n=>n.id==='publish-days').value='1';
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();
 await view.click('预览发布安排');
 const submit=view.find(n=>n.tag==='button'&&n.textContent==='保存待执行任务');assert.equal(submit.disabled,false);
 await view.click('预览发布安排');assert.equal(submit.disabled,true);
 reject(Error('资料已变化'));await view.flush();assert.equal(submit.disabled,true);
 await view.click('保存待执行任务');assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
test('commerce save requires explicit confirmation and suppresses duplicate clicks',async()=>{
 let writes=0,finish;
 const view=await mount(op=>{
  if(op==='commerce.prepare')return {...baseData('commerce.preview'),token:'prepared'};
  if(op==='commerce.confirm'){writes++;return new Promise(r=>finish=r);}
  return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 await view.click('检查发布安排');await view.click('保存团购待执行任务');
 assert.equal(writes,0);assert.equal(view.ids.get('overlay').hidden,false);
 await view.click('继续修改');assert.equal(writes,0);
 await view.click('保存团购待执行任务');await view.click('确认保存团购到本机');await view.click('确认保存团购到本机');
 assert.equal(writes,1);finish({id:'task',quantity:1,state:'paused',submitted:false});await view.flush();
 assert.equal(view.ids.get('overlay').hidden,true);
 assert.match(view.ids.get('toast').textContent,/未提交平台/);
 assert.equal(view.find(n=>n.tag==='button'&&n.textContent==='保存团购待执行任务').disabled,true);
 view.dispose();
});
test('editing commerce after preparation revokes the save action',async()=>{
 const view=await mount(op=>op==='commerce.prepare'?{...baseData('commerce.preview'),token:'prepared'}:baseData(op));
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');
 cb.checked=false;cb.onchange();await view.click('保存团购待执行任务');
 assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
test('commerce software timing requires consent and schedule edits revoke the choice',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{
  if(op==='commerce.prepare'){
   requests.push(p);const result=baseData(op);result.items[0].account_id='a';result.items[0].required_choice=['choose'];result.locally_valid=false;return result;
  }return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');
 await view.click('处理本条发布时间');
 view.find(n=>n.attrs['aria-label']==='团购本条发布时间处理').value='software_schedule';
 await view.click('应用本条时间选择');assert.equal(requests.length,1);assert.equal(view.ids.get('overlay').hidden,false);
 view.find(n=>n.attrs['aria-label']==='同意团购软件定时运行条件').checked=true;
 await view.click('应用本条时间选择');assert.equal(requests.length,2);
 assert.deepEqual(requests[1].plan.time_choices,{'a:v':{mode:'software_schedule',software_consent:true}});
 const start=view.find(n=>n.attrs['aria-label']==='开始日期 / 时间');start.oninput();await view.click('检查发布安排');
 assert.deepEqual(requests[2].plan.time_choices,{});view.dispose();
});
test('commerce timing has no default choice and cancel does not change preparation',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{
  if(op==='commerce.prepare'){
   requests.push(p);const result=baseData(op);result.items[0].account_id='a';result.items[0].required_choice=['choose'];return result;
  }return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');
 await view.click('处理本条发布时间');
 assert.equal(view.find(n=>n.attrs['aria-label']==='团购本条发布时间处理').value,'');
 await view.click('应用本条时间选择');assert.equal(requests.length,1);
 await view.click('取消');assert.equal(view.ids.get('overlay').hidden,true);assert.equal(requests.length,1);
 await view.click('处理本条发布时间');view.find(n=>n.attrs['aria-label']==='团购本条发布时间处理').value='publish_now';
 await view.click('应用本条时间选择');
 assert.deepEqual(requests[1].plan.time_choices,{'a:v':{mode:'publish_now',software_consent:false}});
 await view.click('处理本条发布时间');await view.click('返回修改公共排期');await view.click('检查发布安排');
 assert.deepEqual(requests[2].plan.time_choices,{});view.dispose();
});
