"""Actual browser menu/SPA navigation; fixture HTTP never reaches a platform."""
import time
import json
from types import SimpleNamespace

import pytest

from test_channels_dom_visibility import offline_browser, offline_page
from test_channels_multi_publish import case
from yijianjifa.webui import channels_publish_form as form
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


HOME = 'https://channels.weixin.qq.com/platform'
HTML = r'''<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app">
<ul class="finder-ui-desktop-menu finder-ui-desktop-menu_global">
<li><a class="finder-ui-desktop-menu__sub__link" id="content">内容管理</a>
<ul id="submenu" style="display:none"><li><a class="finder-ui-desktop-menu__link finder-ui-desktop-menu__only-icon" id="video">\n 视频 \n</a></li></ul></li></ul>
<div id="micro"></div><div class="side-bar-footer">Fixture identity</div></div>
<script>
window.navigationEvents=[];window.contentActions=[];
// Characterize the real regression: an unauthenticated deep-link bootstrap
// falls back to HOME; an ordinary in-document menu transition mounts the app.
if(location.pathname.endsWith('/create'))history.replaceState({},'', '/platform');
const finder={finderUsername:'finder-test',nickname:'合成视频号'};
window._store={userStore:{hasLogin:true,isExpired:false,finder}};
const footer=document.querySelector('.side-bar-footer');
document.querySelector('#app').__vue__={$children:[{$el:footer,$children:[],finder}]};
const host=document.querySelector('#micro');let shadow=host.attachShadow({mode:'open'});
// Identical menu classes inside Wujie must not collide with the outer menu.
shadow.innerHTML='<a class="finder-ui-desktop-menu__sub__link" onclick="navigationEvents.push(\'wrong-content\')">内容管理</a>'+Array.from({length:65},()=>'<a class="finder-ui-desktop-menu__link finder-ui-desktop-menu__only-icon" onclick="navigationEvents.push(\'wrong-video\')">视频</a>').join('');
document.querySelector('#content').onclick=()=>{navigationEvents.push('content');document.querySelector('#submenu').style.display='block';};
document.querySelector('#video').onclick=()=>{
 navigationEvents.push('video');history.pushState({},'', '/platform/post/list');
 shadow.innerHTML='<div class="post-list"><div class="option"><div class="weui-desktop-btn_wrp"><button class="weui-desktop-btn weui-desktop-btn_primary">发表视频</button></div></div></div>';
 shadow.querySelector('button').onclick=()=>{
  navigationEvents.push('create');history.pushState({},'', '/platform/post/create');
  shadow.innerHTML='<div class="post-create-wrap"><div class="post-create"><div class="post-edit-wrap material-edit-wrap"><div class="post-desc-box"><div class="input-editor" contenteditable="true"></div></div><div class="post-short-title-wrap"><input></div><input type="file" onchange="contentActions.push(\'file\')"></div><div class="form-btns"><button onclick="contentActions.push(\'final\')">发表</button></div></div></div>';
  const parent=shadow.querySelector('.post-create-wrap'),el=shadow.querySelector('.post-edit-wrap');
  const inner={$el:el,$children:[],combinedStore:{postObjDesc:{media:[],description:''},tempPost:{topics:[]},canPost:false},finder};
  parent.__vue__={$el:parent,$children:[inner],$refs:{postCreate:inner},postState:{}};
 };
};
</script></body></html>'''.replace('>\\n 视频 \\n<', '>\n 视频 \n<')


def mount_navigation(page):
    page.context.route('https://channels.weixin.qq.com/**', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
    page.goto(HOME)


def test_baseline_uses_menu_then_list_entry_instead_of_reloading_create(case, offline_page, db_engine):
    adapter, payload, _, _, _, _ = case
    adapter.step_timeout = 5
    assert adapter.validate_target(payload, payload, payload['execution_mode']).authorized
    session = adapter._session(payload)
    adapter._navigate_upload_form = type(adapter)._navigate_upload_form.__get__(adapter)
    mount_navigation(offline_page)
    session.page = offline_page
    result = adapter.submit(payload, context=None, before_final=lambda: None)
    assert result.state == 'platform_scheduled'
    assert offline_page.evaluate('navigationEvents') == ['content', 'video', 'create']
    assert offline_page.evaluate('contentActions') == []
    assert offline_page.url == form.UPLOAD_URL
    assert offline_page.evaluate(form.READ_FORM)['form_count'] == 1
    with session_scope(db_engine) as db:
        log = json.loads(db.get(Setting, 'publish.execution:target:1').value_json)
    navigation = [e['checks'] for e in log if e['step'] == 'channels.navigation']
    assert {e['target'] for e in navigation if e['ready']} == {'content_menu', 'video_list', 'create_form'}
    assert all(type(v) in (bool, int) or v in {'content_menu', 'video_list', 'create_form'}
               for e in navigation for v in e.values())


def session_for(page, **changes):
    return SimpleNamespace(page=page, final=False, uploaded=False, **changes)


def navigate(adapter, session):
    return adapter._navigate_upload_form(session, time.monotonic() + 5)


def test_outer_menu_is_unique_even_when_shadow_app_has_same_menu_classes(db_engine, app_paths, offline_page):
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    mount_navigation(offline_page)
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    session = session_for(offline_page)
    for _ in range(5):
        if navigate(adapter, session):
            break
    assert offline_page.evaluate('navigationEvents') == ['content', 'video', 'create']
    assert offline_page.evaluate('contentActions') == []
    assert offline_page.evaluate(form.READ_FORM)['form_count'] == 1
    adapter.close()


@pytest.mark.parametrize('blocked', ['cancelled', 'final', 'uploaded'])
def test_navigation_does_not_touch_cancelled_or_already_owned_content(db_engine, app_paths, offline_page, blocked):
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    mount_navigation(offline_page)
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    session = session_for(offline_page)
    if blocked == 'cancelled': adapter.cancel.set()
    else: setattr(session, blocked, True)
    assert navigate(adapter, session) is False
    assert offline_page.evaluate('navigationEvents') == []
    adapter.close()


def test_duplicate_list_entries_are_not_clicked(db_engine, app_paths, offline_page):
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    mount_navigation(offline_page)
    offline_page.evaluate("() => {document.querySelector('#content').click();document.querySelector('#video').click();const r=document.querySelector('#micro').shadowRoot;r.querySelector('.option').append(r.querySelector('button').cloneNode(true));}")
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    assert navigate(adapter, session_for(offline_page)) is False
    assert offline_page.evaluate('navigationEvents') == ['content', 'video']
    assert offline_page.evaluate('contentActions') == []
    adapter.close()


def test_form_final_button_is_never_a_navigation_entry(db_engine, app_paths, offline_page):
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    mount_navigation(offline_page)
    offline_page.evaluate("() => {document.querySelector('#content').click();document.querySelector('#video').click();document.querySelector('#micro').shadowRoot.querySelector('button').click();}")
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    assert navigate(adapter, session_for(offline_page)) is True
    assert offline_page.evaluate('contentActions') == []
    adapter.close()
