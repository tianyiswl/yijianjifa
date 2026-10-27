from __future__ import annotations

import uuid
from dataclasses import replace
from PySide6.QtCore import Qt

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QSplitter, QFormLayout, QPlainTextEdit,
    QPushButton, QLabel, QMessageBox, QFileDialog, QMenu, QInputDialog, QHBoxLayout,
)

from yijianjifa.features.library.copy_service import CopyDraft, CopyRepository
from yijianjifa.features.library.excel_import import (
    classify_rows, read_excel_rows, apply_import, export_template, export_with_ids,
)
from .widgets import RecordList
from .categories import Categories, CategoryChecks
from yijianjifa.features.library.usage_progress import UsageProgressService


class CopyPane(QWidget):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.repository = CopyRepository(engine)
        self.progress = UsageProgressService(engine)
        self._base_categories = set()
        self.current_id = None
        self._base = None
        self._new = False
        self.import_preview = None
        layout = QVBoxLayout(self)
        self.categories = Categories(engine, "copy")
        layout.addWidget(self.categories)
        split = QSplitter()
        self.list = RecordList()
        self.new_button = QPushButton("手动新建")
        self.import_button = QPushButton("导入 Excel")
        self.template_button = QPushButton("下载六列模板")
        self.export_button = QPushButton("导出已选文案")
        for b in (self.new_button, self.import_button, self.template_button, self.export_button):
            self.list.toolbar.addWidget(b)
        self.new_button.clicked.connect(self.new_copy)
        self.import_button.clicked.connect(self.choose_excel)
        self.template_button.clicked.connect(self.save_template)
        self.export_button.clicked.connect(self.export_selected)
        self.list.opened.connect(self.open_record)
        self.list.files_dropped.connect(self.dropped)
        self.list.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.table.customContextMenuRequested.connect(self.context_menu)
        split.addWidget(self.list)
        detail = QWidget()
        right = QVBoxLayout(detail)
        self.identity = QLabel("双击文案查看和编辑")
        right.addWidget(self.identity)
        form = QFormLayout()
        self.fields = {}
        for field, label in (("title", "标题"), ("voice", "口播稿（可空）"), ("body", "发布文案"), ("tags", "标签"), ("full", "全程字幕（可空）")):
            edit = QPlainTextEdit()
            edit.setMaximumHeight(85 if field in {"voice", "body"} else 55)
            self.fields[field] = edit
            form.addRow(label, edit)
        self.editor_categories = CategoryChecks(engine, "copy")
        form.addRow("文案分类", self.editor_categories)
        right.addLayout(form)
        self.save_button = QPushButton("保存文案")
        self.save_button.clicked.connect(self.save)
        right.addWidget(self.save_button)
        right.addStretch()
        split.addWidget(detail)
        split.setSizes([650, 380])
        layout.addWidget(split)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress_labels, self.reset_buttons = {}, {}
        for scope, label in (("mixing", "制作选文案"), ("finished_metadata", "外部成品配文案")):
            row = QHBoxLayout()
            self.progress_labels[scope] = QLabel()
            button = QPushButton(f"{label}：重新开始一轮")
            button.clicked.connect(lambda checked=False, scope=scope: self.reset_progress(scope))
            self.reset_buttons[scope] = button
            row.addWidget(self.progress_labels[scope])
            row.addWidget(button)
            layout.addLayout(row)
        self.categories.filter_changed.connect(self.categories_changed)
        self.categories.assignment_requested.connect(self.assign_categories)
        self._enable_editor(False)
        self.refresh()

    def _enable_editor(self, enabled):
        for field in self.fields.values():
            field.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.editor_categories.setEnabled(enabled)

    def values(self):
        return {key: edit.toPlainText() for key, edit in self.fields.items()}

    @property
    def dirty(self):
        if self._new:
            return any(self.values().values()) or bool(self.editor_categories.checked())
        return self._base is not None and (self.values() != self._base or self.editor_categories.checked() != self._base_categories)

    def may_leave(self):
        if not self.dirty:
            return True
        dialog = QMessageBox(self)
        dialog.setWindowTitle("文案尚未保存")
        dialog.setText("如何处理右侧尚未保存的修改？")
        save = dialog.addButton("保存并继续", QMessageBox.ButtonRole.AcceptRole)
        discard = dialog.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
        keep = dialog.addButton("继续编辑", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(keep)
        dialog.exec()
        if dialog.clickedButton() is save:
            return self.save()
        if dialog.clickedButton() is discard:
            if self.current_id:
                self._load(self.repository.get(self.current_id))
            else:
                self._load(None)
            return True
        return False

    def refresh(self):
        records = self.repository.list_all()
        visible = [r for r in records if not self.categories.filter_ids or self.repository.category_ids(r.id) & self.categories.filter_ids]
        self.list.set_records([(r.id, r.title or "（无标题）", r.id) for r in visible])
        for scope, label in (("mixing", "制作选文案"), ("finished_metadata", "外部成品配文案")):
            used = sum(self.progress.is_used(scope, r.id) for r in records)
            self.progress_labels[scope].setText(f"{label} · 本轮已用 {used} / {len(records)}（全部文案）")

    def _load(self, record):
        self.current_id = record.id if record else None
        self._new = False
        self.editor_categories.refresh()
        self._base_categories = self.repository.category_ids(record.id) if record else set()
        self.editor_categories.set_checked(self._base_categories)
        values = {key: getattr(record, key) if record else "" for key in self.fields}
        self._base = values.copy() if record else None
        for key, value in values.items():
            self.fields[key].setPlainText(value)
        self.identity.setText(f"文案编号：{self.current_id}" if record else "未选择文案")
        self._enable_editor(record is not None)

    def open_record(self, id):
        if id == self.current_id:
            return
        if self.may_leave():
            self._load(self.repository.get(id))

    def new_copy(self):
        if not self.may_leave():
            return
        self._load(None)
        self._new = True
        self.identity.setText("新文案 · 保存后自动生成编号")
        self._enable_editor(True)
        self.fields["title"].setFocus()

    def save(self):
        try:
            if self._new:
                if not any(v.strip() for v in self.values().values()):
                    self.status.setText("请先填写文案内容。")
                    return False
                record = self.repository.create(id="W" + uuid.uuid4().hex[:12].upper(), **self.values())
            elif self.current_id:
                record = self.repository.update(CopyDraft(id=self.current_id, **self.values()))
            else:
                return False
            self.repository.set_categories(record.id, self.editor_categories.checked())
            self._load(record)
            self.refresh()
            self.status.setText("已保存到本机；不会修改已确认任务的快照。")
            return True
        except Exception as exc:
            self.status.setText(f"保存失败，输入已保留：{exc}")
            return False

    def preview_excel(self, path):
        self.import_preview = classify_rows(self.repository, read_excel_rows(path))
        p = self.import_preview
        self.status.setText(f"新增 {len(p.new)} · 重复 {len(p.duplicate)} · 待更新 {len(p.updates)} · 错误 {len(p.errors)}")
        return p

    def apply_excel(self, *, approve_updates=False):
        if self.import_preview is None:
            return
        category_ids = self.categories.destination_ids()
        if category_ids is None:
            return
        p = self.import_preview
        result = apply_import(self.repository, p, approved_update_ids={d.id for d in p.updates} if approve_updates else set())
        for id in result.new_ids + result.updated_ids:
            if category_ids:
                self.repository.set_categories(id, category_ids)
        self.import_preview = None
        self.refresh()
        self.status.setText(f"已新增 {len(result.new_ids)} 条，更新 {len(result.updated_ids)} 条，错误 {result.error_count} 行未导入。")
        if self.current_id and not self.dirty:
            self._load(self.repository.get(self.current_id))
        return result

    def choose_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入六列文案表", "", "Excel (*.xlsx)")
        if path:
            self.confirm_excel(path)

    def dropped(self, paths):
        if len(paths) != 1 or not paths[0].lower().endswith(".xlsx"):
            self.status.setText("请一次拖入一个 .xlsx 文案表。")
        else:
            self.confirm_excel(paths[0])

    def confirm_excel(self, path):
        if not self.may_leave():
            return
        try:
            preview = self.preview_excel(path)
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Excel 导入预检查")
            dialog.setText(self.status.text())
            details = [f"待更新 {d.id}：{d.title}" for d in preview.updates]
            details += [f"错误行 {e.row_number}：{e.reason}" for e in preview.errors]
            dialog.setDetailedText("\n".join(details) or "没有待更新或错误行。")
            add_only = dialog.addButton("仅导入新增", QMessageBox.ButtonRole.AcceptRole)
            update = dialog.addButton("确认新增与更新", QMessageBox.ButtonRole.ActionRole)
            cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.exec()
            if dialog.clickedButton() in (add_only, update):
                self.apply_excel(approve_updates=dialog.clickedButton() is update)
        except Exception as exc:
            self.status.setText(f"导入失败：{exc}")

    def save_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存文案模板", "文案模板.xlsx", "Excel (*.xlsx)")
        if path:
            try:
                export_template(path)
                self.status.setText("六列模板与填写示例已保存。")
            except Exception as exc:
                self.status.setText(f"保存失败：{exc}")

    def export_selected(self):
        ids = self.list.selection.selected_ids
        if not ids:
            self.status.setText("请先选择要导出的文案。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出带编号文案", "文案导出.xlsx", "Excel (*.xlsx)")
        if path:
            try:
                export_with_ids(self.repository, sorted(ids), path)
                self.status.setText(f"已导出 {len(ids)} 条文案。")
            except Exception as exc:
                self.status.setText(f"导出失败：{exc}")


    def categories_changed(self):
        self.editor_categories.refresh()
        self._base_categories &= {r.id for r in self.categories.repository.list("copy")}
        self.refresh()

    def assign_categories(self, ids, targets=None):
        if not self.may_leave():
            return
        if len(ids) > 1:
            from .categories import choose_categories
            ids = choose_categories(self.repository.engine, 'copy', set(), self)
            if ids is None:
                return
        targets = self.list.selection.selected_ids if targets is None else targets
        for id in targets:
            self.repository.set_categories(id, ids)
        if self.current_id in targets:
            self._load(self.repository.get(self.current_id))
        self.refresh()

    def rename_record(self, id, title):
        if not self.may_leave():
            return
        self.repository.update(replace(self.repository.get(id), title=title))
        if self.current_id == id:
            self._load(self.repository.get(id))
        self.refresh()

    def rename_dialog(self, id):
        title, ok = QInputDialog.getText(self, "重命名文案", "标题", text=self.repository.get(id).title)
        if ok:
            self.rename_record(id, title)

    def delete_records(self, ids):
        if not ids or not self.may_leave():
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("删除文案")
        dialog.setText(f"删除 {len(ids)} 条文案？已确认任务与历史保留。")
        delete = dialog.addButton("删除记录", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        if dialog.clickedButton() is not delete:
            return
        errors, count = [], 0
        for id in tuple(ids):
            try:
                self.repository.delete(id)
                count += 1
                if self.current_id == id:
                    self._load(None)
            except Exception as exc:
                errors.append(str(exc))
        self.refresh()
        self.status.setText(f"已删除 {count} 条。" + "；".join(errors))

    def context_targets(self, id):
        return self.list.selection.context_targets(id)

    def context_menu(self, point):
        row = self.list.table.rowAt(point.y())
        if row < 0:
            return
        id = self.list.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        targets = self.context_targets(id)
        menu = QMenu(self)
        if len(targets) == 1:
            menu.addAction("查看 / 编辑", lambda: self.open_record(id))
            menu.addAction("重命名", lambda: self.rename_dialog(id))
        menu.addAction(f"设置分类（{len(targets)} 条）", lambda: self.assign_categories(self.categories.checked(), targets))
        menu.addAction(f"删除 {len(targets)} 条记录", lambda: self.delete_records(targets))
        menu.exec(self.list.table.viewport().mapToGlobal(point))

    def reset_progress(self, scope):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("重新开始一轮")
        dialog.setText("只重置这一套文案使用进度，不清空防重复历史。")
        reset = dialog.addButton("重新开始一轮", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        if dialog.clickedButton() is reset:
            self.progress.reset_cycle(scope)
            self.refresh()
