from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from yijianjifa.app_services.media_access import MediaAccess

def test_native_play_uses_only_a_live_video_grant(db_engine,app_paths,tmp_path):
 media=MediaAccess(db_engine,app_paths,native_video=True);window=Mock()
 path=tmp_path/'视频.mp4';path.write_bytes(b'fixture')
 grant=media.grant_read(path,name=path.name,kind='video',owner='library')
 token=grant['url'].rsplit('/',1)[1]
 assert media.play_native(token,window)=={'playing':True}
 window.play_media.assert_called_once_with(media,token)
 media.revoke_read('library')
 with pytest.raises(Exception):media.play_native(token,window)
 assert window.play_media.call_count==1
 media.registry.close()

@pytest.mark.parametrize('value',['/tmp/video.mp4','file:///tmp/video.mp4','https://example.test/video',None,'unknown_token_123456789'])
def test_native_play_rejects_paths_urls_and_missing_grants(db_engine,app_paths,value):
 media=MediaAccess(db_engine,app_paths,native_video=True);window=Mock()
 with pytest.raises(Exception):media.play_native(value,window)
 window.play_media.assert_not_called();media.registry.close()

def test_audio_and_non_windows_cannot_invoke_native_video(db_engine,app_paths,tmp_path):
 for enabled,extension in [(False,'.mp4'),(True,'.mp3')]:
  media=MediaAccess(db_engine,app_paths,native_video=enabled);window=Mock()
  path=tmp_path/('input'+extension);path.write_bytes(b'fixture')
  grant=media.grant_read(path,name=path.name,kind='video',owner='library')
  with pytest.raises(ValueError):media.play_native(grant['url'].rsplit('/',1)[1],window)
  window.play_media.assert_not_called();media.registry.close()

def test_native_controls_require_trusted_app_bridge(db_engine,app_paths,tmp_path):
 import json,threading
 from yijianjifa.webui.host_commands import make_host
 media=MediaAccess(db_engine,app_paths,native_video=True);window=Mock()
 path=tmp_path/'preview.mp4';path.write_bytes(b'fixture');grant=media.grant_read(path,name='preview',kind='video',owner='library')
 token=grant['url'].rsplit('/',1)[1]
 api=SimpleNamespace(dispatch=lambda *a,**k:{'ok':False},_error=lambda *a:{'ok':False})
 host=make_host(api=api,engine=db_engine,window=window,webview=None,exit_started=threading.Event(),finished_files=None,finished_deletion=None,deletion_recovery_pending=False,cover_response=None,drops=None,drop_access=None,media=media,excel=None,output=None,voice_catalog=None,volc_config=None,voices=None,play_sound=None,copy_text=None,request_exit=None)
 def request(op,p):return json.dumps(dict(v=1,request_id='preview',operation=op,params=p))
 surface={'rect':[30,40,320,180],'clip':[30,40,320,180],'visible':True}
 assert not host.dispatch(request('media.play',{'token':token,'surface':surface}))['ok']
 window.play_media.assert_not_called()
 assert not host.dispatch(request('media.play',{'token':token}),trusted_origin=True)['ok']
 assert host.dispatch(request('media.play',{'token':token,'surface':surface}),trusted_origin=True)['ok']
 window.play_media.assert_called_once_with(media,token,surface)
 assert host.dispatch(request('media.position',{'token':token,'surface':surface}),trusted_origin=True)['ok']
 window.position_media.assert_called_once_with(token,surface)
 assert host.dispatch(request('media.expand',{'token':token}),trusted_origin=True)['ok']
 window.expand_media.assert_called_once_with(token)
 invalid={**surface,'rect':[0,0,float('nan'),180]}
 assert not host.dispatch(request('media.position',{'token':token,'surface':invalid}),trusted_origin=True)['ok']
 assert not host.dispatch(request('media.play',{'token':token,'path':str(path)}),trusted_origin=True)['ok']
 media.revoke_read('library')
 assert host.dispatch(request('media.stop',{'token':token}),trusted_origin=True)['ok']
 assert not host.dispatch(request('media.position',{'token':token,'surface':surface}),trusted_origin=True)['ok']
 assert not host.dispatch(request('media.expand',{'token':token}),trusted_origin=True)['ok']
 window.stop_media.assert_called_once_with(token)
 media.registry.close()
