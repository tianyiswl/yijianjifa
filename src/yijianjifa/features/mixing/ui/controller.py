from PySide6.QtCore import QObject, Signal, QTimer
from yijianjifa.features.library.ui.widgets import BackgroundWork
from ..desktop_service import DesktopMixService


class MixController(QObject):
    changed = Signal()
    problem = Signal(str)

    def __init__(self, engine, paths, parent=None):
        super().__init__(parent)
        self.service = DesktopMixService(engine, paths)
        self.recovered = self.service.hold_after_restart()
        self.busy = False
        self.job = None
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        if self.busy:
            self.changed.emit()
            return
        if not any(t.state == 'waiting' for t in self.service.tasks()):
            return
        self.busy = True
        self.job = BackgroundWork(self.service.queue.run_once_until_idle, self)
        self.job.failed.connect(self._failed)
        self.job.finished.connect(self._finished)
        self.job.start()
        self.changed.emit()

    def _failed(self, error):
        self.service.hold_after_restart()
        self.problem.emit(error)

    def _finished(self):
        self.busy = False
        self.job.deleteLater()
        self.job = None
        self.changed.emit()

    def prepare_close(self):
        if not self.busy:
            self.timer.stop()
        for task in self.service.tasks():
            if task.state in {'waiting', 'running'}:
                self.service.queue.request_pause(task.id)
        return not self.busy
