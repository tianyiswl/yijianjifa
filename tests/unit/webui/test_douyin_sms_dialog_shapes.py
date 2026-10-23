"""Network-blocked Chromium checks for observed SMS wording and modal shapes.

The real-page screenshot confirms the SMS heading, resend label, and separate
cancel/verify footer.  The HTML hierarchy below is synthetic: it intentionally
varies the unobserved DOM nesting instead of claiming to reproduce its source.
"""
import pytest

from yijianjifa.webui import douyin_sms


@pytest.fixture(scope="module")
def sms_shape_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, channel="chromium")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def sms_shape_page(sms_shape_browser):
    context = sms_shape_browser.new_context(service_workers="block")
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def modal_html(*, resend="重新发送", nested=False, label_text=""):
    send_label = f"<span>{resend}</span>" if nested else resend
    verify_label = "<span>验证</span>" if nested else "验证"
    return f"""
      <div class="verify-modal">
        <div class="modal-heading">接收短信验证码</div>
        <div class="modal-body">
          <p>为确保本人操作抖音账号，请输入当前手机号收到的短信验证码</p>
          <div class="input-row">
            <label>{label_text}<input placeholder="请输入验证码"
              oninput="this.closest('.verify-modal').querySelector('.verify-submit').classList.remove('disabled')"></label>
            <div class="send-code" onclick="window.sent++;this.textContent='59s'">{send_label}</div>
          </div>
        </div>
        <div class="modal-footer">
          <div class="cancel" onclick="window.cancelled++">取消</div>
          <div class="verify-submit disabled" onclick="if(!this.classList.contains('disabled')){{window.verified++;this.closest('.verify-modal').remove();}}">{verify_label}</div>
        </div>
      </div>"""


def render(page, modal):
    page.set_content("""
      <script>window.sent=0;window.verified=0;window.published=0;window.cancelled=0;</script>
      <main><button onclick="window.published++">确认</button>
        <button onclick="window.published++">发布</button>
        <button onclick="window.published++">定时发布</button>
      </main>""" + modal)


def assert_no_publish(page):
    assert page.evaluate("window.published") == 0
    assert page.evaluate("window.cancelled") == 0


def test_observed_short_resend_label_is_clicked_in_its_sms_modal(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html())

    douyin_sms.request_code(page)

    assert page.evaluate("window.sent") == 1
    assert_no_publish(page)


def test_body_description_does_not_hide_separate_verification_footer(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html(resend="获取验证码"))

    douyin_sms.submit_code(page, "123456")

    assert page.evaluate("window.verified") == 1
    assert page.locator("input").count() == 0
    assert_no_publish(page)


def test_sms_label_does_not_narrow_scope_to_the_input_label(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html(label_text="短信验证码"))

    douyin_sms.request_code(page)
    douyin_sms.submit_code(page, "123456")

    assert page.evaluate("window.sent") == 1
    assert page.evaluate("window.verified") == 1
    assert_no_publish(page)


def test_nested_span_actions_each_click_once_without_outside_confirmation(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html(nested=True))

    douyin_sms.request_code(page)
    douyin_sms.submit_code(page, "123456")

    assert page.evaluate("window.sent") == 1
    assert page.evaluate("window.verified") == 1
    assert_no_publish(page)


@pytest.mark.parametrize("action", ["request", "submit"])
def test_two_visible_sms_modals_do_not_send_fill_or_click(sms_shape_page, action):
    page = sms_shape_page
    render(page, modal_html() + modal_html())

    with pytest.raises(ValueError):
        if action == "request":
            douyin_sms.request_code(page)
        else:
            douyin_sms.submit_code(page, "123456")

    assert page.evaluate("window.sent") == 0
    assert page.evaluate("window.verified") == 0
    assert page.locator("input").evaluate_all("inputs=>inputs.every(input=>input.value==='')")
    assert_no_publish(page)


def test_missing_modal_verification_never_uses_outside_confirmation(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html())
    page.locator(".verify-submit").evaluate("element=>element.remove()")

    with pytest.raises(ValueError):
        douyin_sms.submit_code(page, "123456")

    assert page.locator("input").input_value() == ""
    assert page.evaluate("window.verified") == 0
    assert_no_publish(page)


def test_short_resend_outside_modal_is_not_used_for_missing_sms_action(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html())
    page.locator(".send-code").evaluate("element=>element.remove()")
    page.locator("main").evaluate("element=>element.insertAdjacentHTML('beforeend', '<button onclick=\"window.sent++\">重新发送</button>')")

    with pytest.raises(ValueError):
        douyin_sms.request_code(page)

    assert page.evaluate("window.sent") == 0
    assert_no_publish(page)


@pytest.mark.parametrize(("portal_class", "inner_class"), [
    (portal, inner)
    for portal in ("modal-root", "modal-container", "modal-wrapper")
    for inner in ("verify-modal", "sms-box-a17b92")
])
def test_missing_modal_verification_cannot_escape_to_shared_portal_confirmation(
        sms_shape_page, portal_class, inner_class):
    page = sms_shape_page
    dialog = modal_html().replace("verify-modal", inner_class)
    render(page, f'<div class="{portal_class}"><button onclick="window.published++">确认</button>'
           + dialog + '</div>')
    page.locator(".verify-submit").evaluate("element=>element.remove()")

    with pytest.raises(ValueError):
        douyin_sms.submit_code(page, "123456")

    assert page.locator("input").input_value() == ""
    assert page.evaluate("window.verified") == 0
    assert_no_publish(page)


def test_nested_label_waits_until_its_parent_verification_button_is_enabled(sms_shape_page):
    page = sms_shape_page
    render(page, modal_html(nested=True))
    page.locator("input").evaluate("""element=>element.oninput=()=>{
        const button=element.closest('.verify-modal').querySelector('.verify-submit');
        setTimeout(()=>button.classList.remove('disabled'),2200);
    }""")

    douyin_sms.submit_code(page, "123456")

    assert page.evaluate("window.verified") == 1
    assert page.locator("input").count() == 0
    assert_no_publish(page)
