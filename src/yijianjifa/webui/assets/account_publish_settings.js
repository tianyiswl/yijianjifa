/* Account defaults editor. Saving never prepares a task or submits a video. */
(function(root){'use strict';
function validate(platform,fields){
 if(platform==='channels'&&fields.channels_short_title&&(fields.channels_short_title.length<6||fields.channels_short_title.length>16||!/^[\u2103\u4E00-\u9FA5A-Za-z0-9《》"“”:：+?？%\s]+$/.test(fields.channels_short_title)))throw Error('视频号短标题需为6至16个支持的字符，或留空。');
 if(platform==='bilibili'){
  const reprint=fields.creation_statement_choice==='reprint'||fields.creation_statement_id===-2,source=fields.reprint_source||'';
  if(reprint&&(!source.trim()||fields.content_authorization===true))throw Error('转载需要填写来源，并取消自制内容授权。');
  if(!reprint&&source)throw Error('选择非转载声明时，请清空转载来源。');
 }
}
function create(h){
 const {el,button,host,account,api,intent,modal,dialog}=h;
 let alive=true,opened=false,saving=false,loading=false,revision=0,controller=null;
 const live=()=>alive&&h.isAlive?.()!==false;
 const card=el('section',undefined,'card account-publish-settings'),summary=el('p','设置一次，抖音发布、多平台分发和混剪会自动沿用。','muted');
 const launch=button('发布设置',open);launch.setAttribute('aria-label','发布设置 '+account.id);
 card.append(el('h3','发布设置'),summary,launch);host.append(card);
 function close(){if(saving)return false;revision++;controller?.dispose();controller=null;opened=false;loading=false;modal.style.display='none';dialog.classList.remove('account-settings-dialog');launch.disabled=false;return true;}
 async function open(){
  if(!live()||opened)return;opened=true;loading=true;const ticket=++revision;
  modal.style.display='grid';dialog.classList.add('account-settings-dialog');dialog.replaceChildren(el('h3',(account.remark||account.nickname)+' · 发布设置'),el('p','正在读取设置…'),button('取消',close));
  try{
   const data=await api('account.publish_settings.read',{account_id:account.id});if(!live()||!opened||ticket!==revision)return;
   const values=JSON.parse(JSON.stringify(data.fields||{})),body=el('div',undefined,'account-settings-body'),error=el('p',undefined,'note warn'),foot=el('div',undefined,'foot');
   dialog.replaceChildren(el('h3',(account.remark||account.nickname)+' · 发布设置'),el('p','以后自动用于该账号的新任务。标题、正文和封面跟随每条视频。','muted'),body,error,foot);
   const current=()=>live()&&opened&&ticket===revision;
   if(root.P011MultiPlatformFields.supported(data.schema))controller=root.P011MultiPlatformFields.create({el,button,host:body,api,platform:data.platform,account_id:account.id,schema:data.schema,values,prefix:'默认',isAlive:current});
   let douyin={},locationPatch=false,declarationPatch=false,location=data.douyin?.location||null;
   if(data.platform==='douyin'){
    const choice=el('select'),choiceStatus=el('p',undefined,'muted');choice.setAttribute('aria-label','默认内容声明');choice.disabled=true;choice.append(new Option('正在读取声明…',''));body.append(el('label','内容声明'),choice,choiceStatus);
    try{const policy=await api('publish.declarations.query',{account_id:account.id});if(!current())return;if(!policy.available)throw Error('内容声明暂不可用，请重新打开设置。');choice.replaceChildren(new Option('不添加声明',''),...(policy.items||[]).map(r=>new Option(r.label,r.id)));choice.value=data.douyin?.declaration_id||'';if(choice.value&&!(policy.items||[]).some(r=>r.id===choice.value)){choice.append(new Option('原声明需要重新选择',choice.value));choiceStatus.textContent='原选择已保留，请重新选择适合实际内容的声明。';}else choiceStatus.textContent='按实际内容选择；未设置时不添加。';choice.disabled=false;choice.onchange=()=>{declarationPatch=true;douyin.declaration_id=choice.value||null;};}catch(e){if(current())choiceStatus.textContent=e.message;}
    if(!current())return;
    const details=el('details'),locationText=el('p',location?location.name+' · '+location.address:'不添加地点','muted'),query=el('input'),scope=el('select'),results=el('select'),status=el('p',undefined,'muted');
    details.append(el('summary','发布地点（选填）'));query.setAttribute('aria-label','默认发布地点搜索');query.placeholder='输入店铺或地点名称';query.value=location?.query||'';scope.setAttribute('aria-label','默认地点搜索范围');scope.append(new Option('本地','local'),new Option('国内','national'));scope.value=location?.scope||'local';results.setAttribute('aria-label','默认发布地点');results.hidden=true;
    let token=null,rows=[],searching=false;
    const search=button('搜索地点',async()=>{if(!current()||searching||saving)return;if(!query.value.trim()){status.textContent='请先输入地点名称。';return;}searching=true;search.disabled=true;results.disabled=true;status.textContent='正在搜索…';try{const found=await api('publish.locations.search',{account_id:account.id,query:query.value.trim(),scope:scope.value,refresh:true});if(!current())return;if(!found.available||!found.token)throw Error('地点暂时无法读取，请确认账号已登录。');token=found.token;rows=found.items||[];results.replaceChildren(new Option('请选择地点',''),...rows.map(r=>new Option(r.name+' · '+r.address,r.id)));results.value='';results.hidden=!rows.length;results.disabled=false;status.textContent=rows.length?'选择需要添加的地点。':'未找到地点，请换个名称。';}catch(e){if(current())status.textContent=e.message;}finally{searching=false;if(current()){search.disabled=false;results.disabled=false;}}});
    results.onchange=()=>{if(!current()||saving||!token)return;const row=rows.find(r=>r.id===results.value);if(!row)return;locationPatch=true;douyin.location={token,id:row.id};locationText.textContent=row.name+' · '+row.address;};
    const clear=button('不添加地点',()=>{if(saving)return;locationPatch=true;douyin.location=null;locationText.textContent='不添加地点';results.value='';});
    details.append(locationText,query,scope,search,results,status,clear);body.append(details);
   }else if(!controller)body.append(el('p','该平台不需要额外设置，发布时自动使用视频资料。','muted'));
   if(!current())return;
   const cancel=button('取消',close),save=button('保存',async()=>{
    if(!current()||saving||loading)return;saving=true;save.disabled=cancel.disabled=true;error.textContent='';
    const inputs=[...body.querySelectorAll('input,select,button,textarea')],disabled=inputs.map(n=>n.disabled);inputs.forEach(n=>n.disabled=true);
    try{controller?.materialize();validate(data.platform,values);const payload={account_id:account.id,fields:values};if(declarationPatch||locationPatch)payload.douyin=douyin;await intent.run('account.publish_settings.save',payload);if(current()){summary.textContent='已保存，新任务会自动沿用。';saving=false;close();h.notice?.('发布设置已保存。');}}
    catch(e){if(current())error.textContent='保存失败：'+e.message;}
    finally{saving=false;if(current()){save.disabled=cancel.disabled=false;inputs.forEach((n,i)=>n.disabled=disabled[i]);}}
   },'btn primary');foot.append(cancel,save);loading=false;
  }catch(e){if(live()&&opened&&ticket===revision){loading=false;dialog.replaceChildren(el('h3','发布设置'),el('p','设置未能读取：'+e.message),button('关闭',close));}}
 }
 return {get isOpen(){return opened;},get busy(){return saving||loading;},close,dispose(){alive=false;saving=false;close();card.remove();}};
}
root.P011AccountPublishSettings={create};
})(globalThis);
