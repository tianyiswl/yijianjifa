from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget

from .media_pane import MediaPane
from .copy_pane import CopyPane


class LibraryPage(QWidget):
    def __init__(self, engine, paths, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.video = MediaPane(engine, paths, "video")
        self.copy = CopyPane(engine)
        self.music = MediaPane(engine, paths, "music")
        for widget, title in ((self.video, "视频素材"), (self.copy, "文案"), (self.music, "背景音乐")):
            self.tabs.addTab(widget, title)
        layout.addWidget(self.tabs)
        self._previous_tab = 0
        self.tabs.currentChanged.connect(self._tab_changed)

    def _tab_changed(self, index):
        if self._previous_tab == 1 and not self.copy.may_leave():
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(1)
            self.tabs.blockSignals(False)
            return
        self.stop_preview()
        self._previous_tab = index

    def stop_preview(self):
        self.video.preview.stop()
        self.music.preview.stop()

    @property
    def busy(self):
        return self.video.busy or self.music.busy

    def may_leave(self):
        return self.copy.may_leave()
