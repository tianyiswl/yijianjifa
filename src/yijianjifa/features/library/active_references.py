"""Protect library records referenced by unfinished frozen tasks."""
import json

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


def ensure_not_referenced(session, identities):
    identities = {str(value) for value in identities if value}
    terminal = {"completed", "completed_with_errors", "cancelled", "published", "success"}
    tables = set(inspect(session.connection()).get_table_names())
    if {'publish_deliveries','publish_v2_resource_consumers'}<=tables:
        from yijianjifa.features.publish.deliveries import PublishDeliveryRow, ResourceConsumerRow
        # The child consumer is authoritative. A remaining sibling video cannot
        # keep an already released original locked through the mixed parent.
        rows=session.execute(select(PublishDeliveryRow).join(ResourceConsumerRow,
            ResourceConsumerRow.delivery_id==PublishDeliveryRow.id).where(ResourceConsumerRow.released.is_(False)))
        for row in rows.scalars():
            values=set(_strings(json.loads(row.snapshot_json)))
            if identities & values:
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
