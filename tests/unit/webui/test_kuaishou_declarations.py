"""Author choices and final evidence: offline fixtures, no account or HTTP IO."""
import pytest

from test_kuaishou_multi_publish import case, submit_response, persist_schedule_result
from yijianjifa.app_services import kuaishou_declaration_catalog as catalog
from yijianjifa.features.publish.deliveries import digest
from yijianjifa.webui import kuaishou_publish_form as contract
from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter


def form(choice='none'):
    return contract.validate_form({'fields':{'title':'离线合成标题','body':'','tags':'',
        'content_declaration_choice':choice},'execution_mode':'publish_now'})


def readback(expected):
    declaration=expected['declaration']
    return {'caption':expected['caption'],'tags':expected['tags'],'time_text':'','mode':'立即发布',
            'editor_count':1,'upload_complete':True,'button_count':1,'button_enabled':True,
            'declaration_count':1,'declaration_variant':'legacy','declaration_label':declaration['name'],
            'declaration_item_count':0 if declaration['choice']=='none' else 1}


def test_local_catalog_exposes_semantic_choices_and_never_account_statement_ids():
    expected=[{'choice':'none','name':'不添加作者声明'},
              {'choice':'ai_generated','name':'内容为AI生成'},
              {'choice':'fictional_performance','name':'演绎情节，仅供娱乐'},
              {'choice':'personal_opinion','name':'个人观点，仅供参考'}]
    assert catalog.choices()==expected
    assert KuaishouMultiPublishAdapter.public_field_schema['content_declaration_choice']['choices']==expected
    assert catalog.catalog()['account_statement_ids_verified'] is False
    first=catalog.choices();first[0]['name']='tampered'
    assert catalog.choices()==expected


@pytest.mark.parametrize('choice',[None,True,1,'1','内容为AI生成','network_source','unknown',''])
def test_unverified_choice_is_rejected_without_silent_no_declaration(choice):
    with pytest.raises(ValueError):form(choice)


def test_absent_and_explicit_none_are_compatible_but_unknown_fields_are_rejected():
    payload={'fields':{'title':'离线合成标题','body':'','tags':''},'execution_mode':'publish_now'}
    assert contract.validate_form(payload)==form()
    payload['fields']['unknown_author_statement']='AI'
    with pytest.raises(ValueError):contract.validate_form(payload)


@pytest.mark.parametrize('choice,source',[
    ('ai_generated',1),('fictional_performance',2),('personal_opinion',3)])
def test_source_mapping_and_readback_must_both_match(choice,source):
    expected=form(choice);raw=readback(expected)
    assert contract.verify_form(raw,expected)
    assert contract.verify_submit_declaration({'declareInfo':{'source':source}},expected)
    assert not contract.verify_form({**raw,'declaration_label':'素材来源于网络'},expected)
    assert not contract.verify_form({**raw,'declaration_variant':'account_statement'},expected)
    assert not contract.verify_submit_declaration({'declareInfo':{'source':4}},expected)


@pytest.mark.parametrize('change',[
    {'declaration_count':0},{'declaration_count':2},{'declaration_count':True},{'declaration_variant':'unavailable'},
    {'declaration_variant':'account_statement'},{'declaration_label':'内容为AI生成'},
    {'declaration_item_count':1}])
def test_none_requires_one_verified_empty_legacy_control(change):
    expected=form();raw=readback(expected)
    assert not contract.verify_form({**raw,**change},expected)


@pytest.mark.parametrize('declaration',[
    {'source':True},{'source':'1'},{'source':1.0},{'source':2},{'statementId':1},
    {'statementId':1001},{'source':1,'statementId':1},{'source':1,'unknown':None},
    {'source':1,'platform':None},'{"source":1}',[],{},None])
def test_successful_response_cannot_bind_unknown_or_extra_declaration_metadata(declaration):
    assert not contract.verify_submit_declaration({'declareInfo':declaration},form('ai_generated'))


@pytest.mark.parametrize('submit_body',[{}, {'declareInfo':None},{'declareInfo':{}}])
def test_legacy_absence_is_only_compatible_with_none(submit_body):
    assert contract.verify_submit_declaration(submit_body,form())
    assert not contract.verify_submit_declaration(submit_body,form('ai_generated'))


@pytest.mark.parametrize('declaration',[{'source':0},{'source':None},{'source':1},{'unknown':None},[],False,''])
def test_selected_or_unrecognized_declaration_never_establishes_none(declaration):
    assert not contract.verify_submit_declaration({'declareInfo':declaration},form())


@pytest.mark.parametrize('choice,source',[
    ('ai_generated',1),('fictional_performance',2),('personal_opinion',3)])
def test_listener_binds_source_and_recovery_does_not_repeat_upload_or_final(case,monkeypatch,choice,source):
    adapter,payload,_,events,_=case
    payload['fields']['content_declaration_choice']=choice
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    def click(session,frame):
        events.append(('final',None));session.upload_id='99'
        adapter._response(session,submit_response(payload,declareInfo={'source':source}))
    monkeypatch.setattr(adapter,'_click_final',click)
    try:
        assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state=='platform_scheduled'
        persist_schedule_result(adapter,payload)
        assert adapter.reconcile(payload,context=None).state=='platform_scheduled'
        assert [event[0] for event in events if event[0] in {'upload','journal','final'}]==['upload','journal','final']
    finally:adapter.close()


@pytest.mark.parametrize('declaration',[None,{}, {'source':2},{'statementId':1001},{'source':1,'unknown':None}])
def test_listener_rejects_wrong_declaration_even_after_success_ack(case,declaration):
    adapter,payload,_,_,_=case
    payload['fields']['content_declaration_choice']='ai_generated'
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    session=adapter._session(payload);adapter._init_observations(session)
    session.upload_id='99';session.final=True
    try:
        adapter._response(session,submit_response(payload,declareInfo=declaration))
        assert not session.submit_ok and not session.schedule_accepted and not session.receipt
    finally:adapter.close()


@pytest.fixture(scope='module')
def offline_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as driver:
        # On macOS without compositor ticks, this headless synthetic page can
        # be visible and correctly sized while requestAnimationFrame never
        # fires. Keep Playwright's real actionability/stability checks enabled;
        # let this offline browser produce frames without waiting for vsync.
        browser=driver.chromium.launch(headless=True,channel='chromium',
                                       args=['--disable-frame-rate-limit'])
        try:yield browser
        finally:browser.close()


@pytest.fixture
def offline_page(offline_browser):
    context=offline_browser.new_context(service_workers='block')
    context.route('**/*',lambda route:route.abort())
    page=context.new_page()
    # These tests verify declaration choices, readback and fail-stop behavior,
    # not a one-second renderer/driver benchmark. Match the host context's 10s
    # action budget so real auto-waits also work on a busy Windows runner.
    # Navigation retains its independent budget; production limits are untouched.
    page.set_default_navigation_timeout(10000)
    page.set_default_timeout(10000)
    try:yield page
    finally:context.close()


def mount(page,*,required=False,selected='',duplicate=False,missing_option=False):
    # Ant select markup reduced to the public DOM contract. The content editor
    # also contains a declaration label to ensure metadata is not a control.
    selected_markup=f'<span class="ant-select-selection-item">{selected}</span>' if selected else ''
    star='<span>*</span>' if required else ''
    labels=[row['name'] for row in catalog.choices()[1:]]
    if missing_option:labels.remove('内容为AI生成')
    options=''.join(f'<div class="ant-select-item-option-content">'+
        (f'<div><div class="primary-label">{label}</div><div>平台补充说明</div></div>' if required else label)+
        '</div>' for label in labels)
    row=f'''<div class="_edit-form-item_fixture"><label>{star}作者声明<img></label>
      <div class="ant-select-selector" style="width:240px;height:32px;border:1px solid">{selected_markup}</div>
      <span class="ant-select-clear">清除</span><div class="dropdown" style="display:none">{options}</div></div>'''
    page.set_content(f'''<div id="work-description-edit" contenteditable="true">作者声明</div>
      <div class="publish-time-container"><input type="radio" value="1" checked></div>
      <div class="upload-status-text">上传成功</div>
      <div class="edit-section-btns"><div class="_button_fixture">发布</div></div>
      {row}{row if duplicate else ''}<script>
      for (const row of document.querySelectorAll('._edit-form-item_fixture')) {{
        row.querySelector('.ant-select-selector').onclick=()=>row.querySelector('.dropdown').style.display='block';
        row.querySelector('.ant-select-clear').onclick=()=>row.querySelector('.ant-select-selection-item')?.remove();
        for (const option of row.querySelectorAll('.ant-select-item-option-content')) option.onclick=()=>{{
          const primary=option.querySelector('.primary-label')||option;
          row.querySelector('.ant-select-selector').innerHTML='<span class="ant-select-selection-item">'+primary.textContent+'</span>';
          row.querySelector('.dropdown').style.display='none';
        }};
      }}
      </script>''')


@pytest.mark.parametrize('choice',['ai_generated','fictional_performance','personal_opinion'])
def test_selects_only_through_visible_original_control_and_reads_back(offline_page,choice):
    mount(offline_page);adapter=object.__new__(KuaishouMultiPublishAdapter)
    expected=form(choice);raw=offline_page.evaluate(contract.READ_FORM)
    assert raw['declaration_count']==1 and raw['declaration_item_count']==0
    adapter._fill_declaration(offline_page,raw,expected)
    assert contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),expected)


def test_declaration_selection_observes_real_actionability_without_a_one_second_speed_requirement(offline_page):
    mount(offline_page);adapter=object.__new__(KuaishouMultiPublishAdapter)
    expected=form('ai_generated');raw=offline_page.evaluate(contract.READ_FORM)
    assert raw['declaration_count']==1 and raw['declaration_item_count']==0
    offline_page.evaluate('''() => {
      window.fixtureDeclarationClicks={selector:0,option:0,final:0};
      const selector=document.querySelector('.ant-select-selector');
      selector.addEventListener('click',()=>window.fixtureDeclarationClicks.selector++);
      for(const option of document.querySelectorAll('.ant-select-item-option-content'))
        option.addEventListener('click',()=>window.fixtureDeclarationClicks.option++);
      document.querySelector('._button_fixture').addEventListener('click',()=>window.fixtureDeclarationClicks.final++);
      // Exercise real asynchronous actionability, without force/click stubs.
      // A brief overlay can delay a correct original control on either host.
      const cover=document.createElement('div');
      cover.style.cssText='position:fixed;inset:0;z-index:99999;background:white';
      document.body.append(cover);setTimeout(()=>cover.remove(),1800);
    }''')
    adapter._fill_declaration(offline_page,raw,expected)
    assert contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),expected)
    assert offline_page.evaluate('window.fixtureDeclarationClicks')=={'selector':1,'option':1,'final':0}


def test_explicit_none_clears_existing_selection_through_original_control(offline_page):
    mount(offline_page,selected='内容为AI生成');adapter=object.__new__(KuaishouMultiPublishAdapter)
    adapter._fill_declaration(offline_page,offline_page.evaluate(contract.READ_FORM),form())
    assert contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),form())


@pytest.mark.parametrize('kwargs',[{'required':True},{'duplicate':True},{'missing_option':True}])
def test_required_new_form_ambiguous_control_and_missing_option_stop_before_final(offline_page,kwargs):
    mount(offline_page,**kwargs);adapter=object.__new__(KuaishouMultiPublishAdapter)
    with pytest.raises(ValueError):adapter._fill_declaration(
        offline_page,offline_page.evaluate(contract.READ_FORM),form('ai_generated'))
    assert not contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),form('ai_generated'))


def expose_statement_props(page,options,*,alternate=None):
    # This models the props passed by the official Sy component. Reading only
    # bounded id/text props must neither call a component method nor a store.
    page.evaluate('''args=>{
      const row=document.querySelector('._edit-form-item_fixture');
      const parent={memoizedProps:{authorStatementList:args.options}};
      if(args.alternate!==null)parent.alternate={memoizedProps:{authorStatementList:args.alternate}};
      row.__reactFiber$fixture={memoizedProps:{},return:parent};
    }''',{'options':options,'alternate':alternate})


@pytest.mark.parametrize('statement_id',[0,1001,7391])
def test_v2_uses_unique_current_option_id_and_visible_original_control(offline_page,statement_id):
    mount(offline_page,required=True)
    expose_statement_props(offline_page,[{'id':statement_id,'text':'内容为AI生成','subText':'平台补充说明'}])
    expected=form('ai_generated');raw=offline_page.evaluate(contract.READ_FORM)
    adapter=object.__new__(KuaishouMultiPublishAdapter)
    binding=adapter._fill_declaration(offline_page,raw,expected)
    assert binding=={'kind':'account_statement','choice':'ai_generated','name':'内容为AI生成','statement_id':statement_id}
    assert contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),expected,declaration_binding=binding)
    assert contract.verify_submit_declaration({'declareInfo':{'statementId':statement_id}},expected,declaration_binding=binding)
    assert not contract.verify_submit_declaration({'declareInfo':{'source':1}},expected,declaration_binding=binding)
    assert not contract.verify_submit_declaration({'declareInfo':{'statementId':statement_id+1}},expected,declaration_binding=binding)
    assert not contract.verify_submit_declaration({'declareInfo':{'statementId':statement_id,'source':1}},expected,declaration_binding=binding)
    expose_statement_props(offline_page,[{'id':statement_id+1,'text':'内容为AI生成'}])
    assert not contract.verify_declaration(offline_page.evaluate(contract.READ_FORM),expected,declaration_binding=binding)


@pytest.mark.parametrize('options,alternate',[
    ([{'id':1,'text':'内容由AI生成'}],None),
    ([{'id':1,'text':'内容为AI生成'},{'id':2,'text':'内容为AI生成'}],None),
    ([{'id':1,'text':'内容为AI生成'},{'id':1,'text':'个人观点，仅供参考'}],None),
    ([{'id':True,'text':'内容为AI生成'}],None),
    ([{'id':'1','text':'内容为AI生成'}],None),
    ([{'id':9007199254740992,'text':'内容为AI生成'}],None),
    ([{'id':1,'text':'内容为AI生成'}],[{'id':2,'text':'内容为AI生成'}]),
])
def test_v2_changed_label_duplicate_options_and_stale_alternate_props_never_establish_mapping(offline_page,options,alternate):
    mount(offline_page,required=True);expose_statement_props(offline_page,options,alternate=alternate)
    raw=offline_page.evaluate(contract.READ_FORM)
    with pytest.raises(ValueError):contract.resolve_declaration(raw,form('ai_generated'))
    assert not contract.verify_declaration(raw,form('ai_generated'))


def test_v2_required_none_is_blocked_even_if_dom_is_empty(offline_page):
    mount(offline_page,required=True)
    expose_statement_props(offline_page,[{'id':1001,'text':'内容为AI生成'}])
    with pytest.raises(ValueError,match='author statement required'):
        contract.resolve_declaration(offline_page.evaluate(contract.READ_FORM),form())


def test_v2_success_ack_is_bound_to_observed_id_not_legacy_source(case):
    adapter,payload,_,_,_=case;payload['fields']['content_declaration_choice']='ai_generated'
    adapter.bind_delivery(digest=digest(payload),delivery_id='target',task_id='batch')
    session=adapter._session(payload);adapter._init_observations(session)
    session.upload_id='99';session.final=True
    session.declaration_binding={'kind':'account_statement','choice':'ai_generated','name':'内容为AI生成','statement_id':7391}
    try:
        adapter._response(session,submit_response(payload,declareInfo={'statementId':1001}))
        assert not session.submit_ok
        adapter._response(session,submit_response(payload,declareInfo={'source':1}))
        assert not session.submit_ok
        adapter._response(session,submit_response(payload,declareInfo={'statementId':7391}))
        assert session.submit_ok and session.schedule_accepted
    finally:adapter.close()


def test_v2_none_and_unknown_binding_metadata_are_never_accepted():
    binding={'kind':'account_statement','choice':'ai_generated','name':'内容为AI生成','statement_id':1001}
    assert not contract.verify_submit_declaration({'declareInfo':{'statementId':1001}},form('ai_generated'),
        declaration_binding={**binding,'unknown':None})
    assert not contract.verify_submit_declaration({'declareInfo':{'statementId':1001}},form(),
        declaration_binding={**binding,'choice':'none','name':''})


@pytest.mark.parametrize('message,code',[
    ('kuaishou author statement required','kuaishou_author_statement_required'),
    ('kuaishou author statement options not verified','kuaishou_declaration_options_unverified'),
    ('kuaishou declaration option not verified','kuaishou_declaration_options_unverified')])
def test_unverified_declaration_returns_specific_prefinal_failure_without_final(case,monkeypatch,message,code):
    adapter,payload,_,events,_=case
    def fail(*args,**kwargs):raise ValueError(message)
    monkeypatch.setattr(adapter,'_prepare_form',fail)
    try:
        assert adapter.validate_target(payload,payload,payload['execution_mode']).authorized
        result=adapter.submit(payload,context=None,before_final=lambda:events.append(('journal',None)))
        assert result.state=='pre_submit_failed' and result.error_code==code
        assert not any(event[0] in {'journal','final'} for event in events)
    finally:adapter.close()


def test_postfinal_declaration_failure_never_becomes_retryable_prefinal(case,monkeypatch):
    adapter,payload,_,_,_=case;session=adapter._session(payload);session.final=True
    def fail(*args,**kwargs):raise ValueError('kuaishou author statement required')
    monkeypatch.setattr(adapter,'_advance_steps',fail)
    try:
        with pytest.raises(ValueError):adapter._advance(session)
    finally:adapter.close()


def test_builtin_mapping_matches_frozen_primary_source_without_network():
    from pathlib import Path
    import hashlib
    import json
    # Extracted from the original anonymous upstream archive, never from catalog.
    # Keep only four label/value facts instead of redistributing its entire JS.
    source=Path(__file__).resolve().parents[2]/'fixtures/kuaishou-author-declarations-20261009.json'
    data=source.read_bytes()
    assert hashlib.sha256(data).hexdigest()=='5b8c8ec654f84bab1099b66ef879f31ef3485a9df037f73d1aa560ed3eef5e88'
    frozen=json.loads(data)
    assert frozen['source']['sha256']=='31700ae668ae3ecb4d2204f108ac4b0bee2c4045b33a07b6947b02d422743b7a'
    assert frozen['source']['collected_on']=='2026-10-09'
    assert frozen['source']['url']=='https://p66-plat.wskwai.com/kos/nlav11104/onvideo-cp/static/js/index-ih4TlJ04.js'
    observed={row['label']:row['value'] for row in frozen['declarations']}
    assert len(observed)==4
    for row in catalog.choices()[1:]:
        selected=catalog.selection(row['choice'])
        assert observed[row['name']]==selected['source']
    assert observed['素材来源于网络']==4
