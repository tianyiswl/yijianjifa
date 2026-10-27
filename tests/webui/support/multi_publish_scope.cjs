// Actual distribution handlers with synthetic data; no browser, media or platform I/O.
const {mount}=require('./commerce_view.cjs');
async function requestAfterSelectionChange({scenario='video',videoIds=['old-video','selected-video'],accountIds=['channels-account','bili-account','bili-other-account']}={}){
 const calls=[],schedule={mode:'daily',interval:'3',per_day:'3',days:'5',clock_mode:'interval',times:['12:00']};
 const settings={schema_version:2,targets:[{account_id:accountIds[0],platform:'channels'}],distribution_by_platform:{},start_policy:{kind:'relative_tomorrow_noon'},schedule,schedule_by_platform:{},common_fields:{},fields_by_platform:{channels:{content_declaration_choice:'none',channels_original:false}}};
 if(scenario.startsWith('platform')){
  settings.targets.push({account_id:accountIds[1],platform:'bilibili'});
  settings.distribution_by_platform.bilibili='whole_batch';
  settings.schedule_by_platform.bilibili={start_policy:{kind:'relative_tomorrow_noon'},schedule:{...schedule}};
  settings.fields_by_platform.bilibili={category_choice:'human:knowledge',creation_statement_choice:'no_mark',content_authorization:false};
 }
 const rows=videoIds.map((id,i)=>({id,display_name:'离线视频'+i,title:'合成标题'+i,body:'合成正文',tags:'',duration_ms:3000,category_names:[]}));
 const platforms=['channels','bilibili'],labels=['视频号','B站'];
 const handler=async(op,p)=>{
  calls.push({op,p:structuredClone(p)});
  if(op==='multi_publish.catalog')return {items:platforms.map((platform,i)=>({platform,label:labels[i],login_ready:true,ordinary_ready:true,schedule_ready:true,publish_now_ready:true,field_schema:platform==='channels'?{channels_short_title:{max_length:16},content_declaration_choice:{choices:[{choice:'none',name:'不添加声明'},{choice:'ai_generated',name:'人工智能生成'}]},channels_original:{type:'boolean'}}:{}}))};
  if(op==='multi_publish.settings.read')return {settings,issues:[]};
  if(op==='multi_publish.settings.save')return {saved:true,settings:p.settings};
  if(op==='account.list')return {items:accountIds.map((id,i)=>({id,platform:i===0?'channels':'bilibili',nickname:'合成账号'+i,is_default:i<2,capabilities:{ordinary:'available'},login_state:'valid'})),total:accountIds.length,groups:[]};
  if(op==='multi_publish.candidates')return {items:rows,total:rows.length};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='media.preview')return {url:'fixture-media'};
  if(op==='multi_publish.prepare')return {prepared_id:'a'.repeat(48),targets:[{platform:'channels',account_id:accountIds[0],video_id:videoIds[1],state:'waiting_configuration',hold_reason:'account_verification_required',reason_message:'离线检查停止于准备'}],summary:{videos:1,accounts:1,targets:1,ready:0,blocked:1,excluded:0}};
  throw Error('Unexpected operation '+op);
 };
 const view=await mount(handler,'multi_publish');
 try{
  if(scenario.startsWith('video')){
   await view.click('预览 / 编辑');
   if(scenario==='video'){const content=view.find(n=>n.attrs['aria-label']==='本条标题');content.value='保留旧视频草稿';content.oninput();}
  }else if(scenario.startsWith('platform')){
   const account=view.find(n=>n.dataset.account===accountIds[1]);account.checked=false;await account.onchange({isTrusted:true});
  }else throw Error('Unknown fixture scenario');
  const selected=view.find(n=>n.attrs['aria-label']==='选择 离线视频1');selected.checked=true;selected.onchange();
  await view.flush();await view.click('开始发布');
  const first=calls.filter(c=>c.op==='multi_publish.prepare').at(-1)?.p;
  if(!first)throw Error('No actual prepare request');
  // The preparation stub stops before confirm and releases selection. Re-select
  // the old scope to prove projection does not destroy temporary edit choices.
  await view.click('返回修改');
  if(scenario.startsWith('video')){
   const old=view.find(n=>n.attrs['aria-label']==='选择 离线视频0');old.checked=true;old.onchange();
  }else{
   const account=view.find(n=>n.dataset.account===accountIds[2]);account.checked=true;await account.onchange({isTrusted:true});
  }
  await view.click('预览发布安排');
  return {first,reselected:calls.filter(c=>c.op==='multi_publish.prepare').at(-1).p,saved:calls.filter(c=>c.op==='multi_publish.settings.save').map(c=>c.p.settings),operations:calls.map(c=>c.op)};
 }finally{view.dispose();}
}
module.exports={requestAfterSelectionChange};
if(require.main===module){
 const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
 requestAfterSelectionChange(input).then(data=>process.stdout.write(JSON.stringify(data))).catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
}
