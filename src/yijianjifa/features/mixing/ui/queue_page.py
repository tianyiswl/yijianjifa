import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QLabel, QPushButton, QPlainTextEdit, QMessageBox, QMenu
from yijianjifa.features.library.ui.widgets import RecordList, MediaPreview
from yijianjifa.os_integration.reveal import reveal_path

STATES = {'cancelling': '正在取消（当前条不入库）', 'waiting': '等待制作', 'running': '制作中', 'paused': '已暂停', 'interrupted': '中断待处理',
          'cancelled': '已取消剩余', 'completed': '制作完成', 'completed_with_errors': '制作结束（有失败）'}


class QueuePage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller, self.service = controller, controller.service
        self.current_id = None
        layout = QVBoxLayout(self)
        self.notice = QLabel('制作队列 · 暂停会先完成当前视频；重启后须选择任务并点击继续。发布任务尚未接入本页。')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        split = QSplitter()
        self.list = RecordList()
        self.pause_button, self.resume_button = QPushButton('暂停'), QPushButton('继续')
        self.cancel_button = QPushButton('取消剩余')
        for b in (self.pause_button, self.resume_button, self.cancel_button):
            self.list.toolbar.addWidget(b)
        self.pause_button.clicked.connect(lambda: self.apply('request_pause'))
        self.resume_button.clicked.connect(lambda: self.apply('resume'))
        self.cancel_button.clicked.connect(lambda: self.cancel_tasks(self.list.selection.selected_ids))
        self.list.opened.connect(self.open_task)
        self.list.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.table.customContextMenuRequested.connect(self.context_menu)
        split.addWidget(self.list)
        detail = QWidget()
        right = QVBoxLayout(detail)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        right.addWidget(self.details)
        self.preview = MediaPreview()
        right.addWidget(self.preview)
        self.outputs = RecordList()
        self.outputs.table.setMaximumHeight(130)
        self.outputs.opened.connect(self.open_output)
        right.addWidget(self.outputs)
        reveal = QPushButton('查看当前批次文件夹')
        reveal.clicked.connect(self.reveal_folder)
        right.addWidget(reveal)
        split.addWidget(detail)
        split.setSizes([600, 480])
        layout.addWidget(split)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controller.changed.connect(self.refresh)
        controller.problem.connect(self.status.setText)
        self.refresh()

    def refresh(self):
        self.list.set_records([(t.id, f'批次 {t.id[:8]} · {STATES.get(t.state, t.state)}',
            f'完成 {t.completed_count}/{t.quantity} · 成功 {t.success_count} · 失败 {t.failure_count}') for t in self.service.tasks()])
        if self.current_id:
            self.show_detail()

    def apply(self, action, targets=None):
        ids = self.list.selection.selected_ids if targets is None else targets
        errors = []
        for id in ids:
            try:
                getattr(self.service.queue, action)(id)
            except Exception as exc:
                errors.append(str(exc))
        self.status.setText('；'.join(errors) if errors else '任务状态已更新。')
        self.refresh()

    def move_task(self, id, delta):
        try:
            self.service.queue.move(id, delta)
            self.refresh()
        except Exception as exc:
            self.status.setText(f'无法移动：{exc}')

    def cancel_tasks(self, ids, *, confirm=True):
        if not ids:
            return
        if confirm and QMessageBox.question(self, '取消剩余', '保留已完成成品，取消尚未完成的剩余项目？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.apply('cancel_remaining', ids)

    def open_task(self, id):
        self.preview.stop()
        self.current_id = id
        self.show_detail()

    def show_detail(self):
        task = self.service.queue.get(self.current_id)
        results = self.service.item_results(task.id)
        lines = [f'任务号：{task.id}', f'状态：{STATES.get(task.state, task.state)}',
                 f'成功 {task.success_count} / 失败 {task.failure_count} / 共 {task.quantity}',
                 f'保存目录：{task.snapshot.get("output_directory", "旧任务未记录")}',
                 '问题详情：']
        for r in results:
            if r['error']:
                lines.extend([f'第 {r["index"] + 1} 条制作失败 · {r["time"]}', f'原因：{r["error"]}',
                              '影响：本条没有可用成品，其他条目按原设置继续。',
                              '建议：核对素材文件、可用组合和本机配音/FFmpeg；修复后创建新批次。'])
        if not any(r['error'] for r in results):
            lines.append(task.last_error or '暂无制作错误。')
        plan = task.snapshot.get('plan', {})
        lines.extend(['', '确认时设置：',
                      f'画面：{plan.get("aspect_ratio", "未记录")} · {plan.get("resolution", "未记录")}',
                      '画面适配：' + ('裁切填满' if plan.get('frame_fit') == 'crop_fill' else '保留完整画面 + 模糊背景'),
                      f'无口播总长：{task.snapshot.get("total_ms", 0) / 1000:g} 秒（有口播跟随配音）',
                      f'片段时长：{task.snapshot.get("segment_ms", 0) / 1000:g} 秒',
                      '转场：无；背景音乐：无；自动发布：关闭'])
        text = '\n'.join(lines)
        if self.details.toPlainText() != text:
            self.details.setPlainText(text)
        from sqlalchemy import select
        from yijianjifa.storage.db import session_scope
        from yijianjifa.storage.models import Setting
        with session_scope(self.service.engine) as session:
            receipts = [json.loads(r.value_json) for r in session.scalars(select(Setting).where(Setting.key.like(f'mix-success:{task.id}:%')))]
        self.outputs.set_records([(r['path'], Path(r['path']).name, '双击预览成品') for r in sorted(receipts, key=lambda r: r['path'])])

    def open_output(self, path):
        self.preview.load(Path(path))

    def reveal_folder(self):
        if self.current_id:
            t = self.service.queue.get(self.current_id)
            path = Path(t.snapshot.get('output_directory', '')) / t.id
            if not reveal_path(path):
                self.status.setText('当前批次尚未创建文件夹。')

    def context_menu(self, point):
        row = self.list.table.rowAt(point.y())
        if row < 0:
            return
        id = self.list.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        targets = self.list.selection.context_targets(id)
        menu = QMenu(self)
        if len(targets) == 1:
            menu.addAction('查看详情', lambda: self.open_task(id))
            menu.addAction('上移', lambda: self.move_task(id, -1))
            menu.addAction('下移', lambda: self.move_task(id, 1))
        menu.addAction('暂停', lambda: self.apply('request_pause', targets))
        menu.addAction('继续', lambda: self.apply('resume', targets))
        menu.addAction('取消剩余', lambda: self.cancel_tasks(targets))
        menu.exec(self.list.table.viewport().mapToGlobal(point))
