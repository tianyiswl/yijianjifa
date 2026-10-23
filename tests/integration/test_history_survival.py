from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import PublishHistoryRepository


def test_delete_and_reimport_same_video_keeps_publish_history(db_engine, tmp_path):
    path = tmp_path / "same-video.mp4"
    path.write_bytes(b"stable-video-identity")
    finished = FinishedVideoService(db_engine)
    history = PublishHistoryRepository(db_engine)

    first = finished.import_external(path, duration_ms=1000)
    history.record(first.content_hash, "A1", "published")
    finished.delete(first.id)

    second = finished.import_external(path, duration_ms=1000)

    assert second.content_hash == first.content_hash
    assert history.has_quick_block(second.content_hash) is True
    assert "published" in history.states_for_video(second.content_hash)
