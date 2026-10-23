"""Transient, request-bound user handoff. Never persist verification values."""
import secrets
import threading
import time


INTERVENTION_REASONS = frozenset({
    'identity_unavailable', 'identity_mismatch', 'session_changed', 'login_required',
    'sms_required', 'challenge_required',
})
DEFAULT_REASONS = {'login': 'login_required', 'sms': 'sms_required', 'complex': 'challenge_required'}


def intervention_reason(kind, reason=None):
    value = DEFAULT_REASONS.get(kind) if reason is None else reason
    if type(value) is not str or value not in INTERVENTION_REASONS:
        raise ValueError('无效处理原因')
    if value in {'identity_unavailable', 'identity_mismatch', 'session_changed', 'login_required'} and kind != 'login':
        raise ValueError('处理原因与操作类型不匹配')
    if (value == 'sms_required' and kind != 'sms') or (value == 'challenge_required' and kind != 'complex'):
        raise ValueError('处理原因与操作类型不匹配')
    return value


def safe_resume(*, final_attempted):
    return 'result' if final_attempted else 'prepare'


class InterventionBroker:
    def __init__(self):
        self._lock=threading.Lock(); self._request=None; self._response=None

    def request(self, account_id, account_name, kind, message, *, reason=None, external_browser=None):
        reason = intervention_reason(kind, reason)
        with self._lock:
            if self._request is not None:raise ValueError('已有验证正在等待处理')
            token=secrets.token_hex(24)
            self._request=dict(id=token,account_id=account_id,account_name=account_name,kind=kind,reason=reason,message=message,expires_at=time.time()+600)
            if type(external_browser) is bool:self._request['external_browser']=external_browser
            self._response=None
            return token

    def status(self):
        with self._lock:return {'request':dict(self._request) if self._request else None}

    def respond(self, token, action, code=''):
        with self._lock:
            r=self._request
            if not r or r['id']!=token or time.time()>r['expires_at']:raise ValueError('验证请求已结束，请重新检查任务')
            if self._response is not None:raise ValueError('正在处理，请稍候')
            if action not in {'code','resend_code','visible','done','cancel','pointer','scroll','refresh'}:raise ValueError('无效验证操作')
            if action=='resend_code' and r['kind']!='sms':raise ValueError('当前不是短信验证')
            if action=='code' and (r['kind']!='sms' or not isinstance(code,str) or not 1<=len(code.strip())<=16 or not code.strip().isalnum()):raise ValueError('请输入有效验证码')
            framed=r['kind'] in {'complex','login'} or r.get('view_mode')=='page'
            if action=='pointer' and (not framed or type(code) is not list or len(code) not in (2,4) or any(type(v) not in (int,float) or not 0<=v<=(1440 if i%2==0 else 1000) for i,v in enumerate(code))):raise ValueError('无效验证位置')
            if action=='scroll' and (not framed or type(code) is not list or len(code)!=2 or any(type(v) not in (int,float) or not -1000<=v<=1000 for v in code)):raise ValueError('无效滚动操作')
            self._response=(action,code.strip() if action=='code' else code if action in {'pointer','scroll'} else '')
            if action in {'visible','refresh','scroll','pointer','code','resend_code'}:
                r.update(view_state='queued',view_action=action,view_revision=r.get('view_revision',0)+1)
                if action=='code':r.update(code_state='queued',code_message='验证码请求已排队，正在交给原平台页面。')
                if action=='resend_code':
                    r.update(sms_state='queued',sms_message='正在获取新的短信验证码。')
                    r.pop('code_state',None);r.pop('code_message',None)
                r.pop('view_error',None)
                return {'accepted':True,'view_revision':r['view_revision']}
            return {'accepted':True}

    def code_feedback(self,token,state,message):
        with self._lock:
            if self._request and self._request['id']==token:
                self._request.update(code_state=state,code_message=message)

    def sms_feedback(self,token,state,message):
        with self._lock:
            if self._request and self._request['id']==token:
                self._request.update(sms_state=state,sms_message=message,message=message)

    def view_result(self,token,revision,*,error=None):
        with self._lock:
            if self._request and self._request['id']==token and self._request.get('view_revision')==revision:
                self._request['view_state']='failed' if error else 'ready'
                if error:self._request['view_error']=error
                else:self._request.pop('view_error',None)

    def consume(self, token, *, view_revision=None):
        with self._lock:
            if not self._request or self._request['id']!=token:return None
            if view_revision is not None and self._request.get('view_revision')!=view_revision:return None
            value=self._response;self._response=None;return value

    def frame(self,token,image):
        with self._lock:
            if self._request and self._request['id']==token:self._request['image']=image

    def open_view(self,token):
        with self._lock:
            if self._request and self._request['id']==token:self._request['view_mode']='page'

    def message(self,token,message):
        with self._lock:
            if self._request and self._request['id']==token:self._request['message']=message

    def finish(self, token):
        with self._lock:
            if self._request and self._request['id']==token:self._request=None;self._response=None

broker=InterventionBroker()
