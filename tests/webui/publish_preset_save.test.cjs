const test=require('node:test'),assert=require('node:assert/strict');
const {mount,baseData}=require('./support/commerce_view.cjs');
for(const kind of ['normal_publish','commerce'])test(kind+' saves reusable settings without video choices, dates or transient references',async()=>{
 const saved=[];
 const view=await mount((op,p)=>{if(op==='account.list')return {items:[{id:'a',nickname:'A'}],total:1};if(op==='publish.candidates')return {...baseData(op),default_account_id:'a'};if(op==='preset.get')return {id:'saved',name:saved.at(-1).payload.name,payload:saved.at(-1).payload};if(op==='preset.save'){saved.push(p);return {id:'saved',name:p.payload.name};}return baseData(op);},kind==='commerce'?'commerce':'publish');
 await view.click('定时 · 按间隔');await view.click(kind==='commerce'?'保存为常用方案':'保存当前设置为方案');view.find(n=>n.attrs['aria-label']==='发布方案名称').value='复用排期';if(kind==='commerce')view.find(n=>n.attrs['aria-label']==='方案首次延后小时').value='5';await view.click(kind==='commerce'?'另存新方案':'保存方案');assert.equal(saved.length,1);assert.equal(saved[0].id,null);assert.equal(saved[0].payload.kind,kind);assert.equal(saved[0].payload.schedule_rule.mode,'interval');assert.equal(saved[0].payload.schedule_rule.first_delay_hours,kind==='commerce'?5:3);if(kind==='normal_publish')assert.equal(saved[0].payload.keep_start_time,true);assert.equal('start' in saved[0].payload.schedule_rule,false);assert.equal('video_ids' in saved[0].payload,false);assert.equal('cover_overrides' in saved[0].payload,false);
 await view.click(kind==='commerce'?'保存为常用方案':'保存当前设置为方案');view.find(n=>n.attrs['aria-label']==='发布方案名称').value='修改后的规则';await view.click(kind==='commerce'?'更新当前方案':'覆盖保存“复用排期”');assert.equal(saved[1].id,'saved');view.dispose();
});
test('reopening a saved rule retains its chosen delay and cover mode',async()=>{
 const view=await mount((op,p)=>{if(op==='preset.save')return {id:'p',name:p.payload.name,payload:p.payload};return baseData(op);});
 await view.click('保存为常用方案');view.find(n=>n.attrs['aria-label']==='发布方案名称').value='规则';view.find(n=>n.attrs['aria-label']==='方案首次延后小时').value='7';view.find(n=>n.attrs['aria-label']==='方案封面规则').value='uploaded_image';await view.click('另存新方案');await view.click('保存为常用方案');assert.equal(view.find(n=>n.attrs['aria-label']==='方案首次延后小时').value,'7');assert.equal(view.find(n=>n.attrs['aria-label']==='方案封面规则').value,'uploaded_image');view.dispose();
});
