"""A local fixture of the observed card binding, never a live platform claim."""
from datetime import datetime, timezone

import pytest

from yijianjifa.webui.douyin_image_form import READ_IMAGE_RESULT, verified_image_result
from test_douyin_image_form import request, run_dom


KEYS = [f'{n:032x}' for n in (1, 2, 3)]
UPLOADS = ['tos-cn-i-jm8ajry58r/' + key for key in KEYS]
IMAGES = [{'uri': 'tos-cn-i-dy/' + key} for key in KEYS]
ITEM_ID = '7696358409028930826'


def form():
    value = request(tags='#产品 #实拍', scheduled_time_text='2026-10-15 12:00', now=datetime(2026, 10, 14, tzinfo=timezone.utc))
    value.update(image_names=['1.jpg', '2.jpg', '3.jpg'], image_count=3, image_upload_ids=UPLOADS)
    return value


def observed(value=None, rows=None, **change):
    return run_dom(READ_IMAGE_RESULT, form=value or form(), management=True, actualImageCard=True, rows=rows or [{}], **change)['result']


def test_unique_bound_card_proves_scheduled_receipt_with_exact_uploads_and_string_id():
    value = form()
    result = observed(value)
    assert verified_image_result(result, value) == {
        'accepted': True, 'published': False, 'scheduled_time_text': '2026-10-15 12:00', 'item_id': ITEM_ID,
    }
    assert result['matches'][0]['item_id'] == ITEM_ID


def test_exact_iso_timestamp_is_supported_without_accepting_one_minute_change():
    value = form()
    assert verified_image_result(observed(value, [{'time': '2026-10-15 12:00'}]), value)['accepted']
    assert verified_image_result(observed(value, [{'time': '2026-10-15 12:01'}]), value) is None


@pytest.mark.parametrize('metadata', [
    {'aweme_id': 7696358409028930826}, {'aweme_id': ''},
    {'author_user_id': 456}, {'author': {'uid': '456'}},
    {'is_pic_word': False}, {'aweme_type': 0}, {'desc': '其他标题。其他正文'},
    {'images': list(reversed(IMAGES))}, {'images': IMAGES[:2]},
    {'images': [{'uri': 'tos-cn-i-other/' + key} for key in KEYS]},
    {'images': [{'uri': 'tos-cn-i-dy/' + f'{n:032x}'} for n in (4, 5, 6)]},
    {'timer': {'public_time': 1792036801, 'status': 0}}, {'timer': {'public_time': 1792036800, 'status': 1}},
    {'status_value': 0},
    {'status': {'aweme_id': '7696358409028930827', 'in_reviewing': False, 'is_delete': False, 'is_prohibited': False}},
    {'status': {'aweme_id': ITEM_ID, 'in_reviewing': False, 'is_delete': True, 'is_prohibited': False}},
    {'status': {'aweme_id': ITEM_ID, 'in_reviewing': False, 'is_delete': False, 'is_prohibited': True}},
])
def test_unbound_changed_or_unknown_card_metadata_does_not_prove_receipt(metadata):
    value = form()
    assert verified_image_result(observed(value, [{'metadata': metadata}]), value) is None


@pytest.mark.parametrize('spec', [
    {'noMetadata': True}, {'fiberDepth': 9}, {'text': '相同排期中的别的作品'},
    {'time': '2026年10月15日 12:01'}, {'status': '已发布'}, {'hidden': True},
])
def test_visible_caption_exact_dom_time_and_bounded_binding_required(spec):
    value = form()
    assert verified_image_result(observed(value, [spec]), value) is None


def test_identical_old_caption_without_matching_upload_keys_does_not_mask_current_card():
    value = form()
    old = {'metadata': {'aweme_id': '7696358409028930827', 'images': [{'uri': 'tos-cn-i-dy/' + f'{n:032x}'} for n in (4, 5, 6)]}}
    assert verified_image_result(observed(value, [old, {}]), value)['item_id'] == ITEM_ID
    assert verified_image_result(observed(value, [{}, {}]), value) is None


@pytest.mark.parametrize('uploads', [[], UPLOADS[:2], ['tos-cn-i-other/' + key for key in KEYS], [UPLOADS[0]] * 3])
def test_receipt_requires_original_complete_ordered_authenticated_uploads(uploads):
    value = form()
    value['image_upload_ids'] = uploads
    assert verified_image_result(observed(value), value) is None


def test_bound_submission_id_adds_constraint_without_replacing_upload_proof():
    value = form()
    value['submission_item_id'] = ITEM_ID
    assert verified_image_result(observed(value), value)['accepted']
    value['submission_item_id'] = '7696358409028930827'
    assert verified_image_result(observed(value), value) is None


def test_reader_does_not_write_search_controls_or_click_and_checks_identity_again():
    value = form()
    result = run_dom(READ_IMAGE_RESULT, form=value, management=True, actualImageCard=True, searchMissingValue=True)
    assert verified_image_result(result['result'], value)['accepted']
    assert result['events'] == [] and result['submitClicks'] == 0
    wrong = observed(value, wrongIdentity=True)
    assert verified_image_result(wrong, value) is None
    assert verified_image_result(observed(value, [{'fiberDepth': 8}]), value)['accepted']


@pytest.mark.parametrize('field,bad', [
    ('item_id', 7696358409028930826), ('status_item_id', '7696358409028930827'),
    ('author_uid', '456'), ('author_user_id', '456'), ('caption', '图文标题。不是原正文'),
    ('caption', []), ('dom_caption', {}), ('dom_caption', '只有标题'), ('is_pic_word', False), ('aweme_type', True),
    ('image_uris', list(reversed(IMAGES))), ('time', '2026-10-15 12:01'),
    ('scheduled_epoch', 1792036801), ('timer_status', False), ('status_value', 0),
    ('is_delete', True), ('is_prohibited', True), ('in_reviewing', True),
])
def test_python_boundary_independently_rechecks_observed_receipt_fields(field, bad):
    value = form()
    result = observed(value)
    result['matches'][0][field] = bad
    assert verified_image_result(result, value) is None
