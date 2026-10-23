import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.tasks.service import TaskCenterService, TaskIssueRow


def test_issue_is_redacted_before_persistence(db_engine):
    center=TaskCenterService(db_engine)
    center.register_task('redaction',kind='make',name='local fixture',state='running',snapshot_json='{}')
    center.record_issue(task_id='redaction',state='failed',title='failure password=FAKE_PASSWORD',
                        reason='Cookie: sid=FAKE_COOKIE; token=FAKE_SECOND\naccess_token=FAKE_TOKEN',
                        next_action='retry',error_code='LOCAL_ERROR',
                        evidence={'final_action_triggered':False,'selector_state':'password=FAKE_NESTED','cookie':'FAKE_EVIDENCE'})
    with session_scope(db_engine) as session:
        row=session.scalar(select(TaskIssueRow).where(TaskIssueRow.task_id=='redaction'))
        stored=json.dumps({c.name:getattr(row,c.name) for c in TaskIssueRow.__table__.columns},default=str)
    assert 'FAKE_' not in stored
    issue=center.latest_issue('redaction')
    assert issue.error_code=='LOCAL_ERROR' and issue.next_action=='retry'
    assert issue.evidence=={'final_action_triggered':False,'selector_state':'[REDACTED]'}
    assert center.get('redaction').state=='failed'
