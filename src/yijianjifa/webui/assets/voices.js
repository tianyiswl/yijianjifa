P011Views.voices=()=>{
 const $=id=>document.getElementById(id);let alive=true,busy=false,catalog=null,timer,page=0,active='',sampleBusy=false;const pageSize=20;
 function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
 async function api(operation,params){const r=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id:[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join(''),operation,params})));if(!r.ok)throw Error(r.error.message);return r.data;}
 function notice(text){if(!alive)return;$('toast').textContent=text;$('toast').style.display='block';clearTimeout(timer);timer=setTimeout(()=>$('toast').style.display='none',7000);}
 function button(text,fn,cls='btn'){const b=el('button',text,cls);b.onclick=e=>{e.stopPropagation();return Promise.resolve(fn()).catch(e=>notice(e.message));};return b;}
 function hideModal(){$('dialog').querySelectorAll('input[type=password]').forEach(n=>n.value='');$('modal').style.display='none';}
 function modal(title,body,actions){$('dialog').replaceChildren(el('h2',title),...body);const foot=el('div',undefined,'foot');foot.append(...actions);$('dialog').append(foot);$('modal').style.display='grid';}
 const navs=[['资料库','library'],['混剪制作','mixing'],['成品库','finished'],['抖音发布','publish'],['账号管理','accounts'],['任务记录','tasks'],['配音管理','voices'],['常用方案与设置','presets']];document.querySelector('.nav').append(...navs.map(([label,route])=>button(label,()=>window.p011Navigate(()=>P011Router.go(route)),route==='voices'?'on':'')));
 window.p011Navigate=fn=>{if(busy||cloud.isBusy()){notice('正在处理配音，请稍候。');return;}fn();};window.p011BeforeClose=()=>window.p011Navigate(()=>api('app.exit',{}));
 function stopAudio(){document.querySelectorAll('audio').forEach(a=>a.pause());}
 async function playAudio(audio,host){const current=()=>alive&&host.contains(audio);const failed=()=>{if(current())notice('音频播放失败，请重新打开试听；已生成的配音无需重复生成。');};audio.onerror=failed;try{await audio.play();return current();}catch(e){if(current()){if(e?.name==='NotAllowedError')notice('点击播放器的播放按钮即可试听。');else failed();}return false;}}
 function setActive(id){if(busy||cloud.isBusy())return;stopAudio();active=id;$('standardPlayer').replaceChildren();$('customPlayer').replaceChildren();render();}
 async function save(selected,favorites=catalog.favorites,message){if(busy)return;catalog=await api('voice.selection.save',{selected,favorites});render();cloud.selectionChanged();notice(message??(selected?'已设为默认配音，新建制作会使用此音色。':'已恢复系统配音（免费）。'));}
 function toggleFavorite(id){if(cloud.isBusy())return;const wasFavorite=catalog.favorites.includes(id);return save(catalog.selected,wasFavorite?catalog.favorites.filter(voice=>voice!==id):[...catalog.favorites,id],wasFavorite?'已取消收藏。':'已收藏。');}
 function activeVoice(){return catalog?.items.find(v=>v.id===active);}
 function updateDetail(){const v=activeVoice();$('activeName').textContent=v?.name||'请选择音色';$('activeMeta').textContent=v?`${v.gender} · ${(v.scenes||[v.style]).join(' / ')} · ${v.language}`:'';$('voiceAvailability').textContent=!v?'':!catalog.configured?'登录云配音后核实是否可用。':v.available===false?'暂不可用，请选择标有“可用”的音色。':'已开通，可用于试听和制作。';$('selectedName').textContent=catalog.mode==='own'&&catalog.selected?'请重新选择音色':catalog.items.find(v=>v.id===catalog.selected)?.name||'系统配音';$('useVoice').textContent=catalog.mode!=='own'&&catalog.selected===active?'已设为默认配音':'设为默认配音';$('useVoice').disabled=!v||!cloud.canChoose(v)||busy||(catalog.mode!=='own'&&catalog.selected===active);$('standardAudition').disabled=!v?.standard_sample||sampleBusy||busy;$('systemVoice').hidden=!catalog.selected;$('systemVoice').disabled=busy||cloud.isBusy();$('sampleNote').textContent=v?.local_test?'本地测试使用系统声音模拟，不代表大模型音色。':v?.sample_source==='官方试听'?'官方示例已保存在本机，不消耗配音额度。':v?.standard_sample?'本机测试示例，不消耗配音额度。':'此音色暂未取得官方示例，可输入文字生成试听。';$('standardAudition').textContent=v?.sample_source==='官方试听'?'听官方示例':'听示例';if(!busy)$('customAudition').textContent=`生成试听（${P011CloudVoice.words($('auditionText').value.trim())} 字）`;$('customAudition').disabled=!v||(!catalog.configured||v?.available===false)||busy||cloud.isBusy()||!$('auditionText').value.trim();}
 function render(resetScroll=false){
  if(!alive||!catalog)return;
  const scroll=document.querySelector('.voice-scroll'),rows=$('voiceRows');
  const top=resetScroll?0:scroll.scrollTop,left=resetScroll?0:scroll.scrollLeft;
  const focused=document.activeElement,focusedRow=focused?.closest('tr');
  const focusedVoice=focusedRow&&rows.contains(focusedRow)?focusedRow.dataset.voiceId:null;
  const focusedAction=focused?.dataset.voiceAction;let focusTarget;
  const query=$('voiceSearch').value.trim().toLowerCase();
  const items=catalog.items.filter(v=>(v.name+' '+v.id).toLowerCase().includes(query)&&(!$('genderFilter').value||v.gender===$('genderFilter').value)&&(!$('sceneFilter').value||(v.scenes||[v.style]).includes($('sceneFilter').value))&&(!$('languageFilter').value||(v.language_names||[v.language]).includes($('languageFilter').value))&&(!$('favoriteOnly').checked||catalog.favorites.includes(v.id)));
  page=Math.min(page,Math.max(0,Math.ceil(items.length/pageSize)-1));
  $('voiceTotal').textContent=`共 ${catalog.items.length} 个音色`;
  rows.replaceChildren(...items.slice(page*pageSize,(page+1)*pageSize).map(v=>{
   const row=el('tr',undefined,'row');row.tabIndex=0;row.dataset.voiceId=v.id;
   row.setAttribute('aria-selected',String(active===v.id));row.onclick=()=>setActive(v.id);
   row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setActive(v.id);}};
   const name=el('td');name.append(el('span',v.name,'name'),el('small',v.gender+' · '+v.language));
   if(catalog.configured)name.append(el('span',v.available===false?'暂不可用':'可用',v.available===false?'tag':'tag blue'));
   if(catalog.selected===v.id)name.append(el('span',catalog.mode==='own'?'待重新选择':'制作默认','tag blue'));
   const preview=el('td'),favorite=el('td');
   const play=button(v.standard_sample?'听示例':'文字试听',()=>{if(v.standard_sample)return standard(v.id);setActive(v.id);$('auditionText').focus({preventScroll:true});},'btn small');
   play.dataset.voiceAction='sample';play.disabled=sampleBusy||busy;preview.append(play);
   const fav=button(catalog.favorites.includes(v.id)?'已收藏':'收藏',()=>toggleFavorite(v.id),'btn small');
   fav.dataset.voiceAction='favorite';fav.disabled=busy||cloud.isBusy();favorite.append(fav);
   row.append(name,el('td',v.style),preview,favorite);
   if(!resetScroll&&v.id===focusedVoice)focusTarget=focusedAction==='sample'?play:focusedAction==='favorite'?fav:row;
   return row;
  }));
  if(!items.length){const row=el('tr');const cell=el('td','没有匹配的音色，请调整筛选。','voice-empty');cell.colSpan=4;row.append(cell);rows.append(row);}
  const pages=Math.max(1,Math.ceil(items.length/pageSize));
  $('pageInfo').textContent=`共 ${items.length} 条 · ${page+1}/${pages} 页`;
  $('pageSelect').replaceChildren(...Array.from({length:pages},(_,i)=>new Option(`第 ${i+1} / ${pages} 页`,String(i))));
  $('pageSelect').value=String(page);$('pageSelect').disabled=busy||pages===1;
  $('prevPage').disabled=page===0||busy;$('nextPage').disabled=(page+1)*pageSize>=items.length||busy;
  updateDetail();
  // Replacing the table removes its scroll anchor and focused control in Chromium.
  // Restore the position at redraw time, including scrolling while an API call waited.
  if(focusTarget&&!focusTarget.disabled)focusTarget.focus({preventScroll:true});
  scroll.scrollTop=top;scroll.scrollLeft=left;
 }
 async function standard(id){if(sampleBusy||busy)return;if(active!==id)setActive(id);sampleBusy=true;stopAudio();updateDetail();try{const r=await api('voice.standard',{voice:id});if(!alive||active!==id)return;const audio=el('audio');audio.controls=true;audio.src=r.url;$('standardPlayer').replaceChildren(audio);await playAudio(audio,$('standardPlayer'));}catch(e){notice(e.message);}finally{sampleBusy=false;if(alive)render();}}
 async function load(){catalog=await api('voice.catalog',{});if(!alive)return;if(!active||!catalog.items.some(v=>v.id===active))active=catalog.selected||catalog.items[0]?.id||'';const chosen=$('sceneFilter').value;$('sceneFilter').replaceChildren(new Option('全部场景',''),...Array.from(new Set(catalog.items.flatMap(v=>v.scenes||[v.style]))).map(v=>new Option(v,v)));$('sceneFilter').value=chosen;const lang=$('languageFilter').value;$('languageFilter').replaceChildren(new Option('全部语言',''),...Array.from(new Set(catalog.items.flatMap(v=>v.language_names||[v.language]))).map(v=>new Option(v,v)));$('languageFilter').value=lang;render();cloud.selectionChanged();}
 for(const id of ['voiceSearch','genderFilter','sceneFilter','languageFilter','favoriteOnly'])$(id)[id==='voiceSearch'?'oninput':'onchange']=()=>{page=0;render(true);};$('pageSelect').onchange=()=>{const n=Number($('pageSelect').value);if(Number.isInteger(n)&&n>=0){page=n;render(true);}};$('prevPage').onclick=()=>{page--;render(true);};$('nextPage').onclick=()=>{page++;render(true);};$('systemVoice').onclick=()=>{if(cloud.isBusy())return;return save('').catch(e=>notice(e.message));};$('useVoice').onclick=()=>{const selected=active;return cloud.chooseVoice(()=>save(selected));};$('standardAudition').onclick=()=>standard(active);$('auditionText').oninput=()=>{$('textCount').textContent=`${Array.from($('auditionText').value).length} / 4000 字`;if(catalog)updateDetail();};
 $('customAudition').onclick=async()=>{if(busy||cloud.isBusy()||!activeVoice())return;const text=$('auditionText').value.trim(),speed=Number($('voiceSpeed').value);if(!text){notice('请输入试听文字。');return;}if(Array.from(text).length>4000){notice('试听文字不能超过4000字。');return;}if(!Number.isFinite(speed)||speed<0.5||speed>2){notice('语速请填写 0.5 到 2。');return;}busy=true;stopAudio();$('auditionText').disabled=true;$('voiceSpeed').disabled=true;$('customAudition').textContent='正在生成…';render();try{const r=await api('voice.audition.text',{text,voice:active,speed});if(!alive)return;const audio=el('audio');audio.controls=true;audio.src=r.url;$('customPlayer').replaceChildren(el('p',`生成完成 · ${(r.duration_ms/1000).toFixed(1)} 秒`,'muted'),audio);if(await playAudio(audio,$('customPlayer')))notice('文字试听已生成。');}catch(e){if(alive)$('customPlayer').replaceChildren(el('p',e.message,'muted'));notice(e.message);}finally{busy=false;if(alive){$('auditionText').disabled=false;$('voiceSpeed').disabled=false;$('customAudition').textContent='生成文字试听';render();cloud.refreshAfterGeneration();}}};
 const cloud=P011CloudVoice.mount({$,api,el,button,modal,hideModal,notice,changed:load,isBusy:()=>busy,selectedVoice:()=>catalog?.selected??null,useSystem:()=>save(''),selectionUpdated:()=>{if(catalog)updateDetail();}});

 $('modal').onclick=e=>{if(e.target===$('modal'))hideModal();};
 load().catch(e=>notice(e.message));
 return()=>{cloud.dispose();alive=false;clearTimeout(timer);stopAudio();};
};
