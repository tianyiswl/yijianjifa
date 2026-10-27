"""在真实离线浏览器检查取消弹窗期间整页轮询是否重复改写界面。"""
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright


@pytest.mark.parametrize('result_wait',[False,True])
@pytest.mark.parametrize('confirm_pending',[False,True])
def test_cancel_dialog_poll_keeps_unchanged_controls_stable(confirm_pending,result_wait):
    assets=Path(__file__).resolve().parents[3]/'src/yijianjifa/webui/assets'
    with sync_playwright() as driver:
        browser=driver.chromium.launch(channel='chromium',headless=True)
        try:
            page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)));page.route('**/*',lambda route:route.abort())
            page.set_content((assets/'mixing.html').read_text(encoding='utf-8'))
            page.add_style_tag(path=str(assets/'common.css'))
            page.evaluate('(value)=>{window.qaResultWait=value}',result_wait)
            page.evaluate('''() => {
                window.qaTimers=[];const original=setTimeout;
                window.setTimeout=(fn,delay,...args)=>delay===1200?(window.qaTimers.push(fn),100000+window.qaTimers.length):original(fn,delay,...args);
                window.P011Views={};window.P011Router={dailyBatchId:'mine'};window.qaStatusReads=0;window.qaCalls=[];window.qaCancelled=false;
                const account={id:'a',platform:'douyin',nickname:'合成账号',login_state:'valid',is_default:true};
                window.webkit={messageHandlers:{p011App:{postMessage:async raw=>{
                    const q=JSON.parse(raw);window.qaCalls.push(q.operation);let data={};
                    if(q.operation==='mix.daily.read')data={settings:null,accounts:[account],capabilities:{douyin:['platform_schedule']},has_saved_settings:false};
                    if(q.operation==='mix.daily.status'){window.qaStatusReads++;data={id:'mine',revision:window.qaStatusReads,phase:window.qaCancelled?'cancelled':'needs_attention',can_continue:!window.qaCancelled,publish_task_ids:[],execution:{making:{total:2,completed:0,succeeded:0,failed:0},timing:{active:false},publishing:{targets:window.qaResultWait?[{id:'unknown',account_id:'a',platform:'douyin',account_label:'抖音 · 合成账号',state:'result_unknown'}]:[],complete:true,omitted:0}}};}
                    if(q.operation==='task.cancel'){data=await new Promise(resolve=>{window.qaResolveCancel=()=>{window.qaCancelled=true;resolve({id:'mine',state:'cancelled'});};window.qaCancelWaiting=true;});}
                    if(q.operation==='preset.mix.list'||q.operation==='category.list')data={items:[],total:0};
                    if(q.operation==='library.list')data={items:[],total:q.params.kind==='music'?0:1};
                    if(q.operation==='voice.catalog')data={selected:'',favorites:[],items:[]};
                    if(q.operation==='settings.output.read')data={directory:'C:/synthetic'};
                    if(q.operation==='settings.mix_preset.read')data={id:''};
                    return JSON.stringify({ok:true,data});
                }}}};
            }''')
            for name in ['write_intent','account_login','mixing_form','account_picker','mixing_publish','mixing_advanced','mixing_execution','mixing']:
                page.add_script_tag(path=str(assets/(name+'.js')))
            page.evaluate('() => {window.qaDispose=P011Views.mixing();}')
            try:page.wait_for_function('window.qaStatusReads>0 && window.qaTimers.length>0')
            except Exception as error:raise AssertionError(page.evaluate('({calls:window.qaCalls,toast:document.querySelector("#toast").textContent,feedback:document.querySelector("#daily-save-state").textContent,timers:window.qaTimers.length})'),errors) from error
            if result_wait:
                banner=page.locator('#mixing-batch-progress .batch-progress-login')
                assert banner.is_visible()
                assert '自动流程已暂停' in banner.inner_text()
                assert banner.locator('p').evaluate('(e)=>getComputedStyle(e).maxHeight')=='none'
                assert page.locator('#daily-progress').get_by_role('button',name='查看待核对任务',exact=True).is_visible()
            page.locator('#daily-progress').get_by_role('button',name='取消本批',exact=True).click()
            assert page.locator('#modal-root').is_visible()
            if confirm_pending:
                page.locator('#modal-footer').get_by_role('button',name='确认取消',exact=True).click()
                page.wait_for_function('window.qaCancelWaiting===true')
            changes=page.evaluate('''async()=>{
                const records=[],observer=new MutationObserver(rows=>records.push(...rows.map(r=>({id:r.target.id,attribute:r.attributeName,type:r.type}))));
                observer.observe(document.body,{subtree:true,attributes:true,childList:true,characterData:true});
                for(let i=0;i<3;i++)await window.qaTimers.shift()();
                await new Promise(resolve=>setTimeout(resolve,0));observer.disconnect();return records;
            }''')
            assert changes==[],changes
            assert page.locator('#modal-root').is_visible()
            if confirm_pending:
                page.evaluate('window.qaResolveCancel()')
                page.wait_for_function('document.querySelector("#modal-root").classList.contains("hide")')
                assert page.evaluate('window.qaCalls.filter(x=>x==="task.cancel").length')==1
            page.evaluate('window.qaDispose()')
        finally:browser.close()
