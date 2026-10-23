from __future__ import annotations

from pathlib import Path
import platform
import subprocess


def reveal_path(path: str | Path, *, dry_run: bool = False) -> bool:
    target = Path(path).expanduser()
    if not target.exists():
        return False
    if dry_run:
        return True

    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["explorer", "/select,", str(target)])
    elif system == "Darwin":
        subprocess.Popen(["open", "-R", str(target)])
    else:
        # Useful for Linux development without changing Windows/macOS behavior.
        subprocess.Popen(["xdg-open", str(target.parent)])
    return True
