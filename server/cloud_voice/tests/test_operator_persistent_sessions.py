from cryptography.fernet import Fernet
from yijianjifa_cloud_voice.auth import _hash_password
from yijianjifa_cloud_voice.operator_web import create_operator_app
from yijianjifa_cloud_voice.store import Store
from test_operator_web import Client, PASSWORD, ORIGIN


def test_thirty_day_session_survives_restart_and_logout_revokes_every_worker(tmp_path):
    clock=[1000.0]; path=tmp_path/'voice.db'
    settings=dict(password_hash=_hash_password(PASSWORD),operator_code_key=Fernet.generate_key(),origin=ORIGIN,test_mode=True)
    def restart():return Client(create_operator_app(Store(path,clock=lambda:clock[0]),**settings))
    client=restart();client.login();cookie=client.cookie
    # Persistent cookie is retained by a newly opened browser.
    assert 'Max-Age=2592000' in client.request('login','POST',{'password':PASSWORD})['headers']['Set-Cookie']
    cookie=client.cookie
    clock[0]+=86400*29
    other=restart();other.cookie=cookie
    state=other.request()['body']['data'];assert state['authenticated'] is True
    other.csrf=state['csrf'];assert other.request('codes')['status']==200
    assert other.request('logout','POST')['status']==200
    # A stale copy and an already running worker must both reject the token.
    client.cookie=cookie;assert client.request('codes')['status']==401
    again=restart();again.cookie=cookie;assert again.request('codes')['status']==401


def test_fixed_thirty_day_expiry_and_password_rotation(tmp_path):
    clock=[1000.0];store=Store(tmp_path/'voice.db',clock=lambda:clock[0])
    settings=dict(password_hash=_hash_password(PASSWORD),operator_code_key=Fernet.generate_key(),origin=ORIGIN)
    def restart():return Client(create_operator_app(store,**settings))
    client=restart();client.login();cookie=client.cookie
    clock[0]+=2591999;assert client.request('codes')['status']==200
    clock[0]+=1;client=restart();client.cookie=cookie;assert client.request('codes')['status']==401
    client.login();cookie=client.cookie
    settings['password_hash']=_hash_password('changed-owner-password')
    client=restart();client.cookie=cookie;assert client.request('codes')['status']==401


def test_database_contains_no_bearer_cookie_and_tampering_is_rejected(tmp_path):
    store=Store(tmp_path/'voice.db');app=create_operator_app(store,password_hash=_hash_password(PASSWORD),operator_code_key=Fernet.generate_key(),origin=ORIGIN)
    client=Client(app);client.login();cookie=client.cookie
    token=cookie.split('=',1)[1]
    with store.connect() as conn:
        rows=conn.execute('SELECT * FROM operator_sessions').fetchall()
        assert len(rows)==1
        assert token not in str([tuple(row) for row in rows])
    client.cookie=cookie+'tampered';assert client.request('codes')['status']==401
