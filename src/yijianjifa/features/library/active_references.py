"""Protect library records referenced by unfinished frozen tasks."""
import json
from pathlib import Path

from sqlalchemy import inspect, text, select


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def cancelled_delivery_owns_copy(session, delivery, finished):
    """Cancelled/deleted local tasks retain evidence in their own frozen copy.

    This does not release consumers, authorize retries, or change publish history.
    Running/uncancelled targets and legacy histories without a copy still lock.
    """
    from yijianjifa.features.publish.deliveries import PublishResourceRow, ResourceConsumerRow, digest
    from yijianjifa.features.tasks.service import TaskItemRow
    from .media_identity import sha256_file
    if (finished is None or not delivery.cancel_requested
            or delivery.video_id != finished.id or delivery.video_hash != finished.content_hash):
        return False
    item = session.get(TaskItemRow, delivery.item_id)
    if item is None or item.state in {'running', 'paused_auth'}:
        return False
    try:
        snapshot = json.loads(delivery.snapshot_json)
        if (digest(snapshot) != delivery.request_digest or snapshot.get('video_id') != finished.id
                or snapshot.get('video_hash') != finished.content_hash):
            return False
        resource = session.get(PublishResourceRow, snapshot.get('resource_id'))
        if (resource is None or resource.cleaned or resource.task_id != delivery.task_id
                or resource.content_hash != finished.content_hash or resource.path != snapshot.get('video_path')
                or session.scalar(select(ResourceConsumerRow.id).where(ResourceConsumerRow.resource_id == resource.id,
                    ResourceConsumerRow.delivery_id == delivery.id)) is None):
            return False
        path = Path(resource.path)
        original = finished.stored_path or finished.original_path
        if (not original or path.is_symlink() or path.parent.is_symlink()
                or path.parent.parent.name != 'publish_resources' or not path.parent.name.startswith('v2-')
                or path.resolve() == Path(original).resolve() or not path.is_file()):
            return False
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
        hashes = session.info.setdefault('finished_deletion_copy_hashes', {})
        if key not in hashes:
            hashes[key] = sha256_file(path)
        return hashes[key] == finished.content_hash
    except (ValueError, TypeError, OSError):
        return False


def ensure_not_referenced(session, identities, *, finished=None):
    identities = {str(value) for value in identities if value}
    terminal = {"completed", "completed_with_errors", "cancelled", "published", "success"}
    tables = set(inspect(session.connection()).get_table_names())
    if finished is not None and finished.origin_type=='generated' and finished.stored_path and 'mix_tasks' in tables:
        from yijianjifa.features.mixing.queue import MixTask
        # Serial parents keep success receipts outside their original snapshot.
        # Protect that parent's files until the batch itself has ended/stopped.
        path=Path(finished.stored_path)
        parent=session.get(MixTask,path.parent.name)
        if parent is not None and parent.state not in terminal:
            snapshot=json.loads(parent.snapshot_json)
            if path.parent==Path(snapshot.get('output_directory',''))/parent.id:
                raise RuntimeError('成品仍用于本批制作与发布，请先结束或取消该批任务。')
    if {'publish_deliveries','publish_v2_resource_consumers'}<=tables:
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow, ResourceConsumerRow
        # The child consumer is authoritative. A remaining sibling video cannot
        # keep an already released original locked through the mixed parent.
        rows=session.execute(select(PublishDeliveryRow).join(ResourceConsumerRow,
            ResourceConsumerRow.delivery_id==PublishDeliveryRow.id).where(ResourceConsumerRow.released.is_(False)))
        for row in rows.scalars():
            values=set(_strings(json.loads(row.snapshot_json)))
            if identities & values:
                if cancelled_delivery_owns_copy(session, row, finished):
                    continue
                raise RuntimeError('资料仍被发布目标引用，请先处理该目标后再删除')
    for table in ("mix_tasks", "task_center_records", "publish_tasks"):
        if table not in tables:
            continue
        for state, snapshot in session.execute(text(f"SELECT state, snapshot_json FROM {table}")):
            if state in terminal:
                continue
            try:
                payload=json.loads(snapshot)
                if type(payload) is dict and payload.get('version')==2 and payload.get('kind')=='multi_publish':
                    continue
                values = set(_strings(payload))
            except (ValueError, TypeError) as exc:
                raise RuntimeError("任务快照无法读取，请先处理任务后再删除资料") from exc
            if identities & values:
                raise RuntimeError("资料仍被未完成任务引用，请先处理任务后再删除")
