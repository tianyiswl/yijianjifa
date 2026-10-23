"""Finite own-code diagnostics; no URL, DOM text, account or exception content."""
import json
import logging
import pytest
from playwright.sync_api import Error
from test_channels_multi_publish import case, module


@pytest.mark.parametrize('message,kind',[
    ('TypeError: e.getClientRects is not a function','node_geometry_unavailable'),
    ("TypeError: getComputedStyle parameter 1 is not of type 'Element'",'non_element_style_read'),
    ('TypeError: fixture changed','type_error'),
    ('ReferenceError: fixture missing','reference_error'),
    ('SyntaxError: fixture invalid','syntax_error'),
    ('fixture browser failure','other_browser_error'),
])
def test_preparation_diagnostic_uses_finite_js_reason_and_own_frame(case,monkeypatch,caplog,message,kind):
    monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS','1')
    adapter,payload,_,events,_,_=case
    adapter.validate_target(payload,payload,payload['execution_mode'])
    s=adapter._session(payload);s.phase='prepare'
    original=s.page.evaluate
    def fail(script):
        if script==module.form_contract.READ_FORM:
            raise Error(message+' SECRET fixture-user https://private.invalid/?token=hidden')
        return original(script)
    monkeypatch.setattr(s.page,'evaluate',fail)
    monkeypatch.setattr(adapter,'_upload',module.ChannelsMultiPublishAdapter._upload.__get__(adapter))
    with caplog.at_level(logging.WARNING),pytest.raises(Error) as caught:
        adapter.submit(payload,context=None,before_final=lambda:pytest.fail('must not submit'))
    detail=adapter.preparation_diagnostic()
    assert detail['js_kind']==kind and detail['error_type']=='Error'
    assert detail['source']=='channels_multi_publish.py' and detail['method']=='_upload' and detail['line']>0
    assert detail['error_stage']=='upload_preparation'
    public=adapter.failure_diagnostic(caught.value)
    assert public['error_stage']=='upload_preparation'
    expected='javascript_read_failed' if 'Error:' in message else 'execution_failed'
    assert public['error_category']==expected
    serialized=json.dumps(detail)+json.dumps(public)+caplog.text
    assert all(private not in serialized for private in ('SECRET','fixture-user','private.invalid','token=hidden'))
    assert not any(event[0] in {'upload','final'} for event in events) and not s.final
    adapter.close()


def test_safe_preparation_metadata_has_no_default_log(case,monkeypatch,caplog):
    monkeypatch.delenv('P011_PUBLISH_DIAGNOSTICS',raising=False)
    adapter,*_=case;adapter._preparation_stage='upload_preparation'
    with caplog.at_level(logging.WARNING):
        adapter._record_preparation_failure(Error('TypeError: fixture SECRET'))
    assert not caplog.records
    assert adapter.preparation_diagnostic()['js_kind']=='type_error'
    assert 'SECRET' not in json.dumps(adapter.preparation_diagnostic())
    adapter.close()


@pytest.mark.parametrize('change,media_owned,draft_present,uid_present,uid_matches,invalid',[
    ('invalid-media',False,False,True,True,['videoPlayLen']),
    ('missing-media',False,False,True,True,[]),
    ('draft',True,True,True,True,[]),
    ('missing-finder',True,False,False,False,[]),
    ('wrong-finder',True,False,True,False,[]),
])
def test_uploaded_gate_conditions_are_separate_finite_checks_without_values(case,monkeypatch,caplog,
        change,media_owned,draft_present,uid_present,uid_matches,invalid):
    from copy import deepcopy
    from test_channels_publish_form import live_form
    adapter,payload,_,events,_,media=case
    monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS','1')
    adapter.validate_target(payload,payload,payload['execution_mode'])
    s=adapter._session(payload);s.phase='prepare';s.uploaded=True;s.form_filled=False
    value=deepcopy(live_form(module.form_contract.validate_form(payload),media));value['state_ready']=True;value['upload_complete']=True
    if change=='invalid-media':value['media']['videoPlayLen']=None
    elif change=='missing-media':value['media']=None
    elif change=='draft':value['draft_id']='SECRET-draft-id'
    elif change=='missing-finder':value['finder_uid']=None
    elif change=='wrong-finder':value['finder_uid']='SECRET-other-account'
    original=s.page.evaluate
    monkeypatch.setattr(s.page,'evaluate',lambda script:value if script==module.form_contract.READ_FORM else original(script))
    monkeypatch.setattr(adapter,'_prepare_form',module.ChannelsMultiPublishAdapter._prepare_form.__get__(adapter))
    with caplog.at_level(logging.WARNING),pytest.raises(ValueError,match='uploaded media or account unavailable') as caught:
        adapter.submit(payload,context=None,before_final=lambda:pytest.fail('no final allowed'))
    detail=adapter.preparation_diagnostic()
    assert detail['method']=='_prepare_form' and detail['error_stage']=='form_preparation'
    assert detail['upload_gate']==dict(media_record=change!='missing-media',media_owned=media_owned,
        media_invalid_fields=invalid,draft_present=draft_present,finder_uid_present=uid_present,finder_uid_matches=uid_matches)
    assert adapter.failure_diagnostic(caught.value)==dict(error_stage='form_preparation',error_category='form_changed')
    serialized=json.dumps(detail)+caplog.text
    assert 'SECRET' not in serialized and 'owned.mp4' not in serialized and 'finder-test' not in serialized
    assert not s.final and not any(event[0]=='final' for event in events)
    adapter.close()


@pytest.mark.parametrize('key,value,failed_gate', [
    ('tags', [], 'tags_match'),
    ('state_tags', [], 'state_tags_match'),
    ('effective_time', 1, 'effective_time_matches'),
    ('time_text', 'SECRET-other-time', 'time_text_matches'),
    ('no_extras', False, 'no_extras'),
    ('button_enabled', False, 'final_control_enabled'),
    ('caption', 'SECRET-private-caption', 'caption_matches'),
])
def test_real_prepare_loop_timeout_identifies_failed_gate_without_private_values(
        case, monkeypatch, caplog, key, value, failed_gate):
    from copy import deepcopy
    from test_channels_publish_form import live_form
    adapter, payload, _, events, _, media = case
    monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS', '1')
    adapter.validate_target(payload, payload, payload['execution_mode'])
    s = adapter._session(payload)
    s.phase = 'prepare'; s.uploaded = True; s.form_filled = True
    s.owned_media = deepcopy(media)
    s.content_selection_attempted = s.original_selection_attempted = True
    raw = live_form(module.form_contract.validate_form(payload), media)
    raw.update(state_ready=True, **{key: value})
    original = s.page.evaluate
    monkeypatch.setattr(s.page, 'evaluate', lambda script: raw if script == module.form_contract.READ_FORM else original(script))
    monkeypatch.setattr(adapter, '_prepare_form', module.ChannelsMultiPublishAdapter._prepare_form.__get__(adapter))
    with caplog.at_level(logging.WARNING):
        result = adapter.submit(payload, context=None, before_final=lambda: pytest.fail('no final'))
    assert result.state == 'pre_submit_failed' and result.error_code == 'timeout'
    detail = adapter.preparation_diagnostic()
    assert detail['error_stage'] == 'form_preparation' and detail['error_category'] == 'timeout'
    assert detail['phase'] == 'prepare' and detail['progress']['form_filled'] is True
    assert detail['form_observation']['checks'][failed_gate] is False
    assert all(type(v) is bool for v in detail['form_observation']['checks'].values())
    assert detail['form_observation']['counts']['media_count'] == 1
    assert not any(secret in json.dumps(detail) + caplog.text for secret in
                   ('SECRET', 'finder-test', 'owned.mp4', 'owned-uuid', payload['fields']['title'], 'https://'))
    assert not s.final and not any(event[0] == 'final' for event in events)
    adapter.close()


def test_timeout_diagnostics_remain_unlogged_by_default_and_counts_are_bounded(case, monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.delenv('P011_PUBLISH_DIAGNOSTICS', raising=False)
    adapter, payload, *_ = case
    s = SimpleNamespace(phase='prepare', payload=payload, uploaded=False, form_filled=False)
    adapter._observe_form_gates(s, {'editor_count': 9007199254740993, 'media_count': True,
                                  'finder_uid': 'SECRET', 'caption': 'SECRET', 'url': 'SECRET'})
    adapter._preparation_stage = 'upload_preparation'
    with caplog.at_level(logging.WARNING):
        adapter._record_preparation_timeout(s)
    assert not caplog.records
    detail = adapter.preparation_diagnostic()
    assert detail['form_observation']['counts']['editor_count'] == 'unknown'
    assert detail['form_observation']['counts']['media_count'] == 'unknown'
    assert 'SECRET' not in json.dumps(detail)
    adapter.close()


def test_post_final_result_timeout_does_not_misreport_preparation_failure(case, monkeypatch, caplog):
    monkeypatch.setenv('P011_PUBLISH_DIAGNOSTICS', '1')
    adapter, payload, *_ = case
    adapter.validate_target(payload, payload, payload['execution_mode'])
    s = adapter._session(payload); s.final = True; s.phase = 'result'
    with caplog.at_level(logging.WARNING):
        result = adapter._advance(s, query=True)
    assert result.state == 'result_unknown'
    assert adapter.preparation_diagnostic() == {} and not caplog.records
    adapter.close()
