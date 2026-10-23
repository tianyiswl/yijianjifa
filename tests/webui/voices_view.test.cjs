const test=require('node:test'),assert=require('node:assert/strict');
const {mount}=require('./support/commerce_view.cjs');
const items=Array.from({length:25},(_,i)=>({id:`doubao:${i}`,name:`音色${i}`,gender:i%2?'男声':'女声',style:i%2?'视频配音':'通用场景',language:'中文',standard_sample:i===0,sample_source:i===0?'官方试听':null}));
function setup(calls,selected=''){let catalog={items,selected,favorites:[],configured:true};return async(op,p)=>{calls.push([op,p]);if(op==='voice.catalog')return catalog;if(op==='voice.settings.read')return {configured:true,app_id:'test'};if(op==='voice.selection.save'){catalog={...catalog,...p};return catalog;}if(op==='voice.standard')return {url:'p011media://asset/official'};if(op==='voice.audition.text')return {url:'p011media://asset/custom',duration_ms:2000};throw Error(op);};}
for(const selected of ['', 'doubao:1'])test(`favorites report the action without changing the ${selected?'cloud':'system'} voice default`,async()=>{
 const calls=[],v=await mount(setup(calls,selected),'voices');
 const favorite=()=>v.getId('voiceRows').children[0].children[3].children[0];
 try{
  await favorite().onclick({stopPropagation(){}});
  assert.match(v.getId('toast').textContent,/已收藏/);
  assert.doesNotMatch(v.getId('toast').textContent,/恢复系统配音|选择已保存/);
  assert.equal(favorite().textContent,'已收藏');
  assert.equal(v.getId('selectedName').textContent,selected?'音色1':'系统配音');
  assert.deepEqual(calls.filter(([op])=>op==='voice.selection.save').map(([,p])=>p),[{selected,favorites:['doubao:0']}]);
  await favorite().onclick({stopPropagation(){}});
  assert.match(v.getId('toast').textContent,/取消收藏/);
  assert.equal(favorite().textContent,'收藏');
  assert.equal(v.getId('selectedName').textContent,selected?'音色1':'系统配音');
  assert.deepEqual(calls.filter(([op])=>op==='voice.selection.save').map(([,p])=>p),[{selected,favorites:['doubao:0']},{selected,favorites:[]}]);
  assert.ok(!calls.some(([op])=>op==='voice.cloud.mode.save'||op==='voice.audition.text'));
 }finally{v.dispose();}
});
test('voice library pages and filters without generating cloud speech',async()=>{const calls=[];const v=await mount(setup(calls),'voices');assert.equal(v.getId('voiceRows').children.length,20);v.getId('nextPage').onclick();assert.equal(v.getId('voiceRows').children.length,5);v.getId('voiceSearch').value='音色24';v.getId('voiceSearch').oninput();assert.equal(v.getId('voiceRows').children.length,1);assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();});
// A detached table can clamp its overflow container to zero during a redraw.
// This models that layout consequence; the native Qt probe covers real layout.
function scrolledList(v){
 const scroll=v.query('.voice-scroll'),rows=v.getId('voiceRows'),replace=rows.replaceChildren.bind(rows);
 scroll.scrollTop=420;scroll.scrollLeft=24;
 rows.replaceChildren=(...nodes)=>{replace(...nodes);scroll.scrollTop=0;scroll.scrollLeft=0;};
 return scroll;
}
for(const action of ['select','favorite','sample'])test(`${action} keeps the voice list at the user's browsing position`,async()=>{
 const calls=[],base=setup(calls),v=await mount(async(op,p)=>op==='voice.catalog'?{...await base(op,p),items:items.map(item=>({...item,standard_sample:true}))}:base(op,p),'voices');
 try{
  const scroll=scrolledList(v),row=v.getId('voiceRows').children[15];
  if(action==='select')row.onclick();
  else await row.children[action==='favorite'?3:2].children[0].onclick({stopPropagation(){}});
  await v.flush();
  assert.equal(scroll.scrollTop,420);
  assert.equal(scroll.scrollLeft,24);
  if(action==='select'||action==='sample')assert.equal(v.getId('activeName').textContent,'音色15');
  if(action==='favorite')assert.equal(v.getId('voiceRows').children[15].children[3].children[0].textContent,'已收藏');
  assert.ok(!calls.some(([op])=>op==='voice.audition.text'));
 }finally{v.dispose();}
});
test('a delayed favorite response preserves the latest position rather than the click-time position',async()=>{
 const base=setup([]);let release;
 const v=await mount(async(op,p)=>{if(op==='voice.selection.save')await new Promise(resolve=>release=resolve);return base(op,p);},'voices');
 try{
  const scroll=scrolledList(v),pending=v.getId('voiceRows').children[15].children[3].children[0].onclick({stopPropagation(){}});
  scroll.scrollTop=610;release();await pending;
  assert.equal(scroll.scrollTop,610);
 }finally{v.dispose();}
});
test('filtering and explicit pagination start the new voice results at the top',async()=>{
 const v=await mount(setup([]),'voices');
 try{
  const scroll=v.query('.voice-scroll');scroll.scrollTop=420;v.getId('nextPage').onclick();assert.equal(scroll.scrollTop,0);
  scroll.scrollTop=150;v.getId('prevPage').onclick();assert.equal(scroll.scrollTop,0);
  scroll.scrollTop=420;v.getId('voiceSearch').value='音色1';v.getId('voiceSearch').oninput();assert.equal(scroll.scrollTop,0);
  scroll.scrollTop=200;v.getId('favoriteOnly').checked=true;v.getId('favoriteOnly').onchange();assert.equal(scroll.scrollTop,0);
 }finally{v.dispose();}
});
test('switching to text audition focuses its input without scrolling the voice library',async()=>{
 const v=await mount(setup([]),'voices');
 try{
  let focusOptions;v.getId('auditionText').focus=options=>focusOptions=options;
  v.getId('voiceRows').children[15].children[2].children[0].onclick({stopPropagation(){}});
  assert.equal(focusOptions?.preventScroll,true);
 }finally{v.dispose();}
});
test('a redraw restores focus to the same voice action without scrolling',async()=>{
 const v=await mount(setup([]),'voices');
 try{
  const doc=v.runtime.document,create=doc.createElement;
  doc.createElement=tag=>{const node=create(tag);node.focus=options=>{assert.equal(options.preventScroll,true);doc.activeElement=node;};return node;};
  const before=v.getId('voiceRows').children[15].children[3].children[0];doc.activeElement=before;
  await before.onclick({stopPropagation(){}});
  const after=v.getId('voiceRows').children[15].children[3].children[0];
  assert.equal(doc.activeElement===after,true,'focus remains on the same voice favorite action');
  assert.equal(after.textContent,'已收藏');
 }finally{v.dispose();}
});
test('fixed official sample never invokes synthesis',async()=>{const calls=[];const v=await mount(setup(calls),'voices');await v.getId('standardAudition').onclick();await v.flush();assert.ok(calls.some(([op])=>op==='voice.standard'));assert.ok(!calls.some(([op])=>op==='voice.audition.text'));assert.equal(v.getId('standardPlayer').children[0].src,'p011media://asset/official');v.dispose();});
test('typed text uses viewed voice without requiring library copy or changing default',async()=>{const calls=[];const v=await mount(setup(calls),'voices');v.getId('voiceRows').children[1].onclick();v.getId('auditionText').value='自己的文字';v.getId('voiceSpeed').value='1.2';v.getId('auditionText').oninput();await v.getId('customAudition').onclick();assert.deepEqual(calls.find(([op])=>op==='voice.audition.text')[1],{text:'自己的文字',voice:'doubao:1',speed:1.2});assert.ok(!calls.some(([op])=>op==='library.list'||op==='voice.selection.save'));assert.equal(v.getId('customPlayer').children[1].src,'p011media://asset/custom');v.dispose();});
test('empty text and invalid speed cannot invoke paid generation',async()=>{const calls=[];const v=await mount(setup(calls),'voices');v.getId('voiceSpeed').value='1';await v.getId('customAudition').onclick();v.getId('auditionText').value='文字';v.getId('voiceSpeed').value='3';await v.getId('customAudition').onclick();assert.ok(!calls.some(([op])=>op==='voice.audition.text'));v.dispose();});
test('failed audition replaces previous success and audio with current error',async()=>{
 const base=setup([]);let attempts=0;
 const v=await mount(async(op,p)=>{if(op==='voice.audition.text'&&++attempts===2)throw Error('配音失败，本段未扣字数。');return base(op,p);},'voices');
 v.getId('auditionText').value='成功文字';v.getId('voiceSpeed').value='1';
 await v.getId('customAudition').onclick();
 assert.equal(v.getId('customPlayer').children[1].tag,'audio');
 v.getId('auditionText').value='失败文字';await v.getId('customAudition').onclick();
 assert.equal(v.getId('customPlayer').children.length,1);
 assert.match(v.getId('customPlayer').children[0].textContent,/未扣字数/);v.dispose();
});
for(const custom of [false,true])test(`a ${custom?'generated':'sample'} decode failure is not reported as an autoplay prompt`,async()=>{
 const calls=[],v=await mount(setup(calls),'voices'),create=v.runtime.document.createElement;
 v.runtime.document.createElement=tag=>{const n=create(tag);if(tag==='audio')n.play=()=>Promise.reject(Object.assign(Error('unsupported media'),{name:'NotSupportedError'}));return n;};
 if(custom){v.getId('auditionText').value='试听文字';v.getId('voiceSpeed').value='1';await v.getId('customAudition').onclick();}else await v.getId('standardAudition').onclick();
 assert.match(v.getId('toast').textContent,/音频播放失败/);
 assert.doesNotMatch(v.getId('toast').textContent,/示例已加载|文字试听已生成/);
 assert.equal(calls.filter(([op])=>op==='voice.audition.text').length,custom?1:0);v.dispose();
});
test('autoplay permission rejection keeps the manual play hint',async()=>{
 const v=await mount(setup([]),'voices'),create=v.runtime.document.createElement;
 v.runtime.document.createElement=tag=>{const n=create(tag);if(tag==='audio')n.play=()=>Promise.reject(Object.assign(Error('gesture needed'),{name:'NotAllowedError'}));return n;};
 await v.getId('standardAudition').onclick();assert.match(v.getId('toast').textContent,/点击.*播放/);v.dispose();
});
test('late media errors are shown only while that player is still current',async()=>{
 const v=await mount(setup([]),'voices');await v.getId('standardAudition').onclick();
 const audio=v.getId('standardPlayer').children[0];audio.onerror();assert.match(v.getId('toast').textContent,/音频播放失败/);
 v.getId('voiceRows').children[1].onclick();v.getId('toast').textContent='new voice';audio.onerror();assert.equal(v.getId('toast').textContent,'new voice');v.dispose();audio.onerror();assert.equal(v.getId('toast').textContent,'new voice');
});

test('unavailable voices explain availability before users can choose or generate',async()=>{
 const base=setup([]);
 const v=await mount(async(op,p)=>{
  if(op==='voice.catalog')return {selected:'',favorites:[],configured:true,mode:'hosted',items:[{...items[0],available:false},{...items[1],available:true}]};
  if(op==='voice.cloud.read')return {enabled:true,authenticated:true,mode:'hosted',available_words:100};
  return base(op,p);
 },'voices');
 assert.match(v.getId('voiceAvailability').textContent,/暂不可用/);
 assert.equal(v.getId('useVoice').disabled,true);
 assert.equal(v.getId('customAudition').disabled,true);
 assert.ok(v.getId('voiceRows').children[0].children[0].children.some(n=>n.textContent==='暂不可用'));
 v.getId('voiceRows').children[1].onclick();
 assert.match(v.getId('voiceAvailability').textContent,/已开通/);
 assert.equal(v.getId('useVoice').disabled,false);
 v.dispose();
});
