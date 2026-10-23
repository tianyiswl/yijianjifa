import io
import json
from pathlib import Path

import pytest
from yijianjifa_cloud_voice.billing import SupplierBalance, signed_request, save_credentials


def config(tmp_path):
    root=tmp_path/'private'; root.mkdir(mode=0o700)
    file=root/'billing.json'
    save_credentials(file,'test-ak','test-sk')
    return file


def test_unconfigured_never_queries(tmp_path):
    assert SupplierBalance(tmp_path/'missing',opener=lambda *a,**kw: pytest.fail('network')).read()['status']=='unconfigured'


def test_cache_and_failure_keep_previous_value_without_zero(tmp_path):
    clock=[1000]; calls=[]
    def opener(req,**kw):
        calls.append(req)
        if len(calls)>1:raise TimeoutError('test-ak test-sk')
        return io.BytesIO(json.dumps({'Result':{'AvailableBalance':'77.01','CashBalance':'83.01','ArrearsBalance':'0.00','AccountID':123}}).encode())
    balance=SupplierBalance(config(tmp_path),clock=lambda:clock[0],opener=opener)
    assert balance.read()['available_balance']=='77.01'
    assert balance.read()['status']=='ok' and len(calls)==1
    clock[0]=1061
    stale=balance.read()
    assert stale['status']=='unavailable' and stale['available_balance']=='77.01' and stale['checked_at']==1000
    assert 'test-sk' not in str(stale) and 'test-ak' not in str(stale)
    balance.read();assert len(calls)==2


@pytest.mark.parametrize('value',['NaN','Infinity','not-money',None,True,'1e100'])
def test_invalid_money_never_becomes_zero(tmp_path,value):
    reply={'Result':{'AvailableBalance':value,'CashBalance':'1','ArrearsBalance':'0'}}
    result=SupplierBalance(config(tmp_path),opener=lambda *a,**kw:io.BytesIO(json.dumps(reply).encode())).read()
    assert result['status']=='unavailable' and 'available_balance' not in result


def test_private_config_no_overwrite_or_public_permissions(tmp_path):
    file=config(tmp_path)
    with pytest.raises(FileExistsError):save_credentials(file,'other-ak','other-sk')
    file.chmod(0o644)
    result=SupplierBalance(file).read()
    assert result['status']=='unavailable' and 'available_balance' not in result


def test_signature_scope_and_headers():
    req=signed_request('public-example-ak','test-secret','20250329T180937Z')
    assert req.full_url=='https://billing.volcengineapi.com/?Action=QueryBalanceAcct&Version=2022-01-01'
    assert req.get_header('X-date')=='20250329T180937Z'
    auth=req.get_header('Authorization')
    assert '/20250329/cn-beijing/billing/request' in auth and 'SignedHeaders=host;x-date' in auth
    assert 'test-secret' not in auth
