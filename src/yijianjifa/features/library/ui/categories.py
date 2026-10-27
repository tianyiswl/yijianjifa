from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QInputDialog, QMessageBox, QComboBox

from yijianjifa.features.library.copy_service import CategoryRepository, single_category_ids


class Categories(QWidget):
    filter_changed = Signal()
    assignment_requested = Signal(set)

    def __init__(self, engine, kind, parent=None):
        super().__init__(parent)
        self.repository = CategoryRepository(engine)
        self.kind = kind
        self.filter_ids = set()
        layout = QVBoxLayout(self)
        self.items = QListWidget()
        self.items.setMaximumHeight(85)
        layout.addWidget(self.items)
        controls = QHBoxLayout()
        for label, callback in (("新建分类", self.create), ("重命名分类", self.rename),
                                ("删除分类", self.delete), ("全选分类", self.select_all),
                                ("清空分类选择", lambda: self.set_checked(set())),
                                ("按分类筛选", self.apply_filter), ("显示全部", self.clear_filter)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.assign_button = QPushButton("设置分类")
        self.assign_button.clicked.connect(self.request_assignment)
        layout.addWidget(self.assign_button)
        self.refresh()

    def checked(self):
        return {self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count())
                if self.items.item(i).checkState() == Qt.CheckState.Checked}

    def destination_ids(self):
        ids = self.checked()
        return ids if len(ids) <= 1 else choose_categories(self.repository.engine, self.kind, set(), self)

    def request_assignment(self):
        ids = self.destination_ids()
        if ids is not None:
            self.assignment_requested.emit(ids)

    def set_checked(self, ids):
        for i in range(self.items.count()):
            item = self.items.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in ids else Qt.CheckState.Unchecked)

    def refresh(self):
        checked = self.checked()
        self.items.clear()
        records = self.repository.list(self.kind)
        for record in records:
            item = QListWidgetItem(record.name)
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.items.addItem(item)
        self.set_checked(checked)
        self.filter_ids &= {r.id for r in records}
        self.filter_changed.emit()

    def select_all(self):
        self.set_checked({self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count())})

    def apply_filter(self):
        self.filter_ids = self.checked()
        self.filter_changed.emit()

    def clear_filter(self):
        self.filter_ids = set()
        self.filter_changed.emit()

    def create(self):
        name, ok = QInputDialog.getText(self, "新建分类", "分类名称")
        if ok and name.strip():
            self.repository.create(self.kind, name)
            self.refresh()

    def rename(self):
        item = self.items.currentItem()
        if item is None:
            return
        name, ok = QInputDialog.getText(self, "重命名分类", "分类名称", text=item.text())
        if ok and name.strip():
            self.repository.rename(item.data(Qt.ItemDataRole.UserRole), name)
            self.refresh()

    def delete(self):
        item = self.items.currentItem()
        if item is not None and QMessageBox.question(self, "删除分类", "仅删除分类，不删除资料。继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.repository.delete(item.data(Qt.ItemDataRole.UserRole))
            self.refresh()


class CategoryChecks(QWidget):
    """One destination category, separate from the multi-category filter."""
    def __init__(self, engine, kind, parent=None):
        super().__init__(parent)
        self.repository = CategoryRepository(engine)
        self.kind = kind
        self.items = QComboBox()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.items)
        self.refresh()

    def checked(self):
        identity = self.items.currentData()
        return {identity} if identity else set()

    def set_checked(self, ids):
        identities = single_category_ids(ids)
        index = self.items.findData(identities[0]) if identities else 0
        self.items.setCurrentIndex(max(0, index))

    def refresh(self):
        checked = self.checked()
        self.items.clear()
        self.items.addItem('未分类', None)
        for r in self.repository.list(self.kind):
            self.items.addItem(r.name, r.id)
        self.set_checked(checked)


def choose_categories(engine, kind, initial, parent):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel
    dialog = QDialog(parent)
    dialog.setWindowTitle("设置分类")
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("选择要放入的分类，或选择“未分类”。"))
    checks = CategoryChecks(engine, kind)
    checks.set_checked(initial)
    layout.addWidget(checks)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return checks.checked() if dialog.exec() == QDialog.DialogCode.Accepted else None
