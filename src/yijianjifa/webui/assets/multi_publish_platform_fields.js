/* Shared platform-only fields. Reading options never prepares or submits a video. */
(function(root){'use strict';
const KEYS=['category_choice','creation_statement_choice','category_id','category_name','category_mode','creation_statement_id','creation_statement_name','content_authorization','reprint_source'];
function supported(schema){return !!(schema?.creation_statement_choice||schema?.creation_statement_id||schema?.content_declaration_choice||schema?.channels_original||schema?.channels_short_title);}
function keysFor(schema){return [...(schema?.creation_statement_choice||schema?.creation_statement_id?KEYS:[]),...(schema?.content_declaration_choice?['content_declaration_choice']:[]),...(schema?.channels_original?['channels_original']:[]),...(schema?.channels_short_title?['channels_short_title']:[])];}
function createShortTitle(h){const {el,host,values}=h,base=h.effective||{},input=el('input');let alive=true;const live=()=>alive&&h.isAlive?.()!==false&&host.isConnected!==false;input.setAttribute('aria-label',(h.prefix||'视频号')+'短标题（选填）');input.maxLength=16;const draw=()=>{input.value=Object.hasOwn(values,'channels_short_title')?values.channels_short_title:base.channels_short_title||'';};draw();input.placeholder='留空时不设置短标题';input.oninput=()=>{if(live()){values.channels_short_title=input.value;h.onChange?.();}};host.append(el('label','视频号短标题（选填）'),input,el('p','留空即可；填写时需 6–16 个字符。标题和正文仍继承成品资料。','hint'));return {keys:['channels_short_title'],materialize(){},reset(){if(live()){if(h.onInherit){input.value='';input.placeholder='保存后恢复公共短标题';}else draw();}},dispose(){alive=false;},get refreshing(){return false;}};}
function createLocal(h){
 const {el,host,values}=h,schema=h.schema||{},base=h.effective||{},prefix=h.prefix||'本平台';let alive=true;
 const live=()=>alive&&h.isAlive?.()!==false&&host.isConnected!==false;
 const inheritedReset=new Set(),current=key=>Object.hasOwn(values,key)?values[key]:inheritedReset.has(key)?undefined:base[key],section=el('section',undefined,'publish-video-form'),controls=[];
 const status=el('p',undefined,'hint');status.setAttribute('role','status');status.setAttribute('aria-label',prefix+'内容声明说明');
 const choiceKey='content_declaration_choice',originalKey='channels_original';let statement=null,original=null,originalState=null;
 const seen=new Set(),rows=(Array.isArray(schema[choiceKey]?.choices)?schema[choiceKey].choices:[]).filter(row=>{if(typeof row?.choice!=='string'||!row.choice||typeof row.name!=='string'||!row.name||seen.has(row.choice))return false;seen.add(row.choice);return true;});
 const hasNone=rows.some(row=>row.choice==='none'),validChoice=value=>typeof value==='string'&&rows.some(row=>row.choice===value);
 const declarationHelp=rows.length===1&&hasNone?'当前支持不添加内容声明；其他声明选项仍待验证。无需登录或读取即可选择。':'内容声明可直接选择，无需登录或读取；请按实际内容选择。';
 function drawDeclaration(){if(!statement)return;const selected=current(choiceKey),owned=Object.hasOwn(values,choiceKey);statement.replaceChildren();
  if(h.inherit)statement.append(new Option('继承公共设置（未设置时不添加）',''));
  for(const row of rows)statement.append(new Option(row.name,row.choice));
  if(selected!==undefined&&!validChoice(selected)){statement.append(new Option('已保存的声明需要重新选择','__saved__'));statement.value='__saved__';}
  else if(h.inherit&&!owned)statement.value='';else statement.value=selected===undefined?(hasNone?'none':''):selected;
  statement.disabled=!rows.length;
  status.textContent=selected!==undefined&&!validChoice(selected)?'已保存的内容声明不在当前选项中，请重新选择；已有资料已保留。':!hasNone?'声明选项暂不可用，请稍后重新打开发布设置。':inheritedReset.has(choiceKey)?'保存后将恢复公共内容声明；公共设置未选择时不添加。':h.inherit&&!owned&&validChoice(selected)?'当前沿用：'+rows.find(row=>row.choice===selected).name+'。可继承公共设置，也可单独选择不添加。':declarationHelp;
 }
 function drawOriginal(){if(!original)return;const value=current(originalKey);original.checked=value===true;original.indeterminate=inheritedReset.has(originalKey)&&!Object.hasOwn(values,originalKey);originalState.textContent=value!==undefined&&typeof value!=='boolean'?'已保存的原创设置需要重新确认，请勾选或取消勾选。':original.indeterminate?'保存后将恢复公共原创设置；公共设置未确认原创时不勾选。':h.inherit&&!Object.hasOwn(values,originalKey)?'未单独修改时沿用已有原创设置；未设置时不勾选。':'原创与内容声明分别确认，只有你确认本条为原创时才勾选。';}
 section.append(status);
 if(schema[choiceKey]){statement=el('select');statement.setAttribute('aria-label',prefix+'内容声明');statement.onchange=()=>{if(!live())return;const value=statement.value;if(value===''&&h.inherit){delete values[choiceKey];if(h.onInherit)inheritedReset.add(choiceKey);h.onInherit?.(choiceKey);}else if(validChoice(value)){values[choiceKey]=value;inheritedReset.delete(choiceKey);}else{drawDeclaration();return;}drawDeclaration();h.onChange?.();};section.append(el('label','内容声明（选填）'),statement);if(schema[choiceKey].help||schema[choiceKey].description)section.append(el('p',String(schema[choiceKey].help||schema[choiceKey].description),'hint'));controls.push(statement);}
 if(schema[originalKey]){original=el('input');original.type='checkbox';original.setAttribute('aria-label',prefix+'声明原创');original.onchange=()=>{if(!live())return;values[originalKey]=original.checked===true;inheritedReset.delete(originalKey);drawOriginal();h.onChange?.();};const label=el('label');label.append(original,el('span','声明原创（需由本人确认）'));originalState=el('p',undefined,'hint');originalState.setAttribute('aria-label',prefix+'原创说明');section.append(label,originalState);controls.push(original);
  if(h.inherit){const inherit=el('button','恢复公共原创设置','small');inherit.type='button';inherit.setAttribute('aria-label',prefix+'恢复公共原创设置');inherit.onclick=()=>{if(!live())return;delete values[originalKey];if(h.onInherit)inheritedReset.add(originalKey);h.onInherit?.(originalKey);drawOriginal();h.onChange?.();};section.append(inherit);controls.push(inherit);}
  section.append(el('p','发布时如出现原创协议，请在验证页面亲自确认。','hint'));
 }
 host.append(section);drawDeclaration();drawOriginal();const short=schema.channels_short_title?createShortTitle(h):null;
 return {keys:keysFor(schema),materialize(){if(!live())return;
  if(statement){const value=current(choiceKey);if(!hasNone||value!==undefined&&!validChoice(value))throw Error('请重新选择有效的内容声明。');}
  if(original){const value=current(originalKey);if(value!==undefined&&typeof value!=='boolean')throw Error('请重新确认原创设置。');}
  if(!h.inherit){if(statement&&!Object.hasOwn(values,choiceKey)&&!Object.hasOwn(base,choiceKey))values[choiceKey]='none';if(original&&!Object.hasOwn(values,originalKey)&&!Object.hasOwn(base,originalKey))values[originalKey]=false;}
  short?.materialize();
 },reset(){if(live()){if(h.onInherit)for(const key of [choiceKey,originalKey])if(schema[key])inheritedReset.add(key);drawDeclaration();drawOriginal();short?.reset();}},dispose(){if(!alive)return;alive=false;short?.dispose();for(const control of controls)control.disabled=true;},get refreshing(){return false;}};
}
function createBuiltin(h){
 const {el,host,values}=h,schema=h.schema,base=h.effective||{},prefix=h.prefix||'B站';let alive=true;
 const live=()=>alive&&h.isAlive?.()!==false&&host.isConnected!==false;
 const inheritedReset=new Set(),current=k=>Object.hasOwn(values,k)?values[k]:inheritedReset.has(k)?undefined:base[k];
 const groups={category_choice:['category_choice','category_id','category_name','category_mode'],creation_statement_choice:['creation_statement_choice','creation_statement_id','creation_statement_name']};
 const section=el('section',undefined,'publish-video-form'),category=el('select'),statement=el('select'),authorization=el('input'),source=el('textarea'),sourceWrap=el('label','转载来源');
 const status=el('p','分区和创作声明可直接选择；发布时核对当前账号的实际投稿表单。','hint');status.setAttribute('role','status');status.setAttribute('aria-label',prefix+'选项读取状态');
 const categoryHint=el('p',undefined,'hint'),statementHint=el('p',undefined,'hint');categoryHint.setAttribute('aria-label',prefix+'已保存分区');statementHint.setAttribute('aria-label',prefix+'已保存声明');
 category.setAttribute('aria-label',prefix+'分区');statement.setAttribute('aria-label',prefix+'创作声明');
 const allRows=k=>(schema[k]?.choices||[]).filter(x=>typeof x?.choice==='string'&&typeof x.name==='string');
 const rows=k=>allRows(k).filter(x=>k!=='category_choice'||(x.mode!=='legacy'&&!x.choice.startsWith('legacy:')));
 const validDefault=k=>typeof schema[k]?.default==='string'&&rows(k).some(x=>x.choice===schema[k].default)?schema[k].default:undefined;
 const hasSaved=k=>groups[k].some(x=>current(x)!==undefined),hasOwned=k=>groups[k].some(x=>Object.hasOwn(values,x));
 function draw(input,key,oldId,oldName,hint){const list=rows(key),selected=current(key),saved=hasSaved(key),inherited=h.inherit&&!hasOwned(key),row=list.find(x=>x.choice===selected);
  input.replaceChildren(new Option(inherited?'继承公共设置（未设置时使用默认）':saved&&!row?'已保存，重新选择可替换':'请选择',''));
  for(const entry of list)input.append(new Option(entry.name,entry.choice));
  input.value=inherited?'':row?selected:saved?'':validDefault(key)||'';
  hint.hidden=!saved;hint.textContent='';
  if(saved){const label=row?.name||current(oldName)||allRows(key).find(x=>x.choice===selected)?.name;
   hint.textContent=(inherited?'当前沿用：':'已保存：')+(label||'先前选择')+'。'+(!row?'原设置已保留；选择当前选项后替换，发布时核对。':'');
  }
 }
 authorization.type='checkbox';authorization.checked=current('content_authorization')===true;authorization.setAttribute('aria-label',prefix+'内容授权');
 source.setAttribute('aria-label',prefix+'转载来源');source.maxLength=schema.reprint_source?.max_length||200;source.value=current('reprint_source')??'';sourceWrap.append(source);
 function sourceState(){const choice=current('creation_statement_choice');sourceWrap.hidden=choice!==undefined?choice!=='reprint':current('creation_statement_id')!==-2;source.required=!sourceWrap.hidden;}
 function drawAll(){draw(category,'category_choice','category_id','category_name',categoryHint);draw(statement,'creation_statement_choice','creation_statement_id','creation_statement_name',statementHint);authorization.checked=current('content_authorization')===true;source.value=current('reprint_source')??'';sourceState();}
 function choose(input,key,oldKeys){if(!live())return;const value=input.value;if(value!==''&&!rows(key).some(x=>x.choice===value))return;
  for(const old of oldKeys)delete values[old];if(value===''){delete values[key];if(h.inherit&&h.onInherit)for(const k of [...groups[key],...(key==='creation_statement_choice'?['reprint_source']:[])]){delete values[k];inheritedReset.add(k);h.onInherit(k);}}
  else{values[key]=value;for(const k of groups[key])inheritedReset.delete(k);}
  if(key==='creation_statement_choice'&&value!==''&&value!=='reprint'){values.reprint_source='';source.value='';}
  drawAll();h.onChange?.();
 }
 category.onchange=()=>choose(category,'category_choice',['category_id','category_name','category_mode']);
 statement.onchange=()=>choose(statement,'creation_statement_choice',['creation_statement_id','creation_statement_name']);
 authorization.onchange=()=>{if(live()){values.content_authorization=authorization.checked===true;h.onChange?.();}};
 source.oninput=()=>{if(live()){values.reprint_source=source.value;h.onChange?.();}};
 const authorized=el('label');authorized.append(authorization,el('span','我确认本内容为自制，并拥有发布权利'));
 section.append(status,el('label','分区（必选）'),category,categoryHint,el('label','创作声明'),statement,statementHint,sourceWrap,authorized,el('p','创作声明与内容授权分别确认；请按实际内容选择。','hint'));host.append(section);drawAll();
 return {keys:KEYS.slice(),materialize(){if(!live()||h.inherit)return;for(const key of Object.keys(groups))if(!hasSaved(key)&&validDefault(key)!==undefined)values[key]=validDefault(key);if(!Object.hasOwn(values,'content_authorization')&&!Object.hasOwn(base,'content_authorization'))values.content_authorization=false;},reset(){if(live()){if(h.onInherit)for(const key of KEYS)inheritedReset.add(key);drawAll();}},dispose(){alive=false;for(const input of [category,statement,authorization,source])input.disabled=true;},get refreshing(){return false;}};
}
function create(h){if(h.schema?.content_declaration_choice||h.schema?.channels_original)return createLocal(h);if(h.schema?.category_choice?.choices_source==='builtin'&&h.schema?.creation_statement_choice?.choices_source==='builtin')return createBuiltin(h);if(!h.schema?.creation_statement_id)return createShortTitle(h);
 const {el,button,host,api,values}=h,schema=h.schema||{},prefix=h.prefix||'B站',base=h.effective||{};
 let alive=true,refreshing=false,generation=0,timer=null,wake=null,options=null,readTicket=null,verificationId=null;
 const accountId=h.account_id,cancelledTickets=new Set();
 const live=()=>alive&&h.account_id===accountId&&h.isAlive?.()!==false&&host.isConnected!==false;
 const current=key=>Object.hasOwn(values,key)?values[key]:base[key];
 const section=el('section',undefined,'publish-video-form'),actions=el('div',undefined,'toolbar'),status=el('p',undefined,'hint');
 status.setAttribute('role','status');status.setAttribute('aria-label',prefix+'选项读取状态');
 const refresh=button('读取平台选项',refreshOptions,'small');refresh.setAttribute('aria-label',prefix+'读取平台选项');refresh.disabled=!h.account_id;
 const show=button('打开原验证页面',()=>verificationAction('show'),'small'),resume=button('已完成验证，继续读取',()=>verificationAction('continue'),'small'),pause=button('暂停读取',()=>verificationAction('cancel'),'small');
 for(const [control,text] of [[show,'打开原验证页面'],[resume,'已完成验证，继续读取'],[pause,'暂停读取']]){control.setAttribute('aria-label',prefix+text);control.hidden=true;}
 actions.append(refresh,show,resume,pause);section.append(actions,status);
 const category=el('select'),statement=el('select'),authorization=el('input'),source=el('textarea'),sourceWrap=el('label','转载来源');
 category.setAttribute('aria-label',prefix+'分区');statement.setAttribute('aria-label',prefix+'创作声明');
 authorization.type='checkbox';authorization.checked=current('content_authorization')===true;authorization.setAttribute('aria-label',prefix+'内容授权');
 source.setAttribute('aria-label',prefix+'转载来源');source.value=current('reprint_source')??'';source.maxLength=schema.reprint_source?.max_length||2000;
 const authorized=el('label');authorized.append(authorization,el('span','我确认本内容为自制，并拥有发布权利'));sourceWrap.append(source);
 section.append(el('label','分区（必选）'),category,el('label','创作声明（必选）'),statement,sourceWrap,authorized,el('p','创作声明与内容授权分别确认；请按本条内容实际情况选择。','hint'));host.append(section);
 function choices(key){const list=key==='category_id'?(options?.categories||schema.category_id?.choices||[]):(options?.statements||schema.creation_statement_id?.choices||[]);return list.filter(c=>Number.isInteger(c.id)&&(key!=='category_id'||c.id>0));}
 function drawChoice(input,key){const selected=current(key),list=choices(key),empty=h.inherit?'继承公共设置（未设置时需选择）':'请选择';input.replaceChildren(new Option(empty,''));for(const c of list)input.append(new Option(String((key==='category_id'&&c.parent_name?c.parent_name+' / ':'')+(c.name||c.label||c.id)),String(c.id)));if(Number.isInteger(selected)&&!list.some(c=>c.id===selected)){const name=current(key==='category_id'?'category_name':'creation_statement_name');input.append(new Option(String(name||selected)+'（已保存，待核对）',String(selected)));}input.value=Number.isInteger(selected)?String(selected):'';input.disabled=!list.length&&!Number.isInteger(selected);}
 function sourceState(){sourceWrap.hidden=current('creation_statement_id')!==-2;source.required=!sourceWrap.hidden;}
 function draw(){drawChoice(category,'category_id');drawChoice(statement,'creation_statement_id');sourceState();}
 function changed(){h.onChange?.();}
 function pick(input,key,names){if(!live())return;if(input.value===''){for(const k of [key,...names])delete values[k];}else{const entry=choices(key).find(c=>String(c.id)===input.value);if(!entry)return;values[key]=entry.id;values[names[0]]=String(entry.name||entry.label||entry.id);if(key==='category_id'){const mode=entry.mode||options?.category_mode;if(mode==='legacy'||mode==='human')values.category_mode=mode;else delete values.category_mode;}}
  if(key==='creation_statement_id'&&input.value!==''&&current(key)!==-2){values.reprint_source='';source.value='';}sourceState();changed();
 }
 category.onchange=()=>pick(category,'category_id',['category_name','category_mode']);
 statement.onchange=()=>pick(statement,'creation_statement_id',['creation_statement_name']);
 authorization.onchange=()=>{if(live()){values.content_authorization=authorization.checked===true;changed();}};
 source.oninput=()=>{if(live()){values.reprint_source=source.value;changed();}};
 function receive(result){const value=result?.options||(Array.isArray(result?.categories)||Array.isArray(result?.statements)?result:null);if(value&&(value.status==='ready'||value.categories?.length||value.statements?.length)){options=value;draw();return true;}return false;}
 function message(result,fallback){return String(result?.message||fallback);}
 async function loadCached(){if(!h.account_id){status.textContent='请先选择账号，再读取平台选项。';return;}const ticket=generation;status.textContent='正在读取本地选项缓存…';try{const result=await api('multi_publish.platform_options.cached',{account_id:h.account_id});if(!live()||ticket!==generation)return;const has=receive(result);status.textContent=message(result,has?'已显示缓存选项；需要更新时点击读取平台选项。':'尚无平台选项缓存，请点击读取平台选项。');}catch(e){if(live()&&ticket===generation)status.textContent='缓存读取失败：'+e.message+'；已保存的选择保留。';}}
 function wait(){return new Promise(resolve=>{wake=resolve;timer=setTimeout(()=>{timer=null;wake=null;resolve();},1000);});}
 function controls(){refresh.disabled=refreshing||!!readTicket||!accountId;for(const control of [show,resume,pause]){control.hidden=!verificationId;control.disabled=refreshing||!verificationId;}}
 function cancelRead(ticket){if(!ticket||cancelledTickets.has(ticket))return;cancelledTickets.add(ticket);Promise.resolve().then(()=>api('multi_publish.platform_options.cancel',{ticket})).catch(()=>{});}
 function validResult(result,ticket,nonce){if(result?.ticket&&result.ticket!==ticket)throw Error('读取状态已变化，旧响应已忽略，请稍后重试。');if(nonce&&result?.verification_id&&result.verification_id!==nonce)throw Error('验证状态已变化，旧响应已忽略，请稍后重试。');}
 function needsPoll(result,action){return result?.status==='loading'||result?.status==='cancelling'||(action==='show'&&result?.verification_view_state==='queued')||(action==='show'&&result?.accepted===true&&!result?.verification_view_state&&result?.shown!==true);}
 async function poll(result,key,action,nonce){for(let n=0;needsPoll(result,action)&&n<120;n++){if(!readTicket)throw Error('平台未返回有效读取标识。');const ticket=readTicket;await wait();if(!live()||key!==generation)return null;result=await api('multi_publish.platform_options.status',{ticket});if(!live()||key!==generation)return null;validResult(result,ticket,action==='show'?nonce:null);if(action==='continue'&&result?.status==='verification_required'&&result.verification_id===nonce)result={ticket,status:'loading'};}if(needsPoll(result,action))throw Error('读取等待超时，请稍后重试。');return result;}
 function finish(result,action){receive(result);const retained=result?.status==='verification_required'&&result?.verification_available===true&&typeof result.verification_id==='string'&&!!result.verification_id&&!!readTicket;
  verificationId=retained?result.verification_id:null;if(!retained)readTicket=null;
  if(action==='show'&&result?.verification_view_state==='failed')status.textContent='原验证页面未能打开：'+String(result.verification_view_error||result.message||'请稍后重试，或暂停后重新读取。');
  else if(action==='show'&&(result?.verification_view_state==='opened'||result?.shown===true))status.textContent=message(result,'原验证页面已打开；请完成页面验证，再点击“已完成验证，继续读取”。当前不会上传或发表视频。');
  else status.textContent=message(result,result?.status==='ready'?'平台选项读取完成。':result?.status==='cancelled'?'已暂停读取，原读取页面已关闭；已有选择保留。':'平台选项未能读取，已有选择保留。');
  if(result?.status==='ready')h.onReady?.(result.options);else if(result?.status!=='cancelled'&&result?.status!=='verification_required')h.onError?.(status.textContent);
 }
 async function refreshOptions(){if(refreshing||readTicket||!live()||!accountId)return;refreshing=true;const key=++generation;controls();status.textContent='正在读取平台选项，请稍候…';try{const started=await api('multi_publish.platform_options.refresh',{account_id:accountId});if(!live()||key!==generation){if(['loading','verification_required'].includes(started?.status))cancelRead(started?.ticket);return;}readTicket=typeof started?.ticket==='string'&&started.ticket?started.ticket:null;receive(started?.cached);const result=await poll(started,key);if(result)finish(result);}catch(e){if(live()&&key===generation){cancelRead(readTicket);readTicket=null;verificationId=null;status.textContent='平台选项读取失败：'+e.message+'；已有选择保留。';h.onError?.(status.textContent);}}finally{if(key===generation){refreshing=false;if(live())controls();}}}
 async function verificationAction(action){if(refreshing||!live()||!readTicket||!verificationId)return;const ticket=readTicket,nonce=verificationId,key=++generation;refreshing=true;controls();status.textContent=action==='show'?'正在打开原验证页面，请稍候…':action==='continue'?'正在原页面继续读取分区和声明，请稍候…':'正在暂停读取并关闭原页面…';try{let result=await api('multi_publish.platform_options.respond',{ticket,verification_id:nonce,action});if(!live()||key!==generation)return;validResult(result,ticket,action==='continue'?null:nonce);if(action==='continue'&&!result?.status)result={...result,ticket,status:'loading'};result=await poll(result,key,action,nonce);if(result)finish(result,action);}catch(e){if(live()&&key===generation){status.textContent=(action==='show'?'原验证页面未能打开：':action==='continue'?'继续读取失败：':'暂停读取失败：')+e.message+'；已有选择保留。';h.onError?.(status.textContent);}}finally{if(key===generation){refreshing=false;if(live())controls();}}}
 draw();void loadCached();
 return {keys:KEYS.slice(),materialize(){if(!Object.hasOwn(values,'content_authorization')&&!Object.hasOwn(base,'content_authorization'))values.content_authorization=false;},dispose(){if(!alive)return;alive=false;generation++;cancelRead(readTicket);readTicket=null;verificationId=null;refreshing=false;for(const control of [show,resume,pause]){control.hidden=true;control.disabled=true;}refresh.disabled=true;if(timer!==null)clearTimeout(timer);if(wake)wake();timer=null;wake=null;},get refreshing(){return refreshing;}};
}
root.P011MultiPlatformFields={supported,create,keysFor};
})(globalThis);
