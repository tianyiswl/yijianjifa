"""Read-only supplier funds. Private operator process only; never voice credentials."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error
import urllib.request

_ENDPOINT='https://billing.volcengineapi.com/'
_QUERY='Action=QueryBalanceAcct&Version=2022-01-01'


def _private_path(value):
    path=Path(value)
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('费用配置需使用无符号链接的绝对路径。')
    return path


def _credential(value):
    if not isinstance(value,str) or not 1<=len(value)<=512 or not re.fullmatch(r'[A-Za-z0-9_+/=.-]+',value):
        raise ValueError('费用查询密钥格式不正确。')
    return value


def save_credentials(destination,ak,sk):
    path=_private_path(destination)
    payload={'access_key_id':_credential(ak),'secret_access_key':_credential(sk)}
    if not path.parent.is_dir() or path.parent.stat().st_mode&0o077:
        raise ValueError('费用配置目录必须仅经营服务账号可读。')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as stream:
        json.dump(payload,stream);stream.flush();os.fsync(stream.fileno())


def signed_request(ak,sk,date):
    host='billing.volcengineapi.com'
    scope=date[:8]+'/cn-beijing/billing/request'
    canonical='GET\n/\n'+_QUERY+'\nhost:'+host+'\nx-date:'+date+'\n\nhost;x-date\n'+hashlib.sha256(b'').hexdigest()
    signing='HMAC-SHA256\n'+date+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
    key=sk.encode()
    for part in (date[:8],'cn-beijing','billing','request'):
        key=hmac.new(key,part.encode(),hashlib.sha256).digest()
    signature=hmac.new(key,signing.encode(),hashlib.sha256).hexdigest()
    auth='HMAC-SHA256 Credential='+ak+'/'+scope+', SignedHeaders=host;x-date, Signature='+signature
    return urllib.request.Request(_ENDPOINT+'?'+_QUERY,headers={'Host':host,'X-Date':date,'Authorization':auth},method='GET')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


class SupplierBalance:
    def __init__(self,config_path,*,clock=None,opener=None):
        self.path=Path(config_path)
        self.clock=clock or time.time
        self.opener=opener or urllib.request.build_opener(_NoRedirect()).open
        self.lock=threading.Lock()
        self._next=0
        self._last={}
        self._result=None

    def __repr__(self):return '<SupplierBalance private>'

    def read(self):
        with self.lock:
            now=self.clock()
            if self._result is not None and now<self._next:return dict(self._result)
            self._next=now+60
            try:
                path=_private_path(self.path)
                fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                with os.fdopen(fd,'r',encoding='utf-8') as stream:
                    info=os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or path.parent.stat().st_mode&0o077 or info.st_size>4096:raise ValueError()
                    config=json.load(stream)
                if not isinstance(config,dict) or set(config)!={'access_key_id','secret_access_key'}:raise ValueError()
                ak=_credential(config['access_key_id']);sk=_credential(config['secret_access_key'])
                date=datetime.fromtimestamp(now,timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                request=signed_request(ak,sk,date)
                with self.opener(request,timeout=8) as response:
                    if getattr(response,'status',200)!=200 or getattr(response,'geturl',lambda:request.full_url)()!=request.full_url:raise ValueError()
                    raw=response.read(65537)
                if len(raw)>65536:raise ValueError()
                payload=json.loads(raw)
                if not isinstance(payload,dict) or payload.get('ResponseMetadata',{}).get('Error'):raise ValueError()
                data=payload['Result'];result={}
                for field,target in [('AvailableBalance','available_balance'),('CashBalance','cash_balance'),('ArrearsBalance','arrears_balance')]:
                    value=data[field]
                    if not isinstance(value,str) or len(value)>40 or not re.fullmatch(r'-?\d{1,18}(?:\.\d{1,6})?',value):raise ValueError()
                    number=Decimal(value)
                    if not number.is_finite():raise ValueError()
                    result[target]=format(number,'f')
                self._last={**result,'checked_at':now}
                self._result={**self._last,'status':'ok','message':'账户资金余额，不代表语音资源包剩余字数。'}
            except FileNotFoundError:
                self._last={}
                self._result={'status':'unconfigured','checked_at':None,'message':'尚未配置供应商只读费用查询。'}
            except Exception:
                # Never pass supplier bodies, exception strings, account IDs or credentials to the UI/logs.
                self._result={**self._last,'status':'unavailable','message':'暂时无法查询，请检查只读费用权限、密钥和网络。'}
            return dict(self._result)
