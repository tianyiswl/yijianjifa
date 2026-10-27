from datetime import date, timedelta
from pathlib import Path
import pytest
from yijianjifa.app_services.multi_publish import MultiPublishService
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation, PublishCoordinator
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog


@pytest.fixture
def batch(db_engine,app_paths,tmp_path):
    accounts=[AccountService(db_engine).create('douyin',uid,'测试'+uid) for uid in ('111','222')]
    videos=[];calls=[]
    for index in range(3):
        path=tmp_path/f'{index}.mp4';path.write_bytes(f'owned-video-{index}'.encode())
        videos.append(FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title=f'视频{index}'))
    class Adapter:
        platform_id='douyin';verified_capabilities={'ordinary','publish_now','result_query'}
        limited=True
        def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
        def reconcile(self,payload,*,context):return AdapterOutcome('result_unknown')
        def submit(self,payload,*,context,before_final):
            before_final();uid=payload['platform_user_id'];calls.append(uid)
            if uid=='111' and self.limited:
                self.limited=False
                return AdapterOutcome('platform_rejected',error_code='platform_rejected',platform_message='今天投稿次数已达到上限，请明天再试')
            return AdapterOutcome('platform_accepted',receipt_reference={'work_id':payload['video_id']})
    adapter=Adapter();service=MultiPublishService(db_engine,app_paths,catalog=PlatformCatalog(transports={'douyin':adapter}))
    def prepare(identity,selected_videos,selected_accounts):
        p=service.prepare(request_id=identity,video_ids=[v.id for v in selected_videos],
            targets=[{'platform':'douyin','account_id':a.id} for a in selected_accounts],common_fields={},execution_mode='publish_now',
            distribution_by_platform={'douyin':'whole_batch'})
        return service.save(p['prepared_id'],request_id=identity)['id']
    task=prepare('batch',videos[:2],accounts)
    return service,PublishCoordinator(service),task,adapter,calls,prepare,videos,accounts


def test_multi_batch_skips_same_account_but_other_account_continues(batch):
    service,coordinator,task,adapter,calls,prepare,videos,accounts=batch
    for _ in range(4):coordinator.run_once(task)
    assert calls.count('111')==1 and calls.count('222')==2
    rows=service.get_task(task)['deliveries']
    assert sum(r['state']=='platform_accepted' for r in rows)==2
    assert sum(r.get('error_code')=='posting_limit_skipped' and not r['final_started'] for r in rows)==1
    assert coordinator.run_once(task) is None
    other=prepare('independent',videos[2:],accounts[:1])
    assert coordinator.run_once(other)=='platform_accepted'
    assert calls.count('111')==2


def test_daily_limit_expires_after_the_shanghai_date_changes(batch,monkeypatch):
    from yijianjifa.features.publish import posting_limits
    service,coordinator,task,adapter,calls,_,_,accounts=batch
    rows=service.get_task(task)['deliveries'];limited=[r for r in rows if r['account_id']==accounts[0].id]
    assert coordinator.run_once(task,[limited[0]['id']])=='platform_rejected'
    tomorrow=(date.fromisoformat(posting_limits.today())+timedelta(days=1)).isoformat()
    monkeypatch.setattr(posting_limits,'today',lambda:tomorrow)
    assert coordinator.run_once(task,[limited[1]['id']])=='platform_accepted'
    assert calls==['111','111']


@pytest.mark.parametrize('message,expected',[
    ('今天投稿次数已达到上限，请明天再试',True),('今日发布数量已用完',True),
    ('今天投稿次数还剩3次',False),('今天投稿未达到上限',False),
    ('今日发布上限为10条',False),('网络异常，请稍后再试',False),
])
def test_only_explicit_exhausted_daily_limit_skips_account(message,expected):
    from yijianjifa.features.publish.posting_limits import explicit_daily_limit
    assert explicit_daily_limit(message) is expected


def test_old_generated_skip_reason_is_presented_without_claiming_other_accounts():
    from yijianjifa.features.publish.platform_feedback import failure_message
    reason=failure_message({'error_code':'posting_limit_skipped',
        'platform_message':'本账号今日投稿次数已达上限，本批已跳过；其他账号继续发布。'},'pre_submit_failed')
    assert '本条未提交' in reason and '已跳过' in reason
    assert '其他账号' not in reason
