import json
import pytest
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.presets.service import PresetService


def call(api,op,p,rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=p)),trusted_origin=True)


@pytest.mark.parametrize('kind,values',[
 ('normal_publish',{'schedule_rule':{'first_delay_hours':4,'per_day':2,'days':7,'interval_hours':2},'distribution_mode':'whole_batch_per_account'}),
 ('commerce',{'commission_filter':'confirmed_only','favorite_music_mode':'random'}),
 ('mix_then_publish',{'mix_preset_id':'missing','first_delay_hours':6,'interval_hours':2}),
])
def test_four_type_catalog_persists_rules_without_creating_tasks(db_engine,app_paths,kind,values):
    api=DesktopApi(db_engine,app_paths);payload={'kind':kind,'name':'规则方案',**values}
    result=call(api,'preset.save',{'id':None,'payload':payload},'save')
    assert result['ok'],result
    assert result==call(api,'preset.save',{'id':None,'payload':payload},'save')
    stored=PresetService(db_engine).get(result['data']['id']).preset
    assert stored.kind==kind
    for k,v in values.items():assert stored.model_dump()[k]==({'mode':'daily','daily_times':None,**v} if k=='schedule_rule' else v)
    listing=call(api,'preset.list',{'query':'规则','kind':'','offset':0,'limit':20})
    assert listing['data']['total']==1
    if kind=='mix_then_publish':assert listing['data']['items'][0]['state']=='needs_repair'
    with db_engine.connect() as c:assert c.exec_driver_sql('select count(*) from mix_tasks').scalar_one()==0
    api.close(.1)


def test_preset_delete_is_atomic_and_does_not_delete_tasks_or_referencing_preset(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths);service=PresetService(db_engine)
    mix=service.create_mix(name='制作')
    result=call(api,'preset.save',{'id':None,'payload':{'kind':'mix_then_publish','name':'组合','mix_preset_id':mix.id}},'save')
    assert result['ok']
    deleted=call(api,'preset.delete',{'ids':[mix.id]},'delete')
    assert deleted['ok'] and deleted==call(api,'preset.delete',{'ids':[mix.id]},'delete')
    combo=call(api,'preset.get',{'id':result['data']['id']})['data']
    assert combo['state']=='needs_repair' and combo['payload']['mix_preset_id']==mix.id
    api.close(.1)


def test_reject_dates_unknown_keys_type_change_and_sound_bool(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    for i,extra in enumerate([{'publish_at':'2026-09-25'},{'account_ids':['a','b']}]):
        r=call(api,'preset.save',{'id':None,'payload':{'kind':'commerce','name':'bad',**extra}},str(i))
        assert not r['ok']
    assert call(api,'settings.sound.save',{'enabled':False},'sound')['ok']
    assert call(api,'settings.sound.read',{})['data']=={'enabled':False}
    assert not call(api,'settings.sound.save',{'enabled':1},'bad')['ok']
    api.close(.1)


def test_type_change_rejected_and_account_references_not_exposed_as_sessions(db_engine,app_paths):
    from yijianjifa.features.accounts.service import AccountService
    api=DesktopApi(db_engine,app_paths)
    a=AccountService(db_engine).create('douyin','synthetic-id','测试账号')
    refs=call(api,'preset.references',{})['data']
    assert refs=={'accounts':[{'id':a.id,'name':'测试账号'}]}
    first=call(api,'preset.save',{'id':None,'payload':{'kind':'commerce','name':'团购'}},'first')['data']
    result=call(api,'preset.save',{'id':first['id'],'payload':{'kind':'normal_publish','name':'wrong'}},'change')
    assert not result['ok']
    assert PresetService(db_engine).get(first['id']).kind=='commerce'
    api.close(.1)


def test_publish_presets_keep_relative_modes_and_daily_clocks_without_concrete_dates(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    try:
        for index,rule in enumerate([
            {'mode':'interval','first_delay_hours':4,'interval_hours':2.5},
            {'mode':'exact','first_delay_hours':6},
            {'mode':'immediate'},
            {'mode':'daily','per_day':2,'daily_times':['09:00','15:30']},
        ]):
            for kind in ('normal_publish','commerce'):
                payload={'kind':kind,'name':'排期规则','schedule_rule':rule}
                result=call(api,'preset.save',{'id':None,'payload':payload},f'{kind}-{index}')
                assert result['ok'],result
                stored=call(api,'preset.get',{'id':result['data']['id']})['data']['payload']['schedule_rule']
                assert stored['mode']==rule['mode']
                if 'daily_times' in rule:assert stored['daily_times']==rule['daily_times']
        for rule in [{'mode':'daily','per_day':2,'daily_times':['09:00']},{'mode':'daily','per_day':2,'daily_times':['09:00','09:00']},{'mode':'interval','daily_times':['09:00']},{'mode':'daily','daily_times':['25:00']},{'mode':'exact','start':'2026-10-01T09:00'}]:
            r=call(api,'preset.save',{'id':None,'payload':{'kind':'commerce','name':'非法规则','schedule_rule':rule}},str(rule))
            assert not r['ok']
    finally:api.close(.1)


def test_combined_preset_persists_linkage_and_old_records_default_to_per_item(db_engine,app_paths):
    from yijianjifa.features.presets.models import MixThenPublishPreset
    assert MixThenPublishPreset(name='旧组合',mix_preset_id='mix').linkage=='per_item'
    api=DesktopApi(db_engine,app_paths)
    try:
        for linkage in ('per_item','whole_batch'):
            payload={'kind':'mix_then_publish','name':'衔接方案','mix_preset_id':'missing','linkage':linkage}
            result=call(api,'preset.save',{'id':None,'payload':payload},linkage)
            assert result['ok'],result
            stored=call(api,'preset.get',{'id':result['data']['id']})['data']
            assert stored['payload']['linkage']==linkage and stored['state']=='needs_repair'
        result=call(api,'preset.save',{'id':None,'payload':{**payload,'linkage':'immediate'}},'bad-linkage')
        assert not result['ok']
    finally:api.close(.1)
