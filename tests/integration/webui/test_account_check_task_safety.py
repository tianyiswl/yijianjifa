"""Account observations update the library without granting task execution."""
import json
import threading
import time

from sqlalchemy import select

from test_multi_publish_api import fixture, saved
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.account_access import AccountAccess
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow, PublishDeliveryRow
from yijianjifa.features.publish.history import PublishHistoryRow
from yijianjifa.features.tasks.service import TaskItemRow
from yijianjifa.storage.db import session_scope


def test_startup_check_updates_account_but_never_resumes_paused_publish(fixture, db_engine, app_paths):
    old_api, account, _, adapter = fixture
    batch = saved(fixture, 'check-only')
    assert old_api.close(2)
    service = AccountService(db_engine)
    service.attach_session(account.id, SessionStore(app_paths.data_dir / 'profiles'), b'synthetic archive')
    checked = threading.Event()

    def provider(*args):
        raise AssertionError('Startup must never ask for an interactive login')

    def check_session(account_id, cancel):
        assert account_id == account.id and not cancel.is_set()
        checked.set()
        return {'state': 'valid', 'platform_user_id': '111'}

    provider.check_session = check_session
    provider.publish_adapters = {'douyin': adapter}
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    with session_scope(db_engine) as session:
        delivery = session.scalar(select(PublishDeliveryRow).where(PublishDeliveryRow.task_id == batch['id']))
        session.get(TaskItemRow, delivery.item_id).state = 'paused_auth'
        session.get(PublishHistoryRow, delivery.history_id).state = 'paused_auth'

    def frozen_targets():
        with session_scope(db_engine) as session:
            return [(d.id, d.request_digest, d.snapshot_json, session.get(TaskItemRow, d.item_id).state,
                     a.final_json, a.result_json, a.context_json, a.owner_hash)
                    for d, a in session.execute(select(PublishDeliveryRow, DeliveryAttemptRow)
                        .join(DeliveryAttemptRow, DeliveryAttemptRow.delivery_id == PublishDeliveryRow.id)
                        .where(PublishDeliveryRow.task_id == batch['id']).order_by(PublishDeliveryRow.id))]

    before = frozen_targets()
    try:
        api.start_local_checks()
        assert checked.wait(2), 'Startup should actually check saved account sessions'
        deadline = time.monotonic() + 2
        result = None
        while time.monotonic() < deadline:
            result = api.dispatch(json.dumps({'v': 1, 'request_id': 'account-observation',
                'operation': 'account.get', 'params': {'id': account.id}}), trusted_origin=True)
            if result['ok'] and result['data']['login_check'] is not None:
                break
            time.sleep(.01)
        assert result['ok'] and result['data']['login_check']['state'] == 'valid', result
        assert result['data']['login_check']['checked_at']
        assert 'session_ref' not in json.dumps(result)
        assert frozen_targets() == before
        assert adapter.calls == []
    finally:
        assert api.close(3)


def test_account_state_filters_match_the_current_observed_status(db_engine, app_paths):
    service = AccountService(db_engine)
    store = SessionStore(app_paths.data_dir / 'profiles')
    accounts = {}
    for state in ('valid', 'session_missing', 'login_required', 'unavailable', 'identity_mismatch', 'saved_only'):
        account = service.create('channels', state, state)
        ref = service.attach_session(account.id, store, ('synthetic ' + state).encode())
        if state != 'saved_only':
            assert service.observe_login_check(account.id, platform_user_id=state, session_ref=ref, state=state)
        accounts[state] = account.id

    access = AccountAccess(db_engine)
    def filtered(state):
        return {row['id'] for row in access.list('', '', '', state, 0, 20)['items']}

    assert filtered('relogin') == {accounts['session_missing'], accounts['login_required']}
    assert filtered('pending') == {accounts['unavailable'], accounts['identity_mismatch']}
    assert filtered('ok') == {accounts['valid'], accounts['saved_only']}
