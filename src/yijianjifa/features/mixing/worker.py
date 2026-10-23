from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from yijianjifa.features.finished.service import FinishedVideoRecord, FinishedVideoService
from yijianjifa.features.library.usage_progress import UsageProgressService
from yijianjifa.features.mixing.ffmpeg_renderer import RenderRequest, RenderResult


class Renderer(Protocol):
    def render(self, request: RenderRequest) -> RenderResult: ...


@dataclass(frozen=True, slots=True)
class ProductionItemSpec:
    render_request: RenderRequest
    title: str = ""
    body: str = ""
    tags: str = ""
    copy_id: str | None = None


class MixProductionExecutor:
    """Bridge a frozen queue item to renderer + finished library.

    A file is registered in the finished library only after the renderer returns
    successfully. Any partial output from a failed render is removed.
    """

    def __init__(
        self,
        renderer: Renderer,
        finished: FinishedVideoService,
        progress: UsageProgressService,
        item_factory: Callable[[str, int, Mapping[str, Any]], ProductionItemSpec],
    ):
        self.renderer = renderer
        self.finished = finished
        self.progress = progress
        self.item_factory = item_factory

    def render_item(
        self,
        task_id: str,
        item_index: int,
        snapshot: Mapping[str, Any],
    ) -> FinishedVideoRecord:
        item = self.item_factory(task_id, item_index, snapshot)
        output = Path(item.render_request.output_path)
        try:
            rendered = self.renderer.render(item.render_request)
            record = self.finished.register_generated(
                rendered.output_path,
                duration_ms=rendered.duration_ms,
                title=item.title,
                body=item.body,
                tags=item.tags,
                copy_id=item.copy_id,
            )
        except Exception:
            if output.exists():
                output.unlink()
            raise

        if item.copy_id:
            self.progress.mark_used("mixing", item.copy_id)
        return record


class MixWorker:
    def __init__(self, queue):
        self.queue = queue

    def run(self):
        return self.queue.run_once_until_idle()
