"""Free local use ignores legacy annual data and never rewrites it."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.use_term import UseTerm
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


EXPIRED_RECEIPT = json.dumps({
    'started_at': '2020-01-01T00:00:00+00:00',
    'expires_at': '2021-01-01T00:00:00+00:00',
    'last_seen_at': '2026-01-01T00:00:00+00:00',
})


def settings(engine):
    with session_scope(engine) as session:
        return {row.key: row.value_json for row in session.scalars(select(Setting))}


@pytest.mark.parametrize('legacy', [None, EXPIRED_RECEIPT, 'broken', '[]'])
def test_free_use_is_read_only_for_new_expired_or_corrupt_settings(db_engine, tmp_path, legacy):
    # Reintroducing annual receipt creation/parsing/heartbeats would either deny
    # work or change settings; neither may affect a free installation.
    with session_scope(db_engine) as session:
        session.add(Setting(key='user-preference', value_json='{"keep":"exact bytes"}'))
        if legacy is not None:
            session.add(Setting(key=UseTerm.KEY, value_json=legacy))
    account = AccountService(db_engine).create('douyin', 'synthetic-user', '保留账号')
    copy = CopyRepository(db_engine).create(id='keep-copy', title='保留文案', voice='', body='正文', tags='', full='')
    queue = MixQueue(db_engine, SimpleNamespace(render_item=lambda *args: None))
    task = queue.enqueue({'quantity': 2})
    material = tmp_path / 'keep-material.mp4'
    material.write_bytes(b'synthetic user material')
    original = settings(db_engine)
    service = UseTerm(db_engine)
    for now in (datetime(2035, 3, 1, tzinfo=timezone.utc), datetime(2024, 2, 29, tzinfo=timezone.utc)):
        status = service.status(now=now)
        assert status['valid'] is True and status['expired'] is False
        assert settings(db_engine) == original
        assert status.get('mode') == 'free' and status.get('free') is True
        assert status['started_at'] is None and status['expires_at'] is None
        assert service.allowed() is True
    assert settings(db_engine) == original
    assert AccountService(db_engine).get(account.id).nickname == '保留账号'
    assert CopyRepository(db_engine).get(copy.id).body == '正文'
    assert queue.get(task.id).state == 'waiting'
    assert material.read_bytes() == b'synthetic user material'


@pytest.mark.parametrize('legacy', [EXPIRED_RECEIPT, 'broken'])
def test_free_desktop_operations_reach_normal_validation_with_legacy_receipt(db_engine, app_paths, legacy):
    # The bridge must reach ordinary payload validation rather than an annual gate.
    from yijianjifa.app_services.desktop_api import DesktopApi
    with session_scope(db_engine) as session:
        session.add(Setting(key=UseTerm.KEY, value_json=legacy))
    api = DesktopApi(db_engine, app_paths)
    try:
        for op in ('mix.submit', 'mix.daily.submit', 'mix.daily.continue', 'task.resume',
                   'mix.publish.retry', 'publish.execution.start', 'publish.confirm', 'commerce.confirm'):
            result = api.dispatch(json.dumps(dict(v=1, request_id='free-validation', operation=op, params={})), trusted_origin=True)
            assert not result['ok'] and result['error']['code'] == 'invalid_request', (op, result)
        result = api.dispatch(json.dumps(dict(v=1, request_id='free-status', operation='product.use_term', params={})), trusted_origin=True)
        assert result['ok'] and result['data']['mode'] == 'free' and not result['data']['expired']
        assert settings(db_engine)[UseTerm.KEY] == legacy
    finally:
        api.close(.1)


def test_queue_runtime_guard_still_pauses_without_starting_next_video(db_engine):
    calls = []
    queue = MixQueue(db_engine, SimpleNamespace(render_item=lambda *args: calls.append(args)))
    task = queue.enqueue({'quantity': 2})
    queue.can_start = lambda: False
    queue.run_once_until_idle()
    assert not calls and queue.get(task.id).state == 'paused'
