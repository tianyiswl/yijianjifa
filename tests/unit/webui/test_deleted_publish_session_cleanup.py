"""Real V2 delete/worker/ledger flow, with browser IO fully synthetic."""
import json
from datetime import datetime, timedelta
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
from yijianjifa.webui.chromium_publish import account_lock
from test_chromium_multi_publish import browser


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('synthetic worker did not reach expected state')


@pytest.mark.parametrize('fail_close_once', [False, True])
def test_user_deletes_final_parked_task_releases_page_on_owner_and_can_publish_next_task(
        browser, db_engine, app_paths, monkeypatch, tmp_path, fail_close_once):
    from yijianjifa.webui import chromium_publish as legacy
    payload, pages, events, _ = browser
    contexts, close_attempts = [], []

    def restore(context, *args):
        contexts.append(context)
        if len(contexts) == 1:
            context.pages[0].post_challenge = True
            original = context.close

            def close():
                close_attempts.append(threading.get_ident())
                if fail_close_once and len(close_attempts) == 1:
                    raise RuntimeError('synthetic close failure')
                return original()

            context.close = close

    monkeypatch.setattr(legacy, 'restore_account_session', restore)
    bridge = ChromiumMultiPublishAdapter(db_engine, app_paths)
    api = DesktopApi(db_engine, app_paths, publish_adapters={'douyin': bridge})

    def call(operation, params=None, request_id='read'):
        return api.dispatch(json.dumps({'v': 1, 'request_id': request_id,
            'operation': operation, 'params': params or {}}), trusted_origin=True)

    def make_task(source, intent):
        video = FinishedVideoService(db_engine).import_external(source, duration_ms=1000, title='测试')
        scheduled_at = (datetime.fromisoformat(payload['scheduled_at']) +
                        timedelta(hours=2 if intent == 'next-plan' else 0)).isoformat()
        response = call('multi_publish.prepare', {'video_ids': [video.id],
            'targets': [{'account_id': payload['account_id'], 'platform': 'douyin'}],
            'common_fields': {}, 'execution_mode': 'platform_schedule',
            'allow_new_request': intent == 'next-plan',  # Same grant as an explicitly created daily batch.
            'scheduled_times': {video.id: scheduled_at}}, intent)
        assert response['ok'], response
        confirmed = call('multi_publish.confirm', {'prepared_id': response['data']['prepared_id'],
            'intent_id': intent, 'consent_partial': False}, 'save-' + intent)
        assert confirmed['ok'], confirmed
        return confirmed['data']['id']

    def start(task_id, request_id):
        detail = call('multi_publish.detail', {'id': task_id})['data']
        ids = [delivery['id'] for delivery in detail['deliveries']]
        checked = call('multi_publish.execution.check', {'id': task_id, 'delivery_ids': ids})['data']
        if checked['summary']['ready'] != 1:
            raise AssertionError(json.dumps(checked, ensure_ascii=False))
        result = call('multi_publish.execution.start', {'id': task_id, 'delivery_ids': ids,
            'digest': checked['digest'], 'consent': True, 'consent_partial': False}, request_id)
        assert result['ok'], (result, checked)
        return ids[0]

    def state(task_id):
        return call('multi_publish.detail', {'id': task_id})['data']['deliveries'][0]['state']

    try:
        task_id = make_task(Path(payload['video_path']), 'first-plan')
        delivery_id = start(task_id, 'start-first')
        wait_for(lambda: state(task_id) == 'result_unknown' and bridge.interventions())
        with session_scope(db_engine) as session:
            attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
            original_final, original_digest = attempt.final_json, attempt.request_digest
            assert original_final
        assert account_lock(payload['account_id']).locked()
        assert bridge.account_available({'account_id': payload['account_id']}) is False
        with pytest.raises(ValueError, match='original host worker thread'):
            bridge.cancel_delivery_session(delivery_id)
        # A cleanup job without committed user cancellation cannot discard an
        # unknown-result page, even when invoked on its actual owner worker.
        cleanup_checked = threading.Event()
        cancel_session = bridge.cancel_delivery_session

        def checked_cleanup(target):
            try:
                return cancel_session(target)
            finally:
                cleanup_checked.set()

        monkeypatch.setattr(bridge, 'cancel_delivery_session', checked_cleanup)
        api._multi_publish.cancel_session(delivery_id)
        assert cleanup_checked.wait(2)
        assert close_attempts == [] and len(pages) == 1  # No user deletion means keep original page.
        original_session = next(iter(bridge.sessions.values()))

        deleted = call('task.delete', {'id': task_id}, 'delete-first')
        assert deleted['ok'], deleted
        # Cleanup retries must run even without a new publish job to drive them.
        wait_for(lambda: not account_lock(payload['account_id']).locked())
        assert not bridge.interventions()
        assert original_session.final and original_session.page is None and original_session.context is None
        assert original_session.request_id is None and original_session.broker.status()['request'] is None
        assert len(close_attempts) == (2 if fail_close_once else 1)
        assert set(close_attempts) == {events[0][2]} and close_attempts[0] != threading.get_ident()
        with session_scope(db_engine) as session:
            row = session.get(PublishDeliveryRow, delivery_id)
            attempt = session.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id == delivery_id))
            assert row.hidden and row.cancel_requested
            assert attempt.final_json == original_final and attempt.request_digest == original_digest
            assert session.get(TaskItemRow, row.item_id).state == 'result_unknown'
            assert session.get(PublishHistoryRow, row.history_id).state == 'result_unknown'
        assert len([event for event in events if event[0] == 'final']) == 1

        source = tmp_path / 'next-owned.mp4'
        source.write_bytes(b'separate-synthetic-video')
        next_task = make_task(source, 'next-plan')
        start(next_task, 'start-next')
        wait_for(lambda: state(next_task) == 'platform_scheduled')
        assert len(pages) == 2 and len([event for event in events if event[0] == 'final']) == 2
        assert state(task_id) == 'result_unknown'  # The old task was never retried or relabelled.
    finally:
        assert api.close(5)
