"""真实离线浏览器核对未变化进度的DOM稳定性，不读取用户资料。"""
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.parametrize('phase',['making','needs_login'])
def test_unchanged_progress_poll_does_not_mutate_rendered_dom(phase):
    asset=Path(__file__).resolve().parents[3]/'src/yijianjifa/webui/assets/mixing_execution.js'
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='chromium',headless=True)
        try:
            page=browser.new_page();page.route('**/*',lambda route:route.abort())
            page.set_content('<div id="summary"></div><div id="host"></div>')
            page.add_script_tag(path=str(asset))
            changes=page.evaluate('''async phase=>{
                const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
                const button=(text,fn)=>{const n=el('button',text);n.addEventListener('click',fn);return n;};
                const panel=P011MixExecution({host:document.querySelector('#host'),summaryHost:document.querySelector('#summary'),el,button,onClear:async()=>{},onDetails(){},onContinue(){},onCopy:async()=>{}});
                const data={batch:{id:'synthetic'},status:{phase,total:3,accepted:0,execution:{making:{succeeded:1,completed:1,failed:0,total:3}}}};
                panel.render(data);
                const changes=[],observer=new MutationObserver(records=>changes.push(...records.map(r=>({type:r.type,attribute:r.attributeName}))));
                observer.observe(document.body,{subtree:true,attributes:true,childList:true,characterData:true});
                for(let i=0;i<5;i++)panel.render(data);
                await new Promise(resolve=>setTimeout(resolve,0));observer.disconnect();panel.dispose();return changes;
            }''',phase)
            assert changes==[]
        finally:browser.close()
