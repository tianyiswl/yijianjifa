"""Safety at the final boundary using a synthetic, thread-local creator page."""
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
from types import SimpleNamespace
import pytest
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope


def module():
    assert importlib.util.find_spec('yijianjifa.webui.kuaishou_multi_publish'), '快手发布通道尚未实现'
    from yijianjifa.webui import kuaishou_multi_publish
    return kuaishou_multi_publish


@pytest.fixture
def case(db_engine,app_paths,tmp_path,monkeypatch):
    m=module();a=AccountService(db_engine).create('kuaishou','111','合成快手')
    with session_scope(db_engine) as db:
        r=db.get(AccountRow,a.id);r.session_ref='version-1';r.login_state='valid'
    path=tmp_path/'test.mp4';path.write_bytes(b'owned-test')
    p={'platform':'kuaishou','platform_user_id':'111','account_id':a.id,'video_path':str(path),'video_hash':sha256_file(path),
       'session_fingerprint':hashlib.sha256(b'version-1').hexdigest(),'video_id':'fixture',
       'fields':{'title':'测试标题','body':'测试说明','tags':'#功能测试','cover':{'kind':'first_frame','frame_ms':0}},
       'execution_mode':'platform_schedule','scheduled_at':(datetime.now(timezone.utc)+timedelta(days=1)).replace(second=0,microsecond=0).isoformat()}
    events=[];identity={'origin':'https://cp.kuaishou.com','logged_in':True,'uid':'111','profile_uid':'111','nickname':'合成快手'}
    # Include SQLite execution-log writes in the synthetic lifecycle budget on Windows.
    adapter=m.KuaishouMultiPublishAdapter(db_engine,app_paths,step_timeout=1)
    adapter.bind_delivery(digest=digest(p),delivery_id='target',task_id='batch')
    class Page:
        url=m.form_contract.MANAGE_URL
        def goto(self,url,**kwargs):self.url=url;events.append(('navigate',url))
        def wait_for_timeout(self,n):pass
        def is_closed(self):return False
        def screenshot(self,**kwargs):return b'frame'
        def evaluate(self,script):return identity
    page=Page()
    def open_session(s,**kwargs):
        if s.page:return
        s.page=page;s.context=SimpleNamespace(close=lambda:events.append(('close',None)));s.original_ref='version-1'
        adapter._init_observations(s);s.list_seen=True;s.list_complete=True;s.list_rows=[]
    monkeypatch.setattr(adapter,'_open',open_session)
    monkeypatch.setattr(adapter,'_challenge',lambda s:None)
    monkeypatch.setattr(adapter,'_frame',lambda s:page)
    monkeypatch.setattr(adapter,'_capture_session_blob',lambda *a:b'archive')
    monkeypatch.setattr(adapter,'_persist_sessions',lambda:None)
    def upload(s,frame):events.append(('upload',None));s.uploaded=True
    monkeypatch.setattr(adapter,'_upload',upload)
    def prepare(s,frame,f):s.form_filled=True;return True
    monkeypatch.setattr(adapter,'_prepare_form',prepare)
    def click(s,frame):
        events.append(('final',None));s.submit_ok=True;s.receipt={'work_id':'42'}
    monkeypatch.setattr(adapter,'_click_final',click)
    return adapter,p,page,events,identity


def test_journal_before_one_final_and_missing_readback_never_claims_published(case):
    a,p,_,events,_=case
    assert a.validate_target(p,p,p['execution_mode']).authorized
    result=a.submit(p,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_accepted' and result.receipt_reference=={'work_id':'42'}
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    with pytest.raises(ValueError):a.submit(p,context=None,before_final=lambda:None)
    a.close()


def test_wrong_live_account_prevents_upload_and_final(case):
    a,p,_,events,identity=case;identity.update(uid='222',profile_uid='222')
    assert a.validate_target(p,p,p['execution_mode']).authorized is False
    assert not any(e[0] in {'upload','final'} for e in events)
    a.close()


def test_navigation_hydration_is_not_an_account_mismatch(case,monkeypatch):
    a,p,page,events,_=case
    assert a.validate_target(p,p,p['execution_mode']).authorized
    original=a._identity;missing=[True]
    def identity(s):
        if page.url==module().form_contract.UPLOAD_URL and missing[0]:
            missing[0]=False
            raise ValueError('identity unavailable')
        return original(s)
    monkeypatch.setattr(a,'_identity',identity)
    result=a.submit(p,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_accepted'
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    a.close()


def test_different_account_after_navigation_still_stops_before_upload(case,monkeypatch):
    a,p,page,events,_=case
    assert a.validate_target(p,p,p['execution_mode']).authorized
    original=a._identity
    def identity(s):
        if page.url==module().form_contract.UPLOAD_URL:raise AccountIdentityMismatch('account changed')
        return original(s)
    from yijianjifa.features.accounts.service import AccountIdentityMismatch
    monkeypatch.setattr(a,'_identity',identity)
    result=a.submit(p,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='pre_submit_failed' and result.error_code=='account_mismatch'
    assert not any(e[0] in {'upload','journal','final'} for e in events)
    a.close()


def test_no_work_id_means_unknown_and_query_never_reuploads(case,monkeypatch):
    a,p,_,events,_=case
    def click(s,frame):events.append(('final',None));s.submit_ok=True;s.receipt={}
    monkeypatch.setattr(a,'_click_final',click)
    a.validate_target(p,p,p['execution_mode'])
    first=a.submit(p,context=None,before_final=lambda:None)
    assert first.state=='result_unknown'
    later=a.reconcile(p,context=first.context)
    assert later.state=='result_unknown'
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    a.close()


def test_same_caption_is_not_evidence_of_duplicate_video(case):
    a,p,_,events,_=case;a.validate_target(p,p,p['execution_mode'])
    s=a._session(p);s.list_rows=[{'title':module().form_contract.validate_form(p)['caption'],'workId':'41'}]
    result=a.submit(p,context=None,before_final=lambda:None)
    assert result.state=='platform_accepted'
    assert [e[0] for e in events if e[0] in {'upload','final'}]==['upload','final']
    a.close()


def test_pre_final_challenge_parks_without_final(case,monkeypatch):
    a,p,_,events,_=case;a.validate_target(p,p,p['execution_mode'])
    monkeypatch.setattr(a,'_challenge',lambda s:'complex')
    result=a.submit(p,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='paused_auth' and a.interventions()[0]['platform']=='kuaishou'
    assert not any(e[0] in {'upload','journal','final'} for e in events)
    a.close()


def test_failed_step_records_bounded_scope_without_exception_text(case,monkeypatch):
    from yijianjifa.storage.models import Setting
    a,p,_,events,_=case
    assert a.validate_target(p,p,p['execution_mode']).authorized
    def fail(s,frame,f):raise RuntimeError('SECRET_TOKEN https://private.invalid')
    monkeypatch.setattr(a,'_prepare_form',fail)
    with pytest.raises(RuntimeError):a.submit(p,context=None,before_final=lambda:None)
    with session_scope(a.engine) as db:
        value=db.get(Setting,'publish.kuaishou.diagnostic:target:1').value_json
    facts=json.loads(value)
    assert facts['reason']=='unknown' and facts['final_attempted'] is False
    assert facts['phase']=='prepare' and facts['upload_started'] is True
    assert 'SECRET_TOKEN' not in value and 'private.invalid' not in value
    assert p['fields']['title'] not in value and p['fields']['tags'] not in value
    assert not any(e[0]=='final' for e in events)
    a.close()


def test_receipt_listener_only_trusts_own_final_request(case):
    a,p,_,_,_=case;s=a._session(p);a._init_observations(s);s.upload_id='99'
    f=module().form_contract.validate_form(p)
    def response(url,body,data):
        return SimpleNamespace(url=url,request=SimpleNamespace(method='POST',post_data_json=body),json=lambda:data)
    body={'fileId':'99','caption':f['caption'],'publishTime':f['publish_ms']}
    url='https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit'
    a._response(s,response(url,body,{'result':1,'data':{'workId':'42'}}))
    assert not s.submit_ok
    s.final=True
    a._response(s,response(url,{**body,'fileId':'100'},{'result':1,'data':{'workId':'43'}}))
    assert not s.submit_ok
    a._response(s,response(url,body,{'result':1,'data':{'fileId':'99'}}))
    assert s.submit_ok and s.receipt=={}
    a._response(s,response(url,body,{'result':1,'data':{'workId':'42'}}))
    assert s.receipt=={'work_id':'42'}
    a.close()


def test_successful_finish_supplements_unmounted_upload_status_but_pre_does_not(case,monkeypatch):
    a,p,page,_,_=case;s=a._session(p);a._init_observations(s);s.uploaded=True
    monkeypatch.setattr(page,'evaluate',lambda script:{'upload_complete':False,'editor_count':1})
    def response(stage,file_id):
        return SimpleNamespace(url='https://cp.kuaishou.com/rest/cp/works/v2/video/pc/upload/'+stage,
                               json=lambda:{'result':1,'data':{'fileId':file_id}})
    a._response(s,response('pre','99'))
    assert not a._observed_form(s,page)['upload_complete']
    a._response(s,response('finish','100'))
    assert not a._observed_form(s,page)['upload_complete']
    a._response(s,response('finish','99'))
    assert a._observed_form(s,page)['upload_complete']
    a.close()


def submit_response(payload,**changes):
    form=module().form_contract.validate_form(payload,query=True)
    request={'fileId':'99','caption':form['caption'],'publishTime':form['publish_ms']}
    request.update(changes)
    return SimpleNamespace(url='https://cp.kuaishou.com/rest/cp/works/v3/video/pc/submit',
        request=SimpleNamespace(method='POST',post_data_json=request),json=lambda:{'result':1})


def persist_schedule_result(adapter,payload,state='platform_scheduled'):
    """Synthetic immutable attempt with the same journal shape as the boundary."""
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow,canonical
    snapshot=canonical(payload);key=digest(payload)
    with session_scope(adapter.engine) as db:
        db.add(PublishDeliveryRow(id='target',task_id='batch',item_id='item',history_id='history',
            platform='kuaishou',account_id=payload['account_id'],platform_user_id=payload['platform_user_id'],
            video_id=payload['video_id'],video_hash=payload['video_hash'],snapshot_json=snapshot,request_digest=key,
            execution_mode='platform_schedule',scheduled_at=payload['scheduled_at'],attempt=1,position=0))
        db.add(DeliveryAttemptRow(id='attempt',delivery_id='target',number=1,snapshot_json=snapshot,request_digest=key,
            final_json=canonical({'request_digest':key,'state':'result_unknown','at':'2026-10-07T00:00:00+00:00'}),
            result_json=canonical({'request_digest':key,'state':state,'scheduled_at':payload['scheduled_at'],
                                   'receipt_reference':{},'error_code':None})))


def test_bound_scheduled_ack_without_id_is_accepted_once_and_query_retains_receipt(case,monkeypatch):
    adapter,payload,_,events,_=case
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload))
    monkeypatch.setattr(adapter,'_click_final',click)
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    first=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert first.state=='platform_scheduled' and first.scheduled_at==payload['scheduled_at']
    assert first.receipt_reference=={}  # An upload file ID is never a work ID.
    persist_schedule_result(adapter,payload)
    later=adapter.reconcile(payload,context=None)
    assert later.state=='platform_scheduled' and later.scheduled_at==payload['scheduled_at']
    assert not later.receipt_reference
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    assert adapter._session(payload).stage=='快手已接收定时安排，作品编号待核对'
    with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=lambda:None)
    adapter.close()


@pytest.mark.parametrize('failure',['identity','capture'])
def test_bound_scheduled_ack_survives_session_refresh_failure_without_repeat_final(case,monkeypatch,failure):
    from yijianjifa.storage.models import Setting
    adapter,payload,_,events,_=case
    original_identity=adapter._identity
    if failure=='identity':
        def identity(session):
            if session.stage=='正在核对快手提交结果':
                raise ValueError('PRIVATE_IDENTITY https://private.invalid')
            return original_identity(session)
        monkeypatch.setattr(adapter,'_identity',identity)
    else:
        def capture(*args):raise RuntimeError('PRIVATE_SESSION https://private.invalid')
        monkeypatch.setattr(adapter,'_capture_session_blob',capture)
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload))
    monkeypatch.setattr(adapter,'_click_final',click)
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_scheduled' and result.scheduled_at==payload['scheduled_at']
    assert result.receipt_reference=={} and not adapter.refreshed
    with session_scope(adapter.engine) as db:
        diagnostic=db.get(Setting,'publish.kuaishou.diagnostic:target:1').value_json
    facts=json.loads(diagnostic)
    assert facts['final_attempted'] and facts['submit_ok'] and not facts['has_work_reference']
    assert 'PRIVATE_' not in diagnostic and 'private.invalid' not in diagnostic
    assert payload['fields']['title'] not in diagnostic
    persist_schedule_result(adapter,payload)
    later=adapter.reconcile(payload,context=None)
    assert later.state=='platform_scheduled' and later.scheduled_at==payload['scheduled_at']
    with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=lambda:None)
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    adapter.close()


def post_final_guard(adapter,monkeypatch,guard):
    if guard=='login':monkeypatch.setattr(adapter,'_challenge',lambda session:'login' if session.final else None)
    elif guard in {'hydration','account_change'}:
        original=adapter._identity
        def identity(session):
            if session.final:
                from yijianjifa.features.accounts.service import AccountIdentityMismatch
                raise (AccountIdentityMismatch('account changed') if guard=='account_change' else ValueError('identity unavailable'))
            return original(session)
        monkeypatch.setattr(adapter,'_identity',identity)


@pytest.mark.parametrize('guard',['login','hydration','account_change','version'])
def test_exact_scheduled_ack_precedes_post_final_guards_without_repeating_final(case,monkeypatch,guard):
    adapter,payload,_,events,_=case
    post_final_guard(adapter,monkeypatch,guard)
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload))
        if guard=='version':
            with session_scope(adapter.engine) as db:db.get(AccountRow,payload['account_id']).session_ref='version-2'
    monkeypatch.setattr(adapter,'_click_final',click)
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_scheduled' and result.scheduled_at==payload['scheduled_at']
    assert result.receipt_reference=={} and not adapter.interventions()
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=lambda:None)
    adapter.close()


def test_exact_scheduled_ack_remains_confirmed_when_final_callback_crosses_deadline(case,monkeypatch):
    import time
    adapter,payload,_,events,_=case
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload))
        time.sleep(1.05)  # The real receipt arrives before the local callback ends.
    monkeypatch.setattr(adapter,'_click_final',click)
    try:
        assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state=='platform_scheduled',result
        assert result.scheduled_at==payload['scheduled_at']
        assert [e[0] for e in events if e[0] in {'journal','final'}]==['journal','final']
    finally:adapter.close()


@pytest.mark.parametrize('guard',['login','hydration','account_change','version'])
def test_post_final_guards_do_not_upgrade_an_old_unknown_attempt(case,monkeypatch,guard):
    adapter,payload,_,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    session=adapter._session(payload);session.final=True;session.phase='result';session.submit_ok=True
    persist_schedule_result(adapter,payload,state='result_unknown')
    post_final_guard(adapter,monkeypatch,guard)
    if guard=='version':
        with session_scope(adapter.engine) as db:db.get(AccountRow,payload['account_id']).session_ref='version-2'
    result=adapter.reconcile(payload,context=None)
    assert result.state=='result_unknown' and not result.receipt_reference
    assert not any(e[0] in {'upload','final'} for e in events)
    adapter.close()


@pytest.mark.parametrize('verified',[False,True])
def test_existing_accepted_or_verified_receipt_survives_session_capture_failure(case,monkeypatch,verified):
    adapter,payload,_,events,_=case
    def capture(*args):raise RuntimeError('PRIVATE_SESSION')
    monkeypatch.setattr(adapter,'_capture_session_blob',capture)
    if verified:
        def click(session,frame):
            events.append(('final',None));session.receipt={'work_id':'42'}
            session.list_rows=[{'workId':'42','title':module().form_contract.validate_form(payload)['caption'],
                                'publishType':2,'publishStatus':4}]
        monkeypatch.setattr(adapter,'_click_final',click)
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state==('published' if verified else 'platform_accepted')
    assert result.receipt_reference=={'work_id':'42'}
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    adapter.close()


@pytest.mark.parametrize('bad',['file','caption','time','float_time','method','origin','result','before_final'])
def test_scheduled_ack_requires_the_exact_own_normal_final_post(case,bad):
    adapter,payload,_,_,_=case;session=adapter._session(payload);adapter._init_observations(session)
    session.final=True;session.upload_id='99';response=submit_response(payload)
    if bad=='file':response.request.post_data_json['fileId']='100'
    if bad=='caption':response.request.post_data_json['caption']='another video'
    if bad=='time':response.request.post_data_json['publishTime']+=60000
    if bad=='float_time':response.request.post_data_json['publishTime']=float(response.request.post_data_json['publishTime'])
    if bad=='method':response.request.method='GET'
    if bad=='origin':response.url=response.url.replace('cp.kuaishou.com','another.invalid')
    if bad=='result':response.json=lambda:{'result':True}
    if bad=='before_final':session.final=False
    adapter._response(session,response)
    assert not session.schedule_accepted and not session.submit_ok and session.receipt=={}
    adapter.close()


def test_bound_immediate_ack_without_work_id_remains_unknown(case,monkeypatch):
    adapter,payload,_,events,_=case
    payload.update(execution_mode='publish_now',scheduled_at=None)
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload))
    monkeypatch.setattr(adapter,'_click_final',click)
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='result_unknown' and not result.receipt_reference
    session=adapter._session(payload)
    assert session.submit_ok and not session.schedule_accepted
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    adapter.close()


def test_reopened_query_retains_only_durable_schedule_ack_without_reupload(case,monkeypatch):
    adapter,payload,page,events,_=case
    persist_schedule_result(adapter,payload);adapter.close()
    reopened=module().KuaishouMultiPublishAdapter(adapter.engine,adapter.paths,step_timeout=1)
    def open_session(session,**kwargs):
        if session.page:return
        session.page=page;session.context=SimpleNamespace(close=lambda:events.append(('close',None)))
        reopened._init_observations(session)
    monkeypatch.setattr(reopened,'_open',open_session)
    monkeypatch.setattr(reopened,'_challenge',lambda session:None)
    monkeypatch.setattr(reopened,'_remember_session',lambda session:None)
    result=reopened.reconcile({**payload,'receipt_reference':{}},context=None)
    assert result.state=='platform_scheduled' and result.scheduled_at==payload['scheduled_at']
    assert not result.receipt_reference and reopened._session(payload).final is True
    assert not any(e[0] in {'upload','final'} for e in events)
    reopened.close()


def test_query_login_challenge_does_not_erase_original_durable_schedule_ack(case,monkeypatch):
    adapter,payload,_,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    session=adapter._session(payload);session.final=True
    persist_schedule_result(adapter,payload)
    monkeypatch.setattr(adapter,'_challenge',lambda session:'login')
    result=adapter.reconcile(payload,context=None)
    assert result.state=='platform_scheduled' and result.scheduled_at==payload['scheduled_at']
    assert not result.receipt_reference and not adapter.interventions()
    assert not any(e[0] in {'upload','final'} for e in events)
    adapter.close()


@pytest.mark.parametrize('bad',['unknown','final_missing','final_digest','result_digest','result_time','attempt_digest',
                               'current_attempt','binding_digest','account','platform','snapshot','row_time'])
def test_query_does_not_promote_unknown_or_mismatched_durable_ack(case,bad):
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow,canonical
    adapter,payload,_,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    session=adapter._session(payload);session.final=True;session.phase='result'
    persist_schedule_result(adapter,payload)
    with session_scope(adapter.engine) as db:
        row=db.get(PublishDeliveryRow,'target');attempt=db.get(DeliveryAttemptRow,'attempt')
        result=json.loads(attempt.result_json);final=json.loads(attempt.final_json)
        if bad=='unknown':result['state']='result_unknown'
        if bad=='final_digest':final['request_digest']='changed'
        if bad=='result_digest':result['request_digest']='changed'
        if bad=='result_time':result['scheduled_at']='2026-10-08T01:00:00+00:00'
        if bad=='attempt_digest':attempt.request_digest='changed'
        if bad=='current_attempt':row.attempt=2
        if bad=='binding_digest':row.request_digest='changed'
        if bad=='account':row.platform_user_id='222'
        if bad=='platform':row.platform='douyin'
        if bad=='snapshot':row.snapshot_json=canonical({**payload,'video_id':'another-video'})
        if bad=='row_time':row.scheduled_at='2026-10-08T01:00:00+00:00'
        attempt.result_json=canonical(result)
        attempt.final_json=None if bad=='final_missing' else canonical(final)
    # Neither a diagnostic success flag nor an existing unique caption is an ACK.
    session.submit_ok=True
    session.list_rows=[{'workId':'42','title':module().form_contract.validate_form(payload)['caption']}]
    result=adapter.reconcile(payload,context=None)
    assert result.state=='result_unknown' and not result.receipt_reference
    assert not any(e[0] in {'upload','final'} for e in events)
    adapter.close()


@pytest.mark.parametrize('cursor,complete',[('no_more',True),('next-page',False),('',False),(None,False)])
def test_list_end_requires_official_no_more_sentinel(case,cursor,complete):
    adapter,payload,_,_,_=case;session=adapter._session(payload);adapter._init_observations(session)
    response=SimpleNamespace(url='https://cp.kuaishou.com/rest/cp/works/v2/video/pc/photo/list',
        json=lambda:{'result':1,'data':{'list':[],'nextCursor':cursor}})
    adapter._response(session,response)
    assert session.list_seen and session.list_complete is complete
    adapter.close()
