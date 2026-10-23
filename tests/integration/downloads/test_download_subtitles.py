"""Isolated subtitle editing; no media/network/user database access."""
import json

import pytest
from sqlalchemy import select

from yijianjifa.features.downloads.contracts import Candidate, DownloadError, SubtitleSegment
from yijianjifa.features.downloads.repository import DownloadRepository
from yijianjifa.storage.models import DownloadSubtitleOriginalRow, DownloadSubtitleRevisionRow


def setup_store(engine, count=100, text='这是测试字幕' * 15):
    from yijianjifa.features.downloads.subtitles import SubtitleStore
    repo = DownloadRepository(engine)
    with engine.begin() as connection:
        candidate = repo.save_candidate(connection, Candidate('candidate', 'direct', '', 'direct:' + 'a' * 64, None,
                                                              duration_ms=500000))
        segments = tuple(SubtitleSegment('seg' + str(i), i * 1000, i * 1000 + 800, text + str(i)) for i in range(count))
        resource = repo.save_subtitle_original(connection, candidate, None, 'asr', segments)
    return SubtitleStore(repo), repo, resource, candidate


def test_paged_edits_preserve_original(db_engine):
    store, repo, resource, _ = setup_store(db_engine)
    page = store.read(resource, 'asr', None)
    assert len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()) <= 12288
    assert 0 < len(page['segments']) < 100
    with db_engine.begin() as connection:
        saved = store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': '已编辑'},))
    assert saved['revision'] == 1 and saved['saved_count'] == 1
    assert store.read(resource, 'asr', None)['segments'][0]['text'] == '已编辑'
    with db_engine.connect() as connection:
        original = connection.execute(select(DownloadSubtitleOriginalRow.segments_json).where(DownloadSubtitleOriginalRow.id == resource)).scalar_one()
    assert json.loads(original)[0]['text'] != '已编辑'


def test_cursor_bound_to_resource_origin_and_revision(db_engine):
    store, _, resource, candidate = setup_store(db_engine)
    page = store.read(resource, 'asr', None)
    with db_engine.begin() as connection:
        store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': 'change'},))
    with pytest.raises(DownloadError) as conflict:
        store.read(resource, 'asr', page['next_cursor'])
    assert conflict.value.code == 'subtitle_conflict'
    with pytest.raises(DownloadError):
        store.read(resource, 'ocr', None)
    with pytest.raises(DownloadError):
        store.read(resource, 'asr', 'made-up-cursor')


def test_cross_page_overlap_rejected_without_revision(db_engine):
    store, _, resource, _ = setup_store(db_engine)
    page = store.read(resource, 'asr', None)
    last = page['segments'][-1]
    with db_engine.begin() as connection:
        with pytest.raises(DownloadError) as invalid:
            store.save(connection, resource, 'asr', 0, ({'id': last['id'], 'end_ms': last['end_ms'] + 900},))
        assert invalid.value.code == 'subtitle_invalid'
    assert store.read(resource, 'asr', None)['revision'] == 0


def test_full_export_and_reopen_keeps_edit(db_engine, tmp_path):
    store, repo, resource, _ = setup_store(db_engine)
    with db_engine.begin() as connection:
        store.save(connection, resource, 'asr', 0, ({'id': 'seg99', 'text': '最后一段'},))
    from yijianjifa.features.downloads.subtitles import SubtitleStore
    reopened = SubtitleStore(repo)
    for format in ('txt', 'srt'):
        path = tmp_path / ('字幕.' + format)
        reopened.export(resource, 'asr', format, path)
        result = path.read_text('utf-8')
        assert '最后一段' in result
        assert '这是测试字幕' in result
        if format == 'srt':
            assert '100\n00:01:39,000 --> 00:01:39,800\n最后一段' in result


def test_conflict_and_invalid_patches_keep_previous_changes(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=2)
    with db_engine.begin() as connection:
        store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': 'first'},))
    with db_engine.begin() as connection:
        with pytest.raises(DownloadError) as conflict:
            store.save(connection, resource, 'asr', 0, ({'id': 'seg1', 'text': 'second'},))
        assert conflict.value.code == 'subtitle_conflict'
    for patch in ({'id': 'missing', 'text': 'x'}, {'id': 'seg0', 'start_ms': -1},
                  {'id': 'seg0', 'text': 'x', 'unknown': 1}, {'id': 'seg0', 'end_ms': 600000},
                  {'id': 'seg0', 'text': '中' * 6000}):
        with db_engine.begin() as connection:
            with pytest.raises(DownloadError):
                store.save(connection, resource, 'asr', 1, (patch,))
    assert store.read(resource, 'asr', None)['segments'][0]['text'] == 'first'
    assert store.read(resource, 'asr', None)['revision'] == 1


def test_too_large_single_segment_never_truncated(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=1, text='中' * 5000)
    with pytest.raises(DownloadError) as invalid:
        store.read(resource, 'asr', None)
    assert invalid.value.code == 'input_limit'


def test_rerecognition_does_not_redirect_old_edits(db_engine):
    store, repo, old, candidate = setup_store(db_engine, count=1, text='old')
    with db_engine.begin() as connection:
        store.save(connection, old, 'asr', 0, ({'id': 'seg0', 'text': 'edited'},))
        new = repo.save_subtitle_original(connection, candidate, None, 'asr', (SubtitleSegment('new', 0, 1000, 'new'),))
    assert store.read(old, 'asr', None)['segments'][0]['text'] == 'edited'
    assert store.read(new, 'asr', None)['segments'][0]['text'] == 'new'
    assert store.read(new, 'asr', None)['revision'] == 0


def test_save_rolls_back_with_receipt_transaction(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=2)
    with pytest.raises(RuntimeError):
        with db_engine.begin() as connection:
            store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': 'not committed'},))
            raise RuntimeError('simulated receipt failure')
    assert store.read(resource, 'asr', None)['revision'] == 0
    with db_engine.connect() as connection:
        assert not connection.execute(select(DownloadSubtitleRevisionRow.id)).first()


def test_export_never_overwrites_or_follows_symlink(db_engine, tmp_path):
    store, _, resource, _ = setup_store(db_engine, count=1)
    existing = tmp_path / 'existing.txt'
    existing.write_text('keep', encoding='utf-8')
    with pytest.raises(DownloadError):
        store.export(resource, 'asr', 'txt', existing)
    assert existing.read_text('utf-8') == 'keep'
    link = tmp_path / 'link.txt'
    link.symlink_to(existing)
    with pytest.raises(DownloadError):
        store.export(resource, 'asr', 'txt', link)
    assert existing.read_text('utf-8') == 'keep'


def test_serial_patch_batches_use_latest_revision(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=3)
    revision = 0
    for index in range(3):
        with db_engine.begin() as connection:
            result = store.save(connection, resource, 'asr', revision, ({'id': 'seg' + str(index), 'text': 'batch' + str(index)},))
        revision = result['revision']
    assert revision == 3
    assert [row['text'] for row in store.read(resource, 'asr', None)['segments']] == ['batch0', 'batch1', 'batch2']


def test_all_pages_preserve_every_stable_segment_and_budget(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=300)
    cursor, ids = None, []
    while True:
        page = store.read(resource, 'asr', cursor, budget_bytes=2048)
        assert len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()) <= 2048
        ids.extend(row['id'] for row in page['segments'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert ids == ['seg' + str(index) for index in range(300)]


def test_export_cancel_removes_only_own_temporary_file(db_engine, tmp_path):
    store, _, resource, _ = setup_store(db_engine, count=3)
    unrelated = tmp_path / 'keep.tmp'
    unrelated.write_text('keep')
    before = {path.name for path in tmp_path.iterdir()}
    class Cancel:
        count = 0
        def cancelled(self):
            self.count += 1
            return self.count > 1
    with pytest.raises(DownloadError) as cancelled:
        store.export(resource, 'asr', 'txt', tmp_path / 'output.txt', cancel=Cancel())
    assert cancelled.value.code == 'cancelled'
    assert {path.name for path in tmp_path.iterdir()} == before


def test_duplicate_patch_and_changed_export_version_reject(db_engine, tmp_path):
    store, _, resource, _ = setup_store(db_engine, count=2)
    with db_engine.begin() as connection:
        with pytest.raises(DownloadError):
            store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': 'a'}, {'id': 'seg0', 'text': 'b'}))
        store.save(connection, resource, 'asr', 0, ({'id': 'seg0', 'text': 'a'},))
    with pytest.raises(DownloadError) as conflict:
        store.export(resource, 'asr', 'txt', tmp_path / 'changed.txt', expected_revision=0)
    assert conflict.value.code == 'subtitle_conflict'
    assert not (tmp_path / 'changed.txt').exists()


def test_patch_batch_fits_full_native_envelope(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=3)
    params = dict(resource_id=resource, origin='asr', expected_revision=0,
                  patches=({'id': 'seg0', 'text': '中' * 3000},))
    envelope = dict(v=1, request_id='b' * 36, operation='download.subtitle.save', params=params)
    assert len(json.dumps(envelope, ensure_ascii=False).encode()) < 16384
    with db_engine.begin() as connection:
        assert store.save(connection, **params)['saved_count'] == 1


def test_patch_cannot_save_a_segment_that_read_budget_cannot_return(db_engine):
    store, _, resource, _ = setup_store(db_engine, count=2)
    params = dict(resource_id=resource, origin='asr', expected_revision=0,
                  patches=({'id': 'seg0', 'text': '中' * 4050},))
    assert len(json.dumps(params, ensure_ascii=False, separators=(',', ':')).encode()) <= 12288
    with db_engine.begin() as connection:
        with pytest.raises(DownloadError) as invalid:
            store.save(connection, **params)
        assert invalid.value.code == 'input_limit'
    assert store.read(resource, 'asr', None)['revision'] == 0


def test_pinned_stream_export_writes_full_document_without_opening_path(db_engine):
    import io
    store, _, resource, _ = setup_store(db_engine, count=100)
    stream = io.StringIO()
    store.write_to(resource, 'asr', 'srt', stream, expected_revision=0)
    assert '100\n00:01:39,000' in stream.getvalue()


def test_cleanup_error_does_not_expose_paths_or_replace_cancel(db_engine, tmp_path, monkeypatch):
    from pathlib import Path
    store, _, resource, _ = setup_store(db_engine, count=1)
    native_unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path.name.startswith('.p011-subtitle-'):
            raise PermissionError('private native target path')
        return native_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', locked)
    class Cancel:
        def cancelled(self):
            return True
    with pytest.raises(DownloadError) as result:
        store.export(resource, 'asr', 'txt', tmp_path / 'target.txt', cancel=Cancel())
    assert result.value.code == 'cancelled'
    assert 'private native' not in str(result.value)
