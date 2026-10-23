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
