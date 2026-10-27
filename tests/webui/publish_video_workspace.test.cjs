const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('publish library filters categories, edits shared video metadata and shows plan in a dialog',async()=>{
 let rows=[{id:'v',display_name:'视频.mp4',name:'视频',format:'MP4',title:'标题',body:'正文',tags:'',duration_ms:3000,note:'备注',category_ids:['c'],category_names:['产品']},{id:'v2',display_name:'其他',title:'',body:'',tags:'',duration_ms:1000,note:'',category_ids:[],category_names:[]}];const writes=[];
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {items:rows,total:rows.length,default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
  if(op==='media.thumbnail')return {image:'test-image'};
  if(op==='media.preview')return {url:'test-video'};
  if(op==='finished.save'){writes.push([op,p]);rows[0]={...rows[0],...p};return rows[0];}
  if(op==='video.note.save'){writes.push([op,p]);rows[0].note=p.note;return p;}
  if(op==='publish.prepare')return {...baseData('commerce.prepare'),token:'ready'};
  throw Error(op);
 },'publish');
 const filter=view.find(n=>n.attrs['aria-label']==='筛选发布视频分类');filter.value='c';filter.onchange();
 await view.click('全选','视频全选');assert.equal(view.find(n=>n.attrs['aria-label']==='选择 视频.mp4').checked,true);
 await view.click('预览 / 编辑');const title=view.find(n=>n.attrs['aria-label']==='发布视频发布标题');title.value='新标题';title.oninput();await view.click('保存视频信息');assert.equal(writes.length,1);assert.equal(writes[0][1].title,'新标题');assert.equal(writes[0][1].display_name,'视频.mp4');assert.equal(rows[0].note,'备注');
 await view.click('立即发布');await view.click('预览发布安排');assert.equal(view.ids.get('overlay').hidden,false);assert.ok(view.find(n=>n.className==='dialog publish-plan-dialog'));assert.ok(view.find(n=>n.textContent==='地点：不添加'));await view.click('返回修改');assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
