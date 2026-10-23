"""Complete PNG avatars survive same-account Douyin login and public DTO reads."""
import base64
import binascii
import json
import struct
import zlib

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.login_flow import LoginFlow
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def png_avatar(rgba):
    """A full 1x1 RGBA PNG with compressed scanline and CRC-valid chunks."""
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload +
                struct.pack('>I', binascii.crc32(kind + payload) & 0xffffffff))

    raw = (b'\x89PNG\r\n\x1a\n' +
           chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) +
           chunk(b'IDAT', zlib.compress(b'\x00' + bytes(rgba))) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')


@pytest.fixture
def profile(db_engine, app_paths):
    flow = LoginFlow(db_engine, SessionStore(app_paths.data_dir / 'profiles'))
    api = DesktopApi(db_engine, app_paths)
    yield flow, api, AccountService(db_engine)
    flow.close()
    assert api.close(3)


def public_profile(api, account_id):
    def query(operation, params):
        response = api.dispatch(json.dumps({'v': 1, 'request_id': 'read-avatar',
            'operation': operation, 'params': params}), trusted_origin=True)
        assert response['ok'], response
        return response['data']

    detail = query('account.get', {'id': account_id})
    listed = query('account.list', {'query': '', 'platform': 'douyin', 'group': '',
        'state': '', 'offset': 0, 'limit': 20})
    assert listed['total'] == 1 and listed['items'] == [detail]
    assert all(key not in detail for key in ('platform_user_id', 'session_ref', 'session_fingerprint'))
    return detail


@pytest.mark.parametrize('empty_avatar', [None, ''])
def test_douyin_relogin_keeps_missing_avatar_then_updates_same_account_in_get_and_list(profile, empty_avatar):
    flow, api, accounts = profile
    old_avatar = png_avatar((220, 50, 50, 255))
    new_avatar = png_avatar((50, 80, 220, 255))
    first = flow.begin(platform='douyin')
    saved = flow.complete(first['id'], platform_user_id='original-douyin-user', nickname='原账号',
        session_blob=b'synthetic-first-session', avatar_data=old_avatar)
    assert saved['state'] == 'completed'
    account_id = saved['account_id']
    assert public_profile(api, account_id)['avatar_data'] == old_avatar

    again = flow.begin(account_id)
    result = flow.complete(again['id'], platform_user_id='original-douyin-user', nickname='原账号',
        session_blob=b'synthetic-second-session', **({} if empty_avatar is None else {'avatar_data': empty_avatar}))
    assert result['state'] == 'completed' and result['account_id'] == account_id
    assert accounts.get(account_id).avatar_data == old_avatar
    assert public_profile(api, account_id)['avatar_data'] == old_avatar

    third = flow.begin(account_id)
    updated = flow.complete(third['id'], platform_user_id='original-douyin-user', nickname='原账号新昵称',
        session_blob=b'synthetic-third-session', avatar_data=new_avatar)
    assert updated['state'] == 'completed' and updated['account_id'] == account_id
    detail = public_profile(api, account_id)
    assert detail['avatar_data'] == new_avatar and detail['nickname'] == '原账号新昵称'
    assert len(accounts.list('douyin')) == 1


def test_different_douyin_identity_cannot_overwrite_original_avatar_or_session(profile):
    flow, api, accounts = profile
    original_avatar = png_avatar((220, 50, 50, 255))
    first = flow.begin(platform='douyin')
    saved = flow.complete(first['id'], platform_user_id='original-douyin-user', nickname='原账号',
        session_blob=b'synthetic-original-session', avatar_data=original_avatar)
    account_id = saved['account_id']
    original_ref = accounts.get(account_id).session_ref
    before = public_profile(api, account_id)
    again = flow.begin(account_id)
    rejected = flow.complete(again['id'], platform_user_id='different-douyin-user', nickname='另一账号',
        session_blob=b'synthetic-wrong-session', avatar_data=png_avatar((50, 80, 220, 255)))
    assert rejected['state'] == 'failed' and rejected['code'] == 'identity_mismatch'
    assert public_profile(api, account_id) == before
    assert accounts.get(account_id).session_ref == original_ref
    assert accounts.get(account_id).avatar_data == original_avatar and len(accounts.list('douyin')) == 1
