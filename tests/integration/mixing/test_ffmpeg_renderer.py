from __future__ import annotations

import json
from pathlib import Path
import subprocess
import pytest

from yijianjifa.features.mixing.ffmpeg_renderer import FFmpegRenderer, RenderRequest, RenderSegment


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def test_renderer_produces_target_duration_audio_and_1080p(tmp_path):
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "out.mp4"
    _run("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2:r=25", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip_a))
    _run("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=240x320:d=2:r=25", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip_b))
    _run("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:a", "pcm_s16le", str(voice))

    request = RenderRequest(
        segments=(
            RenderSegment(clip_a, 0, 2000),
            RenderSegment(clip_b, 0, 2000),
            RenderSegment(clip_a, 500, 1000),
        ),
        output_path=output,
        aspect_ratio="9:16",
        resolution="1080p",
        frame_fit="preserve_blur",
        voice_path=voice,
        keep_original_audio=False,
    )
    result = FFmpegRenderer().render(request)
    assert result.output_path == output
    assert output.exists()

    probe = _probe(output)
    duration = float(probe["format"]["duration"])
    assert abs(duration - 5.0) <= 0.08
    videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audios = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert (videos[0]["width"], videos[0]["height"]) == (1080, 1920)
    assert audios


def test_transition_with_looped_music_keeps_duration_voice_and_fade(tmp_path):
    import array, math
    from yijianjifa.os_integration.resources import ffmpeg_path
    ff = str(ffmpeg_path())
    a,b,voice,music = [tmp_path/n for n in ('red.mp4','blue.mp4','voice.wav','music.wav')]
    for path,color in ((a,'red'),(b,'blue')):
        _run(ff,'-y','-v','error','-f','lavfi','-i',f'color=c={color}:s=160x120:r=25:d=1.2','-c:v','libx264',str(path))
    for path,freq,duration in ((voice,440,3),(music,1000,.4)):
        _run(ff,'-y','-v','error','-f','lavfi','-i',f'sine=frequency={freq}:duration={duration}','-c:a','pcm_s16le',str(path))
    for mode in ('fixed','random'):
        output=tmp_path/f'{mode}.mp4'
        FFmpegRenderer().render(RenderRequest(segments=(RenderSegment(a,0,1000),RenderSegment(b,0,1000),RenderSegment(a,0,1000)),output_path=output,resolution='720p',aspect_ratio='16:9',keep_original_audio=False,voice_path=voice,music_path=music,voice_gain=.8,music_gain=.5,transition_mode=mode,transition_name='slideleft',transition_duration_ms=300,transition_seed=4))
        info=_probe(output)
        assert abs(float(info['format']['duration'])-3)<=.08
        _run(ff,'-v','error','-i',str(output),'-f','null','-')
        raw=subprocess.run([ff,'-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','f32le','-'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
        samples=array.array('f');samples.frombytes(raw)
        def amplitude(freq,start):
            chunk=samples[round(start*48000):round((start+.1)*48000)]
            return 2*abs(sum(x*complex(math.cos(2*math.pi*freq*i/48000),math.sin(2*math.pi*freq*i/48000)) for i,x in enumerate(chunk)))/len(chunk)
        # Music originally lasts .4s; it must still sound at 1.4s, then fade at the end.
        assert amplitude(1000,1.4)>.01
        assert amplitude(1000,2.85)<amplitude(1000,1.4)*.3
        # Voice remains present across joins and through the music fade.
        assert amplitude(440,1.4)>.02
        assert amplitude(440,2.85)>.02


@pytest.mark.parametrize('durations,target', [([121]*20+[155],2.575),([120,120,1],.241)])
def test_non_frame_aligned_segments_keep_total_video_length_without_audio(tmp_path,durations,target):
    from yijianjifa.os_integration.resources import ffmpeg_path
    ff=str(ffmpeg_path());source=tmp_path/'timing.mp4';output=tmp_path/'timing-out.mp4'
    _run(ff,'-y','-v','error','-f','lavfi','-i','color=c=red:s=160x120:r=25:d=1','-c:v','libx264',str(source))
    FFmpegRenderer().render(RenderRequest(segments=tuple(RenderSegment(source,0,d) for d in durations),output_path=output,resolution='720p',aspect_ratio='16:9',keep_original_audio=False))
    video=next(s for s in _probe(output)['streams'] if s['codec_type']=='video')
    assert abs(float(video['duration'])-target)<=.04


def test_random_transition_range_regression_keeps_actual_video_stream_length(tmp_path):
    from yijianjifa.os_integration.resources import ffmpeg_path
    ff=str(ffmpeg_path());source=tmp_path/'range.mp4';output=tmp_path/'range-out.mp4'
    _run(ff,'-y','-v','error','-f','lavfi','-i','color=c=red:s=160x120:r=25:d=1','-c:v','libx264',str(source))
    FFmpegRenderer().render(RenderRequest(segments=tuple(RenderSegment(source,0,d) for d in [537,574,680,407,100]),output_path=output,resolution='720p',aspect_ratio='16:9',keep_original_audio=True,transition_mode='random',transition_duration_ms=500,transition_seed=328062563))
    video=next(s for s in _probe(output)['streams'] if s['codec_type']=='video')
    assert abs(float(video['duration'])-2.298)<=.04
