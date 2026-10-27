"""Local HTML only; never open a creator site or submit a real video."""
import pytest
from playwright.sync_api import sync_playwright
from yijianjifa.webui import submit_feedback as feedback


@pytest.mark.parametrize('message,state',[
    ('发布成功','accepted'),('定时发布成功','accepted'),('投稿成功','accepted'),
    ('发表成功，审核通过后可见','accepted'),('上传成功',None),
    ('发布失败：今天投稿次数已达到上限，请明天再试','rejected'),
    ('投稿失败：标题包含违规内容','rejected'),
    ('发布失败：网络异常，请稍后重试','unknown'),
    ('验证码错误，请重新输入',None),
    ('今日投稿次数还剩3次',None),('今日发布上限为10条',None),
    ('发布成功率有所提升',None),
])
def test_only_final_notices_are_captured(message,state):
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content('<main></main>')
            feedback.install(page)
            page.locator('main').evaluate('(e,t)=>{const n=document.createElement("div");n.className="semi-toast";n.textContent=t;e.append(n)}',message)
            page.evaluate('()=>new Promise(r=>setTimeout(r,0))')
            value=feedback.read(page)
            assert (value or {}).get('state')==state
            if state:
                page.locator('.semi-toast').evaluate('(e)=>e.remove()')
                assert feedback.read(page)==value
        finally:browser.close()


@pytest.mark.parametrize('kind',['stale','caption','hidden','edited-stale'])
def test_stale_hidden_and_user_content_cannot_complete_submission(kind):
    with sync_playwright() as driver:
        browser=driver.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content('<main><div role="status">发布成功</div></main>')
            feedback.install(page)
            if kind=='edited-stale':page.locator('[role=status]').evaluate('(e)=>e.textContent="投稿成功"')
            elif kind!='stale':
                page.locator('main').evaluate('(e,k)=>{const n=document.createElement("div");n.textContent="发布成功";if(k==="caption")n.contentEditable="true";else{n.className="semi-toast";n.style.display="none"}e.append(n)}',kind)
            page.evaluate('()=>new Promise(r=>setTimeout(r,0))')
            assert feedback.read(page) is None
        finally:browser.close()


def test_platform_reason_is_bounded_and_private_values_are_redacted():
    message=feedback.safe_message('发布失败：请联系13312345678，https://example.com/?token=secret Cookie: abc')
    assert '发布失败' in message
    assert all(value not in message for value in ('13312345678','example.com','secret','abc'))
    assert len(feedback.safe_message('标题过长'*200))<=200
