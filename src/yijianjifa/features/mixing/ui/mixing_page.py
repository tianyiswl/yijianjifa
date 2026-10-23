from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QFormLayout, QGroupBox, QScrollArea,
    QLineEdit, QFileDialog)
from yijianjifa.features.library.ui.categories import CategoryChecks


def combo(items):
    widget = QComboBox()
    for label, value in items:
        widget.addItem(label, value)
    return widget


def seconds(default):
    widget = QDoubleSpinBox()
    widget.setRange(0.1, 3600)
    widget.setDecimals(1)
    widget.setValue(default)
    widget.setSuffix(' 秒')
    return widget


class MixingPage(QWidget):
    tasks_requested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.service = controller.service
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(QLabel('快速制作 · 默认方案'))
        layout.addWidget(QLabel('素材和文案不勾分类时使用全部；口播稿为空则不配音。默认无转场、无背景音乐。'))
        ranges = QHBoxLayout()
        self.materials = CategoryChecks(self.service.engine, 'video')
        self.copies = CategoryChecks(self.service.engine, 'copy')
        for title, checks in (('素材范围（多分类须全部覆盖）', self.materials), ('文案范围', self.copies)):
            group = QGroupBox(title)
            box = QVBoxLayout(group)
            box.addWidget(checks)
            ranges.addWidget(group)
        layout.addLayout(ranges)
        quick = QFormLayout()
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 10000)
        self.quantity.setValue(10)
        quick.addRow('生成数量', self.quantity)
        self.copy_mode = combo([('顺序接着上次', 'sequential'), ('随机优先未用', 'random')])
        quick.addRow('文案使用', self.copy_mode)
        layout.addLayout(quick)
        toggle = QCheckBox('高级设置')
        layout.addWidget(toggle)
        advanced = QWidget()
        form = QFormLayout(advanced)
        self.resolution = combo([('1080p', '1080p'), ('720p', '720p')])
        self.ratio = combo([('竖屏 9:16', '9:16'), ('横屏 16:9', '16:9'), ('方形 1:1', '1:1')])
        self.fit = combo([('保留完整画面 + 模糊背景', 'preserve_blur'), ('裁切填满', 'crop_fill')])
        self.total = seconds(15)
        self.segment = seconds(3)
        self.original = QCheckBox('保留素材原声')
        self.original.setChecked(True)
        self.spoken = QCheckBox('口播字幕（使用系统配音实际句子时间）')
        self.spoken.setChecked(True)
        self.sentence_cut = QCheckBox('按口播句子切画面')
        self.full = QCheckBox('全程字幕')
        self.full_text = QLineEdit()
        self.full_text.setPlaceholderText('本批统一文字；留空使用每条文案的全程字幕')
        self.voice = combo([('系统默认音色', None)])
        voices = QPushButton('读取本机系统音色')
        voices.clicked.connect(self.load_voices)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.5)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(1)
        self.output = QLineEdit(str(self.service.settings.output_directory))
        self.output.setReadOnly(True)
        choose_output = QPushButton('选择以后新成品的保存目录')
        choose_output.clicked.connect(self.choose_output)
        for label, widget in (('画面比例', self.ratio), ('清晰度', self.resolution), ('画面适配', self.fit),
                              ('无口播总长', self.total), ('每段时长', self.segment), ('原声', self.original),
                              ('系统音色', self.voice), ('', voices), ('配音语速', self.speed),
                              ('切画面', self.sentence_cut), ('口播字幕', self.spoken), ('全程字幕', self.full),
                              ('全程文字', self.full_text), ('成品目录', self.output), ('', choose_output)):
            form.addRow(label, widget)
        form.addRow(QLabel('有口播时，总长跟随实际配音。字幕当前采用固定样式；其他高级制作设置仍待接入。'))
        advanced.setVisible(False)
        toggle.toggled.connect(advanced.setVisible)
        layout.addWidget(advanced)
        layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)
        buttons = QHBoxLayout()
        self.generate = QPushButton('一键生成')
        self.generate.clicked.connect(self.submit)
        tasks = QPushButton('查看制作队列')
        tasks.clicked.connect(self.tasks_requested.emit)
        buttons.addWidget(self.generate)
        buttons.addWidget(tasks)
        root.addLayout(buttons)
        self.status = QLabel('只制作本地视频；制作后自动发布尚未接入。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def refresh(self):
        self.materials.refresh()
        self.copies.refresh()

    def load_voices(self):
        try:
            from ..tts.base import system_tts_adapter
            adapter = system_tts_adapter()
            current = self.voice.currentData()
            self.voice.clear()
            self.voice.addItem('系统默认音色', None)
            for voice in adapter.list_voices():
                self.voice.addItem(voice.name, voice.id)
            self.voice.setCurrentIndex(max(0, self.voice.findData(current)))
        except Exception as exc:
            self.status.setText(f'读取音色失败：{exc}')

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择新成品保存位置', self.output.text())
        if path:
            self.service.settings.output_directory = path
            self.output.setText(path)

    def submit(self):
        try:
            task = self.service.submit(quantity=self.quantity.value(), material_categories=self.materials.checked(),
                copy_categories=self.copies.checked(), total_ms=round(self.total.value() * 1000),
                segment_ms=round(self.segment.value() * 1000), resolution=self.resolution.currentData(),
                aspect_ratio=self.ratio.currentData(), frame_fit=self.fit.currentData(), copy_mode=self.copy_mode.currentData(),
                keep_original_audio=self.original.isChecked(), voice=self.voice.currentData(), speed=self.speed.value(),
                sentence_cut=self.sentence_cut.isChecked(), spoken_enabled=self.spoken.isChecked(),
                full_enabled=self.full.isChecked(), batch_full_text=self.full_text.text())
            warning = ''
            if self.full.isChecked() and not self.full_text.text().strip() and any(not c['full'] for c in task.snapshot['copies']):
                warning = ' 部分文案没有全程字幕文字，这些成品不加全程字幕。'
            self.status.setText(f'已加入制作队列：{task.quantity} 条。设置与文案已固定，可继续创建下一批。' + warning)
            self.controller.changed.emit()
        except Exception as exc:
            self.status.setText(f'未创建任务：{exc}')
