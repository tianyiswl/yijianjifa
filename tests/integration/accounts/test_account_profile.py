from yijianjifa.features.accounts.login_flow import LoginFlow
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.accounts.service import AccountService


def test_verified_profile_persists_avatar_and_ordinary_without_granting_commerce(db_engine,tmp_path):
    flow=LoginFlow(db_engine,SessionStore(tmp_path/'profiles'));job=flow.begin()
    avatar='data:image/png;base64,iVBORw0KGgo='
    result=flow.complete(job['id'],platform_user_id='123',nickname='Fixture',session_blob=b'fixture',avatar_data=avatar,ordinary_available=True)
    account=AccountService(db_engine).get(result['account_id'])
    assert account.avatar_data==avatar
    assert account.capabilities.ordinary=='available'
    assert account.capabilities.commerce=='pending'


def test_avatar_fetch_rejects_untrusted_destinations():
    from yijianjifa.features.accounts.avatar import avatar_url_allowed
    assert avatar_url_allowed('https://p3.douyinpic.com/avatar.jpg')
    for url in ('http://p3.douyinpic.com/a','https://douyinpic.com.evil.test/a','https://localhost/a','https://u:p@p3.douyinpic.com/a','file:///tmp/a'):
        assert not avatar_url_allowed(url)
