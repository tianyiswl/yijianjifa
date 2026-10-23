"""Client viewer commands stay bound to one original delivery; no browser IO."""
import json

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
from yijianjifa.webui.chromium_publish_session import LiveSession


@pytest.mark.parametrize('action,extra', [('visible', {}), ('refresh', {}), ('scroll', {'code': [0, 450]})])
def test_view_commands_queue_only_original_page_operation(db_engine, app_paths, monkeypatch, action, extra):
    adapter = ChannelsMultiPublishAdapter(db_engine, app_paths)
    session = LiveSession({}, {'delivery_id': 'synthetic-delivery'})
    session.final = True
    session.request_id = session.broker.request('fixture', '合成账号', 'complex', '离线验证')
    adapter.sessions['synthetic'] = session
    api = DesktopApi(db_engine, app_paths, publish_adapters={'channels': adapter})
    jobs = []
    monkeypatch.setattr(api._multi_publish, 'enqueue', lambda *args: jobs.append(args))
    params = {'id': session.request_id, 'delivery_id': 'synthetic-delivery', 'action': action, **extra}
    def call(values):
        return api.dispatch(json.dumps({'v': 1, 'request_id': 'synthetic-command',
            'operation': 'multi_publish.intervention.respond', 'params': values}), trusted_origin=True)
    try:
        assert not call({**params, 'delivery_id': 'another-delivery'})['ok']
        assert session.broker.consume(session.request_id) is None and jobs == []
        accepted=call(params)
        assert accepted['ok'] and accepted['data']['view_revision']==1
        assert session.broker.consume(session.request_id) == (action, extra.get('code', ''))
        assert jobs == [('verification_view', ('channels','synthetic-delivery',session.request_id,1))]
        assert session.final and session.page is None
    finally:
        session.broker.finish(session.request_id)
        api.close()


@pytest.mark.parametrize('action,extra',[('visible',{}),('refresh',{}),('scroll',{'code':[0,450]})])
def test_pre_final_view_does_not_require_publication_resume_consent(db_engine,app_paths,monkeypatch,action,extra):
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths)
    session=LiveSession({}, {'delivery_id':'synthetic-delivery'})
    session.request_id=session.broker.request('fixture','合成账号','login','原画面')
    adapter.sessions['synthetic']=session
    api=DesktopApi(db_engine,app_paths,publish_adapters={'channels':adapter})
    jobs=[]
    monkeypatch.setattr(api._multi_publish,'enqueue',lambda *args:jobs.append(args))
    monkeypatch.setattr(adapter,'_grant_resume',lambda s:pytest.fail('view must not grant final authorization'))
    params={'id':session.request_id,'delivery_id':'synthetic-delivery','action':action,**extra}
    try:
        result=api.dispatch(json.dumps({'v':1,'request_id':'view-command','operation':'multi_publish.intervention.respond','params':params}),trusted_origin=True)
        assert result['ok'] and result['data']['view_revision']==1
        assert jobs==[('verification_view',('channels','synthetic-delivery',session.request_id,1))]
        assert not session.resume_authorized
    finally:session.broker.finish(session.request_id);api.close()


@pytest.mark.parametrize('action,extra',[('done',{}),('code',{'code':'1234'}),('pointer',{'code':[0,0]})])
def test_pre_final_continue_and_pointer_keep_same_account_gate(db_engine,app_paths,action,extra):
    adapter=ChannelsMultiPublishAdapter(db_engine,app_paths)
    session=LiveSession({}, {'delivery_id':'synthetic-delivery'})
    session.request_id=session.broker.request('fixture','合成账号','sms' if action=='code' else 'login','原画面')
    adapter.sessions['synthetic']=session
    api=DesktopApi(db_engine,app_paths,publish_adapters={'channels':adapter})
    params={'id':session.request_id,'delivery_id':'synthetic-delivery','action':action,**extra}
    try:
        result=api.dispatch(json.dumps({'v':1,'request_id':'continue-command','operation':'multi_publish.intervention.respond','params':params}),trusted_origin=True)
        assert not result['ok'] and session.broker.consume(session.request_id) is None
    finally:session.broker.finish(session.request_id);api.close()


@pytest.mark.parametrize('action,extra',[('code',{'code':'123456'}),('resend_code',{})])
def test_post_final_sms_action_stays_on_original_page_before_reconciliation(db_engine,app_paths,monkeypatch,action,extra):
    from yijianjifa.webui.chromium_multi_publish import ChromiumMultiPublishAdapter
    adapter=ChromiumMultiPublishAdapter(db_engine,app_paths)
    session=LiveSession({}, {'delivery_id':'synthetic-delivery'})
    session.final=True
    session.request_id=session.broker.request('fixture','合成账号','sms','原短信验证')
    adapter.sessions['synthetic']=session
    api=DesktopApi(db_engine,app_paths,publish_adapters={'douyin':adapter})
    jobs=[]
    monkeypatch.setattr(api._multi_publish,'enqueue',lambda *args:jobs.append(args))
    try:
        result=api.dispatch(json.dumps({'v':1,'request_id':'synthetic-code',
          'operation':'multi_publish.intervention.respond','params':{'id':session.request_id,
          'delivery_id':'synthetic-delivery','action':action,**extra}}),trusted_origin=True)
        assert result['ok'],result
        revision=result['data']['view_revision']
        assert jobs==[('verification_view',('douyin','synthetic-delivery',session.request_id,revision))]
        assert session.broker.consume(session.request_id)==(action,extra.get('code',''))
        assert '123456' not in repr(api._multi_publish.intervention_status())
    finally:session.broker.finish(session.request_id);api.close()
