"""Each real adapter advances once; all browser/network boundaries are synthetic."""
from types import SimpleNamespace
import pytest
from test_bilibili_multi_publish import case as bilibili_case
from test_kuaishou_multi_publish import case as kuaishou_case
from test_channels_multi_publish import case as channels_case


@pytest.mark.parametrize('platform',['bilibili','kuaishou','channels'])
def test_changed_account_after_final_cannot_supply_a_notice_for_original_target(request,monkeypatch,platform):
    from yijianjifa.features.accounts.service import AccountIdentityMismatch
    case=request.getfixturevalue(platform+'_case');adapter,payload,page,events=case[:4];reads=[]
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    def changed(*args):raise AccountIdentityMismatch('synthetic mismatch')
    def final(s,surface):
        events.append(('final',None));monkeypatch.setattr(adapter,'_identity',changed)
        s.feedback_surface=SimpleNamespace(evaluate=lambda script:reads.append(script) or {'state':'rejected','message':'投稿失败：内容不符合要求'})
    monkeypatch.setattr(adapter,'_click_final',final)
    try:
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state=='result_unknown' and result.error_code=='account_mismatch'
        assert not reads
    finally:adapter.close()


@pytest.mark.parametrize('platform',['bilibili','kuaishou','channels'])
@pytest.mark.parametrize('notice,state',[
    ({'state':'accepted','message':'定时发布成功'},'platform_scheduled'),
    ({'state':'rejected','message':'投稿失败：今天发布次数已达到上限'},'platform_rejected'),
    ({'state':'unknown','message':'发布失败：网络异常'},'result_unknown'),
])
def test_final_notice_completes_without_waiting_for_list(request,monkeypatch,platform,notice,state):
    case=request.getfixturevalue(platform+'_case')
    adapter,payload,page,events=case[:4]
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    def final(s,surface):
        events.append(('final',None))
        s.feedback_surface=SimpleNamespace(evaluate=lambda script:dict(notice))
    monkeypatch.setattr(adapter,'_click_final',final)
    try:
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state==state
        assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
        if state=='platform_scheduled':
            assert result.scheduled_at==payload['scheduled_at']
            assert result.submission_notice['platform']==platform
        else:assert result.platform_message==notice['message']
        with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=lambda:None)
    finally:adapter.close()


@pytest.mark.parametrize('platform',['bilibili','kuaishou','channels'])
def test_network_notice_does_not_override_later_success(request,monkeypatch,platform):
    case=request.getfixturevalue(platform+'_case');adapter,payload,page,events=case[:4];reads=[]
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    def observe(script):
        reads.append(script)
        return {'state':'unknown','message':'发布失败：网络异常'} if len(reads)==1 else {'state':'accepted','message':'定时发布成功'}
    def final(s,surface):
        events.append(('final',None));s.feedback_surface=SimpleNamespace(evaluate=observe)
    monkeypatch.setattr(adapter,'_click_final',final)
    try:
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state=='platform_scheduled' and len(reads)>=2
        assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    finally:adapter.close()
