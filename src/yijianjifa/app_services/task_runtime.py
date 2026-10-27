"""Own local queue execution independently of WebView documents."""
import threading
import uuid


class TaskRuntime:
    def __init__(self, service, *, handoff=None):
        self._service = service
        self._lock = threading.RLock()
        self._thread = None
        self._closed = False
        self._wake = threading.Event()
        self._epoch = uuid.uuid4().hex
        self._revision = 0
        self._snapshot = None
        self._worker_failed = False
        self._startup_task_ids = frozenset(service.hold_after_restart() or ())
        self._handoff = handoff
        self.daily_handoff = None
        if handoff is not None:service.queue.on_settled = self.handoff

    def handoff(self, task_id):
        allowed=getattr(self,'can_start',None)
        if allowed and not allowed():return
        with self._lock:
            if self._closed:return
        snapshot = self._service.queue.get(task_id).snapshot
        if snapshot.get('daily_publish'):
            if self.daily_handoff is not None:
                return self.daily_handoff.reconcile(task_id)
            return
        if not snapshot.get('publish_after'):return
        import json
        from yijianjifa.storage.db import session_scope
        from yijianjifa.storage.models import Setting
        try:
            result=self._handoff.reconcile(task_id)
        except Exception:
            result={'tasks':[],'blocked':[{'indices':[],'code':'post_mix_review_required'}]}
        with session_scope(self._handoff.engine) as session:
            key='mix-publish-status:'+task_id
            row=session.get(Setting,key)
            if row is None:session.add(Setting(key=key,value_json=json.dumps(result)))
            else:row.value_json=json.dumps(result)
        return result

    def start(self):
        with self._lock:
            if self._closed:
                return False
            self._wake.set()
            if self._thread is not None and self._thread.is_alive():
                return True
            self._worker_failed = False
            self._thread = threading.Thread(target=self._run, name='p011-mix-queue', daemon=False)
            self._thread.start()
            return True

    def _run(self):
        while True:
            signaled = self._wake.wait(.5)
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
            try:
                if signaled:
                    self._service.queue.run_once_until_idle()
                if self.daily_handoff is not None:
                    self.daily_handoff.recheck_serial()
            except Exception:
                # Never emit renderer stderr, filesystem paths or credentials to JS.
                with self._lock:
                    self._worker_failed = True

    def poll(self, *, epoch=None, after=None):
        with self._lock:
            tasks = [dict(id=t.id, state=t.state, quantity=t.quantity,
                          completed=t.completed_count, succeeded=t.success_count,
                          failed=t.failure_count, position=t.position, created_at=t.created_at,
                          pause_requested=t.pause_requested,
                          has_error=bool(t.last_error)) for t in self._service.tasks()]
            current = {'tasks':tasks, 'worker_failed':self._worker_failed}
            if current != self._snapshot:
                self._snapshot = current
                self._revision += 1
            changed = epoch != self._epoch or after != self._revision
            result = dict(epoch=self._epoch, revision=self._revision, changed=changed)
            if changed:
                # Return a fresh value; consumers must not mutate cached snapshots.
                result.update(tasks=[dict(t) for t in tasks], worker_failed=self._worker_failed)
            return result

    def recovery(self):
        """Describe held work from this startup; reading never starts the worker."""
        from yijianjifa.features.mixing.serial_gate import has_flow_marker
        with self._lock:
            return {'tasks': [dict(id=t.id, state=t.state, quantity=t.quantity,
                                   completed=t.completed_count, succeeded=t.success_count,
                                   failed=t.failure_count)
                              for t in self._service.tasks()
                              if t.id in self._startup_task_ids
                              and (t.state == 'waiting_publish' or t.state in {'paused', 'interrupted'}
                                   and (t.completed_count < t.quantity or has_flow_marker(t.snapshot)))]}

    def close(self, timeout=0):
        """Stop future work, finish the active item, and report whether exit is safe.

        Caller keeps the native event loop alive while False; no daemon/background
        continuation after app exit. Ordinary page changes never call close.
        """
        with self._lock:
            self._closed = True
            self._wake.set()
            for task in self._service.tasks():
                if task.state in {'waiting', 'running', 'waiting_publish'}:
                    self._service.queue.request_pause(task.id)
            thread = self._thread
        if thread:
            thread.join(timeout)
        return thread is None or not thread.is_alive()
