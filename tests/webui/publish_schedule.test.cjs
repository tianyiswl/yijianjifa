const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
function visible(node){for(let n=node;n;n=n._parent)if(n.hidden||n.style?.display==='none'||n.classList.contains('hide'))return false;return true;}
function field(view,label){return view.find(n=>n.attrs['aria-label']===label&&visible(n));}
function edit(view,label,value){const input=field(view,label);input.value=String(value);(input.oninput||input.onchange)({isTrusted:true,target:input});}
function select(view,name){const input=field(view,'选择 '+name);input.checked=true;input.onchange();}
const video=(id,name)=>({id,display_name:name,title:name,body:'正文',tags:'',duration_ms:1000});
async function setup(items=[video('v','测试视频')]){
 const requests=[],ops=[];const view=await mount((op,p)=>{
  ops.push(op);
  if(op==='publish.candidates')return {items,total:items.length,default_account_id:'a'};
  if(op==='account.list')return {items:[{id:'a',platform:'douyin',nickname:'账号A'},{id:'b',platform:'douyin',nickname:'账号B'}],total:2};
  if(op==='publish.prepare'){requests.push(structuredClone(p));return {token:'prepared',issues:[],items:p.video_ids.map(id=>({video_id:id,account_id:p.account_ids[0],title:id,body:'',tags:'',time:p.start,blockers:[],required_choice:[]}))};}
  return baseData(op);
 },'publish');return {view,requests,ops};
}
test('ordinary publishing exposes the five daily fields and hides legacy modes and advanced controls',async()=>{
 const {view,requests,ops}=await setup();
 for(const label of ['开始日期','发布天数','每天发布条数','首条时间：小时','首条时间：分钟','两条间隔'])assert(field(view,label));
 for(const text of ['定时 · 按间隔','指定时间','立即发布','高级设置','排期说明'])assert.equal(visible(view.find(n=>n.textContent===text&&['button','summary'].includes(n.tag))),false,text);
 assert.equal(field(view,'开始日期').value,field(view,'开始日期').min);
 edit(view,'发布天数',1);edit(view,'每天发布条数',1);edit(view,'两条间隔',2.5);edit(view,'首条时间：小时','09');edit(view,'首条时间：分钟','15');select(view,'测试视频');
 await view.click('预览发布安排');assert.equal(requests.length,1);const request=requests[0];
 assert.equal(request.mode,'daily');assert.equal(request.interval_hours,2.5);assert.equal(request.daily_count,1);assert.equal(request.days,1);assert.deepEqual(request.video_ids,['v']);
 assert.equal(request.start,field(view,'开始日期').value+'T09:15:00+08:00');assert.equal(Object.hasOwn(request,'daily_times'),false);
 assert.equal(ops.some(op=>/confirm|execution\.start/.test(op)),false);view.dispose();
});
test('switching the one visible account keeps the daily rule and sends no hidden account exceptions',async()=>{
 const {view,requests}=await setup();edit(view,'发布天数',2);edit(view,'每天发布条数',1);edit(view,'首条时间：小时','10');edit(view,'首条时间：分钟','05');
 const account=field(view,'本次发布账号');account.value='b';account.oninput({isTrusted:true,target:account});select(view,'测试视频');await view.click('预览发布安排');
 assert.deepEqual(requests[0].account_ids,['b']);assert.equal(requests[0].distribution,'split_across_accounts');assert.equal(requests[0].days,2);assert.match(requests[0].start,/T10:05:00\+08:00$/);
 for(const key of ['account_schedules','item_schedules','moves','daily_times'])assert.equal(Object.hasOwn(requests[0],key),false,key);
 assert.equal(visible(view.find(n=>n.attrs['aria-label']==='视频分配方式')),false);view.dispose();
});
test('ordinary schedule selection respects the current video search',async()=>{
 const {view,requests}=await setup([video('hidden','其他视频'),video('match','目标视频')]);
 edit(view,'发布天数',1);edit(view,'每天发布条数',1);const search=field(view,'搜索可发布视频');search.value='目标';search.oninput();
 await view.click('按排期选择 1 条');await view.click('预览发布安排');assert.deepEqual(requests[0].video_ids,['match']);view.dispose();
});
test('invalid visible clock or quantity never prepares a task and a corrected rule can be previewed',async()=>{
 const {view,requests,ops}=await setup();select(view,'测试视频');edit(view,'发布天数',1);edit(view,'每天发布条数',1);
 edit(view,'首条时间：分钟','99');await view.click('预览发布安排');assert.equal(requests.length,0);assert.match(view.ids.get('toast').textContent,/时间/);
 edit(view,'首条时间：分钟','15');edit(view,'两条间隔',.25);await view.click('预览发布安排');assert.equal(requests.length,0);assert.match(view.ids.get('toast').textContent,/间隔/);
 edit(view,'两条间隔',3);await view.click('预览发布安排');assert.equal(requests.length,1);assert.match(requests[0].start,/:15:00\+08:00$/);
 assert.equal(ops.some(op=>/confirm|execution\.start/.test(op)),false);view.dispose();
});
test('a partial daily selection uses only the selected videos instead of requiring full capacity',async()=>{
 const {view,requests}=await setup();select(view,'测试视频');await view.click('预览发布安排');
 assert.equal(requests.length,1);assert.deepEqual(requests[0].video_ids,['v']);assert.equal(requests[0].daily_count,2);assert.equal(requests[0].days,5);view.dispose();
});
test('a manual video selection above daily capacity is blocked instead of silently dropping a video',async()=>{
 const {view,requests}=await setup([video('v1','第一条'),video('v2','第二条')]);edit(view,'发布天数',1);edit(view,'每天发布条数',1);select(view,'第一条');select(view,'第二条');
 await view.click('预览发布安排');assert.equal(requests.length,0);assert.match(view.ids.get('toast').textContent,/超过排期容量/);assert.equal(field(view,'选择 第一条').checked,true);assert.equal(field(view,'选择 第二条').checked,true);view.dispose();
});

for(const kind of ['publish','commerce'])test(kind+' automatically selects filtered videos as daily quantity or days changes',async()=>{
 const view=await mount((op,p)=>{
  if(op==='publish.candidates')return {default_account_id:'a',total:4,items:[0,1,2,3].map(i=>({id:'v'+i,display_name:i===3?'其他':'目标'+i,title:'视频',duration_ms:1000}))};
  if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
  return baseData(op);
 },kind);
 const daily=field(view,kind==='publish'?'每天发布条数':'每天数量'),days=field(view,kind==='publish'?'发布天数':'连续天数');
 const search=field(view,kind==='publish'?'搜索可发布视频':'搜索团购视频');search.value='目标';search.oninput();
 const checked=name=>field(view,(kind==='publish'?'选择 ':'选择团购视频 ')+name).checked;
 if(kind==='publish'){edit(view,'发布天数',1);edit(view,'每天发布条数',2);}else{days.value='1';daily.value='2';daily.oninput();}
 assert.equal(checked('目标0'),true);assert.equal(checked('目标1'),true);assert.equal(checked('目标2'),false);
 if(kind==='publish')edit(view,'发布天数',2);else{days.value='2';days.oninput();}
 assert.equal(checked('目标2'),true);assert.match(view.ids.get('toast').textContent,/只有.?3.?条/);
 if(kind==='publish'){edit(view,'发布天数',1);edit(view,'每天发布条数',1);}else{days.value='1';daily.value='1';daily.oninput();}
 assert.equal(checked('目标0'),true);assert.equal(checked('目标1'),false);
 daily.value='';daily.oninput({isTrusted:true,target:daily});assert.equal(checked('目标0'),true);view.dispose();
});
