"""B站网页登录表单契约；只读页面状态与正常请求，不改平台 store。

Checked against official videoup 02c1a54b assets on 2026-10-09. Category
and creation statement are explicit user choices, never inferred originality.
"""
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email import policy
import re
from urllib.parse import urlsplit
from yijianjifa.app_services import bilibili_static_catalog as catalog

UPLOAD_URL='https://member.bilibili.com/platform/upload/video/frame'
MANAGE_URL='https://member.bilibili.com/platform/upload-manager/article'
TITLE='.video-title input'
BODY='.desc-container .archive-info-editor .ql-editor[contenteditable="true"]'
TAGS='#tag-container input'
FINAL='.submit-container .submit-add'
VIDEO_FILE='.video-entrance .upload-wrp .upload input[type="file"]'
COVER_FILE='.cover-editor .cover-upload input[type="file"]'
COVER_OPEN='.cover-content .cover-module-main .cover-slot'
COVER_DONE='.cover-editor .cover-editor-button .button.submit:not(.submit-disabled)'
SCHEDULE_CONTAINER='.video-basic .form .time-container:not(.time-release-dialog .time-container):not(.batch-fill .time-container):not(.batch-dialog .time-container):not(.bcc-dialog__wrap .time-container)'


def compact(value):return ' '.join(value.split()) if type(value) is str else ''


def utf16_length(value):return len(value.encode('utf-16-le'))//2


def safe_id(value):
    if type(value) is int and 0<value<=9007199254740991:return str(value)
    return value if type(value) is str and re.fullmatch(r'[1-9][0-9]{0,15}',value) else None


def receipt_reference(value):
    if type(value) is not dict:return {}
    aid=safe_id(value.get('aid'));bvid=value.get('bvid')
    result={}
    # The host receipt vocabulary is platform-neutral. These remain exact
    # aid/bvid references, never client-created IDs or inferred success.
    if aid:result['work_id']=aid
    if type(bvid) is str and re.fullmatch(r'BV[1-9A-HJ-NP-Za-km-z]{10}',bvid):result['publish_id']=bvid
    return result


def validate_form(payload,*,query=False,now=None,local_choices=False,options=None):
    # Local intent has no invented posting ID. Only the original normal page's
    # unique current options can resolve it for actual UI actions and final.
    f=catalog.resolve_fields(payload['fields'],options,local=local_choices)
    if f.get('location') is not None or f.get('declaration') is not None:
        raise ValueError('B站地点或通用声明尚未接通')
    title=f.get('title','');body=f.get('body','');tags=f.get('tags','')
    if any(type(v) is not str for v in (title,body,tags)) or not title.strip() or not 1<=utf16_length(title)<=80 or utf16_length(body)>2000:
        raise ValueError('B站标题须为1至80字，简介不能超过2000字')
    names=[s.lstrip('#') for s in tags.split()]
    if not 1<=len(names)<=10 or len(set(names))!=len(names) or any(not s or utf16_length(s)>20 or '#' in s or ',' in s for s in names):
        raise ValueError('B站需要1至10个标签，每个最多20字')
    category=f.get('category_id');mode=f.get('category_mode');name=f.get('category_name')
    local_category=local_choices and 'category_choice' in f and category is None
    if (not local_category and (type(category) is not int or category<=0)) or type(mode) is not str or mode not in {'legacy','human'} or type(name) is not str or not 1<=len(name)<=100:
        raise ValueError('请选择后台真实分区')
    statement=f.get('creation_statement_id');statement_name=f.get('creation_statement_name')
    local_statement=local_choices and 'creation_statement_choice' in f and statement is None
    if (not local_statement and (type(statement) is not int or not (statement in {-1,-2} or statement>0))) or type(statement_name) is not str or not 1<=len(statement_name)<=255:
        raise ValueError('请选择后台真实创作声明')
    authorization=f.get('content_authorization',False);source=f.get('reprint_source','')
    if type(authorization) is not bool or type(source) is not str or utf16_length(source)>200:
        raise ValueError('内容授权或转载来源无效')
    if statement==-2 and (authorization or not source.strip()):raise ValueError('转载须填写来源，不能勾选自制内容授权')
    if statement!=-2 and source:raise ValueError('非转载声明不应包含转载来源')
    cover=f.get('cover') or {'kind':'first_frame','frame_ms':0}
    if cover.get('kind')!='first_frame' or cover.get('frame_ms',0)!=0:raise ValueError('B站目前支持视频第一帧封面')
    mode_execution=payload['execution_mode'];dtime=0;date='';clock=''
    if type(mode_execution) is not str or mode_execution not in {'publish_now','software_schedule','platform_schedule'}:raise ValueError('发布方式无效')
    if mode_execution=='platform_schedule':
        when=datetime.fromisoformat(payload['scheduled_at'])
        if when.tzinfo is None or when.second or when.microsecond or when.minute%5:
            raise ValueError('B站定时需带时区并按5分钟刻度选择')
        if not query and when<= (now or datetime.now(timezone.utc)):raise ValueError('B站定时时间已过期')
        dtime=int(when.timestamp());local=when.astimezone(timezone(timedelta(hours=8)))
        date=local.strftime('%Y-%m-%d');clock=local.strftime('%H:%M')
    return dict(title=title,body=body,tags=names,category_id=category,category_mode=mode,category_name=name,
        creation_statement_id=statement,creation_statement_name=statement_name,content_authorization=authorization,
        copyright=2 if statement==-2 else 1 if authorization else 3,source=source,dtime=dtime,date=date,clock=clock)


def safe_options(pre,human=None,*,now=None):
    if type(pre) is not dict:return None
    mode='human' if pre.get('type_mode') else 'legacy';categories=[]
    if mode=='legacy':
        groups=pre.get('typelist',[])
        if type(groups) is not list or len(groups)>100:return None
        for parent in groups:
            if type(parent) is not dict:continue
            children=parent.get('children',[])
            if type(children) is not list or len(children)>200:continue
            for item in children:
                if (type(item) is dict and type(item.get('id')) is int and item['id']>0 and type(item.get('name')) is str
                        and 1<=len(item['name'])<=100 and type(parent.get('name')) is str and 1<=len(parent['name'])<=100):
                    categories.append(dict(id=item['id'],name=item['name'],mode=mode,parent_name=parent['name']))
    else:
        values=human if type(human) is list else []
        for item in values[:500]:
            if type(item) is dict and type(item.get('id')) is int and item['id']>0 and type(item.get('name')) is str and 1<=len(item['name'])<=100:
                categories.append(dict(id=item['id'],name=item['name'],mode=mode,parent_name=''))
    mark=pre.get('neutral_mark');statements=[]
    if type(mark) is dict and type(mark.get('mark_list')) is list:
        for item in mark['mark_list'][:100]:
            if (type(item) is dict and type(item.get('id')) is int and (item['id'] in {-1,-2} or item['id']>0)
                    and type(item.get('content')) is str and 1<=len(item['content'])<=255):
                statements.append({'id':item['id'],'name':item['content']})
    info=pre.get('myinfo');limits=None;current=now or int(datetime.now(timezone.utc).timestamp())
    if type(info) is dict and type(info.get('deftime')) is int and type(info.get('deftime_end')) is int:
        low=info['deftime']-current;high=info['deftime_end']-current
        if 0<=low<high<=31*86400:limits=dict(min_lead_seconds=low,max_lead_seconds=high,minute_step=5,timezone='Asia/Shanghai')
    return dict(categories=categories,statements=statements,category_mode=mode,schedule=limits)


def verify_options(options,f):
    if type(options) is not dict:return False
    categories=[x for x in options.get('categories',[]) if x.get('id')==f['category_id'] and x.get('name')==f['category_name'] and x.get('mode')==f['category_mode']]
    statements=[x for x in options.get('statements',[]) if x.get('id')==f['creation_statement_id'] and x.get('name')==f['creation_statement_name']]
    return len(categories)==len(statements)==1


# Read only existing component values. Native DOM checks and Vue values must
# agree before final; no methods, mutation, cookie or private endpoint calls.
READ_FORM=r'''() => {
 const roots=[document]; for(let i=0;i<roots.length&&i<20;i++) for(const e of roots[i].querySelectorAll('micro-app')) if(e.shadowRoot) roots.push(e.shadowRoot);
 const all=q=>roots.flatMap(r=>[...r.querySelectorAll(q)]);
 const vis=e=>!!(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
 const one=q=>{const es=all(q).filter(vis);return es.length===1?es[0]:null;};
 const title=one('.video-title input'),body=one('.desc-container .archive-info-editor .ql-editor[contenteditable="true"]');
 const element=one('.creation-statement-container'),entrance=one('.video-entrance');
 const uploader=one('.video-entrance .upload-wrp .upload'),entranceVm=entrance?.__vue__,uploaderVm=uploader?.__vue__;
 const uploadEnabledKnown=typeof entranceVm?.uploadDisabled==='boolean'&&typeof uploaderVm?.disabled==='boolean';
 // BccUpload's hidden input has no disabled attribute. Its component controls
 // ordinary clicks, so setting that input must not bypass the native gate.
 const uploadEnabled=uploadEnabledKnown&&entranceVm.uploadDisabled===false&&uploaderVm.disabled===false&&
   !uploader.classList.contains('bcc-upload-disabled');
 let vm=element?.__vue__||entrance?.__vue__,store=null,config=null;
 for(let i=0;vm&&i<30;i++,vm=vm.$parent){if(vm.$store)store=vm.$store;if(vm.$taskConfig)config=vm.$taskConfig;}
 const id=config?.currentTaskId,task=Array.isArray(config?.tasks)?config.tasks.find(x=>x.id===id):null;
 const state=store?.state?.['task_'+id],archive=task?.archive||{},videos=task?.videoTasks||[];
 // A local recovery prompt is separate from an active/editing task. The
 // official ordinary entrance keeps its native uploader enabled and creates
 // a fresh task without recovery(false), which would erase local records.
 // Require observed empty task config and explicit normal-mode flags; an
 // uninitialized shell must never grant upload authority by default.
 const rootState=store?.state,from=rootState?.from,object=v=>!!v&&typeof v==='object'&&!Array.isArray(v);
 const route=new URLSearchParams(location.search);
 const editSource=v=>object(v)&&(v.type==='edit'||v.type==='draft'||v.type==='highlight-slice'||v.page==='interactive'||
   (v.cid&&v.filename)||
   ['aid','bvid','draftId','draft_id'].some(k=>v[k]!==undefined&&v[k]!==null&&v[k]!==''));
 const routeBlocked=/interactive/.test(location.pathname)||route.get('type')==='edit'||route.get('type')==='draft'||
   route.get('type')==='highlight-slice'||(route.get('cid')&&route.get('filename'))||
   route.get('page')==='interactive'||['aid','bvid','draftId','draft_id'].some(k=>route.has(k));
 const activeForm=all('.creation-statement-container').filter(vis).length>0||all('.video-title input').filter(vis).length>0||
   all('.desc-container .archive-info-editor .ql-editor[contenteditable="true"]').filter(vis).length>0||
   (Array.isArray(config?.tasks)&&config.tasks.length>0)||(id!==undefined&&id!==null);
 const modeBlocked=rootState?.isInteractive===true||rootState?.isEditMode===true||rootState?.isEditDraftMode===true||editSource(from)||routeBlocked;
 const normalEmpty=object(rootState)&&object(from)&&rootState.isInteractive===false&&rootState.isEditMode===false&&
   rootState.isEditDraftMode===false&&Array.isArray(config?.tasks)&&config.tasks.length===0&&id===null;
 const newUploadState=modeBlocked?'unsupported_mode':activeForm?'active_form':
   (entrance&&all('.video-entrance').filter(vis).length===1&&normalEmpty&&uploadEnabled?'ready':'unavailable');
 const mode=all('.video-human-type').filter(vis).length===1?'human':'legacy';
 const category=one(mode==='human'?'.video-human-type .select-item-cont':'.video-type .select-item-cont');
 const scheduleCandidates=all('.time-container').filter(vis),regularSchedules=all('.video-basic .form .time-container:not(.time-release-dialog .time-container):not(.batch-fill .time-container):not(.batch-dialog .time-container):not(.bcc-dialog__wrap .time-container)').filter(vis);
 const schedule=scheduleCandidates.length===1&&regularSchedules.length===1?regularSchedules[0]:null;
 const scheduleOne=selector=>{const values=schedule?Array.from(schedule.querySelectorAll(selector)).filter(vis):[];return values.length===1?values[0]:null;};
 const switcher=scheduleOne('.switch-container'),date=scheduleOne('.date-picker-date .date-show'),clock=scheduleOne('.date-picker-timer .date-show');
 const buttons=all('.submit-container .submit-add').filter(vis),source=one('.statement-source input');
 const cover=state?.mainCover?.main,coverRef=cover?.edited||cover?.origin||'';
 const timeVm=scheduleOne('.d-time-container')?.__vue__;
 let releaseVm=timeVm;
 for(let i=0;releaseVm&&i<15;i++,releaseVm=releaseVm.$parent){if(releaseVm.$options?.name==='TimeRelease')break;}
 const observedDtime=releaseVm?.$options?.name==='TimeRelease'?releaseVm.dtime:state?.dtime??archive.dtime;
 return {title:title?.value||'',title_count:all('.video-title input').filter(vis).length,body:body?.innerText?.replace(/\n$/,'')||'',body_count:all('.desc-container .archive-info-editor .ql-editor[contenteditable="true"]').filter(vis).length,
  tags:all('#tag-container .tag-pre-wrp .label-item-v2-content').map(e=>e.innerText.trim()),
  category_id:mode==='human'?state?.human_type2:archive.tid,category_mode:mode,category_text:category?.innerText?.trim()||'',
  creation_statement_id:state?.creationStatementId,copyright:state?.copyright,source:source?.value||'',
  statement_text:element?.querySelector('.bcc-select-input-inner')?.value||'',
  content_authorization:state?.copyright===1,cover_reference:typeof coverRef==='string'?coverRef:'',
  upload_complete:videos.length===1&&videos[0].status===3&&all('.file-item-content-status-text .success').filter(vis).some(e=>e.innerText.trim()==='上传完成'),
  videos:videos.slice(0,2).map(x=>({filename:x.filename,cid:x.cid,status:x.status})),
  existing_draft:all('.entrance-tip').filter(vis).some(e=>/未提交|上次.*稿件/.test(e.innerText)),
  new_upload_state:newUploadState,new_upload_ready:newUploadState==='ready',
  native_upload_enabled_known:uploadEnabledKnown,native_upload_enabled:uploadEnabled,
  native_task_count:Array.isArray(config?.tasks)?Math.min(config.tasks.length,2):-1,
  native_task_present:Array.isArray(config?.tasks)&&config.tasks.length===1&&!!task&&task===config.tasks[0],
  native_video_count:Array.isArray(videos)?Math.min(videos.length,2):-1,
  native_video_status:videos.length===1&&Number.isInteger(videos[0].status)&&videos[0].status>=0&&videos[0].status<=10?videos[0].status:-1,
  dtime:observedDtime??0,time_enabled:switcher?switcher.classList.contains('switch-container-active'):null,date:date?.innerText.trim()||'',clock:clock?.innerText.trim()||'',timezone:timeVm?.timezone||'',
  button_count:buttons.length,button_enabled:buttons.length===1&&!buttons[0].classList.contains('submit-add-disabled')&&!buttons[0].classList.contains('btn-loading')&&buttons[0].innerText.trim()==='立即投稿'};
}'''


def verify_form(raw,f):
    if type(raw) is not dict:return False
    if any(type(raw.get(key)) is not int for key in ('category_id','creation_statement_id','copyright','dtime')):return False
    return (raw.get('title')==f['title'] and raw.get('title_count')==1 and raw.get('body')==f['body'] and raw.get('body_count')==1
        and raw.get('tags')==f['tags'] and raw.get('category_id')==f['category_id'] and raw.get('category_mode')==f['category_mode']
        and raw.get('creation_statement_id')==f['creation_statement_id'] and raw.get('copyright')==f['copyright']
        and raw.get('content_authorization') is f['content_authorization'] and raw.get('source','')==f['source']
        and bool(raw.get('cover_reference')) and raw.get('upload_complete') is True and raw.get('button_count')==1 and raw.get('button_enabled') is True
        and raw.get('dtime',0)==f['dtime'] and raw.get('time_enabled') is bool(f['dtime'])
        and (not f['dtime'] or (raw.get('date')==f['date'] and raw.get('clock')==f['clock'] and raw.get('timezone')=='Asia/Shanghai')))


def form_diagnostic(raw,f):
    """Finite readback differences; never include text, media IDs or dates."""
    if type(raw) is not dict:return {'readback_available':False}
    checks={key+'_matches':type(raw.get(key)) is type(f.get(key)) and raw.get(key)==f.get(key)
            for key in ('title','body','tags','category_id','category_mode','creation_statement_id',
                        'copyright','content_authorization','source','dtime')}
    checks.update(title_unique=raw.get('title_count')==1,body_unique=raw.get('body_count')==1,
                  cover_present=bool(raw.get('cover_reference')),upload_complete=raw.get('upload_complete') is True,
                  button_unique=raw.get('button_count')==1,button_enabled=raw.get('button_enabled') is True,
                  time_enabled_matches=raw.get('time_enabled') is bool(f['dtime']),
                  date_matches=not f['dtime'] or raw.get('date')==f['date'],
                  clock_matches=not f['dtime'] or raw.get('clock')==f['clock'],
                  timezone_matches=not f['dtime'] or raw.get('timezone')=='Asia/Shanghai')
    return checks


def matches_submit(request,f,raw):
    if type(request) is not dict or not verify_form(raw,f):return False
    videos=request.get('videos');own=raw.get('videos')
    if type(videos) is not list or len(videos)!=1 or type(videos[0]) is not dict or type(own) is not list or len(own)!=1:return False
    filename=own[0].get('filename')
    if type(filename) is not str or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,512}',filename) or not safe_id(own[0].get('cid')):return False
    if videos[0].get('filename')!=own[0]['filename'] or safe_id(videos[0].get('cid'))!=safe_id(own[0]['cid']):return False
    category_key='human_type2' if f['category_mode']=='human' else 'tid'
    statement=request.get('creation_statement')
    return (request.get('title')==f['title'] and request.get('desc','')==f['body'] and request.get('tag')==','.join(f['tags'])
        and type(request.get(category_key)) is int and request[category_key]==f['category_id']
        and type(request.get('copyright')) is int and request['copyright']==f['copyright'] and request.get('source','')==f['source']
        and type(statement) is dict and type(statement.get('id')) is int and statement['id']==f['creation_statement_id']
        and type(request.get('dtime',0)) is int and request.get('dtime',0)==f['dtime'] and request.get('cover')==raw['cover_reference']
        and not request.get('aid') and not request.get('mission_id') and not request.get('topic_id'))


def uploaded_cover_reference(content_type,body,expected,value):
    """Bind the page's ordinary cover conversion to its selected bitmap.

    The official page posts its data URL in a multipart `cover` part at final,
    then substitutes response.data.url in add/v3. Only that exact bitmap can
    establish the matching remote cover URL. CSRF parts are ignored entirely.
    """
    if (type(expected) is not str or not expected.startswith('data:image/') or len(expected)>15*1024*1024
            or type(body) is not bytes or len(body)>16*1024*1024 or type(content_type) is not str
            or not content_type.startswith('multipart/form-data;') or len(content_type)>500 or type(value) is not dict):return None
    remote=value.get('url')
    if type(remote) is not str or not 1<=len(remote)<=4096:return None
    parsed=urlsplit(remote)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or not parsed.hostname.endswith('.hdslb.com') or parsed.username or parsed.password:return None
    try:
        message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
        covers=[part.get_payload(decode=True) for part in message.iter_parts() if part.get_param('name',header='content-disposition')=='cover']
        if len(covers)==1 and covers[0].decode('utf-8')==expected:return remote
    except (ValueError,TypeError,UnicodeError):return None
    return None


def verified_row(rows,reference,f):
    if not reference:return None
    matches=[]
    for value in rows:
        if type(value) is not dict:continue
        actual=receipt_reference(value)
        if not all(actual.get(k)==v for k,v in reference.items()) or value.get('title')!=f['title']:continue
        if type(value.get('dtime',0)) is not int or value.get('dtime',0)!=f['dtime']:continue
        state=value.get('state')
        if type(state) is not int:continue
        mapped={0:'published',-1:'reviewing',-2:'platform_rejected',-40:'platform_scheduled'}.get(state,'processing')
        if mapped=='platform_scheduled' and not f['dtime']:continue
        matches.append({'state':mapped,'reference':actual})
    return matches[0] if len(matches)==1 else None
