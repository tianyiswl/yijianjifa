const test=require('node:test'),assert=require('node:assert/strict');
const {mountMixing}=require('./support/mixing_view.cjs');

function response(op,p){
 if(op==='mix.daily.read')return {settings:null,capabilities:{douyin:['platform_schedule']},accounts:[{id:'account',platform:'douyin',nickname:'演示账号',login_state:'valid',is_default:true}],has_saved_settings:false};
 if(op==='mix.daily.save')return {saved:true};
 if(op==='preset.mix.list')return {items:[],total:0};
 if(op==='settings.mix_preset.read')return {id:''};
 if(op==='settings.output.read')return {directory:'/tmp/output'};
 if(op==='voice.catalog')return {selected:'',favorites:[],items:[]};
 if(op==='category.list')return {items:[],total:0};
 if(op==='library.list')return {items:p.kind==='copy'?[{id:'copy',title:'示例',voice:'一句口播。',full:'全程文字'}]:[],total:p.kind==='music'?0:1};
 throw Error(op);
}

test('effect editing opens directly on picture settings and lets users jump straight to sound and subtitles',async()=>{
 const v=await mountMixing(response);
 await v.click(v.query('[data-action="open-advanced"]'));
 const tabs=v.document.querySelectorAll('[data-step]');
 assert.deepEqual(tabs.map(x=>x.textContent.trim()),['画面','声音','字幕']);
 assert.equal(v.query('[data-panel="0"]').classList.contains('active'),true);
 assert.ok(v.query('[data-panel="0"] [data-group="ratio"]'));
 await v.click(tabs[1]);
 assert.equal(v.query('[data-panel="1"]').classList.contains('active'),true);
 assert.ok(v.query('[data-panel="1"] [data-key="voice"]'));
 await v.click(tabs[2]);
 assert.equal(v.query('[data-panel="2"]').classList.contains('active'),true);
 assert.ok(v.query('[data-panel="2"] [data-key="fullCaption"]'));
 v.dispose();
});

test('finishing effect edits keeps the main publication plan and saves actual sound settings',async()=>{
 const saved=[];const v=await mountMixing((op,p)=>{if(op==='mix.daily.save'){saved.push(p);return {saved:true};}return response(op,p);});
 await v.change(v.get('daily-publish-days'),'3');
 await v.change(v.get('daily-publish-per-day'),'2');
 await v.click(v.query('[data-action="open-advanced"]'));
 await v.click(v.query('[data-step="1"]'));
 await v.change(v.query('[data-key="rate"]'),'1.25');
 const finish=v.query('[data-action="finish-effects"]');assert.ok(finish,'one direct return action finishes effect edits');await v.click(finish);
 assert.equal(v.get('quick-home').classList.contains('hide'),false);
 assert.equal(v.get('advanced-view').classList.contains('hide'),true);
 await v.save();
 assert.equal(saved.at(-1).mix.speed,1.25);
 assert.equal(saved.at(-1).publishing.days,3);
 assert.equal(saved.at(-1).publishing.per_day,2);
 assert.equal(Number(v.get('quick-quantity').value),6);
 v.dispose();
});

test('subtitle fine controls remain available on demand and survive returning to the main screen',async()=>{
 const saved=[];const v=await mountMixing((op,p)=>{if(op==='mix.daily.save'){saved.push(p);return {saved:true};}return response(op,p);});
 await v.click(v.query('[data-action="open-advanced"]'));
 await v.click(v.query('[data-step="2"]'));
 const height=v.query('[data-key="speechHeight"]'),more=height.closest('details');
 assert.ok(more,'subtitle height is kept inside optional fine controls');
 assert.equal(more.attrs.open,undefined);
 more.setAttribute('open','');
 await v.change(height,'72.5');
 await v.change(v.query('[data-key="speechSize"]'),'90');
 await v.click(v.query('[data-action="finish-effects"]'));
 await v.save();
 assert.equal(saved.at(-1).mix.spoken_style.y_percent,72.5);
 assert.equal(saved.at(-1).mix.spoken_style.size,90);
 v.dispose();
});

test('duration badge appears only without copy and does not change the actual configured duration',async()=>{
 const saved=[];const v=await mountMixing((op,p)=>{if(op==='mix.daily.save'){saved.push(p);return {saved:true};}return response(op,p);});
 assert.doesNotMatch(v.get('quick-tags').textContent,/无口播/);
 await v.click(v.get('quick-copy'));
 await v.change(v.query('[data-scope="none"]'),true);
 assert.match(v.get('quick-tags').textContent,/无口播15秒/);
 await v.save();assert.equal(saved.at(-1).mix.total_ms,15000);assert.equal(saved.at(-1).mix.use_copy,false);
 await v.change(v.query('#quick-copy-menu [data-scope="all"]'),true);
 assert.doesNotMatch(v.get('quick-tags').textContent,/无口播/);
 await v.save();assert.equal(saved.at(-1).mix.total_ms,15000);assert.equal(saved.at(-1).mix.use_copy,true);
 v.dispose();
});

test('saved unavailable voice is shown as unavailable and cannot be chosen again',async()=>{
 const v=await mountMixing((op,p)=>op==='voice.catalog'?{selected:'doubao:blocked',favorites:['doubao:ready'],items:[{id:'doubao:blocked',name:'未开通声音',available:false},{id:'doubao:ready',name:'可用声音',available:true}]}:response(op,p));
 const options=v.query('[data-key="voice"]').options;
 const blocked=Array.from(options).find(o=>o.value==='doubao:blocked');
 assert.ok(blocked);assert.equal(blocked.disabled,true);assert.match(blocked.textContent,/暂不可用/);
 assert.equal(Array.from(options).find(o=>o.value==='doubao:ready').disabled,false);
 assert.equal(v.query('[data-key="voice"]').value,'doubao:blocked');
 v.dispose();
});
