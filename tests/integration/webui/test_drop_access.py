import pytest
from yijianjifa.app_services import media_access
from yijianjifa.webui.media_registry import DropRegistry
from yijianjifa.app_services.excel_access import ExcelAccess
from yijianjifa.features.library.copy_service import CopyRepository


def access(engine,paths):
    cls=getattr(media_access,'DropAccess',None)
    assert cls is not None,'drop import orchestration is missing'
    grants=DropRegistry()
    return cls(grants,media_access.MediaAccess(engine,paths),ExcelAccess(engine)),grants


def test_dropped_folder_and_file_reuse_same_dedupe_and_no_path_response(db_engine,app_paths,tmp_path):
    a,g=access(db_engine,app_paths);folder=tmp_path/'folder';folder.mkdir();file=folder/'视频.mp4';file.write_bytes(b'video')
    token=g.grant([folder,file])
    result=a.media(token,kind='video',keep_copy=False,category_ids=[])
    assert result['imported']==1 and result['reused']==1
    assert str(tmp_path) not in str(result)
    with pytest.raises(ValueError):a.media(token,kind='video',keep_copy=False,category_ids=[])


def test_dropped_excel_only_previews_without_writes(db_engine,app_paths,tmp_path):
    from yijianjifa.features.library.excel_import import export_template
    from openpyxl import load_workbook
    a,g=access(db_engine,app_paths);file=tmp_path/'文案.xlsx';export_template(file,include_example=False)
    book=load_workbook(file);book.active.append(['DROP-1','拖入标题','','','','']);book.save(file);book.close()
    result=a.excel(g.grant([file]))
    assert result['new_count']==1
    assert CopyRepository(db_engine).list_all()==[]
    with pytest.raises(ValueError):a.excel(g.grant([file,file]))
