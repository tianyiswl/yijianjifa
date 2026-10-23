from unittest.mock import Mock
from tools.audit_supplier_voices import audit_voice, validate_plan


def entry(state='pending'):
    return {'id':'doubao:test', 'name':'测试', 'test_text':'测试。', 'task_id':'a'*32, 'state':state}


def test_audit_persists_task_before_submission_and_does_not_store_signed_url():
    row=entry(); snapshots=[]
    provider=Mock()
    def submit(*args):
        assert snapshots[-1]['state']=='submitting'
        assert snapshots[-1]['task_id']==row['task_id']
    provider.submit.side_effect=submit
    provider.query.return_value={'state':'succeeded','url':'https://sample.bytespeech.com/audio?token=PRIVATE','duration_ms':500}
    audit_voice(row, provider, lambda: snapshots.append(dict(row)), max_wait=0)
    assert row['state']=='verified'
    assert provider.submit.call_count==1
    assert 'PRIVATE' not in str(snapshots) and 'url' not in row


def test_resume_unknown_submission_queries_original_task_without_new_charge():
    row=entry('submitting'); provider=Mock()
    provider.query.return_value={'state':'succeeded','url':'https://sample.bytespeech.com/audio','duration_ms':500}
    audit_voice(row,provider,lambda:None,max_wait=0)
    provider.submit.assert_not_called()
    provider.query.assert_called_once_with('a'*32)
    assert row['state']=='verified'


def test_failed_or_unknown_result_is_never_marked_verified_or_resubmitted():
    for result in ({'state':'failed'}, {'state':'running'}):
        row=entry();provider=Mock();provider.submit.side_effect=RuntimeError('do not log PRIVATE')
        provider.query.return_value=result
        audit_voice(row,provider,lambda:None,max_wait=0)
        assert row['state'] in {'failed','unconfirmed'}
        assert provider.submit.call_count==1 and 'PRIVATE' not in str(row)


def test_full_probe_budget_and_duplicate_ids_checked_before_any_provider_request():
    assert validate_plan([entry()],max_characters=3)==3
    for rows, limit in (([entry()],2),([entry(),entry()],100)):
        try:validate_plan(rows,max_characters=limit)
        except ValueError:pass
        else:raise AssertionError('invalid probe plan accepted')
