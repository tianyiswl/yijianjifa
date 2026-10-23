from yijianjifa.features.publish.executor import PublishExecutor, SubmissionEvidence


def test_final_action_triggered_without_confirmation_becomes_result_unknown():
    executor = PublishExecutor()
    ev = SubmissionEvidence(final_action_triggered=True)
    state = executor.classify(ev)
    assert state == "result_unknown"
    assert executor.retry_allowed(state) is False


def test_pre_submit_failure_is_retryable_after_user_fixes():
    executor = PublishExecutor()
    ev = SubmissionEvidence(final_action_triggered=False, error_code="location_missing")
    assert executor.classify(ev) == "pre_submit_failed"
    assert executor.retry_allowed("pre_submit_failed") is True


def test_explicit_platform_schedule_confirmation_has_distinct_state():
    executor = PublishExecutor()
    ev = SubmissionEvidence(final_action_triggered=True, platform_schedule_confirmation="2026-09-24 09:00")
    assert executor.classify(ev) == "platform_scheduled"
    assert executor.retry_allowed("platform_scheduled") is False


def test_explicit_public_success_is_published():
    executor = PublishExecutor()
    ev = SubmissionEvidence(final_action_triggered=True, public_result="published")
    assert executor.classify(ev) == "published"


def test_auth_pause_only_blocks_affected_account():
    executor = PublishExecutor()
    executor.register_batch("B1", ["A1", "A2"])
    executor.pause_account("B1", "A1", reason="auth_required")
    assert executor.account_state("B1", "A1") == "paused_auth"
    assert executor.account_state("B1", "A2") == "ready"
