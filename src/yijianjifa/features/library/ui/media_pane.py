from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QSplitter, QPushButton, QCheckBox, QLabel,
    QFileDialog, QMessageBox, QInputDialog, QMenu,
)

from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.library.preferences import LibraryPreferences
from yijianjifa.os_integration.reveal import reveal_path
from .widgets import MediaPreview, RecordList, BackgroundWork
from .categories import Categories, choose_categories


class MediaPane(QWidget):
    import_finished = Signal()

    def __init__(self, engine, paths, kind, parent=None):
        super().__init__(parent)
        self.service = MediaStorageService(engine, paths, kind=kind)
        self.preferences = LibraryPreferences(engine)
        self.kind = kind
        self.busy = False
        self._job = None
        self.current_id = None
        layout = QVBoxLayout(self)
        self.keep_copy = QCheckBox("另外保存一份素材" if kind == "video" else "另外保存一份音乐")
        self.keep_copy.setChecked(self.preferences.keep_copy(kind))
        self.keep_copy.toggled.connect(lambda v: self.preferences.set_keep_copy(kind, v))
        layout.addWidget(self.keep_copy)
        self.categories = Categories(engine, kind)
        layout.addWidget(self.categories)
        split = QSplitter()
        self.list = RecordList()
        # Public controls are shared with the list component, not duplicate state.
        for name in ("table", "selection", "search", "select_all_button", "clear_all_button", "next_button"):
            setattr(self, name, getattr(self.list, name))
        self.import_button = QPushButton("导入文件")
        folder_button = QPushButton("导入文件夹")
        self.delete_button = QPushButton("删除记录")
        for button in (self.import_button, folder_button, self.delete_button):
            self.list.toolbar.addWidget(button)
        self.import_button.clicked.connect(self.choose_files)
        folder_button.clicked.connect(self.choose_folder)
        self.delete_button.clicked.connect(lambda: self.delete_records(self.selection.selected_ids))
        self.list.files_dropped.connect(self.start_import)
        self.list.opened.connect(self.open_record)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        split.addWidget(self.list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self.name = QLabel("双击列表预览")
        self.paths = QLabel()
        self.paths.setWordWrap(True)
        self.paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview = MediaPreview()
        detail_layout.addWidget(self.name)
        detail_layout.addWidget(self.preview)
        detail_layout.addWidget(self.paths)
        self.reveal_original = QPushButton("查看原文件")
        self.reveal_managed = QPushButton("查看程序副本")
        self.reveal_original.clicked.connect(lambda: self.reveal(False))
        self.reveal_managed.clicked.connect(lambda: self.reveal(True))
        detail_layout.addWidget(self.reveal_original)
        detail_layout.addWidget(self.reveal_managed)
        detail_layout.addStretch()
        split.addWidget(detail)
        split.setSizes([650, 380])
        layout.addWidget(split)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.categories.filter_changed.connect(self.refresh)
        self.categories.assignment_requested.connect(self.assign_categories)
        self.refresh()

    def assign_categories(self, ids):
        for asset_id in self.selection.selected_ids:
            self.service.append_categories(asset_id, ids)
        self.refresh()

    def refresh(self):
        records = self.service.list_assets(category_ids=self.categories.filter_ids)
        self.list.set_records([(r.id, r.display_name, str(r.effective_path) if r.effective_path.exists() else "文件未找到") for r in records])

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择本机文件", "", "媒体文件 (*)")
        if files:
            self.start_import(files)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择素材文件夹")
        if folder:
            self.start_import([folder])

    def start_import(self, paths):
        if self.busy:
            self.status.setText("正在导入，请等待当前导入结束。")
            return
        keep_copy = self.keep_copy.isChecked()
        category_ids = self.categories.destination_ids()
        if category_ids is None:
            return
        sources = tuple(Path(p) for p in paths)
        def execute():
            completed, errors = 0, []
            for source in sources:
                files = sorted(source.rglob("*")) if source.is_dir() else [source]
                for path in files:
                    if path.is_dir() or (source.is_dir() and path.suffix.lower() not in self.service.supported_extensions):
                        continue
                    try:
                        self.service.import_media(path, keep_copy=keep_copy, category_ids=category_ids)
                        completed += 1
                    except Exception as exc:
                        errors.append(f"{path.name}: {exc}")
            return completed, errors
        self.busy = True
        self.list.setEnabled(False)
        self.status.setText("正在读取和校验文件，原文件不会被修改……")
        self._job = BackgroundWork(execute, self)
        self._job.completed.connect(self._import_result)
        self._job.failed.connect(self._import_error)
        self._job.finished.connect(self._import_done)
        self._job.start()

    def _import_result(self, result):
        count, errors = result
        self.refresh()
        self.status.setText(f"已处理 {count} 个文件，失败 {len(errors)} 个。重复文件合并为同一条记录。" + ("\n" + "\n".join(errors[:10]) if errors else ""))

    def _import_error(self, error):
        self.status.setText(f"导入失败：{error}")

    def _import_done(self):
        self.busy = False
        self.list.setEnabled(True)
        self.import_finished.emit()

    def open_record(self, id):
        record = self.service.refresh_missing_state(id)
        self.current_id = id
        self.name.setText(record.display_name)
        self.paths.setText(f"导入来源：{record.original_path}\n保存方式：{'程序副本' if record.managed_path else '使用原文件'}\n程序副本：{record.managed_path or '未创建'}")
        self.reveal_managed.setEnabled(record.managed_path is not None)
        self.preview.load(record.effective_path, video=self.kind == "video")

    def reveal(self, managed):
        record = self.service.get(self.current_id) if self.current_id else None
        if record:
            path = record.managed_path if managed else record.original_path
            if path is None or not reveal_path(path):
                self.status.setText("文件未找到，未打开其他位置。")

    def rename_record(self, id, name):
        self.service.rename(id, name)
        self.refresh()
        if id == self.current_id:
            self.name.setText(name)

    def rename_dialog(self, id):
        record = self.service.get(id)
        text, ok = QInputDialog.getText(self, "重命名显示名称", "不会修改电脑原文件名", text=record.display_name)
        if ok and text.strip():
            self.rename_record(id, text)

    def delete_records(self, ids):
        if not ids or self.busy:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("确认删除资料库记录")
        dialog.setText(f"删除 {len(ids)} 条记录？电脑原文件保留；程序另存的副本会删除。历史保留。")
        delete = dialog.addButton("删除记录", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        if dialog.clickedButton() is not delete:
            return
        self.preview.stop()
        errors, deleted = [], 0
        for id in tuple(ids):
            try:
                self.service.delete_media_record(id)
                deleted += 1
                if self.current_id == id:
                    self.current_id = None
                    self.name.setText("双击列表预览")
                    self.paths.clear()
            except (RuntimeError, OSError) as exc:
                errors.append(str(exc))
        self.refresh()
        self.status.setText(f"已删除 {deleted} 条记录，原文件保留。" + ("\n未删除：" + "；".join(errors) if errors else ""))

    def context_targets(self, id):
        return self.selection.context_targets(id)

    def context_menu(self, point):
        row = self.table.rowAt(point.y())
        if row < 0:
            return
        id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        targets = self.context_targets(id)
        menu = QMenu(self)
        if len(targets) == 1:
            menu.addAction("预览", lambda: self.open_record(id))
            menu.addAction("重命名", lambda: self.rename_dialog(id))
            record = self.service.get(id)
            menu.addAction("查看原文件", lambda: reveal_path(record.original_path))
            if record.managed_path:
                menu.addAction("查看程序副本", lambda: reveal_path(record.managed_path))
        menu.addAction("调整分类", lambda: self.category_dialog(targets))
        menu.addAction(f"删除 {len(targets)} 条记录", lambda: self.delete_records(targets))
        menu.exec(self.table.viewport().mapToGlobal(point))


    def replace_categories(self, targets, ids):
        for id in targets:
            self.service.set_categories(id, ids)
        self.refresh()

    def category_dialog(self, targets):
        initial = set.intersection(*(self.service.category_ids(id) for id in targets))
        ids = choose_categories(self.service.engine, self.kind, initial, self)
        if ids is not None:
            self.replace_categories(targets, ids)
