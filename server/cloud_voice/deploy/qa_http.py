#!/usr/bin/env python3
"""Private, disposable HTTP acceptance; never connects to production storage.

Run with the deployed venv and this directory's production.py. Only QA result
names are printed. Passwords, tokens, redemption codes and responses stay in RAM.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request


PORT = 18770
BASE = f'http://127.0.0.1:{PORT}'


def require(condition, label):
    if not condition:
        raise AssertionError(label)


@contextmanager
def isolated_environment(database):
    names = [name for name in os.environ
             if name.startswith('YJJF_CLOUD_') or name.startswith('YJJF_VOLC_')]
    before = {name: os.environ[name] for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ['YJJF_CLOUD_DATABASE'] = str(database)
        yield
    finally:
        for name in list(os.environ):
            if name.startswith('YJJF_CLOUD_') or name.startswith('YJJF_VOLC_'):
                os.environ.pop(name, None)
        os.environ.update(before)


def request(method, path, payload=None, token=None, *, origin=None, raw=None):
    data = (json.dumps(payload).encode() if payload is not None else raw)
    headers = {'Content-Type': 'application/json', 'Connection': 'close'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if origin:
        headers['Origin'] = origin
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    # Never inherit a machine proxy or send bearer-bearing requests to redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(2 * 1024 * 1024)
        try:
            result = json.loads(body)
        except (UnicodeError, ValueError):
            result = {}
        return response.status, dict(response.headers), result


def load_production():
    source = Path(__file__).with_name('production.py')
    require(source.is_file(), 'production entry is missing')
    spec = importlib.util.spec_from_file_location('cloud_voice_production_qa', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oversized_request_status():
    # Send the declared oversize header only: Waitress must reject before it
    # receives a body. Uploading a full body can yield BrokenPipe on rejection.
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
    try:
        connection.putrequest('POST', '/v1/accounts')
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', str(1024 * 1024 + 1))
        connection.putheader('Connection', 'close')
        connection.endheaders()
        response = connection.getresponse()
        response.read(8192)
        return response.status
    finally:
        connection.close()


def run_checks(production, database):
    from yijianjifa_cloud_voice.credits import CreditService
    from yijianjifa_cloud_voice.store import Store

    status, headers, _ = request('GET', '/missing')
    require(status == 404 and headers.get('Cache-Control') == 'no-store', 'readiness')
    require(request('GET', '/v1/me')[0] == 401, 'authentication required')

    password = secrets.token_urlsafe(24)
    status, _, account = request('POST', '/v1/accounts',
                                 {'username': 'qa_owner', 'password': password})
    require(status == 201, 'registration')
    status, _, login = request('POST', '/v1/sessions',
                               {'username': 'qa_owner', 'password': password})
    require(status == 200 and login.get('account_id') == account.get('account_id'), 'login')
    token = login['access_token']
    status, _, other = request('POST', '/v1/accounts',
                               {'username': 'qa_other', 'password': secrets.token_urlsafe(24)})
    require(status == 201, 'second isolated account')

    # This database is disposable, not the installed service's production DB.
    code = CreditService(Store(database)).issue(1, order_ref='QA-NOT-A-PAYMENT')[0]['code']
    redeem = {'code': code, 'idempotency_key': 'qa-redemption-0001'}
    status, _, first = request('POST', '/v1/redemptions', redeem, token)
    require(status == 200 and first.get('added_words') == 10000 and
            first.get('available_words') == 10000 and first.get('replayed') is False,
            'single redemption')
    status, _, replay = request('POST', '/v1/redemptions', redeem, token)
    require(status == 200 and replay.get('id') == first.get('id') and
            replay.get('available_words') == 10000 and replay.get('replayed') is True,
            'redemption replay')
    require(request('POST', '/v1/redemptions', redeem, other['access_token'])[0] == 409,
            'cross-account redemption rejection')
    require(request('GET', '/v1/me', token=other['access_token'])[2].get('available_words') == 0,
            'cross-account balance isolation')
    require(request('GET', '/v1/usage', token=other['access_token'])[2].get('total') == 0,
            'cross-account ledger isolation')
    status, _, usage = request('GET', '/v1/usage', token=token)
    require(status == 200 and usage.get('total') == 1, 'one ledger entry')
    for method, path, payload in (('GET', '/v1/voices', None),
                                  ('POST', '/v1/batches', {'idempotency_key': 'qa-batch', 'items': []})):
        status, _, result = request(method, path, payload, token)
        require(status == 503 and result.get('error', {}).get('code') == 'service_not_configured',
                'generation stays unavailable')
    require(request('GET', '/v1/me', token=token, origin='https://invalid.example')[0] == 403,
            'browser origin rejection')
    require(oversized_request_status() == 413, 'HTTP request body bound')

    # Short bounded read-only concurrency, below existing authentication limits.
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: request('GET', '/v1/me', token=token), range(8)))
    require(all(status == 200 and result.get('available_words') == 10000
                for status, _, result in responses), 'eight read-only requests')

    # The production factory must reject concurrent ownership of this exact DB.
    try:
        duplicate = production.build_server(port=PORT)
    except RuntimeError:
        pass
    else:
        duplicate.close()
        raise AssertionError('duplicate database process lock')

    backup = database.with_name('qa-restored.db')
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    os.chmod(backup, 0o600)
    with sqlite3.connect(backup) as connection:
        require(connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                'backup integrity')
        require(connection.execute('SELECT credits,used,reserved FROM accounts WHERE id=?',
                                   (account['account_id'],)).fetchone() == (10000, 0, 0),
                'restored balance')
        require(connection.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 1,
                'restored ledger')
    return backup, password, account['account_id']


def main():
    if len(sys.argv) != 1:
        print('QA accepts no arguments or production database paths.', file=sys.stderr)
        return 2
    production = load_production()
    # Refuse to touch any service already occupying the fixed QA listener.
    with socket.socket() as guard:
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        guard.bind(('127.0.0.1', PORT))
    with tempfile.TemporaryDirectory(prefix='yjjf-cloud-http-qa-') as directory:
        os.chmod(directory, 0o700)
        database = Path(directory) / 'qa.db'
        with isolated_environment(database):
            runtime = production.build_server(port=PORT)
            server_thread = threading.Thread(target=runtime.run, daemon=True)
            server_thread.start()
            try:
                backup, password, account_id = run_checks(production, database)
            finally:
                runtime.close()
                server_thread.join(timeout=5)
            require(not server_thread.is_alive(), 'QA server shutdown')
            # Recover the consistent backup through the real app and HTTP stack.
            os.environ['YJJF_CLOUD_DATABASE'] = str(backup)
            restored = production.build_server(port=PORT)
            restored_thread = threading.Thread(target=restored.run, daemon=True)
            restored_thread.start()
            try:
                status, _, account = request('POST', '/v1/sessions',
                                             {'username': 'qa_owner', 'password': password})
                require(status == 200 and account.get('account_id') == account_id,
                        'restored HTTP account login')
                status, _, balance = request('GET', '/v1/me', token=account['access_token'])
                require(status == 200 and balance.get('available_words') == 10000,
                        'restored HTTP balance')
                require(request('GET', '/v1/usage', token=account['access_token'])[2].get('total') == 1,
                        'restored HTTP ledger')
            finally:
                restored.close()
                restored_thread.join(timeout=5)
            require(not restored_thread.is_alive(), 'restored QA server shutdown')
    print('PASS: isolated HTTP, concurrency, lock and backup checks; no paid generation.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        # Assertion labels are fixed strings, never provider or HTTP body text.
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print('FAIL: QA infrastructure (' + type(exc).__name__ + '); no response or secret printed.',
              file=sys.stderr)
        raise SystemExit(1)
