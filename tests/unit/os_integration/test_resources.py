from pathlib import Path
import sys
import pytest


def test_resource_path_uses_bundle_root_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    from yijianjifa.os_integration.resources import resource_path

    assert resource_path("bin/ffmpeg").parent == tmp_path / "bin"


def test_resource_path_uses_project_packaging_resources_in_source(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    from yijianjifa.os_integration.resources import resource_path

    path = resource_path("THIRD_PARTY_NOTICES.txt")
    assert path.name == "THIRD_PARTY_NOTICES.txt"
    assert path.parent.name == "resources"


def test_ffmpeg_and_ffprobe_names_follow_platform(monkeypatch):
    from yijianjifa.os_integration import resources

    monkeypatch.setattr(resources, "_development_ffmpeg", lambda: None)
    monkeypatch.setattr(resources.sys, "platform", "win32")
    monkeypatch.setattr(resources.shutil, "which", lambda name: None)
    assert resources.ffmpeg_path().name == "ffmpeg.exe"
    assert resources.ffprobe_path().name == "ffprobe.exe"


@pytest.mark.parametrize('platform,ffmpeg,ffprobe', [
    ('darwin', 'ffmpeg', 'ffprobe'),
    ('linux', 'ffmpeg', 'ffprobe'),
    ('win32', 'ffmpeg.exe', 'ffprobe.exe'),
])
def test_source_tool_path_falls_back_to_system_binary_when_bundle_copy_is_absent(monkeypatch, tmp_path, platform, ffmpeg, ffprobe):
    from yijianjifa.os_integration import resources

    monkeypatch.delattr(resources.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(resources.sys, 'platform', platform)
    monkeypatch.setattr(resources, "resource_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(resources, "_development_ffmpeg", lambda: None)
    monkeypatch.setattr(resources.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert resources.ffmpeg_path() == Path('/usr/local/bin') / ffmpeg
    assert resources.ffprobe_path() == Path('/usr/local/bin') / ffprobe


def test_source_runtime_prefers_development_ffmpeg_with_subtitle_support(monkeypatch, tmp_path):
    from yijianjifa.os_integration import resources
    monkeypatch.delattr(resources.sys, '_MEIPASS', raising=False)
    monkeypatch.setattr(resources, 'resource_path', lambda rel: tmp_path / rel)
    monkeypatch.setattr(resources, '_development_ffmpeg', lambda: Path('/dev-runtime/ffmpeg'))
    assert resources.ffmpeg_path() == Path('/dev-runtime/ffmpeg')
