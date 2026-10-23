import json
import pytest
from sqlalchemy import text
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from test_library_management import call


def prepared(db_engine,app_paths,tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'fixture')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='C1',title='before',voice='你好',body='',tags='',full='')
    return DesktopApi(db_engine,app_paths)


def test_options_are_frozen_and_task_detail_hides_paths_and_internal_error(db_engine,app_paths,tmp_path):
    api=prepared(db_engine,app_paths,tmp_path)
    options=dict(voice=None,speed=1.2,sentence_cut=True,spoken_enabled=True,full_enabled=True,batch_full_text='全程文字',keep_original_audio=False,original_gain=.4,voice_gain=.9)
    result=call(api,'mix.submit',dict(quantity=1,**options),'submit')
    assert result['ok'],result
    id=result['data']['id']
    with db_engine.begin() as c:
        snapshot=json.loads(c.execute(text('SELECT snapshot_json FROM mix_tasks WHERE id=:id'),{'id':id}).scalar_one())
        expected={**options,'spoken_style':{'size':75,'y_percent':77},'full_style':{'size':100,'y_percent':12}}
        assert snapshot['options']==expected
        c.execute(text('UPDATE mix_tasks SET last_error=:error WHERE id=:id'),{'id':id,'error':'sensitive/internal/file/path'})
    CopyRepository(db_engine).update(__import__('dataclasses').replace(CopyRepository(db_engine).get('C1'),title='after'))
    detail=call(api,'task.detail',dict(id=id),'detail')
    assert detail['ok'],detail
    assert detail['data']['copies'][0]['title']=='before'
    assert detail['data']['options']==expected
    assert str(tmp_path) not in json.dumps(detail) and 'sensitive/internal' not in json.dumps(detail)
    assert detail['data']['has_error'] is True
    api.close(.1)


@pytest.mark.parametrize('options',[{'speed':True},{'speed':float('nan')},{'speed':3},{'original_gain':-1},{'voice_gain':float('inf')},{'sentence_cut':'true'},{'voice':[]},{'auto_publish':True},{'output_directory':'/tmp/unauthorized'}])
def test_rejects_invalid_or_unapproved_options_before_task_creation(db_engine,app_paths,tmp_path,options):
    api=prepared(db_engine,app_paths,tmp_path)
    result=call(api,'mix.submit',dict(quantity=1,**options))
    assert not result['ok']
    with db_engine.connect() as c:assert c.exec_driver_sql('SELECT count(*) FROM mix_tasks').scalar_one()==0
    api.close(.1)


def test_styles_survive_submission_and_detail(db_engine, app_paths, tmp_path):
    api=prepared(db_engine,app_paths,tmp_path)
    style={'font':'sans','size':34,'color':'#123456','position':'top','chars':12}
    result=call(api,'mix.submit',{'quantity':1,'spoken_style':style},'style-submit')
    assert result['ok'],result
    detail=call(api,'task.detail',{'id':result['data']['id']},'style-detail')
    assert detail['data']['options']['spoken_style']==style
    api.close(.1)


@pytest.mark.parametrize('options',[
    {'background_music':{'mode':'fixed'}},
    {'background_music':{'mode':'none','path':'/tmp/test.wav'}},
    {'transition':{'mode':'fixed','name':'fade;bad'}},
    {'transition':{'mode':'random','duration_ms':True}},
    {'music_gain':-1},
])
def test_effect_validation_rejects_before_queue(db_engine,app_paths,tmp_path,options):
    api=prepared(db_engine,app_paths,tmp_path)
    result=call(api,'mix.submit',{'quantity':1,**options})
    assert not result['ok']
    assert not api._mix.tasks()
    api.close(.1)


def test_duration_ranges_are_validated_and_reported(db_engine,app_paths,tmp_path):
    api=prepared(db_engine,app_paths,tmp_path)
    result=call(api,'mix.submit',{'quantity':2,'total_range_ms':[2100,2900],'segment_range_ms':[400,700]},'range-submit')
    assert result['ok'],result
    detail=call(api,'task.detail',{'id':result['data']['id']},'range-detail')['data']
    assert detail['total_range_ms']==[2100,2900]
    assert detail['segment_range_ms']==[400,700]
    assert len(detail['item_total_ms'])==2
    assert all(2100<=x<=2900 for x in detail['item_total_ms'])
    invalid=call(api,'mix.submit',{'quantity':1,'total_range_ms':[2900,2100]},'range-invalid')
    assert not invalid['ok']
    assert len(api._mix.tasks())==1
    api.close(.1)
