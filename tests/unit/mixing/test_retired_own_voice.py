"""A retired personal provider never becomes a newly billable cloud request."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yijianjifa.app_services.cloud_voice_access import CloudVoiceAccess
from yijianjifa.app_services.voice_catalog import VoiceCatalog
from yijianjifa.features.mixing.tts.hosted import VoicePaused
from yijianjifa.features.mixing.tts.voice_services import VoiceSourceConfig, VoiceSourceTTS, build_voice_services
from yijianjifa.features.mixing.tts.volcengine import VOICES


VOICE = next(iter(VOICES))


def legacy_services(tmp_path, mode='own'):
    paths = SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path / 'cache')
    cloud = CloudVoiceAccess(paths, endpoint='', opener=Mock())
    cloud.mode_path.write_text(json.dumps({'mode': mode}))
    own = Mock()
    own.status.return_value = {'configured': True, 'app_id': 'private-app'}
    config = VoiceSourceConfig(own, cloud)
    own_tts, hosted, system = Mock(), Mock(), Mock()
    router = VoiceSourceTTS(config, own_tts, hosted, system)
    return paths, config, cloud, router


def snapshot():
    return {'quantity': 3, 'options': {'voice': VOICE}, 'copies': [{'voice': '旧口播。'}],
            'planned_publish_times': [100, 200, 300]}


def test_retired_mode_is_readable_but_cannot_be_selected_again(tmp_path):
    _, _, cloud, _ = legacy_services(tmp_path)
    original = cloud.mode_path.read_bytes()
    with pytest.raises(ValueError):
        cloud.set_mode('own')
    assert cloud.mode == 'own' and cloud.mode_path.read_bytes() == original
    assert cloud.set_mode('hosted')['mode'] == 'hosted'


@pytest.mark.parametrize('damaged', ['{broken', '{}', '[]', '{"mode":"unexpected"}'])
def test_damaged_mode_file_does_not_silently_activate_cloud_billing(tmp_path, damaged):
    cloud = CloudVoiceAccess(SimpleNamespace(data_dir=tmp_path), endpoint='')
    cloud.mode_path.write_text(damaged)
    assert cloud.mode == 'own'
    assert cloud.mode_path.read_text() == damaged
    assert cloud.set_mode('hosted')['mode'] == 'hosted'


def test_legacy_catalog_keeps_choice_but_disables_own_voices_without_reading_keys(tmp_path):
    paths, config, cloud, router = legacy_services(tmp_path)
    preferences = paths.data_dir / 'voice-preferences.json'
    preferences.write_text(json.dumps({'selected': VOICE, 'favorites': [VOICE]}))
    catalog = VoiceCatalog(paths, SimpleNamespace(), config)
    result = catalog.read()
    assert result['selected'] == VOICE and result['favorites'] == [VOICE]
    assert result['mode'] == 'own' and result['configured'] is False
    assert not any(item['available'] for item in result['items'])
    assert router.list_voices() == []
    assert not config.own.method_calls and not router.own.method_calls
    with pytest.raises(ValueError, match='云配音'):
        catalog.save(VOICE, [VOICE])
    assert catalog.save('', [VOICE])['selected'] == ''
    assert cloud.mode == 'own'


@pytest.mark.parametrize('operation', ['freeze', 'synthesize'])
def test_new_own_batch_or_audition_is_rejected_without_any_provider_call(tmp_path, operation):
    _, config, _, router = legacy_services(tmp_path)
    with pytest.raises(VoicePaused) as error:
        if operation == 'freeze':
            router.freeze(snapshot())
        else:
            router.synthesize('测试。', VOICE, 1, tmp_path / 'audio.aiff')
    assert error.value.code == 'own_voice_retired'
    assert not config.own.method_calls
    assert not router.own.method_calls and not router.hosted.method_calls and not router.system.method_calls


@pytest.mark.parametrize('mode', ['own', 'hosted'])
@pytest.mark.parametrize('explicit_identity', [True, False])
def test_old_own_batch_never_becomes_hosted_after_a_mode_change(tmp_path, mode, explicit_identity):
    _, config, cloud, router = legacy_services(tmp_path, mode)
    original = snapshot()
    if explicit_identity:
        original['voice_identity'] = {'source': 'own', 'app_id': 'private-app'}
    before = json.dumps(original, sort_keys=True)
    with pytest.raises(VoicePaused) as error:
        with router.batch_context('original-task', original):
            pytest.fail('a retired batch must not start rendering')
    assert error.value.code == 'own_voice_retired'
    assert json.dumps(original, sort_keys=True) == before
    assert cloud.mode == mode and not config.own.method_calls
    assert not router.own.method_calls and not router.hosted.method_calls and not router.system.method_calls


def test_system_voice_stays_usable_with_legacy_mode_and_no_cloud(tmp_path):
    _, config, cloud, router = legacy_services(tmp_path)
    current = snapshot()
    current['options']['voice'] = ''
    assert router.freeze(current) == {'source': 'system'}
    current['voice_identity'] = {'source': 'system'}
    router.system.synthesize.return_value = SimpleNamespace(duration_ms=100)
    assert router.synthesize('测试。', '', 1, tmp_path / 'system.aiff').duration_ms == 100
    with router.batch_context('new-system-task', current) as adapter:
        assert adapter is router.system
    assert not config.own.method_calls and not router.own.method_calls and not router.hosted.method_calls
    assert cloud.mode == 'own'


def test_retired_batch_pauses_after_prior_success_without_advancing_or_recreating(db_engine, app_paths):
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    from yijianjifa.features.mixing.queue import MixTask
    from yijianjifa.storage.db import session_scope
    _, _, _, router = legacy_services(app_paths.data_dir)
    probe = Mock()
    service = DesktopMixService(db_engine, app_paths, tts=router, probe=probe)
    original = snapshot()
    original['voice_identity'] = {'source': 'own', 'app_id': 'private-app'}
    task = service.queue.enqueue(original)
    with session_scope(db_engine) as session:
        row = session.get(MixTask, task.id)
        row.current_index = row.completed_count = row.success_count = 1
    service.queue.run_once_until_idle()
    record = service.queue.get(task.id)
    assert (record.state, record.current_index, record.completed_count, record.success_count, record.failure_count) == ('paused', 1, 1, 1, 0)
    assert record.snapshot == original and record.planned_publish_times == [100, 200, 300]
    probe.assert_not_called()
    assert not router.own.method_calls and not router.hosted.method_calls
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).state == 'paused'


def test_source_factory_marks_old_preference_without_loading_own_credentials(tmp_path, monkeypatch):
    import keyring
    from yijianjifa.features.mixing.tts import voice_services
    preferences = tmp_path / 'voice-preferences.json'
    preferences.write_text(json.dumps({'selected': VOICE, 'favorites': [VOICE]}))
    old_preferences = preferences.read_bytes()
    monkeypatch.setattr(voice_services, 'system_tts_adapter', lambda: Mock())
    read_key = Mock(return_value=json.dumps({'app_id': 'private-app', 'token': 'private-key'}))
    monkeypatch.setattr(keyring, 'get_password', read_key)
    config, router, cloud = build_voice_services(SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path / 'cache'))
    assert cloud.mode == 'own' and config.status()['configured'] is False
    assert router.list_voices() == [] and preferences.read_bytes() == old_preferences
    read_key.assert_not_called()


def test_configured_new_install_uses_system_default_with_only_hosted_session_lookup(tmp_path, monkeypatch):
    import keyring
    from yijianjifa.features.mixing.tts import voice_services
    monkeypatch.setattr(voice_services, 'system_tts_adapter', lambda: Mock())
    read_key = Mock(return_value=None)
    monkeypatch.setattr(keyring, 'get_password', read_key)
    config, router, cloud = build_voice_services(SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path / 'cache'), endpoint='https://voice.example.test')
    assert cloud.mode == 'hosted' and config.status()['configured'] is False
    assert json.loads(cloud.mode_path.read_text()) == {'mode': 'hosted'}
    current = snapshot()
    current['options']['voice'] = ''
    assert router.freeze(current) == {'source': 'system'}
    read_key.assert_called_once_with(cloud.service, 'session')


def test_new_cloud_voice_selection_survives_restart_without_becoming_retired(tmp_path, monkeypatch):
    import keyring
    from yijianjifa.features.mixing.tts import voice_services
    paths = SimpleNamespace(data_dir=tmp_path, cache_dir=tmp_path / 'cache')
    monkeypatch.setattr(voice_services, 'system_tts_adapter', lambda: Mock())
    read_key = Mock()
    monkeypatch.setattr(keyring, 'get_password', read_key)
    _, _, first = build_voice_services(paths)
    assert first.mode == 'hosted'
    preferences = tmp_path / 'voice-preferences.json'
    preferences.write_text(json.dumps({'selected': VOICE, 'favorites': [VOICE]}))
    original = preferences.read_bytes()
    _, _, restarted = build_voice_services(paths)
    assert restarted.mode == 'hosted'
    assert preferences.read_bytes() == original
    read_key.assert_not_called()


def test_direct_personal_config_save_is_rejected_without_writing_keys(tmp_path):
    _, config, cloud, _ = legacy_services(tmp_path)
    before = cloud.mode_path.read_bytes()
    with pytest.raises(ValueError):
        config.save('12345', 'old-private-key')
    assert not config.own.method_calls
    assert cloud.mode_path.read_bytes() == before
