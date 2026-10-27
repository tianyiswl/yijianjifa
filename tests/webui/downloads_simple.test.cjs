const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const options={save_video:true,prefer_clean:true,audio_format:null,silent_video:false,asr_language:null,ocr_enabled:false,model_ids:[]};
const flush=async view=>{for(let n=0;n<5;n++)await view.flush();};
async function setup(extra=async()=>undefined){
 const calls=[];
 const view=await mount(async(op,p)=>{
  calls.push({op,p:structuredClone(p)});const result=await extra(op,p);if(result!==undefined)return result;
  if(op==='download.settings.read')return {options,filter:{query:'',state:''}};
  if(op==='download.settings.save')return {saved:true};
  if(op==='download.output.read')return {grant_id:'saved-folder',display_path:'上次保存位置'};
  if(op==='download.models.list')return {items:[],jobs:[]};
  if(op==='download.list')return {items:[],total:0};
  if(op==='download.resolve.start')return {job_id:'read-'+p.input.split('/').at(-1),session_id:'videos'};
  if(op==='download.resolve.status')return {state:'succeeded',session_id:'videos'};
  if(op==='download.resolve.list')return {session_id:'videos',source_kind:'douyin_video',items:[{id:'selected-video',title:'新视频'}],total:1,total_read:1,completion:'complete'};
  if(op==='download.start')return {task_id:'download-task',count:1};
  if(op==='download.detail')return {state:'completed',items:[]};
  throw Error('unexpected '+op);
 },'downloads');await flush(view);return {view,calls};
}
test('pasting a video link automatically reads it but only an explicit start downloads to the saved folder',async()=>{
 const {view,calls}=await setup();view.getId('downloadInput').value='https://example.com/new-video';view.getId('downloadInput').oninput?.();
 await view.tick();await flush(view);
 assert.equal(calls.filter(c=>c.op==='download.resolve.start').length,1);
 assert.equal(view.getId('downloadStart').textContent,'开始下载');
 assert(!calls.some(c=>['download.start','download.output.choose'].includes(c.op)));
 view.getId('downloadStart').onclick();await flush(view);
 const start=calls.find(c=>c.op==='download.start');assert(start);assert.equal(start.p.directory_grant_id,'saved-folder');
 assert.equal(start.p.options.audio_format,null);assert.equal(start.p.options.asr_language,null);view.dispose();
});
test('editing a link immediately clears its old selection and ignores the old late read',async()=>{
 let release;const {view,calls}=await setup(async(op,p)=>op==='download.resolve.start'&&p.input.endsWith('/old')?new Promise(r=>release=r):undefined);
 const input=view.getId('downloadInput');input.value='https://example.com/old';input.oninput?.();const oldRead=view.tick();await flush(view);
 assert.equal(typeof release,'function');input.value='https://example.com/new';input.oninput();await view.tick();await flush(view);
 assert.equal(calls.filter(c=>c.op==='download.resolve.start').length,2);
 release({job_id:'old-read',session_id:'old-session'});await oldRead;await flush(view);
 assert(!calls.some(c=>c.op==='download.resolve.status'&&c.p.job_id==='old-read'));
 view.getId('downloadStart').onclick();await flush(view);assert.equal(calls.filter(c=>c.op==='download.start').length,1);view.dispose();
});
test('a short sequence of edits reads only the latest nonempty link and never opens a login window',async()=>{
 const {view,calls}=await setup();const input=view.getId('downloadInput');input.value='https://example.com/o';input.oninput?.();input.value='https://example.com/final';input.oninput?.();
 for(let n=0;n<3;n++)await view.tick();await flush(view);
 const reads=calls.filter(c=>c.op==='download.resolve.start');assert.equal(reads.length,1);assert.equal(reads[0].p.input,input.value);assert.equal(reads[0].p.show_browser,false);
 input.value='';input.oninput();await view.tick();await flush(view);assert.equal(view.getId('downloadStart').disabled,true);assert.equal(calls.filter(c=>c.op==='download.resolve.start').length,1);view.dispose();
});
for(const cancel of [false,true])test('first start '+(cancel?'cancels cleanly when no folder is chosen':'asks for a folder once then downloads'),async()=>{
 const {view,calls}=await setup(async op=>op==='download.output.read'?{}:op==='download.output.choose'?(cancel?{cancelled:true}:{grant_id:'first-folder',display_path:'首次选择的目录'}):undefined);
 view.getId('downloadInput').value='https://example.com/new-video';view.getId('downloadRead').onclick();await flush(view);
 view.getId('downloadStart').onclick();await flush(view);
 assert.equal(calls.filter(c=>c.op==='download.output.choose').length,1);assert.equal(calls.filter(c=>c.op==='download.start').length,cancel?0:1);
 if(cancel)assert.match(view.getId('downloadStartStatus').textContent,/取消/);else assert.equal(calls.find(c=>c.op==='download.start').p.directory_grant_id,'first-folder');view.dispose();
});

test('login recovery is offered only after the platform asks for verification',async()=>{
 const {view,calls}=await setup(async op=>op==='download.resolve.status'?{state:'failed',session_id:'videos',verification_required:true,message:'需要登录'}:undefined);
 assert.equal(view.getId('downloadReadVisible').hidden,true);
 view.getId('downloadInput').value='https://example.com/requires-login';view.getId('downloadInput').oninput();await view.tick();await flush(view);
 assert.equal(view.getId('downloadReadVisible').hidden,false);assert.equal(view.getId('downloadModal').style.display,'grid');
 assert(!calls.some(c=>c.op==='download.resolve.start'&&c.p.show_browser));assert(!calls.some(c=>c.op==='download.start'));view.dispose();
});
test('preview opens on demand and local source switches to fixed audio extraction',async()=>{
 const {view,calls}=await setup();view.getId('downloadInput').value='https://example.com/new-video';view.getId('downloadInput').oninput();await view.tick();await flush(view);
 view.getId('downloadRows').children[0].children[3].children[0].onclick({});await flush(view);
 assert.equal(view.getId('downloadPreviewPanel').open,true);
 view.getId('downloadAudioAction').onclick();view.getId('downloadLocalTab').onclick();await flush(view);assert.equal(view.getId('downloadStart').textContent,'开始提取');assert(!calls.some(c=>c.op==='download.start'));view.dispose();
});
test('selecting an individual video immediately enables start without rereading the list',async()=>{
 const {view,calls}=await setup(async op=>op==='download.resolve.list'?{session_id:'videos',source_kind:'douyin_profile',items:[{id:'a',title:'甲'},{id:'b',title:'乙'}],total:2,total_read:2,completion:'complete'}:undefined);
 view.getId('downloadInput').value='https://example.com/profile';view.getId('downloadInput').oninput();await view.tick();await flush(view);
 assert.equal(view.getId('downloadStart').disabled,true);const choice=view.getId('downloadRows').children[0].children[0].children[0];choice.checked=true;choice.onchange();
 assert.equal(view.getId('downloadStart').disabled,false);choice.checked=false;choice.onchange();assert.equal(view.getId('downloadStart').disabled,true);
 assert.equal(calls.filter(c=>c.op==='download.resolve.list').length,1);view.dispose();
});
