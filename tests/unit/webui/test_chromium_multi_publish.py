"""Exercise the actual new bridge against a strict synthetic browser boundary."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys
import threading

import pytest
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome,AdapterValidation
from yijianjifa.features.publish.deliveries import digest


@pytest.fixture
def browser(db_engine,app_paths,tmp_path,monkeypatch):
    from yijianjifa.webui import chromium_publish as old
    events=[];pages=[];thread=threading.get_ident()
    identity={'uid':'111','uid_after':'111','status':0,'after_status':0,'profile_status':0,'nickname':'合成抖音'}
    class Locator:
        def count(self):return 1
        def is_enabled(self):return True
        def set_input_files(self,path):events.append(('upload',path,threading.get_ident()))
        def fill(self,code):events.append(('code','redacted',threading.get_ident()));self.page.challenge=None
        def click(self):pass
    class Page:
        def __init__(self):self.url='';self.challenge=None;self.result=False;self.post_challenge=False;self.form=None;self.identity=identity;self.mouse=SimpleNamespace(click=lambda *a:None)
        def is_closed(self):return False
        def goto(self,url,**k):self.url=url
        def add_init_script(self,code):pass
        def locator(self,selector):l=Locator();l.page=self;return l
        def get_by_role(self,*a,**k):return self.locator('button')
        def wait_for_timeout(self,n):pass
        def screenshot(self,**kwargs):return b'synthetic-frame'
        def evaluate(self,code,args=None):
            assert threading.get_ident()==events[0][2]
            if code==old.CHALLENGE:return self.challenge
            if 'return identity;' in code:return self.identity
            if 'p011ResultNeedsSearch' in code:
                rows=[]
                if self.result:rows=[{'time':datetime.strptime(self.form['scheduled_time_text'],'%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M'),'review':'审核中'}]
                return {'identity':self.identity,'ready':True,'path':'/creator-micro/content/manage','query':'测试','matches':rows}
            self.form=args
            if 'p011FinalClicked=true' in code:
                events.append(('final',None,threading.get_ident()));self.result=True;self.url='https://creator.douyin.com/creator-micro/content/manage'
                if self.post_challenge:self.challenge={'sms':True}
                return {'attempted':True}
            self.url='https://creator.douyin.com/creator-micro/content/post/video'
            return {'identity':self.identity,'ready':True,'path':'/creator-micro/content/post/video','title':args['title_text'],'body':args['body_text'],
                'declaration':args['declaration_label'],'title_count':1,'editor_count':1,'mode_controls':1,'final_button_count':1,
                'upload_complete':True,'final_button_enabled':True,'scheduled_time_text':args['scheduled_time_text'],'location':args.get('location_selection')}
    class Context:
        def __init__(self):self.pages=[Page()];pages.append(self.pages[0])
        def set_default_timeout(self,n):pass
        def route(self,pattern,handler):self.write_guard=(pattern,handler)
        def close(self):events.append(('close',None,threading.get_ident()))
        def new_page(self):return self.pages[0]
    def launch(*a,**k):events.append(('launch',k['headless'],threading.get_ident()));return Context()
    driver=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=lambda:events.append(('stop',None,threading.get_ident())))
    monkeypatch.setitem(sys.modules,'playwright.sync_api',SimpleNamespace(sync_playwright=lambda:SimpleNamespace(start=lambda:driver)))
    monkeypatch.setattr(old,'restore_account_session',lambda *a:None)
    monkeypatch.setattr(old,'session_blob',lambda *a:b'synthetic-not-real-cookie')
    monkeypatch.setattr(old,'persist_refreshed_sessions',lambda *a:events.append(('persist',None,threading.get_ident())))
    # This suite fakes browser IO; actual SMS DOM/React controls are covered by
    # test_douyin_sms_verification against offline Chromium.
    from yijianjifa.webui import douyin_sms
    monkeypatch.setattr(douyin_sms,'submit_code',lambda page,code:page.locator('sms-input').fill(code))
    a=AccountService(db_engine).create('douyin','111','合成抖音')
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,a.id);row.session_ref='version-1';row.capability_json=json.dumps({'ordinary':'available','commerce':'pending','location_scope':'pending'})
    video=tmp_path/'owned.mp4';video.write_bytes(b'synthetic-owned')
    when=(datetime.now(timezone.utc)+timedelta(days=1)).replace(second=0,microsecond=0).isoformat()
    payload={'platform':'douyin','account_id':a.id,'platform_user_id':'111','video_id':'video','video_path':str(video),'video_hash':sha256_file(video),
        'session_fingerprint':hashlib.sha256(b'version-1').hexdigest(),'fields':{'title':'测试','body':'简介','tags':'','cover':{'kind':'first_frame','frame_ms':0}},
        'execution_mode':'platform_schedule','scheduled_at':when}
    return payload,pages,events,identity


def adapter(db_engine,app_paths,payload):
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    a=ChromiumMultiPublishAdapter(db_engine,app_paths)
    a.bind_delivery(digest=digest(payload),delivery_id='delivery',task_id='batch')
    return a


def test_new_publish_submission_waits_for_previous_sms_sixty_second_interval(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p);now=[1000.0]
    a.sms_cooldown.clock=lambda:now[0]
    a.sms_cooldown.reserve(p['account_id'])
    assert isinstance(a.validate_target(p,p,p['execution_mode']),AdapterValidation)
    pages[0].wait_for_timeout=lambda milliseconds:now.__setitem__(0,now[0]+milliseconds/1000)
    journals=[]
    try:
        a.submit(p,context=None,before_final=lambda:journals.append(now[0]))
        assert journals and journals[0]>=1060.0
        assert len([e for e in events if e[0]=='final'])==1
    finally:a.close()


def test_cancel_while_waiting_for_sms_interval_never_crosses_final_boundary(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    a.sms_cooldown.reserve(p['account_id'])
    a.validate_target(p,p,p['execution_mode'])
    pages[0].wait_for_timeout=lambda milliseconds:a.cancel.set()
    try:
        a.submit(p,context=None,before_final=lambda:pytest.fail('cancelled task cannot submit'))
        assert not any(e[0]=='final' for e in events)
    finally:a.close()


def test_sms_parks_immediately_and_resume_reuses_page_upload_and_final_once(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    assert isinstance(a.validate_target(p,p,p['execution_mode']),AdapterValidation)
    page=pages[0];page.challenge={'sms':True}
    outcome=a.submit(p,context=None,before_final=lambda:events.append(('journal',None,threading.get_ident())))
    assert outcome.state=='paused_auth' and set(outcome.context)=={'handle'}
    request=a.interventions()[0]
    assert request['delivery_id']=='delivery' and request['platform']=='douyin'
    a.authorize_resume('delivery');a.respond(request['id'],'code','123456')
    result=a.resume(p,context=outcome.context,before_final=lambda:events.append(('journal',None,threading.get_ident())))
    assert result.state=='platform_scheduled'
    assert pages==[page]
    assert [e[0] for e in events if e[0] in {'upload','journal','final'}]==['upload','journal','final']
    a.close()


def test_post_final_challenge_resumes_only_query_and_never_second_final(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    a.validate_target(p,p,p['execution_mode']);pages[0].post_challenge=True
    first=a.submit(p,context=None,before_final=lambda:None)
    assert first.state=='result_unknown'
    r=a.interventions()[0];a.respond(r['id'],'code','123456')
    result=a.reconcile(p,context=first.context)
    assert result.state=='platform_scheduled'
    assert sum(e[0]=='upload' for e in events)==1 and sum(e[0]=='final' for e in events)==1
    a.close()


def test_changed_session_requires_explicit_same_identity_authorization_and_audit(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    with session_scope(db_engine) as s:s.get(AccountRow,p['account_id']).session_ref='version-2'
    result=a.validate_target(p,p,p['execution_mode'])
    assert isinstance(result,AdapterOutcome) and result.state=='paused_auth'
    assert not pages
    with pytest.raises(ValueError):a.resume(p,context=result.context,before_final=lambda:None)
    a.authorize_resume('delivery')
    result=a.resume(p,context=result.context,before_final=lambda:None)
    assert result.state=='platform_scheduled'
    from yijianjifa.storage.models import Setting
    from sqlalchemy import select
    with session_scope(db_engine) as s:
        audits=list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.session_audit:'))))
        assert len(audits)==1
        data=json.loads(audits[0].value_json)
        assert data['old_version']!=data['new_version'] and data['same_identity_authorized'] is True
    a.close()


def test_wrong_live_uid_never_uploads_or_marks_final(browser,db_engine,app_paths):
    p,pages,events,identity=browser;a=adapter(db_engine,app_paths,p)
    identity.update(uid='222',uid_after='222')
    result=a.validate_target(p,p,p['execution_mode'])
    assert result.authorized is False
    assert not any(e[0] in {'upload','final'} for e in events)
    a.close()


@pytest.mark.parametrize('mode,headless',[('background',True),('visible',False)])
def test_saved_browser_mode_controls_persistent_session_launch(browser,db_engine,app_paths,mode,headless):
    from yijianjifa.storage.models import Setting
    p,_,events,_=browser;a=adapter(db_engine,app_paths,p)
    with session_scope(db_engine) as s:s.add(Setting(key='publish.browser_mode',value_json=json.dumps(mode)))
    try:
        assert isinstance(a.validate_target(p,p,p['execution_mode']),AdapterValidation)
        assert next(event[1] for event in events if event[0]=='launch') is headless
    finally:a.close()


def test_production_factories_keep_ordinary_transport_and_offer_independent_creator_adapters(db_engine,app_paths,monkeypatch):
    import playwright.sync_api
    from yijianjifa.webui.douyin_auth import make_douyin_provider
    from yijianjifa.webui.windows_provider import make_windows_provider
    monkeypatch.setattr(playwright.sync_api,'sync_playwright',lambda:pytest.fail('factory must not create a browser'))
    mac=make_douyin_provider(db_engine,app_paths);windows=make_windows_provider(db_engine,app_paths)
    for provider in [mac,windows]:
        assert set(provider.publish_adapters)=={'douyin','kuaishou','channels','bilibili','douyin:image_post'}
        assert callable(provider.scheduled_publish)
        assert len({id(transport) for transport in provider.publish_adapters.values()})==5
        for platform,transport in provider.publish_adapters.items():
            assert transport.platform_id==platform.split(':')[0] and transport.driver is None
            assert 'background_execution' not in transport.verified_capabilities
        assert 'publish_now' not in provider.publish_adapters['douyin'].verified_capabilities


def test_rich_readiness_requires_real_cached_options_and_valid_form_rules(browser,db_engine,app_paths):
    from yijianjifa.app_services.publish_metadata_cache import PublishMetadataCache
    p,_,_,_=browser;a=adapter(db_engine,app_paths,p)
    account=AccountService(db_engine).get(p['account_id'])
    cache=PublishMetadataCache(db_engine)
    chosen={'id':'loc','name':'合成地点','address':'合成地址'}
    cache.write(account,'locations','local|合成',[chosen])
    p['fields']['location']=chosen
    assert a.validate_fields(p) is True
    assert a.plan_readiness(p) is None
    p['fields']['location']={**chosen,'name':'伪造'}
    assert a.validate_fields(p) is False
    p['fields']['title']='超'*31
    assert a.plan_readiness(p)=='platform_fields_invalid'
    a.close()


@pytest.mark.parametrize('ordinary',['pending','not_found'])
def test_readiness_accepts_valid_login_without_an_ordinary_probe_and_keeps_mode_limit(browser,db_engine,app_paths,ordinary):
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    AccountService(db_engine).update_capabilities(p['account_id'],CapabilitySnapshot(ordinary=ordinary))
    assert a.plan_readiness(p) is None
    assert a.plan_readiness({**p,'execution_mode':'publish_now'})=='execution_mode_not_verified'
    assert not pages and not events
    a.close()


@pytest.mark.parametrize('state',['expired','pending_verification','missing_session'])
def test_readiness_rejects_invalid_login_or_missing_session_before_browser_work(browser,db_engine,app_paths,state):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,p['account_id'])
        if state=='missing_session':row.session_ref=None
        else:row.login_state=state
    assert a.plan_readiness(p)=='account_verification_required'
    assert not pages and not events
    a.close()


@pytest.mark.parametrize('post_final,delayed_restore',[(False,False),(True,False),(False,True)])
def test_real_bridge_worker_yields_second_account_and_response_has_no_durable_code(browser,db_engine,app_paths,monkeypatch,post_final,delayed_restore):
    import time
    from sqlalchemy import text
    from yijianjifa.app_services.desktop_api import DesktopApi
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    from yijianjifa.webui import chromium_publish as old
    payload,pages,events,identity=browser
    second=AccountService(db_engine).create('douyin','222','第二合成账号')
    with session_scope(db_engine) as s:
        row=s.get(AccountRow,second.id);row.session_ref='version-second';row.capability_json=json.dumps({'ordinary':'available','commerce':'pending','location_scope':'pending'})
    def restore(context,store,current,profile,uid):
        if uid=='111':
            # A local browser/session can become ready after the old 3s test
            # observation budget. Keep the real worker and coordinator running.
            if delayed_restore:time.sleep(3.2)
            if post_final:context.pages[0].post_challenge=True
            else:context.pages[0].challenge={'sms':True}
        else:context.pages[0].identity={**identity,'uid':uid,'uid_after':uid}
    monkeypatch.setattr(old,'restore_account_session',restore)
    video=FinishedVideoService(db_engine).import_external(Path(payload['video_path']),duration_ms=1000,title='测试')
    bridge=ChromiumMultiPublishAdapter(db_engine,app_paths)
    api=DesktopApi(db_engine,app_paths,publish_adapters={'douyin':bridge})
    def call(op,p=None,rid='read'):
        return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+op,'params':p or {}}),trusted_origin=True)
    try:
        plan=call('prepare',{'video_ids':[video.id],'targets':[{'account_id':aid,'platform':'douyin'} for aid in [payload['account_id'],second.id]],
            'common_fields':{},'execution_mode':'platform_schedule','scheduled_times':{video.id:payload['scheduled_at']},'distribution_by_platform':{'douyin':'whole_batch'}},'plan')['data']
        batch=call('confirm',{'prepared_id':plan['prepared_id'],'intent_id':'plan','consent_partial':False},'save')['data']
        ids=[d['id'] for d in call('detail',{'id':batch['id']})['data']['deliveries']]
        check=call('execution.check',{'id':batch['id'],'delivery_ids':ids})['data']
        assert call('execution.start',{'id':batch['id'],'delivery_ids':ids,'digest':check['digest'],'consent':True,'consent_partial':False},'start')['ok']
        # Observe the asynchronous worker with the same finite budget used for
        # resume below; this contract does not promise a 3s database/host start.
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            children=call('detail',{'id':batch['id']})['data']['deliveries']
            if [c['state'] for c in children]==['result_unknown' if post_final else 'paused_auth','platform_scheduled']:break
            time.sleep(.01)
        assert [c['state'] for c in children]==['result_unknown' if post_final else 'paused_auth','platform_scheduled']
        finals_before=sum(e[0]=='final' for e in events)
        request=call('intervention.status')['data']['request']
        assert request['delivery_id']==ids[0]
        response=call('intervention.respond',{'id':request['id'],'delivery_id':ids[0],'action':'code','code':'654321','consent_same_identity':True},'sensitive-code')
        assert response['ok'],response
        # Resuming the paused account is a separate asynchronous operation.
        # The first account wait must not consume its completion budget.
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            children=call('detail',{'id':batch['id']})['data']['deliveries']
            if all(c['state']=='platform_scheduled' for c in children):break
            time.sleep(.01)
        assert all(c['state']=='platform_scheduled' for c in children)
        assert sum(e[0]=='final' for e in events)==(finals_before if post_final else finals_before+1)
        assert len(pages)==2 and sum(e[0]=='upload' for e in events)==2 and sum(e[0]=='final' for e in events)==2
        assert len({e[2] for e in events})==1 and events[0][2]!=threading.get_ident()
        with db_engine.connect() as c:
            rows=c.execute(text('SELECT request_id,response_json FROM web_command_receipts')).all()
            assert 'sensitive-code' not in {r[0] for r in rows}
            assert '654321' not in json.dumps(rows,default=str)
    finally:assert api.close(5)


def test_expired_verification_gets_new_request_only_on_explicit_same_page_resume(browser,db_engine,app_paths):
    import time
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    a.validate_target(p,p,p['execution_mode']);pages[0].challenge={'sms':True}
    parked=a.submit(p,context=None,before_final=lambda:None)
    old=a.interventions()[0]['id']
    session=next(iter(a.sessions.values()));session.broker._request['expires_at']=time.time()-1
    a.authorize_resume('delivery')
    result=a.resume(p,context=parked.context,before_final=lambda:None)
    assert result.state=='paused_auth' and a.interventions()[0]['id']!=old
    assert len(pages)==1 and not any(e[0]=='upload' for e in events)
    a.close()


def test_cancelling_one_parked_page_releases_only_its_local_browser(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    a.validate_target(p,p,p['execution_mode']);pages[0].challenge={'sms':True}
    a.submit(p,context=None,before_final=lambda:None)
    a.cancel_delivery_session('delivery')
    assert a.interventions()==[] and sum(e[0]=='close' for e in events)==1
    a.close()


def test_completed_receipt_releases_account_for_next_target_on_same_worker(browser,db_engine,app_paths):
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    assert a.submit(p,context=None,before_final=lambda:None).state=='platform_scheduled'
    second={**p,'video_id':'second-video'}
    a.bind_delivery(digest=digest(second),delivery_id='second-delivery',task_id='batch')
    assert a.submit(second,context=None,before_final=lambda:None).state=='platform_scheduled'
    assert sum(e[0]=='upload' for e in events)==2 and sum(e[0]=='final' for e in events)==2
    a.close()


@pytest.mark.parametrize('failure',['wrong_uid','observation_exception'])
def test_initial_identity_failure_releases_original_page_and_same_delivery_retry(browser,db_engine,app_paths,monkeypatch,failure):
    from sqlalchemy import select
    from yijianjifa.app_services.multi_publish_runtime import HostMultiPublishService
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.features.publish.coordinator import PublishCoordinator
    from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
    from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
    from yijianjifa.webui import chromium_publish as old
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    p,pages,events,identity=browser
    a=ChromiumMultiPublishAdapter(db_engine,app_paths)
    service=HostMultiPublishService(db_engine,app_paths,catalog=PlatformCatalog(transports={'douyin':a}))
    video=FinishedVideoService(db_engine).import_external(Path(p['video_path']),duration_ms=1000,title='测试')
    plan=service.prepare(request_id='identity-plan',video_ids=[video.id],targets=[{'account_id':p['account_id'],'platform':'douyin'}],common_fields={},execution_mode='platform_schedule',scheduled_times={video.id:p['scheduled_at']})
    batch=service.save(plan['prepared_id'],request_id='identity-plan')
    did=service.get_task(batch['id'])['deliveries'][0]['id'];coordinator=PublishCoordinator(service)
    evaluate=old.evaluate_script;fail=[True]
    if failure=='wrong_uid':identity.update(uid='222',uid_after='222')
    def observed(page,script,arguments):
        if failure=='observation_exception' and fail[0] and script.endswith('return identity;'):raise RuntimeError('synthetic failed identity read')
        return evaluate(page,script,arguments)
    monkeypatch.setattr(old,'evaluate_script',observed)
    try:
        assert coordinator.run_once(batch['id'])=='pre_submit_failed'
        assert sum(e[0]=='close' for e in events)==1
        assert not any(e[0]=='final' for e in events)
        fail[0]=False;identity.update(uid='111',uid_after='111')
        with session_scope(db_engine) as s:s.get(AccountRow,p['account_id']).session_ref='version-corrected'
        assert service.reprepare_delivery(did,{'body':'纠正资料'})['attempt']==2
        assert coordinator.run_once(batch['id'])=='paused_auth'
        a.authorize_resume(did)
        assert coordinator.resume_verification(did)=='platform_scheduled'
        assert service.get_task(batch['id'])['deliveries'][0]['id']==did
        assert len(pages)==2 and sum(e[0]=='final' for e in events)==1
        with session_scope(db_engine) as s:
            attempts=list(s.scalars(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did).order_by(DeliveryAttemptRow.number)))
            assert attempts[0].final_json is None and attempts[1].final_json is not None
            from yijianjifa.storage.models import Setting
            audits=list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.session_audit:'))))
            assert len(audits)==1 and json.loads(audits[0].value_json)['attempt']==2
    finally:a.close()


def test_same_identity_consent_accepts_only_exact_current_version_once(browser,db_engine,app_paths):
    from sqlalchemy import select
    from yijianjifa.storage.models import Setting
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    try:
        with session_scope(db_engine) as s:s.get(AccountRow,p['account_id']).session_ref='version-2'
        parked=a.validate_target(p,p,p['execution_mode'])
        a.authorize_resume('delivery');live=next(iter(a.sessions.values()))
        assert a._version(live);a._open(live)
        with session_scope(db_engine) as s:s.get(AccountRow,p['account_id']).session_ref='version-3'
        denied=a.resume(p,context=parked.context,before_final=lambda:events.append(('journal',None,threading.get_ident())))
        assert denied.state=='paused_auth' and len(pages)==1 and not any(e[0]=='final' for e in events)
        with session_scope(db_engine) as s:assert len(list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.session_audit:')))))==1
        a.authorize_resume('delivery')
        assert a.resume(p,context=denied.context,before_final=lambda:None).state=='platform_scheduled'
        with session_scope(db_engine) as s:
            rows=list(s.scalars(select(Setting).where(Setting.key.startswith('multi_publish.session_audit:'))))
            assert len(rows)==2
            audits=[json.loads(r.value_json) for r in rows]
            assert all(r['delivery_id']=='delivery' and r['attempt']==1 for r in audits)
        assert len(pages)==1 and sum(e[0]=='final' for e in events)==1
    finally:a.close()


def test_invalid_intervention_response_cannot_authorize_changed_version(browser,db_engine,app_paths):
    p,_,events,_=browser;a=adapter(db_engine,app_paths,p)
    try:
        with session_scope(db_engine) as s:s.get(AccountRow,p['account_id']).session_ref='version-2'
        a.validate_target(p,p,p['execution_mode']);live=next(iter(a.sessions.values()));request=a.interventions()[0]
        with pytest.raises(ValueError):a.respond(request['id'],'code','123456')  # login challenge cannot accept SMS code
        assert a._version(live) is False
        assert not any(e[0]=='final' for e in events)
    finally:a.close()


def test_final_revalidates_frozen_schedule_after_upload_crosses_minimum_window(browser,db_engine,app_paths,monkeypatch):
    from yijianjifa.webui import chromium_publish as old
    p,pages,events,_=browser;a=adapter(db_engine,app_paths,p)
    when=datetime.fromisoformat(p['scheduled_at']);clock=[when-timedelta(hours=2.5,seconds=10)]
    original=old.validate_form_request
    def validate(**kw):return original(**kw,now=clock[0])
    monkeypatch.setattr(old,'validate_form_request',validate)
    try:
        a.validate_target(p,p,p['execution_mode']);page=pages[0];evaluate=page.evaluate
        def crossing(script,args=None):
            value=evaluate(script,args)
            if isinstance(value,dict) and value.get('path')=='/creator-micro/content/post/video':clock[0]=when-timedelta(hours=2.5)+timedelta(seconds=10)
            return value
        page.evaluate=crossing
        def before():
            assert isinstance(a.validate_target(p,p,p['execution_mode']),AdapterValidation)
            events.append(('journal',None,threading.get_ident()))
        result=a.submit(p,context=None,before_final=before)
        assert result.state=='pre_submit_failed'
        assert sum(e[0]=='upload' for e in events)==1
        assert not any(e[0] in {'journal','final'} for e in events)
        assert p['scheduled_at']==when.isoformat()
    finally:a.close()
