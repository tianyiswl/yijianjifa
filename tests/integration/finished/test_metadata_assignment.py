from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.usage_progress import UsageProgressService


def test_copy_assignment_preserves_existing_metadata_by_default(db_engine, copy_repo, tmp_path):
    video = tmp_path / "external.mp4"; video.write_bytes(b"external")
    copy_repo.create(id="W1", title="新标题", voice="口播", body="新文案", tags="#新", full="")
    service = FinishedVideoService(db_engine)
    row = service.import_external(video, duration_ms=1000, title="原标题", body="原文案", tags="#原")
    progress = UsageProgressService(db_engine)

    service.assign_copy(row.id, "W1", replace_existing=False, progress=progress)

    current = service.get(row.id)
    assert (current.title, current.body, current.tags) == ("原标题", "原文案", "#原")
    assert progress.is_used("finished_metadata", "W1")


def test_copy_assignment_can_explicitly_replace_existing_metadata(db_engine, copy_repo, tmp_path):
    video = tmp_path / "external.mp4"; video.write_bytes(b"external")
    copy_repo.create(id="W1", title="新标题", voice="口播", body="新文案", tags="#新", full="")
    service = FinishedVideoService(db_engine)
    row = service.import_external(video, duration_ms=1000, title="原标题", body="原文案", tags="#原")
    progress = UsageProgressService(db_engine)

    service.assign_copy(row.id, "W1", replace_existing=True, progress=progress)

    current = service.get(row.id)
    assert (current.title, current.body, current.tags) == ("新标题", "新文案", "#新")
