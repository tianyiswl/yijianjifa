"""Original-page display is not publication authorization; no platform IO."""
from types import SimpleNamespace

import pytest

from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
from yijianjifa.webui.chromium_publish_session import LiveSession
from yijianjifa.webui.publish_intervention import InterventionBroker


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True,channel='chromium')
        try:yield browser
        finally:browser.close()


@pytest.fixture
def handoff(offline_browser,db_engine,app_paths,monkeypatch):
    context=offline_browser.new_context(viewport={'width':1440,'height':1000},service_workers='block')
    context.route('**/*',lambda route:route.abort())
    page=context.new_page()
    page.set_content('<div class="login-panel"><p>扫码登录</p><button class="qr-refresh" onclick="this.dataset.clicked=\'yes\'">刷新二维码</button></div>')
    # load completion does not guarantee the cold renderer has painted. Wait
    # for fonts and two actual paint frames before exercising the 2s screenshot.
    page.evaluate("""() => {
        window.__p011TestPaintReady = false;
        document.fonts.ready.then(() => requestAnimationFrame(() =>
            requestAnimationFrame(() => { window.__p011TestPaintReady = true; })));
    }""")
    page.wait_for_function('window.__p011TestPaintReady === true', timeout=10000)
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths)
    adapter._thread()
    session=LiveSession({}, {'delivery_id':'delivery'})
    adapter._init_observations(session)
    session.page,session.context=page,context
    session.request_id=session.broker.request('fixture','合成账号','login','请扫码')
    adapter.sessions['synthetic']=session
    monkeypatch.setattr(adapter,'_version',lambda *args,**kwargs:False)
    monkeypatch.setattr(adapter,'_grant_resume',lambda s:pytest.fail('view operation may not grant publication'))
    monkeypatch.setattr(adapter,'_advance',lambda *args,**kwargs:pytest.fail('view operation may not advance publication'))
    try:yield adapter,session,page,context
    finally:context.close()


@pytest.mark.parametrize('final',[False,True])
def test_visible_is_processed_despite_changed_frozen_session_without_resume(handoff,final):
    adapter,session,page,context=handoff;session.final=final
    assert adapter.respond(session.request_id,'visible')['accepted']
    assert session.broker.status()['request']['view_state']=='queued'
    assert adapter.process_verification_view('delivery',session.request_id)['accepted']
    request=session.broker.status()['request']
    assert request['view_state']=='ready' and request['view_mode']=='page' and request['image'].startswith('iVBOR')
    assert session.page is page and session.context is context and session.final is final
    assert session.resume_authorized is False


def test_screenshot_failure_is_visible_and_does_not_claim_ready(handoff,monkeypatch):
    adapter,session,page,_=handoff
    monkeypatch.setattr(page,'screenshot',lambda **kwargs:(_ for _ in ()).throw(RuntimeError('offline render failure')))
    adapter.respond(session.request_id,'visible')
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    request=session.broker.status()['request']
    assert request['view_state']=='failed' and request['view_error']
    assert not request.get('image') and not session.resume_authorized


def test_closed_original_page_has_visible_failure_without_new_page(handoff):
    adapter,session,page,_=handoff;page.close()
    adapter.respond(session.request_id,'visible')
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    request=session.broker.status()['request']
    assert request['view_state']=='failed' and '关闭' in request['view_error']
    assert session.page is page


def test_old_nonce_and_other_delivery_cannot_open_current_page(handoff):
    adapter,session,_,_=handoff
    adapter.respond(session.request_id,'visible')
    with pytest.raises(ValueError):adapter.process_verification_view('another',session.request_id)
    with pytest.raises(ValueError):adapter.process_verification_view('delivery','old')
    assert session.broker.status()['request']['view_state']=='queued'
    assert session.broker.consume(session.request_id)==('visible','')


def point(page,selector):
    box=page.locator(selector).bounding_box()
    return [box['x']+box['width']/2,box['y']+box['height']/2]


def test_pointer_may_refresh_login_qr_but_keeps_handoff_until_explicit_done(handoff):
    adapter,session,page,_=handoff
    adapter.respond(session.request_id,'pointer',point(page,'.qr-refresh'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted']
    assert page.locator('.qr-refresh').get_attribute('data-clicked')=='yes'
    assert session.broker.status()['request']['view_state']=='ready' and not session.resume_authorized


@pytest.mark.parametrize('final',[False,True])
def test_pointer_cannot_click_main_publish_form_even_when_login_dialog_is_present(handoff,final):
    adapter,session,page,_=handoff;session.final=final
    page.set_content('<div class="login-panel">扫码登录</div><div class="post-create"><div class="form-btns"><button onclick="window.didPublish=true">发表</button></div></div>')
    adapter.respond(session.request_id,'pointer',point(page,'button'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    assert not page.evaluate('window.didPublish===true')
    assert session.broker.status()['request']['view_state']=='failed'


@pytest.mark.parametrize('final,allowed',[(False,False),(True,True)])
def test_original_dialog_confirmation_requires_prior_final_authority(handoff,final,allowed):
    adapter,session,page,_=handoff;session.final=final
    page.set_content('<div class="weui-desktop-dialog"><p>管理员本人验证</p><button onclick="window.confirmed=true">直接发表</button></div>')
    session.broker.finish(session.request_id)
    session.request_id=session.broker.request('fixture','合成账号','complex','原验证')
    adapter.respond(session.request_id,'pointer',point(page,'button'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is allowed
    assert page.evaluate('window.confirmed===true') is allowed


def test_restored_form_cannot_be_clicked_as_an_old_login_challenge(handoff):
    adapter,session,page,_=handoff
    page.set_content('<button onclick="window.clicked=true">下一步</button>')
    adapter.respond(session.request_id,'pointer',point(page,'button'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    assert not page.evaluate('window.clicked===true')


def test_opaque_iframe_does_not_grant_whole_page_pointer_permission(handoff):
    adapter,session,page,_=handoff
    page.set_content('<div class="login-panel">扫码登录</div><iframe srcdoc="<button>发表</button>"></iframe>')
    adapter.respond(session.request_id,'pointer',point(page,'iframe'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    assert session.broker.status()['request']['view_state']=='failed'


def test_shadow_login_host_cannot_hide_a_final_button_from_hit_testing(handoff):
    adapter,session,page,_=handoff
    page.set_content('<div class="login-panel">扫码登录</div><div id="login-host"></div>')
    page.locator('#login-host').evaluate("e => e.attachShadow({mode:'open'}).innerHTML='<button onclick=\"window.clicked=true\">发表</button>'")
    adapter.respond(session.request_id,'pointer',point(page,'#login-host button'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    assert not page.evaluate('window.clicked===true')


def test_inspection_rejects_original_confirmation_even_if_final_flag_is_present(handoff):
    adapter,session,page,_=handoff
    adapter.inspection_only=True;session.final=True
    page.set_content('<div class="weui-desktop-dialog"><p>管理员本人验证</p><button onclick="window.confirmed=true">直接发表</button></div>')
    session.broker.finish(session.request_id)
    session.request_id=session.broker.request('fixture','合成账号','complex','原验证')
    adapter.respond(session.request_id,'pointer',point(page,'button'))
    assert adapter.process_verification_view('delivery',session.request_id)['accepted'] is False
    assert not page.evaluate('window.confirmed===true')


@pytest.mark.parametrize('final,expected',[(False,False),(True,True)])
def test_regular_context_network_guard_requires_host_final_marker_before_write(offline_browser,db_engine,app_paths,final,expected):
    context=offline_browser.new_context(service_workers='block');transport=[]
    def fake_transport(route):
        transport.append(route.request.url)
        route.fulfill(status=200,body='{}',headers={'Access-Control-Allow-Origin':'*'})
    context.route('**/*',fake_transport)
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths)
    session=LiveSession({},{});session.context=context;session.final=final
    adapter._install_publish_guard(session)
    page=context.new_page()
    try:
        page.set_content('<p>offline</p>')
        result=page.evaluate("() => fetch('https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create',{method:'POST',body:'{}'}).then(()=>true).catch(()=>false)")
        assert result is expected
        assert bool(transport) is expected
    finally:context.close()


@pytest.mark.parametrize('action',['visible','refresh','scroll','pointer'])
def test_view_operations_record_queued_state_without_leaking_pointer_values(action):
    broker=InterventionBroker();nonce=broker.request('fixture','账号','login','原说明')
    broker.respond(nonce,action,[0,0] if action in {'scroll','pointer'} else '')
    request=broker.status()['request']
    assert request['view_state']=='queued' and request['view_action']==action
    assert request['view_revision']==1
    assert 'code' not in request


def test_old_view_revision_cannot_finish_or_consume_next_action():
    broker=InterventionBroker();nonce=broker.request('fixture','账号','login','原说明')
    first=broker.respond(nonce,'visible')
    assert broker.consume(nonce,view_revision=first['view_revision'])==('visible','')
    broker.view_result(nonce,first['view_revision'])
    second=broker.respond(nonce,'refresh')
    assert second['view_revision']==first['view_revision']+1
    broker.view_result(nonce,first['view_revision'],error='旧失败')
    assert broker.status()['request']['view_state']=='queued'
    assert broker.consume(nonce,view_revision=first['view_revision']) is None
    assert broker.consume(nonce,view_revision=second['view_revision'])==('refresh','')


@pytest.mark.parametrize('platform,url',[
    ('channels','https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create'),
    ('bilibili','https://member.bilibili.com/x/vu/web/add/v3'),
    ('kuaishou','https://cp.kuaishou.com/rest/cp/works/v2/video/pc/submit'),
    ('douyin','https://creator.douyin.com/web/api/media/aweme/create/'),
])
@pytest.mark.parametrize('final',[False,True])
def test_actual_shaped_final_routes_require_the_final_marker(db_engine,app_paths,platform,url,final):
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths);adapter.platform_id=platform
    handlers=[];context=SimpleNamespace(route=lambda pattern,handler:handlers.append(handler))
    session=LiveSession({},{});session.context=context;session.final=final
    adapter._install_publish_guard(session)
    operations=[]
    route=SimpleNamespace(request=SimpleNamespace(url=url,method='POST'),abort=lambda *a:operations.append('abort'),fallback=lambda:operations.append('fallback'))
    handlers[0](route)
    assert operations==(['fallback'] if final else ['abort'])


@pytest.mark.parametrize('inspection',[False,True])
@pytest.mark.parametrize('platform,url',[
    ('channels','https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/helper/upload/create'),
    ('bilibili','https://member.bilibili.com/x/vu/web/upload/create'),
    ('kuaishou','https://cp.kuaishou.com/rest/cp/upload/create'),
])
def test_upload_initialization_is_not_mistaken_for_final_submission(db_engine,app_paths,platform,url,inspection):
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths,inspection_only=inspection);adapter.platform_id=platform
    handlers=[];context=SimpleNamespace(route=lambda pattern,handler:handlers.append(handler))
    session=LiveSession({},{});session.context=context
    adapter._install_publish_guard(session)
    operations=[]
    handlers[0](SimpleNamespace(request=SimpleNamespace(url=url,method='POST'),abort=lambda *a:operations.append('abort'),fallback=lambda:operations.append('fallback')))
    assert operations==['fallback']


def test_finished_broker_request_cannot_be_reopened(handoff):
    adapter,session,_,_=handoff
    session.broker.finish(session.request_id)
    with pytest.raises(ValueError,match='验证请求已结束'):
        adapter.process_verification_view('delivery',session.request_id)
