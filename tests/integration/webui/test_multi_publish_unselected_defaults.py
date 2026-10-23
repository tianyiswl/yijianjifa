"""Platform defaults are local preferences, independent from publish selection."""
import copy
import json
import uuid

import pytest
from sqlalchemy import func, select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_settings import KEY, defaults, validate
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.normal_service import PublishTaskRow
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def dispatch(api, operation, params=None):
    return api.dispatch(json.dumps({'v': 1, 'request_id': uuid.uuid4().hex,
        'operation': 'multi_publish.' + operation, 'params': params or {}}), trusted_origin=True)


def call(api, operation, params=None):
    response = dispatch(api, operation, params)
    assert response['ok'], response
    return response['data']


def platform_defaults():
    return {
        'douyin': {'body': '抖音默认正文'},
        'bilibili': {'category_choice': 'human:tech', 'creation_statement_choice': 'no_mark',
                    'content_authorization': False, 'reprint_source': ''},
        'kuaishou': {'content_declaration_choice': 'fictional_performance'},
        'channels': {'channels_short_title': '视频号默认短标题',
                     'content_declaration_choice': 'ai_generated', 'channels_original': False},
    }


def test_read_distinguishes_first_start_from_saved_empty_selection_without_changing_schema(
        db_engine, app_paths):
    settings = defaults()
    api = DesktopApi(db_engine, app_paths, publish_adapters={})
    try:
        initial = call(api, 'settings.read')
        assert initial['has_saved_settings'] is False
        assert initial['settings'] == settings
        # A reusable preset alone is not the user's current persisted selection.
        call(api, 'presets.save', {'id': None, 'name': '仅保存方案', 'settings': settings})
        assert call(api, 'settings.read')['has_saved_settings'] is False
        rejected = dispatch(api, 'settings.save', {'settings': {**settings, 'has_saved_settings': True}})
        assert rejected['error']['code'] == 'invalid_request'
        assert call(api, 'settings.read')['has_saved_settings'] is False
        call(api, 'settings.save', {'settings': settings})
        remembered = call(api, 'settings.read')
        assert remembered['has_saved_settings'] is True
        assert remembered['settings']['targets'] == []
    finally:
        assert api.close(3)
    with session_scope(db_engine) as session:
        # The metadata is derived from row existence, never stored in schema 2.
        assert json.loads(session.get(Setting, KEY).value_json) == settings
    reopened = DesktopApi(db_engine, app_paths, publish_adapters={})
    try:
        remembered = call(reopened, 'settings.read')
        assert remembered['has_saved_settings'] is True
        assert remembered['settings'] == settings
    finally:
        assert reopened.close(3)


@pytest.mark.parametrize('select_douyin', [False, True])
def test_unselected_platform_defaults_save_reopen_and_preset_without_creating_tasks(
        db_engine, app_paths, select_douyin):
    settings = defaults()
    if select_douyin:
        account = AccountService(db_engine).create('douyin', 'synthetic-default', '合成账号')
        settings['targets'] = [{'platform': 'douyin', 'account_id': account.id}]
    settings['fields_by_platform'] = platform_defaults()
    original = copy.deepcopy(settings)
    api = DesktopApi(db_engine, app_paths, publish_adapters={})
    try:
        assert call(api, 'settings.save', {'settings': settings})['settings'] == original
        preset = call(api, 'presets.save', {'id': None, 'name': '各平台默认资料', 'settings': settings})
        assert preset['schema_version'] == 2 and preset['settings'] == original
        assert settings == original  # Validation must not alter the caller's selection.
    finally:
        assert api.close(3)

    reopened = DesktopApi(db_engine, app_paths, publish_adapters={})
    try:
        remembered = call(reopened, 'settings.read')
        assert remembered['settings'] == original and remembered['issues'] == []
        assert call(reopened, 'presets.get', {'id': preset['id']})['settings'] == original
        assert call(reopened, 'presets.list')['items'][0]['settings'] == original
        changed = copy.deepcopy(original)
        changed['fields_by_platform']['channels']['channels_original'] = True
        assert call(reopened, 'settings.save', {'settings': changed})['settings'] == changed
        saved = call(reopened, 'presets.save', {'id': preset['id'], 'name': '各平台默认资料', 'settings': changed})
        assert saved['settings'] == changed
        assert call(reopened, 'settings.read')['settings']['targets'] == original['targets']
        assert call(reopened, 'presets.get', {'id': preset['id']})['settings'] == changed
    finally:
        assert reopened.close(3)
    with session_scope(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(TaskCenterRow)) == 0
        assert session.scalar(select(func.count()).select_from(PublishTaskRow)) == 0


@pytest.mark.parametrize('overrides', [
    {'unknown-platform': {'title': '不能新增平台'}},
    {'bilibili': {'category_choice': 'human:invented'}},
    {'bilibili': {'category_choice': 'human:tech', 'category_id': 7}},
    {'channels': {'channels_original': 'true'}},
    {'douyin': {'channels_original': False}},
    {'bilibili': {'content_declaration_choice': 'none'}},
    {'channels': {'content_declaration_choice': 'personal_opinion'}},
    {'bilibili': {'cover': None}},
    {'channels': {'location': None}},
    {'douyin': {'declaration': None}},
])
def test_unselected_defaults_still_reject_unknown_fields_platforms_and_temporary_grants(overrides):
    settings = defaults()
    settings['fields_by_platform'] = overrides
    with pytest.raises(ValueError):
        validate(settings)


@pytest.mark.parametrize('key', ['distribution_by_platform', 'schedule_by_platform'])
def test_default_fields_do_not_allow_unselected_schedules_or_distribution(key):
    settings = defaults()
    settings['fields_by_platform'] = platform_defaults()
    settings[key]['bilibili'] = ('whole_batch' if key == 'distribution_by_platform' else
        {'start_policy': copy.deepcopy(settings['start_policy']), 'schedule': copy.deepcopy(settings['schedule'])})
    with pytest.raises(ValueError):
        validate(settings)


@pytest.mark.parametrize('stale', ['fields_by_video', 'distribution_by_platform', 'schedule_by_platform'])
def test_remembering_unselected_defaults_does_not_relax_prepare_scope(db_engine, app_paths, tmp_path, stale):
    account = AccountService(db_engine).create('douyin', 'synthetic-scope', '合成账号')
    path = tmp_path / 'self-owned.mp4'
    path.write_bytes(b'offline synthetic media')
    video = FinishedVideoService(db_engine).import_external(path, duration_ms=1000, title='离线标题')
    settings = defaults()
    settings['targets'] = [{'platform': 'douyin', 'account_id': account.id}]
    settings['fields_by_platform'] = platform_defaults()
    api = DesktopApi(db_engine, app_paths, publish_adapters={})
    try:
        call(api, 'settings.save', {'settings': settings})
        params = {'video_ids': [video.id], 'targets': copy.deepcopy(settings['targets']),
                  'common_fields': {}, 'execution_mode': 'publish_now'}
        call(api, 'prepare', params)
        if stale == 'fields_by_video':
            params[stale] = {'unselected-video': {'title': '旧视频草稿'}}
        elif stale == 'distribution_by_platform':
            params[stale] = {'bilibili': 'whole_batch'}
        else:
            params[stale] = {'bilibili': {'execution_mode': 'publish_now', 'scheduled_times': {}}}
        rejection = dispatch(api, 'prepare', params)
        assert rejection['error']['code'] == 'invalid_request'
    finally:
        assert api.close(3)
