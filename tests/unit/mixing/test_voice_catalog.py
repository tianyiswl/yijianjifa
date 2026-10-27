from types import SimpleNamespace
from unittest.mock import Mock
import shutil
import pytest
from yijianjifa.app_services.voice_catalog import VoiceCatalog
from yijianjifa.features.mixing.tts.volcengine import VOICES, MANIFEST

def test_selection_persists_and_sample_is_local(tmp_path, synthetic_mp3):
    media=SimpleNamespace(registry=Mock(),mime_types={});media.registry.grant.return_value='local-token'
    config=Mock();config.status.return_value={'configured':True}
    service=VoiceCatalog(SimpleNamespace(data_dir=tmp_path),media,config)
    voice='doubao:zh_female_xiaohe_uranus_bigtts'
    # The catalog path contract uses an isolated tone, not redistributed voice audio.
    samples = tmp_path/'synthetic-samples'
    sample = samples/next(item['sample_file'] for item in MANIFEST if item['id']==voice)
    sample.parent.mkdir(parents=True)
    shutil.copyfile(synthetic_mp3, sample)
    service.samples = samples
    service.save(voice,[voice])
    restored=VoiceCatalog(SimpleNamespace(data_dir=tmp_path),media,config)
    restored.samples = samples
    assert restored.read()['selected']==voice
    assert restored.read()['favorites']==[voice]
    assert restored.standard(voice)['url']=='p011media://asset/local-token'
    restored.standard(voice)
    assert media.registry.grant.call_count==1
    assert media.registry.grant.call_args.args[0].exists()
    assert 'token' not in service.file.read_text()


CLOUD_VOICE = 'doubao:zh_female_xiaohe_uranus_bigtts'


def hosted_catalog(tmp_path, balance):
    from yijianjifa.features.mixing.tts.voice_services import VoiceSourceConfig
    cloud = SimpleNamespace(
        mode='hosted', endpoint='https://voice.invalid', local_test=False,
        identity=lambda: {'account_id': 'test-account'},
        available_voices=lambda: [{'id': CLOUD_VOICE}],
        read=lambda: balance,
    )
    return VoiceCatalog(SimpleNamespace(data_dir=tmp_path), SimpleNamespace(), VoiceSourceConfig(None, cloud)), cloud


def test_default_cloud_voice_requires_current_positive_credit_before_writing(tmp_path):
    import pytest
    for index, balance in enumerate([
        {'enabled': True, 'authenticated': True, 'balance_current': True, 'available_words': 0},
        {'enabled': True, 'authenticated': True, 'balance_current': False, 'available_words': 100},
        {'enabled': True, 'authenticated': False, 'balance_current': False, 'available_words': 100},
        {'enabled': True, 'authenticated': True, 'balance_current': True, 'available_words': '100'},
    ]):
        service, _ = hosted_catalog(tmp_path / str(index), balance)
        service.save('', [])
        previous = service.file.read_bytes()
        with pytest.raises(ValueError, match='登录|充值|更新|网络'):
            service.save(CLOUD_VOICE, [CLOUD_VOICE])
        assert service.file.read_bytes() == previous


def test_default_cloud_voice_with_credit_is_restored_on_new_catalog(tmp_path):
    service, _ = hosted_catalog(tmp_path, {'enabled': True, 'authenticated': True,
                                           'balance_current': True, 'available_words': 1})
    service.save(CLOUD_VOICE, [])
    restored = VoiceCatalog(SimpleNamespace(data_dir=tmp_path), SimpleNamespace(), service.config)
    assert restored.read()['selected'] == CLOUD_VOICE


def test_favoriting_preserves_cloud_default_after_credit_is_exhausted(tmp_path):
    balance = {'enabled': True, 'authenticated': True, 'balance_current': True, 'available_words': 20}
    service, cloud = hosted_catalog(tmp_path, balance)
    service.save(CLOUD_VOICE, [])
    cloud.read = lambda: (_ for _ in ()).throw(AssertionError('favorite must not query quota'))
    result = service.save(CLOUD_VOICE, [CLOUD_VOICE])
    assert result['selected'] == CLOUD_VOICE and result['favorites'] == [CLOUD_VOICE]
    assert service.save('', [CLOUD_VOICE])['selected'] == ''


def test_default_cloud_voice_connection_error_keeps_preferences(tmp_path):
    import pytest
    from yijianjifa.features.mixing.tts.hosted import VoicePaused
    service, cloud = hosted_catalog(tmp_path, {})
    service.save('', [])
    previous = service.file.read_bytes()
    def offline():
        raise VoicePaused('service_unavailable', '云配音连接未完成。')
    cloud.read = offline
    with pytest.raises(ValueError, match='网络|连接'):
        service.save(CLOUD_VOICE, [])
    assert service.file.read_bytes() == previous


def test_missing_optional_supplier_sample_is_reported_without_synthesis(tmp_path):
    media=SimpleNamespace(registry=Mock(),mime_types={})
    config=Mock();config.status.return_value={'configured':True}
    service=VoiceCatalog(SimpleNamespace(data_dir=tmp_path),media,config)
    service.samples=tmp_path/'empty-samples'
    voice='doubao:zh_female_xiaohe_uranus_bigtts'
    assert next(item for item in service.read()['items'] if item['id']==voice)['standard_sample'] is False
    with pytest.raises(ValueError, match='示例文件不可用'):
        service.standard(voice)
    media.registry.grant.assert_not_called()
