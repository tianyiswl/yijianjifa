import pytest
from yijianjifa.webui.douyin_scheduled import persist_refreshed_sessions


def test_refresh_only_saves_matching_account_and_original_session(db_engine,app_paths):
    from yijianjifa.features.accounts.service import AccountService, AccountIdentityMismatch
    from yijianjifa.features.accounts.session_store import SessionStore
    service=AccountService(db_engine);store=SessionStore(app_paths.data_dir/'profiles')
    a=service.create('douyin','123','合成账号A');b=service.create('douyin','456','合成账号B')
    original_a=service.attach_session(a.id,store,b'original-a')
    original_b=service.attach_session(b.id,store,b'original-b')
    newer_b=service.attach_session(b.id,store,b'newer-login-b')
    before=set(store.root.rglob('session-*.bin'))
    pending={a.id:dict(original_ref=original_a,platform_user_id='123',session_blob=b'refreshed-a'),
             b.id:dict(original_ref=original_b,platform_user_id='456',session_blob=b'stale-b')}
    persist_refreshed_sessions(db_engine,app_paths,pending)
    refreshed_a=service.get(a.id).session_ref
    assert refreshed_a!=original_a and store.get(a.id,ref=refreshed_a)==b'refreshed-a'
    assert store.get(a.id,ref=original_a)==b'original-a'
    assert service.get(b.id).session_ref==newer_b and store.get(b.id,ref=newer_b)==b'newer-login-b'
    after=set(store.root.rglob('session-*.bin'))
    assert len(after-before)==1
    assert pending=={}
    with pytest.raises(AccountIdentityMismatch):
        persist_refreshed_sessions(db_engine,app_paths,{a.id:dict(original_ref=refreshed_a,platform_user_id='456',session_blob=b'wrong')})
    assert set(store.root.rglob('session-*.bin'))==after
    assert service.get(a.id).session_ref==refreshed_a
    assert service.get(b.id).session_ref==newer_b


def test_refresh_flush_occurs_after_failed_batch_without_retry(db_engine,app_paths):
    from test_scheduled_publish import seed
    from yijianjifa.app_services.scheduled_publish import ScheduledPublish
    from yijianjifa.features.publish.executor import SubmissionEvidence
    seed(db_engine,app_paths);events=[]
    def transport(*args,**kw):events.append('submit');return SubmissionEvidence(False,error_code='prepare_failed')
    transport.flush_sessions=lambda:events.append('save-session')
    service=ScheduledPublish(db_engine,app_paths,transport)
    service.start('T',service.check('T')['digest'],consent=True);service.worker.join(5)
    assert events==['submit','save-session']
    assert not service.check('T')['available']
    service.close()


def test_verified_receipt_refreshes_session_without_resubmitting():
    import time
    from yijianjifa.webui.douyin_scheduled import ScheduledWindow
    window=ScheduledWindow.__new__(ScheduledWindow)
    window.finished=False;window.busy=False;window.view=object()
    window.session_observer=None;window.started=time.monotonic()
    window.receipt_confirmed=True
    events=[]
    window.complete=lambda **kwargs:events.append(kwargs)
    window.capture_session=lambda on_saved:on_saved()
    window.observe()
    assert events==[{'confirmed':True}]
