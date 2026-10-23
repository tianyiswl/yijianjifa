"""Reminder continuation is the same frozen attempt, never a fresh submit."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from yijianjifa.webui import channels_multi_publish as module
from yijianjifa.webui import channels_publish_form as form
from test_channels_publish_form import payload, media, live_form
from test_channels_dom_visibility import offline_browser, offline_page, mount_shadow, REMINDER


@pytest.fixture
def reminder(db_engine, app_paths, payload, media, monkeypatch):
    payload['platform_user_id']='finder-test'
    adapter=module.ChannelsMultiPublishAdapter(db_engine,app_paths)
    raw=live_form(form.validate_form(payload),media);raw['original_intercept']=True
    clicked=[]
    class Control:
        def count(self):return 1
        def is_visible(self):return True
        def is_enabled(self):return True
        def inner_text(self):return '不再提醒'
        def click(self):clicked.append('direct')
    class Page:
        def evaluate(self,script):return raw if script==form.READ_FORM else True
        def locator(self,selector):
            if selector==module.ORIGINAL_REMINDER_PREFERENCE:return SimpleNamespace(count=lambda:0)
            assert selector==module.ORIGINAL_REMINDER_BUTTON
            return Control()
    s=SimpleNamespace(payload=payload,final=True,uploaded=True,phase='result',owned_media=media,
        owned_declaration=None,submit_ok=False,submit_rejected=False,create_pending={},
        create_settled=False,create_ever_started=False,reminder_continued=False,page=Page())
    monkeypatch.setattr(adapter,'_trace',lambda *args:None)
    monkeypatch.setattr(adapter,'_version',lambda *args:True)
    monkeypatch.setattr(adapter,'_identity',lambda *args:('finder-test','测试账号'))
    monkeypatch.setattr(adapter,'_challenge',lambda *args:None)
    return adapter,s,raw,clicked


def test_non_original_reminder_continues_once_without_changing_frozen_schedule(reminder):
    a,s,raw,clicks=reminder;frozen=deepcopy(s.payload)
    assert a._continue_original_reminder(s)
    assert not a._continue_original_reminder(s)
    assert clicks==['direct'];assert s.payload==frozen
    assert raw['original_intercept'] is True


@pytest.mark.parametrize('field,value', [('final',False),('uploaded',False),('submit_ok',True),
    ('submit_rejected',True),('create_pending',{'inflight':0}),('create_settled',True),
    ('create_ever_started',True),('reminder_continued',True)])
def test_an_existing_create_request_or_receipt_never_gets_another_click(reminder,field,value):
    a,s,raw,clicks=reminder;setattr(s,field,value)
    assert not a._continue_original_reminder(s);assert clicks==[]


@pytest.mark.parametrize('field,value',[('time_text','2026-10-11 00:00'),('effective_time',0),
    ('finder_uid','different-account'),('state_original',True),('original_checked',True),
    ('original_dialog',True),('state_caption','changed'),('original_intercept',False)])
def test_reminder_cannot_bypass_schedule_identity_caption_or_original_checks(reminder,field,value):
    a,s,raw,clicks=reminder;raw[field]=value
    assert not a._continue_original_reminder(s);assert clicks==[]


def test_original_requested_or_inspection_never_clicks_direct_publish(reminder):
    a,s,raw,clicks=reminder;s.payload['fields']['channels_original']=True
    assert not a._continue_original_reminder(s)
    s.payload['fields']['channels_original']=False;a.inspection_only=True
    assert not a._continue_original_reminder(s);assert clicks==[]


def test_changed_login_session_cannot_continue_reminder(reminder,monkeypatch):
    a,s,raw,clicks=reminder;monkeypatch.setattr(a,'_version',lambda *args:False)
    assert not a._continue_original_reminder(s);assert clicks==[]


def test_no_more_reminders_is_preferred_then_one_checked_main_click(reminder,monkeypatch):
    a,s,raw,clicks=reminder
    button=SimpleNamespace(count=lambda:1,is_visible=lambda:True,is_enabled=lambda:True,
        inner_text=lambda:'不再提醒',click=lambda:clicks.append('no-tip'))
    s.page.locator=lambda selector:button
    monkeypatch.setattr(a,'_click_final',lambda *args:clicks.append('final'))
    assert a._continue_original_reminder(s);assert clicks==['no-tip']
    assert not a._finish_reminder_preference(s)  # Native report not finished yet.
    assert clicks==['no-tip']
    raw['original_intercept']=False
    assert a._finish_reminder_preference(s);assert clicks==['no-tip','final']
    assert not a._finish_reminder_preference(s);assert clicks==['no-tip','final']


@pytest.mark.parametrize('field,value',[('time_text','2026-10-11 00:00'),('state_caption','changed'),('finder_uid','other')])
def test_preference_close_does_not_grant_a_changed_form(reminder,monkeypatch,field,value):
    a,s,raw,clicks=reminder;s.reminder_preference_pending=True
    raw['original_intercept']=False;raw[field]=value
    monkeypatch.setattr(a,'_click_final',lambda *args:clicks.append('final'))
    assert not a._finish_reminder_preference(s);assert clicks==[]


def test_preference_can_never_start_another_create_request(reminder,monkeypatch):
    a,s,raw,clicks=reminder;s.reminder_preference_pending=True;s.create_ever_started=True
    raw['original_intercept']=False
    monkeypatch.setattr(a,'_click_final',lambda *args:clicks.append('final'))
    assert not a._finish_reminder_preference(s);assert clicks==[]


def test_a_real_captcha_after_preference_close_still_waits_for_the_user(reminder,monkeypatch):
    a,s,raw,clicks=reminder;s.reminder_preference_pending=True;raw['original_intercept']=False
    monkeypatch.setattr(a,'_challenge',lambda *args:'complex')
    monkeypatch.setattr(a,'_click_final',lambda *args:clicks.append('final'))
    assert not a._finish_reminder_preference(s);assert clicks==[]


@pytest.mark.parametrize('surface',['document','wujie'])
def test_native_preference_without_direct_button_closes_before_one_frozen_main_submit(reminder,offline_page,surface,monkeypatch):
    a,s,raw,_=reminder
    frozen=deepcopy(s.payload)
    # Match the observed boundary: exact title/body and preference, no
    # HTML button.cancel-btn. The final button stays on the original form.
    html=REMINDER.replace('class="cancel-btn"','class="native-custom-root"').replace('</div>',
        '<div class="no-tip-btn" onclick="this.closest(\'.weui-desktop-dialog\').remove()">不再提醒</div></div>')
    html+='<div class="post-create"><div class="form-btns"><button>发表</button></div></div>'
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    submissions=[]
    offline_page.expose_function('fixtureSubmit',lambda:submissions.append(deepcopy(s.payload)))
    offline_page.locator(form.FINAL_CONTROL).evaluate("e=>e.addEventListener('click',()=>window.fixtureSubmit())")
    class Page:
        def evaluate(self,script):
            if script==form.READ_FORM:
                raw['original_intercept']=offline_page.locator('.weui-desktop-dialog').count()==1
                return raw
            return offline_page.evaluate(script)
        def locator(self,selector):return offline_page.locator(selector)
    s.page=Page()
    monkeypatch.setattr(a,'_challenge',lambda s:'complex' if offline_page.locator('.weui-desktop-dialog').count() else None)
    assert a._continue_original_reminder(s)
    assert submissions==[] and offline_page.locator('.weui-desktop-dialog').count()==0
    assert not a._continue_original_reminder(s)
    assert a._finish_reminder_preference(s)
    offline_page.wait_for_timeout(50)
    assert submissions==[frozen] and s.payload==frozen
    assert not a._finish_reminder_preference(s)
    assert submissions==[frozen]
