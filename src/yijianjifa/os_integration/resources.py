from __future__ import annotations

from pathlib import Path
import shutil
import sys


def _resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(str(bundled))
    return Path(__file__).resolve().parents[3] / "packaging" / "resources"


def resource_path(relative: str | Path) -> Path:
    return (_resource_root() / Path(relative)).resolve()


def _tool_path(name: str) -> Path:
    bundled = resource_path(Path("bin") / name)
    if getattr(sys, "_MEIPASS", None) or bundled.exists():
        return bundled
    found = shutil.which(name)
    return Path(found) if found else bundled


def ffmpeg_path() -> Path:
    name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    bundled = resource_path(Path("bin") / name)
    if getattr(sys, "_MEIPASS", None) or bundled.exists():
        return bundled
    return _development_ffmpeg() or _tool_path(name)


def _development_ffmpeg() -> Path | None:
    # Optional dev runtime includes libass; a system FFmpeg may omit subtitles.
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return Path(get_ffmpeg_exe())
    except (ImportError, RuntimeError, OSError):
        return None


def ffprobe_path() -> Path:
    name = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
    return _tool_path(name)


def whisper_cli_path() -> Path:
    """Recognition uses only the explicit bundled tool, never PATH."""
    name = 'whisper-cli.exe' if sys.platform.startswith('win') else 'whisper-cli'
    return resource_path(Path('bin') / name)


def recognition_lock_path() -> Path:
    if getattr(sys, '_MEIPASS', None): return resource_path('recognition-lock.json')
    return _resource_root().parent / 'recognition-lock.json'


def playwright_browser_path() -> Path:
    executable = "chrome.exe" if sys.platform.startswith("win") else "chrome"
    return resource_path(Path("playwright") / "chromium" / executable)
