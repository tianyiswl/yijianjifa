from yijianjifa.features.mixing.queue import MixQueue


class FakeExecutor:
    def __init__(self):
        self.started = []
        self.fail_indices = set()
        self.on_start = None

    def render_item(self, task_id, item_index, snapshot):
        self.started.append(item_index)
        if self.on_start:
            self.on_start(item_index)
        if item_index in self.fail_indices:
            raise RuntimeError(f"render failed {item_index}")
        return f"finished-{item_index}.mp4"


def test_pause_finishes_current_item_then_stops_starting_next(db_engine):
    fake = FakeExecutor()
    queue = MixQueue(db_engine, fake)
    task = queue.enqueue({"quantity": 3})
    fake.on_start = lambda index: queue.request_pause(task.id) if index == 0 else None

    queue.run_once_until_idle()

    row = queue.get(task.id)
    assert fake.started == [0]
    assert row.completed_count == 1
    assert row.success_count == 1
    assert row.state == "paused"


def test_resume_continues_from_remaining_item(db_engine):
    fake = FakeExecutor()
    queue = MixQueue(db_engine, fake)
    task = queue.enqueue({"quantity": 3})
    fake.on_start = lambda index: queue.request_pause(task.id) if index == 0 else None
    queue.run_once_until_idle()
    fake.on_start = None

    queue.resume(task.id)
    queue.run_once_until_idle()

    row = queue.get(task.id)
    assert fake.started == [0, 1, 2]
    assert row.success_count == 3
    assert row.state == "completed"
