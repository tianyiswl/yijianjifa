const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {mount,baseData}=require('./support/commerce_view.cjs');
async function fixture(overrides={}){
 let row={id:'v',display_name:'原视频.mp4',name:'原视频',format:'MP4',title:'原标题',body:'原文案',tags:'#产品',duration_ms:3000,note:'保留原备注',category_ids:[],category_names:[]};
 const calls=[],v=await mount((op,p)=>{calls.push({op,p:structuredClone(p)});if(overrides[op]){const value=overrides[op](p);if(value!==undefined)return value;}
  if(op==='publish.candidates')return {items:[row],total:1,default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'测试账号',platform:'douyin',login_state:'valid'}],total:1};
  if(op==='media.preview')return {url:'test-video'};
  if(op==='media.thumbnail')return {image:'test-image'};
  if(op==='finished.save'){row={...row,...p};return row;}
  return baseData(op);
 },'publish');await v.click('预览 / 编辑');return {v,calls,row:()=>row};
}
test('ordinary preview keeps the existing player and only the three publishing content fields',async()=>{
 const {v,calls}=await fixture();try{
  const pane=v.find(n=>n.className==='publish-video-detail'),workspace=v.find(n=>n.className==='publish-video-workspace');
  assert.deepEqual(workspace.children.map(n=>n.className),['publish-video-list']);
  const right=v.find(n=>n.className==='publish-inspection-column');
  assert.deepEqual(right.children.map(n=>n.className),['publish-video-detail','publish-progress-section']);
  assert.equal(right.children[0],pane);assert.equal(workspace.contains(pane),false);
  assert(pane.querySelector('video'));assert(pane.querySelectorAll('button').some(n=>n.textContent==='原画播放'));
  assert.deepEqual(pane.querySelectorAll('input,textarea').map(n=>n.attrs['aria-label']),['发布视频发布标题','发布视频发布文案','发布视频标签']);
  assert.equal(pane.querySelectorAll('select').length,0,'preview does not ask for a cover or another mode');
  assert.equal(calls.some(c=>/finished\.save|video\.note\.save|cover|publish\.(prepare|confirm)|execution\.start/.test(c.op)),false);
 }finally{v.dispose();}
});
test('saving simplified preview preserves the original video name and note without another note write',async()=>{
 const {v,calls,row}=await fixture();try{
  const title=v.find(n=>n.attrs['aria-label']==='发布视频发布标题');title.value='新标题';title.oninput();
  await v.click('保存视频信息');
  assert.deepEqual(calls.filter(c=>c.op==='finished.save').map(c=>c.p),[{id:'v',display_name:'原视频.mp4',title:'新标题',body:'原文案',tags:'#产品'}]);
  assert.equal(calls.some(c=>c.op==='video.note.save'),false);assert.equal(row().note,'保留原备注');
  assert.equal(calls.some(c=>/publish\.(prepare|confirm)|execution\.start/.test(c.op)),false);
 }finally{v.dispose();}
});
test('cancelled preview changes do not write and restore the existing publishing text',async()=>{
 const {v,calls}=await fixture();try{
  const body=v.find(n=>n.attrs['aria-label']==='发布视频发布文案');body.value='尚未保存';body.oninput();
  await v.click('取消修改');assert.equal(v.find(n=>n.attrs['aria-label']==='发布视频发布文案').value,'原文案');
  assert.equal(calls.some(c=>/finished\.save|video\.note\.save|publish\.(prepare|confirm)|execution\.start/.test(c.op)),false);
 }finally{v.dispose();}
});
test('a saved edit remains saved when refreshing the video list fails',async()=>{
 let reads=0;const {v,calls,row}=await fixture({'publish.candidates':()=>{if(++reads>1)throw Error('列表暂时无法读取');}});try{
  const title=v.find(n=>n.attrs['aria-label']==='发布视频发布标题');title.value='已经保存的新标题';title.oninput();
  await v.click('保存视频信息');
  assert.equal(row().title,'已经保存的新标题');assert.match(v.ids.get('toast').textContent,/已保存.*列表刷新/);assert.doesNotMatch(v.ids.get('toast').textContent,/未保存/);
  await v.click('取消修改');assert.equal(v.find(n=>n.attrs['aria-label']==='发布视频发布标题').value,'已经保存的新标题');
  assert.equal(calls.filter(c=>c.op==='finished.save').length,1);assert.equal(calls.some(c=>c.op==='video.note.save'),false);
 }finally{v.dispose();}
});
test('the video list uses its full column and preview shares the right column with execution',()=>{
 const css=fs.readFileSync(path.resolve(__dirname,'../../src/yijianjifa/webui/assets/common.css'),'utf8');
 const rule=(selector)=>[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(m=>m[1].split(',').map(s=>s.trim()).includes(selector)).map(m=>m[2]);
 assert(rule('body .publish-video-workspace').some(s=>s.includes('grid-template-columns:minmax(0,1fr) minmax(280px,30%)')));
 assert(rule('body.multi-publish-page .publish-video-workspace').some(s=>s.includes('minmax(270px,30%)')));
 assert(rule('body .publish-workspace .publish-video-workspace').some(s=>/display:block/.test(s)),'the list no longer reserves a second preview column');
 assert(rule('body .publish-workspace .publish-inspection-column').some(s=>/grid-area:execution/.test(s)&&/grid-template-rows:/.test(s)),'execution and preview are stacked in the existing right column');
 assert(rule('body .publish-workspace .publish-inspection-column>.publish-video-detail').some(s=>/overflow:auto/.test(s)),'preview and editing remain scrollable within the right column');
 assert.equal(rule('body .publish-workspace .publish-video-list').some(s=>/overflow:visible/.test(s)),false);
 assert.equal(rule('body .publish-workspace .publish-video-detail').some(s=>/max-height:320px/.test(s)),false);
});
