"""Recoverable local deletion: durable intent, file staging, then record commit."""
import json
from pathlib import Path
import threading
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.finished.service import FinishedVideo, FinishedVideoService
from yijianjifa.features.library.active_references import ensure_not_referenced, cancelled_delivery_owns_copy
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.history import PublishHistoryRow, _ACTIVE_DUPLICATE_BLOCK_STATES


class FinishedDeletion:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()

    @staticmethod
    def _guard(session, row):
        if row.task_locked:
            raise RuntimeError('成品正在被任务使用，请先处理任务。')
        ensure_not_referenced(session,[row.id,row.content_hash,row.original_path,row.stored_path],finished=row)
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow
        histories=session.execute(select(PublishHistoryRow,PublishDeliveryRow).outerjoin(
                PublishDeliveryRow,PublishDeliveryRow.history_id==PublishHistoryRow.id).where(
                PublishHistoryRow.video_hash==row.content_hash,
                PublishHistoryRow.state.in_(_ACTIVE_DUPLICATE_BLOCK_STATES)))
        for history,delivery in histories:
            if delivery is None or not cancelled_delivery_owns_copy(session,delivery,row):
                raise RuntimeError('视频仍有未处理的发布安排，请先处理任务。')
        if row.origin_type=='generated' and row.stored_path:
            target=Path(row.stored_path)
            if target.is_symlink():raise RuntimeError('文件位置已改变，请检查后再删除。')
            if target.exists() and sha256_file(target)!=row.content_hash:
                raise RuntimeError('文件内容已改变，保留文件与记录，请先检查。')
            for other in session.scalars(select(FinishedVideo).where(FinishedVideo.id!=row.id)):
                path=other.stored_path or other.original_path
                if path and Path(path).resolve()==target.resolve():
                    raise RuntimeError('同一文件还被其他成品记录使用。')

    @staticmethod
    def _put(session, data):
        key='finished-delete:'+data['id'];row=session.get(Setting,key)
        if row is None:session.add(Setting(key=key,value_json=json.dumps(data)))
        else:row.value_json=json.dumps(data)

    def _read(self, pid):
        with session_scope(self.engine) as session:
            row=session.get(Setting,'finished-delete:'+pid)
            return json.loads(row.value_json) if row else None

    def _store(self, data):
        with session_scope(self.engine) as session:self._put(session,data)

    def preview(self, ids):
        items=[]
        with session_scope(self.engine) as session:
            for pid in dict.fromkeys(ids):
                row=session.get(FinishedVideo,pid)
                if row is None:
                    items.append({'id':pid,'name':pid,'allowed':False,'reason':'记录已不存在。'});continue
                item={'id':pid,'name':row.display_name,'origin':row.origin_type,'allowed':True,'reason':''}
                try:self._guard(session,row)
                except RuntimeError as exc:item.update(allowed=False,reason=str(exc))
                items.append(item)
        return {'items':items}

    def _recover_one(self, data):
        if data['state'] in ('completed','aborted'):return
        original=Path(data['original']) if data['owned'] else None
        staged=Path(data['staged']) if data['owned'] else None
        if staged and (staged.parent!=original.parent or staged.name!='.p011-delete-'+data['id'] or staged.is_symlink()):
            raise RuntimeError('删除恢复记录需要人工检查。')
        exists=FinishedVideoService(self.engine).get(data['id']) is not None
        if data['state']=='prepared':
            if not exists:raise RuntimeError('成品删除记录状态不一致。')
            if staged and staged.exists():
                if original.exists():raise RuntimeError('原位置已有文件，保留待恢复副本。')
                staged.rename(original)
            data['state']='aborted';self._store(data)
        elif data['state']=='committed':
            if exists:raise RuntimeError('成品删除记录状态不一致。')
            if staged:staged.unlink(missing_ok=True)
            data['state']='completed';self._store(data)

    def recover(self):
        errors=[]
        with self.lock:
            with session_scope(self.engine) as session:
                journals=[json.loads(r.value_json) for r in session.scalars(select(Setting).where(Setting.key.like('finished-delete:%')))]
            for data in journals:
                try:self._recover_one(data)
                except Exception:errors.append(data['id'])
        return errors

    def delete(self, pid):
        with self.lock:
            old=self._read(pid)
            if old and old['state'] in ('prepared','committed'):
                self._recover_one(old);old=self._read(pid)
            if old and old['state']=='completed':
                return {'id':pid,'deleted':True,'file_cleanup_pending':False}
            # Persist recovery intent before moving bytes. The second transaction
            # revalidates references while holding SQLite's write lock.
            with self.engine.connect() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                with session_scope(connection) as session:
                    row=session.get(FinishedVideo,pid)
                    if row is None:return {'id':pid,'deleted':False,'already_missing':True}
                    self._guard(session,row)
                    owned=row.origin_type=='generated' and bool(row.stored_path)
                    original=Path(row.stored_path) if owned else None
                    staged=original.with_name('.p011-delete-'+pid) if owned else None
                    if staged and staged.exists():raise RuntimeError('存在未处理的删除副本，请先恢复。')
                    data={'id':pid,'state':'prepared','owned':owned,'original':str(original) if original else None,'staged':str(staged) if staged else None,
                          'display_name':row.display_name,'content_hash':row.content_hash}
                    self._put(session,data)
                connection.commit()
            try:
                with self.engine.connect() as connection:
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                    with session_scope(connection) as session:
                        row=session.get(FinishedVideo,pid)
                        if row is None:raise RuntimeError('成品已改变，请重新预览。')
                        self._guard(session,row)
                        if owned and row.stored_path!=str(original):raise RuntimeError('成品位置已改变。')
                        if original and original.exists():original.rename(staged)
                        from yijianjifa.storage.models import CategoryLink
                        session.query(CategoryLink).filter(CategoryLink.target_type=="finished",CategoryLink.target_id==pid).delete(synchronize_session=False)
                        session.delete(row)
                        data['state']='committed';self._put(session,data)
                    connection.commit()
            except Exception:
                data['state']='prepared'
                self._recover_one(data)
                raise
            try:
                self._recover_one(data)
                return {'id':pid,'deleted':True,'file_cleanup_pending':False}
            except OSError:
                return {'id':pid,'deleted':True,'file_cleanup_pending':True}
