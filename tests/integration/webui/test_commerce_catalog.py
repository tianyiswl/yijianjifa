from datetime import timedelta
import pytest
from test_publish_preparation import setup
from yijianjifa.features.accounts.service import AccountService,AccountRow
from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.commerce.stores import StoreCandidate
from yijianjifa.storage.db import session_scope


def fixture(engine,paths,tmp):
    prep,a,v,p,now=setup(engine,paths,tmp)
    AccountService(engine).update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    return a,now


def test_catalog_filters_unknown_commission_and_pages_one_account_snapshot(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    a,now=fixture(db_engine,app_paths,tmp_path);calls=[]
    def fetch(account,scope,query,region):
        calls.append(account.id)
        return [StoreCandidate(str(i),'测试店'+str(i),'广西','local','confirmed' if i%2==0 else 'unknown') for i in range(6)]
    service=CommerceCatalog(db_engine,fetch=fetch)
    result=service.search(dict(account_id=a.id,scope='local',query='测试',region='广西',commission='confirmed_only',favorite_only=False),now=now)
    assert result['available'] and result['total']==3
    page=service.page(dict(account_id=a.id,token=result['token'],offset=1,limit=1),now=now)
    assert page['items'][0]['id']=='2' and page['total']==3 and len(calls)==1
    assert 'fixture-only-session' not in str(result)+str(page)
    with pytest.raises(ValueError):service.page(dict(account_id='other',token=result['token'],offset=0,limit=10),now=now)
    with pytest.raises(ValueError):service.page(dict(account_id=a.id,token=result['token'],offset=0,limit=10),now=now+timedelta(minutes=6))


def test_catalog_scope_and_identity_changes_revoke_candidates(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    a,now=fixture(db_engine,app_paths,tmp_path)
    service=CommerceCatalog(db_engine,fetch=lambda *a:[StoreCandidate('s','店','广西','local','unknown')])
    p=dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False)
    with pytest.raises(ValueError):service.search(dict(p,scope='national'),now=now)
    r=service.search(p,now=now)
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='changed-session'
    with pytest.raises(ValueError):service.page(dict(account_id=a.id,token=r['token'],offset=0,limit=10),now=now)


def test_disconnected_catalog_reports_unavailable_not_empty_search_success(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    a,now=fixture(db_engine,app_paths,tmp_path)
    r=CommerceCatalog(db_engine).search(dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False),now=now)
    assert not r['available'] and r['token'] is None and r['reason']=='account_catalog_not_connected'


def test_preview_resolves_only_current_account_catalog_members(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    AccountService(db_engine).update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    catalog=CommerceCatalog(db_engine,fetch=lambda *args:[StoreCandidate('s','测试店','广西','local','unknown')])
    query=catalog.search(dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False),now=now)
    p.pop('account_ids');p.pop('distribution')
    request=dict(account_id=a.id,plan=p,store_pool=['s'],manual_store_overrides={},store_query_token=query['token'])
    planner=CommercePlanning(db_engine,catalog=catalog)
    result=planner.preview(request,now=now)
    assert result['stores_available'] and not result['ready_to_submit']
    assert result['items'][0]['store']['name']=='测试店'
    assert result['items'][0]['store']['commission']=='unknown'
    assert 'store_availability_unverified' not in result['items'][0]['blockers']
    with pytest.raises(ValueError):planner.preview(dict(request,store_pool=['forged']),now=now)
    with pytest.raises(ValueError):planner.preview(request,now=now+timedelta(minutes=6))
    with session_scope(db_engine) as s:s.get(AccountRow,a.id).session_ref='rotated'
    with pytest.raises(ValueError):planner.preview(request,now=now)


def test_resolved_pool_does_not_clear_stale_manual_assignment(db_engine,app_paths,tmp_path):
    from yijianjifa.app_services.commerce_catalog import CommerceCatalog
    from yijianjifa.app_services.commerce_planning import CommercePlanning
    prep,a,v,p,now=setup(db_engine,app_paths,tmp_path)
    AccountService(db_engine).update_capabilities(a.id,CapabilitySnapshot(commerce='available',location_scope='local'))
    catalog=CommerceCatalog(db_engine,fetch=lambda *args:[StoreCandidate('s','店','广西','local','confirmed')])
    query=catalog.search(dict(account_id=a.id,scope='local',query='',region='',commission='all',favorite_only=False),now=now)
    p.pop('account_ids');p.pop('distribution')
    result=CommercePlanning(db_engine,catalog=catalog).preview(dict(account_id=a.id,plan=p,store_pool=['s'],manual_store_overrides={v.id:'removed'},store_query_token=query['token']),now=now)
    assert not result['locally_valid'] and result['items'][0]['store_id']=='removed'
    assert result['items'][0]['store'] is None
    assert 'store_outside_pool' in result['items'][0]['blockers']
