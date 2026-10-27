const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const rule={first_delay_hours:4,per_day:2,days:3,interval_hours:2.5};
const visible=node=>{for(let n=node;n;n=n._parent)if(n.hidden||n.style?.display==='none'||n.classList.contains('hide'))return false;return true;};
const field=(view,label)=>view.find(n=>n.attrs['aria-label']===label&&visible(n));
function edit(view,label,value){const input=field(view,label);input.value=String(value);(input.oninput||input.onchange)({isTrusted:true,target:input});}
test('normal preset applies its first account and visible daily fields without a confirmation or platform action',async()=>{
 const ops=[],requests=[];
 const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='preset.get')return {id:'p',kind:'normal_publish',name:'双账号方案',state:'ready',payload:{account_mode:'multiple',account_ids:['a','b'],schedule_rule:rule,distribution_mode:'whole_batch_per_account',cover_mode:'video_frame',location_rule:'none'}};
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'A'},{id:'b',nickname:'B'}],total:2};
  if(op==='publish.prepare'){requests.push(structuredClone(p));return baseData('commerce.prepare');}
  if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);
 },'publish',{presetId:'p'});
 assert.equal(ops.filter(x=>x==='preset.get').length,1);assert.equal(view.ids.get('overlay').hidden,true);
 assert.equal(field(view,'开始日期').value,field(view,'开始日期').min);assert.equal(field(view,'两条间隔').value,'2.5');assert.equal(field(view,'每天发布条数').value,'2');assert.equal(field(view,'发布天数').value,'3');assert.equal(field(view,'本次发布账号').value,'a');
 assert.equal(visible(view.find(n=>n.attrs['aria-label']==='视频分配方式')),false);assert.equal(ops.includes('publish.prepare'),false);
 edit(view,'首条时间：小时','12');edit(view,'首条时间：分钟','00');const cb=field(view,'选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
 assert.deepEqual(requests[0].account_ids,['a']);assert.equal(requests[0].mode,'daily');assert.equal(requests[0].distribution,'split_across_accounts');assert.deepEqual(requests[0].cover_overrides,{});assert.equal(ops.some(op=>/confirm|execution\.start/.test(op)),false);view.dispose();
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
test('normal presets ignore a hidden old cover choice and keep using the selected finished video cover',async()=>{
 const ops=[],requests=[];
 const view=await mount((op,p)=>{ops.push(op);if(op==='preset.get')return {kind:'normal_publish',name:'封面方案',state:'ready',payload:{account_mode:'platform_default',account_ids:[],schedule_rule:{...rule,per_day:1,days:1},distribution_mode:'divide',cover_mode:'uploaded_image',location_rule:'none'}};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.prepare'){requests.push(structuredClone(p));return baseData('commerce.prepare');}if(op==='settings.publish.read')return {settings:null};if(op==='settings.publish.save')return {saved:true};if(op==='preset.list')return {items:[],total:0};throw Error(op);},'publish',{presetId:'p'});
 assert.equal(ops.includes('publish.prepare'),false);edit(view,'首条时间：小时','12');edit(view,'首条时间：分钟','00');const cb=field(view,'选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');assert.equal(requests.length,1);assert.deepEqual(requests[0].cover_overrides,{});assert.equal(ops.some(op=>/confirm|execution\.start/.test(op)),false);view.dispose();
});
test('commerce stored interval, exact and daily clocks retain their original scheduling contract',async()=>{
 const kind='commerce';for(const schedule of [{...rule,mode:'interval'},{...rule,mode:'exact'},{...rule,mode:'daily',daily_times:['09:10','15:20']}]){
  const view=await mount(op=>{if(op==='preset.get')return {kind,name:'排期',state:'ready',payload:{account_mode:'specific',account_ids:['a'],account_id:'a',schedule_rule:schedule,distribution_mode:'divide',cover_mode:'first_frame',location_rule:'none',commission_filter:'all',store_rule:'confirm_when_used',favorite_music_mode:'none'}};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};return baseData(op);},kind==='commerce'?'commerce':'publish',{presetId:'p'});
  if(kind==='commerce')await view.click('应用方案到本次');if(schedule.mode==='daily'){assert.equal(view.find(n=>n.attrs['aria-label']===(kind==='commerce'?'每天时刻方式':'普通每天时刻方式')).value,'custom');assert.ok(view.find(n=>n.tag==='input'&&n.value==='09:10'));}else{assert.equal(view.find(n=>n.tag==='button'&&n.textContent===(schedule.mode==='interval'?'定时 · 按间隔':'指定时间')).attrs['aria-pressed'],'true');}view.dispose();
 }
});
test('normal old interval, exact and custom presets become the visible daily rule without hidden schedule data',async()=>{
 for(const schedule of [{...rule,mode:'interval'},{...rule,mode:'exact'},{...rule,mode:'daily',daily_times:['09:10','15:20']}]){
  const requests=[];const view=await mount((op,p)=>{if(op==='preset.get')return {kind:'normal_publish',name:'旧排期',state:'ready',payload:{account_mode:'specific',account_ids:['a'],schedule_rule:schedule,distribution_mode:'whole_batch_per_account',cover_mode:'first_frame',location_rule:'none'}};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};if(op==='publish.prepare'){requests.push(structuredClone(p));return baseData('commerce.prepare');}return baseData(op);},'publish',{presetId:'p'});
  assert.equal(field(view,'开始日期').value,field(view,'开始日期').min);assert.equal(field(view,'发布天数').value,'3');assert.equal(field(view,'每天发布条数').value,'2');assert.equal(field(view,'两条间隔').value,'2.5');
  assert.equal(visible(view.find(n=>n.attrs['aria-label']==='普通每天时刻方式')),false);edit(view,'首条时间：小时','12');edit(view,'首条时间：分钟','00');const cb=field(view,'选择 测试视频');cb.checked=true;cb.onchange();await view.click('预览发布安排');
  assert.equal(requests[0].mode,'daily');assert.equal(requests[0].distribution,'split_across_accounts');assert.equal(Object.hasOwn(requests[0],'daily_times'),false);assert.equal(Object.hasOwn(requests[0],'account_schedules'),false);view.dispose();
 }
});
test('a relative ordinary preset keeps its intended Shanghai clock on a computer in another timezone',async()=>{
 const fixed=Date.parse('2026-10-25T02:00:00Z');class Clock extends Date{constructor(...args){super(...(args.length?args:[fixed]));}static now(){return fixed;}}
 const v=await mount(op=>{if(op==='preset.get')return {id:'p',kind:'normal_publish',state:'ready',name:'明日方案',payload:{account_mode:'specific',account_ids:['a'],schedule_rule:{...rule,first_delay_hours:24},distribution_mode:'divide',use_existing_cover:true,location_rule:'none'}};if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};return baseData(op);},'publish',{presetId:'p'},{Date:Clock});
 assert.equal(v.find(n=>n.attrs['aria-label']==='首条时间：小时').value,'10');assert.equal(v.find(n=>n.attrs['aria-label']==='首条时间：分钟').value,'00');v.dispose();
});
