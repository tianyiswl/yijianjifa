"""Selecting built-in labels needs no session and grants no publish permission."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_publish_preparation import setup
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.douyin_declaration_catalog import catalog
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def query(api, account_id, **options):
    result = api.dispatch(json.dumps({'v': 1, 'request_id': 'builtin-declaration',
        'operation': 'publish.declarations.query', 'params': {'account_id': account_id, **options}}), trusted_origin=True)
    assert result['ok'], result
    return result['data']


@pytest.mark.parametrize('state', ['expired', 'pending_verification', 'missing_session'])
def test_offline_catalog_is_selectable_without_login_but_preparation_remains_blocked(db_engine, app_paths, tmp_path, state):
    _, account, video, params, now = setup(db_engine, app_paths, tmp_path)
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        if state == 'missing_session': row.session_ref = None
        else: row.login_state = state
    class Metadata:
        def __call__(self, *args): pytest.fail('built-in declaration must never open a platform window')
        def close(self): pass
    class Provider:
        metadata = Metadata()
    api = DesktopApi(db_engine, app_paths, login_provider=Provider())
    try:
        data = query(api, account.id, cache_only=True)
        assert data['source'] == 'builtin' and data['available'] and not data['required']
        assert data['items'] == catalog()['items'] and len(data['items']) == 6
        selected = data['items'][0]['id']
        resolved = api._publish_declarations.resolve(account.id, data['token'], [video.id], selected, {}, now=now + timedelta(days=90))
        assert resolved['items'][video.id]['label'] == '内容由AI生成'
        params['declarations'] = {account.id: {'token': data['token'], 'batch': selected, 'items': {}}}
        prepared = api._publish_preparation.prepare(params, now=now)
        assert not prepared['locally_valid'] and not prepared['token']
        assert 'account_needs_verification' in prepared['items'][0]['blockers']
        with session_scope(db_engine) as db:
            assert not list(db.scalars(select(Setting).where(Setting.key.startswith('publish-metadata:'))))
    finally: api.close(5)


def test_same_catalog_scoped_references_preserve_none_and_reject_forgery_or_account_change(db_engine, app_paths, tmp_path):
    _, a, video, _, now = setup(db_engine, app_paths, tmp_path)
    b = AccountService(db_engine).create('douyin', 'other-public-uid', '另一个抖音账号')
    bili = AccountService(db_engine).create('bilibili', 'bili-public-uid', 'B站账号')
    api = DesktopApi(db_engine, app_paths)
    try:
        qa, qb = query(api, a.id), query(api, b.id, refresh=True)
        assert qa['items'] == qb['items'] and qa['token'] != qb['token']
        service = api._publish_declarations
        assert service.resolve(a.id, qa['token'], [video.id], None, {}, now=now)['items'][video.id] is None
        with pytest.raises(ValueError, match='another account'):
            service.resolve(b.id, qa['token'], [video.id], qa['items'][0]['id'], {}, now=now)
        with pytest.raises(ValueError, match='actual platform option'):
            service.resolve(a.id, qa['token'], [video.id], 'invented-platform-id', {}, now=now)
        with pytest.raises(ValueError, match='needs verification'):
            service.query({'account_id': bili.id})
        with session_scope(db_engine) as db: db.get(AccountRow, a.id).platform_user_id = 'changed-public-uid'
        with pytest.raises(ValueError, match='account changed'):
            service.resolve(a.id, qa['token'], [video.id], qa['items'][0]['id'], {}, now=now)
    finally: api.close(5)


def test_catalog_returns_independent_copies_with_legacy_label_references():
    one = catalog(); one['items'][0]['label'] = 'mutated'
    other = catalog()
    assert other['items'][0] == {'id': 'ui-v1:3c27c736185b21b0795ae9d89d1e0475da8db1d502388e7e1db18ddbe893b211', 'label': '内容由AI生成'}
