import json
from pathlib import Path
import pytest
import yijianjifa.app_services as module
from yijianjifa.features.library.media_storage import MediaStorageService


def access(engine,paths):
    cls=getattr(module,'MediaAccess',None)
    assert cls is not None,'native media access service is missing'
    return cls(engine,paths)


def test_cancel_and_duplicate_import_preserve_original(db_engine,app_paths,tmp_path):
    api=access(db_engine,app_paths)
    assert api.import_files('video',[],keep_copy=False,category_ids=[])['cancelled']
    source=tmp_path/'中文 空格.mp4';source.write_bytes(b'media fixture')
    first=api.import_files('video',[source],keep_copy=False,category_ids=[])
    second=api.import_files('video',[source],keep_copy=False,category_ids=[])
    assert first['imported']==1 and second['reused']==1
    assert len(MediaStorageService(db_engine,app_paths,kind='video').list_assets())==1
    assert source.read_bytes()==b'media fixture'
    assert str(tmp_path) not in json.dumps(first)
    api.close()


def test_preview_id_capability_revoked_and_unknown_path_rejected(db_engine,app_paths,tmp_path):
    api=access(db_engine,app_paths);source=tmp_path/'v.mp4';source.write_bytes(b'0123456789')
    record=MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    with pytest.raises(ValueError):api.preview('video',str(source))
    preview=api.preview('video',record.id)
    token=preview['url'].rsplit('/',1)[1]
    assert api.registry.read(token,2,3)==b'234'
    assert str(source) not in json.dumps(preview)
    api.close()
    with pytest.raises(ValueError):api.registry.read(token,0,1)


def test_folder_import_does_not_follow_symlink_outside(db_engine,app_paths,tmp_path):
    api=access(db_engine,app_paths);folder=tmp_path/'folder';folder.mkdir()
    outside=tmp_path/'outside.mp4';outside.write_bytes(b'outside')
    (folder/'link.mp4').symlink_to(outside)
    (folder/'inside.mp4').write_bytes(b'inside')
    result=api.import_files('video',[folder],keep_copy=False,category_ids=[],folder=True)
    assert result['imported']==1
    assert len(MediaStorageService(db_engine,app_paths,kind='video').list_assets())==1
    api.close()


def test_invalid_category_rejected_before_copy(db_engine,app_paths,tmp_path):
    api=access(db_engine,app_paths);source=tmp_path/'v.mp4';source.write_bytes(b'fixture')
    with pytest.raises(ValueError):api.import_files('video',[source],keep_copy=True,category_ids=['missing'])
    assert MediaStorageService(db_engine,app_paths,kind='video').list_assets()==[]
    api.close()


def test_thumbnail_first_frame_cached_without_revoking_player(db_engine,app_paths,tmp_path,monkeypatch):
    import base64
    from types import SimpleNamespace
    import yijianjifa.app_services.media_access as module
    calls=[]
    def decode(command,**kwargs):
        calls.append(command);return SimpleNamespace(returncode=0,stdout='17.5' if '-show_entries' in command else b'\xff\xd8thumbnail')
    monkeypatch.setattr(module.subprocess,'run',decode)
    monkeypatch.setattr('yijianjifa.os_integration.resources.ffmpeg_path',lambda:'ffmpeg')
    source=tmp_path/'首帧.mp4';source.write_bytes(b'video')
    row=MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    api=access(db_engine,app_paths)
    preview=api.preview('video',row.id);token=preview['url'].rsplit('/',1)[1]
    image=api.thumbnail('video',row.id)
    assert image==api.thumbnail('video',row.id) and len(calls)==2
    assert image['duration_ms']==17500
    assert base64.b64decode(image['image'].split(',')[1])==b'\xff\xd8thumbnail'
    assert '-ss' not in calls[0] and calls[0][calls[0].index('-frames:v')+1]=='1'
    assert api.registry.read(token,0,5)==b'video'
    source.write_bytes(b'changed video')
    api.thumbnail('video',row.id);assert len(calls)==4
    api.close()
