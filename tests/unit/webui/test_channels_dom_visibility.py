"""Run the shipped video-channel expressions in network-blocked fixture DOMs."""
import pytest

from yijianjifa.webui.channels_multi_publish import CHALLENGE
from yijianjifa.webui.channels_publish_form import READ_FORM


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


def mount_shadow(page,html,*,host_style='',ancestor_style=''):
    page.set_content('<div id="ancestor"><div id="wujie-host"></div></div>')
    page.evaluate('''args => {
      document.querySelector('#ancestor').style.cssText=args.ancestor_style;
      const host=document.querySelector('#wujie-host');host.style.cssText=args.host_style;
      host.attachShadow({mode:'open'}).innerHTML=args.html;
    }''',dict(html=html,host_style=host_style,ancestor_style=ancestor_style))


CAPTCHA='<iframe id="captcha-fixture" style="width:160px;height:120px;border:0"></iframe>'


@pytest.mark.parametrize('html',[
    '<iframe id="captcha-fixture" style="width:0;height:0;border:0"></iframe>',
    '<iframe id="captcha-fixture" style="width:0;height:120px;border:0"></iframe>',
    '<iframe id="captcha-fixture" style="width:160px;height:0;border:0"></iframe>',
    '<div style="transform:scale(0)">'+CAPTCHA+'</div>',
    '<div style="opacity:0"><div style="opacity:1">'+CAPTCHA+'</div></div>',
    '<div style="display:none">'+CAPTCHA+'</div>',
    '<div style="visibility:hidden"><div style="visibility:visible">'+CAPTCHA+'</div></div>',
    '<iframe id="captcha-fixture" style="visibility:collapse;width:160px;height:120px;border:0"></iframe>',
],ids=['zero-area','zero-width','zero-height','zero-scale','transparent-ancestor',
       'display-none','overridden-hidden-ancestor','collapsed'])
def test_inactive_shadow_captcha_placeholder_does_not_require_verification(offline_page,html):
    mount_shadow(offline_page,html)
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)


@pytest.mark.parametrize('style',['opacity:0','display:none','visibility:hidden','visibility:collapse'])
@pytest.mark.parametrize('level',['host','ancestor'])
def test_hidden_wujie_host_or_host_ancestor_hides_all_verification_controls(offline_page,style,level):
    html='<div class="post-create"><input placeholder="验证码"><button>微信扫码登录</button>'+CAPTCHA+'</div>'
    mount_shadow(offline_page,html,**{level+'_style':style})
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)


@pytest.mark.parametrize('html,style,expected',[
    (CAPTCHA,'',dict(sms=False,complex=True,login=False)),
    (CAPTCHA,'display:contents',dict(sms=False,complex=True,login=False)),
    (CAPTCHA,'opacity:0.01',dict(sms=False,complex=True,login=False)),
    (CAPTCHA,'position:absolute;left:-9999px',dict(sms=False,complex=True,login=False)),
    ('<input placeholder="验证码">','',dict(sms=True,complex=False,login=False)),
    ('<div class="post-create"><button>微信扫码登录</button></div>','',dict(sms=False,complex=False,login=True)),
    ('<div class="weui-desktop-dialog"><button>管理员本人验证</button></div>','',dict(sms=False,complex=True,login=False)),
],ids=['visible-captcha','contents-host','positive-opacity','offscreen-captcha','sms','login','admin-confirmation'])
def test_rendered_shadow_verification_remains_detected(offline_page,html,style,expected):
    mount_shadow(offline_page,html,host_style=style)
    assert offline_page.evaluate(CHALLENGE)==expected


INTERCEPT_TEXTS=['管理员本人验证','需在手机上进行确认','直接发表']


@pytest.mark.parametrize('text',INTERCEPT_TEXTS)
@pytest.mark.parametrize('field',['caption','video-title'])
@pytest.mark.parametrize('surface',['document','wujie'])
def test_video_metadata_is_not_an_original_or_admin_verification_dialog(offline_page,text,field,surface):
    caption=text if field=='caption' else 'Fixture caption'
    title=text if field=='video-title' else 'Fixture video title'
    html=f'''<div class="post-create"><div class="post-desc-box">
      <div class="input-editor" contenteditable="true"><span>{caption}</span></div></div>
      <h2 class="video-title">{title}</h2><div class="form-btns"><button>发表</button></div></div>'''
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)


@pytest.mark.parametrize('text',INTERCEPT_TEXTS)
@pytest.mark.parametrize('surface',['document','wujie'])
def test_visible_original_or_admin_intercept_dialog_still_requires_verification(offline_page,text,surface):
    html=f'<div class="weui-desktop-dialog"><button><span>{text}</span></button></div>'
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=True,login=False)


@pytest.mark.parametrize('text',INTERCEPT_TEXTS)
@pytest.mark.parametrize('style',['opacity:0','display:none','visibility:hidden'])
def test_hidden_original_or_admin_intercept_dialog_does_not_require_verification(offline_page,text,style):
    mount_shadow(offline_page,f'''<div class="weui-desktop-dialog" style="{style}">
      <button style="visibility:visible"><span>{text}</span></button></div>''')
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)


FORM='''<div class="post-create-wrap"><div class="post-create">
  <div class="post-desc-box"><div class="input-editor" contenteditable="true">Fixture caption</div></div>
  <div class="post-time-wrap"><input placeholder="请选择发表时间" value="2026-10-10 12:00"></div>
  <div class="post-short-title-wrap"><input value="Fixture short title"></div>
  <div class="form-btns"><button>发表</button></div>
</div></div>'''


def mount_forms(page,styles):
    mount_shadow(page,''.join('<div class="form-fixture">'+FORM+'</div>' for _ in styles))
    page.evaluate('''styles => {
      const root=document.querySelector('#wujie-host').shadowRoot;
      [...root.querySelectorAll('.form-fixture')].forEach((fixture,index) => {
        fixture.style.cssText=styles[index];
        const el=fixture.querySelector('.post-create');
        const combinedStore={postObjDesc:{description:'Fixture caption',media:[{url:'https://example.invalid/fixture.mp4',thumbUrl:'https://example.invalid/cover'}]},
          tempPost:{topics:[]},canPost:true,createPostStatus:{}};
        el.__vue__={$el:el,$children:[],postState:{},
          $refs:{postCreate:{$el:el,combinedStore,originState:{effectiveTime:0},finder:{finderUsername:'fixture-finder'}}}};
      });
    }''',styles)


@pytest.mark.parametrize('store', [
    {}, {'postObjDesc': {}}, {'tempPost': {}},
    {'postObjDesc': None, 'tempPost': {}}, {'postObjDesc': {}, 'tempPost': None},
    {'postObjDesc': [], 'tempPost': {}}, {'postObjDesc': {}, 'tempPost': []}, [],
], ids=['empty', 'missing-temp', 'missing-desc', 'null-desc', 'null-temp',
        'array-desc', 'array-temp', 'array-store'])
def test_partial_store_reads_as_not_ready_without_inventing_empty_state(offline_page, store):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''store => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      el.__vue__.$refs.postCreate.combinedStore=store;
    }''', store)
    value=offline_page.evaluate(READ_FORM)
    assert value['form_count'] == 1
    assert value['state_ready'] is False
    assert value['no_extras'] is False and value['upload_complete'] is False


@pytest.mark.parametrize('field', ['postObjDesc', 'tempPost'])
@pytest.mark.parametrize('kind', ['date', 'instance'])
def test_non_record_store_fields_are_not_ready(offline_page, field, kind):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''args => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      el.__vue__.$refs.postCreate.combinedStore[args.field]=
        args.kind==='date'?new Date():new (class UnprovedState {})();
    }''', dict(field=field, kind=kind))
    value=offline_page.evaluate(READ_FORM)
    assert value['state_ready'] is False
    assert value['no_extras'] is False and value['upload_complete'] is False


def test_visible_form_shell_without_component_state_is_not_ready(offline_page):
    mount_shadow(offline_page, FORM)
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count'] == 1 and value['form_count'] == 0
    assert value['state_ready'] is False
    assert value['no_extras'] is False and value['upload_complete'] is False


def test_original_visible_form_can_become_ready_after_store_initializes(offline_page):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''() => {
      const inner=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__.$refs.postCreate;
      inner.fixtureReadyStore=inner.combinedStore;inner.combinedStore={};
    }''')
    initial=offline_page.evaluate(READ_FORM)
    assert initial['state_ready'] is False and initial['upload_complete'] is False
    offline_page.evaluate('''() => {
      const inner=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__.$refs.postCreate;
      inner.combinedStore=inner.fixtureReadyStore;delete inner.fixtureReadyStore;
    }''')
    value=offline_page.evaluate(READ_FORM)
    assert value['state_ready'] is True
    assert value['no_extras'] is True and value['upload_complete'] is True


@pytest.mark.parametrize('kind', ['microapp-realm', 'null-prototype'])
def test_plain_records_from_microapp_realm_or_without_prototype_are_ready(offline_page, kind):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''kind => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      const store=el.__vue__.$refs.postCreate.combinedStore;
      const frame=document.createElement('iframe');document.body.append(frame);
      for(const field of ['postObjDesc','tempPost']) {
        const record=kind==='microapp-realm'?new frame.contentWindow.Object():Object.create(null);
        store[field]=Object.assign(record,store[field]);
      }
    }''', kind)
    value=offline_page.evaluate(READ_FORM)
    assert value['state_ready'] is True
    assert value['no_extras'] is True and value['upload_complete'] is True


def test_multiple_visible_component_forms_never_supply_ready_state(offline_page):
    mount_forms(offline_page, ['', ''])
    value=offline_page.evaluate(READ_FORM)
    assert value['form_count'] == 2
    assert value['state_ready'] is False
    assert value['no_extras'] is False and value['upload_complete'] is False


@pytest.mark.parametrize('style',['opacity:0','display:none','visibility:hidden','visibility:collapse','transform:scale(0)'])
def test_hidden_form_is_not_a_live_candidate_for_readback(offline_page,style):
    mount_forms(offline_page,[style])
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count']==value['short_count']==value['button_count']==value['form_count']==0
    assert value['caption']==value['time_text']==value['short_title']=='' and value['button_enabled'] is False
    assert value['state_ready'] is False
    assert value['no_extras'] is False and value['upload_complete'] is False


@pytest.mark.parametrize('level',['host','ancestor'])
def test_transparent_shadow_host_hides_readback_form(offline_page,level):
    mount_forms(offline_page,[''])
    selector='#wujie-host' if level=='host' else '#ancestor'
    offline_page.locator(selector).evaluate("e => e.style.opacity='0'")
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count']==value['short_count']==value['button_count']==value['form_count']==0
    assert value['upload_complete'] is False and value['button_enabled'] is False
    assert value['state_ready'] is False and value['no_extras'] is False


def test_hidden_old_vue_form_does_not_pollute_current_form_readback(offline_page):
    mount_forms(offline_page,['opacity:0',''])
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count']==value['short_count']==value['button_count']==value['form_count']==1
    assert value['caption']=='Fixture caption' and value['state_caption']=='Fixture caption'
    assert value['short_title']=='Fixture short title' and value['time_text']=='2026-10-10 12:00'
    assert value['finder_uid']=='fixture-finder' and value['upload_complete'] is True and value['button_enabled'] is True
    assert value['state_ready'] is True


@pytest.mark.parametrize('style',['display:contents','opacity:0.01','position:absolute;left:-9999px'])
def test_rendered_form_remains_readable_in_background_or_contents_host(offline_page,style):
    mount_forms(offline_page,[''])
    offline_page.locator('#wujie-host').evaluate('(e,style) => e.style.cssText=style',style)
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count']==value['short_count']==value['button_count']==value['form_count']==1
    assert value['button_enabled'] is True and value['finder_uid']=='fixture-finder'


def test_zero_size_form_controls_are_not_accepted_as_visible_fields(offline_page):
    mount_forms(offline_page,[''])
    offline_page.evaluate('''() => {
      const root=document.querySelector('#wujie-host').shadowRoot;
      for(const e of root.querySelectorAll('input,.input-editor,button'))
        e.style.cssText='width:0;height:0;min-width:0;min-height:0;padding:0;border:0;overflow:hidden';
    }''')
    value=offline_page.evaluate(READ_FORM)
    assert value['editor_count']==value['short_count']==value['button_count']==0
    assert value['caption']==value['time_text']==value['short_title']=='' and value['button_enabled'] is False


@pytest.mark.parametrize('kind',['comment','text','document-fragment','document','undefined','svg','microapp-element'])
def test_component_tree_non_element_roots_cannot_break_form_read(offline_page,kind):
    mount_forms(offline_page,[''])
    offline_page.evaluate('''kind => {
      const root=document.querySelector('#wujie-host').shadowRoot;
      const el=root.querySelector('.post-create');let node;
      if(kind==='comment')node=document.createComment('v-if');
      else if(kind==='text')node=document.createTextNode('fixture text');
      else if(kind==='document-fragment')node=document.createDocumentFragment();
      else if(kind==='document')node=document;
      else if(kind==='svg')node=document.createElementNS('http://www.w3.org/2000/svg','svg');
      else if(kind==='microapp-element'){
        const frame=document.createElement('iframe');document.body.append(frame);
        node=frame.contentDocument.createElement('div');node.textContent='fixture other realm';
        frame.contentDocument.body.append(node);
      }
      if(node&&node.nodeType!==9&&kind!=='microapp-element')el.append(node);
      el.__vue__.$children.push({$el:node,$children:[]});
    }''',kind)
    value=offline_page.evaluate(READ_FORM)
    assert value['form_count']==1 and value['state_ready'] is True
    assert value['upload_complete'] is True and value['no_extras'] is True
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)


def test_cross_realm_connected_form_root_keeps_its_live_state(offline_page):
    # A realm-local instanceof Element test would reject this valid root.
    mount_forms(offline_page,[''])
    offline_page.evaluate('''() => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      const frame=document.createElement('iframe');document.body.append(frame);
      const foreign=frame.contentDocument.createElement('div');foreign.textContent='fixture rendered root';
      frame.contentDocument.body.append(foreign);el.__vue__.$el=foreign;
      el.__vue__.$refs.postCreate.$el=foreign;
    }''')
    value=offline_page.evaluate(READ_FORM)
    assert value['form_count']==1 and value['state_ready'] is True and value['upload_complete'] is True


def test_all_shipped_channels_observation_expressions_run_on_blank_document(offline_page):
    from yijianjifa.webui.channels_auth import IDENTITY_SCRIPT
    offline_page.set_content('<div id="app"></div>')
    assert offline_page.evaluate(IDENTITY_SCRIPT) is None
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)
    raw=offline_page.evaluate(READ_FORM)
    assert raw['form_count']==0 and raw['state_ready'] is False


def test_actual_upload_read_handles_connected_vue_comment_without_submitting(
        offline_page,db_engine,app_paths,tmp_path):
    from types import SimpleNamespace
    from yijianjifa.features.library.media_identity import sha256_file
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    mount_shadow(offline_page,'''<div class="post-create" style="width:500px;height:300px">
      <div class="post-desc-box"><div class="input-editor" contenteditable="true"></div></div>
      <div class="upload-wrap"><input type="file" accept="video/*"></div></div>''')
    offline_page.evaluate('''() => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      const comment=document.createComment('v-if');el.append(comment);
      el.__vue__={$el:el,$children:[{$el:comment,$children:[]}],postState:{},
        $refs:{postCreate:{$el:el,combinedStore:{postObjDesc:{media:[],description:''},tempPost:{topics:[]},canPost:false}}}};
    }''')
    video=tmp_path/'fixture.mp4';video.write_bytes(b'offline synthetic file input')
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths)
    s=SimpleNamespace(payload={'video_path':str(video),'video_hash':sha256_file(video)},uploaded=False)
    assert adapter._upload(s,offline_page) is True and s.uploaded is True
    assert offline_page.locator('input[type="file"]').evaluate('e=>e.files.length')==1
    assert offline_page.evaluate(CHALLENGE)==dict(sms=False,complex=False,login=False)
    # No final button or final-authority callback exists in this fixture.
    adapter.close()


REMINDER='''<div class="intercept-original-dialog weui-desktop-dialog">
<h3>声明原创的视频有机会获得广告分成</h3>
<p>你已加入创作分成计划，优质原创视频的评论区有机会展示广告，获得分成收益。</p>
<button class="cancel-btn">直接发表</button><button>声明原创</button></div>'''


@pytest.mark.parametrize('surface',['document','wujie'])
@pytest.mark.parametrize('tag',['a','div'])
def test_native_custom_button_root_is_not_required_to_be_html_button(offline_page,surface,tag):
    from yijianjifa.webui.channels_multi_publish import (ORIGINAL_REMINDER,
        ORIGINAL_REMINDER_DIAGNOSTIC, ORIGINAL_REMINDER_BUTTON)
    html=REMINDER.replace('<button class="cancel-btn">直接发表</button>',
        f'<{tag} class="cancel-btn" onclick="this.dataset.clicked=\'yes\'">直接发表</{tag}>')
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    assert offline_page.evaluate(ORIGINAL_REMINDER) is True
    checks=offline_page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC)
    assert checks['button_count']==1 and checks['button_enabled']
    control=offline_page.locator(ORIGINAL_REMINDER_BUTTON)
    assert control.count()==1
    control.click()
    assert control.get_attribute('data-clicked')=='yes'


@pytest.mark.parametrize('surface',['document','wujie'])
def test_no_tip_preference_is_recognized_without_a_direct_publish_candidate(offline_page,surface):
    from yijianjifa.webui.channels_multi_publish import ORIGINAL_REMINDER, ORIGINAL_REMINDER_DIAGNOSTIC
    html=REMINDER.replace('class="cancel-btn"','class="native-custom-root"').replace('</div>',
        '<div class="no-tip-btn">不再提醒</div></div>')
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    assert offline_page.evaluate(ORIGINAL_REMINDER) is True
    checks=offline_page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC)
    assert checks['button_count']==0 and checks['preference_count']==1


@pytest.mark.parametrize('attributes', ['disabled', 'aria-disabled="true"',
    'class="weui-desktop-btn_disabled"', 'style="pointer-events:none"'])
def test_disabled_custom_direct_control_cannot_continue(offline_page,attributes):
    from yijianjifa.webui.channels_multi_publish import ORIGINAL_REMINDER
    attrs=attributes.replace('class="','class="cancel-btn ') if attributes.startswith('class=') else 'class="cancel-btn" '+attributes
    html=REMINDER.replace('<button class="cancel-btn">直接发表</button>',
        '<a '+attrs+'>直接发表</a>')
    mount_shadow(offline_page,html)
    assert offline_page.evaluate(ORIGINAL_REMINDER) is False


@pytest.mark.parametrize('preference', [
    '<div class="no-tip-btn">同意协议</div>',
    '<div class="no-tip-btn" aria-disabled="true">不再提醒</div>',
    '<div class="no-tip-btn">不再提醒</div><div class="no-tip-btn">不再提醒</div>',
])
def test_invalid_preference_without_direct_control_is_not_a_continuation(offline_page,preference):
    from yijianjifa.webui.channels_multi_publish import ORIGINAL_REMINDER
    mount_shadow(offline_page,REMINDER.replace('class="cancel-btn"','class="native-custom-root"')
        .replace('</div>',preference+'</div>'))
    assert offline_page.evaluate(ORIGINAL_REMINDER) is False


@pytest.mark.parametrize('surface',['document','wujie'])
@pytest.mark.parametrize('nested',[False,True])
def test_native_rendered_dialog_without_component_class_is_found(offline_page,surface,nested):
    from yijianjifa.webui.channels_multi_publish import (ORIGINAL_REMINDER, ORIGINAL_REMINDER_DIAGNOSTIC,
        ORIGINAL_REMINDER_PREFERENCE, ORIGINAL_REMINDER_BUTTON)
    # mp-dialog can retain the component class on its hidden mounting node;
    # the visible native dialog carries only the shared desktop class.
    dialog=REMINDER.replace('intercept-original-dialog ','').replace('</div>',
        '<div class="no-tip-btn" onclick="this.dataset.clicked=\'yes\'">不再提醒</div></div>')
    if nested:dialog='<div class="intercept-original-dialog">'+dialog+'</div>'
    html='<div class="intercept-original-dialog" style="display:none"></div>'+dialog
    if surface=='wujie':mount_shadow(offline_page,html)
    else:offline_page.set_content(html)
    assert offline_page.evaluate(CHALLENGE)['complex'] is True
    assert offline_page.evaluate(ORIGINAL_REMINDER) is True
    checks=offline_page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC)
    assert checks['dialog_count']==checks['button_count']==checks['preference_count']==1
    preference=offline_page.locator(ORIGINAL_REMINDER_PREFERENCE)
    assert preference.count()==1
    preference.click()
    assert preference.get_attribute('data-clicked')=='yes'
    assert offline_page.locator(ORIGINAL_REMINDER_BUTTON).count()==1


@pytest.mark.parametrize('surface',['document','wujie'])
def test_only_the_exact_native_monetization_reminder_is_a_continuation(offline_page,surface):
    from yijianjifa.webui.channels_multi_publish import ORIGINAL_REMINDER, ORIGINAL_REMINDER_DIAGNOSTIC
    if surface=='wujie':mount_shadow(offline_page,REMINDER)
    else:offline_page.set_content(REMINDER)
    assert offline_page.evaluate(ORIGINAL_REMINDER) is True
    checks=offline_page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC)
    assert checks['dialog_count']==checks['button_count']==1
    assert checks['title_matches'] and checks['body_matches'] and checks['button_enabled']
    assert all(type(v) in (bool,int) for v in checks.values())


@pytest.mark.parametrize('html',[
    REMINDER.replace('intercept-original-dialog','declare-original-dialog'),
    REMINDER.replace('声明原创的视频有机会获得广告分成','原创权益'),
    REMINDER.replace('你已加入创作分成计划','请先同意原创协议'),
    REMINDER.replace('class="cancel-btn"','class="cancel-btn" disabled'),
    REMINDER.replace('直接发表','同意协议'),
    REMINDER+REMINDER,
    REMINDER+CAPTCHA,
    REMINDER+'<div class="weui-desktop-dialog">管理员本人验证</div>',
    REMINDER+'<div class="declare-original-dialog">原创权益</div>',
    '<div style="display:none">'+REMINDER+'</div>',
])
def test_original_rights_captcha_ambiguity_and_hidden_controls_are_never_auto_clicked(offline_page,html):
    from yijianjifa.webui.channels_multi_publish import ORIGINAL_REMINDER, ORIGINAL_REMINDER_DIAGNOSTIC
    mount_shadow(offline_page,html)
    assert offline_page.evaluate(ORIGINAL_REMINDER) is False
    assert all(type(v) in (bool,int) for v in offline_page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC).values())


def mount_current_component_form(page, *, post_state=False, inner_state='live'):
    """Observed parent/ref/inner structure, without uploading any fixture media."""
    mount_shadow(page, FORM)
    page.evaluate('''args => {
      const root=document.querySelector('#wujie-host').shadowRoot;
      const parent=root.querySelector('.post-create-wrap'),innerEl=root.querySelector('.post-create');
      const inner={$el:innerEl,$children:[],combinedStore:{postObjDesc:{description:'',media:[]},tempPost:{topics:[]},canPost:false},finder:{finderUsername:'fixture-finder'}};
      const vm={$el:parent,$children:[inner],$refs:{postCreate:inner}};
      if(args.post_state)vm.postState={};
      if(args.inner_state==='detached')inner.$el=document.createElement('div');
      if(args.inner_state==='hidden')innerEl.style.display='none';
      if(args.inner_state==='elsewhere'){inner.$el=document.createElement('div');inner.$el.textContent='Unrelated old component';document.body.append(inner.$el);}
      parent.__vue__=vm;
    }''', dict(post_state=post_state, inner_state=inner_state))


@pytest.mark.parametrize('post_state', [False, True], ids=['current-without-unused-field', 'legacy'])
def test_live_parent_and_inner_ref_supply_empty_form_without_unused_post_state(offline_page, post_state):
    mount_current_component_form(offline_page, post_state=post_state)
    raw=offline_page.evaluate(READ_FORM)
    assert raw['form_count'] == 1
    assert raw['state_ready'] is True
    assert raw['finder_uid'] == 'fixture-finder'
    assert raw['media_count'] == 0 and raw['upload_complete'] is False


@pytest.mark.parametrize('inner_state', ['detached', 'hidden', 'elsewhere'])
def test_stale_inner_ref_cannot_supply_the_visible_parent_store(offline_page, inner_state):
    mount_current_component_form(offline_page, post_state=True, inner_state=inner_state)
    raw=offline_page.evaluate(READ_FORM)
    assert raw['form_count'] == 0
    assert raw['state_ready'] is False and raw['upload_complete'] is False
