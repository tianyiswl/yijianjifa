"""Independent, identity-bound video-channel creator browser adapter.

The official helper owns upload and publishing requests. We use normal browser
controls and passively correlate its responses. A final journal owns one attempt. Known pre-request native reminders may
continue that frozen attempt; subsequent recovery never creates another upload.
"""
from yijianjifa.features.publish.account_wait import AccountBusy
from pathlib import Path
import json
import logging
import math
import os
import re
import time
from urllib.parse import urlsplit
from sqlalchemy import text, select

from yijianjifa.features.accounts.service import AccountRow, AccountIdentityMismatch
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.features.publish.deliveries import PublishDeliveryRow, DeliveryAttemptRow, digest
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.app_services import channels_declaration_catalog as declarations
from .chromium_publish_session import ChromiumPublishSessionAdapter
from . import chromium_publish as browser
from . import channels_auth as auth
from . import creator_auth
from . import channels_publish_form as form_contract


CHALLENGE = r'''() => {
 const roots=[document],seen=new Set();
 for(let i=0;i<roots.length&&roots.length<=40;i++)for(const e of roots[i].querySelectorAll('*'))
   if(e.shadowRoot&&!seen.has(e.shadowRoot)){seen.add(e.shadowRoot);roots.push(e.shadowRoot);}
 const all=q=>roots.flatMap(r=>[...r.querySelectorAll(q)]);
 const visible=e=>{
   if(!e?.isConnected||!Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
   for(let node=e;node;node=node.parentElement||node.getRootNode()?.host){
     const style=getComputedStyle(node);
     if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse'||Number(style.opacity)<=0)return false;
   }
   // A rendered control may be offscreen in the background browser. Traverse
   // shadow hosts too, because Wujie's document lives inside an open shadow tree.
   return true;
 };
 const sms=all('input').filter(e=>visible(e)&&/验证码/.test([e.placeholder,e.getAttribute('aria-label')].join(' ')));
 const leaves=all('body *, .post-create *, .weui-desktop-dialog *').filter(e=>visible(e)&&!e.children.length).map(e=>e.textContent.trim());
 // These words may also be user-authored metadata. Only the confirmed dialog
 // container provides evidence of an original-content or admin interruption.
 const dialogLeaves=all('.weui-desktop-dialog, .weui-desktop-dialog *').filter(e=>visible(e)&&!e.children.length).map(e=>e.textContent.trim());
 const complex=all('iframe,[class*="captcha"],[id*="captcha"]').some(e=>visible(e)&&/captcha|verify|验证/i.test([e.src,e.id,e.className].join(' ')))||
   dialogLeaves.some(v=>['管理员本人验证','需在手机上进行确认','直接发表'].includes(v))||
   all('.declare-original-dialog').some(visible);
 const login=window._store?.userStore?.isExpired===true||/\/platform\/login(?:\/|-|$)/.test(location.pathname)||leaves.some(v=>['扫码登录','微信扫码登录'].includes(v));
 return {sms:sms.length===1,complex,login};
}'''


# Official 772.4b4c8d62.js: postFinder emits `post` and closes this
# reminder; confirmDeclare opens the separate, binding original-rights dialog.
# The component's custom class may stay on a hidden mounting node while
# mp-dialog renders the actual shared desktop dialog elsewhere. Select the
# innermost visible native container; the full text/form checks remain below.
ORIGINAL_REMINDER_DIALOG = ':is(.intercept-original-dialog,.weui-desktop-dialog):visible:not(:has(:is(.intercept-original-dialog,.weui-desktop-dialog):visible))'
ORIGINAL_REMINDER_BUTTON = ORIGINAL_REMINDER_DIALOG+' .cancel-btn:visible'
ORIGINAL_REMINDER_PREFERENCE = ORIGINAL_REMINDER_DIALOG+' .no-tip-btn:visible'
_REMINDER_DOM = r'''
 const roots=[document];
 for(let i=0;i<roots.length&&roots.length<=40;i++)for(const e of roots[i].querySelectorAll('*'))
   if(e.shadowRoot&&!roots.includes(e.shadowRoot))roots.push(e.shadowRoot);
 const all=q=>roots.flatMap(r=>[...r.querySelectorAll(q)]);
 const visible=e=>{
  if(!e?.isConnected||!Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
  for(let n=e;n;n=n.parentElement||n.getRootNode()?.host){const s=getComputedStyle(n);
   if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)<=0)return false;}
  return true;
 };
 const rendered=all('.intercept-original-dialog,.weui-desktop-dialog').filter(visible);
 const dialogs=rendered.filter(e=>!rendered.some(other=>other!==e&&e.contains(other)));
 // mp-button is a component; its rendered root need not be an HTML button.
 const enabled=e=>!!e&&!e.disabled&&!e.hasAttribute('disabled')&&
   e.getAttribute('aria-disabled')!=='true'&&!e.closest('[inert]')&&
   !e.classList.contains('weui-desktop-btn_disabled')&&!e.classList.contains('disabled')&&
   getComputedStyle(e).pointerEvents!=='none';
'''
ORIGINAL_REMINDER = '() => {' + _REMINDER_DOM + r'''
 if(window._store?.userStore?.isExpired===true||/\/platform\/login(?:\/|-|$)/.test(location.pathname))return false;
 if(dialogs.length!==1||all('.declare-original-dialog,iframe[class*="captcha"],iframe[id*="captcha"],[class*="captcha"],[id*="captcha"]').some(visible))return false;
 const d=dialogs[0];
 if(all('.weui-desktop-dialog').some(e=>visible(e)&&e!==d&&!d.contains(e)&&!e.contains(d)))return false;
 const texts=[...d.querySelectorAll('*')].filter(visible).map(e=>e.textContent.trim());
 const buttons=[...d.querySelectorAll('.cancel-btn')].filter(visible);
 const preferences=[...d.querySelectorAll('.no-tip-btn')].filter(visible);
 const preferenceReady=preferences.length===1&&enabled(preferences[0])&&preferences[0].textContent.trim()==='不再提醒';
 const directReady=buttons.length===1&&enabled(buttons[0])&&buttons[0].textContent.trim()==='直接发表';
 return texts.includes('声明原创的视频有机会获得广告分成')&&
   texts.includes('你已加入创作分成计划，优质原创视频的评论区有机会展示广告，获得分成收益。')&&
   (preferences.length?preferenceReady:directReady);
}'''

# A finite observation explains a rejected reminder without copying page text,
# credentials, media URLs or request bodies into the task log.
ORIGINAL_REMINDER_DIAGNOSTIC = '() => {' + _REMINDER_DOM + r'''
 const d=dialogs[0];
 const texts=[...(d?.querySelectorAll('*')||[])].filter(visible).map(e=>e.textContent.trim());
 const buttons=[...(d?.querySelectorAll('.cancel-btn')||[])].filter(visible);
 const preferences=[...(d?.querySelectorAll('.no-tip-btn')||[])].filter(visible);
 return {dialog_count:dialogs.length,
   login_blocked:window._store?.userStore?.isExpired===true||/\/platform\/login(?:\/|-|$)/.test(location.pathname),
   challenge_blocked:all('.declare-original-dialog,iframe[class*="captcha"],iframe[id*="captcha"],[class*="captcha"],[id*="captcha"]').some(visible),
   other_dialog:!!d&&all('.weui-desktop-dialog').some(e=>visible(e)&&e!==d&&!d.contains(e)&&!e.contains(d)),
   title_matches:texts.includes('声明原创的视频有机会获得广告分成'),
   body_matches:texts.includes('你已加入创作分成计划，优质原创视频的评论区有机会展示广告，获得分成收益。'),
   button_count:buttons.length,button_enabled:buttons.length===1&&enabled(buttons[0]),
   button_text_matches:buttons.length===1&&buttons[0].textContent.trim()==='直接发表',
   preference_count:preferences.length,preference_enabled:preferences.length===1&&enabled(preferences[0]),
   preference_text_matches:preferences.length===1&&preferences[0].textContent.trim()==='不再提醒'};
}'''


class ChannelsMultiPublishAdapter(ChromiumPublishSessionAdapter):
    platform_id = 'channels'
    # These are implementation/contract capabilities. Research and release
    # acceptance separately record that a real authorized submit is outstanding.
    verified_capabilities = frozenset({'ordinary', 'publish_now', 'software_schedule', 'platform_schedule', 'result_query'})
    public_field_schema = {
        'title': {'type': 'string'}, 'body': {'type': 'string', 'weighted_caption_limit': 2000},
        'tags': {'type': 'string', 'max_items': 20},
        'channels_short_title': {'type': 'string', 'optional': True, 'min_length': 6, 'max_length': 16},
        'content_declaration_choice': {'type': 'string', 'optional': True, 'choices_source': 'builtin',
            'choices': declarations.choices(), 'evidence_status': declarations.EVIDENCE_STATUS,
            'help': declarations.HELP},
        'channels_original': {'type': 'boolean', 'optional': True, 'default': False},
        'cover': {'kinds': ['first_frame'], 'custom_ready': False},
    }

    def __init__(self, *args, step_timeout=120, **kwargs):
        super().__init__(*args, step_timeout=step_timeout, **kwargs)
        self._validation_stage = 'page_open'
        self._validation_failure = None
        self._preparation_diagnostic = None
        self._prepare_gate_checks = None

    @staticmethod
    def _navigation_observation_error(error):
        if not type(error).__module__.startswith('playwright.'):
            return False
        message = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else ''
        return any(marker in message for marker in (
            'Execution context was destroyed', 'Cannot find context with specified id',
            'Frame was detached', 'Frame has been detached', 'because of a navigation'))

    @staticmethod
    def _validation_category(error, stage):
        from yijianjifa.features.publish.coordinator import failure_category
        message = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else ''
        playwright = type(error).__module__.startswith('playwright.')
        if stage == 'page_open' and playwright:
            if any(marker in message for marker in ('ProcessSingleton', 'profile appears to be in use',
                                                    'user data directory is already in use')):
                return 'profile_in_use'
            return 'browser_launch_failed'
        if message == 'publish page closed' or playwright and type(error).__name__ == 'TargetClosedError':
            return 'page_closed'
        if type(error).__module__ == 'greenlet' and 'switch to a different thread' in message.lower():
            return 'worker_thread_mismatch'
        if stage == 'session_restore':
            return 'session_unavailable'
        category = failure_category(error)
        return 'execution_failed' if category == 'page_context_unavailable' and not playwright else category

    def failure_diagnostic(self, error):
        # The coordinator consumes only finite preparation/verification enums,
        # never the exception's private text.
        result = self._validation_failure
        self._validation_failure = None
        return dict(result) if result else {}

    @staticmethod
    def _preparation_category(error):
        from yijianjifa.features.publish.coordinator import failure_category
        message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else ''
        if type(error) is ValueError:
            if message=='existing video-channel draft must be handled first':return 'existing_draft'
            if message in {'uploaded media or account unavailable','short title entrance unavailable for this account',
                    'video-channel schedule mode unavailable','video-channel date input unavailable',
                    'video-channel calendar unavailable','ambiguous calendar month','calendar labels changed',
                    'calendar navigation unavailable','calendar navigation limit','scheduled day unavailable',
                    'schedule time input unavailable','schedule did not commit'}:return 'form_changed'
            if message=='frozen video changed':return 'resource_unavailable'
        if type(error).__module__.startswith('playwright.') and any(marker in message for marker in
                ('TypeError:','ReferenceError:','SyntaxError:')):return 'javascript_read_failed'
        return failure_category(error)

    def _record_preparation_failure(self, error):
        """Log only finite categories and our own source frames, never page data."""
        known_types={'Error','TimeoutError','TargetClosedError','ValueError','RuntimeError','AccountIdentityMismatch'}
        error_type=type(error).__name__ if type(error).__name__ in known_types else 'other'
        message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else ''
        js='none'
        if type(error).__module__.startswith('playwright.'):
            if 'getClientRects is not a function' in message:js='node_geometry_unavailable'
            elif 'getComputedStyle' in message and ('not of type' in message or 'not an Element' in message):js='non_element_style_read'
            elif 'ReferenceError:' in message:js='reference_error'
            elif 'SyntaxError:' in message:js='syntax_error'
            elif 'TypeError:' in message:js='type_error'
            else:js='other_browser_error'
        methods={'_upload','_prepare_form','_set_schedule','_set_content_declaration','_set_original',
                 '_click_final','_advance_steps','_challenge','_identity','_verification_read'}
        source='unknown';line=0;frame=error.__traceback__
        while frame:
            code=frame.tb_frame.f_code
            if frame.tb_frame.f_globals.get('__name__')==__name__ and code.co_name in methods:
                source=code.co_name;line=frame.tb_lineno
            frame=frame.tb_next
        detail={'error_type':error_type,'js_kind':js,'source':'channels_multi_publish.py',
                'method':source,'line':line,'error_stage':self._preparation_stage,
                'error_category':self._preparation_category(error)}
        if self._prepare_gate_checks is not None and source=='_prepare_form':
            detail['upload_gate']=dict(self._prepare_gate_checks)
        self._preparation_diagnostic=detail
        if os.environ.get('P011_PUBLISH_DIAGNOSTICS')=='1':
            logging.getLogger(__name__).warning('channels preparation diagnostic: %s',json.dumps(detail,sort_keys=True))

    def preparation_diagnostic(self):
        return dict(self._preparation_diagnostic) if self._preparation_diagnostic else {}

    def _observe_form_gates(self, s, raw, f=None):
        """Keep only finite counts and gate booleans from the last form read."""
        if type(raw) is not dict:
            s.form_observation = {'checks': {'form_record': False}}
            return
        counts = {}
        for key in ('editor_count', 'form_count', 'media_count', 'short_count', 'button_count',
                    'original_control_count', 'declaration_component_count', 'declaration_selected_count'):
            value = raw.get(key)
            counts[key] = value if type(value) is int and -1 <= value <= 100 else 'unknown'
        flags = {'state_ready': raw.get('state_ready') is True,
                 'upload_complete': raw.get('upload_complete') is True,
                 'declaration_options_loading': raw.get('declaration_options_loading') is True}
        checks = {}
        if f is not None and 'caption' in f:
            checks.update(form_contract.form_verification_checks(raw, f, s.owned_media,
                          s.payload['platform_user_id'], s.owned_declaration))
        observation = {'counts': counts, 'flags': flags, 'checks': checks}
        # Use fixed key names and booleans only. Never retain the source values
        # or infer authorization from a self-consistent platform clip record.
        value = raw.get('media'); frozen = getattr(s, 'owned_media', None)
        observation['media_invalid_fields'] = (self._uploaded_gate_checks(raw, s.payload['platform_user_id'])['media_invalid_fields']
                                                if type(value) is dict else ['record'])
        keys = ('url', 'md5sum', 'fileSize', 'width', 'height', 'videoPlayLen', 'thumbUrl', 'coverUrl',
                'fullThumbUrl', 'fullCoverUrl', 'shareCoverUrl', 'cardShowStyle', 'mediaType')
        observation['frozen_media_changed_fields'] = [key for key in keys if
            type(value) is dict and type(frozen) is dict and
            ((key in value) != (key in frozen) or value.get(key) != frozen.get(key)
             or type(value.get(key)) is not type(frozen.get(key)))]
        for name, allowed in (
            ('extras_checks', ('no_component', 'no_member', 'ordinary_mode', 'no_bgm', 'no_external_link',
                              'empty_location', 'no_collection', 'empty_event', 'no_mentions', 'neutral_longitude',
                              'neutral_latitude', 'neutral_feed_longitude', 'neutral_feed_latitude', 'neutral_original_flag')),
            ('clip_bindings', ('async_processing', 'ticket_present', 'media_task_present', 'record_ticket_matches', 'processed_from_upload'))):
            item = raw.get(name)
            observation[name] = {key: item[key] if type(item) is dict and type(item.get(key)) is bool else 'unknown'
                                 for key in allowed}
        current_uuid = value.get('md5sum') if type(value) is dict else None
        frozen_uuid = frozen.get('md5sum') if type(frozen) is dict else None
        observation['clip_bindings']['upload_uuid_matches_frozen'] = (
            type(current_uuid) is str and bool(current_uuid) and type(frozen_uuid) is str
            and bool(frozen_uuid) and current_uuid == frozen_uuid)
        if f is not None and 'caption' in f:
            observation['caption_shape'] = self._caption_shape(raw, f['caption'])
        s.form_observation = observation
        self._trace(s,'channels.form_readback',{**counts,**flags,**checks,**observation['extras_checks'],**observation['clip_bindings']})

    @staticmethod
    def _caption_shape(raw, expected):
        result = {}
        expected = form_contract.clean_text(expected)
        for label, field in (('dom', 'caption'), ('state', 'state_caption')):
            text = form_contract.clean_text(raw.get(field))
            valid = type(text) is str and type(expected) is str
            result[label + '_is_text'] = type(text) is str
            result[label + '_matches_trimmed'] = valid and text.strip() == expected.strip()
            result[label + '_matches_nbsp_as_space'] = valid and text.replace('\u00a0', ' ') == expected.replace('\u00a0', ' ')
            result[label + '_matches_whitespace_collapsed'] = valid and ' '.join(text.split()) == ' '.join(expected.split())
            result[label + '_line_break_count_matches'] = valid and text.count('\n') == expected.count('\n')
            result[label + '_length_matches'] = valid and len(text) == len(expected)
            result[label + '_has_nbsp'] = type(text) is str and '\u00a0' in text
        dom, state = (form_contract.clean_text(raw.get(key)) for key in ('caption', 'state_caption'))
        result['dom_state_equal'] = type(dom) is str and type(state) is str and dom == state
        return result

    def _record_preparation_timeout(self, s):
        # No exception occurred. In particular, do not manufacture exception
        # text or make another page read after the deadline has expired.
        phase = s.phase if s.phase in {'baseline', 'prepare', 'result'} else 'unknown'
        detail = {'error_type': 'none', 'js_kind': 'none', 'source': 'channels_multi_publish.py',
                  'method': '_advance_steps', 'error_stage': self._preparation_stage,
                  'error_category': 'timeout', 'phase': phase,
                  'progress': {name: getattr(s, name, False) is True for name in
                      ('uploaded', 'form_filled', 'content_selection_attempted', 'original_selection_attempted')},
                  'form_observation': getattr(s, 'form_observation', None)}
        self._preparation_diagnostic = detail
        if os.environ.get('P011_PUBLISH_DIAGNOSTICS') == '1':
            logging.getLogger(__name__).warning('channels preparation diagnostic: %s', json.dumps(detail, sort_keys=True))

    def _verification_read(self, s, observe, *, identity=False):
        """Retry only observations, never upload, form edits or final clicks."""
        deadline = time.monotonic() + min(10, self.step_timeout)
        self._validation_failure = None
        try:
            while not self.cancel.is_set() and time.monotonic() < deadline:
                if s.page is None or s.page.is_closed():
                    raise ValueError('publish page closed')
                try:
                    return observe(s), True
                except AccountIdentityMismatch:
                    raise
                except Exception as error:
                    unavailable = (identity and type(error) is ValueError and error.args in
                        (('identity unavailable',), ('identity unavailable or changed',)))
                    if s.page.is_closed() or not (unavailable or self._navigation_observation_error(error)):
                        raise
                try:
                    s.page.wait_for_timeout(200)
                except Exception as error:
                    if s.page.is_closed() or not self._navigation_observation_error(error):
                        raise
            if s.page is None or s.page.is_closed():
                raise ValueError('publish page closed')
            return None, False
        except Exception as error:
            self._validation_failure = {'error_stage': 'verification_observation',
                'error_category': self._validation_category(error, 'verification_observation')}
            raise

    def _after_intervention_challenge(self, s):
        try:
            kind, ready = self._verification_read(s, self._challenge)
        except Exception:
            # consume precedes the shared step wrapper. A terminal pre-final
            # observation must release its page, broker and account lock too.
            # Post-final failures retain the original result-query session.
            if not s.final:self._release_page(s)
            raise
        s.verification_observation_pending = not ready
        # Keep the original request until the original page can be observed.
        # Returning a non-empty kind never grants a publish or identity action.
        return kind if ready else 'login'

    def _pause_verification_observation(self, s):
        message = ('原视频号页面仍在跳转或尚未就绪；已进入提交阶段，结果仍待核对，请勿重复投稿。'
                   if s.final else
                   '原视频号页面仍在跳转或尚未就绪，此目标尚未提交。请在原页面核对后明确继续，鞋匠不会重新上传。')
        result = self._park(s, 'login', message, reason='identity_unavailable')
        if s.request_id:s.broker.message(s.request_id, message)
        s.stage = '等待原视频号页面就绪'
        return result

    def _account(self, s):
        a = self.service.get(s.payload['account_id'])
        uid = s.payload['platform_user_id']
        if (a.platform != self.platform_id or a.platform_user_id != uid
                or self.expected_uid and self.expected_uid != uid):
            raise AccountIdentityMismatch('frozen account mismatch')
        auth.verified_identity({'origin': 'https://' + form_contract.HOST, 'logged_in': True,
                               'expired': False, 'uid': uid, 'profile_uid': uid, 'nickname': a.nickname})
        return a

    def _init_observations(self, s):
        s.list_seen = False; s.list_rows = []; s.form_filled = False
        s.form_observation = None
        s.owned_media = None; s.submit_ok = False; s.submit_rejected = False
        s.create_pending = {}; s.create_epoch = 0; s.create_settled = False
        s.create_ever_started = False; s.reminder_continued = False
        s.reminder_preference_pending = False
        s.query_wait_unresolved = False; s.recovery_action_pending = False
        s.original_selection_attempted = False
        s.content_selection_attempted = False; s.owned_declaration = None
        s.baseline_deadline = time.monotonic() + 5
        s.upload_navigation = 'menu'
        s.receipt = dict(s.payload.get('receipt_reference') or getattr(s, 'receipt', {}))

    def _open(self, s, *, query=False):
        if s.page is not None:
            return
        self._validation_stage = 'page_open'
        a = self._account(s)
        if not a.session_ref:
            raise ValueError('identity-bound session unavailable')
        lock = browser.account_lock(a.id)
        if not s.locked:
            if not lock.acquire(blocking=False):
                raise AccountBusy()
            s.locked = True
        with session_scope(self.engine) as db:
            row = db.get(Setting, 'publish.browser_mode')
            mode = json.loads(row.value_json) if row else 'background'
        s.external_browser = mode == 'visible'
        options = self._inspection_browser_options({**browser.browser_options(mode), 'timezone_id': 'Asia/Shanghai'})
        if self.driver is None:
            from .playwright_runtime import acquire_playwright
            self.driver = acquire_playwright()
        profile = creator_auth.chromium_profile(auth.SPEC, self.store, a.platform_user_id)
        s.context = self.driver.chromium.launch_persistent_context(str(profile), **options)
        self._install_publish_guard(s)
        s.context.set_default_timeout(10000)
        self._validation_stage = 'session_restore'
        creator_auth.restore_session(auth.SPEC, s.context, self.store, a, profile)
        s.original_ref = a.session_ref
        s.page = s.context.pages[0] if s.context.pages else s.context.new_page()
        self._init_observations(s)
        s.page.on('request', lambda request: self._create_request_started(s, request))
        s.page.on('response', lambda response: self._response(s, response))
        s.page.on('requestfinished', lambda request: self._create_request_ended(s, request))
        s.page.on('requestfailed', lambda request: self._create_request_ended(s, request))
        self._validation_stage = 'page_navigation'
        s.page.goto(form_contract.MANAGE_URL, wait_until='domcontentloaded')

    def _surface(self, s):
        # Wujie open shadow DOM is part of the top-level rendered page and is
        # pierced by standard Playwright locators. No private iframe service.
        return s.page

    def _navigate_upload_form(self, s, deadline):
        """One ordinary navigation per identity-checked baseline iteration.

        A fresh deep-link load can fall back to HOME before the content
        microapp hydrates. Use its rendered menu/list entry instead. These
        controls only navigate; the final form button is never a candidate.
        """
        if self.cancel.is_set() or s.final or s.uploaded or time.monotonic() >= deadline:
            return False
        url = urlsplit(s.page.url)
        if url.scheme != 'https' or url.netloc != form_contract.HOST:
            return False
        if url.path == urlsplit(form_contract.UPLOAD_URL).path:
            self._trace(s, 'channels.navigation', {'target': 'create_form', 'ready': True})
            return True
        if s.page.locator('.post-create:visible,.post-create-wrap:visible').count():
            return False

        def unique(selector, label, *, outer=False):
            found = []
            for item in s.page.locator(selector).all():
                if item.is_visible() and item.evaluate(r'''(e,args) =>
                  (!args.outer||e.getRootNode()===document)&&
                  (e.textContent||'').trim().replace(/\s+/g,' ')===args.label&&
                  !e.closest('form,.post-create,.post-create-wrap,.form-btns,.submit-add')''',
                        {'outer': outer, 'label': label}):
                    found.append(item)
            target = {'内容管理': 'content_menu', '视频': 'video_list', '发表视频': 'create_form'}[label]
            self._trace(s, 'channels.navigation',
                        {'target': target, 'candidates': len(found), 'ready': len(found) == 1})
            return found[0] if len(found) == 1 else None

        stage = getattr(s, 'upload_navigation', 'menu')
        if stage == 'create':
            return False
        if url.path == urlsplit(form_contract.MANAGE_URL).path:
            control = unique('.post-list button', '发表视频')
            next_stage = 'create'
        elif url.path == '/platform' and stage in {'menu', 'expanded'}:
            control = unique('a.finder-ui-desktop-menu__link.finder-ui-desktop-menu__only-icon',
                             '视频', outer=True)
            next_stage = 'list'
            if control is None and stage == 'menu':
                control = unique('a.finder-ui-desktop-menu__sub__link', '内容管理', outer=True)
                next_stage = 'expanded'
        else:
            return False
        if control is not None and not self.cancel.is_set():
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining > 0:
                control.click(timeout=min(2500, remaining))
                s.upload_navigation = next_stage
        return False

    def _challenge(self, s):
        return browser.challenge_kind(s.page.evaluate(CHALLENGE) or {})

    def _login_required(self,s):
        self._trace(s,'channels.login_expired',{'before_upload':not s.uploaded,'final_started':bool(s.final)})
        if not s.final:self.service.set_login_state(s.payload['account_id'],'expired')
        message=('视频号登录已失效，请先在验证页面登录所选账号；完成后继续核对原提交结果。' if s.final else '视频号登录已失效，请先在验证页面登录所选账号；完成后点击“已完成验证”，客户端将继续发布。尚未提交平台。')
        result=self._park(s,'login',message)
        s.stage='登录已失效，等待重新登录'
        return result

    def _identity(self, s):
        return auth.verified_identity(s.page.evaluate(auth.IDENTITY_SCRIPT), s.payload['platform_user_id'])

    def _park_identity_mismatch(self, s, stage):
        # Retain only a safe failure stage, never identity snapshots or secrets.
        # A user's explicit completion still rechecks the exact frozen UID.
        s.identity_failure_stage = stage
        s.identity_failure_phase = s.phase
        result = self._park(s, 'login',
            '当前页面账号与所选视频号不一致，尚未提交。请在原浏览器切换回所选账号后，再点击“重新核对原账号”。'
            '如果没有可操作的浏览器窗口，请取消此本地目标；等待任务停止后，到账号管理重新登录所选账号。',reason='identity_mismatch')
        s.stage = '等待切回所选视频号'
        return result

    def _validate_session(self, s, payload, execution):
        self._validation_stage = 'page_open'
        self._validation_failure = None
        try:
            return self._validate_session_once(s, payload, execution)
        except Exception as error:
            self._validation_failure = {'error_stage': self._validation_stage,
                                        'error_category': self._validation_category(error, self._validation_stage)}
            raise

    def _validate_session_once(self, s, payload, execution):
        if execution not in {'publish_now', 'software_schedule', 'platform_schedule'} or self.cancel.is_set():
            return AdapterValidation('', False)
        if not self._version(s):
            return self._park(s, 'login', '该账号登录信息已更新，请确认仍为同一视频号后继续。',reason='session_changed')
        self._trace(s,'channels.login_check')
        self._open(s)
        self._validation_stage = 'account_observation'
        deadline = time.monotonic() + min(10, self.step_timeout)
        while not self.cancel.is_set() and time.monotonic() < deadline:
            if s.page.is_closed():
                raise ValueError('publish page closed')
            try:
                kind = self._challenge(s)
                if not kind:
                    uid, _ = self._identity(s)
            except AccountIdentityMismatch:
                return self._park_identity_mismatch(s, 'validation')
            except ValueError:
                pass
            except Exception as error:
                if s.page.is_closed() or not self._navigation_observation_error(error):
                    raise
            else:
                if kind:
                    return self._login_required(s) if kind=='login' else self._park(s, kind)
                self.service.set_login_state(s.payload['account_id'],'valid')
                self._trace(s,'channels.login_valid',{'identity_matches':True})
                return AdapterValidation(uid, True)
            try:
                s.page.wait_for_timeout(200)
            except Exception as error:
                if s.page.is_closed() or not self._navigation_observation_error(error):
                    raise
        if self.cancel.is_set():
            return AdapterValidation('', False)
        if s.page.is_closed():
            raise ValueError('publish page closed')
        return self._park(s, 'login',
            '原视频号页面尚未就绪，暂未确认所选账号；此目标尚未提交。请核对原任务页面，再点击“重新核对原账号”。',reason='identity_unavailable')

    def plan_readiness(self, payload):
        try:
            a = self.service.get(payload['account_id'])
            if a.platform != self.platform_id or a.login_state != 'valid' or not a.session_ref:
                return 'account_verification_required'
            form_contract.validate_form(payload)
        except (ValueError, TypeError, KeyError):
            return 'platform_fields_invalid'
        return None

    def validate_fields(self, payload):
        try:
            form_contract.validate_form(payload)
        except (ValueError, TypeError, KeyError):
            return False
        return payload.get('platform') == self.platform_id

    def interventions(self):
        with self._lock:
            requests = super().interventions()
            for request in requests:
                session = next((s for s in self.sessions.values() if s.request_id == request['id']), None)
                request['external_browser'] = getattr(session, 'external_browser', False) is True
            return requests

    def respond(self, request_id, action, code=''):
        with self._lock:
            result = super().respond(request_id, action, code)
            s = next(s for s in self.sessions.values() if s.request_id == request_id)
            if s.final and action in {'code', 'pointer', 'done'} and not s.submit_ok:
                s.recovery_action_pending = True
            return result

    def _consume(self, s, *, view_only=False, view_revision=None):
        if getattr(s, 'recovery_action_pending', False):
            # Reset only after a valid, request-bound human verification action.
            # An earlier captcha rejection/completion cannot end this round.
            s.create_epoch += 1; s.create_settled = False; s.submit_rejected = False
            s.query_wait_unresolved = False; s.recovery_action_pending = False
        super()._consume(s,view_only=view_only,view_revision=view_revision)

    def _request_observation(self, s, request, f):
        matched = form_contract.request_matches(request, f, s.owned_media, s.owned_declaration)
        req = request if type(request) is dict else {}
        desc = req.get('objectDesc') if type(req.get('objectDesc')) is dict else {}
        items = desc.get('media') if type(desc.get('media')) is list else []
        actual = items[0] if len(items) == 1 and type(items[0]) is dict else {}
        frozen = s.owned_media or {}
        checks = {'request_matches': matched, 'request_record': type(request) is dict,
            'native_flags_match': all(type(req.get(k)) is int and req[k] == v for k, v in
                {'objectType': 0, 'isFullPost': 1, 'handleFlag': 2, 'mode': 1}.items()),
            'single_media': len(items) == 1, 'media_matches': form_contract.media_matches(actual, frozen),
            'caption_matches': form_contract.caption_matches(desc.get('description'), f['caption']),
            'tags_match': req.get('topics', []) == f['tags'],
            'schedule_matches': type(req.get('effectiveTime', 0)) is int and req.get('effectiveTime', 0) == f['effective_time']}
        # Compare finite code-owned fields only. Never record request bodies,
        # credentials, captions, identifiers, URLs or declaration tokens.
        for key in ('url', 'md5sum', 'fileSize', 'width', 'height', 'videoPlayLen', 'thumbUrl', 'coverUrl',
                    'fullThumbUrl', 'fullCoverUrl', 'shareCoverUrl', 'cardShowStyle', 'mediaType', 'urlCdnTaskId'):
            if key in frozen:
                checks['media_' + key + '_matches'] = actual.get(key) == frozen[key]
        return matched, checks

    def _create_request_started(self, s, request):
        """Track only this attempt's normal, frozen final request in memory."""
        try:
            parsed = urlsplit(request.url)
            if (not s.final or parsed.scheme != 'https' or parsed.netloc != form_contract.HOST
                    or parsed.path not in form_contract.CREATE_PATHS or request.method != 'POST'):
                return
            s.create_ever_started = True
            f = form_contract.validate_form(s.payload, query=True)
            matched, checks = self._request_observation(s, request.post_data_json, f)
            self._trace(s, 'channels.submit_request', checks)
            if matched:
                s.create_pending[id(request)] = s.create_epoch
        except Exception:
            return

    def _create_request_ended(self, s, request):
        # HTTP/network completion is not a success receipt. It only permits a
        # normal result query once no correlated request is still in flight.
        key = id(request)
        if key in s.create_pending:
            epoch = s.create_pending.pop(key)
            if epoch == s.create_epoch:
                s.create_settled = True

    def _response(self, s, response):
        """Observe exact creator-origin responses; never issue platform HTTP."""
        try:
            parsed = urlsplit(response.url)
            if (parsed.scheme != 'https' or parsed.netloc != form_contract.HOST
                    or response.request.method != 'POST' or not 200 <= response.status < 300):
                return
            request_epoch = s.create_pending.get(id(response.request), s.create_epoch)
            data = response.json()
            if type(data) is not dict:
                return
            if parsed.path in form_contract.LIST_PATHS:
                body = data.get('data')
                if (type(data.get('errCode')) is not int or data['errCode'] != 0 or type(body) is not dict
                        or type(body.get('list')) is not list or len(body['list']) > 100):
                    return
                s.list_rows = []
                for row in body['list']:
                    if type(row) is not dict or type(row.get('desc')) is not dict:
                        continue
                    desc = row['desc']; caption = desc.get('description')
                    if type(caption) is not str or len(caption) > 4000:
                        continue
                    s.list_rows.append({**{k: row[k] for k in ('objectId', 'effectiveTime', 'handleStatus') if k in row},
                                        'desc': {'description': caption}})
                s.list_seen = True
            elif parsed.path in form_contract.CREATE_PATHS and s.final:
                f = form_contract.validate_form(s.payload, query=True)
                matched, checks = self._request_observation(s, response.request.post_data_json, f)
                success = form_contract.successful_response(data)
                self._trace(s, 'channels.submit_response', {**checks, 'response_success': success,
                    'http_status': response.status, 'work_id_present': bool(form_contract.receipt_reference(data.get('data')))})
                if not matched:
                    return
                if success:
                    s.submit_ok = True
                    s.receipt = form_contract.receipt_reference(data['data'])
                elif type(data.get('errCode')) is int and type(data.get('data')) is dict:
                    base = data['data'].get('baseResp')
                    # Explicit creator rejection is distinct from malformed or
                    # unrelated responses. Admin verification still parks first.
                    if request_epoch == s.create_epoch and type(base) is dict and type(base.get('errcode')) is int and (
                            data['errCode'] != 0 or base['errcode'] != 0):
                        s.submit_rejected = True
        except Exception:
            return  # Changed/malformed contracts are absence of evidence.

    def _upload(self, s, surface):
        raw = surface.evaluate(form_contract.READ_FORM)
        self._observe_form_gates(s, raw)
        if raw.get('state_ready') is not True or raw.get('form_count')!=1:
            return False
        if raw.get('media_count', 0) > 0 or raw.get('caption', '').strip() or raw.get('draft_id'):
            raise ValueError('existing video-channel draft must be handled first')
        path = Path(s.payload['video_path'])
        if not path.is_file() or sha256_file(path) != s.payload['video_hash']:
            raise ValueError('frozen video changed')
        files = surface.locator(form_contract.FILE_INPUT)
        if files.count() != 1:
            return False
        s.uploaded = True
        files.set_input_files(str(path))
        return True

    def _set_schedule(self, surface, f):
        radio = surface.locator('.post-create .post-time-wrap').get_by_text(f['mode'], exact=True)
        if radio.count() != 1:
            raise ValueError('video-channel schedule mode unavailable')
        radio.click()
        if not f['effective_time']:
            return
        date = surface.locator(form_contract.DATE_INPUT)
        if date.count() != 1:
            raise ValueError('video-channel date input unavailable')
        date.click()
        panel = surface.locator('.weui-desktop-picker__panel_day:visible')
        if panel.count() != 1:
            raise ValueError('video-channel calendar unavailable')
        year, month, day = map(int, f['time_text'][:10].split('-'))
        target_month = year * 12 + month
        # Public calendar has at most one month's scheduling range. Navigation
        # is bounded and only clicks its current visible controls.
        for _ in range(3):
            labels = panel.locator('.weui-desktop-picker__panel__label').all_text_contents()
            if len(labels) != 2:
                raise ValueError('ambiguous calendar month')
            match = re.fullmatch(r'\s*(\d{4})年\s*(\d{1,2})月\s*', ''.join(labels))
            if not match:
                raise ValueError('calendar labels changed')
            current = int(match[1]) * 12 + int(match[2])
            if current == target_month:
                break
            direction = 'right' if target_month > current else 'left'
            step = panel.locator('.weui-desktop-btn__icon__' + direction + ':visible')
            if step.count() != 1:
                raise ValueError('calendar navigation unavailable')
            step.click()
        else:
            raise ValueError('calendar navigation limit')
        days = panel.locator('tbody a:not(.weui-desktop-picker__faded):not(.weui-desktop-picker__disabled)').filter(
            has_text=re.compile(r'^\s*' + str(day) + r'\s*$'))
        if days.count() != 1:
            raise ValueError('scheduled day unavailable')
        days.click()
        clock = surface.locator('.weui-desktop-picker__time:visible input')
        if clock.count() != 1:
            raise ValueError('schedule time input unavailable')
        clock.fill(f['time_text'][11:])
        clock.press('Tab')
        surface.locator(form_contract.EDITOR).click()
        if date.input_value() != f['time_text']:
            raise ValueError('schedule did not commit')

    @staticmethod
    def _uploaded_gate_checks(raw, expected_uid):
        """Shape/status only: never expose media URLs, draft IDs or account UID."""
        value=raw.get('media');record=type(value) is dict
        invalid=[]
        if record:
            for key in ('url','md5sum','thumbUrl'):
                item=value.get(key)
                if type(item) is not str or not item or len(item)>12000:invalid.append(key)
                elif key in {'url','thumbUrl'} and not item.startswith('https://'):invalid.append(key)
            for key in ('fileSize','width','height','videoPlayLen'):
                item=value.get(key)
                if type(item) not in (int,float) or not math.isfinite(item) or item<=0:invalid.append(key)
            if type(value.get('mediaType')) is not int or value['mediaType']!=form_contract.VIDEO_MEDIA_TYPE:
                invalid.append('mediaType')
        uid=raw.get('finder_uid')
        return {'media_record':record,'media_owned':form_contract.owned_media(value) is not None,
                'media_invalid_fields':invalid,'draft_present':bool(raw.get('draft_id')),
                'finder_uid_present':type(uid) is str and bool(uid),
                'finder_uid_matches':type(uid) is str and uid==expected_uid}

    def _prepare_form(self, s, surface, f):
        raw = surface.evaluate(form_contract.READ_FORM)
        self._observe_form_gates(s, raw, f)
        if (raw.get('state_ready') is not True or not raw.get('upload_complete')
                or raw.get('editor_count') != 1 or raw.get('form_count') != 1):
            return False
        if not s.form_filled:
            media = form_contract.owned_media(raw.get('media'))
            self._prepare_gate_checks=self._uploaded_gate_checks(raw,s.payload['platform_user_id'])
            if media is None and raw.get('clip_bindings',{}).get('async_processing') is True:
                # The helper marks async upload complete before processed media
                # dimensions and cover arrive. Wait for its complete record.
                return False
            if media is None or raw.get('draft_id') or raw.get('finder_uid') != s.payload['platform_user_id']:
                raise ValueError('uploaded media or account unavailable')
            if media.get('fileSize') is None and raw.get('clip_bindings',{}).get('processed_from_upload') is not True:
                return False
            s.owned_media = media
            editor = surface.locator(form_contract.EDITOR)
            editor.click(); editor.press('ControlOrMeta+A')
            s.page.keyboard.insert_text(f['caption'])
            editor.press('End'); editor.press('Space'); editor.press('Backspace'); editor.press('Tab')
            # DOMSubtreeModified/renderToEditor converts the literal hashtags
            # to real .topic spans and updates topics/finderTopicInfo normally.
            short = surface.locator(form_contract.SHORT_TITLE)
            if f['short_title']:
                if short.count() != 1:
                    raise ValueError('short title entrance unavailable for this account')
                short.fill(f['short_title']); short.press('Tab')
            elif short.count() == 1:
                short.fill(''); short.press('Tab')
            self._preparation_stage='schedule_preparation'
            self._set_schedule(surface, f)
            self._preparation_stage='form_preparation'
            s.form_filled = True
            return False
        processed = form_contract.refresh_processed_media(raw, s.owned_media)
        if processed is not None:
            s.owned_media = processed
        if not self._clear_location(s, surface, raw):
            return False
        if not getattr(s, 'content_selection_attempted', False):
            if (f['content_declaration_choice'] != 'none' and raw.get('declaration_component_count') == 1
                    and raw.get('declaration_options_loading') is True):
                return False  # Wait for the ordinary page's own menu read.
            self._set_content_declaration(s, surface, raw, f)
            return False
        if f['content_declaration_choice'] != 'none' and s.owned_declaration is None:
            if not form_contract.content_declaration_matches(raw, f):
                return False
            # This opaque key never leaves the live browser attempt or enters
            # a receipt. It is evidence from the ordinary control's own event.
            s.owned_declaration = form_contract.safe_tag_info(raw['declaration_tag_info'])
        if not getattr(s, 'original_selection_attempted', False):
            self._set_original(s, surface, raw, f)
            return False
        if not form_contract.content_declaration_matches(raw, f, s.owned_declaration):
            raise ValueError('channels declaration selected state mismatch')
        if not form_contract.declarations_match(raw, f, s.owned_declaration):
            raise ValueError('channels original selected state mismatch')
        return form_contract.verify_form(raw, f, s.owned_media, s.payload['platform_user_id'], s.owned_declaration)

    def _clear_location(self, s, surface, raw):
        # The official page initializes a nearby city even when the user has
        # requested no location. Clear it using the ordinary menu, then read
        # back the empty record; no location gate is relaxed.
        if raw.get('extras_checks', {}).get('empty_location') is True:
            return True
        if getattr(s, 'location_clear_attempted', False):
            return False
        select = surface.locator(form_contract.LOCATION_SELECT)
        if select.count() != 1 or not select.is_visible():
            return False
        if not getattr(s, 'location_menu_opened', False):
            select.click()
            s.location_menu_opened = True
        option = surface.locator(form_contract.LOCATION_OPTION).get_by_text('不显示位置', exact=True)
        if option.count() != 1 or not option.is_visible():
            return False
        s.location_clear_attempted = True
        option.click()
        return False

    def _set_content_declaration(self, s, surface, raw, f):
        if (raw.get('declaration_selected_count') != 0 or raw.get('declaration_name')
                or raw.get('declaration_state_name') or raw.get('declaration_tag_info') is not None
                or raw.get('declaration_component_tag_info') is not None):
            raise ValueError('unexpected existing content declaration')
        if f['content_declaration_choice'] == 'none':
            s.content_selection_attempted = True
            return
        rows = raw.get('declaration_options')
        matches = [row for row in rows if type(row) is dict and row.get('name') == f['content_declaration_name']] if type(rows) is list else []
        if (raw.get('declaration_component_count') != 1 or raw.get('declaration_options_loading') is not False
                or len(matches) != 1 or type(matches[0].get('tag_type')) is not int
                or not 0 < matches[0]['tag_type'] <= 9007199254740991 or matches[0]['tag_type'] in {5, 7}
                or type(raw.get('declaration_tag_key')) is not str or not 1 <= len(raw['declaration_tag_key']) <= 4096):
            raise ValueError('channels AI declaration option unavailable or changed')
        select = surface.locator(form_contract.DECLARATION_SELECT)
        if select.count() != 1 or not select.is_visible():
            raise ValueError('channels declaration menu unavailable')
        select.click()
        option = surface.locator(form_contract.DECLARATION_OPTION).get_by_text(f['content_declaration_name'], exact=True)
        if option.count() != 1 or not option.is_visible():
            raise ValueError('channels declaration option ambiguous')
        s.content_selection_attempted = True
        option.click()

    def _set_original(self, s, surface, raw, f):
        # Only the ordinary checkbox can request the declaration. Its protocol
        # dialog belongs to the human; never set Vue state or check its terms.
        if raw.get('state_original') is not False:
            raise ValueError('unexpected existing original declaration')
        if not form_contract.content_declaration_matches(raw, f, s.owned_declaration):
            raise ValueError('content declaration changed before original selection')
        if not f['channels_original']:
            s.original_selection_attempted = True
            return
        if s.owned_media['videoPlayLen'] < 5:
            raise ValueError('channels original declaration requires at least five seconds')
        control = surface.locator(form_contract.ORIGINAL_INPUT)
        if (raw.get('original_control_count') != 1 or raw.get('original_checked') is not False
                or control.count() != 1 or not control.is_enabled()):
            raise ValueError('original declaration entrance unavailable')
        # Mark this before clicking: a protocol interruption keeps one request
        # and one uploaded asset; resume cannot click again after cancellation.
        s.original_selection_attempted = True
        control.click()

    def _click_final(self, s, surface):
        if self.inspection_only:
            raise ValueError('inspection cannot publish')
        raw = surface.evaluate(form_contract.READ_FORM)
        if not form_contract.verify_form(raw, form_contract.validate_form(s.payload), s.owned_media, s.payload['platform_user_id'], s.owned_declaration):
            raise ValueError('final video-channel form changed')
        final = surface.locator(form_contract.FINAL_CONTROL).filter(has_text=re.compile(r'^\s*发表\s*$'))
        if final.count() != 1 or not final.is_enabled():
            raise ValueError('final video-channel control unavailable')
        final.click()

    def _observe_original_reminder(self, s):
        try:
            checks=s.page.evaluate(ORIGINAL_REMINDER_DIAGNOSTIC)
            self._trace(s,'channels.original_reminder_check',checks if type(checks) is dict else {'readable':False})
        except Exception as error:
            self._trace(s,'channels.original_reminder_check',{'readable':False,'exception_type':type(error).__name__})

    def _continue_original_reminder(self, s):
        """Continue the native no-original reminder once on the owned live page.

        This is not another final attempt: no post_create request may have
        started. The existing final journal owns the attempt, and frozen
        form and response checks remain in force. Never select original rights.
        """
        if (self.inspection_only or not s.final or not s.uploaded or s.phase != 'result'
                or s.submit_ok or s.submit_rejected or s.create_pending or s.create_settled
                or getattr(s, 'create_ever_started', False) or getattr(s, 'reminder_continued', False)):
            return False
        f = form_contract.validate_form(s.payload)
        version_matches=self._version(s)
        self._trace(s,'channels.original_reminder_guard',{'original_requested':f['channels_original'],
            'session_matches':version_matches,'create_started':bool(getattr(s,'create_ever_started',False))})
        if f['channels_original'] or not version_matches or s.page.evaluate(ORIGINAL_REMINDER) is not True:
            return False
        self._identity(s)
        raw = s.page.evaluate(form_contract.READ_FORM)
        if type(raw) is not dict or raw.get('original_intercept') is not True:
            return False
        # Only this exact reminder flag is expected to differ from the frozen
        # form. The separate original agreement remains disallowed.
        checked = {**raw, 'original_intercept': False}
        self._trace(s,'channels.original_reminder_form',form_contract.form_verification_checks(
            checked,f,s.owned_media,s.payload['platform_user_id'],s.owned_declaration))
        if not form_contract.verify_form(checked, f, s.owned_media,
                                        s.payload['platform_user_id'], s.owned_declaration):
            return False
        preference = s.page.locator(ORIGINAL_REMINDER_PREFERENCE)
        if (preference.count() == 1 and preference.is_visible() and preference.is_enabled()
                and preference.inner_text().strip() == '不再提醒'):
            s.reminder_continued = True
            s.reminder_preference_pending = True
            self._trace(s, 'channels.original_reminder_dismiss', {'original_requested': False})
            preference.click()
            return True  # Native report must finish and close before submit.
        button = s.page.locator(ORIGINAL_REMINDER_BUTTON)
        if button.count() != 1 or not button.is_visible() or not button.is_enabled():
            return False
        s.reminder_continued = True
        self._trace(s, 'channels.original_reminder_continue', {'original_requested': False,
                    'schedule_matches': True, 'final_started': True})
        button.click()
        return True

    def _finish_reminder_preference(self, s):
        if (not getattr(s, 'reminder_preference_pending', False) or self.inspection_only
                or not s.final or not s.uploaded or s.phase != 'result' or s.submit_ok
                or s.submit_rejected or s.create_pending or s.create_settled
                or getattr(s, 'create_ever_started', False)):
            return False
        f = form_contract.validate_form(s.payload)
        if f['channels_original'] or not self._version(s) or self._challenge(s) is not None:
            return False
        self._identity(s)
        raw = s.page.evaluate(form_contract.READ_FORM)
        if not form_contract.verify_form(raw, f, s.owned_media,
                                        s.payload['platform_user_id'], s.owned_declaration):
            return False
        # This is the same journaled attempt: the first main click only opened
        # the native reminder and no create request has ever started. Do not
        # change effectiveTime, invoke component methods, or authorize again.
        s.reminder_preference_pending = False
        self._trace(s, 'channels.original_reminder_continue', {'original_requested': False,
                    'schedule_matches': True, 'final_started': True})
        self._click_final(s, s.page)
        return True

    def _persisted_ack(self, s):
        """Original immutable attempt ACK survives close/restart/login failures."""
        if not s.final:
            return None
        try:
            with session_scope(self.engine) as db:
                row = db.get(PublishDeliveryRow, s.binding['delivery_id'])
                if (row is None or row.attempt != s.binding['attempt'] or row.task_id != s.binding['task_id']
                        or row.request_digest != s.binding['digest'] or row.request_digest != self._key(s.payload)
                        or row.platform != self.platform_id or row.account_id != s.payload['account_id']
                        or row.platform_user_id != s.payload['platform_user_id']
                        or digest(json.loads(row.snapshot_json)) != row.request_digest):
                    return None
                attempt = db.scalar(select(DeliveryAttemptRow).where(
                    DeliveryAttemptRow.delivery_id == row.id, DeliveryAttemptRow.number == row.attempt))
                if (attempt is None or attempt.request_digest != row.request_digest or attempt.snapshot_json != row.snapshot_json
                        or not attempt.final_json or not attempt.result_json):
                    return None
                final = json.loads(attempt.final_json); result = json.loads(attempt.result_json)
                if (type(final) is not dict or final.get('request_digest') != row.request_digest
                        or type(result) is not dict or result.get('request_digest') != row.request_digest
                        or result.get('state') not in {'platform_accepted', 'platform_scheduled'}):
                    return None
                if (result['state'] == 'platform_scheduled' and (row.execution_mode != 'platform_schedule'
                        or result.get('scheduled_at') != row.scheduled_at or row.scheduled_at != s.payload['scheduled_at'])):
                    return None
                reference = result.get('receipt_reference') or {}
                if type(reference) is not dict or set(reference) - {'work_id'} or any(
                        form_contract.safe_id(v) != v for v in reference.values()):
                    return None
                if result['state'] == 'platform_accepted' and not reference:
                    return None
                return AdapterOutcome(result['state'], scheduled_at=result.get('scheduled_at'), receipt_reference=reference)
        except (ValueError, TypeError, KeyError):
            return None

    def _advance(self, s, *, before_final=None, query=False):
        self._preparation_stage='page_preparation'
        self._validation_failure=None
        self._preparation_diagnostic=None
        self._prepare_gate_checks=None
        try:
            return self._advance_steps(s, before_final=before_final, query=query)
        except ValueError as error:
            # Only these proved local branches become finite public reasons.
            # Context destruction and other browser errors remain observable.
            message = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else None
            codes = {
                'unexpected existing content declaration': 'channels_declaration_options_unverified',
                'channels AI declaration option unavailable or changed': 'channels_declaration_options_unverified',
                'channels declaration menu unavailable': 'channels_declaration_options_unverified',
                'channels declaration option ambiguous': 'channels_declaration_options_unverified',
                'channels declaration selected state mismatch': 'channels_declaration_options_unverified',
                'content declaration changed before original selection': 'channels_declaration_options_unverified',
                'unexpected existing original declaration': 'channels_original_unavailable',
                'original declaration entrance unavailable': 'channels_original_unavailable',
                'channels original selected state mismatch': 'channels_original_unavailable',
                'channels original declaration requires at least five seconds': 'channels_original_video_too_short',
            }
            if type(error) is ValueError and not s.final and message in codes:
                return AdapterOutcome('pre_submit_failed', error_code=codes[message])
            if not s.final:
                self._record_preparation_failure(error)
                if not self._validation_failure:
                    self._validation_failure={'error_stage':self._preparation_stage,
                        'error_category':self._preparation_category(error)}
            raise
        except Exception as error:
            if not s.final:
                self._record_preparation_failure(error)
                if not self._validation_failure:
                    self._validation_failure={'error_stage':self._preparation_stage,
                        'error_category':self._preparation_category(error)}
            raise

    def _accepted_outcome(self, s, f):
        # A bound successful POST wins over a page transition or elapsed UI
        # deadline; no extra platform request is needed to acknowledge it.
        self._refresh_after_receipt(s)
        if not f['effective_time'] and not s.receipt:
            return AdapterOutcome('result_unknown', context={'handle': s.handle}, error_code='result_unknown')
        return AdapterOutcome('platform_scheduled' if f['effective_time'] else 'platform_accepted',
            scheduled_at=s.payload['scheduled_at'] if f['effective_time'] else None, receipt_reference=s.receipt)

    def _advance_steps(self, s, *, before_final=None, query=False):
        self._thread()
        f = form_contract.validate_form(s.payload, query=query)
        durable = self._persisted_ack(s) if query else None
        # Without a work ID this is only the original durable ACK; it never
        # becomes a fabricated list finding or an automatic resubmission.
        if durable and not durable.receipt_reference:
            return durable
        self._open(s, query=query)
        if durable and not s.receipt:
            s.receipt = dict(durable.receipt_reference)
        if getattr(s, 'verification_observation_pending', False) and not (s.final and s.submit_ok):
            # A page transition cannot invalidate a bound, durable ACK.
            return durable or self._pause_verification_observation(s)
        if query and s.query_wait_unresolved and not s.submit_ok and (s.create_pending or not s.submit_rejected):
            return durable or AdapterOutcome('result_unknown', context={'handle': s.handle},
                                            error_code='result_unknown')
        deadline = time.monotonic() + self.step_timeout
        while not self.cancel.is_set() and time.monotonic() < deadline:
            self._trace(s,'channels.'+s.phase,{'uploaded':bool(s.uploaded),'form_filled':bool(s.form_filled),'final_started':bool(s.final)})
            if s.final and s.submit_ok:
                return self._accepted_outcome(s, f)
            if s.page.is_closed():
                break
            if self._finish_reminder_preference(s):
                s.page.wait_for_timeout(200)
                continue
            kind, ready = self._verification_read(s, self._challenge)
            if not ready:
                return self._pause_verification_observation(s)
            if kind:
                if kind=='complex':
                    self._observe_original_reminder(s)
                if kind == 'complex' and s.final and s.uploaded and s.page.evaluate(ORIGINAL_REMINDER) is True:
                    if self._continue_original_reminder(s):
                        s.page.wait_for_timeout(200)
                        continue
                    # No duplicated verification modal for a monetization
                    # reminder. Keep its original browser and task evidence.
                    if getattr(s, 'reminder_continued', False):
                        s.page.wait_for_timeout(200)
                        continue
                    return AdapterOutcome('result_unknown', context={'handle': s.handle}, error_code='result_unknown')
                if kind=='login':return self._login_required(s)
                return self._park(s, kind, '请在原画面完成视频号验证或原创权益确认。鞋匠不会替你同意原创协议，继续时会核对原选择。')
            if not self._version(s, query=query):
                return self._park(s, 'login', '所选视频号登录资料已更新，请确认仍使用原账号继续。',reason='session_changed')
            try:
                _, ready = self._verification_read(s, self._identity, identity=True)
            except AccountIdentityMismatch:
                if s.final:
                    return AdapterOutcome('result_unknown', error_code='account_mismatch')
                return self._park_identity_mismatch(s, s.phase)
            if not ready:
                return self._pause_verification_observation(s)
            if self.service.get(s.payload['account_id']).login_state=='expired':
                self.service.set_login_state(s.payload['account_id'],'valid')
            if s.phase == 'baseline':
                s.stage = '正在检查视频号已有作品'
                if not s.list_seen and time.monotonic() < s.baseline_deadline:
                    s.page.wait_for_timeout(200); continue
                self._preparation_stage='page_navigation'
                if not self._navigate_upload_form(s, deadline):
                    s.page.wait_for_timeout(200); continue
                s.phase = 'prepare'
            elif s.phase == 'prepare':
                self._preparation_stage='upload_preparation'
                surface = self._surface(s); s.stage = '正在上传视频号视频'
                if not s.uploaded and not self._upload(s, surface):
                    s.page.wait_for_timeout(200); continue
                self._preparation_stage='form_preparation'
                s.stage = '正在填写并核对视频号发布资料'
                if self._prepare_form(s, surface, f):
                    if form_contract.validate_form(s.payload) != f:
                        return AdapterOutcome('pre_submit_failed', error_code='schedule_mismatch')
                    if self.inspection_only:
                        return self._inspection_complete(s)
                    if before_final is None:
                        raise ValueError('final authority unavailable')
                    try:
                        before_final()
                    except ValueError:
                        kind, ready = self._verification_read(s, self._challenge)
                        if not ready:
                            return self._pause_verification_observation(s)
                        if s.request_id or kind:
                            return self._park(s, kind or 'login')
                        raise
                    s.final = True; s.phase = 'result'; s.stage = '正在等待视频号提交回执'
                    self._click_final(s, surface)
            else:
                s.stage = '正在核对视频号原提交结果'
                if s.submit_rejected and not s.create_pending:
                    return AdapterOutcome('platform_rejected', error_code='platform_rejected')
                verified = form_contract.verified_row(s.list_rows, s.receipt, f)
                if verified:
                    self._refresh_after_receipt(s)
                    return AdapterOutcome(verified['state'], receipt_reference=verified['reference'],
                        scheduled_at=s.payload['scheduled_at'] if verified['state'] == 'platform_scheduled' else None)
            s.page.wait_for_timeout(200)
        if s.final and s.submit_ok:
            return self._accepted_outcome(s, f)
        if not s.final and not self.cancel.is_set() and not s.page.is_closed():
            self._record_preparation_timeout(s)
        return durable or AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',
            context={'handle': s.handle} if s.final else None,
            error_code='result_unknown' if s.final else 'timeout')

    def _prepare_result_query(self, s):
        s.query_wait_unresolved = False
        if self.cancel.is_set() or s.page.is_closed():
            s.query_wait_unresolved = True
            return
        if getattr(s, 'verification_observation_pending', False):
            s.query_wait_unresolved = True
            return
        if not s.uploaded:
            # A reopened query has no live upload or request to interrupt.
            kind, ready = self._verification_read(s, self._challenge)
            if not ready:
                s.query_wait_unresolved = True
                self._pause_verification_observation(s)
                return
            if not kind:
                s.page.goto(form_contract.MANAGE_URL, wait_until='domcontentloaded')
            return
        s.stage = '正在等待原发布页返回提交结果'
        deadline = time.monotonic() + min(10, self.step_timeout)
        while not self.cancel.is_set() and not s.page.is_closed() and time.monotonic() < deadline:
            if s.submit_ok or (s.submit_rejected and not s.create_pending):
                return  # Preserve the original receipt or verification page.
            if self._finish_reminder_preference(s):
                s.page.wait_for_timeout(100)
                continue
            kind, ready = self._verification_read(s, self._challenge)
            if not ready:
                s.query_wait_unresolved = True
                self._pause_verification_observation(s)
                return
            if kind:
                if kind=='complex':self._observe_original_reminder(s)
                if kind == 'complex' and self._continue_original_reminder(s):
                    s.page.wait_for_timeout(100)
                    continue
                return
            if s.create_settled and not s.create_pending:
                s.page.goto(form_contract.MANAGE_URL, wait_until='domcontentloaded')
                return
            # A verification action can enqueue post_create on the next event
            # turn. Yield on the original page; do not cancel it by navigating.
            s.page.wait_for_timeout(100)
        s.query_wait_unresolved = True
        s.stage = '原发布页尚未返回明确结果，保留原页等待核对'

    def _refresh_after_receipt(self, s):
        try:
            a = self._account(s); uid, name = self._identity(s)
            self.refreshed[a.id] = {'platform_user_id': uid, 'nickname': name,
                'session_blob': self._capture_session_blob(s.context, uid), 'original_ref': s.original_ref}
        except Exception:
            pass  # Credential refresh never invalidates receipt evidence.

    def _capture_session_blob(self, context, uid):
        return creator_auth.encode_session(auth.SPEC, creator_auth.capture_session_state(auth.SPEC, context), uid)

    def _persist_sessions(self):
        for account_id, value in list(self.refreshed.items()):
            creator_auth.decode_session(auth.SPEC, value['session_blob'], value['platform_user_id'])
            with session_scope(self.engine) as db:
                db.execute(text('BEGIN IMMEDIATE'))
                row = db.get(AccountRow, account_id)
                if (row is not None and row.platform == self.platform_id and row.login_state != 'removed'
                        and row.platform_user_id == value['platform_user_id'] and row.session_ref == value['original_ref']):
                    row.session_ref = self.store.put(row.id, value['session_blob'], versioned=True)
                    row.login_state = 'valid'
            self.refreshed.pop(account_id, None)
