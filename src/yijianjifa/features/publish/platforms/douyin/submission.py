"""Offline page-contract boundary. No live selectors, browser or network transport.

Once the final action is attempted, every transport/readback error is ambiguous.
A caller must persist that boundary before enabling a production transport.
"""
from yijianjifa.features.publish.executor import SubmissionEvidence


def submit_page(page, selectors, request, prepare, validate=lambda: None, *, before_final=None):
    final_attempted = False
    try:
        def identity_error():
            expected = request.expected_platform_user_id
            if not isinstance(expected, str) or not expected.strip():
                return 'identity_unverified'
            if page.text_content(selectors.IDENTITY) != expected:
                return 'identity_mismatch'
            return None

        if page.is_visible(selectors.AUTH_REQUIRED):
            return SubmissionEvidence(False, auth_required=True, error_code='auth_required')
        error = identity_error() or validate()
        if error:
            return SubmissionEvidence(False, error_code=error)
        # A prior receipt on this page cannot establish this request's success.
        if page.is_visible(selectors.PUBLIC_SUCCESS) or page.is_visible(selectors.SCHEDULE_CONFIRMATION):
            return SubmissionEvidence(False, error_code='stale_submission_receipt')
        page.set_input_files(selectors.VIDEO, request.video_path)
        prepare(request)
        page.fill(selectors.SCHEDULE, request.scheduled_time_text or '')
        if page.is_visible(selectors.AUTH_REQUIRED):
            return SubmissionEvidence(False, auth_required=True, error_code='auth_required')
        error = identity_error() or validate()
        if error:
            return SubmissionEvidence(False, error_code=error)
        final_attempted = True
        if before_final is not None:
            before_final()
        page.click(selectors.FINAL_SUBMIT)
        schedule = None
        if request.scheduled_time_text and page.is_visible(selectors.SCHEDULE_CONFIRMATION):
            # The page contract returns only the normalized receipt time, not
            # arbitrary toast text containing a matching substring.
            if page.text_content(selectors.SCHEDULE_CONFIRMATION) == request.scheduled_time_text:
                schedule = request.scheduled_time_text
        published = 'published' if page.is_visible(selectors.PUBLIC_SUCCESS) else None
        return SubmissionEvidence(True, platform_schedule_confirmation=schedule, public_result=published)
    except Exception:
        return SubmissionEvidence(final_attempted, error_code=(
            'submission_result_unknown' if final_attempted else 'submission_prepare_failed'))
