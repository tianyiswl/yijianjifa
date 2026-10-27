"""Computed CSS in offline Chromium, including the actual publishing page rules."""
from pathlib import Path
import re
from playwright.sync_api import sync_playwright


def test_publishing_picker_keeps_compact_checkbox_rows(tmp_path):
    assets=Path(__file__).parents[3]/'src/yijianjifa/webui/assets'
    styles='\n'.join(re.findall(r'<style[^>]*>(.*?)</style>',(assets/'commerce.html').read_text(),re.S))
    styles+='\n'+(assets/'common.css').read_text()
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1280,'height':850})
            page.set_content('<style>'+styles+'</style><main class="publish-workspace"><section class="publish-setup-column compact"><div class="p011-account-picker" style="width:240px"><button id="trigger"></button><div id="menu"></div></div></section></main>')
            page.add_script_tag(content=(assets/'account_picker.js').read_text())
            page.evaluate('''() => {
             const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
             window.picker=P011AccountPicker.create({el,trigger:document.querySelector('#trigger'),panel:document.querySelector('#menu'),onToggle(){}});
             picker.update({accounts:[{id:'b',platform:'bilibili',nickname:'逆浪风',login_state:'valid'},{id:'c',platform:'channels',nickname:'逆浪风',login_state:'expired'},{id:'d',platform:'douyin',nickname:'八道',login_state:'valid'}],selectedIds:['b','c'],platformLabels:{bilibili:'B站',channels:'视频号',douyin:'抖音'}});
            }''')
            page.locator('#trigger').click()
            sizes=page.locator('#menu input').evaluate_all('(nodes)=>nodes.map(n=>({height:n.getBoundingClientRect().height,width:n.getBoundingClientRect().width,row:n.closest("label").getBoundingClientRect().height}))')
            assert len(sizes)==4
            assert all(14<=s['height']<=16 and 14<=s['width']<=16 and s['row']<=38 for s in sizes),sizes
            assert page.locator('#menu').bounding_box()['height']<=280
            assert page.locator('#menu').get_by_text('需要登录').is_visible()
        finally:browser.close()


def test_sidebar_help_and_notification_footer_are_compact_and_aligned():
    assets=Path(__file__).parents[3]/'src/yijianjifa/webui/assets'
    styles='\n'.join(re.findall(r'<style[^>]*>(.*?)</style>',(assets/'commerce.html').read_text(),re.S))+'\n'+(assets/'common.css').read_text()
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1280,'height':850})
            page.set_content('<style>'+styles+'</style><div class="shell"><aside></aside></div>')
            page.evaluate('''()=>{window.P011Version='0.1.48';window.webkit={messageHandlers:{p011App:{postMessage:async()=>JSON.stringify({ok:true,data:{unread:2}})}}}}''')
            page.add_script_tag(content=(assets/'notifications.js').read_text())
            page.evaluate('()=>{window.dispose=P011Notifications()}')
            help=page.locator('.p011-help-links').bounding_box()
            assert help['height']<=36,help
            version=page.get_by_text('版本 0.1.48').bounding_box()
            notice=page.locator('.p011-notify button').filter(has_text='通知').bounding_box()
            assert abs(version['y']+version['height']/2-notice['y']-notice['height']/2)<=2
            assert notice['width']<100
            assert page.locator('.p011-notify').bounding_box()['height']<=100
        finally:browser.close()
