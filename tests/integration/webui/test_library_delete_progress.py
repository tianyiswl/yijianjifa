from sqlalchemy import text
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.usage_progress import UsageProgressService
from test_library_management import call


def test_delete_record_preserves_original_and_history_and_replays(db_engine,app_paths,tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    service=MediaStorageService(db_engine,app_paths,kind='video')
    item=service.import_media(source,keep_copy=False,category_ids=[])
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'media.delete',dict(kind='video',id=item.id))
    assert result['ok'],result
    assert service.get(item.id) is None
    assert source.read_bytes()==b'original'
    assert service.history.identity_seen(item.content_hash)
    assert call(api,'media.delete',dict(kind='video',id=item.id))==result
    api.close(.1)


def test_delete_in_use_refused_and_receipt_failure_preserves_copy(db_engine,app_paths,tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    service=MediaStorageService(db_engine,app_paths,kind='video')
    item=service.import_media(source,keep_copy=True,category_ids=[])
    CopyRepository(db_engine).create(id='C1',title='title',voice='',body='',tags='',full='')
    api=DesktopApi(db_engine,app_paths)
    task=call(api,'mix.submit',dict(quantity=1),'submit')['data']['id']
    result=call(api,'media.delete',dict(kind='video',id=item.id),'inuse')
    assert result['error']['code']=='delete_blocked',result
    assert service.get(item.id) and item.managed_path.is_file()
    assert call(api,'task.cancel',dict(id=task),'cancel')['ok']
    with db_engine.begin() as c:
        c.exec_driver_sql("CREATE TRIGGER reject_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'test'); END")
    result=call(api,'media.delete',dict(kind='video',id=item.id),'rollback')
    assert not result['ok']
    assert service.get(item.id) and item.managed_path.read_bytes()==b'original'
    api.close(.1)


def test_progress_reset_is_scoped_and_retry_does_not_start_another_cycle(db_engine,app_paths):
    progress=UsageProgressService(db_engine)
    for scope in ('mixing','finished_metadata'):progress.mark_used(scope,'C1')
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'copy.progress.reset',dict(scope='mixing'))
    assert result['ok'],result
    assert not progress.is_used('mixing','C1')
    assert progress.is_used('finished_metadata','C1')
    progress.mark_used('mixing','C1')
    assert call(api,'copy.progress.reset',dict(scope='mixing'))==result
    assert progress.is_used('mixing','C1')
    assert not call(api,'copy.progress.reset',dict(scope='all'),'bad')['ok']
    api.close(.1)
