// Real publishing modules with a minimal DOM: structure only, not pixel geometry.
const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const fixtureNow=Date.parse('2026-10-25T04:00:00Z');
class FixtureDate extends Date{constructor(...args){super(...(args.length?args:[fixtureNow]));}static now(){return fixtureNow;}}
const hasClass=(node,name)=>(node.className||'').split(/\s+/).includes(name)||node.classList.contains(name);
const visible=node=>{for(let n=node;n;n=n._parent)if(n.hidden||n.style?.display==='none'||hasClass(n,'hide'))return false;return true;};
const variants=[{name:'ordinary video',route:'publish'},{name:'ordinary image post',route:'publish',context:{contentType:'image'}},{name:'multi-platform',route:'multi_publish'}];
const video={id:'v',display_name:'待发视频',title:'自己的标题',body:'自己的正文',tags:'#产品',duration_ms:3000};
const post={id:'p',display_name:'待发图文',title:'自己的图文',body:'正文',tags:'#产品',image_count:1,images:[{id:'image',thumbnail_url:'p011media://fixture'}]};
async function fixture(variant){
 const calls=[],old={mode:'immediate',interval:'3',per_day:'2',days:'5',clock_mode:'custom',times:['08:00','22:00']};
 const memory={schema_version:2,targets:[{account_id:'a',platform:'douyin'}],distribution_by_platform:{douyin:'whole_batch'},start_policy:{kind:'fixed',value:'2000-01-01T12:00:00+08:00'},schedule:old,schedule_by_platform:{douyin:{start_policy:{kind:'relative_tomorrow_noon'},schedule:old}},common_fields:{title:'隐藏整批标题'},fields_by_platform:{douyin:{title:'隐藏平台标题'}}};
 const v=await mount((op,p)=>{
  calls.push({op,p:structuredClone(p)});
  if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'离线测试账号',login_state:'valid',is_default:true}],total:1};
  if(op==='publish.candidates'||op==='multi_publish.candidates')return {items:[video],total:1,default_account_id:'a'};
  if(op==='publish.execution.availability')return {available:false};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='multi_publish.catalog')return {items:[{platform:'douyin',label:'抖音',login_ready:true,ordinary_ready:true,schedule_ready:true,publish_now_ready:true,field_schema:{}}]};
  if(op==='multi_publish.settings.read')return {settings:memory,issues:[],has_saved_settings:true};
  if(op==='multi_publish.settings.save')return {settings:p.settings,saved:true};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='image_publish.context')return {accounts:[{id:'a',platform:'douyin',nickname:'离线测试账号'}],default_account_id:'a',settings:{mode:'exact',start:'2026-10-26T12:00:00+08:00',interval_hours:3,daily_count:2,days:5},capabilities:{platform_schedule:true,publish_now:true},declarations:[]};
  if(op==='image_posts.list')return {items:[post],total:1};
  if(op==='image_publish.settings.save')return {saved:true};
  if(op==='settings.publish.read')return {settings:{...old,start:'2000-01-01T12:00',date:'2000-01-01'}};
  return baseData(op);
 },variant.route,variant.context,{Date:FixtureDate});await v.flush();return {v,calls};
}
for(const variant of variants){
 test(variant.name+' keeps account and the five daily fields in a single compact setup card',async()=>{
  const {v,calls}=await fixture(variant);try{
   const setup=v.find(n=>hasClass(n,'publish-setup-column'));assert(hasClass(setup,'compact'),'the setup card opts into the compact shared layout');
   if(variant.route==='multi_publish')assert(setup.querySelector('.p011-account-picker'));else assert(setup.querySelectorAll('select').some(n=>n.attrs['aria-label']===(variant.context?'图文发布账号':'本次发布账号')&&visible(n)));
   assert(setup.querySelector('.publish-daily-schedule'));
   for(const label of ['开始日期','发布天数','每天发布条数','首条时间：小时','首条时间：分钟','两条间隔'])assert(setup.querySelectorAll('input,select').some(n=>n.attrs['aria-label']===label&&visible(n)),label);
   assert.equal(setup.querySelectorAll('h3,hr').filter(visible).length,0,'account and daily scheduling do not add separate large headings or separators');
   const inspection=v.ids.get('content').querySelector('.publish-inspection-column');assert(inspection);assert(inspection.children[1].querySelector('h3'),'execution details are below preview');
   assert.equal(calls.some(c=>/prepare|confirm|submit|execution\.start|resume/.test(c.op)),false);
  }finally{v.dispose();}
 });
 test(variant.name+' keeps progress and start in the same action row with execution details below preview',async()=>{
  const {v}=await fixture(variant);try{
   const bottom=v.find(n=>n.id==='publish-batch-progress'),footer=v.ids.get('footer'),action=footer.querySelector('.publish-action-row');
   assert(visible(bottom));assert.equal(bottom.parentElement,action);assert.equal(footer.children.length,1);
   assert(bottom.querySelector('.batch-progress-counts'));assert(bottom.querySelector('.batch-progress-publishing-bar'));
   const progress=v.ids.get('content').querySelector('.publish-progress-section');assert(progress);assert.equal(progress.parentElement.children[1],progress);
   const videos=v.find(n=>hasClass(n,'simple-videos-section')),layout=v.find(n=>hasClass(n,'publish-top-layout'));
   assert(layout.contains(videos));assert.equal(videos.contains(bottom),false);
   if(variant.context?.contentType==='image'){assert.equal(layout.children.length,3);assert.deepEqual(layout.querySelector('.publish-inspection-column').children.map(n=>n.className),['image-publish-detail','publish-progress-section']);}
   else{const inspection=layout.querySelector('.publish-inspection-column');assert.deepEqual(inspection.children.map(n=>n.className),['publish-video-detail','publish-progress-section']);assert.deepEqual(videos.querySelector('.publish-video-workspace').children.map(n=>n.className),['publish-video-list']);}
   assert(action.querySelectorAll('button').some(n=>n.textContent==='开始发布'&&visible(n)));
  }finally{v.dispose();}
 });
 test(variant.name+' compact layout does not reveal removed management or legacy scheduling controls',async()=>{
  const {v,calls}=await fixture(variant);try{
   const setup=v.find(n=>hasClass(n,'publish-setup-column'));
   const forbidden=new Set(['排期说明','高级设置','账号管理','管理账号','整批公共资料（默认继承每条成品）','更多发布安排','定时 · 按间隔','指定时间','立即发布']);
   assert.equal(setup.querySelectorAll('button,summary,h3').some(n=>forbidden.has(n.textContent)&&visible(n)),false);
   assert.equal(setup.querySelectorAll('select').some(n=>['普通每天时刻方式','图文发布方式','视频分配方式'].includes(n.attrs['aria-label'])&&visible(n)),false);
   assert.equal(calls.some(c=>/prepare|confirm|submit|execution\.start|resume/.test(c.op)),false);
  }finally{v.dispose();}
 });
}
