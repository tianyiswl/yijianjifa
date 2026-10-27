const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const {Node}=require('./support/commerce_view.cjs');
function mount({now='2026-10-25T04:00:00Z',platforms=[],maxDays=14,maxItems=100,maxCount=10000,locked=false}={}){
 let instant=Date.parse(now),selected=platforms,changes=0;
 class Clock extends Date{constructor(...args){super(...(args.length?args:[instant]));}static now(){return instant;}}
 const context={Date:Clock},host=new Node('section'),el=(tag,text,cls)=>{const n=new Node(tag);if(text!==undefined)n.textContent=text;n.className=cls||'';return n;};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/yijianjifa/webui/assets/publish_daily_schedule.js'),'utf8'),context);
 const schedule=context.P011DailySchedule.create({el,host,maxDays,maxItems,maxCount,getPlatforms:()=>selected,isLocked:()=>locked,onChange:()=>changes++});
 const field=name=>host.querySelectorAll('input,select').find(n=>n.dataset.field===name),text=()=>host.querySelectorAll('p').map(n=>n.textContent).join('\n');
 return {host,schedule,field,text,get changes(){return changes;},edit(name,value){const n=field(name);n.value=value;(n.oninput||n.onchange)({isTrusted:true,target:n});},platforms(value){selected=value;schedule.syncPlatforms();},lock(value){locked=value;schedule.syncPlatforms();},now(value){instant=Date.parse(value);}};
}
const plain=value=>JSON.parse(JSON.stringify(value));
const valid={start_date:'2026-10-26',time:'12:00',days:3,per_day:2,interval_hours:3};
test('daily schedule has the five mixing fields and defaults to Shanghai tomorrow without changing settings on load',()=>{
 const v=mount({now:'2026-10-24T16:30:00Z'});assert.deepEqual(plain(v.schedule.value()),{...valid,start_date:'2026-10-26',days:5});
 assert.deepEqual(v.host.querySelectorAll('label').map(n=>n.textContent),['开始日期','发布天数','每天发布条数','每天首条时间','两条间隔']);assert.equal(v.field('start_date').type,'date');assert.equal(v.field('start_date').dataset.dateWindow,'daily-start');assert.equal(v.field('hour').children.length,24);assert.equal(v.field('minute').children.length,60);assert.equal(v.changes,0);v.schedule.dispose();
 for(const [name,label]of [['days','发布天数'],['per_day','每天发布条数'],['interval_hours','两条间隔']])assert.equal(v.field(name).attrs['aria-label'],label);
});
test('loading a concrete date preserves the clock and rule and resets the date only when explicitly requested',()=>{
 const v=mount();v.schedule.load({...valid,start_date:'2026-10-29',time:'09:15',per_day:3});assert.equal(v.schedule.value().start_date,'2026-10-29');v.schedule.load({...valid,start_date:'2026-10-29',time:'09:15',per_day:3},{resetDate:true});assert.deepEqual(plain(v.schedule.value()),{...valid,time:'09:15',per_day:3});assert.equal(v.changes,0);v.schedule.dispose();
});
test('times accepts fewer videos than capacity and states the actual selected count and final slot',()=>{
 const v=mount();v.schedule.load(valid);assert.match(v.text(),/6条/);const slots=v.schedule.times(['a','b','c']);assert.deepEqual(plain(slots),{a:'2026-10-26T12:00:00+08:00',b:'2026-10-26T15:00:00+08:00',c:'2026-10-27T12:00:00+08:00'});assert.match(v.text(),/已选3条/);assert.match(v.text(),/容量6条/);assert.match(v.text(),/10月27日12:00/);v.schedule.dispose();
});
test('empty, repeated and over-capacity video selections are rejected rather than shortened or overwritten',()=>{
 const v=mount();v.schedule.load({...valid,days:1});assert.throws(()=>v.schedule.times([]),/至少.*视频/);assert.throws(()=>v.schedule.times(['a','a']),/重复|有效/);assert.throws(()=>v.schedule.times(['a','b','c']),/容量|最多.*2/);assert.equal(v.changes,0);v.schedule.dispose();
});
test('only tomorrow through the thirteenth Shanghai calendar day can be the starting day',()=>{
 const v=mount({maxDays:14});for(const date of ['2026-10-25','2026-11-08','2026-02-30','not-a-date']){v.schedule.load({...valid,start_date:date,days:1,per_day:1});assert.throws(()=>v.schedule.value(),/日期/);assert.equal(v.field('start_date').value,date);}for(const date of ['2026-10-26','2026-11-07']){v.schedule.load({...valid,start_date:date,days:1,per_day:1});assert.equal(v.schedule.value().start_date,date);}assert.equal(v.field('start_date').min,'2026-10-26');assert.equal(v.field('start_date').max,'2026-11-07');v.schedule.dispose();
});
test('the minimum lead time is still two and a half hours when tomorrow starts soon',()=>{
 const v=mount({now:'2026-10-25T15:30:00Z'});v.schedule.load({...valid,time:'00:00',days:1,per_day:1});assert.throws(()=>v.schedule.value(),/2\.5小时/);v.schedule.load({...valid,time:'02:00',days:1,per_day:1});assert.equal(v.schedule.times(['a']).a,'2026-10-26T02:00:00+08:00');v.schedule.dispose();
});
test('midnight refresh never silently moves a previously chosen start date',()=>{
 const v=mount({now:'2026-10-25T15:59:00Z'});v.schedule.load({...valid,days:1,per_day:1});v.now('2026-10-25T16:01:00Z');assert.throws(()=>v.schedule.value(),/日期/);assert.equal(v.field('start_date').value,'2026-10-26');assert.equal(v.field('start_date').min,'2026-10-27');v.schedule.dispose();
});
test('Bili uses five-minute choices and preserves an incompatible saved minute for correction',()=>{
 const v=mount();v.schedule.load({...valid,time:'12:02'});v.platforms(new Set(['bilibili']));assert.equal(v.field('minute').value,'02');assert.equal(v.field('minute').children.length,13);assert.match(v.field('minute').children.at(-1).textContent,/需调整/);assert.throws(()=>v.schedule.value(),/B站.*5分钟/);v.edit('minute','05');assert.equal(v.schedule.times(['a']).a,'2026-10-26T12:05:00+08:00');v.platforms([]);assert.equal(v.field('minute').children.length,60);assert.equal(v.field('minute').value,'05');v.schedule.dispose();
});
test('an invalid saved clock stays visible instead of silently falling back to noon',()=>{
 const v=mount();for(const time of ['25:00','12:99','']){v.schedule.load({...valid,time});assert.throws(()=>v.schedule.value(),/时间/);}v.schedule.load({...valid,time:'12:45'});assert.equal(v.schedule.value().time,'12:45');v.schedule.dispose();
});
test('daily slots may not cross midnight and intervals must be positive half hours',()=>{
 const v=mount();v.schedule.load({...valid,time:'22:00'});assert.throws(()=>v.schedule.value(),/跨.*午夜/);for(const interval of [0,0.25,1.2,337,'']){v.schedule.load({...valid,interval_hours:interval});assert.throws(()=>v.schedule.value(),/间隔/);}v.schedule.load({...valid,time:'22:00',interval_hours:.5});assert.equal(v.schedule.times(['a','b']).b,'2026-10-26T22:30:00+08:00');v.schedule.dispose();
});
test('the actual last selected slot obeys the configured limit and Douyin tightens a fourteen-day view to thirteen',()=>{
 for(const [maxDays,platforms,allowed]of [[14,[],true],[14,['douyin'],false],[13,[],false]]){const v=mount({maxDays,platforms});v.schedule.load({...valid,start_date:'2026-11-07',days:2,per_day:1});assert.equal(v.schedule.times(['a']).a,'2026-11-07T12:00:00+08:00');if(allowed)assert.equal(v.schedule.times(['a','b']).b,'2026-11-08T12:00:00+08:00');else assert.throws(()=>v.schedule.times(['a','b']),/最后一条.*13天/);v.schedule.dispose();}
});
test('positive whole-number days and per-day counts are required without forcing selected count to equal capacity',()=>{
 const v=mount();for(const key of ['days','per_day'])for(const value of ['',0,-1,1.5]){v.schedule.load({...valid,[key]:value});assert.throws(()=>v.schedule.value(),/天数|条数/);}v.schedule.load(valid);assert.equal(Object.keys(v.schedule.times(['one'])).length,1);v.schedule.dispose();
});
test('the ordinary adapter can limit days and daily counts to its existing two-hundred contract',()=>{
 const v=mount({maxCount:200});assert.equal(v.field('days').max,'200');assert.equal(v.field('per_day').max,'200');
 for(const key of ['days','per_day']){v.schedule.load({...valid,[key]:201});assert.throws(()=>v.schedule.value({draft:true}),/1至200/);assert.throws(()=>v.schedule.times(['one']),/1至200/);}
 v.schedule.load({...valid,days:200,per_day:1});assert.equal(v.schedule.value({draft:true}).days,200);assert.equal(v.schedule.times(['one']).one,'2026-10-26T12:00:00+08:00');v.schedule.dispose();
});
test('ordinary batches retain two-hundred items while default multi-platform batches retain one-hundred',()=>{
 const ids=Array.from({length:201},(_,i)=>'video-'+i),rule={...valid,days:11,per_day:20,interval_hours:.5};
 const ordinary=mount({maxItems:200,maxCount:200});ordinary.schedule.load(rule);assert.equal(Object.keys(ordinary.schedule.times(ids.slice(0,200))).length,200);assert.throws(()=>ordinary.schedule.times(ids),/每批最多安排200条/);ordinary.schedule.dispose();
 const multi=mount();multi.schedule.load(rule);assert.equal(Object.keys(multi.schedule.times(ids.slice(0,100))).length,100);assert.throws(()=>multi.schedule.times(ids.slice(0,101)),/每批最多安排100条/);assert.equal(multi.field('days').max,'10000');multi.schedule.dispose();
});
test('busy and externally locked controls cannot change the saved rule or emit edits',()=>{
 const v=mount();v.schedule.load(valid);v.schedule.setBusy(true);assert(v.host.querySelectorAll('input,select').every(n=>n.disabled));v.edit('days','1');assert.equal(v.field('days').value,'3');assert.equal(v.changes,0);v.schedule.setBusy(false);v.edit('days','2');assert.equal(v.changes,1);v.lock(true);v.edit('per_day','1');assert.equal(v.schedule.value().per_day,2);v.lock(false);assert(v.host.querySelectorAll('input,select').every(n=>!n.disabled));v.schedule.dispose();
});
test('draft saving keeps an expired or Bili-incompatible choice for correction while publishing rejects it',()=>{
 const v=mount({platforms:['bilibili']});v.schedule.load({...valid,start_date:'2026-10-25',time:'12:02'});assert.equal(v.schedule.value({draft:true}).start_date,'2026-10-25');assert.equal(v.schedule.value({draft:true}).time,'12:02');assert.throws(()=>v.schedule.times(['a']),/日期|B站/);v.schedule.dispose();
});
test('disposing the view ignores detached input events and cannot generate a new schedule',()=>{
 const v=mount(),days=v.field('days');v.schedule.dispose();v.schedule.dispose();days.value='1';days.oninput?.({isTrusted:true});assert.equal(v.changes,0);assert.throws(()=>v.schedule.times(['a']),/已关闭/);assert.throws(()=>v.schedule.value(),/已关闭/);
});
