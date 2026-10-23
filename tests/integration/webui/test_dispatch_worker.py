import threading
import yijianjifa.app_services as module


def worker_type():
    cls=getattr(module,'DispatchWorker',None)
    assert cls is not None,'bounded background dispatcher is missing'
    return cls


def test_dispatch_does_not_block_caller_and_rejects_over_capacity():
    cls=worker_type();worker=cls(capacity=1);entered=threading.Event();release=threading.Event();done=threading.Event();results=[]
    main=threading.get_ident()
    def slow():
        assert threading.get_ident()!=main
        entered.set();assert release.wait(2);return 'done'
    assert worker.submit(slow,lambda value:(results.append(value),done.set()))
    assert entered.wait(1)
    assert worker.submit(lambda:'extra',results.append) is False
    release.set();assert done.wait(1);assert results==['done']
    worker.close();assert worker.submit(lambda:'after',results.append) is False


def test_exception_response_does_not_leak_details():
    worker=worker_type()();done=threading.Event();results=[]
    def bad():raise RuntimeError('Token=secret /session/private')
    worker.submit(bad,lambda value:(results.append(value),done.set()))
    assert done.wait(1);assert 'secret' not in str(results)
    assert results[0]['error']['code']=='internal_error'
    worker.close()


def test_close_waits_for_pending_delivery_before_resources_can_be_released():
    worker=worker_type()();entered=threading.Event();release=threading.Event();closed=threading.Event();results=[];errors=[]
    def deliver(value):
        entered.set();release.wait(3);results.append(value)
    assert worker.submit(lambda:'completed',deliver)
    assert entered.wait(1)
    def shutdown():
        try:worker.close(wait=True)
        except Exception as exc:errors.append(exc)
        finally:closed.set()
    thread=threading.Thread(target=shutdown);thread.start()
    try:
        assert not closed.wait(.1),'shutdown returned while a dispatch delivery was still active'
        assert worker.submit(lambda:'late',results.append) is False
    finally:
        release.set();thread.join(3);worker.close()
    assert not errors
    assert closed.is_set() and results==['completed']


def test_bridge_close_waits_for_active_work_and_revokes_future_requests():
    from yijianjifa.webui.cocoa_read_bridge import CocoaAppBridge
    entered=threading.Event();release=threading.Event();closed=threading.Event();responses=[];writes=[];errors=[]
    class Service:
        def dispatch(self,raw,*,trusted_origin=False):
            if not trusted_origin:return {'ok':False}
            if raw=='active':entered.set();release.wait(3)
            writes.append(raw);return {'ok':True}
    bridge=CocoaAppBridge(Service(),'');view=object();bridge._gate.bind(view)
    def submit(raw):return bridge._worker.submit(lambda:bridge._gate.receive(raw,view=view,main_frame=True,url='about:blank'),responses.append)
    assert submit('active');assert entered.wait(1)
    def shutdown():
        try:bridge.close(wait=True)
        except Exception as exc:errors.append(exc)
        finally:closed.set()
    thread=threading.Thread(target=shutdown);thread.start()
    try:
        assert not closed.wait(.1),'bridge closed before active work completed'
        assert submit('late') is False
    finally:
        release.set();thread.join(3);bridge.close()
    assert not errors
    assert closed.is_set() and writes==['active'] and responses==[{'ok':True}]
    assert bridge._gate.receive('after',view=view,main_frame=True,url='about:blank')=={'ok':False}
