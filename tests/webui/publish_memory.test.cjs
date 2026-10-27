const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
const visible=node=>{for(let n=node;n;n=n._parent)if(n.hidden||n.style?.display==='none'||n.classList.contains('hide'))return false;return true;};
for(const viewName of ['publish','commerce'])test(viewName+' restores edited visible schedule in a new view without preparing or submitting tasks',async()=>{
 let settings=null;const ops=[];const handler=(op,p)=>{ops.push(op);if(op==='settings.publish.read')return {settings};if(op==='settings.publish.save'){settings=structuredClone(p.settings);return {saved:true};}if(op==='preset.list')return {items:[],total:0};if(op==='account.list')return {items:[],total:0};return baseData(op);};
 let v=await mount(handler,viewName);const field=(v,normal,commerce)=>v.find(n=>n.attrs['aria-label']===(viewName==='publish'?normal:commerce)&&visible(n));
 const hours=field(v,'两条间隔','间隔（小时）');hours.value='4';hours.oninput({isTrusted:true,target:hours});await v.flush();const days=field(v,'发布天数','连续天数');days.value='7';days.oninput({isTrusted:true,target:days});await v.flush();v.dispose();
 v=await mount(handler,viewName);assert.equal(field(v,'两条间隔','间隔（小时）').value,'4');assert.equal(field(v,'发布天数','连续天数').value,'7');
 if(viewName==='publish')assert.equal(field(v,'开始日期').value,field(v,'开始日期').min);
 for(const op of ['publish.prepare','publish.confirm','publish.execution.start','commerce.prepare','commerce.confirm','commerce.execution.start'])assert.equal(ops.includes(op),false,op);v.dispose();
});
test('ordinary memory keeps visible clock and quantity but discards hidden legacy scheduling modes',async()=>{
 let settings={mode:'immediate',start:'2000-01-01T09:15',interval:'2.5',per_day:'2',days:'3',clock_mode:'custom',date:'2000-01-01',times:['08:00','22:00']};const ops=[];
 const handler=(op,p)=>{ops.push(op);if(op==='settings.publish.read')return {settings};if(op==='settings.publish.save'){settings=structuredClone(p.settings);return {saved:true};}if(op==='account.list')return {items:[],total:0};return baseData(op);};
 const v=await mount(handler,'publish'),field=label=>v.find(n=>n.attrs['aria-label']===label&&visible(n));
 assert.equal(field('开始日期').value,field('开始日期').min);assert.equal(field('首条时间：小时').value,'09');assert.equal(field('首条时间：分钟').value,'15');assert.equal(field('每天发布条数').value,'2');assert.equal(field('发布天数').value,'3');
 const interval=field('两条间隔');interval.value='4';interval.oninput({isTrusted:true,target:interval});await v.flush();
 assert.equal(settings.mode,'daily');assert.equal(settings.clock_mode,'interval');assert.equal(settings.interval,'4');assert.equal(settings.times.some(Boolean),false);assert.equal(ops.some(op=>/prepare|confirm|execution\.start/.test(op)),false);v.dispose();
});
test('ordinary publish lists saved presets directly',async()=>{const v=await mount(op=>{if(op==='preset.list')return {items:[{id:'p',name:'每天三条',state:'ready'}],total:1};if(op==='account.list')return {items:[],total:0};return baseData(op);},'publish');const picker=v.find(n=>n.attrs['aria-label']==='选择常用发布方案');assert.ok(picker.children.some(n=>n.value==='p'&&n.textContent==='每天三条'));v.dispose();});
