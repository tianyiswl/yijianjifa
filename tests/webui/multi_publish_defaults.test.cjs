// Actual UI handlers over an isolated file-backed settings API. No platform I/O.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {mount}=require('./support/commerce_view.cjs');
const platforms=['bilibili','channels','kuaishou'],labels={bilibili:'B站',channels:'视频号',kuaishou:'快手',douyin:'抖音'};
const choices=[{choice:'none',name:'不添加声明'},{choice:'ai_generated',name:'包含 AI 内容'}];
const schemas={
 bilibili:{category_choice:{choices_source:'builtin',default:'human:knowledge',choices:[{choice:'human:knowledge',name:'知识',mode:'human'},{choice:'human:tech',name:'科技数码',mode:'human'}]},creation_statement_choice:{choices_source:'builtin',default:'no_mark',choices:[{choice:'no_mark',name:'不添加声明'},{choice:'ai_generated',name:'包含 AI 内容'}]},content_authorization:{type:'boolean'},reprint_source:{max_length:200}},
 channels:{content_declaration_choice:{choices},channels_original:{type:'boolean'},channels_short_title:{max_length:16}},
 kuaishou:{content_declaration_choice:{choices}}
};
const defaults=()=>({schema_version:2,targets:[],distribution_by_platform:{},start_policy:{kind:'relative_tomorrow_noon'},schedule:{mode:'daily',interval:'3',per_day:'3',days:'5',clock_mode:'interval',times:['12:00']},schedule_by_platform:{},common_fields:{},fields_by_platform:{}});
function fixture({accounts=true,douyin=false,hasSaved=true}={}){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'p011-defaults-ui-')),file=path.join(directory,'settings.json'),calls=[];
 fs.writeFileSync(file,JSON.stringify(defaults()));
 const read=()=>JSON.parse(fs.readFileSync(file,'utf8'));
 const directoryPlatforms=douyin?['douyin',...platforms]:platforms;let failSave=false,saved=hasSaved;
 const handler=async(op,p)=>{
  calls.push({op,p:structuredClone(p)});
  if(op==='multi_publish.catalog')return {items:directoryPlatforms.map(platform=>({platform,label:labels[platform],login_ready:accounts,ordinary_ready:accounts,schedule_ready:accounts,publish_now_ready:accounts,field_schema:schemas[platform]||{}}))};
  if(op==='multi_publish.settings.read')return {settings:read(),issues:[],has_saved_settings:saved};
  if(op==='multi_publish.settings.save'){
   if(failSave)throw Error('离线保存失败');
   fs.writeFileSync(file,JSON.stringify(p.settings));saved=true;return {saved:true,settings:read()};
  }
  if(op==='account.list')return {items:accounts?directoryPlatforms.map(platform=>({id:platform+'-account',platform,nickname:'离线'+labels[platform],is_default:true})):[],total:accounts?directoryPlatforms.length:0,groups:[]};
  if(op==='multi_publish.candidates')return {items:[{id:'video',display_name:'离线视频',title:'合成标题',body:'合成正文',tags:'',duration_ms:3000,category_names:[]}],total:1};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='multi_publish.prepare')return {prepared_id:'a'.repeat(48),targets:[],summary:{videos:1,accounts:p.targets.length,targets:p.targets.length,ready:0,blocked:p.targets.length,excluded:0}};
  throw Error('Unexpected operation '+op);
 };
 return {calls,read,handler,set failSave(value){failSave=value;},dispose(){fs.rmSync(directory,{recursive:true,force:true});}};
}
const find=(view,label)=>view.find(n=>n.attrs['aria-label']===label);
async function selection(view,platform,checked){const input=find(view,'发布到'+labels[platform]);input.checked=checked;await input.onchange();await view.flush();}
async function open(view,platform,withAccount=true){const label=labels[platform]+'发布设置'+(withAccount?' '+platform+'-account':'');assert.equal(find(view,label).disabled,false);await view.click('发布设置',label);}
function setValues(view,platform){const prefix=labels[platform]+'整批';
 if(platform==='bilibili'){const category=find(view,prefix+'分区');category.value='human:tech';category.onchange();const statement=find(view,prefix+'创作声明');statement.value='ai_generated';statement.onchange();return {category_choice:'human:tech',creation_statement_choice:'ai_generated',reprint_source:'',content_authorization:false};}
 const statement=find(view,prefix+'内容声明');statement.value='ai_generated';statement.onchange();
 if(platform==='channels'){const short=find(view,prefix+'短标题（选填）');short.value='离线默认短标题';short.oninput();return {content_declaration_choice:'ai_generated',channels_short_title:'离线默认短标题',channels_original:false};}
 return {content_declaration_choice:'ai_generated'};
}
function assertValues(view,platform){const prefix=labels[platform]+'整批';
 if(platform==='bilibili'){assert.equal(find(view,prefix+'分区').value,'human:tech');assert.equal(find(view,prefix+'创作声明').value,'ai_generated');assert.equal(find(view,prefix+'内容授权').checked,false);}
 else{assert.equal(find(view,prefix+'内容声明').value,'ai_generated');if(platform==='channels'){assert.equal(find(view,prefix+'短标题（选填）').value,'离线默认短标题');assert.equal(find(view,prefix+'声明原创').checked,false);}}
}
function assertLocalOnly(calls){assert.equal(calls.some(c=>/platform_options|login|upload|confirm|execution|finished.save/.test(c.op)),false);}
for(const platform of platforms)test(labels[platform]+' unchecked default settings save, reopen and follow re-selection without adding publish targets',async()=>{
 const f=fixture();let view;
 try{
  view=await mount(f.handler,'multi_publish');assert.equal(find(view,'发布到'+labels[platform]).checked,false);
  await open(view,platform);const expected=setValues(view,platform);await view.click('保存发布设置');
  assert.equal(view.getId('overlay').hidden,true);assert.deepEqual(f.read().fields_by_platform[platform],expected);
  assert.deepEqual(f.read().targets,[]);assert.equal(find(view,'发布到'+labels[platform]).checked,false);
  assert.match(view.getId('toast').textContent,/默认发布设置已保存/);
  view.dispose();view=await mount(f.handler,'multi_publish');
  assert.equal(find(view,'发布到'+labels[platform]).checked,false);await open(view,platform);assertValues(view,platform);await view.click('取消');
  await selection(view,platform,true);await selection(view,platform,false);
  assert.deepEqual(f.read().fields_by_platform[platform],expected);assert.deepEqual(f.read().targets,[]);
  await selection(view,platform,true);await open(view,platform);assertValues(view,platform);await view.click('取消');
  const video=find(view,'选择 离线视频');video.checked=true;video.onchange();await view.click('预览发布安排');
  const request=f.calls.filter(c=>c.op==='multi_publish.prepare').at(-1).p;
  assert.deepEqual(request.targets.map(t=>[t.platform,t.account_id]),[[platform,platform+'-account']]);
  assert.deepEqual(request.targets[0].fields,expected);assert.deepEqual(request.common_fields,{});
  assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
test('saved defaults for all platforms survive while only selected Channels enters prepare',async()=>{
 const f=fixture();let view;
 try{
  view=await mount(f.handler,'multi_publish');const expected={};
  for(const platform of platforms){await open(view,platform);expected[platform]=setValues(view,platform);await view.click('保存发布设置');}
  assert.deepEqual(f.read().fields_by_platform,expected);assert.deepEqual(f.read().targets,[]);
  view.dispose();view=await mount(f.handler,'multi_publish');await selection(view,'channels',true);
  const video=find(view,'选择 离线视频');video.checked=true;video.onchange();await view.click('预览发布安排');
  const request=f.calls.filter(c=>c.op==='multi_publish.prepare').at(-1).p;
  assert.equal(request.targets.length,1);assert.equal(request.targets[0].platform,'channels');
  assert.deepEqual(request.targets[0].fields,expected.channels);assert.deepEqual(request.schedule_by_platform,{});assert.deepEqual(request.distribution_by_platform,{});
  assert.deepEqual(f.read().fields_by_platform,expected);assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
test('default settings remain configurable before an account exists',async()=>{
 const f=fixture({accounts:false});let view;
 try{
  view=await mount(f.handler,'multi_publish');
  for(const platform of platforms){await open(view,platform,false);const expected=setValues(view,platform);await view.click('保存发布设置');assert.deepEqual(f.read().fields_by_platform[platform],expected);}
  assert.deepEqual(f.read().targets,[]);assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
test('cancelling edited defaults does not write, and failed persistence does not claim saved',async()=>{
 const f=fixture();let view;
 try{
  view=await mount(f.handler,'multi_publish');await open(view,'channels');setValues(view,'channels');await view.click('取消');
  assert.deepEqual(f.read().fields_by_platform,{});assert.equal(f.calls.some(c=>c.op==='multi_publish.settings.save'),false);
  await open(view,'channels');setValues(view,'channels');f.failSave=true;await view.click('保存发布设置');
  assert.equal(view.getId('overlay').hidden,false);assert.ok(view.find(n=>n.textContent==='设置保存失败：离线保存失败'));
  assert.deepEqual(f.read().fields_by_platform,{});assert.doesNotMatch(view.getId('toast').textContent,/默认发布设置已保存/);
  f.failSave=false;await view.click('保存发布设置');assert.equal(view.getId('overlay').hidden,true);
  assert.equal(f.read().fields_by_platform.channels.content_declaration_choice,'ai_generated');assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
test('an explicitly empty platform selection stays empty after saving defaults and reopening with a default Douyin account',async()=>{
 const f=fixture({douyin:true,hasSaved:false});let view;
 try{
  view=await mount(f.handler,'multi_publish');assert.equal(find(view,'发布到抖音').checked,true);
  await selection(view,'douyin',false);await open(view,'channels');const expected=setValues(view,'channels');await view.click('保存发布设置');
  assert.deepEqual(f.read().targets,[]);view.dispose();view=await mount(f.handler,'multi_publish');
  for(const platform of ['douyin',...platforms])assert.equal(find(view,'发布到'+labels[platform]).checked,false);
  assert.deepEqual(f.read().fields_by_platform.channels,expected);assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
test('failed default save followed by cancel cannot leak abandoned fields into a later automatic save',async()=>{
 const f=fixture();let view;
 try{
  view=await mount(f.handler,'multi_publish');await open(view,'channels');setValues(view,'channels');f.failSave=true;await view.click('保存发布设置');await view.click('取消');
  f.failSave=false;const title=find(view,'整批公共标题');title.value='之后修改公共资料';title.oninput();await view.flush();
  assert.equal(f.read().common_fields.title,'之后修改公共资料');assert.deepEqual(f.read().fields_by_platform,{});
  await open(view,'channels');assert.equal(find(view,'视频号整批内容声明').value,'none');await view.click('取消');assertLocalOnly(f.calls);
 }finally{view?.dispose();f.dispose();}
});
