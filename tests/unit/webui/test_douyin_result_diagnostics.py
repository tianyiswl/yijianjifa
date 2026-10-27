"""结果回读只记录有限门槛，不能把列表可读误认为平台接收。"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_chromium_multi_publish import browser, adapter
from test_douyin_publish_diagnostics import saved_events, local_page


@pytest.mark.parametrize(('rows_kind', 'stable', 'gate', 'count', 'state'), [
    ('empty', 2, 'no_exact_text', 0, 'missing'),
    ('text_only', 2, 'no_result_rows', 0, 'missing'),
    ('wrong_time', 2, 'no_schedule_match', 0, 'missing'),
    ('duplicate', 2, 'ambiguous_schedule', 2, 'ambiguous'),
    ('published', 2, 'review_unconfirmed', 1, 'published'),
    ('unrecognized', 2, 'review_unconfirmed', 1, 'other'),
    ('scheduled', 1, 'unstable', 1, 'scheduled'),
    ('scheduled', 2, 'confirmed', 1, 'scheduled'),
    ('reviewing', 2, 'confirmed', 1, 'reviewing'),
])
def test_receipt_gate_distinguishes_missing_text_time_status_and_stability(
        browser, db_engine, app_paths, rows_kind, stable, gate, count, state):
    payload, _, _, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        session = worker._session(payload)
        session.phase = 'result'; session.final = True; session.uploaded = True
        form = worker._form(payload, query=True)
        expected = datetime.strptime(form['scheduled_time_text'], '%Y-%m-%d %H:%M').strftime('%Y年%m月%d日 %H:%M')
        review = {'scheduled': '定时发布中', 'reviewing': '审核中', 'published': '已发布'}.get(rows_kind, 'PRIVATE_COOKIE_SECRET')
        rows = [] if rows_kind in {'empty', 'text_only'} else [{'time': expected if rows_kind != 'wrong_time' else 'wrong', 'review': review}]
        if rows_kind == 'duplicate': rows = [{'time': expected, 'review': '审核中'}] * 2
        session.last_rows = rows if stable == 2 else None
        session.stable = 1 if stable == 2 else 0
        raw = {'identity': identity, 'path': '/creator-micro/content/manage', 'ready': True,
               'query': form['title_text'], 'matches': rows, 'diagnostic': {'text_match_count': 1 if rows_kind == 'text_only' else len(rows)}}
        worker._readback(session, raw)
        checks = saved_events(db_engine)[-1]['checks']
        assert checks.get('receipt_gate') == gate
        assert checks['schedule_match_count'] == count and checks['review_state'] == state
        assert checks['stable_reads'] == stable
        assert checks['result_count'] == len(rows)
        assert 'SECRET' not in json.dumps(checks)
    finally:
        worker.close()


def test_post_final_timeout_records_failed_receipt_gate_and_one_click(browser, db_engine, app_paths, monkeypatch):
    from yijianjifa.webui import chromium_multi_publish as module
    payload, pages, actions, identity = browser
    worker = adapter(db_engine, app_paths, payload)
    try:
        assert worker.validate_target(payload, payload, payload['execution_mode']).authorized
        page = pages[0]; original = page.evaluate
        def evaluate(code, args=None):
            if page.result and 'p011ResultNeedsSearch' in code:
                return {'identity': identity, 'ready': True, 'path': '/creator-micro/content/manage',
                        'query': '测试', 'matches': [], 'diagnostic': {'text_match_count': 0}}
            return original(code, args)
        monkeypatch.setattr(page, 'evaluate', evaluate)
        clock = [0.0]
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(page, 'wait_for_timeout', lambda ms: clock.__setitem__(0, clock[0] + ms / 1000))
        worker.step_timeout = .5
        outcome = worker.submit(payload, context=None, before_final=lambda: None)
        assert outcome.state == 'result_unknown' and outcome.error_code == 'timeout'
        saved = saved_events(db_engine)
        click = next(event['checks'] for event in saved if event['step'] == 'douyin.final_click' and event['checks']['state'] == 'completed')
        assert click.get('click_reported_attempted') is True
        result = [event['checks'] for event in saved if event['step'] == 'douyin.result_readback'][-1]
        assert result.get('receipt_gate') == 'no_exact_text' and result['stable_reads'] == 2
        uploads = len([action for action in actions if action[0] == 'upload'])
        again = worker.reconcile(payload, context=None)
        assert again.state == 'result_unknown'
        assert len([action for action in actions if action[0] == 'final']) == 1
        assert len([action for action in actions if action[0] == 'upload']) == uploads == 1
    finally:
        worker.close()


def test_local_dom_exposes_exact_text_count_without_exporting_text(local_page):
    from yijianjifa.webui import chromium_multi_publish as module, chromium_publish as legacy
    local_page.set_content('<input placeholder="搜索作品" value="合成标题"><div><p>合成标题 合成正文</p><p>删除作品 2026年10月30日 15:00 定时发布中</p></div>')
    local_page.evaluate('window.p011ResultWait=Date.now()-4000;window.p011ResultNeedsSearch=false')
    code = module.READ_RESULT_WITH_DIAGNOSTICS.replace(legacy.READ_IDENTITY,
        'const identity={uid:"111",uid_after:"111",status:0,after_status:0,profile_status:0};')
    raw = legacy.evaluate_script(local_page, code, {'expected_uid': '111', 'title_text': '合成标题', 'body_text': '合成正文'})
    assert raw['diagnostic'].get('text_match_count') == 1
    assert raw['matches'] == [{'time': '2026年10月30日 15:00', 'review': '定时发布中'}]
    assert all(type(value) in (int, bool) for value in raw['diagnostic'].values())


def test_saved_result_diagnostics_reject_injected_strings_and_invalid_counters():
    from yijianjifa.app_services.multi_publish_dto import safe_identity_execution_events
    raw = {'ready': True, 'final_started': True, 'page_kind': 'manage', 'result_count': True,
           'schedule_match_count': 10001, 'stable_reads': -1, 'review_state': 'SECRET',
           'receipt_gate': 'SECRET', 'text_match_count': 'SECRET', 'message': 'SECRET'}
    saved = safe_identity_execution_events([{'step': 'douyin.result_readback', 'checks': raw}])[0]['checks']
    assert saved == {'ready': True, 'final_started': True, 'page_kind': 'manage'}
    clicked = safe_identity_execution_events([{'step': 'douyin.final_click', 'checks': {
        'state': 'completed', 'phase': 'result', 'final_started': True, 'click_reported_attempted': 'SECRET',
        'source_line': 'SECRET', 'cookie': 'SECRET'}}])[0]['checks']
    assert clicked == {'state': 'completed', 'phase': 'result', 'final_started': True}


def test_result_summary_names_the_failed_gate_without_returning_page_data():
    from yijianjifa.app_services import multi_publish_dto as dto
    summary = getattr(dto, 'result_diagnostic_summary', None)
    assert callable(summary), '需要可复制的中文结果回读诊断摘要'
    text = summary([{'step': 'douyin.result_readback', 'checks': {'final_started': True,
        'receipt_gate': 'no_schedule_match', 'result_count': 1, 'schedule_match_count': 0,
        'stable_reads': 2, 'review_state': 'missing', 'query': 'SECRET'}}])
    assert '排期' in text and '识别1条' in text and '排期匹配0条' in text and 'SECRET' not in text
