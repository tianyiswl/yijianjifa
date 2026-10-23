from PySide6.QtWidgets import QApplication, QTabWidget


def test_launch_opens_library_instead_of_title_placeholder(qapp, monkeypatch, tmp_path):
    from yijianjifa.app.bootstrap import main
    monkeypatch.setenv("YJJF_TEST_DATA_DIR", str(tmp_path / "data"))
    before = set(qapp.topLevelWidgets())
    observed = []
    def inspect_running_window(self):
        windows = [w for w in qapp.topLevelWidgets() if w not in before and w.windowTitle() == "一剪即发"]
        assert windows
        tabs = windows[0].findChild(QTabWidget)
        observed.append(None if tabs is None else [tabs.tabText(i) for i in range(tabs.count())])
        return 0
    monkeypatch.setattr(QApplication, "exec", inspect_running_window)
    try:
        assert main() == 0
        assert observed == [["视频素材", "文案", "背景音乐"]], "Startup must expose the real library, not just a title label"
    finally:
        for widget in qapp.topLevelWidgets():
            if widget not in before:
                widget.close()
