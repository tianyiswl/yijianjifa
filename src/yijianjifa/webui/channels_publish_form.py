"""Video-channel creator form contract from official 2026-10-07 public assets.

This module reads connected DOM/component state. It never calls a platform
service, constructs an upload, changes Vue state, or replays private requests.
Signed media URLs stay in the live adapter and are never receipt identifiers.
"""
from datetime import datetime, timedelta, timezone
import math
import re
from xml.etree import ElementTree

from yijianjifa.app_services import channels_declaration_catalog as declarations

HOST = 'channels.weixin.qq.com'
UPLOAD_URL = 'https://channels.weixin.qq.com/platform/post/create'
MANAGE_URL = 'https://channels.weixin.qq.com/platform/post/list'
CREATE_PATH = '/cgi-bin/mmfinderassistant-bin/post/post_create'
LIST_PATH = '/cgi-bin/mmfinderassistant-bin/post/post_list'
# The content micro-app's official PostService uses its own base prefix
# (97.98a20fe5.js module 41951; 425.db8efacb.js module 86248).
# Keep the original helper routes for restored/older sessions. Only these
# exact same-origin endpoints are observed; no requests are constructed.
CREATE_PATHS = frozenset({CREATE_PATH, '/micro/content' + CREATE_PATH})
LIST_PATHS = frozenset({LIST_PATH, '/micro/content' + LIST_PATH})
# Official 841.d575b04c.js module c45b exports image=2 and video=4.
# New video state, uploaded media and postObjDesc all use that same enum.
VIDEO_MEDIA_TYPE = 4
EDITOR = '.post-create .post-desc-box .input-editor[contenteditable]'
FILE_INPUT = '.post-create .upload-wrap input[type="file"]'
SHORT_TITLE = '.post-create .post-short-title-wrap input'
FINAL_CONTROL = '.post-create .form-btns button'
DATE_INPUT = '.post-create .post-time-wrap input[placeholder="请选择发表时间"]'
ORIGINAL_INPUT = '.post-create .declare-original-checkbox input[type="checkbox"]'
LOCATION_SELECT = '.post-create .post-position-wrap .position-display-wrap'
LOCATION_OPTION = '.post-create .post-position-wrap .location-filter-wrap .location-item .name'
DECLARATION_SELECT = '.post-create .post-with-mark-tag .mark-tag-select'
DECLARATION_OPTION = '.post-create .post-with-mark-tag .mark-tag-option .option-main'


def clean_text(value):
    # The official editor keeps zero-width cursor sentinels. Line endings are
    # equivalent; ordinary whitespace/newlines are content and stay significant.
    return value.replace('\u200b', '').replace('\r\n', '\n') if type(value) is str else None


def caption_matches(actual, expected):
    # The contenteditable renderer may append empty terminal lines. Preserve
    # every internal space/newline and all other text in all three readbacks.
    actual, expected = clean_text(actual), clean_text(expected)
    return type(actual) is str and type(expected) is str and actual.strip(' \t\n') == expected.strip(' \t\n')


def utf16_length(value):
    return len(value.encode('utf-16-le')) // 2


def display_width(value):
    # Official helper: str.replace(/[^\x00-\xff]/g, "aa").length, on JS UTF16.
    units = value.encode('utf-16-le')
    return sum(1 if int.from_bytes(units[i:i+2], 'little') <= 255 else 2
               for i in range(0, len(units), 2))


def normalize_short_title(value):
    # transformPostPayload in 772.4b4c8d62.js intentionally replaces the FIRST
    # ASCII colon/question mark, and all paired ASCII double quotes.
    return re.sub(r'"([^"]*)"', r'“\1”', value.replace(':', '：', 1).replace('?', '？', 1))


def valid_short_title(value):
    return type(value) is str and (not value or (6 <= utf16_length(value) <= 16 and re.fullmatch(
        r'[\u2103\u4E00-\u9FA5A-Za-z0-9《》"“”:：+?？%\s]+', value) is not None))


def validate_form(payload, *, query=False, now=None):
    fields = payload['fields']
    if type(fields) is not dict or set(fields) - {'title', 'body', 'tags', 'cover', 'location', 'declaration', 'channels_short_title', 'content_declaration_choice', 'channels_original'}:
        raise ValueError('unsupported video-channel fields')
    if fields.get('location') is not None or fields.get('declaration') is not None:
        raise ValueError('channels location/declaration not verified')
    declaration = declarations.normalize_choice(fields.get('content_declaration_choice', 'none'))
    original = fields.get('channels_original', False)
    if type(original) is not bool:
        raise ValueError('channels original declaration must be boolean')
    cover = fields.get('cover') or {'kind': 'first_frame', 'frame_ms': 0}
    if cover.get('kind') != 'first_frame' or cover.get('frame_ms', 0) != 0:
        raise ValueError('channels custom cover not verified')
    title, body, tags = (fields.get(k, '') for k in ('title', 'body', 'tags'))
    short = fields.get('channels_short_title', '')
    if any(type(v) is not str for v in (title, body, tags, short)) or not title.strip():
        raise ValueError('invalid channels caption')
    if '\u200b' in title + body + tags + short:
        raise ValueError('unsupported editor sentinel')
    if not valid_short_title(short):
        raise ValueError('channels short title must contain 6 to 16 supported characters')
    names = [v.lstrip('#') for v in tags.split()]
    if (len(names) > 20 or len(names) != len(set(names))
            or any(not v or re.search(r'[\s#@,，.。<]', v) or utf16_length(v) > 100 for v in names)):
        raise ValueError('invalid channels topics')
    text = title + ('\n' + body if body else '')
    # Inline tags inside the title/body would become actual topics/mentions in
    # this editor. Do not silently invent them or drop their semantic payload.
    if re.search(r'[#@][^\n\s\t#@,，.。<]+', text):
        raise ValueError('channels inline topics must use the topic field')
    caption = text + (' ' + ' '.join('#' + v for v in names) if names else '')
    if display_width(caption) > 2000:
        raise ValueError('channels caption exceeds the official weighted 1000-character limit')
    mode = payload['execution_mode']
    if mode not in {'publish_now', 'software_schedule', 'platform_schedule'}:
        raise ValueError('execution mode not verified')
    when = None
    if mode == 'platform_schedule':
        when = datetime.fromisoformat(payload['scheduled_at'])
        if when.tzinfo is None or when.second or when.microsecond:
            raise ValueError('schedule requires a timezone and whole minute')
        current = now or datetime.now(timezone.utc)
        # The public widget caps at one calendar month. A conservative 28-day
        # window fits every month; the application's existing 2.5h buffer stays.
        if not query and not current + timedelta(hours=2.5) <= when <= current + timedelta(days=28):
            raise ValueError('channels schedule outside supported window')
    local = when.astimezone(timezone(timedelta(hours=8))) if when else None
    return {'text': text, 'tags': names, 'caption': caption, 'short_title': short,
            'content_declaration_choice': declaration, 'content_declaration_name': declarations.name_for(declaration),
            'channels_original': original,
            'submitted_short_title': normalize_short_title(short),
            'effective_time': int(when.timestamp()) if when else 0,
            'time_text': local.strftime('%Y-%m-%d %H:%M') if local else '',
            'mode': '定时' if when else '不定时'}


# Wujie renders its content microapp into an open shadow tree. Playwright's
# locators pierce that tree, while a native document.querySelector does not.
# Read only the live connected form; never invoke getPostPayload/validPost.
READ_FORM = r'''() => {
 const roots=[document], seen=new Set();
 for(let i=0;i<roots.length && roots.length<=40;i++) {
   for(const e of roots[i].querySelectorAll('*')) if(e.shadowRoot&&!seen.has(e.shadowRoot)) {
     seen.add(e.shadowRoot); roots.push(e.shadowRoot);
   }
 }
 const all=q=>roots.flatMap(r=>[...r.querySelectorAll(q)]);
 const visible=e=>{
   // Vue's v-if/component roots may be connected Comment or Text nodes. Only
   // rendered Elements have geometry; nodeType works across Wujie's realms.
   if(e?.nodeType!==1||typeof e.getClientRects!=='function'||!e.isConnected||
      !Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
   for(let node=e;node;node=node.parentElement||node.getRootNode()?.host){
     const style=getComputedStyle(node);
     if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse'||Number(style.opacity)<=0)return false;
   }
   return true;
 };
 const editors=all('.post-create .post-desc-box .input-editor[contenteditable]').filter(visible);
 const buttons=all('.post-create .form-btns button').filter(e=>visible(e)&&e.innerText.trim()==='发表');
 const dates=all('.post-create .post-time-wrap input[placeholder="请选择发表时间"]').filter(visible);
 const shorts=all('.post-create .post-short-title-wrap input').filter(visible);
 // Ant's native checkbox input is intentionally transparent. The ordinary
 // checkbox wrapper must be rendered; its connected input supplies checked.
 const originalControls=all('.post-create .declare-original-checkbox').filter(visible).flatMap(
   e=>[...e.querySelectorAll('input[type="checkbox"]')].filter(v=>v.isConnected));
 const declarationSelections=all('.post-create .post-with-mark-tag .select-value').filter(visible);
 const originalDialogs=all('.declare-original-dialog').filter(visible);
 const tagComponents=all('.post-create .post-with-mark-tag').filter(visible).map(e=>e.__vue__).filter(
   v=>v&&visible(v.$el)&&Array.isArray(v.markTagOptions));
 const tagVm=tagComponents.length===1?tagComponents[0]:null;
 const candidates=[], visited=new Set(), queue=all('.post-create,.post-create-wrap').map(e=>e.__vue__).filter(Boolean);
 while(queue.length&&visited.size<600) {
   const vm=queue.shift(); if(!vm||visited.has(vm))continue;visited.add(vm);
   const inner=vm.$refs?.postCreate;
   // The current native wrapper no longer exposes the unused postState field.
   // Its live, contained editor ref owns the same combinedStore records below.
   if(visible(vm.$el)&&inner?.combinedStore&&visible(inner.$el)&&
      (vm.$el===inner.$el||vm.$el.contains(inner.$el))) candidates.push(vm);
   if(Array.isArray(vm.$children))queue.push(...vm.$children);
 }
 const vm=candidates.length===1?candidates[0]:null, inner=vm?.$refs?.postCreate, state=inner?.combinedStore;
 const desc=state?.postObjDesc, temp=state?.tempPost, media=desc?.media;
 // A mounted component can expose its store before both records initialize.
 // Accept plain records from the microapp's realm too, without inventing state.
 const record=v=>{
   if(v===null||typeof v!=='object'||Object.prototype.toString.call(v)!=='[object Object]')return false;
   const proto=Object.getPrototypeOf(v);
   return proto===null||Object.getPrototypeOf(proto)===null;
 };
 const stateReady=record(state)&&record(desc)&&record(temp);
 const bounded=v=>typeof v==='string'&&v.length<=12000?v:null;
 const tagInfo=v=>{
   if(v==null)return null;
   if(typeof v!=='object'||Array.isArray(v)||Object.keys(v).length!==2||
      !Number.isSafeInteger(v.tagType)||v.tagType<=0||typeof v.tagKey!=='string'||!v.tagKey||v.tagKey.length>4096)return false;
   return {tagType:v.tagType,tagKey:v.tagKey};
 };
 const m=Array.isArray(media)&&media.length===1?media[0]:null;
 const own=m?Object.fromEntries(['url','md5sum','fileSize','width','height','videoPlayLen','thumbUrl','coverUrl','fullThumbUrl','fullCoverUrl','shareCoverUrl','cardShowStyle','mediaType','urlCdnTaskId'].filter(k=>m[k]!==undefined).map(k=>[k,typeof m[k]==='string'?bounded(m[k]):m[k]])):null;
 const noLinks=!vm?.componentId&&!vm?.member?.memberLevel&&!vm?.isForVip&&!vm?.isForLesson&&!vm?.isForDrama&&!vm?.bgmData&&!(vm?.extReading?.link);
 const noExtras=stateReady&&noLinks&&(!desc.location||Object.keys(desc.location).length===0)&&!(desc.topic?.collectionId)&&(!desc.event||Object.keys(desc.event).length===0)&&(!desc.mentionedUser||desc.mentionedUser.length===0)&&![temp.longitude,temp.latitude,temp.feedLongitude,temp.feedLatitude,temp.originalFlag].some(v=>v!==undefined&&v!==0);
 // Diagnostic booleans only: these do not replace or loosen noExtras.
 const extrasChecks={no_component:!vm?.componentId,no_member:!vm?.member?.memberLevel,
   ordinary_mode:!vm?.isForVip&&!vm?.isForLesson&&!vm?.isForDrama,no_bgm:!vm?.bgmData,
   no_external_link:!(vm?.extReading?.link),empty_location:!desc?.location||Object.keys(desc.location).length===0,
   no_collection:!(desc?.topic?.collectionId),empty_event:!desc?.event||Object.keys(desc.event).length===0,
   no_mentions:!desc?.mentionedUser||desc.mentionedUser.length===0,
   neutral_longitude:temp?.longitude===undefined||temp.longitude===0,
   neutral_latitude:temp?.latitude===undefined||temp.latitude===0,
   neutral_feed_longitude:temp?.feedLongitude===undefined||temp.feedLongitude===0,
   neutral_feed_latitude:temp?.feedLatitude===undefined||temp.feedLatitude===0,
   neutral_original_flag:temp?.originalFlag===undefined||temp.originalFlag===0};
 const clipTicket=state?.createPostStatus?.clipTicket?.draftId,mediaTask=m?.urlCdnTaskId;
 const validClipId=v=>typeof v==='string'&&v.length>0&&v.length<=12000;
 // Native MaterialEdit projects processedResponse into media. Match the
 // same connected upload record; do not infer ownership from URL alone.
 const files=new Set();
 for(const component of visited) if(visible(component.$el)&&Array.isArray(component.fileList))
   for(const file of component.fileList) if(file?.uuid===m?.md5sum)files.add(file);
 const processedFromUpload=files.size===1&&[...files].some(file=>{
   const p=file.processedResponse;
   return p&&p.url===m?.url&&p.width===m?.width&&p.height===m?.height&&p.duration===m?.videoPlayLen&&
     (p.fileSize??null)===(m?.fileSize??null);
 });
 const clipBindings={processed_from_upload:processedFromUpload,async_processing:state?.asyncClip===true,
   ticket_present:validClipId(clipTicket),media_task_present:validClipId(mediaTask),
   record_ticket_matches:validClipId(clipTicket)&&validClipId(mediaTask)&&clipTicket===mediaTask};
 return {editor_count:editors.length,caption:editors.length===1?editors[0].innerText:'',
   tags:editors.length===1?[...editors[0].querySelectorAll('span.topic[data-type="topic"]')].map(e=>e.innerText.replace(/#/g,'').trim()):[],
   state_caption:bounded(desc?.description),state_tags:Array.isArray(temp?.topics)?[...temp.topics]:null,
   short_title:shorts.length===1?shorts[0].value:'',short_count:shorts.length,
   state_short_title:desc?.shortTitle?.[0]?.shortTitle||'',
   effective_time:inner?.originState?.effectiveTime||0,time_text:dates.length===1?dates[0].value:'',
   state_ready:stateReady,upload_complete:stateReady&&state.canPost===true&&!!m&&!!m.url&&!!m.thumbUrl,
   media:own,media_count:Array.isArray(media)?media.length:-1,form_count:candidates.length,
   finder_uid:bounded(inner?.finder?.finderUsername),no_extras:noExtras,
   extras_checks:extrasChecks,clip_bindings:clipBindings,
   clip_id:bounded(state?.createPostStatus?.clipTicket?.draftId)||'',
   draft_id:bounded(vm?.draftStore?.objectId)||'',
   original_intercept:!!vm?.visibleOriginalIntercept,
   state_original:typeof vm?.checkOriginalFlag==='boolean'?vm.checkOriginalFlag:null,
   state_original_auth:typeof vm?.allowOriginalAuth==='boolean'?vm.allowOriginalAuth:null,
   original_flag:temp?.originalFlag,
   original_control_count:originalControls.length,
   original_checked:originalControls.length===1?originalControls[0].checked:null,
   original_dialog:originalDialogs.length>0,
   declaration_selected_count:declarationSelections.length,
   declaration_name:declarationSelections.length===1?declarationSelections[0].innerText:'',
   declaration_component_count:tagComponents.length,
   declaration_state_name:bounded(tagVm?.selectedTag?.tagName)||'',
   declaration_state_type:tagVm?.selectedTag?.tagType,
   declaration_tag_info:tagInfo(vm?.tagInfo),
   declaration_component_tag_info:tagInfo(tagVm?.currentTagInfo),
   declaration_tag_key:typeof tagVm?.tagSecKey==='string'&&tagVm.tagSecKey.length<=4096?tagVm.tagSecKey:null,
   declaration_options_loading:tagVm?.optionsLoading,
   declaration_options:tagVm&&tagVm.markTagOptions.length<=100?tagVm.markTagOptions.map(v=>({
     name:typeof v?.tagName==='string'&&v.tagName.length<=255?v.tagName:null,
     tag_type:Number.isSafeInteger(v?.tagType)&&v.tagType>0?v.tagType:null})):null,
   button_count:buttons.length,button_enabled:buttons.length===1&&!buttons[0].disabled&&!buttons[0].classList.contains('weui-desktop-btn_disabled')};
}'''


def owned_media(value):
    if type(value) is not dict:
        return None
    result = dict(value)
    if any(type(result.get(k)) is not str or not result[k] or len(result[k]) > 12000
           for k in ('url', 'md5sum', 'thumbUrl')):
        return None
    if not result['url'].startswith('https://') or not result['thumbUrl'].startswith('https://'):
        return None
    for key in ('fileSize', 'width', 'height', 'videoPlayLen'):
        # The native processed response may omit fileSize. Only a native
        # ticket-bearing video can represent that; adapter provenance remains
        # separately required before freezing a processed record.
        if key == 'fileSize' and result.get(key) is None and type(result.get('urlCdnTaskId')) is str and result['urlCdnTaskId']:
            continue
        v = result.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            return None
    if type(result.get('mediaType')) is not int or result['mediaType'] != VIDEO_MEDIA_TYPE:
        return None
    return result


def refresh_processed_media(raw, frozen):
    """Allow the native result for this upload, never a replacement asset."""
    actual = owned_media(raw.get('media'))
    if actual is None or type(frozen) is not dict:
        return None
    ticket = frozen.get('urlCdnTaskId')
    proof = raw.get('clip_bindings', {})
    if (type(ticket) is not str or not ticket or raw.get('clip_id') != ticket
            or actual.get('urlCdnTaskId') != ticket
            or proof.get('record_ticket_matches') is not True
            or proof.get('processed_from_upload') is not True):
        return None
    # All other upload/cover/geometry fields must be exactly the original.
    before = {k:v for k,v in frozen.items() if k not in {'url','fileSize'}}
    after = {k:v for k,v in actual.items() if k not in {'url','fileSize'}}
    if before != after or any(type(after[k]) is not type(v) for k,v in before.items()):
        return None
    return actual


def form_verification_checks(raw, form, media=None, expected_uid=None, declaration=None):
    """The exact final gates as named booleans; never return observed content."""
    if type(raw) is not dict:
        return {'form_record': False}
    actual_media = owned_media(raw.get('media'))
    return {
        'single_editor': raw.get('editor_count') == 1,
        'single_form': raw.get('form_count') == 1,
        'single_media': raw.get('media_count') == 1,
        'upload_complete': raw.get('upload_complete') is True,
        'no_extras': raw.get('no_extras') is True,
        'no_draft': not raw.get('draft_id'),
        'no_clip': not raw.get('clip_id') or (type(media) is dict and actual_media==media and type(media.get('urlCdnTaskId')) is str and bool(media['urlCdnTaskId']) and raw['clip_id']==media['urlCdnTaskId'] and raw.get('clip_bindings',{}).get('record_ticket_matches') is True),
        'no_original_intercept': not raw.get('original_intercept'),
        'declarations_match': bool(declarations_match(raw, form, declaration)),
        'caption_matches': caption_matches(raw.get('caption'), form['caption']),
        'state_caption_matches': caption_matches(raw.get('state_caption'), form['caption']),
        'tags_match': raw.get('tags') == form['tags'],
        'state_tags_match': raw.get('state_tags') == form['tags'],
        'short_title_matches': raw.get('short_title', '') == form['short_title'],
        'state_short_title_matches': raw.get('state_short_title', '') == form['short_title'],
        'short_title_control': bool(not form['short_title'] or raw.get('short_count') == 1),
        'effective_time_matches': type(raw.get('effective_time')) is int and raw['effective_time'] == form['effective_time'],
        'time_text_matches': raw.get('time_text', '') == form['time_text'],
        'owned_media': actual_media is not None,
        'frozen_media_matches': media is None or actual_media == media,
        'finder_uid_matches': expected_uid is None or raw.get('finder_uid') == expected_uid,
        'single_final_control': raw.get('button_count') == 1,
        'final_control_enabled': raw.get('button_enabled') is True,
    }


def verify_form(raw, form, media=None, expected_uid=None, declaration=None):
    return all(form_verification_checks(raw, form, media, expected_uid, declaration).values())


def declarations_match(raw, form, declaration=None):
    """Require the connected checkbox and parent state to agree with intent."""
    original = form['channels_original']
    count = raw.get('original_control_count')
    if (type(raw.get('state_original')) is not bool or raw['state_original'] is not original
            or raw.get('state_original_auth') is not True
            or type(raw.get('original_flag')) is not int or raw['original_flag'] != 0
            or raw.get('original_dialog') is not False
            or type(count) is not int or count not in {0, 1}
            or not content_declaration_matches(raw, form, declaration)):
        return False
    # This account may not expose original rights. Absence is valid only when
    # no original declaration was requested and the parent state is false.
    return (count == 1 and raw.get('original_checked') is original
            or count == 0 and not original and raw.get('original_checked') is None)


def safe_tag_info(value):
    if (type(value) is not dict or set(value) != {'tagType', 'tagKey'}
            or type(value['tagType']) is not int or not 0 < value['tagType'] <= 9007199254740991
            or type(value['tagKey']) is not str or not 1 <= len(value['tagKey']) <= 4096):
        return None
    return dict(value)


def content_declaration_matches(raw, form, declaration=None):
    count = raw.get('declaration_selected_count')
    if type(count) is not int:
        return False
    if form['content_declaration_choice'] == 'none':
        return (count == 0 and not raw.get('declaration_name') and not raw.get('declaration_state_name')
                and raw.get('declaration_tag_info') is None
                and raw.get('declaration_component_tag_info') is None and declaration is None)
    info = safe_tag_info(raw.get('declaration_tag_info'))
    component = safe_tag_info(raw.get('declaration_component_tag_info'))
    rows = raw.get('declaration_options')
    matches = [row for row in rows if type(row) is dict and row.get('name') == form['content_declaration_name']] if type(rows) is list else []
    return (count == 1 and type(raw.get('declaration_component_count')) is int and raw['declaration_component_count'] == 1
            and raw.get('declaration_name') == raw.get('declaration_state_name') == form['content_declaration_name']
            and info is not None and info['tagType'] not in {5, 7} and component == info
            and type(raw.get('declaration_state_type')) is int and raw['declaration_state_type'] == info['tagType']
            and len(matches) == 1 and type(matches[0].get('tag_type')) is int and matches[0]['tag_type'] == info['tagType']
            and raw.get('declaration_tag_key') == info['tagKey']
            and (declaration is None or safe_tag_info(declaration) == info))


def safe_id(value):
    # IDs above JS safe integer must arrive as strings, preserving all digits.
    if type(value) is int and 0 < value <= 9007199254740991:
        value = str(value)
    return value if type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) else None


def receipt_reference(data):
    value = safe_id(data.get('objectId')) if type(data) is dict else None
    return {'work_id': value} if value else {}


def successful_response(data):
    return (type(data) is dict and type(data.get('errCode')) is int and data['errCode'] == 0
            and type(data.get('data')) is dict and type(data['data'].get('baseResp')) is dict
            and type(data['data']['baseResp'].get('errcode')) is int
            and data['data']['baseResp']['errcode'] == 0)


def _empty(value):
    return value is None or value == '' or value == {} or value == []


def media_matches(actual, frozen):
    if type(actual) is not dict or owned_media(frozen) is None:
        return False
    for key, value in frozen.items():
        found = actual.get(key)
        if key in {'fileSize', 'width', 'height', 'videoPlayLen'} and type(value) in (int, float):
            # JS has one Number type; JSON.stringify(3.0) produces 3. Keep
            # exact values, rejecting booleans, strings and nonfinite numbers.
            if type(found) not in (int, float) or not math.isfinite(found) or found != value:
                return False
        elif type(found) is not type(value) or found != value:
            return False
    return True


def request_matches(request, form, media, declaration=None):
    """Correlate one normal page final request to its frozen form and upload."""
    if type(request) is not dict or owned_media(media) is None:
        return False
    if any(type(request.get(k)) is not int or request[k] != v for k, v in
           {'objectType': 0, 'isFullPost': 1, 'handleFlag': 2, 'mode': 1}.items()):
        return False
    desc = request.get('objectDesc')
    if (type(desc) is not dict or type(desc.get('mediaType')) is not int
            or desc['mediaType'] != VIDEO_MEDIA_TYPE
            or type(desc.get('media')) is not list or len(desc['media']) != 1):
        return False
    actual = desc['media'][0]
    if not media_matches(actual, media):
        return False
    if not caption_matches(desc.get('description'), form['caption']) or request.get('topics', []) != form['tags']:
        return False
    titles = desc.get('shortTitle', [])
    if titles not in ([], [{'shortTitle': ''}]) and titles != [{'shortTitle': form['submitted_short_title']}]:
        return False
    if form['short_title'] and titles != [{'shortTitle': form['submitted_short_title']}]:
        return False
    effective = request.get('effectiveTime', 0)
    if effective is None and not form['effective_time']:
        effective = 0
    if type(effective) is not int or effective != form['effective_time']:
        return False
    clip=request.get('videoClipTaskId')
    if not _empty(clip) and (type(clip) is not str or clip!=media.get('urlCdnTaskId')):
        return False
    if any(not _empty(request.get(k)) for k in ('draftExportId', 'originalType')):
        return False
    if form['content_declaration_choice'] == 'none':
        if not _empty(request.get('tagInfo')) or declaration is not None:
            return False
    elif (safe_tag_info(declaration) is None or safe_tag_info(request.get('tagInfo')) != declaration):
        return False
    if any(request.get(k, 0) != 0 or type(request.get(k, 0)) is not int for k in
           ('longitude', 'latitude', 'feedLongitude', 'feedLatitude', 'originalFlag')):
        return False
    # 16 only records declining an automatically recommended collection.
    original_bit = 1 if form['channels_original'] else 0
    if type(request.get('postFlag', 0)) is not int or request.get('postFlag', 0) not in {original_bit, original_bit | 16}:
        return False
    if any(not _empty(desc.get(k)) for k in ('location', 'component', 'member', 'event', 'mentionedUser')):
        return False
    ext = desc.get('extReading', {})
    if type(ext) is not dict or any(not _empty(v) for v in ext.values()):
        return False
    follow = desc.get('followPostInfo', {})
    if type(follow) is not dict or follow not in ({}, {'hasBgm': 0}):
        return False
    topic = desc.get('topic', {})
    if type(topic) is not dict or set(topic) - {'finderTopicInfo'}:
        return False
    xml = topic.get('finderTopicInfo', '')
    if type(xml) is not str or len(xml) > 32000 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        return False
    if not xml:
        return not form['tags']
    try:
        root = ElementTree.fromstring(xml)
        if root.tag != 'finder':
            return False
        topics = [n.text for n in root.iter('topic')]
        return topics == ['#' + n + '#' for n in form['tags']]
    except ElementTree.ParseError:
        return False


def verified_row(rows, reference, form):
    """A work ID plus exact contents/time proves this target's list readback."""
    if not reference:
        return None
    matches = []
    for row in rows:
        if type(row) is not dict or receipt_reference(row) != reference:
            continue
        desc = row.get('desc')
        if type(desc) is not dict or not caption_matches(desc.get('description'), form['caption']):
            continue
        effective = row.get('effectiveTime', 0)
        if type(effective) is not int or effective != form['effective_time']:
            continue
        status = row.get('handleStatus')
        if status is not None and type(status) is not int:
            continue
        if status in {3, 4, 5}:
            state = 'platform_rejected'
        elif status == 1:
            state = 'processing'
        elif status == 2 or status is None:
            # The helper's "already posted" flag is not proof of public
            # visibility. This contract conservatively reports acceptance.
            state = 'platform_scheduled' if effective > 0 else 'platform_accepted'
        else:
            continue
        matches.append({'state': state, 'reference': reference})
    return matches[0] if len(matches) == 1 else None
