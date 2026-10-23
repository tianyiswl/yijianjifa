/* Startup review only. Continuing a batch remains an explicit task-page action. */
const P011StartupRecovery=(()=>{
 let requested=false;
 return async function(){
  if(requested)return;requested=true;
  let tasks=[],failed=false;
  try{
   const request_id=[...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('');
   const response=JSON.parse(await window.webkit.messageHandlers.p011App.postMessage(JSON.stringify({v:1,request_id,operation:'task.recovery',params:{}})));
   if(!response.ok)throw Error('recovery unavailable');
   tasks=response.data.tasks;
  }catch(error){failed=true;}
  if(!failed&&!tasks.length)return;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const overlay=el('div',undefined,'dialog-backdrop'),dialog=el('section',undefined,'dialog narrow');
  const head=el('header',undefined,'dialog-head'),body=el('div',undefined,'dialog-body'),foot=el('footer',undefined,'dialog-foot');
  // The read can finish after navigation has replaced the page's stylesheet.
  // Keep the approved library dialog appearance on these nodes only.
  Object.assign(overlay.style,{position:'fixed',inset:'0',display:'flex',alignItems:'center',justifyContent:'center',background:'#19233766',zIndex:'2000',padding:'24px',boxSizing:'border-box',color:'#222d40',font:'14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif'});
  Object.assign(dialog.style,{position:'relative',margin:'0',padding:'0',transform:'none',boxSizing:'border-box',maxWidth:'100%',width:'570px',maxHeight:'calc(100vh - 48px)',display:'flex',flexDirection:'column',background:'white',border:'1px solid #e1e7f1',borderRadius:'15px',overflow:'hidden',boxShadow:'0 18px 70px #17284d25'});
  Object.assign(head.style,{padding:'19px 22px',borderBottom:'1px solid #e1e7f1',display:'flex',alignItems:'flex-start',justifyContent:'space-between',gap:'15px'});
  Object.assign(body.style,{padding:'19px 22px',overflow:'auto',minHeight:'0'});
  Object.assign(foot.style,{borderTop:'1px solid #e1e7f1',background:'#fafbfe',padding:'13px 22px',display:'flex',alignItems:'center',justifyContent:'space-between',gap:'12px',flexWrap:'wrap'});
  const title=el('h2',failed?'检查未完成的制作':'是否继续未完成的制作？');title.id='startup-recovery-title';head.append(title);
  Object.assign(title.style,{margin:'0',fontSize:'19px'});
  dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.setAttribute('aria-labelledby',title.id);
  if(failed)body.append(el('p','暂时无法读取未完成的制作任务。请到任务记录中检查，再选择是否继续。'));
  else body.append(el('p',`发现 ${tasks.length} 个未完成的制作批次，剩余 ${tasks.reduce((sum,t)=>sum+Math.max(0,t.quantity-t.completed),0)} 条。`));
  body.append(el('p','已完成成品和失败记录保留，任务尚未继续。可先查看，再逐批决定。'));
  for(const p of body.children)p.style.margin='0';
  const background=[...document.body.children].map(n=>[n,n.inert]);
  for(const [n] of background)n.inert=true;
  const dismiss=()=>{overlay.remove();for(const [n,inert] of background)n.inert=inert;};
  const defer=el('button','暂不继续'),review=el('button',failed?'查看任务记录':'查看并选择继续','primary');
  for(const b of [defer,review])Object.assign(b.style,{font:'inherit',color:'#222d40',border:'1px solid #e1e7f1',borderRadius:'8px',minHeight:'36px',background:'white',padding:'7px 13px',cursor:'pointer',whiteSpace:'nowrap'});
  Object.assign(review.style,{background:'#435ff5',borderColor:'#435ff5',color:'white',fontWeight:'600'});
  defer.addEventListener('click',e=>{if(e.isTrusted)dismiss();});
  review.addEventListener('click',async e=>{
   if(!e.isTrusted)return;dismiss();
   await window.p011Navigate(()=>{if(tasks.length)P011Router.pendingTaskId=tasks[0].id;P011Router.go('tasks');});
  });
  overlay.addEventListener('keydown',e=>{if(e.isTrusted&&e.key==='Escape'){e.preventDefault();dismiss();}});
  foot.append(defer,review);dialog.append(head,body,foot);overlay.append(dialog);document.body.append(overlay);defer.focus();
 };
})();
