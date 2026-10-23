"""Bounded recovery of an empty Douyin login widget, never of verification.

DOM observations only guide initial presentation. The caller still verifies
platform identity twice before accepting or persisting any account session.
"""
import logging
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)


HOME = 'https://creator.douyin.com/creator-micro/home'

READ_LOGIN_SURFACE = r'''() => {
 const visible = e => {
   if (!e?.isConnected || !Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0)) return false;
   for(let n=e;n;n=n.parentElement){
     const s=getComputedStyle(n);
     if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)<=0) return false;
   }
   return true;
 };
 const labels=new Set(['我是创作者','我是MCN机构','扫码登录','发布高清视频','发布视频','发布图文','发布作品','作品管理','内容管理','退出登录']);
 const challengeText=/请完成.*验证|拖动.*滑块|请使用已登录账号的设备扫码|安全验证/;
 const words=[];
 // Avoid forcing layout for every marketing-page ancestor twice a second.
 if(document.body){
   const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
   let node, count=0;
   while((node=walker.nextNode())&&count++<20000){
     const text=(node.textContent||'').trim();
     if((labels.has(text)||challengeText.test(text))&&visible(node.parentElement)) words.push(text);
   }
 }
 const inputs=Array.from(document.querySelectorAll('input')).filter(e=>visible(e)&&!e.readOnly&&
   e.type!=='checkbox'&&e.type!=='radio'&&e.getAttribute('role')!=='combobox'&&
   (e.type==='tel'||e.type==='password'||/手机|电话|验证码|密码|phone|password|verification|code/i.test(
     [e.placeholder,e.getAttribute('aria-label'),e.name].join(' '))));
 const qr=Array.from(document.querySelectorAll('img,canvas')).some(e=>visible(e)&&
   ((e.tagName==='IMG'&&e.complete&&e.naturalWidth>0)||e.tagName==='CANVAS')&&
   /二维码|qr.?code/i.test([e.getAttribute('alt'),e.getAttribute('aria-label'),e.id,e.className].join(' ')));
 const challenge=Array.from(document.querySelectorAll('iframe,[class*="captcha"],[id*="captcha"]')).some(e=>visible(e)&&
   ((e.tagName==='IFRAME'&&/captcha|verify|验证/i.test([e.src,e.id,e.className].join(' ')))||
    /captcha/i.test([e.id,e.className].join(' '))))||
   words.some(t=>challengeText.test(t));
 return {
   loading:document.readyState==='loading',
   blank:!document.body||(document.body.innerText||'').trim().length===0,
   landing:words.some(t=>t==='我是创作者'||t==='我是MCN机构'),
   scan:words.includes('扫码登录'), form:inputs.length>0,
   // Only booleans leave the page: no input, QR payload or session is read out.
   qr, challenge, interacting:inputs.some(e=>Boolean(e.value)),
   // A partially mounted marketing shell is not an authenticated dashboard.
   dashboard:words.some(t=>['发布高清视频','发布视频','发布图文','发布作品','作品管理','内容管理','退出登录'].includes(t))
 };
}'''


class DouyinLoginBootstrap:
    def __init__(self, started):
        self.last_navigation = started
        self.reloads = 0
        self.scan_selected = False
        self.protected = False
        self.last_surface = None

    def poll(self, page, now):
        """Return whether to try the separate, strict identity observation."""
        url = urlsplit(page.url)
        if url.scheme != 'https' or url.netloc != 'creator.douyin.com':
            return False
        state = page.evaluate(READ_LOGIN_SURFACE)
        keys = ('loading', 'blank', 'landing', 'scan', 'form', 'qr', 'challenge', 'interacting', 'dashboard')
        if type(state) is not dict or any(type(state.get(k)) is not bool for k in keys):
            return False
        if state != self.last_surface:
            # The probe contains booleans only; never log page/input/session data.
            logger.debug('Douyin login presentation: %s', {key: state[key] for key in keys})
            self.last_surface = dict(state)
        if state['qr'] or state['challenge'] or state['interacting']:
            self.protected = True
        if state['challenge'] or state['interacting'] or state['qr']:
            return False
        if state['scan'] and not self.scan_selected and not self.protected:
            # One normal click per document, even if actionability times out.
            # Retrying clicks every poll can itself keep the widget restarting.
            self.scan_selected = True
            try:
                page.get_by_text('扫码登录', exact=True).filter(visible=True).first.click(timeout=3000)
                logger.debug('Douyin login: selected scan tab')
            except Exception as error:
                message=str(error)
                reason=next((label for snippet,label in (
                    ('intercepts pointer events','covered'),('not stable','moving'),
                    ('not visible','hidden'),('navigations','navigation')) if snippet in message),'unavailable')
                logger.debug('Douyin login: scan tab unavailable (%s, %s)', type(error).__name__,reason)
            return False
        login_visible = state['landing'] or state['scan'] or state['form']
        if state['dashboard'] and not state['loading'] and not state['blank'] and not login_visible:
            return True
        # Empty phone fields may accompany a stuck QR placeholder. Only actual
        # input/challenge/loaded QR protects the user's ongoing login from reload.
        if (not self.protected and self.reloads < 2
                and now - self.last_navigation >= 12
                and url.path.rstrip('/') in {'/creator-micro/home', '/creator-micro/login'}):
            self.reloads += 1
            self.last_navigation = now
            self.scan_selected = False
            logger.debug('Douyin login: empty widget reload %d/2', self.reloads)
            page.reload(wait_until='commit', timeout=10000)
        return False
