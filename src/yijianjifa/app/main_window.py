from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
    QStackedWidget, QLabel,
)

from yijianjifa import APP_NAME
from yijianjifa.features.library.ui.library_page import LibraryPage
from yijianjifa.features.mixing.ui.controller import MixController
from yijianjifa.features.mixing.ui.mixing_page import MixingPage
from yijianjifa.features.mixing.ui.queue_page import QueuePage


class MainWindow(QMainWindow):
    """Native desktop shell; unfinished screens never impersonate working flows."""

    def __init__(self, engine, paths, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        root = QWidget()
        layout = QVBoxLayout(root)
        title = QLabel("一剪即发  ·  视频混剪与发布")
        title.setStyleSheet("font-size: 22px; font-weight: 600; padding: 8px;")
        layout.addWidget(title)
        layout.addWidget(QLabel("开发版本 · 本地资料库与制作队列 · 尚未通过 V1 完整验收 · 不执行真实抖音操作"))
        content = QHBoxLayout()
        self.navigation = QListWidget()
        self.navigation.setMaximumWidth(175)
        labels = ["资料库", "混剪制作", "成品库", "普通发布", "团购带货", "账号管理", "任务记录", "常用方案与设置"]
        self.navigation.addItems(labels)
        self.pages = QStackedWidget()
        self.library = LibraryPage(engine, paths)
        self.pages.addWidget(self.library)
        self.mix_controller = MixController(engine, paths, self)
        self.mixing = MixingPage(self.mix_controller)
        self.task_page = QueuePage(self.mix_controller)
        self.mix_controller.changed.connect(self.library.copy.refresh)
        self.mixing.tasks_requested.connect(lambda: self.navigation.setCurrentRow(6))
        for name in labels[1:]:
            if name == "混剪制作":
                self.pages.addWidget(self.mixing)
                continue
            if name == "任务记录":
                self.pages.addWidget(self.task_page)
                continue
            page = QWidget()
            page_layout = QVBoxLayout(page)
            label = QLabel(f"{name}\n\n现有后台代码保留；正式桌面页面尚未接入。\n本入口不代表功能完成，不执行任务或平台操作。")
            label.setWordWrap(True)
            page_layout.addWidget(label)
            page_layout.addStretch()
            self.pages.addWidget(page)
        content.addWidget(self.navigation)
        content.addWidget(self.pages, 1)
        layout.addLayout(content)
        self.setCentralWidget(root)
        self.navigation.setCurrentRow(0)
        self.navigation.currentRowChanged.connect(self._navigate)

    def _navigate(self, index):
        if self.pages.currentIndex() == 0 and not self.library.may_leave():
            self.navigation.blockSignals(True)
            self.navigation.setCurrentRow(0)
            self.navigation.blockSignals(False)
            return
        self.library.stop_preview()
        self.task_page.preview.stop()
        if index == 1:
            self.mixing.refresh()
        self.pages.setCurrentIndex(index)

    def closeEvent(self, event):
        if self.library.busy:
            self.statusBar().showMessage("文件导入仍在进行，请完成后再关闭，避免中断保存。")
            event.ignore()
            return
        if not self.library.may_leave():
            event.ignore()
            return
        if not self.mix_controller.prepare_close():
            self.statusBar().showMessage("正在完成当前视频，已暂停后续项目；完成后请再次关闭。")
            event.ignore()
            return
        self.task_page.preview.stop()
        self.library.stop_preview()
        event.accept()
