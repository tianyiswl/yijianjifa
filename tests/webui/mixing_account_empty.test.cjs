const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');

function fixture(accounts=[],extra=()=>undefined){return (op,p)=>{
 const result=extra(op,p);if(result!==undefined)return result;
 if(op==='mix.daily.read')return {settings:null,accounts,capabilities:{douyin:['platform_schedule']},has_saved_settings:false};
 if(op==='mix.daily.save')return {saved:true};
 if(op==='preset.mix.list')return {items:[],total:0};
 if(op==='settings.output.read')return {directory:'/tmp/finished'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:[],total:p.kind==='music'?0:1};
 if(op==='settings.mix_preset.read')return {id:''};
 throw Error(op);
};}

test('a confirmed empty publishing account list offers login directly below its field',async()=>{
 const view=await mountMixing(fixture());
 const entry=view.get('daily-login-account');
 assert.ok(entry,'empty publishing accounts need a visible login entry');
 assert.equal(entry.textContent,'登录发布账号');
 assert.ok(entry.closest('.daily-account-field'));
 assert.equal(entry.classList.contains('hide'),false);
 for(const style of ['small','soft'])assert.equal(entry.classList.contains(style),true);
 assert.equal(view.get('quick-generate').disabled,true);
 assert.equal(view.get('quick-only-generate').disabled,false);
 assert.equal(view.routes.length,0);
 view.dispose();
});

test('an existing unselected account does not show the no-account login entry',async()=>{
 const view=await mountMixing(fixture([{id:'a',platform:'douyin',nickname:'发布主号',login_state:'expired',is_default:false}]));
 const entry=view.get('daily-login-account');
 assert.ok(entry);
 assert.equal(entry.classList.contains('hide'),true);
 assert.equal(view.query('[data-account="a"]').checked,false);
 assert.equal(view.get('daily-account-select').textContent,'请选择发布账号');
 view.dispose();
});

test('login entry preserves the current draft and uses account management without starting login or publishing',async()=>{
 const view=await mountMixing(fixture()),entry=view.get('daily-login-account');
 assert.ok(entry);
 await entry.emit('click',{isTrusted:false});await view.flush();
 assert.equal(view.routes.length,0);
 await view.change(view.get('daily-publish-days'),'7');
 await view.change(view.get('daily-publish-per-day'),'1');
 await view.click(entry);
 assert.deepEqual(view.routes,[['accounts']]);
 const saved=view.calls.find(call=>call.operation==='mix.daily.save');
 assert.ok(saved);assert.equal(saved.params.mix.quantity,7);assert.deepEqual(saved.params.publishing.targets,[]);
 assert.equal(view.calls.some(call=>call.operation==='account.login.start'||call.operation==='mix.daily.submit'||call.operation==='mix.submit'),false);
 view.dispose();
});

test('the empty-account entry stays hidden until the account read completes',async()=>{
 let finish;const read=new Promise(resolve=>{finish=resolve;});
 const view=await mountMixing(fixture([],op=>op==='mix.daily.read'?read:undefined));
 const entry=view.get('daily-login-account');
 assert.ok(entry);assert.equal(entry.classList.contains('hide'),true);
 finish({settings:null,accounts:[],capabilities:{},has_saved_settings:false});
 await view.flush();await view.flush();
 assert.equal(entry.classList.contains('hide'),false);
 view.dispose();
});
