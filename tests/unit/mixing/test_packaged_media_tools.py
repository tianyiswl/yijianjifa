from pathlib import Path
from contextlib import contextmanager
import subprocess

from yijianjifa.features.mixing import ffmpeg_renderer as mod


def test_renderer_uses_resolved_packaged_ffmpeg_and_ffprobe(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mod, "ffmpeg_path", lambda: Path("/bundle/bin/ffmpeg"), raising=False)
    monkeypatch.setattr(mod, "ffprobe_path", lambda: Path("/bundle/bin/ffprobe"), raising=False)

    def fake_run(command, **kwargs):
        calls.append(command)
        if str(command[0]).endswith("ffprobe"):
            return subprocess.CompletedProcess(command, 0, stdout='{"format":{"duration":"1.000"}}', stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # FFmpeg now runs in an independent helper interpreter. Substitute at that
    # boundary; keep this test's resolved tool-path contract, not a parent mock
    # that cannot reach the real helper. Its process behavior is tested separately.
    @contextmanager
    def fake_media(command, output):
        calls.append(command)
        temporary = output.with_name('.p011-render-path-test.mp4')
        temporary.write_bytes(b'complete')
        yield temporary
    monkeypatch.setattr(mod, 'guarded_media', fake_media)
    req = mod.RenderRequest(
        segments=(mod.RenderSegment(tmp_path / "clip.mp4", 0, 1000),),
        output_path=tmp_path / "out.mp4",
        keep_original_audio=False,
    )
    mod.FFmpegRenderer().render(req)

    assert calls[0][0] == str(Path("/bundle/bin/ffmpeg"))
    assert calls[-1][0] == str(Path("/bundle/bin/ffprobe"))
