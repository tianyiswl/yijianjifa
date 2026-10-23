import pytest

from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import DuplicatePublishBlocked, PublishHistoryRepository
from yijianjifa.features.publish.normal_service import NormalPublishService


def _finished(db_engine, tmp_path, name="v"):
    path = tmp_path / f"{name}.mp4"; path.write_bytes(name.encode())
    return FinishedVideoService(db_engine).import_external(path, duration_ms=1000)


def test_same_account_success_requires_explicit_repeat_confirmation(db_engine, tmp_path):
    video = _finished(db_engine, tmp_path)
    history = PublishHistoryRepository(db_engine)
    history.record(video.content_hash, "A1", "published")
    with pytest.raises(DuplicatePublishBlocked):
        history.assert_can_create(video.content_hash, "A1", allow_repeat_success=False)
    history.assert_can_create(video.content_hash, "A1", allow_repeat_success=True)


def test_unresolved_or_scheduled_blocks_duplicate_even_with_repeat_flag(db_engine, tmp_path):
    video = _finished(db_engine, tmp_path)
    history = PublishHistoryRepository(db_engine)
    history.record(video.content_hash, "A1", "result_unknown")
    with pytest.raises(DuplicatePublishBlocked):
        history.assert_can_create(video.content_hash, "A1", allow_repeat_success=True)


def test_confirmed_publish_snapshot_is_immutable_after_source_mapping_changes(db_engine):
    service = NormalPublishService(db_engine)
    draft = {"platform": "douyin", "account_ids": ["A1"], "video_ids": ["V1"], "schedule": {"mode": "daily"}}
    task = service.confirm_publish_draft(draft)
    draft["account_ids"][0] = "A2"
    draft["schedule"]["mode"] = "interval"
    assert task.snapshot["account_ids"] == ["A1"]
    assert task.snapshot["schedule"]["mode"] == "daily"
