import pytest
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.publish.executor import PublishExecutor
from yijianjifa.features.publish.platforms.douyin.normal import DouyinNormalAdapter, NormalPublishRequest
from yijianjifa.features.publish.platforms.douyin.commerce import DouyinCommerceAdapter, CommercePublishRequest


class BoundaryPage:
    def __init__(self, failure=None):
        self.failure=failure; self.events=[]; self.clicked=False
    def text_content(self, selector):
        if selector.endswith('identity'):
            return 'other' if self.failure=='wrong_identity' else 'user-a'
        return None
    def is_visible(self, selector):
        if self.failure=='readback' and self.clicked: raise TimeoutError('private cookie')
        if selector.endswith('auth-required'):
            return self.failure=='expired' and bool(self.events)
        return False
    def fill(self, selector, value):
        self.events.append(('fill', selector, value))
        if self.failure=='prepare': raise RuntimeError('private cookie')
    def set_input_files(self, selector, path): self.events.append(('upload', selector, path))
    def click(self, selector):
        self.events.append(('click', selector)); self.clicked=True
        if self.failure=='click': raise TimeoutError('private cookie')
    def commerce_store_available(self, *args): return True
    def favorite_music_ids(self, *args): return []


def submit(kind, page, **kwargs):
    values=dict(video_path='/fixture/video.mp4',title='title',body='body',tags='',
                expected_platform_user_id='user-a',**kwargs)
    if kind=='normal':
        return DouyinNormalAdapter(page).submit_normal(NormalPublishRequest(**values))
    return DouyinCommerceAdapter(page,account_id='local-a',capabilities=CapabilitySnapshot(
        ordinary='available',commerce='available')).submit_commerce(CommercePublishRequest(store_id='s1',**values))


@pytest.mark.parametrize('kind',['normal','commerce'])
@pytest.mark.parametrize('failure,expected', [('prepare','pre_submit_failed'),('click','result_unknown'),('readback','result_unknown'),('expired','paused_auth'),('wrong_identity','pre_submit_failed')])
def test_boundary_never_turns_possible_submission_into_retryable_failure(kind,failure,expected):
    page=BoundaryPage(failure)
    evidence=submit(kind,page)
    assert PublishExecutor().classify(evidence)==expected
    assert 'private' not in repr(evidence)
    if failure in ('wrong_identity','expired','prepare'): assert not page.clicked
    if failure=='wrong_identity': assert page.events==[]
    if failure in ('click','readback'): assert not PublishExecutor.retry_allowed(expected)


@pytest.mark.parametrize('kind',['normal','commerce'])
def test_upload_and_schedule_are_prepared_before_final_action(kind):
    page=BoundaryPage()
    evidence=submit(kind,page,scheduled_time_text='2026-09-28 12:00')
    assert evidence.final_action_triggered
    assert page.events[0][0]=='upload'
    assert any(e[0]=='fill' and e[-1]=='2026-09-28 12:00' for e in page.events)
    assert page.events[-1][0]=='click'


@pytest.mark.parametrize('kind',['normal','commerce'])
def test_identity_switch_during_prepare_stops_final_action(kind):
    class SwitchingPage(BoundaryPage):
        def text_content(self, selector):
            if selector.endswith('identity') and self.events: return 'other'
            return super().text_content(selector)
    page=SwitchingPage()
    ev=submit(kind,page)
    assert ev.error_code=='identity_mismatch'
    assert not page.clicked


@pytest.mark.parametrize('kind',['normal','commerce'])
def test_old_receipt_blocks_before_upload(kind):
    class StalePage(BoundaryPage):
        def is_visible(self, selector): return selector.endswith('public-success')
    page=StalePage()
    ev=submit(kind,page)
    assert ev.error_code=='stale_submission_receipt'
    assert page.events==[]


@pytest.mark.parametrize('kind',['normal','commerce'])
def test_schedule_receipt_requires_exact_normalized_value(kind):
    class ReceiptPage(BoundaryPage):
        def is_visible(self, selector):
            return self.clicked and selector.endswith('schedule-confirmation')
        def text_content(self, selector):
            if selector.endswith('schedule-confirmation'): return '2026-09-28 12:00:59'
            return super().text_content(selector)
    ev=submit(kind,ReceiptPage(),scheduled_time_text='2026-09-28 12:00')
    assert PublishExecutor().classify(ev)=='result_unknown'
