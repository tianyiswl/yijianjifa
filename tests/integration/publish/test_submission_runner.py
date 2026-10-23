from concurrent.futures import ThreadPoolExecutor
import pytest

from yijianjifa.features.publish.executor import SubmissionEvidence
from yijianjifa.features.publish.submission_runner import SingleItemSubmissionRunner
from test_task_submission_boundary import seed
from test_submission_result import states


def test_success_callback_marks_unknown_before_final_click(db_engine):
    seed(db_engine)
    def submit(snapshot, detail, *, before_final):
        assert snapshot['items'] == [detail]
        before_final()
        assert states(db_engine) == ['result_unknown'] * 4
        return SubmissionEvidence(True, public_result='published')
    assert SingleItemSubmissionRunner(db_engine).run('T', 'I', submit) == 'published'
    assert states(db_engine) == ['published'] * 4


@pytest.mark.parametrize('after,state', [(False, 'pre_submit_failed'), (True, 'result_unknown')])
def test_transport_exception_is_classified_at_durable_boundary(db_engine, after, state):
    seed(db_engine)
    def submit(snapshot, detail, *, before_final):
        if after:
            before_final()
        raise TimeoutError()
    assert SingleItemSubmissionRunner(db_engine).run('T', 'I', submit) == state
    assert states(db_engine) == [state] * 4


def test_held_tasks_cannot_start_transport(db_engine):
    seed(db_engine, enabled=False)
    def unexpected(*args, **kwargs):
        pytest.fail('disabled task reached transport')
    with pytest.raises(ValueError, match='disabled'):
        SingleItemSubmissionRunner(db_engine).run('T', 'I', unexpected)


def test_competing_workers_only_one_reaches_transport(db_engine):
    seed(db_engine)
    calls = []
    def submit(snapshot, detail, *, before_final):
        calls.append(True)
        before_final()
        return SubmissionEvidence(True)
    def run(_):
        try:
            return SingleItemSubmissionRunner(db_engine).run('T', 'I', submit)
        except ValueError:
            return 'blocked'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ['blocked', 'result_unknown']
    assert len(calls) == 1
