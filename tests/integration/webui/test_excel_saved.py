import pytest
from openpyxl import load_workbook
from yijianjifa.features.library.excel_import import export_template, read_excel_rows
from yijianjifa.app_services.excel_access import ExcelAccess


def test_template_first_sheet_has_importable_example(tmp_path):
    path=export_template(tmp_path/'template.xlsx')
    rows=read_excel_rows(path)
    assert len(rows)==1 and rows[0]['标题'] and rows[0]['口播稿'] and rows[0]['发布文案'] and rows[0]['标签'] and rows[0]['全程字幕']
    assert rows[0]['文案编号（ID）']==''
    assert read_excel_rows(export_template(tmp_path/'blank.xlsx',include_example=False))==[]


def test_saved_file_actions_only_use_native_registered_paths(db_engine,tmp_path,monkeypatch):
    api=ExcelAccess(db_engine);path=export_template(tmp_path/'example.xlsx')
    result=api.register_saved(path);calls=[]
    monkeypatch.setattr('platform.system',lambda:'Darwin')
    monkeypatch.setattr('subprocess.Popen',lambda args:calls.append(args))
    assert api.open_saved(result['token'],'file')['opened']
    assert api.open_saved(result['token'],'folder')['opened']
    assert calls==[['open',str(path)],['open','-R',str(path)]]
    with pytest.raises(ValueError):api.open_saved(str(path),'file')
    with pytest.raises(ValueError):api.open_saved(result['token'],'execute')
    path.unlink()
    with pytest.raises(ValueError):api.open_saved(result['token'],'file')
