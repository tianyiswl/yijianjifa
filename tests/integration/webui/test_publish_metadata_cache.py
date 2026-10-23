from datetime import timedelta
import pytest
from test_publish_preparation import setup
from yijianjifa.app_services.publish_locations import PublishLocations
from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
from yijianjifa.webui.douyin_metadata import normalize_observation


def test_native_strings_are_normalized_and_empty_results_need_evidence():
    class NativeString(str):pass
    identity=dict(uid='123',uid_after='123',status=0,profile_status=0,after_status=0,nickname='test')
    value=dict(identity=identity,path='/creator-micro/content/post/video',ready=True,labels=[NativeString('AI')])
    assert type(normalize_observation(value,'123','declarations')['items'][0]['label']) is str
    value.update(query='empty',scope='local',rows=[])
    with pytest.raises(ValueError):normalize_observation(value,'123','locations','empty','local')
    assert normalize_observation({**value,'empty':True},'123','locations','empty','local')==[]


def test_corrupt_history_does_not_block_search(db_engine,app_paths,tmp_path):
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    service=PublishLocations(db_engine,fetch=lambda a,q:[])
    with session_scope(db_engine) as session:
        session.add(Setting(key=f'publish-metadata:{a.id}:locations:broken',value_json='not-json'))
    assert service.search(dict(account_id=a.id,query='test'),now=now)['items']==[]
    assert len(service.cache.recent(service._account(a.id),'locations',now=now))==1


def test_location_cache_survives_restart_but_does_not_extend_evidence(db_engine,app_paths,tmp_path):
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    calls=[]
    def fetch(account,query,scope):
        calls.append((query,scope));return [dict(id='place',name='地点',address='地址')]
    first=PublishLocations(db_engine,fetch=fetch,scoped_fetch=True)
    p=dict(account_id=a.id,query='北海',scope='local')
    one=first.search(p,now=now)
    second=PublishLocations(db_engine,fetch=fetch,scoped_fetch=True)
    two=second.search(p,now=now+timedelta(minutes=1))
    assert len(calls)==1 and two['cached'] and two['expires_at']==one['expires_at']
    stale=second.search(p,now=now+timedelta(minutes=6))
    assert stale['available'] and stale['token'] and stale['items'] and stale['needs_verification']
    assert len(calls)==1
    second.search({**p,'scope':'national'},now=now)
    assert calls[-1]==('北海','national')
    second.search({**p,'refresh':True},now=now+timedelta(minutes=6))
    assert len(calls)==3
    recent=second.cache.recent(second._account(a.id),'locations',now=now+timedelta(minutes=7))
    assert {r['query'] for r in recent}=={'local|北海','national|北海'}


def test_declaration_cache_read_no_network_refresh_failure_preserves_evidence(db_engine,app_paths,tmp_path):
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    def fetch(account):return dict(required=True,items=[dict(id='d',label='声明')])
    service=CommerceDeclarations(db_engine,capability='ordinary',fetch=fetch)
    p=dict(account_id=a.id,refresh=False)
    assert not service.query({**p,'cache_only':True},now=now)['available']
    live=service.query(p,now=now)
    restarted=CommerceDeclarations(db_engine,capability='ordinary')
    saved=restarted.query(p,now=now+timedelta(minutes=1))
    assert saved['cached'] and saved['items']==live['items'] and saved['expires_at']==live['expires_at']
    assert restarted.query(p,now=now+timedelta(days=30))['available']
    def failure(account):raise ValueError('offline')
    service._fetch=failure
    with pytest.raises(ValueError):service.query({**p,'refresh':True},now=now)
    assert service.resolve(a.id,live['token'],['v'],'d',{},now=now)['items']['v']['id']=='d'


def test_native_observer_requires_identity_and_scoped_query():
    identity=dict(uid='123',uid_after='123',status=0,profile_status=0,after_status=0,nickname='测试')
    value=dict(identity=identity,path='/creator-micro/content/post/video',ready=True,labels=['内容由AI生成','无需添加自主声明'])
    result=normalize_observation(value,'123','declarations')
    assert result['required'] and result['items'][0]['id'].startswith('ui-v1:')
    with pytest.raises(ValueError):normalize_observation(value,'456','declarations')
    loc={**value,'query':'北海','scope':'local','rows':[dict(name='北海',address='广西')]}
    assert normalize_observation(loc,'123','locations','上海','local') is None
    assert normalize_observation(loc,'123','locations','北海','national') is None
    assert normalize_observation(loc,'123','locations','北海','local')[0]['name']=='北海'


def test_cache_survives_session_change_and_removed_with_account(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    from sqlalchemy import select
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    service=PublishLocations(db_engine,fetch=lambda a,q:[dict(id='p',name='地点',address='地址')])
    service.search(dict(account_id=a.id,query='北海'),now=now)
    current=service._account(a.id)
    assert service.cache.read(current,'locations','local|北海',now=now)
    with session_scope(db_engine) as session:session.get(AccountRow,a.id).session_ref='changed-session'
    assert service.cache.read(service._account(a.id),'locations','local|北海',now=now+timedelta(days=30))
    AccountService(db_engine).remove(a.id)
    with session_scope(db_engine) as session:
        assert list(session.scalars(select(Setting).where(Setting.key.startswith(f'publish-metadata:{a.id}:'))))==[]

@pytest.mark.parametrize('reuse', [False, True])
def test_metadata_success_closes_after_five_seconds_without_duplicate_result(monkeypatch, reuse):
    import sys
    from types import SimpleNamespace
    from yijianjifa.webui.douyin_metadata import MetadataWindow, DouyinCapabilityWindow
    timers=[];results=[];closed=[];titles=[]
    monkeypatch.setitem(sys.modules,'PyObjCTools',SimpleNamespace(AppHelper=SimpleNamespace(callLater=lambda delay,fn,*args:timers.append((fn,args)))))
    def parent_finish(self,value,error):
        closed.append(True);self.finished=True;self.on_closed(value,error)
    monkeypatch.setattr(DouyinCapabilityWindow,'finish',parent_finish)
    window=object.__new__(MetadataWindow)
    window.context={};window.finished=False;window.suspended=False;window.request_serial=1
    window.window=SimpleNamespace(setTitle_=titles.append)
    window.on_closed=lambda value,error:results.append(value)
    window.finish({'items':[]},None)
    assert results==[{'items':[]}]
    assert '5秒' in titles[0]
    if reuse:
        window.request_serial+=1;window.suspended=False
    while timers:
        fn,args=timers.pop(0);fn(*args)
    assert bool(closed) is not reuse
    assert results==[{'items':[]}]


def test_cached_locations_prepare_without_network_verification_is_at_submission(db_engine,app_paths,tmp_path):
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    rows=[dict(id='one',name='One',address='Old'),dict(id='two',name='Two',address='Old')]
    calls=[]
    def fetch(account,query):
        calls.append(query);return [dict(r) for r in rows]
    original=PublishLocations(db_engine,fetch=fetch)
    params=dict(account_id=a.id,query='places')
    original.search(params,now=now)
    restarted=PublishLocations(db_engine,fetch=fetch)
    later=now+timedelta(minutes=6)
    cached=restarted.search(params,now=later)
    assert cached['needs_verification'] and len(calls)==1
    rows[1]['address']='Changed'
    ref=lambda id:dict(token=cached['token'],id=id)
    assert restarted.resolve(a.id,ref('one'),now=later)['address']=='Old'
    assert restarted.resolve(a.id,ref('one'),now=later)['address']=='Old'
    assert len(calls)==1
    assert restarted.resolve(a.id,ref('two'),now=later)['address']=='Old'
    assert len(calls)==1


def test_recent_locations_returns_selectable_snapshots_without_network(db_engine,app_paths,tmp_path):
    _,a,_,_,_=setup(db_engine,app_paths,tmp_path)
    service=PublishLocations(db_engine,fetch=lambda *_:[dict(id='p',name='Place',address='Address')])
    service.search(dict(account_id=a.id,query='place'))
    restarted=PublishLocations(db_engine,fetch=lambda *_:pytest.fail('opening cache must not fetch'))
    recent=restarted.recent(dict(account_id=a.id))
    snapshot=recent['items'][0]['snapshot']
    assert snapshot['cached'] and snapshot['token'] and snapshot['items'][0]['id']=='p'
    assert not restarted.search(dict(account_id=a.id,query='missing',cache_only=True))['available']


def test_durable_declarations_survive_restart_session_rotation_and_age(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountRow
    from yijianjifa.storage.db import session_scope
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    first=CommerceDeclarations(db_engine,capability='ordinary',fetch=lambda _:dict(required=False,items=[dict(id='d',label='旧声明')]))
    first.query(dict(account_id=a.id,refresh=True),now=now)
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='rotated'
    later=now+timedelta(days=90)
    second=CommerceDeclarations(db_engine,capability='ordinary',fetch=lambda _:pytest.fail('cached choice must not fetch'))
    q=second.query(dict(account_id=a.id,cache_only=True),now=later)
    assert q['available'] and q['cached'] and q['observed_at']==now.isoformat()
    assert second.resolve(a.id,q['token'],['v'],'d',{},now=later)['items']['v']['label']=='旧声明'
    second._fetch=lambda _:dict(required=False,items=[dict(id='new',label='新声明')])
    fresh=second.query(dict(account_id=a.id,refresh=True),now=later)
    assert not fresh['cached'] and fresh['items'][0]['id']=='new'
    with pytest.raises(ValueError):second.resolve(a.id,q['token'],['v'],'d',{},now=later)


def test_legacy_cache_migration_and_platform_isolation(db_engine,app_paths,tmp_path):
    import json
    from yijianjifa.storage.db import session_scope
    from yijianjifa.storage.models import Setting
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    service=PublishLocations(db_engine)
    account=service._account(a.id);key=f'publish-metadata:{a.id}:locations:'+__import__('hashlib').sha256('local|旧地点'.encode()).hexdigest()
    with session_scope(db_engine) as s:
        s.add(Setting(key=key,value_json=json.dumps(dict(identity='old-session-hash',query='local|旧地点',observed_at=now.isoformat(),payload=[dict(id='p',name='旧地点',address='地址')]))))
    saved=service.search(dict(account_id=a.id,query='旧地点',cache_only=True),now=now+timedelta(days=90))
    assert saved['available'] and saved['cached']
    assert service.resolve(a.id,dict(token=saved['token'],id='p'),now=now+timedelta(days=91))['name']=='旧地点'
    with session_scope(db_engine) as s:
        row=s.get(Setting,service.cache.key(account,'locations','local|旧地点'));data=json.loads(row.value_json)
        assert data['platform']==account.platform
        data['platform']='other-platform';row.value_json=json.dumps(data)
    assert service.cache.read(account,'locations','local|旧地点',now=now+timedelta(days=91)) is None


def test_same_platform_accounts_share_choices_but_references_remain_scoped(db_engine,app_paths,tmp_path):
    from yijianjifa.features.accounts.service import AccountService,AccountRow
    from yijianjifa.features.accounts.models import CapabilitySnapshot
    from yijianjifa.storage.db import session_scope
    _,a,_,_,now=setup(db_engine,app_paths,tmp_path)
    b=AccountService(db_engine).create('douyin','777','第二账号')
    with session_scope(db_engine) as s:
        s.get(AccountRow,b.id).session_ref='second-session'
        s.get(AccountRow,b.id).login_state='valid'
    AccountService(db_engine).update_capabilities(b.id,CapabilitySnapshot(ordinary='available'))
    declarations=CommerceDeclarations(db_engine,capability='ordinary',fetch=lambda _:dict(required=False,items=[dict(id='d',label='内容由AI生成')]))
    qa=declarations.query(dict(account_id=a.id,refresh=True),now=now)
    declarations._fetch=lambda _:pytest.fail('switching account must not fetch')
    qb=declarations.query(dict(account_id=b.id,cache_only=True),now=now)
    assert qb['available'] and qb['cached'] and qb['items']==qa['items']
    assert declarations.resolve(b.id,qb['token'],['v'],'d',{},now=now)['items']['v']['id']=='d'
    with pytest.raises(ValueError):declarations.resolve(b.id,qa['token'],['v'],'d',{},now=now)
    places=PublishLocations(db_engine,fetch=lambda *_:[dict(id='p',name='地点',address='地址')])
    places.search(dict(account_id=a.id,query='地点'),now=now)
    places._fetch=lambda *_:pytest.fail('switching account must not search')
    cached=places.recent(dict(account_id=b.id))['items'][0]['snapshot']
    assert cached['available'] and cached['cached']
    assert places.resolve(b.id,dict(token=cached['token'],id='p'))['name']=='地点'


def test_metadata_refresh_uses_selected_account_not_startup_account(monkeypatch,db_engine,app_paths):
    from types import SimpleNamespace
    from yijianjifa.webui import douyin_metadata
    from yijianjifa.features.accounts.session_store import SessionStore
    observed=[]
    monkeypatch.setattr(SessionStore,'get',lambda self,*a,**k:b'offline-fixture')
    monkeypatch.setattr(douyin_metadata,'decode_cookies',lambda blob,**kw:observed.append(kw['identity']) or ())
    monkeypatch.setattr(douyin_metadata,'MetadataWindow',lambda *a,**kw:observed.append(kw['expected_uid']))
    class Provider:
        def __init__(self,*a,window_factory):self.factory=window_factory
        def __call__(self,*a):self.factory(None,None,None,None);return {'required':False,'items':[]}
    monkeypatch.setattr(douyin_metadata,'CocoaAuthProvider',Provider)
    fetch=douyin_metadata.make_metadata_provider(db_engine,app_paths,'123')
    result=fetch(SimpleNamespace(id='second',platform='douyin',platform_user_id='777',session_ref='fixture-ref'),'declarations')
    assert result['items']==[] and observed==['777','777']
