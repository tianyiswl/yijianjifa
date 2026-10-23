"""Bounded diagnostic evidence; no browser, account, database or platform IO."""
import json

import pytest
from playwright.sync_api import Error, TimeoutError as PlaywrightTimeoutError

from yijianjifa.webui import kuaishou_publish_diagnostics as diagnostics


@pytest.mark.parametrize(('message', 'reason'), [
    ('existing creator draft must be handled first', 'existing_draft'),
    ('ambiguous publish mode', 'ambiguous_publish_mode'),
    ('final authority unavailable', 'final_authority_unavailable'),
    ('frozen video changed', 'frozen_video_changed'),
])
def test_only_exact_known_local_failures_are_classified(message, reason):
    assert diagnostics.classify_exception(ValueError(message)) == {
        'exception_type': 'ValueError', 'reason': reason,
    }
    assert diagnostics.classify_exception(ValueError(message + ' SECRET_TOKEN'))['reason'] == 'unknown'


@pytest.mark.parametrize(('exception', 'reason'), [
    (Error('Page.evaluate: Execution context was destroyed, most likely because of a navigation. SECRET_TOKEN'),
     'execution_context_destroyed'),
    (Error('Page.evaluate: Cannot find context with specified id SECRET_TOKEN'), 'execution_context_destroyed'),
    (Error('Page.goto: Navigation interrupted by another one https://private.invalid/?token=SECRET_TOKEN'),
     'navigation_interrupted'),
    (Error('Page.goto: net::ERR_ABORTED at https://private.invalid/?token=SECRET_TOKEN'),
     'navigation_interrupted'),
    (PlaywrightTimeoutError('Page.goto: Timeout 30000ms exceeded. SECRET_TOKEN'), 'timeout'),
    (TimeoutError('SECRET_TOKEN'), 'timeout'),
    (Error('Unclassified platform error SECRET_TOKEN'), 'unknown'),
    (ValueError('Execution context was destroyed SECRET_TOKEN'), 'unknown'),
    (RuntimeError('Timeout 30000ms exceeded SECRET_TOKEN'), 'unknown'),
])
def test_error_classification_never_exports_message_or_url(exception, reason):
    result = diagnostics.classify_exception(exception)
    assert result['reason'] == reason
    assert set(result) == {'exception_type', 'reason'}
    assert result['exception_type'] == type(exception).__name__
    encoded = json.dumps(result)
    assert 'SECRET_TOKEN' not in encoded and 'private.invalid' not in encoded


def test_untrusted_exception_string_is_not_evaluated_and_class_name_is_bounded():
    class BrokenStringError(Exception):
        def __str__(self):
            raise AssertionError('Diagnostic must not stringify an arbitrary exception')

    assert diagnostics.classify_exception(BrokenStringError('SECRET_TOKEN')) == {
        'exception_type': 'BrokenStringError', 'reason': 'unknown',
    }
    unusual = type('SECRET_TOKEN / https://private.invalid', (Exception,), {})
    assert diagnostics.classify_exception(unusual('SECRET_TOKEN')) == {
        'exception_type': 'Exception', 'reason': 'unknown',
    }


def form_case():
    expected = {'caption': 'PRIVATE_TITLE\nPRIVATE_BODY #PRIVATE_TAG',
                'tags': ['PRIVATE_TAG'], 'time_text': '2026-10-08 12:00:00', 'mode': '定时发布'}
    raw = {'caption': 'PRIVATE_TITLE  PRIVATE_BODY\n#PRIVATE_TAG',
           'tags': ['PRIVATE_TAG'], 'time_text': expected['time_text'], 'mode': expected['mode'],
           'upload_complete': True, 'editor_count': 1, 'button_count': 1, 'button_enabled': True,
           'identity': 'PRIVATE_UID', 'url': 'https://private.invalid', 'token': 'SECRET_TOKEN'}
    return raw, expected


def test_form_facts_capture_matches_and_only_export_allowed_booleans_and_counts():
    raw, expected = form_case()
    original = json.dumps((raw, expected), sort_keys=True)
    result = diagnostics.formfacts(raw, expected)
    assert result == {'title_match': True, 'tags_match': True, 'time_match': True,
                      'mode_match': True, 'upload_complete': True, 'editor_count': 1,
                      'button_count': 1, 'button_enabled': True}
    assert json.dumps((raw, expected), sort_keys=True) == original
    encoded = json.dumps(result)
    for private in ('PRIVATE_', 'SECRET_TOKEN', 'private.invalid', '2026-10-08', '定时发布'):
        assert private not in encoded


@pytest.mark.parametrize(('field', 'value', 'fact'), [
    ('caption', 'different content', 'title_match'),
    ('tags', [], 'tags_match'),
    ('time_text', '2026-10-09 12:00:00', 'time_match'),
    ('mode', '立即发布', 'mode_match'),
])
def test_changed_fields_remain_visible_as_false_without_exporting_values(field, value, fact):
    raw, expected = form_case()
    raw[field] = value
    result = diagnostics.formfacts(raw, expected)
    assert result[fact] is False
    assert all(result[k] is True for k in ('title_match', 'tags_match', 'time_match', 'mode_match') if k != fact)


@pytest.mark.parametrize('value', [True, False, -1, 101, 1.0, '1', None, [], {}])
def test_invalid_counts_and_truthy_flags_are_not_trusted(value):
    raw, expected = form_case()
    raw.update(editor_count=value, button_count=value, upload_complete=value, button_enabled=value)
    result = diagnostics.formfacts(raw, expected)
    assert result['editor_count'] == result['button_count'] == 0
    assert type(result['editor_count']) is int
    assert result['upload_complete'] is (value is True)
    assert result['button_enabled'] is (value is True)


def test_boundary_counts_and_empty_immediate_fields_are_valid():
    raw = {'caption': 'title', 'tags': [], 'time_text': '', 'mode': '立即发布',
           'editor_count': 0, 'button_count': 100}
    expected = {'caption': 'title', 'tags': [], 'time_text': '', 'mode': '立即发布'}
    result = diagnostics.formfacts(raw, expected)
    assert all(result[k] is True for k in ('title_match', 'tags_match', 'time_match', 'mode_match'))
    assert result['editor_count'] == 0 and result['button_count'] == 100


@pytest.mark.parametrize(('raw', 'expected'), [(None, None), ({}, {}), ([], {}), ({}, [])])
def test_missing_or_malformed_form_is_not_reported_as_matching(raw, expected):
    result = diagnostics.formfacts(raw, expected)
    assert all(value is False for key, value in result.items() if key.endswith('_match'))
    assert result['editor_count'] == result['button_count'] == 0
