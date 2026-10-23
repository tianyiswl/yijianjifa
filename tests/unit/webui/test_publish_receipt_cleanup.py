"""A failed browser cleanup cannot invalidate an already observed outcome."""
from types import SimpleNamespace

import pytest

from yijianjifa.features.publish.coordinator import AdapterOutcome
from yijianjifa.webui.chromium_publish_session import ChromiumPublishSessionAdapter


@pytest.mark.parametrize('state',['platform_scheduled','platform_accepted','published'])
@pytest.mark.parametrize('diagnostic_fails',[False,True])
def test_cleanup_error_preserves_receipt(state,diagnostic_fails):
    adapter=object.__new__(ChromiumPublishSessionAdapter)
    session=SimpleNamespace(final=True)
    outcome=AdapterOutcome(state,receipt_reference={'work_id':'42'})
    events=[]
    adapter._advance=lambda s,**kwargs:outcome
    def release(s):
        events.append('close')
        raise RuntimeError('browser transport closed')
    def diagnostic(s,error):
        events.append('diagnostic')
        if diagnostic_fails:raise RuntimeError('diagnostic unavailable')
    adapter._release_page=release
    adapter._failure_diagnostic=diagnostic
    assert adapter._step(session) is outcome
    assert session.final and events==['close','diagnostic']


@pytest.mark.parametrize('state',['paused_auth','result_unknown'])
def test_waiting_original_context_is_not_closed(state):
    adapter=object.__new__(ChromiumPublishSessionAdapter)
    outcome=AdapterOutcome(state)
    adapter._advance=lambda s,**kwargs:outcome
    def release(s):raise AssertionError('original context must be retained')
    adapter._release_page=release
    assert adapter._step(SimpleNamespace(final=True)) is outcome
