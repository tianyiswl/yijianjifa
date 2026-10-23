const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
async function fixture({respond,request={}}={}){
 const calls=[],r={id:'verify-1',delivery_id:'target-1',account_name:'视频号甲',platform:'channels',kind:'unknown',final_started:false,message:'请完成原账号验证',...request};
 const v=await mount(async(op,p)=>{calls.push({op,p});if(op==='publish.intervention.status')return {request:null};if(op==='multi_publish.intervention.status')return {request:r};if(op==='multi_publish.intervention.respond'){if(respond)return respond(p,r);r.view_state='queued';r.view_revision=(r.view_revision||0)+1;return {accepted:true,view_revision:r.view_revision};}throw Error(op);},'globals',['publish_intervention']);await v.click('验证遇到问题？');return {v,calls,r};
}
const feedback=v=>v.find(n=>n.dataset.feedback==='true');
const responses=calls=>calls.filter(c=>c.op==='multi_publish.intervention.respond');

test('Douyin resend is disabled during cooldown and becomes an original page action after sixty seconds',async()=>{
 const {v,r,calls}=await fixture({request:{platform:'douyin',kind:'sms',final_started:true,sms_retry_after:60}});
 const resend=()=>v.find(n=>n.dataset.action==='resend_code');
 assert.equal(resend().disabled,true);assert.match(resend().textContent,/60/);
 r.sms_retry_after=1;await v.tick();assert.equal(resend().disabled,true);
 r.sms_retry_after=0;await v.tick();assert.equal(resend().disabled,false);
 await v.click('再次获取验证码');
 assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'resend_code'});
 v.dispose();
});

test('resend stops waiting when the original page has closed instead of leaving the button permanently busy',async()=>{
 const {v,r}=await fixture({request:{platform:'douyin',kind:'sms',final_started:true,sms_retry_after:0},respond:(p,r)=>{
  r.view_revision=1;r.view_action=p.action;r.view_state='queued';r.sms_state='queued';return {accepted:true,view_revision:1};
 }});
 await v.click('再次获取验证码');r.view_state='failed';r.view_error='原验证页面已关闭，请重新检查账号登录状态。';await v.tick();
 assert.match(feedback(v).textContent,/原验证页面已关闭/);
 assert.equal(v.find(n=>n.dataset.action==='resend_code').disabled,false);v.dispose();
});

test('expansion shows immediate busy feedback and accepted only means queued until an actual page frame is ready',async()=>{
 let release;const {v,calls,r}=await fixture({respond:(p,r)=>{r.view_state='queued';r.view_revision=1;return new Promise(resolve=>release=()=>resolve({accepted:true,view_revision:1}));}});
 await v.click('打开验证页面');assert.match(feedback(v).textContent,/正在读取原任务页面/);assert.equal(v.find(n=>n.textContent==='打开验证页面').disabled,true);
 assert.deepEqual(responses(calls)[0].p,{id:'verify-1',delivery_id:'target-1',action:'visible'});
 release();await v.flush();await v.tick();assert.match(feedback(v).textContent,/正在读取原任务页面/);assert.equal(v.find(n=>n.textContent==='打开验证页面').disabled,true);
 r.view_state='ready';r.view_mode='page';await v.tick();assert.doesNotMatch(feedback(v).textContent,/原任务页面已显示/);
 r.image='original-page-frame';await v.tick();assert.match(feedback(v).textContent,/原任务页面已显示/);assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,original-page-frame');assert.equal(v.find(n=>n.textContent==='打开验证页面').disabled,false);
 assert.equal(calls.some(c=>/execution.start|account.backend/.test(c.op)),false);v.dispose();
});

test('a failed bridge response survives same request polling and does not replace the platform message',async()=>{
 const {v,r}=await fixture({respond:()=>{throw Error('截图读取失败');}});await v.click('打开验证页面');assert.match(feedback(v).textContent,/截图读取失败/);
 for(let i=0;i<3;i++){r.message='平台提示仍需验证';await v.tick();assert.match(feedback(v).textContent,/截图读取失败/);}
 assert.equal(v.find(n=>n.dataset.message==='true').textContent,'平台提示仍需验证');assert.equal(feedback(v).attrs.role,'status');v.dispose();
});

test('a non-accepted response surfaces its reason instead of claiming the page opened',async()=>{
 const {v}=await fixture({respond:()=>({accepted:false,message:'原会话正在关闭，未受理展开'})});await v.click('打开验证页面');
 assert.match(feedback(v).textContent,/未受理展开/);assert.doesNotMatch(feedback(v).textContent,/原任务页面已显示/);await v.tick();assert.match(feedback(v).textContent,/未受理展开/);v.dispose();
});

test('a ready original request returned by the response is rendered immediately without assuming a native browser',async()=>{
 const {v}=await fixture({respond:(p,r)=>({accepted:true,view_revision:1,request:{...r,view_revision:1,view_state:'ready',view_mode:'page',image:'returned-original-frame'}})});await v.click('打开验证页面');
 assert.match(feedback(v).textContent,/原任务页面已显示/);assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,returned-original-frame');assert.equal(v.find(n=>n.textContent==='打开验证页面').disabled,false);v.dispose();
});

test('a worker expansion failure remains visible and enables an explicit retry',async()=>{
 const {v,r,calls}=await fixture();await v.click('打开验证页面');r.view_state='failed';r.view_error='原会话已关闭，请重新核对任务';await v.tick();assert.match(feedback(v).textContent,/原会话已关闭/);
 await v.tick();assert.match(feedback(v).textContent,/原会话已关闭/);assert.equal(v.find(n=>n.textContent==='打开验证页面').disabled,false);
 await v.click('打开验证页面');assert.match(feedback(v).textContent,/正在读取原任务页面/);assert.equal(responses(calls).length,2);v.dispose();
});

test('view-only controls need no identity consent while code, done and pointer still require the original account',async()=>{
 const {v,r,calls}=await fixture({request:{kind:'sms',view_state:'ready',view_mode:'page',image:'original-frame'}});
 await v.click('更新画面');assert.deepEqual(responses(calls).at(-1).p,{id:'verify-1',delivery_id:'target-1',action:'refresh'});r.view_state='ready';await v.tick();
 await v.click('向下滚动');assert.deepEqual(responses(calls).at(-1).p,{id:'verify-1',delivery_id:'target-1',action:'scroll',code:[0,450]});r.view_state='ready';await v.tick();
 const initial=responses(calls).length;await v.click('检查验证状态');assert.equal(responses(calls).length,initial);
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';await v.click('确认验证码');assert.equal(responses(calls).length,initial);assert.equal(input.value,'123456');
 const img=v.find(n=>n.tag==='img');img.naturalWidth=1440;img.naturalHeight=1000;img.setPointerCapture=()=>{};img.onpointerdown({isTrusted:true,pointerId:1,clientX:20,clientY:20,preventDefault(){}});img.onpointerup({isTrusted:true,clientX:20,clientY:20});await v.flush();assert.equal(responses(calls).length,initial);
 v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;await v.click('确认验证码');assert.deepEqual(responses(calls).at(-1).p,{id:'verify-1',delivery_id:'target-1',action:'code',code:'123456',consent_same_identity:true});v.dispose();
});

test('feedback from an old request cannot alter a new verification request',async()=>{
 let release;const {v,r}=await fixture({respond:()=>new Promise((resolve,reject)=>release=()=>reject(Error('旧请求读取失败')))});await v.click('打开验证页面');r.id='verify-2';await v.tick();release();await v.flush();assert.equal(feedback(v).textContent,'');assert.equal(feedback(v).hidden,true);v.dispose();
});

test('an old ready frame cannot complete a new view operation before its response revision is acknowledged',async()=>{
 let release;const {v,r}=await fixture({request:{view_state:'ready',view_revision:1,view_mode:'page',image:'old-frame'},respond:()=>new Promise(resolve=>release=()=>resolve({accepted:true,view_revision:2}))});await v.click('更新画面');
 await v.tick();assert.match(feedback(v).textContent,/正在更新/);assert.equal(v.find(n=>n.textContent==='更新画面').disabled,true);
 r.view_revision=2;r.image='new-frame';await v.tick();assert.match(feedback(v).textContent,/正在更新/);
 release();await v.flush();assert.match(feedback(v).textContent,/原任务画面已更新/);assert.equal(v.find(n=>n.textContent==='更新画面').disabled,false);v.dispose();
});

test('an older failed or ready view revision cannot replace feedback for the newly accepted operation',async()=>{
 const {v,r}=await fixture({request:{view_state:'ready',view_revision:3,view_mode:'page',image:'old-frame'}});await v.click('更新画面');assert.equal(r.view_revision,4);
 r.view_revision=3;r.view_state='failed';r.view_error='旧画面错误';await v.tick();assert.match(feedback(v).textContent,/正在更新/);assert.doesNotMatch(feedback(v).textContent,/旧画面错误/);
 r.view_state='ready';await v.tick();assert.match(feedback(v).textContent,/正在更新/);
 r.view_revision=4;r.image='new-frame';await v.tick();assert.match(feedback(v).textContent,/原任务画面已更新/);v.dispose();
});

test('a late older snapshot cannot replace the latest original-session frame or platform message',async()=>{
 const {v,r}=await fixture({request:{view_revision:5,view_state:'ready',view_mode:'page',image:'latest-frame',message:'当前验证提示'}});
 r.view_revision=4;r.image='stale-frame';r.message='过期提示';r.view_mode='verification';await v.tick();
 assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,latest-frame');assert.equal(v.find(n=>n.dataset.message==='true').textContent,'当前验证提示');assert.match(v.find(n=>(n.className||'').includes('p011-verification-dialog')).className,/p011-verification-page/);
 r.view_revision=5;r.image='same-revision-fresh-frame';r.message='当前提示更新';r.view_mode='page';await v.tick();
 assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,same-revision-fresh-frame');assert.equal(v.find(n=>n.dataset.message==='true').textContent,'当前提示更新');v.dispose();
});

test('the newly accepted view revision rejects older frames while waiting and a new request resets the revision bound',async()=>{
 const {v,r}=await fixture({request:{view_revision:5,view_state:'ready',view_mode:'page',image:'prior-frame',message:'此前有效提示'}});await v.click('更新画面');assert.equal(r.view_revision,6);
 r.view_revision=5;r.image='late-old-frame';r.message='迟到旧提示';await v.tick();assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,prior-frame');assert.equal(v.find(n=>n.dataset.message==='true').textContent,'此前有效提示');assert.match(feedback(v).textContent,/正在更新/);
 r.view_revision=6;r.view_state='ready';r.image='new-frame';r.message='新画面提示';await v.tick();assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,new-frame');assert.match(feedback(v).textContent,/画面已更新/);
 r.id='verify-2';r.view_revision=1;r.image='new-request-frame';r.message='另一验证提示';await v.tick();assert.equal(v.find(n=>n.tag==='img').src,'data:image/png;base64,new-request-frame');assert.equal(v.find(n=>n.dataset.message==='true').textContent,'另一验证提示');v.dispose();
});

test('an unread Bili identity on its management page asks to recheck the original account without inventing a captcha or QR step',async()=>{
 const {v,r,calls}=await fixture({request:{platform:'bilibili',account_name:'合成B站账号',kind:'login',reason:'identity_unavailable',message:'',image:'management-page'}});
 assert(v.find(n=>n.tag==='h3'&&n.textContent.includes('B站')&&n.textContent.includes('原账号待核对')));
 assert.match(v.find(n=>n.dataset.message==='true').textContent,/程序尚未确认.*合成B站账号/);
 assert.throws(()=>v.find(n=>/扫描下方.*二维码|请在下方平台画面完成验证/.test(n.textContent)));
 await v.click('重新核对原账号');assert.equal(responses(calls).length,0);
 v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;await v.click('重新核对原账号');
 assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'done',consent_same_identity:true});
 assert.match(feedback(v).textContent,/已收到.*重新核对.*客户端/);assert.doesNotMatch(feedback(v).textContent,/等候平台|验证完成|登录成功/);v.dispose();
});
test('a changed session and actual login use distinct instructions and continue only the original target',async()=>{
 for(const reason of ['session_changed','login_required']){
  const {v,r,calls}=await fixture({request:{platform:'bilibili',account_name:'原B站账号',kind:'login',reason,message:'',image:'original-page'}});
  const action=reason==='session_changed'?'确认原账号并继续':'已登录，继续';
  assert(v.find(n=>n.tag==='button'&&n.textContent===action));
  assert.match(v.find(n=>n.dataset.message==='true').textContent,reason==='session_changed'?/登录资料已变化/:/需要.*登录/);
  v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;await v.click(action);
  assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'done',consent_same_identity:true});assert.doesNotMatch(feedback(v).textContent,/等候平台|验证成功|登录成功/);v.dispose();
 }
});
test('actual SMS and interactive challenges keep their human gate and accepted feedback describes a client request',async()=>{
 for(const [kind,reason] of [['sms','sms_required'],['complex','challenge_required']]){
  const {v,calls}=await fixture({request:{kind,reason,platform:'bilibili',image:'challenge-page'}});
  assert(v.find(n=>n.textContent===(kind==='sms'?'检查验证状态':'验证完成')));
  if(kind==='sms'){const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';await v.click('确认验证码');assert.equal(responses(calls).length,0);v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;await v.click('确认验证码');assert.match(feedback(v).textContent,/客户端已收到.*验证码.*请求/);}
  else{v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;await v.click('验证完成');assert.match(feedback(v).textContent,/已收到.*继续.*客户端.*检查/);}
  assert.doesNotMatch(feedback(v).textContent,/等候平台|验证成功/);v.dispose();
 }
});
test('reason changes on the same handle redraw the explanation and action instead of keeping an obsolete login prompt',async()=>{
 const {v,r}=await fixture({request:{kind:'login',reason:'identity_unavailable',platform:'bilibili',message:'',image:'same-page'}});
 assert(v.find(n=>n.textContent==='重新核对原账号'));r.reason='session_changed';await v.tick();assert(v.find(n=>n.textContent==='确认原账号并继续'));assert.throws(()=>v.find(n=>n.textContent==='重新核对原账号'));assert.match(v.find(n=>n.dataset.message==='true').textContent,/登录资料已变化/);v.dispose();
});
test('task cancellation is kept in task records instead of competing with verification controls',async()=>{
 const {v,calls}=await fixture({request:{platform:'bilibili',kind:'login',reason:'identity_unavailable'}});assert.throws(()=>v.find(n=>n.textContent==='取消本地目标'));assert(v.find(n=>/任务记录.*删除/.test(n.textContent)));assert.equal(responses(calls).length,0);v.dispose();
});
test('post-final accepted checking describes reading the original result without a new publish or identity grant',async()=>{
 const {v,calls}=await fixture({request:{platform:'bilibili',kind:'login',reason:'identity_unavailable',final_started:true}});await v.click('检查发布结果');assert.equal(responses(calls).at(-1).p.action,'done');assert.equal(responses(calls).at(-1).p.consent_same_identity,undefined);assert.match(feedback(v).textContent,/已收到核对请求.*原提交结果/);assert.throws(()=>v.find(n=>n.textContent==='已登录，继续'||n.textContent==='重新核对原账号'));v.dispose();
});

test('an identity mismatch requires the selected original UID and cannot grant pointer access by ticking consent',async()=>{
 const {v,r,calls}=await fixture({request:{platform:'channels',kind:'login',reason:'identity_mismatch',message:'',view_mode:'page',view_state:'ready',image:'wrong-account-page'}});
 assert(v.find(n=>n.tag==='h3'&&n.textContent.includes('所选账号不一致')));
 assert.match(v.find(n=>n.dataset.message==='true').textContent,/所选.*账号.*不一致.*核对所选原账号/);
 assert(v.find(n=>/UID.*必须.*一致.*勾选.*不.*其他账号/.test(n.textContent)));
 assert.throws(()=>v.find(n=>n.textContent==='已登录，继续'));
 await v.click('重新核对原账号');assert.equal(responses(calls).length,0);
 v.find(n=>n.attrs['aria-label']==='确认同一账号继续此目标').checked=true;
 const img=v.find(n=>n.tag==='img');img.naturalWidth=1440;img.naturalHeight=1000;img.onpointerdown({isTrusted:true,pointerId:1,clientX:20,clientY:20,preventDefault(){}});img.onpointerup({isTrusted:true,clientX:20,clientY:20});await v.flush();assert.equal(responses(calls).length,0);
 await v.click('重新核对原账号');assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'done',consent_same_identity:true});v.dispose();
});
test('a specific safe login reason is preserved instead of replaced by a generic login explanation',async()=>{
 const message='原账号登录已过期；任务尚未提交，请先完成原页面登录。';
 const {v,r}=await fixture({request:{platform:'channels',kind:'login',reason:'login_required',message}});
 assert.equal(v.find(n=>n.dataset.message==='true').textContent,message);
 r.message='登录尚未完成；原任务仍暂停在提交前。';await v.tick();assert.equal(v.find(n=>n.dataset.message==='true').textContent,r.message);v.dispose();
});
test('an identity page-unready explanation and its submission boundary survive rendering and polling',async()=>{
 for(const final_started of [false,true]){
  const message=final_started?'页面尚未就绪，暂时无法核对账号；提交动作已经发生，仅核对原结果。':'页面尚未就绪，暂时无法核对账号；任务尚未提交。';
  const {v,r}=await fixture({request:{platform:'bilibili',kind:'login',reason:'identity_unavailable',final_started,message}});
  assert.equal(v.find(n=>n.dataset.message==='true').textContent,message);
  r.message=message+'请稍后重新核对原账号。';await v.tick();assert.equal(v.find(n=>n.dataset.message==='true').textContent,r.message);v.dispose();
 }
});

for(const kind of ['complex','sms','login'])test('background '+kind+' offers optional page help without asking users to change browser modes and preserves the original target',async()=>{
 const {v,calls,r}=await fixture({request:{kind,external_browser:false,image:'original-frame'}});
 assert.match(v.find(n=>n.dataset.message==='true').textContent,kind==='sms'?/确认验证码.*自动继续/:/验证遇到问题/);
 assert.equal(v.find(n=>n.tag==='img').hidden,true);
 await v.click('打开验证页面');
 assert.deepEqual(responses(calls)[0].p,{id:'verify-1',delivery_id:'target-1',action:'visible'});
 r.view_state='ready';r.view_mode='page';r.image='original-frame';await v.tick();
 assert.equal(v.find(n=>n.tag==='img').hidden,false);
 assert.equal(calls.some(c=>/execution.start|account.backend|browser_mode.save/.test(c.op)),false);v.dispose();
});

test('SMS processing feedback replaces screenshot feedback and reports actual failure',async()=>{
 const {v,r}=await fixture({request:{platform:'douyin',kind:'sms',final_started:true,view_mode:'page',image:'fixture'},respond:()=>({accepted:true,view_revision:2})});
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';await v.click('确认验证码');
 r.view_revision=2;r.view_action='code';r.code_state='processing';r.code_message='正在填写原平台验证码并提交验证。';await v.tick();
 assert.match(feedback(v).textContent,/正在填写/);
 r.code_state='submitted';r.code_message='验证码已填入并提交原平台，正在等待验证结果。';await v.tick();assert.match(feedback(v).textContent,/已填入/);
 r.code_state='failed';r.code_message='平台提示验证码错误或已失效，请重新输入或获取。';await v.tick();assert.match(feedback(v).textContent,/错误或已失效/);v.dispose();
});

test('pressing Enter submits an SMS code through the same original-request handler',async()=>{
 const {v,r,calls}=await fixture({request:{platform:'douyin',kind:'sms',final_started:true}});
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';
 assert.equal(typeof input.onkeydown,'function');input.onkeydown({isTrusted:true,key:'Enter',preventDefault(){}});await v.flush();
 assert.deepEqual(responses(calls).at(-1).p,{id:r.id,delivery_id:r.delivery_id,action:'code',code:'123456'});v.dispose();
});

test('repeated Enter cannot enqueue another code while the original code is processing',async()=>{
 const {v,calls}=await fixture({request:{platform:'douyin',kind:'sms',final_started:true},respond:()=>({accepted:true,view_revision:1})});
 const input=v.find(n=>n.attrs['aria-label']==='验证码');input.value='123456';
 input.onkeydown({isTrusted:true,key:'Enter',preventDefault(){}});await v.flush();
 input.value='123456';input.onkeydown({isTrusted:true,key:'Enter',preventDefault(){}});await v.flush();
 assert.equal(responses(calls).length,1);v.dispose();
});
