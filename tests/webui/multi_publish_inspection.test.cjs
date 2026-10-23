const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
function fixture({platform='channels',videoCount=1,accountCount=1,overrides={}}={}){
 const calls=[],settings={schema_version:2,targets:Array.from({length:accountCount},(_,i)=>({platform,account_id:'a'+i})),distribution_by_platform:{},start_policy:{kind:'relative_tomorrow_noon'},schedule:{mode:'daily',interval:'3',per_day:'1',days:'1',clock_mode:'interval',times:['12:00']},schedule_by_platform:{},common_fields:{},fields_by_platform:{}};
 const data={inspection_id:'check-1',state:'queued',stage:'等待上传检查',submitted:false,account_name:'账号甲',platform};
 const handler=async(op,p,r)=>{calls.push({op,p,r});if(overrides[op])return overrides[op](p,r);
  if(op==='multi_publish.catalog')return {items:['channels','bilibili','kuaishou','douyin'].map(platform=>({platform,label:{channels:'视频号',bilibili:'B站',kuaishou:'快手',douyin:'抖音'}[platform],login_ready:true,ordinary_ready:true,schedule_ready:true,publish_now_ready:true,field_schema:{}}))};
  if(op==='multi_publish.settings.read')return {settings,issues:[]};
  if(op==='multi_publish.settings.save')return {saved:true};
  if(op==='account.list')return {items:Array.from({length:accountCount},(_,i)=>({id:'a'+i,platform,nickname:'账号甲',is_default:!i,login_state:'valid'})),total:accountCount,groups:[]};
  if(op==='multi_publish.candidates')return {items:Array.from({length:videoCount},(_,i)=>({id:'v'+i,name:'视频'+i,display_name:'视频'+i,title:'标题'+i,body:'正文',tags:'',duration_ms:3000,category_ids:[],category_names:[]})),total:videoCount};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='media.preview')return {url:'fixture-media'};
  if(op==='multi_publish.prepare')return {prepared_id:'a'.repeat(48),quantity:1,requested_quantity:1,excluded_count:0,excluded_targets:[],requires_partial_consent:false,targets:[{platform,account_id:'a0',video_id:'v0',state:'waiting',can_submit:true}],summary:{videos:1,accounts:1,targets:1,ready:1,blocked:0,excluded:0}};
  if(op==='multi_publish.inspection.start'||op==='multi_publish.inspection.status')return {...data};
  if(op==='multi_publish.inspection.current')return {inspection:null};
  if(op==='multi_publish.inspection.cancel'){Object.assign(data,{state:'cancelled',stage:'检查已取消'});return {...data};}
  if(op==='multi_publish.confirm')return {id:'batch',quantity:1,submitted:false,version:2};
  if(op==='multi_publish.detail')return {id:'batch',deliveries:[{delivery_id:'d1',video_id:'v0',account_id:'a0',platform,can_submit:true,allowed_actions:['start']}],summary:{videos:1,accounts:1,targets:1,ready:1,blocked:0,excluded:0}};
  if(op==='multi_publish.execution.check')return {id:'batch',delivery_ids:['d1'],digest:'digest',available:true,requires_partial_consent:false};
  if(op==='multi_publish.execution.start')return {accepted:true,submitted:false};
  throw Error('unexpected '+op);
 };return {calls,settings,data,handler};
}
const publication=calls=>calls.filter(c=>c.op==='multi_publish.confirm'||c.op==='multi_publish.execution.start'||c.op.startsWith('publish.prepare'));
async function select(v,all=false){if(all)await v.click('全选','视频全选');else{const n=v.find(n=>n.attrs['aria-label']==='选择 视频0');n.checked=true;n.onchange();await v.flush();}}

for(const platform of ['channels','bilibili','kuaishou'])test(platform+' distribution keeps direct publishing and has no upload-only check entry',async()=>{
 const f=fixture({platform}),v=await mount(f.handler,'multi_publish');await select(v);
 assert.throws(()=>v.find(n=>n.textContent==='仅检查，不发表'));
 assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.inspection.')),false);
 await v.click('开始发布');
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.prepare').length,1);
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.confirm').length,1);
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.execution.start').length,1);
 assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.inspection.')),false);v.dispose();
});

test('optional preview reads the publishing arrangement without invoking a separate upload check',async()=>{
 const f=fixture(),v=await mount(f.handler,'multi_publish');await select(v);await v.click('预览发布安排');
 assert.ok(v.find(n=>n.textContent==='本次发布安排预览'));
 assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.inspection.')),false);
 assert.equal(publication(f.calls).length,0);v.dispose();
});

test('old inspection UI memory does not recreate a removed dialog or trigger check APIs on navigation',async()=>{
 const f=fixture(),v=await mount(f.handler,'multi_publish',undefined,{P011MultiInspection:{...f.data,state:'running'}});
 assert.equal(v.getId('overlay').hidden,true);assert.throws(()=>v.find(n=>n.textContent==='取消检查'||n.textContent==='仅检查，不发表'));
 assert.equal(f.calls.some(c=>c.op.startsWith('multi_publish.inspection.')),false);v.dispose();
});

test('ordinary publication has no inspection entry or transport changes',async()=>{
 const {baseData}=require('./support/commerce_view.cjs'),calls=[];
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='publish.browser_mode.read')return {mode:'background'};if(op==='account.list')return {items:[],total:0,groups:[]};if(op==='media.thumbnail')return {image:''};return baseData(op);},'publish');
 assert.throws(()=>v.find(n=>n.textContent==='仅检查，不发表'));assert.equal(calls.some(c=>c.op.includes('inspection.')),false);v.dispose();
});
