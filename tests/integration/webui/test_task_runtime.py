import threading
from types import SimpleNamespace

import yijianjifa.app_services as module
from yijianjifa.features.mixing.queue import MixQueue


def runtime_type():
    cls=getattr(module,'TaskRuntime',None)
    assert cls is not None,'UI-independent task runtime not implemented'
    return cls


class Executor:
    def __init__(self):self.entered=threading.Event();self.release=threading.Event();self.calls=0
    def render_item(self,*args):
        self.calls+=1;self.entered.set();assert self.release.wait(3)


def make_service(engine, executor):
    from sqlalchemy import select
    from yijianjifa.storage.db import session_scope
    from yijianjifa.features.mixing.queue import MixTask
    queue=MixQueue(engine,executor)
    def tasks():
        with session_scope(engine) as s:return [queue._to_record(r) for r in s.scalars(select(MixTask).order_by(MixTask.position))]
    def hold():
        queue.recover_interrupted()
        for t in tasks():
            if t.state=='waiting':queue.request_pause(t.id)
    return SimpleNamespace(queue=queue,tasks=tasks,hold_after_restart=hold)


def test_snapshot_cursor_recovers_disconnect_without_paths(db_engine):
    cls=runtime_type();service=make_service(db_engine,Executor());runtime=cls(service)
    first=runtime.poll();assert first['tasks']==[]
    task=service.queue.enqueue({'quantity':1,'path':'/private/session/secret'})
    update=runtime.poll(epoch=first['epoch'],after=first['revision'])
    assert update['revision']>first['revision']
    assert update['tasks'][0]['id']==task.id
    assert 'secret' not in str(update)
    assert runtime.poll(epoch=update['epoch'],after=update['revision'])['changed'] is False
    assert runtime.poll(epoch='old-process',after=999)['changed'] is True
    assert runtime.close(.1)


def test_repeated_start_does_not_duplicate_worker_and_close_pauses_remaining(db_engine):
    cls=runtime_type();executor=Executor();service=make_service(db_engine,executor);runtime=cls(service)
    task=service.queue.enqueue({'quantity':2})
    runtime.start();assert executor.entered.wait(2)
    runtime.start();runtime.start()
    assert runtime.close(.01) is False
    executor.release.set();assert runtime.close(3) is True
    assert executor.calls==1
    assert service.queue.get(task.id).state=='paused'
    assert runtime.start() is False


def test_startup_holds_waiting_and_interrupted_tasks(db_engine):
    cls=runtime_type();executor=Executor();service=make_service(db_engine,executor)
    task=service.queue.enqueue({'quantity':1})
    runtime=cls(service)
    assert service.queue.get(task.id).state=='paused'
    assert executor.calls==0
    assert runtime.poll()['tasks'][0]['completed']==0
    assert runtime.close(.1)


def test_submit_while_worker_finishes_idle_pass_is_not_lost(db_engine):
    cls=runtime_type();executor=Executor();executor.release.set()
    service=make_service(db_engine,executor);runtime=cls(service)
    service.queue.enqueue({'quantity':1})
    original=service.queue.run_once_until_idle
    at_return=threading.Event();allow_return=threading.Event();second_done=threading.Event()
    passes=[]
    def boundary():
        original();passes.append(1)
        if len(passes)==1:at_return.set();assert allow_return.wait(2)
        else:second_done.set()
    service.queue.run_once_until_idle=boundary
    runtime.start();assert at_return.wait(2)
    service.queue.enqueue({'quantity':1});runtime.start();allow_return.set()
    try:
        assert second_done.wait(1),'new task wake-up was lost at idle boundary'
        assert executor.calls==2
    finally:runtime.close(2)
