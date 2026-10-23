"""Additional synthetic review cases; no browser or real account operations."""
from types import SimpleNamespace
import base64
import threading
import pytest

from test_kuaishou_multi_publish import case, module


def test_sms_inside_creator_frame_resumes_same_page_without_reupload(case,monkeypatch):
    adapter,payload,page,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    state={'sms':True}
    # Page locators do not cross an iframe; only its actual Frame can resolve it.
    page.locator=lambda _:SimpleNamespace(count=lambda:0)
    inputs=SimpleNamespace(count=lambda:1,fill=lambda value:state.update(sms=False))
    button=SimpleNamespace(count=lambda:1,is_enabled=lambda:True,click=lambda:None)
    frame=SimpleNamespace(url='https://cp.kuaishou.com/onvideo-application/index.html',
                          locator=lambda _:inputs,get_by_role=lambda *args,**kw:button,
                          evaluate=lambda _:dict(sms=state['sms']))
    page.frames=[frame]
    monkeypatch.setattr(adapter,'_frame',lambda _:frame)
    monkeypatch.setattr(adapter,'_challenge',lambda _:'sms' if state['sms'] else None)
    parked=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
    assert parked.state=='paused_auth'
    assert adapter._session(payload).page is page and not any(e[0]=='close' for e in events)
    request=adapter.interventions()[0]
    adapter.respond(request['id'],'code','123456')
    try:
        outcome=adapter.resume(payload,context=parked.context,
                               before_final=lambda:events.append(('journal',None)))
        assert outcome.state=='platform_accepted'
        assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
        assert sum(e[0]=='close' for e in events)==1
        assert adapter._session(payload).page is None  # Successful receipt closed it.
    finally:adapter.close()


def test_boolean_result_is_not_a_valid_submit_receipt(case):
    adapter,payload,_,_,_=case
    session=adapter._session(payload);adapter._init_observations(session)
    session.upload_id='99';session.final=True
    form=module().form_contract.validate_form(payload)
    response=SimpleNamespace(url='https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit',
        request=SimpleNamespace(method='POST',post_data_json={
            'fileId':'99','caption':form['caption'],'publishTime':form['publish_ms']}),
        json=lambda:{'result':True,'data':{'workId':'42'}})
    adapter._response(session,response)
    try:
        assert not session.submit_ok and not session.submit_rejected
        assert session.receipt=={}
    finally:adapter.close()


@pytest.mark.parametrize('count',[0,2])
def test_sms_surface_must_be_unique_in_the_existing_creator_page(case,monkeypatch,count):
    adapter,payload,page,events,_=case
    session=adapter._session(payload);adapter._open(session)
    page.evaluate=lambda _:dict(sms=count==2)
    frame=SimpleNamespace(evaluate=lambda _:dict(sms=count==2))
    monkeypatch.setattr(adapter,'_frame',lambda _:frame)
    try:
        with pytest.raises(ValueError,match='unavailable or ambiguous'):
            adapter._verification_surface(session)
        assert session.page is page and not any(e[0]=='close' for e in events)
    finally:adapter.close()


def test_boolean_work_status_is_not_a_platform_state(case):
    _,payload,_,_,_=case
    contract=module().form_contract;form=contract.validate_form(payload)
    row={'workId':'42','title':form['caption'],'publishType':2,'publishStatus':True}
    assert contract.verified_row([row],{'work_id':'42'},form) is None


def test_verification_first_seen_at_final_guard_keeps_original_upload(case,monkeypatch):
    adapter,payload,page,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    def before_final():
        monkeypatch.setattr(adapter,'_challenge',lambda _:'sms')
        observed=adapter.validate_target(payload,payload,payload['execution_mode'])
        assert observed.state=='paused_auth'
        # This is the coordinator's response to an unavailable final identity.
        raise ValueError('final account identity or permission is not verified')
    try:
        outcome=adapter.submit(payload,context=None,before_final=before_final)
        assert outcome.state=='paused_auth'
        assert adapter._session(payload).page is page
        assert adapter.interventions()[0]['kind']=='sms'
        assert sum(e[0]=='upload' for e in events)==1
        assert not any(e[0] in {'final','close'} for e in events)
    finally:adapter.close()


def test_login_frames_refresh_only_on_original_worker_and_never_recreate_page(case,monkeypatch):
    from yijianjifa.webui import chromium_publish_session as lifecycle
    adapter,payload,page,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    clock=[10.0];frames=[]
    monkeypatch.setattr(lifecycle.time,'monotonic',lambda:clock[0])
    def screenshot(**kwargs):
        assert kwargs=={'timeout':2000}
        frames.append(threading.get_ident());return ('frame-'+str(len(frames))).encode()
    page.screenshot=screenshot
    session=adapter._session(payload)
    try:
        adapter._park(session,'login')
        first=adapter.interventions()[0]
        assert first['image']==base64.b64encode(b'frame-1').decode()
        clock[0]=11.0
        assert adapter.refresh_interventions() is False
        clock[0]=12.0
        assert adapter.refresh_interventions() is True
        current=adapter.interventions()[0]
        assert current['id']==first['id'] and current['image']==base64.b64encode(b'frame-2').decode()
        assert session.page is page and not any(e[0] in {'upload','final','close'} for e in events)
        assert frames==[adapter.thread_id,adapter.thread_id]
    finally:adapter.close()


def test_inactive_adapter_frame_refresh_performs_no_browser_work(db_engine,app_paths):
    adapter=module().KuaishouMultiPublishAdapter(db_engine,app_paths)
    assert adapter.refresh_interventions() is False and adapter.driver is None


def test_login_can_transition_to_sms_on_same_page_without_stale_request(case,monkeypatch):
    adapter,payload,page,events,_=case
    assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
    challenge=['login']
    monkeypatch.setattr(adapter,'_challenge',lambda _:challenge[0])
    inputs=SimpleNamespace(count=lambda:1,fill=lambda value:challenge.__setitem__(0,None))
    buttons=SimpleNamespace(count=lambda:1,is_enabled=lambda:True,click=lambda:None)
    surface=SimpleNamespace(locator=lambda _:inputs,get_by_role=lambda *args,**kw:buttons)
    monkeypatch.setattr(adapter,'_verification_surface',lambda _:surface)
    try:
        first=adapter.submit(payload,context=None,before_final=lambda:None)
        request=adapter.interventions()[0]
        adapter.respond(request['id'],'done');challenge[0]='sms'
        parked=adapter.resume(payload,context=first.context,before_final=lambda:None)
        assert parked.state=='paused_auth' and adapter._session(payload).page is page
        sms=adapter.interventions()[0]
        assert sms['kind']=='sms' and sms['id']!=request['id']
        with pytest.raises(ValueError):adapter.respond(request['id'],'done')
        adapter.respond(sms['id'],'code','123456')
        result=adapter.resume(payload,context=parked.context,before_final=lambda:None)
        assert result.state=='platform_accepted'
        assert sum(e[0]=='upload' for e in events)==sum(e[0]=='final' for e in events)==1
    finally:adapter.close()
