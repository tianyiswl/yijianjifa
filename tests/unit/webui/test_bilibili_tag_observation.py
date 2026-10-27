"""Only a native check for this page's pending tag may report topic-only."""
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from test_bilibili_multi_publish import case
from yijianjifa.webui import bilibili_multi_publish as m


MESSAGE = '当前tag为话题专用，不允许自定义添加，可在话题搜索页进行搜索'


def native_check(posting_frame, tag='功能测试', **patch):
    data = dict(code=0, data=dict(code=-1, content=MESSAGE))
    return SimpleNamespace(
        url=patch.get('url', 'https://api.bilibili.com/x/vupre/web/topic/tag/check?' + urlencode({'tag': tag})),
        request=SimpleNamespace(method=patch.get('method', 'GET'), frame=patch.get('frame', posting_frame)),
        json=lambda: patch.get('data', data),
    )


def test_native_topic_rejection_is_bound_to_pending_tag_and_posting_frame(case):
    adapter, payload, page, _, _ = case
    session = adapter._session(payload)
    adapter._open(session)
    page.main_frame = object()
    session.phase = 'prepare'
    session.pending_tag = '功能测试'
    adapter._response(session, native_check(page.main_frame))
    assert session.tag_requires_topic is True
    assert session.final is False
    adapter.close()


@pytest.mark.parametrize('change', [
    'other_tag', 'duplicate_query', 'duplicate_blank_query', 'other_frame', 'post', 'foreign_host', 'http',
    'other_path', 'outer_error', 'inner_success', 'boolean_code', 'other_message',
    'not_pending', 'after_final', 'not_preparing',
])
def test_unrelated_or_uncertain_response_cannot_reject_a_tag(case, change):
    adapter, payload, page, _, _ = case
    session = adapter._session(payload)
    adapter._open(session)
    page.main_frame = object()
    session.phase = 'prepare'
    session.pending_tag = '功能测试'
    patch = {}
    if change == 'other_tag': patch['url'] = native_check(page.main_frame, '其他标签').url
    if change == 'duplicate_query': patch['url'] = native_check(page.main_frame).url + '&tag=other'
    if change == 'duplicate_blank_query': patch['url'] = native_check(page.main_frame).url + '&tag='
    if change == 'other_frame': patch['frame'] = object()
    if change == 'post': patch['method'] = 'POST'
    if change == 'foreign_host': patch['url'] = native_check(page.main_frame).url.replace('api.bilibili.com', 'example.test')
    if change == 'http': patch['url'] = native_check(page.main_frame).url.replace('https:', 'http:')
    if change == 'other_path': patch['url'] = native_check(page.main_frame).url.replace('/tag/check', '/tag/other')
    if change == 'outer_error': patch['data'] = dict(code=-1, data=dict(code=-1, content=MESSAGE))
    if change == 'inner_success': patch['data'] = dict(code=0, data=dict(code=0, content=MESSAGE))
    if change == 'boolean_code': patch['data'] = dict(code=0, data=dict(code=True, content=MESSAGE))
    if change == 'other_message': patch['data'] = dict(code=0, data=dict(code=-1, content='unknown rule'))
    if change == 'not_pending': session.pending_tag = None
    if change == 'after_final': session.final = True
    if change == 'not_preparing': session.phase = 'result'
    adapter._response(session, native_check(page.main_frame, **patch))
    assert getattr(session, 'tag_requires_topic', False) is False
    adapter.close()


@pytest.mark.parametrize('from_posting_frame', [True, False])
def test_embedded_posting_surface_owns_its_tag_response(case, monkeypatch, from_posting_frame):
    adapter, payload, page, _, _ = case
    session = adapter._session(payload)
    adapter._open(session)
    page.main_frame = object()
    posting_frame = object()
    monkeypatch.setattr(adapter, '_frame', lambda _: posting_frame)
    session.phase = 'prepare'
    session.pending_tag = '功能测试'
    adapter._response(session, native_check(posting_frame if from_posting_frame else page.main_frame))
    assert session.tag_requires_topic is from_posting_frame
    adapter.close()


def test_topic_only_tag_stops_before_next_input_without_changing_frozen_fields(case, monkeypatch):
    adapter, payload, page, events, _ = case
    frozen = deepcopy(payload)
    session = adapter._session(payload)
    adapter._open(session)
    session.phase = 'prepare'
    page.main_frame = object()
    f = m.form_contract.validate_form(payload)
    entered = []

    class Control:
        def __init__(self, selector): self.selector = selector
        def count(self): return 0 if self.selector.endswith('.label-item-v2-container') else 1
        def click(self, **kwargs): pass
        def filter(self, **kwargs): return self
        def fill(self, text):
            if self.selector == m.form_contract.TAGS: entered.append(text)
        def press(self, key):
            if self.selector == m.form_contract.TAGS:
                assert key == 'Enter'
                adapter._response(session, native_check(page.main_frame, entered[-1]))

    class Form:
        def locator(self, selector): return Control(selector)
        def get_by_text(self, *args, **kwargs): return Control('text')
        def evaluate(self, script):
            return dict(content_authorization=f['content_authorization'], tags=[],
                        creation_statement_id=f['creation_statement_id'],
                        statement_text=f['creation_statement_name'])

    monkeypatch.setattr(adapter, '_select_statement_option', lambda *args: None)
    page.keyboard = SimpleNamespace(insert_text=lambda value: None)
    page.wait_for_timeout = lambda _: pytest.fail('a confirmed rejection must stop immediately')
    with pytest.raises(ValueError, match='^bilibili_tag_requires_topic$'):
        adapter._select_fields(session, Form(), f)
    assert entered == [f['tags'][0]]
    assert payload == frozen
    assert session.pending_tag is None
    assert not any(name in {'journal', 'final'} for name, _ in events)
    adapter.close()
