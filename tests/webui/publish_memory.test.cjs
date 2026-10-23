const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
for(const viewName of ['publish','commerce'])test(viewName+' restores edited schedule in a new view without selecting or submitting videos',async()=>{
 let settings=null;const ops=[];const handler=(op,p)=>{ops.push(op);if(op==='settings.publish.read')return {settings};if(op==='settings.publish.save'){settings=structuredClone(p.settings);return {saved:true};}if(op==='preset.list')return {items:[],total:0};if(op==='account.list')return {items:[],total:0};return baseData(op);};
 let v=await mount(handler,viewName);const field=(v,id,label)=>viewName==='publish'?v.find(n=>n.id===id):v.find(n=>n.attrs['aria-label']===label);
 const hours=field(v,'publish-interval','间隔（小时）');hours.value='4';hours.oninput();await v.flush();const days=field(v,'publish-days','连续天数');days.value='7';days.oninput();await v.flush();v.dispose();
 v=await mount(handler,viewName);assert.equal(field(v,'publish-interval','间隔（小时）').value,'4');assert.equal(field(v,'publish-days','连续天数').value,'7');assert.equal(ops.includes('publish.confirm'),false);assert.equal(ops.includes('publish.execution.start'),false);v.dispose();
});
test('ordinary publish lists saved presets directly',async()=>{const v=await mount(op=>{if(op==='preset.list')return {items:[{id:'p',name:'每天三条',state:'ready'}],total:1};if(op==='account.list')return {items:[],total:0};return baseData(op);},'publish');const picker=v.find(n=>n.attrs['aria-label']==='选择常用发布方案');assert.ok(picker.children.some(n=>n.value==='p'&&n.textContent==='每天三条'));v.dispose();});
