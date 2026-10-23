const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
async function setup(){const requests=[];const view=await mount((op,p)=>{
 if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
 if(op==='account.list')return {items:[{id:'a',nickname:'账号',group_name:''}],total:1};
 if(op==='publish.prepare'){requests.push(p);return {token:'prepared',items:[],issues:[]};}throw Error(op);
},'publish');return {view,requests};}
test('ordinary common schedule supports interval, exact and explicit immediate without daily capacity',async()=>{
 const {view,requests}=await setup();const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();
 const start=view.find(n=>n.id==='publish-start'),hours=view.find(n=>n.id==='publish-interval');hours.value='2.5';
 await view.click('定时 · 按间隔');await view.click('预览发布安排');assert.equal(requests[0].mode,'interval');assert.equal(requests[0].interval_hours,2.5);assert.deepEqual(requests[0].video_ids,['v']);
 const save=view.find(n=>n.tag==='button'&&n.textContent==='保存待执行任务');assert.equal(save.disabled,false);
 await view.click('指定时间');assert.equal(save.disabled,true);assert.equal(hours.disabled,true);await view.click('预览发布安排');assert.equal(requests[1].mode,'exact');
 start.value='';await view.click('立即发布');assert.equal(start.disabled,true);await view.click('预览发布安排');assert.equal(requests[2].mode,'immediate');assert.equal(requests[2].start,null);
 await view.click('定时 · 按天');assert.equal(start.disabled,false);assert.equal(view.find(n=>n.id==='publish-per-day').value,3);assert.equal(save.disabled,true);view.dispose();
});
test('account schedule editor inherits common immediate mode and can save an exact exception',async()=>{
 const {view,requests}=await setup();await view.click('立即发布');await view.click('高级设置');
 await view.click('设置账号排期例外');
 const mode=view.find(n=>n.attrs['aria-label']==='账号排期方式');assert.equal(mode.value,'immediate');
 const start=view.find(n=>n.id==='publish-exception-start');assert.equal(start.disabled,true);
 mode.value='exact';mode.onchange();assert.equal(start.disabled,false);start.value='2026-09-27T12:00';
 await view.click('应用排期例外');// Account, schedule and videos now share the same page.

 view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 assert.equal(requests[0].mode,'immediate');assert.equal(requests[0].start,null);assert.equal(requests[0].account_schedules.a.mode,'exact');assert.match(requests[0].account_schedules.a.start,/2026-09-27T12:00:00/);view.dispose();
});
test('ordinary schedule selection respects the current video search',async()=>{
 let sent;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {default_account_id:'a',total:2,items:[{id:'hidden',display_name:'其他视频',title:'其他',duration_ms:1000},{id:'match',display_name:'目标视频',title:'目标',duration_ms:1000}]};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
  if(op==='publish.prepare'){sent=p;return {items:[],issues:[]};}throw Error(op);
 },'publish');
 view.find(n=>n.id==='publish-per-day').value='1';const days=view.find(n=>n.id==='publish-days');days.value='1';days.oninput();
 const search=view.find(n=>n.attrs['aria-label']==='搜索可发布视频');search.value='目标';search.oninput();
 await view.click('按排期选择 1 条');await view.click('预览发布安排');assert.deepEqual(sent.video_ids,['match']);view.dispose();
});
test('ordinary custom daily clocks require a value and do not leak into other modes',async()=>{
 const {view,requests}=await setup();
 view.find(n=>n.id==='publish-days').value='1';const count=view.find(n=>n.id==='publish-per-day');count.value='1';count.oninput();
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();
 const mode=view.find(n=>n.attrs['aria-label']==='普通每天时刻方式');mode.value='custom';mode.onchange();
 await view.click('预览发布安排');assert.equal(requests.length,0);
 view.find(n=>n.attrs['aria-label']==='普通每天第1条时刻').value='09:15';
 await view.click('预览发布安排');assert.deepEqual(requests[0].daily_times,['09:15']);
 await view.click('定时 · 按间隔');await view.click('预览发布安排');assert.equal(Object.hasOwn(requests[1],'daily_times'),false);
 await view.click('定时 · 按天');assert.equal(view.find(n=>n.attrs['aria-label']==='普通每天第1条时刻').value,'09:15');view.dispose();
});
test('account custom clocks inherit the common list but editing them leaves the common list unchanged',async()=>{
 const {view,requests}=await setup();view.find(n=>n.id==='publish-days').value='1';const count=view.find(n=>n.id==='publish-per-day');count.value='1';count.oninput();
 const mode=view.find(n=>n.attrs['aria-label']==='普通每天时刻方式');mode.value='custom';mode.onchange();view.find(n=>n.attrs['aria-label']==='普通每天第1条时刻').value='09:15';
 await view.click('高级设置');await view.click('设置账号排期例外');
 assert.equal(view.find(n=>n.attrs['aria-label']==='账号每天时刻方式').value,'custom');
 const clock=view.find(n=>n.attrs['aria-label']==='账号每天第1条时刻');assert.equal(clock.value,'09:15');clock.value='11:30';
 await view.click('应用排期例外');// Account, schedule and videos now share the same page.
view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 assert.deepEqual(requests[0].daily_times,['09:15']);assert.deepEqual(requests[0].account_schedules.a.daily_times,['11:30']);view.dispose();
});
test('ordinary item time edits restore cleanly and never follow a video to another account',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号A'},{id:'b',nickname:'账号B'}],total:2};
  if(op==='publish.prepare'){requests.push(p);const r=baseData('commerce.prepare');r.items[0].account_id=p.moves?.v||'a';r.items[0].execution_mode='platform_schedule';return r;}throw Error(op);
 },'publish');
 await view.click('高级设置');await view.click('全选','发布账号全选');// Account, schedule and videos now share the same page.

 view.find(n=>n.attrs['aria-label']==='视频分配方式').value='split_across_accounts';
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 await view.click('修改本账号这条时刻');await view.click('取消');assert.equal(requests.length,1);
 async function immediate(){await view.click('修改本账号这条时刻');const mode=view.find(n=>n.attrs['aria-label']==='本账号单条时刻方式');mode.value='immediate';mode.onchange();await view.click('应用本账号单条时刻');}
 await immediate();assert.deepEqual(requests[1].item_schedules,{'a:v':{mode:'immediate',start:null}});
 await view.click('修改本账号这条时刻');await view.click('恢复本账号排期');assert.deepEqual(requests[2].item_schedules,{});
 await immediate();const target=view.find(n=>n.attrs['aria-label']==='改分账号 测试视频');target.value='b';target.onchange();await view.click('预览发布安排');
 assert.deepEqual(requests[4].item_schedules,{});assert.equal(requests[4].moves.v,'b');view.dispose();
});

for(const kind of ['publish','commerce'])test(kind+' automatically selects filtered videos as daily quantity or days changes',async()=>{
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {default_account_id:'a',total:4,items:[0,1,2,3].map(i=>({id:'v'+i,display_name:i===3?'其他':'目标'+i,title:'视频',duration_ms:1000}))};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
  return baseData(op);
 },kind);
 const daily=view.find(n=>kind==='publish'?n.id==='publish-per-day':n.attrs['aria-label']==='每天数量');
 const days=view.find(n=>kind==='publish'?n.id==='publish-days':n.attrs['aria-label']==='连续天数');
 const search=view.find(n=>n.attrs['aria-label']===(kind==='publish'?'搜索可发布视频':'搜索团购视频'));search.value='目标';search.oninput();
 const checked=name=>view.find(n=>n.attrs['aria-label']===(kind==='publish'?'选择 ':'选择团购视频 ')+name).checked;
 days.value='1';daily.value='2';daily.oninput();
 assert.equal(checked('目标0'),true);assert.equal(checked('目标1'),true);assert.equal(checked('目标2'),false);
 days.value='2';days.oninput();assert.equal(checked('目标2'),true);assert.match(view.ids.get('toast').textContent,/只有.?3.?条/);
 days.value='1';daily.value='1';daily.oninput();assert.equal(checked('目标0'),true);assert.equal(checked('目标1'),false);
 daily.value='';daily.oninput();assert.equal(checked('目标0'),true);
 view.dispose();
});
