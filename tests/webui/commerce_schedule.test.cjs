const test=require('node:test'),assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('commerce sends the local calendar offset rather than UTC for daily scheduling',()=>{
 const script=`
 const assert=require('node:assert/strict');
 const {mount,baseData}=require('./tests/webui/support/commerce_view.cjs');
 (async()=>{
  let sent;
  const view=await mount((op,p)=>{if(op==='commerce.prepare')sent=p;return baseData(op);});
  const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
  view.find(n=>n.attrs['aria-label']==='开始日期 / 时间').value='2026-09-26T23:30';
  await view.click('检查发布安排');
  assert.equal(sent.plan.start,'2026-09-26T23:30:00+08:00');view.dispose();
 })().catch(e=>{console.error(e);process.exitCode=1;});`;
 const result=spawnSync(process.execPath,['-e',script],{cwd:require('node:path').resolve(__dirname,'../..'),env:{...process.env,TZ:'Asia/Shanghai'},encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);
});
test('commerce schedule selection uses all filtered candidates and does not fill from outside search',async()=>{
 let sent;
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {items:[{id:'hidden',display_name:'其他视频',title:'其他',duration_ms:1000},{id:'match',display_name:'目标视频',title:'目标',duration_ms:1000}],total:2};
  if(op==='commerce.prepare')sent=p;
  return baseData(op);
 });
 const daily=view.find(n=>n.attrs['aria-label']==='每天数量'),days=view.find(n=>n.attrs['aria-label']==='连续天数');
 daily.value='2';days.value='1';daily.oninput();
 const search=view.find(n=>n.attrs['aria-label']==='搜索团购视频');search.value='目标';search.oninput();
 await view.click('按排期选择2条');
 assert.match(view.ids.get('toast')?.textContent||'',/当前筛选只有1条/);
 await view.click('检查发布安排');assert.deepEqual(sent.plan.video_ids,['match']);view.dispose();
});
test('commerce interval mode preserves selected videos and time, revokes preparation and uses fractional hours',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{if(op==='commerce.prepare'){requests.push(p);return {...baseData(op),token:'prepared'};}return baseData(op);});
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 const start=view.find(n=>n.attrs['aria-label']==='开始日期 / 时间');start.value='2026-09-27T09:30';
 const hours=view.find(n=>n.attrs['aria-label']==='间隔（小时）');hours.value='2.5';hours.oninput();
 await view.click('检查发布安排');
 const save=view.find(n=>n.tag==='button'&&n.textContent==='保存团购待执行任务');assert.equal(save.disabled,false);
 await view.click('定时 · 按间隔');assert.equal(save.disabled,true);assert.equal(start.value,'2026-09-27T09:30');
 const daily=view.find(n=>n.attrs['aria-label']==='每天数量');assert.equal(daily.disabled,true);
 assert.equal(view.find(n=>n.tag==='button'&&n.textContent==='按排期选择15条').hidden,true);
 await view.click('检查发布安排');assert.equal(requests[1].plan.mode,'interval');assert.equal(requests[1].plan.interval_hours,2.5);assert.deepEqual(requests[1].plan.video_ids,['v']);
 await view.click('定时 · 按天');assert.equal(daily.disabled,false);assert.equal(daily.value,3);assert.equal(save.disabled,true);
 await view.click('检查发布安排');assert.equal(requests[2].plan.mode,'daily');assert.equal(requests[2].plan.daily_count,3);view.dispose();
});
test('commerce exact time hides interval and capacity while preserving values for switching back',async()=>{
 let sent;
 const view=await mount((op,p)=>{if(op==='commerce.prepare')sent=p;return baseData(op);});
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 const hours=view.find(n=>n.attrs['aria-label']==='间隔（小时）');hours.value='2.5';
 await view.click('指定时间');assert.equal(hours.disabled,true);
 assert.equal(view.find(n=>n.attrs['aria-label']==='每天数量').disabled,true);
 await view.click('检查发布安排');assert.equal(sent.plan.mode,'exact');assert.equal(sent.plan.interval_hours,3);
 await view.click('定时 · 按间隔');assert.equal(hours.disabled,false);assert.equal(hours.value,'2.5');view.dispose();
});
test('explicit immediate mode ignores hidden stale date and does not save without confirmation',async()=>{
 let sent,writes=0;
 const view=await mount((op,p)=>{if(op==='commerce.prepare'){sent=p;return {...baseData(op),token:'prepared'};}if(op==='commerce.confirm')writes++;return baseData(op);});
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 const start=view.find(n=>n.attrs['aria-label']==='开始日期 / 时间');start.value='';
 await view.click('立即发布');assert.equal(start.disabled,true);
 await view.click('检查发布安排');assert.equal(sent.plan.mode,'immediate');assert.equal(sent.plan.start,null);assert.deepEqual(sent.plan.time_choices,{});
 assert.equal(writes,0);await view.click('保存团购待执行任务');assert.equal(writes,0);await view.click('继续修改');
 await view.click('指定时间');assert.equal(start.disabled,false);view.dispose();
});
test('commerce custom daily clocks require complete distinct input and ignore hidden interval',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{if(op==='commerce.prepare')requests.push(p);return baseData(op);});
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 const daily=view.find(n=>n.attrs['aria-label']==='每天数量');daily.value='2';daily.oninput();
 const mode=view.find(n=>n.attrs['aria-label']==='每天时刻方式');mode.value='custom';mode.onchange();
 const first=view.find(n=>n.attrs['aria-label']==='每天第1条时刻'),second=view.find(n=>n.attrs['aria-label']==='每天第2条时刻');
 assert.equal(first.value,'');await view.click('检查发布安排');assert.equal(requests.length,0);
 first.value='09:15';second.value='09:15';await view.click('检查发布安排');assert.equal(requests.length,0);
 second.value='18:45';await view.click('检查发布安排');assert.deepEqual(requests[0].plan.daily_times,['09:15','18:45']);assert.equal(requests[0].plan.interval_hours,3);
 await view.click('定时 · 按间隔');await view.click('检查发布安排');assert.equal(Object.hasOwn(requests[1].plan,'daily_times'),false);
 await view.click('定时 · 按天');assert.equal(view.find(n=>n.attrs['aria-label']==='每天第1条时刻').value,'09:15');view.dispose();
});
test('commerce per-item timing is explicit and can restore common timing',async()=>{
 const requests=[];
 const view=await mount((op,p)=>{if(op==='commerce.prepare'){requests.push(p);const r=baseData(op);r.items[0].account_id='a';return r;}return baseData(op);});
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');
 await view.click('修改本条时刻');await view.click('取消');assert.equal(requests.length,1);
 await view.click('修改本条时刻');
 const mode=view.find(n=>n.attrs['aria-label']==='本条时刻方式');mode.value='immediate';mode.onchange();
 await view.click('应用本条时刻');assert.deepEqual(requests[1].plan.item_schedules,{'a:v':{mode:'immediate',start:null}});
 await view.click('修改本条时刻');await view.click('恢复公共排期');assert.deepEqual(requests[2].plan.item_schedules,{});view.dispose();
});
