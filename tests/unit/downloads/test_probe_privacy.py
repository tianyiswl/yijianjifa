import importlib.util
import pytest
from pathlib import Path
spec=importlib.util.spec_from_file_location('download_probe',Path(__file__).parents[3]/'tools/download_probe.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

def test_probe_metadata_whitelist_rejects_source_controlled_text():
    data={'format':{'duration':'12.5','size':'100','tags':{'comment':'https://secret/?token=SECRET'}},'streams':[{'codec_type':'video','codec_name':'h264','width':576,'height':1024,'tags':{'title':'SECRET'}},{'codec_type':'audio','codec_name':'aac','sample_rate':'44100'},{'codec_type':'SECRET','codec_name':'https://secret/?token=SECRET'}]}
    safe=probe.safe_ffprobe(data)
    assert safe['duration_seconds']==12.5
    assert safe['streams'][0]['width']==576
    assert 'SECRET' not in repr(safe) and 'tags' not in repr(safe)

def test_blocked_probe_never_captures_authentication_pixels():
    source=Path(probe.__file__).read_text(encoding='utf-8')
    assert '.screenshot(' not in source
    assert 'Page.captureScreenshot' not in source

@pytest.mark.parametrize('late_summary_link',[False,True])
def test_verification_failure_writes_signals_but_no_pixels(tmp_path,monkeypatch,capsys,late_summary_link):
    import json,sys
    from collections import Counter
    from yijianjifa.features.downloads.contracts import DownloadError
    touched=[]
    outside=tmp_path.with_name(tmp_path.name+'-source')
    outside.write_bytes(b'synthetic source archive')
    class CDP:
        def send(self,name,*a):
            touched.append(name)
            assert name!='Page.captureScreenshot'
        def on(self,*a): pass
    class Page:
        url='https://www.douyin.com/video/1'
        def screenshot(self,*a,**kw): raise AssertionError('authentication pixels')
        def locator(self,*a): return self
        def inner_text(self,**kw): return '扫码登录 验证 https://secret/?token=SECRET'
    class Context:
        def new_cdp_session(self,*a): return CDP()
    class Lease:
        page=Page();context=Context();closed=False;verification_event=None
        last_phase='read';last_failure=None;blocked_requests=[];observed_statuses=[];read_evidence={}
        proxy=type('Proxy',(),{'failure_counts':Counter(),'traffic':Counter()})()
        def close(self): self.closed=True
    lease=Lease()
    class Pool:
        def __init__(self,*a,**kw): pass
        def acquire(self,*a): return lease
    class Provider:
        def __init__(self,*a): pass
        def resolve(self,*a,**kw):
            if late_summary_link: (tmp_path/'summary.json').symlink_to(outside)
            raise DownloadError('verification_required')
        def close(self): pass
    monkeypatch.setattr(probe,'BUILD_ROOT',tmp_path.parent)
    monkeypatch.setattr(probe,'DownloadBrowserPool',Pool)
    monkeypatch.setattr(probe,'DouyinProvider',Provider)
    monkeypatch.setattr(sys,'argv',['probe','--source','https://www.douyin.com/video/1','--output',str(tmp_path)])
    probe.main()
    if late_summary_link:
        assert outside.read_bytes()==b'synthetic source archive'
        assert (tmp_path/'summary.json').is_symlink() and lease.closed
        assert json.loads(capsys.readouterr().out)['error_code']=='unsafe_source'
        assert list(tmp_path.iterdir())==[tmp_path/'summary.json']
        return
    raw=(tmp_path/'summary.json').read_text(encoding='utf-8');report=json.loads(raw)
    assert report['visible_signals']['login'] and report['error_code']=='verification_required'
    assert 'SECRET' not in raw and list(tmp_path.iterdir())==[tmp_path/'summary.json']
    assert lease.closed


def test_atomic_report_refuses_child_symlinks_and_preserves_outside_bytes(tmp_path):
    import pytest
    from yijianjifa.features.downloads.contracts import DownloadError
    source=tmp_path/'source';source.mkdir();outside=source/'archive';outside.write_bytes(b'synthetic archive')
    root=tmp_path/'out';root.mkdir()
    for name in ('summary.json','verification.json'):
        (root/name).symlink_to(outside)
        with pytest.raises(DownloadError): probe.write_report(root,name,{'state':'blocked'})
        assert outside.read_bytes()==b'synthetic archive' and (root/name).is_symlink()
    assert sorted(p.name for p in root.iterdir())==['summary.json','verification.json']


def test_control_symlink_is_not_read_or_deleted(tmp_path):
    import pytest
    from yijianjifa.features.downloads.contracts import DownloadError
    outside=tmp_path/'source';outside.write_bytes(b'{"action":"resume"}')
    root=tmp_path/'out';root.mkdir();(root/'control.json').symlink_to(outside)
    with pytest.raises(DownloadError): probe.consume_control(root)
    assert outside.read_bytes()==b'{"action":"resume"}' and (root/'control.json').is_symlink()


def test_report_atomic_replace_does_not_truncate_hardlinked_source(tmp_path):
    import os,json
    outside=tmp_path/'source';outside.write_bytes(b'keep source')
    root=tmp_path/'out';root.mkdir();os.link(outside,root/'summary.json')
    probe.write_report(root,'summary.json',{'state':'blocked'})
    assert outside.read_bytes()==b'keep source'
    assert json.loads((root/'summary.json').read_text(encoding='utf-8'))=={'state':'blocked'}
    assert [p.name for p in root.iterdir()]==['summary.json']


def test_control_is_bounded_and_only_regular_control_file_consumed(tmp_path,monkeypatch):
    import os
    if not all(hasattr(os,flag) for flag in ('O_NOFOLLOW','O_NONBLOCK')):
        # The human-run POSIX probe is not bundled in the Windows client.
        # An unsupported host must fail before reading or deleting controls.
        control=tmp_path/'control.json';content=b'{"action":"resume"}';control.write_bytes(content)
        monkeypatch.setattr(probe.os,'open',lambda *a,**kw:pytest.fail('unsupported probe opened control'))
        monkeypatch.setattr(probe.os,'read',lambda *a,**kw:pytest.fail('unsupported probe read control'))
        with pytest.raises(AttributeError,match='O_NOFOLLOW|O_NONBLOCK'):probe.consume_control(tmp_path)
        assert control.read_bytes()==content and list(tmp_path.iterdir())==[control]
        return
    for content,expected in [(b'{"action":"resume"}',{'action':'resume'}),(b'x'*513,None),(b'invalid',None)]:
        (tmp_path/'control.json').write_bytes(content)
        assert probe.consume_control(tmp_path)==expected
        assert not (tmp_path/'control.json').exists()
    assert probe.consume_control(tmp_path) is None


@pytest.mark.parametrize('missing_flag',['O_NOFOLLOW','O_NONBLOCK'])
def test_unsupported_control_host_never_reads_or_deletes(tmp_path,monkeypatch,missing_flag):
    content=b'{"action":"resume"}';control=tmp_path/'control.json';control.write_bytes(content)
    monkeypatch.delattr(probe.os,missing_flag,raising=False)
    monkeypatch.setattr(probe.os,'open',lambda *a,**kw:pytest.fail('unsupported probe opened control'))
    monkeypatch.setattr(probe.os,'read',lambda *a,**kw:pytest.fail('unsupported probe read control'))
    with pytest.raises(AttributeError,match='O_NOFOLLOW|O_NONBLOCK'):probe.consume_control(tmp_path)
    assert control.read_bytes()==content and list(tmp_path.iterdir())==[control]
