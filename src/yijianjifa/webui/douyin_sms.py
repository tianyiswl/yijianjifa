"""SMS controls on the original page, scoped away from publishing controls."""
import re
import time

INPUT = 'input[placeholder*="验证码"]:visible, input[aria-label*="验证码"]:visible'
REJECTED = re.compile(r'验证码(?:错误|有误|不正确|已过期|失效)|请重新获取验证码')
TITLE = re.compile(r'^(?:接收短信验证码|短信验证码|短信验证|安全验证|身份验证)$')
REQUEST = re.compile(r'^(?:(?:获取|发送|重新获取|重新发送)(?:短信)?验证码|重新获取|重新发送)$')
# A bare Confirm can belong to publication outside a portal-mounted modal.
# This Douyin helper only accepts explicit verification actions.
SUBMIT = re.compile(r'^(?:验证|确认验证|确认验证码|提交验证码)$')
ACTION_TIMEOUT_MS = 8000


class SMSControlError(ValueError):
    """Only our fixed, code-free explanations may reach the client."""


def safe_error(error, fallback):
    return str(error) if isinstance(error, SMSControlError) else fallback


def _buttons(dialog, pattern):
    return dialog.get_by_text(pattern).filter(visible=True)


def controls(surface):
    field = surface.locator(INPUT)
    if field.count() != 1:
        raise SMSControlError('未找到唯一的短信验证码输入框，请查看原验证页面。')
    # The explanation and input can be in a modal body while Verify is in
    # its sibling footer. The nearest ancestor mentioning SMS is too narrow.
    ancestors = field.locator('xpath=ancestor::*[not(self::body) and not(self::html)]')
    fallback = None
    for index in reversed(range(ancestors.count())):
        dialog = ancestors.nth(index)
        if _buttons(dialog, TITLE).count() == 0:
            continue
        boundary = dialog.evaluate(r'''e=>e.matches('[role="dialog"],[aria-modal="true"],dialog') ||
            Array.from(e.classList).some(c=>/(?:^|[-_])(?:modal|dialog|popup)(?:[-_](?:content|container|panel|wrapper))?$/i.test(c))''')
        # Stop at the actual modal even if it has lost a control. A shared
        # portal above it may contain a separate publishing confirmation.
        if boundary:
            return field, dialog
        if fallback is None and _buttons(dialog, SUBMIT).count() == 1:
            fallback = dialog
    if fallback is not None:
        return field, fallback
    raise SMSControlError('未找到完整的短信验证窗口及验证按钮，请打开验证页面查看。')


def rejection(surface):
    try:
        _, dialog = controls(surface)
        return bool(REJECTED.search(dialog.inner_text(timeout=1500)))
    except Exception:
        return False


def countdown_seconds(surface):
    _,dialog=controls(surface)
    match=re.search(r'(?<!\d)(\d{1,3})\s*(?:s\b|秒)',dialog.inner_text(timeout=1500))
    return int(match[1]) if match else 0


def is_request_click(surface,points):
    _,dialog=controls(surface)
    return dialog.evaluate(r'''(dialog,points)=>points.some(([x,y])=>{
        const hit=document.elementFromPoint(x,y);
        for(let node=hit;node&&dialog.contains(node);node=node.parentElement){
            const label=(node.innerText||node.textContent||'').trim();
            if(/^(?:(?:获取|发送|重新获取|重新发送)(?:短信)?验证码|重新获取|重新发送|\d{1,3}\s*(?:s|秒))$/.test(label))return true;
            if(node===dialog)break;
        }return false;
    })''',points)


def request_code(surface,*,before_send=None):
    _, dialog = controls(surface)
    # Preserve a code already requested manually or by the platform itself.
    if re.search(r'(?<!\d)\d{1,3}\s*(?:s\b|秒)', dialog.inner_text(timeout=1500)):
        return True
    button = _buttons(dialog, REQUEST)
    if button.count() != 1:
        raise SMSControlError('未找到唯一的获取验证码入口，请打开验证页面查看。')
    if before_send:before_send()
    button.click(timeout=ACTION_TIMEOUT_MS)
    return True


def submit_code(surface, code):
    try:
        field, dialog = controls(surface)
        button = _buttons(dialog, SUBMIT)
        if button.count() != 1:
            raise SMSControlError('未找到唯一的短信验证按钮，请查看原验证页面。')
        field.fill(code, timeout=ACTION_TIMEOUT_MS)
        code = ''
        # React may enable a DIV button only after the input event is processed.
        boundary = dialog.element_handle(timeout=1500)
        deadline = time.monotonic() + ACTION_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            # get_by_text may match an inner SPAN. Its enclosing DIV or BUTTON
            # owns the disabled state; normal clicking must wait for that too.
            enabled = button.evaluate(r'''(e,dialog) => {
                for(let node=e;node&&dialog?.contains(node);node=node.parentElement){
                    if(node.disabled||node.getAttribute('aria-disabled')==='true'||
                       /(?:^|[-_\s])disabled?(?:$|[-_\s])/i.test(node.className||''))return false;
                    if(node===dialog)return true;
                }return false;
            }''',boundary)
            if enabled:
                button.click(timeout=ACTION_TIMEOUT_MS)
                surface.wait_for_timeout(200)
                if rejection(surface):
                    raise SMSControlError('平台提示验证码错误或已失效，请重新输入或获取。')
                return
            surface.wait_for_timeout(100)
        raise SMSControlError('平台验证按钮尚未就绪，请查看原验证页面后重试。')
    except SMSControlError:
        raise
    except Exception:
        # Playwright's raw fill errors may echo the code. Never expose them.
        raise SMSControlError('验证码未能提交到原验证页面，请查看页面后重试。') from None
    finally:
        code = ''
