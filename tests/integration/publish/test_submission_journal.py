import pytest
from yijianjifa.features.publish.submission_journal import SubmissionJournal, SubmissionAlreadyAttempted
from yijianjifa.storage.db import create_engine_for


def test_committed_boundary_survives_new_engine(db_engine):
    journal=SubmissionJournal(db_engine)
    journal.mark_before_final('task:item', 'a'*64)
    reopened=create_engine_for(db_engine.url.database)
    try:
        with pytest.raises(SubmissionAlreadyAttempted):
            SubmissionJournal(reopened).mark_before_final('task:item', 'a'*64)
        assert SubmissionJournal(reopened).get('task:item')['state']=='result_unknown'
    finally: reopened.dispose()


def test_changed_request_cannot_reuse_attempt_key(db_engine):
    journal=SubmissionJournal(db_engine)
    journal.mark_before_final('task:item', 'a'*64)
    with pytest.raises(SubmissionAlreadyAttempted):
        journal.mark_before_final('task:item', 'b'*64)


def test_other_items_are_independent(db_engine):
    journal=SubmissionJournal(db_engine)
    journal.mark_before_final('task:one', 'a'*64)
    journal.mark_before_final('task:two', 'b'*64)
    assert journal.get('task:two')['request_digest']=='b'*64


@pytest.mark.parametrize('digest',['', 'secret', 'x'*64])
def test_invalid_digest_cannot_be_recorded(db_engine,digest):
    journal=SubmissionJournal(db_engine)
    with pytest.raises(ValueError): journal.mark_before_final('task:item',digest)
    assert journal.get('task:item') is None


def test_two_connections_only_one_can_mark(db_engine):
    from concurrent.futures import ThreadPoolExecutor
    def mark(_):
        try:
            SubmissionJournal(db_engine).mark_before_final('race:item','c'*64)
            return True
        except SubmissionAlreadyAttempted: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(mark,range(2)))==[False,True]


def test_adapter_crash_after_marker_blocks_replay(db_engine):
    from yijianjifa.features.publish.platforms.douyin.normal import DouyinNormalAdapter, NormalPublishRequest
    from yijianjifa.features.publish.executor import PublishExecutor
    class SimulatedCrash(BaseException): pass
    class Page:
        clicks=0
        def text_content(self, selector): return 'user-a'
        def is_visible(self, selector): return False
        def fill(self,*args): pass
        def set_input_files(self,*args): pass
        def click(self,*args): self.clicks+=1; raise SimulatedCrash()
    journal=SubmissionJournal(db_engine);page=Page()
    request=NormalPublishRequest('fixture.mp4','','','',expected_platform_user_id='user-a')
    boundary=lambda: journal.mark_before_final('confirmed:item','d'*64)
    with pytest.raises(SimulatedCrash):
        DouyinNormalAdapter(page).submit_normal(request,before_final=boundary)
    ev=DouyinNormalAdapter(page).submit_normal(request,before_final=boundary)
    assert page.clicks==1
    assert PublishExecutor().classify(ev)=='result_unknown'


def test_journal_error_never_allows_click(db_engine):
    from yijianjifa.features.publish.platforms.douyin.normal import DouyinNormalAdapter, NormalPublishRequest
    class Page:
        clicks=0
        def text_content(self,selector): return 'user-a'
        def is_visible(self,selector): return False
        def fill(self,*args): pass
        def set_input_files(self,*args): pass
        def click(self,*args): self.clicks+=1
    def failed_commit(): raise OSError('private database details')
    page=Page()
    ev=DouyinNormalAdapter(page).submit_normal(NormalPublishRequest('fixture.mp4','','','',expected_platform_user_id='user-a'),before_final=failed_commit)
    assert page.clicks==0
    assert ev.error_code=='submission_result_unknown'
    assert 'private' not in repr(ev)
