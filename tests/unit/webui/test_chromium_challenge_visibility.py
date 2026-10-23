"""Execute the shipped challenge JS in offline Chromium, with no account data."""
import pytest

from yijianjifa.webui.chromium_publish import CHALLENGE, challenge_kind


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True,channel='chromium')
        try:yield browser
        finally:browser.close()


@pytest.fixture
def offline_page(offline_browser):
    context=offline_browser.new_context(service_workers='block')
    # All documents are supplied with set_content. Even a fixture accidentally
    # adding an external URL cannot send an HTTP request.
    context.route('**/*',lambda route:route.abort())
    page=context.new_page()
    try:yield page
    finally:context.close()


CAPTCHA='<iframe id="captcha-fixture" style="width:160px;height:120px;border:0"></iframe>'


@pytest.mark.parametrize('html',[
    '<iframe id="captcha-fixture" style="width:0;height:0;border:0"></iframe>',
    '<iframe id="captcha-fixture" style="width:0;height:120px;border:0"></iframe>',
    '<iframe id="captcha-fixture" style="width:160px;height:0;border:0"></iframe>',
    '<div style="transform:scale(0)">'+CAPTCHA+'</div>',
    '<div style="opacity:0">'+CAPTCHA+'</div>',
    '<div style="opacity:0"><div style="opacity:1">'+CAPTCHA+'</div></div>',
    '<iframe id="captcha-fixture" style="width:160px;height:120px;opacity:0"></iframe>',
    '<div style="display:none">'+CAPTCHA+'</div>',
    '<iframe id="captcha-fixture" style="display:none;width:160px;height:120px"></iframe>',
    '<div style="visibility:hidden">'+CAPTCHA+'</div>',
    '<iframe id="captcha-fixture" style="visibility:hidden;width:160px;height:120px"></iframe>',
    '<iframe id="captcha-fixture" style="visibility:collapse;width:160px;height:120px"></iframe>',
],ids=['zero-area','zero-width','zero-height','zero-scale','transparent-parent','transparent-ancestor',
       'transparent-frame','display-none-parent','display-none-frame','hidden-parent','hidden-frame','collapsed-frame'])
def test_inactive_challenge_placeholder_is_not_a_verification(offline_page,html):
    offline_page.set_content(html)
    result=offline_page.evaluate(CHALLENGE)
    assert result==dict(sms=False,complex=False,login=False)
    assert challenge_kind(result) is None


@pytest.mark.parametrize('html',[
    CAPTCHA,
    '<div style="display:contents">'+CAPTCHA+'</div>',
    '<div style="opacity:0.01">'+CAPTCHA+'</div>',
    '<div style="position:absolute;left:-9999px">'+CAPTCHA+'</div>',
],ids=['visible','display-contents-parent','positive-opacity','offscreen-retained'])
def test_rendered_challenge_remains_a_verification(offline_page,html):
    offline_page.set_content(html)
    result=offline_page.evaluate(CHALLENGE)
    assert result==dict(sms=False,complex=True,login=False)
    assert challenge_kind(result)=='complex'


@pytest.mark.parametrize('html,expected',[
    ('<input placeholder="验证码">','sms'),
    ('<div style="opacity:0"><input placeholder="验证码"></div>',None),
    ('<input placeholder="验证码"><div style="opacity:0"><input placeholder="验证码"></div>','sms'),
    ('<button>扫码登录</button>','login'),
    ('<div style="opacity:0"><button>扫码登录</button></div>',None),
    ('<div style="visibility:hidden"><button>手机号登录</button></div>',None),
],ids=['sms-visible','sms-transparent','one-real-sms','login-visible','login-transparent','login-hidden'])
def test_sms_and_login_use_the_same_rendered_visibility_guard(offline_page,html,expected):
    offline_page.set_content(html)
    assert challenge_kind(offline_page.evaluate(CHALLENGE))==expected
