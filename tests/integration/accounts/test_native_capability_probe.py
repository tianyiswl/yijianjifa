import pytest
from yijianjifa.webui.douyin_capability import ProbeObservation,accepted_path


def page(**changes):
    value={'identity':dict(uid='123',uid_after='123',status=0,profile_status=0,after_status=0,nickname='fixture'),
           'path':'/creator-micro/content/upload','extension':False,'commerce':False,'upload_success':False,'replace_upload':False,'upload_busy':False,'scopes':[]}
    value.update(changes);return value


def test_upload_is_once_and_never_replaces_existing_form():
    probe=ProbeObservation('123')
    with pytest.raises(ValueError):probe.consume_upload(page(extension=True))
    assert not probe.uploaded
    probe.consume_upload(page())
    with pytest.raises(ValueError):probe.consume_upload(page())


def test_probe_requires_upload_success_and_scopes_remain_independent():
    p=ProbeObservation('123');p.consume_upload(page())
    p.observe(page(extension=True,commerce=True,scopes=['本地','国内']))
    assert p.result()['upload_complete'] is False
    assert p.result()['scope_options']==[]
    p.observe(page(path='/creator-micro/content/post/video',extension=True,commerce=True,upload_success=True))
    assert p.result()['upload_complete'] is True
    assert p.result()['scope_options']==[]
    p.observe(page(extension=True,commerce=True,scopes=['国内']))
    assert p.result()['scope_options']==['national']


def test_identity_change_invalidates_all_probe_evidence():
    p=ProbeObservation('123');p.consume_upload(page());p.observe(page(path='/creator-micro/content/post/video',extension=True,commerce=True,upload_success=True))
    changed=page();changed['identity']['uid_after']='456'
    with pytest.raises(ValueError):p.observe(changed)
    assert p.result() is None
    p.observe(page(path='/creator-micro/content/post/video',extension=True,upload_success=True))
    assert p.result() is None


def test_file_chooser_limited_to_exact_platform_and_upload_paths():
    assert accepted_path('https://creator.douyin.com/creator-micro/content/upload')
    for url in ('https://evil.test/creator-micro/content/upload','https://creator.douyin.com/creator-micro/home','file:///tmp/file','https://user@creator.douyin.com/creator-micro/content/upload'):
        assert not accepted_path(url)


def test_automatic_probe_finishes_only_after_stable_ready_observations(monkeypatch):
    import sys
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_capability import DouyinCapabilityWindow
    monkeypatch.setitem(sys.modules,'WebKit',SimpleNamespace(WKContentWorld=SimpleNamespace(defaultClientWorld=lambda:None)))
    calls=[];finished=[]
    w=object.__new__(DouyinCapabilityWindow)
    w.probe=SimpleNamespace(ready=True,scopes={'local','national'},uploaded=True)
    w.ready_since=None;w.stable_result=None;w.stable_count=0;w.value={'ready':True};w.expected_uid='777'
    w.window=SimpleNamespace(setTitle_=lambda title:None)
    w.view=SimpleNamespace(callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_=lambda *args:calls.append(args))
    w.finish=lambda value,error:finished.append((value,error))
    w.advance_probe();assert not finished and len(calls)==1
    w.advance_probe();assert finished==[({'ready':True},None)]
