"""Final review regressions: synthetic SQLite/media and no platform calls."""
import json
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select
from test_multi_publish_api import fixture,call,saved
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.features.publish.coordinator import AdapterValidation,AdapterOutcome
from yijianjifa.app_services.multi_publish_runtime import AUTH,PAUSE,update_authorization
from yijianjifa.app_services.multi_publish_settings import defaults


def test_pause_during_final_identity_observation_prevents_marker_and_external_call(fixture,db_engine):
    api,_,_,adapter=fixture;batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    with session_scope(db_engine) as s:update_authorization(s,batch['id'],add=[did])
    observations=[];finals=[]
    def validate(target,payload,execution):
        observations.append(True)
        if len(observations)==2:assert call(api,'target.pause',{'delivery_id':did},'pause-final')['data']['paused']
        return AdapterValidation(payload['platform_user_id'],True)
    def submit(payload,*,context,before_final):
        before_final();finals.append(True);return AdapterOutcome('published')
    adapter.validate_target=validate;adapter.submit=submit
    api._multi_publish.coordinator.run_once(batch['id'],[did])
    with session_scope(db_engine) as s:
        attempt=s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did))
        assert attempt.final_json is None
        assert s.get(Setting,PAUSE+did)
    assert not finals


def test_expired_confirm_has_durable_proof_of_nonacceptance(fixture):
    api,account,videos,_=fixture
    p=api._multi_publish.service.prepare(request_id='expired-plan',video_ids=[videos[0].id],targets=[{'account_id':account.id,'platform':'douyin'}],common_fields={},execution_mode='publish_now',now=datetime.now(timezone.utc)-timedelta(hours=1))
    params={'prepared_id':p['prepared_id'],'intent_id':'expired-plan','consent_partial':False}
    r=call(api,'confirm',params,'expired-write');assert not r['ok']
    proof=call(api,'receipt',{'request_id':'expired-write'})['data']
    assert proof['acceptance']=='rejected' and proof['operation']=='confirm' and proof['request_id']=='expired-write'
    assert call(api,'confirm',params,'expired-write')==r
    assert api._multi_publish.service.receipt('expired-plan') is None


def test_changed_execution_check_has_negative_receipt_without_authorization(fixture,db_engine):
    api,*_=fixture;batch=saved(fixture);did=call(api,'detail',{'id':batch['id']})['data']['deliveries'][0]['id']
    r=call(api,'execution.start',{'id':batch['id'],'delivery_ids':[did],'digest':'0'*64,'consent':True,'consent_partial':True},'stale-start')
    assert not r['ok']
    assert call(api,'receipt',{'request_id':'stale-start'})['data']['acceptance']=='rejected'
    with session_scope(db_engine) as s:assert s.get(Setting,AUTH+batch['id']) is None


@pytest.mark.parametrize('hours',['0.5','1.5','336'])
def test_half_hour_settings_real_api_roundtrip(fixture,hours):
    api,*_=fixture;value=defaults();value['schedule'].update(interval=hours,per_day='2',days='2')
    result=call(api,'settings.save',{'settings':value},'hours-'+hours.replace('.','_'))
    assert result['ok'],result
    assert call(api,'settings.read')['data']['settings']==value


@pytest.mark.parametrize('key,value',[('interval','0'),('interval','0.25'),('interval','336.5'),('interval','NaN'),('per_day','1.5'),('days','2.5')])
def test_schedule_rejects_out_of_contract_values(fixture,key,value):
    setting=defaults();setting['schedule'][key]=value
    assert not call(fixture[0],'settings.save',{'settings':setting},'bad-settings')['ok']


def test_held_bili_target_repairs_same_delivery_without_changing_sibling(fixture,db_engine):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.publish.platforms.catalog import PlatformCatalog
    from yijianjifa.app_services.multi_publish_runtime import GuardedAdapter
    from test_multi_publish_api import HostAdapter
    api,account,videos,_=fixture
    bili=AccountService(db_engine).create('bilibili','222','合成B站')
    adapter=HostAdapter();adapter.platform_id='bilibili'
    adapter.public_field_schema={'category_id':{'choices':[{'id':101,'label':'合成分区'}]},'copyright':{'type':'string'},'source':{'type':'string'}}
    adapters=dict(api._multi_publish.catalog._transports);adapters['bilibili']=GuardedAdapter(adapter,api._multi_publish)
    api._multi_publish.catalog=api._multi_publish.service.catalog=PlatformCatalog(transports=adapters)
    p=call(api,'prepare',{'video_ids':[videos[0].id],'targets':[{'account_id':account.id,'platform':'douyin'},{'account_id':bili.id,'platform':'bilibili'}],'common_fields':{},'execution_mode':'publish_now'},'bili-plan')['data']
    b=call(api,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'bili-plan','consent_partial':False},'bili-save')['data']
    before=call(api,'detail',{'id':b['id']})['data']['deliveries'];child=before[1]
    assert child['readiness_reason']=='category_required'
    assert child['field_schema']['category_id']['choices']==[{'id':101,'label':'合成分区'}]
    assert '分区' in child['reason_message']
    r=call(api,'target.reprepare',{'delivery_id':child['id'],'fields':{'category_id':101,'copyright':'repost','source':'合成来源','cover':None},'reset_fields':[]},'bili-fix')
    assert r['ok'] and r['data']['id']==child['id'] and r['data']['attempt']==2
    after=call(api,'detail',{'id':b['id']})['data']['deliveries']
    assert after[0]==before[0] and after[1]['state']=='waiting'
    assert after[1]['effective_fields']['cover'] is None and not after[1]['final_started']
    assert not adapter.calls
    reset=call(api,'target.reprepare',{'delivery_id':child['id'],'fields':{},'reset_fields':['cover','category_id','copyright','source']},'bili-reset')
    assert reset['ok'] and reset['data']['attempt']==3
    current=call(api,'detail',{'id':b['id']})['data']['deliveries'][1]
    assert current['readiness_reason']=='category_required' and current['effective_fields'].get('cover')==child['effective_fields'].get('cover')


def test_default_new_platform_reason_is_specific_and_still_unconfigured(fixture,db_engine):
    from yijianjifa.features.accounts.service import AccountService
    api,_,videos,_=fixture;bili=AccountService(db_engine).create('bilibili','222','合成B站')
    p=call(api,'prepare',{'video_ids':[videos[0].id],'targets':[{'account_id':bili.id,'platform':'bilibili'}],'common_fields':{},'execution_mode':'publish_now'},'unconfigured-plan')['data']
    b=call(api,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'unconfigured-plan','consent_partial':False},'unconfigured-save')['data']
    child=call(api,'detail',{'id':b['id']})['data']['deliveries'][0]
    assert child['readiness_reason']=='official_service_not_configured'
    assert child['field_schema']['category_id']['choices']==[]
    assert '未配置' in child['reason_message'] and not child['can_submit']


def test_ambiguous_post_save_failure_never_writes_negative_receipt(fixture,monkeypatch):
    from test_multi_publish_api import prepared
    api,*_=fixture;p=prepared(fixture,'lost-save');save=api._multi_publish.service.save
    def committed_then_lost(*a,**kw):
        save(*a,**kw);raise ValueError('synthetic lost reply after commit')
    monkeypatch.setattr(api._multi_publish.service,'save',committed_then_lost)
    assert not call(api,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'lost-save','consent_partial':False},'lost-confirm')['ok']
    assert call(api,'receipt',{'request_id':'lost-confirm'})['data'] is None
    assert call(api,'receipt',{'request_id':'lost-save'})['data']['id']


def test_original_batch_receipt_wins_over_expired_prepare(fixture):
    from test_multi_publish_api import prepared
    api,*_=fixture;p=prepared(fixture,'accepted-plan')
    params={'prepared_id':p['prepared_id'],'intent_id':'accepted-plan','consent_partial':False}
    original=call(api,'confirm',params,'accepted-confirm')['data']
    api._multi_publish.service._drafts[p['prepared_id']]['expires']=datetime.now(timezone.utc)-timedelta(hours=1)
    retry=call(api,'confirm',params,'accepted-confirm-recover')
    assert retry['ok'] and retry['data']==original


def test_closed_pre_dispatch_refusal_has_negative_receipt_only_without_original_commit(fixture,db_engine):
    from test_multi_publish_api import prepared
    api,*_=fixture;p=prepared(fixture,'gated-plan')
    api._multi_publish.closed.set()
    r=call(api,'confirm',{'prepared_id':p['prepared_id'],'intent_id':'gated-plan','consent_partial':False},'gated-write')
    assert r['error']['code']=='app_closing'
    assert call(api,'receipt',{'request_id':'gated-write'})['data']['acceptance']=='rejected'
