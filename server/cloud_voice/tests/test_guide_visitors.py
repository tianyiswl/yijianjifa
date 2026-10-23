from concurrent.futures import ThreadPoolExecutor

from test_operator_web import panel
from yijianjifa_cloud_voice.operator_web import _GUIDE_URL, create_operator_app
from cryptography.fernet import Fernet


def visit(client, ip='198.51.100.1', *, real='', method='GET', query='', host=None, purpose='', sec_purpose=''):
    result = {}
    env = {'PATH_INFO': '/operator/guide', 'REQUEST_METHOD': method,
           'HTTP_HOST': host or client.app.host, 'QUERY_STRING': query,
           'REMOTE_ADDR': ip, 'HTTP_X_REAL_IP': real,
           'HTTP_PURPOSE': purpose, 'HTTP_SEC_PURPOSE': sec_purpose}
    def start(status, headers):
        result.update(status=int(status.split()[0]), headers=dict(headers))
    b''.join(client.app(env, start))
    return result


def test_repeated_guide_opens_count_and_survive_service_restart(panel):
    client, store, clock = panel
    assert client.request('guide-stats')['status'] == 401
    for _ in range(3):
        result = visit(client)
        assert result['status'] == 302
        assert result['headers']['Location'] == _GUIDE_URL
        assert result['headers']['Cache-Control'] == 'no-store'
    # Counting opens must not depend on proxy headers or a usable client IP.
    assert visit(client, '127.0.0.1')['status'] == 302
    client.login()
    data = client.request('guide-stats')['body']['data']
    assert data['visits'] == 4
    assert data['started_at'] == 1000.0
    clock[0] += 60
    old = client.app
    client.app = create_operator_app(store, password_hash=old.password_hash,
                                    operator_code_key=Fernet.generate_key(), origin=old.origin, test_mode=True)
    assert visit(client)['status'] == 302
    data = client.request('guide-stats')['body']['data']
    assert data['visits'] == 5
    assert data['started_at'] == 1000.0
    assert client.request('download-stats')['body']['data']['clicks'] == 0


def test_head_prefetch_and_rejected_requests_do_not_count_as_guide_opens(panel):
    client, _, _ = panel
    assert visit(client, method='HEAD')['status'] == 302
    assert visit(client, purpose='prefetch')['status'] == 302
    assert visit(client, sec_purpose='prefetch;prerender')['status'] == 302
    assert visit(client, method='POST')['status'] == 405
    assert visit(client, query='url=https://evil.invalid')['status'] == 400
    assert visit(client, host='evil.invalid')['status'] == 403
    client.login()
    assert client.request('guide-stats')['body']['data']['visits'] == 0


def test_old_unique_ip_history_is_not_reported_as_total_opens(panel):
    client, store, clock = panel
    # Historical unique IPs cannot reveal how many repeat opens happened.
    with store.transaction() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS guide_visitors (ip_digest TEXT PRIMARY KEY, first_seen REAL NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS guide_settings (id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL, started_at REAL NOT NULL)')
        conn.execute("INSERT OR IGNORE INTO guide_settings VALUES (1, 'old-salt', 500)")
        conn.execute("INSERT INTO guide_visitors VALUES (?, 500)", ('a' * 64,))
        conn.execute('DROP TABLE guide_clicks')  # Recreate the pre-upgrade database shape.
    clock[0] = 1200.0
    old = client.app
    client.app = create_operator_app(store, password_hash=old.password_hash,
                                    operator_code_key=Fernet.generate_key(), origin=old.origin, test_mode=True)
    client.login()
    assert client.request('guide-stats')['body']['data']['visits'] == 0
    assert visit(client)['status'] == 302
    data = client.request('guide-stats')['body']['data']
    assert data['visits'] == 1
    assert data['started_at'] == 1200.0
    with store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM guide_visitors').fetchone()[0] == 1


def test_simultaneous_guide_opens_do_not_lose_increments(panel):
    client, _, _ = panel
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(lambda _: visit(client)['status'], range(12)))
    assert statuses == [302] * 12
    client.login()
    assert client.request('guide-stats')['body']['data']['visits'] == 12
