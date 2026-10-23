from test_operator_web import panel
from cryptography.fernet import Fernet
from yijianjifa_cloud_voice.operator_web import _DOWNLOAD_URL


def click(client, *, method='GET', query='', host='127.0.0.1:18771', prefetch=False):
    result={}
    env={'PATH_INFO':'/operator/download/windows', 'REQUEST_METHOD':method,
         'QUERY_STRING':query, 'HTTP_HOST':host, 'HTTP_COOKIE':client.cookie}
    if prefetch: env['HTTP_SEC_PURPOSE']='prefetch'
    def start(status, headers): result.update(status=int(status.split()[0]),headers=dict(headers))
    result['raw']=b''.join(client.app(env,start))
    return result


def test_public_click_redirect_and_persistence(panel):
    client,store,_=panel
    for i in range(3):
        result=click(client)
        assert result['status']==302
        assert result['headers']['Location']==_DOWNLOAD_URL
        assert result['headers']['Cache-Control']=='no-store'
    assert client.request('download-stats')['status']==401
    client.login()
    assert client.request('download-stats')['body']['data']['clicks']==3
    # New app/process using same database must retain counters.
    from yijianjifa_cloud_voice.operator_web import create_operator_app
    old=client.app
    client.app=create_operator_app(store,password_hash=old.password_hash,operator_code_key=Fernet.generate_key(),origin=old.origin,test_mode=True)
    assert client.request('download-stats')['body']['data']['clicks']==3


def test_head_prefetch_invalid_and_test_clicks_do_not_pollute(panel):
    client,store,_=panel
    assert click(client,method='HEAD')['status']==302
    assert click(client,prefetch=True)['status']==302
    assert click(client,method='POST')['status']==405
    assert click(client,host='evil.test')['status']==403
    assert click(client,query='url=https://evil.test')['status']==400
    assert click(client,query='test=1')['status']==401
    client.login()
    assert click(client,query='test=1')['status']==302
    assert client.request('download-stats')['body']['data']['clicks']==0
    with store.connect() as conn:
        assert conn.execute("SELECT count FROM download_clicks WHERE kind='test'").fetchone()[0]==1
