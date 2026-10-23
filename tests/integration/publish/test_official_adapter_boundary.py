"""Real coordinator/temporary SQL/owned resources/local HTTP; synthetic grants."""
import hashlib
import importlib
import importlib.util
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.multi_publish_runtime import MultiPublishRuntime,update_authorization
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.deliveries import DeliveryAttemptRow
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting
from test_official_clients import local_server,executor,module


def auth():return module('official_authorization')


def setup(db_engine,app_paths,tmp_path,local_server,*,platform='bilibili',budget=5):
    a=auth();account=AccountService(db_engine).create(platform,'synthetic-uid','合成')
    video_path=tmp_path/'synthetic.mp4';video_path.write_bytes(b'synthetic-short-video')
    video=FinishedVideoService(db_engine).import_external(video_path,duration_ms=1000,title='标题')
    client=module(platform+'.client').BilibiliClient(executor(local_server,bili=True)) if platform=='bilibili' else module('kuaishou.client').KuaishouClient(module('kuaishou.client').KuaishouAuthorizedExecutor(executor(local_server),'synthetic-app','SENTINEL-access'),gateway_hosts={'approved.example'})
    scopes=frozenset({'USER_INFO','ARC_BASE'} if platform=='bilibili' else {'user_info','user_video_publish','user_video_info'})
    issuer='synthetic-key' if platform=='bilibili' else 'synthetic-app'
    binding=a.IdentityBinding(account.id,platform,issuer,'synthetic-sub','synthetic-uid')
    observation=a.GrantObservation(issuer,'synthetic-sub','synthetic-uid',scopes)
    current=[observation]
    service=a.ClientExecutionService(bindings={account.id:binding},client_for=lambda _:client,observe=lambda _,deadline:current[0],category_choices=[{'id':138,'label':'生活 / 搞笑'}])
    adapters=a.make_official_adapters(db_engine,service=service,budget_s=budget)
    runtime=MultiPublishRuntime(db_engine,app_paths,adapters=adapters)
    return runtime,account,video,service,current


def save(runtime,account,video,*,extra=None):
    fields={'title':'标题','body':'正文','tags':'tag1,tag2','cover':None}
    if account.platform=='bilibili':fields.update(category_id=138,copyright='original')
    if extra:fields.update(extra)
    p=runtime.service.prepare(request_id='plan',video_ids=[video.id],targets=[{'account_id':account.id,'platform':account.platform}],common_fields=fields,execution_mode='publish_now')
    receipt=runtime.service.save(p['prepared_id'],request_id='plan')
    child=runtime.service.get_task(receipt['id'])['deliveries'][0]
    with session_scope(runtime.engine) as s:update_authorization(s,receipt['id'],add=[child['id']])
    return receipt,child


def upload_responses(local_server,platform='bilibili'):
    responses=local_server[2]
    if platform=='bilibili':responses.extend([{'code':0,'data':[{'id':160,'parent':0,'name':'生活','children':[{'id':138,'parent':160,'name':'搞笑'}]}]},{'code':0,'data':{'upload_token':'SENTINEL-upload'}},{'code':0},{'code':0}])
    else:responses.extend([{'result':1,'upload_token':'SENTINEL-upload','endpoint':'approved.example'},{'result':1,'existed':False,'fragment_index':-1,'fragment_list':[],'fragment_index_bytes':0},{'result':1,'size':21,'checksum':'synthetic'},{'result':1}])


def attempt(engine,did):
    with session_scope(engine) as s:return s.scalar(select(DeliveryAttemptRow).where(DeliveryAttemptRow.delivery_id==did))


def test_native_factory_default_is_unconfigured_no_network(db_engine,app_paths):
    a=auth();assert a.make_official_adapters(db_engine)=={}
    api=DesktopApi(db_engine,app_paths)
    result=api.dispatch(json.dumps({'v':1,'request_id':'catalog','operation':'multi_publish.catalog','params':{}}),trusted_origin=True)['data']['items']
    for platform in ('kuaishou','bilibili','channels'):
        item=next(i for i in result if i['platform']==platform)
        assert item['transport_available'] is False and item['ordinary_ready'] is False and item['login_ready'] is False
        assert item['reason']=='official_service_not_configured' if platform!='channels' else item['reason']=='official_contract_pending'
    assert '按标题、正文、标签顺序' in next(i for i in result if i['platform']=='kuaishou')['field_schema']['title']['help']
    api.close(1)


def test_bili_final_server_observes_marker_and_reconcile_only_original(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,service,current=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);did=child['id'];upload_responses(local_server)
    def final(_):
        assert attempt(db_engine,did).final_json is not None
        return {'code':0,'data':{'resource_id':'BV-synthetic'}}
    local_server[2].append(final)
    assert runtime.coordinator.run_once(receipt['id'])=='platform_accepted'
    row=attempt(db_engine,did);assert runtime.registry.stopped(row.owner_hash)
    local_server[2].append({'code':0,'data':{'resource_id':'BV-synthetic','addit_info':{'state':0}}})
    assert runtime.coordinator.reconcile_delivery(did)=='published'
    assert sum('/add-by-utoken' in c[1] for c in local_server[1])==1
    assert local_server[1][-1][0]=='GET' and 'resource_id=BV-synthetic' in local_server[1][-1][1]
    assert 'SENTINEL' not in (row.result_json or '')+(row.context_json or '')


@pytest.mark.parametrize('change',[{'issuer':'wrong'},{'subject':'wrong'},{'canonical_uid':'wrong'},{'scopes':frozenset({'USER_INFO'})}])
def test_observed_issuer_subject_uid_scope_mismatch_blocks_all_upload(db_engine,app_paths,tmp_path,local_server,change):
    runtime,account,video,_,current=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video)
    current[0]=replace(current[0],**change)
    assert runtime.coordinator.run_once(receipt['id'])=='pre_submit_failed'
    assert not local_server[1] and attempt(db_engine,child['id']).final_json is None


def test_renewed_identity_fails_at_final_after_upload_without_final_request(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,current=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);upload_responses(local_server)
    def complete(_):
        current[0]=replace(current[0],issuer='different-issuer');return {'code':0}
    local_server[2][-1]=complete
    assert runtime.coordinator.run_once(receipt['id'])=='pre_submit_failed'
    assert attempt(db_engine,child['id']).final_json is None
    assert all('/add-by-utoken' not in c[1] for c in local_server[1])


@pytest.mark.parametrize('final_response',[{'code':0,'data':{}},{'code':True,'data':{'resource_id':'BV-synthetic'}},(200,b'not json')])
def test_missing_or_invalid_final_receipt_stays_unknown_never_reposts(db_engine,app_paths,tmp_path,local_server,final_response):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);upload_responses(local_server);local_server[2].append(final_response)
    assert runtime.coordinator.run_once(receipt['id'])=='result_unknown'
    assert runtime.coordinator.run_once(receipt['id']) is None
    assert runtime.coordinator.reconcile_delivery(child['id'])=='result_unknown'
    assert sum('/add-by-utoken' in c[1] for c in local_server[1])==1
    assert attempt(db_engine,child['id']).final_json is not None


def test_final_timeout_is_unknown_and_synchronous_worker_stops(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server,budget=.15)
    receipt,child=save(runtime,account,video);upload_responses(local_server)
    def delayed(_):time.sleep(.25);return {'code':0,'data':{'resource_id':'BV-synthetic'}}
    local_server[2].append(delayed)
    assert runtime.coordinator.run_once(receipt['id'])=='result_unknown'
    assert attempt(db_engine,child['id']).final_json is not None
    assert runtime.registry.stopped(attempt(db_engine,child['id']).owner_hash)
    assert runtime.coordinator.reconcile_delivery(child['id'])=='result_unknown'
    assert len([c for c in local_server[1] if '/add-by-utoken' in c[1]])==1


def test_upload_timeout_is_pre_final_and_no_final_marker(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server,budget=.15)
    receipt,child=save(runtime,account,video)
    def delayed(_):time.sleep(.25);return {'code':0,'data':[]}
    local_server[2].append(delayed)
    assert runtime.coordinator.run_once(receipt['id'])=='pre_submit_failed'
    assert attempt(db_engine,child['id']).final_json is None
    assert all('/add-by-utoken' not in c[1] for c in local_server[1])


def test_bilibili_selected_cover_hold_and_no_silent_drop(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video,extra={'cover':{'kind':'first_frame','frame_ms':0}})
    assert child['state']=='waiting_configuration'
    assert runtime.coordinator.run_once(receipt['id']) is None and not local_server[1]


def test_kuaishou_required_cover_without_owned_image_holds(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server,platform='kuaishou')
    receipt,child=save(runtime,account,video)
    assert child['state']=='waiting_configuration'
    assert runtime.coordinator.run_once(receipt['id']) is None and not local_server[1]


def test_kuaishou_prepare_source_then_owned_cover_and_frozen_caption(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server,platform='kuaishou')
    # Real PNG encoding generated without user media or image libraries.
    import struct,zlib
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xff\x00\x00'))+chunk(b'IEND',b'')
    digest=hashlib.sha256(png).hexdigest();cover_dir=app_paths.data_dir/'covers';cover_dir.mkdir();(cover_dir/(digest+'.png')).write_bytes(png)
    with session_scope(db_engine) as s:s.add(Setting(key='finished-cover:'+video.id,value_json=json.dumps({'kind':'first_frame','frame_ms':0,'sha256':digest})))
    # Cover source metadata is frozen by prepare; owned paths exist only after save.
    p=runtime.service.prepare(request_id='plan',video_ids=[video.id],targets=[{'account_id':account.id,'platform':'kuaishou'}],common_fields={'title':'标题','body':'正文','tags':'#标签'},execution_mode='publish_now')
    assert p['targets'][0]['state']=='waiting'
    receipt=runtime.service.save(p['prepared_id'],request_id='plan');child=runtime.service.get_task(receipt['id'])['deliveries'][0]
    with session_scope(db_engine) as s:update_authorization(s,receipt['id'],add=[child['id']])
    upload_responses(local_server,'kuaishou');local_server[2].append({'result':1,'video_info':{'photo_id':'photo-synthetic','pending':True}})
    assert runtime.coordinator.run_once(receipt['id'])=='platform_accepted'
    final=local_server[1][-1];assert '标题\n正文\n#标签'.encode() in final[3] and png in final[3]
    assert sum('/photo/publish' in c[1] for c in local_server[1])==1


def test_operator_final_deduplication_returns_original_receipt_no_second_post(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,service,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);upload_responses(local_server);local_server[2].append({'code':0,'data':{'resource_id':'BV-synthetic'}})
    assert runtime.coordinator.run_once(receipt['id'])=='platform_accepted'
    context=json.loads(attempt(db_engine,child['id']).context_json)
    def forbidden():raise AssertionError('duplicate final callback')
    result=service.execute_final(context['handle'],before_final=forbidden,deadline=time.monotonic()+5)
    assert result.receipt_reference=={'resource_id':'BV-synthetic'}
    assert sum('/add-by-utoken' in c[1] for c in local_server[1])==1


def test_pre_final_expired_execute_budget_has_no_final_or_unknown_receipt(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,service,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);upload_responses(local_server)
    adapter=runtime.catalog.transport_for('bilibili');adapter.bind_delivery(digest=attempt(db_engine,child['id']).request_digest,delivery_id=child['id'],task_id=receipt['id'])
    payload=json.loads(attempt(db_engine,child['id']).snapshot_json)
    handle=service.stage(adapter.adapter._binding,payload,deadline=time.monotonic()+5)
    with pytest.raises(module('official_http').ProtocolError):service.execute_final(handle,before_final=lambda:None,deadline=time.monotonic()-1)
    assert all('/add-by-utoken' not in c[1] for c in local_server[1])


def test_restart_query_does_not_pick_other_delivery_by_video_or_account(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,_,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);upload_responses(local_server);local_server[2].append({'code':0,'data':{'resource_id':'BV-synthetic'}})
    assert runtime.coordinator.run_once(receipt['id'])=='platform_accepted'
    row=attempt(db_engine,child['id']);payload=json.loads(row.snapshot_json);payload['receipt_reference']={'resource_id':'BV-synthetic'};payload['fields']['title']='changed-claim'
    local_server[2].append({'code':0,'data':{'resource_id':'BV-synthetic','addit_info':{'state':0}}})
    assert runtime.catalog.transport_for('bilibili').reconcile(payload,context=json.loads(row.context_json)).state=='result_unknown'
    assert all('/archive/view' not in c[1] for c in local_server[1])


def test_native_unconfigured_can_save_honest_waiting_configuration(db_engine,app_paths,tmp_path):
    api=DesktopApi(db_engine,app_paths);account=AccountService(db_engine).create('bilibili','synthetic','合成')
    path=tmp_path/'clip.mp4';path.write_bytes(b'synthetic');video=FinishedVideoService(db_engine).import_external(path,duration_ms=1000,title='标题')
    prepared=api._multi_publish.service.prepare(request_id='plan',video_ids=[video.id],targets=[{'account_id':account.id,'platform':'bilibili'}],common_fields={'title':'标题','tags':'tag','category_id':138,'copyright':'original','cover':None},execution_mode='publish_now')
    assert prepared['targets'][0]['state']=='waiting_configuration'
    receipt=api._multi_publish.service.save(prepared['prepared_id'],request_id='plan')
    assert api._multi_publish.service.get_task(receipt['id'])['deliveries'][0]['can_submit'] is False
    api.close(1)


def test_factory_sanitizes_cached_choices_before_public_schema(db_engine):
    a=auth()
    service=SimpleNamespace(binding=lambda _:None,observe=lambda *a,**k:None,stage=lambda *a,**k:None,execute_final=lambda *a,**k:None,query=lambda *a,**k:None,category_choices=lambda:[{'id':True,'label':'invalid','token':'SENTINEL'}])
    with pytest.raises(module('official_http').ProtocolError):a.make_official_adapters(db_engine,service=service)


def test_operator_stage_rejects_payload_changed_from_bound_digest_before_network(db_engine,app_paths,tmp_path,local_server):
    runtime,account,video,service,_=setup(db_engine,app_paths,tmp_path,local_server)
    receipt,child=save(runtime,account,video);row=attempt(db_engine,child['id']);payload=json.loads(row.snapshot_json);payload['fields']['body']='changed'
    with pytest.raises(module('official_http').ProtocolError):service.stage((receipt['id'],child['id'],1,row.request_digest),payload,deadline=time.monotonic()+5)
    assert not local_server[1]


def test_bili_official_observer_reads_subject_scopes_under_explicit_issuer(local_server):
    a=auth();assert hasattr(a,'observe_bilibili_grant'),'official observer composition missing'
    client=module('bilibili.client').BilibiliClient(executor(local_server,bili=True))
    local_server[2].extend([{'code':0,'data':{'openid':'observed-sub'}},{'code':0,'data':{'openid':'observed-sub','scopes':['USER_INFO','ARC_BASE']}}])
    binding=a.IdentityBinding('account','bilibili','synthetic-key','observed-sub','canonical-uid')
    observation=a.observe_bilibili_grant(binding,client,deadline=time.monotonic()+5)
    assert observation==a.GrantObservation('synthetic-key','observed-sub','canonical-uid',frozenset({'USER_INFO','ARC_BASE'}))
    assert [c[1] for c in local_server[1]]==['/arcopen/fn/user/account/info','/arcopen/fn/user/account/scopes']
