"""Explicit revisions to a missed, never-submitted local publish item."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.commerce.service import CommerceTaskRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.publish.schedule import ScheduleItem,PlatformLimits,validate_platform_window
from yijianjifa.features.tasks.service import TaskCenterRow,TaskItemRow
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.core.snapshots import FrozenSnapshot


def validate_request(p):
    if set(p)!={'id','item_key','mode','when','software_consent'}:raise ValueError('invalid reschedule fields')
    if any(type(p[k]) is not str or not 1<=len(p[k])<=128 for k in ('id','item_key')):raise ValueError('invalid task item')
    if p['mode'] not in ('platform_schedule','software_schedule','publish_now') or type(p['software_consent']) is not bool:raise ValueError('invalid mode or consent')
    if p['mode']=='publish_now':
        if p['when'] is not None:raise ValueError('immediate choice does not accept a scheduled time')
    elif type(p['when']) is not str or len(p['when'])>64:raise ValueError('invalid time')


def reschedule_local(connection,p,*,now=None):
    validate_request(p);now=now or datetime.now(timezone.utc)
    when=now if p['mode']=='publish_now' else datetime.fromisoformat(p['when'])
    if p['mode']!='publish_now':
        checked=validate_platform_window([ScheduleItem(0,when)],now=now,limits=PlatformLimits(2.5,13,reject_short=True)).items[0]
        if checked.error_code:raise ValueError(checked.error_code)
        if checked.required_choice and p['mode']=='platform_schedule':raise ValueError('scheduled time must be at least three hours ahead')
    if p['mode']=='software_schedule' and not p['software_consent']:raise ValueError('software consent required')
    with session_scope(connection) as s:
        task=s.get(PublishTaskRow,p['id']);center=s.get(TaskCenterRow,p['id'])
        kind='normal_publish';capability='ordinary'
        if task is None:
            task=s.get(CommerceTaskRow,p['id']);kind='commerce_publish';capability='commerce'
        if task is None or center is None:raise KeyError(p['id'])
        old=task.snapshot_json;snapshot=json.loads(old)
        if snapshot.get('kind')!=kind or snapshot.get('execution_enabled') is not False or task.state!='missed_waiting_user' or center.state!='missed_waiting_user':raise ValueError('task is not locally missed')
        row=s.scalar(select(TaskItemRow).where(TaskItemRow.task_id==task.id,TaskItemRow.item_key==p['item_key']))
        if row is None or row.state!='missed_waiting_user':raise ValueError('item is not missed')
        item=json.loads(row.detail_json)
        video=next(v for v in snapshot['videos'] if v['id']==item['video_id'])
        account=AccountService(connection).get(item['account_id'])
        identity=next(a for a in snapshot['accounts'] if a['id']==account.id)
        if account.login_state!='valid' or not account.session_ref or (capability!='ordinary' and getattr(account.capabilities,capability)!='available'):raise ValueError('account needs verification')
        if account.platform_user_id!=identity['platform_user_id'] or hashlib.sha256(account.session_ref.encode()).hexdigest()!=identity['session_fingerprint']:raise ValueError('account identity changed')
        for path,digest in [(video['path'],video['content_hash'])]+([(video['cover']['path'],video['cover']['sha256'])] if 'path' in video['cover'] else [])+([(item['cover']['path'],item['cover']['sha256'])] if 'path' in item.get('cover',{}) else []):
            if not Path(path).is_file() or sha256_file(Path(path))!=digest:raise ValueError('frozen resource changed')
        histories=list(s.scalars(select(PublishHistoryRow).where(PublishHistoryRow.video_hash==video['content_hash'],PublishHistoryRow.account_id==account.id)))
        owned=[h for h in histories if h.task_id==task.id]
        if not owned or any(h.state!='queued' for h in owned) or any(h.task_id!=task.id and h.state not in {'published','cancelled','platform_rejected'} for h in histories):raise ValueError('unresolved publication history')
        revision=snapshot.get('revision',0)
        audit_key='publish-revision:'+task.id+':'+str(revision)
        if s.get(Setting,audit_key) is not None:raise ValueError('revision conflict')
        s.add(Setting(key=audit_key,value_json=old))
        item.update(time=when.isoformat(),execution_mode=p['mode'],software_consent=p['mode']=='software_schedule',required_choice=[],blockers=[])
        snapshot['items']=[item if x['account_id']+':'+x['video_id']==p['item_key'] else x for x in snapshot['items']]
        snapshot['revision']=revision+1
        # Original params remain the original confirmed draft; explicit revisions are recorded separately.
        snapshot.setdefault('reschedules',[]).append(dict(item_key=p['item_key'],time=when.isoformat(),mode=p['mode'],confirmed_at=now.isoformat()))
        task.snapshot_json=center.snapshot_json=FrozenSnapshot.from_mapping(snapshot).json
        row.detail_json=json.dumps(item,ensure_ascii=False);row.state='paused';s.flush()
        remaining=s.scalar(select(TaskItemRow.id).where(TaskItemRow.task_id==task.id,TaskItemRow.state=='missed_waiting_user').limit(1))
        task.state=center.state='missed_waiting_user' if remaining else 'paused'
        return dict(id=task.id,state=task.state,revision=revision+1,submitted=False)
