"""Owned output boundaries using real SQLite, files and FFmpeg fixtures."""
import importlib
import importlib.util
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select, text

from yijianjifa.features.downloads.contracts import Candidate, DownloadError
from yijianjifa.features.downloads.repository import DownloadRepository
from yijianjifa.storage.models import Setting
from tests.integration.downloads.fixtures import NeverCancel, digest, make_video, probe


def owner(engine, paths, **kwargs):
    name = 'yijianjifa.features.downloads.files'
    assert importlib.util.find_spec(name), 'OwnedFiles is not implemented'
    return importlib.import_module(name).OwnedFiles(DownloadRepository(engine), paths, **kwargs)


def save_candidate(engine, title='CON.mp4', identity='candidate'):
    candidate = Candidate(identity, 'douyin', '123456789', 'douyin_video:' + 'a' * 64,
                          'https://www.douyin.com/video/123456789', title=title)
    with engine.begin() as connection:
        return DownloadRepository(engine).save_candidate(connection, candidate)


def owned_video(files, engine, tmp_path, *, kind='video'):
    cid = save_candidate(engine)
    temporary = files.allocate(cid, kind)
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    return files.promote(cid, temporary, kind)


def test_owned_outputs_cannot_escape_grant(db_engine, app_paths, tmp_path):
    files = owner(db_engine, app_paths)
    root = tmp_path / 'outputs'; root.mkdir()
    grant = files.choose_output(root)
    cid = save_candidate(db_engine)
    outside = make_video(tmp_path / 'outside')
    original = outside.read_bytes()
    with pytest.raises(DownloadError):
        files.promote(cid, outside, 'video')
    assert outside.exists() and outside.read_bytes() == original
    temporary = files.allocate(cid, 'video', grant_id=grant['grant_id'])
    temporary.unlink(); temporary.symlink_to(outside)
    with pytest.raises(DownloadError):
        files.promote(cid, temporary, 'video')
    assert outside.read_bytes() == original


def test_cover_cache_grant_renews_original_identity_without_changing_output(db_engine,app_paths,tmp_path):
    now=[1000.0];files=owner(db_engine,app_paths,clock=lambda:now[0],grant_ttl=60)
    output=tmp_path/'chosen';output.mkdir();selected=files.choose_output(output)
    grant=files.cover_cache_grant()
    with files._grant(grant) as bound:assert bound.path==app_paths.cache_dir
    now[0]+=61
    restarted=owner(db_engine,app_paths,clock=lambda:now[0],grant_ttl=60)
    assert restarted.cover_cache_grant()==grant
    assert restarted._read('current','output')['grant_id']==selected['grant_id']
    with restarted._grant(grant) as bound:assert bound.path==app_paths.cache_dir


@pytest.mark.parametrize('replacement',['directory','symlink'])
def test_cover_cache_grant_does_not_adopt_replaced_directory(db_engine,app_paths,tmp_path,replacement):
    files=owner(db_engine,app_paths);grant=files.cover_cache_grant()
    app_paths.cache_dir.rename(tmp_path/'original-cache')
    outside=tmp_path/'replacement';outside.mkdir();sentinel=outside/'user.png';sentinel.write_bytes(b'untouched')
    if replacement=='symlink':app_paths.cache_dir.symlink_to(outside,target_is_directory=True)
    else:outside.rename(app_paths.cache_dir);sentinel=app_paths.cache_dir/'user.png'
    with pytest.raises(DownloadError):owner(db_engine,app_paths).cover_cache_grant()
    assert sentinel.read_bytes()==b'untouched' and files._read('current','cover')['grant_id']==grant


@pytest.mark.parametrize('format',['PNG','JPEG','WEBP'])
def test_actual_cover_image_is_owned_verified_and_only_unreferenced_ttl_cleanup(db_engine,app_paths,format,monkeypatch):
    from PIL import Image
    from yijianjifa.app_services.download_access import DownloadAccess
    from yijianjifa.app_services.media_access import MediaAccess
    monkeypatch.setattr('yijianjifa.app_services.media_access.mimetypes.guess_type',lambda name:(None,None))
    now=[1000.0];files=owner(db_engine,app_paths,clock=lambda:now[0]);cid=save_candidate(db_engine)
    temporary=files.allocate(cid,'cover',grant_id=files.cover_cache_grant())
    Image.new('RGB',(12,8),'navy').save(temporary,format=format)
    files.confirm_temporary(temporary);resource=files.promote(cid,temporary,'cover')
    media=MediaAccess(db_engine,app_paths);access=DownloadAccess(files,db_engine,app_paths,media)
    try:
        response=access.preview(resource.id);token=response['url'].rsplit('/',1)[-1]
        assert response['kind']=='cover' and media.mime_types[token]=={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}[format]
        assert media.registry.read(token,0,resource.size_bytes)==files.path_for(resource.id).read_bytes()
        with db_engine.begin() as connection:files.repository.attach_cover(connection,cid,resource.id)
        now[0]+=86401;assert files.cleanup_previews()==0
        with db_engine.begin() as connection:files.repository.attach_cover(connection,cid,None)
        assert files.cleanup_previews()==1
        with pytest.raises(ValueError):media.registry.size(token)
    finally:media.close()


@pytest.mark.parametrize('payload',[b'',b'<html>wrong</html>',b'<svg></svg>',b'GIF89a',b'\x89PNG\r\n\x1a\ncorrupt'])
def test_invalid_cover_never_becomes_owned_output(db_engine,app_paths,payload):
    files=owner(db_engine,app_paths);cid=save_candidate(db_engine)
    temporary=files.allocate(cid,'cover',grant_id=files.cover_cache_grant());temporary.write_bytes(payload)
    with pytest.raises(DownloadError):files.promote(cid,temporary,'cover')
    with db_engine.connect() as connection:assert not connection.execute(text('SELECT id FROM download_outputs')).all()


@pytest.mark.parametrize('invalid',['dimensions','bytes'])
def test_cover_rejects_actual_oversized_dimensions_or_body(db_engine,app_paths,invalid):
    from PIL import Image
    files=owner(db_engine,app_paths);cid=save_candidate(db_engine)
    temporary=files.allocate(cid,'cover',grant_id=files.cover_cache_grant())
    Image.new('RGB',(16385,1) if invalid=='dimensions' else (12,8),'navy').save(temporary,format='PNG')
    if invalid=='bytes':
        with temporary.open('ab') as stream:stream.truncate(16*1024*1024+1)
    with pytest.raises(DownloadError) as error:files.promote(cid,temporary,'cover')
    assert error.value.code=='media_invalid'
    with db_engine.connect() as connection:assert not connection.execute(text('SELECT id FROM download_outputs')).all()


@pytest.mark.parametrize('title', ['CON.mp4', 'AUX', '../escape', 'C:foo', 'LPT1.txt', '长标题' * 500], ids=['reserved-ext','reserved','traversal','drive','printer','long-chinese'])
def test_titles_do_not_control_owned_filenames(db_engine, app_paths, tmp_path, title):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir()
    files.choose_output(root); cid = save_candidate(db_engine, title)
    source = make_video(tmp_path / 'fixture')
    first = files.allocate(cid, 'video'); shutil.copyfile(source, first)
    r1 = files.promote(cid, first, 'video')
    p1 = files.path_for(r1.id); before = p1.read_bytes()
    second = files.allocate(cid, 'video'); shutil.copyfile(source, second)
    r2 = files.promote(cid, second, 'video')
    assert p1 != files.path_for(r2.id) and p1.read_bytes() == before
    assert p1.parent == root and len(p1.name.encode()) < 100


@pytest.mark.parametrize('payload', [b'', b'<html>not a movie</html>', b'\x00\x00\x00\x18ftypisom\x00' * 2])
def test_unprobed_media_never_becomes_an_output(db_engine, app_paths, tmp_path, payload):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir()
    files.choose_output(root); cid = save_candidate(db_engine)
    temporary = files.allocate(cid, 'video'); temporary.write_bytes(payload)
    with pytest.raises(DownloadError): files.promote(cid, temporary, 'video')
    with db_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar() == 0


def test_directory_replacement_and_expiry_reject_existing_resources(db_engine, app_paths, tmp_path):
    now = [1000.0]
    files = owner(db_engine, app_paths, clock=lambda: now[0], grant_ttl=60)
    root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    resource = owned_video(files, db_engine, tmp_path)
    restarted = owner(db_engine, app_paths, clock=lambda: now[0], grant_ttl=60)
    assert digest(restarted.path_for(resource.id)) == resource.sha256
    now[0] = 1061
    with pytest.raises(DownloadError): restarted.path_for(resource.id)
    restarted.choose_output(root)
    actual = root / files.path_for(resource.id).name
    root.rename(tmp_path / 'moved')
    outside = tmp_path / 'outside'; outside.mkdir()
    sentinel = outside / actual.name; sentinel.write_bytes(b'user content')
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DownloadError): restarted.path_for(resource.id)
    with pytest.raises(DownloadError): restarted.delete(resource.id)
    assert sentinel.read_bytes() == b'user content'


def test_replaced_inode_or_changed_digest_cannot_preview_or_recover(db_engine, app_paths, tmp_path):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    resource = owned_video(files, db_engine, tmp_path); path = files.path_for(resource.id)
    same_bytes = tmp_path / 'replaced.mp4'; same_bytes.write_bytes(path.read_bytes()); os.replace(same_bytes, path)
    with pytest.raises(DownloadError): files.path_for(resource.id)


def test_prepare_journal_recovers_verified_file_once(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    real = files.repository.save_output
    def fail_after_move(*args, **kwargs): raise RuntimeError('crash fixture')
    monkeypatch.setattr(files.repository, 'save_output', fail_after_move)
    with pytest.raises(RuntimeError): files.promote(cid, temporary, 'video')
    assert len(list(root.glob('*.mp4'))) == 1
    monkeypatch.setattr(files.repository, 'save_output', real)
    restarted = owner(db_engine, app_paths)
    assert restarted.recover() == 1 and restarted.recover() == 0
    with db_engine.connect() as connection:
        rid = connection.execute(text('SELECT id FROM download_outputs')).scalar_one()
    assert probe(restarted.path_for(rid))['streams'][0]['codec_type'] == 'video'


def test_tampered_prepared_file_is_not_recovered(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    monkeypatch.setattr(files.repository, 'save_output', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid, temporary, 'video')
    next(root.glob('*.mp4')).write_bytes(b'tampered')
    with pytest.raises(DownloadError): owner(db_engine, app_paths).recover()
    with db_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar() == 0


@pytest.mark.parametrize('kind,expected', [('mp3','audio_mp3'),('wav','audio_wav'),('silent','silent_video'),('asr_wav',None)])
def test_real_owned_files_and_media_processor_composition(db_engine, app_paths, tmp_path, kind, expected):
    from yijianjifa.features.downloads.media import MediaProcessor
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    source = owned_video(files, db_engine, tmp_path, kind='source_video')
    result = MediaProcessor(files).run(source, kind, cancel=NeverCancel(), progress=lambda p: None)
    assert result.stat().st_dev == files.path_for(source.id).stat().st_dev
    data = probe(result)
    if kind == 'asr_wav':
        assert data['streams'][0]['sample_rate'] == '16000' and data['streams'][0]['channels'] == 1
        files.release_temporary(result); assert not result.exists()
    else:
        promoted = files.promote(source.candidate_id, result, expected)
        assert files.path_for(promoted.id).exists() and promoted.duration_ms > 0
    assert digest(files.path_for(source.id)) == source.sha256


def test_owned_preview_cleanup_respects_recent_access_and_only_owned(db_engine, app_paths, tmp_path):
    now = [1000.0]
    files = owner(db_engine, app_paths, clock=lambda: now[0]); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    preview = owned_video(files, db_engine, tmp_path, kind='preview')
    sentinel = root / 'user.mp4'; sentinel.write_bytes(b'user')
    now[0] += 23 * 3600; files.path_for(preview.id)
    now[0] += 2 * 3600; assert files.cleanup_previews() == 0
    now[0] += 24 * 3600; assert files.cleanup_previews() == 1
    assert sentinel.read_bytes() == b'user'


@pytest.mark.parametrize('kind,text', [('subtitle_txt','字幕内容\n'), ('subtitle_srt','1\n00:00:00,000 --> 00:00:01,200\n字幕内容\n'), ('subtitle_vtt','WEBVTT\n\n00:00:00.000 --> 00:00:01.200\n字幕内容\n')])
def test_physical_subtitle_outputs_are_validated_without_media_probe(db_engine, app_paths, tmp_path, kind, text):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, kind); temporary.write_text(text, encoding='utf-8')
    resource = files.promote(cid, temporary, kind)
    assert files.path_for(resource.id).read_text(encoding='utf-8') == text and resource.duration_ms is None


@pytest.mark.parametrize('kind,text', [('subtitle_txt',b'\xff'), ('subtitle_srt',b'not an srt'), ('subtitle_vtt',b'WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nbad\n')])
def test_bad_physical_subtitle_formats_are_not_registered(db_engine, app_paths, tmp_path, kind, text):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, kind); temporary.write_bytes(text)
    with pytest.raises(DownloadError): files.promote(cid, temporary, kind)


def test_publication_collision_never_overwrites_user_file(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    forced = 'b' * 32; outside = root / ('video-' + forced + '.mp4'); outside.write_bytes(b'user file')
    monkeypatch.setattr(module, '_id', lambda: forced)
    with pytest.raises(DownloadError): files.promote(cid, temporary, 'video')
    assert outside.read_bytes() == b'user file' and temporary.exists()
    with pytest.raises(DownloadError): files.recover()
    assert outside.read_bytes() == b'user file'


def test_prepare_recovery_handles_crash_between_link_and_unlink(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    real = _Guard.unlink
    monkeypatch.setattr(_Guard, 'unlink', lambda self: (_ for _ in ()).throw(RuntimeError('crash before unlink')))
    with pytest.raises(RuntimeError): files.promote(cid, temporary, 'video')
    final = next(root.glob('*.mp4')); assert final.samefile(temporary)
    monkeypatch.setattr(_Guard, 'unlink', real)
    restarted = owner(db_engine, app_paths)
    assert restarted.recover() == 1 and restarted.recover() == 0 and not temporary.exists()
    with db_engine.connect() as connection: rid = connection.execute(text('SELECT id FROM download_outputs')).scalar_one()
    assert digest(restarted.path_for(rid)) == digest(final)


def test_unowned_third_hardlink_stops_prepare_recovery(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path / 'fixture'), temporary)
    real = _Guard.unlink
    monkeypatch.setattr(_Guard, 'unlink', lambda self: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid, temporary, 'video')
    final = next(root.glob('*.mp4')); outside = tmp_path / 'user-link.mp4'; os.link(final, outside)
    before = outside.read_bytes(); monkeypatch.setattr(_Guard, 'unlink', real)
    with pytest.raises(DownloadError): files.recover()
    assert outside.read_bytes() == before and temporary.exists()


def test_malformed_grant_rejected_without_exposing_backend_path(db_engine, app_paths, tmp_path):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir()
    grant = files.choose_output(root)
    with db_engine.begin() as connection:
        connection.execute(text('UPDATE settings SET value_json=:v WHERE key=:k'),
            {'v':'{"v":1,"path":42}', 'k':'download-owned:grant:' + grant['grant_id']})
    with pytest.raises(DownloadError): files.choose_output(root)


def test_temporary_area_reparse_flag_is_rejected(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine)
    original = Path.lstat
    class ReparseStat:
        st_file_attributes = 0x400
        def __init__(self, result): self.result = result
        def __getattr__(self, name): return getattr(self.result, name)
    def flagged(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return ReparseStat(result) if path.name.startswith('.yijianjifa-download-') else result
    monkeypatch.setattr(Path, 'lstat', flagged)
    with pytest.raises(DownloadError): files.allocate(cid, 'video')


def test_cross_volume_temporary_area_is_rejected_at_identity_boundary(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); original = _Guard.__enter__
    def cross_volume(guard):
        result = original(guard)
        if guard.path.name.startswith('.yijianjifa-download-'): guard.identity = (guard.identity[0] + 1, guard.identity[1])
        return result
    monkeypatch.setattr(_Guard, '__enter__', cross_volume)
    with pytest.raises(DownloadError): files.allocate(cid, 'video')


def test_file_changed_during_publication_never_receives_success_row(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine, app_paths); root = tmp_path / 'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid, 'video')
    shutil.copyfile(make_video(tmp_path/'fixture'),temporary)
    real = os.link
    def tamper(*args, **kwargs):
        real(*args, **kwargs); temporary.write_bytes(b'changed during publication')
    monkeypatch.setattr(os,'link',tamper)
    with pytest.raises(DownloadError): files.promote(cid,temporary,'video')
    with db_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar_one() == 0
    with pytest.raises(DownloadError): files.recover()


def test_temporary_metadata_has_finite_types(db_engine, app_paths, tmp_path):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('字幕', encoding='utf-8')
    tid, allocation = files._temporary(temporary); allocation['grant_id'] = 42
    with db_engine.begin() as connection: files._save(connection,'temporary',tid,allocation)
    with pytest.raises(DownloadError): files.promote(cid,temporary,'subtitle_txt')
    assert temporary.read_text(encoding='utf-8') == '字幕'


def test_recovery_rejects_invalid_resource_metadata_as_download_error(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('字幕', encoding='utf-8')
    monkeypatch.setattr(files.repository,'save_output',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid,temporary,'subtitle_txt')
    rid, journal = files._values('journal')[0]; journal['resource']['kind'] = 'unsafe_unknown'
    with db_engine.begin() as connection: files._save(connection,'journal',rid,journal)
    with pytest.raises(DownloadError): files.recover()


def test_recovery_rechecks_digest_after_probe(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('字幕', encoding='utf-8')
    monkeypatch.setattr(files.repository,'save_output',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid,temporary,'subtitle_txt')
    restarted = owner(db_engine,app_paths); real = restarted._validate
    def changed(path, *args):
        result = real(path,*args); path.write_text('替换字幕', encoding='utf-8'); return result
    monkeypatch.setattr(restarted,'_validate',changed)
    with pytest.raises(DownloadError): restarted.recover()
    with db_engine.connect() as connection: assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar_one() == 0


@pytest.mark.parametrize('operation', ['release','promote'])
def test_temporary_regular_directory_replacement_retains_unowned_files(db_engine, app_paths, tmp_path, operation):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    area = temporary.parent; saved = root/'saved-area'; area.rename(saved); area.mkdir()
    temporary.write_text('unowned sentinel', encoding='utf-8')
    with pytest.raises(DownloadError):
        if operation=='release': files.release_temporary(temporary)
        else: files.promote(cid,temporary,'subtitle_txt')
    assert temporary.read_text(encoding='utf-8') == 'unowned sentinel' and (saved/temporary.name).read_text(encoding='utf-8') == 'owned'


def test_release_does_not_adopt_a_same_name_temporary_replacement(db_engine, app_paths, tmp_path):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    saved = temporary.with_name('saved-owned.txt'); temporary.rename(saved); temporary.write_text('unowned sentinel', encoding='utf-8')
    with pytest.raises(DownloadError): files.release_temporary(temporary)
    assert temporary.read_text(encoding='utf-8') == 'unowned sentinel' and saved.read_text(encoding='utf-8') == 'owned'


@pytest.mark.parametrize('failure', ['missing_tool','cancel'])
def test_actual_processor_failure_allocations_are_reconciled_on_restart(db_engine, app_paths, tmp_path, failure):
    from yijianjifa.features.downloads.media import MediaProcessor
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    source = owned_video(files,db_engine,tmp_path)
    live = files.allocate(source.candidate_id,'subtitle_txt'); live.write_text('live owned temporary', encoding='utf-8')
    class CancelAfterAllocation:
        def cancelled(self): return len(files._values('temporary')) > 1
    for _ in range(3):
        if failure=='cancel': token = CancelAfterAllocation(); processor = MediaProcessor(files)
        else: token = NeverCancel(); processor = MediaProcessor(files,ffmpeg=tmp_path/'missing-ffmpeg')
        with pytest.raises(DownloadError) as exc: processor.run(source,'mp3',cancel=token,progress=lambda p: None)
        assert exc.value.code == ('cancelled' if failure=='cancel' else 'media_tool_missing')
        files.recover()
        assert len(files._values('temporary')) == 1 and live.read_text(encoding='utf-8') == 'live owned temporary'
    restarted = owner(db_engine,app_paths); restarted.recover()
    assert len(restarted._values('temporary')) == 1 and restarted.path_for(source.id).exists()


def test_missing_allocations_do_not_consume_new_allocation_admission(db_engine, app_paths, tmp_path):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); abandoned = files.allocate(cid,'subtitle_txt'); abandoned.unlink()
    another = files.allocate(cid,'subtitle_txt')
    assert another.exists() and len(files._values('temporary')) == 1


def test_trusted_writer_replacement_requires_explicit_identity_handoff(db_engine, app_paths, tmp_path):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt')
    written = temporary.with_name('trusted-writer.txt'); written.write_text('trusted completed text', encoding='utf-8')
    os.replace(written,temporary)
    with pytest.raises(DownloadError): files.promote(cid,temporary,'subtitle_txt')
    files.confirm_temporary(temporary,cancel=NeverCancel())
    resource = files.promote(cid,temporary,'subtitle_txt')
    assert files.path_for(resource.id).read_text(encoding='utf-8') == 'trusted completed text'


@pytest.mark.parametrize('replacement', ['directory','symlink','hardlink','outside'])
def test_writer_handoff_cannot_expand_namespace_or_directory_grant(db_engine, app_paths, tmp_path, replacement):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    outside = tmp_path/'outside.txt'; outside.write_text('user content', encoding='utf-8')
    if replacement=='directory':
        area = temporary.parent; area.rename(root/'saved'); area.mkdir(); temporary.write_text('user sentinel', encoding='utf-8')
    elif replacement=='symlink': temporary.unlink(); temporary.symlink_to(outside)
    elif replacement=='hardlink': temporary.unlink(); os.link(outside,temporary)
    else: temporary = outside
    with pytest.raises(DownloadError): files.confirm_temporary(temporary)
    assert outside.read_text(encoding='utf-8') == 'user content'
    if replacement=='directory': assert temporary.read_text(encoding='utf-8') == 'user sentinel'


def test_cancelled_writer_handoff_preserves_allocation_and_file(db_engine, app_paths, tmp_path):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    before = files._values('temporary')
    class Cancel:
        def cancelled(self): return True
    with pytest.raises(DownloadError): files.confirm_temporary(temporary,cancel=Cancel())
    assert files._values('temporary') == before and temporary.read_text(encoding='utf-8') == 'owned'


def test_area_replacement_stops_recovery_and_future_allocations(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    real = _Guard.unlink
    monkeypatch.setattr(_Guard,'unlink',lambda self: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid,temporary,'subtitle_txt')
    monkeypatch.setattr(_Guard,'unlink',real)
    area = temporary.parent; area.rename(root/'saved-area'); area.mkdir(); temporary.write_text('user sentinel', encoding='utf-8')
    with pytest.raises(DownloadError): owner(db_engine,app_paths).recover()
    with pytest.raises(DownloadError): files.allocate(cid,'subtitle_txt')
    assert temporary.read_text(encoding='utf-8') == 'user sentinel' and next(root.glob('subtitle_txt-*.txt')).read_text(encoding='utf-8') == 'owned'


@pytest.mark.parametrize('operation', ['output','temporary','cleanup'])
@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_final_delete_entry_race_preserves_replacement_and_receipt(db_engine, app_paths, tmp_path, monkeypatch, operation):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned content', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt') if operation=='output' else None
    path = files.path_for(resource.id) if resource else temporary
    held = path.with_name('held-owned.txt'); real = _Guard.verify; real_stat = os.stat
    active = [False]; injected = [False]
    def replace():
        path.rename(held); path.write_text('user replacement', encoding='utf-8'); injected[0] = True
    def swap(guard):
        result = real(guard)
        if active[0] and not injected[0] and guard.path == path:
            replace()
        return result
    def swap_stat(name,*args,**kwargs):
        result = real_stat(name,*args,**kwargs)
        if active[0] and not injected[0] and name==path.name and kwargs.get('dir_fd') is not None: replace()
        return result
    def inside(method):
        def run(guard,*args,**kwargs):
            active[0] = True
            try: return method(guard,*args,**kwargs)
            finally: active[0] = False
        return run
    monkeypatch.setattr(_Guard,'verify',swap)
    monkeypatch.setattr(os,'stat',swap_stat)
    monkeypatch.setattr(_Guard,'unlink',inside(_Guard.unlink))
    monkeypatch.setattr(_Guard,'cleanup_owned',inside(_Guard.cleanup_owned))
    with pytest.raises(DownloadError):
        if operation=='output': files.delete(resource.id)
        elif operation=='temporary': files.release_temporary(path)
        else:
            with files.open_temporary(path) as bound: bound.guard.cleanup_owned()
    assert injected[0] and path.read_text(encoding='utf-8') == 'user replacement' and held.read_text(encoding='utf-8') == 'owned content'
    if resource: assert files._journal(resource.id)['state']=='committed'


def test_recover_can_cancel_after_probe_without_registering_output(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    monkeypatch.setattr(files.repository,'save_output',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): files.promote(cid,temporary,'subtitle_txt')
    restarted = owner(db_engine,app_paths); cancelled = [False]; real = restarted._validate
    class Cancel:
        def cancelled(self): return cancelled[0]
    token = Cancel()
    def after_probe(path,kind,cancel):
        assert cancel is token
        result = real(path,kind,cancel); cancelled[0] = True; return result
    monkeypatch.setattr(restarted,'_validate',after_probe)
    with pytest.raises(DownloadError) as exc: restarted.recover(cancel=token)
    assert exc.value.code=='cancelled'
    with db_engine.connect() as connection: assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar_one()==0


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_native_exclusive_rename_never_overwrites_an_existing_entry(tmp_path):
    from yijianjifa.features.downloads.files import _rename_exclusive
    source = tmp_path/'source'; source.write_text('owned', encoding='utf-8')
    destination = tmp_path/'destination'; destination.write_text('user', encoding='utf-8')
    fd = os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError): _rename_exclusive(fd,source.name,fd,destination.name)
        assert source.read_text(encoding='utf-8')=='owned' and destination.read_text(encoding='utf-8')=='user'
        destination.unlink(); _rename_exclusive(fd,source.name,fd,destination.name)
        assert not source.exists() and destination.read_text(encoding='utf-8')=='owned'
    finally: os.close(fd)


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_capture_race_restores_replacement_without_deleting_it(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id); held = root/'held-owned.txt'
    original = module._rename_exclusive; injected = [False]
    def swap(source_fd,source_name,destination_fd,destination_name):
        if source_name==path.name and not injected[0]:
            path.rename(held); path.write_text('user replacement', encoding='utf-8'); injected[0] = True
        return original(source_fd,source_name,destination_fd,destination_name)
    monkeypatch.setattr(module,'_rename_exclusive',swap)
    with pytest.raises(DownloadError): files.delete(resource.id)
    assert path.read_text(encoding='utf-8')=='user replacement' and held.read_text(encoding='utf-8')=='owned'
    assert files._journal(resource.id)['state']=='committed' and not files._values('deletion')


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_conflicting_restore_retains_data_and_recovery_record(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id); held = root/'held-owned.txt'
    original = module._rename_exclusive; injected = [False]
    def conflict(source_fd,source_name,destination_fd,destination_name):
        if source_name==path.name and not injected[0]:
            path.rename(held); path.write_text('user captured', encoding='utf-8'); injected[0]=True
            original(source_fd,source_name,destination_fd,destination_name)
            path.write_text('user occupying original', encoding='utf-8')
            return
        return original(source_fd,source_name,destination_fd,destination_name)
    monkeypatch.setattr(module,'_rename_exclusive',conflict)
    with pytest.raises(DownloadError): files.delete(resource.id)
    [(identity,raw)] = files._values('deletion'); record = files._deletion(identity)
    captured = path.parent/record['quarantine_name']/'object'
    assert record['state']=='retained' and captured.read_text(encoding='utf-8')=='user captured'
    assert path.read_text(encoding='utf-8')=='user occupying original' and held.read_text(encoding='utf-8')=='owned'
    assert files._journal(resource.id)['state']=='committed'
    occupying = root/'user-preserved.txt'; path.rename(occupying)
    monkeypatch.setattr(module,'_rename_exclusive',original)
    assert owner(db_engine,app_paths).recover_deletions()==1
    assert path.read_text(encoding='utf-8')=='user captured' and occupying.read_text(encoding='utf-8')=='user occupying original'
    assert held.read_text(encoding='utf-8')=='owned' and not files._values('deletion')


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_capture_crash_can_restore_owned_file_on_restart(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id)
    original = module._rename_exclusive
    def crash(*args):
        original(*args)
        if args[1]==path.name: raise SystemExit('isolated crash after capture')
    monkeypatch.setattr(module,'_rename_exclusive',crash)
    with pytest.raises(SystemExit): files.delete(resource.id)
    assert not path.exists() and files._values('deletion') and files._journal(resource.id)['state']=='committed'
    monkeypatch.setattr(module,'_rename_exclusive',original)
    restarted = owner(db_engine,app_paths)
    assert restarted.recover_deletions()==1 and restarted.path_for(resource.id).read_text(encoding='utf-8')=='owned'
    assert not files._values('deletion')


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_recover_cancel_during_temporary_cleanup_keeps_owned_links(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads.files import _Guard
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    real_unlink = _Guard.unlink
    monkeypatch.setattr(_Guard,'unlink',lambda self: (_ for _ in ()).throw(RuntimeError('crash before unlink')))
    with pytest.raises(RuntimeError): files.promote(cid,temporary,'subtitle_txt')
    monkeypatch.setattr(_Guard,'unlink',real_unlink)
    restarted = owner(db_engine,app_paths); real_digest = restarted._digest_pinned; cancelled = [False]
    class Cancel:
        def cancelled(self): return cancelled[0]
    def cancel_digest(*args,**kwargs):
        cancelled[0] = True
        return real_digest(*args,**kwargs)
    monkeypatch.setattr(restarted,'_digest_pinned',cancel_digest)
    with pytest.raises(DownloadError) as exc: restarted.recover(cancel=Cancel())
    assert exc.value.code=='cancelled' and temporary.read_text(encoding='utf-8')=='owned'
    assert next(root.glob('subtitle_txt-*.txt')).read_text(encoding='utf-8')=='owned' and not restarted._values('deletion')
    with db_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM download_outputs')).scalar_one()==0


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_delete_rechecks_new_reference_after_capture_and_hashes_outside_write(db_engine, app_paths, tmp_path, monkeypatch):
    from sqlalchemy import event
    from yijianjifa.features.downloads import files as module
    from yijianjifa.storage.models import MediaAsset
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id)
    real_capture = module._rename_exclusive; real_digest = files._digest_pinned
    writing = set(); checked = [0]
    def track(connection,cursor,statement,*args):
        if statement.lstrip().upper().startswith(('BEGIN IMMEDIATE','INSERT','UPDATE','DELETE')): writing.add(connection)
    def completed(connection): writing.discard(connection)
    def check_digest(*args,**kwargs):
        assert not writing, 'content hash inside SQLite write transaction'
        checked[0] += 1
        return real_digest(*args,**kwargs)
    def insert_reference(*args):
        real_capture(*args)
        if args[1]==path.name:
            with db_engine.begin() as connection:
                connection.execute(MediaAsset.__table__.insert().values(id='late-library-reference',kind='video',
                    display_name='existing library ref',original_path=str(path),content_hash=resource.sha256))
    event.listen(db_engine,'before_cursor_execute',track)
    event.listen(db_engine,'commit',completed); event.listen(db_engine,'rollback',completed)
    monkeypatch.setattr(files,'_digest_pinned',check_digest)
    monkeypatch.setattr(module,'_rename_exclusive',insert_reference)
    try:
        with pytest.raises(DownloadError): files.delete(resource.id)
    finally:
        event.remove(db_engine,'before_cursor_execute',track)
        event.remove(db_engine,'commit',completed); event.remove(db_engine,'rollback',completed)
    assert checked[0]==2 and path.read_text(encoding='utf-8')=='owned' and files._journal(resource.id)['state']=='committed'
    assert not files._values('deletion')


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_unsupported_native_capture_rejects_without_path_unlink(db_engine, app_paths, tmp_path, monkeypatch):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id)
    monkeypatch.setattr(module.ctypes,'CDLL',lambda *a,**k: object())
    with pytest.raises(DownloadError): files.delete(resource.id)
    assert path.read_text(encoding='utf-8')=='owned' and files._journal(resource.id)['state']=='committed'
    assert not files._values('deletion') and not list(root.glob('.p011-delete-*'))


@pytest.mark.parametrize('change',['directory','missing_object','permissions'])
@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_deletion_recovery_rejects_changed_private_boundary(db_engine, app_paths, tmp_path, monkeypatch, change):
    from yijianjifa.features.downloads import files as module
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id)
    real = module._rename_exclusive
    def crash(*args):
        real(*args)
        if args[1]==path.name: raise SystemExit('isolated crash after capture')
    monkeypatch.setattr(module,'_rename_exclusive',crash)
    with pytest.raises(SystemExit): files.delete(resource.id)
    monkeypatch.setattr(module,'_rename_exclusive',real)
    did,record = files._values('deletion')[0]; area = root/record['quarantine_name']; held = root/'held-private'
    if change=='directory':
        area.rename(held); area.mkdir(mode=0o700); (area/'object').write_text('user replacement', encoding='utf-8')
    elif change=='missing_object': (area/'object').rename(root/'held-owned.txt')
    else: area.chmod(0o755)
    restarted = owner(db_engine,app_paths)
    if change=='missing_object':
        assert restarted.recover_deletions()==0 and restarted._deletion(did)['state']=='missing'
        assert (root/'held-owned.txt').read_text(encoding='utf-8')=='owned' and not path.exists()
    else:
        with pytest.raises(DownloadError): restarted.recover_deletions()
        assert (area/'object').read_text(encoding='utf-8')==('user replacement' if change=='directory' else 'owned')
        if change=='directory': assert (held/'object').read_text(encoding='utf-8')=='owned'
    assert files._journal(resource.id)['state']=='committed' and restarted._values('deletion')


@pytest.mark.skipif(os.name=='nt', reason='POSIX quarantine; Windows native execution pending')
def test_delete_rejects_changed_pinned_content_before_capture(db_engine, app_paths, tmp_path, monkeypatch):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    cid = save_candidate(db_engine); temporary = files.allocate(cid,'subtitle_txt'); temporary.write_text('owned', encoding='utf-8')
    resource = files.promote(cid,temporary,'subtitle_txt'); path = files.path_for(resource.id)
    real = files._digest_pinned; changed = [False]
    def change_before_hash(*args,**kwargs):
        if not changed[0]: path.write_text('new content after final verification', encoding='utf-8'); changed[0]=True
        return real(*args,**kwargs)
    monkeypatch.setattr(files,'_digest_pinned',change_before_hash)
    with pytest.raises(DownloadError): files.delete(resource.id)
    assert changed[0] and path.read_text(encoding='utf-8')=='new content after final verification'
    assert files._journal(resource.id)['state']=='committed' and not files._values('deletion')


@pytest.mark.parametrize('state',['completed','completed_with_errors'])
def test_task_center_completed_states_allow_owned_output_delete(db_engine, app_paths, tmp_path, state):
    from yijianjifa.features.downloads.contracts import DownloadOptions,Region,StageResult
    from yijianjifa.features.tasks.service import TaskCenterService
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); grant=files.choose_output(root)
    cid = save_candidate(db_engine)
    with db_engine.begin() as connection:
        task = files.repository.create_batch(connection,(cid,),DownloadOptions(),grant['grant_id'],{cid:Region()})
    item = files.repository.summary(task)['items'][0]['id']
    temporary = files.allocate(cid,'video',item_id=item); shutil.copyfile(make_video(tmp_path/'fixture'),temporary)
    resource = files.promote(cid,temporary,'video',item_id=item); path = files.path_for(resource.id)
    with db_engine.begin() as connection: files.repository.record_stage(connection,item,StageResult('video','succeeded',resource.id,None))
    TaskCenterService(db_engine).set_state(task,state)
    files.delete(resource.id)
    assert not path.exists() and files._journal(resource.id)['state']=='deleted'
    assert files.repository.get_output(resource.id)==resource


@pytest.mark.parametrize('kind',['resource_preview','resource_reveal','resource_import','resource_delete'])
def test_active_auxiliary_input_protects_owned_output(db_engine, app_paths, tmp_path, kind):
    files = owner(db_engine,app_paths); root = tmp_path/'outputs'; root.mkdir(); files.choose_output(root)
    resource = owned_video(files,db_engine,tmp_path); path = files.path_for(resource.id)
    with db_engine.begin() as connection:
        job = files.repository.create_auxiliary_job(connection,kind=kind,output_resource_id=resource.id)
        files.repository.record_auxiliary_job(connection,job,state='running')
    with pytest.raises(DownloadError): files.delete(resource.id)
    assert path.exists() and files._journal(resource.id)['state']=='committed'
