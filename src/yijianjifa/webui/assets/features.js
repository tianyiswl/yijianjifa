const P011Features=Object.freeze({commerce:false});
function P011AddedTime(value){
 if(typeof value!=='string'||!value.trim())return '—';
 const text=value.trim().replace(' ','T'),date=new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(text)?text:text+'Z');
 if(!Number.isFinite(date.getTime()))return '—';
 const parts=Object.fromEntries(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(date).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
 return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
const P011Navigation={
 items:[['混剪制作','mixing'],['资料库','library'],['成品库','finished'],['普通发布','publish'],['多平台分发','multi_publish'],['团购带货','commerce'],['账号管理','accounts'],['任务记录','tasks'],['配音管理','voices'],['视频下载','downloads'],['常用方案与设置','presets']],
 render(nav){
  const buttons=this.items.filter(([,route])=>route!=='commerce'||P011Features.commerce).map(([label,route])=>{
   const button=document.createElement('button'),active=P011Router.current===route;
   button.type='button';button.textContent=label;button.dataset.route=route;button.className='nav-item'+(active?' active on':'');button.disabled=active;
   if(active)button.setAttribute('aria-current','page');
   button.onclick=event=>{if(event.isTrusted&&!button.disabled)window.p011Navigate(()=>P011Router.go(route));};
   return button;
  });nav.replaceChildren(...buttons);
 }
};
function P011ApplyFeatures(){
 const nav=document.querySelector?.('.nav,nav,#nav');
 if(nav)P011Navigation.render(nav);
 document.querySelectorAll('[data-commerce-column]').forEach(n=>n.hidden=!P011Features.commerce);
 if(P011Features.commerce)return;
 [...document.querySelectorAll('nav button'),...document.querySelectorAll('.nav button')].forEach(b=>{if(b.textContent.trim()==='团购带货')b.remove();});
 for(const id of ['commerce-btn','checkSelected']){const node=document.getElementById(id);if(node)node.hidden=true;}
}

function P011NativeVideoPreview(player,result,api,notice){
 if(!result.native_video)return false;
 const match=/^p011media:\/\/asset\/([A-Za-z0-9_-]{32})$/.exec(result.url||'');
 if(!match)throw Error('视频预览地址无效，请重新选择视频。');
 const token=match[1],button=document.createElement('button');let opening=null,opened=false,wanted=false,disposed=false,queued=false,lastSurface='';
 button.type='button';button.className='btn';button.textContent='播放视频';
 player.hidden=false;player.controls=false;player.setAttribute?.('data-native-video','true');
 function surface(){
  const r=player.getBoundingClientRect(),rect=[r.left,r.top,r.width,r.height];
  if(!rect.every(Number.isFinite)||r.width<=0||r.height<=0)return null;
  let left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(window.innerWidth||r.left+r.width,r.left+r.width),bottom=Math.min(window.innerHeight||r.top+r.height,r.top+r.height),visible=true;
  if(typeof window.getComputedStyle==='function'){
   for(let node=player;node;node=node.parentElement){
    const style=window.getComputedStyle(node);
    if(style.display==='none'||style.visibility==='hidden'||style.opacity==='0')visible=false;
    if(node===player)continue;
    const bounds=node.getBoundingClientRect();
    if(/hidden|clip|scroll|auto/.test(style.overflowX)){left=Math.max(left,bounds.left);right=Math.min(right,bounds.right);}
    if(/hidden|clip|scroll|auto/.test(style.overflowY)){top=Math.max(top,bounds.top);bottom=Math.min(bottom,bounds.bottom);}
   }
   const overlay=document.querySelector('#overlay');
   if(overlay&&!overlay.hidden&&!overlay.contains(player))visible=false;
  }
  if(right<=left||bottom<=top)return {rect,clip:[r.left,r.top,0,0],visible:false};
  if(document.elementsFromPoint){
   const near=Math.min(1,(right-left)/4,(bottom-top)/4);
   for(const [x,y] of [[left+near,top+near],[right-near,top+near],[left+near,bottom-near],[right-near,bottom-near],[(left+right)/2,(top+bottom)/2]]){
    const front=document.elementsFromPoint(x,y)[0];
    if(front!==player&&!player.contains?.(front)){visible=false;break;}
   }
  }
  return {rect,clip:[left,top,right-left,bottom-top],visible};
 }
 function stop(){wanted=false;button.hidden=false;if(opened){opened=false;void api('media.stop',{token}).catch(()=>{});}}
 function dispose(){if(disposed)return;stop();disposed=true;resize?.disconnect();mutation?.disconnect();window.removeEventListener?.('resize',schedule);document.removeEventListener?.('scroll',schedule,true);}
 function schedule(){
  if(disposed||queued)return;queued=true;
  const frame=async()=>{
   queued=false;if(disposed)return;if(player.isConnected===false){dispose();return;}if(!opened)return;
   const next=surface()||(lastSurface?{...JSON.parse(lastSurface),visible:false}:null);if(!next)return;
   const raw=JSON.stringify(next);if(raw===lastSurface)return;lastSurface=raw;
   try{await api('media.position',{token,surface:next});}catch(e){stop();notice(e.message);}
  };
  if(window.requestAnimationFrame)window.requestAnimationFrame(frame);else void Promise.resolve().then(frame);
 }
 const play=()=>{
  if(disposed)return Promise.reject(Error('预览已关闭，请重新选择视频。'));
  wanted=true;if(opening)return opening;
  const next=surface();if(!next)return Promise.reject(Error('预览区域暂不可用，请重新选择视频。'));
  button.disabled=true;
  opening=Promise.resolve().then(()=>api('media.play',{token,surface:next})).then(()=>{
   opened=true;lastSurface=JSON.stringify(next);button.hidden=true;
   if(!wanted||disposed||player.isConnected===false){stop();return;}schedule();
  }).finally(()=>{opening=null;button.disabled=false;});
  return opening;
 };
 const resize=typeof ResizeObserver==='function'?new ResizeObserver(schedule):null;
 const mutation=typeof MutationObserver==='function'?new MutationObserver(schedule):null;
 resize?.observe(player);mutation?.observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['class','style','hidden']});
 window.addEventListener?.('resize',schedule);document.addEventListener?.('scroll',schedule,true);
 player.p011NativePlay=async()=>{await play();if(opened)await api('media.expand',{token});};player.play=play;
 player.pause=stop;
 player.after(button);button.onclick=()=>play().catch(e=>notice(e.message));
 return true;
}
