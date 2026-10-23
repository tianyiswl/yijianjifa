import pytest
from dataclasses import replace
from yijianjifa.features.accounts.capability_probe import fixture_path,interpret_observation
from yijianjifa.app_services.account_access import public_account
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.models import CapabilitySnapshot


def observation(**changes):
    return dict(uid_before='123',uid_after='123',upload_complete=True,form_ready=True,commerce_visible=True,scope_options=['local','national'],**changes)


def test_fixture_is_fixed_and_small():
    assert fixture_path().stat().st_size < 500_000


@pytest.mark.parametrize('contents', [None, b'tampered capability fixture'])
def test_missing_or_changed_fixture_is_rejected(tmp_path, monkeypatch, contents):
    from yijianjifa.features.accounts import capability_probe

    if contents is not None:
        fixture = tmp_path / 'webui/assets/capability-test.mp4'
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(contents)
    monkeypatch.setattr(capability_probe, '__file__',
                        str(tmp_path / 'features/accounts/capability_probe.py'))
    with pytest.raises(ValueError, match='capability fixture unavailable'):
        capability_probe.fixture_path()


@pytest.mark.parametrize('key',['upload_complete','form_ready'])
def test_incomplete_upload_never_grants_or_denies_capabilities(key):
    value=observation();value[key]=False
    assert interpret_observation(value,'123')==CapabilitySnapshot()


def test_observed_entry_and_location_scopes_are_independent():
    value=observation();value['scope_options']=[]
    assert interpret_observation(value,'123')==CapabilitySnapshot('available','available','pending')
    value['commerce_visible']=False;value['scope_options']=['national']
    assert interpret_observation(value,'123')==CapabilitySnapshot('available','not_found','national')


def test_identity_change_rejected():
    value=observation();value['uid_after']='456'
    with pytest.raises(ValueError):interpret_observation(value,'123')


def test_web_account_preserves_confirmed_scope(db_engine):
    a=AccountService(db_engine).create('douyin','123','fixture')
    a=replace(a,capabilities=CapabilitySnapshot('available','available','local_national'))
    assert public_account(a)['capabilities']['location_scope']=='local_national'


def test_cancelled_probe_cannot_update_capabilities(db_engine,tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    service=AccountService(db_engine);a=service.create('douyin','123','fixture')
    flow=LoginFlow(db_engine,SessionStore(tmp_path/'sessions'));job=flow.begin(a.id)
    flow.window_closed(job['id']);flow.complete_capabilities(job['id'],observation())
    assert service.get(a.id).capabilities==CapabilitySnapshot()


def test_probe_is_account_bound_and_preserves_session(db_engine,tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    service=AccountService(db_engine);a=service.create('douyin','123','fixture');b=service.create('douyin','456','other')
    flow=LoginFlow(db_engine,SessionStore(tmp_path/'sessions'));job=flow.begin(a.id)
    assert flow.complete_capabilities(job['id'],observation())['state']=='completed'
    assert service.get(a.id).capabilities==CapabilitySnapshot('available','available','local_national')
    assert service.get(a.id).session_ref==a.session_ref
    assert service.get(b.id).capabilities==CapabilitySnapshot()


def test_open_backend_preserves_independent_capability_results(db_engine,tmp_path):
    from yijianjifa.features.accounts.login_flow import LoginFlow
    from yijianjifa.features.accounts.session_store import SessionStore
    service=AccountService(db_engine);a=service.create('douyin','123','fixture')
    flow=LoginFlow(db_engine,SessionStore(tmp_path/'sessions'))
    probe=flow.begin(a.id);flow.complete_capabilities(probe['id'],observation())
    login=flow.begin(a.id)
    flow.complete(login['id'],platform_user_id='123',nickname='fixture',session_blob=b'synthetic',ordinary_available=True,preserve_capabilities=True)
    assert service.get(a.id).capabilities==CapabilitySnapshot('available','available','local_national')
