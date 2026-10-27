"""Existing publishing reads feed one identity/version-bound account status."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.chromium_publish_session import LiveSession
from yijianjifa.webui import (channels_multi_publish, chromium_multi_publish,
                              bilibili_multi_publish, kuaishou_multi_publish)
from test_douyin_login_bootstrap import login_browser, surface
from yijianjifa.webui.chromium_publish import observe_login_session as actual_login_observer


ADAPTERS = [
    ('channels', channels_multi_publish.ChannelsMultiPublishAdapter, 'https://channels.weixin.qq.com'),
    ('douyin', chromium_multi_publish.ChromiumMultiPublishAdapter, 'https://creator.douyin.com'),
    ('bilibili', bilibili_multi_publish.BilibiliMultiPublishAdapter, 'https://member.bilibili.com'),
    ('kuaishou', kuaishou_multi_publish.KuaishouMultiPublishAdapter, 'https://cp.kuaishou.com'),
]


@pytest.fixture(params=ADAPTERS, ids=[row[0] for row in ADAPTERS])
def observed_creator(request, db_engine, app_paths, monkeypatch):
    platform, factory, origin = request.param
    service = AccountService(db_engine)
    account = service.create(platform, '123', '合成账号')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.login_state = 'valid'; row.session_ref = 'synthetic-original'
    identity = dict(origin=origin, logged_in=True, expired=False, uid='123',
                    profile_uid='123', uid_after='123', nickname='合成账号',
                    status=0, profile_status=0, after_status=0)
    state = {'challenge': {}, 'error': None, 'change_archive': False, 'reads': []}
    class Page:
        frames = []
        def evaluate(self, code, args=None):
            state['reads'].append(datetime.now(timezone.utc))
            if state['change_archive']:
                with session_scope(db_engine) as db:
                    db.get(AccountRow, account.id).session_ref = 'synthetic-new-login'
            if state['error']: raise state['error']
            if code in {getattr(channels_multi_publish, 'CHALLENGE', ''),
                         chromium_multi_publish.legacy.CHALLENGE,
                         bilibili_multi_publish.BILIBILI_CHALLENGE,
                         kuaishou_multi_publish.browser.CHALLENGE}:
                return state['challenge']
            return dict(identity)
    adapter = factory(db_engine, app_paths)
    monkeypatch.setattr(adapter, '_trace', lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter, '_frame', lambda s: s.page, raising=False)
    s = LiveSession(dict(platform=platform, account_id=account.id, platform_user_id='123'), {})
    s.original_ref = 'synthetic-original'; s.page = Page()
    def read():
        return adapter._read_identity(s) if platform == 'douyin' else adapter._identity(s)
    return SimpleNamespace(platform=platform, adapter=adapter, session=s, identity=identity,
                           state=state, service=service, account=account, read=read)


def check(case):
    current = case.service.get(case.account.id)
    return case.service.login_checks([current])[current.id]


@pytest.mark.parametrize('inspection', [False, True])
def test_existing_exact_identity_read_updates_shared_status_before_any_upload(observed_creator, inspection):
    c = observed_creator; c.adapter.inspection_only = inspection
    c.read()
    health = check(c)
    assert health is not None and health['state'] == 'valid'
    assert datetime.fromisoformat(health['checked_at']) <= c.state['reads'][0]
    assert len(c.state['reads']) == 1 and not c.session.uploaded and not c.session.final


def test_wrong_verified_account_is_recorded_without_relaxing_the_frozen_uid(observed_creator):
    c = observed_creator; c.identity.update(uid='999', profile_uid='999', uid_after='999')
    if c.platform == 'douyin':
        # This low-level Douyin reader retains its original raw-result contract.
        assert c.read()['uid'] == '999'
    else:
        with pytest.raises(Exception): c.read()
    assert check(c)['state'] == 'identity_mismatch'
    assert c.session.payload['platform_user_id'] == '123' and not c.session.resume_authorized


@pytest.mark.parametrize('failure', ['missing', 'network'])
def test_unknown_identity_and_read_failures_do_not_expire_saved_login(observed_creator, failure):
    c = observed_creator
    if failure == 'missing': c.identity.clear()
    else: c.state['error'] = TimeoutError('synthetic private failure')
    try: c.read()
    except Exception: pass
    assert check(c)['state'] == 'unavailable'
    assert c.service.get(c.account.id).login_state == 'valid'


def test_explicit_login_page_updates_shared_status(observed_creator):
    c = observed_creator; c.state['challenge'] = {'login': True}
    assert c.adapter._challenge(c.session) == 'login'
    assert check(c)['state'] == 'login_required'
    assert c.service.get(c.account.id).login_state == 'expired'


def test_verification_challenge_is_unavailable_not_expired(observed_creator):
    c = observed_creator; c.state['challenge'] = {'complex': True}
    assert c.adapter._challenge(c.session) == 'complex'
    assert check(c)['state'] == 'unavailable'
    assert c.service.get(c.account.id).login_state == 'valid'


def test_read_from_an_old_open_page_cannot_replace_new_login_status(observed_creator):
    c = observed_creator; c.state['change_archive'] = True
    c.read()
    assert check(c) is None
    assert c.session.original_ref == 'synthetic-original'


def test_no_challenge_is_not_sufficient_to_claim_a_valid_login(observed_creator):
    c = observed_creator
    assert c.adapter._challenge(c.session) is None
    assert check(c) is None


def test_new_saved_archive_keeps_the_time_of_its_real_successful_identity_read(observed_creator, monkeypatch):
    c = observed_creator; c.read()
    original = check(c)
    value = dict(platform_user_id='123', nickname='合成账号', session_blob=b'synthetic-session',
                 original_ref=c.session.original_ref, login_observed_at=datetime.fromisoformat(original['checked_at']))
    c.adapter.refreshed[c.account.id] = value
    if c.platform in {'channels', 'bilibili'}:
        monkeypatch.setattr(channels_multi_publish.creator_auth, 'decode_session', lambda *args:None)
    elif c.platform == 'kuaishou':
        monkeypatch.setattr(kuaishou_multi_publish.auth, 'decode_session', lambda *args:None)
    c.adapter._persist_sessions()
    current = c.service.get(c.account.id)
    assert current.session_ref != c.session.original_ref
    assert check(c) == original


@pytest.mark.parametrize('new_state', ['unavailable', 'login_required', 'identity_mismatch'])
def test_older_success_cannot_rotate_archive_or_replace_a_newer_login_observation(observed_creator, monkeypatch, new_state):
    c = observed_creator; c.read()
    checked_at = datetime.fromisoformat(check(c)['checked_at'])
    c.service.observe_login_check(c.account.id, platform_user_id='123', session_ref=c.session.original_ref,
                                  state=new_state, observed_at=checked_at + timedelta(seconds=1))
    expected = check(c)
    c.adapter.refreshed[c.account.id] = dict(platform_user_id='123', nickname='合成账号',
        session_blob=b'synthetic-session', original_ref=c.session.original_ref, login_observed_at=checked_at)
    if c.platform in {'channels', 'bilibili'}:
        monkeypatch.setattr(channels_multi_publish.creator_auth, 'decode_session', lambda *args:None)
    elif c.platform == 'kuaishou':
        monkeypatch.setattr(kuaishou_multi_publish.auth, 'decode_session', lambda *args:None)
    c.adapter._persist_sessions()
    assert c.service.get(c.account.id).session_ref == c.session.original_ref
    assert check(c) == expected


@pytest.mark.parametrize('outcome', ['valid', 'login_required', 'identity_mismatch', 'unavailable'])
def test_existing_account_login_and_backend_provider_records_its_actual_identity_read(
        login_browser, db_engine, monkeypatch, outcome):
    from yijianjifa.webui import chromium_publish as module
    b = login_browser; service = AccountService(db_engine)
    account = service.create('douyin', '123', '合成账号')
    with session_scope(db_engine) as db:
        db.get(AccountRow, account.id).session_ref = 'synthetic-login-archive'
    monkeypatch.setattr(module, 'restore_account_session', lambda *args:None)
    monkeypatch.setattr(module, 'session_blob', lambda *args:b'synthetic-session')
    monkeypatch.setattr(module, 'observe_login_session', actual_login_observer)
    reads = []
    def read(*args):
        reads.append(datetime.now(timezone.utc))
        if outcome != 'valid': b.cancel.set()
        if outcome == 'unavailable': raise TimeoutError('synthetic network failure')
        uid = '999' if outcome == 'identity_mismatch' else '123'
        status = 8 if outcome == 'login_required' else 0
        return dict(uid=uid, uid_after=uid, status=status, after_status=status,
                    profile_status=0, nickname='合成账号')
    monkeypatch.setattr(module, 'evaluate_script', read)
    b.page.state = surface(dashboard=True)
    b.login(account.id, b.cancel)
    current = service.get(account.id)
    health = service.login_checks([current])[current.id]
    assert health is not None and health['state'] == outcome
    assert datetime.fromisoformat(health['checked_at']) <= reads[-1]
