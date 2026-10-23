"""Platform choices survive reopen and remain isolated from other targets."""
import json
import uuid
import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.coordinator import AdapterValidation


class Adapter:
    verified_capabilities={'ordinary','publish_now','platform_schedule','result_query'}
    def validate_target(self,target,payload,execution):return AdapterValidation(payload['platform_user_id'],True)
    def submit(self,*a,**k):raise AssertionError('configuration cannot submit')
    def reconcile(self,*a,**k):raise AssertionError('configuration cannot query works')


def call(api,operation,params=None,request_id=None):
    result=api.dispatch(json.dumps({'v':1,'request_id':request_id or uuid.uuid4().hex,'operation':'multi_publish.'+operation,
        'params':params or {}}),trusted_origin=True)
    assert result['ok'],result
    return result['data']


@pytest.mark.parametrize('builtin', [False, True])
def test_creator_fields_freeze_per_platform_and_reopen_without_upload(db_engine,app_paths,tmp_path,builtin):
    class Bilibili(Adapter):
        platform_id='bilibili';public_field_schema={'creation_statement_id':{'type':'integer'}}
    class Channels(Adapter):
        platform_id='channels';public_field_schema={'channels_short_title':{'type':'string'}}
    accounts=AccountService(db_engine)
    bli=accounts.create('bilibili','111','合成B站')
    channel=accounts.create('channels','222','合成视频号')
    video_path=tmp_path/'owned.mp4';video_path.write_bytes(b'isolated video')
    video=FinishedVideoService(db_engine).import_external(video_path,duration_ms=1000,title='公共标题')
    adapters={'bilibili':Bilibili(),'channels':Channels()}
    fields={'category_id':1,'category_name':'测试分区','category_mode':'human',
        'creation_statement_id':-1,'creation_statement_name':'不标记',
        'content_authorization':False,'reprint_source':''}
    if builtin:fields={'category_choice':'human:tech','creation_statement_choice':'ai_generated','content_authorization':False,'reprint_source':''}
    api=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        settings=call(api,'settings.read')['settings']
        settings['targets']=[{'account_id':a.id,'platform':a.platform} for a in [bli,channel]]
        settings['fields_by_platform']={'bilibili':fields,'channels':{'channels_short_title':'测试短标题'}}
        call(api,'settings.save',{'settings':settings})
        prepared=call(api,'prepare',{'video_ids':[video.id],
            'targets':[{'account_id':a.id,'platform':a.platform,'fields':settings['fields_by_platform'][a.platform]} for a in [bli,channel]],
            'common_fields':{},'execution_mode':'publish_now'},request_id='prepare')
        assert prepared['summary']['ready']==2
        batch=call(api,'confirm',{'prepared_id':prepared['prepared_id'],'intent_id':'prepare','consent_partial':False})
    finally:assert api.close(3)
    reopened=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        assert call(reopened,'settings.read')['settings']['fields_by_platform']==settings['fields_by_platform']
        deliveries=call(reopened,'detail',{'id':batch['id']})['deliveries']
        by_platform={d['platform']:d for d in deliveries}
        assert all(d['state']=='waiting' and not d['final_started'] for d in deliveries)
        if builtin:
            assert by_platform['bilibili']['effective_fields']['category_choice']=='human:tech'
            assert by_platform['bilibili']['effective_fields']['creation_statement_choice']=='ai_generated'
            assert 'category_id' not in by_platform['bilibili']['effective_fields']
        else:assert by_platform['bilibili']['effective_fields']['creation_statement_id']==-1
        assert by_platform['bilibili']['effective_fields']['content_authorization'] is False
        assert 'channels_short_title' not in by_platform['bilibili']['effective_fields']
        assert by_platform['channels']['effective_fields']['channels_short_title']=='测试短标题'
        assert 'creation_statement_id' not in by_platform['channels']['effective_fields']
        assert 'creation_statement_choice' not in by_platform['channels']['effective_fields']
        did=by_platform['channels']['delivery_id']
        call(reopened,'target.reprepare',{'delivery_id':did,'fields':{'channels_short_title':''},'reset_fields':[]})
        changed=call(reopened,'detail',{'id':batch['id']})['deliveries']
        assert next(d for d in changed if d['delivery_id']==did)['effective_fields']['channels_short_title']==''
    finally:assert reopened.close(3)


def test_builtin_and_legacy_selectors_replace_inherited_source_and_reprepare_without_mixing(db_engine, app_paths, tmp_path):
    class Bilibili(Adapter):
        platform_id='bilibili';public_field_schema={'creation_statement_id':{'type':'integer'}}
    account=AccountService(db_engine).create('bilibili','111','离线B站')
    path=tmp_path/'owned.mp4';path.write_bytes(b'isolated media')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='离线标题')
    native={'category_id':7,'category_name':'旧分区','category_mode':'legacy',
        'creation_statement_id':-1,'creation_statement_name':'原表单文字'}
    semantic={'category_choice':'human:tech','creation_statement_choice':'ai_generated'}
    api=DesktopApi(db_engine,app_paths,publish_adapters={'bilibili':Bilibili()})
    try:
        prepared=call(api,'prepare',{'video_ids':[video.id],
            'targets':[{'account_id':account.id,'platform':'bilibili','fields':semantic}],
            'common_fields':native,'execution_mode':'publish_now'},request_id='switch-selectors')
        effective=prepared['targets'][0]['effective_fields']
        assert effective['category_choice']=='human:tech' and effective['creation_statement_choice']=='ai_generated'
        assert not set(native)&set(effective)
        batch=call(api,'confirm',{'prepared_id':prepared['prepared_id'],'intent_id':'switch-selectors','consent_partial':False})
        did=call(api,'detail',{'id':batch['id']})['deliveries'][0]['delivery_id']
        call(api,'target.reprepare',{'delivery_id':did,'fields':native,'reset_fields':[]})
        effective=call(api,'detail',{'id':batch['id']})['deliveries'][0]['effective_fields']
        assert effective['category_id']==7 and not set(semantic)&set(effective)
        call(api,'target.reprepare',{'delivery_id':did,'fields':semantic,'reset_fields':[]})
        effective=call(api,'detail',{'id':batch['id']})['deliveries'][0]['effective_fields']
        assert not set(native)&set(effective)
        call(api,'target.reprepare',{'delivery_id':did,'fields':{},'reset_fields':list(semantic)})
        effective=call(api,'detail',{'id':batch['id']})['deliveries'][0]['effective_fields']
        assert effective['category_id']==7 and not set(semantic)&set(effective)
    finally:assert api.close(3)


@pytest.mark.parametrize('value', [
    {'category_choice':'human:tech','category_id':7},
    {'creation_statement_choice':'no_mark','creation_statement_name':'伪造声明'},
    {'creation_statement_choice':'invented'},
    {'category_choice':True},
    {'category_choice':'human:invented'},
    {'category_choice':'legacy:999999'},
])
def test_same_source_conflicting_or_unsupported_builtin_choice_is_rejected(value):
    from yijianjifa.app_services.multi_publish import fields
    with pytest.raises(ValueError):fields(value)


@pytest.mark.parametrize('overrides',[
    {},
    {'category_choice':'human:tech','creation_statement_choice':'ai_generated'},
    {'category_choice':'legacy:201','creation_statement_choice':'no_mark'},
    {'category_id':201,'category_name':'科学科普','category_mode':'legacy',
     'creation_statement_id':-1,'creation_statement_name':'已保存声明'},
])
def test_native_bilibili_defaults_work_without_opening_settings_and_survive_reopen_and_reset(
        db_engine,app_paths,tmp_path,overrides):
    from yijianjifa.webui.bilibili_multi_publish import BilibiliMultiPublishAdapter
    from yijianjifa.webui.bilibili_publish_form import validate_form
    from yijianjifa.app_services.bilibili_static_catalog import DEFAULT_FIELDS
    class Bilibili(Adapter):
        platform_id='bilibili'
        public_field_schema=BilibiliMultiPublishAdapter.public_field_schema
        def plan_readiness(self,payload):
            validate_form(payload,local_choices=True)
            return None
    account=AccountService(db_engine).create('bilibili','111','离线默认测试')
    path=tmp_path/'owned.mp4';path.write_bytes(b'isolated default video')
    video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='保留成品文案',tags='#离线测试')
    adapters={'bilibili':Bilibili()}
    api=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        prepared=call(api,'prepare',{'video_ids':[video.id],
            'targets':[{'account_id':account.id,'platform':'bilibili','fields':overrides}],
            'common_fields':{},'execution_mode':'publish_now'},request_id='defaults')
        assert prepared['summary']['ready']==1
        effective=prepared['targets'][0]['effective_fields']
        assert effective['title']=='保留成品文案'
        assert effective.get('content_authorization') is not True
        if not overrides:
            assert all(effective[key]==value for key,value in DEFAULT_FIELDS.items())
        for key,value in overrides.items():assert effective[key]==value
        if 'category_id' in overrides:assert 'category_choice' not in effective
        if 'creation_statement_id' in overrides:assert 'creation_statement_choice' not in effective
        batch=call(api,'confirm',{'prepared_id':prepared['prepared_id'],'intent_id':'defaults','consent_partial':False})
    finally:assert api.close(3)
    reopened=DesktopApi(db_engine,app_paths,publish_adapters=adapters)
    try:
        child=call(reopened,'detail',{'id':batch['id']})['deliveries'][0]
        assert child['state']=='waiting' and not child['final_started']
        assert child['effective_fields']==effective
        call(reopened,'target.reprepare',{'delivery_id':child['delivery_id'],
            'fields':{},'reset_fields':list(overrides)})
        reset=call(reopened,'detail',{'id':batch['id']})['deliveries'][0]
        assert all(reset['effective_fields'][key]==value for key,value in DEFAULT_FIELDS.items())
        assert not reset['final_started'] and reset['effective_fields'].get('content_authorization') is not True
    finally:assert reopened.close(3)
