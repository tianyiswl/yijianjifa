"""Douyin image posts use content identities and the existing durable task lane.

One click freezes all images, metadata and the target session together. Retries
of that click read a durable receipt; subsequent explicit clicks are new tasks.
No browser, credentials or native path is accepted from the public request.
"""
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import uuid

from sqlalchemy import select

from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.features.image_posts.service import ImagePostService
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.deliveries import (
    PublishDeliveryRow, DeliveryAttemptRow, PublishResourceRow, ResourceConsumerRow,
    PublishBatchReceiptRow, canonical, digest,
)
from yijianjifa.features.publish.history import PublishHistoryRepository, PublishHistoryRow
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.schedule import ScheduleRule, build_schedule
from yijianjifa.features.tasks.service import TaskCenterRow, TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from .douyin_declaration_catalog import catalog as declaration_catalog
from .multi_publish_dto import REASONS
from .multi_publish_runtime import update_authorization


SETTINGS_KEY = 'image_post.publish.settings'
DEFAULT_SETTINGS = {'account_id':None, 'mode':'exact', 'start':None,
    'interval_hours':3, 'daily_count':1, 'days':1, 'declaration':None}


def _id(value):
    if type(value) is not str or not value.strip() or len(value) > 128:
        raise ValueError('invalid identifier')
    return value


class ImagePostPublishing:
    def __init__(self, engine, paths, runtime, *, declarations=None, can_start=None):
        self.engine, self.paths, self.runtime = engine, paths, runtime
        self.posts = ImagePostService(engine, paths)
        self.declarations = declarations
        self.can_start = can_start

    @staticmethod
    def _normalize(p, *, posts=True, settings=False):
        keys = {'account_id','mode','start','interval_hours','daily_count','days','declaration'}
        if posts:
            keys.add('post_ids')
        if type(p) is not dict or set(p) - keys:
            raise ValueError('invalid image publication settings')
        value = {**DEFAULT_SETTINGS, **copy.deepcopy(p)}
        if posts:
            ids = value.get('post_ids')
            if (type(ids) is not list or not 1 <= len(ids) <= 100 or
                    any(type(pid) is not str for pid in ids) or len(set(ids)) != len(ids)):
                raise ValueError('select image posts')
            for pid in ids:
                _id(pid)
        if value['account_id'] is not None:
            _id(value['account_id'])
        elif not settings:
            raise ValueError('select douyin account')
        if value['mode'] not in {'immediate','exact','interval','daily'}:
            raise ValueError('invalid schedule mode')
        interval = value['interval_hours']
        if type(interval) not in {int,float} or not math.isfinite(interval) or not 0 < interval <= 336:
            raise ValueError('invalid interval')
        for key in ('daily_count', 'days'):
            if type(value[key]) is not int or not 1 <= value[key] <= 200:
                raise ValueError('invalid daily schedule')
        if value['start'] is not None:
            try:
                start = datetime.fromisoformat(value['start'])
            except (TypeError, ValueError):
                raise ValueError('invalid schedule time') from None
            if start.tzinfo is None or start.utcoffset() is None:
                raise ValueError('scheduled timezone required')
            value['start'] = start.isoformat()
        if value['mode'] == 'immediate':
            # Saving a form's last date must not convert an immediate task.
            value['start'] = None
        elif value['start'] is None and not settings:
            raise ValueError('schedule time required')
        choice = value['declaration']
        if choice is not None and (type(choice) is not str or not any(
                item['id'] == choice for item in declaration_catalog()['items'])):
            raise ValueError('invalid declaration')
        return value

    def context(self):
        accounts = AccountService(self.engine).list('douyin')
        with session_scope(self.engine) as s:
            row = s.get(Setting, SETTINGS_KEY)
            settings = json.loads(row.value_json) if row else dict(DEFAULT_SETTINGS)
        if row is None:
            settings['start'] = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).replace(
                hour=12, minute=0, second=0, microsecond=0).isoformat()
        default = next((a.id for a in accounts if a.is_default), None)
        if settings.get('account_id') not in {a.id for a in accounts}:
            settings['account_id'] = default
        try:
            adapter = self.runtime.catalog.transport_for_content('douyin', 'image_post')
        except ValueError:
            adapter = None
        capabilities = set(getattr(adapter, 'verified_capabilities', ()))
        return {'accounts':[{'id':a.id, 'nickname':a.nickname, 'remark':a.remark,
                    'is_default':a.is_default, 'login_state':a.login_state} for a in accounts],
            'default_account_id':default, 'settings':settings,
            'declarations':declaration_catalog(),
            'capabilities':{'integrated':adapter is not None, 'publish_now':'publish_now' in capabilities,
                'platform_schedule':'platform_schedule' in capabilities, 'background':True,
                'platform_acceptance_tested':getattr(adapter, 'platform_acceptance_tested', False) is True}}

    def save_settings(self, p):
        value = self._normalize(p, posts=False, settings=True)
        if value['account_id'] is not None:
            account = AccountService(self.engine).get(value['account_id'])
            if account.platform != 'douyin':
                raise ValueError('douyin account required')
        with session_scope(self.engine) as s:
            row = s.get(Setting, SETTINGS_KEY)
            if row:
                row.value_json = canonical(value)
            else:
                s.add(Setting(key=SETTINGS_KEY, value_json=canonical(value)))
        return value

    def _allowed(self, engine=None):
        if self.runtime.closed.is_set():
            raise ValueError('app_closing')
        # Preserve the optional host guard outside the write transaction; host
        # callbacks may use their own connection, so must not open a second writer.
        if engine is None and callable(self.can_start) and self.can_start() is False:
            raise ValueError('publication_unavailable')

    def _plan(self, value, now):
        self._allowed()
        account = AccountService(self.engine).get(value['account_id'])
        if account.platform != 'douyin' or not account.platform_user_id:
            raise ValueError('douyin account required')
        if account.login_state != 'valid':
            raise ValueError('account_verification_required')
        captured = {'id':account.id, 'platform':'douyin', 'platform_user_id':account.platform_user_id,
            'session_fingerprint':hashlib.sha256((account.session_ref or '').encode()).hexdigest()}
        records = []
        for pid in value['post_ids']:
            record = self.posts.get(pid)
            if record is None or not record.images:
                raise ValueError('image_post_unavailable')
            if not record.title.strip():
                raise ValueError('image_post_title_required')
            if any(type(getattr(record,key)) is not str or len(getattr(record,key)) > 10000 for key in ('title','body','tags')):
                raise ValueError('invalid image post metadata')
            # The actual creator input has maxLength=20. HTML counts UTF-16
            # units, so supplementary characters must match that same limit.
            if len(record.title.encode('utf-16-le')) // 2 > 20:
                raise ValueError('image_post_title_too_long')
            if len(record.images) > 100 or [image.position for image in record.images] != list(range(len(record.images))):
                raise ValueError('image_post_order_invalid')
            for image in record.images:
                path = Path(image.path)
                if not path.is_file() or sha256_file(path) != image.content_hash:
                    raise ValueError('image_post_resource_changed')
            records.append(record)
        mode = 'publish_now' if value['mode'] == 'immediate' else 'platform_schedule'
        if mode == 'publish_now':
            times = [None] * len(records)
        else:
            if value['mode'] == 'exact' and len(records) != 1:
                raise ValueError('multiple_posts_need_interval_or_daily')
            start = datetime.fromisoformat(value['start'])
            if value['mode'] == 'daily' and start + timedelta(hours=(value['daily_count'] - 1) * value['interval_hours']) >= start.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1):
                raise ValueError('daily_schedule_crosses_midnight')
            rule = ScheduleRule(value['mode'], start, value['interval_hours'], value['daily_count'], value['days'])
            slots = build_schedule(rule, count=len(records), now=now)
            if any(slot.local_time <= now for slot in slots):
                raise ValueError('scheduled_time_in_past')
            times = [slot.local_time.astimezone(timezone.utc).isoformat() for slot in slots]
        declaration = next((dict(item) for item in declaration_catalog()['items'] if item['id'] == value['declaration']), None)
        planned = []
        selected_content = set()
        for record, when in zip(records, times):
            metadata = {key:getattr(record,key) for key in ('title','body','tags')}
            # Publication identity includes the text: one image group can form
            # several distinct works. The source image hash remains separate
            # for the managed-resource version checks performed during commit.
            content_hash = digest({'image_hash':record.content_hash, **metadata})
            if content_hash in selected_content:
                raise ValueError('duplicate_image_post_content')
            selected_content.add(content_hash)
            if declaration is not None:
                metadata['declaration'] = declaration
            payload = {'version':2, 'platform':'douyin', 'content_type':'image_post',
                'content_id':record.id, 'content_hash':content_hash, 'image_content_hash':record.content_hash,
                'account_id':account.id, 'platform_user_id':account.platform_user_id,
                'session_fingerprint':captured['session_fingerprint'],
                'fields':metadata, 'base_fields':copy.deepcopy(metadata), 'platform_overrides':{},
                'images':[{'image_id':image.id, 'path':str(image.path), 'content_hash':image.content_hash,
                    'position':image.position, 'width':image.width, 'height':image.height,
                    'format':image.format, 'original_name':image.original_name} for image in record.images],
                'execution_mode':mode, 'scheduled_at':when, 'allow_new_request':True}
            state, reason = self.runtime.service._hold(payload)
            with session_scope(self.engine) as s:
                try:
                    PublishHistoryRepository.assert_no_other_content_history(s,'image_post',content_hash,account.id,
                        identity=('douyin',account.platform_user_id),allow_new_request=True)
                except ValueError:
                    state, reason = 'waiting_configuration','existing_target_unresolved'
            planned.append({'record':record, 'payload':payload, 'state':state, 'reason':reason})
        return captured, planned

    def preview(self, p):
        messages = {'select image posts':'请选择要发布的图文。', 'select douyin account':'请选择抖音账号。',
            'account_verification_required':'账号登录已失效，请先登录。',
            'image_post_unavailable':'图文已删除或没有图片，请重新选择。', 'image_post_title_required':'请先填写每篇图文的标题。',
            'image_post_title_too_long':'图文标题最多20字，请缩短后再发布。',
            'image_post_resource_changed':'图文图片已改变或丢失，请重新导入。',
            'scheduled timezone required':'请选择带时区的发布时间。',
            'multiple_posts_need_interval_or_daily':'多篇图文请选择按间隔或每天数量安排。',
            'daily_schedule_crosses_midnight':'当天的发布安排超过午夜，请减小数量或间隔。',
            'duplicate_image_post_content':'所选图文中有图片和文案完全相同的作品，请取消其中一篇后再发布。',
            'scheduled_time_in_past':'发布时间已过，请重新选择。'}
        try:
            value = self._normalize(p)
            _, planned = self._plan(value, datetime.now(timezone.utc))
        except (ValueError, KeyError) as error:
            code = str(error).strip("'")
            return {'items':[], 'issues':[{'code':code if code in messages else 'invalid_request',
                'message':messages.get(code,'请检查图文、账号和发布时间后重试。')}], 'ready':False}
        issues = [{'code':item['reason'], 'message':REASONS.get(item['reason'],'发布条件尚未满足，请检查设置。'),
            'post_id':item['record'].id} for item in planned if item['state'] != 'waiting']
        return {'items':[{'post_id':item['record'].id, 'title':item['record'].title,
                    'image_count':len(item['record'].images), 'account_id':value['account_id'],
                    'time':item['payload']['scheduled_at'], 'scheduled_at':item['payload']['scheduled_at'],
                    'execution_mode':item['payload']['execution_mode'], 'state':item['state'],
                    'ready':item['state']=='waiting'} for item in planned], 'issues':issues, 'ready':not issues}

    @staticmethod
    def _previous(session, request_id, request_digest):
        row = session.get(PublishBatchReceiptRow, request_id)
        if row is None:
            return None
        saved = json.loads(row.receipt_json)
        if saved.get('image_params_digest') != request_digest:
            raise ValueError('request_id reused with changed content')
        return saved['result']

    def submit(self, p, request_id):
        _id(request_id)
        value = self._normalize(p)
        request_digest = digest({'operation':'image_post.publish.submit', 'params':value})
        with session_scope(self.engine) as s:
            previous = self._previous(s, request_id, request_digest)
        if previous is not None:
            return previous
        account, planned = self._plan(value, datetime.now(timezone.utc))
        if any(item['state'] != 'waiting' for item in planned):
            raise ValueError(next(item['reason'] for item in planned if item['state'] != 'waiting'))
        root = self.paths.data_dir / 'publish_resources'
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='images-', dir=root))
        task_id = str(uuid.uuid4())
        committed = False
        try:
            resources = []
            for item_index, item in enumerate(planned):
                for image in item['payload']['images']:
                    target = directory / f'{item_index}-{image["position"]}{Path(image["path"]).suffix}'
                    shutil.copyfile(image['path'], target)
                    if sha256_file(target) != image['content_hash']:
                        raise ValueError('image_post_resource_changed')
                    image['path'] = str(target)
                    image['resource_id'] = str(uuid.uuid4())
                    resources.append(PublishResourceRow(id=image['resource_id'],task_id=task_id,
                        path=str(target),content_hash=image['content_hash'],cleaned=False))
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    with session_scope(connection) as s:
                        previous = self._previous(s, request_id, request_digest)
                        if previous is not None:
                            connection.rollback()
                            shutil.rmtree(directory)
                            return previous
                        self._allowed(connection)
                        actual = AccountService(connection).get(account['id'])
                        if (actual.platform,actual.platform_user_id,actual.login_state) != ('douyin',account['platform_user_id'],'valid') or hashlib.sha256((actual.session_ref or '').encode()).hexdigest() != account['session_fingerprint']:
                            raise ValueError('target identity or session changed')
                        for item in planned:
                            original = item['record']
                            current = self.posts.get(original.id)
                            if current is None or (current.content_hash,current.title,current.body,current.tags) != (original.content_hash,original.title,original.body,original.tags):
                                raise ValueError('image_post_changed_before_save')
                            if [(i.id,i.position,i.content_hash) for i in current.images] != [(i.id,i.position,i.content_hash) for i in original.images]:
                                raise ValueError('image_post_changed_before_save')
                            PublishHistoryRepository.assert_no_other_content_history(s,'image_post',item['payload']['content_hash'],account['id'],
                                identity=('douyin',account['platform_user_id']),allow_new_request=True)
                        frozen = {'version':2,'kind':'multi_publish','content_type':'image_post','request_id':request_id,
                            'accounts':[account],'quantity':len(planned),'requested_quantity':len(planned),
                            'excluded_count':0,'consent_partial':False}
                        s.add(PublishTaskRow(id=task_id,platform='douyin',state='waiting',snapshot_json=canonical(frozen)))
                        s.add(TaskCenterRow(id=task_id,kind='multi_publish',name='抖音图文发布',account_id=account['id'],state='waiting',snapshot_json=canonical(frozen)))
                        s.flush()
                        s.add_all(resources)
                        s.flush()
                        deliveries = []
                        for position, item in enumerate(planned):
                            payload = item['payload']
                            did, iid, hid = [str(uuid.uuid4()) for _ in range(3)]
                            prior = list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.content_type=='image_post',
                                PublishHistoryRow.content_hash==payload['content_hash'],PublishHistoryRow.account_id==account['id'])))
                            s.add(TaskItemRow(id=iid,task_id=task_id,item_key=did,state='waiting',detail_json=canonical(
                                {'delivery_id':did,'content_type':'image_post','content_id':payload['content_id'],'account_id':account['id'],'platform':'douyin'})))
                            s.add(PublishHistoryRow(id=hid,video_hash=None,content_type='image_post',content_id=payload['content_id'],
                                content_hash=payload['content_hash'],account_id=account['id'],attempt=max((h.attempt for h in prior),default=0)+1,state='waiting',task_id=task_id))
                            s.flush()
                            s.add(PublishDeliveryRow(id=did,task_id=task_id,item_id=iid,history_id=hid,platform='douyin',
                                account_id=account['id'],platform_user_id=account['platform_user_id'],video_id=None,video_hash=None,
                                content_type='image_post',content_id=payload['content_id'],content_hash=payload['content_hash'],snapshot_json=canonical(payload),
                                request_digest=digest(payload),execution_mode=payload['execution_mode'],scheduled_at=payload['scheduled_at'],attempt=1,position=position,
                                hidden=False,cancel_requested=False))
                            s.flush()
                            s.add(DeliveryAttemptRow(id=str(uuid.uuid4()),delivery_id=did,number=1,snapshot_json=canonical(payload),request_digest=digest(payload)))
                            for image in payload['images']:
                                s.add(ResourceConsumerRow(id=str(uuid.uuid4()),resource_id=image['resource_id'],delivery_id=did,released=False))
                            deliveries.append(did)
                        s.flush()
                        update_authorization(s,task_id,add=deliveries)
                        result = {'task_id':task_id,'quantity':len(planned),'state':'waiting'}
                        s.add(PublishBatchReceiptRow(request_id=request_id,request_digest=request_digest,task_id=task_id,
                            receipt_json=canonical({'image_params_digest':request_digest,'result':result})))
                    # Once commit starts, ambiguous outcomes retain their copies.
                    committed = True
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            self.runtime.wake()
            return result
        except BaseException:
            if not committed:
                shutil.rmtree(directory,ignore_errors=True)
            raise
