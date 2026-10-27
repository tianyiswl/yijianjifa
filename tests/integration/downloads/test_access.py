import importlib
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace

import pytest
from sqlalchemy import text

from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.library.media_storage import MediaStorageService
from tests.integration.downloads.fixtures import NeverCancel
from tests.integration.downloads.test_files import owner, owned_video


def access(files, engine, paths, media):
    name = 'yijianjifa.app_services.download_access'
    assert importlib.util.find_spec(name), 'DownloadAccess is not implemented'
    return importlib.import_module(name).DownloadAccess(files, engine, paths, media)


def setup_owned(engine, paths, tmp_path):
    files = owner(engine, paths); media = MediaAccess(engine, paths)
    api = access(files, engine, paths, media)
    root = tmp_path / 'outputs'; root.mkdir(); api.choose_output(root)
    resource = owned_video(files, engine, tmp_path)
    return files, media, api, resource


def test_unimported_preview_uses_shared_registry_without_revoking_library(db_engine, app_paths, tmp_path):
    files, media, api, resource = setup_owned(db_engine, app_paths, tmp_path)
    library_file = tmp_path / 'library.mp4'; library_file.write_bytes(b'library')
    asset = MediaStorageService(db_engine, app_paths, kind='video').import_media(library_file, keep_copy=False, category_ids=[])
    library_token = media.preview('video', asset.id)['url'].rsplit('/', 1)[1]
    view = api.preview(resource.id); token = view['url'].rsplit('/', 1)[1]
    assert media.registry.read(token, 0, 4) == files.path_for(resource.id).read_bytes()[:4]
    assert media.registry.read(library_token, 0, 7) == b'library'
    assert str(tmp_path) not in json.dumps(view)
    with pytest.raises(DownloadError): api.preview(str(library_file))
    api.delete_file(resource.id)
    with pytest.raises(ValueError): media.registry.read(token, 0, 1)
    assert library_file.read_bytes() == b'library'
    media.close()


def test_video_import_reuses_hash_and_blocks_physical_file_delete(db_engine, app_paths, tmp_path):
    files, media, api, resource = setup_owned(db_engine, app_paths, tmp_path)
    first = api.import_video(resource.id, (), cancel=NeverCancel())
    second = api.import_video(resource.id, (), cancel=NeverCancel())
    assert first['asset_id'] == second['asset_id'] and second['reused']
    library = MediaStorageService(db_engine, app_paths, kind='video')
    assert len(library.list_assets()) == 1
    assert library.get(first['asset_id']).effective_path == files.path_for(resource.id)
    with pytest.raises(DownloadError): api.delete_file(resource.id)
    assert files.path_for(resource.id).exists()
    with db_engine.connect() as connection:
        source = connection.execute(text("SELECT value_json FROM settings WHERE key=:key"), {'key':'download-owned:import:' + resource.id}).scalar_one()
    assert 'https://www.douyin.com/video/123456789' in source
    media.close()


def test_import_cancel_or_invalid_categories_do_not_register_media(db_engine, app_paths, tmp_path):
    _, media, api, resource = setup_owned(db_engine, app_paths, tmp_path)
    class Cancelled:
        def cancelled(self): return True
    with pytest.raises(DownloadError) as exc: api.import_video(resource.id, (), cancel=Cancelled())
    assert exc.value.code == 'cancelled'
    with pytest.raises(DownloadError): api.import_video(resource.id, ('missing',), cancel=NeverCancel())
    from yijianjifa.features.library.copy_service import CategoryRepository
    cats=CategoryRepository(db_engine)
    first,second=(cats.create('video',name) for name in ('A','B'))
    with pytest.raises(DownloadError) as exc:
        api.import_video(resource.id,(first.id,second.id),cancel=NeverCancel())
    assert exc.value.code=='single_category_only'
    assert MediaStorageService(db_engine, app_paths, kind='video').list_assets() == []
    media.close()


def test_import_prepared_transaction_does_no_hash_copy_or_probe(db_engine, app_paths, tmp_path, monkeypatch):
    _, media, api, resource = setup_owned(db_engine, app_paths, tmp_path)
    import yijianjifa.features.library.media_storage as storage
    real = storage.MediaStorageService.import_prepared
    def register(self, connection, *args, **kwargs):
        monkeypatch.setattr(storage, 'sha256_file', lambda p: (_ for _ in ()).throw(AssertionError('hash in transaction')))
        monkeypatch.setattr(storage.shutil, 'copy2', lambda *a: (_ for _ in ()).throw(AssertionError('copy in transaction')))
        return real(self, connection, *args, **kwargs)
    monkeypatch.setattr(storage.MediaStorageService, 'import_prepared', register)
    assert api.import_video(resource.id, (), cancel=NeverCancel())['asset_id']
    media.close()


def test_import_delete_share_mutation_admission(db_engine, app_paths, tmp_path, monkeypatch):
    files, media, api, resource = setup_owned(db_engine, app_paths, tmp_path)
    entered, resume, delete_started = Event(), Event(), Event()
    real = MediaStorageService.import_prepared
    def paused(self, *args, **kwargs):
        entered.set(); assert resume.wait(5)
        return real(self, *args, **kwargs)
    def remove():
        delete_started.set()
        with pytest.raises(DownloadError): api.delete_file(resource.id)
    monkeypatch.setattr(MediaStorageService, 'import_prepared', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(api.import_video, resource.id, (), cancel=NeverCancel())
        assert entered.wait(5)
        deleting = pool.submit(remove); assert delete_started.wait(5)
        assert not deleting.done()
        resume.set(); assert importing.result(5)['asset_id']; deleting.result(5)
    assert files.path_for(resource.id).exists(); media.close()


def test_preview_cleanup_preserves_library_reference(db_engine, app_paths, tmp_path):
    now = [1000.0]
    files = owner(db_engine, app_paths, clock=lambda: now[0]); media = MediaAccess(db_engine, app_paths)
    api = access(files, db_engine, app_paths, media)
    root = tmp_path / 'outputs'; root.mkdir(); api.choose_output(root)
    resource = owned_video(files, db_engine, tmp_path, kind='preview')
    api.import_video(resource.id, (), cancel=NeverCancel())
    now[0] += 25 * 3600
    assert files.cleanup_previews() == 0 and files.path_for(resource.id).exists(); media.close()


def test_owned_audio_does_not_become_a_music_asset(db_engine, app_paths, tmp_path):
    from yijianjifa.features.downloads.media import MediaProcessor
    files, media, api, source = setup_owned(db_engine,app_paths,tmp_path)
    temporary = MediaProcessor(files).run(source,'mp3',cancel=NeverCancel(),progress=lambda p: None)
    audio = files.promote(source.candidate_id,temporary,'audio_mp3')
    with pytest.raises(DownloadError): api.import_video(audio.id,(),cancel=NeverCancel())
    assert MediaStorageService(db_engine,app_paths,kind='music').list_assets() == []
    assert files.path_for(audio.id).exists(); media.close()


@pytest.mark.parametrize('format', ['txt', 'srt'])
def test_native_subtitle_export_registers_distinct_owned_physical_resource(db_engine, app_paths, tmp_path, format):
    from tests.integration.downloads.test_download_subtitles import setup_store
    store, repo, original, candidate = setup_store(db_engine, count=2, text='完整字幕')
    files = owner(db_engine, app_paths); media = MediaAccess(db_engine, app_paths); api = access(files,db_engine,app_paths,media)
    output = tmp_path / 'download'; output.mkdir(); chosen = api.choose_output(output)
    destination = tmp_path / ('大帅字幕.' + format)
    resource = api.export_subtitle(store, original, 'asr', format, destination,
        cancel=NeverCancel(), progress=lambda p: None, expected_revision=0)
    assert resource.id != original and resource.candidate_id == candidate and resource.item_id is None
    assert resource.kind == 'subtitle_' + format and files.path_for(resource.id) == destination
    assert '完整字幕1' in destination.read_text(encoding='utf-8')
    assert api.read_output()['grant_id'] == chosen['grant_id']
    api.delete_file(resource.id); assert not destination.exists(); media.close()


@pytest.mark.parametrize('name', ['CON.srt', 'LPT1.txt', 'subtitle:ads.srt', '../escape.srt', '中文' * 120 + '.srt'])
def test_native_subtitle_export_rejects_unsafe_selected_name(db_engine, app_paths, tmp_path, name):
    from tests.integration.downloads.test_download_subtitles import setup_store
    store, _, original, _ = setup_store(db_engine, count=1)
    files = owner(db_engine, app_paths); media = MediaAccess(db_engine, app_paths); api = access(files,db_engine,app_paths,media)
    with pytest.raises(DownloadError):
        api.export_subtitle(store,original,'asr','srt',tmp_path/name,cancel=NeverCancel(),progress=None,expected_revision=0)
    media.close()


def test_native_subtitle_export_collision_and_cancel_preserve_user_file(db_engine, app_paths, tmp_path):
    from tests.integration.downloads.test_download_subtitles import setup_store
    store, _, original, _ = setup_store(db_engine, count=2)
    files = owner(db_engine, app_paths); media = MediaAccess(db_engine, app_paths); api = access(files,db_engine,app_paths,media)
    destination = tmp_path / 'chosen.srt'; destination.write_bytes(b'user subtitle')
    with pytest.raises(DownloadError):
        api.export_subtitle(store,original,'asr','srt',destination,cancel=NeverCancel(),progress=None,expected_revision=0)
    assert destination.read_bytes() == b'user subtitle'
    destination.unlink()
    class Cancel:
        def cancelled(self): return True
    with pytest.raises(DownloadError):
        api.export_subtitle(store,original,'asr','srt',destination,cancel=Cancel(),progress=None,expected_revision=0)
    assert not destination.exists() and not list(tmp_path.rglob('temporary-*.srt')); media.close()


@pytest.mark.skipif(os.name=='nt', reason='POSIX fd publication test; native Windows execution remains pending')
def test_native_export_parent_replacement_never_writes_outside(db_engine, app_paths, tmp_path, monkeypatch):
    from tests.integration.downloads.test_download_subtitles import setup_store
    store, _, original, _ = setup_store(db_engine, count=2)
    files = owner(db_engine, app_paths); media = MediaAccess(db_engine, app_paths); api = access(files,db_engine,app_paths,media)
    parent = tmp_path / 'selected'; parent.mkdir(); outside = tmp_path/'outside'; outside.mkdir()
    sentinel = outside/'chosen.srt'; sentinel.write_bytes(b'user')
    write = store.write_to
    def replace(*args, **kwargs):
        parent.rename(tmp_path/'moved'); parent.symlink_to(outside,target_is_directory=True)
        return write(*args,**kwargs)
    monkeypatch.setattr(store,'write_to',replace)
    with pytest.raises(DownloadError):
        api.export_subtitle(store,original,'asr','srt',parent/'chosen.srt',cancel=NeverCancel(),progress=None,expected_revision=0)
    assert sentinel.read_bytes() == b'user' and list(outside.iterdir()) == [sentinel]; media.close()


@pytest.mark.parametrize('operation', ['preview','delete','reveal','import'])
@pytest.mark.parametrize('replacement_type', ['inode','content'])
def test_verified_owned_object_replacement_cannot_be_used(db_engine, app_paths, tmp_path, monkeypatch, operation, replacement_type):
    files, media, api, resource = setup_owned(db_engine,app_paths,tmp_path)
    path = files.path_for(resource.id); original = path.read_bytes(); held = tmp_path/'held-owned.mp4'
    real = files._hash; calls = [0]; blocked = []; at = 2 if operation=='import' else 1
    def replace_after_validation(guard, *args, **kwargs):
        result = real(guard,*args,**kwargs)
        if guard.path == path:
            calls[0] += 1
            if calls[0] == at:
                if replacement_type=='inode' and os.name=='nt':
                    # The pinned native handle excludes delete sharing, so
                    # Windows prevents this attack before any replacement.
                    with pytest.raises(PermissionError) as error: path.rename(held)
                    assert error.value.winerror == 32
                    blocked.append(True)
                    return result
                if replacement_type=='inode': path.rename(held)
                else: held.write_bytes(original)
                path.write_bytes(b'PRIVATE_REPLACEMENT')
        return result
    monkeypatch.setattr(files,'_hash',replace_after_validation)
    revealed = []
    import yijianjifa.os_integration.reveal as native_reveal
    monkeypatch.setattr(native_reveal,'reveal_path',lambda p: revealed.append(p))
    def execute():
        if operation=='import': return api.import_video(resource.id,(),cancel=NeverCancel())
        if operation=='delete': return api.delete_file(resource.id)
        return getattr(api,operation)(resource.id)
    if replacement_type=='inode' and os.name=='nt':
        try:
            result = execute()
            assert blocked == [True] and not held.exists()
            assets = MediaStorageService(db_engine,app_paths,kind='video').list_assets()
            if operation=='delete':
                assert not path.exists() and not assets and not media._read_players
            else:
                assert path.read_bytes() == original
                if operation=='preview':
                    token = result['url'].rsplit('/',1)[-1]
                    assert media.registry.read(token,0,len(original)) == original
                elif operation=='import':
                    assert len(assets)==1 and assets[0].id==result['asset_id']
                    assert assets[0].effective_path.read_bytes() == original
                else:
                    assert revealed == [path] and not assets
        finally: media.close()
        return
    with pytest.raises(DownloadError): execute()
    assert path.read_bytes() == b'PRIVATE_REPLACEMENT' and held.read_bytes() == original
    assert not revealed and not media._read_players
    assert MediaStorageService(db_engine,app_paths,kind='video').list_assets() == []
    media.close()


def test_preview_checks_the_actual_registry_fd(db_engine, app_paths, tmp_path, monkeypatch):
    files, media, api, resource = setup_owned(db_engine,app_paths,tmp_path)
    path = files.path_for(resource.id); held = tmp_path/'held.mp4'; private = tmp_path/'private.mp4'
    real = media.registry.grant
    def redirect_then_restore(selected):
        if os.name=='nt':
            with pytest.raises(PermissionError) as error: path.rename(held)
            assert error.value.winerror == 32
            # Windows pins the original pathname. Still present a different
            # real FD to verify the registry capability itself is checked.
            private.write_bytes(b'PRIVATE_REPLACEMENT')
            return real(private)
        path.rename(held); path.write_bytes(b'PRIVATE_REPLACEMENT')
        try: return real(selected)
        finally: path.rename(private); held.rename(path)
    monkeypatch.setattr(media.registry,'grant',redirect_then_restore)
    with pytest.raises(DownloadError): api.preview(resource.id)
    assert private.read_bytes() == b'PRIVATE_REPLACEMENT' and not media.registry._files
    if os.name=='nt': assert path.read_bytes() != b'PRIVATE_REPLACEMENT' and not held.exists()
    assert not media._read_players; media.close()


def test_deleted_receipt_does_not_pin_a_new_export_using_the_same_name(db_engine, app_paths, tmp_path):
    from tests.integration.downloads.test_download_subtitles import setup_store
    store, _, original, _ = setup_store(db_engine,count=2)
    files = owner(db_engine,app_paths); media = MediaAccess(db_engine,app_paths); api = access(files,db_engine,app_paths,media)
    target = tmp_path/'chosen.txt'
    one = api.export_subtitle(store,original,'asr','txt',target,cancel=NeverCancel())
    api.delete_file(one.id)
    two = api.export_subtitle(store,original,'asr','txt',target,cancel=NeverCancel())
    api.delete_file(two.id)
    assert not target.exists() and one.id != two.id
    assert files._journal(one.id)['state'] == files._journal(two.id)['state'] == 'deleted'
    assert files.repository.get_output(one.id).id == one.id and files.repository.get_output(two.id).id == two.id
    media.close()


def test_unknown_output_receipt_still_protects_physical_reference(db_engine, app_paths, tmp_path):
    files,media,api,resource = setup_owned(db_engine,app_paths,tmp_path)
    path = files.path_for(resource.id)
    another = replace(resource,id='e'*32)
    with db_engine.begin() as connection: files.repository.save_output(connection,another,backend_path=str(path))
    with pytest.raises(DownloadError): api.delete_file(resource.id)
    assert path.exists() and files._journal(resource.id)['state']=='committed'; media.close()


def test_running_delete_job_excludes_only_its_own_resource_reference(db_engine, app_paths, tmp_path):
    files, media, api, resource = setup_owned(db_engine,app_paths,tmp_path); path=files.path_for(resource.id)
    with db_engine.begin() as connection:
        job=files.repository.create_auxiliary_job(connection,kind='resource_delete',output_resource_id=resource.id)
        files.repository.record_auxiliary_job(connection,job,state='running')
    try:
        api.delete_file(resource.id,auxiliary_job_id=job)
        with db_engine.begin() as connection: files.repository.record_auxiliary_job(connection,job,state='succeeded')
        assert not path.exists() and files._journal(resource.id)['state']=='deleted'
        assert files.repository.get_auxiliary_job(job)['state']=='succeeded'
    finally: media.close()


def test_current_delete_job_does_not_ignore_another_delete_job(db_engine, app_paths, tmp_path):
    files,media,api,resource=setup_owned(db_engine,app_paths,tmp_path); path=files.path_for(resource.id)
    jobs=[]
    with db_engine.begin() as connection:
        for _ in range(2):
            job=files.repository.create_auxiliary_job(connection,kind='resource_delete',output_resource_id=resource.id)
            files.repository.record_auxiliary_job(connection,job,state='running'); jobs.append(job)
    try:
        with pytest.raises(DownloadError): api.delete_file(resource.id,auxiliary_job_id=jobs[0])
        assert path.exists() and files._journal(resource.id)['state']=='committed'
    finally: media.close()


@pytest.mark.parametrize('invalid',['kind','target','not_running','unknown'])
def test_delete_self_exemption_requires_exact_running_job(db_engine, app_paths, tmp_path, invalid):
    files,media,api,resource=setup_owned(db_engine,app_paths,tmp_path); path=files.path_for(resource.id)
    if invalid=='unknown': job='job_unknown'
    else:
        if invalid=='target':
            temporary=files.allocate(resource.candidate_id,'subtitle_txt'); temporary.write_text('another owned output')
            other=files.promote(resource.candidate_id,temporary,'subtitle_txt')
        with db_engine.begin() as connection:
            job=files.repository.create_auxiliary_job(connection,kind='resource_reveal' if invalid=='kind' else 'resource_delete',
                output_resource_id=other.id if invalid=='target' else resource.id)
            if invalid!='not_running': files.repository.record_auxiliary_job(connection,job,state='running')
    try:
        with pytest.raises(DownloadError): api.delete_file(resource.id,auxiliary_job_id=job)
        assert path.exists() and files._journal(resource.id)['state']=='committed'
    finally: media.close()
