import wave
import pytest
from yijianjifa.app_services.voice_access import VoiceAccess
from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.mixing.tts.base import VoiceInfo,VoiceResult


class Voice:
    def list_voices(self):return [VoiceInfo('中文 声音','中文 声音')]
    def synthesize(self,text,voice,speed,output):
        assert text=='这是试听。' and voice=='中文 声音' and speed==1.2
        with wave.open(str(output),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\x01\x00'*1600)
        return VoiceResult(output,200)


def test_audition_grants_audio_revokes_old_and_never_enqueues(db_engine,app_paths):
    CopyRepository(db_engine).create(id='V',title='测试',voice='这是试听。',body='',tags='',full='')
    media=MediaAccess(db_engine,app_paths);service=VoiceAccess(db_engine,app_paths,media,adapter=Voice())
    assert service.voices()['items'][0]['id']=='中文 声音'
    first=service.audition('V','中文 声音',1.2);token=first['url'].rsplit('/',1)[-1]
    assert media.registry.size(token)>0
    assert first['text']=='这是试听。' and first['duration_ms']==200
    assert str(app_paths.data_dir) not in str(first)
    second=service.audition('V','中文 声音',1.2)
    with pytest.raises(ValueError):media.registry.size(token)
    with db_engine.connect() as c:assert c.exec_driver_sql('select count(*) from mix_tasks').scalar_one()==0
    service.close()
    with pytest.raises(ValueError):media.registry.size(second['url'].rsplit('/',1)[-1])
    assert not list(app_paths.cache_dir.glob('audition-*'))
    media.close()


@pytest.mark.parametrize('voice,speed',[('不存在',1.2),('中文 声音',True),('中文 声音',float('nan')),('中文 声音',5)])
def test_invalid_audition_does_not_create_audio(db_engine,app_paths,voice,speed):
    CopyRepository(db_engine).create(id='V',title='测试',voice='这是试听。',body='',tags='',full='')
    media=MediaAccess(db_engine,app_paths);service=VoiceAccess(db_engine,app_paths,media,adapter=Voice())
    with pytest.raises(ValueError):service.audition('V',voice,speed)
    assert not list(app_paths.cache_dir.glob('audition-*'))
    service.close();media.close()


def test_typed_text_audition_does_not_require_library_copy(db_engine,app_paths):
    media=MediaAccess(db_engine,app_paths)
    service=VoiceAccess(db_engine,app_paths,media,adapter=Voice())
    result=service.audition_text('这是试听。','中文 声音',1.2)
    assert result['duration_ms']==200
    assert result['text']=='这是试听。'
    service.close();media.close()

@pytest.mark.parametrize('text',['', '  ', '字'*4001, None])
def test_invalid_typed_text_never_synthesizes(db_engine,app_paths,text):
    from unittest.mock import Mock
    adapter=Mock()
    media=MediaAccess(db_engine,app_paths)
    service=VoiceAccess(db_engine,app_paths,media,adapter=adapter)
    with pytest.raises(ValueError):service.audition_text(text,None,1)
    adapter.synthesize.assert_not_called()
    service.close();media.close()
