(function(root){'use strict';
class AccountLogin{
 // The caller already requested login/backend access. A native login handoff
 // may show that same bound page; it never resumes the publishing target.
 static async handleBusy(account_id,{api,ask,openVerification,alive=()=>true}){
  if(typeof account_id!=='string'||!account_id||!alive())return false;
  let request=null;
  try{request=(await api('multi_publish.intervention.status',{account_id}))?.request;}catch{}
  if(!alive())return false;
  if(request?.account_id!==account_id){
   request=null;
   try{const legacy=(await api('publish.intervention.status',{}))?.request;if(legacy?.account_id===account_id&&legacy.browser_login===true&&!legacy.delivery_id)request=legacy;}catch{}
  }
  if(!alive())return false;
  const delivery=request?.account_id===account_id&&typeof request.delivery_id==='string'&&/^[A-Za-z0-9_-]{1,64}$/.test(request.delivery_id)&&!request.inspection_id&&typeof openVerification==='function'?request.delivery_id:null;
  const native=request?.account_id===account_id&&request.browser_login===true&&!request.final_started&&!request.inspection_id&&typeof request.id==='string'&&/^[A-Za-z0-9_-]{1,96}$/.test(request.id)&&typeof openVerification==='function'&&(delivery||!request.delivery_id);
  if(native){
   try{
    const opened=await openVerification(delivery||{request_id:request.id,account_id});
    if(!alive())return true;
    if(!opened)throw Error('original request unavailable');
    const result=await api((delivery?'multi_publish.':'publish.')+'intervention.respond',{id:request.id,...(delivery?{delivery_id:delivery}:{}),action:'visible'});
    if(result?.accepted!==true)throw Error('original browser not accepted');
   }catch{
    if(!alive())return true;
    if(await ask('登录页面未能打开','请重新打开原登录页面。原发布任务保持暂停，打开页面不会继续发布。',[['关闭',false],['重新打开',true,'btn primary']])){
     // A new login/challenge may have replaced the original nonce while its
     // page opened. Only a fresh account-bound read can authorize this retry.
     return AccountLogin.handleBusy(account_id,{api,ask,openVerification,alive});
    }
   }
   return true;
  }
  if(!delivery){await ask('账号正在处理任务','该账号正在登录、发布或处理验证，当前未打开新的登录窗口。请稍后重试；有待处理任务时，可到“任务记录”继续。',[['知道了',false]]);return true;}
  const chosen=await ask('账号有待完成的验证','该账号有一条未完成的验证，原页面仍在保留。请先处理这次验证，再重新登录。',[['关闭',false],['处理验证',true,'btn primary']]);
  if(chosen&&alive()){
   let opened=false;try{opened=await openVerification(delivery);}catch{}
   if(!opened&&alive())await ask('验证页面暂时无法打开','请到“任务记录”查看该账号的任务状态，再决定是否继续。',[['知道了',false]]);
  }
  return true;
 }
 constructor(api,progress,wait=()=>new Promise(resolve=>setTimeout(resolve,300))){this.api=api;this.progress=progress;this.wait=wait;this.id=null;this.cancelled=false;this.cancelSent=false;}
 async cancel(){this.cancelled=true;if(this.id&&!this.cancelSent){this.cancelSent=true;try{await this.api('account.login.cancel',{id:this.id});}catch(error){this.cancelSent=false;throw error;}}}
 async run(account_id,platform){let result=await this.api('account.login.start',{account_id,...(platform===undefined?{}:{platform})});this.id=result.id;
  try{for(;;){if(this.cancelled){await this.cancel();return {state:'cancelled'};}this.progress(result);if(['completed','cancelled','failed'].includes(result.state))return result;if(result.state!=='waiting')throw Error('登录状态无法确认，请检查账号列表。');await this.wait();result=await this.api('account.login.status',{id:this.id});}}
  catch(error){try{await this.cancel();}catch(ignored){}throw error;}
 }
}
if(typeof module!=='undefined'&&module.exports)module.exports={AccountLogin};else root.P011AccountLogin=AccountLogin;
})(globalThis);
