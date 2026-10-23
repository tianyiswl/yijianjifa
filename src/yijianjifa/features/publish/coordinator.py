"""One finite, serial adapter step per call. Parked sessions yield the worker.

Trusted host adapters validate identity and permission before submit. A parked
adapter MUST return a bounded private context and release its execution thread;
resume operates on that same session through resume(), never submit() again.
Reconciliation after a durable final marker uses reconcile(), never submit().
"""
from dataclasses import dataclass
import re
from .account_wait import AccountBusy


ERROR_CODES=frozenset({'adapter_failed','account_mismatch','permission_missing','configuration_required','verification_required',
    'platform_rejected','timeout','recovery_unavailable','result_unknown','schedule_mismatch',
    'kuaishou_author_statement_required','kuaishou_declaration_options_unverified',
    'channels_declaration_options_unverified','channels_original_unavailable','channels_original_video_too_short',
    'image_count_mismatch','image_order_unverified','image_upload_unverified','image_fields_unverified',
    'image_receipt_invalid','image_declaration_unverified','image_type_unverified'})
ERROR_STAGES=frozenset({'adapter_setup','account_validation','page_preparation','verification_resume',
    'final_validation','submission_result','receipt_recording','page_open','session_restore','page_navigation','account_observation','verification_observation',
    'upload_preparation','form_preparation','schedule_preparation'})
ERROR_CATEGORIES=frozenset({'dependency_missing','account_busy','session_unavailable','worker_thread_mismatch',
    'timeout','page_context_unavailable','identity_mismatch','resource_unavailable','execution_failed',
    'browser_launch_failed','profile_in_use','page_closed','existing_draft','form_changed','javascript_read_failed'})


def failure_category(error):
    """Normalize failures without retaining exception text, paths or credentials."""
    from yijianjifa.features.accounts.service import AccountIdentityMismatch
    if isinstance(error,ImportError):return 'dependency_missing'
    if isinstance(error,AccountIdentityMismatch):return 'identity_mismatch'
    if isinstance(error,TimeoutError) or (type(error).__module__.startswith('playwright.') and type(error).__name__=='TimeoutError'):
        return 'timeout'
    message=error.args[0] if len(error.args)==1 and type(error.args[0]) is str else ''
    if message=='account busy':return 'account_busy'
    if message=='Playwright requires its original host worker thread':return 'worker_thread_mismatch'
    if type(error).__module__.startswith('greenlet') and 'Cannot switch to a different thread' in message:return 'worker_thread_mismatch'
    if message in {'identity-bound session unavailable','invalid session archive','session identity or platform mismatch',
            'invalid session state','invalid session cookie','invalid session storage'}:return 'session_unavailable'
    if type(error).__module__.startswith('playwright.') and any(marker in message for marker in ('Execution context was destroyed','Cannot find context with specified id',
            'Target page, context or browser has been closed')):return 'page_context_unavailable'
    if isinstance(error,OSError):return 'resource_unavailable'
    return 'execution_failed'


@dataclass(frozen=True)
class AdapterValidation:
    observed_uid: str
    authorized: bool


@dataclass(frozen=True)
class AdapterOutcome:
    state: str
    context: dict | None = None
    scheduled_at: str | None = None
    receipt_reference: dict | None = None
    error_code: str | None = None
    error_stage: str | None = None
    error_category: str | None = None


def outcome_evidence(outcome, request_digest):
    if outcome.scheduled_at is not None and (type(outcome.scheduled_at) is not str or len(outcome.scheduled_at)>64):
        raise ValueError('invalid scheduled receipt time')
    reference=outcome.receipt_reference or {}
    if type(reference) is not dict or set(reference)-{'photo_id','resource_id','work_id','publish_id','kind','item_id'}:
        raise ValueError('unsupported receipt reference')
    if {'kind','item_id'} & set(reference):
        if (set(reference) != {'kind','item_id'} or reference['kind'] != 'image_post' or
                type(reference['item_id']) is not str or not re.fullmatch(r'[0-9]{1,32}',reference['item_id'])):
            raise ValueError('invalid image receipt reference')
    if any(type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value) for value in reference.values()):
        raise ValueError('invalid receipt reference')
    if outcome.error_code is not None and (type(outcome.error_code) is not str or outcome.error_code not in ERROR_CODES):raise ValueError('unsupported normalized error code')
    evidence={'state':outcome.state,'request_digest':request_digest,'scheduled_at':outcome.scheduled_at,
            'receipt_reference':reference,'error_code':outcome.error_code}
    if outcome.error_stage is not None or outcome.error_category is not None:
        if (outcome.state not in {'pre_submit_failed','result_unknown'} or outcome.error_code!='adapter_failed'
                or type(outcome.error_stage) is not str or outcome.error_stage not in ERROR_STAGES
                or type(outcome.error_category) is not str or outcome.error_category not in ERROR_CATEGORIES):
            raise ValueError('unsupported normalized failure diagnostic')
        evidence.update(error_stage=outcome.error_stage,error_category=outcome.error_category)
    return evidence


class PublishCoordinator:
    def __init__(self, service):
        self.service=service
        self.boundary=service.deliveries
        self._contexts={}  # Owner tokens are host-only and never API-returned.

    def run_once(self, task_id, delivery_ids=None):
        if not self.service._execution_lock.acquire(blocking=False):return None
        try:return self._run_once(task_id,delivery_ids)
        finally:self.service._execution_lock.release()

    def _run_once(self, task_id, delivery_ids):
        dto=self.service.get_task(task_id)
        if dto.get('legacy_version') or dto['paused']:
            return None
        selected={d['id'] for d in dto['deliveries']}
        if delivery_ids is not None:
            if type(delivery_ids) is not list or any(type(d) is not str for d in delivery_ids) or len(set(delivery_ids))!=len(delivery_ids) or not set(delivery_ids)<=selected:
                raise ValueError('invalid authorized delivery subset')
            selected=set(delivery_ids)
        for child in dto['deliveries']:
            if child['id'] not in selected or child['state']!='waiting':continue
            if self.service.check_schedule(child['id'])!='due' or not child['can_submit']:continue
            try:adapter=self.service.catalog.transport_for_content(child['platform'],child.get('content_type','video'))
            except Exception:adapter=None  # Setup failures retain the normal durable diagnostic path.
            available=getattr(adapter,'account_available',None)
            if callable(available) and not available(child):
                from .execution_log import append_event
                append_event(self.service.engine,child['id'],child['attempt'],'account.wait',{'busy':True})
                continue
            try:claim=self.boundary.claim(child['id'])
            except ValueError:continue
            result=self._execute(child['id'],claim)
            if result is not None:return result
        return None

    def _execute(self, delivery_id, claim, *, resume=False):
        payload=claim['payload']
        stage='adapter_setup'
        def before():
            nonlocal stage
            stage='final_validation'
            # Validate the actual session again at the final boundary, including
            # after a parked login action resumes. DB identity is not observation.
            check=adapter.validate_target(payload,payload,payload['execution_mode'])
            if not isinstance(check,AdapterValidation) or check.authorized is not True or check.observed_uid!=payload['platform_user_id']:
                raise ValueError('final account identity or permission is not verified')
            self.boundary.mark_before_final(delivery_id,claim['digest'],owner_token=claim['owner_token'])
            stage='submission_result'
        try:
            adapter=self.service.catalog.transport_for_content(payload['platform'],payload.get('content_type','video'))
            binder=getattr(adapter,'bind_delivery',None)
            if callable(binder):binder(digest=claim['digest'],delivery_id=delivery_id,task_id=claim['task_id'])
            if resume:
                stage='verification_resume'
                outcome=adapter.resume(payload,context=claim['context'],before_final=before)
            else:
                stage='account_validation'
                validation=adapter.validate_target(payload,payload,payload['execution_mode'])
                if isinstance(validation,AdapterOutcome) and validation.state=='paused_auth':
                    outcome=validation
                elif (not isinstance(validation,AdapterValidation) or validation.authorized is not True or
                        validation.observed_uid!=payload['platform_user_id']):
                    outcome=AdapterOutcome('pre_submit_failed',error_code='account_mismatch')
                else:
                    stage='page_preparation'
                    outcome=adapter.submit(payload,context=None,before_final=before)
            stage='receipt_recording'
            if not isinstance(outcome,AdapterOutcome):raise ValueError('invalid adapter outcome')
            if outcome.state=='paused_auth' and (outcome.context is None or not callable(getattr(adapter,'resume',None))):
                raise ValueError('adapter must yield a resumable parked session')
            state=self.boundary.record(delivery_id,claim['digest'],owner_token=claim['owner_token'],outcome=outcome)
            if outcome.context is not None:
                self._contexts[delivery_id]={**claim,'context':outcome.context}
            else:
                self._contexts[delivery_id]=claim
            return state
        except Exception as error:
            if isinstance(error,AccountBusy) and not resume and stage in {'account_validation','page_preparation'}:
                if self.boundary.defer_account_busy(delivery_id,claim['digest'],owner_token=claim['owner_token']):
                    from .execution_log import append_event
                    append_event(self.service.engine,delivery_id,claim['attempt'],'account.wait',{'busy':True})
                    return None
            # Whether final was crossed is decided by the durable journal.
            category=failure_category(error)
            if stage in {'account_validation','verification_resume','page_preparation'}:
                try:
                    diagnostic=getattr(adapter,'failure_diagnostic',None)
                    value=diagnostic(error) if callable(diagnostic) else None
                    if (type(value) is dict and set(value)=={'error_stage','error_category'}
                            and type(value['error_stage']) is str and value['error_stage'] in ERROR_STAGES
                            and type(value['error_category']) is str and value['error_category'] in ERROR_CATEGORIES):
                        stage=value['error_stage'];category=value['error_category']
                except Exception:pass  # Diagnostic failures cannot mask the actual outcome.
            self.boundary.record(delivery_id,claim['digest'],owner_token=claim['owner_token'],
                outcome=AdapterOutcome('pre_submit_failed',error_code='adapter_failed',error_stage=stage,error_category=category))
            return self.service.delivery_state(delivery_id)
        finally:
            self.boundary.workers.finished(claim['owner_hash'])

    def resume_verification(self, delivery_id):
        if not self.service._execution_lock.acquire(blocking=False):raise ValueError('coordinator worker is busy')
        try:return self._resume_verification(delivery_id)
        finally:self.service._execution_lock.release()

    def _resume_verification(self, delivery_id):
        claim=self._contexts.get(delivery_id)
        if claim is None:raise ValueError('same-session verification context unavailable; recover conservatively')
        if self.service.delivery_state(delivery_id)!='paused_auth':
            raise ValueError('post-final verification requires reconciliation')
        self.boundary.workers.resumed(claim['owner_hash'])
        return self._execute(delivery_id,claim,resume=True)

    def reconcile_delivery(self, delivery_id):
        # Durable attempt/receipt plus trusted stopped-owner proof survive UI
        # reopen. Reconciliation remains available after the new-use term ends.
        if not self.service._execution_lock.acquire(blocking=False):raise ValueError('coordinator worker is busy')
        try:return self.service.recover_delivery(delivery_id)['state']
        finally:self.service._execution_lock.release()
