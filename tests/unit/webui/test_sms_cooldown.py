import pytest
from yijianjifa.webui.sms_cooldown import SMSCooldown, SMSCooldownActive


def test_same_account_across_tasks_and_restart_must_wait_sixty_seconds(db_engine):
    now=[1000.0]
    first=SMSCooldown(db_engine,clock=lambda:now[0])
    first.reserve('account-a')
    restarted=SMSCooldown(db_engine,clock=lambda:now[0])
    now[0]=1059.9
    with pytest.raises(SMSCooldownActive):restarted.reserve('account-a')
    assert restarted.remaining('account-a')==1
    restarted.reserve('account-b')
    now[0]=1060.0
    restarted.reserve('account-a')
    assert first.remaining('account-a')==60


def test_next_submit_waits_and_cancellation_prevents_submission(db_engine):
    now=[1000.0];cooldown=SMSCooldown(db_engine,clock=lambda:now[0]);cooldown.reserve('a')
    waited=[]
    def wait(seconds):waited.append(seconds);now[0]+=seconds
    assert cooldown.wait('a',cancelled=lambda:False,pause=wait)
    assert sum(waited)==pytest.approx(60)
    cooldown.reserve('a')
    assert not cooldown.wait('a',cancelled=lambda:True,pause=lambda _:pytest.fail('cancelled must not wait'))


def test_observed_platform_countdown_does_not_shorten_known_cooldown(db_engine):
    now=[1000.0];cooldown=SMSCooldown(db_engine,clock=lambda:now[0]);cooldown.reserve('a')
    now[0]=1001.0;cooldown.observe('a',20)
    assert cooldown.remaining('a')==59
