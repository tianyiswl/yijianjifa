const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {parse,Element}=require('./support/mixing_view.cjs');
const assets=path.resolve(__dirname,'../../src/yijianjifa/webui/assets');
const videoOptions={save_video:true,prefer_clean:true,audio_format:null,silent_video:false,asr_language:null,ocr_enabled:false,model_ids:[]};
const audioOptions={...videoOptions,save_video:false,prefer_clean:false,audio_format:'mp3'};
const legacyOptions={...videoOptions,save_video:false,audio_format:'wav',silent_video:true,asr_language:'zh',ocr_enabled:true,model_ids:['whisper-small-multilingual','rapidocr-cls-mobile-v2']};
const flush=async view=>{for(let n=0;n<5;n++)await view.flush();};
async function setup(extra=async()=>undefined,incoming){
 const calls=[];const handler=async(op,p,request)=>{
  calls.push({op,p:structuredClone(p),request_id:request.request_id});const result=await extra(op,p);if(result!==undefined)return result;
  if(op==='download.settings.read')return {options:legacyOptions,source_mode:'link',filter:{query:'',state:''}};
  if(op==='download.settings.save')return {saved:true};
  if(op==='download.output.read')return {grant_id:'saved-folder',display_path:'上次保存位置'};
  if(op==='download.models.list')return {items:[],jobs:[]};
  if(op==='download.list')return {items:[],total:0};
  if(op==='download.resolve.start')return {job_id:'read-link',session_id:'link-session'};
  if(op==='download.local.choose')return {job_id:'read-local',session_id:'local-session'};
  if(op==='download.resolve.status')return {state:'succeeded',session_id:p.job_id==='read-local'?'local-session':'link-session'};
  if(op==='download.resolve.list')return {session_id:p.session_id,source_kind:p.session_id==='local-session'?'local':'douyin_video',items:[{id:'video-a',title:'测试视频'}],total:1,total_read:1,completion:'complete'};
  if(op==='download.start')return {task_id:'task',count:1};
  if(op==='download.detail')return {state:'completed',options:videoOptions,items:[]};
  if(op==='download.resume'||op==='download.retry'||op==='download.cancel')return {accepted:true};
  throw Error('unexpected '+op);
 };
 const root=parse(fs.readFileSync(path.join(assets,'downloads.html'),'utf8')),document={body:root.querySelector('body'),createElement:tag=>new Element(tag),getElementById:id=>root.querySelector('#'+id),querySelector:selector=>root.querySelector(selector),querySelectorAll:selector=>root.querySelectorAll(selector)},timers=new Map();let timerId=0;
 const context={document,crypto:require('node:crypto').webcrypto,P011Views:{},P011Router:{go(){}},setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),Option:function(text,value){const option=new Element('option');option.textContent=text;option.value=value;return option;}};
 context.window={webkit:{messageHandlers:{p011App:{postMessage:async raw=>{const request=JSON.parse(raw);try{return JSON.stringify({ok:true,data:await handler(request.operation,request.params,request)});}catch(error){return JSON.stringify({ok:false,error:{message:error.message}});}}}}}};
 for(const script of ['write_intent','selection_toolbar','downloads'])vm.runInNewContext(fs.readFileSync(path.join(assets,script+'.js'),'utf8'),context);
 const dispose=context.P011Views.downloads(incoming),view={getId:id=>document.getElementById(id),ids:new Map(root.querySelectorAll('[id]').map(node=>[node.id,node])),flush:()=>new Promise(resolve=>setImmediate(resolve)),dispose,async tick(){const entry=timers.entries().next().value;if(entry){timers.delete(entry[0]);await entry[1].fn();}await this.flush();},async click(text){const node=root.querySelectorAll('button').find(node=>node.textContent===text);assert(node,'Visible action missing: '+text);node.onclick({isTrusted:true,stopPropagation(){}});await this.flush();}};
 await flush(view);return {view,calls};
}
async function read(view){view.getId('downloadInput').value='https://example.com/video';view.getId('downloadInput').oninput();await view.tick();await flush(view);}
async function start(view){view.getId('downloadStart').onclick();await flush(view);}
test('the default video action never restores old text recognition or audio settings and never queries models',async()=>{
 const {view,calls}=await setup();assert.equal(calls.some(c=>c.op==='download.models.list'),false);await read(view);await start(view);
 assert.deepEqual(calls.find(c=>c.op==='download.start').p.options,videoOptions);view.dispose();
});
test('link audio extraction is one fixed MP3 action with no hidden subtitle or silent-video work',async()=>{
 const {view,calls}=await setup();view.getId('downloadAudioAction').onclick?.();await read(view);await start(view);
 assert.equal(view.getId('downloadStart').textContent,'开始提取');assert.deepEqual(calls.find(c=>c.op==='download.start').p.options,audioOptions);view.dispose();
});
test('local video only extracts MP3 and returning to video download cannot use local candidates',async()=>{
 const {view,calls}=await setup();view.getId('downloadAudioAction').onclick?.();view.getId('downloadLocalTab').onclick();await flush(view);view.getId('downloadLocalChoose').onclick();await flush(view);await start(view);
 assert.deepEqual(calls.find(c=>c.op==='download.start').p.options,audioOptions);
 view.getId('downloadVideoAction').onclick?.();await flush(view);assert.equal(view.getId('downloadLocalSource').hidden,true);assert.equal(view.getId('downloadStart').disabled,true);assert.equal(view.getId('downloadStart').textContent,'开始下载');view.dispose();
});
test('old local subtitle preferences do not replace the default video entry',async()=>{
 const {view,calls}=await setup(async op=>op==='download.settings.read'?{options:legacyOptions,source_mode:'local',filter:{query:'',state:''}}:undefined);
 assert.equal(view.getId('downloadLocalSource').hidden,true);assert.equal(view.getId('downloadStart').textContent,'开始下载');assert(!calls.some(c=>c.op==='download.local.choose'||c.op==='download.start'));view.dispose();
});
test('an incomplete old subtitle task is read-only except cancel and does not automatically resume verification',async()=>{
 const {view,calls}=await setup(async op=>op==='download.detail'?{state:'paused_auth',options:legacyOptions,verification_required:true,items:[{id:'old-item',title:'旧视频',stages:{video:{state:'succeeded'},asr:{state:'failed'}}}]}:undefined,{task_id:'old-task'});
 const nodes=[...view.ids.values()].flatMap(function walk(n){return [n,...n.children.flatMap(walk)];});
 assert(!nodes.some(n=>n.tag==='button'&&/继续未完成|重试未完成|登录.*验证|编辑.*字幕/.test(n.textContent)));
 assert.notEqual(view.getId('downloadModal').style.display,'grid');assert(!calls.some(c=>['download.start','download.resume','download.retry','download.subtitle.read'].includes(c.op)));view.dispose();
});
test('a supported failed audio task retries only its failed audio stage using the existing task',async()=>{
 const {view,calls}=await setup(async op=>op==='download.detail'?{state:'failed',options:audioOptions,items:[{id:'audio-item',title:'音频',stages:{video:{state:'succeeded'},audio:{state:'failed'}}}]}:undefined,{task_id:'audio-task'});
 await view.click('重试未完成步骤');await flush(view);const retry=calls.find(c=>c.op==='download.retry');assert.deepEqual(retry?.p,{task_id:'audio-task',item_ids:['audio-item'],stages:['audio']});assert(!calls.some(c=>c.op==='download.start'));view.dispose();
});
test('an unacknowledged start cannot switch action and retries the same frozen request',async()=>{
 let count=0;const {view,calls}=await setup(async op=>op==='download.start'&&count++===0?Promise.reject(Error('回执连接中断')):undefined);await read(view);await start(view);
 view.getId('downloadAudioAction').onclick?.();await flush(view);await start(view);
 const requests=calls.filter(c=>c.op==='download.start');assert.equal(requests.length,2);assert.deepEqual(requests[1],requests[0]);assert.deepEqual(requests[1].p.options,videoOptions);view.dispose();
});
test('an obsolete paused task can be cancelled without resuming its removed processing',async()=>{
 let cancelled=false;const {view,calls}=await setup(async op=>{
  if(op==='download.cancel'){cancelled=true;return {accepted:true};}
  if(op==='download.detail')return {state:cancelled?'cancelled':'paused_auth',options:legacyOptions,items:[]};
 },{task_id:'old-task'});
 await view.click('取消未完成部分');await flush(view);assert.deepEqual(calls.find(c=>c.op==='download.cancel')?.p,{task_id:'old-task'});assert(!calls.some(c=>['download.resume','download.retry','download.start'].includes(c.op)));view.dispose();
});
test('a newly pasted source clears an old saved filter so the video can be selected directly',async()=>{
 const {view,calls}=await setup(async op=>op==='download.settings.read'?{options:legacyOptions,source_mode:'link',filter:{query:'旧标题',state:'downloaded'}}:undefined);
 await read(view);const listing=calls.find(c=>c.op==='download.resolve.list');assert.deepEqual(listing.p.filter,{query:'',state:''});view.dispose();
});

test('the shipped page contains only video and audio actions with no hidden text-processing controls',async()=>{
 const {view,calls}=await setup();
 assert.equal(view.getId('downloadVideoAction').textContent,'下载视频');assert.equal(view.getId('downloadAudioAction').textContent,'提取音频');
 for(const id of ['downloadModels','downloadModelsState','downloadExtras','downloadSubtitles','downloadRegion','downloadDrawRegion','downloadSaveRegion','asrLanguage','ocrEnabled','silentVideo','audioFormat','saveVideo','preferClean'])assert.equal(view.getId(id),null,'removed control must not exist: '+id);
 assert(!calls.some(c=>c.op.startsWith('download.models.')||c.op.startsWith('download.subtitle.')));view.dispose();
});
test('a pending first folder choice keeps the start action fixed until the task is acknowledged',async()=>{
 let choose;const {view,calls}=await setup(async op=>op==='download.output.read'?{}:op==='download.output.choose'?new Promise(resolve=>choose=resolve):undefined);
 await read(view);await start(view);assert.equal(typeof choose,'function');view.getId('downloadAudioAction').onclick();await flush(view);
 assert.equal(view.getId('downloadVideoAction').attrs['aria-selected'],'true');assert.equal(view.getId('downloadAudioAction').disabled,true);
 choose({grant_id:'first-folder',display_path:'首次保存位置'});await flush(view);
 assert.deepEqual(calls.find(c=>c.op==='download.start')?.p.options,videoOptions);assert.equal(view.getId('downloadAudioAction').disabled,false);view.dispose();
});
test('waiting for settings does not let an audio start change action or source underneath it',async()=>{
 let save;const {view,calls}=await setup(async op=>op==='download.settings.save'?new Promise(resolve=>save=resolve):undefined);await read(view);
 view.getId('downloadAudioAction').onclick();await flush(view);await start(view);view.getId('downloadVideoAction').onclick();view.getId('downloadLocalTab').onclick();await flush(view);
 assert.equal(view.getId('downloadAudioAction').attrs['aria-selected'],'true');assert.equal(view.getId('downloadLocalSource').hidden,true);assert(!calls.some(c=>c.op==='download.start'));
 save({saved:true});await flush(view);assert.deepEqual(calls.find(c=>c.op==='download.start')?.p.options,audioOptions);view.dispose();
});
test('dismissing a repeat confirmation unlocks actions without creating or resuming a task',async()=>{
 const {view,calls}=await setup(async op=>op==='download.start'?{confirmation_required:true,existing_count:1,active_count:0}:undefined);await read(view);await start(view);
 const overlay=view.getId('downloadModal');assert.equal(overlay.style.display,'grid');overlay.onclick({target:overlay});view.getId('downloadAudioAction').onclick();await flush(view);
 assert.equal(overlay.style.display,'none');assert.equal(view.getId('downloadAudioAction').attrs['aria-selected'],'true');assert.equal(calls.filter(c=>c.op==='download.start').length,1);assert(!calls.some(c=>c.op==='download.resume'));view.dispose();
});
