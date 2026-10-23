"""Account-isolated metadata read; never selects a declaration/POI or submits posts."""
import hashlib
import threading
import time
from .douyin_capability import DouyinCapabilityWindow, READ_IDENTITY, HOST, DOMAINS
from .douyin_auth import verified_identity
from .cocoa_auth import CocoaAuthProvider
from .cocoa_session import decode_cookies

# Uses observed semantic controls. Generated CSS suffixes are not persisted.
# The client world has no application bridge and does not read site internals.
READ_METADATA = READ_IDENTITY + r'''
if(String(identity.uid)!==expected_uid||String(identity.uid_after)!==expected_uid||identity.status!==0||identity.after_status!==0||identity.profile_status!==0)return {identity,ready:false};
const visible=e=>{if(!e||!e.getClientRects().length)return false;for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);if(s.visibility==='hidden'||s.display==='none'||s.opacity==='0'||p.getAttribute('aria-hidden')==='true'||p.classList.contains('semi-modal-content-animate-hide'))return false;}return true;};
const exact=t=>Array.from(document.querySelectorAll('body *')).find(e=>visible(e)&&e.children.length===0&&e.textContent.trim()===t);
const result={identity,path:location.pathname,ready:false,stage:'等待发布页面'};
const activate=e=>{e.scrollIntoView({block:'center'});e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));e.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));e.click();};
if(location.pathname==='/creator-micro/home'){
 const next=exact('继续编辑')||exact('发布高清视频');if(next)next.click();return result;
}
if(location.pathname==='/creator-micro/content/upload'){
 result.stage='正在上传内置测试片，随后自动读取';
 if(!window.p011MetadataUploadStarted){const upload=exact('上传视频');if(upload){window.p011MetadataUploadStarted=true;activate(upload);}}
 return result;
}
if(location.pathname!=='/creator-micro/content/post/video')return result;
if(kind==='declarations'){
 if(!exact('请选择声明类型（单选）')){const open=exact('请选择自主声明');if(open)open.click();return result;}
 const labels=Array.from(document.querySelectorAll('label')).filter(e=>visible(e)&&e.querySelector('input[type=radio]')).map(e=>e.innerText.trim());
 if(labels.length&&labels.length<=100){result.ready=true;result.labels=labels;}return result;
}
if(exact('请选择声明类型（单选）')){result.stage='关闭声明窗口';const cancel=Array.from(document.querySelectorAll('button')).find(e=>visible(e)&&e.textContent.replace(/\s/g,'')==='取消');if(cancel)activate(cancel.querySelector('span')||cancel);return result;}
const marker=exact('输入地理位置');
let input=marker?.parentElement.querySelector('input')||window.p011MetadataInput;
if(visible(input))window.p011MetadataInput=input;
if(!visible(input)){
 if(marker)activate(marker);result.stage='展开地点';return result;
}
// Force a new search on a new request, including refreshing the same keyword.
if(window.p011MetadataRequest!==request_id){
 window.p011MetadataRequest=request_id;window.p011MetadataWait=Date.now();
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'');
 input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
 result.stage='准备地点搜索';return result;
}
const scopeNode=document.getElementById(scope==='national'?'domestic':'local');
if(!visible(scopeNode)){const trigger=Array.from(document.querySelectorAll('body *')).reverse().find(e=>visible(e)&&e.textContent.replace(/\s/g,'')==='本地国内');if(trigger)activate(trigger);result.stage='展开地点范围';return result;}
if(!scopeNode.querySelector('[class*="item-text-selected-"]')){activate(scopeNode);result.stage='切换地点范围';window.p011MetadataWait=Date.now();return result;}
if(input.value!==query){
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,query);
 input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
 window.p011MetadataWait=Date.now();result.stage='搜索地点';return result;
}
if(!window.p011MetadataWait)window.p011MetadataWait=Date.now();
if(Date.now()-window.p011MetadataWait<1800)return result;
result.stage='等待地点结果';
const options=Array.from(document.querySelectorAll('[role=option]')).filter(visible);
const rows=options.map(e=>({name:e.querySelector('[class^="name-"]')?.innerText.trim(),address:e.querySelector('[class^="address-"]')?.innerText.trim()}));
if(!rows.length&&exact('未搜索到相关位置')){result.ready=true;result.rows=[];result.empty=true;result.query=input.value;result.scope=scope;}
if(rows.length&&rows.every(r=>r.name&&r.address)){
 result.ready=true;result.rows=rows;result.query=input.value;result.scope=scope;
 result.has_more=!!exact('点击加载更多');
}
return result;
'''


def normalize_observation(value, uid, kind, query='', scope='local'):
    if not isinstance(value, dict):return None
    verified_identity(dict(value['identity']), expected_uid=uid)
    if value.get('path')!='/creator-micro/content/post/video' or value.get('ready') is not True:return None
    def key(*parts):
        return 'ui-v1:'+hashlib.sha256('\0'.join(parts).encode()).hexdigest()
    if kind=='declarations':
        labels=list(value.get('labels',[]))
        if not 1<=len(labels)<=100 or len(set(labels))!=len(labels) or any(not isinstance(s,str) or not 1<=len(s)<=500 for s in labels):raise ValueError('invalid declaration labels')
        # No inferred optionality: require an explicit choice, including the site's none option.
        return dict(required=True,items=[dict(id=key(s),label=str(s)) for s in labels])
    if value.get('query')!=query or value.get('scope')!=scope:return None
    rows=[]
    for r in value.get('rows',[]):
        name,address=r.get('name'),r.get('address')
        if not isinstance(name,str) or not 1<=len(name)<=500 or not isinstance(address,str) or not 1<=len(address)<=1000:raise ValueError('invalid location row')
        rows.append(dict(id=key(scope,name,address),name=str(name),address=str(address)))
    if (not rows and value.get('empty') is not True) or len(rows)>1000 or len({r['id'] for r in rows})!=len(rows):raise ValueError('invalid location rows')
    return rows


class MetadataWindow(DouyinCapabilityWindow):
    def __init__(self,*args,kind,query,scope,context,**kwargs):
        super().__init__(*args,**kwargs)
        self.kind,self.query,self.scope=kind,query,scope
        self.context=context
        self.previous=None;self.matches=0;self.suspended=False;self.request_serial=1

    def configure(self,config):
        super().configure(config)
        import WebKit
        if not self.context:
            self.context.update(store=WebKit.WKWebsiteDataStore.nonPersistentDataStore(),pool=WebKit.WKProcessPool.alloc().init())
        config.setWebsiteDataStore_(self.context['store']);config.setProcessPool_(self.context['pool'])

    def show(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None);return
        super().show()
        self.window.setTitle_('一剪即发 · 读取发布信息 · 自动使用内置测试片 · 不发布')

    def observe(self):
        if self.finished or self.view is None or self.busy or self.suspended:return
        if time.monotonic()-self.started>180:self.fail();return
        import WebKit
        self.busy=True;generation=self.generation
        def done(value,error):
            self.busy=False
            if self.finished or generation!=self.generation:return
            if error is not None:
                self.window.setTitle_('读取未完成 · 页面结构需要检查');return
            try:
                raw=dict(value)
                self.window.setTitle_('一剪即发 · '+str(raw.get('stage','读取声明'))+' · 不发布')
                result=normalize_observation(raw,self.expected_uid,self.kind,self.query,self.scope)
            except (TypeError,ValueError,KeyError):return
            if result is None:return
            # Require two identical observations to avoid caching an intermediate search render.
            self.matches=self.matches+1 if result==self.previous else 1;self.previous=result
            if self.matches>=2:self.finish(result,None)
        self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
            READ_METADATA,dict(kind=self.kind,query=self.query,scope=self.scope,expected_uid=self.expected_uid,request_id=str(self.request_serial)),None,WebKit.WKContentWorld.defaultClientWorld(),done)

    def finish(self,value,error):
        if value is not None and error is None:
            self.suspended=True;self.context['window']=self
            from PyObjCTools import AppHelper
            serial=self.request_serial
            def countdown(seconds):
                if self.finished or not self.suspended or self.request_serial!=serial:return
                if seconds<=0:
                    self.finish(None,None)
                    return
                self.window.setTitle_(f'一剪即发 · 信息已读取 · {seconds}秒后关闭 · 不发布')
                AppHelper.callLater(1,countdown,seconds-1)
            countdown(5)
            self.on_closed(value,None)
            return
        self.context.pop('window',None)
        if self.suspended:self.on_closed=lambda *_:None
        self.allow_close=True
        super().finish(value,error)

    def request_close(self):
        if self.allow_close or self.finished:return True
        self.finish(None,None);return False


def make_metadata_provider(engine,paths,expected_uid):
    cancel=threading.Event();lock=threading.Lock();contexts={}
    def fetch(account,kind,query='',scope='local'):
        from yijianjifa.features.accounts.session_store import SessionStore
        if account.platform!='douyin' or not account.platform_user_id or not account.session_ref:raise ValueError('account needs login')
        selected_uid=account.platform_user_id
        if not lock.acquire(blocking=False):raise ValueError('metadata read already running')
        try:
            blob=SessionStore(paths.data_dir/'profiles').get(account.id,ref=account.session_ref)
            cookies=decode_cookies(blob,identity=selected_uid,domains=DOMAINS)
            context=contexts.setdefault((account.id,account.session_ref),{})
            def factory(*args):
                window=context.get('window')
                if window is not None and not window.finished:
                    window.on_closed=args[3];window.kind=kind;window.query=query;window.scope=scope
                    window.request_serial+=1
                    window.suspended=False;window.previous=None;window.matches=0;window.started=time.monotonic()
                    return window
                return MetadataWindow(*args,expected_uid=selected_uid,restored_cookies=cookies,kind=kind,query=query,scope=scope,context=context)
            result=CocoaAuthProvider('https://'+HOST+'/creator-micro/home',{HOST},lambda *a:None,window_factory=factory)(account.id,cancel)
            if result is None:raise ValueError('metadata read cancelled')
            return result
        finally:lock.release()
    def close():
        cancel.set()
        from PyObjCTools import AppHelper
        def cleanup():
            for context in contexts.values():
                window=context.get('window')
                if window is not None and not window.finished:window.cancel()
            contexts.clear()
        AppHelper.callAfter(cleanup)
    fetch.close=close
    return fetch
