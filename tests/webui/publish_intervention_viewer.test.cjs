const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');

const request=(kind,final_started=false)=>({id:'verification',delivery_id:'target',account_id:'account',account_name:'视频号甲',platform:'channels',kind,final_started,message:'请完成原账号验证',image:'current-frame'});
async function viewer(r){
 const calls=[];
 const v=await mount((op,p)=>{
  calls.push({op,p});
  if(op==='publish.intervention.status')return {request:null};
  if(op==='multi_publish.intervention.status')return {request:r,pending:1};
  if(op==='multi_publish.intervention.respond'){if(['visible','refresh','scroll','pointer'].includes(p.action)){r.view_state='queued';r.view_revision=(r.view_revision||0)+1;}return {accepted:true,view_revision:r.view_revision};}if(op==='multi_publish.intervention.defer')return {accepted:true};
  throw Error(op);
 },'globals',['publish_intervention']);
 if(!(r.platform==='channels'&&r.final_started===true&&r.external_browser===true&&r.kind==='complex'))await v.click('验证遇到问题？');return {v,calls,r};
}
const responses=calls=>calls.filter(c=>c.op==='multi_publish.intervention.respond');
const consent=v=>v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标');
function point(img,x=18,y=30,trusted=true){
 img.naturalWidth=1440;img.naturalHeight=1000;img.setPointerCapture=()=>{};
 img.onpointerdown({isTrusted:trusted,pointerId:1,clientX:x,clientY:y,preventDefault(){}});
 img.onpointerup({isTrusted:trusted,clientX:x,clientY:y});
}

for(const kind of ['sms','login','complex','unknown'])test(`${kind} exposes an original-session viewer without claiming a native browser or a new submission`,async()=>{
 const {v,calls}=await viewer(request(kind));
 assert.ok(v.find(n=>n.tag==='button'&&n.textContent==='打开验证页面'));
 assert.throws(()=>v.find(n=>n.textContent==='显示浏览器处理'||n.textContent==='继续发布'||n.textContent==='重新发布'));
 assert.equal(responses(calls).length,0);
 consent(v).checked=true;
 await v.click('打开验证页面');
 assert.deepEqual(responses(calls)[0].p,{id:'verification',delivery_id:'target',action:'visible'});
 assert.equal(calls.some(c=>/execution\.start|publish\.confirm|account\.backend/.test(c.op)),false);
 v.dispose();
});

test('SMS page expansion retains its original code input and same-account checkbox during screenshot polling',async()=>{
 const {v,calls,r}=await viewer(request('sms'));
 const input=v.find(n=>n.attrs['aria-label']==='验证码'),check=consent(v);
 input.value='654321';check.checked=true;
 await v.click('打开验证页面');
 r.view_mode='page';r.view_state='ready';r.image='expanded-frame';await v.tick();
 const img=v.find(n=>n.tag==='img'),dialog=v.find(n=>(n.className||'').includes('p011-verification-dialog'));
 assert.equal(img.hidden,false);assert.equal(img.src,'data:image/png;base64,expanded-frame');
 assert.match(dialog.className,/p011-verification-page/);
 assert.equal(v.find(n=>n.attrs['aria-label']==='验证码'),input);
 assert.equal(consent(v),check);assert.equal(input.value,'654321');assert.equal(check.checked,true);
 r.image='next-frame';await v.tick();
 assert.equal(v.find(n=>n.tag==='img'),img);assert.equal(img.src,'data:image/png;base64,next-frame');
 point(img);await v.flush();
 assert.deepEqual(responses(calls).at(-1).p,{id:'verification',delivery_id:'target',action:'pointer',code:[144,300],consent_same_identity:true});
 assert.equal(input.value,'654321');
 // An accepted screenshot click is still queued. Serialize code submission
 // until the owning worker has finished that original-page operation.
 assert.equal(v.find(n=>n.textContent==='确认验证码').disabled,true);
 r.view_state='ready';await v.tick();
 await v.click('确认验证码');assert.equal(input.value,'');
 assert.equal(responses(calls).at(-1).p.action,'code');
 v.dispose();
});

test('unchanged screenshot polling never reloads its image and a new frame updates the same verification controls once',async()=>{
 const {v,r,calls}=await viewer({...request('login'),view_mode:'page',view_state:'ready',view_revision:3});
 const img=v.find(n=>n.tag==='img'),dialog=v.find(n=>(n.className||'').includes('p011-verification-dialog')),done=v.find(n=>n.textContent==='已登录，继续'),check=consent(v);check.checked=true;
 let src=img.src,writes=0;Object.defineProperty(img,'src',{configurable:true,get:()=>src,set:value=>{writes++;src=value;}});
 for(let i=0;i<3;i++)await v.tick();assert.equal(writes,0);assert.equal(src,'data:image/png;base64,current-frame');
 r.image='updated-frame';await v.tick();assert.equal(writes,1);assert.equal(src,'data:image/png;base64,updated-frame');await v.tick();assert.equal(writes,1);
 r.view_revision=2;r.image='stale-frame';await v.tick();assert.equal(writes,1);assert.equal(src,'data:image/png;base64,updated-frame');
 assert.equal(v.find(n=>n.tag==='img'),img);assert.equal(v.find(n=>(n.className||'').includes('p011-verification-dialog')),dialog);assert.equal(v.find(n=>n.textContent==='已登录，继续'),done);assert.equal(consent(v),check);assert.equal(check.checked,true);assert.equal(responses(calls).length,0);v.dispose();
});

test('login screenshot accepts only human clicks and explains WeChat quick login without forcing a QR scan',async()=>{
 const {v,calls}=await viewer(request('login'));
 assert.ok(v.find(n=>/微信快捷登录.*微信.*已登录，继续/.test(n.textContent)));
 assert.throws(()=>v.find(n=>/扫描下方.*二维码/.test(n.textContent)));
 const img=v.find(n=>n.tag==='img');
 point(img,18,30,false);await v.flush();assert.equal(responses(calls).length,0);
 point(img);await v.flush();assert.equal(responses(calls).length,0);
 consent(v).checked=true;
 point(img);await v.flush();
 assert.deepEqual(responses(calls).at(-1).p,{id:'verification',delivery_id:'target',action:'pointer',code:[144,300],consent_same_identity:true});
 v.dispose();
});

test('expanded original page provides bounded scroll and screenshot refresh with trusted events',async()=>{
 const {v,calls,r}=await viewer({...request('unknown',true),view_mode:'page',view_state:'ready'});
 for(const label of ['向上滚动','向下滚动','更新画面']){
  const button=v.find(n=>n.tag==='button'&&n.textContent===label);
  button.onclick({isTrusted:false});await v.flush();
 }
 assert.equal(responses(calls).length,0);
 await v.click('向上滚动');r.view_state='ready';await v.tick();await v.click('向下滚动');r.view_state='ready';await v.tick();await v.click('更新画面');
 assert.deepEqual(responses(calls).map(c=>c.p),[
  {id:'verification',delivery_id:'target',action:'scroll',code:[0,-450]},
  {id:'verification',delivery_id:'target',action:'scroll',code:[0,450]},
  {id:'verification',delivery_id:'target',action:'refresh'},
 ]);
 assert.ok(v.find(n=>n.textContent==='检查发布结果'));
 assert.throws(()=>v.find(n=>n.textContent==='继续发布'||n.textContent==='重新发布'));
 v.dispose();
});

test('unknown verification retains its boundary until the original page view is granted',async()=>{
 const {v,calls,r}=await viewer(request('unknown'));
 assert.throws(()=>v.find(n=>n.attrs['aria-label']==='验证码'));
 consent(v).checked=true;
 r.view_mode='page';await v.tick();
 const img=v.find(n=>n.tag==='img');point(img);await v.flush();
 assert.equal(responses(calls).at(-1).p.action,'pointer');
 assert.equal(responses(calls).some(c=>c.p.action==='code'),false);
 v.dispose();
});

test('a trusted complex drag keeps its original screenshot coordinates and target identity',async()=>{
 const {v,calls}=await viewer(request('complex',true));
 const img=v.find(n=>n.tag==='img');
 img.naturalWidth=1440;img.naturalHeight=1000;img.setPointerCapture=()=>{};
 img.onpointerdown({isTrusted:true,pointerId:1,clientX:18,clientY:30,preventDefault(){}});
 img.onpointerup({isTrusted:true,clientX:60,clientY:30});await v.flush();
 assert.deepEqual(responses(calls).at(-1).p,{id:'verification',delivery_id:'target',action:'pointer',code:[144,300,480,300]});
 v.dispose();
});

for(const kind of ['sms','unknown'])test(`legacy ${kind} keeps its original controls and transport`,async()=>{
 const calls=[],r={id:'legacy',account_name:'抖音甲',kind,message:'请完成验证'};
 const v=await mount((op,p)=>{
  calls.push({op,p});
  if(op==='publish.intervention.status')return {request:r};
  if(op==='multi_publish.intervention.status')return {request:null};
  if(op==='publish.intervention.respond')return {accepted:true};
  throw Error(op);
 },'globals',['publish_intervention']);
 for(const label of ['打开验证页面','更新画面','向上滚动','向下滚动'])assert.throws(()=>v.find(n=>n.textContent===label));
 assert.throws(()=>v.find(n=>n.tag==='img'));
 await v.click('显示浏览器处理');
 assert.deepEqual(calls.find(c=>c.op==='publish.intervention.respond').p,{id:'legacy',action:'visible',code:''});
 assert.equal(responses(calls).length,0);
 v.dispose();
});

test('a visible Channels browser after submit does not automatically duplicate its page in a blocking client modal',async()=>{
 const {v,calls}=await viewer({...request('complex',true),external_browser:true});
 assert.throws(()=>v.find(n=>(n.className||'').includes('p011-verification-overlay')));
 assert.equal(responses(calls).length,0);
 await v.window.p011CheckPublishResult('target');
 assert.deepEqual(responses(calls).at(-1).p,{id:'verification',delivery_id:'target',action:'done'});
 assert.equal(calls.some(c=>/execution.start/.test(c.op)),false);v.dispose();
});

test('a real Channels captcha in background mode retains its actionable verification input',async()=>{
 const {v}=await viewer({...request('complex',true),external_browser:false});
 assert(v.find(n=>n.tag==='img'));assert(v.find(n=>n.textContent==='验证完成'));v.dispose();
});
