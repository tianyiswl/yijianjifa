from pathlib import Path

from yijianjifa.features.finished.service import FinishedVideoService
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderRequest, RenderResult, RenderSegment
from yijianjifa.features.mixing.worker import MixProductionExecutor, ProductionItemSpec


class FakeRenderer:
    def __init__(self, *, fail=False):
        self.fail = fail

    def render(self, request):
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"partial" if self.fail else b"finished-video")
        if self.fail:
            raise RuntimeError("synthetic render failure")
        return RenderResult(request.output_path, duration_ms=1000, width=1080, height=1920)


def _factory(tmp_path, copy_id="W1"):
    def build(task_id, item_index, snapshot):
        output = tmp_path / f"{task_id}-{item_index}.mp4"
        request = RenderRequest(
            segments=(RenderSegment(tmp_path / "source.mp4", 0, 1000),),
            output_path=output,
            keep_original_audio=False,
        )
        return ProductionItemSpec(
            render_request=request,
            title="标题",
            body="发布文案",
            tags="#标签",
            copy_id=copy_id,
        )
    return build


def test_failed_render_is_cleaned_and_never_enters_finished_library(db_engine, tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"source")
    finished = FinishedVideoService(db_engine)
    progress = UsageProgressService(db_engine)
    executor = MixProductionExecutor(FakeRenderer(fail=True), finished, progress, _factory(tmp_path))

    try:
        executor.render_item("M1", 0, {"quantity": 1})
    except RuntimeError as exc:
        assert "synthetic render failure" in str(exc)
    else:
        raise AssertionError("expected render failure")

    assert list(tmp_path.glob("M1-0.mp4")) == []
    assert finished.list_by_ids([]) == []
    assert not progress.is_used("mixing", "W1")


def test_successful_render_enters_finished_library_then_advances_copy_progress(db_engine, tmp_path, copy_repo):
    (tmp_path / "source.mp4").write_bytes(b"source")
    copy_repo.create(id="W1", title="标题", voice="口播", body="发布文案", tags="#标签", full="")
    finished = FinishedVideoService(db_engine)
    progress = UsageProgressService(db_engine)
    executor = MixProductionExecutor(FakeRenderer(), finished, progress, _factory(tmp_path))

    record = executor.render_item("M1", 0, {"quantity": 1})

    stored = finished.get(record.id)
    assert stored is not None
    assert stored.title == "标题"
    assert stored.copy_id == "W1"
    assert progress.is_used("mixing", "W1")
