import pytest
from yijianjifa.webui.portable_session import encode_session,decode_session
DOMAINS={'douyin.com','creator.douyin.com'}
def cookie(domain='.douyin.com'):
    return dict(name='session',value='test-only-cookie',domain=domain,path='/',secure=True,httpOnly=True,expires=-1,sameSite='Lax')
def test_identity_bound_windows_archive_round_trip():
    raw=encode_session([cookie()],'123',DOMAINS)
    assert decode_session(raw,'123',DOMAINS)==[cookie()]
    with pytest.raises(ValueError):decode_session(raw,'456',DOMAINS)
    with pytest.raises(ValueError):decode_session(raw,'123',{'douyin.com'})
def test_rejects_wrong_domain_duplicates_and_malformed_rows():
    with pytest.raises(ValueError):encode_session([cookie('example.com')],'123',DOMAINS)
    with pytest.raises(ValueError):encode_session([cookie(),cookie()],'123',DOMAINS)
    with pytest.raises(ValueError):decode_session(b'{}','123',DOMAINS)
    with pytest.raises(ValueError):encode_session([dict(domain='douyin.com')],'123',DOMAINS)
