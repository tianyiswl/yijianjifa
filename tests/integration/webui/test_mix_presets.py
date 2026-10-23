import json
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.presets.service import PresetService
from yijianjifa.features.library.copy_service import CategoryRepository


def call(api, op, params, rid='read'):
    return api.dispatch(json.dumps(dict(v=1,request_id=rid,operation=op,params=params)),trusted_origin=True)


def test_complete_mix_preset_roundtrip_and_durable_retry(db_engine, app_paths):
    api=DesktopApi(db_engine,app_paths)
    params={'quantity':3,'total_ms':4000,'segment_ms':1200,'total_range_ms':[3000,5000],
            'segment_range_ms':[500,1500],'copy_mode':'random','sentence_cut':True,
            'voice':'Eddy (中文（中国大陆）)','speed':1.2,'music_gain':.18,
            'transition':{'mode':'fixed','name':'slideleft','duration_ms':300},
            'spoken_style':{'font':'serif','size':32,'chars':9,'position':'middle','color':'#aabbcc'},
            'full_style':{'font':'sans','size':48,'chars':12,'position':'top','color':'#123456'}}
    request={'id':None,'name':'完整制作方案','settings':params}
    first=call(api,'preset.mix.save',request,'save')
    assert first['ok'],first
    assert first==call(api,'preset.mix.save',request,'save')
    assert len(PresetService(db_engine).list(kind='mix'))==1
    got=call(api,'preset.mix.get',{'id':first['data']['id']})['data']
    for key,value in params.items():assert got['settings'][key]==value
    assert got['state']=='ready'
    assert api.close(.1)


def test_missing_reference_never_becomes_all_and_update_keeps_identity(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths);cats=CategoryRepository(db_engine)
    category=cats.create('video','素材分类')
    saved=call(api,'preset.mix.save',{'id':None,'name':'分类方案','settings':{'quantity':1,'material_categories':[category.id]}},'save')
    assert saved['ok'],saved
    pid=saved['data']['id'];cats.delete(category.id)
    got=call(api,'preset.mix.get',{'id':pid})['data']
    assert got['state']=='needs_repair' and got['settings']['material_categories']==[category.id]
    assert category.id in got['missing_category_ids']
    assert call(api,'preset.mix.save',{'id':pid,'name':'已修复','settings':{'quantity':2}},'update')['ok']
    assert len(PresetService(db_engine).list(kind='mix'))==1
    assert call(api,'preset.mix.get',{'id':pid})['data']['settings']['quantity']==2
    assert api.close(.1)


def test_preset_rejects_paths_dates_and_batch_override(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    for i,extra in enumerate([{'output_directory':'/tmp'}, {'batch_full_text':'本批文字'}, {'publish_at':'2026-01-01'}, {'auto_publish':True}]):
        result=call(api,'preset.mix.save',{'id':None,'name':'invalid','settings':{'quantity':1,**extra}},str(i))
        assert not result['ok'],result
    assert PresetService(db_engine).list()==[]
    assert api.close(.1)


def test_legacy_and_missing_music_require_repair_without_discarding_choice(db_engine,app_paths):
    service=PresetService(db_engine)
    old=service.create_mix(name='旧版指定音乐',background_music_mode='fixed')
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'preset.mix.get',{'id':old.id})['data']
    assert result['state']=='needs_repair' and result['missing_music']
    assert result['settings']['background_music']['mode']=='fixed'
    saved=call(api,'preset.mix.save',{'id':None,'name':'丢失音乐','settings':{'quantity':1,'background_music':{'mode':'fixed','music_id':'missing'}}},'save-music')
    assert saved['ok'] and saved['data']['state']=='needs_repair'
    assert saved['data']['settings']['background_music']['music_id']=='missing'
    api.close(.1)


def test_updating_preset_does_not_mutate_existing_task(db_engine,app_paths,tmp_path):
    from yijianjifa.features.library.copy_service import CopyRepository
    from yijianjifa.features.library.media_storage import MediaStorageService
    from yijianjifa.features.mixing.desktop_service import DesktopMixService
    media=tmp_path/'video.mp4';media.write_bytes(b'fixture')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(media,keep_copy=False,category_ids=[])
    CopyRepository(db_engine).create(id='C',title='旧任务',voice='',body='',tags='',full='')
    api=DesktopApi(db_engine,app_paths)
    saved=call(api,'preset.mix.save',{'id':None,'name':'制作','settings':{'quantity':1}},'save-task')['data']
    task=call(api,'mix.submit',saved['settings'],'submit')['data']
    before=DesktopMixService(db_engine,app_paths).queue.get(task['id']).snapshot
    assert call(api,'preset.mix.save',{'id':saved['id'],'name':'已修改','settings':{'quantity':9,'resolution':'720p'}},'change')['ok']
    assert DesktopMixService(db_engine,app_paths).queue.get(task['id']).snapshot==before
    api.close(.1)


def test_no_copy_preset_roundtrip(db_engine, app_paths):
    api=DesktopApi(db_engine,app_paths)
    saved=call(api,'preset.mix.save',{'id':None,'name':'纯画面','settings':{'quantity':2,'use_copy':False,'copy_categories':[]}},'no-copy')
    assert saved['ok'],saved
    settings=call(api,'preset.mix.get',{'id':saved['data']['id']})['data']['settings']
    assert settings['use_copy'] is False
    assert settings['copy_categories']==[]
    assert api.close(.1)


def test_last_selected_mix_preset_survives_reopening(db_engine,app_paths):
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'settings.mix_preset.save',{'id':'remembered'},'remember')
    assert result['ok'],result
    assert api.close(.1)
    reopened=DesktopApi(db_engine,app_paths)
    try:
        assert call(reopened,'settings.mix_preset.read',{})['data']['id']=='remembered'
        assert call(reopened,'settings.mix_preset.save',{'id':''},'default')['ok']
        assert call(reopened,'settings.mix_preset.read',{})['data']['id']==''
    finally:reopened.close(.1)
