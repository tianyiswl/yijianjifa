"""Real local encoding and receipts; the platform transport is deliberately offline."""
import json
import subprocess
import platform
from unittest.mock import Mock

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountRow, AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.mixing.ffmpeg_renderer import _probe
from yijianjifa.features.publish.coordinator import AdapterOutcome, AdapterValidation
from yijianjifa.os_integration.resources import ffmpeg_path
from yijianjifa.storage.db import session_scope


class OfflinePlatform:
    platform_id = 'douyin'
    verified_capabilities = {'ordinary', 'platform_schedule', 'result_query'}

    def validate_target(self, target, payload, execution):
        return AdapterValidation(payload['platform_user_id'], True)

    def submit(self, payload, *, context, before_final):
        before_final()
        return AdapterOutcome('platform_scheduled', scheduled_at=payload['scheduled_at'])

    def reconcile(self, payload, *, context):
        raise AssertionError('a successful original attempt needs no query or repeat submission')


@pytest.mark.parametrize('system_voice', [False, True])
def test_chinese_source_is_really_rendered_and_only_its_output_enters_offline_publishing(db_engine, app_paths, tmp_path, monkeypatch, daily_auth_sync, system_voice):
    if system_voice and platform.system() not in ('Windows', 'Darwin'):
        pytest.skip('Native system speech requires Windows or macOS')
    source = tmp_path / '中文素材.mp4'
    subprocess.run([str(ffmpeg_path()), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x120:r=25:d=4', '-c:v', 'libx264', str(source)], check=True, timeout=30)
    original_bytes = source.read_bytes()
    MediaStorageService(db_engine, app_paths, kind='video').import_media(source, keep_copy=True, category_ids=[])
    CopyRepository(db_engine).create(id='local-copy', title='中文本批标题', body='本批正文', tags='#本批标签', voice='你好。' if system_voice else '', full='')
    account = AccountService(db_engine).create('douyin', '123', '离线合成账号')
    with session_scope(db_engine) as s:
        s.get(AccountRow, account.id).session_ref = 'synthetic-session-never-sent'
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': OfflinePlatform()})
    cloud = None
    legacy_receipt = '{"started_at":"2020-01-01T00:00:00+00:00","expires_at":"2021-01-01T00:00:00+00:00"}'
    if system_voice:
        from yijianjifa.app_services.use_term import UseTerm
        from yijianjifa.features.mixing.tts.voice_services import build_voice_services
        from yijianjifa.storage.models import Setting
        with session_scope(db_engine) as s:
            s.add(Setting(key=UseTerm.KEY, value_json=legacy_receipt))
        _, api._mix.tts, cloud = build_voice_services(app_paths, endpoint='')
        cloud.opener = Mock(side_effect=AssertionError('Free system speech must not call a cloud supplier'))
        cloud.vault = Mock()
    daily_auth_sync(api)
    monkeypatch.setattr(api._multi_publish, 'wake', lambda: None)
    params = {'mix': {'quantity': 1, 'total_ms': 2000 if system_voice else 400, 'segment_ms': 400,
                      'spoken_enabled': system_voice, 'full_enabled': False},
              'publishing': {'targets': [{'account_id': account.id, 'platform': 'douyin'}],
                             'mode': 'platform_schedule', 'time': '12:00', 'day_offset': 1, 'interval_hours': 3}}
    try:
        response = api.dispatch(json.dumps({'v': 1, 'request_id': 'real-local-render',
            'operation': 'mix.daily.submit', 'params': params}), trusted_origin=True)
        assert response['ok'], response
        task_id = response['data']['id']
        api._mix.queue.run_once_until_idle()
        assert api._mix.queue.get(task_id).success_count == 1
        status = api._daily.status(task_id)
        assert status['phase'] == 'publishing' and len(status['publish_task_ids']) == 1
        child = status['publish_task_ids'][0]
        deliveries = api._multi_publish.detail(child)['deliveries']
        assert len(deliveries) == 1
        fields = deliveries[0]['effective_fields']
        assert (fields['title'], fields['body'], fields['tags']) == ('中文本批标题', '本批正文', '#本批标签')
        finished = FinishedVideoService(db_engine).get(deliveries[0]['video_id'])
        rendered = _probe(finished.effective_path)
        if system_voice:
            assert float(rendered['format']['duration']) > 0.3
            assert any(stream['codec_type'] == 'audio' for stream in rendered['streams'])
            cloud.opener.assert_not_called()
            cloud.vault.get_password.assert_not_called()
            with session_scope(db_engine) as s:
                assert s.get(Setting, UseTerm.KEY).value_json == legacy_receipt
        else:
            assert 0.36 <= float(rendered['format']['duration']) <= 0.44
        api._multi_publish.coordinator.run_once(child, [deliveries[0]['id']])
        status = api._daily.status(task_id)
        assert (status['phase'], status['accepted'], status['published']) == ('accepted', 1, 0)
        assert source.read_bytes() == original_bytes
    finally:
        api.close(3)
