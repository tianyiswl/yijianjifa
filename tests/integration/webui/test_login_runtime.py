import json
import threading
import pytest
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService


def call(api,op,p,rid='test',trusted=True):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=p)),trusted_origin=trusted)


def test_login_without_native_provider_is_unavailable(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    try:
        result=call(api,'account.login.availability',{})
        assert result['ok'] and result['data']=={'available':False,'platforms':[]}
        assert not call(api,'account.login.start',{'account_id':None})['ok']
        assert AccountService(db_engine).list()==[]
    finally:api.close(1)


def test_cancel_and_exit_wait_for_provider_without_accepting_late_identity(db_engine,app_paths):
    entered=threading.Event();release=threading.Event();calls=[]
    def provider(account_id,cancel):
        calls.append(account_id);entered.set();release.wait(3)
        return dict(platform_user_id='synthetic',nickname='test',session_blob=b'private bytes')
    api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        first=call(api,'account.login.start',{'account_id':None},'login')
        assert first['ok'];jid=first['data']['id'];assert entered.wait(1)
        replay=call(api,'account.login.start',{'account_id':None},'login')
        assert replay['data']['id']==jid and calls==[None]
        assert not call(api,'account.login.start',{'account_id':None},'other')['ok']
        result=call(api,'account.login.cancel',{'id':jid})
        assert result['data']['state']=='cancelled'
        assert api.close(.01) is False
        release.set();assert api.close(2) is True
        assert AccountService(db_engine).list()==[]
    finally:release.set();api.close(3)


def test_verified_provider_is_private_and_not_controlled_by_page(db_engine,app_paths):
    def provider(account_id,cancel):
        return dict(platform_user_id='synthetic-private-id',nickname='test',session_blob=b'private bytes')
    api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        assert not call(api,'account.login.start',{'account_id':None},trusted=False)['ok']
        assert not call(api,'account.login.start',{'account_id':None,'session_blob':'injected'})['ok']
        result=call(api,'account.login.start',{'account_id':None},'start');assert result['ok']
        api._login_runtime.thread.join(2)
        status=call(api,'account.login.status',{'id':result['data']['id']})
        assert status['data']['state']=='completed'
        assert 'private' not in json.dumps(status) and 'profile:' not in json.dumps(status)
        assert len(AccountService(db_engine).list())==1
    finally:api.close(2)


def test_provider_window_close_and_failure_never_imply_login_success(db_engine,app_paths):
    for provider in (lambda account,cancel:None, lambda account,cancel:dict(platform_user_id='incomplete')):
        api=DesktopApi(db_engine,app_paths,login_provider=provider)
        try:
            result=call(api,'account.login.start',{'account_id':None},'start')
            api._login_runtime.thread.join(2)
            status=call(api,'account.login.status',{'id':result['data']['id']})
            assert status['data']['state'] in {'cancelled','failed'}
            assert AccountService(db_engine).list()==[]
        finally:api.close(2)


def test_provider_exception_is_not_exposed_to_page(db_engine,app_paths):
    def provider(account,cancel):raise RuntimeError('Cookie=private /private/profile')
    api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        result=call(api,'account.login.start',{'account_id':None},'start')
        api._login_runtime.thread.join(2)
        status=call(api,'account.login.status',{'id':result['data']['id']})
        assert status['data']['state']=='failed'
        assert 'Cookie' not in str(status) and '/private' not in str(status)
        assert AccountService(db_engine).list()==[]
    finally:api.close(2)


def test_observed_wrong_platform_account_reports_identity_mismatch_immediately(db_engine,app_paths):
    from yijianjifa.features.accounts.service import AccountIdentityMismatch
    def provider(account,cancel):raise AccountIdentityMismatch('private observed identity')
    api=DesktopApi(db_engine,app_paths,login_providers={'kuaishou':provider})
    try:
        result=call(api,'account.login.start',{'account_id':None,'platform':'kuaishou'},'start')
        assert result['ok'];api._login_runtime.thread.join(2)
        status=call(api,'account.login.status',{'id':result['data']['id']})
        assert status['data']['state']=='failed' and status['data']['code']=='identity_mismatch'
        assert 'private' not in str(status) and AccountService(db_engine).list()==[]
    finally:api.close(2)


def test_backend_op_is_account_bound_and_cannot_replay_login_request(db_engine,app_paths):
    service=AccountService(db_engine);account=service.create('douyin','123','Fixture')
    entered=threading.Event();release=threading.Event();opened=[]
    def provider(account_id,cancel):raise AssertionError('wrong provider')
    def backend(account_id,cancel):
        opened.append(account_id);entered.set();release.wait(2)
        return dict(platform_user_id='123',nickname='Fixture',session_blob=b'fixture')
    provider.open_backend=backend
    api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        assert not call(api,'account.backend.start',{'account_id':None},'none')['ok']
        result=call(api,'account.backend.start',{'account_id':account.id},'backend')
        assert result['ok'] and entered.wait(1)
        assert not call(api,'account.login.start',{'account_id':account.id},'backend')['ok']
        assert call(api,'account.backend.start',{'account_id':account.id},'backend')['data']['id']==result['data']['id']
        release.set();api._login_runtime.thread.join(2)
        assert opened==[account.id]
        assert call(api,'account.login.status',{'id':result['data']['id']})['data']['state']=='completed'
    finally:release.set();api.close(2)


def test_capability_upload_requires_exact_fixture_and_explicit_consent(db_engine,app_paths):
    from yijianjifa.features.accounts.capability_probe import FIXTURE_SHA256
    service=AccountService(db_engine);account=service.create('douyin','123','Fixture');calls=[]
    def provider(*args):raise AssertionError('wrong provider')
    def probe(account_id,cancel,path):
        calls.append(account_id)
        return dict(uid_before='123',uid_after='123',upload_complete=True,form_ready=True,commerce_visible=True,scope_options=['local'])
    provider.check_capabilities=probe
    api=DesktopApi(db_engine,app_paths,login_provider=provider)
    try:
        params={'account_id':account.id,'upload_consent':True,'fixture_sha256':FIXTURE_SHA256}
        for change in ({'upload_consent':False},{'upload_consent':1},{'fixture_sha256':'other'}):
            assert not call(api,'account.capabilities.start',{**params,**change})['ok']
        assert not call(api,'account.capabilities.start',params,trusted=False)['ok']
        assert calls==[]
        result=call(api,'account.capabilities.start',params,'probe');assert result['ok']
        api._login_runtime.thread.join(2)
        assert call(api,'account.capabilities.start',params,'probe')['data']['id']==result['data']['id']
        assert calls==[account.id]
        assert service.get(account.id).capabilities.location_scope=='local'
        assert not call(api,'account.backend.start',{'account_id':account.id},'probe')['ok']
    finally:api.close(2)


def test_kuaishou_host_provider_saves_identity_without_claiming_publish_ready(db_engine,app_paths):
    calls=[]
    def kuaishou(account_id,cancel):
        calls.append(account_id)
        return dict(platform_user_id='ks-fixture',nickname='快手测试',session_blob=b'kuaishou-private-synthetic',ordinary_available=True)
    api=DesktopApi(db_engine,app_paths,login_providers={'kuaishou':kuaishou})
    try:
        assert call(api,'account.login.availability',{})['data']=={'available':True,'platforms':['kuaishou']}
        assert not call(api,'account.login.start',{'account_id':None},'legacy')['ok']
        first=call(api,'account.login.start',{'account_id':None,'platform':'kuaishou'},'ks-login')
        assert first['ok']
        api._login_runtime.thread.join(2)
        saved=call(api,'account.login.status',{'id':first['data']['id']})['data']
        assert saved['state']=='completed' and saved['platform']=='kuaishou'
        assert calls==[None]
        rows=AccountService(db_engine).list('kuaishou')
        assert len(rows)==1 and rows[0].nickname=='快手测试' and rows[0].last_login_at
        assert rows[0].capabilities.ordinary=='pending'
        assert 'private-synthetic' not in json.dumps(saved) and 'profile:' not in json.dumps(saved)
        catalog=call(api,'multi_publish.catalog',{})['data']['items']
        ks=next(row for row in catalog if row['platform']=='kuaishou')
        assert ks['login_ready'] is True
        assert not any(ks[key] for key in ('ordinary_ready','schedule_ready','publish_now_ready','query_ready','transport_available'))
        assert next(row for row in catalog if row['platform']=='douyin')['login_ready'] is False
    finally:api.close(2)


def test_existing_account_platform_is_derived_but_explicit_mismatch_is_rejected(db_engine,app_paths):
    account=AccountService(db_engine).create('kuaishou','ks-fixture','快手测试');calls=[]
    def kuaishou(account_id,cancel):
        calls.append(account_id)
        return dict(platform_user_id='ks-fixture',nickname='快手新昵称',session_blob=b'kuaishou-private-synthetic')
    api=DesktopApi(db_engine,app_paths,login_providers={'kuaishou':kuaishou})
    try:
        assert not call(api,'account.login.start',{'account_id':account.id,'platform':'douyin'},'wrong-platform')['ok']
        assert calls==[]
        started=call(api,'account.login.start',{'account_id':account.id},'derived')
        assert started['ok']
        api._login_runtime.thread.join(2)
        assert calls==[account.id]
        assert call(api,'account.login.status',{'id':started['data']['id']})['data']['state']=='completed'
        assert AccountService(db_engine).get(account.id).nickname=='快手新昵称'
    finally:api.close(2)


def test_login_receipt_includes_platform_and_kuaishou_probe_never_runs(db_engine,app_paths):
    from yijianjifa.features.accounts.capability_probe import FIXTURE_SHA256
    opened=[]
    def douyin(account_id,cancel):
        opened.append('douyin')
        return dict(platform_user_id='fixture',nickname='抖音测试',session_blob=b'douyin-synthetic')
    def kuaishou(account_id,cancel):
        opened.append('kuaishou')
        return dict(platform_user_id='fixture',nickname='快手测试',session_blob=b'kuaishou-synthetic')
    kuaishou.check_capabilities=lambda *args:opened.append('forbidden-probe')
    api=DesktopApi(db_engine,app_paths,login_provider=douyin,login_providers={'kuaishou':kuaishou})
    try:
        first=call(api,'account.login.start',{'account_id':None},'shared-id')
        assert first['ok'];api._login_runtime.thread.join(2)
        assert not call(api,'account.login.start',{'account_id':None,'platform':'kuaishou'},'shared-id')['ok']
        assert opened==['douyin']
        second=call(api,'account.login.start',{'account_id':None,'platform':'kuaishou'},'ks-id')
        assert second['ok'];api._login_runtime.thread.join(2)
        account=AccountService(db_engine).list('kuaishou')[0]
        assert not call(api,'account.capabilities.prepare',{'account_id':account.id})['ok']
        assert not call(api,'account.capabilities.start',{'account_id':account.id,'upload_consent':True,'fixture_sha256':FIXTURE_SHA256},'probe')['ok']
        assert opened==['douyin','kuaishou']
        assert api._login_runtime.provider is douyin
    finally:api.close(2)


def test_only_known_callable_host_login_providers_can_be_registered(db_engine,app_paths):
    for providers in ({'kuaishou':object()},{'unknown':lambda *args:None},[],{'douyin':lambda *args:None}):
        kwargs={'login_providers':providers}
        if type(providers) is dict and 'douyin' in providers:kwargs['login_provider']=lambda *args:None
        with pytest.raises(ValueError):DesktopApi(db_engine,app_paths,**kwargs)


def test_kuaishou_backend_restores_selected_provider_and_keeps_publishing_pending(db_engine,app_paths):
    account=AccountService(db_engine).create('kuaishou','ks-fixture','快手测试');opened=[]
    def kuaishou(*args):raise AssertionError('login function must not be used for backend')
    def backend(account_id,cancel):
        opened.append(account_id)
        return dict(platform_user_id='ks-fixture',nickname='快手昵称',session_blob=b'backend-kuaishou-synthetic')
    kuaishou.open_backend=backend
    api=DesktopApi(db_engine,app_paths,login_providers={'kuaishou':kuaishou})
    try:
        assert not call(api,'account.backend.start',{'account_id':None,'platform':'kuaishou'},'no-account')['ok']
        started=call(api,'account.backend.start',{'account_id':account.id},'backend')
        assert started['ok'];api._login_runtime.thread.join(2)
        assert opened==[account.id]
        assert call(api,'account.login.status',{'id':started['data']['id']})['data']['state']=='completed'
        saved=AccountService(db_engine).get(account.id)
        assert saved.nickname=='快手昵称' and saved.capabilities.ordinary=='pending'
        from yijianjifa.features.accounts.session_store import SessionStore
        assert SessionStore(app_paths.data_dir/'profiles').get(saved.id,ref=saved.session_ref)==b'backend-kuaishou-synthetic'
    finally:api.close(2)
    assert api._login_runtime.platforms()==[]


def test_cancelled_kuaishou_provider_cannot_save_late_identity(db_engine,app_paths):
    entered=threading.Event();release=threading.Event()
    def kuaishou(account_id,cancel):
        entered.set();release.wait(3)
        return dict(platform_user_id='ks-fixture',nickname='迟到的账号',session_blob=b'late-kuaishou-synthetic')
    api=DesktopApi(db_engine,app_paths,login_providers={'kuaishou':kuaishou})
    try:
        started=call(api,'account.login.start',{'account_id':None,'platform':'kuaishou'},'ks')
        assert started['ok'] and entered.wait(1)
        assert call(api,'account.login.cancel',{'id':started['data']['id']})['data']['state']=='cancelled'
        assert api.close(.01) is False
        release.set();assert api.close(2) is True
        assert AccountService(db_engine).list()==[]
    finally:release.set();api.close(2)
