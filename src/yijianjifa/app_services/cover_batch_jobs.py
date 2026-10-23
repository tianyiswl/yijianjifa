"""One cancellable cover batch per app, with bounded in-memory status receipts."""
import copy
import hashlib
import json
import re
import secrets
import threading
from .finished_covers import CoverBatchCancelled


class CoverBatchJobs:
    def __init__(self,covers):
        self.covers=covers;self.lock=threading.RLock();self.jobs={};self.thread=None;self.closed=False

    def start(self,p):
        if type(p) is not dict or set(p)!={'id','token','video_ids'}:raise ValueError('invalid cover batch')
        if type(p['id']) is not str or not 1<=len(p['id'])<=64 or type(p['token']) is not str or not re.fullmatch('[a-f0-9]{48}',p['token']):raise ValueError('invalid cover batch')
        ids=p['video_ids']
        if type(ids) is not list or not 1<=len(ids)<=200 or any(type(v) is not str or not 1<=len(v)<=64 for v in ids) or len(set(ids))!=len(ids):raise ValueError('invalid video selection')
        payload=copy.deepcopy(p);key=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.closed:raise ValueError('封面服务已关闭。')
            for jid,job in self.jobs.items():
                if job['key']==key and job['state'] not in ('failed','cancelled'):return self.status(jid)
            if self.thread and self.thread.is_alive():raise ValueError('已有批量封面正在处理，请稍候或取消。')
            while len(self.jobs)>=16:self.jobs.pop(next(iter(self.jobs)))
            jid=secrets.token_hex(24)
            job=dict(id=jid,key=key,state='running',done=0,total=len(ids),cancel=threading.Event())
            self.jobs[jid]=job
            self.thread=threading.Thread(target=self._run,args=(job,payload),name='p011-cover-batch',daemon=False)
            self.thread.start()
            return self.status(jid)

    def _run(self,job,p):
        def progress(done,total):
            with self.lock:job.update(done=done,total=total)
        try:
            result=self.covers.stage_for_videos(p['id'],p['token'],p['video_ids'],progress=progress,cancelled=job['cancel'].is_set)
            with self.lock:job.update(state='completed',result=result)
        except CoverBatchCancelled:
            with self.lock:job['state']='cancelled'
        except Exception:
            with self.lock:job.update(state='failed',message='批量封面未完成，本批新副本已撤回；请检查素材、时间或存储空间后重试。')

    def status(self,jid):
        with self.lock:
            if type(jid) is not str or jid not in self.jobs:raise ValueError('封面任务不存在或已过期。')
            return copy.deepcopy({k:v for k,v in self.jobs[jid].items() if k not in ('key','cancel')})

    def cancel(self,jid):
        with self.lock:
            self.status(jid);job=self.jobs[jid]
            if job['state'] in ('running','cancelling'):
                job['cancel'].set();job['state']='cancelling'
            return self.status(jid)

    def close(self,timeout=0):
        with self.lock:
            self.closed=True
            for jid in self.jobs:self.cancel(jid)
            thread=self.thread
        if thread:thread.join(timeout)
        return thread is None or not thread.is_alive()
