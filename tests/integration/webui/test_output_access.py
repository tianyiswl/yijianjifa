from pathlib import Path
import pytest
from yijianjifa.app_services.output_access import OutputAccess
from yijianjifa.core.settings import GlobalSettings
from yijianjifa.features.mixing.queue import MixQueue


def test_choose_persists_and_only_affects_future_batches(db_engine, app_paths, tmp_path):
    service = OutputAccess(db_engine, app_paths)
    settings = GlobalSettings(db_engine, default_output_directory=app_paths.data_dir / '成品')
    queue = MixQueue(db_engine, object(), global_settings=settings)
    first = queue.confirm_batch({'quantity': 1})
    chosen = tmp_path / '中文 成品'; chosen.mkdir()
    assert service.choose([str(chosen)]) == {'directory': str(chosen.resolve()), 'cancelled': False}
    assert list(chosen.iterdir()) == []
    assert OutputAccess(db_engine, app_paths).read()['directory'] == str(chosen.resolve())
    second = queue.confirm_batch({'quantity': 1})
    assert first.snapshot['output_directory'] != second.snapshot['output_directory']
    assert second.snapshot['output_directory'] == str(chosen.resolve())


def test_cancel_and_invalid_selection_preserve_settings(db_engine, app_paths, tmp_path):
    service = OutputAccess(db_engine, app_paths)
    before = service.read()
    assert service.choose(None) == {**before, 'cancelled': True}
    file = tmp_path / 'file'; file.write_text('keep')
    for selected in ([str(file)], [str(tmp_path / 'missing')], [str(tmp_path), str(tmp_path)]):
        with pytest.raises(ValueError): service.choose(selected)
        assert service.read() == before
    assert file.read_text() == 'keep'


def test_write_failure_preserves_settings(db_engine, app_paths, tmp_path, monkeypatch):
    service = OutputAccess(db_engine, app_paths); before = service.read()
    def denied(*args, **kwargs): raise PermissionError('private path')
    monkeypatch.setattr('yijianjifa.app_services.output_access.tempfile.TemporaryFile', denied)
    with pytest.raises(ValueError, match='无法写入'): service.choose([str(tmp_path)])
    assert service.read() == before
