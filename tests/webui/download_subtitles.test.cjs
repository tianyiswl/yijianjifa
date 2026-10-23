const test=require('node:test'),assert=require('node:assert/strict');
const {SubtitleEditor,patchBatches}=require('../../src/yijianjifa/webui/assets/download_subtitles.js');
const {WriteIntent}=require('../../src/yijianjifa/webui/assets/write_intent.js');
const rows=count=>Array.from({length:count},(_,i)=>({id:'s'+i,start_ms:i*1000,end_ms:i*1000+800,text:'original'+i,confidence:i===0?.3:.99}));
function editor(send){let id=0;return new SubtitleEditor({send,makeId:()=>String(++id).padStart(32,'0'),WriteIntent});}
test('subtitle conflict keeps dirty edits and reports only acknowledged saves',async()=>{
 let revision=0,writes=0;const e=editor(async(op,p)=>{
  if(op==='download.subtitle.read')return {revision:0,segments:rows(5),next_cursor:null};
  if(op==='download.subtitle.save'){if(++writes===2)throw Error('版本冲突');assert.equal(p.expected_revision,revision);return {revision:++revision,saved_count:p.patches.length};}
 });await e.load('original','asr');for(let i=0;i<5;i++)e.edit('s'+i,{text:'中'.repeat(2500)+i});
 await assert.rejects(e.save(),error=>{assert.equal(error.savedCount,1);assert.equal(error.remainingCount,4);return true;});
 assert.equal(e.revision,1);assert.equal(e.dirty.size,4);assert.ok(e.hasUnsaved());
});
test('long originals are loaded serially across safe pages',async()=>{
 const calls=[];const e=editor(async(op,p)=>{calls.push(p.cursor);return p.cursor?{revision:0,segments:rows(4).slice(2),next_cursor:null}:{revision:0,segments:rows(4).slice(0,2),next_cursor:'page2'};});
 await e.load('r','ocr');assert.deepEqual(calls,[null,'page2']);assert.equal(e.segments.length,4);assert.equal(e.segments.at(-1).id,'s3');
});
test('batches obey both UTF8 params and full bridge byte budgets',()=>{
 const patches=rows(12).map(row=>({id:row.id,text:'字'.repeat(1800)}));
 const batches=patchBatches('r','asr',patches,7);
 assert.ok(batches.length>1);assert.equal(batches.flat().length,12);
 batches.forEach(batch=>{const params={resource_id:'r',origin:'asr',expected_revision:7,patches:batch};assert.ok(Buffer.byteLength(JSON.stringify(params))<=12288);assert.ok(Buffer.byteLength(JSON.stringify({v:1,request_id:'x'.repeat(64),operation:'download.subtitle.save',params}))<=16384);});
 assert.throws(()=>patchBatches('r','asr',[{id:'s',text:'中'.repeat(6000)}],0));
});
test('lost response retries the exact receipt before newer changes',async()=>{
 const receipts=new Map(),ids=[];let lost=true;const e=editor(async(op,p,id)=>{
  if(op==='download.subtitle.read')return {revision:0,segments:rows(1),next_cursor:null};
  ids.push(id);if(receipts.has(id))return receipts.get(id);const response={revision:1,saved_count:1};receipts.set(id,response);if(lost){lost=false;throw Error('连接中断');}return response;
 });await e.load('r','asr');e.edit('s0',{text:'first'});await assert.rejects(e.save());
 assert.equal(e.dirty.size,1);await e.save();assert.equal(ids[0],ids[1]);assert.equal(receipts.size,1);assert.equal(e.dirty.size,0);assert.equal(e.revision,1);
});
test('a pending receipt remains immutable when text changes after response loss',async()=>{
 let fail=true;const sent=[];const e=editor(async(op,p,id)=>{
  if(op==='download.subtitle.read')return {revision:0,segments:rows(1),next_cursor:null};
  sent.push({id,params:structuredClone(p)});if(fail){fail=false;throw Error('lost');}return {revision:sent.length===2?1:2,saved_count:1};
 });await e.load('r','asr');e.edit('s0',{text:'old'});await assert.rejects(e.save());e.edit('s0',{text:'new'});await e.save();
 assert.equal(sent[0].id,sent[1].id);assert.deepEqual(sent[0].params,sent[1].params);assert.equal(sent[2].params.expected_revision,1);assert.equal(sent[2].params.patches[0].text,'new');assert.equal(e.dirty.size,0);
});
test('unsaved source switch requires an explicit discard and time seek is milliseconds',async()=>{
 const e=editor(async()=>({revision:0,segments:rows(2),next_cursor:null}));await e.load('one','asr');e.edit('s0',{text:'dirty'});
 await assert.rejects(e.load('two','ocr'));assert.equal(e.resourceId,'one');e.discard();await e.load('two','ocr');assert.equal(e.resourceId,'two');
 const player={currentTime:0};e.seek('s1',player);assert.equal(player.currentTime,1);assert.equal(e.segments[0].confidence,.3);
});
test('invalid timing and unknown segments never enter a write intent',async()=>{
 let writes=0;const e=editor(async(op)=>{if(op==='download.subtitle.save')writes++;return {revision:0,segments:rows(2),next_cursor:null};});await e.load('r','asr');
 assert.throws(()=>e.edit('missing',{text:'bad'}));assert.throws(()=>e.edit('s0',{start_ms:-1}));assert.throws(()=>e.edit('s0',{end_ms:1600}));assert.equal(writes,0);assert.equal(e.dirty.size,0);
});
test('read version changes never produce a mixed original',async()=>{
 const e=editor(async(op,p)=>p.cursor?{revision:2,segments:rows(2).slice(1),next_cursor:null}:{revision:1,segments:rows(2).slice(0,1),next_cursor:'next'});
 await assert.rejects(e.load('r','asr'));assert.equal(e.segments.length,0);
});
test('malformed save acknowledgment retains the original receipt identity',async()=>{
 let wrong=true;const ids=[];const e=editor(async(op,p,id)=>{if(op==='download.subtitle.read')return {revision:0,segments:rows(1),next_cursor:null};ids.push(id);if(wrong){wrong=false;return {revision:1};}return {revision:1,saved_count:1};});await e.load('r','asr');e.edit('s0',{text:'changed'});await assert.rejects(e.save());await e.save();assert.equal(ids[0],ids[1]);assert.equal(e.dirty.size,0);
});
test('a definitive conflict keeps edits but allows explicit discard',async()=>{
 const e=editor(async(op)=>{if(op==='download.subtitle.read')return {revision:0,segments:rows(1),next_cursor:null};const error=Error('版本冲突');error.code='subtitle_conflict';throw error;});await e.load('r','asr');e.edit('s0',{text:'kept'});await assert.rejects(e.save());assert.equal(e.dirty.size,1);assert.equal(e.pending,null);e.discard();assert.equal(e.dirty.size,0);
});
test('small patches are still split at the finite 200-patch limit',()=>{
 const batches=patchBatches('r','asr',rows(250).map(row=>({id:row.id,text:'x'})),0);assert.equal(batches.length,2);assert.deepEqual(batches.map(batch=>batch.length),[200,50]);
});
test('reverting to original text after response loss remains a newer edit',async()=>{
 let fail=true,revision=0;const receipts=new Map(),saved=[];const e=editor(async(op,p,id)=>{if(op==='download.subtitle.read')return {revision:0,segments:rows(1),next_cursor:null};
  if(receipts.has(id))return receipts.get(id);const result={revision:++revision,saved_count:p.patches.length};receipts.set(id,result);saved.push(p.patches[0].text);if(fail){fail=false;throw Error('lost response');}return result;
 });await e.load('r','asr');e.edit('s0',{text:'first'});await assert.rejects(e.save());e.edit('s0',{text:'original0'});assert.equal(e.current()[0].text,'original0');assert.ok(e.hasUnsaved());await e.save();assert.deepEqual(saved,['first','original0']);assert.equal(e.current()[0].text,'original0');assert.equal(e.hasUnsaved(),false);
});
