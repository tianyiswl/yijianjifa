import pytest
from yijianjifa.webui.douyin_publish_form import validate_form_request, verify_prepared_form


def sample():
    request=validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',declaration='无需添加自主声明')
    raw=dict(identity=dict(uid='123',uid_after='123',status=0,after_status=0,profile_status=0,nickname='测试账号'),
        path='/creator-micro/content/post/video',ready=True,title='中文测试',body='完整简介。',
        declaration='无需添加自主声明',title_count=1,editor_count=1,mode_controls=1,
        final_button_count=1,upload_complete=True,final_button_enabled=True)
    return request,raw


def test_exact_readback_is_prepared_not_published():
    request,raw=sample()
    assert verify_prepared_form(raw,request)==dict(title='中文测试',body='完整简介。',declaration='无需添加自主声明',mode='publish_now',prepared=True,submitted=False)


@pytest.mark.parametrize('key,value', [('title','错位内容'),('body',''),('declaration','内容由AI生成'),
    ('editor_count',2),('mode_controls',0),('final_button_count',2),('upload_complete',False),
    ('final_button_enabled',False),('ready',False),('path','/creator-micro/home')])
def test_partial_or_changed_form_is_rejected(key,value):
    request,raw=sample();raw[key]=value
    with pytest.raises(ValueError):verify_prepared_form(raw,request)


def test_identity_change_blocks_ready_form():
    request,raw=sample();raw['identity']['uid_after']='456'
    with pytest.raises(ValueError):verify_prepared_form(raw,request)


@pytest.mark.parametrize('field,value',[('title',''),('title','字'*31),('body','字'*1001),('declaration',None),('expected_uid','x')])
def test_invalid_request_rejected_before_native_window(field,value):
    data=dict(expected_uid='123',title='测试',body='测试',declaration='无需添加自主声明');data[field]=value
    with pytest.raises(ValueError):validate_form_request(**data)


def test_schedule_window_and_exact_time_readback():
    from datetime import datetime,timezone,timedelta
    now=datetime(2026,9,29,2,40,tzinfo=timezone.utc)
    request,raw=sample()
    for stamp in ('2026-09-29 13:09','2026-10-12 10:41'):
        with pytest.raises(ValueError):validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',declaration='无需添加自主声明',scheduled_time_text=stamp,now=now)
    validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',declaration='',scheduled_time_text='2026-09-29 13:10',now=now)
    request=validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',declaration='无需添加自主声明',scheduled_time_text='2026-09-30 10:40',now=now)
    with pytest.raises(ValueError):verify_prepared_form(raw,request)
    raw['scheduled_time_text']='2026-09-30 10:40'
    assert verify_prepared_form(raw,request)['mode']=='platform_schedule'


@pytest.mark.parametrize('change',['none','wrong_time','duplicate','published','wrong_query','wrong_account','not_ready','wrong_page'])
def test_schedule_receipt_requires_exact_unique_account_bound_result(change):
    from yijianjifa.webui.douyin_scheduled import verified_scheduled_receipt
    request,raw=sample();request['scheduled_time_text']='2026-09-30 10:40'
    raw.update(path='/creator-micro/content/manage',query='中文测试',matches=[dict(time='2026年09月30日 10:40',review='审核中')])
    if change=='wrong_time':raw['matches'][0]['time']='2026年09月29日 10:40'
    if change=='duplicate':raw['matches']*=2
    if change=='published':raw['matches'][0]['review']='已发布'
    if change=='wrong_query':raw['query']='不同标题'
    if change=='not_ready':raw['ready']=False
    if change=='wrong_page':raw['path']='/creator-micro/content/post/video'
    if change=='wrong_account':
        raw['identity']['uid_after']='456'
        with pytest.raises(ValueError):verified_scheduled_receipt(raw,'123',request)
    else:assert verified_scheduled_receipt(raw,'123',request)==(change=='none')


def test_scheduled_receipt_survives_manage_page_window_close():
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_scheduled import ScheduledWindow
    from yijianjifa.features.publish.executor import SubmissionEvidence
    calls=[]
    class View:
        def URL(self):return 'https://creator.douyin.com/creator-micro/content/manage'
        def stopLoading(self):calls.append('stop')
        def setNavigationDelegate_(self,v):pass
        def setUIDelegate_(self,v):pass
    evidence=SubmissionEvidence(True,platform_schedule_confirmation='2026-09-30T12:00:00+08:00')
    w=ScheduledWindow.__new__(ScheduledWindow)
    w.finished=False;w.pending_reply=None;w.session_observer=None
    w.timer=SimpleNamespace(invalidate=lambda:calls.append('timer'))
    w.view=View();w.window=None;w.delegate=None;w.generation=0
    w.value=evidence;w.error=None;w.on_closed=lambda value,error:calls.append((value,error))
    w.closed();w.closed()
    assert calls==['timer','stop',(evidence,None)]
    assert w.finished and w.view is None


def test_approved_scheduled_work_is_still_a_schedule_receipt_not_publication():
    from yijianjifa.webui.douyin_scheduled import verified_scheduled_receipt
    form={'title_text':'测试','scheduled_time_text':'2026-09-30 12:00'}
    raw={'identity':{'uid':'123','uid_after':'123','status':0,'after_status':0,'profile_status':0,'nickname':'测试账号'},
         'path':'/creator-micro/content/manage','ready':True,'query':'测试',
         'matches':[{'time':'2026年09月30日 12:00','review':'定时发布中'}]}
    assert verified_scheduled_receipt(raw,'123',form)
    raw['matches'][0]['review']='已发布'
    assert not verified_scheduled_receipt(raw,'123',form)


def test_topic_tokens_are_required_not_just_visible_hash_text():
    request,raw=sample()
    request=validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',tags='#功能测试 #剪辑测试',declaration='无需添加自主声明')
    raw['body']='完整简介。 \u00a0#功能测试\u00a0  \u00a0#剪辑测试 '
    for found in (None,[],['功能测试'],['功能测试','其他'],['剪辑测试','功能测试']):
        raw['topics']=found
        with pytest.raises(ValueError):verify_prepared_form(raw,request)
    raw['topics']=['功能测试','剪辑测试']
    assert verify_prepared_form(raw,request)['prepared']
    raw['body']='完整简介被替换。 #功能测试 #剪辑测试'
    with pytest.raises(ValueError):verify_prepared_form(raw,request)


def test_login_expiry_after_final_waits_for_readback_and_never_resubmits(monkeypatch):
    import sys,time
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_scheduled import ScheduledWindow,READ_RESULT
    observations=[{'identity':{'status':8,'profile_status':8,'after_status':8},'ready':False}]
    receipt={'identity':{'uid':'123','uid_after':'123','status':0,'after_status':0,'profile_status':0,'nickname':'测试账号'},
             'path':'/creator-micro/content/manage','ready':True,'query':'测试',
             'matches':[{'time':'2026年09月30日 12:00','review':'审核中'}]}
    observations.extend([receipt,receipt,receipt])
    calls=[];titles=[];completed=[]
    class View:
        def callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(self,code,args,frame,world,callback):
            calls.append(code);callback(observations.pop(0),None)
    monkeypatch.setitem(sys.modules,'WebKit',SimpleNamespace(WKContentWorld=SimpleNamespace(defaultClientWorld=lambda:None)))
    w=ScheduledWindow.__new__(ScheduledWindow)
    w.finished=False;w.busy=False;w.view=View();w.generation=0;w.started=time.monotonic()
    w.receipt_confirmed=False;w.phase='result';w.final_attempted=True;w.expected_uid='123';w.previous=None;w.matches=0
    w.session_observer=None;w.session_saved=True;w.reauth_needed=False;w.account_name='测试账号';w.on_expired=lambda:None
    captured=[]
    def capture(on_saved=None):
        captured.append(True);w.session_saved=True;w.reauth_needed=False
        if on_saved:on_saved()
    w.capture_session=capture
    w.form={'title_text':'测试','scheduled_time_text':'2026-09-30 12:00'}
    w.window=SimpleNamespace(setTitle_=titles.append);w.complete=lambda **kw:completed.append(kw)
    w.before_final=lambda:pytest.fail('must never repeat the final action')
    w.observe()
    assert not completed and '登录已失效' in titles[-1] and '不重复提交' in titles[-1]
    w.observe();assert not completed and captured==[True]
    w.observe();assert not completed
    w.observe();assert completed==[{'confirmed':True}]
    assert calls==[READ_RESULT]*4


def test_old_same_copy_does_not_block_new_schedule():
    from yijianjifa.webui.douyin_scheduled import matching_schedule_rows
    form={'scheduled_time_text':'2026-10-04 15:00'}
    assert matching_schedule_rows([{'time':'2026年10月03日 12:00','review':'已发布'}],form)==[]
    row={'time':'2026年10月04日 15:00','review':'审核中'}
    assert matching_schedule_rows([row],form)==[row]

def test_selected_location_requires_exact_readback():
    request,raw=sample()
    location=dict(id='cached-id',name='三千海',address='测试地址')
    request=validate_form_request(expected_uid='123',title='中文测试',body='完整简介。',declaration='无需添加自主声明',location=location)
    with pytest.raises(ValueError):verify_prepared_form(raw,request)
    raw['location']={'name':'三千海','address':'错误地址'}
    with pytest.raises(ValueError):verify_prepared_form(raw,request)
    raw['location']={'name':'三千海','address':'测试地址'}
    assert verify_prepared_form(raw,request)['prepared']
