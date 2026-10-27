"""B站 boundary behavior: exact account, one final, query-only recovery."""
from datetime import datetime,timedelta,timezone
import hashlib
from types import SimpleNamespace
from copy import deepcopy
import pytest
from yijianjifa.features.accounts.service import AccountService,AccountRow,AccountIdentityMismatch
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.storage.db import session_scope
from yijianjifa.webui import bilibili_multi_publish as m
from test_bilibili_publish_form import raw,request


def use_builtin_intent(case,statement='no_mark'):
    adapter,payload,_,_,_=case
    for key in ('category_id','category_name','category_mode','creation_statement_id','creation_statement_name'):
        payload['fields'].pop(key,None)
    payload['fields'].update(category_choice='human:tech',creation_statement_choice=statement)
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    adapter.validate_target(payload,payload,payload['execution_mode'])
    session=adapter._session(payload)
    session.options['categories']=[{'id':901,'name':'科技数码','mode':'human'}]
    session.options['statements']=[{'id':-1,'name':'真实页面无标记选项'},{'id':67,'name':'该内容使用人工智能合成技术'}]
    return session


def resolved_payload(payload,options):
    result=deepcopy(payload)
    result['fields']=m.catalog.resolve_fields(payload['fields'],options)
    for key in ('category_choice','creation_statement_choice'):result['fields'].pop(key,None)
    return result


@pytest.mark.parametrize('official_ai_label', ['该内容使用人工智能合成技术', '含AI生成内容'])
def test_static_user_intent_resolves_current_ids_before_upload_and_binds_one_final(case,monkeypatch,official_ai_label):
    adapter,payload,_,events,_=case;session=use_builtin_intent(case,'ai_generated');frozen=deepcopy(payload)
    session.options['statements'][1]['name']=official_ai_label
    def click(s,frame):
        actual=resolved_payload(payload,s.options)
        assert s.final_spec['category_id']==901 and s.final_spec['creation_statement_id']==67
        events.append(('final',None));s.final_form=raw(actual)
        # A later unrelated options response cannot alter the submitted shape.
        s.options['categories'][0]['id']=902
        adapter._response(s,response(actual,s.final_form))
    monkeypatch.setattr(adapter,'_click_final',click)
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='platform_scheduled' and payload==frozen
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    assert session.final_spec['category_id']==901 and session.final_spec['creation_statement_id']==67
    adapter.close()


@pytest.mark.parametrize('change',['missing_category','ambiguous_category','wrong_mode','missing_statement','ambiguous_statement'])
def test_unresolved_static_intent_blocks_before_upload_or_journal(case,change):
    adapter,payload,_,events,_=case;s=use_builtin_intent(case,'ai_generated')
    if change=='missing_category':s.options['categories']=[]
    if change=='ambiguous_category':s.options['categories'].append({'id':902,'name':'科技数码','mode':'human'})
    if change=='wrong_mode':s.options['categories'][0]['mode']='legacy'
    if change=='missing_statement':s.options['statements']=[{'id':-1,'name':'不标记'}]
    if change=='ambiguous_statement':s.options['statements'].append({'id':68,'name':'该视频使用人工智能合成技术'})
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='pre_submit_failed' and not any(e[0] in {'upload','journal','final'} for e in events)
    adapter.close()


def test_mapping_change_during_form_fill_cannot_silently_reassign_or_click(case,monkeypatch):
    adapter,payload,_,events,_=case;use_builtin_intent(case)
    def prepare(s,frame,f):s.options['categories'][0]['id']=902;return True
    monkeypatch.setattr(adapter,'_prepare_form',prepare)
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='pre_submit_failed' and result.error_code=='configuration_required'
    assert any(e[0]=='upload' for e in events) and not any(e[0] in {'journal','final'} for e in events)
    adapter.close()


def test_final_guard_rechecks_frozen_mapping_after_journal(case):
    adapter,payload,_,events,_=case;s=use_builtin_intent(case)
    s.resolved_form=m.form_contract.validate_form(payload,options=s.options)
    actual=resolved_payload(payload,s.options)
    class Frame:
        def evaluate(self,script):return raw(actual)
        def locator(self,*args):raise AssertionError('changed mapping must block before final button lookup')
    s.options['categories'][0]['id']=902
    with pytest.raises(ValueError,match='final B站 form changed'):
        m.BilibiliMultiPublishAdapter._click_final(adapter,s,Frame())
    assert not any(e[0]=='final' for e in events)
    adapter.close()


@pytest.fixture
def case(db_engine,app_paths,tmp_path,monkeypatch):
    account=AccountService(db_engine).create('bilibili','111','合成账号')
    with session_scope(db_engine) as db:
        row=db.get(AccountRow,account.id);row.session_ref='version-1';row.login_state='valid'
    video=tmp_path/'synthetic.mp4';video.write_bytes(b'owned-test')
    when=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=4,minute=0,second=0,microsecond=0)
    payload=dict(platform='bilibili',account_id=account.id,platform_user_id='111',video_id='video',video_path=str(video),video_hash=sha256_file(video),
        session_fingerprint=hashlib.sha256(b'version-1').hexdigest(),execution_mode='platform_schedule',scheduled_at=when.isoformat(),
        fields=dict(title='合成测试',body='明确简介',tags='#功能测试 #剪辑测试',category_id=101,category_mode='human',category_name='合成分区',
        creation_statement_id=-1,creation_statement_name='不标记',content_authorization=False))
    identity=dict(origin='https://member.bilibili.com',logged_in=True,uid='111',profile_uid='111',nickname='合成账号')
    # Include SQLite execution-log writes in the synthetic lifecycle budget on Windows.
    events=[];adapter=m.BilibiliMultiPublishAdapter(db_engine,app_paths,step_timeout=1)
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
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
        adapter._init_observations(s)
        s.options=dict(categories=[dict(id=101,name='合成分区',mode='human')],statements=[dict(id=-1,name='不标记')])
    monkeypatch.setattr(adapter,'_open',open_session);monkeypatch.setattr(adapter,'_frame',lambda s:page)
    monkeypatch.setattr(adapter,'_challenge',lambda s:None);monkeypatch.setattr(adapter,'_persist_sessions',lambda:None)
    monkeypatch.setattr(adapter,'_capture_session_blob',lambda *args:b'archive')
    def upload(s,frame):events.append(('upload',None));s.uploaded=True;return True
    monkeypatch.setattr(adapter,'_upload',upload)
    monkeypatch.setattr(adapter,'_prepare_form',lambda s,frame,f:True)
    def click(s,frame):
        events.append(('final',None));s.final_form=raw(payload)
        adapter._response(s,response(payload,s.final_form))
    monkeypatch.setattr(adapter,'_click_final',click)
    return adapter,payload,page,events,identity


def response(payload,r,*,data=None):
    return SimpleNamespace(url='https://member.bilibili.com/x/vu/web/add/v3',
        request=SimpleNamespace(method='POST',post_data_json=request(payload,r)),
        json=lambda:dict(code=0,data=data if data is not None else dict(aid=42,bvid='BV1xx411c7mD')))


@pytest.mark.parametrize('mode', ['background','visible'])
def test_bundled_browser_options_are_set_before_driver_freezes_environment(case,monkeypatch,mode):
    import os
    from playwright import sync_api
    from yijianjifa.storage.models import Setting
    adapter,payload,_,events,_=case
    with session_scope(adapter.engine) as db:
        db.add(Setting(key='publish.browser_mode',value_json=__import__('json').dumps(mode)))
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','fixture-stale-driver-environment')
    def options(selected):
        assert selected==mode
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','fixture-bundled-browser-runtime')
        events.append(('options',selected))
        return dict(headless=selected=='background',channel='chromium')
    page=SimpleNamespace(on=lambda *args:None,goto=lambda *args,**kwargs:events.append(('navigate',None)))
    context=SimpleNamespace(pages=[page],set_default_timeout=lambda value:None,
                            close=lambda:events.append(('close',None)))
    class FrozenDriver:
        def __init__(self):self.chromium=self
        def start(self):
            self.browsers_path=os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
            events.append(('start',None));return self
        def launch_persistent_context(self,*args,**kwargs):
            assert self.browsers_path=='fixture-bundled-browser-runtime', 'driver inherits the environment only at start'
            assert kwargs==dict(headless=mode=='background',channel='chromium',service_workers='block')
            events.append(('launch',None));return context
        def stop(self):events.append(('stop',None))
    monkeypatch.setattr(sync_api,'sync_playwright',lambda:FrozenDriver())
    monkeypatch.setattr(m.browser,'browser_options',options)
    monkeypatch.setattr(m.creator_auth,'chromium_profile',lambda *args:'fixture-isolated-profile')
    monkeypatch.setattr(m.creator_auth,'restore_session',lambda *args:None)
    monkeypatch.setattr(adapter,'_install_publish_guard',lambda *args:None)
    s=adapter._session(payload)
    try:
        # Exercise the actual opener, not this file's ordinary fake _open.
        m.BilibiliMultiPublishAdapter._open(adapter,s)
        assert [e[0] for e in events[:3]]==['options','start','launch']
    finally:adapter.close()


def test_one_final_after_journal_with_exact_receipt(case):
    adapter,payload,_,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    outcome=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert outcome.state=='platform_scheduled' and outcome.scheduled_at==payload['scheduled_at']
    assert outcome.receipt_reference=={'work_id':'42','publish_id':'BV1xx411c7mD'}
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=lambda:None)
    adapter.close()


def test_immediate_receipt_is_accepted_not_public(case):
    adapter,payload,_,_,_=case;payload.update(execution_mode='publish_now',scheduled_at=None)
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    assert adapter.validate_target(payload,payload,'publish_now').authorized
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='platform_accepted' and result.scheduled_at is None
    adapter.close()


def test_identity_changed_after_navigation_blocks_upload_and_final(case,monkeypatch):
    adapter,payload,page,events,_=case;original=adapter._identity
    def identity(s):
        if page.url==m.form_contract.UPLOAD_URL:raise AccountIdentityMismatch('changed')
        return original(s)
    monkeypatch.setattr(adapter,'_identity',identity)
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert result.state=='pre_submit_failed' and result.error_code=='account_mismatch'
    assert not any(e[0] in {'upload','journal','final'} for e in events)
    adapter.close()


def test_identity_unavailable_before_final_retains_original_upload_and_resumes_once(case,monkeypatch):
    adapter,payload,page,events,_=case;original=adapter._identity
    unavailable=[True]
    def identity(s):
        if s.uploaded and unavailable[0]:raise ValueError('identity unavailable')
        return original(s)
    monkeypatch.setattr(adapter,'_identity',identity)
    result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    s=adapter._session(payload)
    assert result.state=='paused_auth' and s.page is page and s.uploaded and not s.final
    assert adapter.interventions()[0]['reason']=='identity_unavailable'
    assert not any(e[0] in {'journal','final','close'} for e in events)
    unavailable[0]=False
    adapter.authorize_resume('target')
    resumed=adapter.resume(payload,context=result.context,before_final=lambda:events.append(('journal',None)))
    assert resumed.state=='platform_scheduled'
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    adapter.close()


def test_changed_directory_blocks_before_upload(case):
    adapter,payload,_,events,_=case;adapter.validate_target(payload,payload,payload['execution_mode'])
    adapter._session(payload).options['statements'][0]['name']='变化了'
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='pre_submit_failed' and result.error_code=='configuration_required'
    assert not any(e[0] in {'upload','final'} for e in events)
    adapter.close()


def test_before_final_authority_failure_never_clicks(case):
    adapter,payload,_,events,_=case
    def reject():raise ValueError('stale attempt')
    with pytest.raises(ValueError):adapter.submit(payload,context=None,before_final=reject)
    assert not any(e[0]=='final' for e in events)
    assert adapter._session(payload).final is False
    adapter.close()


def test_challenge_keeps_original_page_and_requires_explicit_resume(case,monkeypatch):
    adapter,payload,page,events,_=case;adapter.validate_target(payload,payload,payload['execution_mode'])
    monkeypatch.setattr(adapter,'_challenge',lambda s:'complex')
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='paused_auth' and adapter.interventions()[0]['platform']=='bilibili'
    assert adapter._session(payload).page is page and not any(e[0]=='final' for e in events)
    with pytest.raises(ValueError):adapter.resume(payload,context=result.context,before_final=lambda:None)
    monkeypatch.setattr(adapter,'_challenge',lambda s:None);adapter.authorize_resume('target')
    assert adapter.resume(payload,context=result.context,before_final=lambda:None).state=='platform_scheduled'
    adapter.close()


def test_missing_receipt_is_unknown_and_reconcile_does_not_reupload(case,monkeypatch):
    adapter,payload,_,events,_=case
    def click(s,frame):
        events.append(('final',None));s.final_form=raw(payload);adapter._response(s,response(payload,s.final_form,data={}))
    monkeypatch.setattr(adapter,'_click_final',click)
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='result_unknown' and not result.receipt_reference
    assert adapter.reconcile(payload,context=result.context).state=='result_unknown'
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    adapter.close()


def test_query_only_original_aid_bvid_schedule_and_title(case,monkeypatch):
    adapter,payload,_,events,_=case
    initial=adapter.submit(payload,context=None,before_final=lambda:None)
    s=adapter._session(payload);original_open=adapter._open
    def open_session(session,**kwargs):
        original_open(session,**kwargs)
        session.list_rows=[dict(aid=42,bvid='BV1xx411c7mD',title=payload['fields']['title'],dtime=m.form_contract.validate_form(payload)['dtime'],state=-40)]
    monkeypatch.setattr(adapter,'_open',open_session)
    result=adapter.reconcile({**payload,'receipt_reference':initial.receipt_reference},context=None)
    assert result.state=='platform_scheduled' and result.receipt_reference==initial.receipt_reference
    assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    adapter.close()


@pytest.mark.parametrize('change',['origin','method','before_final','wrong_file','wrong_time','boolean_code','malformed_id'])
def test_response_is_not_a_receipt_unless_exact_own_final(case,change):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.final=True;s.final_form=raw(payload);observed=response(payload,s.final_form)
    if change=='origin':observed.url='https://another.invalid/x/vu/web/add/v3'
    if change=='method':observed.request.method='GET'
    if change=='before_final':s.final=False
    if change=='wrong_file':observed.request.post_data_json['videos'][0]['filename']='different'
    if change=='wrong_time':observed.request.post_data_json['dtime']+=300
    if change=='boolean_code':observed.json=lambda:dict(code=False,data={'aid':42})
    if change=='malformed_id':observed.json=lambda:dict(code=0,data={'aid':True,'bvid':'invalid'})
    adapter._response(s,observed)
    assert not s.submit_ok and s.receipt=={}
    adapter.close()


def test_post_final_logout_cannot_erase_exact_bound_receipt(case,monkeypatch):
    adapter,payload,_,events,_=case;original=adapter._challenge
    monkeypatch.setattr(adapter,'_challenge',lambda s:'login' if s.final else original(s))
    result=adapter.submit(payload,context=None,before_final=lambda:None)
    assert result.state=='platform_scheduled' and not adapter.interventions()
    assert sum(e[0]=='final' for e in events)==1
    adapter.close()


def test_metadata_job_has_no_upload_or_final_and_scopes_cache_to_uid(case,monkeypatch):
    adapter,payload,page,events,_=case
    original=adapter._open
    def open_session(s,**kwargs):
        original(s,**kwargs);s.options_at=datetime.now(timezone.utc).isoformat()
        s.options.update(category_mode='human',schedule={'min_lead_seconds':14400,'max_lead_seconds':1296000,'minute_step':5,'timezone':'Asia/Shanghai'})
    monkeypatch.setattr(adapter,'_open',open_session)
    result=adapter.read_platform_options(payload['account_id'])
    assert result['status']=='ready' and result['categories'][0]['id']==101
    assert adapter.get_cached_platform_options(payload['account_id'])['categories']==result['categories']
    assert not any(e[0] in {'upload','final'} for e in events)
    assert sum(e[0]=='close' for e in events)==1
    other=AccountService(adapter.engine).create('bilibili','222','另一合成账号')
    assert adapter.get_cached_platform_options(other.id)['status']=='empty'
    adapter.close()


def test_metadata_challenge_closes_unuploaded_page_and_returns_no_choices(case,monkeypatch):
    adapter,payload,_,events,_=case;monkeypatch.setattr(adapter,'_challenge',lambda s:'login')
    result=adapter.read_platform_options(payload['account_id'])
    assert result==dict(status='verification_required',categories=[],statements=[],verification_kind='login',verification_stage='manager')
    assert [e[0] for e in events]==['close']
    adapter.close()


def test_metadata_invalid_local_login_directs_to_account_management_without_browser(case,monkeypatch):
    adapter,payload,_,events,_=case
    with session_scope(adapter.engine) as db:db.get(AccountRow,payload['account_id']).login_state='expired'
    monkeypatch.setattr(adapter,'_open',lambda *args,**kwargs:pytest.fail('metadata must not open invalid local session'))
    assert adapter.read_platform_options(payload['account_id'])==dict(status='verification_required',categories=[],statements=[],verification_kind='login',verification_stage='local')
    assert not events
    adapter.close()


@pytest.mark.parametrize('kind',['login','sms','complex'])
@pytest.mark.parametrize('stage',['manager','upload'])
def test_metadata_reports_only_actual_safe_challenge_kind_and_stage(case,monkeypatch,kind,stage):
    adapter,payload,page,events,_=case
    def challenge(s):
        return kind if stage=='manager' or page.url==m.form_contract.UPLOAD_URL else None
    monkeypatch.setattr(adapter,'_challenge',challenge)
    result=adapter.read_platform_options(payload['account_id'])
    assert result==dict(status='verification_required',categories=[],statements=[],verification_kind=kind,verification_stage=stage)
    assert not any(e[0] in {'upload','final'} for e in events)
    assert sum(e[0]=='close' for e in events)==1
    assert [e[1] for e in events if e[0]=='navigate']==([m.form_contract.UPLOAD_URL] if stage=='upload' else [])
    adapter.close()


def test_metadata_timeout_is_error_and_preserves_existing_cache(case,monkeypatch):
    adapter,payload,_,events,_=case;original=adapter._open
    def initial(s,**kwargs):
        original(s,**kwargs);s.options_at=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(adapter,'_open',initial)
    cached=adapter.read_platform_options(payload['account_id'])
    assert cached['status']=='ready'
    def incomplete(s,**kwargs):
        original(s,**kwargs);s.options=None
    ticks=iter((0,21))
    monkeypatch.setattr(adapter,'_open',incomplete)
    monkeypatch.setattr(m.time,'monotonic',lambda:next(ticks))
    result=adapter.read_platform_options(payload['account_id'])
    assert result==dict(status='error',categories=[],statements=[])
    assert adapter.get_cached_platform_options(payload['account_id'])['categories']==cached['categories']
    assert not any(e[0] in {'upload','final'} for e in events)
    adapter.close()


@pytest.mark.parametrize('mode,state',[('publish_now','platform_accepted'),('platform_schedule','platform_scheduled')])
def test_real_coordinator_preserves_bilibili_exact_ack_vocabulary(case,app_paths,monkeypatch,mode,state):
    """Real host persistence with synthetic browser data, never a real upload."""
    from sqlalchemy import select
    from yijianjifa.app_services.multi_publish import MultiPublishService
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.features.publish.coordinator import PublishCoordinator
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow,DeliveryAttemptRow
    from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
    import json
    adapter,payload,_,events,_=case
    service=MultiPublishService(adapter.engine,app_paths,catalog=PlatformCatalog(transports={'bilibili':adapter}))
    service.capture_account=lambda account:dict(session_fingerprint=payload['session_fingerprint'])
    video=FinishedVideoService(adapter.engine).import_external(payload['video_path'],duration_ms=1000,title=payload['fields']['title'])
    common={k:v for k,v in payload['fields'].items() if k in {'title','body','tags'}}
    special={k:v for k,v in payload['fields'].items() if k not in common}
    prepared=service.prepare(request_id='host-roundtrip',video_ids=[video.id],
        targets=[dict(account_id=payload['account_id'],platform='bilibili',fields=special)],common_fields=common,
        execution_mode=mode,scheduled_at=payload['scheduled_at'] if mode=='platform_schedule' else None)
    batch=service.save(prepared['prepared_id'],request_id='host-roundtrip')
    def click(s,frame):
        events.append(('final',None));s.final_form=raw(s.payload)
        adapter._response(s,response(s.payload,s.final_form))
    monkeypatch.setattr(adapter,'_click_final',click)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(batch['id'])==state
    delivery=service.get_task(batch['id'])['deliveries'][0]
    assert delivery['state']==state and sum(e[0]=='final' for e in events)==1
    with session_scope(adapter.engine) as db:
        row=db.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id==batch['id']))
        attempt=db.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==row.id))
        result=json.loads(attempt.result_json)
        assert attempt.final_json and result['request_digest']==row.request_digest
        assert result['receipt_reference']=={'work_id':'42','publish_id':'BV1xx411c7mD'}
        assert result.get('protocol_violation') is None
    assert coordinator.run_once(batch['id']) is None
    adapter.close()


@pytest.mark.parametrize('existing,desired',[(True,False),(True,True),(False,True),(False,False)])
def test_content_authorization_restores_explicit_choice_instead_of_toggling_blindly(case,existing,desired):
    adapter,payload,page,events,_=case;payload['fields']['content_authorization']=desired
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    s=adapter._session(payload);adapter._open(s)
    current={'selected':existing};clicks=[];f=m.form_contract.validate_form(payload)
    class Control:
        def __init__(self,selector):self.selector=selector
        def count(self):return 0 if self.selector.endswith('.label-item-v2-container') else 1
        def click(self,**kwargs):
            clicks.append(self.selector)
            if '.auth-content' in self.selector:current['selected']=not current['selected']
        def fill(self,value):pass
        def press(self,key):pass
        def filter(self,**kwargs):return self
    class Form:
        def locator(self,selector):return Control(selector)
        def get_by_text(self,*args,**kwargs):return Control('text')
        def evaluate(self,script):return {'content_authorization':current['selected'],
            'creation_statement_id':f['creation_statement_id'],'tags':f['tags'],
            'statement_text':'合成自制授权显示' if current['selected'] else f['creation_statement_name']}
    page.keyboard=SimpleNamespace(insert_text=lambda value:None)
    adapter._select_fields(s,Form(),f)
    assert current['selected'] is desired
    assert sum('.auth-content' in selector for selector in clicks)==int(existing!=desired)
    assert not any(e[0]=='final' for e in events)
    adapter.close()


@pytest.mark.parametrize('closed',[False,True])
def test_bilibili_receipt_session_capture_never_visits_an_origin_or_uses_sdk_storage_state(case,closed):
    adapter,payload,_,_,_=case
    reads=[]
    local={'origin':'https://member.bilibili.com','localStorage':[{'name':'fixture','value':'latest-synthetic'}]}
    class ExistingPage:
        url='https://member.bilibili.com/platform/upload-manager/article'
        def is_closed(self):return closed
        def evaluate(self,script,origin):
            assert origin==local['origin'];reads.append(origin);return local
        def goto(self,*args,**kwargs):pytest.fail('capture must not navigate an existing page')
    class ForeignPage:
        url='https://foreign.invalid/'
        def is_closed(self):return False
        def evaluate(self,*args):pytest.fail('capture must not read a foreign origin')
    class Context:
        pages=[ExistingPage(),ForeignPage()]
        def cookies(self):return [dict(name='fixture',value='latest-synthetic',domain='member.bilibili.com',path='/',secure=True)]
        def storage_state(self,*args,**kwargs):pytest.fail('SDK storage_state can create and navigate temporary pages')
        def new_page(self,*args,**kwargs):pytest.fail('capture must not create a temporary page')
    blob=m.BilibiliMultiPublishAdapter._capture_session_blob(adapter,Context(),payload['platform_user_id'])
    state=m.creator_auth.decode_session(m.auth.SPEC,blob,payload['platform_user_id'])
    assert state['cookies'][0]['value']=='latest-synthetic'
    assert state['origins']==([] if closed else [local])
    assert reads==([] if closed else [local['origin']])
    adapter.close()
