const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
function fixture({targets=[{platform:'douyin',account_id:'d1'},{platform:'douyin',account_id:'d2'}],saved=true,missing=false}={}){
 const calls=[],routes=[],accounts=[{id:'d1',platform:'douyin',nickname:'主号',login_state:'valid',is_default:true},{id:'d2',platform:'douyin',nickname:'副号',remark:'门店号',login_state:'expired'},{id:'b1',platform:'bilibili',nickname:'B站账号',login_state:'valid',is_default:true}];
 const settings={schema_version:2,targets,distribution_by_platform:{douyin:'whole_batch'},start_policy:{kind:'relative_tomorrow_noon'},schedule:{mode:'daily',interval:'3',per_day:'1',days:'1',clock_mode:'interval',times:['12:00']},schedule_by_platform:{},common_fields:{},fields_by_platform:{bilibili:{category_id:9}}};
 const handler=async(op,p,r)=>{calls.push({op,p,r});if(op==='multi_publish.catalog')return {items:['douyin','bilibili'].map((platform,i)=>({platform,label:i?'B站':'抖音',login_ready:true,ordinary_ready:true,publish_now_ready:true,schedule_ready:true,field_schema:i?{category_id:{choices:[{id:9,label:'科技'}]}}:{}}))};if(op==='multi_publish.settings.read')return {settings,has_saved_settings:saved,issues:missing?['saved_account_unavailable']:[]};if(op==='multi_publish.settings.save'){Object.assign(settings,structuredClone(p.settings));return {settings,saved:true};}if(op==='account.list')return {items:accounts,total:accounts.length};if(op==='multi_publish.candidates')return {items:[{id:'v',name:'视频',display_name:'视频',title:'标题',body:'正文',tags:'标签',duration_ms:1000}],total:1};if(op==='multi_publish.presets.list')return {items:[]};if(op==='publish.browser_mode.read')return {mode:'background'};if(op==='multi_publish.prepare')return {prepared_id:'x'.repeat(48),intent_id:r.request_id,targets:[],summary:{videos:1,accounts:p.targets.length,targets:p.targets.length,ready:p.targets.length,blocked:0,excluded:0}};if(op==='media.thumbnail')return {image:''};if(op==='media.preview')return {url:'fixture-media'};throw Error(op);};
 return {calls,settings,routes,handler,extra:{P011Router:{go:(...args)=>routes.push(args)}}};
}
const input=(v,id)=>v.find(n=>n.dataset.account===id);
async function choose(v,id,checked){const cb=input(v,id);cb.checked=checked;await cb.onchange({isTrusted:true});await v.flush();}
async function preview(v){const cb=v.find(n=>n.attrs['aria-label']==='选择 视频');cb.checked=true;cb.onchange();await v.click('预览发布安排');}
test('one grouped account picker replaces the lossy per-platform primary dropdown',async()=>{
 const f=fixture(),v=await mount(f.handler,'multi_publish',undefined,f.extra);
 assert.throws(()=>v.find(n=>n.tag==='select'&&n.attrs['aria-label']==='抖音发布账号'));
 const trigger=v.find(n=>n.attrs['aria-controls']==='multi-publish-accounts');assert.equal(trigger.textContent,'已选 2 个账号');
 await v.click(trigger.textContent);assert.equal(v.find(n=>n.id==='multi-publish-accounts').classList.contains('hide'),false);
 assert.deepEqual(v.find(n=>n.id==='multi-publish-accounts').querySelectorAll('legend').map(n=>n.textContent),['抖音','B站']);
 await choose(v,'b1',true);assert.equal(input(v,'d1').checked,true);assert.equal(input(v,'d2').checked,true);
 await choose(v,'d2',false);assert.equal(input(v,'d1').checked,true);await choose(v,'d2',true);
 await preview(v);assert.deepEqual(f.calls.find(c=>c.op==='multi_publish.prepare').p.targets.map(t=>t.account_id).sort(),['b1','d1','d2']);
 assert.equal(f.settings.distribution_by_platform.douyin,'whole_batch');assert.equal(f.calls.some(c=>/confirm|execution.start|login.start/.test(c.op)),false);v.dispose();
});
test('saved missing accounts stay visible and removable without substituting another account',async()=>{
 const f=fixture({targets:[{platform:'douyin',account_id:'gone'}],missing:true}),v=await mount(f.handler,'multi_publish',undefined,f.extra);
 assert.equal(input(v,'gone').checked,true);assert.equal(input(v,'d1').checked,false);assert(v.find(n=>n.textContent.includes('保存的账号已不存在：gone')));
 await choose(v,'gone',false);assert.deepEqual(f.settings.targets,[]);assert.equal(input(v,'d1').checked,false);v.dispose();
});
test('explicit empty saved selection stays empty and first use keeps only its existing default',async()=>{
 for(const saved of [true,false]){const f=fixture({targets:[],saved}),v=await mount(f.handler,'multi_publish',undefined,f.extra);assert.equal(input(v,'d1').checked,!saved);assert.equal(input(v,'b1').checked,false);assert.equal(input(v,'d2').checked,false);v.dispose();}
});
test('login hints and current defaults are readable without adding settings buttons',async()=>{
 const f=fixture(),v=await mount(f.handler,'multi_publish',undefined,f.extra);assert(v.find(n=>n.textContent==='需要登录'));assert(v.find(n=>n.textContent==='门店号'));assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='发布设置'));assert.throws(()=>v.find(n=>n.tag==='button'&&n.textContent==='管理账号'));v.dispose();
});
test('legacy distribution stays archived while the new page uses one unified daily schedule',async()=>{
 const f=fixture(),v=await mount(f.handler,'multi_publish',undefined,f.extra);assert.throws(()=>v.find(n=>n.attrs['aria-label']==='抖音视频分配'));assert.throws(()=>v.find(n=>n.textContent==='更多发布安排'));assert.throws(()=>v.find(n=>n.attrs['aria-label']==='抖音单独排期'));await preview(v);const p=f.calls.find(c=>c.op==='multi_publish.prepare').p;assert.deepEqual(p.distribution_by_platform,{});assert.deepEqual(p.schedule_by_platform,{});assert.equal(p.execution_mode,'platform_schedule');assert.equal(f.settings.distribution_by_platform.douyin,'whole_batch');v.dispose();
});

test('per-video editor only exposes one content draft and preserves old platform settings',async()=>{
 const f=fixture({targets:[{platform:'bilibili',account_id:'b1'}]}),v=await mount(f.handler,'multi_publish',undefined,f.extra);await v.click('预览 / 编辑');assert(v.find(n=>n.attrs['aria-label']==='本条标题'));assert.throws(()=>v.find(n=>n.attrs['aria-label']==='本次发布封面'));assert.throws(()=>v.find(n=>n.attrs['aria-label']==='本条B站分区'));assert.throws(()=>v.find(n=>n.textContent==='B站资料'));assert.throws(()=>v.find(n=>n.textContent==='B站整批资料覆盖'));assert.equal(f.settings.fields_by_platform.bilibili.category_id,9);v.dispose();
});
test('accounts outside the platform catalog do not gain an unsupported publishing entry',async()=>{
 const f=fixture(),base=f.handler,handler=async(op,p,r)=>{if(op==='account.list'){const result=await base(op,p,r);result.items.push({id:'other',platform:'unknown',nickname:'未接入平台',login_state:'valid'});result.total++;return result;}return base(op,p,r);},v=await mount(handler,'multi_publish');assert.throws(()=>input(v,'other'));assert.equal(input(v,'d1').checked,true);v.dispose();
});
test('new preparation inherits account settings without replaying obsolete platform fields or temporary credentials',async()=>{
 const f=fixture({targets:[{platform:'bilibili',account_id:'b1'}]}),old={category_id:9,creation_statement_id:3,location:{token:'old-location'},declaration:{token:'old-declaration'},title:'保留的本条标题',cover:null};f.settings.fields_by_platform.bilibili=structuredClone(old);f.settings.common_fields={body:'公共正文',content_authorization:true,channels_original:true};const v=await mount(f.handler,'multi_publish');await preview(v);const p=f.calls.find(c=>c.op==='multi_publish.prepare').p;assert.deepEqual(p.targets[0].fields,{});assert.deepEqual(p.common_fields,{});assert.equal('inherit_account_settings'in p,false);assert.deepEqual(f.settings.fields_by_platform.bilibili,old);assert.equal(f.calls.some(c=>/confirm|execution.start/.test(c.op)),false);v.dispose();
});
module.exports={fixture,input,choose,preview};


test('多平台与混剪共用全选操作，批量更新一次并保留原账号配置',async()=>{
 const f=fixture();f.settings.targets[0].fields={title:'保留账号设置'};
 const v=await mount(f.handler,'multi_publish',undefined,f.extra);
 const all=()=>v.find(n=>n.dataset.accountAll==='true');assert.equal(all().indeterminate,true);
 const reads=f.calls.filter(c=>c.op==='multi_publish.candidates').length;
 all().checked=true;await all().onchange({isTrusted:true});await v.flush();
 assert.equal(input(v,'d1').checked,true);assert.equal(input(v,'d2').checked,true);assert.equal(input(v,'b1').checked,true);
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.candidates').length,reads+1);
 assert.deepEqual(f.settings.targets.find(t=>t.account_id==='d1').fields,{title:'保留账号设置'});
 all().checked=false;await all().onchange({isTrusted:true});await v.flush();
 assert.equal(input(v,'d1').checked,false);assert.equal(input(v,'d2').checked,false);assert.equal(input(v,'b1').checked,false);
 assert.equal(f.calls.some(c=>/confirm|execution.start|login.start/.test(c.op)),false);v.dispose();
});
