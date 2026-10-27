const test=require('node:test'),assert=require('node:assert/strict');
const {AccountLogin}=require('../../src/yijianjifa/webui/assets/account_login.js');
const {mount}=require('./support/commerce_view.cjs');

test('busy login offers only the exact account original verification and opens it on explicit choice',async()=>{
 const calls=[],opened=[],questions=[];
 const handled=await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push([op,p]);return {request:{id:'r',account_id:'target',delivery_id:'original'}};},ask:async(...args)=>{questions.push(args);return true;},openVerification:async delivery=>{opened.push(delivery);return true;}});
 assert.equal(handled,true);assert.deepEqual(calls,[['multi_publish.intervention.status',{account_id:'target'}]]);
 assert.deepEqual(opened,['original']);assert.match(questions[0][1],/验证/);
 assert.deepEqual(questions[0][2].map(a=>a[0]),['关闭','处理验证']);
});

for(const request of [null,{id:'r',account_id:'other',delivery_id:'wrong',account_name:'同名账号'},{id:'r',account_id:'target'}])test('busy login without exact original delivery only explains contention '+JSON.stringify(request),async()=>{
 const questions=[],opened=[];
 assert.equal(await AccountLogin.handleBusy('target',{api:async()=>({request}),ask:async(...args)=>{questions.push(args);return true;},openVerification:async id=>opened.push(id)}),true);
 assert.deepEqual(opened,[]);assert.deepEqual(questions[0][2].map(a=>a[0]),['知道了']);
 assert.doesNotMatch(questions[0][1],/已过期|登录失效/);
});

test('declining recovery never resumes, relogs or opens a page',async()=>{
 const calls=[];
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push(op);return {request:{account_id:'target',delivery_id:'original'}};},ask:async()=>false,openVerification:async()=>assert.fail('declined recovery opened a page')});
 assert.deepEqual(calls,['multi_publish.intervention.status']);
});

test('unavailable account lookup degrades to safe explanation without leaking raw errors',async()=>{
 let text;
 await AccountLogin.handleBusy('target',{api:async()=>{throw Error('Cookie=private-token');},ask:async(_title,value,actions)=>{text=value;assert.deepEqual(actions.map(a=>a[0]),['知道了']);}});
 assert.doesNotMatch(text,/private-token|Cookie/);
});

test('a disposed view does not open a late busy explanation',async()=>{
 let alive=true,finish;const pending=AccountLogin.handleBusy('target',{api:()=>new Promise(resolve=>finish=resolve),alive:()=>alive,ask:async()=>assert.fail('disposed view showed modal')});
 alive=false;finish({request:null});assert.equal(await pending,false);
});

test('account management replaces generic login failure with original verification recovery',async()=>{
 const row={id:'a',platform:'douyin',nickname:'同名账号',remark:'',group_name:'',login_state:'valid',is_default:false,capabilities:{ordinary:'pending',commerce:'pending',location_scope:'pending'}},calls=[],opened=[];
 const view=await mount((op,p)=>{calls.push([op,p]);if(op==='account.login.availability')return {available:true,platforms:['douyin']};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;if(op==='account.login.start')return {id:'j',state:'failed',code:'login_account_busy'};if(op==='multi_publish.intervention.status')return {request:{id:'r',account_id:'a',delivery_id:'original'}};throw Error(op);},'accounts');
 view.window.p011OpenPublishVerification=async delivery=>{opened.push(delivery);return true;};
 view.getId('rows').children[0].onclick({target:{}});await view.flush();await view.click('重新登录');await view.flush();
 assert.equal(view.getId('modal').style.display,'grid');assert.match(view.getId('dialog').children[0].textContent,/验证|处理/);
 assert.ok(calls.some(([op,p])=>op==='multi_publish.intervention.status'&&p.account_id==='a'));
 assert.deepEqual(opened,[]);await view.click('处理验证');assert.deepEqual(opened,['original']);
 assert.equal(calls.filter(([op])=>op==='account.login.start').length,1);
 assert.equal(row.login_state,'valid');assert.equal(calls.some(([op])=>/respond|resume|reconcile|execution.start/.test(op)),false);
 view.dispose();
});

for(const platform of ['channels','douyin','kuaishou','bilibili'])test(platform+' busy native login opens the bound original browser without another question or publishing authority',async()=>{
 const calls=[],opened=[],request={id:'native-request',account_id:'target',delivery_id:'original',platform,browser_login:true,final_started:false};
 assert.equal(await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push({op,p});return op.endsWith('.status')?{request}:{accepted:true,view_revision:2};},ask:async()=>assert.fail('native recovery asked another question'),openVerification:async delivery=>{opened.push(delivery);return true;}}),true);
 assert.deepEqual(opened,['original']);assert.deepEqual(calls,[{op:'multi_publish.intervention.status',p:{account_id:'target'}},{op:'multi_publish.intervention.respond',p:{id:'native-request',delivery_id:'original',action:'visible'}}]);
});

test('ordinary busy native login falls back to its exact account and uses only the legacy display action',async()=>{
 const calls=[],opened=[],request={id:'native-request',account_id:'target',platform:'douyin',browser_login:true,final_started:false};
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push({op,p});if(op==='multi_publish.intervention.status')return {request:null};return op==='publish.intervention.status'?{request}:{accepted:true,view_revision:2};},ask:async()=>assert.fail('native recovery asked another question'),openVerification:async key=>{opened.push(key);return true;}});
 assert.deepEqual(opened,[{request_id:'native-request',account_id:'target'}]);
 assert.deepEqual(calls.at(-1),{op:'publish.intervention.respond',p:{id:'native-request',action:'visible'}});
 assert.equal(calls.some(c=>/execution|done|resume|reconcile/.test(c.op)||c.p.action==='done'),false);
});

test('native opening failures allow an explicit retry without raw errors or a continuation',async()=>{
 const calls=[],questions=[];let tries=0;
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push({op,p});if(op.endsWith('.status'))return {request:{id:'native-request',account_id:'target',delivery_id:'original',browser_login:true,final_started:false}};if(++tries===1)throw Error('Cookie=private');return {accepted:true,view_revision:2};},ask:async(...args)=>{questions.push(args);return true;},openVerification:async()=>true});
 assert.equal(tries,2);assert.equal(questions.length,1);assert.doesNotMatch(questions[0][1],/Cookie|private|成功/);assert.equal(calls.some(c=>c.p.action==='done'),false);
});

test('a failed request display never sends a browser action to an unshown request',async()=>{
 let writes=0;
 await AccountLogin.handleBusy('target',{api:async(op)=>{if(op.endsWith('.status'))return {request:{id:'native-request',account_id:'target',delivery_id:'original',browser_login:true}};writes++;return {accepted:true};},ask:async()=>false,openVerification:async()=>false});
 assert.equal(writes,0);
});

test('native recovery stops after disposal before a browser action can be sent',async()=>{
 let live=true,writes=0;
 await AccountLogin.handleBusy('target',{api:async(op)=>{if(op.endsWith('.status'))return {request:{id:'native-request',account_id:'target',delivery_id:'original',browser_login:true}};writes++;return {accepted:true};},ask:async()=>assert.fail('disposed recovery showed another question'),alive:()=>live,openVerification:async()=>{live=false;return true;}});
 assert.equal(writes,0);
});

for(const [action,operation] of [['重新登录','account.login.start'],['打开后台','account.backend.start']])test('account management '+action+' brings back the native original window without a second confirmation or resuming',async()=>{
 const calls=[],opened=[],row={id:'creator',platform:'channels',nickname:'视频号甲',remark:'',group_name:'',login_state:'valid',is_default:false,capabilities:{ordinary:'pending',commerce:'pending',location_scope:'pending'}};
 const request={id:'native-request',delivery_id:'original',account_id:row.id,platform:row.platform,browser_login:true,final_started:false};
 const v=await mount((op,p)=>{calls.push({op,p});if(op==='account.login.availability')return {available:true,platforms:['channels']};if(op==='multi_publish.catalog')return {items:[{platform:'channels',login_ready:true,ordinary_ready:true}]};if(op==='account.list')return {items:[row],total:1,groups:[]};if(op==='account.get')return row;if(op===operation)return {id:'busy',state:'failed',code:'login_account_busy'};if(op==='multi_publish.intervention.status')return {request};if(op==='multi_publish.intervention.respond')return {accepted:true,view_revision:2};throw Error(op);},'accounts');
 v.window.p011OpenPublishVerification=async key=>{opened.push(key);return true;};v.getId('rows').children[0].onclick({target:{}});await v.flush();await v.click(action);await v.flush();
 assert.deepEqual(opened,['original']);assert.deepEqual(calls.filter(c=>c.op==='multi_publish.intervention.respond').map(c=>c.p),[{id:'native-request',delivery_id:'original',action:'visible'}]);
 assert.notEqual(v.getId('modal').style.display,'grid');assert.equal(calls.some(c=>c.p.action==='done'||/resume|execution\.start/.test(c.op)),false);
 assert.doesNotMatch(v.getId('toast').textContent,/登录成功|已保存|后台已关闭/);v.dispose();
});

test('a submitted target retains its manual recovery choice even if a stale browser flag is present',async()=>{
 const calls=[],opened=[],questions=[];
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push({op,p});return {request:{id:'native-request',account_id:'target',delivery_id:'original',browser_login:true,final_started:true}};},ask:async(...args)=>{questions.push(args);return false;},openVerification:async key=>{opened.push(key);return true;}});
 assert.equal(questions.length,1);assert.deepEqual(opened,[]);assert.equal(calls.some(c=>c.op.endsWith('.respond')),false);
});

test('an unrelated ordinary login never opens from an exact-account recovery request',async()=>{
 const opened=[],calls=[];
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{calls.push({op,p});return {request:op==='publish.intervention.status'?{id:'other-request',account_id:'other',browser_login:true,platform:'douyin'}:null};},ask:async()=>false,openVerification:async key=>{opened.push(key);return true;}});
 assert.deepEqual(opened,[]);assert.equal(calls.some(c=>c.op.endsWith('.respond')),false);
});

test('an explicit retry rereads the exact account request instead of repeatedly sending a replaced nonce',async()=>{
 const writes=[],opened=[];let active='old-request',reads=0,questions=0;
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{if(op==='multi_publish.intervention.status'){reads++;return {request:{id:active,account_id:'target',delivery_id:'original',browser_login:true,final_started:false}};}writes.push(p);return {accepted:p.id===active};},openVerification:async key=>{opened.push(key);active='new-request';return true;},ask:async()=>++questions===1});
 assert.equal(reads,2);assert.equal(questions,1);assert.deepEqual(opened,['original','original']);assert.deepEqual(writes.map(p=>p.id),['old-request','new-request']);assert.equal(writes.every(p=>p.action==='visible'),true);
});

for(const replacement of [{kind:'complex',browser_login:false},{final_started:true},{account_id:'different'}])test('explicit retry rechecks the original account and native boundary after the request changes: '+JSON.stringify(replacement),async()=>{
 let changed=false,questions=0,reads=0;const writes=[],opened=[];
 await AccountLogin.handleBusy('target',{api:async(op,p)=>{if(op.endsWith('.status')){if(op==='publish.intervention.status')return {request:null};reads++;return {request:{id:changed?'new-request':'old-request',account_id:'target',delivery_id:'original',browser_login:true,final_started:false,...(changed?replacement:{})}};}writes.push(p);return {accepted:false};},openVerification:async key=>{opened.push(key);changed=true;return true;},ask:async()=>++questions===1});
 assert.equal(reads,2);assert.equal(questions,2);assert.deepEqual(opened,['original']);assert.deepEqual(writes,[{id:'old-request',delivery_id:'original',action:'visible'}]);
});
