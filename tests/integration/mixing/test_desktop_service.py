import json
from dataclasses import replace
from pathlib import Path
import pytest
from sqlalchemy import select
from yijianjifa.features.library.copy_service import CopyRepository, CopyDraft
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderResult
from yijianjifa.storage.db import session_scope
from yijianjifa.features.finished.service import FinishedVideo


class Renderer:
    def __init__(self):
        self.requests = []
    def render(self, request):
        self.requests.append(request)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b'finished' + str(len(self.requests)).encode())
        return RenderResult(request.output_path, sum(s.duration_ms for s in request.segments), 720, 1280)


def service_for(db_engine, app_paths, tmp_path):
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    media = MediaStorageService(db_engine, app_paths, kind='video')
    path = tmp_path / 'source.mp4'
    path.write_bytes(b'fixture')
    asset = media.import_media(path, keep_copy=False, category_ids=[])
    copies = CopyRepository(db_engine)
    copies.create(id='A', title='原文', voice='', body='正文', tags='标签', full='')
    renderer = Renderer()
    service = DesktopMixService(db_engine, app_paths, renderer=renderer, probe=lambda p: 2000)
    return service, renderer, copies, asset


def test_snapshot_render_success_progress_history_and_idempotent_receipt(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=2, total_ms=3000, segment_ms=2000)
    copies.update(replace(copies.get('A'), title='后改'))
    assert not UsageProgressService(db_engine).is_used('mixing', 'A')
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count == 2
    assert len(renderer.requests) == 2
    assert all(sum(s.duration_ms for s in r.segments) == 3000 for r in renderer.requests)
    with session_scope(db_engine) as session:
        rows = list(session.scalars(select(FinishedVideo)))
        assert [r.title for r in rows] == ['原文', '原文']
    assert UsageProgressService(db_engine).is_used('mixing', 'A')
    assert not UsageProgressService(db_engine).is_used('finished_metadata', 'A')
    assert len(service.history.all_fingerprints()) == 2
    service.executor.render_item(task.id, 0, task.snapshot)
    assert len(renderer.requests) == 2


def test_failure_does_not_mark_used_and_recovery_holds_waiting_batches(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    waiting = service.submit(quantity=1)
    service.hold_after_restart()
    assert service.queue.get(task.id).state == 'paused'
    assert service.queue.get(waiting.id).state == 'paused'
    assert not renderer.requests
    service.queue.resume(task.id)
    Path(task.snapshot['sources'][0]['path']).unlink()
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).failure_count == 1
    assert not UsageProgressService(db_engine).is_used('mixing', 'A')
    assert not service.history.all_fingerprints()


def test_missing_inputs_block_before_enqueue(db_engine, app_paths):
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    service = DesktopMixService(db_engine, app_paths)
    with pytest.raises(ValueError, match='素材'):
        service.submit()
    assert service.tasks() == []


def test_voice_duration_controls_output_and_full_text_is_not_title(db_engine, app_paths, tmp_path):
    from yijianjifa.features.mixing.tts.base import VoiceResult, SentenceSpan
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    copies.update(replace(copies.get('A'), voice='一句话。', full='全程文字'))
    class TTS:
        def synthesize(self, text, voice, speed, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b'voice fixture')
            return VoiceResult(output, 4700, (SentenceSpan(text, 0, 4700),))
    service.tts = TTS()
    task = service.submit(quantity=1, total_ms=1000, full_enabled=True, spoken_enabled=True)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count == 1
    request = renderer.requests[0]
    assert sum(s.duration_ms for s in request.segments) == 4700
    assert request.voice_path is not None
    subtitles = request.subtitle_ass_path.read_text(encoding='utf-8')
    assert '一句话' in subtitles and '一句话。' not in subtitles and '全程文字' in subtitles
    assert '原文' not in subtitles


def test_queue_retains_each_failure_even_after_later_success(db_engine):
    from yijianjifa.features.mixing.queue import MixQueue
    class Execute:
        def render_item(self, task, index, snapshot):
            if index == 0:
                raise ValueError('坏素材')
    q = MixQueue(db_engine, Execute())
    task = q.enqueue({'quantity': 2})
    q.run_once_until_idle()
    assert '坏素材' in q.get(task.id).last_error
    with session_scope(db_engine) as session:
        from yijianjifa.storage.models import Setting
        rows = list(session.scalars(select(Setting).where(Setting.key.like(f'mix-result:{task.id}:%'))))
        assert len(rows) == 2
        assert any('坏素材' in r.value_json for r in rows)


def test_cancel_during_render_keeps_reference_lock_until_worker_exits(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=2)
    original_render = renderer.render
    def render(request):
        service.queue.cancel_remaining(task.id)
        with pytest.raises(RuntimeError, match='任务'):
            MediaStorageService(db_engine, app_paths, kind='video').delete_media_record(asset.id)
        return original_render(request)
    renderer.render = render
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).state == 'cancelled'
    with session_scope(db_engine) as session:
        assert list(session.scalars(select(FinishedVideo))) == []
    assert not UsageProgressService(db_engine).is_used('mixing', 'A')


def test_ffmpeg_errors_expose_stderr_instead_of_only_command(db_engine, app_paths, tmp_path):
    import subprocess
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    def render(request):
        raise subprocess.CalledProcessError(1, ['ffmpeg'], stderr=b'No such filter: subtitles')
    renderer.render = render
    task = service.submit(quantity=1)
    service.queue.run_once_until_idle()
    assert 'No such filter' in service.queue.get(task.id).last_error


def test_waiting_batches_reserve_copy_order_and_cancel_releases(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    copies.create(id='B', title='乙', voice='', body='', tags='', full='')
    a = service.submit(quantity=1)
    b = service.submit(quantity=1)
    assert a.snapshot['copies'][0]['id'] == 'A'
    assert b.snapshot['copies'][0]['id'] == 'B'
    assert not service.progress.is_used('mixing', 'A')
    service.queue.cancel_remaining(a.id)
    c = service.submit(quantity=1)
    assert c.snapshot['copies'][0]['id'] == 'A'


def test_planner_search_advances_past_prior_hundred_results():
    from yijianjifa.features.mixing.planner import CompositionPlanner, SourceClip
    from yijianjifa.features.mixing.fingerprint import composition_fingerprint
    history = set()
    planner = CompositionPlanner()
    source = SourceClip('A', 'hash', 10000, frozenset())
    for _ in range(101):
        result = planner.plan_for_sentences(quantity=1, sentence_durations_ms=[3000], required_categories=set(), sources=[source], historical_fingerprints=history)
        assert result.missing_count == 0
        history.add(composition_fingerprint(result.compositions[0]))
    assert len(history) == 101


def test_changed_source_does_not_silently_use_new_contents(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1)
    asset.effective_path.write_bytes(b'changed bytes')
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).failure_count == 1
    assert '内容已改变' in service.queue.get(task.id).last_error
    assert not renderer.requests


def test_category_change_after_submit_does_not_change_frozen_scope(db_engine, app_paths, tmp_path):
    from yijianjifa.features.library.copy_service import CategoryRepository
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    categories = CategoryRepository(db_engine)
    category = categories.create('video', '原分类')
    media = MediaStorageService(db_engine, app_paths, kind='video')
    media.append_categories(asset.id, [category.id])
    task = service.submit(quantity=1, material_categories=[category.id], total_ms=1000)
    categories.delete(category.id)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count == 1
    with pytest.raises(ValueError, match='分类已失效'):
        service.submit(material_categories=[category.id])


def test_background_music_is_frozen_protected_and_forwarded(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    music = MediaStorageService(db_engine, app_paths, kind='music')
    path = tmp_path/'music.wav'; path.write_bytes(b'music')
    track = music.import_media(path, keep_copy=False, category_ids=[])
    task = service.submit(quantity=1, total_ms=2000, segment_ms=1000,
                          background_music={'mode':'fixed','music_id':track.id}, music_gain=.3,
                          transition={'mode':'random','duration_ms':300})
    assert task.snapshot['plan']['background_music']['music_id'] == track.id
    assert task.snapshot['music_sources'][0]['content_hash'] == track.content_hash
    with pytest.raises(RuntimeError, match='引用'):
        music.delete_media_record(track.id)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count == 1
    request = renderer.requests[0]
    assert request.music_path == path
    assert request.music_gain == .3
    assert request.transition_mode == 'random'
    assert request.transition_duration_ms == 300
    assert request.transition_seed == task.snapshot['transition_seeds'][0]


def test_changed_music_fails_without_advancing_copy_progress(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    music = MediaStorageService(db_engine, app_paths, kind='music')
    path=tmp_path/'music.wav';path.write_bytes(b'original')
    track=music.import_media(path,keep_copy=False,category_ids=[])
    task=service.submit(quantity=1,background_music={'mode':'fixed','music_id':track.id})
    path.write_bytes(b'changed')
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).failure_count == 1
    assert not renderer.requests
    assert not service.progress.is_used('mixing','A')


def test_random_music_uses_frozen_category_candidates(db_engine,app_paths,tmp_path):
    from yijianjifa.features.library.copy_service import CategoryRepository
    service, renderer, copies, asset=service_for(db_engine,app_paths,tmp_path)
    music=MediaStorageService(db_engine,app_paths,kind='music')
    category=CategoryRepository(db_engine).create('audio','测试音乐分类')
    a=tmp_path/'a.wav';a.write_bytes(b'a')
    included=music.import_media(a,keep_copy=False,category_ids=[category.id])
    b=tmp_path/'b.wav';b.write_bytes(b'b')
    music.import_media(b,keep_copy=False,category_ids=[])
    task=service.submit(quantity=3,background_music={'mode':'random','category_ids':[category.id]})
    assert task.snapshot['music_choices'] == [included.id]*3
    assert len(task.snapshot['music_sources'])==1


def test_duration_range_freezes_totals_and_renders_exact_chosen_lengths(db_engine,app_paths,tmp_path):
    service,renderer,copies,asset=service_for(db_engine,app_paths,tmp_path)
    task=service.submit(quantity=2,total_range_ms=[2100,2900],segment_range_ms=[400,700])
    chosen=task.snapshot['item_total_ms']
    assert len(chosen)==2 and all(2100<=d<=2900 for d in chosen)
    assert task.snapshot['segment_range_ms']==[400,700]
    assert len(task.snapshot['duration_seeds'])==2
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count==2
    assert [sum(s.duration_ms for s in r.segments) for r in renderer.requests]==chosen
    assert all(s.duration_ms<=700 for r in renderer.requests for s in r.segments)


def test_voice_and_sentence_cut_override_duration_ranges(db_engine,app_paths,tmp_path):
    from yijianjifa.features.mixing.tts.base import VoiceResult,SentenceSpan
    service,renderer,copies,asset=service_for(db_engine,app_paths,tmp_path)
    copies.update(replace(copies.get('A'),voice='完整一句话。'))
    class TTS:
        def synthesize(self,text,voice,speed,output):
            output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'voice')
            return VoiceResult(output,4700,(SentenceSpan(text,0,4700),))
    service.tts=TTS()
    task=service.submit(quantity=1,total_range_ms=[1000,1200],segment_range_ms=[200,300],sentence_cut=True)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count==1
    assert sum(s.duration_ms for s in renderer.requests[0].segments)==4700
    assert max(s.duration_ms for s in renderer.requests[0].segments)>300


def test_new_batch_freezes_variation_and_mutes_original_by_default(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    task = service.submit(quantity=1, total_ms=1000)
    assert isinstance(task.snapshot['composition_seed'], int)
    assert task.snapshot['options']['original_gain'] == 0
    service.queue.run_once_until_idle()
    assert renderer.requests[0].original_gain == 0
    second = service.submit(quantity=1, total_ms=1000, original_gain=.6)
    service.queue.run_once_until_idle()
    assert renderer.requests[-1].original_gain == .6


def test_batch_executor_varies_ten_openings(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    media = MediaStorageService(db_engine, app_paths, kind='video')
    for i in range(13):
        path = tmp_path / f'source-{i}.mp4'
        path.write_bytes(f'source-{i}'.encode())
        media.import_media(path, keep_copy=False, category_ids=[])
    service.probe = lambda p: 20000
    task = service.submit(quantity=10, total_ms=6000, segment_ms=2000)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count == 10
    assert len({r.segments[0].path for r in renderer.requests}) == 10
    assert all(len({s.path for s in r.segments}) == 3 for r in renderer.requests)


def test_no_copy_mode_ignores_existing_copy_and_does_not_consume_it(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    copies.update(replace(copies.get('A'), voice='不能朗读', full='不能显示'))
    task=service.submit(quantity=2,total_ms=3000,use_copy=False,full_enabled=True,batch_full_text='也不显示')
    assert task.snapshot['candidate_copy_ids']==[]
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count==2
    assert all(r.voice_path is None and r.subtitle_ass_path is None for r in renderer.requests)
    assert not UsageProgressService(db_engine).is_used('mixing','A')
    with session_scope(db_engine) as session:
        assert all(r.copy_id is None and not r.title and not r.body and not r.tags for r in session.scalars(select(FinishedVideo)))


def test_no_copy_mode_works_with_empty_library(db_engine, app_paths, tmp_path):
    service, renderer, copies, asset = service_for(db_engine, app_paths, tmp_path)
    copies.delete('A')
    task=service.submit(quantity=1,total_ms=3000,use_copy=False)
    service.queue.run_once_until_idle()
    assert service.queue.get(task.id).success_count==1
    assert sum(s.duration_ms for s in renderer.requests[0].segments)==3000
