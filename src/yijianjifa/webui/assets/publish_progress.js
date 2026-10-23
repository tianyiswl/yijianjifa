// Pending work belongs in the task sidebar, never in a cross-page banner.
function P011PublishProgress(){
 let alive=true,timer,badge=null;
 function showCount(total){
  const button=Array.from(document.querySelector('.nav,nav,#nav')?.querySelectorAll('button')||[]).find(n=>n.dataset.route==='tasks');
  if(!button)return;
  if(!badge||badge.parentElement!==button){badge?.remove();badge=document.createElement('span');badge.className='p011-task-count';button.append(badge);}
  const count=Math.max(0,total);badge.textContent=String(count);badge.hidden=count===0;
  badge.setAttribute('aria-label',count+' 个待处理任务');
 }
 async function tick(){
  try{
   const request_id=[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');
   const raw=await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id,operation:'task.center.list',params:{query:'',tab:'action',status:'',offset:0,limit:1}}));
   if(!alive)return;const response=JSON.parse(raw);
   if(response.ok&&Number.isSafeInteger(response.data.total))showCount(response.data.total);
  }catch(e){/* A read failure leaves the last known count; no intrusive banner. */}
  finally{if(alive)timer=setTimeout(tick,3000);}
 }
 tick();return ()=>{alive=false;clearTimeout(timer);badge?.remove();};
}
