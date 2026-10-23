"""Finite, read-only creator options jobs on the existing browser worker."""
import copy
import threading
import time
import uuid

from yijianjifa.features.accounts.service import AccountService
from . import bilibili_static_catalog as catalog


def safe_options(value):
    if type(value) is not dict:return {'status':'error'}
    # Only our reviewed catalog/version may expose local semantic choices.
    # Rebuild rows from that catalog rather than trusting caller names or IDs.
    if value.get('source')=='builtin' and value.get('catalog_version')==catalog.VERSION:
        return catalog.builtin_options()
    status=value.get('status')
    if type(status) is not str or status not in {'ready','empty','verification_required','account_busy','error','not_available','cancelled'}:status='error'
    result={'status':status,'categories':[],'statements':[]}
    if status=='verification_required' and value.get('verification_available') is True:result['verification_available']=True
    view=value.get('verification_view_state')
    if status=='verification_required' and type(view) is str and view in {'opened','failed'}:result['verification_view_state']=view
    if type(value.get('cleanup_complete')) is bool:result['cleanup_complete']=value['cleanup_complete']
    for key,allowed in (('verification_kind',{'login','sms','complex'}),
                        ('verification_stage',{'local','manager','upload'})):
        item=value.get(key)
        if status=='verification_required' and type(item) is str and item in allowed:
            result[key]=item
    for key in ('categories','statements'):
        seen=set()
        rows=value.get(key)
        for row in (rows[:1000] if type(rows) is list else []):
            if type(row) is not dict or type(row.get('id')) is not int:continue
            name=row.get('name',row.get('label'))
            if type(name) is not str or not 1<=len(name)<=255:continue
            mode=row.get('mode')
            if key=='categories' and (row['id']<=0 or type(mode) is not str or mode not in {'legacy','human'}):continue
            identity=(row['id'],mode if key=='categories' else None)
            if identity in seen:continue
            seen.add(identity);item={'id':row['id'],'name':name}
            if key=='categories':
                item['mode']=mode
                parent=row.get('parent_name')
                if type(parent) is str and len(parent)<=255:item['parent_name']=parent
            result[key].append(item)
    if type(value.get('category_mode')) is str and value['category_mode'] in {'legacy','human'}:result['category_mode']=value['category_mode']
    fetched=value.get('fetched_at')
    if type(fetched) is str and len(fetched)<=40:result['fetched_at']=fetched
    schedule=value.get('schedule')
    if type(schedule) is dict:
        result['schedule']={k:v for k,v in schedule.items() if k in {'min_lead_seconds','max_lead_seconds','minute_step'}
            and type(v) is int and 0<v<=31*86400}
        if schedule.get('timezone')=='Asia/Shanghai':result['schedule']['timezone']='Asia/Shanghai'
    return result


MESSAGES={'ready':'平台选项已读取完成。','empty':'尚未读取到平台选项，请刷新。',
    'verification_required':'平台要求验证，请到账号管理打开此账号后台查看，再回来刷新发布选项。',
    'account_busy':'此账号正在操作，请稍后刷新。','error':'平台选项读取未完成，原选择和缓存仍保留。',
    'not_available':'此平台暂不需要读取发布选项。','cancelled':'已停止读取并关闭原验证页面，原选择和缓存保留。'}


def options_message(options):
    if options.get('source')=='builtin':return '分区和创作声明可直接选择；发布时会核对当前账号的实际投稿表单。'
    if options['status']!='verification_required':return MESSAGES[options['status']]
    kind=options.get('verification_kind');stage=options.get('verification_stage')
    if stage=='local':return '本地缺少有效登录资料，请到账号管理登录此账号，再回来刷新发布选项。'
    source={'manager':'内容管理页','upload':'投稿页'}.get(stage,'平台页面')
    requirement={'login':'登录提示','sms':'短信验证码','complex':'图形或滑块验证'}.get(kind,'验证要求')
    if options.get('verification_available'):
        if options.get('verification_view_state')=='failed':return '原验证窗口未能显示，请重试显示原窗口，或停止读取后重新打开。'
        return source+'检测到'+requirement+'。原浏览器已保留；请由本人完成验证，再点击“已完成验证，继续读取”。仅读取分区和创作声明，不上传或投稿。'
    return source+'检测到'+requirement+'；请到账号管理打开此账号后台核对，再回来刷新发布选项。'


class PlatformOptionsJobs:
    ACTIVE={'loading','verification_required','cancelling'}
    PUBLIC={'status','message','options','verification_id','verification_available','verification_view_state'}
    def __init__(self,runtime):
        self.runtime=runtime;self.jobs={};self.lock=threading.RLock()

    def adapter(self,account_id):
        account=AccountService(self.runtime.engine).get(account_id)
        adapter=self.runtime.catalog.transport_for(account.platform)
        if not callable(getattr(adapter,'read_platform_options',None)):raise ValueError('platform options unavailable')
        return adapter

    def cached(self,account_id):
        account=AccountService(self.runtime.engine).get(account_id)
        if account.platform=='bilibili':
            options=catalog.builtin_options()
            return {'status':'ready','options':options,'message':options_message(options)}
        adapter=self.adapter(account_id)
        getter=getattr(adapter,'get_cached_platform_options',None)
        options=safe_options(getter(account_id)) if callable(getter) else {'status':'empty','categories':[],'statements':[]}
        return {'status':options['status'],'options':options,'message':options_message(options)}

    def refresh(self,account_id):
        if self.runtime.closed.is_set():raise ValueError('app closing')
        adapter=self.adapter(account_id)  # No browser IO on the API thread.
        with self.lock:
            pending=next((tid for tid,j in self.jobs.items() if j['account_id']==account_id and self._busy(j)),None)
            if pending:return {'ticket':pending,**self.status(pending),'cached':self.cached(account_id)['options']}
            if len(self.jobs)>=64:
                terminal=next((tid for tid,j in self.jobs.items() if not self._busy(j)),None)
                if terminal:self.jobs.pop(terminal)
                else:raise ValueError('platform options busy')
            ticket=uuid.uuid4().hex
            self.jobs[ticket]={'account_id':account_id,'status':'loading','message':'正在读取平台选项…','expires':time.monotonic()+300,'cancel_requested':False,'_adapter':adapter}
        try:self.runtime.enqueue('platform_options',ticket)
        except Exception:
            with self.lock:self.jobs[ticket].update(status='error',message=MESSAGES['error'])
            raise
        return {'ticket':ticket,'status':'loading','cached':self.cached(account_id)['options']}

    def status(self,ticket):
        with self.lock:
            job=self.jobs.get(ticket)
            if job is None:raise ValueError('platform options request unavailable')
            return copy.deepcopy({k:v for k,v in job.items() if k in self.PUBLIC})

    @staticmethod
    def _busy(job):return job['status'] in {'loading','cancelling'} or job.get('retained') is True

    def busy_accounts(self):
        with self.lock:return {j['account_id'] for j in self.jobs.values() if self._busy(j)}

    def _store(self,ticket,options,*,action=None):
        with self.lock:
            job=self.jobs[ticket]
            if job['cancel_requested'] and action!='cancel':return
            job.update(status=options['status'],options=options,message=options_message(options),
                retained=options.get('verification_available') is True or options.get('cleanup_complete') is False)
            if options['status']=='verification_required' and options.get('verification_available'):
                if action!='show' or not job.get('verification_id'):job['verification_id']=uuid.uuid4().hex
                job['verification_available']=True
                if 'verification_view_state' in options:job['verification_view_state']=options['verification_view_state']
                else:job.pop('verification_view_state',None)
            else:
                for key in ('verification_id','verification_available','verification_view_state'):job.pop(key,None)
            job.pop('pending_action',None)

    def respond(self,ticket,verification_id,action):
        if action not in {'show','continue','cancel'}:raise ValueError('invalid metadata action')
        with self.lock:
            job=self.jobs.get(ticket)
            if (job is None or job['status']!='verification_required' or not job.get('retained')
                    or job.get('verification_id')!=verification_id or time.monotonic()>=job['expires']):
                raise ValueError('original metadata verification unavailable')
            if action=='cancel':return self.cancel(ticket)
            job.update(status='loading',pending_action=action,message='正在显示原验证窗口…' if action=='show' else '正在原页面继续读取…')
        try:self.runtime.enqueue('platform_options_action',(ticket,action))
        except Exception:
            with self.lock:job.update(status='verification_required',message='操作未受理，原验证页面仍保留。');job.pop('pending_action',None)
            raise
        return {'ticket':ticket,'status':'loading','accepted':True}

    def cancel(self,ticket):
        with self.lock:
            job=self.jobs.get(ticket)
            if job is None:raise ValueError('platform options request unavailable')
            if job['status']=='cancelling':return {'ticket':ticket,'status':'cancelling','accepted':True}
            if not self._busy(job):return {'ticket':ticket,**self.status(ticket)}
            previous={k:job.get(k) for k in ('status','cancel_requested','pending_action','message')}
            job.update(status='cancelling',cancel_requested=True,pending_action='cancel',message='正在停止读取并关闭原验证页面…')
        try:self.runtime.enqueue('platform_options_action',(ticket,'cancel'))
        except Exception:
            with self.lock:
                if job.get('pending_action')=='cancel' and job['status']=='cancelling':
                    job.update(previous)
                    if previous['pending_action'] is None:job.pop('pending_action',None)
            raise
        return {'ticket':ticket,'status':'cancelling','accepted':True}

    def run_action(self,ticket,action):
        with self.lock:
            job=self.jobs.get(ticket)
            if job is None or job.get('pending_action')!=action:return
            account_id=job['account_id'];original_adapter=job.get('_adapter')
        try:
            adapter=original_adapter if original_adapter is not None else self.adapter(account_id)
            resume=getattr(adapter,'continue_platform_options',None)
            result=resume(account_id,ticket,action) if callable(resume) else {'status':'cancelled','cleanup_complete':True} if action=='cancel' else {'status':'error'}
            options=safe_options(result)
        except Exception:options={'status':'error','categories':[],'statements':[],'cleanup_complete':False}
        self._store(ticket,options,action=action)

    def expire(self):
        with self.lock:tickets=[tid for tid,j in self.jobs.items() if self._busy(j) and j['status']!='cancelling' and time.monotonic()>=j['expires']]
        for ticket in tickets:
            try:self.cancel(ticket)
            except ValueError:pass  # Shutdown cleans original pages on the worker.

    def close(self):
        with self.lock:tickets=[tid for tid,j in self.jobs.items() if self._busy(j)]
        for ticket in tickets:
            with self.lock:self.jobs[ticket].update(cancel_requested=True,pending_action='cancel')
            self.run_action(ticket,'cancel')

    def run(self,ticket):
        with self.lock:
            job=self.jobs.get(ticket)
            if job is None or job['status']!='loading':return
            account_id=job['account_id'];original_adapter=job.get('_adapter')
        try:
            adapter=original_adapter if original_adapter is not None else self.adapter(account_id)
            begin=getattr(adapter,'begin_platform_options',None)
            options=safe_options(begin(account_id,ticket) if callable(begin) else adapter.read_platform_options(account_id))
        except Exception:options={'status':'error','categories':[],'statements':[]}
        self._store(ticket,options)
