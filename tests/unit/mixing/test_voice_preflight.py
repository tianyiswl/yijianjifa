from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from yijianjifa.features.mixing.tts.voice_services import VoiceSourceTTS
from yijianjifa.features.mixing.tts.hosted import VoicePaused

VOICE='doubao:zh_male_wennuanahu_uranus_bigtts'


def router(allowed):
    cloud=Mock();cloud.mode='hosted';cloud.available_voices.return_value=[{'id':v} for v in allowed]
    hosted=Mock();hosted.freeze.return_value={'source':'hosted','account_id':'qa'}
    return VoiceSourceTTS(SimpleNamespace(cloud=cloud),None,hosted,Mock()),cloud,hosted


def snapshot():
    return {'options':{'voice':VOICE},'copies':[{'voice':'你好。'}]}


def test_unavailable_voice_rejected_before_task_or_reservation():
    tts,cloud,hosted=router([])
    with pytest.raises(VoicePaused,match='音色.*未开通') as error:tts.freeze(snapshot())
    assert error.value.code=='voice_unavailable'
    hosted.prepare_batch.assert_not_called()
    cloud.available_voices.assert_called_once()


def test_verified_voice_freezes_account_without_generating_or_reserving():
    tts,cloud,hosted=router([VOICE])
    assert tts.freeze(snapshot())=={'source':'hosted','account_id':'qa'}
    cloud.available_voices.assert_called_once()
    hosted.prepare_batch.assert_not_called()


def test_system_voice_does_not_require_cloud_availability():
    tts,cloud,hosted=router([])
    s=snapshot();s['options']['voice']=None
    assert tts.freeze(s)=={'source':'system'}
    cloud.available_voices.assert_not_called()
    hosted.freeze.assert_not_called()


def test_failed_availability_query_keeps_actionable_auth_error():
    tts,cloud,_=router([])
    cloud.available_voices.side_effect=VoicePaused('authentication_required','请重新登录。')
    with pytest.raises(VoicePaused) as error:tts.freeze(snapshot())
    assert error.value.code=='authentication_required'
