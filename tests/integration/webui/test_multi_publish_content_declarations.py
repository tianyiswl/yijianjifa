"""Offline declaration persistence and isolation; no platform session or upload."""
import json
import uuid

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish import fields, MultiPublishService
from yijianjifa.app_services.multi_publish_dto import safe_fields, detail
from yijianjifa.app_services.multi_publish_settings import defaults, validate
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterValidation, AdapterOutcome, PublishCoordinator
from yijianjifa.features.publish.platforms.catalog import PlatformCatalog


def call(api,operation,params=None,request_id=None):
    result=api.dispatch(json.dumps({'v':1,'request_id':request_id or uuid.uuid4().hex,
        'operation':'multi_publish.'+operation,'params':params or {}}),trusted_origin=True)
    assert result['ok'],result
    return result['data']


class LocalOnlyAdapter:
    verified_capabilities={'ordinary','publish_now','platform_schedule','result_query'}
    def __init__(self, platform):
        self.platform_id=platform
        choices=[{'choice':'none','name':'不添加声明'}]
        if platform in {'kuaishou','channels'}:
            choices.append({'choice':'ai_generated','name':'已核对的合成声明'})
        self.public_field_schema={'content_declaration_choice':{'type':'string','choices':choices}} if platform in {'kuaishou','channels'} else {}
        if platform=='channels':self.public_field_schema['channels_original']={'type':'boolean'}
        self.calls=[]
    def validate_target(self,target,payload,execution):
        self.calls.append('validate')
        return AdapterValidation(payload['platform_user_id'],True)
    def submit(self,*args,**kwargs):raise AssertionError('configuration must not upload or submit')
    def reconcile(self,*args,**kwargs):raise AssertionError('configuration must not query platform')


@pytest.mark.parametrize('value', [
    {'content_declaration_choice':True}, {'content_declaration_choice':1},
    {'content_declaration_choice':None}, {'content_declaration_choice':'invented'},
    {'channels_original':'true'}, {'channels_original':1}, {'channels_original':None},
])
def test_public_declaration_fields_reject_unknown_choices_and_non_boolean_original(value):
    with pytest.raises(ValueError):fields(value)


def test_safe_dto_retains_explicit_none_and_false_without_promoting_originality():
    expected={'content_declaration_choice':'none','channels_original':False}
    assert safe_fields(fields(expected))==expected
    assert 'channels_original' not in fields({'content_declaration_choice':'ai_generated'})


@pytest.mark.parametrize('platform,value', [
    ('douyin',{'content_declaration_choice':'none'}),
    ('bilibili',{'content_declaration_choice':'ai_generated'}),
    ('kuaishou',{'channels_original':False}),
    ('channels',{'content_declaration_choice':'personal_opinion'}),
])
def test_saved_platform_declaration_is_not_reused_by_another_platform(platform,value):
    settings=defaults()
    settings['targets']=[{'account_id':'synthetic-account','platform':platform}]
    settings['fields_by_platform']={platform:value}
    with pytest.raises(ValueError):validate(settings)


def test_declarations_must_be_saved_in_platform_settings():
    settings=defaults();settings['common_fields']={'channels_original':True}
    with pytest.raises(ValueError):validate(settings)


def test_reopen_presets_video_override_clear_and_target_inherit_without_upload(db_engine,app_paths,tmp_path):
    accounts=[AccountService(db_engine).create(p,'synthetic-'+p,p) for p in ('kuaishou','channels')]
    path=tmp_path/'owned.mp4';path.write_bytes(b'offline synthetic media')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='离线成品')
    adapters={a.platform:LocalOnlyAdapter(a.platform) for a in accounts}
    values={'kuaishou':{'content_declaration_choice':'ai_generated'},
            'channels':{'content_declaration_choice':'ai_generated','channels_original':True}}
    api=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        settings=call(api,'settings.read')['settings']
        settings['targets']=[{'account_id':a.id,'platform':a.platform} for a in accounts]
        settings['fields_by_platform']=values
        call(api,'settings.save',{'settings':settings})
        preset=call(api,'presets.save',{'id':None,'name':'离线声明方案','settings':settings})
        request_id=uuid.uuid4().hex
        plan=call(api,'prepare',{'video_ids':[video.id],
            'targets':[{'account_id':a.id,'platform':a.platform,'fields':values[a.platform],
                        'fields_by_video':{video.id:{'content_declaration_choice':'none',**({'channels_original':False} if a.platform=='channels' else {})}}} for a in accounts],
            'common_fields':{},'execution_mode':'publish_now'},request_id=request_id)
        assert plan['summary']['ready']==2
        saved=call(api,'confirm',{'prepared_id':plan['prepared_id'],'intent_id':request_id,'consent_partial':False})
    finally:assert api.close(3)
    reopened=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        assert call(reopened,'settings.read')['settings']['fields_by_platform']==values
        assert call(reopened,'presets.get',{'id':preset['id']})['settings']['fields_by_platform']==values
        result=call(reopened,'detail',{'id':saved['id']})
        children={d['platform']:d for d in result['deliveries']}
        assert all(d['effective_fields']['content_declaration_choice']=='none' and not d['final_started'] for d in children.values())
        assert children['channels']['effective_fields']['channels_original'] is False
        assert 'channels_original' not in children['kuaishou']['effective_fields']
        channel=children['channels']
        # Updating today's account defaults must not change the already frozen
        # batch or what its explicit "restore inherited settings" action means.
        from yijianjifa.app_services.account_publish_settings import AccountPublishSettings
        current_defaults=AccountPublishSettings(db_engine)
        owner=next(a for a in accounts if a.platform=='channels')
        current_defaults.save(owner.id,{'content_declaration_choice':'none','channels_original':False})
        call(reopened,'target.reprepare',{'delivery_id':channel['delivery_id'],
            'fields':{},'reset_fields':['content_declaration_choice','channels_original']})
        after=call(reopened,'detail',{'id':saved['id']})['deliveries']
        effective=next(d for d in after if d['platform']=='channels')['effective_fields']
        # Reset removes the flattened per-target overrides and restores the
        # account defaults captured at preparation, not today's mutable values.
        assert effective['content_declaration_choice']=='ai_generated' and effective['channels_original'] is True
        assert current_defaults.read(owner.id)['fields']=={'content_declaration_choice':'none','channels_original':False}
        assert all(not d['final_started'] for d in after)
        assert next(d for d in after if d['platform']=='kuaishou')['effective_fields']['content_declaration_choice']=='none'
        assert all(not adapter.calls for adapter in adapters.values())
    finally:assert reopened.close(3)


@pytest.mark.parametrize('platform,value', [
    ('douyin',{'content_declaration_choice':'none'}),
    ('kuaishou',{'channels_original':False}),
    ('channels',{'content_declaration_choice':'personal_opinion'}),
])
def test_wrong_platform_intent_is_held_before_any_platform_operation(db_engine,app_paths,tmp_path,platform,value):
    account=AccountService(db_engine).create(platform,'synthetic-identity',platform)
    path=tmp_path/'owned.mp4';path.write_bytes(b'synthetic')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='离线视频')
    adapter=LocalOnlyAdapter(platform)
    api=DesktopApi(db_engine,app_paths,publish_adapters={platform:adapter})
    try:
        plan=call(api,'prepare',{'video_ids':[video.id],
            'targets':[{'account_id':account.id,'platform':platform,'fields':value}],
            'common_fields':{},'execution_mode':'publish_now'})
        assert plan['summary']['ready']==0
        assert plan['targets'][0]['hold_reason']=='platform_fields_require_validation'
        assert adapter.calls==[]
    finally:assert api.close(3)


@pytest.mark.parametrize('code', [
    'kuaishou_author_statement_required','kuaishou_declaration_options_unverified',
    'channels_declaration_options_unverified','channels_original_unavailable','channels_original_video_too_short',
])
def test_declaration_failure_survives_coordinator_and_dto_without_final_marker(db_engine,app_paths,tmp_path,code):
    platform='kuaishou' if code.startswith('kuaishou_') else 'channels'
    class DeclarationFailure(LocalOnlyAdapter):
        def submit(self,payload,*,context,before_final):
            self.calls.append('checked-local-failure')
            # No final control, network, or journal is invoked in this stub.
            return AdapterOutcome('pre_submit_failed',error_code=code)
    adapter=DeclarationFailure(platform)
    account=AccountService(db_engine).create(platform,'synthetic-identity',platform)
    path=tmp_path/'owned.mp4';path.write_bytes(b'offline failure sample')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='失败原因样例')
    service=MultiPublishService(db_engine,app_paths,catalog=PlatformCatalog(transports={platform:adapter}))
    plan=service.prepare(request_id='prepare-failure',video_ids=[video.id],
        targets=[{'account_id':account.id,'platform':platform}],common_fields={},execution_mode='publish_now')
    saved=service.save(plan['prepared_id'],request_id='prepare-failure')
    coordinator=PublishCoordinator(service)
    coordinator.run_once(saved['id'])
    child=detail(service,saved['id'],coordinator=coordinator)['deliveries'][0]
    assert child['state']=='pre_submit_failed' and child['error_code']==code
    assert '尚未提交' in child['reason_message'] and child['final_started'] is False
    assert 'reprepare' in child['allowed_actions'] and 'resume' not in child['allowed_actions']
