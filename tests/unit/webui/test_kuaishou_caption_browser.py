"""仅用离线合成页面验证正常编辑操作，不访问平台或提交视频。"""
from types import SimpleNamespace
import pytest
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter
from yijianjifa.webui import kuaishou_publish_form as contract


@pytest.fixture
def creator_page():
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='chromium',headless=True)
        context=browser.new_context()
        context.route('**/*',lambda route:route.abort())
        page=context.new_page()
        page.set_content('''<div id="work-description-edit" contenteditable="true" style="position:absolute;left:20px;top:20px;width:300px;height:80px"></div>
<div style="position:absolute;left:20px;top:20px;width:300px;height:80px;z-index:10;background:white">合成遮挡层</div>
<button style="position:absolute;left:20px;top:200px">立即发布</button>
<script>window.inputEvents=0;document.querySelector('#work-description-edit').addEventListener('input',()=>window.inputEvents++);</script>''')
        try:yield page
        finally:context.close();browser.close()


def setup_adapter(page,monkeypatch):
    session=SimpleNamespace(page=page,form_filled=False,final=False)
    adapter=object.__new__(KuaishouMultiPublishAdapter)
    def observed(s,frame):
        editors=frame.locator(contract.EDITOR)
        return {'caption':editors.inner_text() if editors.count() else '', 'tags':[],
                'mode':'立即发布','time_text':'','upload_complete':True,'editor_count':editors.count(),
                'button_count':1,'button_enabled':True,'declaration_count':1,
                'declaration_variant':'legacy','declaration_label':'','declaration_item_count':0}
    monkeypatch.setattr(adapter,'_observed_form',observed)
    form={'text':'合成标题\n合成正文','caption':'合成标题\n合成正文','tags':[],
          'mode':'立即发布','time_text':'','declaration':{'choice':'none','name':'','source':None}}
    return session,adapter,form


def test_pointer_blocked_editor_still_uses_standard_fill_and_readback(creator_page,monkeypatch):
    page=creator_page;session,adapter,form=setup_adapter(page,monkeypatch)
    with pytest.raises(BrowserTimeout):page.locator(contract.EDITOR).click(timeout=200)
    assert adapter._prepare_form(session,page,form) is False
    assert adapter._prepare_form(session,page,form) is True
    assert page.locator(contract.EDITOR).inner_text()==form['text']
    assert page.evaluate('window.inputEvents')>0
    assert session.form_filled and session.final is False


def test_readonly_editor_is_not_modified_or_submitted(creator_page,monkeypatch):
    page=creator_page;page.locator('#work-description-edit').evaluate("e=>{e.textContent='原有文案';e.contentEditable='false';}")
    session,adapter,form=setup_adapter(page,monkeypatch)
    assert adapter._prepare_form(session,page,form) is False
    assert page.locator('#work-description-edit').inner_text()=='原有文案'
    assert page.evaluate('window.inputEvents')==0
    assert not session.form_filled and session.final is False
