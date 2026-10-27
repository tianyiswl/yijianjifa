"""Account-owned reusable choices; no browser, upload or platform submit."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from yijianjifa.app_services.account_publish_settings import AccountPublishSettings
from yijianjifa.app_services.commerce_declarations import CommerceDeclarations
from yijianjifa.app_services.douyin_declaration_catalog import catalog
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_settings import defaults, write as platform_write
from yijianjifa.app_services.publish_locations import PublishLocations
from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_publish_preparation import setup
from test_daily_mixing import daily, call as daily_call


def call(api, op, params, rid='account-settings-read'):
    return api.dispatch(json.dumps({'v': 1, 'operation': op, 'params': params, 'request_id': rid}), trusted_origin=True)


def legacy(engine, platform, fields):
    value = defaults(); value['fields_by_platform'] = {platform: fields}; platform_write(engine, value)


def test_account_fields_inherit_old_values_until_explicit_empty_and_survive_relogin(db_engine):
    a = AccountService(db_engine).create('channels', 'a', 'A')
    b = AccountService(db_engine).create('channels', 'b', 'B')
    legacy(db_engine, 'channels', {'channels_original': False, 'content_declaration_choice': 'ai_generated'})
    service = AccountPublishSettings(db_engine)
    assert service.read(a.id)['source'] == 'platform'
    assert service.read(a.id)['fields']['content_declaration_choice'] == 'ai_generated'
    service.save(a.id, {'channels_original': True, 'content_declaration_choice': 'none'})
    assert service.read(b.id)['fields']['content_declaration_choice'] == 'ai_generated'
    with session_scope(db_engine) as db: db.get(AccountRow, a.id).session_ref = 'new-private-session'
    assert AccountPublishSettings(db_engine).read(a.id)['fields']['channels_original'] is True
    service.save(a.id, {})
    assert service.read(a.id)['fields'] == {} and service.read(a.id)['source'] == 'account'
    with session_scope(db_engine) as db:
        saved = [s.value_json for s in db.scalars(select(Setting)) if s.key.startswith(service.PREFIX)]
    assert 'new-private-session' not in ''.join(saved)


def test_changed_identity_cannot_read_another_accounts_saved_defaults(db_engine):
    account = AccountService(db_engine).create('bilibili', 'first', 'A')
    service = AccountPublishSettings(db_engine)
    service.save(account.id, {'category_choice': 'human:knowledge', 'creation_statement_choice': 'ai_generated', 'content_authorization': False})
    with session_scope(db_engine) as db: db.get(AccountRow, account.id).platform_user_id = 'second'
    assert service.read(account.id)['fields'] == {} and not service.read(account.id)['has_saved_settings']


@pytest.mark.parametrize('platform,fields', [
    ('channels', {'title': 'not-account-metadata'}), ('channels', {'content_authorization': True}),
    ('kuaishou', {'channels_original': True}), ('douyin', {'category_id': 1}),
    ('bilibili', {'category_choice': 'human:knowledge', 'category_id': 1}),
    ('bilibili', {'content_authorization': 'true'}), ('channels', {'declaration': {'token': 'x' * 48, 'id': 'x'}}),
])
def test_settings_refuse_cross_platform_fields_and_transient_grants(db_engine, platform, fields):
    account = AccountService(db_engine).create(platform, 'owner', 'A')
    with pytest.raises(ValueError): AccountPublishSettings(db_engine).save(account.id, fields)


def test_douyin_choices_store_no_tokens_and_rebuild_current_trusted_refs(db_engine):
    account = AccountService(db_engine).create('douyin', 'owner', 'A')
    with session_scope(db_engine) as db: db.get(AccountRow, account.id).session_ref = 'local-session'
    rows = [{'id': 'poi-1', 'name': '无锡工厂', 'address': '合成地址'}]
    locations = PublishLocations(db_engine, fetch=lambda account, query: deepcopy(rows))
    declarations = CommerceDeclarations(db_engine, capability='ordinary', catalog=catalog())
    service = AccountPublishSettings(db_engine, declarations=declarations, locations=locations)
    selected = locations.search({'account_id': account.id, 'query': '无锡', 'scope': 'local'})
    service.save(account.id, {}, douyin={'declaration_id': catalog()['items'][0]['id'],
        'location': {'token': selected['token'], 'id': 'poi-1'}})
    result = service.read(account.id)
    assert result['douyin']['location'] == {**rows[0], 'query': '无锡', 'scope': 'local'}
    with session_scope(db_engine) as db:
        raw = db.get(Setting, service.PREFIX + account.id).value_json
    assert selected['token'] not in raw and 'local-session' not in raw
    reopened = AccountPublishSettings(db_engine)
    resolved = reopened.resolve_fields(account.id)
    assert resolved['declaration']['label'] == '内容由AI生成' and resolved['location'] == rows[0]
    assert 'token' not in json.dumps(resolved)
    service.save(account.id, {}, douyin={'declaration_id': None, 'location': None})
    assert service.resolve_fields(account.id) == {}


def test_arbitrary_durable_location_and_another_accounts_token_cannot_be_saved(db_engine):
    a = AccountService(db_engine).create('douyin', 'a', 'A')
    b = AccountService(db_engine).create('douyin', 'b', 'B')
    with session_scope(db_engine) as db:
        for item in (a, b): db.get(AccountRow, item.id).session_ref = 'session'
    locations = PublishLocations(db_engine, fetch=lambda account, query: [{'id': 'poi', 'name': '名称', 'address': '地址'}])
    service = AccountPublishSettings(db_engine, locations=locations)
    ref = locations.search({'account_id': a.id, 'query': '名称'})
    with pytest.raises(ValueError): service.save(a.id, {}, douyin={'location': {'id': 'poi', 'name': '名称', 'address': '地址', 'query': '名称', 'scope': 'local'}})
    with pytest.raises(ValueError): service.save(b.id, {}, douyin={'location': {'token': ref['token'], 'id': 'poi'}})


def test_api_save_is_idempotent_and_explicitly_empty_without_changing_account_metadata(db_engine, app_paths):
    account = AccountService(db_engine).create('channels', 'uid', 'A')
    api = DesktopApi(db_engine, app_paths)
    try:
        p = {'account_id': account.id, 'fields': {'content_declaration_choice': 'ai_generated', 'channels_original': False}}
        first = call(api, 'account.publish_settings.save', p, 'save-account-fields')
        assert first['ok'], first
        assert call(api, 'account.publish_settings.save', p, 'save-account-fields') == first
        assert not call(api, 'account.publish_settings.save', {**p, 'fields': {}}, 'save-account-fields')['ok']
        saved = call(api, 'account.publish_settings.read', {'account_id': account.id})
        assert saved['ok'] and saved['data']['fields'] == p['fields']
        assert AccountService(db_engine).get(account.id).nickname == 'A'
        assert 'platform_user_id' not in json.dumps(saved) and 'session_ref' not in json.dumps(saved)
    finally: api.close(.1)


def test_normal_prepare_inherits_declaration_but_explicit_none_wins(db_engine, app_paths, tmp_path):
    preparation, account, video, params, now = setup(db_engine, app_paths, tmp_path)
    service = AccountPublishSettings(db_engine)
    service.save(account.id, {}, douyin={'declaration_id': catalog()['items'][0]['id']})
    first = preparation.prepare(params, now=now)
    assert first['items'][0]['declaration']['label'] == '内容由AI生成'
    assert params.get('declarations') is None
    declarations = CommerceDeclarations(db_engine, capability='ordinary', catalog=catalog())
    current = declarations.query({'account_id': account.id})
    preparation.declarations = declarations
    explicit = {**params, 'declarations': {account.id: {'token': current['token'], 'batch': None, 'items': {}}}}
    assert preparation.prepare(explicit, now=now)['items'][0]['declaration'] is None


def test_daily_freezes_each_accounts_choices_and_old_platform_fields_remain_readable(daily, db_engine, monkeypatch):
    api, params, _, account = daily
    service = AccountPublishSettings(db_engine)
    adapter=api._multi_publish.catalog.transport_for('douyin').adapter
    monkeypatch.setattr(adapter,'validate_fields',lambda payload:payload['fields'].get('declaration') in catalog()['items'],raising=False)
    service.save(account.id, {}, douyin={'declaration_id': catalog()['items'][0]['id']})
    result = daily_call(api, 'mix.daily.submit', params, 'per-account-freeze')
    assert result['ok'], result
    task = api._mix.queue.get(result['data']['id'])
    frozen = task.snapshot['daily_publish']
    assert frozen['fields_by_account'][account.id]['declaration']['label'] == '内容由AI生成'
    original = deepcopy(frozen)
    service.save(account.id, {}, douyin={'declaration_id': None})
    assert api._mix.queue.get(task.id).snapshot['daily_publish'] == original
    old=deepcopy(frozen);old.pop('fields_by_account')
    api._daily._check_frozen_fields(db_engine,task.snapshot,old,allow_expired=True)
    api._mix.queue.run_once_until_idle()
    from yijianjifa.features.publish.deliveries import PublishDeliveryRow
    with session_scope(db_engine) as db:
        rows=list(db.scalars(select(PublishDeliveryRow)))
        assert rows and all(json.loads(row.snapshot_json)['fields']['declaration']['label']=='内容由AI生成' for row in rows)


def test_saved_location_missing_from_current_cache_is_not_silently_cleared(db_engine):
    account=AccountService(db_engine).create('douyin','owner','A')
    with session_scope(db_engine) as db:db.get(AccountRow,account.id).session_ref='session'
    locations=PublishLocations(db_engine,fetch=lambda a,q:[{'id':'poi','name':'名称','address':'地址'}])
    service=AccountPublishSettings(db_engine,locations=locations)
    picked=locations.search({'account_id':account.id,'query':'名称'})
    service.save(account.id,{},douyin={'location':{'token':picked['token'],'id':'poi'}})
    with session_scope(db_engine) as db:
        for row in db.scalars(select(Setting).where(Setting.key.startswith('publish-platform-metadata:'))):db.delete(row)
    with pytest.raises(ValueError,match='needs verification'):AccountPublishSettings(db_engine).resolve_fields(account.id)
    assert service.read(account.id)['douyin']['location']['id']=='poi'


def test_deleting_account_clears_only_its_reusable_defaults(db_engine,app_paths):
    a=AccountService(db_engine).create('channels','a','A');b=AccountService(db_engine).create('channels','b','B')
    service=AccountPublishSettings(db_engine);service.save(a.id,{'channels_original':True});service.save(b.id,{})
    api=DesktopApi(db_engine,app_paths)
    try:
        assert call(api,'account.delete',{'id':a.id},'delete-settings-owner')['ok']
        with session_scope(db_engine) as db:
            assert db.get(Setting,service.PREFIX+a.id) is None
            assert db.get(Setting,service.PREFIX+b.id) is not None
    finally:api.close(.1)


def test_distinct_account_declarations_remain_valid_in_the_shared_native_cache(db_engine,app_paths):
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    accounts=[AccountService(db_engine).create('douyin',name,name) for name in ('A','B')]
    with session_scope(db_engine) as db:
        for account in accounts:db.get(AccountRow,account.id).session_ref='local-session'
    settings=AccountPublishSettings(db_engine)
    for account,choice in zip(accounts,catalog()['items'][:2]):
        settings.save(account.id,{},douyin={'declaration_id':choice['id']})
    effective=[settings.resolve_fields(account.id) for account in accounts]
    adapter=ChromiumMultiPublishAdapter(db_engine,app_paths)
    try:
        for account,fields in zip(accounts,effective):
            assert adapter.validate_fields({'platform':'douyin','account_id':account.id,'fields':fields})
    finally:adapter.close()


def test_account_changes_after_normal_preview_require_new_preview_not_task_mutation(db_engine,app_paths,tmp_path):
    preparation,account,_,params,now=setup(db_engine,app_paths,tmp_path)
    service=AccountPublishSettings(db_engine)
    service.save(account.id,{},douyin={'declaration_id':catalog()['items'][0]['id']})
    prepared=preparation.prepare(params,now=now)
    service.save(account.id,{},douyin={'declaration_id':catalog()['items'][1]['id']})
    with pytest.raises(ValueError,match='changed'):preparation.resolve(prepared['token'],now=now)


def test_one_video_declaration_override_does_not_clear_the_other_videos_account_default(db_engine,app_paths,tmp_path):
    from yijianjifa.features.finished.service import FinishedVideoService
    preparation,account,first,params,now=setup(db_engine,app_paths,tmp_path)
    path=tmp_path/'second.mp4';path.write_bytes(b'second owned video')
    second=FinishedVideoService(db_engine).register_generated(path,duration_ms=3000,title='第二条')
    params={**params,'video_ids':[first.id,second.id],'daily_count':2}
    choices=catalog()['items'];defaults=AccountPublishSettings(db_engine)
    defaults.save(account.id,{},douyin={'declaration_id':choices[0]['id']})
    declarations=CommerceDeclarations(db_engine,capability='ordinary',catalog=catalog())
    current=declarations.query({'account_id':account.id})
    preparation.declarations=declarations
    params['declarations']={account.id:{'token':current['token'],'items':{first.id:choices[1]['id']}}}
    prepared=preparation.prepare(params,now=now)
    actual={item['video_id']:item['declaration']['id'] for item in prepared['items']}
    assert actual=={first.id:choices[1]['id'],second.id:choices[0]['id']}


def test_one_video_location_override_does_not_clear_the_other_videos_account_default(db_engine,app_paths,tmp_path):
    from yijianjifa.features.finished.service import FinishedVideoService
    preparation,account,first,params,now=setup(db_engine,app_paths,tmp_path)
    path=tmp_path/'second.mp4';path.write_bytes(b'second owned video')
    second=FinishedVideoService(db_engine).register_generated(path,duration_ms=3000,title='第二条')
    rows=[{'id':key,'name':'工厂'+key,'address':'合成地址'+key} for key in ('A','B')]
    locations=PublishLocations(db_engine,fetch=lambda account,query:deepcopy(rows))
    current=locations.search({'account_id':account.id,'query':'工厂'})
    defaults=AccountPublishSettings(db_engine,locations=locations)
    defaults.save(account.id,{},douyin={'location':{'token':current['token'],'id':'A'}})
    preparation.locations=locations
    params={**params,'video_ids':[first.id,second.id],'daily_count':2,
        'locations':{account.id:{'items':{first.id:{'token':current['token'],'id':'B'}}}}}
    prepared=preparation.prepare(params,now=now)
    assert {item['video_id']:item['location']['id'] for item in prepared['items']}=={first.id:'B',second.id:'A'}


def test_offline_builtin_default_can_be_frozen_without_granting_login_or_execution(db_engine,app_paths,tmp_path):
    preparation,account,_,params,now=setup(db_engine,app_paths,tmp_path)
    with session_scope(db_engine) as db:db.get(AccountRow,account.id).session_ref=None
    defaults=AccountPublishSettings(db_engine)
    defaults.save(account.id,{},douyin={'declaration_id':catalog()['items'][0]['id']})
    assert defaults.resolve_fields(account.id)['declaration']['label']=='内容由AI生成'
    prepared=preparation.prepare(params,now=now)
    assert prepared['token'] is None and not prepared['locally_valid']
    assert 'account_needs_verification' in prepared['items'][0]['blockers']
    assert AccountService(db_engine).get(account.id).session_ref is None


@pytest.mark.parametrize('platform,fields',[
    ('channels',{'channels_short_title':'三字短'}),
    ('channels',{'channels_short_title':'a'*17}),
    ('channels',{'channels_short_title':'不支持的#标题'}),
    ('bilibili',{'creation_statement_choice':'reprint'}),
    ('bilibili',{'creation_statement_id':-2,'creation_statement_name':'转载','reprint_source':'   '}),
    ('bilibili',{'creation_statement_choice':'reprint','reprint_source':'来源','content_authorization':True}),
    ('bilibili',{'creation_statement_choice':'no_mark','reprint_source':'来源'}),
    ('bilibili',{'copyright':'repost','source':''}),
    ('bilibili',{'copyright':'repost','source':'来源','content_authorization':True}),
])
def test_save_rejects_choices_that_the_pure_platform_contract_would_always_refuse(db_engine,platform,fields):
    account=AccountService(db_engine).create(platform,'owner','A')
    with pytest.raises(ValueError):AccountPublishSettings(db_engine).save(account.id,fields)


@pytest.mark.parametrize('platform,fields',[
    ('channels',{}),('channels',{'channels_short_title':''}),
    ('channels',{'channels_short_title':'六个字的标题'}),
    ('bilibili',{}),('bilibili',{'category_choice':'human:knowledge'}),
    ('bilibili',{'creation_statement_choice':'reprint','reprint_source':'本人确认的来源','content_authorization':False}),
])
def test_save_allows_clear_and_valid_partial_settings_without_inventing_authorization(db_engine,platform,fields):
    account=AccountService(db_engine).create(platform,'owner','A')
    saved=AccountPublishSettings(db_engine).save(account.id,fields)
    assert saved['fields']==fields
    assert saved['fields'].get('content_authorization',False) is False


@pytest.mark.parametrize('entry',['multi','daily'])
def test_all_new_entrypoints_resolve_identity_bound_provider_ids_using_the_current_provider(daily,db_engine,tmp_path,monkeypatch,entry):
    from yijianjifa.features.finished.service import FinishedVideoService
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    api,params,_,account=daily
    provider=api._publish_declarations;provider._catalog=None
    provider._fetch=lambda a:{'required':False,'items':[{'id':'account-bound-choice','label':'账号实际选项'}]}
    provider.query({'account_id':account.id})
    saved=call(api,'account.publish_settings.save',{'account_id':account.id,'fields':{},
        'douyin':{'declaration_id':'account-bound-choice'}},'save-provider-choice')
    assert saved['ok'],saved
    native=ChromiumMultiPublishAdapter(db_engine,api._paths)
    adapter=api._multi_publish.catalog.transport_for('douyin').adapter
    monkeypatch.setattr(adapter,'validate_fields',native.validate_fields,raising=False)
    try:
        if entry=='daily':
            result=daily_call(api,'mix.daily.submit',params,'provider-default-daily')
            assert result['ok'],result
            frozen=api._mix.queue.get(result['data']['id']).snapshot['daily_publish']['fields_by_account'][account.id]
        else:
            path=tmp_path/'owned-result.mp4';path.write_bytes(b'owned output')
            video=FinishedVideoService(db_engine).import_external(path,duration_ms=3000,title='标题')
            service=api._multi_publish.service
            result=service.prepare(request_id='provider-default-multi',video_ids=[video.id],
                targets=[{'account_id':account.id,'platform':'douyin'}],common_fields={},execution_mode='publish_now')
            frozen=service._drafts[result['prepared_id']]['snapshot']['items'][0]['payload']['fields']
        assert frozen['declaration']=={'id':'account-bound-choice','label':'账号实际选项'}
    finally:native.close()
