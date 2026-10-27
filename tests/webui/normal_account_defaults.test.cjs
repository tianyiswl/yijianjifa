const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
test('ordinary publishing keeps the default account row simple and sends no empty override over account defaults',async()=>{
 const requests=[],routes=[];const v=await mount((op,p)=>{
  if(op==='account.list')return {items:[{id:'a',nickname:'测试号',platform:'douyin'}],total:1};
  if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
  if(op==='publish.prepare'){requests.push(p);return baseData('commerce.prepare');}
  return baseData(op);
 },'publish',null,{P011Router:{go:route=>routes.push(route)}});
 const section=v.find(n=>n.className==='simple-account-section');
 assert.equal(section.querySelectorAll('button').some(b=>['当前账号内容声明','当前账号发布地点'].includes(b.textContent)),false);
 assert.match(section.children.map(n=>n.textContent).join(' '),/账号管理/);
 assert.equal(section.querySelectorAll('button').length,0);assert.deepEqual(routes,[]);
 const check=v.find(n=>n.attrs['aria-label']==='选择 测试视频');check.checked=true;check.onchange();
 await v.click('预览发布安排');assert.equal(requests.length,1);
 assert.deepEqual(requests[0].declarations,{});assert.deepEqual(requests[0].locations,{});
 v.dispose();
});
