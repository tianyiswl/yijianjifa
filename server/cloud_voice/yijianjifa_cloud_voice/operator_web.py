"""Separate operator application. Never mounted on the customer voice API."""
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .api import _Limits
from .auth import _check_password
from .credits import CreditService, PACK
from .errors import CloudError

_COOKIE = 'yjjf_operator'
_SESSION_SECONDS = 30 * 24 * 60 * 60
_GUIDE_URL = 'https://acnjyfifyd1z.feishu.cn/docx/OeLYdXc63oJrIQxpEe5c4ZTgnNh'
_DOWNLOAD_URL = 'https://acnjyfifyd1z.feishu.cn/file/XldubKaTAoB4uGxMz0fcfm8Znuf'
_ASSETS = Path(__file__).with_name('operator_assets')


def validate_origin(origin):
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme in {'http','https'} and parsed.hostname and
                 not parsed.username and not parsed.password and parsed.path in {'','/'} and
                 not parsed.query and not parsed.fragment and parsed.port != 0)
        if not valid or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1','localhost','::1'}):
            raise ValueError()
        return parsed.scheme + '://' + parsed.netloc.lower()
    except (TypeError,ValueError,AttributeError):
        raise ValueError('管理页地址必须为本机HTTP或可信HTTPS地址，不可包含口令和路径。') from None


def _pairs(values):
    result = {}
    for key,value in values:
        if key in result:raise ValueError('duplicate')
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError('nonfinite')


@dataclass
class _Session:
    csrf: str
    expires: float
    authenticated: bool = False


class OperatorApplication:
    def __init__(self, store, *, password_hash, operator_code_key, origin, test_mode=False, supplier_balance=None):
        self.origin = validate_origin(origin)
        self.host = urlsplit(self.origin).netloc
        self.secure = self.origin.startswith('https:')
        self.password_hash = password_hash
        self.test_mode = bool(test_mode)
        self.supplier_balance = supplier_balance
        self.store = store
        self.credits = CreditService(store, operator_code_key=operator_code_key)
        self.sessions, self.lock = {}, threading.RLock()
        self.limits = _Limits(store.clock)
        # Bind persisted sessions to this exact operator credential and origin.
        self.credential_tag = hashlib.sha256((password_hash + '\0' + self.origin).encode()).hexdigest()
        with self.store.transaction() as conn:
            # Start a new metric: historical unique IPs cannot recover total opens.
            # Existing legacy tables remain untouched for audit and rollback.
            conn.execute("CREATE TABLE IF NOT EXISTS guide_clicks (id INTEGER PRIMARY KEY CHECK(id=1), count INTEGER NOT NULL CHECK(count>=0), started_at REAL NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO guide_clicks VALUES (1, 0, ?)", (self.store.clock(),))
            conn.execute("CREATE TABLE IF NOT EXISTS download_clicks (kind TEXT PRIMARY KEY, count INTEGER NOT NULL, started_at REAL NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO download_clicks VALUES ('windows', 0, ?)", (self.store.clock(),))
            conn.execute("INSERT OR IGNORE INTO download_clicks VALUES ('test', 0, ?)", (self.store.clock(),))
            conn.execute("""CREATE TABLE IF NOT EXISTS operator_sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL, credential_tag TEXT NOT NULL)""")
            conn.execute('DELETE FROM operator_sessions WHERE expires <= ? OR credential_tag != ?',
                         (self.store.clock(), self.credential_tag))

    @staticmethod
    def _key(value):return hashlib.sha256(value.encode()).hexdigest()

    def _new_session(self, *, authenticated=False):
        token = secrets.token_urlsafe(32)
        state = _Session(secrets.token_urlsafe(32), self.store.clock()+(_SESSION_SECONDS if authenticated else 600), authenticated)
        with self.lock:
            self.sessions = {key:value for key,value in self.sessions.items() if value.expires > self.store.clock()}
            anonymous = [key for key,value in self.sessions.items() if not value.authenticated]
            if len(anonymous) >= 64:
                self.sessions.pop(anonymous[0],None)
            if authenticated:
                with self.store.transaction() as conn:
                    conn.execute('DELETE FROM operator_sessions WHERE expires <= ?', (self.store.clock(),))
                    if conn.execute('SELECT COUNT(*) FROM operator_sessions').fetchone()[0] >= 192:
                        raise CloudError('rate_limited','管理页访问较多，请稍后再试。',429)
                    conn.execute('INSERT INTO operator_sessions VALUES (?,?,?,?)',
                                 (self._key(token), state.csrf, state.expires, self.credential_tag))
            else:
                self.sessions[self._key(token)] = state
        return token,state

    def _session(self, env):
        cookie = SimpleCookie()
        try:cookie.load(env.get('HTTP_COOKIE',''))
        except CookieError:return '',None
        token = cookie[_COOKIE].value if _COOKIE in cookie else ''
        if len(token)>128:return '',None
        with self.lock:
            state = self.sessions.get(self._key(token)) if token else None
            if state and state.expires <= self.store.clock():
                self.sessions.pop(self._key(token),None);state=None
        if not state and token:
            with self.store.connect() as conn:
                row = conn.execute('SELECT csrf, expires, credential_tag FROM operator_sessions WHERE token_hash = ?',
                                   (self._key(token),)).fetchone()
            if row and row['expires'] > self.store.clock() and hmac.compare_digest(row['credential_tag'], self.credential_tag):
                state = _Session(row['csrf'], row['expires'], True)
        return token,state

    def _delete_session(self, token):
        with self.lock:
            self.sessions.pop(self._key(token), None)
            with self.store.transaction() as conn:
                conn.execute('DELETE FROM operator_sessions WHERE token_hash = ?', (self._key(token),))

    def _cookie(self, token):
        return f'{_COOKIE}={token}; Path=/operator; HttpOnly; SameSite=Strict; Max-Age={_SESSION_SECONDS}' + ('; Secure' if self.secure else '')

    def _body(self, env, fields):
        if env.get('CONTENT_TYPE','').split(';')[0].strip().lower()!='application/json':
            raise CloudError('invalid_request','请使用管理页提交操作。',415)
        try:
            length=int(env.get('CONTENT_LENGTH','0'))
            if length<1 or length>4096:
                raise CloudError('invalid_request','管理请求大小不正确。',413 if length>4096 else 400)
            raw=env['wsgi.input'].read(length)
            if len(raw)!=length:raise ValueError()
            body=json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
            if not isinstance(body,dict) or set(body)!=set(fields):raise ValueError()
        except (ValueError,UnicodeError,KeyError):
            raise CloudError('invalid_request','管理请求内容不正确，请刷新后重试。') from None
        return body

    def _route(self, env):
        method,path=env.get('REQUEST_METHOD',''),env.get('PATH_INFO','')
        if env.get('HTTP_HOST','').lower()!=self.host:
            raise CloudError('forbidden','管理页访问地址不正确。',403)
        supplied_origin=env.get('HTTP_ORIGIN')
        if supplied_origin and supplied_origin!=self.origin:
            raise CloudError('forbidden','请从管理页直接操作。',403)
        if path in {'/operator','/operator/','/operator/operator.js','/operator/operator.css'}:
            if method!='GET':raise CloudError('method_not_allowed','请求方式不正确。',405)
            name=path.rsplit('/',1)[-1] if path.rsplit('/',1)[-1] in {'operator.js','operator.css'} else 'index.html'
            mime={'index.html':'text/html; charset=utf-8','operator.js':'application/javascript; charset=utf-8','operator.css':'text/css; charset=utf-8'}[name]
            return (_ASSETS/name).read_bytes(),mime,None
        if not path.startswith('/operator/api/'):
            raise CloudError('not_found','未找到管理功能。',404)
        action=path.removeprefix('/operator/api/')
        if method not in {'GET','POST'}:raise CloudError('method_not_allowed','请求方式不正确。',405)
        token,session=self._session(env)
        if action=='session' and method=='GET':
            cookie=None
            if not session:
                token,session=self._new_session();cookie=self._cookie(token)
            return {'authenticated':session.authenticated,'csrf':session.csrf,
                    'test_mode':self.test_mode,'pack':dict(PACK)},None,cookie
        if method=='POST':
            if supplied_origin!=self.origin:
                raise CloudError('forbidden','请从管理页直接操作。',403)
            if not session:
                raise CloudError('operator_login_required','管理登录已过期，请重新登录。',401)
            if not hmac.compare_digest(env.get('HTTP_X_CSRF_TOKEN',''),session.csrf):
                raise CloudError('forbidden','操作验证已失效，请刷新管理页。',403)
        if action=='login' and method=='POST':
            body=self._body(env,{'password'})
            self.limits.check('operator-login',10,window=300)
            if not _check_password(body['password'],self.password_hash):
                raise CloudError('invalid_credentials','管理密码不正确。',401)
            self._delete_session(token)
            token,session=self._new_session(authenticated=True)
            return {'authenticated':True},None,self._cookie(token)
        if not session or not session.authenticated:
            raise CloudError('operator_login_required','请先登录兑换码管理页。',401)
        if action=='logout' and method=='POST':
            self._body(env,set())
            self._delete_session(token)
            return {'logged_out':True},None,f'{_COOKIE}=; Path=/operator; HttpOnly; SameSite=Strict; Max-Age=0'+('; Secure' if self.secure else '')
        if action=='guide-stats' and method=='GET':
            with self.store.connect() as conn:
                row = conn.execute('SELECT count, started_at FROM guide_clicks WHERE id=1').fetchone()
            return {'visits':row['count'], 'started_at':row['started_at'], 'entry_url':self.origin+'/operator/guide'},None,None
        if action=='download-stats' and method=='GET':
            with self.store.connect() as conn:
                row = conn.execute("SELECT count, started_at FROM download_clicks WHERE kind='windows'").fetchone()
            return {'clicks':row['count'], 'started_at':row['started_at']},None,None
        if action=='supplier-balance' and method=='GET':
            return (self.supplier_balance.read() if self.supplier_balance else {'status':'unconfigured','checked_at':None,'message':'尚未配置供应商只读费用查询。'}),None,None
        if action=='codes' and method=='GET':
            try:
                query=parse_qs(env.get('QUERY_STRING',''),keep_blank_values=True,max_num_fields=4)
                if set(query)-{'page','search','status'} or any(len(values)!=1 for values in query.values()):raise ValueError()
                page=int(query.get('page',['0'])[0])
                data=self.credits.list_codes(page=page,page_size=20,search=query.get('search',[''])[0],status=query.get('status',['all'])[0])
            except ValueError:raise CloudError('invalid_request','查询条件不正确。') from None
            return data,None,None
        if action=='issue-purchase' and method=='POST':
            body=self._body(env,{'note','count','purchase_id'})
            if not isinstance(body['purchase_id'],str):
                raise CloudError('invalid_request','本次购买编号无效。')
            issued=self.credits.issue(body['count'],order_ref=body['note'],purchase_id=body['purchase_id'])
            return {'codes':issued,'replayed':all(item['replayed'] for item in issued)},None,None
        if action=='issue' and method=='POST':
            body=self._body(env,{'order_ref','count','payment_confirmed'})
            if body['payment_confirmed'] is not True:
                raise CloudError('payment_not_confirmed','请确认已收到对应金额后再生成兑换码。')
            issued=self.credits.issue(body['count'],order_ref=body['order_ref'])
            return {'codes':issued,'replayed':all(item['replayed'] for item in issued)},None,None
        if action in {'reveal','delivered','revoke'} and method=='POST':
            body=self._body(env,{'id'})
            if not isinstance(body['id'],str) or not 1<=len(body['id'])<=64:
                raise CloudError('invalid_request','兑换码编号无效。')
            call={'reveal':self.credits.reveal_code,'delivered':self.credits.mark_delivered,'revoke':self.credits.revoke}[action]
            return call(body['id']),None,None
        raise CloudError('not_found','未找到管理功能。',404)

    def __call__(self, env, start_response):
        status,cookie=200,None
        try:
            if env.get('PATH_INFO') == '/operator/guide':
                if env.get('HTTP_HOST','').lower() != self.host:
                    raise CloudError('forbidden','入口地址不正确。',403)
                if env.get('REQUEST_METHOD') not in {'GET','HEAD'}:
                    raise CloudError('method_not_allowed','请求方式不正确。',405)
                if env.get('QUERY_STRING',''):
                    raise CloudError('invalid_request','入口参数不正确。')
                if env.get('REQUEST_METHOD') == 'GET' and env.get('HTTP_PURPOSE') != 'prefetch' and not env.get('HTTP_SEC_PURPOSE','').startswith('prefetch'):
                    with self.store.transaction() as conn:
                        conn.execute('UPDATE guide_clicks SET count=count+1 WHERE id=1')
                start_response('302 Found', [('Location',_GUIDE_URL),('Cache-Control','no-store'),('Content-Length','0'),('Referrer-Policy','no-referrer')])
                return [b'']
            if env.get('PATH_INFO') == '/operator/download/windows':
                if env.get('HTTP_HOST','').lower() != self.host:
                    raise CloudError('forbidden','下载地址不正确。',403)
                if env.get('REQUEST_METHOD') not in {'GET','HEAD'}:
                    raise CloudError('method_not_allowed','请求方式不正确。',405)
                query = env.get('QUERY_STRING','')
                if query not in {'','test=1'}:
                    raise CloudError('invalid_request','下载参数不正确。')
                if query and not (self._session(env)[1] and self._session(env)[1].authenticated):
                    raise CloudError('operator_login_required','测试入口仅供已登录经营者使用。',401)
                if env.get('REQUEST_METHOD') == 'GET' and not env.get('HTTP_PURPOSE') == 'prefetch' and not env.get('HTTP_SEC_PURPOSE','').startswith('prefetch'):
                    with self.store.transaction() as conn:
                        conn.execute('UPDATE download_clicks SET count=count+1 WHERE kind=?', ('test' if query else 'windows',))
                start_response('302 Found', [('Location',_DOWNLOAD_URL),('Cache-Control','no-store'),('Content-Length','0'),('Referrer-Policy','no-referrer')])
                return [b'']
            data,mime,cookie=self._route(env)
            if not mime:
                mime='application/json; charset=utf-8';data=json.dumps({'ok':True,'data':data},ensure_ascii=False).encode()
        except CloudError as error:
            status=error.status;mime='application/json; charset=utf-8'
            data=json.dumps({'ok':False,'error':{'code':error.code,'message':error.message}},ensure_ascii=False).encode()
        except Exception:
            status=500;mime='application/json; charset=utf-8'
            data=json.dumps({'ok':False,'error':{'code':'operator_unavailable','message':'管理操作未完成，请查询原订单后再试，不要更换订单重复发码。'}},ensure_ascii=False).encode()
        headers=[('Content-Type',mime),('Content-Length',str(len(data))),('Cache-Control','no-store'),
                 ('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                 ('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','no-referrer')]
        if cookie:headers.append(('Set-Cookie',cookie))
        start_response(f'{status} {HTTPStatus(status).phrase}',headers)
        return [data]


def create_operator_app(store, **settings):
    return OperatorApplication(store,**settings)
