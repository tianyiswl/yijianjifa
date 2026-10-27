const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
function deferred(){let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};}
async function fixture(confirm){
 const calls=[],summary={videos:1,accounts:1,targets:1,ready:1,blocked:0,excluded:0};
 const settings={schema_version:2,targets:[{platform:'douyin',account_id:'a'}],distribution_by_platform:{},start_policy:{kind:'relative_tomorrow_noon'},schedule:{mode:'daily',interval:'3',per_day:'1',days:'1',clock_mode:'interval',times:['12:00']},schedule_by_platform:{},common_fields:{},fields_by_platform:{}};
 const handler=async(op,p,r)=>{calls.push({op,p:structuredClone(p)});
  if(op==='account.list')return {items:['a','b'].map((id,i)=>({id,platform:'douyin',nickname:i?'副号':'主号',login_state:'valid',is_default:!i})),total:2};
  if(op==='multi_publish.catalog')return {items:[{platform:'douyin',label:'抖音',schedule_ready:true,ordinary_ready:true,login_ready:true,field_schema:{}}]};
  if(op==='multi_publish.settings.read')return {settings,has_saved_settings:true,issues:[]};
  if(op==='multi_publish.settings.save')return {settings:p.settings,saved:true};
  if(op==='multi_publish.candidates')return {items:[{id:'v',display_name:'待发视频',title:'原标题',body:'原正文',tags:'#自动剪辑',duration_ms:3000}],total:1};
  if(op==='multi_publish.presets.list')return {items:[]};
  if(op==='publish.browser_mode.read')return {mode:'background'};
  if(op==='media.thumbnail')return {image:''};
  if(op==='multi_publish.prepare')return {prepared_id:'p'.repeat(48),intent_id:r.request_id,summary,targets:[]};
  if(op==='multi_publish.confirm')return confirm?confirm():{id:'current',quantity:1,state:'waiting',submitted:false};
  if(op==='multi_publish.detail')return {id:'current',summary,deliveries:[{delivery_id:'d',platform:'douyin',account_id:'a',state:'paused_auth',accepted:true,worker_running:true,can_submit:false,allowed_actions:[]}]};
  return baseData(op);
 };
 const v=await mount(handler,'multi_publish');
 const selected=v.find(n=>n.attrs['aria-label']==='选择 待发视频');selected.checked=true;selected.onchange();await v.flush();
 const field=name=>v.find(n=>n.dataset.field===name),trigger=()=>v.find(n=>n.attrs['aria-controls']==='multi-publish-accounts');
 return {v,calls,field,trigger};
}

test('returning from an unconfirmed preview unlocks real account and date controls and prepares the edited selection',async()=>{
 const f=await fixture();await f.v.click('预览发布安排');
 assert.equal(f.field('start_date').disabled,true);assert.equal(f.trigger().disabled,true);
 await f.v.click('返回修改');assert.equal(f.v.getId('overlay').hidden,true);
 assert.equal(f.field('start_date').disabled,false);assert.equal(f.field('hour').disabled,false);assert.equal(f.trigger().disabled,false);
 const start=f.field('start_date'),next=new Date(start.value+'T12:00:00Z');next.setUTCDate(next.getUTCDate()+1);start.value=next.toISOString().slice(0,10);start.onchange();
 f.field('hour').value='13';f.field('hour').onchange();await f.v.flush();
 await f.v.click(f.trigger().textContent);
 for(const [id,checked]of [['b',true],['a',false]]){const account=f.v.find(n=>n.dataset.account===id);assert.equal(account.disabled,false);account.checked=checked;await account.onchange({isTrusted:true});await f.v.flush();}
 await f.v.click('预览发布安排');
 const preparations=f.calls.filter(c=>c.op==='multi_publish.prepare');assert.equal(preparations.length,2,f.v.getId('toast').textContent);assert.deepEqual(preparations[1].p.targets.map(t=>t.account_id),['b']);assert.equal(preparations[1].p.scheduled_times.v,start.value+'T13:00:00+08:00');
 assert.equal(f.calls.some(c=>/confirm|execution\.start/.test(c.op)),false);f.v.dispose();
});

test('a stale preview return cannot unlock an accepted batch or an unknown confirmation receipt',async()=>{
 for(const mode of ['accepted','unknown']){
  const pending=deferred(),f=await fixture(()=>pending.promise);await f.v.click('预览发布安排');
  const back=f.v.find(n=>n.tag==='button'&&n.textContent==='返回修改');await f.v.click('开始发布');
  if(mode==='accepted')pending.resolve({id:'current',quantity:1,state:'waiting',submitted:false});else pending.resolve(Promise.reject(Error('尚未取得确认回执')));
  await f.v.flush();await f.v.flush();back.onclick({isTrusted:true});await f.v.flush();
  assert.equal(f.trigger().disabled,true);assert.equal(f.field('start_date').disabled,true);
  assert.equal(f.calls.filter(c=>c.op==='multi_publish.confirm').length,1);assert.equal(f.calls.filter(c=>c.op==='multi_publish.prepare').length,1);assert.equal(f.calls.some(c=>c.op==='multi_publish.execution.start'),false);f.v.dispose();
 }
});

test('preview keeps its player and only one set of content fields with the original cover inherited',async()=>{
 const f=await fixture();await f.v.click('预览 / 编辑');
 const pane=f.v.find(n=>n.attrs['aria-label']==='视频预览与编辑');
 assert.equal(pane.querySelectorAll('video').length,1);
 assert.equal(pane.querySelectorAll('input,textarea,select').length,3);
 for(const [key,value]of [['标题','原标题'],['正文','原正文'],['标签','#自动剪辑']])assert.equal(f.v.find(n=>n.attrs['aria-label']==='本条'+key).value,value);
 for(const text of ['公共资料','抖音资料','恢复公共资料','恢复成品资料','编辑成品信息（同步成品库）','本次封面'])assert.throws(()=>f.v.find(n=>n.textContent===text));
 assert.throws(()=>f.v.find(n=>n.attrs['aria-label']==='本次发布封面'));
 await f.v.click('预览发布安排');const p=f.calls.find(c=>c.op==='multi_publish.prepare').p;
 assert.equal(p.fields_by_video.v?.cover,undefined);assert.equal(p.targets[0].fields_by_video.v?.cover,undefined);
 assert.equal(f.calls.some(c=>c.op==='finished.save'),false);f.v.dispose();
});

test('one preview draft applies to all targets without saving library metadata or choosing a cover',async()=>{
 const f=await fixture();await f.v.click('预览 / 编辑');
 for(const [key,value]of [['标题','本次标题'],['正文',''],['标签','#新标签']]){const input=f.v.find(n=>n.attrs['aria-label']==='本条'+key);input.value=value;input.oninput();}
 await f.v.click('预览发布安排');const p=f.calls.find(c=>c.op==='multi_publish.prepare').p;
 assert.deepEqual(p.fields_by_video.v,{title:'本次标题',body:'',tags:'#新标签'});
 assert.deepEqual(p.targets[0].fields_by_video,{});assert.equal(f.calls.some(c=>c.op==='finished.save'),false);
 assert.equal('cover'in p.fields_by_video.v,false);f.v.dispose();
});

test('a retained draft input cannot change a frozen preparation or mutate after leaving',async()=>{
 const f=await fixture();await f.v.click('预览 / 编辑');const input=f.v.find(n=>n.attrs['aria-label']==='本条标题');
 input.value='原本草稿';input.oninput();await f.v.click('预览发布安排');
 input.value='冻结时迟到输入';input.oninput();await f.v.click('返回修改');await f.v.click('预览发布安排');
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.prepare').at(-1).p.fields_by_video.v.title,'原本草稿');
 f.v.dispose();const saves=f.calls.filter(c=>c.op==='multi_publish.settings.save').length;input.value='离页迟到输入';input.oninput();await f.v.flush();
 assert.equal(f.calls.filter(c=>c.op==='multi_publish.settings.save').length,saves);assert.equal(f.calls.some(c=>c.op==='finished.save'),false);
});
