"""Source-runtime invocation of the same video check run inside the ZIP."""
import tempfile
import os
from pathlib import Path
import imageio_ffmpeg
from yijianjifa.webui.native_video_check import check
with tempfile.TemporaryDirectory() as tmp:
    report=Path(os.environ.get('P011_NATIVE_VIDEO_REPORT',str(Path(tmp)/'video-check.json')))
    report.parent.mkdir(parents=True,exist_ok=True)
    raise SystemExit(check(report,imageio_ffmpeg.get_ffmpeg_exe()))
