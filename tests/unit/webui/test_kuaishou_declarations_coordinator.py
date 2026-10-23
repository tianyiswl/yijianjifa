"""Declaration failures survive the ordinary coordinator and durable boundary.

All identities, media and transports are synthetic; no browser or network IO.
"""
import json

import pytest
from sqlalchemy import select

from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.app_services.multi_publish_dto import detail
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation, PublishCoordinator
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter


@pytest.mark.parametrize('code,expected',[
    ('kuaishou_author_statement_required','kuaishou_author_statement_required'),
    ('kuaishou_declaration_options_unverified','kuaishou_declaration_options_unverified'),
    ('UNTRUSTED_RAW_FAILURE_SECRET','adapter_failed'),
])
@pytest.mark.parametrize('crossed_final',[False,True])
def test_coordinator_records_bounded_declaration_failure_and_preserves_final_boundary(
        db_engine,app_paths,tmp_path,code,expected,crossed_final):
    class SyntheticAdapter:
        platform_id='kuaishou'
        verified_capabilities={'ordinary','publish_now','result_query'}
        public_field_schema=KuaishouMultiPublishAdapter.public_field_schema
        calls=[]
        def validate_target(self,target,payload,execution):
            return AdapterValidation(payload['platform_user_id'],True)
        def validate_fields(self,payload):return True
        def reconcile(self,*args,**kwargs):raise AssertionError('no new result query expected')
        def submit(self,payload,*,context,before_final):
            self.calls.append('submit')
            if crossed_final:before_final();self.calls.append('final')
            return AdapterOutcome('pre_submit_failed',error_code=code)
    account=AccountService(db_engine).create('kuaishou','synthetic-kuaishou','合成快手')
    path=tmp_path/'owned.mp4';path.write_bytes(b'synthetic owned media')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='离线合成测试')
    adapter=SyntheticAdapter()
    service=MultiPublishService(db_engine,app_paths,catalog=PlatformCatalog(transports={'kuaishou':adapter}))
    prepared=service.prepare(request_id='declaration-boundary',video_ids=[video.id],
        targets=[{'account_id':account.id,'platform':'kuaishou','fields':{'content_declaration_choice':'none'}}],
        common_fields={'title':'离线合成测试'},execution_mode='publish_now')
    receipt=service.save(prepared['prepared_id'],request_id='declaration-boundary')
    coordinator=PublishCoordinator(service)
    result=coordinator.run_once(receipt['id'])
    assert result==('result_unknown' if crossed_final else 'pre_submit_failed')
    with session_scope(db_engine) as db:
        attempt=db.scalar(select(DeliveryAttemptRow))
        evidence=json.loads(attempt.result_json)
        assert evidence['state']==result and evidence['error_code']==expected
        assert (attempt.final_json is not None) is crossed_final
        assert 'UNTRUSTED_RAW_FAILURE_SECRET' not in attempt.result_json
    public=detail(service,receipt['id'],coordinator=coordinator)['deliveries'][0]
    assert public['state']==result and public['final_started'] is crossed_final
    assert public['error_code']==expected
    if expected.startswith('kuaishou_'):assert '快手' in public['reason_message']
    if crossed_final:assert 'reprepare' not in public['allowed_actions'] and 'start' not in public['allowed_actions']
    assert public['effective_fields']['content_declaration_choice']=='none'
    assert coordinator.run_once(receipt['id']) is None
    assert adapter.calls==(['submit','final'] if crossed_final else ['submit'])
