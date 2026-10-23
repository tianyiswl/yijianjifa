from collections import defaultdict,deque
from http import HTTPStatus
import hashlib
import ipaddress
import json
import re
import threading
import time
from urllib.parse import parse_qs

from .auth import AuthService
from .credits import CreditService
from .errors import CloudError

MAX_BODY=1_048_576


class _Limits:
    def __init__(self,clock):
        self.clock=clock
        self.entries=defaultdict(deque)
        self.lock=threading.Lock()

    def check(self,key,limit,window=60):
        with self.lock:
            now=self.clock()
            # Keep bounded state even if callers continually vary account names.
            if len(self.entries)>10000:
                self.entries={k:v for k,v in self.entries.items() if v and v[-1]>now-3600}
                self.entries=defaultdict(deque,self.entries)
                if len(self.entries)>10000:
                    raise CloudError('rate_limited','请求较多，请稍后再试。',429)
            values=self.entries[key]
            while values and values[0]<=now-window:
                values.popleft()
            if len(values)>=limit:
                raise CloudError('rate_limited','请求较多，请稍后再试。',429)
            values.append(now)


def _strict_ip(value):
    if not isinstance(value,str) or not value or len(value)>45 or value!=value.strip() or '%' in value:
        raise ValueError('invalid IP address')
    return ipaddress.ip_address(value)


def create_app(store,*,auth=None,credits=None,jobs=None,trusted_proxies=()):
    auth=auth or AuthService(store)
    credits=credits or CreditService(store)
    limits=_Limits(store.clock)
    if not isinstance(trusted_proxies,(tuple,list,set,frozenset)) or len(trusted_proxies)>64:
        raise ValueError('可信代理需为明确的IP地址列表。')
    try:
        trusted_peer_addresses=frozenset(_strict_ip(value) for value in trusted_proxies)
    except ValueError:
        raise ValueError('可信代理需为明确的IP地址列表。') from None

    def request_source(env):
        try:
            peer=_strict_ip(env.get('REMOTE_ADDR',''))
        except ValueError:
            return 'unknown'
        if peer not in trusted_peer_addresses:
            return str(peer)
        # Exactly one verified proxy hop; never trust client-supplied forwarding chains.
        try:
            return str(_strict_ip(env.get('HTTP_X_REAL_IP','')))
        except ValueError:
            raise CloudError('invalid_source_ip','请求来源无法核对，请稍后重试。') from None

    def fields(body,required=(),optional=()):
        if not isinstance(body,dict) or set(body)-set(required)-set(optional) or set(required)-set(body):
            raise CloudError('invalid_request','请求内容不符合要求，请更新客户端后重试。')
        return body

    def route(env):
        method=env.get('REQUEST_METHOD','GET').upper()
        path=env.get('PATH_INFO','')
        source=request_source(env)
        # Desktop talks through its native host. Do not expose a browser-callable billing API.
        if env.get('HTTP_ORIGIN'):
            raise CloudError('origin_forbidden','不支持从此页面操作配音账号。',403)
        limits.check(('source',source),180)
        body={}
        if method in ('POST','PUT','DELETE'):
            try:
                length=int(env.get('CONTENT_LENGTH','0') or '0')
            except ValueError:
                raise CloudError('invalid_request','请求内容无效。') from None
            if length<0 or length>MAX_BODY:
                raise CloudError('request_too_large','请求内容过大，请减少本批文案。',413)
            if length:
                if env.get('CONTENT_TYPE','').split(';')[0]!='application/json':
                    raise CloudError('invalid_request','请求格式无效。',415)
                raw=env['wsgi.input'].read(length)
                if len(raw)!=length:
                    raise CloudError('invalid_request','请求内容不完整。')
                try:
                    body=json.loads(raw)
                except (UnicodeDecodeError,json.JSONDecodeError):
                    raise CloudError('invalid_request','请求内容无效。') from None
                if not isinstance(body,dict):
                    raise CloudError('invalid_request','请求内容无效。')
        if path in ('/v1/accounts','/v1/sessions') and method=='POST':
            fields(body,('username','password'))
            limits.check(('auth-source',source),10)
            login_name=body['username'] if isinstance(body['username'],str) else ''
            limits.check(('auth-name',hashlib.sha256(login_name.casefold().encode()).hexdigest()),10,900)
            result=(auth.register if path=='/v1/accounts' else auth.login)(body['username'],body['password'])
            return result,201 if path=='/v1/accounts' else 200
        if path=='/v1/sessions/refresh' and method=='POST':
            fields(body,('refresh_token',)); limits.check(('refresh',source),30)
            return auth.refresh(body['refresh_token']),200
        if path=='/v1/accounts/recover' and method=='POST':
            fields(body,('account_id','recovery_secret','new_password'))
            limits.check(('recover',source),5,900)
            return auth.recover(body['account_id'],body['recovery_secret'],body['new_password']),200
        known=(path in ('/v1/me','/v1/usage','/v1/voices','/v1/redemptions','/v1/sessions/current',
                        '/v1/sessions','/v1/accounts/password','/v1/accounts/recovery','/v1/batches')
               or path.startswith('/v1/batches/') or path.startswith('/v1/jobs/'))
        if not known:
            raise CloudError('not_found','未找到此功能。',404)
        header=env.get('HTTP_AUTHORIZATION','')
        if not header.startswith('Bearer '):
            raise CloudError('session_expired','请先登录配音账号。',401)
        token=header[7:]
        account=auth.authenticate(token)
        limits.check(('account',account),120)
        if jobs and hasattr(jobs,'maintenance') and path in ('/v1/me','/v1/usage','/v1/redemptions'):
            jobs.maintenance()
        if path=='/v1/me' and method=='GET':
            return credits.balance(account),200
        if path=='/v1/usage' and method=='GET':
            query=parse_qs(env.get('QUERY_STRING',''))
            try: page=int(query.get('page',['0'])[0])
            except ValueError: raise CloudError('invalid_page','页码无效。') from None
            return credits.usage(account,page),200
        if path=='/v1/redemptions' and method=='POST':
            fields(body,('code','idempotency_key'))
            limits.check(('redeem',account),10,900)
            return credits.redeem(account,body['code'],body['idempotency_key']),200
        if path=='/v1/sessions/current' and method=='DELETE':
            fields(body); return auth.logout(token),200
        if path=='/v1/sessions' and method=='DELETE':
            fields(body,('password',)); return auth.logout_all(account,body['password']),200
        if path=='/v1/accounts/password' and method=='POST':
            fields(body,('old_password','new_password'))
            limits.check(('password',account),5,900)
            return auth.change_password(account,body['old_password'],body['new_password']),200
        if path=='/v1/accounts/recovery' and method=='POST':
            fields(body,('password',)); limits.check(('recovery',account),5,900)
            return auth.export_recovery(account,body['password']),200
        if not jobs:
            raise CloudError('service_not_configured','云配音服务暂未开通。',503)
        if path=='/v1/voices' and method=='GET':
            return {'voices':jobs.allowed_voices()},200
        if path=='/v1/batches' and method=='POST':
            fields(body,('idempotency_key','items'))
            limits.check(('prepare',account),20)
            return jobs.prepare(account,body),200
        batch_match=re.fullmatch(r'/v1/batches/([A-Za-z0-9-]+)(?:/(lease|release))?',path)
        if batch_match:
            batch_id,action=batch_match.groups()
            if action is None and method=='GET': return jobs.batch(account,batch_id),200
            if action in ('lease','release') and method=='POST':
                fields(body); return getattr(jobs,action)(account,batch_id),200
        start_match=re.fullmatch(r'/v1/batches/([A-Za-z0-9-]+)/jobs/([a-f0-9]{32})/start',path)
        if start_match and method=='POST':
            fields(body); limits.check(('generate',account),30)
            return jobs.start(account,*start_match.groups()),200
        job_match=re.fullmatch(r'/v1/jobs/([A-Za-z0-9-]+)(?:/(audio|download|retry|restore))?',path)
        if job_match:
            job_id,action=job_match.groups()
            if action is None and method=='GET': return jobs.get(account,job_id),200
            if action=='audio' and method=='GET': return jobs.audio(account,job_id),200
            if action=='download' and method=='GET':
                limits.check(('download',account),60)
                return jobs.download(account,job_id),200
            if action=='restore' and method=='POST':
                fields(body,('idempotency_key','expected_attempt')); limits.check(('restore',account),30,900)
                return jobs.restore(account,job_id,body),200
            if action=='retry' and method=='POST':
                fields(body,('idempotency_key',)); limits.check(('retry',account),5,900)
                return jobs.retry(account,job_id,body),200
        raise CloudError('not_found','未找到此功能。',404)

    def app(environ,start_response):
        mime='application/json; charset=utf-8'
        try:
            result,status=route(environ)
            if isinstance(result,tuple) and len(result)==2 and isinstance(result[0],bytes):
                payload,mime=result
            else:
                payload=json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        except CloudError as exc:
            status=exc.status
            payload=json.dumps({'error':{'code':exc.code,'message':exc.message,'details':exc.details}},ensure_ascii=False).encode()
        except Exception:
            # No stack, submitted text, passwords, codes, or supplier responses go to clients/logs.
            status=500
            payload=json.dumps({'error':{'code':'internal_error','message':'服务暂时无法完成操作，请查询原结果后重试。','details':None}},ensure_ascii=False).encode()
        headers=[('Content-Type',mime),('Content-Length',str(len(payload))),('Cache-Control','no-store'),
                 ('X-Content-Type-Options','nosniff')]
        if status==429: headers.append(('Retry-After','60'))
        start_response(f'{status} {HTTPStatus(status).phrase}',headers)
        return [payload]
    return app
