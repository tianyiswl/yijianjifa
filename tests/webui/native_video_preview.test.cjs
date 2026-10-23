const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function setup(){const buttons=[],calls=[],messages=[];const player={after:b=>buttons.push(b),hidden:false,play:async()=>calls.push('html'),pause:()=>calls.push('htmlpause')};const context={document:{createElement:()=>({setAttribute(){}})},window:{}};vm.createContext(context);vm.runInContext(fs.readFileSync('src/yijianjifa/webui/assets/features.js','utf8'),context);return {player,buttons,calls,messages,prepare:r=>context.P011NativeVideoPreview(player,r,async(op,p)=>calls.push([op,p.token]),s=>messages.push(s))};}
test('Windows video preview keeps its original page slot and stops on cleanup',async()=>{const v=setup();v.player.getBoundingClientRect=()=>({left:40,top:60,width:320,height:180});assert.equal(v.prepare({native_video:true,url:'p011media://asset/abcdefghijklmnopqrstuvwx12345678'}),true);assert.equal(v.calls.length,0);await v.player.play();v.player.pause();assert.deepEqual(v.calls,[['media.play','abcdefghijklmnopqrstuvwx12345678'],['media.stop','abcdefghijklmnopqrstuvwx12345678']]);assert.equal(v.player.hidden,false);assert.equal(v.buttons[0].textContent,'播放视频');});
test('other media retain HTML playback',async()=>{const v=setup();assert.equal(v.prepare({url:'p011media://asset/token'}),false);await v.player.play();assert.deepEqual(v.calls,['html']);});
test('invalid native URLs never become file or network playback',()=>{for(const url of ['file:///tmp/a','https://example.test/a','p011media://other/token']){const v=setup();assert.throws(()=>v.prepare({native_video:true,url}));assert.equal(v.calls.length,0);}});
const {mount}=require('./support/commerce_view.cjs');
function positioned(){
 const calls=[],frames=[],listeners={},observers=[];let rect={left:30,top:40,width:320,height:180},covered=false;
 const player={hidden:false,isConnected:true,parentElement:null,getBoundingClientRect:()=>rect,after(){},setAttribute(){}};
 const window={innerWidth:1000,innerHeight:800,addEventListener:(n,fn)=>listeners[n]=fn,removeEventListener:n=>delete listeners[n],requestAnimationFrame:fn=>frames.push(fn)};
 const document={body:{},createElement:()=>({}),querySelector:()=>null,elementsFromPoint:()=>covered?[{}]:[player],addEventListener(){},removeEventListener(){}};
 class Observer{constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}}
 const ctx={window,document,ResizeObserver:Observer,MutationObserver:Observer};vm.createContext(ctx);vm.runInContext(fs.readFileSync('src/yijianjifa/webui/assets/features.js','utf8'),ctx);
 ctx.P011NativeVideoPreview(player,{native_video:true,url:'p011media://asset/abcdefghijklmnopqrstuvwx12345678'},async(op,p)=>calls.push([op,JSON.parse(JSON.stringify(p))]),()=>{});
 return {player,calls,observers,move:r=>{rect=r;listeners.resize();},cover:()=>{covered=true;observers.at(-1).fn();},async flush(){while(frames.length)frames.shift()();await Promise.resolve();await Promise.resolve();}};
}
test('embedded preview follows page geometry without restarting playback and hides behind dialogs',async()=>{
 const v=positioned();await v.player.play();assert.deepEqual(v.calls[0][1].surface.rect,[30,40,320,180]);
 v.move({left:50,top:100,width:400,height:240});await v.flush();assert.equal(v.calls.at(-1)[0],'media.position');assert.deepEqual(v.calls.at(-1)[1].surface.rect,[50,100,400,240]);
 v.cover();await v.flush();assert.equal(v.calls.at(-1)[1].surface.visible,false);
 assert.equal(v.calls.filter(([op])=>op==='media.play').length,1);
 v.player.isConnected=false;v.observers.at(-1).fn();await v.flush();assert.equal(v.calls.at(-1)[0],'media.stop');assert.ok(v.observers.every(o=>o.disconnected));
});
test('original-size playback expands inside the existing app after establishing the video grant',async()=>{
 const v=positioned();await v.player.p011NativePlay();
 assert.deepEqual(v.calls.map(([op])=>op),['media.play','media.expand']);
 assert.equal(v.calls.at(-1)[1].token,'abcdefghijklmnopqrstuvwx12345678');v.player.pause();
});
test('an original-size click during initial playback waits for the same open request',async()=>{
 const v=positioned();const playing=v.player.play(),expanding=v.player.p011NativePlay();await Promise.all([playing,expanding]);
 assert.deepEqual(v.calls.map(([op])=>op),['media.play','media.expand']);v.player.pause();
});
for(const route of ['library','finished'])test(route+' opens native preview from the real view and closes it on leave',async()=>{
 const calls=[],token='abcdefghijklmnopqrstuvwx12345678',row={id:'video-1',name:'测试视频',display_name:'测试视频',managed:false,missing:false,category_ids:[],duration_ms:2000};
 const v=await mount(async(op,p)=>{calls.push([op,p]);if(op==='media.preference')return {keep_copy:false};if(op==='category.list')return {items:[]};if(op==='library.list'||op==='finished.list')return {items:[row],total:1};if(op==='media.preview')return {url:'p011media://asset/'+token,native_video:true};if(op==='media.thumbnail')return {image:'data:image/jpeg;base64,fixture'};if(op==='media.play'||op==='media.stop')return {};throw Error(op);},route,undefined,{structuredClone,P011Router:{current:route,go(){}}});
 if(route==='library')await v.find(n=>n.tag==='tr'&&n.dataset.rowId===row.id).ondblclick({isTrusted:true});
 else await v.click('预览 / 编辑');
 await v.flush();assert.equal(calls.filter(([op])=>op==='media.play').length,1);
 const player=v.find(n=>n.tag==='video');assert.equal(player.hidden,false);assert.ok(v.find(n=>n.textContent==='播放视频'));
 if(route==='finished'){
  await v.click('原画播放');assert.ok(v.find(n=>n.className==='p011-original-player'));
  await v.click('返回预览');assert.ok(player.parentElement);
 }
 v.dispose();await v.flush();assert.ok(calls.some(([op,p])=>op==='media.stop'&&p.token===token));
});
