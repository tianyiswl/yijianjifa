import pytest
from yijianjifa.webui.publish_intervention import InterventionBroker, safe_resume

def test_codes_are_transient_and_bound_to_request():
    b=InterventionBroker(); r=b.request('a','账号','sms','需要验证')
    assert 'code' not in b.status()
    with pytest.raises(ValueError): b.respond('wrong','code','123456')
    b.respond(r,'code','123456'); assert '123456' not in str(b.status())
    assert b.consume(r)==('code','123456'); assert b.consume(r) is None
    b.finish(r); assert b.status()['request'] is None

def test_cancel_and_visible_are_explicit():
    b=InterventionBroker();r=b.request('a','账号','login','需要登录')
    b.respond(r,'visible'); assert b.consume(r)==('visible','')
    assert safe_resume(final_attempted=False)=='prepare'
    assert safe_resume(final_attempted=True)=='result'

def test_invalid_code_and_second_pending_rejected():
    b=InterventionBroker();r=b.request('a','账号','sms','验证')
    with pytest.raises(ValueError):b.respond(r,'code','x'*40)
    with pytest.raises(ValueError):b.request('b','其他','login','验证')

def test_manual_pointer_is_bound_and_not_a_code():
    b=InterventionBroker();r=b.request('a','账号','complex','请自行操作')
    b.respond(r,'pointer',[10,20,50,20]);assert b.consume(r)==('pointer',[10,20,50,20])
    with pytest.raises(ValueError):b.respond(r,'pointer',[-1,20])
    b.frame(r,'test');b.finish(r);assert b.status()['request'] is None
