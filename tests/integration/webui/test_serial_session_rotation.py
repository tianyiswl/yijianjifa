"""串行自动继续只接纳SDK原guard内提交的连续身份绑定轮换。"""
import hashlib
import json

import pytest
from sqlalchemy import select

from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.webui.douyin_scheduled import persist_refreshed_sessions


def fp(value): return hashlib.sha256((value or '').encode()).hexdigest()


@pytest.mark.parametrize('rounds', [1, 3])
def test_sdk_guard_records_continuous_rotation_without_raw_session_refs(db_engine, app_paths, rounds):
    service = AccountService(db_engine)
    account = service.create('douyin', '123', '合成轮换账号')
    old = service.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'original')
    for i in range(rounds):
        current = service.get(account.id)
        persist_refreshed_sessions(db_engine, app_paths, {account.id: {'original_ref': current.session_ref,
            'platform_user_id': '123', 'session_blob': f'sdk-{i}'.encode()}})
    current = service.get(account.id)
    with session_scope(db_engine) as s:
        audits = list(s.scalars(select(Setting).where(Setting.key.startswith('account.sdk_rotation:'))))
        assert len(audits) == rounds
        assert old not in '\n'.join(row.value_json for row in audits)
        from yijianjifa.features.accounts.session_rotation import allows_rotation
        assert allows_rotation(s, account.id, 'douyin', '123', fp(old), fp(current.session_ref))


@pytest.mark.parametrize('change', ['external', 'gap', 'uid', 'platform', 'stale_sdk'])
def test_external_or_discontinuous_session_change_cannot_be_used_as_rotation(db_engine, app_paths, change):
    service = AccountService(db_engine)
    account = service.create('douyin', '123', '合成轮换账号')
    old = service.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'original')
    persist_refreshed_sessions(db_engine, app_paths, {account.id: {'original_ref': old,
        'platform_user_id': '123', 'session_blob': b'sdk-first'}})
    with session_scope(db_engine) as s:
        assert list(s.scalars(select(Setting).where(Setting.key.startswith('account.sdk_rotation:'))))
    with session_scope(db_engine) as s:
        row = s.get(AccountRow, account.id)
        if change in {'external', 'stale_sdk'}: row.session_ref = 'arbitrary-external-ref'
        if change == 'gap':
            for audit in s.scalars(select(Setting).where(Setting.key.startswith('account.sdk_rotation:'))): s.delete(audit)
        if change == 'uid': row.platform_user_id = '456'
        if change == 'platform': row.platform = 'kuaishou'
    if change == 'stale_sdk':
        persist_refreshed_sessions(db_engine, app_paths, {account.id: {'original_ref': old,
            'platform_user_id': '123', 'session_blob': b'stale-sdk'}})
    current = service.get(account.id)
    with session_scope(db_engine) as s:
        from yijianjifa.features.accounts.session_rotation import allows_rotation
        assert not allows_rotation(s, account.id, 'douyin', '123', fp(old), fp(current.session_ref))


def test_failed_rotation_proof_write_rolls_back_reference(db_engine, app_paths, monkeypatch):
    service = AccountService(db_engine)
    account = service.create('douyin', '123', '合成轮换账号')
    old = service.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'original')
    def broken(*args): raise OSError('合成证明写入失败')
    monkeypatch.setattr('yijianjifa.features.accounts.session_rotation.record_rotation', broken)
    with pytest.raises(OSError, match='合成证明写入失败'):
        persist_refreshed_sessions(db_engine, app_paths, {account.id: {'original_ref': old,
            'platform_user_id': '123', 'session_blob': b'sdk-new'}})
    assert service.get(account.id).session_ref == old
