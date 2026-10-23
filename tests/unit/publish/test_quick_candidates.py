from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.publish.history import PublishHistoryRepository
from yijianjifa.features.publish.normal_service import AdvancedVideoSelection, NormalPublishService


def _video(service, tmp_path, name):
    path = tmp_path / f"{name}.mp4"; path.write_bytes(name.encode())
    return service.import_external(path, duration_ms=1000)


def test_quick_candidates_exclude_any_app_published_or_unresolved_video(db_engine, tmp_path):
    finished = FinishedVideoService(db_engine)
    history = PublishHistoryRepository(db_engine)
    videos = [_video(finished, tmp_path, f"v{i}") for i in range(6)]
    states = [None, "published", "queued", "platform_scheduled", "result_unknown", "pre_submit_failed"]
    for video, state in zip(videos, states):
        if state:
            history.record(video.content_hash, "A1", state)
    service = NormalPublishService(db_engine)
    assert service.quick_candidate_ids() == {videos[0].id}


def test_published_on_other_account_still_excludes_quick_candidate(db_engine, tmp_path):
    finished = FinishedVideoService(db_engine)
    history = PublishHistoryRepository(db_engine)
    video = _video(finished, tmp_path, "same")
    history.record(video.content_hash, "OTHER", "published")
    assert NormalPublishService(db_engine).quick_candidate_ids() == set()


def test_turning_off_show_published_removes_published_hidden_selection():
    vm = AdvancedVideoSelection(new_ids={"new-1"}, published_ids={"published-1"})
    vm.show_published = True
    vm.select_ids({"new-1", "published-1"})
    assert vm.selected_ids == {"new-1", "published-1"}
    vm.show_published = False
    assert vm.selected_ids == {"new-1"}


def test_turning_on_show_published_does_not_auto_select_published():
    vm = AdvancedVideoSelection(new_ids={"new-1"}, published_ids={"published-1"})
    vm.show_published = True
    assert vm.selected_ids == set()
