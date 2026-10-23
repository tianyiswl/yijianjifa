"""Download persistence without network work or privately owned transactions.

Every mutation uses the caller's Connection. SQLite's existing shared engine
keeps its FK policy; reference checks below run in the same write transaction.
The migration additionally declares real FKs for connections enforcing them.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import get_args
import uuid

from sqlalchemy import Engine, Connection, func, insert, inspect, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import NoSuchTableError

from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.models import (
    DownloadCandidateRow, DownloadResolutionRow, DownloadAuxiliaryJobRow,
    DownloadItemRow, DownloadOutputRow, DownloadStageRow, DownloadSourceRow,
    DownloadSubtitleOriginalRow, DownloadSubtitleDraftRow, DownloadSubtitleRevisionRow,
    DownloadDecisionRow,
    Category, MediaAsset,
)
from .contracts import AuxiliaryKind, Candidate, DownloadError, DownloadOptions, ErrorCode, OutputResource, Region, SourceInput, StageResult, SubtitleSegment
from .input import validate_url

STATES = ('waiting', 'running', 'succeeded', 'partial', 'failed', 'skipped', 'cancelled', 'interrupted')
STAGE_STATES = tuple(state for state in STATES if state != 'partial')
TERMINAL = ('succeeded', 'skipped', 'cancelled')
KINDS = ('source_video', 'video', 'audio_mp3', 'audio_wav', 'silent_video', 'asr', 'ocr', 'cover', 'preview', 'subtitle_txt', 'subtitle_srt', 'subtitle_vtt')
COMPLETIONS = ('partial', 'complete', 'limited')
AUXILIARY_INPUTS = ('session_id', 'candidate_id', 'model_id', 'output_resource_id', 'subtitle_original_id',
                    'subtitle_draft_id', 'subtitle_revision', 'subtitle_origin', 'export_format',
                    'input_grant_id', 'output_grant_id', 'category_ids_json')


def _now():
    return datetime.now(timezone.utc)


def _id(prefix=''):
    return prefix + uuid.uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _identity(value, size=64):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_:-]{1,' + str(size) + '}', value):
        raise DownloadError('input_limit')
    return value


def _model_identity(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
        raise DownloadError('input_limit')
    return value


def _display(value):
    if type(value) is not str:
        raise DownloadError('metadata_missing')
    value = re.sub(r'https?://\S+', '[链接]', value)
    value = re.sub(r'(?i)\b(?:cookie|authorization|token|access_token|refresh_token|mstoken|password|session|session_ref|sessionid|session_id|signature|x-bogus)\s*[:=]\s*[^\r\n]*', '[已脱敏]', value)
    value = re.sub(r'(?i)\bbearer\s+\S+', '[已脱敏]', value)
    return ''.join(char for char in value if ord(char) >= 32 and ord(char) != 127)[:500]


def _source_key(value):
    if type(value) is not str or not re.fullmatch(r'(direct|douyin_video|douyin_profile|local):[a-f0-9]{64}', value):
        raise DownloadError('unsafe_source')
    return value


def _source(source: SourceInput):
    if not isinstance(source, SourceInput) or source.kind not in ('direct', 'douyin_video', 'douyin_profile', 'local'):
        raise DownloadError('unsupported_source')
    key = _source_key(source.source_key)
    if not key.startswith(source.kind + ':'):
        raise DownloadError('unsafe_source')
    if source.kind == 'local':
        if source.canonical_source is not None:raise DownloadError('unsafe_source')
        return dict(source_kind='local',source_key=key,canonical_source=None)
    if source.kind == 'direct':
        # Signed URL and private input are retained only in provider memory.
        return dict(source_kind=source.kind, source_key=key, canonical_source=None)
    if type(source.canonical_source) is not str:
        raise DownloadError('unsafe_source')
    parts = validate_url(source.canonical_source)
    if (parts.scheme != 'https' or parts.query or parts.fragment
            or parts.hostname not in ('www.douyin.com', 'douyin.com', 'v.douyin.com')
            or source.kind == 'douyin_profile' and not re.fullmatch(r'/user/[A-Za-z0-9_-]{1,200}/?', parts.path)
            or source.kind == 'douyin_video' and not (re.fullmatch(r'/video/\d{1,32}/?', parts.path)
                or parts.hostname == 'v.douyin.com' and re.fullmatch(r'/[A-Za-z0-9_-]{1,200}/?', parts.path))):
        raise DownloadError('unsafe_source')
    return dict(source_kind=source.kind, source_key=key, canonical_source=source.canonical_source)


def _error_code(value):
    if value is not None and value not in get_args(ErrorCode):
        raise DownloadError('metadata_missing')


def _aggregate(states):
    if not states or all(state == 'waiting' for state in states):
        return 'waiting'
    if all(state == 'succeeded' for state in states):
        return 'succeeded'
    if any(state == 'running' for state in states):
        return 'running'
    if 'succeeded' in states or 'partial' in states:
        return 'partial'
    for state in ('interrupted', 'failed', 'cancelled', 'skipped'):
        if state in states:
            return state
    return 'waiting'


class DownloadRepository:
    def __init__(self, engine: Engine, *, known_model_ids: tuple[str, ...] = ()):
        if (type(known_model_ids) is not tuple or len(known_model_ids) > 64
                or any(type(identity) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', identity) for identity in known_model_ids)
                or len(set(known_model_ids)) != len(known_model_ids)):
            raise DownloadError('input_limit')
        self.engine = engine
        # Task 11 injects its bundled manifest IDs, without invoking disk/model
        # verification or installation while a write receipt holds the lock.
        self.known_model_ids = frozenset(known_model_ids)

    @staticmethod
    def _write(connection):
        if not isinstance(connection, Connection) or not connection.in_transaction():
            raise DownloadError('metadata_missing')

    @staticmethod
    def _row(connection, model, identity):
        result = connection.execute(select(model.__table__).where(model.id == identity)).mappings().first()
        if result is None:
            raise DownloadError('source_missing')
        return result

    def _item(self, connection, item_id):
        item = self._row(connection, DownloadItemRow, item_id)
        self._row(connection, DownloadCandidateRow, item['candidate_id'])
        task = self._row(connection, TaskCenterRow, item['task_id'])
        if task['kind'] != 'download':
            raise DownloadError('metadata_missing')
        return item

    def _ownership(self, connection, candidate_id, item_id):
        self._row(connection, DownloadCandidateRow, candidate_id)
        if item_id is not None and self._item(connection, item_id)['candidate_id'] != candidate_id:
            raise DownloadError('identity_mismatch')

    def save_candidate(self, connection: Connection, candidate: Candidate, *, durable_id: str | None = None) -> str:
        self._write(connection)
        if not isinstance(candidate, Candidate) or candidate.platform not in ('direct', 'douyin', 'local'):
            raise DownloadError('unsupported_source')
        _identity(candidate.id, 128)
        if durable_id is not None and (type(durable_id) is not str or not re.fullmatch(r'[a-f0-9]{32}', durable_id)):
            raise DownloadError('input_limit')
        key = _source_key(candidate.source_key)
        canonical = None
        if candidate.platform == 'douyin':
            if type(candidate.work_id) is not str or not re.fullmatch(r'\d{1,32}', candidate.work_id):
                raise DownloadError('metadata_missing')
            if not key.startswith(('douyin_video:', 'douyin_profile:')):
                raise DownloadError('unsafe_source')
            canonical = 'https://www.douyin.com/video/' + candidate.work_id
            if candidate.canonical_source != canonical:
                raise DownloadError('unsafe_source')
            identity_filter = (DownloadCandidateRow.platform == 'douyin') & (DownloadCandidateRow.work_id == candidate.work_id)
            work_id = candidate.work_id
        elif candidate.platform == 'local':
            if not key.startswith('local:') or candidate.canonical_source is not None:
                raise DownloadError('unsafe_source')
            identity_filter = (DownloadCandidateRow.platform == 'local') & (DownloadCandidateRow.source_key == key)
            work_id = key
        else:
            if not key.startswith('direct:'):
                raise DownloadError('unsafe_source')
            identity_filter = (DownloadCandidateRow.platform == 'direct') & (DownloadCandidateRow.source_key == key)
            work_id = key
        if candidate.duration_ms is not None and (type(candidate.duration_ms) is not int or candidate.duration_ms < 0):
            raise DownloadError('metadata_missing')
        if candidate.published_at is not None:
            try:
                if type(candidate.published_at) is not str or len(candidate.published_at) > 64:
                    raise ValueError()
                datetime.fromisoformat(candidate.published_at)
            except ValueError:
                raise DownloadError('metadata_missing') from None
        existing = connection.execute(select(DownloadCandidateRow.__table__).where(identity_filter)).mappings().first()
        if candidate.cover_resource_id is not None:
            cover = self._row(connection, DownloadOutputRow, candidate.cover_resource_id)
            if not existing or cover['candidate_id'] != existing['id'] or cover['kind'] != 'cover' or cover['item_id'] is not None:
                raise DownloadError('identity_mismatch')
        now = _now()
        values = dict(platform=candidate.platform, work_id=work_id, source_key=key, canonical_source=canonical,
                      title=_display(candidate.title), author=_display(candidate.author), duration_ms=candidate.duration_ms,
                      published_at=candidate.published_at, cover_resource_id=candidate.cover_resource_id,
                      updated_at=now)
        if existing is not None and candidate.cover_resource_id is None:
            values.pop('cover_resource_id')  # Metadata absence is not a detach intent.
        if existing is not None:
            connection.execute(update(DownloadCandidateRow).where(DownloadCandidateRow.id == existing['id']).values(**values))
            return existing['id']
        connection.execute(sqlite_insert(DownloadCandidateRow).values(id=durable_id or candidate.id, **values, region_json=_json(asdict(Region())), created_at=now).on_conflict_do_nothing())
        saved = connection.execute(select(DownloadCandidateRow.id).where(identity_filter)).scalar_one_or_none()
        if saved is None:
            raise DownloadError('identity_mismatch')
        return saved

    def attach_cover(self, connection: Connection, candidate_id: str, resource_id: str | None) -> None:
        self._write(connection)
        self._row(connection,DownloadCandidateRow,candidate_id)
        if resource_id is not None:
            resource = self._row(connection,DownloadOutputRow,resource_id)
            if (resource['candidate_id'] != candidate_id or resource['item_id'] is not None or resource['kind'] != 'cover'):
                raise DownloadError('identity_mismatch')
        connection.execute(update(DownloadCandidateRow).where(DownloadCandidateRow.id==candidate_id)
                           .values(cover_resource_id=resource_id,updated_at=_now()))

    def set_region(self, connection: Connection, candidate_id: str, region: Region) -> None:
        self._write(connection)
        self._row(connection, DownloadCandidateRow, candidate_id)
        if not isinstance(region, Region):
            raise DownloadError('input_limit')
        connection.execute(update(DownloadCandidateRow).where(DownloadCandidateRow.id == candidate_id).values(region_json=_json(asdict(region)), updated_at=_now()))

    def get_region(self, connection: Connection, candidate_id: str) -> Region:
        return Region(**json.loads(self._row(connection, DownloadCandidateRow, candidate_id)['region_json']))

    @staticmethod
    def _selected(options):
        result = {}
        if options.save_video:
            result['video'] = 'video'
        if options.audio_format:
            result['audio'] = 'audio_' + options.audio_format
        if options.silent_video:
            result['silent'] = 'silent_video'
        if options.asr_language:
            result['asr'] = 'asr'
        if options.ocr_enabled:
            result['ocr'] = 'ocr'
        return result

    def create_batch(self, connection: Connection, candidate_ids: tuple[str, ...], options: DownloadOptions,
                     directory_grant_id: str, regions: dict[str, Region]) -> str:
        self._write(connection)
        if (type(candidate_ids) is not tuple or not 1 <= len(candidate_ids) <= 200
                or any(type(key) is not str for key in candidate_ids) or len(set(candidate_ids)) != len(candidate_ids)
                or not isinstance(options, DownloadOptions) or type(regions) is not dict
                or set(regions) != set(candidate_ids) or any(not isinstance(region, Region) for region in regions.values())):
            raise DownloadError('input_limit')
        _identity(directory_grant_id)
        selected = self._selected(options)
        platforms={self._row(connection,DownloadCandidateRow,key)['platform'] for key in candidate_ids}
        local=platforms=={'local'}
        if 'local' in platforms and not local:raise DownloadError('unsupported_source')
        if local and (options.save_video or options.prefer_clean or not selected):raise DownloadError('unsupported_source')
        task_id = _id('download_')
        frozen = []
        now = _now()
        for position, key in enumerate(candidate_ids):
            row = self._row(connection, DownloadCandidateRow, key)
            fields = {field: row[field] for field in Candidate.__dataclass_fields__}
            frozen.append(dict(id=_id(), candidate_id=key, position=position, candidate=fields,
                               region=asdict(regions[key]), options=asdict(options), stages=selected))
        snapshot = dict(version=1, directory_grant_id=directory_grant_id, options=asdict(options), items=frozen)
        if local:snapshot['source_mode']='local'
        connection.execute(insert(TaskCenterRow).values(id=task_id, kind='download', name=f'{"本地提取" if local else "下载与提取"} · {len(frozen)}项',
                           state='waiting', account_id=None, snapshot_json=_json(snapshot), queue_order=0, created_at=now, updated_at=now))
        for item in frozen:
            connection.execute(insert(DownloadItemRow).values(id=item['id'], task_id=task_id, candidate_id=item['candidate_id'],
                               position=item['position'], snapshot_json=_json(item), created_at=now))
            connection.execute(insert(DownloadStageRow), [dict(id=_id(), item_id=item['id'], stage=stage, required_kind=kind,
                               state='waiting', resource_id=None, error_code=None, updated_at=now) for stage, kind in selected.items()])
        return task_id

    def save_output(self, connection: Connection, resource: OutputResource, *, backend_path: str) -> None:
        self._write(connection)
        if not isinstance(resource, OutputResource):
            raise DownloadError('metadata_missing')
        _identity(resource.id)
        if (resource.kind not in KINDS or type(resource.sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}', resource.sha256)
                or type(resource.size_bytes) is not int or resource.size_bytes < 0
                or resource.duration_ms is not None and (type(resource.duration_ms) is not int or resource.duration_ms < 0)
                or type(backend_path) is not str or not Path(backend_path).is_absolute()
                or any(ord(char) < 32 for char in backend_path)):
            raise DownloadError('metadata_missing')
        self._ownership(connection, resource.candidate_id, resource.item_id)
        connection.execute(insert(DownloadOutputRow).values(**asdict(resource), backend_path=backend_path, created_at=_now()))

    def get_output(self, resource_id: str) -> OutputResource:
        with self.engine.connect() as connection:
            row = self._row(connection, DownloadOutputRow, resource_id)
            return OutputResource(**{field: row[field] for field in OutputResource.__dataclass_fields__})

    def output_path(self, resource_id: str) -> str:
        """Backend-only lookup; never include this result in a bridge payload."""
        with self.engine.connect() as connection:
            return self._row(connection, DownloadOutputRow, resource_id)['backend_path']

    def record_stage(self, connection: Connection, item_id: str, result: StageResult) -> None:
        self._write(connection)
        item = self._item(connection, item_id)
        if not isinstance(result, StageResult) or result.state not in STAGE_STATES:
            raise DownloadError('metadata_missing')
        _error_code(result.error_code)
        stage = connection.execute(select(DownloadStageRow.__table__).where(DownloadStageRow.item_id == item_id, DownloadStageRow.stage == result.stage)).mappings().first()
        if stage is None:
            raise DownloadError('metadata_missing')
        if result.state == 'succeeded':
            if result.resource_id is None or result.error_code is not None:
                raise DownloadError('metadata_missing')
            resource = self._row(connection, DownloadOutputRow, result.resource_id)
            if resource['candidate_id'] != item['candidate_id'] or resource['item_id'] != item_id or resource['kind'] != stage['required_kind']:
                raise DownloadError('identity_mismatch')
        elif result.resource_id is not None:
            raise DownloadError('metadata_missing')
        model_wait = result.state == 'waiting' and result.error_code in ('model_required', 'model_invalid', 'model_unknown')
        if result.state not in ('failed', 'cancelled', 'interrupted') and result.error_code is not None and not model_wait:
            raise DownloadError('metadata_missing')
        if stage['state'] in TERMINAL:
            if (stage['state'], stage['resource_id'], stage['error_code']) == (result.state, result.resource_id, result.error_code):
                return
            raise DownloadError('metadata_missing')
        connection.execute(update(DownloadStageRow).where(DownloadStageRow.id == stage['id']).values(state=result.state,
                           resource_id=result.resource_id, error_code=result.error_code, updated_at=_now()))
        self._refresh(connection, item['task_id'])

    def reset_stage(self, connection: Connection, item_id: str, stage: str, *, intent: str,
                    error_code: str | None = None) -> None:
        """Finite explicit control/recovery reset; never automatically rerun success."""
        self._write(connection)
        item = self._item(connection, item_id)
        row = connection.execute(select(DownloadStageRow.__table__).where(
            DownloadStageRow.item_id == item_id, DownloadStageRow.stage == stage)).mappings().first()
        if row is None or intent not in ('resume', 'retry', 'recovery'):
            raise DownloadError('input_limit')
        _error_code(error_code)
        if intent == 'recovery':
            if error_code not in ('source_missing', 'identity_mismatch', 'media_invalid', 'metadata_missing'):
                raise DownloadError('input_limit')
            state = 'interrupted'
        else:
            if error_code is not None or row['state'] == 'running':
                raise DownloadError('input_limit')
            if row['state'] == 'succeeded' and not (intent == 'retry' and stage in ('asr', 'ocr')):
                return
            if row['state'] == 'skipped':
                return
            state = 'waiting'
        connection.execute(update(DownloadStageRow).where(DownloadStageRow.id == row['id']).values(
            state=state, resource_id=None, error_code=error_code, updated_at=_now()))
        self._refresh(connection, item['task_id'])

    def _summary(self, connection, task_id):
        task = self._row(connection, TaskCenterRow, task_id)
        if task['kind'] != 'download':
            raise DownloadError('unsupported_source')
        counts = dict.fromkeys(STATES, 0)
        stage_counts = {}
        result_items = []
        snapshot = json.loads(task['snapshot_json'])
        items = snapshot['items']
        stage_rows = connection.execute(select(DownloadStageRow.__table__).join(DownloadItemRow, DownloadStageRow.item_id == DownloadItemRow.id).where(DownloadItemRow.task_id == task_id)).mappings().all()
        by_item = {item['id']: {} for item in items}
        for stage in stage_rows:
            if stage['item_id'] in by_item:
                by_item[stage['item_id']][stage['stage']] = stage
        for item in items:
            stages = []
            for name, required_kind in item['stages'].items():
                stage = by_item[item['id']].get(name)
                if stage is None or stage['required_kind'] != required_kind:
                    stage = dict(stage=name, state='interrupted', resource_id=None, error_code='metadata_missing')
                stages.append(stage)
                stage_counts.setdefault(name, dict.fromkeys(STAGE_STATES, 0))[stage['state']] += 1
            state = _aggregate([stage['state'] for stage in stages])
            counts[state] += 1
            result_items.append(dict(id=item['id'], candidate_id=item['candidate_id'], state=state,
                stages={stage['stage']: {field: stage[field] for field in ('state', 'resource_id', 'error_code')} for stage in stages}))
        return dict(task_id=task_id, state=task['state'], total=len(items), counts=counts,
                    stages=stage_counts, items=result_items, snapshot=snapshot)

    def _refresh(self, connection, task_id):
        summary = self._summary(connection, task_id)
        state = _aggregate([item['state'] for item in summary['items']])
        connection.execute(update(TaskCenterRow).where(TaskCenterRow.id == task_id).values(state=state, updated_at=_now()))

    def summary(self, task_id: str) -> dict:
        with self.engine.connect() as connection:
            return self._summary(connection, task_id)

    def record_decision(self, connection: Connection, item_id: str, decision: str) -> str:
        self._write(connection)
        item = self._item(connection, item_id)
        if decision not in ('original', 'skip'):
            raise DownloadError('input_limit')
        identity = _id()
        connection.execute(insert(DownloadDecisionRow).values(id=identity, item_id=item_id, decision=decision, created_at=_now()))
        if decision == 'skip':
            connection.execute(update(DownloadStageRow).where(DownloadStageRow.item_id == item_id, DownloadStageRow.state.not_in(TERMINAL))
                               .values(state='skipped', resource_id=None, error_code=None, updated_at=_now()))
            self._refresh(connection, item['task_id'])
        return identity

    def create_resolution(self, connection: Connection, source: SourceInput) -> str:
        self._write(connection)
        values = _source(source)
        identity, now = _id('resolve_'), _now()
        connection.execute(insert(DownloadResolutionRow).values(id=identity, **values, completion='partial', created_at=now, updated_at=now))
        return identity

    @staticmethod
    def _auxiliary_schema(connection):
        # 0014 is unreleased. Detect an older disposable developer database
        # rather than silently treating its old auxiliary rows as this schema.
        try:
            present = {column['name'] for column in inspect(connection).get_columns('download_auxiliary_jobs')}
        except NoSuchTableError:
            raise DownloadError('metadata_missing') from None
        if not set(DownloadAuxiliaryJobRow.__table__.columns.keys()) <= present:
            raise DownloadError('metadata_missing')

    @staticmethod
    def _categories(encoded):
        try:
            ids = json.loads(encoded)
        except (ValueError, TypeError):
            raise DownloadError('metadata_missing') from None
        if type(ids) is not list or len(ids) > 100 or any(type(identity) is not str for identity in ids) or len(set(ids)) != len(ids):
            raise DownloadError('input_limit')
        return tuple(_identity(identity) for identity in ids)

    def _auxiliary_inputs(self, connection, kind, values, *, check_categories=True):
        allowed = {
            'resolve': {'session_id', 'candidate_id'},
            'cover': {'session_id', 'candidate_id'},
            'preview': {'session_id', 'candidate_id'},
            'region': {'session_id', 'candidate_id'},
            'resource_import': {'candidate_id', 'output_resource_id', 'category_ids_json'},
            'resource_preview': {'candidate_id', 'output_resource_id'},
            'resource_reveal': {'candidate_id', 'output_resource_id'},
            'resource_delete': {'candidate_id', 'output_resource_id'},
            'subtitle_export': {'candidate_id', 'subtitle_original_id', 'subtitle_draft_id', 'subtitle_revision',
                                'subtitle_origin', 'export_format', 'output_grant_id'},
            'model_download': {'model_id'},
            'model_import': {'model_id', 'input_grant_id'},
        }
        if kind not in get_args(AuxiliaryKind):
            raise DownloadError('input_limit')
        present = {field for field in AUXILIARY_INPUTS if values[field] is not None
                   and not (field == 'category_ids_json' and values[field] == '[]')}
        if not present <= allowed[kind]:
            raise DownloadError('input_limit')
        for field in present - {'category_ids_json', 'subtitle_revision', 'export_format', 'subtitle_origin'}:
            if field == 'model_id':
                _model_identity(values[field])
            else:
                _identity(values[field], 128 if field == 'candidate_id' else 64)
        if values['session_id'] is not None:
            self._row(connection, DownloadResolutionRow, values['session_id'])
        if kind == 'resolve' and values['session_id'] is None:
            raise DownloadError('input_limit')
        if kind in ('cover', 'preview', 'region') and values['candidate_id'] is None:
            raise DownloadError('input_limit')
        if kind in ('model_download', 'model_import'):
            if values['model_id'] not in self.known_model_ids:
                raise DownloadError('model_unknown')
            if kind == 'model_import' and values['input_grant_id'] is None:
                raise DownloadError('input_limit')
        elif kind in ('resource_import', 'resource_preview', 'resource_reveal', 'resource_delete'):
            if values['output_resource_id'] is None:
                raise DownloadError('input_limit')
            resource = self._row(connection, DownloadOutputRow, values['output_resource_id'])
            permitted = ('source_video', 'video', 'silent_video') if kind == 'resource_import' else ('source_video', 'video', 'preview', 'silent_video', 'audio_mp3', 'audio_wav', 'cover') if kind == 'resource_preview' else KINDS
            if resource['kind'] not in permitted:
                raise DownloadError('unsupported_source')
            self._ownership(connection, resource['candidate_id'], resource['item_id'])
            if values['candidate_id'] is not None and values['candidate_id'] != resource['candidate_id']:
                raise DownloadError('identity_mismatch')
            values['candidate_id'] = resource['candidate_id']
            categories = self._categories(values['category_ids_json'])
            if check_categories and kind == 'resource_import':
                for identity in categories:
                    if self._row(connection, Category, identity)['kind'] != 'video':
                        raise DownloadError('identity_mismatch')
        elif kind == 'subtitle_export':
            if (values['subtitle_original_id'] is None or values['output_grant_id'] is None
                    or values['export_format'] not in ('txt', 'srt', 'vtt')
                    or type(values['subtitle_revision']) is not int):
                raise DownloadError('input_limit')
            original = self._row(connection, DownloadSubtitleOriginalRow, values['subtitle_original_id'])
            self._ownership(connection, original['candidate_id'], original['item_id'])
            if values['subtitle_origin'] != original['origin']:
                raise DownloadError('identity_mismatch')
            if values['candidate_id'] is not None and values['candidate_id'] != original['candidate_id']:
                raise DownloadError('identity_mismatch')
            values['candidate_id'] = original['candidate_id']
            if values['subtitle_draft_id'] is None:
                if values['subtitle_revision'] != 0:
                    raise DownloadError('subtitle_invalid')
            else:
                draft = self._row(connection, DownloadSubtitleDraftRow, values['subtitle_draft_id'])
                if draft['original_id'] != original['id']:
                    raise DownloadError('identity_mismatch')
                revision = connection.execute(select(DownloadSubtitleRevisionRow.id).where(
                    DownloadSubtitleRevisionRow.draft_id == draft['id'], DownloadSubtitleRevisionRow.revision == values['subtitle_revision'])).scalar_one_or_none()
                if revision is None:
                    raise DownloadError('subtitle_invalid')
        if values['candidate_id'] is not None:
            self._row(connection, DownloadCandidateRow, values['candidate_id'])
        return values

    def create_auxiliary_job(self, connection: Connection, *, kind: AuxiliaryKind, session_id: str | None = None,
                             candidate_id: str | None = None, model_id: str | None = None, output_resource_id: str | None = None,
                             subtitle_original_id: str | None = None, subtitle_draft_id: str | None = None,
                             subtitle_revision: int | None = None, subtitle_origin: str | None = None, export_format: str | None = None,
                             input_grant_id: str | None = None, output_grant_id: str | None = None, category_ids: tuple[str, ...] = ()) -> str:
        self._write(connection)
        self._auxiliary_schema(connection)
        if type(category_ids) is not tuple or len(category_ids) > 100 or any(type(identity) is not str for identity in category_ids):
            raise DownloadError('input_limit')
        values = dict(session_id=session_id, candidate_id=candidate_id, model_id=model_id, output_resource_id=output_resource_id,
                      subtitle_original_id=subtitle_original_id, subtitle_draft_id=subtitle_draft_id, subtitle_revision=subtitle_revision,
                      subtitle_origin=subtitle_origin, export_format=export_format, input_grant_id=input_grant_id,
                      output_grant_id=output_grant_id, category_ids_json=_json(category_ids))
        values = self._auxiliary_inputs(connection, kind, values)
        identity, now = _id('job_'), _now()
        connection.execute(insert(DownloadAuxiliaryJobRow).values(id=identity, kind=kind, **values,
                           state='waiting', completion='partial', error_code=None, resource_id=None, library_id=None,
                           installed_model_id=None, created_at=now, updated_at=now))
        return identity

    def record_auxiliary_job(self, connection: Connection, job_id: str, *, state: str, completion: str = 'partial',
                             error_code: str | None = None, resource_id: str | None = None, library_id: str | None = None,
                             installed_model_id: str | None = None) -> None:
        self._write(connection)
        self._auxiliary_schema(connection)
        row = self._row(connection, DownloadAuxiliaryJobRow, job_id)
        if state not in STAGE_STATES or completion not in COMPLETIONS:
            raise DownloadError('metadata_missing')
        _error_code(error_code)
        self._auxiliary_inputs(connection, row['kind'], {field: row[field] for field in AUXILIARY_INPUTS},
                               check_categories=state not in ('failed', 'cancelled', 'interrupted'))
        results = dict(resource_id=resource_id, library_id=library_id, installed_model_id=installed_model_id)
        result_fields = {field for field, value in results.items() if value is not None}
        for field in result_fields:
            if field == 'installed_model_id':
                _model_identity(results[field])
            else:
                _identity(results[field])
        expected = {'cover': 'resource_id', 'preview': 'resource_id', 'resource_import': 'library_id',
                    'subtitle_export': 'resource_id', 'model_download': 'installed_model_id', 'model_import': 'installed_model_id'}.get(row['kind'])
        if (state != 'succeeded' and result_fields
                or state == 'succeeded' and result_fields != ({expected} if expected else set())):
            raise DownloadError('metadata_missing')
        if state == 'succeeded' and expected == 'resource_id':
            resource = self._row(connection, DownloadOutputRow, resource_id)
            if row['kind'] == 'subtitle_export':
                original = self._row(connection, DownloadSubtitleOriginalRow, row['subtitle_original_id'])
                expected_kind, expected_item = 'subtitle_' + row['export_format'], original['item_id']
            else:
                expected_kind, expected_item = row['kind'], None
            if resource['kind'] != expected_kind or resource['candidate_id'] != row['candidate_id'] or resource['item_id'] != expected_item:
                raise DownloadError('identity_mismatch')
        elif state == 'succeeded' and expected == 'library_id':
            source = self._row(connection, DownloadOutputRow, row['output_resource_id'])
            library = self._row(connection, MediaAsset, library_id)
            if library['kind'] != 'video' or library['content_hash'] != source['sha256'] or library['missing']:
                raise DownloadError('identity_mismatch')
        elif state == 'succeeded' and expected == 'installed_model_id' and installed_model_id != row['model_id']:
            raise DownloadError('identity_mismatch')
        connection.execute(update(DownloadAuxiliaryJobRow).where(DownloadAuxiliaryJobRow.id == job_id).values(
            state=state, completion=completion, error_code=error_code, **results, updated_at=_now()))
        if row['session_id'] is not None:
            connection.execute(update(DownloadResolutionRow).where(DownloadResolutionRow.id == row['session_id']).values(completion=completion, updated_at=_now()))

    def get_auxiliary_job(self, job_id: str) -> dict:
        with self.engine.connect() as connection:
            self._auxiliary_schema(connection)
            row = self._row(connection, DownloadAuxiliaryJobRow, job_id)
            fields = ('id', 'kind', 'state', 'completion', 'error_code', 'resource_id', 'library_id', 'installed_model_id')
            result = {field: row[field] for field in fields + AUXILIARY_INPUTS if field != 'category_ids_json'}
            result['category_ids'] = self._categories(row['category_ids_json'])
            return result

    def link_source(self, connection: Connection, candidate_id: str, source: SourceInput, *, session_id: str | None = None) -> str:
        self._write(connection)
        self._row(connection, DownloadCandidateRow, candidate_id)
        values = _source(source)
        if session_id is not None:
            session = self._row(connection, DownloadResolutionRow, session_id)
            if session['source_key'] != values['source_key']:
                raise DownloadError('identity_mismatch')
        existing = connection.execute(select(DownloadSourceRow.id).where(DownloadSourceRow.candidate_id == candidate_id, DownloadSourceRow.source_key == values['source_key'])).scalar_one_or_none()
        if existing is not None:
            return existing
        identity = _id()
        connection.execute(insert(DownloadSourceRow).values(id=identity, candidate_id=candidate_id, session_id=session_id, **values, created_at=_now()))
        return identity

    @staticmethod
    def _segments(segments):
        if type(segments) is not tuple or len(segments) > 100000 or any(not isinstance(segment, SubtitleSegment) for segment in segments):
            raise DownloadError('input_limit')
        if len({segment.id for segment in segments}) != len(segments) or any(right.start_ms < left.start_ms for left, right in zip(segments, segments[1:])):
            raise DownloadError('input_limit')
        return _json([asdict(segment) for segment in segments])

    def save_subtitle_original(self, connection: Connection, candidate_id: str, item_id: str | None, origin: str, segments: tuple[SubtitleSegment, ...]) -> str:
        self._write(connection)
        self._ownership(connection, candidate_id, item_id)
        if origin not in ('platform_caption', 'embedded_caption', 'asr', 'ocr'):
            raise DownloadError('input_limit')
        encoded = self._segments(segments)
        identity = _id('original_')
        connection.execute(insert(DownloadSubtitleOriginalRow).values(id=identity, candidate_id=candidate_id, item_id=item_id,
                           origin=origin, segments_json=encoded, created_at=_now()))
        return identity

    def create_subtitle_draft(self, connection: Connection, original_id: str) -> str:
        self._write(connection)
        original = self._row(connection, DownloadSubtitleOriginalRow, original_id)
        self._ownership(connection, original['candidate_id'], original['item_id'])
        identity = _id('draft_')
        connection.execute(insert(DownloadSubtitleDraftRow).values(id=identity, original_id=original_id, created_at=_now()))
        return identity

    def append_subtitle_revision(self, connection: Connection, draft_id: str, segments: tuple[SubtitleSegment, ...]) -> int:
        self._write(connection)
        draft = self._row(connection, DownloadSubtitleDraftRow, draft_id)
        original = self._row(connection, DownloadSubtitleOriginalRow, draft['original_id'])
        self._ownership(connection, original['candidate_id'], original['item_id'])
        encoded = self._segments(segments)
        previous = connection.execute(select(func.max(DownloadSubtitleRevisionRow.revision)).where(DownloadSubtitleRevisionRow.draft_id == draft_id)).scalar_one()
        revision = (previous or 0) + 1
        connection.execute(insert(DownloadSubtitleRevisionRow).values(id=_id(), draft_id=draft_id, revision=revision,
                           segments_json=encoded, created_at=_now()))
        return revision
