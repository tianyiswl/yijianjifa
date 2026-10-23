"""Single-item trusted coordinator; does not enable tasks or select a transport."""
import json
from sqlalchemy import select

from yijianjifa.storage.db import session_scope
from yijianjifa.features.tasks.service import TaskItemRow
from .executor import SubmissionEvidence
from .task_submission_boundary import TaskSubmissionBoundary
from .submission_result import SubmissionResultStore


class SingleItemSubmissionRunner:
    def __init__(self, engine):
        self.engine = engine

    def run(self, task_id, item_id, submit):
        """Call a trusted adapter exactly once with its durable final callback.

        `submit(snapshot, detail, before_final=...)` must verify the frozen
        request against the actual page, and invoke before_final immediately
        before its final click. This is not exposed to the renderer.
        """
        boundary = TaskSubmissionBoundary(self.engine)
        with session_scope(self.engine) as session:
            task, _, item, _, _, expected_digest = boundary._load(session, task_id, item_id)
            snapshot, detail = json.loads(task.snapshot_json), json.loads(item.detail_json)
            rows = list(session.scalars(select(TaskItemRow).where(TaskItemRow.task_id == task_id)))
            if snapshot.get('kind') != 'normal_publish' or len(rows) != len(snapshot.get('items',[])) or detail not in snapshot['items']:
                raise ValueError('ordinary publish item required')
            # Claim must correspond to these exact bytes, not a later revision.
        claim = boundary.claim(task_id, item_id)
        if claim['digest'] != expected_digest:
            raise ValueError('submission changed while claiming')
        final_attempted = False

        def before_final():
            nonlocal final_attempted
            boundary.mark_before_final(task_id, item_id, claim['digest'], owner_token=claim['owner_token'])
            final_attempted = True

        try:
            evidence = submit(snapshot, detail, before_final=before_final)
            if not isinstance(evidence, SubmissionEvidence):
                raise ValueError('adapter returned invalid evidence')
        except Exception:
            evidence = SubmissionEvidence(final_attempted, error_code=(
                'submission_result_unknown' if final_attempted else 'submission_prepare_failed'))
        return SubmissionResultStore(self.engine).record(
            task_id, item_id, owner_token=claim['owner_token'],
            expected_digest=claim['digest'], evidence=evidence)
