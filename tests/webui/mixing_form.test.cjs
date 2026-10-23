const {test}=require('node:test');const assert=require('node:assert/strict');
const {defaults,params,preview}=require('../../src/yijianjifa/webui/assets/mixing_form.js');
test('approved controls map to frozen API params, independent subtitle styles',()=>{
 const f={...defaults(),quantity:2,ratio:'16:9',quality:'720p',fill:'crop',order:'random',totalFixed:2.5,clipFixed:.5,fullCaption:true,override:'本批文字',speechPosition:'top',fullPosition:'middle',speechColor:'#ff8800',fullColor:'#123456'};
 const p=params(f,['V'],['C']);assert.equal(p.total_ms,2500);assert.equal(p.segment_ms,500);assert.equal(p.frame_fit,'crop_fill');assert.equal(p.copy_mode,'random');assert.equal(p.aspect_ratio,'16:9');assert.deepEqual(p.material_categories,['V']);assert.equal(p.batch_full_text,'本批文字');assert.equal(p.spoken_style.position,'top');assert.equal(p.full_style.position,'middle');assert.equal(p.spoken_style.color,'#ff8800');assert.equal(p.full_style.color,'#123456');assert.equal(p.auto_publish,undefined);
});
test('invalid numbers and unknown choices never silently become defaults',()=>{
 for(const patch of [{quantity:1.5},{totalFixed:NaN},{clipFixed:0},{speechSize:201},{fullChars:2},{ratio:'4:3'},{speechColor:'red'}]) assert.throws(()=>params({...defaults(),...patch},[],[]));
});
test('preview uses real copy text, keeps independent toggles, wraps Unicode and never falls back to title',()=>{
 const copy={title:'禁止补标题',voice:'一二三四五。下一句。',full:'甲乙丙丁戊己'};
 const f={...defaults(),speechChars:4,fullChars:4,fullCaption:true};
 assert.deepEqual(preview(f,copy),{spoken:'一二三',full:'甲乙丙丁\n戊己',source:'文案全程字幕'});
 assert.equal(preview({...f,fullCaption:false,override:'覆盖'},copy).full,'');
 assert.equal(preview(f,{title:'禁止补标题',voice:'',full:''}).full,'');
 assert.equal(preview({...f,override:'覆盖'},copy).full,'覆盖');assert.equal(copy.full,'甲乙丙丁戊己');
});
test('music and transition controls produce finite plan choices and independent gain',()=>{
 const f={...defaults(),musicMode:'specific',musicId:'track-1',musicVolume:35,transition:'specific',effect:'向左滑动',transitionSeconds:.3};
 const p=params(f,[],[]);assert.deepEqual(p.background_music,{mode:'fixed',music_id:'track-1'});assert.deepEqual(p.transition,{mode:'fixed',name:'slideleft',duration_ms:300});assert.equal(p.music_gain,.35);
 assert.deepEqual(params({...f,musicMode:'random',musicScope:'categories',musicCategories:['C'],transition:'random'},[],[]).background_music,{mode:'random',category_ids:['C']});
 assert.throws(()=>params({...f,musicId:''},[],[]));assert.throws(()=>params({...f,musicMode:'random',musicScope:'categories',musicCategories:[]},[],[]));
});
test('duration range settings map to milliseconds and reject reversed limits',()=>{
 const f={...defaults(),totalMode:'range',totalMin:2.1,totalMax:2.9,clipMode:'range',clipMin:.4,clipMax:.7};
 const p=params(f,[],[]);assert.deepEqual(p.total_range_ms,[2100,2900]);assert.deepEqual(p.segment_range_ms,[400,700]);
 assert.throws(()=>params({...f,totalMin:3},[],[]));assert.throws(()=>params({...f,clipMin:0},[],[]));
 const fixed=params({...f,totalMode:'fixed',clipMode:'fixed'},[],[]);assert.equal(fixed.total_range_ms,undefined);assert.equal(fixed.segment_range_ms,undefined);
});
test('new requests use system default even when an old form contains a named voice',()=>{
 const f={...defaults(),voice:'Eddy (中文（中国大陆）)'};
 assert.equal(params(f,[],[]).voice,null);
 assert.equal(require('../../src/yijianjifa/webui/assets/mixing_form.js').fromParams({...params(f,[],[]),voice:f.voice}).voice,'');
 assert.equal(params({...f,voice:''},[],[]).voice,null);
});

test('saved settings restore every supported control without carrying batch-only text',()=>{
 const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');
 const f={...form.defaults(),quantity:4,totalMode:'range',totalMin:3,totalMax:5,clipMode:'range',clipMin:.5,clipMax:1.5,voice:'Eddy (中文（中国大陆）)',rate:1.2,order:'random',musicMode:'specific',musicId:'M1',transition:'specific',effect:'向左滑动',speechColor:'#123456',fullCaption:true,override:'本批文字'};
 const p=form.params(f,['V1'],['C1']);delete p.batch_full_text;
 const restored=form.fromParams(p);
 assert.equal(restored.override,'');assert.deepEqual(form.params(restored,['V1'],['C1']),{...p,batch_full_text:''});
});

test('new mixing form mutes source sound by default and permits opt in',()=>{
 assert.equal(params(defaults(),[],[]).original_gain,0);
 assert.equal(params({...defaults(),originalVolume:60},[],[]).original_gain,.6);
});
test('large subtitle sizes survive form and preset round trip',()=>{
 const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');
 const p=params({...defaults(),speechSize:100,fullSize:200},[],[]);
 assert.equal(p.spoken_style.size,100);assert.equal(p.full_style.size,200);
 const restored=form.fromParams(p);assert.equal(restored.speechSize,100);assert.equal(restored.fullSize,200);
});
test('subtitle defaults and line pagination are independent of output resolution',()=>{
 const f=defaults();assert.equal(f.speechSize,75);assert.equal(f.fullSize,100);
 const copy={voice:'同一个地方，在不同的光线下，看起来可能很不一样。',full:'测试'};
 for(const ratio of ['9:16','16:9','1:1'])assert.deepEqual(preview({...f,ratio,quality:'720p'},copy),preview({...f,ratio,quality:'1080p'},copy));
});
test('no-copy request strips text, subtitles and voice without mutating stored controls',()=>{
 const f={...defaults(),voice:'voice',speechCaption:true,fullCaption:true,override:'不能显示',bySentence:true};
 const p=params(f,[],['old-category'],false);
 assert.equal(p.use_copy,false);assert.deepEqual(p.copy_categories,[]);assert.equal(p.voice,null);assert.equal(p.spoken_enabled,false);assert.equal(p.full_enabled,false);assert.equal(p.batch_full_text,'');assert.equal(p.sentence_cut,false);
 assert.equal(f.override,'不能显示');assert.equal(params(f,[],[],true).voice,null);
});
test('manual subtitle heights are independent and survive preset restoration',()=>{
 const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');const p=params({...defaults(),speechHeight:72.5,fullHeight:23},[],[]);
 assert.equal(p.spoken_style.y_percent,72.5);assert.equal(p.full_style.y_percent,23);
 const f=form.fromParams(p);assert.equal(f.speechHeight,72.5);assert.equal(f.fullHeight,23);
 assert.throws(()=>params({...defaults(),speechHeight:101},[],[]));
});

test('spoken preview respects clause boundaries',()=>{assert.equal(preview(defaults(),{voice:'一会儿；重复的画面，就适合换个角度。'}).spoken,'一会儿');});

test('spoken preview hides punctuation while full text and source remain intact',()=>{const copy={voice:'你好，世界！',full:'你好，世界！'},p=preview({...defaults(),fullCaption:true,fullChars:20},copy);assert.equal(p.spoken,'你好');assert.equal(p.full,copy.full);assert.equal(copy.voice,'你好，世界！');});

test('cloud voice survives request and saved preset round trip',()=>{
 const P011MixForm=require('../../src/yijianjifa/webui/assets/mixing_form.js');const f=P011MixForm.defaults();f.voice='doubao:zh_female_xiaohe_uranus_bigtts';
 const p=P011MixForm.params(f,[],[]);assert.equal(p.voice,f.voice);
 assert.equal(P011MixForm.fromParams(p).voice,f.voice);
});

test('daily settings restore the full subtitle override while named presets can omit it',()=>{
 const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');
 const p=form.params({...form.defaults(),fullCaption:true,override:'每天保留的字幕'},[],[]);
 assert.equal(form.params(form.fromParams(p),[],[]).batch_full_text,'每天保留的字幕');
 delete p.batch_full_text;assert.equal(form.fromParams(p).override,'');
});

test('restoring partial persisted params merges defaults inside nested effects without replacing explicit choices',()=>{
 const form=require('../../src/yijianjifa/webui/assets/mixing_form.js');
 const restored=form.fromParams({quantity:3,total_ms:1000,segment_ms:1000,spoken_style:{size:120,color:'#123456'},full_style:{color:'#654321'},background_music:{mode:'random',category_ids:['music-cat']},transition:{mode:'random',duration_ms:800},keep_original_audio:false,original_gain:0});const p=form.params(restored,[],[]);assert.equal(p.quantity,3);assert.equal(p.spoken_style.size,120);assert.equal(p.spoken_style.color,'#123456');assert.equal(p.spoken_style.font,'sans');assert.equal(p.full_style.color,'#654321');assert.equal(p.full_style.size,100);assert.equal(p.keep_original_audio,false);assert.equal(p.original_gain,0);assert.deepEqual(p.background_music,{mode:'random',category_ids:['music-cat']});assert.deepEqual(p.transition,{mode:'random',duration_ms:800});
});
