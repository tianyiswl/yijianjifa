"""Original-session manual handoff; all Chromium content is supplied offline."""
import pytest

from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
from yijianjifa.webui.chromium_publish_session import LiveSession
from yijianjifa.webui.publish_intervention import InterventionBroker
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel='chromium')
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def handoff(offline_browser, db_engine, app_paths):
    context = offline_browser.new_context(viewport={'width': 1440, 'height': 1000}, service_workers='block')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    page.set_content('''<style>body {margin:0} .space {height:2600px}</style>
        <button class="qr-refresh" onclick="this.dataset.clicked='yes'">刷新二维码</button>
        <p>扫码登录</p><div class="space"></div>''')
    # load completion does not guarantee the cold renderer has painted. Wait
    # for fonts and two actual paint frames before exercising the 2s screenshot.
    page.evaluate("""() => {
        window.__p011TestPaintReady = false;
        document.fonts.ready.then(() => requestAnimationFrame(() =>
            requestAnimationFrame(() => { window.__p011TestPaintReady = true; })));
    }""")
    page.wait_for_function('window.__p011TestPaintReady === true', timeout=10000)
    accounts = AccountService(db_engine)
    account = accounts.create('channels', '123', '合成账号')
    ref = accounts.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'synthetic session')
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    session = LiveSession({'account_id': account.id, 'platform_user_id': '123', 'platform': 'channels'}, {})
    session.original_ref = ref
    session.page, session.context = page, context
    session.request_id = session.broker.request('fixture', '合成账号', 'login', '请扫码')
    try:
        yield adapter, session, page, context
    finally:
        context.close()


@pytest.mark.parametrize('final', [False, True])
def test_open_view_displays_original_page_without_rebuilding_session(handoff, final):
    adapter, session, page, context = handoff
    session.final = final
    session.broker.respond(session.request_id, 'visible')
    adapter._consume(session)
    request = session.broker.status()['request']
    assert request.get('view_mode') == 'page'
    assert request['image'].startswith('iVBOR')
    assert session.page is page and session.context is context and session.final is final


def test_viewing_an_unread_identity_does_not_instruct_nonexistent_captcha(handoff):
    adapter, session, page, context = handoff
    session.broker.finish(session.request_id)
    session.request_id = session.broker.request('fixture', '合成账号', 'login', '程序尚未确认所选账号。', reason='identity_unavailable')
    session.broker.respond(session.request_id, 'visible')
    adapter._consume(session, view_only=True)
    request = session.broker.status()['request']
    assert request['reason'] == 'identity_unavailable'
    assert '重新核对原账号' in request['message']
    assert '完成登录或验证码' not in request['message']
    assert session.page is page and session.context is context
    assert session.resume_authorized is False and session.final is False


def test_login_qr_can_be_refreshed_by_a_human_click_on_same_page(handoff):
    adapter, session, page, context = handoff
    rect = page.locator('.qr-refresh').bounding_box()
    session.broker.respond(session.request_id, 'pointer', [rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2])
    adapter._consume(session)
    assert page.locator('.qr-refresh').get_attribute('data-clicked') == 'yes'
    assert session.page is page and session.context is context


@pytest.mark.parametrize('choice,final,platform,permitted', [
    (True,False,'channels',True), (False,False,'channels',False),
    (None,False,'channels',False), ('true',False,'channels',False),
    (True,True,'channels',False), (True,False,'kuaishou',False),
])
def test_manual_original_agreement_requires_exact_prefinal_channels_intent(handoff,choice,final,platform,permitted):
    adapter,session,page,_=handoff
    adapter.platform_id=platform
    session.payload={'fields':{'channels_original':choice}}
    session.final=final
    page.set_content('''<div class="declare-original-dialog"><h3>原创权益</h3>
        <label><input type="checkbox">我已阅读原创协议</label>
        <button onclick="this.dataset.clicked='yes'">声明原创</button></div>
        <button onclick="this.dataset.clicked='yes'">发表</button>''')
    session.broker.finish(session.request_id)
    session.request_id=session.broker.request('fixture','合成账号','complex','请亲自确认原创协议')
    adapter._challenge=lambda s:'complex'
    rect=page.get_by_role('checkbox').bounding_box()
    session.broker.respond(session.request_id,'pointer',[rect['x']+rect['width']/2,rect['y']+rect['height']/2])
    if permitted:
        adapter._consume(session)
        assert page.get_by_role('checkbox').is_checked()
        # The host did not accept or click the agreement confirmation itself.
        assert page.get_by_role('button',name='声明原创',exact=True).get_attribute('data-clicked') is None
    else:
        with pytest.raises(ValueError):adapter._consume(session)
        assert not page.get_by_role('checkbox').is_checked()
    assert session.final is final


@pytest.mark.parametrize('label', ['发表','发布','确认发表','保存草稿','存为草稿'])
def test_manual_original_dialog_cannot_replace_final_journal_or_save_draft(handoff,label):
    adapter,session,page,_=handoff
    session.payload={'fields':{'channels_original':True}}
    page.set_content('<div class="declare-original-dialog"><h3>原创权益</h3><button onclick="this.dataset.clicked=\'yes\'">'+label+'</button></div>')
    session.broker.finish(session.request_id)
    session.request_id=session.broker.request('fixture','合成账号','complex','原创协议')
    adapter._challenge=lambda s:'complex'
    rect=page.get_by_role('button',name=label,exact=True).bounding_box()
    session.broker.respond(session.request_id,'pointer',[rect['x']+rect['width']/2,rect['y']+rect['height']/2])
    with pytest.raises(ValueError):adapter._consume(session)
    assert page.get_by_role('button',name=label,exact=True).get_attribute('data-clicked') is None
    assert session.final is False


def test_scroll_reveals_original_page_without_navigation_or_reload(handoff):
    adapter, session, page, context = handoff
    session.broker.respond(session.request_id, 'scroll', [0, 450])
    adapter._consume(session)
    page.wait_for_function('scrollY > 300')
    assert session.page is page and session.context is context and page.url == 'about:blank'


def test_refresh_updates_frame_without_reloading_or_resetting_page(handoff):
    adapter, session, page, _ = handoff
    adapter._refresh_intervention_frame(session, force=True)
    initial = session.broker.status()['request']['image']
    page.locator('.qr-refresh').evaluate("e => e.textContent='二维码已更新'")
    session.broker.respond(session.request_id, 'refresh')
    adapter._consume(session)
    assert session.broker.status()['request']['image'] != initial
    assert page.locator('.qr-refresh').inner_text() == '二维码已更新'


def test_sms_expanded_view_includes_original_page(handoff):
    adapter, session, page, _ = handoff
    page.set_content('<input placeholder="验证码">')
    session.broker.finish(session.request_id)
    session.request_id = session.broker.request('fixture', '合成账号', 'sms', '请输入验证码')
    session.broker.respond(session.request_id, 'visible')
    adapter._consume(session)
    request = session.broker.status()['request']
    assert request.get('view_mode') == 'page' and request['image'].startswith('iVBOR')


@pytest.mark.parametrize('code', [[0, 1001], [0, float('nan')], [True, 1], [0], '450', [0, 0, 0]])
def test_scroll_rejects_unbound_or_invalid_interactions(code):
    broker = InterventionBroker()
    token = broker.request('fixture', '合成账号', 'complex', '离线')
    with pytest.raises(ValueError):
        broker.respond(token, 'scroll', code)
    assert broker.consume(token) is None


def test_frame_actions_are_bound_to_current_request_and_not_persisted():
    broker = InterventionBroker()
    token = broker.request('fixture', '合成账号', 'login', '离线')
    with pytest.raises(ValueError):
        broker.respond('stale', 'refresh')
    broker.respond(token, 'scroll', [0, -450])
    assert broker.consume(token) == ('scroll', [0, -450])
    assert 'code' not in broker.status()['request']
    broker.finish(token)
    assert broker.status()['request'] is None
