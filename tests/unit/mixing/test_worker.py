from yijianjifa.features.mixing.worker import MixWorker


class QueueSpy:
    def __init__(self):
        self.calls = 0

    def run_once_until_idle(self):
        self.calls += 1
        return ["M1"]


def test_worker_runs_queue_until_idle():
    queue = QueueSpy()
    worker = MixWorker(queue)
    assert worker.run() == ["M1"]
    assert queue.calls == 1
