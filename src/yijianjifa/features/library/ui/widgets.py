from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, QThread
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from yijianjifa.core.selection import FilteredSelection


class BackgroundWork(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.completed.emit(self.function())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class MediaPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(170)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.6)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        layout.addWidget(self.video)
        controls = QHBoxLayout()
        self.play_button = QPushButton("播放 / 暂停")
        self.seek = QSlider(Qt.Orientation.Horizontal)
        controls.addWidget(self.play_button)
        controls.addWidget(self.seek)
        layout.addLayout(controls)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.play_button.clicked.connect(self.toggle)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.player.durationChanged.connect(lambda duration: self.seek.setRange(0, duration))
        self.player.positionChanged.connect(self.seek.setValue)
        self.player.errorOccurred.connect(lambda *args: self.error.setText(self.player.errorString()))

    def load(self, path: Path, *, video=True):
        self.player.stop()
        self.error.clear()
        self.video.setVisible(video)
        self.player.setSource(QUrl())
        if not path.is_file():
            self.error.setText("文件未找到，请检查右侧位置。")
            return
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.player.play()

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()


class RecordList(QWidget):
    opened = Signal(str)
    files_dropped = Signal(list)
    PAGE_SIZE = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection = FilteredSelection()
        self.records = []
        self.page_number = 0
        self._painting = False
        self.setAcceptDrops(True)
        self.layout = QVBoxLayout(self)
        self.toolbar = QHBoxLayout()
        self.layout.addLayout(self.toolbar)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索当前资料")
        self.layout.addWidget(self.search)
        self.search.textChanged.connect(self._filter_changed)
        controls = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.clear_all_button = QPushButton("取消全选")
        self.count = QLabel()
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.clear_all_button)
        controls.addWidget(self.count)
        self.layout.addLayout(controls)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["选择", "名称", "说明"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.itemChanged.connect(self._checked)
        self.layout.addWidget(self.table)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel()
        paging.addWidget(self.previous_button)
        paging.addWidget(self.page_label)
        paging.addWidget(self.next_button)
        self.layout.addLayout(paging)
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        self.select_all_button.clicked.connect(self._select_all)
        self.clear_all_button.clicked.connect(self._clear_all)

    @property
    def filtered(self):
        query = self.search.text().strip().casefold()
        return [r for r in self.records if not query or query in " ".join(r[1:]).casefold()]

    def set_records(self, rows):
        self.records = list(rows)
        valid = {r[0] for r in rows}
        for id in self.selection.selected_ids - valid:
            self.selection.clear(id)
        self.paint_rows()

    def _filter_changed(self):
        self.page_number = 0
        self.paint_rows()

    def paint_rows(self):
        rows = self.filtered
        pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_number = min(self.page_number, pages - 1)
        current = rows[self.page_number * self.PAGE_SIZE:(self.page_number + 1) * self.PAGE_SIZE]
        self._painting = True
        self.table.setRowCount(len(current))
        for i, (id, name, description) in enumerate(current):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setData(Qt.ItemDataRole.UserRole, id)
            check.setCheckState(Qt.CheckState.Checked if self.selection.is_selected(id) else Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, check)
            self.table.setItem(i, 1, QTableWidgetItem(name))
            self.table.setItem(i, 2, QTableWidgetItem(description))
        self._painting = False
        self.count.setText(f"筛选 {len(rows)} 条 · 已选 {self.selection.count} 条")
        self.page_label.setText(f"{self.page_number + 1} / {pages}")
        self.previous_button.setEnabled(self.page_number > 0)
        self.next_button.setEnabled(self.page_number < pages - 1)

    def _checked(self, item):
        if self._painting or item.column() != 0:
            return
        id = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selection.select(id)
        else:
            self.selection.clear(id)
        self.count.setText(f"筛选 {len(self.filtered)} 条 · 已选 {self.selection.count} 条")

    def _select_all(self):
        self.selection.select_all(r[0] for r in self.filtered)
        self.paint_rows()

    def _clear_all(self):
        self.selection.clear_all(r[0] for r in self.filtered)
        self.paint_rows()

    def _page(self, delta):
        self.page_number = max(0, self.page_number + delta)
        self.paint_rows()

    def _open_row(self, row, column):
        self.opened.emit(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.files_dropped.emit([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.acceptProposedAction()
