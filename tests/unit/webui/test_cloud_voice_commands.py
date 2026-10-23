import json
import threading
from types import SimpleNamespace

import pytest

from yijianjifa.webui.host_commands import make_host


def host_for(cloud, picker=None, config=None):
    api = SimpleNamespace(dispatch=lambda raw, **kwargs: {'ok': False, 'error': {'code': 'unsupported'}},
                          _error=lambda code, rid: {'ok': False, 'error': {'code': code}},
                          _use_term=SimpleNamespace(allowed=lambda: True))
    return make_host(api=api, engine=None, window=SimpleNamespace(create_file_dialog=picker),
                     webview=SimpleNamespace(FileDialog=SimpleNamespace(SAVE='save', OPEN='open')),
                     exit_started=threading.Event(), finished_files=None, finished_deletion=None,
                     deletion_recovery_pending=False, cover_response=None, drops=None, drop_access=None,
                     media=None, excel=None, output=None, voice_catalog=None, volc_config=config,
                     voices=None, play_sound=None, copy_text=None, request_exit=None, cloud_voice=cloud)


def request(op, params):
    return json.dumps(dict(v=1, request_id='cloud-test', operation=op, params=params))


def test_cloud_commands_only_reach_service_from_trusted_view():
    calls = []
    cloud = SimpleNamespace(read=lambda: calls.append('read') or {'available_words': 10000})
    host = host_for(cloud)
    assert not host.dispatch(request('voice.cloud.read', {}))['ok']
    assert calls == []
    result = host.dispatch(request('voice.cloud.read', {}), trusted_origin=True)
    assert result['ok'] and result['data']['available_words'] == 10000


def test_recovery_commands_are_unavailable_and_never_open_file_dialog(tmp_path):
    calls = []
    host = host_for(SimpleNamespace(), lambda *args, **kwargs: calls.append('dialog'))
    for operation in ('voice.cloud.recovery.export', 'voice.cloud.recovery.import'):
        assert not host.dispatch(request(operation, {'password': 'fixture-password'}), trusted_origin=True)['ok']
    assert calls == []


@pytest.mark.parametrize('operation,params', [
    ('voice.cloud.mode.save', {'mode': 'unsafe'}),
    ('voice.cloud.usage', {'page': True}),
    ('voice.cloud.redeem', {'code': 'test', 'words': 1000000}),
    ('voice.cloud.login', {'username': 'test', 'password': None}),
])
def test_invalid_cloud_fields_are_rejected_before_service(operation, params):
    result = host_for(SimpleNamespace()).dispatch(request(operation, params), trusted_origin=True)
    assert not result['ok']


def test_draft_estimate_is_an_upper_bound_without_creating_tasks(db_engine):
    from yijianjifa.features.library.copy_service import CopyRepository
    from yijianjifa.webui.cloud_voice_commands import preview_words
    CopyRepository(db_engine).create(id='estimate-copy', title='文案', voice='  你好，世界。\n 今天  上新！  ', body='', tags='', full='')
    result = preview_words(db_engine, dict(voice='doubao:test', quantity=2, copy_categories=[], use_copy=True), 'hosted')
    assert result == {'source': 'hosted', 'words_upper_bound': 26}
    assert preview_words(db_engine, dict(voice='', quantity=2, copy_categories=[], use_copy=True), 'hosted')['source'] == 'system'
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql('select count(*) from mix_tasks').scalar_one() == 0


@pytest.mark.parametrize('operation,params', [
    ('voice.settings.read', {}),
    ('voice.settings.save', {'app_id': '12345', 'token': 'private-key'}),
])
def test_personal_provider_commands_are_retired_without_reading_or_writing_credentials(operation, params):
    from unittest.mock import Mock
    config = Mock()
    result = host_for(SimpleNamespace(), config=config).dispatch(request(operation, params), trusted_origin=True)
    assert not result['ok'] and result['error']['code'] == 'voice_settings_retired'
    assert not config.method_calls and not config.own.method_calls


def test_personal_provider_mode_cannot_be_reselected_through_native_bridge():
    from unittest.mock import Mock
    cloud = Mock()
    result = host_for(cloud).dispatch(request('voice.cloud.mode.save', {'mode': 'own'}), trusted_origin=True)
    assert not result['ok']
    cloud.set_mode.assert_not_called()
