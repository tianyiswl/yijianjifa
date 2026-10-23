"""Native ordinary form preparation, deliberately without any submit action.

Semantic controls observed on the authorized creator form on 2026-09-29.
A ready form is not a published video or a platform receipt.
"""
from .douyin_capability import READ_IDENTITY
from .douyin_topics import compose_description

TOPIC_RECOVERY = r'''
// Re-query only the unconfirmed suffix; preserve existing mention nodes.
const recoverPendingTopic=(editor,name,state,now)=>{
 if(now-state.last<8000)return 'waiting';
 if(state.attempts>=3)return 'failed';
 const leaves=Array.from(editor.querySelectorAll('[data-string]:not([data-enter])')).filter(e=>!e.closest('[contenteditable="false"]'));
 const node=leaves.at(-1)?.lastChild;
 if(!node||node.nodeType!==Node.TEXT_NODE||!node.textContent.endsWith(name))return 'unsafe';
 editor.focus();
 const range=document.createRange(),selection=window.getSelection();
 // Replace the final character with itself to trigger the editor's real input
 // handling without rebuilding its rich-text document or dropping mentions.
 const tail=Array.from(name).at(-1);
 range.setStart(node,node.length-tail.length);range.setEnd(node,node.length);
 selection.removeAllRanges();selection.addRange(range);
 document.execCommand('insertText',false,tail);
 state.last=now;state.attempts+=1;return 'retry';
};
'''

PREPARE_FORM = READ_IDENTITY + TOPIC_RECOVERY + r'''
if(String(identity.uid)!==expected_uid||String(identity.uid_after)!==expected_uid||identity.status!==0||identity.after_status!==0||identity.profile_status!==0)return {ready:false,stage:'账号身份不符'};
const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none'&&!e.closest('[aria-hidden="true"],.semi-modal-content-animate-hide');
const exact=t=>Array.from(document.querySelectorAll('body *')).filter(e=>visible(e)&&e.children.length===0&&e.textContent.trim()===t);
const click=t=>{const a=exact(t);if(a.length!==1)return false;a[0].click();return true;};
const result={identity,path:location.pathname,ready:false,stage:'等待视频编辑页'};
if(location.pathname==='/creator-micro/content/upload'){
 result.stage='上传内置测试片';if(!window.p011PrepareUpload){if(click('上传视频'))window.p011PrepareUpload=true;}return result;
}
if(location.pathname!=='/creator-micro/content/post/video')return result;
const titles=Array.from(document.querySelectorAll('input[placeholder="填写作品标题，为作品获得更多流量"]')).filter(visible);
const editors=Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(visible);
result.stage='核验标题和简介控件';result.title_count=titles.length;result.editor_count=editors.length;
if(titles.length!==1||editors.length!==1)return result;
const title=titles[0],editor=editors[0];
// Dismiss only the observed, non-binding video-preview onboarding notice.
if(exact('视频预览功能').length&&click('我知道了')){result.stage='关闭视频预览引导';return result;}
const clean=s=>s.replace(/\u200b/g,'').replace(/\r\n/g,'\n').trim();
if(title.value!==title_text){
 const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
 setter.call(title,title_text);title.dispatchEvent(new Event('input',{bubbles:true}));title.dispatchEvent(new Event('change',{bubbles:true}));
 result.stage='填写标题';return result;
}
const topics=typeof topic_names==='undefined'?[]:topic_names;
const topicNorm=s=>clean(s).replace(/\s+/g,' ');
const readTopics=()=>Array.from(editor.querySelectorAll('[data-mention="#"]')).map(e=>e.textContent.trim().replace(/^#/,''));
if(topics.length){
 const found=readTopics();
 if(found.some((name,i)=>name!==topics[i])||found.length>topics.length){result.stage='话题与安排不符';return result;}
 if(!window.p011TopicsInitialized&&!found.length){
  if(topicNorm(editor.innerText)===topicNorm(plain_body_text)){window.p011TopicsInitialized=true;result.stage='简介已核对';return result;}
  editor.focus();await new Promise(resolve=>setTimeout(resolve,50));
  if(topicNorm(editor.innerText)){
   document.execCommand('selectAll',false,null);document.execCommand('delete',false,null);editor.blur();result.stage='清空待替换简介';return result;
  }
  document.execCommand('insertText',false,plain_body_text);editor.blur();result.stage='填写话题前的简介';return result;
 }
 if(found.length<topics.length){
  const next=topics[found.length],prefix=topicNorm([plain_body_text,...found.map(n=>'#'+n)].join(' ')),text=topicNorm(editor.innerText);
  if(text===prefix){
   const leaves=Array.from(editor.querySelectorAll('[data-string]:not([data-enter])')).filter(e=>!e.closest('[contenteditable="false"]'));
   const leaf=leaves.at(-1),node=leaf?.lastChild,selection=window.getSelection();
   if(!node||node.nodeType!==Node.TEXT_NODE){result.stage='等待可编辑的话题光标';return result;}
   const cursor=window.p011TopicCursor;
   if(!cursor||cursor.name!==next||cursor.node!==node||document.activeElement!==editor||!selection.isCollapsed||selection.anchorNode!==node||selection.anchorOffset!==node.length){
    editor.focus();const range=document.createRange();range.setStart(node,node.length);range.collapse(true);selection.removeAllRanges();selection.addRange(range);
    window.p011TopicCursor={name:next,node};result.stage='定位话题光标：'+next;return result;
   }
   window.p011TopicCursor=null;
   window.p011TopicWait={name:next,last:Date.now(),attempts:0};
   document.execCommand('insertText',false,' #'+next);result.stage='输入话题：'+next;return result;
  }
  if(text!==topicNorm(prefix+' #'+next)){result.stage='话题输入内容不符';return result;}
  const options=Array.from(document.querySelectorAll('[class*="tag-hash-view-name-"]')).filter(e=>visible(e)&&!editor.contains(e)&&e.textContent.trim()===next);
  if(options.length!==1){
   let wait=window.p011TopicWait;
   if(!wait||wait.name!==next)wait=window.p011TopicWait={name:next,last:Date.now(),attempts:0};
   const recovery=recoverPendingTopic(editor,next,wait,Date.now());
   if(recovery==='failed'||recovery==='unsafe'){
    result.failure_code='submission_topic_unavailable';result.stage='话题未能识别：'+next+'，请修改话题后重试';return result;
   }
   result.stage=(recovery==='retry'?'重新搜索话题：':'等待完全匹配的话题：')+next;return result;
  }
  window.p011TopicWait=null;
  options[0].click();result.stage='选择话题：'+next;return result;
 }
 if(topicNorm(editor.innerText)!==topicNorm(body_text)){result.stage='简介与话题回读不符';return result;}
 result.topics=found;
}
if(!topics.length&&clean(editor.innerText)!==clean(body_text)){
 editor.focus();const selection=window.getSelection(),range=document.createRange();range.selectNodeContents(editor);selection.removeAllRanges();selection.addRange(range);
 document.execCommand('insertText',false,body_text);editor.blur();result.stage='填写简介';return result;
}
if(!declaration_label&&!exact('请选择自主声明').length){result.stage='未能确认声明为空';return result;}
if(exact('请选择声明类型（单选）').length){
 const labels=Array.from(document.querySelectorAll('label')).filter(e=>visible(e)&&e.querySelector('input[type=radio]')&&e.innerText.trim()===declaration_label);
 if(labels.length!==1){result.stage='声明选项不可用';return result;}
 const radio=labels[0].querySelector('input[type=radio]');
 if(!radio.checked){labels[0].click();result.stage='选择声明';return result;}
 const buttons=Array.from(document.querySelectorAll('button')).filter(e=>visible(e)&&e.textContent.trim()==='确定');
 if(buttons.length===1)buttons[0].click();result.stage='确认声明';return result;
}
if(declaration_label&&!exact(declaration_label).length){
 click('请选择自主声明');result.stage='打开声明';return result;
}
const scheduled=typeof scheduled_time_text==='string'&&scheduled_time_text.length>0;
const modeLabel=scheduled?'定时发布':'立即发布';
const modes=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&['radio','checkbox'].includes(e.type)&&e.closest('label')?.innerText.trim()===modeLabel);
result.mode_controls=modes.length;
if(modes.length!==1){result.stage='发布时间控件待适配';return result;}
if(!modes[0].checked){modes[0].click();result.stage='设置发布时间方式';return result;}
if(scheduled){
 const dates=Array.from(document.querySelectorAll('input[placeholder="日期和时间"]')).filter(visible);
 if(dates.length!==1){result.stage='日期控件待适配';return result;}
 if(dates[0].value!==scheduled_time_text){
  dates[0].focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(dates[0],scheduled_time_text);
  dates[0].dispatchEvent(new Event('input',{bubbles:true}));dates[0].dispatchEvent(new Event('change',{bubbles:true}));dates[0].blur();
  result.stage='填写定时时间';return result;
 }
 result.scheduled_time_text=dates[0].value;
}

const targetLocation=typeof location_selection==='undefined'?null:location_selection;
const locationControls=Array.from(document.querySelectorAll('#douyin_creator_pc_anchor_jump .semi-select-filterable.semi-select-no-arrow')).filter(visible);
if(targetLocation){
 result.stage='核对发布地点';
 if(locationControls.length!==1)return result;
 const control=locationControls[0],input=control.querySelector('input');
 if(!visible(input)){control.click();result.stage='展开发布地点';return result;}
 const national=document.getElementById('domestic');
 if(!visible(national)){result.stage='等待地点范围';return result;}
 if(!national.querySelector('[class*="item-text-selected-"]')){national.click();window.p011LocationWait=Date.now();result.stage='切换国内地点';return result;}
 if(input.value!==targetLocation.name){
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,targetLocation.name);
  input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
  window.p011LocationWait=Date.now();result.stage='搜索发布地点：'+targetLocation.name;return result;
 }
 if(!window.p011LocationWait)window.p011LocationWait=Date.now();
 if(Date.now()-window.p011LocationWait<1800)return result;
 const options=Array.from(document.querySelectorAll('[role="option"]')).filter(visible);
 const matches=options.filter(e=>e.querySelector('[class^="name-"]')?.innerText.trim()===targetLocation.name&&e.querySelector('[class^="address-"]')?.innerText.trim()===targetLocation.address);
 if(matches.length!==1){
  result.stage='等待名称及地址完全匹配的地点：'+targetLocation.name;
  if(matches.length>1||Date.now()-window.p011LocationWait>15000){result.failure_code='submission_location_unavailable';result.stage='地点未找到或存在歧义，请刷新地点后重试';}
  return result;
 }
 if(!matches[0].classList.contains('semi-select-option-selected')){matches[0].click();result.stage='选择发布地点：'+targetLocation.name;return result;}
 result.location={name:matches[0].querySelector('[class^="name-"]').innerText.trim(),address:matches[0].querySelector('[class^="address-"]').innerText.trim()};
}else if(locationControls.length===1){
 const selected=locationControls[0].querySelector('.semi-select-selection-text');
 if(selected&&selected.textContent.trim()&&selected.textContent.trim()!=='输入地理位置'){result.failure_code='submission_location_mismatch';result.stage='页面存在未安排的发布地点';return result;}
}
const finalButtons=Array.from(document.querySelectorAll('button')).filter(e=>visible(e)&&e.textContent.trim()==='发布');
result.title=title.value;result.body=clean(editor.innerText);result.declaration=declaration_label;
result.final_button_count=finalButtons.length;result.final_button_enabled=finalButtons.length===1&&!finalButtons[0].disabled;
result.upload_complete=window.p011UploadCompleted===true;
result.ready=result.upload_complete&&result.final_button_enabled;result.stage=result.ready?'表单已核对，未发布':'等待上传完成';
return result;
'''


def validate_form_request(*, expected_uid, title, body, declaration, scheduled_time_text=None, now=None, tags="", location=None):
    if not isinstance(expected_uid,str) or not expected_uid.isdecimal():raise ValueError('verified identity required')
    if not isinstance(title,str) or not 1<=len(title)<=30:raise ValueError('发布标题须为1～30个字符，请检查是否为空或超过30字。')
    if not isinstance(body,str) or len(body)>1000:raise ValueError('body too long')
    description,topics=compose_description(body,tags)
    if not isinstance(declaration,str) or not 0<=len(declaration)<=100:raise ValueError('invalid declaration')
    selected_location=None
    if location:
        if not isinstance(location,dict) or any(not isinstance(location.get(k),str) or not 1<=len(location[k])<=1000 for k in ('name','address')):raise ValueError('invalid location')
        selected_location={k:location[k] for k in ('name','address')}
    if scheduled_time_text is not None:
        from datetime import datetime, timezone, timedelta
        zone=timezone(timedelta(hours=8))
        when=datetime.strptime(scheduled_time_text,'%Y-%m-%d %H:%M').replace(tzinfo=zone)
        current=now or datetime.now(timezone.utc)
        if current.tzinfo is None or not current+timedelta(hours=2.5)<=when<=current+timedelta(days=13):
            raise ValueError('schedule must be 2.5 hours to 13 days ahead')
    return dict(location_selection=selected_location,expected_uid=expected_uid,title_text=title,body_text=description,plain_body_text=body,topic_names=topics,declaration_label=declaration,
                scheduled_time_text=scheduled_time_text or '')


def verify_prepared_form(value, request):
    """Only accept an exact readback bound to the host's approved request."""
    from .douyin_auth import verified_identity
    if not isinstance(value,dict):raise ValueError('form observation unavailable')
    verified_identity(dict(value.get('identity',{})),expected_uid=request['expected_uid'])
    if value.get('ready') is not True or value.get('path')!='/creator-micro/content/post/video':
        raise ValueError('form not ready')
    expected_body=request['body_text'].replace('\u200b','').replace('\r\n','\n').strip()
    expected={'title':request['title_text'],'body':expected_body,'declaration':request['declaration_label'],
              'title_count':1,'editor_count':1,'mode_controls':1,'final_button_count':1,
              'upload_complete':True,'final_button_enabled':True}
    if request.get('location_selection'):expected['location']=request['location_selection']
    if request.get('scheduled_time_text'):expected['scheduled_time_text']=request['scheduled_time_text']
    if request.get('topic_names'):
        import re
        normalize=lambda s:re.sub(r'\s+',' ',s.replace('\u200b','')).strip()
        if value.get('topics')!=request['topic_names'] or not isinstance(value.get('body'),str):raise ValueError('topic readback mismatch')
        value={**value,'body':normalize(value['body'])};expected['body']=normalize(expected_body)
    if any(value.get(k)!=v for k,v in expected.items()):raise ValueError('form readback mismatch')
    return dict(title=value['title'],body=value['body'],declaration=value['declaration'],
                mode='platform_schedule' if request.get('scheduled_time_text') else 'publish_now',prepared=True,submitted=False)


from .douyin_capability import DouyinCapabilityWindow


class DouyinTestFormWindow(DouyinCapabilityWindow):
    """Fixture-only native preparation; retain the verified form for review.

    Inherits account isolation and the hash-checked, single-use file chooser.
    Never implements a final action or enables an existing queued task.
    """
    def __init__(self,*args,request,on_prepared,**kwargs):
        super().__init__(*args,**kwargs)
        self.form_request=validate_form_request(expected_uid=self.expected_uid,
            title=request['title'],body=request['body'],declaration=request['declaration'])
        self.on_prepared=on_prepared
        self.prepared=False
        self.last_stage=None
        self.repeats=0

    def observe(self):
        import time
        if self.finished or self.prepared or self.view is None or self.busy:return
        if time.monotonic()-self.started>180:self.fail();return
        import WebKit
        self.busy=True
        generation=self.generation
        def done(value,error):
            self.busy=False
            if self.finished or generation!=self.generation:return
            if error is not None:
                self.window.setTitle_('一剪即发 · 表单读取未完成 · 不发布');return
            try:
                raw=dict(value)
                stage=str(raw.get('stage','等待表单'))
                self.repeats=self.repeats+1 if stage==self.last_stage else 1
                self.last_stage=stage
                self.window.setTitle_('一剪即发 · '+stage+' · 不发布')
                if raw.get('ready') is True:
                    verified=verify_prepared_form(raw,self.form_request)
                    if self.timer:self.timer.invalidate();self.timer=None
                    self.prepared=True
                    self.on_prepared(verified)
                elif stage in {'填写标题','填写简介','选择声明','确认声明'} and self.repeats>=5:
                    self.fail()
            except (KeyError,TypeError,ValueError):self.fail()
        self.view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
            PREPARE_FORM,self.form_request,None,WebKit.WKContentWorld.defaultClientWorld(),done)
