const test=require('node:test'),assert=require('node:assert/strict');
const {requestAfterSelectionChange}=require('./support/multi_publish_scope.cjs');
test('start projects draft fields onto selected videos and retains edits for re-selection',async()=>{
 const result=await requestAfterSelectionChange();
 assert.deepEqual(result.first.video_ids,['selected-video']);
 assert.deepEqual(result.first.fields_by_video,{});
 assert.deepEqual(result.first.targets[0].fields_by_video,{});
 assert.deepEqual(result.first.targets[0].fields,{content_declaration_choice:'none',channels_original:false});
 assert.equal(result.reselected.fields_by_video['old-video'].title,'保留旧视频公共草稿');
 assert.equal(result.reselected.targets[0].fields_by_video['old-video'].title,'保留旧视频平台草稿');
 assert.equal(result.operations.some(op=>/confirm|execution|upload|login|finished.save/.test(op)),false);
});
test('opening an unselected video form does not send its empty draft shell',async()=>{
 const result=await requestAfterSelectionChange({scenario:'video_shell'});
 assert.deepEqual(result.first.fields_by_video,{});
 assert.deepEqual(result.first.targets[0].fields_by_video,{});
 assert.deepEqual(result.reselected.fields_by_video['old-video'],{});
 assert.deepEqual(result.reselected.targets[0].fields_by_video['old-video'],{});
});
for(const scenario of ['platform_dropdown','platform_clear','platform_toggle'])test('start projects active platforms while settings retain platform defaults after '+scenario,async()=>{
 const result=await requestAfterSelectionChange({scenario});
 assert.deepEqual(result.first.targets.map(t=>[t.platform,t.account_id]),[['channels','channels-account']]);
 assert.deepEqual(result.first.schedule_by_platform,{});
 assert.deepEqual(result.first.distribution_by_platform,{});
 const saved=result.saved.find(s=>s.targets.length===1);
 assert.ok(saved);assert.deepEqual(saved.schedule_by_platform,{});assert.deepEqual(saved.distribution_by_platform,{});assert.equal(saved.fields_by_platform.bilibili.category_choice,'human:knowledge');
 assert.equal(result.reselected.targets.find(t=>t.platform==='bilibili').account_id,'bili-other-account');
 assert.equal(result.reselected.schedule_by_platform.bilibili.execution_mode,'platform_schedule');
 assert.equal(result.reselected.distribution_by_platform.bilibili,'whole_batch');
 assert.equal(result.reselected.targets.find(t=>t.platform==='bilibili').fields.category_choice,'human:knowledge');
 assert.equal(result.operations.some(op=>/confirm|execution|upload|login|finished.save/.test(op)),false);
});
