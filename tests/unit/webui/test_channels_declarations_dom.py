"""Normal control actions on network-blocked synthetic official-style DOMs."""
from types import SimpleNamespace
import pytest

from test_channels_dom_visibility import offline_browser, offline_page, mount_shadow
from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter, CHALLENGE
from yijianjifa.webui import channels_publish_form as form


def fixture_form(page, *, duplicate=False, name=None):
    name = name or form.declarations.AI_LABEL
    html = '''<div class="post-create"><div class="post-desc-box">
      <div class="input-editor" contenteditable="true">Fixture caption</div></div>
      <div class="post-with-mark-tag"><div class="mark-tag-select"><div class="select-display">
      <span class="select-placeholder">选择视频标注</span></div><div class="mark-tag-options" style="display:none"></div></div></div>
      <div class="declare-original-checkbox"><label style="position:relative;display:inline-block">
      <input type="checkbox" style="opacity:0;position:absolute;width:16px;height:16px">声明后，作品将展示原创标记。</label></div>
      <div class="form-btns"><button>发表</button></div></div>
      <div class="weui-desktop-dialog declare-original-dialog" style="display:none">
      <span>原创权益</span><input type="checkbox" id="protocol"><span>我已阅读并同意原创声明须知</span>
      <button disabled>声明原创</button></div>'''
    mount_shadow(page, html)
    page.evaluate('''args => {
      const root=document.querySelector('#wujie-host').shadowRoot, el=root.querySelector('.post-create');
      const media={url:'https://example.invalid/owned.mp4',md5sum:'synthetic-uuid',fileSize:1000,
        width:1920,height:1080,videoPlayLen:6,thumbUrl:'https://example.invalid/cover',mediaType:4};
      const store={postObjDesc:{description:'Fixture caption',media:[media]},tempPost:{topics:[],originalFlag:0},canPost:true,createPostStatus:{}};
      const vm={$el:el,$children:[],postState:{},checkOriginalFlag:false,allowOriginalAuth:true,tagInfo:null,
        $refs:{postCreate:{$el:el,combinedStore:store,originState:{effectiveTime:0},finder:{finderUsername:'fixture-finder'}}}};
      el.__vue__=vm;
      const tagEl=root.querySelector('.post-with-mark-tag'), options=root.querySelector('.mark-tag-options');
      const rows=Array.from({length:args.duplicate?2:1},()=>({tagType:431,tagName:args.name}));
      const tagVm={$el:tagEl,markTagOptions:rows,optionsLoading:false,selectedTag:null,currentTagInfo:null,tagSecKey:'synthetic-live-key'};
      tagEl.__vue__=tagVm;
      for(const row of rows){
        const option=document.createElement('div');option.className='mark-tag-option';
        const main=document.createElement('div');main.className='option-main';main.textContent=row.tagName;option.append(main);
        option.addEventListener('click',event=>{event.stopPropagation();tagVm.selectedTag=row;
          tagVm.currentTagInfo={tagType:row.tagType,tagKey:tagVm.tagSecKey};vm.tagInfo={...tagVm.currentTagInfo};
          const display=root.querySelector('.select-display');display.innerHTML='<span class="select-value"></span>';
          display.firstChild.textContent=row.tagName;options.style.display='none';});options.append(option);
      }
      root.querySelector('.mark-tag-select').addEventListener('click',()=>options.style.display='block');
      const original=root.querySelector('.declare-original-checkbox input'), dialog=root.querySelector('.declare-original-dialog');
      original.addEventListener('click',event=>{event.preventDefault();dialog.style.display='block';});
      const protocol=root.querySelector('#protocol'),confirm=dialog.querySelector('button');
      protocol.addEventListener('change',()=>confirm.disabled=!protocol.checked);
      confirm.addEventListener('click',()=>{vm.checkOriginalFlag=true;original.checked=true;dialog.style.display='none';});
    }''', dict(name=name, duplicate=duplicate))
    f = form.validate_form({'fields': {'title': 'Fixture caption', 'channels_original': True,
        'content_declaration_choice': 'ai_generated'}, 'execution_mode': 'publish_now'})
    return f


def test_normal_ai_control_freezes_current_mapping_then_original_protocol_needs_human(
        offline_page, db_engine, app_paths):
    f = fixture_form(offline_page)
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    s = SimpleNamespace(owned_declaration=None, owned_media=offline_page.evaluate(form.READ_FORM)['media'])
    raw = offline_page.evaluate(form.READ_FORM)
    # Transparent Ant input is still the connected ordinary checkbox.
    assert raw['original_control_count'] == 1 and raw['original_checked'] is False
    adapter._set_content_declaration(s, offline_page, raw, f)
    raw = offline_page.evaluate(form.READ_FORM)
    assert form.content_declaration_matches(raw, f)
    s.owned_declaration = form.safe_tag_info(raw['declaration_tag_info'])
    assert s.owned_declaration == {'tagType': 431, 'tagKey': 'synthetic-live-key'}
    adapter._set_original(s, offline_page, raw, f)
    assert offline_page.evaluate(CHALLENGE)['complex'] is True
    raw = offline_page.evaluate(form.READ_FORM)
    assert raw['state_original'] is False and raw['original_dialog'] is True
    assert not offline_page.locator('#protocol').is_checked()
    assert not form.verify_form(raw, f, s.owned_media, 'fixture-finder', s.owned_declaration)
    # The fixture's explicit human action agrees and confirms on the same page.
    offline_page.locator('#protocol').check()
    offline_page.locator('.declare-original-dialog button').click()
    raw = offline_page.evaluate(form.READ_FORM)
    assert offline_page.evaluate(CHALLENGE)['complex'] is False
    assert form.verify_form(raw, f, s.owned_media, 'fixture-finder', s.owned_declaration)
    adapter.close()


@pytest.mark.parametrize('change', ['duplicate', 'different_name'])
def test_changed_or_ambiguous_ai_menu_stops_before_any_choice_click(offline_page, db_engine, app_paths, change):
    f = fixture_form(offline_page, duplicate=change == 'duplicate',
        name='内容由AI生成' if change == 'different_name' else None)
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    s = SimpleNamespace(owned_declaration=None)
    with pytest.raises(ValueError, match='unavailable or changed'):
        adapter._set_content_declaration(s, offline_page, offline_page.evaluate(form.READ_FORM), f)
    assert not getattr(s, 'content_selection_attempted', False)
    assert offline_page.evaluate(form.READ_FORM)['declaration_selected_count'] == 0
    adapter.close()


@pytest.mark.parametrize('style', ['opacity:0', 'display:none', 'visibility:hidden'])
def test_hidden_original_protocol_dialog_is_not_a_challenge(offline_page, style):
    mount_shadow(offline_page, '<div class="declare-original-dialog" style="'+style+'"><button>声明原创</button></div>')
    assert offline_page.evaluate(CHALLENGE)['complex'] is False


def test_none_and_false_never_add_a_tag_or_open_original_protocol(offline_page, db_engine, app_paths):
    fixture_form(offline_page)
    f = form.validate_form({'fields': {'title': 'Fixture caption', 'content_declaration_choice': 'none',
                                     'channels_original': False}, 'execution_mode': 'publish_now'})
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    raw = offline_page.evaluate(form.READ_FORM)
    s = SimpleNamespace(owned_declaration=None, owned_media=raw['media'])
    adapter._set_content_declaration(s, offline_page, raw, f)
    adapter._set_original(s, offline_page, raw, f)
    assert offline_page.evaluate(CHALLENGE)['complex'] is False
    raw = offline_page.evaluate(form.READ_FORM)
    assert raw['original_checked'] is False and raw['declaration_selected_count'] == 0
    assert form.verify_form(raw, f, s.owned_media, 'fixture-finder')
    adapter.close()


@pytest.mark.parametrize('cause', ['short_video', 'disabled_control'])
def test_original_request_with_short_video_or_disabled_entrance_never_opens_protocol(
        offline_page, db_engine, app_paths, cause):
    fixture_form(offline_page)
    f = form.validate_form({'fields': {'title': 'Fixture caption', 'channels_original': True}, 'execution_mode': 'publish_now'})
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    raw = offline_page.evaluate(form.READ_FORM)
    s = SimpleNamespace(owned_declaration=None, owned_media=raw['media'])
    if cause == 'short_video':s.owned_media['videoPlayLen'] = 3
    else:offline_page.locator(form.ORIGINAL_INPUT).evaluate('e => e.disabled=true')
    with pytest.raises(ValueError):adapter._set_original(s, offline_page, raw, f)
    assert not getattr(s, 'original_selection_attempted', False)
    assert offline_page.evaluate(CHALLENGE)['complex'] is False
    adapter.close()
