"""Durable local confirmation. It never starts a platform adapter."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from yijianjifa.core.snapshots import FrozenSnapshot
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.publish.history import PublishHistoryRepository
from yijianjifa.features.tasks.service import TaskCenterService, TaskItemRow
from yijianjifa.storage.models import Setting
from yijianjifa.storage.db import session_scope


class PublishConfirmation:
    snapshot_kind='normal_publish'
    receipt_prefix='publish-confirm:'
    task_kind='publish'
    task_name='普通发布 · 本地待执行'

    def _capture_locked(self,connection,params,now):
        from .publish_preparation import PublishPreparation
        return PublishPreparation(connection,self.paths,declarations=self.preparation.declarations,locations=self.preparation.locations,covers=self.preparation.covers)._capture(params,now)

    def _freeze_specific(self,snapshot):
        pass

    def _task_row(self,task_id,frozen):
        return PublishTaskRow(id=task_id,platform='douyin',state='paused',snapshot_json=frozen.json)

    def __init__(self, engine, paths, preparation):
        self.engine, self.paths, self.preparation = engine, paths, preparation

    @staticmethod
    def _receipt(engine, key):
        with session_scope(engine) as session:
            row = session.get(Setting, key)
            return json.loads(row.value_json) if row else None

    def confirm(self, token, *, now=None):
        if type(token) is not str or not re.fullmatch('[a-f0-9]{48}', token):
            raise ValueError('invalid preparation token')
        fixed_now = now
        now = now or datetime.now(timezone.utc)
        key = self.receipt_prefix+hashlib.sha256(token.encode()).hexdigest()
        previous = self._receipt(self.engine, key)
        if previous:
            return previous
        original = self.preparation.resolve(token, now=now)
        snapshot = original.data
        if snapshot.get('kind')!=self.snapshot_kind:raise ValueError('incorrect preparation kind')
        root = self.paths.data_dir/'publish_resources'
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='batch-', dir=root))
        commit_attempted = False
        try:
            for index, video in enumerate(snapshot['videos']):
                source = Path(video['path'])
                target = directory/(str(index)+source.suffix)
                shutil.copyfile(source, target)
                if sha256_file(target) != video['content_hash']:
                    raise ValueError('video changed while copying')
                video['path'] = str(target)
                cover = video['cover']
                if 'path' in cover:
                    target_cover = directory/(str(index)+'-cover.png')
                    shutil.copyfile(cover['path'], target_cover)
                    if sha256_file(target_cover) != cover['sha256']:
                        raise ValueError('cover changed while copying')
                    cover['path'] = str(target_cover)
            for item in snapshot['items']:
                cover=item.get('cover')
                if cover and 'path' in cover:
                    target=directory/('item-cover-'+cover['sha256']+'.png')
                    shutil.copyfile(cover['path'],target)
                    if sha256_file(target)!=cover['sha256']:raise ValueError('item cover changed while copying')
                    cover['path']=str(target)
            now = fixed_now or datetime.now(timezone.utc)
            self.preparation.resolve(token, now=now)
            task_id = str(uuid.uuid4())
            snapshot['execution_enabled'] = False
            snapshot['hold_reason'] = 'platform_execution_not_authorized'
            self._freeze_specific(snapshot)
            frozen = FrozenSnapshot.from_mapping(snapshot)
            result = dict(id=task_id, state='paused', quantity=len(snapshot['items']), submitted=False)
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    previous = self._receipt(connection, key)
                    if previous:
                        connection.rollback()
                        shutil.rmtree(directory)
                        return previous
                    # Recheck under the SQLite write lock so history/metadata cannot race registration.
                    preview, current = self._capture_locked(connection,original.data['params'],now)
                    if not preview['locally_valid'] or current.json != original.json:
                        raise ValueError('preparation changed before confirmation')
                    history = PublishHistoryRepository(connection)
                    for item in snapshot['items']:
                        video = next(v for v in snapshot['videos'] if v['id']==item['video_id'])
                        history.assert_can_create(video['content_hash'],item['account_id'],allow_repeat_success=item.get('repeat_confirmed') is True,allow_new_request=item.get('allow_new_request') is True)
                        history.record(video['content_hash'],item['account_id'],'queued',task_id=task_id)
                    with session_scope(connection) as session:
                        session.add(self._task_row(task_id,frozen))
                    center = TaskCenterService(connection)
                    center.register_task(task_id,kind=self.task_kind,name=self.task_name,state='paused',snapshot_json=frozen.json,
                                         account_id=snapshot['accounts'][0]['id'] if len(snapshot['accounts'])==1 else None)
                    with session_scope(connection) as session:
                        for item in snapshot['items']:
                            session.add(TaskItemRow(task_id=task_id,item_key=item['account_id']+':'+item['video_id'],state='paused',detail_json=json.dumps(item,ensure_ascii=False)))
                        session.add(Setting(key=key,value_json=json.dumps(result,ensure_ascii=False)))
                    commit_attempted = True
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return result
        except BaseException:
            # A commit error can be ambiguous: retain resources rather than breaking a durable task.
            if not commit_attempted:
                shutil.rmtree(directory, ignore_errors=True)
            raise
