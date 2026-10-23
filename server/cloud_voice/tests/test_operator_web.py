import io
import json
from http.cookies import SimpleCookie

import pytest
from cryptography.fernet import Fernet

from yijianjifa_cloud_voice.auth import _hash_password
from yijianjifa_cloud_voice.store import Store
from yijianjifa_cloud_voice.operator_web import create_operator_app

ORIGIN = 'http://127.0.0.1:18771'
PASSWORD = 'isolated-owner-password-123'


@pytest.fixture
def panel(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path / 'isolated.db', clock=lambda: clock[0])
    app = create_operator_app(store, password_hash=_hash_password(PASSWORD),
                              operator_code_key=Fernet.generate_key(), origin=ORIGIN, test_mode=True)
    return Client(app), store, clock


class Client:
    def __init__(self, app):
        self.app, self.cookie, self.csrf = app, '', ''

    def request(self, path='session', method='GET', body=None, *, origin=ORIGIN, csrf=True, host='127.0.0.1:18771', raw=None):
        payload = raw if raw is not None else json.dumps(body or {}).encode()
        env = {'REQUEST_METHOD': method, 'PATH_INFO': '/operator/api/' + path.split('?')[0],
               'QUERY_STRING': path.partition('?')[2], 'HTTP_HOST': host, 'HTTP_COOKIE': self.cookie,
               'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(payload)),
               'REMOTE_ADDR': '127.0.0.1', 'wsgi.input': io.BytesIO(payload)}
        if origin is not None:env['HTTP_ORIGIN'] = origin
        if csrf:env['HTTP_X_CSRF_TOKEN'] = self.csrf
        result = {}
        def start(status, headers):result.update(status=int(status.split()[0]), headers=dict(headers))
        data = b''.join(self.app(env, start))
        if 'Set-Cookie' in result['headers']:
            cookie = SimpleCookie();cookie.load(result['headers']['Set-Cookie'])
            self.cookie = '; '.join(f'{key}={part.value}' for key, part in cookie.items())
        result['body'] = json.loads(data)
        return result

    def login(self):
        result = self.request();self.csrf = result['body']['data']['csrf']
        assert self.request('login', 'POST', {'password':PASSWORD})['status'] == 200
        self.csrf = self.request()['body']['data']['csrf']


def test_login_is_separate_from_customer_accounts_and_cannot_be_bypassed(panel):
    client, store, _ = panel
    assert client.request('codes')['status'] == 401
    state = client.request();client.csrf = state['body']['data']['csrf']
    assert state['body']['data']['test_mode'] is True
    assert client.request('login', 'POST', {'password':'bad'})['status'] == 401
    client.login()
    assert client.request('codes')['status'] == 200
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0


@pytest.mark.parametrize('path,method,body', [
    ('codes','GET',None), ('issue','POST',{'order_ref':'order','count':1,'payment_confirmed':True}),
    ('reveal','POST',{'id':'x'}),('delivered','POST',{'id':'x'}),('revoke','POST',{'id':'x'})])
def test_customer_or_anonymous_requests_have_no_operator_permissions(panel,path,method,body):
    client,_,_ = panel
    result=client.request();client.csrf=result['body']['data']['csrf']
    assert client.request(path,method,body)['status'] == 401


@pytest.mark.parametrize('origin,csrf,host', [
    ('https://evil.invalid',True,'127.0.0.1:18771'),(ORIGIN,False,'127.0.0.1:18771'),
    (None,True,'127.0.0.1:18771'),(ORIGIN,True,'evil.invalid')])
def test_cross_site_or_missing_csrf_or_forged_host_cannot_issue(panel,origin,csrf,host):
    client,store,_=panel;client.login()
    result=client.request('issue','POST',{'order_ref':'blocked','count':1,'payment_confirmed':True},origin=origin,csrf=csrf,host=host)
    assert result['status'] == 403
    with store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 0


def test_issue_copy_mark_delivered_and_replay_keep_one_original_code(panel):
    client,store,_=panel;client.login()
    body={'order_ref':'QA-ORDER-1','count':1,'payment_confirmed':True}
    first=client.request('issue','POST',body)['body']['data']
    second=client.request('issue','POST',body)['body']['data']
    assert second['replayed'] is True
    assert first['codes'][0]['code'] == second['codes'][0]['code']
    code_id=first['codes'][0]['id']
    shown=client.request('reveal','POST',{'id':code_id})['body']['data']
    assert shown['code'] == first['codes'][0]['code']
    assert client.request('delivered','POST',{'id':code_id})['status'] == 200
    listing=client.request('codes?status=delivered&search=QA-ORDER-1')['body']['data']
    assert listing['total'] == 1
    assert 'code' not in listing['items'][0] and 'encrypted_code' not in listing['items'][0]
    with store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 1


def test_issue_demands_explicit_payment_confirmation_and_strict_shape(panel):
    client,store,_=panel;client.login()
    for body in ({'order_ref':'QA','count':1,'payment_confirmed':False},
                 {'order_ref':'QA','count':1,'payment_confirmed':1},
                 {'order_ref':'QA','count':1,'payment_confirmed':True,'words':999999}):
        assert client.request('issue','POST',body)['status'] == 400
    with store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 0


def test_logout_and_session_expiration_revoke_operator_access(panel):
    client,_,clock=panel;client.login()
    old_cookie=client.cookie
    assert client.request('logout','POST')['status'] == 200
    client.cookie=old_cookie
    assert client.request('codes')['status'] == 401
    client.login();clock[0]+=30*86400+1
    assert client.request('codes')['status'] == 401


def test_login_is_rate_limited_and_error_messages_never_contain_password(panel):
    client,_,_=panel
    state=client.request();client.csrf=state['body']['data']['csrf']
    statuses=[]
    for _ in range(12):
        result=client.request('login','POST',{'password':'bad-secret'})
        statuses.append(result['status']);assert 'bad-secret' not in json.dumps(result['body'])
    assert 429 in statuses


def test_security_headers_and_login_rotate_session(panel):
    client,_,_=panel
    initial=client.request();old_cookie=client.cookie;client.csrf=initial['body']['data']['csrf']
    result=client.request('login','POST',{'password':PASSWORD})
    assert client.cookie != old_cookie
    assert 'HttpOnly' in result['headers']['Set-Cookie']
    assert 'SameSite=Strict' in result['headers']['Set-Cookie']
    assert result['headers']['Cache-Control'] == 'no-store'
    assert "frame-ancestors 'none'" in result['headers']['Content-Security-Policy']
    assert PASSWORD not in json.dumps(result['body'])


def test_malformed_large_or_duplicate_json_is_rejected_without_writing(panel):
    client,store,_=panel;client.login()
    for raw in (b'{"count":1,"count":2}',b'{"count":NaN}',b'x'*5000):
        assert client.request('issue','POST',raw=raw)['status'] in (400,413)
    with store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0] == 0


def test_truncated_issue_body_is_rejected_before_issuing(panel):
    client,store,_=panel;client.login()
    raw=json.dumps({'order_ref':'TRUNCATED','count':1,'payment_confirmed':True}).encode()
    env={'REQUEST_METHOD':'POST','PATH_INFO':'/operator/api/issue','HTTP_HOST':'127.0.0.1:18771',
         'HTTP_ORIGIN':ORIGIN,'HTTP_COOKIE':client.cookie,'HTTP_X_CSRF_TOKEN':client.csrf,
         'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)+1),'wsgi.input':io.BytesIO(raw)}
    result={}
    list(client.app(env,lambda status,headers:result.update(status=int(status.split()[0]))))
    assert result['status']==400
    with store.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]==0


def test_anonymous_session_flood_does_not_lock_out_owner(panel):
    client,_,_=panel;client.login()
    anonymous=Client(client.app)
    for _ in range(300):
        anonymous.cookie=''
        assert anonymous.request()['status']==200
    assert client.request('codes')['status']==200
    fresh=Client(client.app);fresh.login()
    assert fresh.request('codes')['status']==200


def test_expired_post_returns_login_required_so_page_clears_original_codes(panel):
    client,_,clock=panel;client.login();clock[0]+=30*86400+1
    result=client.request('issue','POST',{'order_ref':'EXPIRED','count':1,'payment_confirmed':True})
    assert result['status']==401
    assert result['body']['error']['code']=='operator_login_required'


def test_customer_api_never_mounts_operator_routes(panel):
    from yijianjifa_cloud_voice.api import create_app
    _,store,_=panel
    public=create_app(store)
    for path in ('/operator/','/operator/api/session','/operator/api/issue'):
        result={}
        list(public({'REQUEST_METHOD':'GET','PATH_INFO':path,'REMOTE_ADDR':'127.0.0.1'},
                    lambda status,headers:result.update(status=int(status.split()[0]))))
        assert result['status']==404


def test_supplier_funds_requires_operator_login_and_never_calls_before_auth(panel):
    client,_,_=panel
    class Balance:
        calls=0
        def read(self):
            self.calls+=1
            return {'status':'ok','available_balance':'12.30','arrears_balance':'0','checked_at':1000}
    source=Balance();client.app.supplier_balance=source
    assert client.request('supplier-balance')['status']==401 and source.calls==0
    client.login()
    response=client.request('supplier-balance')
    assert response['body']['data']['available_balance']=='12.30' and source.calls==1
    assert response['headers']['Cache-Control']=='no-store'


def test_same_purchase_note_can_issue_distinct_batches_but_retry_is_idempotent(panel):
    client,store,_=panel;client.login()
    body={'note':'微信昵称','count':1,'purchase_id':'a'*32}
    first=client.request('issue-purchase','POST',body)
    assert first['status']==200
    again=client.request('issue-purchase','POST',body)
    assert again['body']['data']['replayed'] is True
    assert again['body']['data']['codes'][0]['id']==first['body']['data']['codes'][0]['id']
    body['purchase_id']='b'*32
    second=client.request('issue-purchase','POST',body)
    assert second['body']['data']['codes'][0]['id']!=first['body']['data']['codes'][0]['id']
    records=client.request('codes?search=微信昵称')['body']['data']
    assert records['total']==2
    assert all(row['order_ref']=='微信昵称' for row in records['items'])
    assert client.app.credits.code_status(records['items'][0]['id'])['order_ref']=='微信昵称'
    body['note']='另一人'
    assert client.request('issue-purchase','POST',body)['status']==409
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]==2


@pytest.mark.parametrize('purchase_id',[None,'',123,'x'*32])
def test_purchase_id_is_required_and_validated(panel,purchase_id):
    client,store,_=panel;client.login()
    assert client.request('issue-purchase','POST',{'note':'测试','count':1,'purchase_id':purchase_id})['status']==400
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]==0


def test_purchase_issuance_still_requires_operator_session_origin_and_csrf(panel):
    client,store,_=panel
    body={'note':'blocked','count':1,'purchase_id':'c'*32}
    state=client.request();client.csrf=state['body']['data']['csrf']
    assert client.request('issue-purchase','POST',body)['status']==401
    client.login()
    assert client.request('issue-purchase','POST',body,csrf=False)['status']==403
    assert client.request('issue-purchase','POST',body,origin='https://evil.invalid')['status']==403
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM redemption_codes').fetchone()[0]==0
