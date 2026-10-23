import json

import pytest

from yijianjifa.app_services.account_access import AccountAccess
from yijianjifa.features.accounts.service import AccountService


def test_account_access_lists_supported_platform_metadata_without_secrets_or_fake_login(db_engine):
    core = AccountService(db_engine)
    accounts = [core.create(platform, 'private-' + platform, platform, group_name=platform)
                for platform in ('douyin', 'kuaishou', 'channels', 'bilibili')]
    access = AccountAccess(db_engine)
    all_rows = access.list(query='', platform='', group='', state='', offset=0, limit=20)
    assert all_rows['total'] == 4
    assert {row['platform'] for row in all_rows['items']} == {'douyin', 'kuaishou', 'channels', 'bilibili'}
    only = access.list(query='', platform='channels', group='', state='', offset=0, limit=20)
    assert only['items'][0]['platform'] == 'channels' and only['total'] == 1
    for account in accounts:
        row = access.get(account.id)
        assert row['login_state'] == 'pending_verification'
        assert row['capabilities']['ordinary'] == 'pending'
        public = json.dumps(row)
        assert account.platform_user_id not in public and 'session_ref' not in public
    assert core.list() == accounts or {a.id for a in core.list()} == {a.id for a in accounts}


def test_account_access_rejects_unknown_platform_instead_of_showing_douyin(db_engine):
    core = AccountService(db_engine)
    core.create('douyin', 'a', '抖音')
    unknown = core.create('wechat', 'b', '未知平台')
    access = AccountAccess(db_engine)
    with pytest.raises(ValueError):
        access.list(query='', platform='wechat', group='', state='', offset=0, limit=20)
    with pytest.raises((ValueError, KeyError)):
        access.get(unknown.id)
    assert access.list(query='', platform='', group='', state='', offset=0, limit=20)['total'] == 1


@pytest.mark.parametrize(('filters', 'expected_names', 'expected_total'), [
    ({'platform': 'douyin', 'limit': 1}, ['alpha'], 2),
    ({'platform': 'douyin', 'query': 'beta', 'group': '品牌', 'state': 'relogin'}, ['beta'], 1),
    ({'platform': 'douyin', 'group': '海外'}, [], 0),
    ({'platform': 'kuaishou', 'query': '不存在'}, [], 0),
    ({'group': '品牌', 'offset': 1, 'limit': 1}, ['快手账号'], 2),
])
def test_saved_groups_remain_available_across_platform_filters_and_pages(
        db_engine, filters, expected_names, expected_total):
    core = AccountService(db_engine)
    core.create('douyin', 'a', 'alpha', group_name='门店')
    expired = core.create('douyin', 'b', 'beta', group_name='品牌')
    core.set_login_state(expired.id, 'expired')
    core.create('kuaishou', 'c', '快手账号', group_name='品牌')
    core.create('channels', 'd', '视频号账号', group_name='海外')
    core.create('bilibili', 'e', '未分组账号')
    core.create('wechat', 'unsupported', '未知平台账号', group_name='不支持平台的分组')
    access = AccountAccess(db_engine)

    params = dict(query='', platform='', group='', state='', offset=0, limit=20)
    result = access.list(**(params | filters))

    assert result['groups'] == sorted(['门店', '品牌', '海外'])
    assert result['total'] == expected_total
    assert [item['nickname'] for item in result['items']] == expected_names
