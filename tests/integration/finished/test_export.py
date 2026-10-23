from __future__ import annotations

import json
from pathlib import Path

from yijianjifa.features.finished.service import FinishedVideoService


def test_export_keeps_video_and_metadata_correspondence(db_engine, tmp_path):
    source_a = tmp_path / "a.mp4"; source_a.write_bytes(b"video-a")
    source_b = tmp_path / "b.mp4"; source_b.write_bytes(b"video-b")
    service = FinishedVideoService(db_engine)
    a = service.register_generated(source_a, title="标题A", body="文案A", tags="#A", duration_ms=1000)
    b = service.register_generated(source_b, title="标题B", body="文案B", tags="#B", duration_ms=1000)

    manifest = service.export([a.id, b.id], destination=tmp_path / "export", include_metadata=True)

    assert len(manifest) == 2
    for entry in manifest:
        assert entry.video_basename.split(".")[0] == entry.metadata_basename.split(".")[0]
        payload = json.loads((tmp_path / "export" / entry.metadata_basename).read_text(encoding="utf-8"))
        assert payload["title"] == entry.title
    assert manifest[0].title != manifest[1].title


def test_external_delete_never_unlinks_original(db_engine, tmp_path):
    original = tmp_path / "external.mp4"; original.write_bytes(b"external")
    service = FinishedVideoService(db_engine)
    row = service.import_external(original, duration_ms=1000)

    service.delete(row.id)

    assert original.exists()
    assert service.get(row.id) is None


def test_generated_delete_unlinks_generated_file(db_engine, tmp_path):
    generated = tmp_path / "generated.mp4"; generated.write_bytes(b"generated")
    service = FinishedVideoService(db_engine)
    row = service.register_generated(generated, duration_ms=1000)

    service.delete(row.id)

    assert not generated.exists()
    assert service.get(row.id) is None


def test_active_task_reference_blocks_delete(db_engine, tmp_path):
    generated = tmp_path / "busy.mp4"; generated.write_bytes(b"busy")
    service = FinishedVideoService(db_engine)
    row = service.register_generated(generated, duration_ms=1000)
    service.set_task_lock(row.id, True)

    try:
        service.delete(row.id)
    except RuntimeError as exc:
        assert "active task" in str(exc)
    else:
        raise AssertionError("expected active-task delete block")
    assert generated.exists()


def test_export_does_not_overwrite_existing_video_or_metadata(db_engine, tmp_path):
    source = tmp_path/'source.mp4';source.write_bytes(b'video')
    service = FinishedVideoService(db_engine)
    row = service.import_external(source, duration_ms=1000, title='original')
    folder = tmp_path/'export'
    first = service.export([row.id], destination=folder, include_metadata=True)[0]
    (folder/first.video_basename).write_bytes(b'keep video')
    (folder/first.metadata_basename).write_text('keep metadata')
    second = service.export([row.id], destination=folder, include_metadata=True)[0]
    assert second.video_basename != first.video_basename
    assert (folder/first.video_basename).read_bytes() == b'keep video'
    assert (folder/first.metadata_basename).read_text() == 'keep metadata'
    assert (folder/second.video_basename).read_bytes() == b'video'
