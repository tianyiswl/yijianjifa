"""Image-only Douyin transport on the existing account/session lifecycle.

Only application-owned image bytes, an exact form and the same request/receipt
can complete a delivery. The local contract is tested; real platform acceptance
is tracked separately. No video uploader or fake duration is used.
"""
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zlib
from urllib.parse import urlsplit

from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from . import chromium_publish as legacy
from .chromium_multi_publish import ChromiumMultiPublishAdapter
from .douyin_topics import compose_description


CREATE_PATH = '/web/api/media/aweme/create'

READ_IMAGE_DIAGNOSTIC = r'''() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).display!=='none';
 const safeClass=e=>String(e.className||'').replace(/[^a-zA-Z0-9_ -]/g,'').slice(0,160);
 const match=e=>visible(e)&&/已添加\d+张图片/.test((e.innerText||'').replace(/\s/g,''));
 const leaves=Array.from(document.querySelectorAll('body *')).filter(e=>match(e)&&!Array.from(e.children).some(match));
 return {markers:leaves.slice(0,4).map(e=>{const chain=[];for(let node=e,i=0;node&&i<8;node=node.parentElement,i++){
  const backgrounds=Array.from(node.querySelectorAll('*')).filter(x=>visible(x)&&getComputedStyle(x).backgroundImage!=='none');
  const media=Array.from(node.querySelectorAll('img,canvas')).slice(0,16).map(x=>{const r=x.getBoundingClientRect();let path='';try{const u=new URL(x.getAttribute('src')||'',location.href);if(u.protocol==='https:')path=u.pathname;}catch{}return {tag:x.tagName,classes:safeClass(x),parent:safeClass(x.parentElement),path,rect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)]};});
  chain.push({tag:node.tagName,classes:safeClass(node),imgs:node.querySelectorAll('img').length,media,children:Array.from(node.children).slice(0,18).map(x=>({tag:x.tagName,classes:safeClass(x),imgs:x.querySelectorAll('img').length})),backgrounds:backgrounds.slice(0,12).map(x=>({tag:x.tagName,classes:safeClass(x),parent:safeClass(x.parentElement),rect:Math.round(x.getBoundingClientRect().width)+'x'+Math.round(x.getBoundingClientRect().height)}))});
 }return chain;}),file_inputs:Array.from(document.querySelectorAll('input[type="file"]')).slice(0,8).map(e=>({accept:e.accept,multiple:e.multiple,parent:safeClass(e.parentElement)}))};
}'''


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def image_ids(value):
    value = _json(value)
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if isinstance(item, dict):
            item = next((item.get(key) for key in ('uri', 'image_uri', 'image_id', 'ImageUri', 'StoreUri') if item.get(key)), None)
        if not isinstance(item, str) or not 1 <= len(item) <= 2048:
            return None
        result.append(item)
    return result


def _upload_uri(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        for key in ('ImageUri', 'image_uri', 'StoreUri', 'store_uri', 'uri', 'image_id'):
            if isinstance(value.get(key), str) and 1 <= len(value[key]) <= 2048:
                return value[key]
        values = [_upload_uri(item, depth + 1) for item in value.values()]
    elif isinstance(value, list):
        values = [_upload_uri(item, depth + 1) for item in value[:40]]
    else:
        return None
    found = {item for item in values if item}
    return next(iter(found)) if len(found) == 1 else None


class ImageUploadLedger:
    """Bind successful upload IDs to the exact frozen image bytes, in order."""
    def __init__(self, images):
        self.images = tuple(images)
        self.ids = [None] * len(self.images)
        self.requests = []
        self.samples = []
        self.hashes = {}
        for index, image in enumerate(self.images):
            if image['position'] != index:
                raise ValueError('image order invalid')
            self.hashes.setdefault(image['content_hash'], []).append(index)

    @property
    def ordered_ids(self):
        return list(self.ids) if self.ids and all(self.ids) else []

    def observe_upload(self, request, response):
        if any(previous is request for previous in self.requests):
            return
        if not 200 <= response.status < 300:
            return
        data = request.post_data_buffer
        if not isinstance(data, bytes) or not data or len(data) > 26 * 1024 * 1024:
            return
        content_type = request.headers.get('content-type', '')
        blobs = []
        if content_type.startswith('multipart/form-data'):
            message = BytesParser(policy=policy.default).parsebytes(
                b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii') + b'\r\n\r\n' + data)
            if not message.is_multipart():
                return
            blobs = [part.get_payload(decode=True) for part in message.iter_parts() if part.get_filename()]
        elif content_type.startswith('image/') or content_type == 'application/octet-stream':
            blobs = [data]
        hashes = {hashlib.sha256(blob).hexdigest() for blob in blobs if isinstance(blob, bytes)} & self.hashes.keys()
        if len(hashes) != 1:
            return
        try:
            result = response.json()
        except Exception:
            return
        if os.environ.get('P011_PUBLISH_DIAGNOSTICS') == '1' and len(self.samples) < 8:
            def shape(value, depth=0):
                if depth>4:return type(value).__name__
                if isinstance(value,dict):return {str(k)[:64]:shape(v,depth+1) for k,v in list(value.items())[:15]}
                if isinstance(value,list):return [shape(v,depth+1) for v in value[:3]]
                return type(value).__name__
            self.samples.append({'request_host':urlsplit(request.url).hostname,'request_path':urlsplit(request.url).path[:500], 'response_shape':shape(result),'code':result.get('code') if isinstance(result,dict) and type(result.get('code')) is int else None,'crc32':(result.get('data') or {}).get('crc32') if isinstance(result,dict) and isinstance(result.get('data'),dict) else None})
        if not isinstance(result, dict) or any(type(result[key]) not in (int, float) or result[key] != 0
                for key in ('status_code', 'error_code') if key in result):
            return
        uri = _upload_uri(result)
        if uri and 'code' in result and (type(result['code']) is not int or result['code'] not in (0,2000)):
            return
        # ImageX binary upload acknowledges the bytes with their CRC. Its
        # authenticated storage key is in the successful upload request path,
        # rather than the response JSON. Never infer it from a preview URL.
        if not uri:
            parsed=urlsplit(request.url)
            match=re.fullmatch(r'/upload/v1/(tos-cn-i-[a-z0-9-]+)/([0-9a-f]{32})',parsed.path)
            crc=(result.get('data') or {}).get('crc32') if isinstance(result.get('data'),dict) else None
            checks={value for blob in blobs if isinstance(blob,bytes) for value in
                    (str(zlib.crc32(blob)&0xffffffff),format(zlib.crc32(blob)&0xffffffff,'08x'))}
            trusted_host=bool(parsed.hostname and (parsed.hostname.endswith('.douyin.com') or
                    re.fullmatch(r'tos-[a-z0-9-]+\.(?:snssdk\.com|bytedancevod\.com)',parsed.hostname)))
            if (parsed.scheme=='https' and trusted_host
                    and match and len(blobs)==1 and type(result.get('code')) is int and result['code'] in (0,2000)
                    and isinstance(crc,str) and crc in checks):
                uri=match.group(1)+'/'+match.group(2)
        if not uri:
            return
        for index in self.hashes[next(iter(hashes))]:
            if self.ids[index] is None:
                self.ids[index] = uri
                self.requests.append(request)
                break


def matches_image_request(payload, request, upload_ids):
    if not isinstance(request, dict) or not upload_ids:
        return False
    fields = payload['fields']
    description, _ = compose_description(fields.get('body', ''), fields.get('tags', ''))
    clean = lambda value: re.sub(r'\s+', ' ', str(value).replace('\u200b', '')).strip()
    title = next((request.get(key) for key in ('title', 'item_title', 'caption') if key in request), None)
    body = next((request.get(key) for key in ('desc', 'description', 'text') if key in request), None)
    if title != fields['title'] or not isinstance(body, str) or clean(body) != clean(description):
        return False
    images = next((request.get(key) for key in ('images', 'image_list') if key in request), None)
    if image_ids(images) != list(upload_ids):
        return False
    uid = request.get('uid', request.get('user_id'))
    if uid is not None and str(uid) != payload['platform_user_id']:
        return False
    times = [request[key] for key in ('scheduled_publish_time', 'schedule_time', 'publish_time') if key in request]
    parsed = []
    for value in times:
        if type(value) not in (int, float, str) or isinstance(value, bool):
            return False
        try:
            number = float(value)
            if number != int(number) or number < 0:
                return False
            parsed.append(int(number / 1000) if number > 10**12 else int(number))
        except (ValueError, OverflowError):
            return False
    if payload['execution_mode'] == 'platform_schedule':
        expected = int(datetime.fromisoformat(payload['scheduled_at']).timestamp())
        if not parsed or any(value != expected for value in parsed) or ('is_schedule' in request and request['is_schedule'] not in (True,1)):
            return False
    elif payload['execution_mode'] == 'publish_now':
        if any(parsed) or request.get('is_schedule') not in (None, False, 0):
            return False
    else:
        return False
    return True


def verified_image_receipt(payload, request, response, upload_ids):
    if not matches_image_request(payload, request, upload_ids) or not isinstance(response, dict):
        return None
    objects = [response] + [response[key] for key in ('data', 'extra') if isinstance(response.get(key), dict)]
    codes = [item[key] for item in objects for key in ('status_code', 'error_code') if key in item]
    if not codes or any(type(code) not in (int, float) or code != 0 for code in codes):
        return None
    ids = []
    for item in objects:
        for value in (item.get('aweme_id'), item.get('item_id'), (item.get('aweme') or {}).get('aweme_id') if isinstance(item.get('aweme'), dict) else None):
            if value is not None and re.fullmatch(r'[0-9]{1,32}', str(value)):
                ids.append(str(value))
    return ids[0] if ids and len(set(ids)) == 1 else None


class DouyinImagePostAdapter(ChromiumMultiPublishAdapter):
    content_type = 'image_post'
    verified_capabilities = frozenset({'ordinary', 'publish_now', 'platform_schedule', 'result_query'})
    # One real three-image scheduled post, plus readback and automatic close,
    # was verified on 2026-10-14. This does not claim immediate public visibility.
    platform_acceptance_tested = True
    public_field_schema = {'title': {'type': 'string', 'max_length': 20},
        'body': {'type': 'string', 'max_length': 1000}, 'tags': {'type': 'string'},
        'declaration': {'selection': 'trusted_policy_reference'},
        'images': {'min_count': 1, 'max_count': 30, 'order': 'frozen'}}

    def __init__(self, engine, paths, **kwargs):
        kwargs.setdefault('step_timeout', 180)
        super().__init__(engine, paths, **kwargs)

    def _form(self, payload, *, query=False):
        from .douyin_image_form import validate_image_form_request
        fields = payload['fields']
        clock = None
        if payload['execution_mode'] == 'platform_schedule':
            clock = datetime.fromisoformat(payload['scheduled_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        args = validate_image_form_request(expected_uid=payload['platform_user_id'],
            title=fields['title'], body=fields.get('body', ''), tags=fields.get('tags', ''),
            declaration=(fields.get('declaration') or {}).get('label', ''),
            image_names=[Path(image['path']).name for image in payload['images']],
            scheduled_time_text=None if query else clock)
        if query:
            args['scheduled_time_text'] = clock or ''
            reference = payload.get('receipt_reference') or {}
            if reference.get('kind') == 'image_post' and re.fullmatch(r'[0-9]{1,32}', str(reference.get('item_id', ''))):
                args['submission_item_id'] = reference['item_id']
        return args

    def _diagnose_image_preview(self, s):
        if os.environ.get('P011_PUBLISH_DIAGNOSTICS') != '1':
            return
        try:
            value=s.page.evaluate(READ_IMAGE_DIAGNOSTIC)
            if not isinstance(value,dict) or not value.get('markers'):
                return
            value['authenticated_upload_count']=len(s.image_uploads.ordered_ids)
            value['byte_bound_upload_responses']=s.image_uploads.samples
            signature=json.dumps(value,sort_keys=True)
            if getattr(s,'image_diagnostic_signature',None)==signature:
                return
            s.image_diagnostic_signature=signature
            folder=self.paths.logs_dir;folder.mkdir(parents=True,exist_ok=True)
            target=folder/('image-preview-'+s.binding['delivery_id']+'.json')
            target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception:
            pass

    def _upload_proof_scope(self, s):
        return {'delivery_id':s.binding['delivery_id'], 'attempt':s.binding['attempt'], 'digest':s.binding['digest'],
            'platform_user_id':s.payload['platform_user_id'],
            'images':[{'position':x['position'],'content_hash':x['content_hash']} for x in s.payload['images']]}

    def _persist_upload_proof(self, s):
        ids=s.image_uploads.ordered_ids
        if len(ids)!=len(s.payload['images']) or not ids:
            raise ValueError('image upload proof unavailable')
        proof={**self._upload_proof_scope(s),'upload_ids':ids}
        key='publish.image_uploads.'+s.binding['delivery_id']+'.'+str(s.binding['attempt'])
        with session_scope(self.engine) as db:
            row=db.get(Setting,key)
            if row:
                if json.loads(row.value_json)!=proof:
                    raise ValueError('image upload proof changed')
            else:
                db.add(Setting(key=key,value_json=json.dumps(proof,ensure_ascii=False)))

    def _restore_upload_proof(self, s):
        key='publish.image_uploads.'+s.binding['delivery_id']+'.'+str(s.binding['attempt'])
        with session_scope(self.engine) as db:
            row=db.get(Setting,key)
            proof=json.loads(row.value_json) if row else None
        if not isinstance(proof,dict) or any(proof.get(k)!=v for k,v in self._upload_proof_scope(s).items()):
            return
        ids=proof.get('upload_ids')
        if not isinstance(ids,list) or len(ids)!=len(s.payload['images']) or not all(isinstance(x,str) and 1<=len(x)<=2048 for x in ids):
            return
        s.image_uploads.ids=list(ids)

    def validate_fields(self, payload):
        if payload.get('platform') != 'douyin' or payload.get('content_type') != 'image_post':
            return False
        try:
            self._form(payload)
            if payload['fields'].get('location') or payload['fields'].get('music'):
                return False
            if payload['fields'].get('declaration'):
                from yijianjifa.app_services.douyin_declaration_catalog import catalog
                if payload['fields']['declaration'] not in catalog()['items']:
                    return False
        except (ValueError, KeyError, TypeError):
            return False
        return True

    def plan_readiness(self, payload):
        try:
            account = self.service.get(payload['account_id'])
            if account.platform != 'douyin' or account.login_state != 'valid' or not account.session_ref:
                return 'account_verification_required'
            if not self.validate_fields(payload):
                return 'platform_fields_invalid'
            if payload['execution_mode'] not in {'publish_now', 'platform_schedule'}:
                return 'execution_mode_not_verified'
        except (ValueError, KeyError, TypeError):
            return 'platform_fields_invalid'
        return None

    def _open(self, s, *, query=False):
        if s.page is not None:
            return
        super()._open(s, query=query)
        s.image_uploads = ImageUploadLedger(s.payload['images'])
        self._restore_upload_proof(s)
        s.image_receipt = None
        s.image_request = None
        s.image_request_invalid = False
        s.image_confirm_attempted = False
        def observe(response):
            request = response.request
            parsed = urlsplit(request.url)
            if parsed.hostname == legacy.HOST and parsed.path.rstrip('/') == CREATE_PATH:
                if not s.final or not s.image_request:
                    return
                try:
                    item_id = verified_image_receipt(s.payload, s.image_request, response.json(), s.image_uploads.ordered_ids)
                except Exception:
                    item_id = None
                if item_id:
                    s.image_receipt = item_id
                    self._trace(s, 'image.receipt_received', {'verified': True, 'image_count': len(s.payload['images'])})
                return
            try:
                s.image_uploads.observe_upload(request, response)
            except Exception:
                pass
        s.page.on('response', observe)
        def guard(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.hostname != legacy.HOST or parsed.path.rstrip('/') != CREATE_PATH:
                route.fallback(); return
            try:
                packet = request.post_data_json
            except Exception:
                packet = None
            if os.environ.get('P011_PUBLISH_DIAGNOSTICS')=='1' and isinstance(packet,dict):
                # Only schema and this test's public content/clock; no headers,
                # tokens, cookies, or signed URLs are included.
                safe={'schema':{str(k):type(v).__name__ for k,v in packet.items()},
                    'fields':{k:packet[k] for k in ('title','item_title','caption','desc','description','text','scheduled_publish_time','schedule_time','publish_time','is_schedule') if k in packet},
                    'images':{k:{'type':type(packet[k]).__name__,'item_keys':[list(x) if isinstance(x,dict) else type(x).__name__ for x in (_json(packet[k]) or [])[:30]] if isinstance(_json(packet[k]),list) else [],
                        'ids':[x if x in s.image_uploads.ordered_ids else 'unmatched' for x in (image_ids(packet[k]) or [])]} for k in ('images','image_list') if k in packet},
                    'authenticated_ids':s.image_uploads.ordered_ids}
                self.paths.logs_dir.mkdir(parents=True,exist_ok=True)
                (self.paths.logs_dir/('image-request-'+s.binding['delivery_id']+'.json')).write_text(json.dumps(safe,ensure_ascii=False,indent=2),encoding='utf-8')
            if not s.final or not matches_image_request(s.payload, packet, s.image_uploads.ordered_ids):
                s.image_request_invalid = True
                self._trace(s, 'image.form_verified', {'request_match': False})
                route.abort('blockedbyclient'); return
            if s.image_request is not None:
                route.abort('blockedbyclient'); return
            s.image_request = packet
            self._trace(s, 'image.form_verified', {'request_match': True})
            route.fallback()
        s.context.route('**/web/api/media/aweme/create*', guard)

    def _validate_session(self, s, payload, execution):
        if execution not in {'platform_schedule', 'publish_now'} or self.cancel.is_set():
            return AdapterValidation('', False)
        if not self._version(s):
            return self._park(s, 'login', '账号登录信息已更新，请重新确认原账号。')
        self._open(s)
        kind = self._challenge(s)
        if kind:
            return self._park(s, kind)
        observed = legacy.evaluate_script(s.page, legacy.READ_IDENTITY + 'return identity;', {})
        try:
            legacy.verified_identity(observed, expected_uid=payload['platform_user_id'])
        except ValueError:
            return AdapterValidation(str((observed or {}).get('uid', '')), False)
        return AdapterValidation(str(observed['uid']), True)

    def _outcome(self, s, item_id, *, published=False):
        account = self._account(s)
        self.refreshed[account.id] = {'platform_user_id': account.platform_user_id, 'nickname': account.nickname,
            'session_blob': legacy.session_blob(s.context, account.platform_user_id), 'original_ref': s.original_ref}
        scheduled = s.payload['execution_mode'] == 'platform_schedule'
        state = 'platform_scheduled' if scheduled else ('published' if published else 'platform_accepted')
        return AdapterOutcome(state, scheduled_at=s.payload['scheduled_at'] if scheduled else None,
            receipt_reference={'kind': 'image_post', 'item_id': item_id})

    def _prepare_result_query(self, s):
        # The shared resume lifecycle calls this before the platform controller.
        # Keep a live original image form; it may still own a confirmation or an
        # in-flight request. Reopened sessions already start on management.
        from .douyin_image_form import IMAGE_FORM_PATHS
        path = urlsplit(s.page.url).path if s.page is not None else ''
        if path in IMAGE_FORM_PATHS:
            return
        super()._prepare_result_query(s)

    def _advance(self, s, *, before_final=None, query=False):
        from .douyin_image_form import (IMAGE_UPLOAD_URL, PREPARE_IMAGE_FORM, FINAL_IMAGE_CLICK, CONFIRM_IMAGE_SUBMISSION,
                                        READ_IMAGE_RESULT, verify_image_form, verified_image_result)
        self._thread(); self._open(s, query=query)
        form = self._form(s.payload, query=query)
        if s.phase == 'baseline' and not s.final:
            s.phase = 'prepare'
            s.page.goto(IMAGE_UPLOAD_URL, wait_until='domcontentloaded')
        deadline = time.monotonic() + self.step_timeout
        while not self.cancel.is_set() and time.monotonic() < deadline:
            kind = self._challenge(s)
            if kind:
                return self._park(s, kind)
            if not self._version(s, query=query):
                return self._park(s, 'login', '账号登录资料已改变，请确认原账号后继续。')
            observed = legacy.evaluate_script(s.page, legacy.READ_IDENTITY + 'return identity;', {})
            try:
                legacy.verified_identity(observed, expected_uid=s.payload['platform_user_id'])
            except ValueError:
                return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed', error_code='account_mismatch')
            if s.final:
                s.stage = '正在核对图文发布结果'
                if s.image_receipt:
                    return self._outcome(s, s.image_receipt)
                if s.image_request_invalid:
                    return AdapterOutcome('result_unknown', error_code='image_receipt_invalid')
                # Keep the original submit page alive while it sends the packet
                # or shows its ordinary confirmation. Navigating immediately
                # can cancel the write and discard the confirmation.
                form['image_upload_ids'] = s.image_uploads.ordered_ids
                if not query and not s.image_request and not s.image_confirm_attempted and form['image_upload_ids']:
                    confirmation = legacy.evaluate_script(s.page, CONFIRM_IMAGE_SUBMISSION, form)
                    if isinstance(confirmation, dict) and confirmation.get('attempted'):
                        s.image_confirm_attempted = True
                        self._trace(s, 'image.confirm_submit', {'attempted': True})
                    elif isinstance(confirmation, dict) and confirmation.get('needs_user'):
                        return self._park(s, 'complex', '请在原浏览器完成平台确认后继续核对结果。')
                if query:
                    self._prepare_result_query(s)
                result = legacy.evaluate_script(s.page, READ_IMAGE_RESULT, form)
                # A previous identical post is never a receipt for this attempt.
                # Reopened readbacks require its already-bound platform ID.
                try:
                    proof = verified_image_result(result, form) if isinstance(result, dict) and (form.get('submission_item_id') or form['image_upload_ids']) else None
                except ValueError:
                    proof = None
                if proof:
                    self._trace(s, 'image.receipt_received', {'verified': True, 'source': 'management'})
                    return self._outcome(s, proof.get('item_id', ''), published=proof['published'])
                s.page.wait_for_timeout(150); continue
            if s.phase == 'prepare' and not s.uploaded:
                images = s.payload['images']
                root = (self.paths.data_dir / 'publish_resources').resolve()
                for image in images:
                    path = Path(image['path'])
                    if (path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file()
                            or sha256_file(path) != image['content_hash']):
                        raise ValueError('frozen image changed')
                inputs = s.page.locator('input[type="file"][multiple][accept*="image"]')
                if inputs.count() != 1:
                    s.stage = '等待图文上传入口'
                    s.page.wait_for_timeout(150); continue
                self._trace(s, 'image.upload_start', {'image_count': len(images)})
                inputs.set_input_files([image['path'] for image in images])
                s.uploaded = True
            form['image_upload_ids'] = s.image_uploads.ordered_ids
            raw = legacy.evaluate_script(s.page, PREPARE_IMAGE_FORM, form)
            if not isinstance(raw, dict):
                s.page.wait_for_timeout(150); continue
            s.stage = raw.get('stage', '正在准备图文发布资料')
            self._trace(s, 'image.form_verified', {'ready': raw.get('ready') is True,
                'image_count': raw.get('image_count', 0), 'order_verified': raw.get('image_order_verified') is True,
                'failure_code': raw.get('failure_code', '')})
            code = raw.get('failure_code')
            if code in {'image_preview_unavailable','image_upload_unverified','image_order_unverified'} and s.uploaded:
                self._diagnose_image_preview(s)
            waiting = {'image_order_unverified', 'image_upload_unverified', 'image_preview_unavailable'}
            if code and code not in waiting:
                if code == 'image_confirmation_required':
                    return self._park(s, 'complex', '请切换显示浏览器，完成平台验证后继续。')
                public = code if code in {'image_count_mismatch', 'image_type_unverified', 'image_declaration_unverified'} else 'image_fields_unverified'
                return AdapterOutcome('pre_submit_failed', error_code=public)
            if raw.get('ready') is True:
                verify_image_form(raw, form)
                self._diagnose_image_preview(s)
                self._trace(s, 'image.upload_ready', {'image_count': len(s.payload['images'])})
                self._trace(s, 'image.order_verified', {'verified': True})
                if not s.image_uploads.ordered_ids:
                    return AdapterOutcome('pre_submit_failed', error_code='image_upload_unverified')
                if self.inspection_only:
                    return self._inspection_complete(s)
                if before_final is None:
                    raise ValueError('final authority unavailable')
                # Recheck the original clock immediately before the journal.
                current = self._form(s.payload)
                current['image_upload_ids'] = s.image_uploads.ordered_ids
                verify_image_form(raw, current)
                self._persist_upload_proof(s)
                try:
                    before_final()
                except ValueError:
                    kind = self._challenge(s)
                    if s.request_id or kind:
                        return self._park(s, kind or 'login')
                    raise
                s.final = True; s.phase = 'result'
                try:
                    click=legacy.evaluate_script(s.page, FINAL_IMAGE_CLICK, current)
                    self._trace(s,'image.final_click',{'attempted':isinstance(click,dict) and click.get('attempted') is True,
                        'failure_code':click.get('failure_code','') if isinstance(click,dict) else 'unknown'})
                    if isinstance(click,dict) and click.get('attempted') is False:
                        # The authority journal remains consumed; never clear
                        # it or click twice. Surface the proven DOM rejection.
                        return AdapterOutcome('result_unknown',error_code='image_fields_unverified')
                except Exception:
                    pass
            s.page.wait_for_timeout(150)
        return AdapterOutcome('result_unknown' if s.final else 'pre_submit_failed',
            error_code='image_receipt_invalid' if s.final else 'image_upload_unverified')
