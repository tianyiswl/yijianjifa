"""Semantic contract checks; no live account or platform IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from yijianjifa.webui import channels_publish_form as form


@pytest.fixture
def payload():
    return {'platform': 'channels', 'fields': {'title': '测试视频标题', 'body': '正文内容。',
        'tags': '#功能测试 #剪辑测试', 'channels_short_title': '视频号功能测试',
        'cover': {'kind': 'first_frame', 'frame_ms': 0}}, 'execution_mode': 'platform_schedule',
        'scheduled_at': (datetime.now(timezone.utc) + timedelta(days=1)).replace(second=0, microsecond=0).isoformat()}


@pytest.fixture
def media():
    return {'url': 'https://example.invalid/owned.mp4', 'md5sum': 'upload-uuid', 'fileSize': 15360,
        'width': 1920, 'height': 1080, 'videoPlayLen': 3.0, 'thumbUrl': 'https://example.invalid/cover', 'mediaType': 4}


def page_request(f, media):
    # Shape emitted by the public creator getCreateOrPreviewParams/transform.
    return {'objectType': 0, 'topics': f['tags'], 'postFlag': 0, 'originalFlag': 0,
        'longitude': 0, 'latitude': 0, 'feedLongitude': 0, 'feedLatitude': 0,
        'isFullPost': 1, 'handleFlag': 2, 'mode': 1, 'effectiveTime': f['effective_time'],
        'objectDesc': {'description': f['caption'], 'mediaType': 4, 'media': [deepcopy(media)],
            'shortTitle': [{'shortTitle': f['submitted_short_title']}], 'location': {}, 'event': {},
            'extReading': {}, 'member': {}, 'mentionedUser': [],
            'topic': {'finderTopicInfo': '<finder><version>1</version>' + ''.join(
                '<value0><topic><![CDATA[#' + t + '#]]></topic></value0>' for t in f['tags']) + '</finder>'}}}


def live_form(f, media):
    return {'editor_count': 1, 'form_count': 1, 'media_count': 1, 'upload_complete': True,
        'caption': f['caption'], 'state_caption': f['caption'], 'tags': f['tags'], 'state_tags': f['tags'],
        'short_title': f['short_title'], 'state_short_title': f['short_title'], 'short_count': 1,
        'effective_time': f['effective_time'], 'time_text': f['time_text'], 'media': deepcopy(media),
        'finder_uid': 'finder-test', 'extras_checks': {'empty_location': True}, 'no_extras': True, 'draft_id': '', 'original_intercept': False,
        'state_original': f['channels_original'], 'state_original_auth': True, 'original_flag': 0,
        'original_control_count': 1, 'original_checked': f['channels_original'], 'original_dialog': False,
        'declaration_selected_count': 0, 'declaration_tag_info': None,
        'button_count': 1, 'button_enabled': True}


@pytest.mark.parametrize('value', [2, True, 4.0, '4', None, 1, 8])
def test_uploaded_video_requires_native_video_enum_with_integer_type(media, value):
    media['mediaType'] = value
    assert form.owned_media(media) is None


@pytest.mark.parametrize('level', ['description', 'media', 'frozen_media'])
@pytest.mark.parametrize('value', [2, True, 4.0, '4', None, 1, 8])
def test_final_video_enum_is_consistent_with_frozen_upload(payload, media, level, value):
    fields = form.validate_form(payload)
    request = page_request(fields, media)
    assert form.request_matches(request, fields, media)
    target = (request['objectDesc'] if level == 'description' else
              request['objectDesc']['media'][0] if level == 'media' else media)
    target['mediaType'] = value
    assert not form.request_matches(request, fields, media)


def test_caption_and_short_title_preserve_content_and_real_topics(payload, media):
    f = form.validate_form(payload)
    assert f['caption'] == '测试视频标题\n正文内容。 #功能测试 #剪辑测试'
    assert f['effective_time'] == int(datetime.fromisoformat(payload['scheduled_at']).timestamp())
    assert form.verify_form(live_form(f, media), f, media, 'finder-test')
    assert form.request_matches(page_request(f, media), f, media)


@pytest.mark.parametrize('field,value', [('location', {'name': '三千海'}), ('declaration', 'AI'),
    ('cover', {'kind': 'image', 'path': 'local.jpg'}), ('cover', {'kind': 'first_frame', 'frame_ms': 2000})])
def test_unimplemented_rich_fields_never_silently_omitted(payload, field, value):
    payload['fields'][field] = value
    with pytest.raises(ValueError):
        form.validate_form(payload)


@pytest.mark.parametrize('short', ['太短', '这个标题超出平台规定的十六字限制了', '含有，逗号的短标题', '包含😀特殊字符'])
def test_short_title_official_constraints(payload, short):
    payload['fields']['channels_short_title'] = short
    with pytest.raises(ValueError):
        form.validate_form(payload)


def test_short_title_is_optional_and_platform_normalization_is_exact(payload, media):
    payload['fields']['channels_short_title'] = '测试:"短标题"?'
    f = form.validate_form(payload)
    assert f['submitted_short_title'] == '测试：“短标题”？'
    assert form.request_matches(page_request(f, media), f, media)
    payload['fields']['channels_short_title'] = ''
    assert form.validate_form(payload)['short_title'] == ''


@pytest.mark.parametrize('tags', ['#功能测试 #功能测试', '#一,二', '#一.二', '#一@二', '#一<二', '#'])
def test_ambiguous_topic_names_rejected(payload, tags):
    payload['fields']['tags'] = tags
    with pytest.raises(ValueError):
        form.validate_form(payload)


def test_description_weight_matches_official_utf16_count(payload):
    assert form.display_width('abc中文😀') == 11
    payload['fields'].update(title='中' * 1000, body='', tags='', channels_short_title='')
    assert form.validate_form(payload)
    payload['fields']['title'] += '中'
    with pytest.raises(ValueError):
        form.validate_form(payload)


def test_inline_unrequested_topic_is_not_added_from_body(payload):
    payload['fields']['body'] = '正文里的 #另外话题'
    with pytest.raises(ValueError):
        form.validate_form(payload)


@pytest.mark.parametrize('raw_change', [
    {'state_caption': '其他文案'}, {'caption': '测试视频标题 正文内容。 #功能测试 #剪辑测试'},
    {'tags': []}, {'state_tags': []}, {'time_text': '2026-01-01 12:00'}, {'effective_time': True},
    {'no_extras': False}, {'draft_id': 'existing'}, {'original_intercept': True}, {'finder_uid': 'someone-else'},
    {'editor_count': 2}, {'form_count': 2}, {'button_count': 2}, {'button_enabled': False}, {'upload_complete': False},
])
def test_live_form_mismatch_stops_before_final(payload, media, raw_change):
    f = form.validate_form(payload); raw = live_form(f, media); raw.update(raw_change)
    assert not form.verify_form(raw, f, media, 'finder-test')


@pytest.mark.parametrize('change', ['caption', 'time', 'topic', 'xml', 'media', 'cover', 'location', 'declaration', 'original', 'music'])
def test_normal_final_request_must_match_frozen_content_asset_and_time(payload, media, change):
    f = form.validate_form(payload); request = page_request(f, media)
    if change == 'caption': request['objectDesc']['description'] += ' other content'
    if change == 'time': request['effectiveTime'] += 60
    if change == 'topic': request['topics'] = ['另一个话题']
    if change == 'xml': request['objectDesc']['topic']['finderTopicInfo'] = '<finder><topic>#别的话题#</topic></finder>'
    if change == 'media': request['objectDesc']['media'][0]['url'] = 'https://example.invalid/foreign.mp4'
    if change == 'cover': request['objectDesc']['media'][0]['thumbUrl'] = 'https://example.invalid/foreign-cover'
    if change == 'location': request['objectDesc']['location'] = {'name': '三千海'}
    if change == 'declaration': request['tagInfo'] = {'tagType': 1}
    if change == 'original': request['postFlag'] = 1
    if change == 'music': request['objectDesc']['followPostInfo'] = {'hasBgm': 1, 'musicInfo': {'docId': '3'}}
    assert not form.request_matches(request, f, media)


def test_upload_uuid_and_success_toast_are_not_work_receipts():
    assert form.receipt_reference({'fileId': '43', 'md5sum': 'upload-uuid'}) == {}
    assert form.receipt_reference({'objectId': 9007199254740993}) == {}
    assert form.receipt_reference({'objectId': '9007199254740993'}) == {'work_id': '9007199254740993'}
    assert not form.successful_response({'errCode': False, 'data': {'baseResp': {'errcode': 0}}})
    assert not form.successful_response({'errCode': 0, 'data': {'baseResp': {'errcode': False}}})
    assert not form.successful_response({'errCode': 0, 'data': {}})
    assert form.successful_response({'errCode': 0, 'data': {'baseResp': {'errcode': 0}}})


def test_readback_requires_id_and_exact_time_and_never_claims_publication(payload):
    f = form.validate_form(payload)
    row = {'objectId': '100', 'desc': {'description': f['caption']}, 'effectiveTime': f['effective_time'], 'handleStatus': 2}
    assert form.verified_row([row], {}, f) is None
    assert form.verified_row([row], {'work_id': '101'}, f) is None
    assert form.verified_row([row], {'work_id': '100'}, f)['state'] == 'platform_scheduled'
    assert form.verified_row([row, row], {'work_id': '100'}, f) is None
    row['effectiveTime'] += 60
    assert form.verified_row([row], {'work_id': '100'}, f) is None


def test_immediate_and_software_schedule_do_not_set_platform_schedule(payload, media):
    for mode in ('publish_now', 'software_schedule'):
        payload['execution_mode'] = mode
        f = form.validate_form(payload)
        assert f['effective_time'] == 0 and f['mode'] == '不定时'
        request = page_request(f, media); request.pop('effectiveTime')
        assert form.request_matches(request, f, media)


def test_platform_schedule_boundaries_and_query_of_expired_schedule(payload):
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    payload['scheduled_at'] = (now + timedelta(hours=2.5)).isoformat()
    assert form.validate_form(payload, now=now)
    payload['scheduled_at'] = (now + timedelta(hours=2, minutes=29)).isoformat()
    with pytest.raises(ValueError): form.validate_form(payload, now=now)
    assert form.validate_form(payload, query=True, now=now)
    payload['scheduled_at'] = (now + timedelta(days=29)).isoformat()
    with pytest.raises(ValueError): form.validate_form(payload, now=now)


def test_absent_fields_keep_legacy_none_and_false_but_explicit_choice_is_preserved(payload):
    f = form.validate_form(payload)
    assert f['content_declaration_choice'] == 'none' and f['channels_original'] is False
    payload['fields'].update(content_declaration_choice='none', channels_original=True)
    f = form.validate_form(payload)
    assert f['content_declaration_choice'] == 'none' and f['channels_original'] is True


@pytest.mark.parametrize('choice', ['', 'fictional_performance', '内容由AI生成', 0, False, None, [], {}])
def test_unproved_content_choices_never_silently_become_none(payload, choice):
    payload['fields']['content_declaration_choice'] = choice
    with pytest.raises(ValueError): form.validate_form(payload)


@pytest.mark.parametrize('original', [0, 1, 'true', 'false', None, [], {}])
def test_original_intent_requires_actual_boolean(payload, original):
    payload['fields']['channels_original'] = original
    with pytest.raises(ValueError): form.validate_form(payload)


@pytest.mark.parametrize('requested', [False, True])
@pytest.mark.parametrize('flag', [0, 1, 2, 4, 8, 16, 17, 18, 32, True])
def test_original_request_bit_is_independent_from_temp_original_flag(payload, media, requested, flag):
    payload['fields']['channels_original'] = requested
    f = form.validate_form(payload); request = page_request(f, media); request['postFlag'] = flag
    expected = type(flag) is int and flag in ({1, 17} if requested else {0, 16})
    assert form.request_matches(request, f, media) is expected
    request['originalFlag'] = 1
    assert not form.request_matches(request, f, media)


@pytest.mark.parametrize('change', [
    {'state_original': False}, {'state_original_auth': False}, {'original_checked': False}, {'original_flag': 1},
    {'original_flag': False}, {'original_control_count': 0, 'original_checked': None},
    {'original_control_count': 2}, {'original_dialog': True},
    {'declaration_selected_count': 1}, {'declaration_selected_count': False},
    {'declaration_tag_info': {'tagType': 5}},
])
def test_original_checkbox_state_and_unrequested_content_tag_are_correlated(payload, media, change):
    payload['fields']['channels_original'] = True
    f = form.validate_form(payload); raw = live_form(f, media)
    assert form.verify_form(raw, f, media, 'finder-test')
    raw.update(change)
    assert not form.verify_form(raw, f, media, 'finder-test')


def test_original_entrance_absence_is_only_compatible_with_false(payload, media):
    f = form.validate_form(payload); raw = live_form(f, media)
    raw.update(original_control_count=0, original_checked=None)
    assert form.verify_form(raw, f, media, 'finder-test')
    raw['state_original'] = None
    assert not form.verify_form(raw, f, media, 'finder-test')


def ai_form(payload, media):
    payload['fields']['content_declaration_choice'] = 'ai_generated'
    f = form.validate_form(payload); raw = live_form(f, media)
    # Arbitrary fixture-only type/key; the adapter never hardcodes this type.
    info = {'tagType': 431, 'tagKey': 'synthetic-live-key'}
    raw.update(declaration_selected_count=1, declaration_name=f['content_declaration_name'],
        declaration_component_count=1, declaration_state_name=f['content_declaration_name'],
        declaration_state_type=431, declaration_tag_info=dict(info), declaration_component_tag_info=dict(info),
        declaration_tag_key=info['tagKey'], declaration_options=[{'name': f['content_declaration_name'], 'tag_type': 431}])
    request = page_request(f, media); request['tagInfo'] = dict(info)
    return f, raw, request, info


def test_ai_declaration_requires_live_type_key_and_preserves_separate_original_intent(payload, media):
    f, raw, request, info = ai_form(payload, media)
    assert f['channels_original'] is False
    assert form.verify_form(raw, f, media, 'finder-test', info)
    assert form.request_matches(request, f, media, info)
    assert not form.request_matches(request, f, media)
    request['postFlag'] = 1
    assert not form.request_matches(request, f, media, info)


@pytest.mark.parametrize('change', ['name', 'duplicate', 'type', 'key', 'component', 'extra', 'missing'])
def test_ai_page_mapping_or_final_tag_mutation_is_never_accepted(payload, media, change):
    f, raw, request, info = ai_form(payload, media)
    if change == 'name': raw['declaration_name'] = '内容由AI生成'
    if change == 'duplicate': raw['declaration_options'] *= 2
    if change == 'type': raw['declaration_state_type'] = True
    if change == 'key': raw['declaration_tag_key'] = 'changed'
    if change == 'component': raw['declaration_component_tag_info']['tagType'] = 432
    if change == 'extra': raw['declaration_tag_info']['repostSource'] = 'unrequested'
    if change == 'missing': raw['declaration_tag_info'] = None
    assert not form.verify_form(raw, f, media, 'finder-test', info)
    request['tagInfo']['tagKey'] = 'foreign-live-key'
    assert not form.request_matches(request, f, media, info)


@pytest.mark.parametrize('tag_info', [None, {}, {'tagType': True, 'tagKey': 'synthetic'},
    {'tagType': 431, 'tagKey': ''}, {'tagType': 431, 'tagKey': 'synthetic', 'shootInfo': {}}])
def test_ai_final_tag_info_requires_exact_frozen_structure(payload, media, tag_info):
    f, _, request, info = ai_form(payload, media); request['tagInfo'] = tag_info
    assert not form.request_matches(request, f, media, info)


def test_native_processing_ticket_must_match_this_frozen_upload(payload,media):
    f=form.validate_form(payload);owned={**media,'urlCdnTaskId':'owned-native-ticket'}
    raw=live_form(f,owned);raw.update(clip_id='owned-native-ticket',clip_bindings={'record_ticket_matches':True})
    assert form.verify_form(raw,f,owned,'finder-test')
    assert not form.verify_form(raw,f,media,'finder-test')
    raw['clip_id']='other-upload-ticket'
    assert not form.verify_form(raw,f,owned,'finder-test')
    request=page_request(f,owned);request['videoClipTaskId']='owned-native-ticket'
    assert form.request_matches(request,f,owned)
    request['videoClipTaskId']='other-upload-ticket'
    assert not form.request_matches(request,f,owned)
