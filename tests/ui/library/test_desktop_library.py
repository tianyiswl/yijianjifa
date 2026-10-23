from pathlib import Path
import wave

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog

from yijianjifa.features.library.copy_service import CopyDraft, CopyRepository
from yijianjifa.features.library.excel_import import export_with_ids
from yijianjifa.features.library.media_storage import MediaStorageService


@pytest.fixture
def window(qtbot, db_engine, app_paths):
    from yijianjifa.app.main_window import MainWindow
    widget = MainWindow(db_engine, app_paths)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def draft(repo, id, title):
    return repo.create(id=id, title=title, voice="", body="正文", tags="标签", full="字幕")


def answer_dialog(text):
    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        matches = [b for b in dialog.buttons() if b.text() == text]
        assert len(matches) == 1
        matches[0].click()
    QTimer.singleShot(30, choose)


def test_category_filter_assignment_and_delete_preserve_media(window, tmp_path):
    pane = window.library.video
    category = pane.categories.repository.create("video", "分类甲")
    pane.categories.refresh()
    path = tmp_path / "category.mp4"
    path.write_bytes(b"category fixture")
    asset = pane.service.import_media(path, keep_copy=False, category_ids=[])
    pane.refresh()
    pane.selection.select(asset.id)
    pane.categories.set_checked({category.id})
    pane.categories.assign_button.click()
    assert pane.service.category_ids(asset.id) == {category.id}
    assert pane.table.rowCount() == 1
    pane.categories.repository.delete(category.id)
    pane.categories.refresh()
    assert pane.service.get(asset.id) is not None
    assert path.exists()


def test_task_reference_delete_is_reported_in_ui_without_losing_record(window, tmp_path, db_engine):
    from yijianjifa.features.mixing.queue import MixQueue
    pane = window.library.video
    path = tmp_path / "protected.mp4"
    path.write_bytes(b"protected")
    asset = pane.service.import_media(path, keep_copy=False, category_ids=[])
    MixQueue(db_engine, None).enqueue({"quantity": 1, "asset_id": asset.id})
    answer_dialog("删除记录")
    pane.delete_records({asset.id})
    assert pane.service.get(asset.id) is not None
    assert "任务" in pane.status.text()


def test_import_button_uses_real_files_and_reuses_content_identity(window, qtbot, tmp_path, monkeypatch):
    pane = window.library.video
    original = tmp_path / "原始 视频.mp4"
    renamed = tmp_path / "改名.mp4"
    original.write_bytes(b"same-video")
    renamed.write_bytes(b"same-video")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(original), str(renamed)], ""))
    with qtbot.waitSignal(pane.import_finished, timeout=10000):
        qtbot.mouseClick(pane.import_button, Qt.MouseButton.LeftButton)
    records = pane.service.list_assets()
    assert len(records) == 1
    assert records[0].managed_path is None
    assert original.read_bytes() == b"same-video"
    assert "2" in pane.status.text()


def test_preferences_are_separate_and_survive_reopening(window, db_engine):
    from yijianjifa.features.library.preferences import LibraryPreferences
    assert not window.library.video.keep_copy.isChecked()
    assert not window.library.music.keep_copy.isChecked()
    window.library.video.keep_copy.setChecked(True)
    prefs = LibraryPreferences(db_engine)
    assert prefs.keep_copy("video") is True
    assert prefs.keep_copy("music") is False


def test_select_all_includes_other_pages_and_clear_respects_filter(window, tmp_path):
    pane = window.library.video
    for i in range(57):
        path = tmp_path / f"镜头-{i:02}.mp4"
        path.write_bytes(str(i).encode())
        pane.service.import_media(path, keep_copy=False, category_ids=[])
    pane.refresh()
    assert pane.table.rowCount() < 57
    pane.select_all_button.click()
    assert pane.selection.count == 57
    pane.search.setText("镜头-0")
    pane.clear_all_button.click()
    assert pane.selection.count == 47
    pane.search.clear()
    pane.next_button.click()
    assert pane.table.rowCount() == 7
    assert pane.table.item(0, 0).checkState() == Qt.CheckState.Checked


def test_rename_changes_display_only_and_delete_keeps_original(window, tmp_path):
    pane = window.library.video
    path = tmp_path / "source.mp4"
    path.write_bytes(b"protected original")
    record = pane.service.import_media(path, keep_copy=False, category_ids=[])
    pane.refresh()
    pane.rename_record(record.id, "展示名称")
    assert path.exists()
    assert pane.service.get(record.id).display_name == "展示名称"
    pane.selection.select(record.id)
    answer_dialog("删除记录")
    pane.delete_button.click()
    assert pane.service.get(record.id) is None
    assert path.read_bytes() == b"protected original"
    assert pane.service.history.identity_seen(record.content_hash)


def test_new_copy_saves_all_fields_with_generated_id(window, qtbot):
    pane = window.library.copy
    window.library.tabs.setCurrentWidget(pane)
    pane.new_button.click()
    pane.fields["title"].setPlainText("新标题")
    pane.fields["body"].setPlainText("新正文")
    pane.fields["full"].setPlainText("全程文字")
    pane.save_button.click()
    records = pane.repository.list_all()
    assert len(records) == 1
    assert records[0].id and records[0].title == "新标题"
    assert records[0].body == "新正文"
    assert records[0].voice == ""
    assert records[0].full == "全程文字"


@pytest.mark.parametrize("decision, expected_id, expected_title", [
    ("继续编辑", "A", "原始"),
    ("放弃修改", "B", "原始"),
    ("保存并继续", "B", "已修改"),
])
def test_copy_switch_obeys_three_way_decision(window, decision, expected_id, expected_title):
    pane = window.library.copy
    draft(pane.repository, "A", "原始")
    draft(pane.repository, "B", "另一个")
    window.library.tabs.setCurrentWidget(pane)
    pane.open_record("A")
    pane.fields["title"].setPlainText("已修改")
    answer_dialog(decision)
    pane.open_record("B")
    assert pane.current_id == expected_id
    assert pane.repository.get("A").title == expected_title
    if decision == "继续编辑":
        assert pane.fields["title"].toPlainText() == "已修改"
        pane.fields["title"].setPlainText("原始")


def test_dirty_editor_cannot_be_discarded_by_closing_window(window):
    pane = window.library.copy
    draft(pane.repository, "A", "原始")
    window.library.tabs.setCurrentWidget(pane)
    pane.open_record("A")
    pane.fields["title"].setPlainText("未保存")
    answer_dialog("继续编辑")
    assert window.close() is False
    assert window.isVisible()
    assert pane.fields["title"].toPlainText() == "未保存"
    pane.save_button.click()
    assert window.close() is True


def test_dirty_new_copy_cancel_tab_change_preserves_draft(window):
    pane = window.library.copy
    window.library.tabs.setCurrentWidget(pane)
    pane.new_button.click()
    pane.fields["title"].setPlainText("尚未保存的新文案")
    answer_dialog("继续编辑")
    window.library.tabs.setCurrentWidget(window.library.music)
    assert window.library.tabs.currentWidget() is pane
    assert pane.fields["title"].toPlainText() == "尚未保存的新文案"
    pane.save_button.click()


def test_excel_updates_require_confirmation_and_export_preserves_ids(window, tmp_path):
    pane = window.library.copy
    row = draft(pane.repository, "A", "原始")
    pane.repository.update(CopyDraft("A", "更新标题", "", "正文", "标签", "字幕"))
    source = export_with_ids(pane.repository, ["A"], tmp_path / "有编号.xlsx")
    pane.repository.update(row)
    pane.preview_excel(source)
    assert pane.repository.get("A").title == "原始"
    assert len(pane.import_preview.updates) == 1
    pane.apply_excel(approve_updates=False)
    assert pane.repository.get("A").title == "原始"
    pane.preview_excel(source)
    pane.apply_excel(approve_updates=True)
    assert pane.repository.get("A").title == "更新标题"


def test_music_preview_loads_real_audio_and_stops_on_tab_change(window, tmp_path, qtbot):
    pane = window.library.music
    path = tmp_path / "试听.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    record = pane.service.import_media(path, keep_copy=False, category_ids=[])
    window.library.tabs.setCurrentWidget(pane)
    pane.open_record(record.id)
    qtbot.waitUntil(lambda: pane.preview.player.duration() > 0, timeout=10000)
    assert Path(pane.preview.player.source().toLocalFile()) == path
    window.library.tabs.setCurrentWidget(window.library.video)
    from PySide6.QtMultimedia import QMediaPlayer
    assert pane.preview.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState


def test_context_menu_on_unselected_item_does_not_use_other_selection(window, tmp_path):
    pane = window.library.video
    records = []
    for i in range(3):
        path = tmp_path / f"{i}.mp4"
        path.write_bytes(str(i).encode())
        records.append(pane.service.import_media(path, keep_copy=False, category_ids=[]))
    pane.refresh()
    pane.selection.select(records[0].id)
    pane.selection.select(records[1].id)
    assert pane.context_targets(records[2].id) == {records[2].id}
    assert pane.context_targets(records[0].id) == {records[0].id, records[1].id}
