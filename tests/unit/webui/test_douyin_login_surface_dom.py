"""Run the actual login presentation probe in offline Chromium."""
import pytest

from yijianjifa.webui.douyin_login_bootstrap import READ_LOGIN_SURFACE


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel='chromium')
        try: yield browser
        finally: browser.close()


@pytest.fixture
def page(offline_browser):
    context = offline_browser.new_context(service_workers='block')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    page.set_default_navigation_timeout(10000)
    try: yield page
    finally: context.close()


def test_empty_login_panel_is_not_a_dashboard(page):
    page.set_content('<div>我是创作者</div><div>我是MCN机构</div><div style="height:400px"></div>')
    state = page.evaluate(READ_LOGIN_SURFACE)
    assert state['landing'] and not state['form'] and not state['qr']


@pytest.mark.parametrize('style', ['display:none', 'opacity:0', 'width:0;height:0;border:0'])
def test_hidden_sdk_initialization_is_not_a_human_challenge(page, style):
    page.set_content(f'<div>我是创作者</div><iframe id="captcha-init" style="{style}"></iframe>')
    assert page.evaluate(READ_LOGIN_SURFACE)['challenge'] is False


def test_visible_challenge_is_preserved(page):
    page.set_content('<iframe id="captcha-verify" style="width:200px;height:200px"></iframe>')
    assert page.evaluate(READ_LOGIN_SURFACE)['challenge'] is True


def test_loaded_qr_and_failed_qr_image_are_distinguished(page):
    page.set_content('''<img alt="二维码" width="180" height="180"
        src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">''')
    assert page.evaluate(READ_LOGIN_SURFACE)['qr'] is True
    page.set_content('<img alt="二维码" src="https://offline.invalid/qr">')
    assert page.evaluate(READ_LOGIN_SURFACE)['qr'] is False


def test_phone_input_is_only_reported_as_a_boolean(page):
    page.set_content('<span>扫码登录</span><input placeholder="手机号" value="fixture-private-phone">')
    state = page.evaluate(READ_LOGIN_SURFACE)
    assert state['scan'] and state['form'] and state['interacting']
    assert all(type(value) is bool for value in state.values())
    assert 'fixture-private-phone' not in repr(state)


def test_hidden_old_form_does_not_hide_authenticated_dashboard(page):
    page.set_content('<div style="display:none"><input value="old"><span>扫码登录</span></div><h1>工作台</h1>')
    state = page.evaluate(READ_LOGIN_SURFACE)
    assert not any(state[key] for key in ('loading', 'blank', 'landing', 'scan', 'form', 'qr', 'challenge', 'interacting'))


def test_default_country_code_is_not_user_input(page):
    page.set_content('''<span>扫码登录</span>
        <input role="combobox" aria-label="国家/地区" value="+86">
        <input type="tel" placeholder="请输入手机号">
        <input type="tel" placeholder="请输入验证码">''')
    state = page.evaluate(READ_LOGIN_SURFACE)
    assert state['form'] and state['scan'] and not state['interacting']


@pytest.mark.parametrize('label', ['发布高清视频', '发布视频', '内容管理', '作品管理'])
def test_dashboard_observation_accepts_more_than_one_account_layout(page, label):
    page.set_content(f'<nav><span>{label}</span></nav>')
    assert page.evaluate(READ_LOGIN_SURFACE)['dashboard'] is True


def test_partial_marketing_body_is_not_a_dashboard(page):
    page.set_content('<h1>抖音创作者中心</h1><div>正在加载</div>')
    assert page.evaluate(READ_LOGIN_SURFACE)['dashboard'] is False


def test_identity_budget_bounds_a_fetch_wrapper_that_ignores_abort(page):
    from yijianjifa.webui.chromium_publish import READ_BOUNDED_LOGIN_IDENTITY
    page.set_content('<h1>offline dashboard</h1>')
    # Use the exact production wrapper while replacing only its origin guard in
    # this offline document. The never-settling fetch must not stall polling.
    script = READ_BOUNDED_LOGIN_IDENTITY.replace("location.origin!=='https://creator.douyin.com'", 'false')
    result = page.evaluate('''async script => {
      const original=window.fetch;let signal;
      window.fetch=async(_url, options)=>{signal=options.signal;return await new Promise(()=>{});};
      try {
        const read=new (Object.getPrototypeOf(async function(){}).constructor)('loginBudgetMs',script);
        try{await read(40);return {timedOut:false};}
        catch(error){return {timedOut:error.message==='login-identity-timeout',aborted:signal.aborted};}
      } finally {window.fetch=original;}
    }''', script)
    assert result == {'timedOut': True, 'aborted': True}


def test_authenticated_dashboard_search_is_not_a_login_form(page):
    page.set_content('<nav>内容管理</nav><input type="search" placeholder="搜索作品" value="fixture-query">')
    state = page.evaluate(READ_LOGIN_SURFACE)
    assert state['dashboard'] and not state['form'] and not state['interacting']
