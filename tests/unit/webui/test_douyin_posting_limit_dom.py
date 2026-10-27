"""离线验证抖音短暂投稿限制提示，只接受本次提交后的明确提示。"""
import pytest
from playwright.sync_api import sync_playwright
from yijianjifa.webui.chromium_multi_publish import POSTING_LIMIT_WATCH


@pytest.mark.parametrize('kind',['new','stale','hidden','caption','other'])
def test_posting_limit_observer_is_scoped_to_new_visible_toast(kind):
    message='抱歉，今天投稿次数已达到上限，请明天再试'
    with sync_playwright() as driver:
        browser=driver.chromium.launch(channel='chromium',headless=True)
        try:
            page=browser.new_page()
            page.set_content('<main></main>')
            if kind=='stale':page.locator('main').evaluate('(e,t)=>{e.innerHTML="<div class=semi-toast></div>";e.firstChild.textContent=t}',message)
            page.evaluate('()=>{'+POSTING_LIMIT_WATCH+'}')
            page.locator('main').evaluate('''(e,args)=>{
                if(args.kind==='stale'){e.setAttribute('data-changed','yes');return;}
                const n=document.createElement('div');n.className=args.kind==='caption'?'caption':'semi-toast';
                if(args.kind==='hidden')n.style.display='none';
                n.textContent=args.kind==='other'?'网络异常，请稍后重试':args.message;e.append(n);
                if(args.kind==='new')n.remove();
            }''',{'kind':kind,'message':message})
            # 平台提示显示一个微任务后移除，记录仍应保留；未曾可见的节点不能作为拒绝证据。
            if kind=='new':
                page.locator('main').evaluate('(e,t)=>{const n=document.createElement("div");n.className="semi-toast";n.textContent=t;e.append(n)}',message)
                page.wait_for_function('window.p011PostingLimitWatch.seen===true')
                page.locator('.semi-toast').last.evaluate('(e)=>e.remove()')
            else:page.evaluate('()=>new Promise(resolve=>setTimeout(resolve,0))')
            assert page.evaluate('window.p011PostingLimitWatch.seen')==(kind=='new')
        finally:browser.close()
