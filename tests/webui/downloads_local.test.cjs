const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const linkOptions={save_video:true,prefer_clean:true,audio_format:null,silent_video:false,asr_language:null,ocr_enabled:false,model_ids:[]};
const localOptions={save_video:false,prefer_clean:false,audio_format:'mp3',silent_video:false,asr_language:null,ocr_enabled:false,model_ids:[]};
const rows=[{id:'local-a',platform:'local',title:'甲.MOV',duration_ms:1200,state:'received',source_resource_id:'owned-a'},
 {id:'local-b',platform:'local',title:'乙.M4V',duration_ms:2400,state:'received',source_resource_id:'owned-b'}];
const token='0123456789abcdef0123456789abcdef';
const descendants=n=>[n,...n.children.flatMap(descendants)];
const flush=async v=>{for(let i=0;i<4;i++)await v.flush();};
async function setup({mode='link',items=rows,extra=async()=>undefined,incoming}={}){
 const calls=[];
 const v=await mount(async(op,p)=>{
  calls.push({op,p:structuredClone(p)});const value=await extra(op,p);if(value!==undefined)return value;
  if(op==='download.settings.read')return {options:mode==='local'?localOptions:linkOptions,filter:{query:'',state:''},source_mode:mode};
  if(op==='download.settings.save')return {saved:true};
  if(op==='download.output.read')return {grant_id:'output-grant',display_path:'隔离输出目录'};
  if(op==='download.models.list')return {items:[],jobs:[]};
  if(op==='download.list')return {items:[],total:0};
  if(op==='download.local.choose'||op==='download.local.drop')return {job_id:'local-intake',session_id:'local-session'};
  if(op==='download.resolve.status')return {state:'succeeded',session_id:'local-session',received:items.length,failed:0,errors:[]};
  if(op==='download.resolve.list')return {session_id:'local-session',source_kind:'local',items,total:items.length,total_read:items.length,completion:'complete'};
  if(op==='download.cancel')return {job_id:p.job_id};
  if(op==='download.start')return {task_id:'local-task',count:p.candidate_ids.length};
  if(op==='download.detail')return {state:'completed',source_mode:'local',source_kind:'local',items:[]};
  throw Error('unexpected '+op);
 },'downloads',incoming);
 v.getId('downloadLocalDrop').getBoundingClientRect=()=>({left:10,right:200,top:20,bottom:120,width:190,height:100});
 await flush(v);return {v,calls};
}
async function local(v){v.getId('downloadLocalTab').onclick?.();await flush(v);}
async function choose(v){v.getId('downloadLocalChoose').onclick?.();await flush(v);}

test('local audio mode uses fixed MP3 and video action returns to link download',async()=>{
 const {v,calls}=await setup();await local(v);
 assert.equal(v.getId('downloadLinkSource').hidden,true);assert.equal(v.getId('downloadLocalSource').hidden,false);
 assert.equal(v.getId('downloadStart').textContent,'开始提取');
 v.getId('downloadLinkTab').onclick();await flush(v);assert.equal(v.getId('downloadLocalSource').hidden,true);assert.equal(v.getId('downloadReadVisible').hidden,true);
 v.getId('downloadVideoAction').onclick();await flush(v);assert.equal(v.getId('downloadStart').textContent,'开始下载');
 assert(calls.some(c=>c.op==='download.settings.save'&&c.p.source_mode==='local'&&c.p.options.audio_format==='mp3'));
 assert(!calls.some(c=>c.op==='download.start'||c.op==='download.resolve.start'));v.dispose();
});

for(const items of [rows.slice(0,1),rows])test('native chooser receives '+items.length+' local videos and selects them without starting',async()=>{
 const {v,calls}=await setup({items});await local(v);await choose(v);
 assert.deepEqual(calls.find(c=>c.op==='download.local.choose')?.p,{});
 assert.equal(v.getId('downloadRows').children.length,items.length);
 assert(v.getId('downloadRows').children.every(r=>r.children[0].children[0]?.checked));
 assert.match(v.getId('downloadLocalStatus').textContent,/已接收/);
 assert(!calls.some(c=>c.op==='download.start'||c.op==='download.resolve.start'));v.dispose();
});
test('native drop consumes only an opaque token in the explicit local zone',async()=>{
 const {v,calls}=await setup();await local(v);
 const file={};Object.defineProperty(file,'path',{get(){throw Error('browser File.path must not be read');}});
 await v.window.p011Drop?.({token,x:12,y:21,files:[file],path:'file:///untrusted'});await flush(v);
 assert.deepEqual(calls.find(c=>c.op==='download.local.drop')?.p,{token});
 assert(!JSON.stringify(calls).includes('file:///untrusted'));assert.equal(v.getId('downloadRows').children.length,2);v.dispose();
});
for(const condition of ['link','outside','modal','disposed','nonfinite','bad-token'])test('local drop does not consume token when '+condition,async()=>{
 const {v,calls}=await setup();if(condition!=='link')await local(v);
 if(condition==='modal')v.getId('downloadModal').style.display='grid';
 if(condition==='disposed')v.dispose();
 await v.window.p011Drop?.({token:condition==='bad-token'?'file:///wrong':token,x:condition==='outside'?500:condition==='nonfinite'?NaN:12,y:21});await flush(v);
 assert(!calls.some(c=>c.op==='download.local.drop'));if(condition!=='disposed')v.dispose();
});
test('second native intake is ignored while the first receipt is outstanding',async()=>{
 let finish;const {v,calls}=await setup({extra:async op=>op==='download.local.choose'?new Promise(r=>finish=r):undefined});await local(v);await choose(v);
 await v.window.p011Drop?.({token,x:12,y:21});v.getId('downloadLocalChoose').onclick?.();await flush(v);
 assert.equal(calls.filter(c=>c.op==='download.local.choose').length,1);assert(!calls.some(c=>c.op==='download.local.drop'));
 finish?.({cancelled:true});await flush(v);v.dispose();
});
test('picker cancellation preserves the existing received selection and creates no task',async()=>{
 let number=0;const {v,calls}=await setup({extra:async op=>op==='download.local.choose'&&number++?{cancelled:true}:undefined});await local(v);await choose(v);
 await choose(v);assert(v.getId('downloadRows').children.every(r=>r.children[0].children[0]?.checked));
 assert.match(v.getId('downloadLocalStatus').textContent,/已取消/);assert(!calls.some(c=>c.op==='download.start'));v.dispose();
});
for(const change of ['mode','dispose'])test('late native receipt cannot replace view after '+change,async()=>{
 let finish;const {v,calls}=await setup({extra:async op=>op==='download.local.choose'?new Promise(r=>finish=r):undefined});await local(v);await choose(v);
 if(change==='mode'){v.getId('downloadLinkTab').onclick?.();await flush(v);}else v.dispose();
 finish?.({job_id:'old-intake',session_id:'old-local'});await flush(v);
 assert(!calls.some(c=>c.op==='download.resolve.status'&&c.p.job_id==='old-intake'));
 assert(!calls.some(c=>c.op==='download.resolve.list'&&c.p.session_id==='old-local'));if(change==='mode')v.dispose();
});
test('local receive progress, per-file error text and explicit cancellation use the existing job lane',async()=>{
 let complete=false;const {v,calls}=await setup({extra:async(op)=>{
  if(op==='download.resolve.status')return complete?{state:'cancelled',session_id:'local-session',received:1,failed:1,errors:[{name:'<img src=x>.MOV',code:'media_invalid',message:'文件损坏'}]}:{state:'running',session_id:'local-session',progress:{stage:'local_copy',current:50,total:100}};
  if(op==='download.cancel'){complete=true;return {accepted:true};}
 }});await local(v);await choose(v);assert.match(v.getId('downloadLocalStatus').textContent,/50.*100/);
 v.getId('downloadLocalCancel').onclick?.();await flush(v);for(let i=0;i<3;i++)await v.tick();
 assert.deepEqual(calls.find(c=>c.op==='download.cancel')?.p,{job_id:'local-intake'});
 assert(v.find(n=>n.textContent.includes('<img src=x>.MOV')&&n.textContent.includes('文件损坏')));
 assert(!calls.some(c=>c.op==='download.start'));v.dispose();
});
test('local start freezes only selected extraction outputs and uses local task labels',async()=>{
 const {v,calls}=await setup({extra:async op=>op==='download.detail'?{state:'interrupted',source_mode:'local',options:localOptions,items:[{id:'i',candidate_id:'local-a',title:'甲.MOV',decision_required:true,stages:{video:{state:'succeeded'},audio:{state:'waiting'}}}],verification_required:true}:undefined});
 await local(v);await choose(v);
 v.getId('downloadStart').onclick();await flush(v);
 const start=calls.find(c=>c.op==='download.start');assert(start);assert.deepEqual(start.p.candidate_ids,['local-a','local-b']);
 assert.equal(start.p.options.save_video,false);assert.equal(start.p.options.prefer_clean,false);assert.equal(start.p.options.audio_format,'mp3');
 assert.equal(start.p.options.asr_language,null);assert.equal(start.p.options.ocr_enabled,false);assert.deepEqual(start.p.options.model_ids,[]);
 assert(v.find(n=>n.textContent.includes('视频接收：已完成')));assert(!v.find(n=>n.tag==='button'&&n.textContent==='继续未完成部分').hidden);
 assert(![...v.ids.values()].flatMap(n=>[n,...n.children.flatMap(function descend(n){return [n,...n.children.flatMap(descend)];})]).some(n=>n.tag==='button'&&['显示登录 / 验证窗口','原样下载','打开验证窗口'].includes(n.textContent)));
 v.dispose();
});

test('local candidate preview opens its received owned source without importing or preparing another source',async()=>{
 const {v,calls}=await setup({extra:async(op,p)=>op==='download.preview'?{resource_id:p.resource_id,kind:'source_video',url:'p011media://asset/received'}:undefined});
 await local(v);await choose(v);assert.equal(v.getId('downloadRows').children.length,2);v.getId('downloadRows').children[0].children[3].children[0].onclick({});await flush(v);
 v.getId('downloadPreparePreview').onclick();await flush(v);assert.equal(v.getId('downloadPlayer').src,'p011media://asset/received');
 assert.deepEqual(calls.find(c=>c.op==='download.preview')?.p,{resource_id:'owned-a'});
 assert(!calls.some(c=>['download.preview.prepare','download.import','download.resolve.start'].includes(c.op)));v.dispose();
});

test('local receive selects all 21 files across bounded pages before an explicit start',async()=>{
 const many=Array.from({length:21},(_,i)=>({...rows[0],id:'local-'+i,title:i+'.MP4'}));
 const {v,calls}=await setup({items:many,extra:async(op,p)=>op==='download.resolve.list'?{session_id:'local-session',source_kind:'local',items:many.slice((p.page-1)*20,p.page*20),total:21,total_read:21,completion:'complete'}:undefined});
 await local(v);await choose(v);assert.equal(v.getId('downloadRows').children.length,20);
 assert(!calls.some(c=>c.op==='download.start'));v.getId('downloadStart').onclick();await flush(v);
 assert.equal(calls.find(c=>c.op==='download.start').p.candidate_ids.length,21);v.dispose();
});
test('late receive status after mode switch cannot replace link catalog or show verification',async()=>{
 let finish;const {v,calls}=await setup({extra:async op=>op==='download.resolve.status'?new Promise(r=>finish=r):undefined});
 await local(v);await choose(v);v.getId('downloadLinkTab').onclick();await flush(v);
 finish({state:'succeeded',session_id:'local-session',verification_required:true});await flush(v);
 assert.equal(v.getId('downloadLocalSource').hidden,true);assert(!calls.some(c=>c.op==='download.resolve.list'));
 assert.notEqual(v.getId('downloadModal').style.display,'grid');v.dispose();
});
test('local repeat confirmation keeps the original frozen selection and uses processing language',async()=>{
 let starts=0;const {v,calls}=await setup({extra:async op=>op==='download.start'&&starts++===0?{confirmation_required:true,existing_count:1,active_count:0}:undefined});
 await local(v);await choose(v);v.getId('downloadStart').onclick();await flush(v);
 assert(v.find(n=>n.tag==='h2'&&n.textContent==='再次处理'));assert(v.find(n=>n.textContent.includes('已有处理结果')));
 const frozen=structuredClone(calls.find(c=>c.op==='download.start').p);
 v.getId('downloadRows').children[1].children[0].children[0].checked=false;v.getId('downloadRows').children[1].children[0].children[0].onchange();
 v.getId('downloadVideoAction').onclick();await v.click('确认重新处理');await flush(v);
 const requests=calls.filter(c=>c.op==='download.start');assert.equal(requests.length,2);
 assert.deepEqual(requests[1].p,{...frozen,confirm_existing:true});v.dispose();
});

test('default-selection page reply is discarded after switching mode',async()=>{
 let finish,reads=0;const {v,calls}=await setup({extra:async op=>op==='download.resolve.list'&&++reads===2?new Promise(r=>finish=r):undefined});
 await local(v);await choose(v);assert(finish);v.getId('downloadLinkTab').onclick();await flush(v);
 finish({session_id:'local-session',source_kind:'local',items:rows,total:2,total_read:2,completion:'complete'});await flush(v);
 assert.equal(v.getId('downloadStart').disabled,true);assert(!calls.some(c=>c.op==='download.start'));v.dispose();
});
test('mode change stops queued intake polling and cannot imply a processing task was created',async()=>{
 let finish;const {v,calls}=await setup({extra:async op=>op==='download.local.choose'?new Promise(r=>finish=r):op==='download.resolve.status'?{state:'running',session_id:'local-session'}:undefined});
 await local(v);await choose(v);assert.match(v.getId('downloadStart').textContent,/接收视频/);
 finish({job_id:'local-intake',session_id:'local-session'});await flush(v);
 v.getId('downloadLinkTab').onclick();await flush(v);const readCount=calls.filter(c=>c.op==='download.resolve.status').length;
 for(let i=0;i<3;i++)await v.tick();assert.equal(calls.filter(c=>c.op==='download.resolve.status').length,readCount);
 assert(!calls.some(c=>c.op==='download.start'));v.dispose();
});
test('new intake clears prior local filtering so received files are all selected',async()=>{
 const {v,calls}=await setup({extra:async(op,p)=>op==='download.resolve.list'?{session_id:'local-session',source_kind:'local',items:p.filter.query?rows.slice(0,1):rows,total:p.filter.query?1:2,total_read:2,completion:'complete'}:undefined});
 await local(v);await choose(v);v.getId('downloadSearch').value='甲';v.getId('downloadSearch').onchange();await flush(v);
 await choose(v);v.getId('downloadStart').onclick();await flush(v);
 assert.deepEqual(calls.find(c=>c.op==='download.start')?.p.candidate_ids,['local-a','local-b']);v.dispose();
});
test('late initial settings cannot reset an explicitly switched local mode',async()=>{
 let finish;const {v}=await setup({extra:async op=>op==='download.settings.read'?new Promise(r=>finish=r):undefined});
 await local(v);finish({source_mode:'link',options:linkOptions,filter:{query:'old',state:''}});await flush(v);
 assert.equal(v.getId('downloadLocalSource').hidden,false);assert.equal(v.getId('downloadStart').textContent,'开始提取');assert.equal(v.getId('downloadSearch').value,'');v.dispose();
});
test('local processed and interrupted catalog states use truthful local labels',async()=>{
 const {v}=await setup({items:[{...rows[0],state:'processed'},{...rows[1],state:'interrupted',source_resource_id:null}]});
 await local(v);await choose(v);const labels=descendants(v.getId('downloadRows')).map(n=>n.textContent).join(' ');
 assert.match(labels,/已处理 · 本地视频/);assert.match(labels,/重新选择视频/);assert(!labels.includes('封面暂不可用'));v.dispose();
});
for(const failure of ['status','catalog','selection'])test('scheduled local '+failure+' rejection retains a same-job retry and cancel action',async()=>{
 let statuses=0,lists=0,failed=false;
 const {v,calls}=await setup({extra:async(op)=>{
  if(op==='download.resolve.status'){
   statuses++;if(statuses===2&&failure==='status'){failed=true;throw Error('状态连接暂时失败');}
   return {state:statuses===1?'running':'succeeded',session_id:'local-session',received:2,failed:0};
  }
  if(op==='download.resolve.list'){
   lists++;if(!failed&&((failure==='catalog'&&lists===2)||(failure==='selection'&&lists===3))){failed=true;throw Error('列表连接暂时失败');}
  }
 }});await local(v);await choose(v);for(let i=0;i<3;i++)await v.tick();await flush(v);
 assert(failed,'a scheduled status/catalog/selection request must actually fail');
 assert.equal(v.getId('downloadLocalRetry').hidden,false);assert.equal(v.getId('downloadLocalRetry').disabled,false);
 assert.equal(v.getId('downloadLocalCancel').hidden,false);assert.equal(v.getId('downloadLocalCancel').disabled,false);
 assert.match(v.getId('downloadLocalStatus').textContent,/核对|重试/);
 assert.equal(v.getId('downloadStart').textContent,'等待接收核对');
 v.getId('downloadLocalRetry').onclick?.();await flush(v);
 assert.equal(v.getId('downloadLocalChoose').disabled,false);assert.equal(v.getId('downloadStart').disabled,false);
 assert(calls.filter(c=>c.op==='download.resolve.status').every(c=>c.p.job_id==='local-intake'));
 assert(calls.filter(c=>c.op==='download.resolve.list').every(c=>c.p.session_id==='local-session'));
 assert.equal(calls.filter(c=>c.op==='download.local.choose').length,1);assert(!calls.some(c=>c.op==='download.local.drop'||c.op==='download.start'));v.dispose();
});
test('cancel after a scheduled local status failure settles the same intake without a new receipt',async()=>{
 let statuses=0,cancelled=false;
 const {v,calls}=await setup({extra:async op=>{
  if(op==='download.resolve.status'){if(++statuses===2)throw Error('状态读取失败');return {state:cancelled?'cancelled':'running',session_id:'local-session',received:2};}
  if(op==='download.cancel'){cancelled=true;return {accepted:true};}
 }});await local(v);await choose(v);for(let i=0;i<3;i++)await v.tick();await flush(v);
 v.getId('downloadLocalCancel').onclick?.();await flush(v);
 assert.deepEqual(calls.find(c=>c.op==='download.cancel')?.p,{job_id:'local-intake'});
 assert.equal(v.getId('downloadLocalChoose').disabled,false);assert.equal(v.getId('downloadLocalCancel').hidden,true);
 assert.equal(calls.filter(c=>c.op==='download.local.choose').length,1);v.dispose();
});
test('returning to local mode retains the unobserved intake for explicit same-job checking',async()=>{
 let statuses=0;const {v,calls}=await setup({extra:async op=>{
  if(op==='download.resolve.status'){if(++statuses===2)throw Error('状态读取失败');return {state:statuses===1?'running':'succeeded',session_id:'local-session',received:2};}
 }});await local(v);await choose(v);for(let i=0;i<3;i++)await v.tick();await flush(v);
 v.getId('downloadLinkTab').onclick();await flush(v);await local(v);
 assert.equal(statuses,2);assert.equal(v.getId('downloadLocalRetry').hidden,false);assert.equal(v.getId('downloadLocalChoose').disabled,true);
 v.getId('downloadLocalRetry').onclick();await flush(v);
 assert.equal(v.getId('downloadLocalChoose').disabled,false);assert.equal(v.getId('downloadStart').disabled,false);
 assert(calls.filter(c=>c.op==='download.resolve.status').every(c=>c.p.job_id==='local-intake'));
 assert.equal(calls.filter(c=>c.op==='download.local.choose').length,1);assert(!calls.some(c=>c.op==='download.start'));v.dispose();
});
test('unconfirmed local start retries frozen payload and request ID after live outputs are cleared',async()=>{
 let starts=0;const {v,calls}=await setup({extra:async op=>{if(op==='download.start'&&starts++===0)throw Error('任务收据连接中断');}});
 await local(v);await choose(v);const endpoint=v.window.webkit.messageHandlers.p011App,post=endpoint.postMessage,wire=[];
 endpoint.postMessage=raw=>{wire.push(JSON.parse(raw));return post(raw);};
 v.getId('downloadStart').onclick();await flush(v);
 v.getId('downloadVideoAction').onclick();await flush(v);
 v.getId('downloadStart').onclick();await flush(v);const requests=wire.filter(c=>c.operation==='download.start');
 assert.equal(requests.length,2);assert.deepEqual(requests[1],requests[0]);assert.equal(requests[1].params.options.audio_format,'mp3');
 assert.equal(calls.filter(c=>c.op==='download.local.choose').length,1);v.dispose();
});
test('local state filters send received processed interrupted values and restore link option values',async()=>{
 const items=[{...rows[0],state:'received'},{...rows[1],state:'processed'},{...rows[0],id:'local-c',state:'interrupted',source_resource_id:null}];
 const {v,calls}=await setup({items,extra:async(op,p)=>{
  if(op==='download.resolve.list'){const matched=items.filter(row=>!p.filter.state||row.state===p.filter.state);return {session_id:'local-session',source_kind:'local',items:matched,total:matched.length,total_read:2,completion:'complete'};}
 }});const state=v.getId('downloadState');state.options=[{value:'',textContent:'全部状态'},{value:'not_downloaded',textContent:'未下载'},{value:'downloaded',textContent:'已下载'},{value:'interrupted',textContent:'需重新选择',hidden:true,disabled:true}];
 await local(v);assert.deepEqual(state.options.filter(o=>!o.hidden).map(o=>o.value),['','received','processed','interrupted']);assert.equal(state.options[1].textContent,'已接收');await choose(v);
 for(const value of ['received','processed','interrupted']){state.value=value;state.onchange();await flush(v);
  assert.equal(calls.filter(c=>c.op==='download.resolve.list').at(-1).p.filter.state,value);
  assert.equal(calls.filter(c=>c.op==='download.settings.save').at(-1).p.filter.state,value);
  assert.equal(v.getId('downloadRows').children.length,1);assert.match(descendants(v.getId('downloadRows')).map(n=>n.textContent).join(' '),value==='received'?/已接收 · 本地视频/:value==='processed'?/已处理 · 本地视频/:/视频副本不可用，请重新选择视频/);
 }
 v.getId('downloadLinkTab').onclick();await flush(v);assert.deepEqual(state.options.filter(o=>!o.hidden).map(o=>o.value),['','not_downloaded','downloaded']);assert.equal(state.options[3].disabled,true);v.dispose();
});
