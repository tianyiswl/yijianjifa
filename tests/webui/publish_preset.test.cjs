const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const rule={first_delay_hours:4,per_day:2,days:3,interval_hours:2.5};
test('normal preset applies immediately without a confirmation or platform action',async()=>{
 const ops=[];
 const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='preset.get')return {id:'p',kind:'normal_publish',name:'双账号方案',state:'ready',payload:{account_mode:'multiple',account_ids:['a','b'],schedule_rule:rule,distribution_mode:'whole_batch_per_account',cover_mode:'video_frame',location_rule:'none'}};
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'A'},{id:'b',nickname:'B'}],total:2};
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish',{presetId:'p'});
 assert.equal(ops.filter(x=>x==='preset.get').length,1);assert.equal(view.ids.get('overlay').hidden,true);
 assert.ok(Number.isFinite(new Date(view.find(n=>n.id==='publish-start').value).getTime()));assert.equal(view.find(n=>n.id==='publish-interval').value,'2.5');assert.equal(view.find(n=>n.attrs['aria-label']==='视频分配方式').value,'whole_batch');
 for(const name of ['A','B'])assert.equal(view.find(n=>n.attrs['aria-label']==='发布账号 '+name).checked,true);
 assert.equal(ops.includes('publish.prepare'),false);view.dispose();
});
test('commerce preset applies filters and requires current cover choice while preserving no platform side effects',async()=>{
 const ops=[];
 const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='preset.get')return {id:'p',kind:'commerce',name:'团购方案',state:'ready',payload:{account_id:'a',schedule_rule:rule,commission_filter:'confirmed_only',store_rule:'favorites_then_confirm',favorite_music_mode:'random',cover_mode:'uploaded_image'}};
  return baseData(op);
 },'commerce',{presetId:'p'});
 await view.click('应用方案到本次');assert.equal(view.find(n=>n.attrs['aria-label']==='返佣筛选').value,'confirmed_only');assert.equal(view.find(n=>n.attrs['aria-label']==='仅收藏门店').checked,true);assert.equal(view.find(n=>n.attrs['aria-label']==='平台收藏音乐模式').value,'random');
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');assert.equal(ops.includes('commerce.prepare'),false);assert.match(view.ids.get('toast').textContent,/封面/);
 await view.click('批量发布封面');assert.equal(view.find(n=>n.attrs['aria-label']==='本条发布封面方式').value,'uploaded');view.dispose();
});
test('commerce preset with account chosen at use time retains cover and music rules',async()=>{
 const ops=[];
 const view=await mount((op)=>{ops.push(op);if(op==='preset.get')return {kind:'commerce',name:'使用时选账号',state:'ready',payload:{account_id:null,schedule_rule:rule,commission_filter:'all',store_rule:'confirm_when_used',favorite_music_mode:'fixed',cover_mode:'video_frame'}};return baseData(op);},'commerce',{presetId:'p'});
 await view.click('应用方案到本次');const account=view.find(n=>n.attrs['aria-label']==='团购发布账号');account.value='a';const switching=account.onchange();await view.flush();await view.click('确认切换并清除');await switching;
 assert.equal(view.find(n=>n.attrs['aria-label']==='平台收藏音乐模式').value,'fixed');const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');assert.match(view.ids.get('toast').textContent,/封面/);await view.click('批量发布封面');assert.equal(view.find(n=>n.attrs['aria-label']==='本条发布封面方式').value,'frame');view.dispose();
});
test('cancelling or encountering a stale preset does not apply stored values',async()=>{
 for(const state of ['ready','needs_repair']){
  const view=await mount(op=>{if(op==='preset.get')return {kind:'commerce',name:'失效方案',state,payload:{account_id:'a',schedule_rule:rule,commission_filter:'confirmed_only',store_rule:'favorites_then_confirm',favorite_music_mode:'random',cover_mode:'first_frame'}};return baseData(op);},'commerce',{presetId:'p'});
  if(state==='ready')await view.click('取消');else assert.match(view.ids.get('toast').textContent,/修复/);
  assert.notEqual(view.find(n=>n.attrs['aria-label']==='返佣筛选').value,'confirmed_only');assert.equal(view.find(n=>n.attrs['aria-label']==='平台收藏音乐模式').value,'none');view.dispose();
 }
});
test('switching a loaded normal preset to quick mode cannot silently skip its pending cover choice',async()=>{
 const ops=[];
 const view=await mount(op=>{ops.push(op);if(op==='preset.get')return {kind:'normal_publish',name:'封面方案',state:'ready',payload:{account_mode:'platform_default',account_ids:[],schedule_rule:{...rule,per_day:1,days:1},distribution_mode:'divide',cover_mode:'uploaded_image',location_rule:'none'}};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.prepare')return baseData('commerce.prepare');if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);},'publish',{presetId:'p'});
 const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');assert.equal(ops.includes('publish.prepare'),false);assert.match(view.ids.get('toast').textContent,/封面/);view.dispose();
});
test('stored interval, exact and daily clock rules are restored without reverting to daily interval defaults',async()=>{
 for(const kind of ['normal_publish','commerce'])for(const schedule of [{...rule,mode:'interval'},{...rule,mode:'exact'},{...rule,mode:'daily',daily_times:['09:10','15:20']}]){
  const view=await mount(op=>{if(op==='preset.get')return {kind,name:'排期',state:'ready',payload:{account_mode:'specific',account_ids:['a'],account_id:'a',schedule_rule:schedule,distribution_mode:'divide',cover_mode:'first_frame',location_rule:'none',commission_filter:'all',store_rule:'confirm_when_used',favorite_music_mode:'none'}};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};return baseData(op);},kind==='commerce'?'commerce':'publish',{presetId:'p'});
  if(kind==='commerce')await view.click('应用方案到本次');if(schedule.mode==='daily'){assert.equal(view.find(n=>n.attrs['aria-label']===(kind==='commerce'?'每天时刻方式':'普通每天时刻方式')).value,'custom');assert.ok(view.find(n=>n.tag==='input'&&n.value==='09:10'));}else{assert.equal(view.find(n=>n.tag==='button'&&n.textContent===(schedule.mode==='interval'?'定时 · 按间隔':'指定时间')).attrs['aria-pressed'],'true');}view.dispose();
 }
});
