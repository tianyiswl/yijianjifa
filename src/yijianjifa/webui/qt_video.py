"""Native video preview from an existing read grant; no page-supplied paths."""
import os
from PySide6.QtCore import QFile,QIODevice,QUrl,Qt,QRect,QEvent
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget,QVBoxLayout,QPushButton
from yijianjifa.features.library.ui.widgets import MediaPreview
from yijianjifa.app_services.media_access import native_video_surface

class NativeVideoPanel(QWidget):
    def __init__(self,media,token,parent):
        super().__init__(parent)
        self.token=token
        self.expanded=False;self._surface=None;self._zoom=1.0
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName('p011NativeVideo')
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.preview=MediaPreview(self);layout.addWidget(self.preview)
        self.preview.layout().setContentsMargins(0,0,0,0)
        self.preview.video.setMinimumSize(0,0);self.preview.error.hide()
        self.preview.player.errorOccurred.connect(lambda *_:self.preview.error.show())
        self.expand_button=QPushButton('放大',self)
        self.preview.layout().itemAt(1).layout().addWidget(self.expand_button)
        self.expand_button.clicked.connect(self.toggle_expanded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus);parent.installEventFilter(self)
        self.stream=QFile(self);fd=media.registry.duplicate(token)
        if not self.stream.open(fd,QIODevice.OpenModeFlag.ReadOnly,QFile.FileHandleFlag.AutoCloseHandle):
            os.close(fd);raise ValueError('video grant unavailable')
        self.preview.player.setSourceDevice(self.stream,QUrl('file:///preview.mp4'))

    def place(self,surface,*,zoom=1.0):
        surface=native_video_surface(surface)
        self._surface,self._zoom=surface,zoom
        def rect(values):return QRect(*(round(v*zoom) for v in values))
        bounds=self.parentWidget().rect() if self.expanded else rect(surface['rect'])
        clip=bounds if self.expanded else rect(surface['clip']).intersected(bounds).intersected(self.parentWidget().rect())
        self.setGeometry(bounds)
        if not surface['visible'] or clip.isEmpty():self.hide();return
        self.setMask(QRegion(clip.translated(-bounds.topLeft())))
        self.show();self.raise_()

    def toggle_expanded(self):
        if self._surface is None:return
        self.expanded=not self.expanded
        self.expand_button.setText('返回预览' if self.expanded else '放大')
        self.place(self._surface,zoom=self._zoom)
        (self if self.expanded else self.parentWidget()).setFocus()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Resize and self.expanded and self._surface is not None:
            self.place(self._surface,zoom=self._zoom)
        return super().eventFilter(watched,event)

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape and self.expanded:self.toggle_expanded();event.accept()
        else:super().keyPressEvent(event)

    def closeEvent(self,event):
        self.preview.stop();self.preview.player.setSource(QUrl());self.stream.close()
        super().closeEvent(event)
