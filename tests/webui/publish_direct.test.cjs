const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
async function fixture(prepare,confirm){const view=await mount((op,p)=>{
 if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
 if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
 if(op==='publish.prepare')return prepare(p);
 if(op==='publish.confirm')return confirm(p);
 if(op==='media.thumbnail')return {image:''};throw Error(op);
},'publish');const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();return view;}
test('direct save checks and saves without requiring preview or another dialog',async()=>{
 const calls=[];const view=await fixture(p=>{calls.push('check');return {...baseData('commerce.prepare'),token:'valid'};},p=>{calls.push(['save',p.token]);return {id:'task',quantity:1};});
 await view.click('开始发布');assert.deepEqual(calls,['check',['save','valid']]);assert.equal(view.ids.get('overlay').hidden,true);assert.match(view.ids.get('toast').textContent,/尚未发布/);view.dispose();
});
test('blocked direct check opens reasons and never saves',async()=>{
 let saved=0;const view=await fixture(()=>{const r=baseData('commerce.prepare');r.items[0].blockers=['account_needs_verification'];return r;},()=>{saved++;});
 await view.click('开始发布');assert.equal(saved,0);assert.equal(view.ids.get('overlay').hidden,false);assert.ok(view.find(n=>n.textContent==='检查结果：账号需要登录或检查'));view.dispose();
});
test('double click during automatic check makes only one task',async()=>{
 let finish,checks=0,saves=0;const view=await fixture(()=>{checks++;return new Promise(r=>finish=r);},()=>{saves++;return {id:'task',quantity:1};});
 const button=view.find(n=>n.textContent==='开始发布');button.onclick({isTrusted:true});button.onclick({isTrusted:true});await view.flush();assert.equal(checks,1);finish({...baseData('commerce.prepare'),token:'ok'});await view.flush();assert.equal(saves,1);view.dispose();
});
async function scheduledFixture(check){const calls=[];const view=await mount((op,p)=>{
 if(op==='publish.execution.availability')return {available:true};
 if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};
 if(op==='account.list')return {items:[{id:'a',nickname:'账号'}],total:1};
 if(op==='publish.prepare'){calls.push('prepare');return {...baseData('commerce.prepare'),token:'ok'};}
 if(op==='publish.confirm'){calls.push('confirm');return {id:'task',quantity:1};}
 if(op==='publish.execution.check'){calls.push('check');return check;}
 if(op==='publish.execution.start'){calls.push(['start',p]);return {id:'task',state:'running'};}
 if(op==='media.thumbnail')return {image:''};throw Error(op);
},'publish');const cb=view.find(n=>n.attrs['aria-label']==='选择 测试视频');cb.checked=true;cb.onchange();return {view,calls};}
test('scheduled direct action freezes then checks and explicitly starts once',async()=>{
 const {view,calls}=await scheduledFixture({available:true,digest:'frozen'});
 await view.click('开始发布');assert.deepEqual(calls,['prepare','confirm','check',['start',{id:'task',digest:'frozen',consent:true}]]);assert.equal(view.ids.get('overlay').hidden,true);assert.equal(view.find(n=>n.id==='publish-batch-progress').dataset.taskId,'task');view.dispose();
});
test('unsupported scheduled combination stays saved without transport',async()=>{
 const {view,calls}=await scheduledFixture({available:false,reason:'当前暂不支持地点'});
 await view.click('开始发布');assert.deepEqual(calls,['prepare','confirm','check']);assert.match(view.ids.get('toast').textContent,/当前暂不支持地点/);assert.equal(view.ids.get('overlay').hidden,false);assert.ok(view.find(n=>n.attrs.role==='alertdialog'));assert.ok(view.find(n=>n.textContent==='原因：当前暂不支持地点'));await view.click('知道了');assert.equal(view.ids.get('overlay').hidden,true);view.dispose();
});
test('optional preview can submit through the same execution checks',async()=>{
 const {view,calls}=await scheduledFixture({available:true,digest:'frozen'});
 await view.click('预览发布安排');assert.deepEqual(calls,['prepare']);
 const button=view.ids.get('overlay').children[0].children[2]?.children?.find(n=>n.textContent==='开始发布');
 // Use the visible dialog button rather than the page footer.
 function walk(n){if(n.textContent==='开始发布'&&typeof n.onclick==='function')return n;for(const c of n.children||[]){const found=walk(c);if(found)return found;}}
 const submit=walk(view.ids.get('overlay'));assert.ok(submit);submit.onclick({isTrusted:true});await view.flush();
 assert.deepEqual(calls,['prepare','confirm','check',['start',{id:'task',digest:'frozen',consent:true}]]);view.dispose();
});
