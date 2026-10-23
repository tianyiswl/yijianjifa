"""Independent Douyin image form controller using ordinary, observable DOM.

The upload entry and semantic fields are supported by the primary flowx source:
https://github.com/menciusnh4/flowx/blob/main/flowx-desktop/docs/抖音平台技术文档.md
Local fixtures verify our controller; they do not verify today's live platform DOM.
No private stores, cookies, upload-success flags, or video form fields. Receipt
reading uses bounded metadata attached to the observed visible image cards.
"""
from datetime import datetime, timedelta, timezone
import re

from .douyin_auth import verified_identity
from .douyin_capability import READ_IDENTITY
from .douyin_topics import compose_description

IMAGE_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload?default-tab=3"
IMAGE_FORM_PATHS = frozenset({"/creator-micro/content/upload", "/creator-micro/content/publish", "/creator-micro/content/post/image"})


def validate_image_form_request(*, expected_uid, title, body, tags="", declaration="", scheduled_time_text=None, image_names=(), now=None):
    if not isinstance(expected_uid, str) or not re.fullmatch(r"[0-9]{1,32}", expected_uid):
        raise ValueError("发布前需要核验抖音账号身份")
    if not isinstance(title, str) or not title.strip() or len(title) > 80:
        raise ValueError("图文标题须为 1 至 80 个字符，请修改后重试")
    description, topics = compose_description(body, tags)
    if not isinstance(declaration, str) or len(declaration) > 100:
        raise ValueError("图文内容声明无效，请重新选择")
    if not isinstance(image_names, (tuple, list)) or not 1 <= len(image_names) <= 30:
        raise ValueError("当前客户端每篇图文支持 1 至 30 张图片")
    names = list(image_names)
    if any(not isinstance(name, str) or not 1 <= len(name) <= 255 or name in {".", ".."} or re.search(r"[/\\\x00-\x1f]", name) for name in names) or len(set(names)) != len(names):
        raise ValueError("图文图片名称或顺序无效，请重新选择")
    if scheduled_time_text is not None:
        if not isinstance(scheduled_time_text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", scheduled_time_text):
            raise ValueError("定时时间格式不正确")
        when = datetime.strptime(scheduled_time_text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or not current + timedelta(hours=2.5) <= when <= current + timedelta(days=13):
            raise ValueError("定时时间需要在 2.5 小时至 13 天之后")
    return {"expected_uid": expected_uid, "title_text": title, "plain_body_text": body, "body_text": description,
            "topic_names": topics, "declaration_label": declaration, "scheduled_time_text": scheduled_time_text or "",
            "image_count": len(names), "image_names": names}


def _body(value, topics):
    value = value.replace("\u200b", "").replace("\r\n", "\n").strip()
    return re.sub(r"\s+", " ", value) if topics else value


def verify_image_form(value, request):
    if not isinstance(value, dict):
        raise ValueError("图文发布页面没有返回可核验资料")
    verified_identity(dict(value.get("identity", {})), expected_uid=request["expected_uid"])
    if value.get("path") not in IMAGE_FORM_PATHS or value.get("image_page") is not True:
        raise ValueError("当前页面不是可核验的抖音图文表单")
    if value.get("ready") is not True:
        raise ValueError(str(value.get("stage") or "图文表单尚未准备完成"))
    expected = {"title": request["title_text"], "declaration": request["declaration_label"], "declaration_verified": True,
                "topics": request["topic_names"], "scheduled_time_text": request["scheduled_time_text"],
                "schedule_mode": "scheduled" if request["scheduled_time_text"] else "immediate",
                "title_count": 1, "editor_count": 1, "mode_controls": 1, "final_button_count": 1, "final_button_enabled": True,
                "image_count": request["image_count"], "image_names": request["image_names"], "image_order_verified": True, "upload_complete": True}
    if any(value.get(key) != field for key, field in expected.items()):
        raise ValueError("图文图片、文案、声明或发布时间与安排不一致")
    if not isinstance(value.get("body"), str) or _body(value["body"], request["topic_names"]) != _body(request["body_text"], request["topic_names"]):
        raise ValueError("图文正文与安排不一致")
    upload_ids = request.get("image_upload_ids")
    if upload_ids and value.get("image_upload_ids") != upload_ids:
        raise ValueError("图文上传图片标识与安排顺序不一致")
    return {"title": value["title"], "body": value["body"], "declaration": value["declaration"], "image_count": value["image_count"],
            "mode": "platform_schedule" if request["scheduled_time_text"] else "publish_now", "prepared": True, "submitted": False}


IMAGE_FORM_COMMON = r'''
const visible = e => !!e && !!e.getClientRects().length && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden' && !e.closest('[aria-hidden="true"]');
const clean = value => String(value || '').replace(/\u200b/g, '').replace(/\r\n/g, '\n').trim();
const norm = value => clean(value).replace(/\s+/g, ' ');
const all = selector => Array.from(document.querySelectorAll(selector));
const shown = selector => all(selector).filter(visible);
const topics = typeof topic_names === 'undefined' ? [] : topic_names;
const names = typeof image_names === 'undefined' ? [] : image_names;
const uploadIds = typeof image_upload_ids === 'undefined' ? [] : image_upload_ids;
const scheduled = typeof scheduled_time_text === 'string' && scheduled_time_text.length > 0;
const desiredDeclaration = typeof declaration_label === 'string' ? declaration_label : '';
const sameList = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const checked = e => e.type === 'radio' || e.type === 'checkbox' ? e.checked === true : e.getAttribute('aria-checked') === 'true';
const labelText = e => clean(e.closest('label')?.innerText || e.getAttribute('aria-label') || e.innerText);
const validIdentity = identity && identity.uid === expected_uid && identity.uid_after === expected_uid && identity.status === 0 && identity.after_status === 0 && identity.profile_status === 0;
const isImagePage = () => location.origin === 'https://creator.douyin.com' && (
 location.pathname === '/creator-micro/content/post/image' || location.pathname === '/creator-micro/content/publish' ||
 (location.pathname === '/creator-micro/content/upload' && new URL(location.href).searchParams.get('default-tab') === '3'));
const fail = (result, code, stage) => ({...result, ready: false, failure_code: code, stage});
const modes = () => shown('input[type="radio"],input[type="checkbox"],[role="radio"]').filter(e => labelText(e) === (scheduled ? '定时发布' : '立即发布'));
const dateInputs = () => shown('input').filter(e => e.type === 'datetime-local' || /日期和时间|发布时间|定时时间/.test(e.placeholder || e.getAttribute('aria-label') || ''));
const dateText = e => String(e.value || '').replace('T', ' ').replace(/:00$/, match => /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:00$/.test(e.value) ? '' : match);
const editors = () => shown('[contenteditable="true"]').filter(e => !e.closest('[role="dialog"],dialog'));
const titles = () => shown('input').filter(e => /标题/.test(e.placeholder || e.getAttribute('aria-label') || '') && !e.closest('[role="dialog"],dialog'));
const leafText = (root, text) => Array.from(root.querySelectorAll('*')).filter(e => visible(e) && e.children.length === 0 && clean(e.textContent) === text);
const nativeValue = (input, text) => {
 input.focus();
 if (input.setSelectionRange) input.setSelectionRange(0, String(input.value).length);
 const inserted = document.execCommand('insertText', false, text);
 if (!inserted || input.value !== text) Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, text);
 input.dispatchEvent(new Event('input', {bubbles: true})); input.dispatchEvent(new Event('change', {bubbles: true})); input.blur();
};
const declarations = editor => {
 const controls = shown('select,[role="combobox"],.semi-select-selection-text').filter(e => !editor?.contains(e) && (
  /声明|标注/.test(e.getAttribute('aria-label') || e.closest('label')?.innerText || '') ||
  /^(请选择自主声明|请选择声明|请选择内容声明|无需添加自主声明|不添加声明)$/.test(clean(e.textContent)) || clean(e.textContent) === desiredDeclaration && desiredDeclaration));
 const unique = controls.filter(e => !controls.some(other => other !== e && e.contains(other)));
 if (unique.length === 1) {
  const control = unique[0];
  const text = control.tagName === 'SELECT' ? clean(control.selectedOptions[0]?.textContent) : clean(control.innerText || control.textContent);
  const empty = /^(请选择自主声明|请选择声明|请选择内容声明|无需添加自主声明|不添加声明)$/.test(text);
  return {count: 1, value: empty ? '' : text, verified: desiredDeclaration === '' ? empty : text === desiredDeclaration, control};
 }
 // Some creator forms render a simple placeholder span within a select wrapper.
 const markers = all('body *').filter(e => visible(e) && e.children.length === 0 && !editor?.contains(e) && /^(请选择自主声明|请选择声明|请选择内容声明|无需添加自主声明|不添加声明)$/.test(clean(e.textContent)));
 if (markers.length === 1) return {count: 1, value: '', verified: desiredDeclaration === '', control: markers[0].closest('[role="combobox"],button,.semi-select') || markers[0]};
 return {count: unique.length, value: null, verified: false, control: null};
};
const readTopics = editor => {
 if (!topics.length) return [];
 const rich = Array.from(editor.querySelectorAll('[data-mention="#"],[data-topic-name],a[href*="/hashtag/"]')).map(e => clean(e.getAttribute('data-topic-name') || e.textContent).replace(/^#/, ''));
 if (rich.length) return rich;
 // Ordinary image editors can submit literal hashtags. Prove their actual text,
 // in order, at the end of the real editor; never trust a controller flag.
 const suffix = topics.map(name => '#' + name).join(' ');
 return norm(editor.innerText).endsWith(norm(suffix)) ? topics.slice() : [];
};
const matchesImageUploadUri = (source, id) => {
 if (!id) return false;
 try {
  const url = new URL(source, location.href);if (!/^https?:$/.test(url.protocol)) return false;
  const path = decodeURIComponent(url.pathname), key = '/' + String(id).replace(/^\/+/, '');
  // The whole authenticated key must match. A signed query or ID substring
  // cannot establish identity, order, upload completion, or acceptance.
  return path.endsWith(key) || path.includes(key + '.') || path.includes(key + '~');
 } catch {return false;}
};
const renderedImageElement = e => !!e && !!e.getClientRects().length && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
const excludedImageElement = e => !!e.closest('svg,nav,aside,[class*="avatar"],[class*="user-info"],[class*="cover"],[class*="phone-preview"],[class*="preview-phone"],[class*="mobile-preview"],[class*="decoration"],[aria-label*="封面"],[aria-label*="手机"],[aria-label*="头像"]');
const backgroundImageUrl = e => {
 if (!renderedImageElement(e) || excludedImageElement(e)) return '';
 const value = String(getComputedStyle(e).backgroundImage || '');
 // A single real HTTP(S) image only. SVG/data/blob assets and multi-layer
 // decorative backgrounds do not become evidence for an uploaded image.
 const match = value.match(/^url\(\s*(?:"([^"]+)"|'([^']+)'|([^)]*))\s*\)$/);
 if (!match) return '';
 try {const url = new URL((match[1] || match[2] || match[3] || '').trim(), location.href);return /^https?:$/.test(url.protocol) ? url.href : '';} catch {return '';}
};
const httpImageUrl = e => {
 if (e.tagName !== 'IMG' || !renderedImageElement(e) || excludedImageElement(e)) return '';
 try {const url = new URL(e.currentSrc || e.src, location.href);return /^https?:$/.test(url.protocol) ? url.href : '';} catch {return '';}
};
const independentImageTiles = (items, area) => {
 const tiles = items.map(item => item.element.parentElement);
 if (tiles.some(tile => !tile || tile === area) || new Set(tiles).size !== items.length) return false;
 // The ordinary wrappers have a repeated public shape. SVG handles/deletion
 // icons may be siblings, but each independent tile holds one real HTTP photo.
 if (tiles.some(tile => Array.from(tile.querySelectorAll('img')).filter(httpImageUrl).length !== 1)) return false;
 if (new Set(tiles.map(tile => tile.tagName)).size !== 1) return false;
 const classes = tiles.map(tile => String(tile.getAttribute('class') || '').split(/\s+/).filter(Boolean));
 if (classes.every(tokens => tokens.length)) return classes[0].some(token => classes.every(tokens => tokens.includes(token)));
 if (classes.some(tokens => tokens.length)) return false;
 const shapes = tiles.map(tile => Array.from(tile.children).map(child => child.tagName).join(','));
 return new Set(shapes).size === 1;
};
const visualImageOrder = items => {
 const rects = items.map(item => ({...item, rect: item.element.getBoundingClientRect()}));
 if (rects.some(item => ![item.rect.left, item.rect.top, item.rect.right, item.rect.bottom, item.rect.width, item.rect.height].every(Number.isFinite) || item.rect.width <= 0 || item.rect.height <= 0)) return null;
 if (rects.length <= 1) return rects;
 // React may prepend the last completed upload. Prove displayed order using
 // geometry rather than treating asynchronous DOM insertion as image order.
 const row = Math.max(...rects.map(item => item.rect.top)) - Math.min(...rects.map(item => item.rect.top)) <= 2;
 const column = Math.max(...rects.map(item => item.rect.left)) - Math.min(...rects.map(item => item.rect.left)) <= 2;
 if (row) {
  rects.sort((a, b) => a.rect.left - b.rect.left);
  return rects.every((item, index) => index === 0 || rects[index - 1].rect.right <= item.rect.left + 2) ? rects : null;
 }
 if (column) {
  rects.sort((a, b) => a.rect.top - b.rect.top);
  return rects.every((item, index) => index === 0 || rects[index - 1].rect.bottom <= item.rect.top + 2) ? rects : null;
 }
 // A wrapped grid or overlapping tiles need actual layout evidence before
 // automatic submission; do not silently infer an order from URI or filename.
 return null;
};
const backgroundImageManifest = () => {
 const empty = {image_count: 0, image_names: [], image_upload_ids: [], image_order_verified: false, upload_complete: false, image_container_count: 0, image_dom_mode: 'background'};
 // Find the deepest ordinary DOM node containing the complete public counter.
 // The text may be split over spans. Neither a CSS hash nor editor metadata
 // identifies the upload area, and a phone/cover preview is excluded.
 const markers = all('body *').filter(e => renderedImageElement(e) && !excludedImageElement(e) && !e.closest('[contenteditable="true"]') && /已添加\s*\d+\s*张图片/.test(e.innerText || ''));
 const deepest = markers.filter(e => !markers.some(other => other !== e && e.contains(other)));
 if (deepest.length !== 1) return {...empty, image_failure: 'image_preview_unavailable'};
 const marker = deepest[0], count = Number((marker.innerText || '').match(/已添加\s*(\d+)\s*张图片/)[1]);
 let area = null, previews = [], imageMode = 'background';
 const textControls = [...titles(), ...editors()];
 // Walk only this bounded edit area. Stop before crossing into any title or
 // body control, rather than scanning page-wide images or a side preview.
 for (let node = marker, depth = 0; node && depth < 7; node = node.parentElement, depth++) {
  if (['BODY', 'HTML'].includes(node.tagName) || excludedImageElement(node) || textControls.some(control => node.contains(control))) break;
  const images = Array.from(node.querySelectorAll('img')).map(element => ({element, url: httpImageUrl(element)})).filter(item => item.url);
  const candidates = images.length ? images : [node, ...Array.from(node.querySelectorAll('*'))].map(element => ({element, url: backgroundImageUrl(element)})).filter(item => item.url);
  if (!area || candidates.length > previews.length) {area = node;previews = candidates;imageMode = images.length ? 'counter-http-img' : 'background';}
  if (candidates.length === count && count > 0) {area = node;previews = candidates;imageMode = images.length ? 'counter-http-img' : 'background';break;}
 }
 if (!area || !previews.length) return {...empty, declared_image_count: count, image_failure: 'image_preview_unavailable'};
 const out = {...empty, image_container_count: 1, declared_image_count: count, image_count: previews.length, image_dom_mode: imageMode};
 const ackComplete = uploadIds.length === names.length && names.length > 0;
 if (count !== names.length || previews.length !== count) return {...out, image_failure: ackComplete ? 'image_count_mismatch' : 'image_upload_unverified'};
 const shaped = imageMode !== 'counter-http-img' || independentImageTiles(previews, area);
 const visual = imageMode === 'counter-http-img' ? visualImageOrder(previews) : previews;
 const ordered = ackComplete && shaped && visual !== null && visual.every((item, index) => matchesImageUploadUri(item.url, uploadIds[index]));
 out.image_order_verified = ordered;
 out.image_names = ordered ? names.slice() : previews.map(() => '');
 out.image_upload_ids = ordered ? uploadIds.slice() : [];
 const busy = Array.from(area.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="uploading"],[class*="loading"]')).some(renderedImageElement);
 const broken = imageMode === 'counter-http-img' && previews.some(item => !item.element.complete || item.element.naturalWidth <= 0 || item.element.naturalHeight <= 0);
 const errors = Array.from(area.querySelectorAll('*')).some(e => renderedImageElement(e) && e.children.length === 0 && /上传失败|图片损坏|格式不支持/.test(clean(e.textContent)));
 // The host binds successful authenticated upload responses to exact frozen
 // image bytes. A styled URL alone, even signed, never proves completion.
 out.upload_complete = ordered && !busy && !broken && !errors;
 out.image_failure = errors ? 'image_upload_failed' : busy || broken ? '' : !ackComplete ? 'image_upload_unverified' : !ordered ? 'image_order_unverified' : '';
 return out;
};
const imageManifest = () => {
 const inputs = all('input[type="file"]').filter(e => e.multiple && /image\//i.test(e.accept));
 const roots = [];
 for (const input of inputs) {
  const area = input.closest('.upload-container,[class*="image-upload"],[class*="upload-images"],[class*="image-list"]') || input.parentElement;
  if (area) roots.push(area);
 }
 for (const area of shown('[class*="image-list"],[aria-label="图片预览"],[data-content-type="image"]')) roots.push(area);
 const unique = Array.from(new Set(roots)).filter(e => !roots.some(other => other !== e && other.contains(e)));
 const result = {image_count: 0, image_names: [], image_upload_ids: [], image_order_verified: false, upload_complete: false, image_container_count: unique.length};
 if (unique.length !== 1) return backgroundImageManifest();
 const area = unique[0];
 const previews = Array.from(area.querySelectorAll('img')).filter(e => visible(e) && !e.closest('header,nav,[class*="avatar"],[class*="user-info"]'));
 if (!previews.length) return backgroundImageManifest();
 const tiles = previews.map(e => e.closest('[class*="image-item"],[class*="upload-item"],[class*="image-preview"]') || e.parentElement);
 result.image_count = previews.length;
 const actualNames = previews.map((e, i) => {
  const own = e.getAttribute('data-file-name') || e.getAttribute('data-filename') || e.getAttribute('alt') || e.getAttribute('title') || '';
  if (names.includes(own)) return own;
  const labels = names.filter(name => leafText(tiles[i], name).length === 1);
  return labels.length === 1 ? labels[0] : '';
 });
 const idMatches = uploadIds.length === names.length && previews.length === names.length && previews.every((e, i) => {
  const id = String(uploadIds[i] || ''); if (!id) return false;
  const exposed = e.getAttribute('data-image-id') || tiles[i]?.getAttribute('data-image-id');
  if (exposed) return exposed === id;
  return matchesImageUploadUri(e.currentSrc || e.src, id);
 });
 const nameMatches = sameList(actualNames, names) && names.length > 0;
 result.image_names = idMatches ? names.slice() : actualNames;
 result.image_upload_ids = idMatches ? uploadIds.slice() : [];
 result.image_order_verified = uploadIds.length ? idMatches : nameMatches;
 const busy = Array.from(area.querySelectorAll('[aria-busy="true"],[role="progressbar"],[class*="uploading"],[class*="loading"]')).some(visible);
 const broken = previews.some(e => !e.complete || e.naturalWidth <= 0 || e.naturalHeight <= 0);
 const errors = Array.from(area.querySelectorAll('*')).some(e => visible(e) && e.children.length === 0 && /上传失败|图片损坏|格式不支持/.test(clean(e.textContent)));
 const statuses = tiles.every(tile => tile && (
  /^(uploaded|complete|success)$/.test(tile.getAttribute('data-upload-status') || '') || leafText(tile, '上传成功').length === 1));
 // Authenticated upload IDs supplied by the host prove server upload, but each
 // must also correspond to the actual displayed preview in the same order.
 result.upload_complete = result.image_count === names.length && result.image_order_verified && !busy && !broken && !errors && (idMatches || statuses);
 result.image_failure = errors ? 'image_upload_failed' : broken || busy ? '' : !result.image_order_verified ? 'image_order_unverified' : !result.upload_complete ? 'image_upload_unverified' : '';
 return result;
};
const observeForm = (confirmedDialog = false) => {
 const out = {identity, path: location.pathname, image_page: isImagePage(), ready: false, stage: '等待抖音图文编辑页'};
 if (!validIdentity) return fail(out, 'image_identity_unverified', '抖音登录已失效或账号身份不符，请重新登录');
 if (!out.image_page) return fail(out, 'image_page_unverified', '当前页面不是抖音图文发布页面');
 const titleControls = titles(), bodyControls = editors();
 out.title_count = titleControls.length; out.editor_count = bodyControls.length;
 if (titleControls.length !== 1 || bodyControls.length !== 1) return {...out, stage: '未找到唯一的图文标题和正文控件'};
 const title = titleControls[0], editor = bodyControls[0];
 if (title.maxLength >= 0 && title_text.length > title.maxLength) return fail(out, 'image_title_too_long', '平台标题控件最多允许 ' + title.maxLength + ' 个字符，当前标题有 ' + title_text.length + ' 个，请修改标题');
 const bodyMax = Number(editor.getAttribute('maxlength') || editor.getAttribute('data-maxlength') || 1000);
 if (Number.isFinite(bodyMax) && bodyMax > 0 && body_text.length > bodyMax) return fail(out, 'image_body_too_long', '平台正文控件最多允许 ' + bodyMax + ' 个字符，请修改正文及标签');
 const declaration = declarations(editor), mode = modes();
 out.title = title.value; out.body = clean(editor.innerText); out.topics = readTopics(editor);
 out.declaration = declaration.value; out.declaration_verified = declaration.verified; out.declaration_control_count = declaration.count;
 out.mode_controls = mode.length; out.schedule_mode = mode.length === 1 && checked(mode[0]) ? scheduled ? 'scheduled' : 'immediate' : '';
 const dates = dateInputs(); out.scheduled_time_text = scheduled && dates.length === 1 ? dateText(dates[0]) : '';
 Object.assign(out, imageManifest());
 const buttons = shown('button').filter(e => clean(e.textContent) === '发布');
 out.final_button_count = buttons.length; out.final_button_enabled = buttons.length === 1 && !buttons[0].disabled && buttons[0].getAttribute('aria-disabled') !== 'true';
 const dialogs = shown('[role="dialog"],dialog,.semi-modal-content');
 if (dialogs.length && !confirmedDialog) return fail(out, 'image_confirmation_required', '请在显示浏览器中处理平台验证或确认弹窗，再继续原任务');
 if (out.image_failure) return fail(out, out.image_failure, ({image_upload_failed: '图片上传失败，请检查平台提示', image_count_mismatch: '平台中的图片数量与安排不符，请核对原图文页面', image_order_unverified: '无法核对平台图片数量和顺序，请打开浏览器检查', image_upload_unverified: '无法确认平台图片已经上传完成', image_preview_unavailable: '未找到唯一的图文图片预览区域'})[out.image_failure]);
 const bodyMatches = topics.length ? norm(out.body) === norm(body_text) : clean(out.body) === clean(body_text);
 out.ready = out.title === title_text && bodyMatches && sameList(out.topics, topics) && out.declaration_verified && out.mode_controls === 1 && out.schedule_mode === (scheduled ? 'scheduled' : 'immediate') && out.scheduled_time_text === (scheduled ? scheduled_time_text : '') && out.upload_complete && out.final_button_enabled;
 out.stage = out.ready ? '图文表单已核对，尚未提交' : '正在准备图文发布表单';
 return out;
};
'''


PREPARE_IMAGE_FORM = READ_IDENTITY + IMAGE_FORM_COMMON + r'''
let result = observeForm();
if (!validIdentity || !result.image_page || result.failure_code && !['image_order_unverified', 'image_upload_unverified', 'image_preview_unavailable'].includes(result.failure_code)) return result;
const titleControls = titles(), bodyControls = editors();
if (titleControls.length !== 1 || bodyControls.length !== 1) return result;
const title = titleControls[0], editor = bodyControls[0];
if (title.value !== title_text) {nativeValue(title, title_text); return {...result, ready: false, stage: '填写图文标题'};}
const bodyMatches = topics.length ? norm(editor.innerText) === norm(body_text) : clean(editor.innerText) === clean(body_text);
if (!bodyMatches) {
 editor.focus();const range = document.createRange(), selection = window.getSelection();range.selectNodeContents(editor);selection.removeAllRanges();selection.addRange(range);
 if (!document.execCommand('insertText', false, body_text)) return fail(result, 'image_body_input_failed', '图文正文输入失败，请打开浏览器检查');
 editor.dispatchEvent(new Event('input', {bubbles: true}));editor.dispatchEvent(new Event('change', {bubbles: true}));editor.blur();
 return {...result, ready: false, stage: '填写图文正文和标签'};
}
const declaration = declarations(editor);
if (!declaration.verified) {
 if (!declaration.control) return fail(result, 'image_declaration_unverified', '无法核对图文内容声明，请打开浏览器确认');
 if (declaration.control.tagName === 'SELECT') {
  const wanted = Array.from(declaration.control.options).filter(option => clean(option.textContent) === desiredDeclaration || !desiredDeclaration && /^(不添加声明|无需添加自主声明|请选择自主声明|请选择声明)$/.test(clean(option.textContent)));
  if (wanted.length !== 1) return fail(result, 'image_declaration_unavailable', '平台没有唯一匹配的图文声明选项');
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(declaration.control, wanted[0].value);
  declaration.control.dispatchEvent(new Event('input', {bubbles: true}));declaration.control.dispatchEvent(new Event('change', {bubbles: true}));
  return {...result, ready: false, stage: '设置图文内容声明'};
 }
 // Opening the requested declaration control is reversible. A platform modal
 // still requires observable confirmation, never automatic agreement acceptance.
 declaration.control.click();return {...result, ready: false, stage: '请在浏览器选择对应内容声明后继续'};
}
const mode = modes();
if (mode.length !== 1) return fail(result, 'image_schedule_control_unavailable', '未找到唯一的图文发布时间设置');
if (!checked(mode[0])) {mode[0].click();return {...result, ready: false, stage: scheduled ? '选择图文定时发布' : '选择图文立即发布'};}
if (scheduled) {
 const dates = dateInputs();if (dates.length !== 1) return fail(result, 'image_schedule_input_unavailable', '未找到唯一的图文定时时间输入框');
 const date = dates[0], wanted = date.type === 'datetime-local' ? scheduled_time_text.replace(' ', 'T') : scheduled_time_text;
 if (date.min && wanted < date.min || date.max && wanted > date.max) return fail(result, 'image_schedule_outside_platform_range', '所选定时时间超出平台当前允许范围，请修改时间');
 if (dateText(date) !== scheduled_time_text) {nativeValue(date, wanted);return {...result, ready: false, stage: '填写图文精确定时时间'};}
}
return observeForm();
'''


FINAL_IMAGE_CLICK = READ_IDENTITY + IMAGE_FORM_COMMON + r'''
const result = observeForm();
if (!result.ready) return {...result, attempted: false};
// Re-read the very same DOM and exact request immediately before the only click.
const fresh = observeForm();
if (!fresh.ready) return {...fresh, attempted: false};
const buttons = shown('button').filter(e => clean(e.textContent) === '发布' && !e.disabled && e.getAttribute('aria-disabled') !== 'true');
if (buttons.length !== 1) return fail({...fresh, attempted: false}, 'image_submit_button_unavailable', '图文发布按钮不唯一或不可用');
buttons[0].click();return {...fresh, ready: true, attempted: true, submitted: false};
'''


# A second, ordinary confirmation is part of the original attempt. Never agree
# to declarations, rights, or new terms. The frozen form must remain unchanged.
CONFIRM_IMAGE_SUBMISSION = READ_IDENTITY + IMAGE_FORM_COMMON + r'''
const dialogs = shown('[role="dialog"],dialog,.semi-modal-content').filter(e => !shown('[role="dialog"],dialog,.semi-modal-content').some(other => other !== e && e.contains(other)));
if (!dialogs.length) return {confirmation: false, attempted: false};
if (dialogs.length !== 1 || /协议|同意|原创|权益|授权|验证码|扫码|验证|规则/.test(dialogs[0].innerText || '')) return {confirmation: true, attempted: false, needs_user: true};
const buttons = Array.from(dialogs[0].querySelectorAll('button')).filter(e => visible(e) && clean(e.textContent) === '确认发布' && !e.disabled && e.getAttribute('aria-disabled') !== 'true');
const form = observeForm(true);
if (buttons.length !== 1 || !form.ready) return {confirmation: true, attempted: false, needs_user: true};
buttons[0].click();return {confirmation: true, attempted: true};
'''


READ_IMAGE_RESULT = READ_IDENTITY + r'''
const out = {identity, ready: false, path: location.pathname, source: 'visible-image-card', matches: []};
if (location.origin !== 'https://creator.douyin.com' || location.pathname !== '/creator-micro/content/manage' || identity.uid !== expected_uid || identity.uid_after !== expected_uid || identity.status !== 0 || identity.after_status !== 0 || identity.profile_status !== 0) return out;
const visible = e => !!e && !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
const norm = text => String(text || '').replace(/\u200b/g, '').replace(/\s+/g, ' ').trim();
const uriKey = value => typeof value === 'string' ? value.match(/^(?:tos-cn-i-jm8ajry58r|tos-cn-i-dy)\/([0-9a-f]{32})$/)?.[1] || '' : '';
const ids = typeof image_upload_ids === 'undefined' ? [] : image_upload_ids;
const keys = Array.isArray(ids) ? ids.map(uriKey) : [];
if (!keys.length || keys.some(key => !key) || new Set(keys).size !== keys.length || keys.length !== image_count) return {...out, failure_code: 'image_result_upload_proof_unavailable'};
// These buckets are aliases observed for the same authenticated image bytes.
// Do not infer aliases from arbitrary CDN URLs or trust only a common suffix.
const scheduled = typeof scheduled_time_text === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(scheduled_time_text);
if (!scheduled) return out;
const epoch = Math.floor(new Date(scheduled_time_text.replace(' ', 'T') + ':00+08:00').getTime() / 1000);
if (!Number.isSafeInteger(epoch)) return out;
const topics = typeof topic_names === 'undefined' ? [] : topic_names;
const captions = new Set([norm(title_text + '。' + body_text), norm(title_text + '。' + plain_body_text + topics.map(name => '#' + name).join(''))]);
const wantedId = typeof submission_item_id === 'undefined' ? '' : String(submission_item_id || '');
if (Array.from(document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')).some(visible)) return out;
const allCards = Array.from(document.querySelectorAll('[class*="video-card-s"]')).filter(visible);
const cards = allCards.filter(card => !allCards.some(other => other !== card && other.contains(card)));
for (const card of cards) {
 const domCaptions = Array.from(card.querySelectorAll('*')).filter(visible).map(node => norm(node.innerText)).filter(text => captions.has(text));
 if (!domCaptions.length || !/定时发布中/.test(card.innerText || '')) continue;
 const cardText = card.innerText || '';
 const dates = [...cardText.matchAll(/(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})/g), ...cardText.matchAll(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/g)].map(match => match[1] + '-' + match[2].padStart(2, '0') + '-' + match[3].padStart(2, '0') + ' ' + match[4] + ':' + match[5]);
 if (dates.length !== 1 || dates[0] !== scheduled_time_text) continue;
 // The live card exposes no data-id/link. Read only the observed card's own
 // React binding, bounded to eight return edges and its memoizedProps.data.
 // No state mutation, unrelated store traversal, cookies, or page-wide globals.
 const bindings = Object.getOwnPropertyNames(card).filter(key => key.startsWith('__reactFiber$'));
 if (bindings.length !== 1) continue;
 const candidates = new Set();
 let fiber = card[bindings[0]];
 for (let depth = 0; fiber && depth <= 8; depth++, fiber = fiber.return) {
  const data = fiber.memoizedProps?.data;
  if (data && typeof data === 'object' && captions.has(norm(data.desc))) candidates.add(data);
 }
 if (candidates.size !== 1) continue;
 const data = Array.from(candidates)[0], id = data.aweme_id, status = data.status, timer = data.timer;
 if (typeof id !== 'string' || !/^[0-9]{1,32}$/.test(id) || wantedId && id !== wantedId || data.author?.uid !== expected_uid || String(data.author_user_id) !== expected_uid || data.is_pic_word !== true || data.aweme_type !== 2) continue;
 if (!status || status.aweme_id !== id || status.in_reviewing !== false || status.is_delete !== false || status.is_prohibited !== false || data.status_value !== 140) continue;
 if (!timer || timer.status !== 0 || !Number.isSafeInteger(timer.public_time) || timer.public_time !== epoch || !Array.isArray(data.images)) continue;
 const actualUris = data.images.map(image => image?.uri), actualKeys = actualUris.map(uriKey);
 if (JSON.stringify(actualKeys) !== JSON.stringify(keys)) continue;
 out.matches.push({content_type: 'image', item_id: id, status_item_id: status.aweme_id, author_uid: data.author.uid,
  author_user_id: String(data.author_user_id), caption: norm(data.desc), dom_caption: domCaptions[0], image_uris: actualUris,
  is_pic_word: data.is_pic_word, aweme_type: data.aweme_type, review: 'scheduled', time: dates[0],
  scheduled_epoch: timer.public_time, timer_status: timer.status, status_value: data.status_value,
  in_reviewing: status.in_reviewing, is_delete: status.is_delete, is_prohibited: status.is_prohibited});
}
out.ready = out.matches.length === 1;
return out;
'''


def verified_image_result(value, request):
    """A unique observed image card bound to frozen bytes and exact scheduling.

    Card fields and the two URI bucket aliases were observed on 2026-10-14.
    Only that scheduled state is accepted; a receipt is not public visibility.
    """
    if not isinstance(value, dict):
        return None
    try:
        verified_identity(value.get("identity"), expected_uid=request["expected_uid"])
    except (ValueError, KeyError):
        return None
    if value.get("path") != "/creator-micro/content/manage" or value.get("source") != "visible-image-card" or value.get("ready") is not True:
        return None
    rows = value.get("matches")
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    row = rows[0]
    if not isinstance(row, dict) or row.get("content_type") != "image":
        return None
    item_id = row.get("item_id")
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,32}", item_id) or row.get("status_item_id") != item_id:
        return None
    if request.get("submission_item_id") and item_id != request["submission_item_id"]:
        return None
    uid = request["expected_uid"]
    if row.get("author_uid") != uid or row.get("author_user_id") != uid or row.get("is_pic_word") is not True or type(row.get("aweme_type")) is not int or row["aweme_type"] != 2:
        return None
    normalize = lambda text: re.sub(r"\s+", " ", str(text).replace("\u200b", "")).strip()
    captions = {normalize(request["title_text"] + "。" + request["body_text"]),
                normalize(request["title_text"] + "。" + request["plain_body_text"] + "".join("#" + tag for tag in request["topic_names"]))}
    if not isinstance(row.get("caption"), str) or not isinstance(row.get("dom_caption"), str) or row["caption"] not in captions or row["dom_caption"] not in captions:
        return None
    def canonical(values):
        if not isinstance(values, (list, tuple)):
            return None
        keys = []
        for uri in values:
            match = re.fullmatch(r"(?:tos-cn-i-jm8ajry58r|tos-cn-i-dy)/([0-9a-f]{32})", uri) if isinstance(uri, str) else None
            if not match:
                return None
            keys.append(match[1])
        return keys
    keys = canonical(request.get("image_upload_ids"))
    if not keys or len(keys) != request["image_count"] or len(set(keys)) != len(keys) or canonical(row.get("image_uris")) != keys:
        return None
    when = request.get("scheduled_time_text")
    if not when or row.get("time") != when or row.get("review") != "scheduled":
        return None
    try:
        epoch = int(datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
    except (TypeError, ValueError):
        return None
    if type(row.get("scheduled_epoch")) is not int or row["scheduled_epoch"] != epoch or type(row.get("timer_status")) is not int or row["timer_status"] != 0 or type(row.get("status_value")) is not int or row["status_value"] != 140:
        return None
    if any(row.get(flag) is not False for flag in ("in_reviewing", "is_delete", "is_prohibited")):
        return None
    return {"accepted": True, "published": False, "scheduled_time_text": when, "item_id": item_id}
