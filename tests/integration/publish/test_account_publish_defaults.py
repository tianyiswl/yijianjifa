"""New intentions inherit account defaults; frozen attempts never do."""
import json

from sqlalchemy import select

from test_multi_publish_v2 import setup_v2
from yijianjifa.app_services.account_publish_settings import AccountPublishSettings
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.publish.deliveries import PublishDeliveryRow
from yijianjifa.storage.db import session_scope


def test_same_platform_accounts_freeze_distinct_defaults_and_keep_content_text(setup_v2,db_engine):
    service,video,accounts,_=setup_v2
    first=next(a for a in accounts if a.platform=='bilibili')
    second=AccountService(db_engine).create('bilibili','other','B')
    settings=AccountPublishSettings(db_engine)
    settings.save(first.id,{'category_id':10,'copyright':'original'})
    settings.save(second.id,{'category_id':20,'copyright':'repost','source':'synthetic source'})
    prepared=service.prepare(request_id='owned-defaults',video_ids=[video.id],
        targets=[{'account_id':a.id,'platform':'bilibili'} for a in (first,second)],
        common_fields={'body':'本条正文'},execution_mode='publish_now',distribution_by_platform={'bilibili':'whole_batch'})
    assert all(target['state']=='waiting' for target in prepared['targets'])
    receipt=service.save(prepared['prepared_id'],request_id='owned-defaults')
    with session_scope(db_engine) as db:
        rows=list(db.scalars(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==receipt['id'])))
        frozen={row.account_id:row.snapshot_json for row in rows}
    assert json.loads(frozen[first.id])['fields']['category_id']==10
    assert json.loads(frozen[second.id])['fields']['category_id']==20
    assert all(json.loads(raw)['fields']['body']=='本条正文' for raw in frozen.values())
    settings.save(first.id,{'category_id':30,'copyright':'original'})
    with session_scope(db_engine) as db:
        assert {row.account_id:row.snapshot_json for row in db.scalars(select(PublishDeliveryRow))}==frozen


def test_explicit_target_overrides_default_as_a_selector_group(setup_v2):
    service,video,accounts,_=setup_v2
    account=next(a for a in accounts if a.platform=='bilibili')
    AccountPublishSettings(service.engine).save(account.id,{'category_choice':'human:knowledge','copyright':'original'})
    prepared=service.prepare(request_id='specific-selector',video_ids=[video.id],
        targets=[{'account_id':account.id,'platform':'bilibili','fields':{'category_id':25,'category_name':'本次分区'}}],
        common_fields={},execution_mode='publish_now')
    payload=service._drafts[prepared['prepared_id']]['snapshot']['items'][0]['payload']
    assert payload['fields']['category_id']==25 and 'category_choice' not in payload['fields']


def test_frozen_handoff_can_explicitly_skip_all_current_account_defaults(setup_v2):
    service,video,accounts,_=setup_v2
    account=next(a for a in accounts if a.platform=='bilibili')
    AccountPublishSettings(service.engine).save(account.id,{'category_id':99,'copyright':'original'})
    prepared=service.prepare(request_id='original-handoff',video_ids=[video.id],
        targets=[{'account_id':account.id,'platform':'bilibili','fields':{'category_id':12,'copyright':'original'}}],
        common_fields={},execution_mode='publish_now',inherit_account_settings=False)
    payload=service._drafts[prepared['prepared_id']]['snapshot']['items'][0]['payload']
    assert payload['fields']['category_id']==12 and payload['base_fields'].get('category_id') is None
