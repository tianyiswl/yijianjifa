"""Local desktop bridge: immutable inputs, real render, atomic success receipt.

This module has no publishing imports or platform side effects.
"""
from dataclasses import asdict, replace
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import random
import uuid

from sqlalchemy import select, func
from yijianjifa.core.settings import GlobalSettings
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import HistoricalIdentity, Setting, UsageProgress
from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.features.finished.service import FinishedVideo
from .ffmpeg_renderer import FFmpegRenderer, RenderRequest, RenderSegment, _probe, _profile_dimensions
from .fingerprint import composition_fingerprint
from .history import CompositionHistoryRepository
from .models import MixPlan
from .output_names import new_output_stamp, output_filename
from .desktop_audio import synthesize_sentences, write_subtitles
from .planner import CompositionPlanner, SourceClip
from .queue import MixQueue, MixTask, ItemCancelled, ItemInterrupted
from .diagnostics import MixStepError, error_diagnostic
from .tts.hosted import VoicePaused


@contextmanager
def _mix_step(stage, materials=()):
    try:
        yield
    except (ItemCancelled, ItemInterrupted, VoicePaused):
        raise
    except Exception as exc:
        raise MixStepError(error_diagnostic(exc, stage, materials)) from exc


def probe_duration(path):
    return round(float(_probe(Path(path))['format']['duration']) * 1000)


class EmptyCopySelection(ValueError):
    pass


class DesktopMixService:
    def __init__(self, engine, paths, *, renderer=None, probe=None, tts=None):
        self.engine, self.paths = engine, paths
        self.probe = probe or probe_duration
        self.tts = tts
        self.history = CompositionHistoryRepository(engine)
        self.progress = UsageProgressService(engine)
        self.settings = GlobalSettings(engine, default_output_directory=paths.data_dir / '成品')
        self.executor = DesktopMixExecutor(self, renderer or FFmpegRenderer())
        self.queue = MixQueue(engine, self.executor, self.settings)

    def tasks(self):
        with session_scope(self.engine) as session:
            return [MixQueue._to_record(r) for r in session.scalars(select(MixTask).order_by(MixTask.position))]

    def hold_after_restart(self):
        self.queue.recover_interrupted()
        for task in self.tasks():
            if task.state == 'waiting':
                self.queue.request_pause(task.id)
            elif task.state == 'cancelled':
                self.executor.discard_cancelled(task.id)
        return [t.id for t in self.tasks() if t.state in {'paused', 'interrupted', 'waiting_publish'}]

    def submit(self, *, quantity=10, material_categories=(), copy_categories=(),
               total_ms=15000, segment_ms=3000, resolution='720p', aspect_ratio='9:16',
               frame_fit='preserve_blur', copy_mode='sequential', use_copy=True, background_music=None, transition=None,
               total_range_ms=None, segment_range_ms=None, publish_after=None, daily_publish=None, **options):
        if type(use_copy) is not bool:
            raise ValueError('文案选项无效')
        if not use_copy:
            copy_categories=()
            options.update(spoken_enabled=False, full_enabled=False, batch_full_text='', sentence_cut=False, voice=None)
        from .publish_after import freeze
        publish_plan=freeze(self.engine,quantity,publish_after) if publish_after is not None else None
        from .duration_ranges import validate_range
        validate_range(total_range_ms)
        validate_range(segment_range_ms)
        from .subtitle_styles import validate_style
        for key in ('spoken_style', 'full_style'):
            if key in options:
                validate_style(options[key])
        from .effect_options import music_spec, transition_spec, validate_music_gain
        music = music_spec(background_music)
        transition = transition_spec(transition)
        validate_music_gain(options.get('music_gain', .25))
        plan = MixPlan(background_music=music, transition=transition, quantity=quantity, resolution=resolution, aspect_ratio=aspect_ratio,
                       frame_fit=frame_fit, use_copy=use_copy, material_scope='categories' if material_categories else 'all',
                       material_category_ids=tuple(material_categories),
                       copy_scope='categories' if copy_categories else 'all', copy_category_ids=tuple(copy_categories))
        if total_ms < 1 or segment_ms < 1:
            raise ValueError('时长必须大于零')
        if copy_mode not in {'sequential', 'random'}:
            raise ValueError('无效文案顺序')
        categories = CategoryRepository(self.engine)
        for kind, selected in (('video', material_categories), ('copy', copy_categories)):
            if set(selected) - {r.id for r in categories.list(kind)}:
                raise ValueError('分类已失效，请重新选择；不会改用全部资料')
        media = MediaStorageService(self.engine, self.paths, kind='video')
        records = media.list_assets(category_ids=material_categories)
        if not records:
            raise ValueError('请先导入视频素材，或调整素材分类')
        sources = []
        for r in records:
            if not r.effective_path.is_file():
                raise ValueError(f'文件未找到：{r.display_name}')
            sources.append({'asset_id': r.id, 'content_hash': r.content_hash, 'path': str(r.effective_path), 'display_name': r.display_name,
                            'category_ids': sorted(media.category_ids(r.id))})
        music_sources, music_choices = [], []
        if music['mode'] != 'none':
            storage = MediaStorageService(self.engine, self.paths, kind='music')
            # Web UI stores music categories as "music"; retain legacy "audio"
            # category identities without rewriting records or broadening the scope.
            music_categories = {r.id for kind in ('music', 'audio') for r in categories.list(kind)}
            if set(music['category_ids']) - music_categories:
                raise ValueError('音乐分类已失效，请重新选择')
            candidates = storage.list_assets(category_ids=music['category_ids'])
            if music['mode'] == 'fixed':
                candidates = [r for r in candidates if r.id == music['music_id']]
            if not candidates:
                raise ValueError('没有可用的背景音乐，请导入或重新选择')
            if any(not r.effective_path.is_file() for r in candidates):
                raise ValueError('背景音乐文件未找到，请处理后重新提交')
            # Resolve each item's random choice now, so retries and later imports cannot change it.
            chosen = [random.choice(candidates) for _ in range(quantity)]
            music_choices = [r.id for r in chosen]
            music_sources = [{'asset_id': r.id, 'content_hash': r.content_hash, 'display_name': r.display_name,
                              'path': str(r.effective_path)} for r in {r.id: r for r in chosen}.values()]
        copies=[]
        selected=[{'id':None,'title':'','voice':'','body':'','tags':'','full':''} for _ in range(quantity)]
        if use_copy:
            repo = CopyRepository(self.engine)
            copies = [r for r in repo.list_all() if not copy_categories or repo.category_ids(r.id) & set(copy_categories)]
            if not copies:
                raise EmptyCopySelection('当前没有可用文案，请选择不使用文案，或添加文案后重试。')
            reserved = set()
            for task in self.tasks():
                if task.state in {'waiting', 'running', 'paused', 'interrupted', 'waiting_publish'}:
                    reserved.update(c['id'] for c in task.snapshot.get('copies', [])[task.current_index:])
            unused = [r for r in copies if not self.progress.is_used('mixing', r.id) and r.id not in reserved]
            # Reserve selection in this batch without advancing successful-use progress.
            pool = list(unused or copies)
            selected = []
            for _ in range(quantity):
                if not pool:
                    pool = list(copies)
                choice = random.choice(pool) if copy_mode == 'random' else pool[0]
                pool.remove(choice)
                selected.append(asdict(choice))
        options.setdefault('spoken_style', {'size':75, 'y_percent':77})
        options.setdefault('full_style', {'size':100, 'y_percent':12})
        options.setdefault('original_gain', 0.0)
        snapshot = {'output_name_stamp': new_output_stamp(), 'composition_seed': random.getrandbits(64), 'quantity': quantity, 'plan': plan.model_dump(mode='json'), 'sources': sources,
                    'copies': selected, 'candidate_copy_ids': [r.id for r in copies],
                    'total_ms': total_ms, 'segment_ms': segment_ms, 'options': options,
                    'auto_publish': False, 'schema': 'desktop-local-v1'}
        if publish_plan is not None:
            snapshot['publish_after']=publish_plan
            snapshot['planned_publish_times']=[slot['time'] for slot in publish_plan['slots']]
        if daily_publish is not None:
            snapshot['daily_publish'] = json.loads(json.dumps(daily_publish))
        if total_range_ms is not None:
            snapshot.update(total_range_ms=list(total_range_ms),
                            item_total_ms=[random.randint(*total_range_ms) for _ in range(quantity)])
        if segment_range_ms is not None:
            snapshot.update(segment_range_ms=list(segment_range_ms),
                            duration_seeds=[random.getrandbits(32) for _ in range(quantity)])
        if music_sources:
            snapshot.update(music_sources=music_sources, music_choices=music_choices)
        if transition['mode'] == 'random':
            snapshot['transition_seeds'] = [random.getrandbits(32) for _ in range(quantity)]
        if self.tts is not None and hasattr(self.tts, 'freeze'):
            snapshot['voice_identity'] = self.tts.freeze(snapshot)
        return self.queue.confirm_batch(snapshot)

    def item_results(self, task_id):
        with session_scope(self.engine) as session:
            rows = session.scalars(select(Setting).where(Setting.key.like(f'mix-result:{task_id}:%')))
            return sorted((json.loads(r.value_json) for r in rows), key=lambda r: r['index'])


class DesktopMixExecutor:
    def __init__(self, service, renderer):
        self.service, self.renderer = service, renderer

    def render_item(self, task_id, item_index, snapshot):
        # Voice reservation precedes probing or rendering the first video and
        # includes every frozen copy. Completed video receipts remain decisive.
        with session_scope(self.service.engine) as session:
            if session.get(Setting, f'mix-success:{task_id}:{item_index}') is not None:
                return self._render_item(task_id, item_index, snapshot)
            authorization = session.get(Setting, 'mix-voice-retry:' + task_id)
            retry = json.loads(authorization.value_json).get('authorization') if authorization else None
        tts = self.service.tts
        if tts is not None and hasattr(tts, 'batch_context'):
            voice_snapshot = {**snapshot, 'voice_retry_authorization': retry} if retry else snapshot
            with tts.batch_context(task_id, voice_snapshot) as adapter:
                return self._render_item(task_id, item_index, snapshot, tts=adapter)
        return self._render_item(task_id, item_index, snapshot)

    def release_voice_reservation(self, task_id, snapshot):
        adapter = self.service.tts
        if adapter is not None and hasattr(adapter, 'release_batch'):
            try:
                adapter.release_batch(task_id, snapshot)
            except VoicePaused:
                # The server lease is the fallback for unavailable connectivity.
                # Never turn a pause/cancel into a failed making item.
                pass

    def _render_item(self, task_id, item_index, snapshot, *, tts=None):
        service = self.service
        receipt_key = f'mix-success:{task_id}:{item_index}'
        with session_scope(service.engine) as session:
            receipt = session.get(Setting, receipt_key)
            if receipt is not None:
                return json.loads(receipt.value_json)['finished_id']
            pending_row = session.get(Setting, f'mix-pending:{task_id}:{item_index}')
            pending = json.loads(pending_row.value_json) if pending_row is not None else None
        if snapshot.get('schema') != 'desktop-local-v1':
            raise ValueError('这条旧任务尚不能由桌面制作器执行，请取消后重新创建')
        if pending is not None:
            self._validate_pending(task_id, item_index, snapshot, pending)
            recovered = self._finish_pending(task_id, item_index, snapshot, pending)
            if recovered is not None:
                return recovered
        copy = snapshot['copies'][item_index]
        plan = snapshot['plan']
        options = snapshot.get('options', {})
        folder = Path(snapshot['output_directory']) / task_id
        output = folder / output_filename(snapshot, item_index)
        sources = []
        paths = {}
        for r in snapshot['sources']:
            path = Path(r['path'])
            material = [(path, r.get('display_name', path.name))]
            with _mix_step('检查素材文件', material):
                if not path.is_file():
                    raise FileNotFoundError(f'文件未找到：{path.name}')
                if sha256_file(path) != r['content_hash']:
                    raise ValueError(f'素材内容已改变：{path.name}，请重新导入后创建任务')
            with _mix_step('读取素材视频信息', material):
                duration_ms = service.probe(path)
            sources.append(SourceClip(r['asset_id'], r['content_hash'], duration_ms, frozenset(r['category_ids'])))
            paths[r['asset_id']] = path
        music_path = None
        if plan.get('background_music', {}).get('mode', 'none') != 'none':
            chosen = snapshot['music_choices'][item_index]
            track = next(r for r in snapshot['music_sources'] if r['asset_id'] == chosen)
            music_path = Path(track['path'])
            with _mix_step('检查背景音乐', [(music_path, track.get('display_name', music_path.name))]):
                if not music_path.is_file():
                    raise ValueError('背景音乐文件未找到')
                if sha256_file(music_path) != track['content_hash']:
                    raise ValueError('背景音乐内容已改变，请重新导入后创建任务')
        duration = snapshot['item_total_ms'][item_index] if 'item_total_ms' in snapshot else snapshot['total_ms']
        voice = None
        if copy['voice'].strip():
            with _mix_step('生成配音'):
                voice = synthesize_sentences(copy['voice'], options.get('voice'), options.get('speed', 1.0),
                                             folder / f'配音-{item_index + 1:03d}', tts if tts is not None else service.tts)
            duration = voice.duration_ms
        if voice and options.get('sentence_cut', False):
            durations = [s.end_ms - s.start_ms for s in voice.sentence_spans]
        else:
            from .duration_ranges import segment_durations
            seed = snapshot['duration_seeds'][item_index] if 'duration_seeds' in snapshot else 0
            durations = segment_durations(duration, snapshot['segment_ms'], snapshot.get('segment_range_ms'), seed=seed)
        with _mix_step('生成字幕'):
            subtitles = write_subtitles(folder / f'{item_index + 1:03d}.ass', copy, options, duration, voice.sentence_spans if voice else (),
                                        aspect_ratio=plan["aspect_ratio"], resolution=plan["resolution"])
        planned = CompositionPlanner().plan_for_sentences(
            quantity=1, sentence_durations_ms=durations, required_categories=set(plan['material_category_ids']),
            sources=sources, historical_fingerprints=service.history.all_fingerprints(),
            variation_seed=snapshot.get('composition_seed'), variant_index=item_index)
        if not planned.compositions:
            raise ValueError('素材组合不足：无法继续生成满足分类与防重复要求的成品，请补充素材')
        composition = planned.compositions[0]
        request = RenderRequest(
            segments=tuple(RenderSegment(paths[s.asset_id], s.source_start_ms, s.duration_ms) for s in composition.segments),
            output_path=output, resolution=plan['resolution'], aspect_ratio=plan['aspect_ratio'], frame_fit=plan['frame_fit'],
            keep_original_audio=options.get('keep_original_audio', True),
            voice_path=voice.output_path if voice else None, subtitle_ass_path=subtitles,
            original_gain=options.get('original_gain', 1.0), voice_gain=options.get('voice_gain', 1.0),
            music_path=music_path, music_gain=options.get('music_gain', .25),
            transition_mode=plan.get('transition', {}).get('mode', 'none'),
            transition_name=plan.get('transition', {}).get('name') or 'fade',
            transition_duration_ms=plan.get('transition', {}).get('duration_ms', 500),
            transition_seed=snapshot.get('transition_seeds', [0] * snapshot['quantity'])[item_index])
        if output.exists() or output.is_symlink():
            raise ItemInterrupted('发现没有制作凭据的同名成品；已保留文件，请处理后继续')
        attempt = output.with_name(f'.p011-complete-{uuid.uuid4().hex}.mp4')
        pending = dict(schema=1, task_id=task_id, index=item_index,
                       snapshot_hash=self._snapshot_hash(snapshot), output=str(output), attempt=str(attempt),
                       copy=dict(copy), composition=[dict(asset_id=s.asset_id, content_hash=s.asset_content_hash,
                       start_ms=s.source_start_ms, duration_ms=s.duration_ms) for s in composition.segments],
                       fingerprint=composition_fingerprint(composition), expected_duration_ms=duration,
                       phase='prepared')
        try:
            self._save_pending(task_id, item_index, pending)
        except Exception as exc:
            raise ItemInterrupted('制作准备记录未能保存；请检查存储后继续') from exc
        try:
            labels = {Path(r['path']): r.get('display_name', Path(r['path']).name) for r in snapshot['sources']}
            involved = list(dict.fromkeys(segment.path for segment in request.segments))
            render_materials = [(path, labels.get(path, path.name)) for path in involved]
            if music_path:
                render_materials.append((music_path, track.get('display_name', music_path.name)))
            with _mix_step('合成视频', render_materials):
                rendered = self.renderer.render(replace(request, output_path=attempt))
        except Exception:
            self._discard_pending(task_id, item_index, pending)
            raise
        try:
            pending.update(phase='ready', content_hash=sha256_file(attempt), duration_ms=rendered.duration_ms)
            self._save_pending(task_id, item_index, pending)
            return self._finish_pending(task_id, item_index, snapshot, pending)
        except ItemCancelled:
            self._discard_pending(task_id, item_index, pending)
            raise
        except Exception as exc:
            # A complete file must survive a failed or uncertain database commit.
            raise ItemInterrupted('成品已生成，登记尚未完成；文件已保留，请检查存储后继续') from exc

    @staticmethod
    def _snapshot_hash(snapshot):
        value = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(value.encode()).hexdigest()

    def _validate_pending(self, task_id, index, snapshot, pending):
        output = Path(snapshot['output_directory']) / task_id / output_filename(snapshot, index)
        attempt = Path(pending.get('attempt', ''))
        name = attempt.name
        if (pending.get('schema') != 1 or pending.get('task_id') != task_id
                or pending.get('index') != index or pending.get('snapshot_hash') != self._snapshot_hash(snapshot)
                or pending.get('output') != str(output) or pending.get('copy') != snapshot['copies'][index]
                or attempt.parent != output.parent or not name.startswith('.p011-complete-')
                or not name.endswith('.mp4') or len(name.removeprefix('.p011-complete-').removesuffix('.mp4')) != 32
                or pending.get('phase') not in {'prepared', 'ready'}):
            raise ItemInterrupted('制作恢复记录与任务不匹配；已保留文件，请检查后继续')
        if attempt.is_symlink() or output.is_symlink():
            raise ItemInterrupted('制作恢复文件路径已改变；已保留文件，请检查后继续')
        try:
            sources = {s['asset_id']: s['content_hash'] for s in snapshot['sources']}
            segments = pending['composition']
            nonce = name.removeprefix('.p011-complete-').removesuffix('.mp4')
            payload = [dict(content_hash=s['content_hash'], source_start_ms=s['start_ms'],
                            duration_ms=s['duration_ms']) for s in segments]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
            valid = (bool(segments) and hashlib.sha256(encoded.encode()).hexdigest() == pending['fingerprint']
                     and all(c in '0123456789abcdef' for c in nonce)
                     and sum(s['duration_ms'] for s in segments) == pending['expected_duration_ms']
                     and all(sources.get(s['asset_id']) == s['content_hash'] and s['start_ms'] >= 0
                             and s['duration_ms'] > 0 for s in segments))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ItemInterrupted('制作恢复组合与任务不匹配；已保留文件，请检查后继续')

    def discard_cancelled(self, task_id):
        with session_scope(self.service.engine) as session:
            task = session.get(MixTask, task_id)
            if task is None or task.state != 'cancelled':
                return
            snapshot = json.loads(task.snapshot_json)
            rows = session.scalars(select(Setting).where(Setting.key.like(f'mix-pending:{task_id}:%')))
            pending_items = [json.loads(row.value_json) for row in rows]
        self.release_voice_reservation(task_id, snapshot)
        for pending in pending_items:
            try:
                self._validate_pending(task_id, pending['index'], snapshot, pending)
                self._discard_pending(task_id, pending['index'], pending)
            except (ItemInterrupted, KeyError, TypeError, ValueError):
                # Preserve files with broken ownership evidence without preventing
                # startup or undoing an already saved cancellation.
                with session_scope(self.service.engine) as session:
                    task = session.get(MixTask, task_id)
                    task.last_error = '制作恢复记录无法核验；相关文件已保留，请检查后清理'
            except OSError:
                # File permissions or an unavailable output volume must not make
                # an already cancelled task prevent the rest of the app starting.
                # Keep the intent for a later cleanup; database errors still escape.
                with session_scope(self.service.engine) as session:
                    task = session.get(MixTask, task_id)
                    task.last_error = '取消后的制作文件暂时无法清理；记录已保留，请检查文件权限或存储位置'

    def _save_pending(self, task_id, index, pending):
        with session_scope(self.service.engine) as session:
            key = f'mix-pending:{task_id}:{index}'
            row = session.get(Setting, key)
            value = json.dumps(pending, ensure_ascii=False)
            if row is None:
                session.add(Setting(key=key, value_json=value))
            else:
                row.value_json = value

    def _discard_pending(self, task_id, index, pending):
        attempt, output = Path(pending['attempt']), Path(pending['output'])
        with session_scope(self.service.engine) as session:
            if session.get(Setting, f'mix-success:{task_id}:{index}') is not None:
                return
            # Only this intent's unique attempt and verified formal bytes are owned.
            attempt.unlink(missing_ok=True)
            if (pending.get('phase') == 'ready' and output.is_file() and not output.is_symlink()
                    and sha256_file(output) == pending.get('content_hash')):
                output.unlink()
            row = session.get(Setting, f'mix-pending:{task_id}:{index}')
            if row is not None:
                session.delete(row)

    def _finish_pending(self, task_id, index, snapshot, pending):
        attempt, output = Path(pending['attempt']), Path(pending['output'])
        try:
            with session_scope(self.service.engine) as session:
                task = session.get(MixTask, task_id)
                if task is not None and task.state in {'cancelling', 'cancelled'}:
                    raise ItemCancelled()
            if pending['phase'] == 'prepared':
                if not attempt.is_file():
                    if output.exists():
                        raise ItemInterrupted('同名文件没有完成凭据；已保留文件，请检查后继续')
                    self._discard_pending(task_id, index, pending)
                    return None  # no render was completed; rebuild from frozen inputs
                info = _probe(attempt)
                video = next(s for s in info['streams'] if s.get('codec_type') == 'video')
                width, height = _profile_dimensions(snapshot['plan']['aspect_ratio'], snapshot['plan']['resolution'])
                duration = round(float(info['format']['duration']) * 1000)
                if (video.get('width'), video.get('height')) != (width, height) or abs(duration - pending['expected_duration_ms']) > 120:
                    raise ItemInterrupted('待登记成品未通过时长或尺寸核验；已保留文件，请检查后继续')
                pending.update(phase='ready', content_hash=sha256_file(attempt), duration_ms=duration)
                self._save_pending(task_id, index, pending)
            candidate = attempt if attempt.is_file() else output
            if not candidate.is_file() or candidate.is_symlink() or sha256_file(candidate) != pending['content_hash']:
                raise ItemInterrupted('待登记成品缺失或内容已改变；已保留文件，请检查后继续')
            if candidate == attempt:
                if output.exists() and sha256_file(output) != pending['content_hash']:
                    raise ItemInterrupted('同名成品内容不符；已保留两个文件，请检查后继续')
                os.replace(attempt, output)
            return self._commit_success(task_id, index, snapshot, pending)
        except ItemCancelled:
            self._discard_pending(task_id, index, pending)
            raise
        except ItemInterrupted:
            raise
        except Exception as exc:
            raise ItemInterrupted('成品登记尚未完成；文件已保留，请检查存储后继续') from exc

    def _commit_success(self, task_id, index, snapshot, pending):
        """Keep success, history, usage, and the replay receipt in one transaction."""
        with session_scope(self.service.engine) as session:
            key = f'mix-success:{task_id}:{index}'
            receipt = session.get(Setting, key)
            if receipt is not None:
                return json.loads(receipt.value_json)['finished_id']
            task = session.get(MixTask, task_id)
            if task is not None and task.state in {'cancelling', 'cancelled'}:
                raise ItemCancelled()
            output, copy = Path(pending['output']), snapshot['copies'][index]
            row = FinishedVideo(origin_type='generated', display_name=output.name, stored_path=str(output),
                                content_hash=pending['content_hash'], duration_ms=pending['duration_ms'],
                                title=copy['title'], body=copy['body'], tags=copy['tags'], copy_id=copy['id'])
            session.add(row)
            session.flush()
            session.add(HistoricalIdentity(identity_kind='mix:composition', content_hash=pending['fingerprint']))
            if copy['id'] is not None:
                self._mark_used(session, copy['id'], snapshot['candidate_copy_ids'])
            session.add(Setting(key=key, value_json=json.dumps({'finished_id': row.id, 'path': str(output)})))
            intent = session.get(Setting, f'mix-pending:{task_id}:{index}')
            if intent is not None:
                session.delete(intent)
            return row.id

    def _mark_used(self, session, copy_id, candidates):
        progress = self.service.progress
        cycle = progress._cycle(session, 'mixing')
        used = set(session.scalars(select(UsageProgress.copy_id).where(UsageProgress.scope == 'mixing', UsageProgress.cycle == cycle)))
        if set(candidates) <= used:
            cycle += 1
            progress._set_cycle(session, 'mixing', cycle)
        row = session.scalar(select(UsageProgress).where(UsageProgress.scope == 'mixing', UsageProgress.copy_id == copy_id))
        order = (session.scalar(select(func.max(UsageProgress.used_order)).where(UsageProgress.scope == 'mixing', UsageProgress.cycle == cycle)) or 0) + 1
        if row is None:
            session.add(UsageProgress(scope='mixing', copy_id=copy_id, cycle=cycle, used_order=order))
        elif row.cycle != cycle:
            row.cycle, row.used_order = cycle, order
