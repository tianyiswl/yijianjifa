"""Exclusive process ownership for the single-node deployment entry point."""

import json
import os
from pathlib import Path


class ServiceProcessLock:
    def __init__(self,database):
        self.owner_pid=os.getpid()
        self.descriptor=None
        path=Path(database).resolve()
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=path.with_name(path.name+'.process.lock')
        try:
            import fcntl
        except ImportError:
            raise RuntimeError('云配音服务需在支持系统文件锁的 Linux 或 macOS 服务器运行。') from None
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        descriptor=os.open(self.path,flags,0o600)
        try:
            os.set_inheritable(descriptor,False)
            os.fchmod(descriptor,0o600)
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError):
            os.close(descriptor)
            raise RuntimeError('该云配音数据库已由另一服务进程使用，请保持单实例运行。') from None
        self.descriptor=descriptor
        # Informational only: the operating-system lock, not the PID text, grants ownership.
        os.ftruncate(descriptor,0)
        os.write(descriptor,str(self.owner_pid).encode('ascii'))

    def owned(self):
        return self.descriptor is not None and self.owner_pid==os.getpid()

    def close(self):
        descriptor,self.descriptor=self.descriptor,None
        if descriptor is not None:
            if self.owner_pid==os.getpid():
                import fcntl
                fcntl.flock(descriptor,fcntl.LOCK_UN)
            # A forked child must not unlock the parent's inherited open-file description.
            os.close(descriptor)
        # Never unlink: changing the inode could let another process bypass a live lock.

    def __del__(self):
        try:
            self.close()
        except (OSError,ImportError):
            pass


class LockedApplication:
    def __init__(self,app,lock):
        self.app,self.lock=app,lock

    def __call__(self,environ,start_response):
        if not self.lock.owned():
            payload=json.dumps({'error':{'code':'invalid_process_owner',
                'message':'服务进程配置不正确，请经营方使用单实例且关闭预加载。','details':None}},ensure_ascii=False).encode()
            start_response('503 Service Unavailable',[('Content-Type','application/json; charset=utf-8'),
                ('Content-Length',str(len(payload))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')])
            return [payload]
        return self.app(environ,start_response)

    def close(self):
        self.lock.close()
