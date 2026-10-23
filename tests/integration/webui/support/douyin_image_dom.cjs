// Deterministic ordinary DOM only. No browser, framework stores, or real network.
const fs = require('node:fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const change = payload.change || {}, request = payload.request;
let submitClicks = 0;
const events = [];
function simpleMatch(node, selector) {
  const tag = selector.match(/^[a-zA-Z]+/)?.[0];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const match of selector.matchAll(/\.([\w-]+)/g)) {
    if (!String(node.attrs.class || '').split(/\s+/).includes(match[1])) return false;
  }
  for (const match of selector.matchAll(/\[([\w-]+)(?:(\*=|=)"([^"]*)")?\]/g)) {
    const actual = node.getAttribute(match[1]);
    if (actual === null) return false;
    if (match[2] === '=' && actual !== match[3]) return false;
    if (match[2] === '*=' && !actual.includes(match[3])) return false;
  }
  return true;
}
class Element {
  constructor(tag, attrs = {}, text = '') {
    this.tagName = tag.toUpperCase();this.attrs = {...attrs};this.children = [];this.parentElement = null;
    this._text = text;this._value = '';this.hidden = false;this.checked = false;this.disabled = false;
    this.maxLength = -1;this.complete = true;this.naturalWidth = 80;this.naturalHeight = 60;
  }
  append(...values) {for (const value of values) {value.parentElement = this;this.children.push(value);}return this;}
  get value() {return this._value;}
  set value(value) {this._value = value;}
  get innerText() {return [this._text, ...this.children.map(e => e.innerText)].filter(Boolean).join(' ');}
  set innerText(value) {this._text = value;this.children = [];}
  get textContent() {return this.innerText;}
  get placeholder() {return this.attrs.placeholder || '';}
  get type() {return this.attrs.type || 'text';}
  get accept() {return this.attrs.accept || '';}
  get multiple() {return this.attrs.multiple === 'true';}
  get src() {return this.attrs.src || '';}
  get currentSrc() {return this.src;}
  get min() {return this.attrs.min || '';}
  get max() {return this.attrs.max || '';}
  get options() {return this.children.filter(e => e.tagName === 'OPTION');}
  get selectedOptions() {return this.options.filter(e => e.attrs.value === this.value);}
  getAttribute(name) {return this.attrs[name] ?? null;}
  matches(selector) {return selector.split(',').some(part => simpleMatch(this, part.trim()));}
  descendants() {return this.children.flatMap(node => [node, ...node.descendants()]);}
  querySelectorAll(selector) {
    if (selector === '*') return this.descendants();
    return this.descendants().filter(node => node.matches(selector));
  }
  closest(selector) {for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;return null;}
  contains(node) {return node === this || this.descendants().includes(node);}
  getClientRects() {for (let node = this; node; node = node.parentElement) if (node.hidden) return [];return [{}];}
  getBoundingClientRect() {
    const left = Number(this.attrs.rectLeft ?? 0), top = Number(this.attrs.rectTop ?? 0), width = Number(this.attrs.rectWidth ?? 79), height = Number(this.attrs.rectHeight ?? 105);
    return {left, top, right: left + width, bottom: top + height, width, height};
  }
  focus() {document.activeElement = this;}
  blur() {if (document.activeElement === this) document.activeElement = null;}
  setSelectionRange() {}
  dispatchEvent(event) {events.push({tag: this.tagName, name: this.placeholder || this.attrs['aria-label'] || '', type: event.type});}
  click() {
    if (this.type === 'radio') {for (const input of document.querySelectorAll('input[type="radio"]')) input.checked = input === this;}
    if (this.tagName === 'BUTTON' && ['发布', '确认发布'].includes(this.textContent)) submitClicks++;
  }
}
const body = new Element('body');
const avatar = new Element('img', {alt: '用户头像', src: 'https://images.test/avatar.jpg'});
body.append(new Element('header').append(avatar));
const form = new Element('section');body.append(form);
const counterTiles = change.backgroundTiles || change.counterHttpTiles;
const area = new Element('div', {class: counterTiles ? 'arbitrary-rendered-class' : 'upload-container'});form.append(area);
const file = new Element('input', {type: 'file', accept: 'image/png, image/jpeg, image/webp', multiple: 'true'});
file.hidden = true;file.files = request.image_names.map(name => ({name}));
if (!counterTiles) area.append(file);
if (counterTiles) {
  const counter = new Element('div');
  counter.append(new Element('span', {}, '已添加'), new Element('span', {}, String(change.counterCount ?? request.image_names.length)), new Element('span', {}, '张图片'));
  area.append(counter);
}
const counterContainer = change.nestedCounterContainer ? new Element('div', {class: 'unhashed-fixture-container'}) : area;
if (counterContainer !== area) area.append(counterContainer);
let manifest = request.image_names.map((name, index) => ({name, id: `upload-image-${index + 1}`}));
if (change.reversedImages) manifest.reverse();
if (change.missingImage) manifest.pop();
for (const [domIndex, item] of manifest.entries()) {
  if (change.counterHttpTiles) {
    const itemIndex = Number(item.id.match(/\d+$/)[0]) - 1;
    const visualIndex = change.reversedVisual ? request.image_names.length - 1 - itemIndex : itemIndex;
    const left = change.overlappedImages ? 8 : 8 + visualIndex * 95;
    const top = change.verticalImages ? 8 + visualIndex * 121 : 8;
    const tile = new Element('div', {class: 'unhashed-fixture-tile'});
    const image = new Element('img', {src: `https://images.test/${item.id}.jpg?signature=not-evidence`, rectLeft: change.verticalImages ? 8 : left, rectTop: top, rectWidth: change.zeroWidthImage ? 0 : 79});
    if (change.hiddenImage) image.hidden = true;
    if (change.brokenImage) image.naturalWidth = 0;
    tile.append(image, new Element('img', {src: 'data:image/svg+xml,drag-handle', rectWidth: 6, rectHeight: 111}), new Element('img', {src: 'data:image/svg+xml,delete-icon', rectWidth: 0, rectHeight: 0}));
    if (change.sameHttpParent) {
      area.append(image);
    } else counterContainer.append(tile);
    continue;
  }
  if (change.backgroundTiles) {
    const source = change.dataBackground ? 'data:image/svg+xml,decoration' : `https://images.test/${item.id}.jpg?signature=must-not-be-a-receipt`;
    const tile = new Element('div', {class: 'another-arbitrary-class', backgroundImage: `url("${source}")`});
    tile.append(new Element('svg'));
    if (change.hiddenBackground) tile.hidden = true;
    area.append(tile);
    continue;
  }
  const attrs = {class: 'image-item'};
  if (!change.noStatus) attrs['data-upload-status'] = 'success';
  const tile = new Element('div', attrs);
  const image = new Element('img', {alt: change.noFilename ? '' : item.name, src: `https://images.test/${item.id}.jpg`});
  if (change.brokenImage) image.naturalWidth = 0;
  if (!change.noFilename) tile.append(new Element('span', {}, item.name));
  tile.append(image);area.append(tile);
}
if (counterTiles) {
  // Same asset may appear in cover/phone preview. It must not count again.
  for (const label of ['封面预览', '手机预览', '用户头像']) {
    area.append(new Element('div', {'aria-label': label}).append(new Element('div', {backgroundImage: 'url("https://images.test/upload-image-1.jpg?signature=other")'}), new Element('img', {src: 'https://images.test/upload-image-1.jpg?signature=other'})));
  }
  area.append(new Element('svg', {backgroundImage: 'url("data:image/svg+xml,decorative-icon")'}));
  if (change.decorationBackground) area.append(new Element('div', {backgroundImage: 'url("https://images.test/decoration.jpg")'}));
}
if (change.uploadBusy) area.append(new Element('div', {'role': 'progressbar'}));
if (change.extraAvatarInArea) area.append(new Element('div', {class: 'avatar'}).append(new Element('img', {alt: 'avatar'})));
const title = new Element('input', {placeholder: '添加作品标题'});title.maxLength = change.titleMax ?? 80;
title.value = change.emptyTitle ? '' : request.title_text;form.append(title);
const editor = new Element('div', {contenteditable: 'true', 'data-placeholder': '添加作品描述'}, change.emptyBody ? '' : request.body_text);
if (change.bodyMax) editor.attrs['data-maxlength'] = String(change.bodyMax);
form.append(editor);
if (change.secondEditor) form.append(new Element('div', {contenteditable: 'true'}, '另一编辑器'));
const declaration = new Element('select', {'aria-label': '内容声明'});
declaration.append(new Element('option', {value: ''}, '不添加声明'), new Element('option', {value: 'ai'}, '内容由AI生成'));
declaration.value = change.aiDeclaration ? 'ai' : '';if (!change.noDeclaration) form.append(declaration);
const immediate = new Element('input', {type: 'radio'}), scheduled = new Element('input', {type: 'radio'});
immediate.checked = !request.scheduled_time_text || change.immediateMode;
scheduled.checked = !!request.scheduled_time_text && !change.immediateMode;
form.append(new Element('label').append(immediate, new Element('span', {}, '立即发布')));
form.append(new Element('label').append(scheduled, new Element('span', {}, '定时发布')));
const date = new Element('input', {type: change.datetimeLocal ? 'datetime-local' : 'text', placeholder: '日期和时间'});
date.value = change.wrongTime || request.scheduled_time_text;
if (change.datetimeLocal) date.value = date.value.replace(' ', 'T');
form.append(date);
const submit = new Element('button', {}, '发布');submit.disabled = !!change.disabled;form.append(submit);
if (change.duplicatePublish) form.append(new Element('button', {}, '发布'));
if (change.dialog) body.append(new Element('div', {role: 'dialog'}, '请同意原创协议'));
if (change.confirmDialog) body.append(new Element('div', {role: 'dialog'}, change.confirmTerms ? '确认发布并同意原创协议' : '确认发布作品').append(new Element('button', {}, '确认发布')));
global.document = {
  body, activeElement: null,
  querySelectorAll(selector) {return selector === 'body *' ? body.descendants() : body.querySelectorAll(selector);},
  createRange() {return {selectNodeContents(node) {this.node = node;}};},
  execCommand(command, ui, value) {
    if (command !== 'insertText' || !this.activeElement || change.inputFailure) return false;
    if (this.activeElement.tagName === 'INPUT') this.activeElement.value = value;else this.activeElement.innerText = value;
    return true;
  },
};
global.window = {getSelection() {return {removeAllRanges() {}, addRange() {}};}, p011UploadCompleted: true};
global.getComputedStyle = node => ({display: node.hidden ? 'none' : 'block', visibility: node.hidden ? 'hidden' : 'visible', backgroundImage: node.attrs.backgroundImage || 'none'});
global.HTMLInputElement = Element;global.HTMLSelectElement = Element;
global.KeyboardEvent = class KeyboardEvent extends Event {constructor(type, options) {super(type, options);this.key = options.key;this.code = options.code;}};
global.location = {origin: 'https://creator.douyin.com', pathname: change.path || '/creator-micro/content/publish', href: 'https://creator.douyin.com' + (change.path || '/creator-micro/content/publish') + (change.query || '?itemId=fixture')};
if (change.management) {
  location.pathname = '/creator-micro/content/manage';location.href = location.origin + location.pathname;
  const search = new Element('input', {placeholder: '搜索作品'});search.value = change.searchMissingValue ? '' : request.title_text;body.append(search);
  const rows = change.rows || [{type: 'image', time: (request.scheduled_time_text || '2026-10-14 12:00').replace(/^(\d{4})-(\d{2})-(\d{2}) /, '$1年$2月$3日 '), status: request.scheduled_time_text ? '定时发布中' : '审核中', id: 'fixture-item-1'}];
  for (const spec of rows) {
    if (change.actualImageCard) {
      const caption = request.title_text + '。' + request.plain_body_text + (request.topic_names || []).map(name => '#' + name).join('');
      const timestamp = Math.floor(new Date((request.scheduled_time_text || '2026-10-15 12:00').replace(' ', 'T') + ':00+08:00').getTime() / 1000);
      const itemId = '7696358409028930826';
      const data = {
        aweme_id: itemId, item_id: Number(itemId), author_user_id: Number(request.expected_uid), author: {uid: request.expected_uid},
        is_pic_word: true, aweme_type: 2, desc: caption,
        images: (request.image_upload_ids || []).map(uri => ({uri: uri.replace(/^tos-cn-i-jm8ajry58r\//, 'tos-cn-i-dy/')})),
        timer: {public_time: timestamp, status: 0}, status_value: 140, create_time: timestamp,
        status: {aweme_id: itemId, in_reviewing: false, is_delete: false, is_prohibited: false},
        ...(spec.metadata || {}),
      };
      const row = new Element('div', {class: 'video-card-s-unhashed-fixture'});
      const content = new Element('div', {class: 'video-card-content'});
      content.append(new Element('span', {}, spec.text || caption));
      row.append(content, new Element('div', {class: 'video-card-info'}, (spec.status || '定时发布中') + ' ' + (spec.time || (request.scheduled_time_text || '').replace(/^(\d{4})-(\d{2})-(\d{2}) /, '$1年$2月$3日 ')) + ' 3张'));
      let fiber = {memoizedProps: {data}};
      for (let depth = 0; depth < (spec.fiberDepth ?? 1); depth++) fiber = {return: fiber};
      if (!spec.noMetadata) row['__reactFiber$fixture'] = fiber;
      if (spec.hidden) row.hidden = true;
      body.append(row);
      continue;
    }
    const row = new Element('div', {class: 'content-item', 'data-content-type': spec.type || 'image', 'data-item-id': spec.id || ''});
    row.append(new Element('span', {}, spec.text || `${request.title_text} ${request.body_text}`));
    row.append(new Element('span', {}, spec.type === 'video' ? '视频' : '图文'));
    row.append(new Element('span', {}, spec.time || ''), new Element('span', {}, spec.status || ''), new Element('button', {}, '删除作品'));
    body.append(row);
  }
}
global.fetch = async path => ({ok: true, async json() {return path === '/aweme/v1/creator/user/info/' ? {status_code: 0, user_profile: {nick_name: '测试账号'}} : {uid: change.wrongIdentity ? '456' : '123', status_code: 0};}});
global.AbortSignal = {timeout() {return null;}};
async function main() {
  const run = new (Object.getPrototypeOf(async function(){}).constructor)(...Object.keys(request), payload.script);
  let result;
  for (let turn = 0; turn < (payload.turns || 1); turn++) result = await run(...Object.values(request));
  process.stdout.write(JSON.stringify({result, submitClicks, events, title: title.value, body: editor.innerText, date: date.value, scheduled: scheduled.checked}));
}
main().catch(error => {console.error(error.stack);process.exitCode = 1;});
