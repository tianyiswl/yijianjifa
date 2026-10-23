const test=require('node:test'),assert=require('node:assert/strict');
const dates=require('../../src/yijianjifa/webui/assets/date_picker.js');
test('calendar validates leap days and month boundaries',()=>{assert.equal(dates.days(2028,1),29);assert.equal(dates.days(2100,1),28);assert.equal(dates.compose('date',2028,1,29,0,0),'2028-02-29');assert.equal(dates.compose('date',2026,1,29,0,0),null);assert.equal(dates.compose('date',2026,3,31,0,0),null);});
test('picker preserves local clock without timezone conversion',()=>{assert.equal(dates.compose('datetime-local',2026,8,28,0,5),'2026-09-28T00:05');assert.equal(dates.compose('time',2026,8,28,23,59),'23:59');assert.equal(dates.compose('time',2026,8,28,24,0),null);assert.equal(dates.compose('time',2026,8,28,2,60),null);});
test('live schedule window rounds inward and accepts only two and half hours through thirteen days',()=>{
 const now=new Date('2026-09-28T10:20:30').getTime(),r=dates.window(now);
 assert.equal(r.min,'2026-09-28T12:51');assert.equal(r.max,'2026-10-11T10:20');
 assert.equal(dates.allowed('2026-09-28T12:50',now),false);assert.equal(dates.allowed(r.min,now),true);
 assert.equal(dates.allowed(r.max,now),true);assert.equal(dates.allowed('2026-10-11T10:21',now),false);
 assert.equal(dates.allowed(r.min,now+60000),false);
});

test('a reusable daily clock is not constrained to the calendar picker current-day window',()=>{
 const fs=require('node:fs'),vm=require('node:vm'),listeners={};
 const clock={type:'time',value:'12:00',dataset:{recurringClock:'true'},matches:()=>true,checkValidity:()=>true};
 const date={type:'datetime-local',value:'',dataset:{},matches:()=>true,checkValidity:()=>true};
 const tomorrow={type:'date',value:'2026-10-16',readOnly:true,dataset:{},matches:()=>true,checkValidity:()=>true};
 const context={document:{body:{},querySelectorAll:()=>[clock,date,tomorrow],addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener(){}},MutationObserver:class{observe(){}disconnect(){}},setInterval:()=>1,clearInterval(){}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../../src/yijianjifa/webui/assets/date_picker.js'),'utf8')+';P011DatePicker();',context);
 assert.equal(clock.min,undefined);assert.equal(clock.max,undefined);assert.equal(clock.title,undefined);
 assert.equal(tomorrow.min,undefined);assert.equal(tomorrow.max,undefined);assert.equal(tomorrow.title,undefined);
 assert.ok(date.min&&date.max);
 let prevented=0;listeners.mousedown({target:clock,preventDefault(){prevented++}});assert.equal(prevented,0);
 listeners.mousedown({target:date,preventDefault(){prevented++}});assert.equal(prevented,1);
});

test('the daily start calendar uses Shanghai dates from tomorrow through day thirteen',()=>{
 assert.deepEqual(dates.dailyWindow(Date.parse('2026-10-15T15:59:00Z')),{min:'2026-10-16T00:00',max:'2026-10-28T23:59'});
 assert.deepEqual(dates.dailyWindow(Date.parse('2026-10-15T16:01:00Z')),{min:'2026-10-17T00:00',max:'2026-10-29T23:59'});
});

test('the calendar refresh does not overwrite the daily start field with the generic current-time window',()=>{
 const fs=require('node:fs'),vm=require('node:vm');
 const start={type:'date',value:'2026-10-28',dataset:{dateWindow:'daily-start'},matches:()=>true};
 const Now=class extends Date{constructor(...args){super(...(args.length?args:['2026-10-15T15:59:00Z']));}static now(){return Date.parse('2026-10-15T15:59:00Z');}};
 const context={Date:Now,document:{body:{},querySelectorAll:()=>[start],addEventListener(){},removeEventListener(){}},MutationObserver:class{observe(){}disconnect(){}},setInterval:()=>1,clearInterval(){}};
 vm.runInNewContext(fs.readFileSync(require.resolve('../../src/yijianjifa/webui/assets/date_picker.js'),'utf8')+';P011DatePicker();',context);
 assert.equal(start.min,'2026-10-16');assert.equal(start.max,'2026-10-28');assert.equal(start.value,'2026-10-28');assert.doesNotMatch(start.title,/2.5小时/);
});
