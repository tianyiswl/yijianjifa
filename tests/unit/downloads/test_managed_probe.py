import importlib.util
import sqlite3
from pathlib import Path
import pytest
from yijianjifa.features.downloads.contracts import DownloadError, Candidate, ResolvePage

spec=importlib.util.spec_from_file_location('catalog_probe',Path(__file__).parents[3]/'tools/download_probe.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

def test_catalog_default_never_calls_media(tmp_path):
    class Provider:
        def resolve(self,*a,**kw): return ResolvePage((Candidate('douyin:1','douyin','1','s','https://www.douyin.com/video/1'),),None,False,'complete')
        def media(self,*a,**kw): raise AssertionError('unselected media')
    report={'works':[]}
    probe.collect(Provider(),None,tmp_path,report,selected_id=None,max_pages=1,connector=None)
    assert report['catalog']=={'ids':['1'],'count':1,'pages':1,'completion':'complete','has_more':False}
    assert report['state']=='catalog-read'

def test_managed_source_missing_and_wal_do_not_create_or_modify_files(tmp_path):
    from yijianjifa.features.downloads.probe_session import open_managed_sessions
    with pytest.raises(DownloadError): open_managed_sessions(tmp_path)
    assert list(tmp_path.iterdir())==[]
    db=tmp_path/'app.db'; conn=sqlite3.connect(db)
    conn.execute('pragma journal_mode=wal');conn.execute('create table example(x)');conn.commit()
    before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    with pytest.raises(DownloadError): open_managed_sessions(tmp_path)
    assert before=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    conn.close()

def test_readonly_composes_real_service_store_and_missing_default_fails(tmp_path):
    from sqlalchemy import create_engine
    from yijianjifa.features.accounts.service import AccountService, AccountRow
    from yijianjifa.features.accounts.session_store import SessionStore
    from yijianjifa.features.downloads.probe_session import open_managed_sessions
    engine=create_engine('sqlite:///'+str(tmp_path/'app.db'))
    AccountRow.__table__.create(engine)
    service=AccountService(engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','123','Synthetic')
    service.attach_session(account.id,store,b'synthetic-archive')
    before={p.relative_to(tmp_path).as_posix():(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.rglob('*') if p.is_file()}
    sessions,readonly=open_managed_sessions(tmp_path,decoder=lambda blob,uid:[{'name':'test'}])
    with pytest.raises(DownloadError): sessions.default_account_id()
    readonly.dispose()
    service.set_default('douyin',account.id);engine.dispose()
    before={p.relative_to(tmp_path).as_posix():(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.rglob('*') if p.is_file()}
    sessions,readonly=open_managed_sessions(tmp_path,decoder=lambda blob,uid:[{'name':'test'}])
    assert sessions.default_account_id()==account.id
    assert sessions.load(account.id).expected_uid=='123'
    with pytest.raises(Exception):
        with readonly.begin() as c: c.exec_driver_sql('delete from accounts')
    readonly.dispose()
    assert before=={p.relative_to(tmp_path).as_posix():(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_mode) for p in tmp_path.rglob('*') if p.is_file()}

def test_probe_path_and_mode_validation_precedes_writes(tmp_path):
    from types import SimpleNamespace
    args=SimpleNamespace(output=str(tmp_path/'outside'),consumer_data=None,managed_default=True,managed_data=str(tmp_path/'source'),allow_observed_page_init=True,prepare_verification=False,select_id=None,max_pages=1)
    with pytest.raises(DownloadError): probe.validate_args(args)
    assert not (tmp_path/'outside').exists()

def test_only_explicit_selected_work_downloads_and_ffprobe_is_bounded(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    calls=[]
    class Provider:
        registry=object()
        def resolve(self,*a,**kw):
            return ResolvePage(tuple(Candidate('douyin:'+i,'douyin',i,'s','https://www.douyin.com/video/'+i) for i in ('1','2')),None,False,'complete')
        def media(self,candidate,**kw): calls.append(candidate.work_id);return SimpleNamespace(watermark='unknown')
    class Fetcher:
        def __init__(self,*a,**kw): pass
        def download(self,media,path,**kw): path.write_bytes(b'synthetic video')
    monkeypatch.setattr(probe,'SecureFetcher',Fetcher)
    monkeypatch.setattr(probe.subprocess,'check_output',lambda *a,**kw: json.dumps({'format':{'duration':'1','tags':{'secret':'PRIVATE'}},'streams':[]}).encode())
    report={'works':[]}
    probe.collect(Provider(),None,tmp_path,report,selected_id='2',max_pages=1,connector=None)
    assert calls==['2'] and report['works'][0]['bytes']==15 and report['frame_review']=='pending'
    assert 'PRIVATE' not in repr(report)
    with pytest.raises(DownloadError): probe.collect(Provider(),None,tmp_path,{'works':[]},selected_id='3',max_pages=1,connector=None)
    assert calls==['2']

def test_managed_main_wires_explicit_account_and_closes_engine(tmp_path,monkeypatch):
    import sys,json
    from types import SimpleNamespace
    from yijianjifa.features.downloads import probe_session
    calls=[]
    managed=SimpleNamespace(default_account_id=lambda:'synthetic-account')
    engine=SimpleNamespace(dispose=lambda:calls.append('dispose'))
    monkeypatch.setattr(probe_session,'open_managed_sessions',lambda path:(managed,engine))
    cdp=SimpleNamespace(send=lambda *a:None,on=lambda *a:None)
    lease=SimpleNamespace(context=SimpleNamespace(new_cdp_session=lambda page:cdp),page=object(),closed=False,
        proxy=SimpleNamespace(failure_counts={},traffic={}),last_phase='read',last_failure=None,
        verification_event=None,observed_statuses=[],read_evidence={},close=lambda:calls.append('close'))
    class Pool:
        def __init__(self,path,**kwargs):
            assert kwargs['managed_sessions'] is managed and not kwargs['anonymous_probe_initializers']
        def acquire(self,account_id): calls.append(account_id);return lease
    class Provider:
        def __init__(self,*a): pass
        def resolve(self,*a,**kw): return ResolvePage((),None,False,'complete')
        def media(self,*a,**kw): raise AssertionError('default download')
        def close(self): pass
    monkeypatch.setattr(probe,'BUILD_ROOT',tmp_path)
    monkeypatch.setattr(probe,'DownloadBrowserPool',Pool)
    monkeypatch.setattr(probe,'DouyinProvider',Provider)
    monkeypatch.setattr(sys,'argv',['probe','--source','https://www.douyin.com/user/a','--managed-default','--managed-data',str(tmp_path/'source'),'--output',str(tmp_path/'out')])
    probe.main()
    report=json.loads((tmp_path/'out'/'summary.json').read_text())
    assert calls==['synthetic-account','dispose','close']
    assert report['mode']=='managed-default-read-only' and report['expected_identity_match'] is True
    assert report['catalog']['count']==0 and report['works']==[]
    assert 'synthetic-account' not in repr(report)

@pytest.mark.parametrize('kind',['root','account','archive'])
def test_readonly_store_rejects_symlinks(tmp_path,kind):
    from yijianjifa.features.downloads.probe_session import ReadOnlySessionStore
    from yijianjifa.features.accounts.session_store import SessionStore
    real=tmp_path/'real';writer=SessionStore(real);ref=writer.put('synthetic',b'fake')
    if kind=='root':
        link=tmp_path/'link';link.symlink_to(real)
        with pytest.raises(DownloadError): ReadOnlySessionStore(link)
    else:
        store=ReadOnlySessionStore(real);archive=writer._payload_path('synthetic')
        target=tmp_path/'elsewhere'
        if kind=='account': archive.parent.rename(target);archive.parent.symlink_to(target)
        else: archive.rename(target);archive.symlink_to(target)
        with pytest.raises(ValueError): store.get('synthetic',ref=ref)

@pytest.mark.parametrize('name',['summary.json','verification.json','control.json','1.mp4'])
def test_child_symlinks_rejected_before_probe_and_preserve_source(tmp_path,monkeypatch,name):
    from types import SimpleNamespace
    source=tmp_path/'source';source.mkdir();outside=source/'untouched';outside.write_bytes(b'synthetic source')
    output=tmp_path/'build'/'out';output.mkdir(parents=True);(output/name).symlink_to(outside)
    monkeypatch.setattr(probe,'BUILD_ROOT',tmp_path/'build')
    args=SimpleNamespace(output=str(output),consumer_data=None,managed_default=True,managed_data=str(source),allow_observed_page_init=False,prepare_verification=False,select_id='1',max_pages=1)
    with pytest.raises(DownloadError) as error: probe.validate_args(args)
    assert error.value.code=='unsafe_source'
    assert outside.read_bytes()==b'synthetic source' and (output/name).is_symlink()


def test_selected_destination_symlink_rejected_before_media(tmp_path):
    outside=tmp_path/'source';outside.write_bytes(b'keep')
    root=tmp_path/'out';root.mkdir();(root/'1.mp4').symlink_to(outside)
    class Provider:
        registry=object()
        def resolve(self,*a,**kw): return ResolvePage((Candidate('douyin:1','douyin','1','s','https://www.douyin.com/video/1'),),None,False,'complete')
        def media(self,*a,**kw): raise AssertionError('must reject before private media')
    with pytest.raises(DownloadError) as error:
        probe.collect(Provider(),None,root,{'works':[]},selected_id='1',max_pages=1,connector=None)
    assert error.value.code=='unsafe_source' and outside.read_bytes()==b'keep'
