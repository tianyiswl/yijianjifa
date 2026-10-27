"""Offline posting-shell readiness and private-data-free failure diagnostics."""
import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from yijianjifa.webui import bilibili_multi_publish as m
from test_bilibili_multi_publish import case


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
    context.route('**/*',lambda route:route.abort())
    page=context.new_page()
    try:yield page
    finally:context.close()


@pytest.mark.parametrize('html',[
    '<main>合成投稿页外壳</main>',
    '<main><micro-app><div>合成投稿页外壳</div></micro-app></main>',
])
def test_upload_shell_is_unready_without_python_or_javascript_failure(case,offline_page,monkeypatch,html):
    adapter,payload,_,events,_=case
    offline_page.set_content(html)
    # Bili reads elements selected from DOM, not Vue children's $el. A Vue
    # comment node attached to a shell must not become a geometry observation.
    offline_page.evaluate("""() => {
        const element=document.querySelector('.creation-statement-container');
        if(element)element.__vue__={$children:[{$el:document.createComment('owned fixture')}]};
    }""")
    raw=offline_page.evaluate(m.form_contract.READ_FORM)
    assert raw['upload_complete'] is False and raw['videos']==[] and raw['title_count']==0
    assert m.BilibiliMultiPublishAdapter._frame(adapter,SimpleNamespace(page=offline_page)) is offline_page
    monkeypatch.setattr(adapter,'_frame',lambda s:offline_page)
    monkeypatch.setattr(adapter,'_upload',m.BilibiliMultiPublishAdapter._upload.__get__(adapter))
    monkeypatch.setattr(adapter,'_prepare_form',m.BilibiliMultiPublishAdapter._prepare_form.__get__(adapter))
    # The contract under test is which stage times out, not SQLite/CPU speed.
    # Advance the adapter clock only at its existing browser-wait boundaries.
    clock=[0.0]
    monkeypatch.setattr(m,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
    monkeypatch.setattr(case[2],'wait_for_timeout',lambda n:clock.__setitem__(0,clock[0]+n/1000))
    adapter.step_timeout=.35
    outcome=adapter.submit(payload,context=None,before_final=lambda:pytest.fail('shell grants no final authority'))
    assert outcome.state=='pre_submit_failed' and outcome.error_code=='adapter_failed'
    assert outcome.error_stage=='upload_preparation' and outcome.error_category=='timeout'
    session=adapter._session(payload)
    assert session.uploaded is False and session.final is False
    assert not any(name in {'upload','journal','final'} for name,_ in events)
    assert adapter.preparation_diagnostic()['error_category']=='timeout'
    adapter.close()


def test_same_shell_can_later_supply_a_complete_observed_form(case,offline_page):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    adapter._init_observations(s)
    f=m.form_contract.validate_form(payload)
    offline_page.set_content('<main>合成外壳</main>')
    assert m.BilibiliMultiPublishAdapter._prepare_form(adapter,s,offline_page,f) is False
    offline_page.set_content('''
        <div class="video-title"><input></div>
        <div class="desc-container"><div class="archive-info-editor"><div class="ql-editor" contenteditable="true"></div></div></div>
        <div id="tag-container"><div class="tag-pre-wrp"></div></div>
        <div class="video-human-type"><span class="select-item-cont"></span></div>
        <div class="creation-statement-container"><input class="bcc-select-input-inner"></div>
        <div class="file-item-content-status-text"><span class="success">上传完成</span></div>
        <div class="video-basic"><div class="form"><div class="time-container"><div class="switch-container switch-container-active">定时</div>
            <div class="date-picker-date"><div class="date-show"></div></div>
            <div class="date-picker-timer"><div class="date-show"></div></div><div class="d-time-container">时间</div></div></div></div>
        <div class="submit-container"><button class="submit-add">立即投稿</button></div>
    ''')
    offline_page.evaluate('''f => {
        document.querySelector('.video-title input').value=f.title;
        document.querySelector('.ql-editor').innerText=f.body;
        for(const tag of f.tags){const e=document.createElement('span');e.className='label-item-v2-content';e.innerText=tag;document.querySelector('.tag-pre-wrp').append(e);}
        document.querySelector('.select-item-cont').innerText=f.category_name;
        const element=document.querySelector('.creation-statement-container');
        element.querySelector('input').value=f.creation_statement_name;
        element.__vue__={$store:{state:{task_owned:{human_type2:f.category_id,creationStatementId:f.creation_statement_id,
            copyright:f.copyright,mainCover:{main:{origin:'data:image/jpeg;base64,Zg=='}}}}},
            $taskConfig:{currentTaskId:'owned',tasks:[{id:'owned',archive:{dtime:f.dtime},videoTasks:[{filename:'owned-fixture',cid:24,status:3}]}]}};
        document.querySelector('.date-picker-date .date-show').innerText=f.date;
        document.querySelector('.date-picker-timer .date-show').innerText=f.clock;
        document.querySelector('.d-time-container').__vue__={timezone:'Asia/Shanghai',$parent:{$options:{name:'TimeRelease'},dtime:f.dtime}};
        element.__vue__.$taskConfig.tasks[0].archive.dtime=0;
    }''',f)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert m.form_contract.verify_form(observed,f)
    s.form_filled=True;s.cover_ready=True
    assert m.BilibiliMultiPublishAdapter._prepare_form(adapter,s,offline_page,f) is True
    assert s.final is False
    adapter.close()


@pytest.mark.parametrize('stage,error,category',[
    ('upload_preparation',ValueError('existing B站 draft must be handled first'),'existing_draft'),
    ('schedule_preparation',ValueError('B站时分控件发生变化'),'form_changed'),
    ('form_preparation',ValueError('frozen video changed before cover'),'resource_unavailable'),
    ('form_preparation',AttributeError('private-document-value'),'execution_failed'),
])
def test_finite_failure_diagnostic_is_consumed_and_never_relabels_final(case,monkeypatch,stage,error,category):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    def fail(*args,**kwargs):
        adapter._preparation_stage=stage
        raise error
    monkeypatch.setattr(adapter,'_advance_steps',fail)
    with pytest.raises(type(error)) as caught:adapter._advance(s)
    assert caught.value is error
    assert adapter.failure_diagnostic(error)=={'error_stage':stage,'error_category':category}
    assert adapter.failure_diagnostic(error)=={}
    assert 'private-document-value' not in json.dumps(adapter.preparation_diagnostic())
    s.final=True
    with pytest.raises(type(error)):adapter._advance(s,query=True)
    assert adapter.failure_diagnostic(error)=={} and adapter.preparation_diagnostic()=={}
    adapter.close()


@pytest.mark.parametrize('enabled',[False,True])
def test_source_diagnostic_is_opt_in_and_never_contains_private_exception_or_page_values(case,monkeypatch,caplog,enabled):
    from playwright.sync_api import Error
    adapter,payload,page,_,_=case
    secret='PRIVATE_UID_URL_COOKIE_TITLE'
    if enabled:monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS','1')
    else:monkeypatch.delenv('P011_PUBLISH_DIAGNOSTICS',raising=False)
    def evaluate(script):raise Error('Page.evaluate: TypeError: getClientRects is not a function '+secret)
    page.evaluate=evaluate
    # Keep strict identity reads isolated; exercise actual adapter _upload so
    # the traceback must identify the exact owned Python call to JS.
    monkeypatch.setattr(adapter,'_identity',lambda s:('111','合成账号'))
    monkeypatch.setattr(adapter,'_upload',m.BilibiliMultiPublishAdapter._upload.__get__(adapter))
    with caplog.at_level('WARNING'):
        with pytest.raises(Error):adapter.submit(payload,context=None,before_final=lambda:pytest.fail('no final on failed read'))
    detail=adapter.preparation_diagnostic()
    assert detail['error_type']=='Error' and detail['js_kind']=='node_geometry_unavailable'
    assert detail['error_stage']=='upload_preparation' and detail['error_category']=='javascript_read_failed'
    assert [frame['method'] for frame in detail['frames']][-2:]==['_advance_steps','_upload']
    assert all(set(frame)=={'source','method','line'} and frame['source']=='bilibili_multi_publish.py'
               and type(frame['line']) is int and frame['line']>0 for frame in detail['frames'])
    serialized=json.dumps(detail,ensure_ascii=False)
    assert secret not in serialized and str(payload['video_path']) not in serialized
    assert secret not in caplog.text and ('bilibili preparation diagnostic:' in caplog.text) is enabled
    # Callers cannot rewrite the retained metadata through the getter.
    detail['frames'].clear()
    assert adapter.preparation_diagnostic()['frames']
    assert adapter.failure_diagnostic(Error(''))=={
        'error_stage':'upload_preparation','error_category':'javascript_read_failed'}
    adapter.close()


def test_existing_draft_is_identified_from_real_dom_and_remains_blocked(case,offline_page,monkeypatch):
    adapter,payload,_,events,_=case
    offline_page.set_content('<div class="video-title"><input value="合成已有草稿"></div>')
    monkeypatch.setattr(adapter,'_frame',lambda s:offline_page)
    monkeypatch.setattr(adapter,'_upload',m.BilibiliMultiPublishAdapter._upload.__get__(adapter))
    with pytest.raises(ValueError,match='existing B站 draft must be handled first') as caught:
        adapter.submit(payload,context=None,before_final=lambda:pytest.fail('draft grants no final authority'))
    assert adapter.failure_diagnostic(caught.value)=={'error_stage':'upload_preparation','error_category':'existing_draft'}
    assert not any(name in {'upload','journal','final'} for name,_ in events)
    assert adapter._session(payload).final is False
    adapter.close()


def native_new_upload_entrance(page,*,state=None,tasks=None,current_id=None,prompt=True):
    # Fulfilled locally under a synthetic origin solely to exercise its own
    # localStorage. The context's catch-all abort still rejects all traffic.
    markup='''<div class="video-entrance">
        <div class="entrance-tip">本地浏览器存在1个未提交的视频
            <button id="continue-record">继续编辑</button><button id="discard-record">不用了</button></div>
        <div class="upload-wrp"><div class="upload"><input type="file" accept=".mp4"></div></div></div>'''
    page.context.route('https://owned-new-upload.invalid/',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=markup))
    page.goto('https://owned-new-upload.invalid/')
    actual={'isInteractive':False,'isEditMode':False,'isEditDraftMode':False,'from':{}}
    if state is not None:actual.update(state)
    page.evaluate('''value => {
        const entrance=document.querySelector('.video-entrance');
        window.ownedConfig={currentTaskId:value.current_id,tasks:value.tasks};
        entrance.__vue__={uploadDisabled:false,$store:{state:value.state},$taskConfig:window.ownedConfig,$children:[{$el:document.createComment('owned fixture')}]};
        document.querySelector('.upload-wrp .upload').__vue__={disabled:false};
        window.ownedActions=[];
        if(!value.prompt)document.querySelector('.entrance-tip').remove();
        localStorage.setItem('bili_videoup_record',JSON.stringify({data:[{id:'owned-old',title:'合成旧稿',videos:[{filename:'owned-old-file',cid:1}]}]}));
        document.querySelector('#continue-record')?.addEventListener('click',()=>{window.ownedActions.push('continue');throw Error('never continue fixture record');});
        document.querySelector('#discard-record')?.addEventListener('click',()=>{window.ownedActions.push('discard');localStorage.removeItem('bili_videoup_record');});
        document.querySelector('input[type=file]').addEventListener('change',event=>{
            window.ownedActions.push('new-upload');
            window.ownedConfig.currentTaskId='owned-new';
            window.ownedConfig.tasks.push({id:'owned-new',archive:{},videoTasks:[{filename:event.target.files[0].name,status:0}]});
            // Model the ordinary RecordConfig's keyed append: its complete
            // storage string changes, while the previous record is retained.
            const records=JSON.parse(localStorage.getItem('bili_videoup_record'));
            records.data.push({id:'owned-new',videos:[]});
            localStorage.setItem('bili_videoup_record',JSON.stringify(records));
        });
    }''',{'state':actual,'tasks':[] if tasks is None else tasks,'current_id':current_id,'prompt':prompt})


@pytest.mark.parametrize('prompt',[False,True])
def test_native_ordinary_upload_creates_new_task_without_continuing_or_erasing_local_record(case,offline_page,prompt):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page,prompt=prompt)
    before=offline_page.evaluate("JSON.stringify(JSON.parse(localStorage.getItem('bili_videoup_record')).data[0])")
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['existing_draft'] is prompt
    assert observed['new_upload_state']=='ready' and observed['new_upload_ready'] is True
    assert m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page) is True
    assert s.uploaded is True and s.final is False
    assert offline_page.evaluate('window.ownedActions')==['new-upload']
    assert offline_page.evaluate("JSON.stringify(JSON.parse(localStorage.getItem('bili_videoup_record')).data[0])")==before
    assert offline_page.evaluate("JSON.parse(localStorage.getItem('bili_videoup_record')).data.map(record=>record.id)")==['owned-old','owned-new']
    assert offline_page.evaluate('window.ownedConfig.tasks.map(task=>task.id)')==['owned-new']
    assert adapter._upload_entry_kind=='ready'
    adapter.close()


@pytest.mark.parametrize('change',[
    {'isInteractive':True},{'isEditMode':True},{'isEditDraftMode':True},
    {'from':{'type':'edit'}},{'from':{'type':'draft'}},{'from':{'page':'interactive'}},
    {'from':{'aid':1}},{'from':{'bvid':'owned-id'}},{'from':{'draftId':1}},
    {'from':{'type':'highlight-slice'}},{'from':{'cid':1,'filename':'owned-import'}},
])
def test_local_record_prompt_cannot_bypass_interactive_or_any_editor(case,offline_page,change):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page,state=change)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['new_upload_state']=='unsupported_mode' and observed['new_upload_ready'] is False
    with pytest.raises(ValueError,match='existing B站 draft must be handled first'):
        m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page)
    assert not s.uploaded and not s.final and offline_page.evaluate('window.ownedActions')==[]
    adapter.close()


@pytest.mark.parametrize('shape',['uninitialized','missing-flag','missing-from','missing-config','missing-task-list','ambiguous-entrance'])
def test_prompt_with_unknown_or_ambiguous_native_state_waits_without_upload(case,offline_page,shape):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page)
    offline_page.evaluate('''shape => {
        const entrance=document.querySelector('.video-entrance'),vm=entrance.__vue__;
        if(shape==='uninitialized')delete entrance.__vue__;
        if(shape==='missing-flag')delete vm.$store.state.isEditDraftMode;
        if(shape==='missing-from')delete vm.$store.state.from;
        if(shape==='missing-config')delete vm.$taskConfig;
        if(shape==='missing-task-list')delete vm.$taskConfig.tasks;
        if(shape==='ambiguous-entrance'){const other=document.createElement('div');other.className='video-entrance';other.innerText='合成重复入口';document.body.append(other);}
    }''',shape)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['new_upload_state']=='unavailable' and observed['new_upload_ready'] is False
    assert m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page) is False
    assert not s.uploaded and not s.final and offline_page.evaluate('window.ownedActions')==[]
    adapter.close()


@pytest.mark.parametrize('shape',['entrance-disabled','uploader-disabled','disabled-class','unknown-entrance','unknown-uploader'])
def test_native_disabled_or_unknown_uploader_never_selects_file_despite_enabled_input(case,offline_page,shape):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page)
    offline_page.evaluate('''shape => {
        const entrance=document.querySelector('.video-entrance'),upload=entrance.querySelector('.upload');
        if(shape==='entrance-disabled')entrance.__vue__.uploadDisabled=true;
        if(shape==='uploader-disabled')upload.__vue__.disabled=true;
        if(shape==='disabled-class')upload.classList.add('bcc-upload-disabled');
        if(shape==='unknown-entrance')delete entrance.__vue__.uploadDisabled;
        if(shape==='unknown-uploader')delete upload.__vue__.disabled;
    }''',shape)
    # The official BccUpload input has no disabled attribute: its parent
    # handles ordinary clicks. set_input_files alone bypasses that native gate.
    assert offline_page.locator('input[type=file]').is_disabled() is False
    assert m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page) is False
    assert not s.uploaded and not s.final
    assert offline_page.evaluate('window.ownedActions')==[]
    assert offline_page.locator('input[type=file]').evaluate('(e)=>e.files.length')==0
    adapter.close()


def test_selected_file_without_native_task_is_reported_once_and_never_reselected_or_submitted(case,offline_page,monkeypatch):
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    adapter._open(s)
    native_new_upload_entrance(offline_page)
    # Model a native change handler that did not create a platform task. This
    # is an observation failure, never permission to choose the file again.
    offline_page.evaluate('''() => {
        const old=document.querySelector('input[type=file]'),input=old.cloneNode();old.replaceWith(input);
        input.addEventListener('change',()=>window.ownedActions.push('selected_without_task'));
    }''')
    s.page=offline_page;s.phase='prepare';adapter.step_timeout=.5
    monkeypatch.setattr(adapter,'_frame',lambda s:offline_page)
    monkeypatch.setattr(adapter,'_identity',lambda s:('111','合成账号'))
    monkeypatch.setattr(adapter,'_upload',m.BilibiliMultiPublishAdapter._upload.__get__(adapter))
    monkeypatch.setattr(adapter,'_prepare_form',m.BilibiliMultiPublishAdapter._prepare_form.__get__(adapter))
    result=adapter.submit(payload,context=None,before_final=lambda:pytest.fail('unaccepted file grants no final'))
    assert result.state=='pre_submit_failed' and result.error_code=='adapter_failed'
    assert result.error_stage=='upload_preparation' and result.error_category=='timeout'
    assert s.uploaded and not s.final and offline_page.evaluate('window.ownedActions')==['selected_without_task']
    with session_scope(adapter.engine) as db:
        events=json.loads(db.get(Setting,'publish.execution:target:1').value_json)
    observations=[e['checks'] for e in events if e['step']=='bilibili.upload_observation']
    assert any(e['file_selected'] is True and e['native_task_present'] is False for e in observations)
    timeout=next(e['checks'] for e in events if e['step']=='bilibili.preparation_timeout')
    assert timeout['stage']=='upload_preparation' and timeout['file_selected'] is True
    assert all(type(value) in {bool,int,str} for value in timeout.values())
    assert str(payload['video_path']) not in json.dumps(observations) and '111' not in json.dumps(observations)
    adapter.close()


def test_entry_waits_for_native_enablement_then_selects_exactly_once(case,offline_page):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page)
    offline_page.evaluate('''() => {
        document.querySelector('.video-entrance').__vue__.uploadDisabled=true;
        document.querySelector('.upload-wrp .upload').__vue__.disabled=true;
    }''')
    assert m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page) is False
    assert offline_page.evaluate('window.ownedActions')==[]
    offline_page.evaluate('''() => {
        document.querySelector('.video-entrance').__vue__.uploadDisabled=false;
        document.querySelector('.upload-wrp .upload').__vue__.disabled=false;
    }''')
    assert m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page) is True
    assert s.file_selection_returned and offline_page.evaluate('window.ownedActions')==['new-upload']
    assert not s.final
    adapter.close()


def test_normal_bilibili_deadline_keeps_specific_reason_through_real_saved_delivery_and_daily_panel(case,app_paths,monkeypatch):
    from yijianjifa.app_services.mix_execution import read_execution
    from yijianjifa.app_services.multi_publish import MultiPublishService
    from yijianjifa.app_services.multi_publish_dto import detail
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.features.mixing.queue import MixQueue,MixTask
    from yijianjifa.features.publish.coordinator import PublishCoordinator
    from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    adapter,payload,_,actions,_=case;adapter.step_timeout=.15
    video=FinishedVideoService(adapter.engine).import_external(payload['video_path'],duration_ms=1000,title='离线原因管道')
    service=MultiPublishService(adapter.engine,app_paths,catalog=PlatformCatalog(transports={'bilibili':adapter}))
    service.capture_account=lambda account:{'session_fingerprint':m.fingerprint(account)}
    prepared=service.prepare(request_id='bili-normal-timeout',video_ids=[video.id],
        targets=[{'account_id':payload['account_id'],'platform':'bilibili'}],common_fields=payload['fields'],
        execution_mode=payload['execution_mode'],scheduled_at=payload['scheduled_at'])
    child=service.save(prepared['prepared_id'],request_id='bili-normal-timeout')['id']
    def wait_for_native_upload(s,frame,f):
        adapter._preparation_stage='upload_preparation';return False
    monkeypatch.setattr(adapter,'_prepare_form',wait_for_native_upload)
    coordinator=PublishCoordinator(service)
    assert coordinator.run_once(child)=='pre_submit_failed'
    target=detail(service,child,coordinator=coordinator)['deliveries'][0]
    assert target['error_code']=='adapter_failed' and target['error_stage']=='upload_preparation'
    assert target['error_category']=='timeout' and not target['final_started']
    assert '视频上传准备失败' in target['reason_message'] and '超时' in target['reason_message'] and '尚未提交' in target['reason_message']
    parent=MixQueue(adapter.engine,SimpleNamespace(render_item=lambda *args:pytest.fail('no rendering'))).enqueue(
        {'quantity':1,'daily_publish':{'targets':[{'account_id':payload['account_id'],'platform':'bilibili'}]}})
    with session_scope(adapter.engine) as db:
        db.get(MixTask,parent.id).state='completed'
        db.add(Setting(key='mix.daily.status:'+parent.id,value_json=json.dumps({'publish_task_ids':[child]})))
        db.add(Setting(key='multi_publish.daily_source:'+child,value_json=json.dumps({'mix_task_id':parent.id})))
    shown=read_execution(adapter.engine,parent.id)['publishing']['targets'][0]
    assert shown['reason']==target['reason_message'] and shown['state']=='pre_submit_failed'
    assert coordinator.run_once(child) is None and not any(name in {'journal','final'} for name,_ in actions)
    adapter.close()


@pytest.mark.parametrize('final,query,cancelled,state,code',[
    (True,False,False,'result_unknown','result_unknown'),
    (True,True,False,'result_unknown','result_unknown'),
    (False,True,False,'pre_submit_failed','timeout'),
    (False,False,True,'pre_submit_failed','timeout'),
])
def test_normal_timeout_diagnostic_never_relabels_query_final_or_cancellation(case,final,query,cancelled,state,code):
    adapter,payload,_,actions,_=case;s=adapter._session(payload)
    s.final=final;s.phase='result' if final else 'prepare'
    adapter.step_timeout=-.001
    if cancelled:adapter.cancel.set()
    result=adapter._advance(s,query=query,before_final=lambda:pytest.fail('no final during expired observation'))
    assert (result.state,result.error_code)==(state,code)
    assert result.error_stage is None and result.error_category is None
    assert adapter.preparation_diagnostic()=={}
    assert not any(name in {'upload','journal','final'} for name,_ in actions)
    adapter.close()


@pytest.mark.parametrize('shape',['restored-task','title','empty-editor','creation-statement','ambiguous-creation-statement'])
def test_prompt_exception_never_overwrites_active_or_restored_form(case,offline_page,shape):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page)
    offline_page.evaluate('''shape => {
        if(shape==='restored-task'){window.ownedConfig.tasks.push({id:'owned-active',archive:{},videoTasks:[]});window.ownedConfig.currentTaskId='owned-active';}
        else {const e=document.createElement('div');
          e.innerHTML=shape==='title'?'<div class="video-title"><input value="合成活动稿件"></div>':
            shape==='empty-editor'?'<div class="desc-container"><div class="archive-info-editor"><div class="ql-editor" contenteditable="true">合成简介</div></div></div>':
            '<div class="creation-statement-container">合成声明</div>';
          document.body.append(e);}
        if(shape==='ambiguous-creation-statement')document.body.append(document.querySelector('.creation-statement-container').cloneNode(true));
    }''',shape)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['new_upload_state']=='active_form' and observed['new_upload_ready'] is False
    with pytest.raises(ValueError,match='existing B站 draft must be handled first'):
        m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page)
    assert not s.uploaded and not s.final and offline_page.evaluate('window.ownedActions')==[]
    adapter.close()


@pytest.mark.parametrize('route',[
    '/?type=edit','/?type=draft','/?page=interactive','/?aid=1','/?bvid=owned','/?draftId=1',
    '/?type=highlight-slice','/?cid=1&filename=owned-import',
    '/platform/upload/video/interactive',
])
def test_url_editor_source_cannot_grant_new_upload_before_vue_mode_flags_change(case,offline_page,route):
    adapter,payload,_,_,_=case;s=adapter._session(payload)
    native_new_upload_entrance(offline_page)
    offline_page.evaluate('route => history.replaceState({},"",route)',route)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['new_upload_state']=='unsupported_mode' and observed['new_upload_ready'] is False
    with pytest.raises(ValueError):m.BilibiliMultiPublishAdapter._upload(adapter,s,offline_page)
    assert not s.uploaded and not s.final and offline_page.evaluate('window.ownedActions')==[]
    adapter.close()


# From official 607.82adef05.css: the enter frame has scaleY(0), followed by
# a 0.2s transform transition. The real-component probe is separately recorded
# in build/bilibili-public-catalog-20261009/statement-transition-observation.json.
STATEMENT_TRANSITION_STYLE='''
.bcc-select-zoom-in-top-enter-active{opacity:1;transform-origin:top;transition:transform .2s cubic-bezier(.23,1,.32,1),opacity .2s cubic-bezier(.23,1,.32,1);transform:scaleY(1)}
.bcc-select-zoom-in-top-enter{opacity:0;transform:scaleY(0)}
.bcc-option{height:36px;line-height:36px}
'''


def statement_menu(page,f,*,kind='normal',initial_id=None,initial_text=''):
    page.set_content('<style>'+STATEMENT_TRANSITION_STYLE+'''</style>
        <div class="creation-statement-container"><input class="bcc-select-input-inner" readonly>
            <ul class="bcc-select-list-wrap" style="display:none"><li class="bcc-option"><span></span></li></ul></div>''')
    page.evaluate('''value=>{
        const element=document.querySelector('.creation-statement-container'),input=element.querySelector('input'),menu=element.querySelector('ul');
        window.ownedStatement={creationStatementId:value.initial_id,copyright:3};
        element.__vue__={$store:{state:{task_owned:window.ownedStatement}},$taskConfig:{currentTaskId:'owned',tasks:[{id:'owned',archive:{},videoTasks:[]}]}};
        input.value=value.initial_text;window.ownedMenuOpens=0;window.ownedStatementClicks=0;window.ownedInitialMenuHeight=null;
        const option=menu.querySelector('li');option.querySelector('span').innerText=value.kind==='missing'?'合成其他选项':value.label;
        if(value.kind==='duplicate')menu.append(option.cloneNode(true));
        if(value.kind==='disabled')option.classList.add('is-disabled');
        input.addEventListener('click',()=>{
            window.ownedMenuOpens++;
            if(value.kind==='hidden')return;
            menu.style.display='block';menu.classList.add('bcc-select-zoom-in-top-enter-active','bcc-select-zoom-in-top-enter');
            window.ownedInitialMenuHeight=menu.getBoundingClientRect().height;
            requestAnimationFrame(()=>requestAnimationFrame(()=>menu.classList.remove('bcc-select-zoom-in-top-enter')));
        });
        for(const item of menu.querySelectorAll('li'))item.addEventListener('click',()=>{
            if(item.classList.contains('is-disabled'))return;
            window.ownedStatementClicks++;
            window.ownedStatement.creationStatementId=value.kind==='wrong-state'?42:value.id;
            input.value=value.label;menu.style.display='none';
        });
    }''',{'kind':kind,'initial_id':initial_id,'initial_text':initial_text,'id':f['creation_statement_id'],'label':f['creation_statement_name']})


def test_statement_transition_waits_for_one_native_option_and_reads_exact_selected_id(case,offline_page):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=5
    f=m.form_contract.validate_form(payload);statement_menu(offline_page,f)
    adapter._select_statement_option(s,offline_page,f,offline_page.evaluate(m.form_contract.READ_FORM))
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['creation_statement_id']==f['creation_statement_id'] and observed['statement_text']==f['creation_statement_name']
    assert offline_page.evaluate('window.ownedInitialMenuHeight')==0
    assert offline_page.evaluate('window.ownedStatementClicks')==1 and not s.final
    assert adapter._statement_diagnostic['visible_match_count']==1 and adapter._statement_diagnostic['expected_label_present'] is True
    adapter.close()


@pytest.mark.parametrize('state_matches,text_matches',[(True,True),(True,False),(False,True)])
def test_default_no_mark_skips_menu_only_for_exact_native_state_and_display(case,offline_page,state_matches,text_matches):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=5
    f=m.form_contract.validate_form(payload)
    statement_menu(offline_page,f,initial_id=f['creation_statement_id'] if state_matches else 42,
                   initial_text=f['creation_statement_name'] if text_matches else '合成其他显示')
    adapter._select_statement_option(s,offline_page,f,offline_page.evaluate(m.form_contract.READ_FORM))
    skip=state_matches and text_matches
    assert offline_page.evaluate('window.ownedMenuOpens')==(0 if skip else 1)
    assert offline_page.evaluate('window.ownedStatementClicks')==(0 if skip else 1)
    assert adapter._statement_diagnostic['choice_path']==('already_selected' if skip else 'menu')
    assert not s.final
    adapter.close()


@pytest.mark.parametrize('kind',['duplicate','disabled','hidden','missing','wrong-state'])
def test_statement_wait_never_guesses_ambiguous_disabled_missing_or_changed_options(case,offline_page,kind):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=5 if kind in {'duplicate','wrong-state'} else 1
    f=m.form_contract.validate_form(payload);statement_menu(offline_page,f,kind=kind)
    with pytest.raises(ValueError):adapter._select_statement_option(s,offline_page,f,offline_page.evaluate(m.form_contract.READ_FORM))
    if kind=='duplicate':assert adapter._statement_diagnostic['visible_match_count']==2
    if kind in {'disabled','hidden'}:assert adapter._statement_diagnostic['expected_label_present'] is True
    if kind=='missing':assert adapter._statement_diagnostic['expected_label_present'] is False
    assert offline_page.evaluate('window.ownedStatementClicks')==(1 if kind=='wrong-state' else 0)
    assert not s.final
    adapter.close()


def test_statement_wait_cannot_extend_the_original_preparation_deadline(case,offline_page):
    import time
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=3
    f=m.form_contract.validate_form(payload);statement_menu(offline_page,f,kind='hidden')
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    started=time.monotonic();adapter._preparation_deadline=started+.08
    with pytest.raises((ValueError,PlaywrightTimeoutError)):adapter._select_statement_option(s,offline_page,f,observed)
    assert time.monotonic()-started<1
    assert not s.final and offline_page.evaluate('window.ownedStatementClicks')==0
    adapter.close()


@pytest.mark.parametrize('expired',[False,True])
def test_statement_open_click_uses_remaining_preparation_deadline(case,offline_page,expired,monkeypatch):
    import time
    from playwright.sync_api import Locator,TimeoutError as PlaywrightTimeoutError
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=3
    f=m.form_contract.validate_form(payload);statement_menu(offline_page,f)
    offline_page.set_default_timeout(1500)
    offline_page.evaluate("""() => {
        const overlay=document.createElement('div');
        overlay.style.cssText='position:fixed;inset:0;z-index:9999;background:white';
        document.body.append(overlay);
    }""")
    clicks=[];native_click=Locator.click
    def click(locator,**kwargs):
        clicks.append(kwargs.get('timeout'));return native_click(locator,**kwargs)
    monkeypatch.setattr(Locator,'click',click)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    started=time.monotonic();adapter._preparation_deadline=started+(-.01 if expired else .08)
    with pytest.raises((ValueError,PlaywrightTimeoutError)):
        adapter._select_statement_option(s,offline_page,f,observed)
    assert all(type(timeout) in {int,float} and 0<timeout<=80 for timeout in clicks)
    if expired:assert clicks==[]
    assert offline_page.evaluate('window.ownedMenuOpens')==0 and not s.final
    adapter.close()


def test_statement_counter_diagnostic_contains_only_finite_metadata(case,offline_page,monkeypatch,caplog):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=1
    f=m.form_contract.validate_form(payload);f['creation_statement_name']='PRIVATE_NATIVE_MENU_LABEL'
    statement_menu(offline_page,f,kind='missing');monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS','1')
    adapter._preparation_stage='form_preparation'
    with pytest.raises(ValueError) as caught:adapter._select_statement_option(s,offline_page,f,offline_page.evaluate(m.form_contract.READ_FORM))
    with caplog.at_level('WARNING'):adapter._record_preparation_failure(caught.value)
    detail=adapter.preparation_diagnostic();observation=detail['statement_observation']
    assert observation['visible_match_count']==0 and observation['expected_label_present'] is False
    assert observation['state_id_known'] is False and observation['statement_text_matches'] is False
    assert 'PRIVATE_NATIVE_MENU_LABEL' not in json.dumps(detail) and 'PRIVATE_NATIVE_MENU_LABEL' not in caplog.text
    assert str(payload['video_path']) not in caplog.text
    assert detail['frames'][-1]['method']=='_select_statement_option'
    adapter.close()


def schedule_menu(page,f,kind='normal'):
    page.set_content('''<div class="video-title"><input></div><div class="creation-statement-container">合成状态</div>
        <div class="video-basic"><div class="form"><div class="time-container">
        <div class="switch-container">合成开关</div><div class="d-time-container">合成时区</div>
        <div class="date-picker-date"><span class="date-show"></span></div><div class="date-picker-nav-title"></div>
        <div class="date-wrp"><span class="date-picker-body-item"></span></div>
        <div class="date-picker-timer"><span class="date-show"></span></div>
        <div class="time-picker-panel-select-wrp"><span class="time-picker-panel-select-item"></span></div>
        <div class="time-picker-panel-select-wrp"><span class="time-picker-panel-select-item"></span></div>
        </div></div></div><div class="time-release-dialog" style="display:none"></div>''')
    page.evaluate('''value=>{
        const main=document.querySelector('.time-container'),hidden=main.cloneNode(true),f=value.f;
        document.querySelector('.time-release-dialog').append(hidden);
        window.ownedScheduleClicks={regular:0,hidden:0};
        for(const node of main.querySelectorAll('div,span'))node.addEventListener('click',()=>ownedScheduleClicks.regular++);
        for(const node of hidden.querySelectorAll('div,span'))node.addEventListener('click',()=>ownedScheduleClicks.hidden++);
        const switcher=main.querySelector('.switch-container');switcher.addEventListener('click',()=>switcher.classList.toggle('switch-container-active'));
        main.querySelector('.date-picker-date .date-show').innerText=f.date;
        main.querySelector('.date-picker-timer .date-show').innerText=f.clock;
        main.querySelector('.date-picker-nav-title').innerText=f.date.slice(0,4)+'年'+Number(f.date.slice(5,7))+'月';
        main.querySelector('.date-picker-body-item').innerText=Number(f.date.slice(-2));
        const panels=main.querySelectorAll('.time-picker-panel-select-item');
        f.clock.split(':').forEach((part,index)=>panels[index].innerText=part);
        main.querySelector('.d-time-container').__vue__={timezone:'Asia/Shanghai'};
        document.querySelector('.creation-statement-container').__vue__={$store:{state:{task_owned:{}}},
            $taskConfig:{currentTaskId:'owned',tasks:[{id:'owned',archive:{dtime:f.dtime},videoTasks:[]}]}};
        if(value.kind==='zero')main.remove();
        if(value.kind==='two-main')main.parentElement.append(main.cloneNode(true));
        if(value.kind==='visible-dialog')document.querySelector('.time-release-dialog').style.display='';
        if(value.kind==='hidden-only')main.style.display='none';
        if(value.kind==='visible-batch'){main.parentElement.classList.add('batch-fill');}
        if(value.kind==='two-switch')main.append(switcher.cloneNode(true));
        if(value.kind==='disabled')switcher.classList.add('switch-container-disabled');
        if(value.kind==='delayed'){
            const date=main.querySelector('.date-picker-date'),timer=main.querySelector('.date-picker-timer'),
                calendar=main.querySelector('.date-picker-nav-title'),day=main.querySelector('.date-wrp'),
                panels=main.querySelectorAll('.time-picker-panel-select-wrp');
            for(const node of [date,timer,calendar,day,...panels])node.style.display='none';
            switcher.addEventListener('click',()=>setTimeout(()=>{date.style.display='';timer.style.display='';},120));
            date.addEventListener('click',()=>setTimeout(()=>{calendar.style.display='';day.style.display='';},120));
            timer.addEventListener('click',()=>setTimeout(()=>{for(const panel of panels)panel.style.display='';},120));
        }
    }''',{'f':f,'kind':kind})


@pytest.mark.parametrize('kind',['normal','delayed'])
def test_schedule_uses_one_visible_primary_scope_and_ignores_hidden_dialog(case,offline_page,kind):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=5
    f=m.form_contract.validate_form(payload);schedule_menu(offline_page,f,kind)
    s.options={'schedule':{'min_lead_seconds':0,'max_lead_seconds':172800}}
    assert offline_page.locator('.time-container .switch-container').count()==2
    adapter._set_schedule(s,offline_page,f)
    observed=offline_page.evaluate(m.form_contract.READ_FORM)
    assert observed['time_enabled'] is True
    assert (observed['dtime'],observed['date'],observed['clock'],observed['timezone'])==(f['dtime'],f['date'],f['clock'],'Asia/Shanghai')
    clicks=offline_page.evaluate('window.ownedScheduleClicks')
    assert clicks['regular']>0 and clicks['hidden']==0 and not s.final
    assert adapter._schedule_diagnostic['all_switch_count']==2 and adapter._schedule_diagnostic['visible_switch_count']==1
    adapter.close()


@pytest.mark.parametrize('expected',[1,2])
def test_schedule_wait_rejects_more_than_expected_controls_without_click(case,offline_page,expected):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=1
    offline_page.set_content('<div class="synthetic-picker">选项</div>'*(expected+1));adapter._schedule_diagnostic={}
    with pytest.raises(ValueError):adapter._schedule_candidates(s,offline_page.locator('.synthetic-picker:visible'),expected,'B站时分控件发生变化','panels')
    assert adapter._schedule_diagnostic=={'control':'panels','control_count':expected+1,'expected_count':expected}
    assert not s.final;adapter.close()


def test_schedule_wait_stops_at_original_deadline_without_click(case,offline_page):
    import time
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page;adapter.step_timeout=5
    offline_page.set_content('<div>合成未挂载面板</div>');adapter._schedule_diagnostic={}
    adapter._preparation_deadline=time.monotonic()-.01
    with pytest.raises(ValueError):adapter._schedule_candidates(s,offline_page.locator('.synthetic-picker:visible'),2,'B站时分控件发生变化','panels')
    assert adapter._schedule_diagnostic=={} and not s.final;adapter.close()


@pytest.mark.parametrize('kind',['zero','two-main','visible-dialog','hidden-only','visible-batch','two-switch','disabled'])
def test_schedule_rejects_missing_ambiguous_dialog_batch_or_disabled_scope(case,offline_page,kind,monkeypatch,caplog):
    adapter,payload,_,_,_=case;s=adapter._session(payload);s.page=offline_page
    f=m.form_contract.validate_form(payload);schedule_menu(offline_page,f,kind)
    offline_page.evaluate("document.querySelector('.creation-statement-container').append('PRIVATE_SCHEDULE_TEXT')")
    monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS','1');adapter._preparation_stage='schedule_preparation'
    with pytest.raises(ValueError) as caught:adapter._set_schedule(s,offline_page,f)
    with caplog.at_level('WARNING'):adapter._record_preparation_failure(caught.value)
    detail=adapter.preparation_diagnostic();observation=detail['schedule_observation']
    assert observation and all(type(value) in {int,bool} for value in observation.values())
    assert 'PRIVATE_SCHEDULE_TEXT' not in json.dumps(detail) and 'PRIVATE_SCHEDULE_TEXT' not in caplog.text
    assert offline_page.evaluate('window.ownedScheduleClicks')=={'regular':0,'hidden':0} and not s.final
    if kind!='disabled':assert offline_page.evaluate(m.form_contract.READ_FORM)['time_enabled'] is None
    adapter.close()


@pytest.mark.parametrize('reference,expected',[('',False),('data:image/jpeg;base64,Zg==',True)])
def test_cover_done_flag_requires_actual_page_readback(case,reference,expected,monkeypatch):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.cover_uploaded=True;s.cover_ready=True
    frame=SimpleNamespace(evaluate=lambda script:{'cover_reference':reference})
    monkeypatch.setattr(adapter,'_trace',lambda *args:None)
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is expected
    assert s.cover_ready is expected
    assert s.final is False
    adapter.close()


def test_missing_cover_after_confirmation_has_specific_safe_failure(case,monkeypatch):
    adapter,payload,_,_,_=case
    def pending(s,frame,f):
        s.cover_uploaded=True;s.cover_confirm_clicked=True;s.cover_ready=False
        return False
    monkeypatch.setattr(adapter,'_prepare_form',pending)
    adapter.step_timeout=.35
    result=adapter.submit(payload,context=None,before_final=lambda:pytest.fail('未读回封面不得投稿'))
    assert result.state=='pre_submit_failed' and result.error_category=='cover_unconfirmed'
    assert adapter._session(payload).final is False
    adapter.close()


def test_cover_confirmation_waits_for_page_without_clicking_done_twice(case,monkeypatch):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.cover_uploaded=True;s.cover_ready=False
    calls=[];raw={'cover_reference':''}
    done=SimpleNamespace(count=lambda:1,click=lambda **options:None if options.get('trial') else calls.append('done'))
    s.cover_path=Path('owned.jpg');s.cover_opened=True;s.cover_uploaded_ratios={'four_three','sixteen_nine'}
    observed={'editor_count':1,'observed':True,'main_slot':True,'task_matches':True,'needs_four_three':True,'four_three_ready':True,'sixteen_nine_ready':True,'submit_enabled':True}
    frame=SimpleNamespace(locator=lambda selector:done,evaluate=lambda script:raw if script==m.form_contract.READ_FORM else observed)
    monkeypatch.setattr(adapter,'_trace',lambda *args:None)
    for _ in range(3):assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is False
    assert calls==['done'] and not s.cover_ready and not s.final
    raw['cover_reference']='data:image/jpeg;base64,Zg=='
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is True
    assert calls==['done'] and s.cover_ready and not s.final
    adapter.close()


def test_stalled_cover_save_retries_only_local_confirmation_once_then_stops_before_post(case,monkeypatch):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.cover_ready=False;s.cover_confirm_clicked=False
    clock=[0.0];calls=[]
    monkeypatch.setattr(m,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
    s.cover_path=Path('owned.jpg');s.cover_opened=True;s.cover_uploaded=True
    s.cover_uploaded_ratios={'four_three','sixteen_nine'}
    observed={'editor_count':1,'observed':True,'main_slot':True,'task_matches':True,
        'needs_four_three':True,'four_three_ready':True,'sixteen_nine_ready':True,'submit_enabled':True}
    done=SimpleNamespace(count=lambda:1,click=lambda **options:None if options.get('trial') else calls.append(clock[0]))
    frame=SimpleNamespace(locator=lambda selector:done,evaluate=lambda script:{'cover_reference':''} if script==m.form_contract.READ_FORM else observed)
    monkeypatch.setattr(adapter,'_trace',lambda *args:None)
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is False
    clock[0]=16
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is False
    clock[0]=32
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,frame) is False
    assert calls==[0,16] and s.cover_wait_expired and not s.final
    adapter.close()


@pytest.mark.parametrize('ready43,ready169,can_confirm',[(False,False,False),(True,False,False),(False,True,False),(True,True,True)])
def test_cover_done_requires_both_native_aspect_editors_ready(case,offline_page,tmp_path,ready43,ready169,can_confirm):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.cover_path=tmp_path/'owned.jpg';s.cover_path.write_bytes(b'owned synthetic image')
    s.cover_ready=False;s.cover_confirm_clicked=False
    s.cover_opened=True;s.cover_uploaded=True;s.cover_uploaded_ratios={'four_three','sixteen_nine'}
    offline_page.set_content('<div class="cover-editor"><div class="cover-editor-button"><button class="button submit">完成</button></div></div>')
    offline_page.evaluate('''ready=>{
        window.coverConfirmCount=0;document.querySelector('button').onclick=()=>window.coverConfirmCount++;
        document.querySelector('.cover-editor').__vue__={id:'owned-task',coverIndex:0,$taskConfig:{currentTaskId:'owned-task'},
          isOverseas:false,activeEditor:'editor_16_9',submitDisabled:false,coverForEditor:{origin_4_3:'owned',origin_16_9:'owned'},
          isCoverEditorReady_4_3:ready[0],isCoverEditorReady_16_9:ready[1],
          imageEditor_4_3:ready[0]?{}:null,imageEditor_16_9:ready[1]?{}:null};
    }''',[ready43,ready169])
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page) is False
    assert offline_page.evaluate('window.coverConfirmCount')==int(can_confirm)
    assert s.cover_confirm_clicked is can_confirm and s.final is False
    adapter.close()


def native_cover_fixture(page):
    page.set_content('''<div class="creation-statement-container">合成任务</div>
      <div class="cover-content"><div class="cover-module-main"><button class="cover-slot">封面</button></div></div>
      <div id="cover-shell"><div class="cover-editor">
      <div class="cover-editor-panel-canvas-title"><span class="text">首页推荐封面（4:3）</span></div>
      <div class="cover-editor-panel-canvas-title"><span class="text">个人空间封面（16:9）</span></div>
      <div class="cover-upload"><input type="file"></div>
      <div class="cover-editor-button"><button class="button submit">完成</button></div></div></div>''')
    page.evaluate('''()=>{
      const store={state:{task_owned:{}}},config={currentTaskId:'owned',tasks:[{id:'owned',archive:{},videoTasks:[]}]};
      document.querySelector('.creation-statement-container').__vue__={$store:store,$taskConfig:config};
      const vm={id:'owned',coverIndex:0,$taskConfig:config,isOverseas:false,activeEditor:'editor_16_9',submitDisabled:false,
        coverForEditor:{origin_4_3:'owned',origin_16_9:'owned'},isCoverEditorReady_4_3:true,isCoverEditorReady_16_9:true,
        imageEditor_4_3:{},imageEditor_16_9:{}};
      window.coverClicks=0;window.coverSelections=[];document.querySelector('.cover-editor').__vue__=vm;
      document.querySelector('.submit').onclick=()=>{window.coverClicks++;store.state.task_owned.mainCover={main:{origin:'owned-committed'}};};
      document.querySelector('.cover-slot').onclick=()=>{
        document.querySelector('#cover-shell').style.opacity='1';vm.coverForEditor={};
        vm.isCoverEditorReady_4_3=vm.isCoverEditorReady_16_9=false;vm.imageEditor_4_3=vm.imageEditor_16_9=null;
      };
      document.querySelectorAll('.text').forEach((e,i)=>e.onclick=()=>{vm.activeEditor=i===0?'editor_4_3':'editor_16_9';});
      document.querySelector('input').onchange=()=>{const ratio=vm.activeEditor==='editor_4_3'?'4_3':'16_9';
        window.coverSelections.push(ratio);vm.coverForEditor['origin_'+ratio]='owned';vm['isCoverEditorReady_'+ratio]=true;vm['imageEditor_'+ratio]={};};
    }''')


@pytest.mark.parametrize('hidden',['ancestor-opacity','ancestor-aria','confirm-button'])
def test_stale_or_hidden_native_cover_controls_never_grant_confirmation(offline_page,hidden):
    native_cover_fixture(offline_page)
    if hidden=='ancestor-opacity':offline_page.locator('#cover-shell').evaluate("e=>e.style.opacity='0'")
    elif hidden=='ancestor-aria':offline_page.locator('#cover-shell').evaluate("e=>e.setAttribute('aria-hidden','true')")
    else:offline_page.locator('.submit').evaluate("e=>e.style.display='none'")
    observed=offline_page.evaluate(m.form_contract.READ_COVER_EDITOR)
    if hidden=='confirm-button':assert observed['editor_count']==1 and observed['submit_enabled'] is False
    else:assert observed['editor_count']==0
    assert offline_page.evaluate('window.coverClicks')==0


def test_obscured_cover_retry_waits_without_default_timeout_then_confirms_once(case,offline_page,monkeypatch):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s);native_cover_fixture(offline_page)
    s.cover_confirm_clicked=True;s.cover_wait_started=0;s.cover_uploaded=True;s.cover_ready=False
    clock=[16.0];adapter.step_timeout=.08
    monkeypatch.setattr(m,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time))
    offline_page.set_default_timeout(150)
    offline_page.evaluate("()=>{const e=document.createElement('div');e.id='overlay';e.style='position:fixed;inset:0;z-index:9999;background:white';document.body.append(e)}")
    started=time.monotonic()
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page) is False
    assert time.monotonic()-started<1
    assert s.cover_retry_count==0 and not s.final and offline_page.evaluate('window.coverClicks')==0
    offline_page.locator('#overlay').evaluate('e=>e.remove()');clock[0]=17
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page) is False
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page) is True
    assert s.cover_retry_count==1 and not s.final and offline_page.evaluate('window.coverClicks')==1
    adapter.close()


def test_closed_cover_recovery_reopens_same_frame_without_video_upload_or_final(case,offline_page,tmp_path,monkeypatch):
    adapter,payload,_,events,_=case;s=adapter._session(payload);adapter._init_observations(s);native_cover_fixture(offline_page)
    s.cover_path=tmp_path/'owned.jpg';s.cover_path.write_bytes(b'owned local frame')
    s.cover_opened=True;s.cover_uploaded=True;s.cover_uploaded_ratios={'four_three','sixteen_nine'}
    s.cover_confirm_clicked=True;s.cover_ready=False;s.cover_wait_started=0
    monkeypatch.setattr(m,'time',SimpleNamespace(monotonic=lambda:16.0,time=time.time))
    offline_page.set_default_timeout(150)
    offline_page.locator('#cover-shell').evaluate("e=>e.style.opacity='0'")
    ready=False
    for _ in range(9):
        ready=m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page)
        if ready:break
    assert ready and s.cover_ready and s.cover_retry_count==1 and not s.final
    assert offline_page.evaluate('window.coverClicks')==1
    assert sorted(offline_page.evaluate('window.coverSelections'))==['16_9','4_3']
    assert not any(name in {'upload','journal','final'} for name,_ in events)
    adapter.close()


def native_cover_sync_prompt(page,*,task_matches=True,title='检测到16:9封面未修改'):
    page.evaluate('''({task_matches,title})=>{
      const editor=document.querySelector('.cover-editor'),vm=editor.__vue__;
      vm.hasShownSyncConfirm=true;if(!task_matches)vm.id='other';
      const box=document.createElement('div');box.className='bcc-dialog__wrap';box.setAttribute('role','dialog');
      box.style='position:fixed;inset:0;background:white;z-index:100';
      const heading=document.createElement('h3');heading.textContent=title;box.append(heading);
      for(const text of ['确认同步','不同步，手动编辑']){const b=document.createElement('button');b.textContent=text;
        b.onclick=()=>{window.coverSyncChoice=text;box.remove();};box.append(b);}
      document.body.append(box);
    }''',{'task_matches':task_matches,'title':title})


@pytest.mark.parametrize('task_matches,title,expected',[(True,'检测到16:9封面未修改',True),(False,'检测到16:9封面未修改',False),(True,'请完成安全验证',False)])
def test_only_current_cover_sync_confirmation_is_recognized(offline_page,task_matches,title,expected):
    native_cover_fixture(offline_page);native_cover_sync_prompt(offline_page,task_matches=task_matches,title=title)
    assert offline_page.evaluate(m.form_contract.READ_COVER_EDITOR).get('sync_prompt',False) is expected


def test_cover_sync_dialog_can_be_handled_while_background_editor_is_aria_hidden(offline_page):
    native_cover_fixture(offline_page);native_cover_sync_prompt(offline_page)
    offline_page.locator('#cover-shell').evaluate("e=>e.setAttribute('aria-hidden','true')")
    observed=offline_page.evaluate(m.form_contract.READ_COVER_EDITOR)
    assert observed['sync_prompt'] is True and observed['editor_count']==0
    assert observed['submit_enabled'] is False


def test_native_cover_sync_prompt_preserves_both_selections_then_saves_before_post(case,offline_page,tmp_path):
    adapter,payload,_,events,_=case;s=adapter._session(payload);adapter._init_observations(s)
    native_cover_fixture(offline_page)
    s.cover_path=tmp_path/'owned.jpg';s.cover_path.write_bytes(b'owned local frame')
    s.cover_opened=True;s.cover_uploaded=True;s.cover_uploaded_ratios={'four_three','sixteen_nine'}
    # Materialize the normal confirmation between browser turns, after the
    # first done click; native choice and second done remain actual DOM clicks.
    offline_page.evaluate("()=>{document.querySelector('.submit').onclick=()=>{window.coverClicks++;if(!window.openedSyncPrompt){window.openedSyncPrompt=true;return;}document.querySelector('.creation-statement-container').__vue__.$store.state.task_owned.mainCover={main:{origin:'owned-committed'}};};}")
    assert m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page) is False
    native_cover_sync_prompt(offline_page)
    for _ in range(5):
        if m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page):break
    assert s.cover_ready and not s.final
    assert offline_page.evaluate('window.coverSyncChoice')=='不同步，手动编辑'
    assert offline_page.evaluate('window.coverClicks')==2
    assert not any(name in {'upload','journal','final'} for name,_ in events)
    adapter.close()


def test_both_cover_ratios_use_native_selection_then_confirm_once(case,offline_page,tmp_path):
    adapter,payload,_,_,_=case;s=adapter._session(payload);adapter._init_observations(s)
    s.cover_path=tmp_path/'owned.jpg';s.cover_path.write_bytes(b'owned synthetic image')
    s.cover_opened=True;s.cover_uploaded=False;s.cover_uploaded_ratios=set();s.cover_ready=False;s.cover_confirm_clicked=False
    offline_page.set_content('''<div class="creation-statement-container" style="height:20px">合成任务</div><div class="cover-editor">
      <div class="cover-editor-panel-canvas-title"><span class="text">首页推荐封面（4:3）</span></div>
      <div class="cover-editor-panel-canvas-title"><span class="text">个人空间封面（16:9）</span></div>
      <div class="cover-upload"><input type="file"></div><div class="cover-editor-button"><button class="button submit">完成</button></div></div>''')
    offline_page.evaluate('''()=>{
      window.coverActions=[];const store={state:{task_owned:{}}},config={currentTaskId:'owned',tasks:[{id:'owned',archive:{},videoTasks:[]}]};
      document.querySelector('.creation-statement-container').__vue__={$store:store,$taskConfig:config};
      const vm={id:'owned',coverIndex:0,$taskConfig:config,isOverseas:false,activeEditor:null,submitDisabled:false,coverForEditor:{},isCoverEditorReady_4_3:false,isCoverEditorReady_16_9:false,imageEditor_4_3:null,imageEditor_16_9:null};
      document.querySelector('.cover-editor').__vue__=vm;
      document.querySelectorAll('.text').forEach((e,i)=>e.onclick=()=>{vm.activeEditor=i===0?'editor_4_3':'editor_16_9';});
      document.querySelector('input').onchange=()=>{const ratio=vm.activeEditor==='editor_4_3'?'4_3':'16_9';window.coverActions.push('upload_'+ratio);vm.coverForEditor['origin_'+ratio]='owned';vm['isCoverEditorReady_'+ratio]=true;vm['imageEditor_'+ratio]={};};
      document.querySelector('button').onclick=()=>{if(!vm.imageEditor_4_3||!vm.imageEditor_16_9)return;window.coverActions.push('confirm');store.state.task_owned.mainCover={main:{origin:'owned-committed'}};};
    }''')
    for _ in range(8):
        ready=m.BilibiliMultiPublishAdapter._prepare_cover(adapter,s,offline_page)
        assert not s.final
        if ready:break
    assert ready and s.cover_ready,(offline_page.evaluate(m.form_contract.READ_COVER_EDITOR),offline_page.evaluate('window.coverActions'),offline_page.evaluate(m.form_contract.READ_FORM).get('cover_reference'))
    assert offline_page.evaluate('window.coverActions')==['upload_4_3','upload_16_9','confirm']
    adapter.close()
