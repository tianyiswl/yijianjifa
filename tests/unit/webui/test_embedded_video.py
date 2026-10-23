"""Native preview remains a clipped child of the existing application window."""
from pathlib import Path
import pytest
from PySide6.QtWidgets import QWidget,QApplication
from PySide6.QtCore import QRect,Qt
from yijianjifa.app_services.media_access import MediaAccess


def test_native_preview_is_embedded_clipped_and_closes_its_granted_device(qtbot,tmp_path):
    from yijianjifa.webui.qt_video import NativeVideoPanel
    parent=QWidget();parent.resize(800,600);qtbot.addWidget(parent);parent.show()
    path=tmp_path/'fixture.mp4';path.write_bytes(b'controlled invalid media')
    media=MediaAccess(None,None,native_video=True)
    token=media.grant_read(path,name='fixture',kind='video',owner='library')['url'].rsplit('/',1)[1]
    windows=set(QApplication.topLevelWidgets())
    panel=NativeVideoPanel(media,token,parent)
    surface={'rect':[30,40,320,180],'clip':[30,70,320,150],'visible':True}
    panel.place(surface,zoom=1.5)
    assert panel.parentWidget() is parent and not panel.isWindow()
    assert set(QApplication.topLevelWidgets())==windows
    assert panel.geometry()==QRect(45,60,480,270)
    assert panel.mask().boundingRect()==QRect(0,45,480,225)
    panel.toggle_expanded()
    assert panel.geometry()==parent.rect() and not panel.isWindow()
    assert set(QApplication.topLevelWidgets())==windows
    qtbot.keyClick(panel,Qt.Key.Key_Escape)
    assert panel.geometry()==QRect(45,60,480,270)
    assert panel.stream.isOpen()
    panel.place({**surface,'visible':False},zoom=1.5)
    assert panel.isHidden() and panel.stream.isOpen()
    panel.place(surface,zoom=1.5)
    assert not panel.isHidden()
    panel.close()
    assert not panel.stream.isOpen()
    media.registry.close()


@pytest.mark.parametrize('surface',[
    {},{'rect':[0,0,float('nan'),20],'clip':[0,0,20,20],'visible':True},
    {'rect':[0,0,20,20],'clip':[0,0,30,20],'visible':True},
    {'rect':[0,0,20,20],'clip':[0,0,20,20],'visible':1},
    {'rect':[0,0,20,20],'clip':[0,0,20,20],'visible':True,'path':'private'},
])
def test_invalid_native_surfaces_cannot_control_application_geometry(surface):
    from yijianjifa.app_services.media_access import native_video_surface
    with pytest.raises(ValueError):native_video_surface(surface)
