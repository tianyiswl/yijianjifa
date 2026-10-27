/* Shared visible daily rule. It produces times only; it never submits work. */
(function(root){'use strict';
const DAY=86400000,HOUR=3600000,ZONE=8*HOUR;
const formatter=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
const parts=date=>Object.fromEntries(formatter.formatToParts(date).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
const dateOf=date=>{const p=parts(date);return `${p.year}-${p.month}-${p.day}`;};
const addDays=(value,count)=>new Date(Date.parse(value+'T00:00:00Z')+count*DAY).toISOString().slice(0,10);
const iso=ms=>new Date(ms+ZONE).toISOString().slice(0,19)+'+08:00';
const short=ms=>{const p=parts(new Date(ms));return `${Number(p.month)}月${Number(p.day)}日${p.hour}:${p.minute}`;};
let serial=0;
function create({el,host,onChange=()=>{},getPlatforms=()=>[],isLocked=()=>false,maxDays=14,maxItems=100,maxCount=10000}){
 let alive=true,busy=false;
 const tomorrow=()=>addDays(dateOf(new Date()),1),prefix='publish-daily-'+(++serial)+'-';
 let state={start_date:tomorrow(),time:'12:00',days:'5',per_day:'2',interval_hours:'3'};
 const grid=el('div',undefined,'p011-daily-schedule'),summary=el('p',undefined,'p011-daily-schedule-summary'),error=el('p',undefined,'p011-daily-schedule-error'),controls={};error.setAttribute('role','alert');
 function field(text,name,tag='input',type='number'){
  const wrap=el('div',undefined,'p011-daily-schedule-field'),label=el('label',text),input=el(tag);input.id=prefix+name;input.dataset.field=name;input.setAttribute('aria-label',text);if(tag==='input')input.type=type;label.htmlFor=input.id;wrap.append(label,input);grid.append(wrap);controls[name]=input;return {wrap,input};
 }
 const date=field('开始日期','start_date','input','date').input;date.dataset.dateWindow='daily-start';date.setAttribute('aria-label','开始日期');
 const days=field('发布天数','days').input,perDay=field('每天发布条数','per_day').input;
 for(const input of [days,perDay]){input.min='1';input.max=String(maxCount);input.step='1';}
 const clock=field('每天首条时间','hour','select'),minute=el('select');minute.id=prefix+'minute';minute.dataset.field='minute';controls.minute=minute;
 clock.input.setAttribute('aria-label','首条时间：小时');minute.setAttribute('aria-label','首条时间：分钟');
 const clockRow=el('div',undefined,'p011-daily-schedule-clock');clockRow.setAttribute('role','group');clockRow.setAttribute('aria-label','每天首条时间');clockRow.append(clock.input,el('span',':'),minute);clock.wrap.append(clockRow);
 const spacing=field('两条间隔','interval_hours'),unit=el('div',undefined,'p011-daily-schedule-unit');spacing.input.min='0.5';spacing.input.max='336';spacing.input.step='0.5';unit.append(spacing.input,el('span','小时'));spacing.wrap.append(unit);
 host.append(grid,summary,error);
 const platforms=()=>Array.from(getPlatforms()||[]),locked=()=>busy||isLocked();
 const deadlineDays=()=>Math.min(Number(maxDays)||14,platforms().includes('douyin')?13:Infinity);
 function window(){const today=dateOf(new Date());date.min=addDays(today,1);date.max=addDays(today,13);return {min:date.min,max:date.max};}
 function options(input,values,value){
  const choices=values.map(v=>{const n=el('option',v);n.value=v;return n;});
  if(value&&!values.includes(value)){const n=el('option',value+'（需调整）');n.value=value;choices.push(n);}
  input.replaceChildren(...choices);input.value=value;
 }
 function paint(){
  date.value=state.start_date;days.value=state.days;perDay.value=state.per_day;spacing.input.value=state.interval_hours;
  const [hour='',min='']=state.time.split(':'),step=platforms().includes('bilibili')?5:1;
  options(clock.input,Array.from({length:24},(_,i)=>String(i).padStart(2,'0')),hour);
  options(minute,Array.from({length:60/step},(_,i)=>String(i*step).padStart(2,'0')),min);
  window();for(const input of Object.values(controls))input.disabled=locked();
 }
 function validate(draft=false){
  if(!alive)throw Error('发布安排已关闭，请重新打开页面。');const bounds=window();
  const start=Date.parse(state.start_date+'T00:00:00+08:00');
  if(!/^\d{4}-\d{2}-\d{2}$/.test(state.start_date)||!Number.isFinite(start)||dateOf(new Date(start))!==state.start_date)throw Error('请选择有效的开始日期。');
  if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(state.time))throw Error('请选择有效的首条时间。');
  const count=Number(state.days),each=Number(state.per_day),interval=Number(state.interval_hours);
  if(!Number.isInteger(count)||count<1||count>maxCount||!Number.isInteger(each)||each<1||each>maxCount)throw Error('发布天数和每天发布条数须为1至'+maxCount+'的整数。');
  if(!state.interval_hours||!Number.isFinite(interval)||interval<.5||interval>336||!Number.isInteger(interval*2))throw Error('两条间隔须为0.5小时的倍数，且不超过336小时。');
  if(!draft){
   if(state.start_date<bounds.min||state.start_date>bounds.max)throw Error('开始日期须选择上海明天至第13天之间的日期。');
   const first=Date.parse(state.start_date+'T'+state.time+':00+08:00');
   if(first<Date.now()+2.5*HOUR)throw Error('首条发布时间须在当前时间2.5小时之后，请调整首条时间。');
   if(platforms().includes('bilibili')&&Number(state.time.slice(3))%5!==0)throw Error('B站定时发布要求5分钟刻度，请调整分钟（00、05…55）。');
   const [hour,minute]=state.time.split(':').map(Number);
   if(hour*60+minute+(each-1)*interval*60>=1440)throw Error('每日发布时间会跨午夜，请减少每天条数、缩短间隔或提前首条时间。');
  }
  return {start_date:state.start_date,time:state.time,days:count,per_day:each,interval_hours:interval};
 }
 const capacity=value=>value.days*value.per_day;
 function slot(value,index){return Date.parse(value.start_date+'T'+value.time+':00+08:00')+Math.floor(index/value.per_day)*DAY+(index%value.per_day)*value.interval_hours*HOUR;}
 function describe(value,count,actual=false){summary.textContent=(actual?'已选'+count+'条 · ':'')+'排期容量'+capacity(value)+'条 · 最后一条：'+short(slot(value,count-1));}
 function showCapacity(){error.textContent='';try{const value=validate();describe(value,capacity(value));}catch(e){summary.textContent='';error.textContent=e.message;}}
 function edited(){
  if(!alive)return;if(locked()){paint();return;}
  state={start_date:date.value,time:clock.input.value+':'+minute.value,days:days.value,per_day:perDay.value,interval_hours:spacing.input.value};paint();showCapacity();onChange();
 }
 for(const [name,input]of Object.entries(controls)){input.onchange=edited;if(!['hour','minute'].includes(name))input.oninput=edited;}
 paint();showCapacity();
 return {
  load(value={}, {resetDate=false}={}){
   if(!alive)return;for(const key of Object.keys(state))if(Object.hasOwn(value,key))state[key]=String(value[key]??'');if(resetDate)state.start_date=tomorrow();paint();showCapacity();
  },
  value({draft=false}={}){const result=validate(draft);if(!draft){error.textContent='';describe(result,capacity(result));}return result;},
  times(videoIds){
   const value=validate();if(!Array.isArray(videoIds)||!videoIds.length)throw Error('请至少选择一条视频。');
   if(videoIds.some(id=>typeof id!=='string'||!id)||new Set(videoIds).size!==videoIds.length)throw Error('视频选择含重复或无效项目，请重新选择。');
   if(videoIds.length>capacity(value))throw Error('已选视频超过排期容量，当前最多安排'+capacity(value)+'条。');
   if(videoIds.length>maxItems)throw Error('每批最多安排'+maxItems+'条视频。');
   const count=videoIds.length,last=slot(value,count-1),limit=deadlineDays();describe(value,count,true);error.textContent='';
   if(last>Date.now()+limit*DAY){const message='最后一条超过'+limit+'天，请减少已选视频或调整发布安排。';error.textContent=message;throw Error(message);}
   return Object.fromEntries(videoIds.map((id,index)=>[id,iso(slot(value,index))]));
  },
  syncPlatforms(){if(alive){paint();showCapacity();}},
  setBusy(value){busy=!!value;if(alive)paint();},
  dispose(){if(!alive)return;alive=false;for(const input of Object.values(controls))input.oninput=input.onchange=null;}
 };
}
root.P011DailySchedule={create};
})(globalThis);
