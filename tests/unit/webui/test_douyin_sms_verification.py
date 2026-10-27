"""Original-page SMS verification with synthetic DOM and all network blocked."""
import pytest

from yijianjifa.features.accounts.service import AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
from yijianjifa.webui.chromium_publish_session import LiveSession


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True,channel='chromium')
        try:yield browser
        finally:browser.close()


@pytest.fixture
def sms(browser,db_engine,app_paths):
    context=browser.new_context(service_workers='block')
    context.route('**/*',lambda route:route.abort())
    page=context.new_page()
    page.set_content('''<script>window.sent=0;window.verified=0;window.published=0;</script>
      <button onclick="published++">确认</button><button onclick="published++">定时发布</button>
      <section role="dialog" class="verify-dialog"><h3>接收短信验证码</h3>
        <label>验证码<input placeholder="请输入验证码" oninput="setTimeout(()=>document.querySelector('.verify-submit').classList.remove('disabled'),150)"></label>
        <span class="send-code" onclick="sent++;this.textContent='59s'">获取验证码</span>
        <p class="error"></p><div class="verify-submit disabled" onclick="if(!this.classList.contains('disabled')){verified++;this.closest('section').remove();}">验证</div>
      </section>''')
    adapter=ChromiumMultiPublishAdapter(db_engine,app_paths)
    account=adapter.service.create('douyin','123456789','合成账号')
    ref=adapter.store.put(account.id,b'synthetic-sms-session',versioned=True)
    with session_scope(db_engine) as db:
        db.get(AccountRow,account.id).session_ref=ref
    session=LiveSession({'account_id':account.id,'platform_user_id':account.platform_user_id},
                        {'delivery_id':'fixture-delivery'})
    session.original_ref=ref
    session.context=context;session.page=page;session.final=True;session.uploaded=True
    adapter.sessions['fixture']=session
    try:yield adapter,session,page
    finally:context.close()


def test_sms_request_is_sent_once_on_the_original_page(sms):
    adapter,session,page=sms
    adapter._park(session,'sms')
    adapter._park(session,'sms')
    assert page.evaluate('sent')==1
    assert page.evaluate('published')==0
    assert session.uploaded and session.final


def test_auto_sms_exposes_cooldown_and_rejects_early_resend_on_backend(sms):
    adapter,session,page=sms
    adapter._park(session,'sms')
    request=adapter.interventions()[0]
    assert request.get('sms_retry_after')==60
    with pytest.raises(ValueError,match='60|间隔|等待'):
        adapter.respond(request['id'],'resend_code')
    assert page.evaluate('sent')==1


def test_resend_after_sixty_seconds_uses_same_page_and_never_republishes(sms):
    adapter,session,page=sms;now=[1000.0];adapter.sms_cooldown.clock=lambda:now[0]
    adapter._park(session,'sms');request=adapter.interventions()[0]
    now[0]=1059.99
    with pytest.raises(ValueError):adapter.respond(request['id'],'resend_code')
    now[0]=1060.0;page.locator('.send-code').evaluate("e=>e.textContent='重新获取验证码'")
    accepted=adapter.respond(request['id'],'resend_code')
    result=adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    assert result=={'accepted':True}
    assert page.evaluate('sent')==2 and page.evaluate('published')==0
    assert adapter.interventions()[0]['sms_retry_after']==60


def test_first_automatic_request_waits_for_previous_task_cooldown_then_sends_once(sms):
    adapter,session,page=sms;now=[1000.0];adapter.sms_cooldown.clock=lambda:now[0]
    adapter.sms_cooldown.reserve(session.payload['account_id']);adapter._thread()
    adapter._park(session,'sms')
    adapter.refresh_interventions()
    assert page.evaluate('sent')==0
    now[0]=1060.0;adapter.refresh_interventions();adapter.refresh_interventions()
    assert page.evaluate('sent')==1 and page.evaluate('published')==0


@pytest.mark.parametrize('lost_reply',[False,True])
def test_sixty_seconds_start_after_the_request_finishes_even_if_click_reply_is_lost(sms,monkeypatch,lost_reply):
    from yijianjifa.webui import douyin_sms
    adapter,session,page=sms;now=[1000.0];adapter.sms_cooldown.clock=lambda:now[0]
    original=douyin_sms.request_code
    def delayed(surface,*,before_send):
        def become_ready():before_send();now[0]+=2
        result=original(surface,before_send=become_ready)
        if lost_reply:raise TimeoutError('synthetic click result unknown')
        return result
    monkeypatch.setattr(douyin_sms,'request_code',delayed)
    adapter._park(session,'sms')
    assert page.evaluate('sent')==1
    assert adapter.interventions()[0]['sms_retry_after']==60


def test_clicking_sms_in_embedded_picture_cannot_bypass_the_cooldown(sms):
    adapter,session,page=sms
    adapter._park(session,'sms');request=adapter.interventions()[0]
    # A platform that visually enables its control early still cannot send.
    page.locator('.send-code').evaluate("e=>e.textContent='获取验证码'")
    session.broker.open_view(request['id'])
    rect=page.locator('.send-code').bounding_box();point=[rect['x']+rect['width']/2,rect['y']+rect['height']/2]
    accepted=adapter.respond(request['id'],'pointer',point)
    adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    assert page.evaluate('sent')==1 and page.evaluate('published')==0


@pytest.mark.parametrize('label',['29s','29秒后重新获取'])
def test_already_running_countdown_does_not_send_another_sms(sms,label):
    adapter,session,page=sms
    page.locator('.send-code').evaluate('(e,label)=>e.textContent=label',label)
    adapter._park(session,'sms')
    assert page.evaluate('sent')==0


def test_post_final_code_is_processed_without_replaying_publish(sms):
    adapter,session,page=sms
    adapter._park(session,'sms')
    request=adapter.interventions()[0]
    accepted=adapter.respond(request['id'],'code','123456')
    result=adapter.process_verification_view('fixture-delivery',request['id'],accepted.get('view_revision'))
    assert page.evaluate('verified')==1
    assert page.evaluate('published')==0
    assert result.get('verification_complete') is True
    assert session.page is page and session.uploaded and session.final


def test_wrong_code_reports_platform_rejection_without_leaking_code(sms):
    adapter,session,page=sms
    page.locator('.verify-submit').evaluate("e=>e.onclick=()=>document.querySelector('.error').textContent='验证码错误，请重新输入'")
    adapter._park(session,'sms')
    request=adapter.interventions()[0]
    accepted=adapter.respond(request['id'],'code','654321')
    result=adapter.process_verification_view('fixture-delivery',request['id'],accepted.get('view_revision'))
    assert result.get('verification_complete') is not True
    status=adapter.interventions()[0]
    assert status.get('code_state')=='failed'
    assert '验证码' in status.get('code_message','')
    assert '654321' not in repr(status)
    assert page.evaluate('published')==0


def test_ambiguous_sms_dialog_never_sends_or_fills(sms):
    adapter,session,page=sms
    page.locator('section').evaluate('e=>e.after(e.cloneNode(true))')
    adapter._park(session,'sms')
    assert page.evaluate('sent')==0
    request=adapter.interventions()[0]
    accepted=adapter.respond(request['id'],'code','123456')
    adapter.process_verification_view('fixture-delivery',request['id'],accepted.get('view_revision'))
    assert page.locator('input').evaluate_all('es=>es.every(e=>!e.value)')
    assert page.evaluate('published')==0


def test_platform_response_can_enable_and_complete_asynchronously(sms):
    adapter,session,page=sms
    page.locator('.verify-submit').evaluate("e=>e.onclick=()=>setTimeout(()=>{window.verified++;e.closest('section').remove();},500)")
    adapter._park(session,'sms')
    request=adapter.interventions()[0]
    accepted=adapter.respond(request['id'],'code','123456')
    result=adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    assert result['verification_complete'] is True
    assert page.evaluate('published')==0


def test_delayed_wrong_code_feedback_keeps_the_original_page(sms):
    adapter,session,page=sms
    page.locator('.verify-submit').evaluate("e=>e.onclick=()=>setTimeout(()=>document.querySelector('.error').textContent='验证码已过期',500)")
    adapter._park(session,'sms')
    request=adapter.interventions()[0]
    accepted=adapter.respond(request['id'],'code','123456')
    result=adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    assert result['verification_complete'] is False
    assert adapter.interventions()[0]['code_state']=='failed'
    assert session.page is page and page.evaluate('published')==0


@pytest.mark.parametrize('action',['code','resend_code'])
def test_fixed_control_failures_explain_missing_controls_instead_of_wrong_code(sms,monkeypatch,action):
    from yijianjifa.webui.douyin_sms import SMSControlError
    adapter,session,page=sms;now=[1000.0];adapter.sms_cooldown.clock=lambda:now[0]
    adapter._park(session,'sms');request=adapter.interventions()[0];now[0]=1060.0
    message='未找到完整的短信验证窗口及验证按钮，请打开验证页面查看。'
    def fail(*args):raise SMSControlError(message)
    monkeypatch.setattr(adapter,'_submit_verification_code' if action=='code' else '_request_verification_sms',fail)
    accepted=adapter.respond(request['id'],action,'123456' if action=='code' else '')
    adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    latest=adapter.interventions()[0]
    assert latest['code_message' if action=='code' else 'sms_message']==message
    assert page.evaluate('published')==0 and session.page is page


def test_raw_input_errors_never_reach_the_verification_feedback(sms,monkeypatch):
    adapter,session,page=sms
    adapter._park(session,'sms');request=adapter.interventions()[0]
    def fail(*args):raise ValueError('sensitive synthetic code 123456 in raw input error')
    monkeypatch.setattr(adapter,'_submit_verification_code',fail)
    accepted=adapter.respond(request['id'],'code','123456')
    adapter.process_verification_view('fixture-delivery',request['id'],accepted['view_revision'])
    latest=adapter.interventions()[0]
    assert latest['code_state']=='failed'
    assert '123456' not in repr(latest) and 'sensitive' not in repr(latest)
    assert page.evaluate('published')==0


def test_first_request_waits_for_a_slow_but_eventually_clickable_platform_control(sms):
    adapter,session,page=sms
    page.locator('.send-code').evaluate("""element=>{
        element.style.pointerEvents='none';
        setTimeout(()=>element.style.pointerEvents='auto',3500);
    }""")
    adapter._park(session,'sms')
    assert page.evaluate('sent')==1
    assert adapter.interventions()[0]['sms_state']=='sent'
    assert page.evaluate('published')==0
