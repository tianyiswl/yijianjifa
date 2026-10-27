const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const oldSchedule={mode:'immediate',interval:'3',per_day:'2',days:'5',clock_mode:'custom',times:['08:00','22:00']};
const row={id:'one',display_name:'待发视频',title:'自己的标题',body:'自己的正文',tags:'#视频',duration_ms:3000};
async function fixture(route){
 const calls=[],memory={schema_version:2,targets:[{account_id:'a',platform:'douyin'}],distribution_by_platform:{douyin:'whole_batch'},start_policy:{kind:'fixed',value:'2000-01-01T22:00:00+08:00'},schedule:oldSchedule,schedule_by_platform:{douyin:{start_policy:{kind:'relative_tomorrow_noon'},schedule:oldSchedule}},common_fields:{title:'隐藏旧整批标题'},fields_by_platform:{douyin:{title:'隐藏平台标题'}}};
 const v=await mount((op,p)=>{
  calls.push({op,p:structuredClone(p)});
  if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'测试账号',login_state:'valid',is_default:true}],total:1};
  if(op==='publish.candidates'||op==='multi_publish.candidates')return {items:[row],total:1,default_account_id:'a'};
  if(op==='publish.execution.availability')return {available:false};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='multi_publish.catalog')return {items:[{platform:'douyin',label:'抖音',login_ready:true,ordinary_ready:true,schedule_ready:true,publish_now_ready:true,field_schema:{}}]};
  if(op==='multi_publish.settings.read')return {settings:memory,issues:[],has_saved_settings:true};
  if(op==='multi_publish.settings.save')return {settings:p.settings,saved:true};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='multi_publish.prepare')return {prepared_id:'a'.repeat(48),targets:[],summary:{videos:1,accounts:1,targets:1,ready:0,blocked:1,excluded:0}};
  if(op==='publish.prepare')return {...baseData('commerce.prepare'),token:null};
  if(op==='settings.publish.read')return {settings:{...oldSchedule,start:'2000-01-01T22:00',date:'2000-01-01'}};
  return baseData(op);
 },route);await v.flush();return {v,calls};
}
function visible(n){for(let node=n;node;node=node._parent)if(node.hidden||node.style?.display==='none')return false;return true;}
for(const route of ['publish','multi_publish'])test(route+' shows account and mixed-style daily timing in one setup column, with inline this-batch progress',async()=>{
 const {v}=await fixture(route);
 const column=v.find(n=>(n.className||'').split(/\s+/).includes('publish-setup-column'));assert(column.querySelector('select')||column.querySelector('.p011-account-picker'));
 assert(column.querySelector('.publish-daily-schedule'));assert(v.ids.get('footer').querySelector('.publish-action-row').querySelector('.publish-bottom-progress'));
 for(const name of ['排期说明','高级设置','账号管理','管理账号','整批公共资料（默认继承每条成品）','更多发布安排'])assert.equal(column.querySelectorAll('button,summary').some(n=>n.textContent===name&&visible(n)),false,name);
 for(const name of ['开始日期','发布天数','每天发布条数','首条时间：小时','首条时间：分钟','两条间隔'])assert(v.find(n=>n.attrs['aria-label']===name));v.dispose();
});
test('multi-platform simple scheduling does not consume hidden legacy modes, text or per-platform arrangements',async()=>{
 const {v,calls}=await fixture('multi_publish');const check=v.find(n=>n.attrs['aria-label']==='选择 待发视频');check.checked=true;check.onchange();await v.click('预览发布安排');
 const request=calls.find(c=>c.op==='multi_publish.prepare')?.p;assert(request);assert.equal(request.execution_mode,'platform_schedule');assert.equal(Object.keys(request.scheduled_times).length,1);
 assert.deepEqual(request.common_fields,{});assert.deepEqual(request.schedule_by_platform,{});assert.deepEqual(request.distribution_by_platform,{});assert.deepEqual(request.targets[0].fields,{});
 assert.equal(calls.some(c=>/execution.start|confirm/.test(c.op)),false);v.dispose();
});
test('ordinary new visible schedule sends Shanghai time and shows the actual selected last slot',async()=>{
 const {v,calls}=await fixture('publish');const hour=v.find(n=>n.attrs['aria-label']==='首条时间：小时');hour.value='12';hour.onchange();const chosen=v.find(n=>n.attrs['aria-label']==='选择 待发视频');chosen.checked=true;chosen.onchange();
 const summary=v.find(n=>n.className==='p011-daily-schedule-summary');assert.match(summary.textContent,/已选1条/);assert.match(summary.textContent,/最后一条：.*12:00/);
 await v.click('预览发布安排');const p=calls.find(c=>c.op==='publish.prepare')?.p;assert(p);assert.equal(p.mode,'daily');assert.match(p.start,/T12:00:00\+08:00$/);assert.equal(p.daily_count,2);assert.equal(p.days,5);assert.deepEqual(p.account_ids,['a']);assert.deepEqual(p.video_ids,['one']);assert.equal(Object.hasOwn(p,'daily_times'),false);v.dispose();
});
