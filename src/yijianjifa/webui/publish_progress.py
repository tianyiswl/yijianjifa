"""Transient task progress shared by every client view. No platform secrets."""
import threading
import time

class PublishProgress:
    def __init__(self):self._lock=threading.Lock();self._task=None
    def start(self,task_id,total):
        with self._lock:self._task=dict(id=task_id,total=total,index=0,title='',stage='任务已开始',state='running',running=True,updated_at=time.time())
    def update(self,task_id,*,stage,title=None,index=None):
        with self._lock:
            if not self._task or self._task['id']!=task_id:return
            self._task.update(stage=stage,updated_at=time.time())
            if title is not None:self._task['title']=title
            if index is not None:self._task['index']=index
    def finish(self,task_id,state):
        with self._lock:
            if not self._task or self._task['id']!=task_id:return
            self._task.update(state=state,running=False,updated_at=time.time(),stage={'platform_scheduled':'平台已接收定时安排','result_unknown':'提交结果待核对，不会自动重发','paused_auth':'账号验证需要处理','pre_submit_failed':'提交前失败，请查看具体原因','paused':'已暂停','cancelled':'已取消'}.get(state,'执行已结束，请查看任务记录'))
    def status(self):
        with self._lock:return {'task':dict(self._task) if self._task else None}
progress=PublishProgress()
