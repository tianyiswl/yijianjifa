"""Ordinary preparation follows saved login, without granting platform transports."""
import pytest

from test_publish_preparation import setup
from test_publish_reschedule import missed
from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
from yijianjifa.app_services.publish_locations import PublishLocations
from yijianjifa.app_services.publish_planning import PublishPlanning
from yijianjifa.app_services.publish_reschedule import reschedule_local
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.storage.db import session_scope


def unavailable_login(engine, account_id, state):
    with session_scope(engine) as session:
        account = session.get(AccountRow, account_id)
        if state == 'missing_session':
            account.session_ref = None
        else:
            account.login_state = state


@pytest.mark.parametrize('ordinary', ['pending', 'not_found'])
def test_valid_login_prepares_ordinary_without_a_separate_capability_probe(db_engine, app_paths, tmp_path, ordinary):
    prep, account, _, params, now = setup(db_engine, app_paths, tmp_path)
    accounts = AccountService(db_engine)
    accounts.update_capabilities(account.id, CapabilitySnapshot(ordinary=ordinary))
    result = prep.prepare(params, now=now)
    assert result['locally_valid'] and result['token']
    assert result['items'][0]['blockers'] == []
    assert not result['ready_to_submit'] and not result['submitted']
    assert accounts.get(account.id).capabilities.ordinary == ordinary


@pytest.mark.parametrize('ordinary', ['pending', 'not_found'])
def test_valid_login_can_read_and_apply_ordinary_metadata_without_a_probe(db_engine, app_paths, tmp_path, ordinary):
    _, account, video, _, now = setup(db_engine, app_paths, tmp_path)
    AccountService(db_engine).update_capabilities(account.id, CapabilitySnapshot(ordinary=ordinary))
    declarations = CommerceDeclarations(db_engine, capability='ordinary', fetch=lambda _: {
        'required': False, 'items': [{'id': 'ai', 'label': '内容由AI生成'}]})
    locations = PublishLocations(db_engine, fetch=lambda *_: [{'id': 'poi', 'name': '三千海', 'address': '测试地址'}])
    declaration = declarations.query({'account_id': account.id}, now=now)
    location = locations.search({'account_id': account.id, 'query': '三千海'}, now=now)
    assert declarations.resolve(account.id, declaration['token'], [video.id], 'ai', {}, now=now)['items'][video.id]['id'] == 'ai'
    assert locations.resolve(account.id, {'token': location['token'], 'id': 'poi'}, now=now)['name'] == '三千海'


@pytest.mark.parametrize('state', ['expired', 'pending_verification', 'missing_session'])
def test_invalid_login_blocks_ordinary_preparation_and_metadata_even_with_old_available_capability(db_engine, app_paths, tmp_path, state):
    prep, account, video, params, now = setup(db_engine, app_paths, tmp_path)
    declarations = CommerceDeclarations(db_engine, capability='ordinary', fetch=lambda _: {
        'required': False, 'items': [{'id': 'ai', 'label': '内容由AI生成'}]})
    locations = PublishLocations(db_engine, fetch=lambda *_: [{'id': 'poi', 'name': '三千海', 'address': '测试地址'}])
    declaration = declarations.query({'account_id': account.id}, now=now)
    location = locations.search({'account_id': account.id, 'query': '三千海'}, now=now)
    unavailable_login(db_engine, account.id, state)
    result = prep.prepare(params, now=now)
    assert not result['locally_valid'] and result['token'] is None
    assert 'account_needs_verification' in result['items'][0]['blockers']
    for operation in (
        lambda: declarations.query({'account_id': account.id}, now=now),
        lambda: declarations.resolve(account.id, declaration['token'], [video.id], 'ai', {}, now=now),
        lambda: locations.search({'account_id': account.id, 'query': '三千海'}, now=now),
        lambda: locations.resolve(account.id, {'token': location['token'], 'id': 'poi'}, now=now),
    ):
        with pytest.raises(ValueError, match='needs verification'):
            operation()


@pytest.mark.parametrize('ordinary', ['pending', 'not_found'])
def test_valid_login_can_reschedule_a_never_submitted_ordinary_item_without_a_probe(db_engine, app_paths, tmp_path, ordinary):
    task, account, video, now = missed(db_engine, app_paths, tmp_path)
    AccountService(db_engine).update_capabilities(account.id, CapabilitySnapshot(ordinary=ordinary))
    params = dict(id=task['id'], item_key=account.id+':'+video.id, mode='publish_now', when=None, software_consent=False)
    with db_engine.begin() as connection:
        result = reschedule_local(connection, params, now=now)
    assert result['state'] == 'paused' and not result['submitted']


@pytest.mark.parametrize('state', ['expired', 'pending_verification', 'missing_session'])
def test_invalid_login_still_blocks_ordinary_rescheduling(db_engine, app_paths, tmp_path, state):
    task, account, video, now = missed(db_engine, app_paths, tmp_path)
    unavailable_login(db_engine, account.id, state)
    params = dict(id=task['id'], item_key=account.id+':'+video.id, mode='publish_now', when=None, software_consent=False)
    with db_engine.begin() as connection:
        with pytest.raises(ValueError, match='account needs verification'):
            reschedule_local(connection, params, now=now)


def test_login_does_not_grant_commerce_capability(db_engine, app_paths, tmp_path):
    _, account, _, params, now = setup(db_engine, app_paths, tmp_path)
    AccountService(db_engine).update_capabilities(account.id, CapabilitySnapshot())
    result = PublishPlanning(db_engine).preview(params, now=now, capability='commerce')
    assert 'commerce_capability_unverified' in result['items'][0]['blockers']
    with pytest.raises(ValueError, match='declaration account needs verification'):
        CommerceDeclarations(db_engine).query({'account_id': account.id}, now=now)
