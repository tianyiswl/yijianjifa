const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('commerce item music can be cancelled, cleared, set and restored; refreshing revokes it',async()=>{
 const sent=[];
 const view=await mount((op,p)=>{
  if(op==='commerce.music.query')return {available:true,token:'music-token',items:[{id:'none',title:'收藏曲目'}]};
  if(op==='commerce.prepare'){sent.push(p);return baseData(op);}
  return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();
 await view.click('刷新当前账号收藏音乐');await view.click('检查发布安排');
 await view.click('修改本条音乐');await view.click('取消');assert.equal(sent.length,1);
 async function apply(value){await view.click('修改本条音乐');view.find(n=>n.attrs['aria-label']==='本条平台音乐').value=value;await view.click('应用本条音乐');}
 await apply('none');assert.deepEqual(sent.at(-1).music_overrides,{v:{mode:'none',ids:[],token:null}});
 await apply('track:none');assert.deepEqual(sent.at(-1).music_overrides,{v:{mode:'fixed',ids:['none'],token:'music-token'}});
 assert.equal(sent.at(-1).music_choice.mode,'none');
 await view.click('修改本条音乐');await view.click('恢复批量音乐');assert.deepEqual(sent.at(-1).music_overrides,{});
 await apply('track:none');await view.click('刷新当前账号收藏音乐');await view.click('检查发布安排');assert.deepEqual(sent.at(-1).music_overrides,{});
 view.dispose();
});
test('a single no-music override requires account-switch confirmation and is cleared only on approval',async()=>{
 const sent=[];
 const view=await mount((op,p)=>{
  if(op==='commerce.context'){const data=baseData(op);data.accounts.push({...data.accounts[0],id:'b',nickname:'账号B'});return data;}
  if(op==='commerce.prepare'){sent.push(p);return baseData(op);}
  return baseData(op);
 });
 const cb=view.find(n=>n.attrs['aria-label']==='选择团购视频 测试视频');cb.checked=true;cb.onchange();await view.click('检查发布安排');
 await view.click('修改本条音乐');view.find(n=>n.attrs['aria-label']==='本条平台音乐').value='none';await view.click('应用本条音乐');
 const account=view.find(n=>n.attrs['aria-label']==='团购发布账号');
 account.value='b';let switching=account.onchange();await view.flush();assert.equal(account.value,'a');assert.equal(view.ids.get('overlay').hidden,false);
 await view.click('保留当前账号');await switching;await view.click('检查发布安排');assert.ok(sent.at(-1).music_overrides.v);
 account.value='b';switching=account.onchange();await view.flush();await view.click('确认切换并清除');await switching;await view.click('检查发布安排');
 assert.equal(sent.at(-1).account_id,'b');assert.deepEqual(sent.at(-1).music_overrides,{});assert.deepEqual(sent.at(-1).plan.video_ids,['v']);view.dispose();
});
