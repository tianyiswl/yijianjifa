"""Local ordinary-DOM fixtures; these do not establish the live platform contract."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

from yijianjifa.webui.douyin_image_form import validate_image_form_request, verify_image_form


def request(**values):
    return validate_image_form_request(expected_uid="123", title="图文标题", body="图文正文", image_names=("01.jpg", "02.jpg"), **values)


def ready_form(form):
    return {"identity": {"uid": "123", "uid_after": "123", "status": 0, "after_status": 0, "profile_status": 0, "nickname": "测试账号"},
            "path": "/creator-micro/content/publish", "image_page": True,
            "ready": True, "title": form["title_text"], "body": form["body_text"], "topics": form["topic_names"],
            "declaration": form["declaration_label"], "declaration_verified": True,
            "scheduled_time_text": form["scheduled_time_text"], "schedule_mode": "scheduled" if form["scheduled_time_text"] else "immediate",
            "title_count": 1, "editor_count": 1, "mode_controls": 1, "final_button_count": 1, "final_button_enabled": True,
            "image_count": 2, "image_names": ["01.jpg", "02.jpg"], "image_order_verified": True, "upload_complete": True}


def test_image_title_uses_image_limit_and_counts_tags_losslessly():
    value = validate_image_form_request(expected_uid="123", title="字" * 80, body="正文", tags="#产品 #产品 #实拍", image_names=("1.jpg",))
    assert value["title_text"] == "字" * 80
    assert value["body_text"] == "正文\n#产品 #实拍"
    assert value["topic_names"] == ["产品", "实拍"]
    with pytest.raises(ValueError):
        validate_image_form_request(expected_uid="123", title="字" * 81, body="", image_names=("1.jpg",))
    with pytest.raises(ValueError):
        validate_image_form_request(expected_uid="123", title="标题", body="字" * 998, tags="#测试", image_names=("1.jpg",))


@pytest.mark.parametrize("names", [(), tuple(str(n) + ".jpg" for n in range(31)), ("../1.jpg",), ("1.jpg", "1.jpg")])
def test_invalid_image_manifest_rejected_before_browser(names):
    with pytest.raises(ValueError):
        validate_image_form_request(expected_uid="123", title="标题", body="正文", image_names=names)


def test_exact_image_readback_is_prepared_only():
    form = request()
    assert verify_image_form(ready_form(form), form)["submitted"] is False


@pytest.mark.parametrize("change", [
    {"path": "/creator-micro/content/post/video"}, {"image_page": False}, {"title": "改了标题"}, {"body": ""},
    {"image_count": 1}, {"image_names": ["02.jpg", "01.jpg"]}, {"image_order_verified": False}, {"upload_complete": False},
    {"final_button_count": 2}, {"final_button_enabled": False}, {"declaration_verified": False}, {"declaration": "AI生成"},
    {"topics": ["错误"]}, {"mode_controls": 0}, {"schedule_mode": "scheduled"},
])
def test_changed_or_unproven_form_is_rejected(change):
    form = request()
    with pytest.raises(ValueError):
        verify_image_form({**ready_form(form), **change}, form)


def test_schedule_is_exact_and_does_not_fall_back_to_immediate():
    now = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)
    form = request(scheduled_time_text="2026-10-15 12:00", now=now)
    raw = ready_form(form)
    assert verify_image_form(raw, form)["mode"] == "platform_schedule"
    for change in ({"scheduled_time_text": "2026-10-15 12:01"}, {"schedule_mode": "immediate"}):
        with pytest.raises(ValueError):
            verify_image_form({**raw, **change}, form)
    for time in ("2026-10-14 12:29", "2026-10-27 10:01"):
        with pytest.raises(ValueError):
            request(scheduled_time_text=time, now=now)


def test_identity_change_never_accepts_ready_form():
    form = request()
    raw = ready_form(form)
    raw["identity"]["uid_after"] = "456"
    with pytest.raises(ValueError):
        verify_image_form(raw, form)


def run_dom(script, form=None, turns=1, **change):
    result = subprocess.run(["node", str(Path(__file__).parent / "support/douyin_image_dom.cjs")],
                            input=json.dumps({"script": script, "request": form or request(), "change": change, "turns": turns}),
                            text=True, encoding='utf-8', capture_output=True, check=True)
    return json.loads(result.stdout)


def test_node_response_keeps_chinese_under_windows_legacy_code_page(monkeypatch):
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    # Windows Python 3.12 may default subprocess text pipes to a legacy code
    # page, while Node emits UTF-8 JSON. Exercise the real child-process output.
    monkeypatch.setattr(subprocess, '_text_encoding', lambda: 'cp1252')
    observed = run_dom(PREPARE_IMAGE_FORM)
    assert observed['title'] == '图文标题'
    assert observed['body'] == '图文正文'
    verify_image_form(observed['result'], request())


def test_dom_reads_only_image_preview_container_excluding_avatar():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    observed = run_dom(PREPARE_IMAGE_FORM, extraAvatarInArea=True)
    assert observed["result"]["ready"] is True
    assert observed["result"]["image_count"] == 2
    assert observed["submitClicks"] == 0
    verify_image_form(observed["result"], request())


def test_plain_confirmation_rechecks_original_form_without_accepting_new_terms():
    from yijianjifa.webui.douyin_image_form import CONFIRM_IMAGE_SUBMISSION
    plain = run_dom(CONFIRM_IMAGE_SUBMISSION, confirmDialog=True)
    assert plain['result']['attempted'] and plain['submitClicks'] == 1
    for change in ({'confirmTerms': True}, {'emptyTitle': True}, {'reversedImages': True}, {'wrongIdentity': True}):
        unsafe = run_dom(CONFIRM_IMAGE_SUBMISSION, confirmDialog=True, **change)
        assert not unsafe['result']['attempted'] and unsafe['submitClicks'] == 0


@pytest.mark.parametrize("change,code", [
    ({"reversedImages": True}, "image_order_unverified"),
    ({"missingImage": True}, "image_order_unverified"),
    ({"noStatus": True}, "image_upload_unverified"),
    ({"noFilename": True, "noStatus": True}, "image_order_unverified"),
    ({"titleMax": 2}, "image_title_too_long"),
    ({"bodyMax": 2}, "image_body_too_long"),
    ({"wrongIdentity": True}, "image_identity_unverified"),
    ({"path": "/creator-micro/content/post/video"}, "image_page_unverified"),
    ({"dialog": True}, "image_confirmation_required"),
])
def test_dom_fails_precisely_without_submit_or_truncation(change, code):
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    observed = run_dom(FINAL_IMAGE_CLICK, **change)
    assert observed["result"]["failure_code"] == code
    assert observed["result"]["attempted"] is False
    assert observed["submitClicks"] == 0
    assert observed["title"] == "图文标题"


def test_authenticated_image_ids_must_match_actual_preview_order():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    form = {**request(), "image_upload_ids": ["upload-image-1", "upload-image-2"]}
    observed = run_dom(PREPARE_IMAGE_FORM, form=form, noFilename=True, noStatus=True)
    assert observed["result"]["ready"]
    verify_image_form(observed["result"], form)
    reversed_result = run_dom(PREPARE_IMAGE_FORM, form=form, noFilename=True, noStatus=True, reversedImages=True)
    assert reversed_result["result"]["failure_code"] == "image_order_unverified"
    # Correct names cannot conceal changed content once authenticated IDs exist.
    wrong_ids = {**form, "image_upload_ids": ["other-image-1", "other-image-2"]}
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    assert run_dom(FINAL_IMAGE_CLICK, form=wrong_ids)["submitClicks"] == 0
    substring_ids = {**form, "image_upload_ids": ["upload-image", "upload-image"]}
    assert run_dom(FINAL_IMAGE_CLICK, form=substring_ids)["submitClicks"] == 0


def test_public_image_counter_and_css_background_tiles_prove_order_without_img():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    form = {**request(), "image_upload_ids": ["upload-image-1", "upload-image-2"]}
    observed = run_dom(PREPARE_IMAGE_FORM, form=form, backgroundTiles=True)
    assert observed['result']['ready']
    assert observed['result']['image_count'] == 2
    assert observed['result']['image_upload_ids'] == form['image_upload_ids']
    assert observed['submitClicks'] == 0
    verify_image_form(observed['result'], form)
    # Signed-looking public URLs alone carry no upload/acceptance authority.
    without_upload_ack = run_dom(PREPARE_IMAGE_FORM, backgroundTiles=True)
    assert not without_upload_ack['result']['ready']


def test_nested_counter_without_input_reads_independent_http_img_tiles_and_visual_order():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM, FINAL_IMAGE_CLICK
    form = {**request(), 'image_upload_ids': ['upload-image-1', 'upload-image-2']}
    # Real shape: each independent tile has one HTTP IMG and two data SVGs;
    # counter spans have no img role, and there is no file input after upload.
    for change in ({}, {'reversedImages': True}, {'verticalImages': True}):
        observed = run_dom(PREPARE_IMAGE_FORM, form=form, counterHttpTiles=True, **change)
        assert observed['result']['ready']
        assert observed['result']['image_count'] == 2
        assert observed['result']['image_upload_ids'] == form['image_upload_ids']
        verify_image_form(observed['result'], form)
        assert observed['submitClicks'] == 0
    assert run_dom(FINAL_IMAGE_CLICK, form=form, counterHttpTiles=True, reversedImages=True)['submitClicks'] == 1


@pytest.mark.parametrize('ids', [[], ['upload-image-1'], ['other-1', 'other-2'], ['upload-image', 'upload-image']])
def test_counter_http_signed_urls_need_exact_complete_authenticated_upload_ids(ids):
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    observed = run_dom(FINAL_IMAGE_CLICK, form={**request(), 'image_upload_ids': ids}, counterHttpTiles=True)
    assert not observed['result']['ready'] and observed['submitClicks'] == 0


def test_single_and_three_photo_counter_tiles_preserve_authenticated_manifest():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    for count in (1, 3):
        form = validate_image_form_request(expected_uid='123', title='图文标题', body='正文', image_names=tuple(f'{n + 1}.jpg' for n in range(count)))
        form['image_upload_ids'] = [f'upload-image-{n + 1}' for n in range(count)]
        observed = run_dom(PREPARE_IMAGE_FORM, form=form, counterHttpTiles=True, reversedImages=True)
        assert observed['result']['ready'] and observed['result']['image_count'] == count
        verify_image_form(observed['result'], form)


def test_counter_sibling_container_has_only_three_photos_and_six_svg_icons():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM
    form = validate_image_form_request(expected_uid='123', title='图文标题', body='正文', image_names=('1.jpg', '2.jpg', '3.jpg'))
    form['image_upload_ids'] = ['upload-image-1', 'upload-image-2', 'upload-image-3']
    observed = run_dom(PREPARE_IMAGE_FORM, form=form, counterHttpTiles=True, nestedCounterContainer=True)
    assert observed['result']['ready'] and observed['result']['image_count'] == 3
    assert observed['result']['image_dom_mode'] == 'counter-http-img'
    assert observed['submitClicks'] == 0
    verify_image_form(observed['result'], form)


@pytest.mark.parametrize('change', [
    {'reversedVisual': True}, {'sameHttpParent': True}, {'overlappedImages': True},
    {'missingImage': True}, {'counterCount': 3}, {'brokenImage': True}, {'uploadBusy': True},
    {'zeroWidthImage': True}, {'hiddenImage': True},
])
def test_counter_http_img_geometry_shape_and_ack_fail_closed(change):
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    form = {**request(), 'image_upload_ids': ['upload-image-1', 'upload-image-2']}
    observed = run_dom(FINAL_IMAGE_CLICK, form=form, counterHttpTiles=True, **change)
    assert not observed['result']['ready'] and observed['submitClicks'] == 0


@pytest.mark.parametrize('change', [
    {'reversedImages': True}, {'missingImage': True}, {'counterCount': 3},
    {'uploadBusy': True}, {'hiddenBackground': True}, {'dataBackground': True},
    {'decorationBackground': True},
])
def test_css_background_count_order_and_upload_proof_are_rechecked_before_final(change):
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    form = {**request(), "image_upload_ids": ["upload-image-1", "upload-image-2"]}
    observed = run_dom(FINAL_IMAGE_CLICK, form=form, backgroundTiles=True, **change)
    assert observed['submitClicks'] == 0
    assert observed['result']['attempted'] is False


def test_prepare_fills_with_input_events_and_preserves_exact_schedule():
    from yijianjifa.webui.douyin_image_form import PREPARE_IMAGE_FORM, FINAL_IMAGE_CLICK
    form = request(tags="#产品 #实拍", scheduled_time_text="2026-10-15 12:00", now=datetime(2026, 10, 14, tzinfo=timezone.utc))
    observed = run_dom(PREPARE_IMAGE_FORM, form=form, turns=5, emptyTitle=True, emptyBody=True, immediateMode=True, wrongTime="2026-10-15 12:01", datetimeLocal=True)
    assert observed["result"]["ready"]
    assert observed["title"] == form["title_text"] and observed["body"] == form["body_text"]
    assert observed["date"] == "2026-10-15T12:00" and observed["scheduled"] is True
    assert {event["type"] for event in observed["events"]} == {"input", "change"}
    assert observed["submitClicks"] == 0
    verify_image_form(observed["result"], form)
    submitted = run_dom(FINAL_IMAGE_CLICK, form=form)
    assert submitted["result"]["attempted"] and submitted["submitClicks"] == 1
    assert submitted["result"]["submitted"] is False


@pytest.mark.parametrize("change", [{"wrongTime": "2026-10-15 12:01"}, {"immediateMode": True}, {"brokenImage": True}, {"uploadBusy": True}, {"duplicatePublish": True}, {"disabled": True}, {"secondEditor": True}])
def test_final_never_submits_changed_or_incomplete_form(change):
    from yijianjifa.webui.douyin_image_form import FINAL_IMAGE_CLICK
    form = request(scheduled_time_text="2026-10-15 12:00", now=datetime(2026, 10, 14, tzinfo=timezone.utc))
    observed = run_dom(FINAL_IMAGE_CLICK, form=form, **change)
    assert observed["submitClicks"] == 0
    assert observed["result"]["attempted"] is False


def test_manage_readback_is_unique_image_copy_time_and_id_bound():
    from yijianjifa.webui.douyin_image_form import READ_IMAGE_RESULT, verified_image_result
    form = request(scheduled_time_text="2026-10-15 12:00", now=datetime(2026, 10, 14, tzinfo=timezone.utc))
    form["image_upload_ids"] = ["tos-cn-i-jm8ajry58r/" + f'{n:032x}' for n in (1, 2)]
    form["submission_item_id"] = "7696358409028930826"
    result = run_dom(READ_IMAGE_RESULT, form=form, management=True, actualImageCard=True)["result"]
    assert verified_image_result(result, form) == {"accepted": True, "published": False, "scheduled_time_text": "2026-10-15 12:00", "item_id": form['submission_item_id']}
    old = {"metadata": {"aweme_id": "7696358409028930827"}}
    current = {}
    result = run_dom(READ_IMAGE_RESULT, form=form, management=True, actualImageCard=True, rows=[old, current])["result"]
    assert verified_image_result(result, form)["accepted"]
    for rows in ([{"metadata": {"is_pic_word": False}}], [{"time": "2026年10月15日 12:01"}], [{"text": "其他作品"}], [current, current], [{"status": "已发布"}], [old]):
        observed = run_dom(READ_IMAGE_RESULT, form=form, management=True, actualImageCard=True, rows=rows)["result"]
        assert verified_image_result(observed, form) is None


def test_manage_redirect_or_toast_alone_does_not_prove_receipt():
    from yijianjifa.webui.douyin_image_form import READ_IMAGE_RESULT, verified_image_result
    form = request()
    for change in ({"path": "/creator-micro/content/manage"}, {"management": True, "rows": []}, {"management": True}):
        observed = run_dom(READ_IMAGE_RESULT, form=form, **change)["result"]
        assert verified_image_result(observed, form) is None
