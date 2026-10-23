import base64
import io
import json
from unittest.mock import Mock
import pytest
from yijianjifa.features.mixing.tts import volcengine as v

class Config:
    def status(self):return {'configured':True}
    def _read(self):return {'app_id':'123','token':'secret'}

def stream(complete=True):
    events=[{'code':0,'data':base64.b64encode(b'audio').decode()}]
    if complete:events.append({'code':20000000})
    return io.BytesIO(b''.join(b'data: '+json.dumps(e).encode()+b'\n' for e in events))

def test_cached_audio_and_request(tmp_path,monkeypatch):
    monkeypatch.setattr(v,'_probe_duration_ms',lambda p:1000)
    opener=Mock(side_effect=lambda *a,**kw:stream())
    adapter=v.RoutedTTS(Config(),tmp_path,system=Mock(),opener=opener)
    voice=next(iter(v.VOICES))
    result=adapter.synthesize('测试',voice,1,tmp_path/'one.aiff')
    adapter.synthesize('测试',voice,1,tmp_path/'two.aiff')
    assert result.output_path.suffix=='.mp3' and result.duration_ms==1000
    assert opener.call_count==1
    request=opener.call_args.args[0]
    assert json.loads(request.data)['req_params']['speaker']==voice.split(':')[1]
    assert request.get_header('X-api-resource-id')=='seed-tts-2.0'

def test_incomplete_not_cached(tmp_path,monkeypatch):
    adapter=v.RoutedTTS(Config(),tmp_path,system=Mock(),opener=lambda *a,**k:stream(False))
    with pytest.raises(ValueError,match='连接中断'):adapter.synthesize('测试',next(iter(v.VOICES)),1,tmp_path/'out.aiff')
    assert not list(tmp_path.rglob('*.mp3'))

def test_invalid_speed_never_calls_cloud(tmp_path):
    opener=Mock();adapter=v.RoutedTTS(Config(),tmp_path,system=Mock(),opener=opener)
    with pytest.raises(ValueError,match='语速'):adapter.synthesize('测试',next(iter(v.VOICES)),2.5,tmp_path/'out.aiff')
    opener.assert_not_called()

def test_status_never_returns_secret(monkeypatch,tmp_path):
    config=v.VolcConfig(tmp_path)
    monkeypatch.setattr(config,'_read',lambda:{'app_id':'123','token':'secret'})
    assert config.status()=={'configured':True,'app_id':'123'}
