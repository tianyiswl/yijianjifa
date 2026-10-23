import json
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.webui.publish_intervention import broker

def call(api,op,params):
    return api.dispatch(json.dumps({'v':1,'request_id':'a'*32,'operation':op,'params':params}),trusted_origin=True)

def test_mode_resets_to_background_on_client_start_and_invalid_choice_rejected(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    assert call(api,'publish.browser_mode.read',{})['data']['mode']=='background'
    assert call(api,'publish.browser_mode.save',{'mode':'visible'})['ok']
    assert call(api,'publish.browser_mode.read',{})['data']['mode']=='visible'
    assert DesktopApi(db_engine,app_paths).dispatch(json.dumps({'v':1,'request_id':'b'*32,'operation':'publish.browser_mode.read','params':{}}),trusted_origin=True)['data']['mode']=='background'
    assert not call(api,'publish.browser_mode.save',{'mode':'stealth'})['ok']
    api.close()

def test_api_verification_never_returns_input_value(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths);r=broker.request('fixture','测试','sms','离线测试')
    try:
        assert call(api,'publish.intervention.respond',{'id':r,'action':'code','code':'123456'})['ok']
        assert '123456' not in str(call(api,'publish.intervention.status',{}))
        assert broker.consume(r)==('code','123456')
    finally:broker.finish(r);api.close()
