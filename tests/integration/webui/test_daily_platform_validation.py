"""Real local B站 contracts fail before making, login IO or publication."""
import copy
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services import bilibili_static_catalog
from yijianjifa.app_services.library_queries import CommandRejected
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.publish.deliveries import PublishBatchReceiptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.bilibili_multi_publish import BilibiliMultiPublishAdapter


CODE = 'bilibili_schedule_requires_five_minutes'


def call(api, operation, params, rid='local-validation'):
    return api.dispatch(json.dumps(dict(v=1, request_id=rid, operation=operation, params=params)), trusted_origin=True)


@pytest.fixture
def local_bilibili(db_engine, app_paths, tmp_path, monkeypatch):
    account = AccountService(db_engine).create('bilibili', '9876543210', 'Synthetic account')
    with session_scope(db_engine) as session:
        session.get(AccountRow, account.id).session_ref = 'private-session-reference'
    source = tmp_path / 'synthetic-source.mp4'
    source.write_bytes(b'local schema fixture')
    MediaStorageService(db_engine, app_paths, kind='video').import_media(source, keep_copy=False, category_ids=[])
    CopyRepository(db_engine).create(id='C1', title='第一份文案', voice='', body='正文', tags='剪辑', full='')
    adapter = BilibiliMultiPublishAdapter(db_engine, app_paths)
    def forbidden(*args, **kwargs): raise AssertionError('Local validation cannot open a browser or start production')
    monkeypatch.setattr(adapter, '_open', forbidden)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'bilibili': adapter})
    monkeypatch.setattr(api._daily, 'start_check', lambda task_id: None)
    monkeypatch.setattr(api._multi_publish, 'wake', forbidden)
    params = {'mix': {'quantity': 2, 'total_ms': 1000, 'segment_ms': 1000},
              'publishing': {'targets': [{'account_id': account.id, 'platform': 'bilibili'}],
                             'mode': 'platform_schedule', 'time': '11:03', 'day_offset': 1, 'interval_hours': 3}}
    yield api, adapter, account, params, source
    assert api.close(.5)


def payload(account, when):
    return dict(account_id=account.id, platform='bilibili', platform_user_id=account.platform_user_id,
        execution_mode='platform_schedule', scheduled_at=when.isoformat(),
        fields={**bilibili_static_catalog.DEFAULT_FIELDS, 'title': '文案', 'body': '正文', 'tags': '剪辑'})


def test_daily_five_minute_diagnostic_reaches_dispatch_without_leaving_a_batch(local_bilibili, db_engine):
    api, _, account, params, source = local_bilibili
    response = call(api, 'mix.daily.submit', params)
    assert not response['ok'] and response['error']['code'] == CODE
    message = response['error']['message']
    assert all(text in message for text in ('B站', '5分钟', '11:03', '账号', '第1份文案'))
    assert all(secret not in json.dumps(response) for secret in (account.platform_user_id, 'private-session-reference', str(source)))
    assert api._mix.tasks() == []
    with session_scope(db_engine) as session:
        assert list(session.scalars(select(PublishBatchReceiptRow))) == []


def test_daily_five_minute_clock_passes_without_starting_making_or_publishing(local_bilibili):
    api, _, _, params, _ = local_bilibili
    params['publishing']['time'] = '11:05'
    response = call(api, 'mix.daily.submit', params)
    assert response['ok'], response
    assert response['data']['phase'] == 'checking_login'
    assert len(api._mix.tasks()) == 1 and api._mix.tasks()[0].state == 'paused'


@pytest.mark.parametrize('bad_fields', [{'title': '长' * 81}, {'tags': ' '.join(f'tag{i}' for i in range(11))}])
def test_daily_generic_field_diagnostic_identifies_the_actual_copy(local_bilibili, db_engine, bad_fields):
    api, _, _, params, _ = local_bilibili
    params['publishing']['time'] = '11:05'
    fields = dict(title='第二份文案', voice='', body='正文', tags='剪辑', full='')
    fields.update(bad_fields)
    CopyRepository(db_engine).create(id='C2', **fields)
    response = call(api, 'mix.daily.submit', params)
    assert not response['ok'] and response['error']['code'] == 'platform_fields_invalid'
    assert all(text in response['error']['message'] for text in ('B站', '账号', '第2份文案'))
    assert api._mix.tasks() == []


@pytest.mark.parametrize('problem', ['minute', 'timezone', 'second', 'microsecond'])
def test_real_adapter_retains_all_schedule_precision_guards(local_bilibili, problem):
    _, adapter, account, _, _ = local_bilibili
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=3, minute=5, second=0, microsecond=0)
    invalid = {'minute': when.replace(minute=3), 'timezone': when.replace(tzinfo=None),
               'second': when.replace(second=1), 'microsecond': when.replace(microsecond=1)}[problem]
    assert adapter.plan_readiness(payload(account, invalid)) == CODE
    assert adapter.plan_readiness(payload(account, when)) is None


def test_ordinary_multi_publish_uses_the_same_specific_reason(local_bilibili, db_engine):
    api, _, account, _, source = local_bilibili
    record = FinishedVideoService(db_engine).import_external(source, duration_ms=1000, title='独立发布文案', tags='剪辑')
    when = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=3, minute=3, second=0, microsecond=0)
    response = call(api, 'multi_publish.prepare', dict(video_ids=[record.id], targets=[{'account_id': account.id, 'platform': 'bilibili'}],
        common_fields=copy.deepcopy(bilibili_static_catalog.DEFAULT_FIELDS), execution_mode='platform_schedule', scheduled_times={record.id: when.isoformat()}))
    assert response['ok'], response
    target = response['data']['targets'][0]
    assert target['hold_reason'] == CODE and '5分钟' in target['reason_message']


@pytest.mark.parametrize('error_type', [ValueError, CommandRejected])
def test_untrusted_exception_messages_stay_sanitized(local_bilibili, monkeypatch, error_type):
    api, _, _, params, _ = local_bilibili
    def failure(*args):
        error = error_type('platform_fields_invalid')
        error.message = 'private Cookie session /private/path'
        raise error
    monkeypatch.setattr(api, '_apply', failure)
    response = call(api, 'mix.daily.submit', params)
    assert not response['ok'] and 'private' not in json.dumps(response) and 'Cookie' not in json.dumps(response)
