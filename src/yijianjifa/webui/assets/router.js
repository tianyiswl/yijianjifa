const P011Views={};
const P011Router={current:null,dispose:null,go(name,context=null){
 if(name==='commerce'&&!P011Features.commerce)return;
 if(!P011Views[name])return;
 this.dispose?.();this.dispose=null;
 document.querySelectorAll('video,audio').forEach(player=>player.pause());
 window.p011Drop=()=>{};window.p011Navigate=fn=>fn();
 document.body.innerHTML=P011Templates[name].body;
 document.body.dataset.view=name;
 document.title='一剪即发 · '+({library:'资料库',mixing:'混剪制作',finished:'成品库',presets:'常用方案与设置',tasks:'任务记录',accounts:'账号管理',publish:'普通发布',multi_publish:'多平台分发',commerce:'团购带货',voices:'配音管理',downloads:'视频下载'}[name]||name);
 document.head.querySelectorAll('style').forEach(style=>style.remove());
 const style=document.createElement('style');style.textContent=P011Templates[name].css;document.head.append(style);
 this.current=name;const disposeView=P011Views[name](context)||(()=>{});P011ApplyFeatures();const disposeTerm=P011UseTerm(),disposeNotifications=P011Notifications(),disposeIntervention=P011PublishIntervention(),disposeProgress=P011PublishProgress(),disposeDatePicker=P011DatePicker();this.dispose=()=>{disposeTerm();disposeDatePicker();disposeNotifications();disposeIntervention();disposeProgress();disposeView();};
}};
