/* Subtitle edits retain original identity and acknowledged revisions. */
(function(root){'use strict';
const byteLength=value=>new TextEncoder().encode(JSON.stringify(value)).length;
function patchBatches(resourceId,origin,patches,revision){
 const batches=[];let batch=[];
 for(const patch of patches){const next=[...batch,patch],params={resource_id:resourceId,origin,expected_revision:revision,patches:next};
  if(next.length>200||byteLength(params)>12288){if(!batch.length)throw Error('这一段字幕过长，请缩短后再保存。');batches.push(batch);batch=[patch];if(byteLength({...params,patches:batch})>12288)throw Error('这一段字幕过长，请缩短后再保存。');}else batch=next;
 }if(batch.length)batches.push(batch);return batches;
}
const clone=value=>JSON.parse(JSON.stringify(value));
class SubtitleEditor{
 constructor({send,makeId,WriteIntent=root.P011WriteIntent}){this.send=send;this.intent=new WriteIntent(async(operation,params,id)=>{const result=await send(operation,params,id);if(!Number.isSafeInteger(result?.revision)||result.revision<=params.expected_revision||result.saved_count!==params.patches.length)throw Error('保存结果不完整，请重试确认。');return result;},makeId);this.segments=[];this.dirty=new Map();this.resourceId=null;this.origin=null;this.revision=0;this.pending=null;this.loading=false;this.saving=false;this.alive=true;this.generation=0;}
 hasUnsaved(){return !!this.dirty.size||!!this.pending;}
 async load(resourceId,origin){
  if(this.hasUnsaved())throw Error('还有未保存的字幕，请先保存或放弃修改。');
  if(this.saving)throw Error('正在保存字幕，请稍候。');
  const generation=++this.generation;this.loading=true;let cursor=null,revision=null,segments=[],seen=new Set(),cursors=new Set();
  try{do{const page=await this.send('download.subtitle.read',{resource_id:resourceId,origin,cursor});
    if(!this.alive||generation!==this.generation)return;
    if(!Number.isSafeInteger(page.revision)||page.revision<0||!Array.isArray(page.segments)||revision!==null&&revision!==page.revision)throw Error('字幕版本发生变化，请重新读取。');
    revision=page.revision;for(const row of page.segments){if(!row||typeof row.id!=='string'||seen.has(row.id)||segments.length>=100000)throw Error('字幕数据不完整，请重新读取。');seen.add(row.id);segments.push(clone(row));}
    cursor=page.next_cursor;if(cursor!==null&&cursor!==undefined){if(typeof cursor!=='string'||cursor.length>512||cursors.has(cursor))throw Error('字幕分页异常，请重新读取。');cursors.add(cursor);}
   }while(cursor);
   this.validate(segments);this.resourceId=resourceId;this.origin=origin;this.revision=revision;this.segments=segments;this.dirty.clear();
  }finally{if(generation===this.generation)this.loading=false;}
 }
 validate(rows){let previousEnd=0;for(const row of rows){if(!Number.isSafeInteger(row.start_ms)||!Number.isSafeInteger(row.end_ms)||row.start_ms<previousEnd||row.end_ms<=row.start_ms||typeof row.text!=='string'||row.text.includes('\0'))throw Error(`字幕 ${row.id} 的时刻有误：起止时间须有效，且不能与相邻字幕重叠。`);previousEnd=row.end_ms;}}
 current(){return this.segments.map(row=>({...row,...(this.pending?.patches.find(patch=>patch.id===row.id)||{}),...(this.dirty.get(row.id)||{})}));}
 edit(id,changes){
  if(this.loading||this.saving)throw Error('正在读取或保存字幕，请稍候。');
  const original=this.segments.find(row=>row.id===id);if(!original)throw Error('找不到这段字幕。');const source={...original,...(this.pending?.patches.find(patch=>patch.id===id)||{})};
  if(!changes||Object.keys(changes).some(key=>!['text','start_ms','end_ms'].includes(key)))throw Error('不支持的字幕修改。');
  const patch={...(this.dirty.get(id)||{}),...changes,id};const next=this.current().map(row=>row.id===id?{...row,...patch}:row);this.validate(next);
  const value={id};for(const key of ['text','start_ms','end_ms'])if(patch[key]!==undefined&&patch[key]!==source[key])value[key]=patch[key];
  if(Object.keys(value).length===1)this.dirty.delete(id);else this.dirty.set(id,value);
 }
 seek(id,player){const row=this.current().find(row=>row.id===id);if(row&&player)player.currentTime=row.start_ms/1000;}
 discard(){if(this.saving)throw Error('正在保存字幕，请稍候。');if(this.pending)throw Error('上次保存的结果尚未确认，请先重试保存，避免丢失修改。');this.dirty.clear();}
 async save(){
  if(this.saving)throw Error('正在保存字幕，请稍候。');if(this.loading||!this.resourceId)throw Error('请先读取字幕。');
  this.saving=true;let savedCount=0;
  try{while(this.dirty.size||this.pending){
    if(!this.pending){const batch=patchBatches(this.resourceId,this.origin,[...this.dirty.values()],this.revision)[0];this.pending={resource_id:this.resourceId,origin:this.origin,expected_revision:this.revision,patches:clone(batch)};}
    const pending=this.pending;const result=await this.intent.run('download.subtitle.save',pending);
    if(!Number.isSafeInteger(result.revision)||result.revision<=this.revision||result.saved_count!==pending.patches.length)throw Error('保存结果不完整，请重试确认。');
    this.revision=result.revision;for(const patch of pending.patches){const source=this.segments.find(row=>row.id===patch.id);Object.assign(source,patch);
     const current=this.dirty.get(patch.id);if(current&&JSON.stringify(current)===JSON.stringify(patch))this.dirty.delete(patch.id);
     else if(current){const remaining={id:patch.id};for(const key of ['text','start_ms','end_ms'])if(current[key]!==undefined&&current[key]!==source[key])remaining[key]=current[key];Object.keys(remaining).length>1?this.dirty.set(patch.id,remaining):this.dirty.delete(patch.id);}
    }savedCount+=pending.patches.length;this.pending=null;
   }return {saved_count:savedCount,remaining_count:this.dirty.size,revision:this.revision};
  }catch(error){if(['subtitle_conflict','subtitle_invalid','input_limit','request_conflict'].includes(error.code)){this.pending=null;this.intent.pending=null;}error.savedCount=savedCount;error.remainingCount=this.dirty.size;throw error;}finally{this.saving=false;}
 }
 dispose(){this.alive=false;this.generation++;}
}
let active=null;
function mount(resourceId,origin,options={}){
 if(active?.hasUnsaved())throw Error('还有未保存的字幕，请先保存或放弃修改。');active?.dispose();
 const host=options.host||document.getElementById('downloadSubtitles');if(!host)throw Error('字幕编辑区尚未打开。');
 const makeId=options.makeId||(()=>[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join(''));
 const send=options.send|| (async(operation,params,requestId)=>{const result=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id:requestId||makeId(),operation,params})));if(!result.ok){const error=Error(result.error.message);error.code=result.error.code;throw error;}return result.data;});
 const core=new SubtitleEditor({send,makeId}),names={asr:'语音识别',ocr:'画面字幕',platform_caption:'平台字幕',embedded_caption:'内嵌字幕'};let page=0,alive=true;const invalid=new Map(),hasUnsaved=()=>core.hasUnsaved()||invalid.size>0;function validInput(){if(invalid.size)throw Error('请先修正字幕中无效的文字或时间，再保存。');}
 const el=(tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;};
 const heading=el('div',undefined,'download-subtitle-heading'),status=el('p','正在读取字幕…','muted'),list=el('div',undefined,'download-subtitle-list'),pager=el('div',undefined,'p011-pager'),actions=el('div',undefined,'download-subtitle-actions');heading.append(el('h3',names[origin]||'字幕'),status);host.replaceChildren(heading,list,pager,actions);
 function button(label,action){const node=el('button',label,'btn');node.type='button';node.onclick=async()=>{try{await action();}catch(error){status.textContent=error.message;}};return node;}
 const save=button('保存字幕',async()=>{if(core.loading||core.saving)return;validInput();save.textContent='正在保存…';try{const pending=core.save();render();const result=await pending;if(alive)status.textContent=`已保存 ${result.saved_count} 段字幕。`;}catch(error){if(alive)status.textContent=`已保存 ${error.savedCount||0} 段；还有 ${error.remainingCount??core.dirty.size} 段未保存。${error.message}`;}finally{if(alive){save.textContent='保存字幕';render();}}});
 const previous=button('上一页',()=>{page--;render();}),next=button('下一页',()=>{page++;render();});pager.append(previous,next);
 const discard=button('放弃修改',()=>{core.discard();invalid.clear();status.textContent='已放弃本次未保存修改。';render();});
 const exportIntent=new root.P011WriteIntent(send,makeId);
 const exportButtons=['txt','srt'].map(format=>button('导出 '+format.toUpperCase(),async()=>{if(hasUnsaved())throw Error('请先保存字幕修改，再导出。');const result=await exportIntent.run('download.subtitle.export',{resource_id:resourceId,origin,format});if(alive)status.textContent=result.cancelled?'已取消导出。':'导出任务已创建，可在下载任务中查看进度。';}));actions.append(save,discard,...exportButtons);
 function render(){if(!alive)return;const rows=core.current();page=Math.max(0,Math.min(page,Math.ceil(rows.length/20)-1));list.replaceChildren();
  rows.slice(page*20,(page+1)*20).forEach(row=>{const card=el('div',undefined,'download-subtitle-row'),seek=button((row.start_ms/1000).toFixed(2)+' 秒',()=>{if(options.canSeek&&!options.canSeek())throw Error('对应视频预览尚未就绪，请先准备同一条视频的预览。');core.seek(row.id,options.player||document.getElementById('downloadPlayer'));}),start=el('input'),end=el('input'),text=el('textarea');
   start.type=end.type='number';start.min=end.min='0';start.step=end.step='.01';start.value=invalid.get(row.id+':start_ms')??String(row.start_ms/1000);end.value=invalid.get(row.id+':end_ms')??String(row.end_ms/1000);text.value=invalid.get(row.id+':text')??row.text;text.rows=2;text.setAttribute('aria-label','字幕文字');start.setAttribute('aria-label','字幕开始秒数');end.setAttribute('aria-label','字幕结束秒数');
   const edit=(field,value,node)=>{try{core.edit(row.id,{[field]:value});invalid.delete(row.id+':'+field);status.textContent=`有 ${core.dirty.size} 段未保存修改。`;}catch(error){invalid.set(row.id+':'+field,node.value);status.textContent=error.message;}save.disabled=invalid.size>0||!hasUnsaved();discard.disabled=!!core.pending||!hasUnsaved();exportButtons.forEach(b=>b.disabled=hasUnsaved());};
   text.oninput=()=>edit('text',text.value,text);start.onchange=()=>edit('start_ms',start.value.trim()?Math.round(Number(start.value)*1000):NaN,start);end.onchange=()=>edit('end_ms',end.value.trim()?Math.round(Number(end.value)*1000):NaN,end);text.disabled=start.disabled=end.disabled=core.saving||core.loading;
   card.append(seek,start,end,text);if(row.confidence!==null&&row.confidence!==undefined&&row.confidence<.65)card.append(el('span','识别信心较低，请核对','download-low-confidence'));list.append(card);
  });save.disabled=core.loading||core.saving||invalid.size>0||!hasUnsaved();discard.disabled=core.loading||core.saving||!!core.pending||!hasUnsaved();exportButtons.forEach(button=>button.disabled=core.loading||core.saving||hasUnsaved());previous.disabled=page===0;next.disabled=(page+1)*20>=rows.length;
  if(root.P011Pager)root.P011Pager.update(pager,rows.length,page,20,n=>{page=n;render();});
 }
 const session={core,hasUnsaved,discard:()=>{core.discard();invalid.clear();render();},save:async()=>{validInput();const result=await core.save();render();return result;},dispose(){alive=false;core.dispose();if(active===session)active=null;}};active=session;
 session.ready=core.load(resourceId,origin).then(()=>{if(alive){status.textContent=`共 ${core.segments.length} 段 · ${names[origin]} · 版本 ${core.revision}`;render();}}).catch(error=>{if(alive)status.textContent=error.message;});return session;
}
const api={SubtitleEditor,patchBatches,mount,hasUnsaved:()=>!!active?.hasUnsaved(),dispose:()=>active?.dispose()};
if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.P011DownloadSubtitles=api;
})(globalThis);
