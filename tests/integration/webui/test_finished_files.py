import json
from pathlib import Path

from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.app_services.finished_files import FinishedFiles


def probe(path):
    if path.name.startswith('坏'): raise ValueError('private path')
    return {'streams': [{'codec_type': 'audio' if path.name.startswith('音频') else 'video'}],
            'format': {'duration': '1.25'}}


def test_native_import_cancel_partial_failure_and_duplicate_preserve_source(db_engine, tmp_path):
    files = FinishedFiles(db_engine, probe=probe)
    assert files.import_files([])['cancelled']
    good = tmp_path/'中文 空格.mp4';good.write_bytes(b'video')
    bad = tmp_path/'坏文件.mp4';bad.write_bytes(b'broken')
    audio = tmp_path/'音频.mp4';audio.write_bytes(b'audio')
    alias = tmp_path/'link.mp4';alias.symlink_to(good)
    r = files.import_files([good, bad, audio, alias])
    assert r['imported'] == 1 and len(r['errors']) == 3
    assert str(tmp_path) not in json.dumps(r)
    assert files.import_files([good])['reused'] == 1
    rows = FinishedVideoService(db_engine).list_by_ids(r['ids'])
    assert rows[0].origin_type == 'external' and rows[0].duration_ms == 1250
    assert rows[0].title == rows[0].body == rows[0].tags == ''
    assert good.read_bytes() == b'video'


def test_export_unique_directory_cancel_and_per_item_failures(db_engine, tmp_path):
    files = FinishedFiles(db_engine, probe=probe)
    a = tmp_path/'视频.mp4';a.write_bytes(b'video')
    r = files.import_files([a]);pid = r['ids'][0]
    core = FinishedVideoService(db_engine)
    core.update_metadata(pid, title='标题', body='正文', tags='#标签')
    assert files.export([pid], [], include_metadata=True)['cancelled']
    target = tmp_path/'导出';target.mkdir()
    first = files.export([pid, 'missing'], [target], include_metadata=True)
    second = files.export([pid], [target], include_metadata=False)
    assert first['exported'] == second['exported'] == 1
    assert len(first['errors']) == 1 and first['folder_name'] != second['folder_name']
    metadata = list((target/first['folder_name']).glob('*.json'))
    assert len(metadata) == 1 and json.loads(metadata[0].read_text(encoding="utf-8"))['body'] == '正文'
    assert list((target/second['folder_name']).glob('*.json')) == []
    assert a.read_bytes() == b'video'
