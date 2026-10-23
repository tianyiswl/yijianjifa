"""Trusted provider observations only, no platform network calls."""
import pytest
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def flow(engine,tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    return LoginFlow(engine,SessionStore(tmp_path/'profiles'))


def test_window_close_and_late_observation_never_create_account(db_engine,tmp_path):
    manager=flow(db_engine,tmp_path);job=manager.begin()
    manager.window_closed(job['id'])
    result=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'fake')
    assert result['state']=='cancelled'
    assert AccountService(db_engine).list()==[]


def test_wrong_relogin_identity_preserves_original_session(db_engine,tmp_path):
    service=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','original-user','original')
    ref=service.attach_session(account.id,store,b'original synthetic')
    manager=flow(db_engine,tmp_path);job=manager.begin(account.id)
    result=manager.complete(job['id'],platform_user_id='wrong-user',nickname='other',session_blob=b'other synthetic')
    assert result['state']=='failed' and result['code']=='identity_mismatch'
    assert service.get(account.id).session_ref==ref
    assert store.get(account.id,ref=ref)==b'original synthetic'


def test_verified_new_login_is_atomic_and_replayed_without_duplicates(db_engine,tmp_path):
    manager=flow(db_engine,tmp_path);job=manager.begin()
    result=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'private-synthetic')
    assert result['state']=='completed'
    assert 'private-synthetic' not in str(result) and 'profile:' not in str(result)
    again=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'private-synthetic')
    assert again==result
    service=AccountService(db_engine);accounts=service.list();assert len(accounts)==1
    assert manager.store.get(accounts[0].id,ref=accounts[0].session_ref)==b'private-synthetic'


def test_relogin_refuses_stale_account_snapshot(db_engine,tmp_path):
    service=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    account=service.create('douyin','synthetic','test');service.attach_session(account.id,store,b'old')
    manager=flow(db_engine,tmp_path);job=manager.begin(account.id)
    refreshed=service.attach_session(account.id,store,b'newer')
    result=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'late')
    assert result['state']=='failed' and result['code']=='account_changed'
    assert service.get(account.id).session_ref==refreshed


def test_database_failure_does_not_leave_partial_account(db_engine,tmp_path):
    from sqlalchemy import event
    manager=flow(db_engine,tmp_path);job=manager.begin()
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('INSERT INTO ACCOUNTS'):raise RuntimeError('private synthetic failure')
    event.listen(db_engine,'before_cursor_execute',fail)
    try:result=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'fake')
    finally:event.remove(db_engine,'before_cursor_execute',fail)
    assert result['state']=='failed' and result['code']=='login_save_failed'
    assert 'private synthetic' not in str(result)
    assert AccountService(db_engine).list()==[]


def test_duplicate_identity_never_overwrites_existing_account(db_engine,tmp_path):
    accounts=AccountService(db_engine);store=SessionStore(tmp_path/'profiles')
    original=accounts.create('douyin','synthetic','original')
    ref=accounts.attach_session(original.id,store,b'original session')
    manager=flow(db_engine,tmp_path);job=manager.begin()
    result=manager.complete(job['id'],platform_user_id='synthetic',nickname='changed',session_blob=b'other')
    assert result['state']=='failed' and result['code']=='account_exists'
    assert accounts.get(original.id).session_ref==ref
    assert accounts.get(original.id).nickname=='original'


def test_close_cancels_pending_and_rejects_new_attempts(db_engine,tmp_path):
    manager=flow(db_engine,tmp_path);job=manager.begin();manager.close()
    with pytest.raises(ValueError):manager.begin()
    result=manager.complete(job['id'],platform_user_id='synthetic',nickname='test',session_blob=b'fake')
    assert result['state']=='cancelled' and AccountService(db_engine).list()==[]

def test_login_time_records_only_success_and_survives_session_refresh(db_engine,tmp_path):
    from yijianjifa.app_services.account_access import public_account
    service=AccountService(db_engine);account=service.create('douyin','123','测试')
    assert public_account(account)['last_login_at'] is None
    manager=flow(db_engine,tmp_path);job=manager.begin(account.id)
    manager.complete(job['id'],platform_user_id='123',nickname='测试',session_blob=b'private-synthetic')
    stamp=public_account(service.get(account.id))['last_login_at']
    assert stamp and stamp.endswith('+00:00')
    service.update_metadata(account.id,remark='备注',group_name='')
    service.attach_session(account.id,manager.store,b'refreshed-synthetic')
    assert public_account(service.get(account.id))['last_login_at']==stamp
    job=manager.begin(account.id);manager.window_closed(job['id'])
    assert public_account(service.get(account.id))['last_login_at']==stamp


def test_login_flow_isolates_same_identity_between_platforms_without_enabling_kuaishou_publish(db_engine,tmp_path):
    manager=flow(db_engine,tmp_path)
    douyin=manager.begin()
    manager.complete(douyin['id'],platform_user_id='123',nickname='抖音昵称',session_blob=b'douyin-synthetic',ordinary_available=True)
    kuaishou=manager.begin(platform='kuaishou')
    saved=manager.complete(kuaishou['id'],platform_user_id='123',nickname='快手昵称',session_blob=b'kuaishou-synthetic',ordinary_available=True)
    assert saved['state']=='completed' and saved['platform']=='kuaishou'
    accounts=AccountService(db_engine)
    d=accounts.list('douyin')[0];k=accounts.list('kuaishou')[0]
    assert d.id!=k.id and d.capabilities.ordinary=='available'
    assert k.nickname=='快手昵称' and k.last_login_at and k.login_state=='valid'
    assert k.capabilities.ordinary=='pending'
    assert manager.store.get(d.id,ref=d.session_ref)==b'douyin-synthetic'
    assert manager.store.get(k.id,ref=k.session_ref)==b'kuaishou-synthetic'


def test_kuaishou_relogin_derives_platform_and_rejects_wrong_identity_and_explicit_platform(db_engine,tmp_path):
    service=AccountService(db_engine);manager=flow(db_engine,tmp_path)
    original=service.create('kuaishou','ks-original','原账号')
    ref=service.attach_session(original.id,manager.store,b'original-kuaishou-synthetic')
    with pytest.raises(ValueError):manager.begin(original.id,platform='douyin')
    attempt=manager.begin(original.id)
    assert attempt['platform']=='kuaishou'
    wrong=manager.complete(attempt['id'],platform_user_id='wrong',nickname='另一个账号',session_blob=b'wrong-synthetic')
    assert wrong['state']=='failed' and wrong['code']=='identity_mismatch'
    assert service.get(original.id).session_ref==ref
    current=manager.begin(original.id)
    result=manager.complete(current['id'],platform_user_id='ks-original',nickname='新昵称',session_blob=b'refreshed-kuaishou-synthetic')
    assert result['state']=='completed'
    assert service.get(original.id).nickname=='新昵称'
    assert service.get(original.id).last_login_at is not None


def test_kuaishou_probe_cannot_enable_douyin_capabilities(db_engine,tmp_path):
    manager=flow(db_engine,tmp_path);account=AccountService(db_engine).create('kuaishou','ks-original','原账号')
    attempt=manager.begin(account.id)
    with pytest.raises(ValueError):manager.complete_capabilities(attempt['id'],{})
    assert AccountService(db_engine).get(account.id).capabilities.ordinary=='pending'


def test_removed_kuaishou_account_cannot_be_revived_by_inflight_login(db_engine,tmp_path):
    service=AccountService(db_engine);manager=flow(db_engine,tmp_path)
    original=service.create('kuaishou','ks-original','原账号')
    attempt=manager.begin(original.id)
    service.remove(original.id)
    result=manager.complete(attempt['id'],platform_user_id='ks-original',nickname='延迟回调',session_blob=b'late-synthetic')
    assert result['state']=='failed' and result['code']=='account_changed'
    assert service.list('kuaishou')==[]
