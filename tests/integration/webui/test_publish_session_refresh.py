from types import SimpleNamespace
import pytest
from yijianjifa.webui.douyin_scheduled import persist_refreshed_sessions


def test_refresh_only_saves_matching_account_and_original_session(monkeypatch,tmp_path):
    import yijianjifa.features.accounts.service as accounts
    import yijianjifa.features.accounts.session_store as stores
    writes=[]
    class Service:
        def __init__(self,engine):pass
        def get(self,account):return SimpleNamespace(session_ref={'A':'old','B':'newer-login'}[account])
        def assert_same_identity(self,account,uid):
            if uid!={'A':'123','B':'456'}[account]:raise ValueError('identity mismatch')
        def attach_session(self,account,store,blob):writes.append((account,blob))
    monkeypatch.setattr(accounts,'AccountService',Service)
    monkeypatch.setattr(stores,'SessionStore',lambda path:object())
    pending={'A':dict(original_ref='old',platform_user_id='123',session_blob=b'refreshed'),
             'B':dict(original_ref='old',platform_user_id='456',session_blob=b'stale')}
    persist_refreshed_sessions(None,SimpleNamespace(data_dir=tmp_path),pending)
    assert writes==[('A',b'refreshed')]
    assert pending=={}
    with pytest.raises(ValueError):
        persist_refreshed_sessions(None,SimpleNamespace(data_dir=tmp_path),{'A':dict(original_ref='old',platform_user_id='456',session_blob=b'wrong')})
    assert len(writes)==1


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
