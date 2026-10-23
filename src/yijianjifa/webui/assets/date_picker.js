/* Local wall-clock values only: no UTC conversion or platform operation. */
const P011DateValues={
 local(date){return `${date.getFullYear()}-${this.pad(date.getMonth()+1)}-${this.pad(date.getDate())}T${this.pad(date.getHours())}:${this.pad(date.getMinutes())}`;},
 window(now=Date.now()){return {min:this.local(new Date(Math.ceil((now+2.5*3600000)/60000)*60000)),max:this.local(new Date(Math.floor((now+13*86400000)/60000)*60000))};},
 dailyWindow(now=Date.now()){const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(now)).filter(p=>p.type!=='literal').map(p=>[p.type,p.value])),base=Date.parse(`${parts.year}-${parts.month}-${parts.day}T00:00:00Z`),day=n=>new Date(base+n*86400000).toISOString().slice(0,10);return {min:day(1)+'T00:00',max:day(13)+'T23:59'};},
 allowed(value,now=Date.now()){const ms=new Date(value).getTime();return Number.isFinite(ms)&&ms>=now+2.5*3600000&&ms<=now+13*86400000;},
 pad(n){return String(n).padStart(2,'0');},
 days(year,month){return new Date(year,month+1,0).getDate();},
 compose(type,year,month,day,hour,minute){
  if(!Number.isInteger(year)||year<1000||year>9999||!Number.isInteger(month)||month<0||month>11||!Number.isInteger(day)||day<1||day>this.days(year,month)||!Number.isInteger(hour)||hour<0||hour>23||!Number.isInteger(minute)||minute<0||minute>59)return null;
  const date=`${year}-${this.pad(month+1)}-${this.pad(day)}`,time=`${this.pad(hour)}:${this.pad(minute)}`;
  return type==='date'?date:type==='time'?time:date+'T'+time;
 }
};
function P011DatePicker(){
 let active=null,source=null;
 const selector='input[type="date"],input[type="datetime-local"],input[type="time"]';
 const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 function close(restore=true){active?.remove();active=null;const old=source;source=null;if(restore&&old?.isConnected)old.focus();}
 function anchor(input){for(let node=input.parentElement;node;node=node.parentElement){const date=[...node.querySelectorAll('input[type="date"],input[type="datetime-local"]')].find(n=>!n.disabled&&!n.hidden&&n.value);if(date)return date.value.slice(0,10);}return P011DateValues.window().min.slice(0,10);}
 function rangeFor(input){return input.dataset.dateWindow==='daily-start'?P011DateValues.dailyWindow():P011DateValues.window();}
 function bound(input){if(input.readOnly||input.dataset.recurringClock==='true')return;const range=rangeFor(input);if(input.type==='datetime-local'){input.min=range.min;input.max=range.max;}else if(input.type==='date'){input.min=range.min.slice(0,10);input.max=range.max.slice(0,10);}else{const date=anchor(input);input.min=date===range.min.slice(0,10)?range.min.slice(11):'00:00';input.max=date===range.max.slice(0,10)?range.max.slice(11):'23:59';}input.title=input.dataset.dateWindow==='daily-start'?'可选明天至当前日期后第13天':'定时时间须在当前时间2.5小时后至13天后之间';}
 function refresh(){document.querySelectorAll(selector).forEach(bound);}
 function changed(e){if(e.target.matches?.(selector)&&e.target.dataset.recurringClock!=='true'){refresh();if(e.isTrusted&&!e.target.disabled&&!e.target.checkValidity())e.target.reportValidity();}}
 function open(input){
  close(false);bound(input);source=input;const type=input.type,range=rangeFor(input),now=new Date(range.min);let initial=input.value||input.min;if(type!=='time'&&(initial<input.min||initial>input.max))initial=input.min;const parts=initial.split('T'),date=type==='time'?anchor(input):parts[0],clock=type==='time'?initial:parts[1];
  let year=date?Number(date.slice(0,4)):now.getFullYear(),month=date?Number(date.slice(5,7))-1:now.getMonth(),day=date?Number(date.slice(8,10)):now.getDate(),viewYear=year,viewMonth=month;
  const shade=el('div',undefined,'p011-date-backdrop'),dialog=el('div',undefined,'p011-date-dialog');active=shade;dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-label',type==='time'?'选择时间':type==='date'?'选择日期':'选择日期和时间');dialog.tabIndex=-1;
  dialog.append(el('h2',dialog.getAttribute('aria-label')),el('p',input.dataset.dateWindow==='daily-start'?'可选开始日期：'+range.min.slice(0,10)+' 至 '+range.max.slice(0,10):'可选范围：'+range.min.replace('T',' ')+' 至 '+range.max.replace('T',' '),'p011-date-range'));shade.append(dialog);
  const button=(text,fn,cls)=>{const b=el('button',text,cls);b.type='button';b.addEventListener('click',e=>{if(e.isTrusted)fn();});return b;};
  const calendar=el('div'),monthBar=el('div',undefined,'p011-date-month'),heading=el('b');
  function move(delta){const d=new Date(viewYear,viewMonth+delta,1),r=rangeFor(input),monthText=P011DateValues.local(d).slice(0,7);if(monthText<r.min.slice(0,7)||monthText>r.max.slice(0,7))return;viewYear=d.getFullYear();viewMonth=d.getMonth();render();}
  monthBar.append(button('‹ 上月',()=>move(-1)),heading,button('下月 ›',()=>move(1)));
  function render(){heading.textContent=`${viewYear}年 ${viewMonth+1}月`;const grid=el('div',undefined,'p011-date-calendar');for(const w of ['一','二','三','四','五','六','日'])grid.append(el('span',w));const offset=(new Date(viewYear,viewMonth,1).getDay()+6)%7;for(let i=0;i<offset;i++)grid.append(el('span',''));
   for(let d=1;d<=P011DateValues.days(viewYear,viewMonth);d++){const b=button(String(d),()=>{year=viewYear;month=viewMonth;day=d;render();syncTime();});const r=rangeFor(input),dateText=P011DateValues.compose('date',viewYear,viewMonth,d,0,0);b.disabled=dateText<r.min.slice(0,10)||dateText>r.max.slice(0,10);b.setAttribute('aria-label',`${viewYear}年${viewMonth+1}月${d}日`);b.setAttribute('aria-pressed',String(year===viewYear&&month===viewMonth&&day===d));grid.append(b);}calendar.replaceChildren(grid);
  }
  if(type!=='time'){dialog.append(monthBar,calendar);render();}
  const times=el('div',undefined,'p011-date-time'),hours=el('select'),minutes=el('select');
  for(const [field,label,count,current] of [[hours,'小时',24,clock?.slice(0,2)||'09'],[minutes,'分钟',60,clock?.slice(3,5)||'00']]){field.setAttribute('aria-label',label);for(let i=0;i<count;i++){const o=el('option',P011DateValues.pad(i));o.value=P011DateValues.pad(i);field.append(o);}field.value=current;const wrap=el('label',label);wrap.append(field);times.append(wrap);}
  function syncTime(){const r=rangeFor(input),dateText=P011DateValues.compose('date',year,month,day,0,0),low=dateText===r.min.slice(0,10)?r.min.slice(11):'00:00',high=dateText===r.max.slice(0,10)?r.max.slice(11):'23:59',outside=dateText<r.min.slice(0,10)||dateText>r.max.slice(0,10);
   for(const o of hours.options)o.disabled=outside||o.value<low.slice(0,2)||o.value>high.slice(0,2);
   if(hours.selectedOptions[0]?.disabled)hours.value=[...hours.options].find(o=>!o.disabled)?.value||'';
   for(const o of minutes.options)o.disabled=outside||!hours.value||hours.value+':'+o.value<low||hours.value+':'+o.value>high;
   if(minutes.selectedOptions[0]?.disabled)minutes.value=[...minutes.options].find(o=>!o.disabled)?.value||'';
  }
  hours.addEventListener('change',syncTime);syncTime();
  if(type!=='date')dialog.append(times);
  const error=el('p','', 'p011-date-error');error.setAttribute('role','alert');dialog.append(error);const actions=el('div',undefined,'p011-date-actions');actions.append(button('取消',()=>close()),button('确定',()=>{
   bound(input);const value=P011DateValues.compose(type,year,month,day,Number(hours.value),Number(minutes.value));
   const combined=type==='time'?anchor(input)+'T'+value:value;
   if(!value||(type!=='date'&&!P011DateValues.allowed(combined))||(input.min&&value<input.min)||(input.max&&value>input.max)){syncTime();error.textContent=input.dataset.dateWindow==='daily-start'?'日期范围已更新，请选择明天至第13天之间的日期。':'时间范围已更新，请选择当前时间2.5小时后至13天后之间的时刻。';return;}
   const previous=input.value;input.value=value;if(!input.checkValidity()){input.value=previous;error.textContent='日期或时间不符合当前字段要求，请调整后重试。';return;}
   input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));close();
  },'primary'));dialog.append(actions);shade.addEventListener('click',e=>{if(e.target===shade)close();});document.body.append(shade);dialog.focus();
 }
 function eligible(e){return e.target.matches?.(selector)&&e.target.dataset.recurringClock!=='true'&&!e.target.disabled&&!e.target.readOnly&&!active;}
 function mouse(e){if(eligible(e))e.preventDefault();}
 function click(e){if(!e.isTrusted||!eligible(e))return;e.preventDefault();open(e.target);}
 function key(e){if(active){if(e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();return;}if(e.key==='Tab'){const items=[...active.querySelectorAll('button,select')].filter(n=>!n.disabled);const first=items[0],last=items.at(-1);if(e.shiftKey&&(document.activeElement===first||!items.includes(document.activeElement))){e.preventDefault();last.focus();}else if(!e.shiftKey&&(document.activeElement===last||!items.includes(document.activeElement))){e.preventDefault();first.focus();}}}else if(e.isTrusted&&eligible(e)&&(e.key==='Enter'||(e.altKey&&e.key==='ArrowDown'))){e.preventDefault();open(e.target);}}
 const observer=new MutationObserver(refresh);observer.observe(document.body,{childList:true,subtree:true});const timer=setInterval(refresh,30000);refresh();document.addEventListener('change',changed,true);document.addEventListener('focusin',refresh);
 document.addEventListener('mousedown',mouse,true);document.addEventListener('click',click,true);document.addEventListener('keydown',key,true);
 return ()=>{observer.disconnect();clearInterval(timer);document.removeEventListener('change',changed,true);document.removeEventListener('focusin',refresh);close(false);document.removeEventListener('mousedown',mouse,true);document.removeEventListener('click',click,true);document.removeEventListener('keydown',key,true);};
}
if(typeof module!=='undefined')module.exports=P011DateValues;
