from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.app_services.media_access import MediaAccess
from yijianjifa.features.finished.service import FinishedVideoService
from test_library_management import call


def test_finished_list_and_metadata_edit_preserve_id_and_media(db_engine,app_paths,tmp_path):
    file=tmp_path/'成品.mp4';file.write_bytes(b'video fixture');core=FinishedVideoService(db_engine)
    item=core.register_generated(file,duration_ms=1500,title='before')
    api=DesktopApi(db_engine,app_paths)
    result=call(api,'finished.list',dict(query='',origin='all',offset=0,limit=20))
    assert result['ok'],result
    assert result['data']['items'][0]['id']==item.id and str(file) not in str(result)
    params=dict(id=item.id,display_name='新名称',title='<script>标题</script>',body='内容',tags='标签')
    result=call(api,'finished.save',params,'save')
    assert result['ok'],result
    assert call(api,'finished.save',params,'save')==result
    saved=core.get(item.id);assert saved.title=='<script>标题</script>' and saved.display_name=='新名称'
    assert saved.effective_path==file and file.read_bytes()==b'video fixture'
    assert call(api,'finished.list',dict(query='内容',origin='generated',offset=0,limit=20))['data']['total']==1
    assert call(api,'finished.list',dict(query='',origin='external',offset=0,limit=20))['data']['total']==0
    api.close(.1)


def test_finished_preview_resolves_id_to_existing_resource_grant(db_engine,app_paths,tmp_path):
    file=tmp_path/'成品.mp4';file.write_bytes(b'finished video')
    item=FinishedVideoService(db_engine).import_external(file,duration_ms=1500)
    media=MediaAccess(db_engine,app_paths)
    result=media.preview('finished',item.id)
    assert str(file) not in str(result)
    token=result['url'].split('/')[-1]
    assert media.registry.read(token,0,len(b'finished video'))==b'finished video'
    media.close()
