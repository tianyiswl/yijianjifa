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
